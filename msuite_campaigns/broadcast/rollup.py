# Copyright (c) 2026, MSuite and contributors
# For license information, please see license.txt

"""Rollup and metrics calculation for Broadcast channels and Marketing Campaigns.

Calculates aggregated delivery and engagement statistics for single Broadcast channels
as well as whole Marketing Campaigns, across both Standard (MariaDB counters) and
AWS (DynamoDB analytics) engines with graceful degradation on network failures.
"""

from __future__ import annotations

import frappe
from frappe import _

from msuite_campaigns.aws_pipeline import rollup as aws_rollup
from msuite_campaigns.broadcast.aws import dynamo_campaign_id


def channel_metrics(broadcast: str, channel: str, days: int = 30) -> dict:
	"""Compute aggregated delivery metrics for a single broadcast channel.

	Standard-engine rows read counters directly from the Broadcast Channel child row.
	AWS-engine rows query DynamoDB via aws_pipeline.rollup.fetch, soft-failing to zeros
	on provider/network errors so that dashboards do not 500.

	Args:
		broadcast: Name of the Broadcast document.
		channel: Channel name ("WhatsApp", "Email", or "SMS").
		days: Lookback window in days (kept for parity; DynamoDB SUMMARY is running total).

	Returns:
		dict: Metric payload mirroring aws_pipeline.rollup.rollup() shape plus 'engine':
			{
				"channel": str,
				"label": str,
				"blasts": int,
				"metrics": {
					"sent": int,
					"delivered": int,
					"failed": int,
					"audience": int,
					"credits": float,
					"delivery_rate": float,
					"read": int (WhatsApp only)
				},
				"engine": "standard" | "aws",
				"stale": bool,
				"retried": int
			}

	Raises:
		frappe.ValidationError: If the Broadcast document or Channel row is not found.

	Side effects:
		None.
	"""
	broadcast_doc = frappe.get_doc("Broadcast", broadcast)
	channel_row = None
	for ch in broadcast_doc.channels or []:
		if ch.channel == channel:
			channel_row = ch
			break

	if not channel_row:
		frappe.throw(_("Channel '{0}' not found on Broadcast '{1}'.").format(channel, broadcast))

	engine = (channel_row.delivery_engine or "AWS").lower()
	audience = int(channel_row.recipient_count or 0)
	credits_val = float(channel_row.actual_credits or channel_row.estimated_credits or 0)

	if engine == "standard":
		sent = int(channel_row.sent_count or 0)
		delivered = int(channel_row.delivered_count or 0)
		failed = int(channel_row.failed_count or 0)
		read = int(channel_row.read_count or 0)
		retried = int(channel_row.retry_count or 0)
		delivery_rate = round(delivered / sent * 100, 1) if sent else 0

		metrics = {
			"sent": sent,
			"delivered": delivered,
			"failed": failed,
			"audience": audience,
			"credits": credits_val,
			"delivery_rate": delivery_rate,
		}
		if channel.lower() == "whatsapp":
			metrics["read"] = read

		return {
			"channel": channel,
			"label": channel,
			"blasts": 1,
			"metrics": metrics,
			"engine": "standard",
			"stale": False,
			"retried": retried,
		}

	# AWS engine
	campaign_id = dynamo_campaign_id(broadcast_doc, channel)
	data = None
	try:
		data = aws_rollup.fetch(campaign_id)
	except Exception:
		frappe.log_error(
			title=f"Broadcast AWS analytics read failed for {campaign_id}",
			message=frappe.get_traceback(),
		)
		data = None

	reachable = data is not None
	data_dict = data or {}
	totals = {
		"sent": int(data_dict.get("sent_count") or 0),
		"delivered": int(data_dict.get("delivered_count") or 0),
		"failed": int(data_dict.get("failed_count") or 0),
		"read": int(data_dict.get("read_count") or 0),
		"retried": int(data_dict.get("retried_count") or 0),
	}

	sent = totals["sent"]
	delivered = totals["delivered"]
	failed = totals["failed"]
	retried = totals["retried"]
	delivery_rate = round(delivered / sent * 100, 1) if sent else 0

	metrics = {
		"sent": sent,
		"delivered": delivered,
		"failed": failed,
		"audience": audience,
		"credits": credits_val,
		"delivery_rate": delivery_rate,
	}
	if channel.lower() == "whatsapp":
		metrics["read"] = totals["read"]

	return {
		"channel": channel,
		"label": channel,
		"blasts": 1,
		"metrics": metrics,
		"engine": "aws",
		"stale": not reachable,
		"retried": retried,
	}


def _combine_channel_metrics(
	existing: dict, incoming: dict, deduplicate_aws: bool = False
) -> dict:
	"""Sum metrics of two rollups for the same channel, recomputing delivery_rate.

	Args:
		existing: Base channel metric dict.
		incoming: Incoming channel metric dict to merge.
		deduplicate_aws: If True, incoming AWS delivery counters (sent, delivered, etc.)
			are not added because they share the same DynamoDB counter; only audience,
			credits, and blast count are incremented.

	Returns:
		dict: Merged channel metrics.

	Exceptions:
		None.

	Side effects:
		None.
	"""
	em = existing.get("metrics") or {}
	im = incoming.get("metrics") or {}
	merged_metrics = dict(em)

	if deduplicate_aws:
		# Audience and credits come from MariaDB and should always be summed
		for key in ("audience", "credits"):
			if key in em or key in im:
				merged_metrics[key] = (em.get(key) or 0) + (im.get(key) or 0)
	else:
		for key in ("sent", "delivered", "failed", "audience", "credits", "read"):
			if key in em or key in im:
				merged_metrics[key] = (em.get(key) or 0) + (im.get(key) or 0)

	sent = merged_metrics.get("sent") or 0
	merged_metrics["delivery_rate"] = (
		round((merged_metrics.get("delivered") or 0) / sent * 100, 1) if sent else 0
	)

	existing_engine = existing.get("engine")
	incoming_engine = incoming.get("engine")
	if existing_engine != incoming_engine:
		engine = "mixed"
	else:
		engine = existing_engine

	retried_delta = 0 if deduplicate_aws else (incoming.get("retried") or 0)

	return {
		**existing,
		"blasts": (existing.get("blasts") or 0) + (incoming.get("blasts") or 0),
		"metrics": merged_metrics,
		"engine": engine,
		"stale": bool(existing.get("stale") or incoming.get("stale")),
		"retried": (existing.get("retried") or 0) + retried_delta,
	}


def campaign_metrics(campaign: str, days: int = 30) -> dict[str, dict]:
	"""Return merged channel metrics over all Broadcast Channel rows for a Marketing Campaign.

	Iterates every Broadcast linked to the given campaign, computes or aggregates metrics
	for each channel, deduplicating DynamoDB queries where appropriate, and merges results
	into a single dictionary keyed by channel name.

	Args:
		campaign: Name of the Marketing Campaign document.
		days: Lookback window in days (defaults to 30).

	Returns:
		dict[str, dict]: Mapping of channel name to merged metrics dictionary.

	Exceptions:
		None.

	Side effects:
		None.
	"""
	broadcast_names = frappe.get_all(
		"Broadcast",
		filters={"campaign": campaign},
		pluck="name",
	)

	results: dict[str, dict] = {}
	# Track AWS dynamo keys already fetched for this campaign to prevent double-counting
	seen_aws_dynamo_keys: set[str] = set()

	for b_name in broadcast_names:
		broadcast_doc = frappe.get_doc("Broadcast", b_name)
		for ch in broadcast_doc.channels or []:
			channel_name = ch.channel
			if not channel_name:
				continue

			engine = (ch.delivery_engine or "AWS").lower()
			dynamo_id = dynamo_campaign_id(broadcast_doc, channel_name)

			dedup_aws = False
			if engine == "aws":
				if dynamo_id in seen_aws_dynamo_keys:
					dedup_aws = True
				else:
					seen_aws_dynamo_keys.add(dynamo_id)

			m = channel_metrics(b_name, channel_name, days=days)

			if channel_name not in results:
				results[channel_name] = m
			else:
				results[channel_name] = _combine_channel_metrics(
					results[channel_name], m, deduplicate_aws=dedup_aws
				)

	return results

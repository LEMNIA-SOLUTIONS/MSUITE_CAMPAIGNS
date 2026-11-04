# Copyright (c) 2026, MSuite and contributors
# For license information, please see license.txt

"""
Dashboard numbers for AWS-routed blasts.

AWS-routed sends never write per-recipient status back to MariaDB — that is
the load the engine exists to remove — so the channel adapters' `rollup()`
would report a campaign full of permanently "Queued" recipients. This module
sources those numbers from DynamoDB instead, **through the provider**: the
client has no API Gateway URL and no analytics key.

The returned shape is byte-identical to what the channel adapters already
return, so `campaign/rollup.py` keeps its single whitelisted entry point and
the SPA renders it unchanged — no new RPC, no frontend work.

Failure is soft on purpose. A dashboard is a read; if the provider or the
analytics API is unreachable, the marketer should see the rest of their
campaign with one channel showing zeros, not a 500.
"""

from __future__ import annotations

import frappe

ANALYTICS_ENDPOINT = "msuite.api.v1.bulk_relay.campaign_analytics"

# Which bulk doctype belongs to which adapter key, and the label the adapter
# uses. Mirrors the adapters' own `rollup()` output so the two are
# indistinguishable downstream.
_CHANNEL = {
	"email": ("Bulk Email Message", "Email"),
	"sms": ("Bulk SMS Message", "SMS"),
	"whatsapp": ("Bulk WhatsApp Message", "WhatsApp"),
}


def aws_blasts(campaign: str, channel: str) -> list[str]:
	"""AWS-routed blasts for one channel of one campaign."""
	spec = _CHANNEL.get(channel)
	if not spec:
		return []
	doctype = spec[0]
	if not frappe.db.exists("DocType", doctype):
		return []
	if not frappe.get_meta(doctype).has_field("delivery_engine"):
		return []
	return frappe.get_all(
		doctype,
		filters={"campaign": campaign, "delivery_engine": "AWS", "docstatus": 1},
		pluck="name",
	)


def dynamo_key(campaign: str, channel: str) -> str:
	"""The one place this composite key format is spelled out.

	Mirrors `manifest._dynamo_campaign_id` exactly -- every reader of the
	analytics table must use this, not `campaign` or a blast's own `name`
	alone, or it silently reads a different (usually all-zero) counter.
	See that function's docstring for why the key has a channel suffix at
	all.
	"""
	return f"{campaign}__{channel}"


def fetch(campaign_id: str) -> dict | None:
	"""One campaign's counters from the analytics API, or None if unavailable."""
	from msuite_campaigns.aws_pipeline.client import _call_provider

	try:
		return _call_provider(ANALYTICS_ENDPOINT, {"campaign_id": campaign_id})
	except Exception:
		frappe.log_error(
			title=f"AWS analytics read failed for {campaign_id}",
			message=frappe.get_traceback(),
		)
		return None


def rollup(campaign: str, channel: str, days: int = 30) -> dict | None:
	"""Adapter-shaped stats for a channel's AWS-routed blasts.

	Returns None when this channel has no AWS-routed blast in this campaign —
	the caller then leaves the adapter's own rollup alone.

	`days` is accepted for signature parity with the adapters and ignored: the
	SUMMARY item is a running total for the campaign, not a windowed series.
	Windowing it would mean scanning recipient rows, which is the read this
	table exists to avoid.
	"""
	blasts = aws_blasts(campaign, channel)
	if not blasts:
		return None

	label = _CHANNEL[channel][1]

	# The analytics table's SUMMARY item is keyed by campaign_id alone -- it
	# has no channel dimension (aws-bulk-messaging/src/common/python/dynamo.py).
	# Every AWS-routed blast for this channel+campaign already writes into
	# ONE counter under `dynamo_key()` -- read it once here, not once per
	# blast, or a campaign with two same-channel blasts would double the
	# real total.
	data = fetch(dynamo_key(campaign, channel))
	reachable = data is not None
	totals = {
		"sent": int((data or {}).get("sent_count") or 0),
		"delivered": int((data or {}).get("delivered_count") or 0),
		"failed": int((data or {}).get("failed_count") or 0),
		"read": int((data or {}).get("read_count") or 0),
		"retried": int((data or {}).get("retried_count") or 0),
	}

	audience = _audience(campaign, channel, blasts)

	metrics = {
		"sent": totals["sent"],
		"delivered": totals["delivered"],
		"failed": totals["failed"],
		"audience": audience,
		"credits": 0,
		"delivery_rate": (
			round(totals["delivered"] / totals["sent"] * 100, 1) if totals["sent"] else 0
		),
	}
	if channel == "whatsapp":
		metrics["read"] = totals["read"]

	return {
		"channel": channel,
		"label": label,
		"blasts": len(blasts),
		"metrics": metrics,
		# Distinguishes "nothing sent yet" from "we could not ask". Without it
		# a broken analytics read is indistinguishable from a campaign that
		# genuinely has zero sends.
		"engine": "aws",
		"stale": not reachable,
		"retried": totals["retried"],
	}


def _audience(campaign: str, channel: str, blasts: list[str]) -> int:
	"""Recipient counts still come from MariaDB.

	The audience is known at export time and never changes; asking DynamoDB
	for it would mean counting recipient rows, which is exactly the scan the
	SUMMARY item exists to avoid.
	"""
	doctype = _CHANNEL[channel][0]
	field = "recipient_count" if frappe.get_meta(doctype).has_field("recipient_count") else None
	if not field:
		return 0
	rows = frappe.get_all(doctype, filters={"name": ["in", blasts]}, fields=[field])
	return sum(int(r.get(field) or 0) for r in rows)


def merge(modules: dict, campaign: str, days: int = 30) -> dict:
	"""Fold AWS numbers into the per-adapter rollup map.

	Called after the adapter loop in `campaign/services/analytics.py`. A
	campaign can hold **both** an AWS-routed and a Standard-routed blast on the
	same channel, so this adds rather than replaces — the adapter's MariaDB
	numbers cover the Standard blasts, and DynamoDB covers the AWS ones.
	"""
	for channel in _CHANNEL:
		try:
			aws = rollup(campaign, channel, days)
		except Exception:
			frappe.log_error(
				title=f"AWS rollup failed for {campaign}/{channel}", message=frappe.get_traceback()
			)
			continue
		if not aws:
			continue

		existing = modules.get(channel)
		modules[channel] = _combine(existing, aws) if isinstance(existing, dict) and existing.get("metrics") else aws
	return modules


def _combine(standard: dict, aws: dict) -> dict:
	"""Sum two rollups of the same channel, recomputing the derived rate.

	Adding two percentages would be wrong, so `delivery_rate` is recomputed
	from the combined totals rather than averaged.
	"""
	sm, am = standard.get("metrics") or {}, aws["metrics"]
	merged = dict(sm)
	for key in ("sent", "delivered", "failed", "audience", "credits", "read"):
		if key in sm or key in am:
			merged[key] = (sm.get(key) or 0) + (am.get(key) or 0)

	sent = merged.get("sent") or 0
	merged["delivery_rate"] = round((merged.get("delivered") or 0) / sent * 100, 1) if sent else 0

	return {
		**standard,
		"blasts": (standard.get("blasts") or 0) + aws["blasts"],
		"metrics": merged,
		"engine": "mixed",
		"stale": aws.get("stale", False),
	}

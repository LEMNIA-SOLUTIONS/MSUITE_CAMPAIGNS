# Copyright (c) 2026, MSuite and contributors
# For license information, please see license.txt

"""AWS delivery engine integration and manifest export for unified Broadcast channels.

Handles DynamoDB campaign key derivation, eligibility checks, NDJSON manifest generation
with keyset pagination, and provider-brokered S3 export and SQS campaign dispatch.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import frappe
from frappe import _

from msuite_campaigns.aws_pipeline import client
from msuite_campaigns.aws_pipeline.dispatch import (
	_check_size,
	_require_feature,
	get_min_recipient_threshold,
	is_ready,
)
from msuite_campaigns.aws_pipeline.rollup import dynamo_key
from msuite_campaigns.broadcast.constants import RECIPIENT_INSERT_BATCH


def dynamo_campaign_id(broadcast_doc: Any, channel: str) -> str:
	"""Return the DynamoDB analytics key for a broadcast doc and channel.

	A migrated blast keeps the key its in-flight AWS analytics already use.
	Uses broadcast_doc.campaign or broadcast_doc.legacy_name or broadcast_doc.name.

	Args:
		broadcast_doc: Broadcast document instance or dict-like object.
		channel: Channel name ("WhatsApp", "Email", or "SMS").

	Returns:
		str: DynamoDB campaign identifier (e.g. '{identifier}__{channel}').

	Exceptions:
		None.

	Side effects:
		None.
	"""
	campaign = (
		getattr(broadcast_doc, "campaign", None)
		or (broadcast_doc.get("campaign") if isinstance(broadcast_doc, dict) else None)
	)
	legacy_name = (
		getattr(broadcast_doc, "legacy_name", None)
		or (broadcast_doc.get("legacy_name") if isinstance(broadcast_doc, dict) else None)
	)
	name = (
		getattr(broadcast_doc, "name", None)
		or (broadcast_doc.get("name") if isinstance(broadcast_doc, dict) else None)
	)
	identifier = campaign or legacy_name or name or ""
	return dynamo_key(identifier, channel)


def should_use_aws(broadcast_doc: Any, channel_row: Any) -> bool:
	"""Determine whether the channel should be routed through the AWS delivery engine.

	Evaluates:
	  1. channel_row delivery_engine == 'AWS'
	  2. recipient_count >= min_recipient_threshold
	  3. is_ready(channel)

	Args:
		broadcast_doc: Broadcast document instance or dict-like object.
		channel_row: Broadcast Channel child row or dict-like object.

	Returns:
		bool: True if the channel should take the AWS delivery path, False otherwise.

	Exceptions:
		None.

	Side effects:
		None.
	"""
	engine = (
		channel_row.get("delivery_engine")
		if isinstance(channel_row, dict)
		else getattr(channel_row, "delivery_engine", None)
	)
	if (engine or "AWS") != "AWS":
		return False

	count = int(
		(
			channel_row.get("recipient_count")
			if isinstance(channel_row, dict)
			else getattr(channel_row, "recipient_count", 0)
		)
		or 0
	)
	threshold = get_min_recipient_threshold()
	if count < threshold:
		return False

	channel = (
		channel_row.get("channel")
		if isinstance(channel_row, dict)
		else getattr(channel_row, "channel", "")
	)
	return bool(is_ready((channel or "").lower()))


def _recipients_generator(broadcast: str, channel: str) -> Iterator[dict]:
	"""Generate sendable (Queued/Retrying) recipient rows using keyset pagination.

	Paginates using 'name > last' in chunks of RECIPIENT_INSERT_BATCH without OFFSET,
	ensuring memory-bounded streaming even for large blasts.

	Args:
		broadcast: Name of the Broadcast document.
		channel: Channel name ("WhatsApp", "Email", or "SMS").

	Yields:
		dict: Plain dictionary representation of one sendable Broadcast Recipient.

	Exceptions:
		None.

	Side effects:
		Queries MariaDB tabBroadcast Recipient in batches.
	"""
	last_name: str | None = None
	while True:
		filters = [
			["broadcast", "=", broadcast],
			["channel", "=", channel],
			["status", "in", ["Queued", "Retrying"]],
		]
		if last_name:
			filters.append(["name", ">", last_name])

		rows = frappe.get_all(
			"Broadcast Recipient",
			filters=filters,
			fields=[
				"name",
				"broadcast",
				"channel",
				"address",
				"recipient_name",
				"recipient_data",
				"status",
				"retry_count",
				"provider_message_id",
			],
			order_by="name asc",
			limit=RECIPIENT_INSERT_BATCH,
		)
		if not rows:
			break
		for row in rows:
			yield row
		if len(rows) < RECIPIENT_INSERT_BATCH:
			break
		last_name = rows[-1]["name"]


def _get_channel_adapter(channel: str) -> dict:
	"""Retrieve the registered broadcast adapter for the given channel.

	Args:
		channel: Channel name ("WhatsApp", "Email", or "SMS").

	Returns:
		dict: Adapter dictionary satisfying the broadcast adapter contract.

	Raises:
		frappe.ValidationError: If adapter resolution fails or channel not supported.

	Side effects:
		None.
	"""
	try:
		from msuite_campaigns.broadcast.adapters import require_channel_adapter

		return require_channel_adapter(channel)
	except (ImportError, ModuleNotFoundError):
		import sys

		if "msuite_campaigns.broadcast.adapters" in sys.modules:
			mod = sys.modules["msuite_campaigns.broadcast.adapters"]
			if hasattr(mod, "require_channel_adapter"):
				return mod.require_channel_adapter(channel)
		frappe.throw(_("{0} is not installed on this site").format(channel))


def export_and_enqueue_channel(broadcast: str, channel: str) -> dict:
	"""Export recipient manifest to S3 and trigger AWS campaign execution for a channel.

	Enforces strict ordering:
	  1. Presign upload URL
	  2. Upload NDJSON manifest to S3
	  3. Sync credentials via adapter sync_secret (aborts before trigger on error)
	  4. Trigger campaign via provider relay
	  5. Record outbound messages via adapter record_outbound

	Guard:
	  A crash after trigger must not re-trigger on retry. The channel status is set to
	  'In Progress' on the channel row and cached in Redis. Subsequent invocations detect
	  the active trigger and return without triggering again.

	Args:
		broadcast: Name of the Broadcast document.
		channel: Channel name ("WhatsApp", "Email", or "SMS").

	Returns:
		dict: {"s3_key": str, "message_id": str, "queued": int}

	Raises:
		frappe.ValidationError: On missing channel row, feature disabled, size limit exceeded,
			or sync_secret failure.

	Side effects:
		Uploads NDJSON to S3, dispatches SQS trigger via provider, writes to tabBroadcast Channel
		and frappe.cache.
	"""
	cache_key = f"broadcast_aws_triggered:{broadcast}:{channel}"
	cached = frappe.cache.get_value(cache_key)
	if cached and isinstance(cached, dict):
		return cached

	broadcast_doc = frappe.get_doc("Broadcast", broadcast)
	channel_row = None
	for row in broadcast_doc.channels or []:
		if row.channel == channel:
			channel_row = row
			break

	if not channel_row:
		frappe.throw(_("Channel '{0}' not found on Broadcast '{1}'.").format(channel, broadcast))

	if channel_row.status == "In Progress":
		return {
			"s3_key": "",
			"message_id": "",
			"queued": int(channel_row.recipient_count or 0),
		}

	_require_feature((channel or "").lower())

	adapter = _get_channel_adapter(channel)
	campaign_id = dynamo_campaign_id(broadcast_doc, channel)

	header_fn = adapter.get("manifest_header")
	if not header_fn or not callable(header_fn):
		frappe.throw(_("Adapter for '{0}' does not provide manifest_header.").format(channel))

	header = dict(header_fn(broadcast_doc, channel_row) or {})
	header["bulk_doctype"] = "Broadcast"
	header["bulk_doc_name"] = broadcast
	header["campaign_id"] = campaign_id

	row_fn = adapter.get("manifest_row")
	if not row_fn or not callable(row_fn):
		frappe.throw(_("Adapter for '{0}' does not provide manifest_row.").format(channel))

	lines = [json.dumps(header, separators=(",", ":"))]
	count = 0
	for recipient in _recipients_generator(broadcast, channel):
		row_data = row_fn(broadcast_doc, channel_row, recipient)
		lines.append(json.dumps(row_data, separators=(",", ":")))
		count += 1

	body = "\n".join(lines)

	# 1. Presign
	slot = client.request_upload_url(channel, "Broadcast", broadcast)
	_check_size(body, slot.get("max_bytes"))

	# 2. Upload
	client.upload_manifest(slot["url"], body)

	# 3. Sync secret (failure aborts before trigger)
	sync_fn = adapter.get("sync_secret")
	if sync_fn and callable(sync_fn):
		try:
			sync_fn(broadcast_doc, channel_row)
		except Exception as e:
			frappe.throw(
				_("Failed to sync {0} credentials for the AWS execution plane: {1}").format(channel, e)
			)

	# 4. Trigger
	result = client.trigger_campaign(
		key=slot["key"],
		channel=channel,
		bulk_doctype="Broadcast",
		bulk_doc_name=broadcast,
		campaign_id=campaign_id,
		recipient_count=count,
	)

	# Record trigger on channel row & cache to guard against re-trigger on retry
	frappe.db.set_value(
		"Broadcast Channel",
		channel_row.name,
		{"status": "In Progress", "launch_error": ""},
		update_modified=False,
	)
	channel_row.status = "In Progress"
	channel_row.launch_error = ""

	response_payload = {
		"s3_key": slot["key"],
		"message_id": result.get("message_id"),
		"queued": count,
	}
	frappe.cache.set_value(cache_key, response_payload, expires_in_sec=86400)

	# 5. Record outbound messages
	record_fn = adapter.get("record_outbound")
	if record_fn and callable(record_fn):
		try:
			record_fn(broadcast_doc, channel_row)
		except Exception as e:
			frappe.logger().warning(
				f"broadcast aws: record_outbound error for {broadcast}/{channel}: {e}"
			)

	frappe.logger().info(
		f"broadcast aws: Broadcast {broadcast} ({channel}) -> {slot['key']} "
		f"({count} recipients, sqs {result.get('message_id')})"
	)

	return response_payload

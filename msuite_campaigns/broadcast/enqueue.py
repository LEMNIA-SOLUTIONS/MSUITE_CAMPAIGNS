# Copyright (c) 2026, MSuite and contributors
# For license information, please see license.txt

"""Keyset-chunked enqueue service for Broadcast channels.

Splits recipient rows for a given broadcast and channel into chunks of SEND_CHUNK_SIZE
using keyset pagination on `name`, enqueuing background worker jobs. Each chunk loops
its slice, respects mid-send pause/cancel gates, and delegates message delivery to the
channel adapter's `send_one` handler.
"""

from __future__ import annotations

import frappe
from frappe import _

from msuite_campaigns.broadcast.constants import (
	SEND_CHUNK_SIZE,
	SEND_QUEUE,
	SEND_TIMEOUT,
	SENDABLE_RECIPIENT_STATUSES,
)
from msuite_campaigns.broadcast.recipients import apply_delivery_status

MAX_FAILURE_REASON_LENGTH: int = 500


def enqueue_broadcast_channel(broadcast: str, channel: str) -> int:
	"""Split sendable recipients into chunks and enqueue background execution jobs.

	Uses keyset pagination on `name` (ORDER BY name ASC, name > last_name, no OFFSET).

	Args:
		broadcast: Name of the Broadcast document.
		channel: Channel name ("WhatsApp", "Email", or "SMS").

	Returns:
		int: Number of chunk jobs enqueued.

	Side effects:
		Calls frappe.enqueue to queue background jobs.
	"""
	chunks = 0
	last_name: str | None = None

	while True:
		filters = [
			["broadcast", "=", broadcast],
			["channel", "=", channel],
			["status", "in", list(SENDABLE_RECIPIENT_STATUSES)],
		]
		if last_name:
			filters.append(["name", ">", last_name])

		rows = frappe.get_all(
			"Broadcast Recipient",
			filters=filters,
			fields=["name"],
			order_by="name asc",
			limit=SEND_CHUNK_SIZE,
		)

		if not rows:
			break

		first_name = rows[0].name
		last_name = rows[-1].name

		frappe.enqueue(
			run_chunk,
			queue=SEND_QUEUE,
			timeout=SEND_TIMEOUT,
			job_name=f"broadcast_chunk_{broadcast}_{channel}_{first_name}",
			enqueue_after_commit=True,
			broadcast=broadcast,
			channel=channel,
			first_name=first_name,
			last_name=last_name,
		)
		chunks += 1

	if chunks == 0:
		from msuite_campaigns.broadcast.counters import request_counter_refresh

		request_counter_refresh(broadcast, channel)

	return chunks


def run_chunk(broadcast: str, channel: str, first_name: str, last_name: str) -> dict:
	"""Process one keyset slice of recipients for a Broadcast channel.

	Args:
		broadcast: Name of the Broadcast document.
		channel: Channel name ("WhatsApp", "Email", or "SMS").
		first_name: Inclusive lower bound name for keyset pagination.
		last_name: Inclusive upper bound name for keyset pagination.

	Returns:
		dict: {"sent": int, "failed": int, "stopped": bool}

	Raises:
		frappe.ValidationError: If channel adapter or channel row is not found.

	Side effects:
		Executes send_one on channel adapter, updates recipient statuses in DB.
	"""
	if not _is_channel_active(broadcast, channel):
		from msuite_campaigns.broadcast.counters import request_counter_refresh

		request_counter_refresh(broadcast, channel)
		return {"sent": 0, "failed": 0, "stopped": True}

	from msuite_campaigns.broadcast.adapters import get_channel_adapter

	adapter = get_channel_adapter(channel)
	if not adapter or not callable(adapter.get("send_one")):
		frappe.throw(_("No channel adapter with send_one found for channel '{0}'.").format(channel))

	send_one_fn = adapter["send_one"]

	broadcast_doc = frappe.get_doc("Broadcast", broadcast)
	channel_row = None
	for ch in broadcast_doc.channels or []:
		if ch.channel == channel:
			channel_row = ch
			break

	if not channel_row:
		frappe.throw(_("Channel '{0}' not found on Broadcast '{1}'.").format(channel, broadcast))

	recipients = frappe.get_all(
		"Broadcast Recipient",
		filters=[
			["broadcast", "=", broadcast],
			["channel", "=", channel],
			["name", ">=", first_name],
			["name", "<=", last_name],
			["status", "in", list(SENDABLE_RECIPIENT_STATUSES)],
		],
		fields=["*"],
		order_by="name asc",
	)

	sent = 0
	failed = 0
	stopped = False

	for r in recipients:
		if not _is_channel_active(broadcast, channel):
			stopped = True
			break

		try:
			send_res = send_one_fn(broadcast_doc, channel_row, r)
			if isinstance(send_res, dict):
				status = send_res.get("status") or "Sent"
				pm_id = send_res.get("provider_message_id") or r.get("provider_message_id")
				if not pm_id:
					pm_id = r["name"]
				if pm_id != r.get("provider_message_id"):
					frappe.db.set_value(
						"Broadcast Recipient",
						r["name"],
						"provider_message_id",
						pm_id,
						update_modified=False,
					)
				extra_fields = {
					k: v
					for k, v in send_res.items()
					if k
					in (
						"sent_at",
						"delivered_at",
						"read_at",
						"opened_at",
						"clicked_at",
						"error_code",
						"failure_reason",
						"is_retryable",
						"retry_count",
						"credits",
						"segments",
					)
					and v is not None
				}
				apply_delivery_status(
					broadcast,
					channel,
					pm_id,
					status,
					**extra_fields,
				)
				if status == "Sent":
					sent += 1
				else:
					failed += 1
			else:
				sent += 1
		except Exception as exc:
			failed += 1
			pm_id = r.get("provider_message_id")
			if not pm_id:
				pm_id = r["name"]
				frappe.db.set_value(
					"Broadcast Recipient",
					r["name"],
					"provider_message_id",
					pm_id,
					update_modified=False,
				)

			err_msg = str(exc)[:MAX_FAILURE_REASON_LENGTH]
			apply_delivery_status(
				broadcast,
				channel,
				pm_id,
				"Failed",
				failure_reason=err_msg,
			)

	from msuite_campaigns.broadcast.counters import request_counter_refresh

	request_counter_refresh(broadcast, channel)
	return {"sent": sent, "failed": failed, "stopped": stopped}


def _is_channel_active(broadcast: str, channel: str) -> bool:
	"""Check whether the Broadcast Channel row is currently sendable.

	Args:
		broadcast: Name of the Broadcast document.
		channel: Channel name ("WhatsApp", "Email", or "SMS").

	Returns:
		bool: True if status is 'In Progress' or 'Queued', False otherwise.

	Side effects:
		Executes SELECT on tabBroadcast Channel.
	"""
	status = frappe.db.get_value("Broadcast Channel", {"parent": broadcast, "channel": channel}, "status")
	return status in ("In Progress", "Queued")

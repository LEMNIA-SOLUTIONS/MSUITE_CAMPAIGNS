# Copyright (c) 2026, MSuite and contributors
# For license information, please see license.txt

"""Broadcast Dispatch Hub service.

Orchestrates multi-channel broadcast previews, per-channel transactional launches
with savepoint isolation, AWS vs Standard delivery engine routing, and lifecycle
action delegation (pause, resume, cancel, retry).
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe import _
from frappe.utils import validate_email_address

from msuite_core.services.validation import phone as phone_validator


def preview(payload: dict[str, Any]) -> dict[str, Any]:
	"""Compute reachable vs dropped counts per channel without writing to DB.

	Reuses `RecipientList.contacts_for` and `RecipientList.rejection_breakdown`
	when `recipient_source` is 'Recipient List', or validates and deduplicates
	using materializer normalization helpers when 'Individual'.

	Args:
		payload: Dictionary containing:
			- recipient_source: 'Recipient List' or 'Individual'
			- recipient_list: Name of Recipient List document or None
			- channels: List of channel names ('WhatsApp', 'Email', 'SMS')
			- individual_rows: List of dicts with 'address' and optional 'recipient_name'

	Returns:
		dict[str, Any]: {
			"channels": {<channel>: {"reachable": int, "dropped": int, "reasons": dict}},
			"total": int
		}

	Raises:
		None.

	Side effects:
		None (writes nothing to the database).
	"""
	recipient_source = payload.get("recipient_source")
	channels = payload.get("channels") or []
	channels_out: dict[str, dict[str, Any]] = {}

	if recipient_source == "Recipient List":
		rec_list_name = payload.get("recipient_list")
		rec_list = frappe.get_doc("Recipient List", rec_list_name) if rec_list_name else None
		total = len(rec_list.contacts or []) if rec_list else 0

		for ch in channels:
			if rec_list:
				contacts = rec_list.contacts_for(ch)
				reasons = rec_list.rejection_breakdown(ch)
				reachable = len(contacts)
				dropped = sum(reasons.values())
			else:
				reachable = 0
				dropped = 0
				reasons = {}

			channels_out[ch] = {
				"reachable": reachable,
				"dropped": dropped,
				"reasons": reasons,
			}
	else:
		ind_rows = payload.get("individual_rows") or []
		total = len(ind_rows)
		region = phone_validator.get_system_default_region()

		for ch in channels:
			reasons: dict[str, int] = {}

			def bump(reason: str) -> None:
				reasons[reason] = reasons.get(reason, 0) + 1

			seen_addresses: set[str] = set()
			valid_rows: list[str] = []

			for item in ind_rows:
				raw_addr = str(item.get("address") or "").strip()
				if ch == "Email":
					clean_email = raw_addr.lower()
					if not clean_email or not validate_email_address(clean_email, throw=False):
						bump("invalid_email")
						continue
					norm_addr = clean_email
				else:
					parsed = phone_validator.normalize(raw_addr, region)
					if not parsed.get("e164") or parsed.get("status") != phone_validator.VALID:
						bump("invalid_phone")
						continue
					norm_addr = parsed["e164"]

				if norm_addr in seen_addresses:
					bump("duplicate")
					continue
				seen_addresses.add(norm_addr)
				valid_rows.append(norm_addr)

			if valid_rows:
				from msuite_campaigns.broadcast.materializer import _get_opted_out_addresses

				opted_out = _get_opted_out_addresses(ch, valid_rows)
				reachable = 0
				for addr in valid_rows:
					if addr in opted_out:
						bump("unsubscribed")
					else:
						reachable += 1
			else:
				reachable = 0

			channels_out[ch] = {
				"reachable": reachable,
				"dropped": sum(reasons.values()),
				"reasons": reasons,
			}

	return {
		"channels": channels_out,
		"total": total,
	}


def launch(
	broadcast: str,
	individual_rows: list[dict[str, Any]] | None = None,
	channels: list[str] | None = None,
) -> dict[str, dict[str, Any]]:
	"""Launch targeted channels of a Broadcast with per-channel savepoint isolation.

	For each targeted channel row:
	1. Validates channel via adapter's optional `validate_channel`.
	2. Materializes recipient rows into `tabBroadcast Recipient`.
	3. If `recipient_count == 0`, leaves the row in Draft and reports dropped.
	4. Invokes `lifecycle.launch(broadcast, channel)`.
	5. If the channel transitioned to `Queued`, calls `hub.start_channel`.

	Each channel executes inside its own database savepoint. A failure in one channel
	rolls back only that channel's operations, records `launch_error` on the channel row
	and parent Broadcast, and allows other channels to proceed.

	Args:
		broadcast: Name of the Broadcast document.
		individual_rows: Optional recipient rows when Broadcast.recipient_source is 'Individual'.
		channels: Optional list of channel names to launch. If None, targets all channel rows.

	Returns:
		dict[str, dict[str, Any]]: Mapping of channel name to:
			{"status": str, "queued": int, "dropped": int, "error": str | None}

	Raises:
		None.

	Side effects:
		Inserts recipient rows in DB.
		Transitions channel and broadcast lifecycle statuses.
		Enqueues delivery jobs.
		Updates `launch_error` on failures.
	"""
	doc = frappe.get_doc("Broadcast", broadcast)
	target_rows = [r for r in (doc.channels or []) if not channels or r.channel in channels]
	results: dict[str, dict[str, Any]] = {}

	for row in target_rows:
		sp = f"sp_launch_{row.channel}_{frappe.generate_hash(length=8)}"
		frappe.db.savepoint(sp)
		try:
			from msuite_campaigns.broadcast.adapters import require_channel_adapter

			adapter = require_channel_adapter(row.channel)
			val_fn = adapter.get("validate_channel")
			if val_fn and callable(val_fn):
				val_fn(doc, row)

			from msuite_campaigns.broadcast.materializer import materialize

			mat = materialize(broadcast, row.channel, individual_rows=individual_rows)
			queued = mat.get("queued", 0)
			dropped = mat.get("dropped", 0)

			if queued == 0:
				frappe.db.release_savepoint(sp)
				results[row.channel] = {
					"status": "Draft",
					"queued": 0,
					"dropped": dropped,
					"error": None,
				}
				continue

			from msuite_campaigns.broadcast.lifecycle import launch as lifecycle_launch

			launch_res = lifecycle_launch(broadcast, row.channel)
			new_status = launch_res.get(row.channel) or frappe.db.get_value("Broadcast Channel", row.name, "status")

			if new_status == "Queued":
				start_channel(broadcast, row.channel)
				new_status = frappe.db.get_value("Broadcast Channel", row.name, "status")

			frappe.db.release_savepoint(sp)
			results[row.channel] = {
				"status": new_status,
				"queued": queued,
				"dropped": dropped,
				"error": None,
			}
		except Exception as exc:
			frappe.db.rollback(save_point=sp)
			err_msg = str(exc)
			frappe.db.set_value("Broadcast Channel", row.name, "launch_error", err_msg, update_modified=False)
			frappe.db.set_value("Broadcast", broadcast, "launch_error", err_msg, update_modified=False)
			current_status = frappe.db.get_value("Broadcast Channel", row.name, "status")
			results[row.channel] = {
				"status": current_status,
				"queued": 0,
				"dropped": 0,
				"error": err_msg,
			}

	return results


def start_channel(broadcast: str, channel: str) -> None:
	"""Transition a Queued channel row to In Progress and dispatch sending.

	Advances status from Queued to In Progress via `lifecycle.transition_channel`.
	Checks if `broadcast/aws.py` exists and `aws.should_use_aws(doc, row)` is True.
	If so, dispatches to `aws.export_and_enqueue_channel`; otherwise, routes to
	the Standard engine keyset worker via `enqueue.enqueue_broadcast_channel`.

	Args:
		broadcast: Name of the Broadcast document.
		channel: Channel name ('WhatsApp', 'Email', or 'SMS').

	Returns:
		None.

	Raises:
		frappe.ValidationError: If status transition is not permitted.

	Side effects:
		Updates channel row and parent broadcast status to 'In Progress'.
		Enqueues background delivery jobs.
	"""
	from msuite_campaigns.broadcast.lifecycle import transition_channel

	transition_channel(broadcast, channel, "In Progress")

	doc = frappe.get_doc("Broadcast", broadcast)
	row = doc.channel_row(channel)

	use_aws = False
	aws = None
	try:
		import importlib

		aws = importlib.import_module("msuite_campaigns.broadcast.aws")
		if hasattr(aws, "should_use_aws") and aws.should_use_aws(doc, row):
			use_aws = True
	except (ImportError, ModuleNotFoundError):
		use_aws = False

	if use_aws and aws is not None:
		try:
			aws.export_and_enqueue_channel(broadcast, channel)
		except TypeError:
			aws.export_and_enqueue_channel(doc, row)
	else:
		from msuite_campaigns.broadcast.enqueue import enqueue_broadcast_channel

		enqueue_broadcast_channel(broadcast, channel)


def pause(broadcast: str, channel: str | None = None) -> dict[str, str]:
	"""Pause running channel rows on a Broadcast.

	Args:
		broadcast: Name of the Broadcast document.
		channel: Optional channel name. If None, applies to all eligible rows.

	Returns:
		dict[str, str]: Mapping of touched channels to their new status.

	Raises:
		frappe.ValidationError: If channel is invalid or transition illegal.

	Side effects:
		Updates channel and broadcast status to Paused in DB.
	"""
	from msuite_campaigns.broadcast import lifecycle

	return lifecycle.pause(broadcast, channel=channel)


def resume(broadcast: str, channel: str | None = None) -> dict[str, str]:
	"""Resume paused channel rows on a Broadcast.

	Starts delivery via `hub.start_channel` for any rows that transitioned to Queued.

	Args:
		broadcast: Name of the Broadcast document.
		channel: Optional channel name. If None, applies to all eligible rows.

	Returns:
		dict[str, str]: Mapping of touched channels to their new status.

	Raises:
		frappe.ValidationError: If channel is invalid or transition illegal.

	Side effects:
		Updates channel and broadcast status in DB.
		Enqueues delivery jobs for resumed channels.
	"""
	from msuite_campaigns.broadcast import lifecycle

	res = lifecycle.resume(broadcast, channel=channel)
	for ch, st in res.items():
		if st == "Queued":
			start_channel(broadcast, ch)
	return res


def retry(broadcast: str, channel: str | None = None) -> dict[str, str]:
	"""Retry Failed or Partially Failed channel rows on a Broadcast.

	Starts delivery via `hub.start_channel` for any rows that transitioned to Queued.

	Args:
		broadcast: Name of the Broadcast document.
		channel: Optional channel name. If None, applies to all eligible rows.

	Returns:
		dict[str, str]: Mapping of touched channels to their new status.

	Raises:
		frappe.ValidationError: If channel is invalid or transition illegal.

	Side effects:
		Requeues failed recipient rows, updates status in DB, and enqueues delivery jobs.
	"""
	from msuite_campaigns.broadcast import lifecycle

	res = lifecycle.retry_failed(broadcast, channel=channel)
	for ch, st in res.items():
		if st == "Queued":
			start_channel(broadcast, ch)
	return res


def cancel(broadcast: str, channel: str | None = None) -> dict[str, str]:
	"""Cancel non-terminal channel rows on a Broadcast.

	Args:
		broadcast: Name of the Broadcast document.
		channel: Optional channel name. If None, applies to all eligible rows.

	Returns:
		dict[str, str]: Mapping of touched channels to their new status.

	Raises:
		frappe.ValidationError: If channel is invalid or transition illegal.

	Side effects:
		Updates channel and broadcast status to Cancelled in DB.
	"""
	from msuite_campaigns.broadcast import lifecycle

	return lifecycle.cancel(broadcast, channel=channel)

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

MAX_LAUNCH_ERROR_LENGTH: int = 500


def _get_aws_module() -> Any:
	"""Dynamically import and return the broadcast.aws module if available."""
	try:
		import importlib

		return importlib.import_module("msuite_campaigns.broadcast.aws")
	except (ImportError, ModuleNotFoundError):
		return None


def _is_aws_engine(doc: Any, row: Any) -> bool:
	"""Determine whether a channel row is configured for AWS engine delivery.

	Args:
		doc: Broadcast Document instance.
		row: Broadcast Channel child row instance or dict.

	Returns:
		bool: True if AWS engine should be used or was already handed off, False otherwise.

	Raises:
		None.

	Side effects:
		None.
	"""
	if not row:
		return False

	engine = row.get("delivery_engine") if isinstance(row, dict) else getattr(row, "delivery_engine", None)
	if engine == "Standard":
		return False

	channel = row.get("channel") if isinstance(row, dict) else getattr(row, "channel", None)
	if doc and getattr(doc, "name", None) and channel:
		cache_key = f"broadcast_aws_triggered:{doc.name}:{channel}"
		if frappe.cache.get_value(cache_key):
			return True

	aws = _get_aws_module()
	if aws and hasattr(aws, "should_use_aws"):
		return bool(aws.should_use_aws(doc, row))

	return False



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

			if launch_res.get(row.channel) == "Queued":
				start_channel(broadcast, row.channel)
				new_status = frappe.db.get_value("Broadcast Channel", row.name, "status")
			else:
				new_status = launch_res.get(row.channel) or frappe.db.get_value("Broadcast Channel", row.name, "status")

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

	Advances status from Queued to In Progress via `lifecycle.transition_channel`
	if not already In Progress (making start_channel idempotent).
	Checks if `broadcast/aws.py` exists and `aws.should_use_aws(doc, row)` is True.
	If so, dispatches to `aws.export_and_enqueue_channel` (skipping re-export if
	already In Progress); otherwise, routes to the Standard engine keyset worker via
	`enqueue.enqueue_broadcast_channel`.

	Wraps engine dispatch: on any exception moves the row from In Progress to Failed
	via `lifecycle.transition_channel`, sets `launch_error` (truncated to
	`MAX_LAUNCH_ERROR_LENGTH`) on the channel row and parent broadcast, and re-raises.

	Args:
		broadcast: Name of the Broadcast document.
		channel: Channel name ('WhatsApp', 'Email', or 'SMS').

	Returns:
		None.

	Raises:
		frappe.ValidationError: If status transition is not permitted or channel not found.
		Exception: Re-raises any exception that occurs during engine dispatch.

	Side effects:
		Updates channel row and parent broadcast status to 'In Progress' or 'Failed'.
		Enqueues background delivery jobs.
		Updates `launch_error` on failures.
	"""
	doc = frappe.get_doc("Broadcast", broadcast)
	row = doc.channel_row(channel)
	if not row:
		frappe.throw(_("Channel {0} not found on Broadcast {1}").format(channel, broadcast), frappe.ValidationError)

	from msuite_campaigns.broadcast.lifecycle import transition_channel

	already_in_progress = (row.status == "In Progress")
	if not already_in_progress:
		transition_channel(broadcast, channel, "In Progress")
		doc = frappe.get_doc("Broadcast", broadcast)
		row = doc.channel_row(channel)

	use_aws = _is_aws_engine(doc, row)
	if use_aws and already_in_progress:
		return

	try:
		if use_aws:
			aws = _get_aws_module()
			if not aws:
				frappe.throw(_("msuite_campaigns.broadcast.aws module not found"), frappe.ValidationError)
			try:
				aws.export_and_enqueue_channel(broadcast, channel)
			except TypeError:
				aws.export_and_enqueue_channel(doc, row)
		else:
			from msuite_campaigns.broadcast.enqueue import enqueue_broadcast_channel

			chunks = enqueue_broadcast_channel(broadcast, channel)
			if chunks == 0:
				from msuite_campaigns.broadcast.counters import refresh_counters

				refresh_counters(broadcast, channel)
	except Exception as exc:
		err_msg = str(exc)[:MAX_LAUNCH_ERROR_LENGTH]
		try:
			transition_channel(broadcast, channel, "Failed")
		except Exception:
			frappe.db.set_value("Broadcast Channel", row.name, "status", "Failed", update_modified=False)
			frappe.db.set_value("Broadcast", broadcast, "status", "Failed", update_modified=False)
		frappe.db.set_value("Broadcast Channel", row.name, "launch_error", err_msg, update_modified=False)
		frappe.db.set_value("Broadcast", broadcast, "launch_error", err_msg, update_modified=False)
		raise


def pause(broadcast: str, channel: str | None = None) -> dict[str, str]:
	"""Pause running channel rows on a Broadcast.

	Guards against pausing AWS-engine rows that are already In Progress, raising
	frappe.ValidationError ('cannot pause after hand-off to AWS').

	Args:
		broadcast: Name of the Broadcast document.
		channel: Optional channel name. If None, applies to all eligible rows.

	Returns:
		dict[str, str]: Mapping of touched channels to their new status.

	Raises:
		frappe.ValidationError: If channel is invalid, transition illegal, or attempting
			to pause an AWS row that is already In Progress.

	Side effects:
		Updates channel and broadcast status to Paused in DB.
	"""
	doc = frappe.get_doc("Broadcast", broadcast)
	target_rows = [r for r in (doc.channels or []) if not channel or r.channel == channel]
	for r in target_rows:
		if r.status == "In Progress" and _is_aws_engine(doc, r):
			frappe.throw(_("cannot pause after hand-off to AWS"), frappe.ValidationError)

	from msuite_campaigns.broadcast import lifecycle

	return lifecycle.pause(broadcast, channel=channel)


def resume(broadcast: str, channel: str | None = None) -> dict[str, str]:
	"""Resume paused channel rows on a Broadcast.

	Restarts sending for every row that ends Queued OR In Progress after lifecycle.resume.
	Guards against re-exporting AWS engine rows.

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
	doc = frappe.get_doc("Broadcast", broadcast)
	for ch, st in res.items():
		if st in ("Queued", "In Progress"):
			row = doc.channel_row(ch)
			if _is_aws_engine(doc, row):
				continue
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

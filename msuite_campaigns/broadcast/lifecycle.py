# Copyright (c) 2026, MSuite and contributors
# For license information, please see license.txt

"""Lifecycle state machine and status rollup for Broadcasts.

Provides transition validation, parent status rollup computation,
and channel actions (launch, pause, resume, cancel, retry_failed).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import frappe
from frappe import _
from frappe.utils import cint, get_datetime, now_datetime

from msuite_campaigns.broadcast.constants import ALLOWED_TRANSITIONS
from msuite_campaigns.broadcast.recipients import requeue_failed_recipients
from msuite_campaigns.broadcast.schedule_time import to_site_datetime
from msuite_core.services.plans.plan_enforcer import check_broadcast_launch


def rollup_status(doc: Any) -> str:
	"""Compute overall Broadcast status from child channel statuses.

	Pure function evaluating channel row statuses in strict precedence order:
	(1) no rows or all Draft -> Draft
	(2) all Cancelled -> Cancelled
	(3) any In Progress -> In Progress
	(4) any Queued -> Queued
	(5) any Paused -> Paused
	(6) any Scheduled -> Scheduled
	(7) any Draft (others finished) -> Draft
	(8) all remaining terminal: all Failed -> Failed; any Failed/Partially Failed -> Partially Failed; else Completed.

	Lifecycle functions are INTERNAL (not whitelisted, no permission check):
	the Task 12 API checks permission before calling them.

	Args:
		doc: Broadcast Document or dict or object containing channels list.

	Returns:
		str: Derived overall Broadcast status string.

	Raises:
		None.

	Side effects:
		None (pure calculation).
	"""
	channels = getattr(doc, "channels", None)
	if channels is None and isinstance(doc, dict):
		channels = doc.get("channels")
	if not channels:
		return "Draft"

	statuses: list[str] = []
	for r in channels:
		st = r.get("status") if isinstance(r, dict) else getattr(r, "status", None)
		if st:
			statuses.append(st)

	if not statuses:
		return "Draft"

	# (1) all Draft -> Draft
	if all(s == "Draft" for s in statuses):
		return "Draft"

	# (2) all Cancelled -> Cancelled
	if all(s == "Cancelled" for s in statuses):
		return "Cancelled"

	# (3) any In Progress -> In Progress
	if any(s == "In Progress" for s in statuses):
		return "In Progress"

	# (4) any Queued -> Queued
	if any(s == "Queued" for s in statuses):
		return "Queued"

	# (5) any Paused -> Paused
	if any(s == "Paused" for s in statuses):
		return "Paused"

	# (6) any Scheduled -> Scheduled
	if any(s == "Scheduled" for s in statuses):
		return "Scheduled"

	# (7) any Draft (others finished) -> Draft
	if any(s == "Draft" for s in statuses):
		return "Draft"

	# (8) all remaining are terminal {Completed, Cancelled, Failed, Partially Failed}:
	if all(s == "Failed" for s in statuses):
		return "Failed"
	if any(s in ("Failed", "Partially Failed") for s in statuses):
		return "Partially Failed"
	return "Completed"


def transition_channel(broadcast: str, channel: str, to_status: str) -> None:
	"""Transition a single channel of a Broadcast to a new status.

	Row-locks the Broadcast document, validates that the channel exists and that
	the requested transition is permitted by ALLOWED_TRANSITIONS, updates the child
	channel row, updates launched_at if leaving Draft/Scheduled for the first time,
	and updates the parent Broadcast status via rollup_status.

	Lifecycle functions are INTERNAL (not whitelisted, no permission check):
	the Task 12 API checks permission before calling them.

	Args:
		broadcast: Name of the Broadcast document.
		channel: Channel name ('WhatsApp', 'Email', or 'SMS').
		to_status: Target status from CHANNEL_STATUSES.

	Raises:
		frappe.ValidationError: If channel is absent from the broadcast or transition
			is not allowed by ALLOWED_TRANSITIONS.

	Side effects:
		Row-locks tabBroadcast, updates tabBroadcast Channel and tabBroadcast in DB.
		No commit inside.
	"""
	doc = frappe.get_doc("Broadcast", broadcast, for_update=True)
	row = doc.channel_row(channel)
	if not row:
		frappe.throw(
			_("Channel {0} not found on Broadcast {1}").format(channel, broadcast),
			frappe.ValidationError,
		)

	allowed = ALLOWED_TRANSITIONS.get(row.status, set())
	if to_status not in allowed:
		frappe.throw(
			_("Cannot move {0} from {1} to {2}").format(channel, row.status, to_status),
			frappe.ValidationError,
		)

	old_status = row.status
	frappe.db.set_value("Broadcast Channel", row.name, "status", to_status)
	row.status = to_status

	new_broadcast_status = rollup_status(doc)
	updates: dict[str, Any] = {"status": new_broadcast_status}

	if not doc.launched_at and old_status in ("Draft", "Scheduled") and to_status not in ("Draft", "Scheduled"):
		now_val = now_datetime()
		updates["launched_at"] = now_val
		doc.launched_at = now_val

	frappe.db.set_value("Broadcast", doc.name, updates)
	doc.status = new_broadcast_status


def _get_target_rows(doc: Any, channel: str | None) -> list[Any]:
	"""Retrieve target child channel rows from Broadcast document.

	Lifecycle functions are INTERNAL (not whitelisted, no permission check):
	the Task 12 API checks permission before calling them.

	Args:
		doc: Broadcast Document.
		channel: Channel name or None for all channels.

	Returns:
		list[Any]: Matching child channel rows.

	Raises:
		frappe.ValidationError: If a specified channel is absent.

	Side effects:
		None.
	"""
	if channel:
		row = doc.channel_row(channel)
		if not row:
			frappe.throw(
				_("Channel {0} not found on Broadcast {1}").format(channel, doc.name),
				frappe.ValidationError,
			)
		return [row]
	return list(doc.channels or [])


def _apply_action_transitions(
	doc: Any,
	transitions: list[tuple[Any, str]],
	before_apply_fn: Callable[[Any, Any], None] | None = None,
) -> dict[str, str]:
	"""Validate all target rows first, then apply transitions atomically without commit.

	Enforces that any ValidationError for one row leaves other rows untouched by
	performing all transition checks before applying updates.

	Lifecycle functions are INTERNAL (not whitelisted, no permission check):
	the Task 12 API checks permission before calling them.

	Args:
		doc: Broadcast Document (loaded for_update).
		transitions: List of (row, target_status) pairs.
		before_apply_fn: Optional callback invoked with (doc, row) before updating status.

	Returns:
		dict[str, str]: Mapping of touched channel names to their new status.

	Raises:
		frappe.ValidationError: If any target transition is illegal.

	Side effects:
		Updates tabBroadcast Channel rows and tabBroadcast in DB.
		No commit inside.
	"""
	if not transitions:
		return {}

	# 1. Validate all target transitions first (Rule 12: never leave half-moved)
	for row, target_status in transitions:
		allowed = ALLOWED_TRANSITIONS.get(row.status, set())
		if target_status not in allowed:
			frappe.throw(
				_("Cannot move {0} from {1} to {2}").format(row.channel, row.status, target_status),
				frappe.ValidationError,
			)

	# 2. Apply all transitions
	results: dict[str, str] = {}
	now_val = now_datetime()
	any_leaves_draft_or_scheduled = False

	for row, target_status in transitions:
		old_status = row.status
		if old_status in ("Draft", "Scheduled") and target_status not in ("Draft", "Scheduled"):
			any_leaves_draft_or_scheduled = True

		if before_apply_fn:
			before_apply_fn(doc, row)

		frappe.db.set_value("Broadcast Channel", row.name, "status", target_status)
		row.status = target_status
		results[row.channel] = target_status

	# 3. Update Broadcast parent doc
	new_broadcast_status = rollup_status(doc)
	updates: dict[str, Any] = {"status": new_broadcast_status}

	if not doc.launched_at and any_leaves_draft_or_scheduled:
		updates["launched_at"] = now_val
		doc.launched_at = now_val

	frappe.db.set_value("Broadcast", doc.name, updates)
	doc.status = new_broadcast_status

	return results


def launch(broadcast: str, channel: str | None = None) -> dict[str, str]:
	"""Launch a Broadcast or a specific channel on a Broadcast.

	FIRST calls plan_enforcer.check_broadcast_launch(doc) to enforce quotas per channel row.
	For rows in Draft/Scheduled, transitions each row to Scheduled if doc.effective_time(row) is in the
	future, else Queued. Rows already Queued, In Progress, or Scheduled (when target is Scheduled)
	are a silent no-op (idempotent double launch). Terminal rows and others are skipped.

	Lifecycle functions are INTERNAL (not whitelisted, no permission check):
	the Task 12 API checks permission before calling them.

	Args:
		broadcast: Name of the Broadcast document.
		channel: Optional channel name ('WhatsApp', 'Email', or 'SMS'). If None,
			applies to all non-terminal channel rows.

	Returns:
		dict[str, str]: Mapping of touched channel names to their new status.

	Raises:
		frappe.ValidationError: If channel is absent, plan check fails, or transition is illegal.

	Side effects:
		Row-locks tabBroadcast, updates tabBroadcast Channel and tabBroadcast in DB.
		No commit inside.
	"""
	doc = frappe.get_doc("Broadcast", broadcast, for_update=True)
	target_rows = _get_target_rows(doc, channel)

	check_broadcast_launch(doc)

	transitions: list[tuple[Any, str]] = []
	for row in target_rows:
		eff_time = doc.effective_time(row)
		eff_dt = to_site_datetime(eff_time)
		is_future = bool(eff_dt and eff_dt > now_datetime())
		target_status = "Scheduled" if is_future else "Queued"

		if row.status in ("Draft", "Scheduled"):
			if row.status == target_status:
				continue  # silent no-op (e.g. already Scheduled and still future)
			transitions.append((row, target_status))
		elif row.status in ("Queued", "In Progress"):
			continue  # silent no-op
		else:
			continue  # terminal or other rows skipped

	return _apply_action_transitions(doc, transitions)


def pause(broadcast: str, channel: str | None = None) -> dict[str, str]:
	"""Pause running channel rows on a Broadcast.

	Moves only Queued and In Progress rows to Paused. Other statuses are skipped.

	Lifecycle functions are INTERNAL (not whitelisted, no permission check):
	the Task 12 API checks permission before calling them.

	Args:
		broadcast: Name of the Broadcast document.
		channel: Optional channel name. If None, applies to all eligible rows.

	Returns:
		dict[str, str]: Mapping of touched channel names to their new status.

	Raises:
		frappe.ValidationError: If channel is absent or transition is illegal.

	Side effects:
		Row-locks tabBroadcast, updates tabBroadcast Channel and tabBroadcast in DB.
		No commit inside.
	"""
	doc = frappe.get_doc("Broadcast", broadcast, for_update=True)
	target_rows = _get_target_rows(doc, channel)

	transitions: list[tuple[Any, str]] = []
	for row in target_rows:
		if row.status in ("Queued", "In Progress"):
			transitions.append((row, "Paused"))
		else:
			continue

	return _apply_action_transitions(doc, transitions)


def resume(broadcast: str, channel: str | None = None) -> dict[str, str]:
	"""Resume paused channel rows on a Broadcast.

	Moves Paused rows to Queued if sent_count is 0, else In Progress.
	Other statuses are skipped.

	Lifecycle functions are INTERNAL (not whitelisted, no permission check):
	the Task 12 API checks permission before calling them.

	Args:
		broadcast: Name of the Broadcast document.
		channel: Optional channel name. If None, applies to all eligible rows.

	Returns:
		dict[str, str]: Mapping of touched channel names to their new status.

	Raises:
		frappe.ValidationError: If channel is absent or transition is illegal.

	Side effects:
		Row-locks tabBroadcast, updates tabBroadcast Channel and tabBroadcast in DB.
		No commit inside.
	"""
	doc = frappe.get_doc("Broadcast", broadcast, for_update=True)
	target_rows = _get_target_rows(doc, channel)

	transitions: list[tuple[Any, str]] = []
	for row in target_rows:
		if row.status == "Paused":
			sent_count = cint(getattr(row, "sent_count", 0))
			target = "Queued" if sent_count == 0 else "In Progress"
			transitions.append((row, target))
		else:
			continue

	return _apply_action_transitions(doc, transitions)


def cancel(broadcast: str, channel: str | None = None) -> dict[str, str]:
	"""Cancel non-terminal channel rows on a Broadcast.

	Moves any non-terminal row whose allowed transitions include Cancelled to Cancelled.
	Terminal rows (Completed, Cancelled) and non-cancellable rows are skipped.

	Lifecycle functions are INTERNAL (not whitelisted, no permission check):
	the Task 12 API checks permission before calling them.

	Args:
		broadcast: Name of the Broadcast document.
		channel: Optional channel name. If None, applies to all eligible rows.

	Returns:
		dict[str, str]: Mapping of touched channel names to their new status.

	Raises:
		frappe.ValidationError: If channel is absent or transition is illegal.

	Side effects:
		Row-locks tabBroadcast, updates tabBroadcast Channel and tabBroadcast in DB.
		No commit inside.
	"""
	doc = frappe.get_doc("Broadcast", broadcast, for_update=True)
	target_rows = _get_target_rows(doc, channel)

	transitions: list[tuple[Any, str]] = []
	for row in target_rows:
		if "Cancelled" in ALLOWED_TRANSITIONS.get(row.status, set()):
			transitions.append((row, "Cancelled"))
		else:
			continue

	return _apply_action_transitions(doc, transitions)


def retry_failed(broadcast: str, channel: str | None = None) -> dict[str, str]:
	"""Retry Failed or Partially Failed channel rows on a Broadcast.

	Moves Failed and Partially Failed rows to Queued and calls
	recipients.requeue_failed_recipients for that channel. Other statuses are skipped.

	Lifecycle functions are INTERNAL (not whitelisted, no permission check):
	the Task 12 API checks permission before calling them.

	Args:
		broadcast: Name of the Broadcast document.
		channel: Optional channel name. If None, applies to all eligible rows.

	Returns:
		dict[str, str]: Mapping of touched channel names to their new status.

	Raises:
		frappe.ValidationError: If channel is absent or transition is illegal.

	Side effects:
		Row-locks tabBroadcast, updates tabBroadcast Recipient rows,
		tabBroadcast Channel and tabBroadcast in DB.
		No commit inside.
	"""
	doc = frappe.get_doc("Broadcast", broadcast, for_update=True)
	target_rows = _get_target_rows(doc, channel)

	transitions: list[tuple[Any, str]] = []
	for row in target_rows:
		if row.status in ("Failed", "Partially Failed"):
			transitions.append((row, "Queued"))
		else:
			continue

	def _before_retry(b_doc: Any, b_row: Any) -> None:
		requeue_failed_recipients(b_doc.name, b_row.channel)

	return _apply_action_transitions(doc, transitions, before_apply_fn=_before_retry)

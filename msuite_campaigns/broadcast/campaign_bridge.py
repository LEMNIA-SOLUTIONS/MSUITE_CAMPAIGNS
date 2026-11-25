# Copyright (c) 2026, MSuite and contributors
# For license information, please see license.txt

"""Shared campaign bridge for Broadcast channel lifecycle controls (Task P3-1).

Provides channel adapters (WhatsApp, Email, SMS) with a unified interface to
inspect and control Broadcast records linked to a Marketing Campaign:
- `has_broadcast_rows(campaign, channel)`: R16 fallback decision.
- `rows_for(campaign, channel)`: Lists matching Broadcast rows respecting read
  permissions and channel roles.
- `preview_rows(campaign, channel)`: Returns the standard adapter preview row shape
  (`{name, title, status, recipients, ready, reason}`).
- `run(name, channel)`: Launches a single broadcast channel via `broadcast.hub.launch`.
- `pause(campaign, channel)`: Pauses Queued / In Progress rows via `broadcast.hub.pause`.
- `resume(campaign, channel)`: Resumes Paused rows via `broadcast.hub.resume`.
- `stop(campaign, channel)`: Cancels pre-flight and in-flight rows via `broadcast.hub.cancel`.
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe import _

from msuite_campaigns.broadcast import hub
from msuite_campaigns.broadcast.constants import (
	CHANNELS,
	STATUS_CANCELLED,
	STATUS_COMPLETED,
	STATUS_DRAFT,
	STATUS_IN_PROGRESS,
	STATUS_PAUSED,
	STATUS_QUEUED,
	STATUS_SCHEDULED,
)
from msuite_campaigns.broadcast.permissions import permitted_channels

OUTCOME_FIRED: str = "fired"
OUTCOME_SKIPPED: str = "skipped"
OUTCOME_FAILED: str = "failed"

PAUSEABLE_STATUSES: tuple[str, ...] = (STATUS_QUEUED, STATUS_IN_PROGRESS)
RESUMABLE_STATUSES: tuple[str, ...] = (STATUS_PAUSED,)
STOPPABLE_STATUSES: tuple[str, ...] = (
	STATUS_DRAFT,
	STATUS_SCHEDULED,
	STATUS_QUEUED,
	STATUS_IN_PROGRESS,
	STATUS_PAUSED,
)
READY_STATUSES: tuple[str, ...] = (STATUS_DRAFT, STATUS_SCHEDULED)


def has_broadcast_rows(campaign: str, channel: str) -> bool:
	"""Check if any Broadcast with the given channel is linked to campaign (R16).

	Used by channel adapters to decide whether to route lifecycle operations
	to the unified Broadcast model or fall back to legacy doctypes.

	Args:
		campaign: Name of the Marketing Campaign.
		channel: Channel identifier ('WhatsApp', 'Email', 'SMS').

	Returns:
		bool: True if at least one matching Broadcast row exists in the database.
	"""
	if not campaign or not channel:
		return False
	if not frappe.db.table_exists("Broadcast") or not frappe.db.table_exists("Broadcast Channel"):
		return False

	return bool(
		frappe.db.sql(
			"""
			SELECT 1
			FROM `tabBroadcast` b
			INNER JOIN `tabBroadcast Channel` c ON c.parent = b.name
			WHERE b.campaign = %s AND c.channel = %s
			LIMIT 1
			""",
			(campaign, channel),
		)
	)


def unmigrated(legacy_doctype: str, names: list[str]) -> list[str]:
	"""Filter legacy document names to only those that do NOT resolve to a Broadcast (R16').

	Legacy rows that resolve to an existing Broadcast are suppressed to prevent
	duplicate actions and sends.

	Args:
		legacy_doctype: DocType name (e.g. 'Bulk WhatsApp Message',
			'Bulk Email Message', 'Bulk SMS Message').
		names: List of legacy document names.

	Returns:
		list[str]: Subset of `names` that do not resolve to a Broadcast.
	"""
	if not legacy_doctype or not names:
		return []
	if not frappe.db.table_exists("Broadcast"):
		return list(names)
	from msuite_campaigns.broadcast.legacy import resolve

	return [name for name in names if not resolve(legacy_doctype, name)]



def rows_for(campaign: str, channel: str) -> list[dict[str, Any]]:
	"""Return Broadcasts linked to campaign that have a row for channel.

	Respects caller's read permissions and channel-role rules:
	- Caller must hold the role for `channel` (via `permitted_channels`).
	- Caller must have read permission on `Broadcast`.
	- Only returns broadcasts linked to `campaign` that contain a `Broadcast Channel`
	  row for `channel`.

	Args:
		campaign: Name of the Marketing Campaign.
		channel: Channel identifier ('WhatsApp', 'Email', 'SMS').

	Returns:
		list[dict[str, Any]]: List of matching broadcast and channel metadata dicts.
	"""
	if not campaign or channel not in CHANNELS:
		return []

	if not frappe.db.table_exists("Broadcast") or not frappe.db.table_exists("Broadcast Channel"):
		return []

	# Enforce channel-role rule
	user_channels = permitted_channels(frappe.session.user)
	if channel not in user_channels:
		return []

	# Enforce Broadcast read permission
	if not frappe.has_permission("Broadcast", "read"):
		return []

	candidate_bcs = frappe.get_list(
		"Broadcast",
		filters={"campaign": campaign},
		fields=["name", "title", "status", "scheduled_time", "total_recipients", "docstatus", "creation", "modified"],
		order_by="creation desc",
		limit=0,
	)

	if not candidate_bcs:
		return []

	allowed_bcs = [
		b for b in candidate_bcs
		if frappe.has_permission("Broadcast", "read", b["name"])
	]
	if not allowed_bcs:
		return []

	bc_names = [b["name"] for b in allowed_bcs]
	ch_rows = frappe.get_all(
		"Broadcast Channel",
		filters={"parent": ["in", bc_names], "channel": channel},
		fields=["name", "parent", "channel", "status", "recipient_count", "delivery_engine"],
	)
	ch_by_parent = {r["parent"]: r for r in ch_rows}

	out: list[dict[str, Any]] = []
	for b in allowed_bcs:
		b_name = b["name"]
		if b_name not in ch_by_parent:
			continue

		ch = ch_by_parent[b_name]
		rec_count = ch.get("recipient_count") or b.get("total_recipients") or 0
		ch_status = ch.get("status") or STATUS_DRAFT
		engine = ch.get("delivery_engine")

		out.append({
			"name": b_name,
			"title": b.get("title") or b_name,
			"status": ch_status,
			"broadcast_status": b.get("status") or STATUS_DRAFT,
			"channel_status": ch_status,
			"recipients": rec_count,
			"recipient_count": rec_count,
			"docstatus": b.get("docstatus", 0),
			"channel_row_name": ch.get("name"),
			"engine": engine,
			"delivery_engine": engine,
			"scheduled_time": b.get("scheduled_time"),
			"creation": b.get("creation"),
			"modified": b.get("modified"),
		})

	return out


def preview_rows(campaign: str, channel: str) -> list[dict[str, Any]]:
	"""Return preview rows matching the standard adapter shape.

	Row shape:
		{
			"name": str,
			"title": str,
			"status": str,
			"recipients": int,
			"ready": bool,
			"reason": str,
		}

	`ready` is True only for rows in Draft or Scheduled status with docstatus != 2.

	Args:
		campaign: Name of the Marketing Campaign.
		channel: Channel identifier ('WhatsApp', 'Email', 'SMS').

	Returns:
		list[dict[str, Any]]: List of preview rows for the campaign and channel.
	"""
	rows = rows_for(campaign, channel)
	out: list[dict[str, Any]] = []
	for r in rows:
		st = r.get("status") or STATUS_DRAFT
		docstatus = r.get("docstatus", 0)

		if st in READY_STATUSES and docstatus != 2:
			if not (r.get("recipients", 0) or 0):
				ready = False
				reason = _("No recipients")
			else:
				ready = True
				reason = ""
		else:
			ready = False
			if st == STATUS_CANCELLED or docstatus == 2:
				reason = _("Broadcast was cancelled")
			else:
				reason = _("Already {0}").format(st)

		out.append({
			"name": r["name"],
			"title": r.get("title") or r["name"],
			"status": st,
			"recipients": r.get("recipients", 0),
			"ready": ready,
			"reason": reason,
		})

	return out


def run(name: str, channel: str) -> dict[str, Any]:
	"""Launch a single broadcast channel row for a campaign run.

	Calls `broadcast.hub.launch(broadcast=name, channels=[channel])` once and
	returns the adapter outcome dictionary.

	Args:
		name: Name of the Broadcast document.
		channel: Channel identifier ('WhatsApp', 'Email', 'SMS').

	Returns:
		dict[str, Any]: Outcome dictionary with 'outcome' and 'message' (or 'reason').

	Raises:
		frappe.ValidationError: If channel is invalid.
		frappe.PermissionError: If caller lacks the channel role or write permission.
	"""
	if channel not in CHANNELS:
		frappe.throw(_("Invalid channel: {0}").format(channel), frappe.ValidationError)

	user_channels = permitted_channels(frappe.session.user)
	if channel not in user_channels:
		frappe.throw(
			_("User lacks permission for channel {0}").format(channel),
			frappe.PermissionError,
		)

	if not frappe.has_permission("Broadcast", "write", doc=name):
		frappe.throw(_("Not permitted"), frappe.PermissionError)

	launch_results = hub.launch(broadcast=name, channels=[channel])
	if isinstance(launch_results, dict) and "outcome" in launch_results:
		return launch_results

	ch_res = launch_results.get(channel) if isinstance(launch_results, dict) else {}
	if not isinstance(ch_res, dict):
		return {"outcome": OUTCOME_FIRED, "message": _("Fired")}

	if ch_res.get("error"):
		return {
			"outcome": OUTCOME_FAILED,
			"message": str(ch_res["error"]),
		}

	queued = ch_res.get("queued", 0) or 0
	if queued == 0:
		return {
			"outcome": OUTCOME_SKIPPED,
			"reason": _("No recipients queued"),
		}

	return {
		"outcome": OUTCOME_FIRED,
		"message": _("Queued {0} recipient(s)").format(queued),
	}


def pause(campaign: str, channel: str) -> list[dict[str, Any]]:
	"""Pause running channel rows on linked Broadcasts.

	Calls `broadcast.hub.pause` only for rows in Queued or In Progress status.
	AWS `frappe.ValidationError` is caught and returned as outcome 'skipped'
	with the reason so the loop continues with subsequent rows.

	Args:
		campaign: Name of the Marketing Campaign.
		channel: Channel identifier ('WhatsApp', 'Email', 'SMS').

	Returns:
		list[dict[str, Any]]: Per-row outcome dicts.
	"""
	rows = rows_for(campaign, channel)
	out: list[dict[str, Any]] = []
	for r in rows:
		st = r.get("status")
		base = {"name": r["name"], "status": st}

		if not frappe.has_permission("Broadcast", "write", doc=r["name"]):
			out.append({**base, "outcome": OUTCOME_SKIPPED, "reason": _("Not permitted")})
			continue

		if st not in PAUSEABLE_STATUSES:
			reason = _("Already paused.") if st == STATUS_PAUSED else _("Status is {0} — pause has no effect.").format(st)
			out.append({**base, "outcome": OUTCOME_SKIPPED, "reason": reason})
			continue

		try:
			hub.pause(broadcast=r["name"], channel=channel)
			out.append({**base, "status": STATUS_PAUSED, "outcome": OUTCOME_FIRED, "message": _("Paused")})
		except frappe.ValidationError as e:
			out.append({**base, "outcome": OUTCOME_SKIPPED, "reason": str(e)})
		except Exception as e:
			out.append({**base, "outcome": OUTCOME_FAILED, "message": str(e)[:200]})

	return out


def resume(campaign: str, channel: str) -> list[dict[str, Any]]:
	"""Resume paused channel rows on linked Broadcasts.

	Calls `broadcast.hub.resume` only for rows in Paused status. Other rows
	are skipped.

	Args:
		campaign: Name of the Marketing Campaign.
		channel: Channel identifier ('WhatsApp', 'Email', 'SMS').

	Returns:
		list[dict[str, Any]]: Per-row outcome dicts.
	"""
	rows = rows_for(campaign, channel)
	out: list[dict[str, Any]] = []
	for r in rows:
		st = r.get("status")
		base = {"name": r["name"], "status": st}

		if not frappe.has_permission("Broadcast", "write", doc=r["name"]):
			out.append({**base, "outcome": OUTCOME_SKIPPED, "reason": _("Not permitted")})
			continue

		if st not in RESUMABLE_STATUSES:
			out.append({**base, "outcome": OUTCOME_SKIPPED, "reason": _("Status is {0} — resume has no effect.").format(st)})
			continue

		try:
			res = hub.resume(broadcast=r["name"], channel=channel)
			new_st = (res.get(channel) if isinstance(res, dict) else None) or STATUS_QUEUED
			out.append({**base, "status": new_st, "outcome": OUTCOME_FIRED, "message": _("Resumed")})
		except Exception as e:
			out.append({**base, "outcome": OUTCOME_FAILED, "message": str(e)[:200]})

	return out


def stop(campaign: str, channel: str) -> list[dict[str, Any]]:
	"""Cancel stoppable channel rows on linked Broadcasts.

	Calls `broadcast.hub.cancel` only for rows whose status allows cancellation:
	Draft, Scheduled, Queued, In Progress, Paused.
	In-flight rows (Queued, In Progress) note that external provider sends
	cannot be recalled.

	Args:
		campaign: Name of the Marketing Campaign.
		channel: Channel identifier ('WhatsApp', 'Email', 'SMS').

	Returns:
		list[dict[str, Any]]: Per-row outcome dicts.
	"""
	rows = rows_for(campaign, channel)
	out: list[dict[str, Any]] = []
	for r in rows:
		st = r.get("status")
		base = {"name": r["name"], "status": st}

		if not frappe.has_permission("Broadcast", "write", doc=r["name"]):
			out.append({**base, "outcome": OUTCOME_SKIPPED, "reason": _("Not permitted")})
			continue

		if st not in STOPPABLE_STATUSES:
			out.append({**base, "outcome": OUTCOME_SKIPPED, "reason": _("Status is {0} — cannot cancel.").format(st)})
			continue

		try:
			hub.cancel(broadcast=r["name"], channel=channel)
			if st in (STATUS_QUEUED, STATUS_IN_PROGRESS):
				msg = _("Cancelled pending chunks; in-flight provider sends cannot be recalled.")
			else:
				msg = _("Cancelled.")
			out.append({**base, "status": STATUS_CANCELLED, "outcome": OUTCOME_FIRED, "message": msg})
		except Exception as e:
			out.append({**base, "outcome": OUTCOME_FAILED, "message": str(e)[:200]})

	return out

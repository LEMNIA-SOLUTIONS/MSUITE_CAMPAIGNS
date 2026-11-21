# Copyright (c) 2026, MSuite and contributors
# For license information, please see license.txt

"""Broadcast whitelisted API (Task 12).

Provides whitelisted methods called by the frontend SPA for listing, metrics,
full document access, save/update, lifecycle operations (launch, pause, resume,
cancel, retry), batch deletion, progress monitoring, recipient inspection,
export, and cloning.

All methods strictly enforce channel-aware P1 permissions via frappe.has_permission
and permission_query_conditions before any read or write operation.
"""

from __future__ import annotations

import json
from typing import Any

import frappe
from frappe import _
from frappe.utils import add_days, cint, getdate, today

from msuite_campaigns.broadcast.constants import (
	CHANNEL_EMAIL,
	CHANNEL_SMS,
	CHANNEL_WHATSAPP,
	STATUS_CANCELLED,
	STATUS_COMPLETED,
	STATUS_DRAFT,
	STATUS_FAILED,
	STATUS_IN_PROGRESS,
	STATUS_PARTIALLY_FAILED,
	STATUS_PAUSED,
	STATUS_QUEUED,
	STATUS_SCHEDULED,
	WRITABLE_BROADCAST_FIELDS,
	WRITABLE_CHANNEL_FIELDS,
)
from msuite_campaigns.broadcast.permissions import permitted_channels

MAX_PAGE_LENGTH: int = 100
EXPORT_LIMIT: int = 10000
MAX_BULK_DELETE_LIMIT: int = 50


def _loads(raw: Any, default: Any) -> Any:
	"""Safely decode JSON string or return existing data structure or default.

	Params:
		raw: Raw JSON string, dict, list, or None.
		default: Fallback value if decoding fails or raw is None/empty.

	Returns:
		Any: Parsed object or default.

	Exceptions:
		None.

	Side effects:
		None.
	"""
	if raw is None or raw == "":
		return default
	if isinstance(raw, (dict, list)):
		return raw
	try:
		return json.loads(raw)
	except (json.JSONDecodeError, TypeError):
		return default


@frappe.whitelist()
def list_broadcasts(
	filters: str = "[]",
	search: str = "",
	status: str = "",
	template: str = "",
	date_range: str = "",
	date_from: str = "",
	date_to: str = "",
	start: int = 0,
	limit: int = 20,
) -> dict:
	"""List Broadcasts matching filter, search, date range, and pagination criteria.

	Filters rows strictly according to user permissions via frappe.get_list.
	Child channel rows are filtered to only permitted channels.

	Params:
		filters: JSON array of Frappe filter expressions.
		search: Optional search term matching Broadcast title.
		status: Optional status string ('inProgress' matches Queued and In Progress).
		template: Optional substring matching template names across channel rows.
		date_range: Optional date range preset ('today', '7days', '30days', 'custom').
		date_from: Start date string when date_range is 'custom'.
		date_to: End date string when date_range is 'custom'.
		start: 0-indexed pagination offset.
		limit: Number of records to return, capped at MAX_PAGE_LENGTH.

	Returns:
		dict: {"rows": list[dict], "total": int}.

	Exceptions:
		frappe.PermissionError: If current user lacks read permission on Broadcast.

	Side effects:
		Queries the database.
	"""
	frappe.has_permission("Broadcast", "read", throw=True)

	start = max(0, cint(start))
	limit = cint(limit) if cint(limit) > 0 else 20
	limit = min(limit, MAX_PAGE_LENGTH)

	raw_filters = _loads(filters, [])
	parsed_filters: list[Any] = []
	if isinstance(raw_filters, list):
		parsed_filters.extend(raw_filters)

	if search and str(search).strip():
		parsed_filters.append(["title", "like", f"%{str(search).strip()}%"])

	if status and str(status).strip():
		st = str(status).strip()
		if st == "inProgress":
			parsed_filters.append(["status", "in", [STATUS_QUEUED, STATUS_IN_PROGRESS]])
		else:
			parsed_filters.append(["status", "=", st])

	if template and str(template).strip():
		tpl_pattern = f"%{str(template).strip()}%"
		matching_parents = frappe.db.sql_list(
			"""
			SELECT DISTINCT parent FROM `tabBroadcast Channel`
			WHERE whatsapp_template LIKE %(pattern)s
			   OR email_template LIKE %(pattern)s
			   OR sms_template LIKE %(pattern)s
			""",
			{"pattern": tpl_pattern},
		)
		if not matching_parents:
			return {"rows": [], "total": 0}
		parsed_filters.append(["name", "in", matching_parents])

	if date_range:
		dr = str(date_range).strip().lower()
		today_date = today()
		if dr == "today":
			parsed_filters.append(["modified", ">=", today_date])
		elif dr == "7days":
			parsed_filters.append(["modified", ">=", str(add_days(today_date, -7))])
		elif dr == "30days":
			parsed_filters.append(["modified", ">=", str(add_days(today_date, -30))])
		elif dr == "custom":
			if date_from and str(date_from).strip():
				try:
					valid_from = str(getdate(str(date_from).strip()))
					parsed_filters.append(["modified", ">=", valid_from])
				except Exception:
					pass
			if date_to and str(date_to).strip():
				try:
					valid_to = str(getdate(str(date_to).strip()))
					parsed_filters.append(["modified", "<=", f"{valid_to} 23:59:59"])
				except Exception:
					pass

	total_res = frappe.get_list(
		"Broadcast",
		filters=parsed_filters,
		fields=[{"COUNT": "name", "as": "total"}],
	)
	total = cint(total_res[0].get("total")) if total_res else 0

	if total == 0 or start >= total:
		return {"rows": [], "total": total}

	docs = frappe.get_list(
		"Broadcast",
		filters=parsed_filters,
		fields=[
			"name",
			"title",
			"status",
			"campaign",
			"recipient_source",
			"recipient_list",
			"total_recipients",
			"scheduled_time",
			"creation",
			"modified",
			"owner",
		],
		order_by="modified desc",
		start=start,
		limit_page_length=limit,
	)

	user_channels = permitted_channels(frappe.session.user)
	broadcast_names = [d["name"] for d in docs]
	channels_by_parent: dict[str, list[dict]] = {name: [] for name in broadcast_names}

	if broadcast_names:
		all_channels = frappe.get_all(
			"Broadcast Channel",
			filters={"parent": ["in", broadcast_names]},
			fields=[
				"parent",
				"channel",
				"status",
				"whatsapp_template",
				"whatsapp_account",
				"email_template",
				"email_account",
				"sms_template",
				"sms_account",
				"recipient_count",
				"sent_count",
				"delivered_count",
				"failed_count",
				"idx",
			],
			order_by="idx asc",
		)
		for ch in all_channels:
			if ch["channel"] not in user_channels:
				continue
			template_name = None
			account_name = None
			if ch["channel"] == CHANNEL_WHATSAPP:
				template_name = ch.get("whatsapp_template")
				account_name = ch.get("whatsapp_account")
			elif ch["channel"] == CHANNEL_EMAIL:
				template_name = ch.get("email_template")
				account_name = ch.get("email_account")
			elif ch["channel"] == CHANNEL_SMS:
				template_name = ch.get("sms_template")
				account_name = ch.get("sms_account")

			channels_by_parent[ch["parent"]].append({
				"channel": ch["channel"],
				"status": ch["status"],
				"template": template_name,
				"account": account_name,
				"recipient_count": cint(ch.get("recipient_count")),
				"sent_count": cint(ch.get("sent_count")),
				"delivered_count": cint(ch.get("delivered_count")),
				"failed_count": cint(ch.get("failed_count")),
			})

	for row in docs:
		row["recipient_count"] = cint(row.get("total_recipients"))
		row["channels"] = channels_by_parent.get(row["name"], [])

	return {"rows": docs, "total": total}


@frappe.whitelist()
def get_broadcast_metrics() -> dict:
	"""Return aggregate KPI counts of permitted Broadcast documents.

	Params:
		None.

	Returns:
		dict: {
			"total": int,
			"draft": int,
			"scheduled": int,
			"inProgress": int,
			"completed": int,
			"failed": int,
			"cancelled": int,
		}.

	Exceptions:
		frappe.PermissionError: If current user lacks read permission on Broadcast.

	Side effects:
		Queries the database.
	"""
	frappe.has_permission("Broadcast", "read", throw=True)

	rows = frappe.get_list(
		"Broadcast",
		fields=["status", {"COUNT": "name", "as": "count"}],
		group_by="status",
	)
	metrics = {
		"total": 0,
		"draft": 0,
		"scheduled": 0,
		"inProgress": 0,
		"completed": 0,
		"failed": 0,
		"cancelled": 0,
	}

	for r in rows:
		st = r.get("status")
		cnt = cint(r.get("count"))
		metrics["total"] += cnt
		if st == STATUS_DRAFT:
			metrics["draft"] += cnt
		elif st == STATUS_SCHEDULED:
			metrics["scheduled"] += cnt
		elif st in (STATUS_QUEUED, STATUS_IN_PROGRESS):
			metrics["inProgress"] += cnt
		elif st == STATUS_COMPLETED:
			metrics["completed"] += cnt
		elif st in (STATUS_FAILED, STATUS_PARTIALLY_FAILED):
			metrics["failed"] += cnt
		elif st == STATUS_CANCELLED:
			metrics["cancelled"] += cnt

	return metrics


@frappe.whitelist()
def get_broadcast(name: str) -> dict:
	"""Retrieve complete Broadcast document, filtered for permitted channels.

	Includes child channel rows, products, per-channel channel_metrics, and launch_error.

	Params:
		name: Broadcast document name.

	Returns:
		dict: Serialized Broadcast document dictionary.

	Exceptions:
		frappe.PermissionError: If current user lacks read permission on the Broadcast.
		frappe.DoesNotExistError: If document does not exist.

	Side effects:
		Queries the database.
	"""
	frappe.has_permission("Broadcast", "read", doc=name, throw=True)
	doc = frappe.get_doc("Broadcast", name)
	doc_dict = doc.as_dict()

	user_channels = permitted_channels(frappe.session.user)
	doc_dict["channels"] = [ch for ch in doc_dict.get("channels", []) if ch.get("channel") in user_channels]

	if CHANNEL_WHATSAPP not in user_channels:
		doc_dict["products"] = []

	for ch in doc_dict.get("channels", []):
		ch_name = ch.get("channel")
		try:
			from msuite_campaigns.broadcast import rollup
			ch["channel_metrics"] = rollup.channel_metrics(doc, ch_name)
		except Exception:
			ch["channel_metrics"] = {}

	return doc_dict


def _sanitize_channel_row(ch: dict) -> dict:
	"""Sanitize child channel dict by enforcing writable field whitelist and zeroing counters.

	Params:
		ch: Dictionary of channel fields.

	Returns:
		dict: Sanitized channel row dictionary.

	Exceptions:
		None.

	Side effects:
		None.
	"""
	row = {k: ch[k] for k in WRITABLE_CHANNEL_FIELDS if k in ch}
	if row.get("channel") != CHANNEL_WHATSAPP:
		row["variable_type"] = ""
	row["status"] = STATUS_DRAFT
	row["recipient_count"] = 0
	row["sent_count"] = 0
	row["delivered_count"] = 0
	row["read_count"] = 0
	row["failed_count"] = 0
	row["bounced_count"] = 0
	row["opened_count"] = 0
	row["clicked_count"] = 0
	row["unsubscribed_count"] = 0
	row["retry_count"] = 0
	row["estimated_credits"] = 0
	row["actual_credits"] = 0
	row["launch_error"] = None
	return row


@frappe.whitelist()
def save_broadcast(
	doc: str,
	launch: int = 0,
	individual_rows: str = "[]",
	name: str = "",
) -> dict:
	"""Create or update a Broadcast document, optionally launching it.

	When name is passed, updates existing document (only permissible while status
	is Draft or Scheduled; throws ValidationError otherwise).

	Params:
		doc: JSON string of Broadcast fields and child tables.
		launch: 1 to launch immediately via hub.launch, 0 to save only.
		individual_rows: JSON string of recipient rows for Individual source.
		name: Optional Broadcast name to update in place.

	Returns:
		dict: {"name": str, "status": str, "results": dict}.

	Exceptions:
		frappe.PermissionError: If user lacks create or write permissions.
		frappe.ValidationError: If document validation fails or status is not Draft/Scheduled.

	Side effects:
		Inserts or updates rows in tabBroadcast and child tables; may enqueue dispatch.
	"""
	if not frappe.session.user or frappe.session.user == "Guest":
		frappe.throw(_("Authentication required."), frappe.PermissionError)

	data = _loads(doc, {})

	if name:
		frappe.has_permission("Broadcast", "write", doc=name, throw=True)
		b_doc = frappe.get_doc("Broadcast", name)
		if b_doc.status not in (STATUS_DRAFT, STATUS_SCHEDULED):
			frappe.throw(
				_("Cannot update broadcast with status '{0}'.").format(b_doc.status),
				frappe.ValidationError,
			)

		for field in WRITABLE_BROADCAST_FIELDS:
			if field in data:
				b_doc.set(field, data[field])

		if "channels" in data:
			b_doc.set("channels", [])
			for ch in data["channels"]:
				if isinstance(ch, dict):
					b_doc.append("channels", _sanitize_channel_row(ch))

		if "products" in data:
			b_doc.set("products", [])
			for prod in data["products"]:
				item_code = prod.get("item_code") if isinstance(prod, dict) else prod
				b_doc.append("products", {"item_code": item_code})

		frappe.has_permission("Broadcast", "write", doc=b_doc, throw=True)
		b_doc.save()
	else:
		b_doc = frappe.new_doc("Broadcast")
		b_doc.status = STATUS_DRAFT
		b_doc.total_recipients = 0
		b_doc.recipient_count = 0
		b_doc.sent_count = 0
		b_doc.delivered_count = 0
		b_doc.read_count = 0
		b_doc.failed_count = 0
		b_doc.launched_at = None
		b_doc.launch_error = None
		b_doc.legacy_doctype = None
		b_doc.legacy_name = None

		for field in WRITABLE_BROADCAST_FIELDS:
			if field in data:
				b_doc.set(field, data[field])

		b_doc.set("channels", [])
		for ch in data.get("channels", []):
			if isinstance(ch, dict):
				b_doc.append("channels", _sanitize_channel_row(ch))

		b_doc.set("products", [])
		for prod in data.get("products", []):
			item_code = prod.get("item_code") if isinstance(prod, dict) else prod
			b_doc.append("products", {"item_code": item_code})

		frappe.has_permission("Broadcast", "create", doc=b_doc, throw=True)
		b_doc.insert()

	results: dict[str, Any] = {}
	if cint(launch):
		parsed_rows = _loads(individual_rows, [])
		try:
			from msuite_campaigns.broadcast import hub
			results = hub.launch(b_doc.name, individual_rows=parsed_rows)
		except ImportError:
			from msuite_campaigns.broadcast import lifecycle
			results = lifecycle.launch(b_doc.name)

		b_doc.reload()

	return {"name": b_doc.name, "status": b_doc.status, "results": results}


@frappe.whitelist()
def launch_broadcast(name: str, individual_rows: str = "[]") -> dict:
	"""Launch a Broadcast via hub.launch or lifecycle.launch.

	Params:
		name: Broadcast document name.
		individual_rows: JSON string of individual recipient rows.

	Returns:
		dict: Mapping of channel name to its resulting status string.

	Exceptions:
		frappe.PermissionError: If user lacks write permission on Broadcast.
		frappe.ValidationError: If Broadcast invariants or plan gates fail.

	Side effects:
		Updates Broadcast and channel statuses in the database; enqueues tasks.
	"""
	frappe.has_permission("Broadcast", "write", doc=name, throw=True)
	parsed_rows = _loads(individual_rows, [])

	try:
		from msuite_campaigns.broadcast import hub
		raw_results = hub.launch(name, individual_rows=parsed_rows)
	except ImportError:
		from msuite_campaigns.broadcast import lifecycle
		raw_results = lifecycle.launch(name)

	results: dict[str, str] = {}
	for ch, val in (raw_results or {}).items():
		if isinstance(val, dict):
			results[ch] = val.get("status", STATUS_QUEUED)
		else:
			results[ch] = str(val)

	return results


@frappe.whitelist()
def pause_broadcast(name: str, channel: str = "") -> dict:
	"""Pause running channel rows on a Broadcast.

	Params:
		name: Broadcast document name.
		channel: Channel name or empty string to apply to all channels.

	Returns:
		dict: Mapping of channel name to new status.

	Exceptions:
		frappe.PermissionError: If user lacks write permission on Broadcast.
		frappe.ValidationError: If transition is illegal.

	Side effects:
		Updates Broadcast and channel row statuses in DB.
	"""
	frappe.has_permission("Broadcast", "write", doc=name, throw=True)
	from msuite_campaigns.broadcast import lifecycle

	target_channel = str(channel).strip() if channel and str(channel).strip() else None
	return lifecycle.pause(name, channel=target_channel)


@frappe.whitelist()
def resume_broadcast(name: str, channel: str = "") -> dict:
	"""Resume paused channel rows on a Broadcast.

	Params:
		name: Broadcast document name.
		channel: Channel name or empty string to apply to all channels.

	Returns:
		dict: Mapping of channel name to new status.

	Exceptions:
		frappe.PermissionError: If user lacks write permission on Broadcast.
		frappe.ValidationError: If transition is illegal.

	Side effects:
		Updates Broadcast and channel row statuses in DB.
	"""
	frappe.has_permission("Broadcast", "write", doc=name, throw=True)
	from msuite_campaigns.broadcast import lifecycle

	target_channel = str(channel).strip() if channel and str(channel).strip() else None
	return lifecycle.resume(name, channel=target_channel)


@frappe.whitelist()
def cancel_broadcast(name: str, channel: str = "") -> dict:
	"""Cancel non-terminal channel rows on a Broadcast.

	Params:
		name: Broadcast document name.
		channel: Channel name or empty string to apply to all channels.

	Returns:
		dict: Mapping of channel name to new status.

	Exceptions:
		frappe.PermissionError: If user lacks write permission on Broadcast.
		frappe.ValidationError: If transition is illegal.

	Side effects:
		Updates Broadcast and channel row statuses in DB.
	"""
	frappe.has_permission("Broadcast", "write", doc=name, throw=True)
	from msuite_campaigns.broadcast import lifecycle

	target_channel = str(channel).strip() if channel and str(channel).strip() else None
	return lifecycle.cancel(name, channel=target_channel)


@frappe.whitelist()
def retry_failed_broadcast(name: str, channel: str = "") -> dict:
	"""Retry Failed or Partially Failed channel rows on a Broadcast.

	Params:
		name: Broadcast document name.
		channel: Channel name or empty string to apply to all channels.

	Returns:
		dict: Mapping of channel name to new status.

	Exceptions:
		frappe.PermissionError: If user lacks write permission on Broadcast.
		frappe.ValidationError: If transition is illegal.

	Side effects:
		Updates Broadcast, channel rows, and recipient rows in DB.
	"""
	frappe.has_permission("Broadcast", "write", doc=name, throw=True)
	from msuite_campaigns.broadcast import lifecycle

	target_channel = str(channel).strip() if channel and str(channel).strip() else None
	return lifecycle.retry_failed(name, channel=target_channel)


@frappe.whitelist()
def delete_broadcasts(names: str) -> dict:
	"""Delete multiple Broadcast documents in batch, returning per-item outcomes.

	Requires delete permission per document, refuses deletion of Queued or In Progress
	broadcasts, and caps the input size to MAX_BULK_DELETE_LIMIT.

	Params:
		names: JSON string of Broadcast document names.

	Returns:
		dict: {"deleted": list[str], "failed": list[dict[str, str]]}.

	Exceptions:
		frappe.PermissionError: If user is Guest.
		frappe.ValidationError: If input list exceeds MAX_BULK_DELETE_LIMIT.

	Side effects:
		Deletes tabBroadcast records and cascading recipients from database.
	"""
	if not frappe.session.user or frappe.session.user == "Guest":
		frappe.throw(_("Authentication required."), frappe.PermissionError)

	name_list = _loads(names, [])
	if not isinstance(name_list, list):
		name_list = [names]

	if len(name_list) > MAX_BULK_DELETE_LIMIT:
		frappe.throw(
			_("Cannot delete more than {0} broadcasts at once.").format(MAX_BULK_DELETE_LIMIT),
			frappe.ValidationError,
		)

	deleted: list[str] = []
	failed: list[dict[str, str]] = []

	for item in name_list:
		if not item or not isinstance(item, str):
			continue
		b_name = item.strip()
		if not frappe.db.exists("Broadcast", b_name):
			failed.append({"name": b_name, "reason": "Not found"})
			continue

		if not frappe.has_permission("Broadcast", "delete", doc=b_name):
			failed.append({"name": b_name, "reason": "Permission denied"})
			continue

		status = frappe.db.get_value("Broadcast", b_name, "status")
		if status in (STATUS_QUEUED, STATUS_IN_PROGRESS):
			failed.append({
				"name": b_name,
				"reason": f"Cannot delete broadcast in status '{status}'.",
			})
			continue

		try:
			frappe.delete_doc("Broadcast", b_name, ignore_permissions=True)
			deleted.append(b_name)
		except Exception as e:
			failed.append({"name": b_name, "reason": str(e)})

	return {"deleted": deleted, "failed": failed}


@frappe.whitelist()
def get_broadcast_progress(name: str) -> dict:
	"""Return per-channel progress counters without recipient table scans.

	Params:
		name: Broadcast document name.

	Returns:
		dict: {
			channel: {
				"status": str,
				"recipient_count": int,
				"sent_count": int,
				"delivered_count": int,
				"read_count": int,
				"failed_count": int,
				"percent_sent": float,
			}
		}.

	Exceptions:
		frappe.PermissionError: If user lacks read permission on Broadcast.

	Side effects:
		Queries the database.
	"""
	frappe.has_permission("Broadcast", "read", doc=name, throw=True)
	user_channels = permitted_channels(frappe.session.user)

	channel_rows = frappe.get_all(
		"Broadcast Channel",
		filters={"parent": name},
		fields=[
			"channel",
			"status",
			"recipient_count",
			"sent_count",
			"delivered_count",
			"read_count",
			"failed_count",
		],
	)

	progress: dict[str, dict[str, Any]] = {}
	for row in channel_rows:
		if row.channel not in user_channels:
			continue
		rec_count = cint(row.recipient_count)
		sent_count = cint(row.sent_count)
		pct = round((sent_count / rec_count) * 100, 2) if rec_count > 0 else 0.0

		progress[row.channel] = {
			"status": row.status,
			"recipient_count": rec_count,
			"sent_count": sent_count,
			"delivered_count": cint(row.delivered_count),
			"read_count": cint(row.read_count),
			"failed_count": cint(row.failed_count),
			"percent_sent": pct,
		}

	return progress


@frappe.whitelist()
def get_broadcast_recipients(
	name: str,
	channel: str = "",
	status: str = "",
	search: str = "",
	start: int = 0,
	length: int = 50,
	export: int = 0,
) -> dict:
	"""Fetch recipient rows for a Broadcast with search, filtering, and export.

	Rows are strictly channel-permission filtered via recipient_query_conditions.

	Params:
		name: Broadcast document name.
		channel: Optional channel filter.
		status: Optional recipient status filter.
		search: Optional search matching address or recipient name.
		start: 0-indexed pagination offset.
		length: Page size, capped at MAX_PAGE_LENGTH.
		export: 1 to export up to EXPORT_LIMIT rows with truncation flag.

	Returns:
		dict: {"rows": list[dict], "total": int, ["truncated": bool]}.

	Exceptions:
		frappe.PermissionError: If user lacks read permissions.

	Side effects:
		Queries the database.
	"""
	frappe.has_permission("Broadcast", "read", doc=name, throw=True)
	frappe.has_permission("Broadcast Recipient", "read", throw=True)

	user_channels = permitted_channels(frappe.session.user)
	target_channel = str(channel).strip() if channel and str(channel).strip() else ""

	if target_channel and target_channel not in user_channels:
		return {"rows": [], "total": 0}

	filters: list[Any] = [["broadcast", "=", name]]
	if target_channel:
		filters.append(["channel", "=", target_channel])
	if status and str(status).strip():
		filters.append(["status", "=", str(status).strip()])

	or_filters: list[Any] = []
	if search and str(search).strip():
		term = f"%{str(search).strip()}%"
		or_filters = [["address", "like", term], ["recipient_name", "like", term]]

	fields = [
		"name",
		"broadcast",
		"channel",
		"address",
		"recipient_name",
		"status",
		"error_code",
		"failure_reason",
		"is_retryable",
		"retry_count",
		"provider_message_id",
		"sent_at",
		"delivered_at",
		"read_at",
		"opened_at",
		"clicked_at",
		"segments",
		"credits",
		"source_contact",
		"recipient_data",
	]

	if cint(export):
		rows = frappe.get_list(
			"Broadcast Recipient",
			filters=filters,
			or_filters=or_filters or None,
			fields=fields,
			order_by="idx asc, creation asc",
			start=0,
			limit_page_length=EXPORT_LIMIT + 1,
		)
		truncated = len(rows) > EXPORT_LIMIT
		if truncated:
			rows = rows[:EXPORT_LIMIT]
		return {"rows": rows, "total": len(rows), "truncated": truncated}

	start = max(0, cint(start))
	length = cint(length) if cint(length) > 0 else 50
	length = min(length, MAX_PAGE_LENGTH)

	if target_channel and not status and not search:
		ch_count = frappe.db.get_value(
			"Broadcast Channel",
			{"parent": name, "channel": target_channel},
			"recipient_count",
		)
		total = cint(ch_count)
	else:
		total_res = frappe.get_list(
			"Broadcast Recipient",
			filters=filters,
			or_filters=or_filters or None,
			fields=[{"COUNT": "name", "as": "total"}],
		)
		total = cint(total_res[0].get("total")) if total_res else 0

	if total == 0 or start >= total:
		return {"rows": [], "total": total}

	rows = frappe.get_list(
		"Broadcast Recipient",
		filters=filters,
		or_filters=or_filters or None,
		fields=fields,
		order_by="idx asc, creation asc",
		start=start,
		limit_page_length=length,
	)

	return {"rows": rows, "total": total}


@frappe.whitelist()
def clone_broadcast(name: str) -> dict:
	"""Clone an existing Broadcast into a new Draft Broadcast without a schedule.

	Copies child channel rows, products, recipient source, and title with suffix ' (copy)'.
	Resets channel statuses to 'Draft' and clears counter fields.

	Params:
		name: Name of existing Broadcast to clone.

	Returns:
		dict: Cloned Broadcast document dictionary.

	Exceptions:
		frappe.PermissionError: If user lacks read or create permissions.
		frappe.DoesNotExistError: If document does not exist.

	Side effects:
		Inserts a new Broadcast document in the database.
	"""
	frappe.has_permission("Broadcast", "read", doc=name, throw=True)
	doc = frappe.get_doc("Broadcast", name)

	if not frappe.has_permission("Broadcast", "create", doc=doc):
		frappe.throw(
			_("Cloning requires access to every channel on the broadcast."),
			frappe.PermissionError,
		)

	new_doc = frappe.new_doc("Broadcast")
	new_doc.title = f"{doc.title} (copy)"
	new_doc.description = doc.description
	new_doc.campaign = doc.campaign
	new_doc.recipient_source = doc.recipient_source
	new_doc.recipient_list = doc.recipient_list
	new_doc.status = STATUS_DRAFT
	new_doc.scheduled_time = None
	new_doc.launched_at = None
	new_doc.launch_error = None
	new_doc.total_recipients = 0

	for ch in doc.channels or []:
		ch_dict = ch.as_dict()
		for k in (
			"name",
			"owner",
			"creation",
			"modified",
			"modified_by",
			"parent",
			"parenttype",
			"parentfield",
			"idx",
		):
			ch_dict.pop(k, None)
		if ch_dict.get("channel") != CHANNEL_WHATSAPP:
			ch_dict["variable_type"] = ""
		ch_dict["status"] = STATUS_DRAFT
		ch_dict["recipient_count"] = 0
		ch_dict["sent_count"] = 0
		ch_dict["delivered_count"] = 0
		ch_dict["read_count"] = 0
		ch_dict["failed_count"] = 0
		ch_dict["bounced_count"] = 0
		ch_dict["opened_count"] = 0
		ch_dict["clicked_count"] = 0
		ch_dict["unsubscribed_count"] = 0
		ch_dict["retry_count"] = 0
		ch_dict["launch_error"] = None
		new_doc.append("channels", ch_dict)

	for prod in doc.products or []:
		new_doc.append("products", {"item_code": prod.item_code})

	new_doc.insert()
	return new_doc.as_dict()

"""
Campaign Calendar REST API.

Endpoints
---------
get_calendar_events(from_date, to_date, types=None, campaign=None)
    Returns Campaign Calendar Event rows within the given date range.
    Optionally filtered to specific event_types and/or a parent campaign.

reschedule_event(event_type, source_name, start_date, end_date)
    Updates the source document's date fields and re-syncs the calendar
    event. Returns the updated calendar event dict.
"""

from __future__ import annotations

import json
from datetime import datetime

import frappe
from frappe import _

from msuite_campaigns.campaign.doctype.campaign_calendar_event.campaign_calendar_event import (
	_EVENT_TYPE_MAP,
	sync_from_source,
)

# ── Reverse map: event_type → (doctype, start_field, end_field) ─────────
_SOURCE_DATE_FIELDS = {
	"Campaign": ("Marketing Campaign", "start_date", "end_date"),
	"Social Post": ("Social Post", "scheduled_at", "scheduled_at"),
	"Ad Campaign": ("Ad Campaign", "start_date", "end_date"),
	"WhatsApp Message": ("Bulk WhatsApp Message", "scheduled_time", "scheduled_time"),
	"Task": ("CRM Task", "due_date", "due_date"),
	"Meeting": ("Event", "starts_on", "ends_on"),
}

_ALL_TYPES = list(_SOURCE_DATE_FIELDS.keys())


@frappe.whitelist()
def get_calendar_events(
	from_date: str,
	to_date: str,
	types: str | None = None,
	campaign: str | None = None,
) -> list[dict]:
	"""Return calendar events in [from_date, to_date].

	Args:
	    from_date: ISO date/datetime string — start of window (inclusive).
	    to_date:   ISO date/datetime string — end of window (inclusive).
	    types:     JSON array of event_type strings to include.
	               Defaults to all types (Campaign, Social Post, Ad Campaign, WhatsApp Message, Task, Meeting).
	    campaign:  Marketing Campaign name to scope the results.
	               When omitted, returns events across all campaigns.

	Returns:
	    List of dicts with keys:
	        name, event_type, source_name, title, status, campaign,
	        start_date, end_date, platform, color_hint, extra_json
	"""
	# Parse types filter
	if isinstance(types, list):
		requested_types = types
	elif isinstance(types, str):
		try:
			requested_types = json.loads(types)
		except Exception:
			requested_types = [t.strip() for t in types.split(",") if t.strip()]
	else:
		requested_types = _ALL_TYPES

	valid_types = [
		t for t in requested_types
		if t in _ALL_TYPES and frappe.has_permission(_SOURCE_DATE_FIELDS[t][0], "read")
	]
	if not valid_types:
		return []

	if campaign and not frappe.has_permission("Marketing Campaign", "read", campaign):
		return []

	# 1. Fetch marketing events from Campaign Calendar Event
	mkt_types = [t for t in valid_types if t in ("Campaign", "Social Post", "Ad Campaign", "WhatsApp Message")]
	rows = []

	if mkt_types:
		filters = [
			["Campaign Calendar Event", "event_type", "in", mkt_types],
			["Campaign Calendar Event", "start_date", ">=", from_date],
			["Campaign Calendar Event", "start_date", "<=", to_date],
		]

		overlap_filters = [
			["Campaign Calendar Event", "event_type", "in", mkt_types],
			["Campaign Calendar Event", "start_date", "<", from_date],
			["Campaign Calendar Event", "end_date", ">=", from_date],
			["Campaign Calendar Event", "end_date", "<=", to_date],
		]

		if campaign:
			filters.append(["Campaign Calendar Event", "campaign", "=", campaign])
			overlap_filters.append(["Campaign Calendar Event", "campaign", "=", campaign])

		fields = [
			"name",
			"event_type",
			"source_name",
			"title",
			"status",
			"campaign",
			"start_date",
			"end_date",
			"platform",
			"color_hint",
			"extra_json",
		]

		rows = frappe.get_all(
			"Campaign Calendar Event",
			filters=filters,
			fields=fields,
			order_by="start_date asc",
			limit=500,
		)

		# Merge overlap rows
		seen = {r["name"] for r in rows}
		try:
			overlap_rows = frappe.get_all(
				"Campaign Calendar Event",
				filters=overlap_filters,
				fields=fields,
				order_by="start_date asc",
				limit=500,
			)
			for r in overlap_rows:
				if r["name"] not in seen:
					rows.append(r)
					seen.add(r["name"])
		except Exception:
			pass

	# 2. Fetch Tasks if requested and not scoped to an unrelated campaign
	# Same CRM Task list as the Task module; get_list applies the user's permissions.
	if "Task" in valid_types and not campaign:
		date_start = from_date[:10]
		date_end = to_date[:10]
		tasks = frappe.get_list(
			"CRM Task",
			fields=["name", "title", "description", "priority", "status", "due_date", "creation"],
			filters=[["status", "!=", "Canceled"]],
			or_filters=[
				["due_date", "between", [date_start, f"{date_end} 23:59:59"]],
				["due_date", "is", "not set"],
			],
			order_by="due_date asc",
			limit=100,
		)
		for t in tasks:
			created = str(t.creation)[:10]
			if not t.due_date and not (date_start <= created <= date_end):
				continue
			due_date = str(t.due_date)[:10] if t.due_date else created
			start_iso = f"{due_date} 09:00:00"
			end_iso = f"{due_date} 10:00:00"
			status_mapped = "Completed" if t.status == "Done" else "Scheduled"
			rows.append(
				{
					"name": f"task-{t.name}",
					"event_type": "Task",
					"source_name": t.name,
					"title": t.title or t.name,
					"status": status_mapped,
					"campaign": "",
					"start_date": start_iso,
					"end_date": end_iso,
					"platform": "Task",
					"color_hint": "amber",
					"extra_json": json.dumps({"priority": t.priority or "Medium", "desc": t.description or ""}),
				}
			)

	# 3. Fetch Meetings (Frappe Event / Google / Outlook) if requested
	if "Meeting" in valid_types and not campaign and frappe.db.exists("DocType", "Event"):
		user = frappe.session.user
		events = frappe.db.sql(
			"""
			SELECT DISTINCT
				e.name, e.subject, e.description, e.starts_on, e.ends_on, e.all_day,
				e.status, COALESCE(e.google_calendar, '') as google_calendar
			FROM `tabEvent` e
			LEFT JOIN `tabEvent Participants` ep ON ep.parent = e.name
			WHERE
				(e.event_type = 'Public' OR e.owner = %(user)s OR ep.email = %(user)s OR ep.reference_docname = %(user)s OR %(user)s = 'Administrator')
				AND (
					(e.starts_on >= %(from_dt)s AND e.starts_on <= %(to_dt)s)
					OR (e.ends_on >= %(from_dt)s AND e.ends_on <= %(to_dt)s)
					OR (e.starts_on <= %(from_dt)s AND e.ends_on >= %(from_dt)s)
				)
			ORDER BY e.starts_on ASC
			LIMIT 100
			""",
			{"user": user, "from_dt": from_date, "to_dt": to_date},
			as_dict=True,
		)
		for ev in events:
			platform_lbl = "Google Calendar" if ev.google_calendar else ("Outlook" if "outlook" in (ev.subject or "").lower() else "Meeting")
			start_iso = str(ev.starts_on)
			end_iso = str(ev.ends_on or ev.starts_on)
			rows.append(
				{
					"name": f"event-{ev.name}",
					"event_type": "Meeting",
					"source_name": ev.name,
					"title": ev.subject or ev.name,
					"status": ev.status or "Scheduled",
					"campaign": "",
					"start_date": start_iso,
					"end_date": end_iso,
					"platform": platform_lbl,
					"color_hint": "emerald",
					"extra_json": json.dumps({"description": ev.description or "", "all_day": bool(ev.all_day)}),
				}
			)

	rows.sort(key=lambda x: str(x.get("start_date", "")))
	return rows


@frappe.whitelist()
def reschedule_event(
	event_type: str,
	source_name: str,
	start_date: str,
	end_date: str,
) -> dict:
	"""Update the source document's date fields and return the updated calendar event dict."""
	if event_type not in _SOURCE_DATE_FIELDS:
		frappe.throw(_(f"Unknown event type: {event_type}"))

	doctype, start_field, end_field = _SOURCE_DATE_FIELDS[event_type]

	if doctype == "CRM Task":
		task = frappe.get_doc("CRM Task", source_name)
		task.due_date = start_date[:10]
		task.save()  # the user's own write permission, as in the Task module
		due = str(task.due_date)[:10]
		return {
			"name": f"task-{task.name}",
			"event_type": "Task",
			"source_name": task.name,
			"title": task.title or task.name,
			"status": "Completed" if task.status == "Done" else "Scheduled",
			"campaign": "",
			"start_date": f"{due} 09:00:00",
			"end_date": f"{due} 10:00:00",
			"platform": "Task",
			"color_hint": "amber",
		}

	if doctype == "Event":
		event = frappe.get_doc("Event", source_name)
		event.starts_on = start_date
		event.ends_on = end_date
		event.save(ignore_permissions=True)
		return {
			"name": f"event-{event.name}",
			"event_type": "Meeting",
			"source_name": event.name,
			"title": event.subject or event.name,
			"status": event.status or "Scheduled",
			"campaign": "",
			"start_date": str(event.starts_on),
			"end_date": str(event.ends_on),
			"platform": "Google Calendar" if event.google_calendar else "Meeting",
			"color_hint": "emerald",
		}

	if not frappe.has_permission(doctype, "write", doc=source_name):
		frappe.throw(_(f"No write permission on {doctype} {source_name}"), frappe.PermissionError)

	doc = frappe.get_doc(doctype, source_name)
	setattr(doc, start_field, start_date)
	if start_field != end_field:
		setattr(doc, end_field, end_date)
	doc.save(ignore_permissions=True)

	event_name = sync_from_source(doctype, source_name)
	if not event_name:
		frappe.throw(_(f"Calendar event sync failed for {doctype}/{source_name}"))

	row = frappe.get_doc("Campaign Calendar Event", event_name)
	return row.as_dict()

"""
Campaign Calendar Event — controller + sync helpers.

This doctype is a *materialised projection* of four source doctypes:
  • Marketing Campaign
  • Social Post
  • Ad Campaign
  • Bulk WhatsApp Message

It is never edited manually. Records are upserted via `sync_from_source`
whenever a source document is saved, and removed via `purge_source` when
a source document is deleted.

The calendar API (`campaign/api/v1/calendar.py`) reads exclusively from
this table — one fast `get_all` instead of four round trips.
"""

from __future__ import annotations

import json
from typing import Optional

import frappe
from frappe.model.document import Document


class CampaignCalendarEvent(Document):
	pass


# ── Colour hint map ────────────────────────────────────────────────────
# Format: (event_type, status_lower) → CSS token string.
# The Vue grid maps these tokens to colours via a CSS-variable lookup.
_COLOR_MAP: dict[tuple[str, str], str] = {
	# Campaign
	("Campaign", "draft"): "campaign-draft",
	("Campaign", "active"): "campaign-active",
	("Campaign", "paused"): "campaign-paused",
	("Campaign", "completed"): "campaign-completed",
	("Campaign", "cancelled"): "campaign-cancelled",
	("Campaign", "archived"): "campaign-archived",
	# Social Post
	("Social Post", "draft"): "social-draft",
	("Social Post", "pending approval"): "social-pending",
	("Social Post", "approved"): "social-approved",
	("Social Post", "rejected"): "social-rejected",
	("Social Post", "scheduled"): "social-scheduled",
	("Social Post", "publishing"): "social-publishing",
	("Social Post", "published"): "social-published",
	("Social Post", "partially failed"): "social-partial",
	("Social Post", "failed"): "social-failed",
	("Social Post", "cancelled"): "social-cancelled",
	# Ad Campaign
	("Ad Campaign", "draft"): "ads-draft",
	("Ad Campaign", "active"): "ads-active",
	("Ad Campaign", "paused"): "ads-paused",
	("Ad Campaign", "scheduled"): "ads-scheduled",
	("Ad Campaign", "completed"): "ads-completed",
	("Ad Campaign", "rejected"): "ads-rejected",
	("Ad Campaign", "failed"): "ads-failed",
	("Ad Campaign", "archived"): "ads-archived",
	# WhatsApp
	("WhatsApp Message", "draft"): "wa-draft",
	("WhatsApp Message", "scheduled"): "wa-scheduled",
	("WhatsApp Message", "queued"): "wa-queued",
	("WhatsApp Message", "in progress"): "wa-in-progress",
	("WhatsApp Message", "completed"): "wa-completed",
	("WhatsApp Message", "partially failed"): "wa-partial",
	("WhatsApp Message", "cancelled"): "wa-cancelled",
}


def _color_hint(event_type: str, status: str) -> str:
	key = (event_type, (status or "").lower())
	return _COLOR_MAP.get(key, f"{event_type.lower().replace(' ', '-')}-default")


# ── Source serialisers ──────────────────────────────────────────────────


def _from_campaign(doc) -> dict:
	return {
		"event_type": "Campaign",
		"source_name": doc.name,
		"title": doc.campaign_name or doc.name,
		"status": doc.status or "Draft",
		"campaign": doc.name,  # self-referential (it IS the campaign)
		"start_date": doc.start_date,
		"end_date": doc.end_date or doc.start_date,
		"platform": "",
		"extra_json": json.dumps(
			{
				"budget": str(doc.budget or ""),
				"objective": doc.objective or "",
			}
		),
	}


def _from_social_post(doc) -> dict:
	accounts = []
	try:
		accounts = [r.social_account for r in (doc.posts or [])]
	except Exception:
		pass
	return {
		"event_type": "Social Post",
		"source_name": doc.name,
		"title": doc.title or doc.name,
		"status": doc.status or "Draft",
		"campaign": doc.campaign or None,
		"start_date": doc.scheduled_at,
		"end_date": doc.scheduled_at,
		"extra_json": json.dumps(
			{
				"post_type": doc.post_type or "",
				"accounts": accounts,
			}
		),
	}


def _from_ad_campaign(doc) -> dict:
	return {
		"event_type": "Ad Campaign",
		"source_name": doc.name,
		"title": doc.campaign_name or doc.name,
		"status": doc.status or "Draft",
		"campaign": doc.campaign or None,
		"start_date": doc.start_date,
		"end_date": doc.end_date or doc.start_date,
		"platform": doc.platform or "",
		"extra_json": json.dumps(
			{
				"objective": doc.objective or "",
				"daily_budget": str(doc.daily_budget or ""),
				"lifetime_budget": str(doc.lifetime_budget or ""),
			}
		),
	}


def _from_whatsapp(doc) -> dict:
	return {
		"event_type": "WhatsApp Message",
		"source_name": doc.name,
		"title": doc.title or doc.name,
		"status": doc.status or "Draft",
		"campaign": doc.campaign or None,
		"start_date": doc.scheduled_time,
		"end_date": doc.scheduled_time,
		"platform": "WhatsApp",
		"extra_json": json.dumps(
			{
				"recipient_count": doc.recipient_count or 0,
				"sent_count": doc.sent_count or 0,
				"template": doc.template or "",
			}
		),
	}


_SERIALISERS = {
	"Marketing Campaign": _from_campaign,
	"Social Post": _from_social_post,
	"Ad Campaign": _from_ad_campaign,
	"Bulk WhatsApp Message": _from_whatsapp,
}

_EVENT_TYPE_MAP = {
	"Marketing Campaign": "Campaign",
	"Social Post": "Social Post",
	"Ad Campaign": "Ad Campaign",
	"Bulk WhatsApp Message": "WhatsApp Message",
}


# ── Public helpers (called from hooks + calendar API) ──────────────────


def sync_from_source(doctype: str, source_name: str) -> str | None:
	"""Upsert a Campaign Calendar Event row for the given source document.

	Returns the name of the upserted event, or None if the doctype is
	not tracked (so callers can ignore unknown types safely).
	"""
	serialiser = _SERIALISERS.get(doctype)
	if not serialiser:
		return None

	try:
		doc = frappe.get_doc(doctype, source_name)
	except frappe.DoesNotExistError:
		return None

	data = serialiser(doc)
	event_type = _EVENT_TYPE_MAP[doctype]
	data["color_hint"] = _color_hint(event_type, data.get("status", ""))

	# Look for an existing event for this source document.
	existing = frappe.db.get_value(
		"Campaign Calendar Event",
		{"event_type": event_type, "source_name": source_name},
		"name",
	)

	if existing:
		event = frappe.get_doc("Campaign Calendar Event", existing)
		for k, v in data.items():
			setattr(event, k, v)
		event.save(ignore_permissions=True)
		return event.name
	else:
		event = frappe.new_doc("Campaign Calendar Event")
		for k, v in data.items():
			setattr(event, k, v)
		event.insert(ignore_permissions=True)
		return event.name


def purge_source(doctype: str, source_name: str) -> None:
	"""Delete the Calendar Event rows associated with the source document."""
	event_type = _EVENT_TYPE_MAP.get(doctype)
	if not event_type:
		return
	rows = frappe.db.get_all(
		"Campaign Calendar Event",
		filters={"event_type": event_type, "source_name": source_name},
		pluck="name",
	)
	for name in rows:
		frappe.delete_doc("Campaign Calendar Event", name, ignore_permissions=True, force=True)


def backfill_all() -> dict:
	"""Bulk-upsert events for every existing source document.

	Call once after install / migration:
	  bench execute msuite_campaigns.campaign.doctype.campaign_calendar_event.campaign_calendar_event.backfill_all
	"""
	counts = {}
	for doctype in _SERIALISERS:
		names = frappe.db.get_all(doctype, pluck="name")
		for n in names:
			try:
				sync_from_source(doctype, n)
			except Exception as exc:
				frappe.log_error(
					title=f"[Calendar Backfill] {doctype}/{n}",
					message=str(exc),
				)
		counts[doctype] = len(names)
	return counts


# ── Hook entry-points (registered in hooks.py) ─────────────────────


def _cce_table_exists() -> bool:
	"""Return True if the Campaign Calendar Event table exists in the DB.

	Frappe creates the table during `bench migrate`. Until that runs, any
	hook that touches this DocType would throw "DocType not found" and
	block legitimate operations (e.g. deleting an Ad Campaign). This
	guard makes the hooks silently no-op until the migration is done.
	"""
	try:
		return frappe.db.table_exists("Campaign Calendar Event")
	except Exception:
		return False


def on_source_save(doc, method=None):
	"""Called on after_insert / on_update for all tracked doctypes.

	No-ops gracefully if the Campaign Calendar Event table does not yet
	exist (i.e. `bench migrate` hasn't been run after this feature was
	deployed).
	"""
	if not _cce_table_exists():
		return
	try:
		sync_from_source(doc.doctype, doc.name)
	except Exception as exc:
		# Never let a calendar-sync failure block the main doc operation.
		frappe.log_error(
			title=f"[Calendar Sync] {doc.doctype}/{doc.name}",
			message=str(exc),
		)


def on_source_delete(doc, method=None):
	"""Called on on_trash for all tracked doctypes.

	No-ops gracefully if the Campaign Calendar Event table does not yet
	exist.
	"""
	if not _cce_table_exists():
		return
	try:
		purge_source(doc.doctype, doc.name)
	except Exception as exc:
		frappe.log_error(
			title=f"[Calendar Purge] {doc.doctype}/{doc.name}",
			message=str(exc),
		)

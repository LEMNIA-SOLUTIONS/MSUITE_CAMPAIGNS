"""
Campaign lifecycle service — pure business logic, no RPC decorators,
no channel-specific code.

This is the post-correction replacement for the orchestration logic
that used to live directly inside `campaign/api/runner.py`. The
difference is not the behaviour (outcomes are identical for Social,
Ads, and WhatsApp) — it's where the channel-specific knowledge lives:

  BEFORE: `campaign/api/runner.py` hardcoded `_social_firability`,
          `_ads_firability`, `_whatsapp_firability`, `_lifecycle_ads`,
          and imported `ads.publish.jobs` / `social_post.scheduler
          .runner` directly. Campaign depended on every channel.

  AFTER:  every function below only knows the generic adapter shape
          (`preview / run / pause / resume / stop / rollup`) from
          `campaign/services/relationships.get_adapters()`. The
          channel-specific rules live in `<module>/campaign_adapter
          .py`, owned by each channel. Campaign depends on an
          interface, not on Ads/Social/WhatsApp by name.

`campaign/api/runner.py` is now a thin `@frappe.whitelist()` wrapper
around these functions — permission checks happen there (RPC
boundary), these functions assume the caller already checked.
"""

from __future__ import annotations

import frappe
from frappe.utils import now_datetime

from msuite_campaigns.campaign.services.events import CampaignEvent, emit
from msuite_campaigns.campaign.services.relationships import get_adapters

_TERMINAL_STATUSES = ("Completed", "Cancelled", "Archived")

# Legal status transitions for the generic status-editor RPCs
# (`campaign/api/v1/composer.py:update_campaign_status` /
# `bulk_update_campaign_status`). The named lifecycle functions above
# (pause/resume/stop/complete/archive) enforce these same rules for
# their own target status; this map lets the generic RPC enforce them
# too instead of allowing an arbitrary status flip.
_ALLOWED_TRANSITIONS: dict[str, tuple[str, ...]] = {
	"Draft": ("Scheduled", "Active", "Cancelled"),
	"Scheduled": ("Active", "Cancelled"),
	"Active": ("Paused", "Completed", "Cancelled"),
	"Paused": ("Active", "Cancelled"),
	"Completed": ("Archived",),
	"Cancelled": ("Archived",),
	"Archived": (),
}


def validate_status_transition(old_status: str, new_status: str) -> None:
	"""Raise if flipping `old_status` -> `new_status` isn't a legal move.

	No-op (same status) is always allowed since it's not a transition.
	"""
	if old_status == new_status:
		return
	allowed = _ALLOWED_TRANSITIONS.get(old_status, ())
	if new_status not in allowed:
		frappe.throw(
			frappe._("Cannot change campaign status from {0} to {1}.").format(old_status, new_status)
		)


# ----------------------------------------------------------------------
# Preview
# ----------------------------------------------------------------------


def preview_campaign_run(campaign: str) -> dict:
	"""What `run_campaign` would do, without doing it. Shape:
	`{campaign, campaign_name, campaign_status, <channel_key>: [...], ...}`
	— one list per registered channel adapter.
	"""
	doc = frappe.get_doc("Marketing Campaign", campaign)
	out = {
		"campaign": doc.name,
		"campaign_name": doc.campaign_name,
		"campaign_status": doc.status,
	}
	for key, adapter in get_adapters().items():
		out[key] = adapter["preview"](campaign)
	return out


# ----------------------------------------------------------------------
# Run
# ----------------------------------------------------------------------


def run_campaign(campaign: str) -> dict:
	"""Fire every ready child entity across every registered channel.
	Never raises on a per-row failure. Flips Draft -> Active first
	when there's at least one ready item, so children that gate their
	own publish path on parent status don't race the flip.
	"""
	doc = frappe.get_doc("Marketing Campaign", campaign)
	adapters = get_adapters()

	preview = preview_campaign_run(doc.name)
	if doc.status == "Draft":
		from frappe.utils import get_datetime

		if doc.start_date and get_datetime(doc.start_date) > now_datetime():
			doc.db_set("status", "Scheduled", update_modified=True)
		else:
			doc.db_set("status", "Active", update_modified=True)
		doc.reload()
		emit(CampaignEvent.STARTED, doc.name)

	outcomes = {}
	for key, adapter in adapters.items():
		rows = preview.get(key, [])
		out = []
		for row in rows:
			if not row.get("ready"):
				out.append({**row, "outcome": "skipped"})
				continue
			try:
				result = adapter["run"](row["name"])
				out.append({**row, **result})
			except Exception as e:
				out.append({**row, "outcome": "failed", "message": str(e)[:200]})
		outcomes[key] = out

	summary = _summarise(outcomes)
	frappe.db.commit()

	return {
		"campaign": doc.name,
		"campaign_status": doc.status,
		"outcomes": outcomes,
		"summary": summary,
		"ran_at": now_datetime().strftime("%Y-%m-%d %H:%M:%S"),
	}


# ----------------------------------------------------------------------
# Pause / Resume / Stop — fan out across every registered channel
# ----------------------------------------------------------------------


def pause_campaign(campaign: str) -> dict:
	doc = frappe.get_doc("Marketing Campaign", campaign)
	if doc.status != "Paused":
		doc.db_set("status", "Paused", update_modified=True)
		emit(CampaignEvent.PAUSED, doc.name)

	outcomes = {key: adapter["pause"](campaign) for key, adapter in get_adapters().items()}
	frappe.db.commit()
	return _lifecycle_result(doc.name, "Paused", outcomes)


def resume_campaign(campaign: str) -> dict:
	doc = frappe.get_doc("Marketing Campaign", campaign)
	if doc.status != "Active":
		doc.db_set("status", "Active", update_modified=True)
		emit(CampaignEvent.RESUMED, doc.name)

	outcomes = {key: adapter["resume"](campaign) for key, adapter in get_adapters().items()}
	frappe.db.commit()
	return _lifecycle_result(doc.name, "Active", outcomes)


def stop_campaign(campaign: str) -> dict:
	"""Terminal cancel — fans out `stop` to every channel, then flips
	the parent Campaign to Cancelled regardless of per-row outcomes.
	A half-stopped state would be worse than the original Active.
	"""
	doc = frappe.get_doc("Marketing Campaign", campaign)

	outcomes = {key: adapter["stop"](campaign) for key, adapter in get_adapters().items()}

	if doc.status not in ("Cancelled", "Completed"):
		doc.db_set("status", "Cancelled", update_modified=True)
		emit(CampaignEvent.CANCELLED, doc.name)

	frappe.db.commit()
	return _lifecycle_result(doc.name, "Cancelled", outcomes)


# ----------------------------------------------------------------------
# Complete / Archive — pure status transitions, no channel fan-out
# ----------------------------------------------------------------------


def complete_campaign(campaign: str) -> dict:
	"""Mark a Campaign Completed. Does not touch children — children
	that are still running (e.g. a WhatsApp blast mid-send) keep
	running; Completed records that the *Campaign's* goal/window is
	done, not that every channel has necessarily finished.
	"""
	doc = frappe.get_doc("Marketing Campaign", campaign)
	if doc.status in _TERMINAL_STATUSES:
		frappe.throw(frappe._(f"Campaign is already {doc.status}."))
	doc.db_set("status", "Completed", update_modified=True)
	emit(CampaignEvent.COMPLETED, doc.name)
	frappe.db.commit()
	return {"campaign": doc.name, "campaign_status": "Completed"}


def archive_campaign(campaign: str) -> dict:
	"""Mark a Campaign Archived — the final, no-further-action state.
	Only valid from a terminal state (Completed/Cancelled); Active/
	Paused campaigns must be stopped or completed first so archiving
	can never silently abandon still-running children.
	"""
	doc = frappe.get_doc("Marketing Campaign", campaign)
	if doc.status not in ("Completed", "Cancelled"):
		frappe.throw(
			frappe._(
				"Only Completed or Cancelled campaigns can be archived. Stop or complete this campaign first."
			)
		)
	doc.db_set("status", "Archived", update_modified=True)
	emit(CampaignEvent.ARCHIVED, doc.name)
	frappe.db.commit()
	return {"campaign": doc.name, "campaign_status": "Archived"}


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _lifecycle_result(name: str, status: str, outcomes: dict) -> dict:
	return {
		"campaign": name,
		"campaign_status": status,
		"outcomes": outcomes,
		"summary": _summarise(outcomes),
		"ran_at": now_datetime().strftime("%Y-%m-%d %H:%M:%S"),
	}


def _summarise(outcomes: dict) -> dict:
	counts = {"fired": 0, "skipped": 0, "failed": 0}
	for channel_rows in outcomes.values():
		for row in channel_rows:
			outcome = row.get("outcome", "skipped")
			counts[outcome] = counts.get(outcome, 0) + 1
	return counts


# ----------------------------------------------------------------------
# Event emission for status changes outside the named transitions
# ----------------------------------------------------------------------

_STATUS_EVENT_MAP = {
	"Active": CampaignEvent.STARTED,
	"Scheduled": CampaignEvent.STARTED,
	"Paused": CampaignEvent.PAUSED,
	"Completed": CampaignEvent.COMPLETED,
	"Cancelled": CampaignEvent.CANCELLED,
	"Archived": CampaignEvent.ARCHIVED,
}


def emit_for_status_change(campaign: str | None, old_status: str, new_status: str) -> None:
	"""Emit the matching lifecycle event for a status change that
	happened OUTSIDE the named transition functions above — e.g.
	`campaign/api/v1/composer.py::update_campaign_status`, a generic
	"set status to X" RPC behind the form's Hero buttons that does a
	direct `doc.status = X; doc.save()` rather than calling
	`pause_campaign`/`resume_campaign`/etc.

	Prefer the named functions when a call site can — they also
	validate the transition and fan out to channels; this is the
	standardization fallback for call sites that intentionally do a
	bare status flip with no channel fan-out, so they still emit a
	correctly-named event instead of silently skipping Phase 4
	entirely. "Active" is ambiguous outside the named functions
	(Started vs Resumed) — resolved here using the previous status,
	same distinction `run_campaign`/`resume_campaign` already encode.
	"""
	if old_status == new_status:
		return
	event = _STATUS_EVENT_MAP.get(new_status)
	if not event:
		return
	if new_status == "Active" and old_status == "Paused":
		event = CampaignEvent.RESUMED
	emit(event, campaign)


def activate_due_marketing_campaigns() -> None:
	"""Cron task: Find all 'Scheduled' Marketing Campaigns whose start_date has arrived,
	and flip their status to 'Active'.
	"""
	from frappe.utils import now

	due_campaigns = frappe.get_all(
		"Marketing Campaign", filters={"status": "Scheduled", "start_date": ["<=", now()]}, pluck="name"
	)
	for name in due_campaigns:
		doc = frappe.get_doc("Marketing Campaign", name)
		if doc.status == "Scheduled":
			doc.db_set("status", "Active", update_modified=True)
			doc.reload()
			emit(CampaignEvent.STARTED, doc.name)
			logger = frappe.logger("msuite_workspace")
			logger.info(f"Marketing Campaign {doc.name} activated automatically.")

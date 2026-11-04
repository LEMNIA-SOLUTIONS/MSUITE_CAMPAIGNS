"""
Channel adapter registry — the one seam Campaign uses to reach a
channel module, and the one seam a channel module uses to announce
itself to Campaign.

Why this exists: before this module, `campaign/api/runner.py` and
`campaign/rollup.py` imported `ads.publish.jobs`, `social_post.scheduler
.runner`, `whatsapp.analytics.*` directly — Campaign depended on every
channel by name. That's backwards per the architecture correction:
channel modules must depend on Campaign, not the reverse.

The fix is the same pattern already used elsewhere in this app for
decoupled registration — `hooks.py`'s `override_whitelisted_methods` /
`permission_query_conditions` — a hook list of dotted import paths that
Campaign reads at call time. A channel module registers itself by
adding one entry to `campaign_channel_adapters` in `hooks.py` and
shipping a `<module>/campaign_adapter.py` with a module-level `ADAPTER`
dict. Campaign never imports `ads`, `social_post`, or `whatsapp` by
name anywhere in `campaign/services/` or `campaign/api/`.

Adapter shape (duck-typed — no base class to keep channel modules from
needing to import anything from `campaign/`):

    ADAPTER = {
        "key":      "social" | "ads" | "whatsapp",
        "label":    "Social" | "Ads" | "WhatsApp",     # for outcome rows
        "preview":  fn(campaign_name) -> list[dict],   # rows: name, status, ready, reason, ...
        "run":      fn(row_name) -> dict,               # {"outcome": "fired"|"failed", "message": str}
        "pause":    fn(campaign_name) -> list[dict],     # per-row outcomes
        "resume":   fn(campaign_name) -> list[dict],
        "stop":     fn(campaign_name) -> list[dict],
        "rollup":   fn(campaign_name, days) -> dict,     # existing campaign_rollup shape
    }

A channel that doesn't support an action (e.g. WhatsApp can't "pause"
an in-flight send) still provides the key — it just always returns
`[]` or a fully-skipped outcome list. Generic callers in `lifecycle.py`
/ `analytics.py` never branch on channel name.
"""

from __future__ import annotations

import frappe

from msuite_core.constants import LOGGER_NAME

logger = frappe.logger(LOGGER_NAME)

_HOOK = "campaign_channel_adapters"
_CACHE_KEY = "campaign_channel_adapters"


def get_adapters() -> dict[str, dict]:
	"""Return `{channel_key: ADAPTER}` for every registered channel.

	Resolved from `hooks.py`'s `campaign_channel_adapters` list (dotted
	paths to each channel's `ADAPTER` dict) and cached for the request
	— these are static module-level dicts, re-importing every call buys
	nothing.
	"""
	cached = getattr(frappe.local, _CACHE_KEY, None)
	if cached is not None:
		return cached

	adapters: dict[str, dict] = {}
	for dotted_path in frappe.get_hooks(_HOOK) or []:
		try:
			adapter = frappe.get_attr(dotted_path)
		except Exception:
			logger.error(f"campaign_channel_adapters: failed to load {dotted_path}", exc_info=True)
			continue
		key = adapter.get("key")
		if not key:
			logger.error(f"campaign_channel_adapters: {dotted_path} has no 'key'")
			continue
		adapters[key] = adapter

	setattr(frappe.local, _CACHE_KEY, adapters)
	return adapters


def get_bulk_adapter(doctype: str) -> dict | None:
	"""The adapter of the channel that sends this bulk message DocType, if it is on this site."""
	for adapter in get_adapters().values():
		if adapter.get("bulk_doctype") == doctype:
			return adapter
	return None


# ----------------------------------------------------------------------
# Active-campaign auto-discovery
# ----------------------------------------------------------------------


def is_campaign_active(campaign: str | None) -> bool:
	"""True when `campaign` is unset (standalone entity, always eligible)
	or when the linked Marketing Campaign's status is Active.

	Single source of truth for the "is this child allowed to run"
	question — every channel scheduler (Social's dispatch_due_posts,
	Ads' launch_due_campaigns, WhatsApp's schedule_bulk_messages) should
	gate on this instead of re-deriving the rule per channel.
	"""
	if not campaign:
		return True
	status = frappe.db.get_value("Marketing Campaign", campaign, "status")
	return status == "Active"


def register_with_campaign(doctype: str, name: str | None, campaign: str | None) -> None:
	"""Call from a channel entity's own `after_insert` / `on_update`
	once its `campaign` link is known.

	This is the documented seam for "a record created under an already
	Active campaign must not require Run Campaign to be clicked again."
	Today every channel already has its own time/status-gated cron
	(dispatch_due_posts / launch_due_campaigns / schedule_bulk_messages)
	that re-scans its own table on a fixed interval and gates on
	`is_campaign_active()` — so a newly created, properly scheduled
	child is already picked up on the next tick with no extra code.

	This function exists as the single place that observation would
	plug in (e.g. an immediate enqueue instead of waiting for the next
	tick) without having to touch the channel doctypes a second time.
	Today it only logs — deliberately not adding a second, eager
	dispatch path alongside the existing tick-based ones, which would
	itself be the "duplicate orchestration logic" the audit flagged.
	"""
	if not campaign:
		return
	if not is_campaign_active(campaign):
		return
	logger.info(
		f"register_with_campaign: {doctype} {name} registered with "
		f"active campaign {campaign} — next scheduler tick will pick it up."
	)


# ----------------------------------------------------------------------
# Reverse-link helpers (Campaign -> children), channel-agnostic
# ----------------------------------------------------------------------


def get_children(campaign: str) -> dict[str, list[dict]]:
	"""`{channel_key: preview_rows}` across every registered channel.
	Thin fan-out — no channel-specific logic lives here."""
	return {key: adapter["preview"](campaign) for key, adapter in get_adapters().items()}

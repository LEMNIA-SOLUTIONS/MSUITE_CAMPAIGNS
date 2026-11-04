"""
Campaign lifecycle event emitter — Phase 4 / Event Architecture.

Deliberately NOT backed by a new "Campaign Event" / "Event Log"
doctype. Persisting event history was the original audit's own
`REQUIRES_APPROVAL` item ("Campaign Lifecycle Event Bus... new
infrastructure... High risk") and has not been approved. What's built
here is the emission + subscription seam only:

  - A synchronous, in-process listener registry resolved from
    `hooks.py`'s `campaign_event_listeners` list — the exact same
    dotted-path pattern `campaign/services/relationships.py` already
    uses for the channel adapter registry. No new persistence; a
    future module (AI Calling, Email, SMS, Automation — the brief's
    own "Future Modules" list) registers a listener with one line in
    `hooks.py`, same as a channel adapter.
  - `frappe.publish_realtime` for any live UI — this is existing
    Frappe core infrastructure (websocket pub/sub), not a new
    doctype or table.

This is "event architecture preparation," not a full event-sourced
system: there is no durable event log, no replay, no guaranteed
delivery. A listener that's down when `emit()` runs simply misses
that event. If a future phase needs durability, that's the moment to
bring the `REQUIRES_APPROVAL` event-log doctype back for sign-off —
not something to smuggle in here as a side effect.
"""

from __future__ import annotations

import frappe

from msuite_core.constants import LOGGER_NAME

logger = frappe.logger(LOGGER_NAME)

_HOOK = "campaign_event_listeners"


class CampaignEvent:
	"""Event name constants. Every value below is emitted somewhere in
	`campaign/services/lifecycle.py`, `campaign/services/analytics.py`,
	`campaign/scheduled_tasks/recurring_cloner.py`, or the Marketing
	Campaign controller — grep for `CampaignEvent.` to find each site."""

	CREATED = "CampaignCreated"
	STARTED = "CampaignStarted"
	PAUSED = "CampaignPaused"
	RESUMED = "CampaignResumed"
	COMPLETED = "CampaignCompleted"
	CANCELLED = "CampaignCancelled"
	ARCHIVED = "CampaignArchived"
	BUDGET_EXCEEDED = "CampaignBudgetExceeded"
	# GOAL_REACHED is intentionally absent — `Marketing Campaign.goal`
	# is free text with no measurable target today, so there is no
	# condition to detect "reached." Needs sign-off before it comes back.


def emit(event: str, campaign: str | None, **data) -> None:
	"""Fire `event` for `campaign`. Never raises — a broken listener
	must not break the lifecycle action that triggered it (same
	failure posture as the channel adapter registry in
	`relationships.py`).
	"""
	payload = {"event": event, "campaign": campaign, **data}

	try:
		frappe.publish_realtime(
			event=f"campaign:{event}",
			message=payload,
			doctype="Marketing Campaign",
			docname=campaign,
		)
	except Exception:
		logger.error(f"publish_realtime failed for {event}/{campaign}", exc_info=True)

	for dotted_path in frappe.get_hooks(_HOOK) or []:
		try:
			listener = frappe.get_attr(dotted_path)
			listener(event, campaign, payload)
		except Exception:
			logger.error(
				f"Campaign event listener {dotted_path} failed for {event}/{campaign}",
				exc_info=True,
			)

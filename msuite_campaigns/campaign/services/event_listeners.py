"""
Built-in listeners for the campaign lifecycle event bus.

Registered via `hooks.py`:

    campaign_event_listeners = [
        "msuite_campaigns.campaign.services.event_listeners.log_campaign_event",
    ]

Why this file exists: `campaign_event_listeners` was declared as an empty
list, so the subscription seam in `events.emit()` had never actually been
exercised — a future module adding the first listener would have been the
one to discover any wiring bug. Shipping one real, side-effect-free
listener keeps the path live.

Deliberately does NOT persist anything. `events.py` is explicit that a
durable event log is a `REQUIRES_APPROVAL` item that has not been signed
off; writing rows here would smuggle that in as a side effect. Logging is
existing infrastructure, so it stays inside the approved boundary.

Listener contract (same shape `events.emit()` calls every entry with):

    listener(event_name: str, campaign: str | None, payload: dict) -> None

Filtering by `event_name` is the listener's job — `emit()` calls every
registered listener for every event, exactly like Frappe's own
`doc_events` dispatch.
"""

from __future__ import annotations

import frappe

from msuite_core.constants import LOGGER_NAME

logger = frappe.logger(LOGGER_NAME)

# Events worth an INFO line. Everything else drops to DEBUG so a busy
# scheduler tick can't flood the log with routine transitions.
_NOTABLE = frozenset(
	{
		"CampaignStarted",
		"CampaignCompleted",
		"CampaignCancelled",
		"CampaignBudgetExceeded",
	}
)


def log_campaign_event(event: str, campaign: str | None, payload: dict) -> None:
	"""Record a campaign lifecycle transition in the app log.

	Never raises. `emit()` already wraps every listener in a try/except, but
	a listener that throws still costs a traceback in the log on every
	event, so failures are swallowed here too — the same posture the
	channel adapter registry takes in `relationships.py`.
	"""
	try:
		extra = {k: v for k, v in (payload or {}).items() if k not in ("event", "campaign")}
		detail = f" {extra}" if extra else ""
		message = f"[campaign-event] {event} campaign={campaign or '-'}{detail}"

		if event in _NOTABLE:
			logger.info(message)
		else:
			logger.debug(message)
	except Exception:
		# Logging must never break a lifecycle action.
		pass

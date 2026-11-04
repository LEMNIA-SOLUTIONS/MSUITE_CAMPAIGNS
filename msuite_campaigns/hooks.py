app_name = "msuite_campaigns"
app_title = "MSuite Campaigns"
app_publisher = "MSuite"
app_description = "Marketing campaigns, audiences, the calendar, broadcast dispatch, the AWS bulk pipeline and performance."
app_email = "dev@msuite.com"
app_license = "mit"

required_apps = ["frappe", "msuite_core"]

before_install = "msuite_campaigns.install.before_install"

PE = "msuite_core.services.plans.plan_enforcer"

# ── Calendar Event sync helper ──
# Keeps Campaign Calendar Event rows up to date whenever a source
# document (Marketing Campaign, Social Post, Ad Campaign, Bulk WhatsApp
# Message) is saved or deleted.
_CCE = "msuite_campaigns.campaign.doctype.campaign_calendar_event.campaign_calendar_event"

# This app's records in the global search. The key sets the order of the groups.
msuite_search_sources = {"10_campaigns": "msuite_campaigns.campaign.search.search"}

# What this app shows on the Home page.
msuite_home_cards = {
	"campaigns": "msuite_campaigns.campaign.home.running_campaigns",
	"running_campaign_count": "msuite_campaigns.campaign.home.running_campaign_count",
}

msuite_home_recent = {
	"10_campaigns": "msuite_campaigns.campaign.recent.recent_campaigns",
	"20_audiences": "msuite_campaigns.campaign.recent.recent_audiences",
}

# This app's plan limits, read by msuite_core.services.plans.plan_enforcer.
msuite_plan_gates = ["msuite_campaigns.audience.plan_gates.GATES"]

doc_events = {
	# Unified audience — routes through the shared gate dispatcher
	# rather than its own function (see audience/plan_gates.py).
	"Audience": {
		"before_insert": f"{PE}.enforce_gate",
		"before_save": f"{PE}.enforce_gate",
	},
	# ── Calendar sync — four source doctypes ─────────────────────────────
	# Each save/delete upserts / removes the Campaign Calendar Event row so
	# the calendar read path is a single get_all query.
	"Marketing Campaign": {
		"after_insert": f"{_CCE}.on_source_save",
		"on_update": f"{_CCE}.on_source_save",
		"on_trash": f"{_CCE}.on_source_delete",
	},
	"Contact": {
		"validate": "msuite_campaigns.audience.services.contact_normalize.normalize_contact",
	},
}

# ── Campaign lifecycle events ──
# Phase 4 / Event Architecture. `campaign/services/events.py::emit()`
# calls every dotted path here as `listener(event_name, campaign,
# payload)` for EVERY event (filtering by event_name is the listener's
# job, same as Frappe's own doc_events dispatch).
#
# Note `emit()` does real work regardless of this list — it always fires
# `frappe.publish_realtime` for live UI. This list is the *additional*
# in-process subscription seam.
#
# `log_campaign_event` is the built-in listener: side-effect-free, writes
# no rows (a durable event log is still REQUIRES_APPROVAL — see
# `campaign/services/events.py`), and exists so this seam is exercised
# rather than theoretical. It was an empty list, which meant the first
# module to subscribe would also have been the first to find any wiring
# bug. A future module (AI Calling, Email, SMS, Automation) adds one line
# here, same pattern as `campaign_channel_adapters`.
campaign_event_listeners = [
	"msuite_campaigns.campaign.services.event_listeners.log_campaign_event",
]

scheduler_events = {
	"cron": {
		"*/5 * * * *": [
			"msuite_campaigns.campaign.services.lifecycle.activate_due_marketing_campaigns",
		],
		"15 * * * *": [
			# Campaign spend sync — Analytics Aggregation (Phase 3).
			# Keeps Marketing Campaign.spent current for Active
			# campaigns nobody happens to be viewing the dashboard for.
			"msuite_campaigns.campaign.scheduled_tasks.spend_sync.run",
		],
		"0 3 * * *": [
			# Recurring Campaign cloner — finds Active campaigns whose
			# `next_run_at` has elapsed and clones the whole umbrella
			# (Campaign + child Social Posts + their posts) for the
			# next cycle. Daily cadence; cron is idempotent.
			"msuite_campaigns.campaign.scheduled_tasks.recurring_cloner.run",
		],
	},
}

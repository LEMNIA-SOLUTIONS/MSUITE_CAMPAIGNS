"""
Periodic spend sync — keeps `Marketing Campaign.spent` current for
Active campaigns even when nobody is looking at the Performance tab
(the dashboard view itself also calls `sync_campaign_spend` — see
`campaign/services/analytics.py` — this cron covers the gap for
report/list views that read `spent` without opening the dashboard).

Bounded scope on purpose: only `status = 'Active'` campaigns are
synced. Completed/Cancelled/Archived campaigns' spend is frozen by
definition (nothing should still be spending against them), and Draft
campaigns haven't launched anything yet — re-querying Ads for either
would be pure waste at scale. This keeps the job O(active campaigns),
not O(all campaigns ever created), which matters per the audit's
Scalability Audit (§5) once a site has tens of thousands of historical
campaigns.

Hook in `hooks.py::scheduler_events.cron` (hourly, same cadence as
`ads.scheduler.alerts.check_budget_pace`, which this is conceptually
adjacent to):
    msuite_campaigns.campaign.scheduled_tasks.spend_sync.run
"""

from __future__ import annotations

import frappe

from msuite_campaigns.campaign.services.analytics import sync_campaign_spend, sync_marketing_campaign_insights
from msuite_core.constants import LOGGER_NAME

logger = frappe.logger(LOGGER_NAME)


def run() -> dict:
	"""Cron entry point — returns a summary for log aggregation."""
	names = frappe.get_all(
		"Marketing Campaign",
		filters={"status": "Active"},
		pluck="name",
	)

	synced, failed = 0, 0
	for name in names:
		try:
			sync_campaign_spend(name)
			sync_marketing_campaign_insights(name)
			synced += 1
		except Exception as e:
			logger.error(f"spend_sync / insights sync failed for Campaign {name}: {e}")
			failed += 1

	frappe.db.commit()
	summary = {"campaigns": len(names), "synced": synced, "failed": failed}
	logger.info(f"Campaign spend and insights sync: {summary}")
	return summary

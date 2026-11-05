"""
Campaign analytics service — cross-channel rollup, generic over the
adapter registry (see `relationships.py`).

Post-correction replacement for `campaign/rollup.py`'s direct imports
of `ads.domain.insights.query`, `social_post.insights.query`, and
`whatsapp.analytics.query`. Adding a future channel (AI Calling, Email,
SMS) means it ships a `rollup` entry on its own adapter — this file
does not change.

NOTE on scope (Phase 3 / Analytics Aggregation): this still computes
the dashboard live, on every call — there is still no materialized
rollup table (that's a new doctype — `REQUIRES_APPROVAL` per the
original audit, not built without explicit sign-off). What Phase 3
*does* add is `sync_campaign_spend`, which closes the audit's "spent
field is dead, nothing ever writes it" finding for real: it's a
read-then-persist of the Ads adapter's own rollup, not a new
aggregation table, and is cheap enough to run on every dashboard view
plus a periodic cron (`campaign/scheduled_tasks/spend_sync.py`) for
campaigns nobody happens to be looking at.
"""

from __future__ import annotations

from datetime import date as _date, timedelta
from typing import Any

import frappe
from frappe.utils import add_days, getdate, today

from msuite_campaigns.campaign.services import attribution
from msuite_campaigns.campaign.services.events import CampaignEvent, emit
from msuite_campaigns.campaign.services.relationships import get_adapters
from msuite_core.constants import (
	CAMPAIGN_DAY_REFRESH_DAYS,
	GRANULARITIES_WITHOUT_DATE,
	GRANULARITY_DAY,
	GRANULARITY_LIFETIME,
	GRANULARITY_WEEK,
	INSIGHT_DAY_KEEP_DAYS,
)
from msuite_core.utils.insight_keys import (
	delete_in_batches,
	make_insight_key,
	week_monday,
)

# Large enough to make `_range(days)` (ads/domain/insights/query.py)
# resolve to "since the dawn of the campaign" without needing a second,
# duplicate lifetime-totals code path — `spent` is meant to be the
# cumulative figure paired with `budget`, not a windowed one.
_LIFETIME_DAYS = 36500


def check_campaign_setup(campaign: str) -> dict:
	"""Setup-progress probe — ready the moment the Campaign has basics
	+ at least one channel attached. Channels are optional individually
	but required in aggregate.
	"""
	doc = frappe.get_doc("Marketing Campaign", campaign)
	basics_done = bool(doc.campaign_name and doc.status)

	adapters = get_adapters()
	children = {key: adapter["preview"](campaign) for key, adapter in adapters.items()}
	channel_count = sum(1 for rows in children.values() if rows)

	hint_parts = [f"{len(rows)} {adapters[key]['label']}" for key, rows in children.items() if rows]
	channel_hint = " · ".join(hint_parts) or " / ".join(a["label"] for a in adapters.values())

	stages = [
		{
			"key": "basics",
			"label": "Basics",
			"hint": doc.goal or "Name + goal",
			"done": basics_done,
			"tab": "tab_overview",
		},
		{
			"key": "channels",
			"label": "Channels",
			"hint": channel_hint,
			"done": channel_count > 0,
			"tab": "tab_channels",
		},
	]
	next_stage = next((s for s in stages if not s["done"]), None)

	return {
		"ready": all(s["done"] for s in stages),
		"stages": stages,
		"next": next_stage,
		"counts": {
			"channels": channel_count,
			**{key: len(rows) for key, rows in children.items()},
		},
	}


def refresh_campaign_insights(campaign: str) -> dict:
	"""On-demand insights refresh — fans out to every channel that
	declares a `refresh` capability on its adapter. A channel without
	one (none today) is simply skipped, not treated as an error.
	"""
	out = {}
	for key, adapter in get_adapters().items():
		refresh_fn = adapter.get("refresh")
		if not refresh_fn:
			continue
		out[key] = _safe(lambda fn=refresh_fn: fn(campaign), key)

	try:
		sync_marketing_campaign_insights(campaign)
	except Exception:
		frappe.log_error(
			title=f"sync_marketing_campaign_insights failed for {campaign}", message=frappe.get_traceback()
		)

	return out


def get_campaign_insight_totals(campaign: str, days: int = 30) -> dict:
	"""Aggregate Marketing Campaign Insight rows across DAY, WEEK, and LIFETIME tiers.

	For lifetime queries (days <= 0 or days >= _LIFETIME_DAYS):
		Reads directly from the campaign's single LIFETIME row.
		reach_approximate is True because ads reach is derived from DAY/WEEK compaction where reach is approximate.

	For date range queries (0 < days < _LIFETIME_DAYS):
		Sums DAY and WEEK rows for the campaign where date is within [today - days, today].
		LIFETIME rows are strictly excluded from date range aggregations.
		Because complete weeks have their constituent DAY rows deleted upon compaction,
		summing DAY and WEEK rows is strictly additive without double counting.
		Reach is summed ONLY from DAY rows (WEEK rows store reach = 0 per non-additive ruling).
		If any WEEK row falls within the window, reach_approximate is set to True, otherwise False.

	Args:
		campaign: Name/ID of the Marketing Campaign.
		days: Lookback window in days (default 30). 0 or >= _LIFETIME_DAYS indicates lifetime.

	Returns:
		dict containing aggregated metrics, has_data flag, and reach_approximate flag.

	Raises:
		None.

	Side effects:
		None (read-only query).
	"""
	days = int(days)
	if days <= 0 or days >= _LIFETIME_DAYS:
		rows = frappe.db.sql(
			"""
			SELECT
				total_spend,
				total_reach,
				total_engagement,
				total_leads,
				total_conversations,
				ads_spend,
				ads_impressions,
				ads_clicks,
				ads_reach,
				ads_conversions,
				ads_conversion_value,
				ads_roas,
				ads_cost_per_conversion,
				ads_frequency,
				social_impressions,
				social_reach,
				social_clicks,
				social_engagement,
				whatsapp_recipients,
				whatsapp_sent,
				whatsapp_delivered,
				whatsapp_read,
				whatsapp_failed
			FROM `tabMarketing Campaign Insight`
			WHERE marketing_campaign = %(campaign)s
			  AND granularity = %(granularity)s
			ORDER BY modified DESC
			LIMIT 1
		""",
			{"campaign": campaign, "granularity": GRANULARITY_LIFETIME},
			as_dict=True,
		)
		if rows and rows[0]:
			r = rows[0]
			return {
				"has_data": True,
				"reach_approximate": True,
				"total_spend": float(r.get("total_spend") or 0.0),
				"total_reach": int(r.get("total_reach") or 0),
				"total_engagement": int(r.get("total_engagement") or 0),
				"total_leads": int(r.get("total_leads") or 0),
				"total_conversations": int(r.get("total_conversations") or 0),
				"ads_spend": float(r.get("ads_spend") or 0.0),
				"ads_impressions": int(r.get("ads_impressions") or 0),
				"ads_clicks": int(r.get("ads_clicks") or 0),
				"ads_reach": int(r.get("ads_reach") or 0),
				"ads_conversions": int(r.get("ads_conversions") or 0),
				"ads_conversion_value": float(r.get("ads_conversion_value") or 0.0),
				"ads_roas": float(r.get("ads_roas") or 0.0),
				"ads_frequency": float(r.get("ads_frequency") or 0.0),
			}
		return {
			"has_data": False,
			"reach_approximate": False,
			"total_spend": 0.0,
			"total_reach": 0,
			"total_engagement": 0,
			"total_leads": 0,
			"total_conversations": 0,
			"ads_spend": 0.0,
			"ads_impressions": 0,
			"ads_clicks": 0,
			"ads_reach": 0,
			"ads_conversions": 0,
			"ads_conversion_value": 0.0,
			"ads_roas": 0.0,
			"ads_frequency": 0.0,
		}

	until = getdate(today())
	since = add_days(until, -days)
	rows = frappe.db.sql(
		"""
		SELECT
			COALESCE(SUM(total_spend), 0.0) AS total_spend,
			COALESCE(SUM(CASE WHEN granularity = %(granularity_day)s THEN total_reach ELSE 0 END), 0) AS total_reach,
			COALESCE(SUM(total_engagement), 0) AS total_engagement,
			COALESCE(SUM(total_leads), 0) AS total_leads,
			COALESCE(SUM(total_conversations), 0) AS total_conversations,
			COALESCE(SUM(ads_spend), 0.0) AS ads_spend,
			COALESCE(SUM(ads_impressions), 0) AS ads_impressions,
			COALESCE(SUM(ads_clicks), 0) AS ads_clicks,
			COALESCE(SUM(CASE WHEN granularity = %(granularity_day)s THEN ads_reach ELSE 0 END), 0) AS ads_reach,
			COALESCE(SUM(ads_conversions), 0) AS ads_conversions,
			COALESCE(SUM(ads_conversion_value), 0.0) AS ads_conversion_value,
			COALESCE(SUM(CASE WHEN granularity = %(granularity_week)s THEN 1 ELSE 0 END), 0) AS week_count,
			COUNT(*) AS row_count
		FROM `tabMarketing Campaign Insight`
		WHERE marketing_campaign = %(campaign)s
		  AND granularity IN (%(granularity_day)s, %(granularity_week)s)
		  AND date BETWEEN %(since)s AND %(until)s
	""",
		{
			"campaign": campaign,
			"granularity_day": GRANULARITY_DAY,
			"granularity_week": GRANULARITY_WEEK,
			"since": since,
			"until": until,
		},
		as_dict=True,
	)
	if rows and rows[0] and rows[0].get("row_count"):
		r = rows[0]
		spend = float(r.get("total_spend") or 0.0)
		ads_spend = float(r.get("ads_spend") or 0.0)
		ads_conv_val = float(r.get("ads_conversion_value") or 0.0)
		ads_impr = int(r.get("ads_impressions") or 0)
		ads_reach = int(r.get("ads_reach") or 0)
		week_count = int(r.get("week_count") or 0)
		return {
			"has_data": True,
			"reach_approximate": week_count > 0,
			"total_spend": spend,
			"total_reach": int(r.get("total_reach") or 0),
			"total_engagement": int(r.get("total_engagement") or 0),
			"total_leads": int(r.get("total_leads") or 0),
			"total_conversations": int(r.get("total_conversations") or 0),
			"ads_spend": ads_spend,
			"ads_impressions": ads_impr,
			"ads_clicks": int(r.get("ads_clicks") or 0),
			"ads_reach": ads_reach,
			"ads_conversions": int(r.get("ads_conversions") or 0),
			"ads_conversion_value": ads_conv_val,
			"ads_roas": _safe_div(ads_conv_val, ads_spend),
			"ads_frequency": _safe_div(ads_impr, ads_reach),
		}

	return {
		"has_data": False,
		"reach_approximate": False,
		"total_spend": 0.0,
		"total_reach": 0,
		"total_engagement": 0,
		"total_leads": 0,
		"total_conversations": 0,
		"ads_spend": 0.0,
		"ads_impressions": 0,
		"ads_clicks": 0,
		"ads_reach": 0,
		"ads_conversions": 0,
		"ads_conversion_value": 0.0,
		"ads_roas": 0.0,
		"ads_frequency": 0.0,
	}


def get_campaign_dashboard(campaign: str, days: int = 30) -> dict:
	"""Everything the Performance tab needs in one shot. Each adapter's
	rollup is independently tolerant of missing data; wrapped anyway so
	one channel's exception doesn't take the whole dashboard down.

	Reads compacted Marketing Campaign Insight rows across DAY, WEEK, and LIFETIME tiers
	without double counting. If the window spans WEEK rows, reach is summed only from
	DAY rows and reach_approximate is set to True.

	Args:
		campaign: Marketing Campaign identifier/name.
		days: Lookback window in days (default 30).

	Returns:
		dict: Dashboard payload containing campaign, days, modules, summary, attribution,
			totals, and reach_approximate.

	Raises:
		None.

	Side effects:
		Logs errors on partial background failures.
	"""
	days = int(days)
	adapters = get_adapters()

	modules = {
		key: _safe(lambda a=adapter: a["rollup"](campaign, days), key) for key, adapter in adapters.items()
	}

	# AWS-routed blasts never write per-recipient status back to MariaDB, so
	# the adapters above report them as permanently Queued. Their numbers come
	# from DynamoDB instead, via the provider. A campaign can hold both engines
	# on one channel, so this adds to the adapter's totals rather than
	# replacing them, and the returned shape is identical either way — no new
	# RPC and no SPA change.
	try:
		from msuite_campaigns.aws_pipeline import rollup as aws_rollup

		modules = aws_rollup.merge(modules, campaign, days)
	except Exception:
		frappe.log_error(title=f"AWS rollup merge failed for {campaign}", message=frappe.get_traceback())

	summary = _cross_module_summary(modules)
	attribution_summary = attribution.get_attribution_summary(campaign)

	# Keep `Marketing Campaign.spent` honest every time someone looks
	# at the dashboard — see `sync_campaign_spend`. Best-effort: a
	# failure here must not take down the dashboard the marketer is
	# actually trying to view.
	try:
		sync_campaign_spend(campaign)
	except Exception:
		frappe.log_error(title=f"sync_campaign_spend failed for {campaign}", message=frappe.get_traceback())

	try:
		sync_marketing_campaign_insights(campaign)
	except Exception:
		frappe.log_error(
			title=f"sync_marketing_campaign_insights failed for {campaign}", message=frappe.get_traceback()
		)

	# Read compacted Marketing Campaign Insight data (DAY + WEEK + LIFETIME)
	insight_totals = get_campaign_insight_totals(campaign, days)
	reach_approximate = insight_totals["reach_approximate"]

	if insight_totals["has_data"]:
		summary["spend"] = insight_totals["total_spend"]
		summary["reach"] = insight_totals["total_reach"]
		summary["engagement"] = insight_totals["total_engagement"]
		if insight_totals["ads_conversions"]:
			summary["conversions"] = float(insight_totals["ads_conversions"])
		if insight_totals["ads_roas"]:
			summary["roas"] = insight_totals["ads_roas"]
		spend = insight_totals["total_spend"]
		reach = insight_totals["total_reach"]
		engagement = insight_totals["total_engagement"]
		leads = insight_totals["total_leads"]
		conversations = insight_totals["total_conversations"]
	else:
		spend = summary["spend"]
		reach = summary["reach"]
		engagement = summary["engagement"]
		leads = attribution_summary["leads"]
		conversations = attribution_summary["conversations"]

	summary["reach_approximate"] = reach_approximate

	totals = {
		"total_spend": spend,
		"total_reach": reach,
		"total_engagement": engagement,
		"total_leads": leads,
		"total_conversations": conversations,
		"reach_approximate": reach_approximate,
	}

	return {
		"campaign": campaign,
		"days": days,
		"modules": modules,
		"summary": summary,
		# Attribution Foundation (Phase 2) — shape-stable regardless of
		# whether Conversation.campaign / CRM Lead.marketing_campaign
		# exist yet on this install (see attribution.py's has_field
		# guards). Counts only today; deeper revenue/lifecycle views
		# are future-readiness work, not Phase 2 scope.
		"attribution": attribution_summary,
		# Analytics Aggregation (Phase 3) — the five named totals the
		# execution brief asks Campaign to expose, without duplicating
		# channel logic: spend/reach/engagement come straight from
		# `summary` above (already computed once), leads/conversations
		# from `attribution_summary` (already computed once too).
		"totals": totals,
		"reach_approximate": reach_approximate,
	}


def sync_campaign_spend(campaign: str) -> float:
	"""Aggregate lifetime Ads spend for `campaign` and persist it onto
	`Marketing Campaign.spent`.

	This is the actual fix for the audit's "spent field is dead, no
	writer exists anywhere" finding (deferred from Phase 1 on purpose —
	a correct figure needed exactly this aggregation, not a stopgap).
	Reuses the Ads adapter's own `rollup` (same SQL `_totals()` the Ads
	Performance tab already runs) rather than a second, duplicated
	spend query — Ads is the only registered channel that reports
	spend today (`_cross_module_summary` already treats spend/
	conversions/ROAS as Ads-only).

	`update_modified=False` — this is a derived analytics value, not a
	marketer edit; touching `modified` here would make "last edited"
	timestamps lie about when a human last changed the campaign.

	Event Architecture (Phase 4): also reads `budget` (which now has a
	real `spent` to compare against, for the first time, because of
	this function) and emits `CampaignBudgetExceeded` exactly once per
	crossing — not on every sync tick while already over budget. The
	"was it already over" check uses the value read BEFORE this call's
	own write, so a campaign that's been over budget for days doesn't
	re-fire the event every hour the cron runs.
	"""
	adapter = get_adapters().get("ads")
	if not adapter:
		return 0.0
	rollup = _safe(lambda: adapter["rollup"](campaign, _LIFETIME_DAYS), "ads")
	spend = float((rollup.get("totals") or {}).get("spend") or 0)

	row = frappe.db.get_value("Marketing Campaign", campaign, ["spent", "budget"], as_dict=True)
	if not row:
		return spend

	budget = float(row.budget or 0)
	was_over = budget > 0 and float(row.spent or 0) >= budget
	is_over = budget > 0 and spend >= budget

	frappe.db.set_value("Marketing Campaign", campaign, "spent", spend, update_modified=False)

	if is_over and not was_over:
		emit(CampaignEvent.BUDGET_EXCEEDED, campaign, spent=spend, budget=budget)

	return spend


# ==========================================================================
# Cross-module summary
# ==========================================================================


def _cross_module_summary(modules: dict[str, dict]) -> dict:
	"""One executive-view card: Reach / Engagement / Spend / Conversions
	/ ROAS. Kept generic over whichever channels are registered — adds
	metrics that exist, treats missing ones as 0 rather than branching
	on channel name.

	Reach + Engagement are `sum_with_caveat` per the Metric Catalog: the
	same person can appear across channels, so these are directional
	ceilings, not deduplicated unique counts.
	"""
	ads = modules.get("ads", {}).get("totals") or {}
	social = modules.get("social", {}).get("totals") or {}
	whatsapp = modules.get("whatsapp", {}).get("totals") or {}
	email = modules.get("email", {}).get("totals") or {}

	reach = (
		int(ads.get("reach") or 0)  # None reach means non-additive across multi-row aggregates
		+ int(social.get("reach") or 0)
		+ int(whatsapp.get("delivered") or 0)
		+ int(email.get("delivered") or 0)
	)
	engagement = (
		int(ads.get("clicks") or 0)
		+ int(social.get("engagement") or 0)
		+ int(whatsapp.get("read") or 0)
		+ int(email.get("opened") or 0)
		+ int(email.get("clicked") or 0)
	)

	return {
		"reach": reach,
		"engagement": engagement,
		"spend": float(ads.get("spend") or 0),
		"conversions": float(ads.get("conversions") or 0),
		"roas": float(ads.get("roas") or 0),
		"currency": modules.get("ads", {}).get("currency") or "",
	}


def get_campaigns_overview(
	days: int = 30,
	filters: dict | None = None,
	period: str | None = None,
) -> dict:
	"""Cross-campaign rollup for the Campaigns Dashboard (`/campaigns`) —
	distinct from `get_campaign_dashboard` above, which is scoped to one
	campaign's Mission Control page.

	Aggregates DAY and WEEK rows from Marketing Campaign Insight for spend_trend,
	reach, engagement, and conversions without double counting. Excludes LIFETIME rows
	from range sums. Sets reach_approximate=True when the query window reaches into
	WEEK rows. Supports periods (7d, mtd/month, 90d/qtr) and previous-period totals.

	Args:
		days: Window size in days (default 30).
		filters: Optional filters dictionary on Marketing Campaign.
		period: Optional period string ('7d', 'mtd'/'month', '90d'/'qtr').

	Returns:
		dict: Cross-campaign overview dictionary containing kpis, status_distribution,
			channel_distribution, spend_trend, leads_trend, campaigns lists, and reach_approximate.

	Raises:
		None.

	Side effects:
		None (read-only query).
	"""
	days = int(days)
	filters = dict(filters or {})
	today_date = getdate(today())

	# Resolve period window and previous comparison window
	if period in ("mtd", "month"):
		current_until = today_date
		current_since = today_date.replace(day=1)
		p_days = (current_until - current_since).days + 1
		prev_until = current_since - timedelta(days=1)
		prev_since = prev_until - timedelta(days=p_days - 1)
		period_label = "month"
	elif period in ("qtr", "90d") or (not period and days == 90):
		p_days = 90
		current_until = today_date
		current_since = add_days(current_until, -p_days + 1)
		prev_until = add_days(current_since, -1)
		prev_since = add_days(prev_until, -p_days + 1)
		period_label = "qtr"
	elif period == "7d" or (not period and days == 7):
		p_days = 7
		current_until = today_date
		current_since = add_days(current_until, -p_days + 1)
		prev_until = add_days(current_since, -1)
		prev_since = add_days(prev_until, -p_days + 1)
		period_label = "7d"
	else:
		p_days = days if days > 0 else 30
		current_until = today_date
		current_since = add_days(current_until, -p_days + 1)
		prev_until = add_days(current_since, -1)
		prev_since = add_days(prev_until, -p_days + 1)
		period_label = f"{p_days}d"

	fields_to_get = [
		"name",
		"campaign_name",
		"status",
		"objective",
		"start_date",
		"end_date",
		"budget",
		"spent",
		"modified",
		"creation",
	]
	has_lead_goal = False
	if frappe.db.exists("DocType", "Marketing Campaign"):
		meta = frappe.get_meta("Marketing Campaign")
		if meta.has_field("lead_goal"):
			fields_to_get.append("lead_goal")
			has_lead_goal = True

	rows = frappe.get_all(
		"Marketing Campaign",
		filters=filters,
		fields=fields_to_get,
		order_by="modified desc",
		limit=500,
	)
	names = [r["name"] for r in rows]

	by_status: dict[str, int] = {}
	total_budget = 0.0
	total_spent = 0.0
	lead_goal_sum = 0
	for r in rows:
		by_status[r["status"]] = by_status.get(r["status"], 0) + 1
		total_budget += float(r.get("budget") or 0)
		total_spent += float(r.get("spent") or 0)
		if has_lead_goal and r.get("lead_goal"):
			lead_goal_sum += int(r.get("lead_goal") or 0)

	leads_total = 0
	prev_leads_total = 0
	leads_by_campaign: dict[str, int] = {}
	leads_trend: dict[str, int] = {}
	if (
		names
		and frappe.db.exists("DocType", "CRM Lead")
		and frappe.get_meta("CRM Lead").has_field("marketing_campaign")
	):
		curr_start_dt = f"{current_since} 00:00:00"
		curr_end_dt = f"{current_until} 23:59:59.999999"
		prev_start_dt = f"{prev_since} 00:00:00"
		prev_end_dt = f"{prev_until} 23:59:59.999999"

		lead_rows = frappe.get_all(
			"CRM Lead",
			filters={
				"marketing_campaign": ["in", names],
				"creation": ["between", [curr_start_dt, curr_end_dt]],
			},
			fields=["marketing_campaign", "creation"],
		)
		leads_total = len(lead_rows)
		for lr in lead_rows:
			leads_by_campaign[lr["marketing_campaign"]] = (
				leads_by_campaign.get(lr["marketing_campaign"], 0) + 1
			)
			if not lr.get("creation"):
				continue
			day_key = frappe.utils.getdate(lr["creation"]).isoformat()
			leads_trend[day_key] = leads_trend.get(day_key, 0) + 1

		prev_lead_count = frappe.db.count(
			"CRM Lead",
			filters={
				"marketing_campaign": ["in", names],
				"creation": ["between", [prev_start_dt, prev_end_dt]],
			},
		)
		prev_leads_total = int(prev_lead_count or 0)

	channel_counts: dict[str, int] = {}
	adapters = get_adapters()
	for name in names:
		for key, adapter in adapters.items():
			try:
				rows_for_channel = adapter["preview"](name)
			except Exception:
				rows_for_channel = []
			if rows_for_channel:
				channel_counts[key] = channel_counts.get(key, 0) + len(rows_for_channel)

	spend_trend: dict[str, float] = {}
	reach_approximate = False
	total_reach = 0
	total_engagement = 0
	total_conversions = 0
	spent_this_period = 0.0

	prev_spend = 0.0
	prev_reach = 0
	prev_engagement = 0
	prev_conversions = 0

	if names:
		if days <= 0 or days >= _LIFETIME_DAYS:
			trend_rows = frappe.db.sql(
				"""
				SELECT date,
					COALESCE(SUM(total_spend), 0.0) AS spend,
					COALESCE(SUM(CASE WHEN granularity = %(granularity_week)s THEN 1 ELSE 0 END), 0) AS week_count
				FROM `tabMarketing Campaign Insight`
				WHERE marketing_campaign IN %(names)s
				  AND granularity IN (%(granularity_day)s, %(granularity_week)s)
				GROUP BY date
				ORDER BY date ASC
			""",
				{
					"names": tuple(names),
					"granularity_day": GRANULARITY_DAY,
					"granularity_week": GRANULARITY_WEEK,
				},
				as_dict=True,
			)
		else:
			trend_rows = frappe.db.sql(
				"""
				SELECT date,
					COALESCE(SUM(total_spend), 0.0) AS spend,
					COALESCE(SUM(CASE WHEN granularity = %(granularity_week)s THEN 1 ELSE 0 END), 0) AS week_count
				FROM `tabMarketing Campaign Insight`
				WHERE marketing_campaign IN %(names)s
				  AND granularity IN (%(granularity_day)s, %(granularity_week)s)
				  AND date BETWEEN %(since)s AND %(until)s
				GROUP BY date
				ORDER BY date ASC
			""",
				{
					"names": tuple(names),
					"granularity_day": GRANULARITY_DAY,
					"granularity_week": GRANULARITY_WEEK,
					"since": current_since,
					"until": current_until,
				},
				as_dict=True,
			)

		if trend_rows:
			for tr in trend_rows:
				d_str = getdate(tr.date).isoformat()
				spend_trend[d_str] = float(tr.spend or 0.0)
				if int(tr.week_count or 0) > 0:
					reach_approximate = True
		else:
			for r in rows:
				if not r.get("creation"):
					continue
				day_key = frappe.utils.getdate(r["creation"]).isoformat()
				spend_trend[day_key] = spend_trend.get(day_key, 0) + float(r.get("spent") or 0)

		# Aggregate current period cross-campaign reach, engagement, conversions, spend
		curr_agg = frappe.db.sql(
			"""
			SELECT
				COALESCE(SUM(total_spend), 0.0) AS spend,
				COALESCE(SUM(CASE WHEN granularity = %(granularity_day)s THEN total_reach ELSE 0 END), 0) AS reach,
				COALESCE(SUM(total_engagement), 0) AS engagement,
				COALESCE(SUM(ads_conversions), 0) AS conversions,
				COALESCE(SUM(CASE WHEN granularity = %(granularity_week)s THEN 1 ELSE 0 END), 0) AS week_count
			FROM `tabMarketing Campaign Insight`
			WHERE marketing_campaign IN %(names)s
			  AND granularity IN (%(granularity_day)s, %(granularity_week)s)
			  AND date BETWEEN %(since)s AND %(until)s
		""",
			{
				"names": tuple(names),
				"granularity_day": GRANULARITY_DAY,
				"granularity_week": GRANULARITY_WEEK,
				"since": current_since,
				"until": current_until,
			},
			as_dict=True,
		)
		if curr_agg and curr_agg[0]:
			ca = curr_agg[0]
			spent_this_period = float(ca.get("spend") or 0.0)
			total_reach = int(ca.get("reach") or 0)
			total_engagement = int(ca.get("engagement") or 0)
			total_conversions = int(ca.get("conversions") or 0)
			if int(ca.get("week_count") or 0) > 0:
				reach_approximate = True

		# Aggregate previous period metrics
		prev_agg = frappe.db.sql(
			"""
			SELECT
				COALESCE(SUM(total_spend), 0.0) AS spend,
				COALESCE(SUM(CASE WHEN granularity = %(granularity_day)s THEN total_reach ELSE 0 END), 0) AS reach,
				COALESCE(SUM(total_engagement), 0) AS engagement,
				COALESCE(SUM(ads_conversions), 0) AS conversions
			FROM `tabMarketing Campaign Insight`
			WHERE marketing_campaign IN %(names)s
			  AND granularity IN (%(granularity_day)s, %(granularity_week)s)
			  AND date BETWEEN %(since)s AND %(until)s
		""",
			{
				"names": tuple(names),
				"granularity_day": GRANULARITY_DAY,
				"granularity_week": GRANULARITY_WEEK,
				"since": prev_since,
				"until": prev_until,
			},
			as_dict=True,
		)
		if prev_agg and prev_agg[0]:
			pa = prev_agg[0]
			prev_spend = float(pa.get("spend") or 0.0)
			prev_reach = int(pa.get("reach") or 0)
			prev_engagement = int(pa.get("engagement") or 0)
			prev_conversions = int(pa.get("conversions") or 0)

	recent = sorted(rows, key=lambda r: r["modified"], reverse=True)[:5]
	upcoming = sorted(
		[
			r
			for r in rows
			if r.get("start_date")
			and r["status"] in ("Draft", "Active")
			and frappe.utils.getdate(r["start_date"]) >= today_date
		],
		key=lambda r: r["start_date"],
	)[:5]
	top_performing = sorted(
		rows,
		key=lambda r: leads_by_campaign.get(r["name"], 0),
		reverse=True,
	)[:5]

	# Cost per lead uses this period's spend only; no spend recorded gives no CPL ("—"),
	# never lifetime spend divided by this period's leads.
	cpl = (spent_this_period / leads_total) if leads_total and spent_this_period > 0 else None
	prev_cpl = (prev_spend / prev_leads_total) if prev_leads_total else None

	kpis = {
		"total_campaigns": len(rows),
		"active_campaigns": by_status.get("Active", 0),
		"paused_campaigns": by_status.get("Paused", 0),
		"draft_campaigns": by_status.get("Draft", 0),
		"completed_campaigns": by_status.get("Completed", 0),
		"total_budget": total_budget,
		"total_spent": total_spent,
		"spent_this_period": spent_this_period,
		"leads_generated": leads_total,
		"cost_per_lead": cpl,
		"reach": total_reach,
		"engagement": total_engagement,
		"conversions": total_conversions,
		"lead_goal": lead_goal_sum if lead_goal_sum > 0 else None,
		"reach_approximate": reach_approximate,
		"period": period_label,
		"period_days": p_days,
		"previous_period": {
			"spent": prev_spend,
			"leads": prev_leads_total,
			"reach": prev_reach,
			"engagement": prev_engagement,
			"conversions": prev_conversions,
			"cost_per_lead": prev_cpl,
		},
	}

	return {
		"days": p_days,
		"period": period_label,
		"kpis": kpis,
		"status_distribution": by_status,
		"channel_distribution": channel_counts,
		"spend_trend": spend_trend,
		"leads_trend": leads_trend,
		"recent_campaigns": recent,
		"upcoming_campaigns": upcoming,
		"top_performing_campaigns": [
			{**r, "leads": leads_by_campaign.get(r["name"], 0)} for r in top_performing
		],
		"all_campaigns": [
			{
				**r,
				"leads": leads_by_campaign.get(r["name"], 0),
			}
			for r in rows
		],
		"reach_approximate": reach_approximate,
	}


def _safe(fn, label: str) -> dict[str, Any]:
	try:
		return fn()
	except Exception as e:
		frappe.log_error(title=f"Campaign rollup for {label} failed", message=frappe.get_traceback())
		return {"module": label, "error": str(e)[:200], "headline": {}, "totals": {}}


def sync_marketing_campaign_insights(campaign: str, full: bool = False) -> None:
	"""Compute and store daily, weekly, and lifetime campaign-level insights.

	Args:
		campaign: Marketing Campaign identifier/name.
		full: If True, backfills all historical days from campaign start_date.
			If False, recomputes only the last CAMPAIGN_DAY_REFRESH_DAYS days
			(or backfills all days if no DAY rows exist yet).

	Returns:
		None.

	Raises:
		None.

	Side effects:
		Upserts Marketing Campaign Insight rows (DAY, WEEK, LIFETIME) in database.
	"""
	doc = frappe.get_doc("Marketing Campaign", campaign)
	if not doc or not doc.start_date:
		return

	today_date = getdate(today())
	start_date = getdate(doc.start_date)
	if start_date > today_date:
		return

	end_date = getdate(doc.end_date) if doc.end_date else None
	if end_date and end_date < start_date:
		return
	sync_end = min(end_date, today_date) if end_date else today_date

	refresh_cutoff = getdate(add_days(today_date, -CAMPAIGN_DAY_REFRESH_DAYS))
	if end_date and end_date < refresh_cutoff:
		has_lifetime = frappe.db.exists(
			"Marketing Campaign Insight",
			{"marketing_campaign": campaign, "granularity": GRANULARITY_LIFETIME},
		)
		if has_lifetime and not full:
			return

	if full:
		current_date = start_date
	else:
		has_prior_insights = frappe.db.exists(
			"Marketing Campaign Insight",
			{"marketing_campaign": campaign},
		)
		if not has_prior_insights:
			current_date = start_date
		else:
			current_date = max(start_date, refresh_cutoff)

	# Fetch child entity names
	# Ads
	ad_campaigns = frappe.get_all("Ad Campaign", filters={"campaign": campaign}, fields=["name"])
	ad_campaign_names = [c.name for c in ad_campaigns] if ad_campaigns else []
	ad_names = []
	if ad_campaigns:
		ad_names = frappe.get_all(
			"Ad", filters={"ad_campaign": ["in", ad_campaign_names]}, pluck="name"
		)

	# Social Posts
	social_posts = frappe.get_all("Social Post", filters={"campaign": campaign}, pluck="name")
	social_post_destinations = []
	if social_posts:
		social_post_destinations = frappe.get_all(
			"Social Post Destination",
			filters={"parent": ["in", social_posts], "parenttype": "Social Post"},
			pluck="name",
		)

	# WhatsApp Blasts
	whatsapp_blasts = frappe.get_all("Bulk WhatsApp Message", filters={"campaign": campaign}, pluck="name")

	# Sync DAY rows
	delta = timedelta(days=1)
	while current_date <= sync_end:
		d_str = current_date.isoformat()
		_sync_campaign_day(
			campaign,
			d_str,
			ad_names,
			social_post_destinations,
			whatsapp_blasts,
			ad_campaign_names=ad_campaign_names,
		)
		current_date += delta

	# Sync LIFETIME row
	_sync_campaign_lifetime(
		campaign,
		ad_names,
		social_post_destinations,
		whatsapp_blasts,
		ad_campaign_names=ad_campaign_names,
	)

	# Roll up older days into week rows
	rebuild_campaign_weeks(campaign)


def _sync_campaign_day(
	campaign: str,
	date_str: str,
	ad_names: list[str],
	social_post_destinations: list[str],
	whatsapp_blasts: list[str],
	ad_campaign_names: list[str] | None = None,
) -> None:
	"""Compute and save a single DAY row for a campaign on date_str.

	Under compacted storage, ads stats read from Ad Campaign DAY rows.

	Args:
		campaign: Name of the Marketing Campaign.
		date_str: ISO date string for the target day.
		ad_names: Legacy list of child ad names.
		social_post_destinations: List of Social Post Destination names.
		whatsapp_blasts: List of Bulk WhatsApp Message names.
		ad_campaign_names: Optional list of Ad Campaign names linked to the campaign.

	Returns:
		None.

	Raises:
		None.

	Side Effects:
		Upserts one Marketing Campaign Insight DAY row.
	"""
	# 1. Ads daily
	ads_stats = {
		"spend": 0.0,
		"impressions": 0,
		"clicks": 0,
		"reach": 0,
		"conversions": 0,
		"conversion_value": 0.0,
	}
	if ad_campaign_names is None:
		ad_campaign_names = frappe.get_all("Ad Campaign", filters={"campaign": campaign}, pluck="name")

	if ad_campaign_names:
		rows = frappe.db.sql(
			"""
            SELECT
                SUM(impressions) AS impressions,
                SUM(reach) AS reach,
                SUM(clicks) AS clicks,
                SUM(spend) AS spend,
                SUM(conversions) AS conversions,
                SUM(conversion_value) AS conversion_value
            FROM `tabAd Insight`
            WHERE reference_doctype = 'Ad Campaign'
              AND reference_name IN %(camp_names)s
              AND date = %(date)s
              AND granularity = %(granularity)s
              AND COALESCE(breakdown_key, '') = ''
        """,
			{"camp_names": tuple(ad_campaign_names), "date": date_str, "granularity": GRANULARITY_DAY},
			as_dict=True,
		)
		if rows and rows[0]:
			r = rows[0]
			ads_stats["impressions"] = int(r.get("impressions") or 0)
			ads_stats["reach"] = int(r.get("reach") or 0)  # None reach means non-additive across multi-row aggregates
			ads_stats["clicks"] = int(r.get("clicks") or 0)
			ads_stats["spend"] = float(r.get("spend") or 0.0)
			ads_stats["conversions"] = int(r.get("conversions") or 0)
			ads_stats["conversion_value"] = float(r.get("conversion_value") or 0.0)

	# 2. Social daily (posts published on date_str)
	social_stats = {
		"impressions": 0,
		"reach": 0,
		"clicks": 0,
		"engagement": 0,
		"reactions": 0,
		"comments": 0,
		"shares": 0,
		"saves": 0,
		"video_views": 0,
		"video_completions": 0,
		"posts": 0,
	}
	if social_post_destinations:
		published_destinations = frappe.db.get_all(
			"Social Post Destination",
			filters={
				"name": ("in", social_post_destinations),
				"status": "Published",
				"published_at": ["between", [f"{date_str} 00:00:00", f"{date_str} 23:59:59"]],
			},
			pluck="name",
		)
		if published_destinations:
			social_stats["posts"] = len(published_destinations)
			rows = frappe.db.sql(
				"""
                SELECT
                    SUM(impressions) AS impressions,
                    SUM(reach) AS reach,
                    SUM(clicks) AS clicks,
                    SUM(engagement) AS engagement,
                    SUM(reactions) AS reactions,
                    SUM(comments) AS comments,
                    SUM(shares) AS shares,
                    SUM(saves) AS saves,
                    SUM(video_views) AS video_views,
                    SUM(video_completions) AS video_completions
                FROM `tabSocial Insight`
                WHERE reference_doctype = 'Social Post Destination'
                  AND reference_name IN %(destinations)s
                  AND granularity = %(granularity)s
            """,
				{"destinations": tuple(published_destinations), "granularity": GRANULARITY_LIFETIME},
				as_dict=True,
			)
			if rows and rows[0]:
				r = rows[0]
				social_stats["impressions"] = int(r.get("impressions") or 0)
				social_stats["reach"] = int(r.get("reach") or 0)
				social_stats["clicks"] = int(r.get("clicks") or 0)
				social_stats["engagement"] = int(r.get("engagement") or 0)
				social_stats["reactions"] = int(r.get("reactions") or 0)
				social_stats["comments"] = int(r.get("comments") or 0)
				social_stats["shares"] = int(r.get("shares") or 0)
				social_stats["saves"] = int(r.get("saves") or 0)
				social_stats["video_views"] = int(r.get("video_views") or 0)
				social_stats["video_completions"] = int(r.get("video_completions") or 0)

	# 3. WhatsApp daily
	whatsapp_stats = {"recipients": 0, "sent": 0, "delivered": 0, "read": 0, "failed": 0}
	if whatsapp_blasts:
		whatsapp_stats["recipients"] = (
			frappe.db.sql(
				"""
            SELECT COALESCE(SUM(recipient_count), 0)
            FROM `tabBulk WhatsApp Message`
            WHERE name IN %(blasts)s
              AND DATE(creation) = %(date)s
        """,
				{"blasts": tuple(whatsapp_blasts), "date": date_str},
			)[0][0]
			or 0
		)

		msg_stats = frappe.db.sql(
			"""
            SELECT
                SUM(CASE WHEN status IN ('sent','delivered','read') THEN 1 ELSE 0 END) AS sent,
                SUM(CASE WHEN status IN ('delivered','read') THEN 1 ELSE 0 END) AS delivered,
                SUM(CASE WHEN status = 'read' THEN 1 ELSE 0 END) AS read_count,
                SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed
            FROM `tabChannel Message`
            WHERE reference_doctype = 'Bulk WhatsApp Message'
              AND reference_name IN %(blasts)s
              AND DATE(creation) = %(date)s
        """,
			{"blasts": tuple(whatsapp_blasts), "date": date_str},
			as_dict=True,
		)
		if msg_stats and msg_stats[0]:
			m = msg_stats[0]
			whatsapp_stats["sent"] = int(m.get("sent") or 0)
			whatsapp_stats["delivered"] = int(m.get("delivered") or 0)
			whatsapp_stats["read"] = int(m.get("read_count") or 0)
			whatsapp_stats["failed"] = int(m.get("failed") or 0)

	# 4. Attribution daily
	leads_count = 0
	if frappe.db.exists("DocType", "CRM Lead") and frappe.get_meta("CRM Lead").has_field(
		"marketing_campaign"
	):
		leads_count = frappe.db.count(
			"CRM Lead",
			{
				"marketing_campaign": campaign,
				"creation": ["between", [f"{date_str} 00:00:00", f"{date_str} 23:59:59"]],
			},
		)

	conversations_count = 0
	if frappe.db.exists("DocType", "Conversation") and frappe.get_meta("Conversation").has_field("campaign"):
		conversations_count = frappe.db.count(
			"Conversation",
			{"campaign": campaign, "creation": ["between", [f"{date_str} 00:00:00", f"{date_str} 23:59:59"]]},
		)

	doc_dict = {
		"doctype": "Marketing Campaign Insight",
		"marketing_campaign": campaign,
		"date": date_str,
		"granularity": GRANULARITY_DAY,
		# KPI Summary
		"total_spend": ads_stats["spend"],
		"total_reach": ads_stats["reach"] + social_stats["reach"] + whatsapp_stats["delivered"],
		"total_engagement": ads_stats["clicks"] + social_stats["engagement"] + whatsapp_stats["read"],
		"total_leads": leads_count,
		"total_conversations": conversations_count,
		# Ads Breakdown
		"ads_spend": ads_stats["spend"],
		"ads_impressions": ads_stats["impressions"],
		"ads_clicks": ads_stats["clicks"],
		"ads_reach": ads_stats["reach"],
		"ads_conversions": ads_stats["conversions"],
		"ads_conversion_value": ads_stats["conversion_value"],
		"ads_ctr": _safe_div(ads_stats["clicks"], ads_stats["impressions"]) * 100,
		"ads_cpc": _safe_div(ads_stats["spend"], ads_stats["clicks"]),
		"ads_cpm": _safe_div(ads_stats["spend"], ads_stats["impressions"]) * 1000,
		"ads_roas": _safe_div(ads_stats["conversion_value"], ads_stats["spend"]),
		"ads_cost_per_conversion": _safe_div(ads_stats["spend"], ads_stats["conversions"]),
		"ads_frequency": _safe_div(ads_stats["impressions"], ads_stats["reach"]),
		"ads_conversion_rate": _safe_div(ads_stats["conversions"], ads_stats["clicks"]),
		# Social Breakdown
		"social_impressions": social_stats["impressions"],
		"social_reach": social_stats["reach"],
		"social_clicks": social_stats["clicks"],
		"social_engagement": social_stats["engagement"],
		"social_reactions": social_stats["reactions"],
		"social_comments": social_stats["comments"],
		"social_shares": social_stats["shares"],
		"social_saves": social_stats["saves"],
		"social_video_views": social_stats["video_views"],
		"social_video_completions": social_stats["video_completions"],
		"social_engagement_rate": _safe_div(social_stats["engagement"], social_stats["reach"]),
		"social_posts": social_stats["posts"],
		# WhatsApp Breakdown
		"whatsapp_recipients": whatsapp_stats["recipients"],
		"whatsapp_sent": whatsapp_stats["sent"],
		"whatsapp_delivered": whatsapp_stats["delivered"],
		"whatsapp_read": whatsapp_stats["read"],
		"whatsapp_failed": whatsapp_stats["failed"],
		"whatsapp_delivery_rate": _safe_div(whatsapp_stats["delivered"], whatsapp_stats["sent"]),
		"whatsapp_read_rate": _safe_div(whatsapp_stats["read"], whatsapp_stats["delivered"]),
		# Attribution
		"leads": leads_count,
		"conversations": conversations_count,
	}

	_upsert_campaign_insight(doc_dict)


def _sync_campaign_lifetime(
	campaign: str,
	ad_names: list[str],
	social_post_destinations: list[str],
	whatsapp_blasts: list[str],
	ad_campaign_names: list[str] | None = None,
) -> None:
	"""Compute and upsert the single LIFETIME row for a campaign across all channels.

	Under compacted storage, ads stats read from Ad Campaign DAY and WEEK rows.

	Args:
		campaign: Name of the Marketing Campaign.
		ad_names: Legacy list of child ad names.
		social_post_destinations: List of Social Post Destination names.
		whatsapp_blasts: List of Bulk WhatsApp Message names.
		ad_campaign_names: Optional list of Ad Campaign names linked to the campaign.

	Returns:
		None.

	Raises:
		None.

	Side Effects:
		Upserts one Marketing Campaign Insight LIFETIME row.
	"""
	# 1. Ads lifetime
	ads_stats = {
		"spend": 0.0,
		"impressions": 0,
		"clicks": 0,
		"reach": 0,
		"conversions": 0,
		"conversion_value": 0.0,
	}
	if ad_campaign_names is None:
		ad_campaign_names = frappe.get_all("Ad Campaign", filters={"campaign": campaign}, pluck="name")

	if ad_campaign_names:
		rows = frappe.db.sql(
			"""
            SELECT
                SUM(impressions) AS impressions,
                SUM(reach) AS reach,
                SUM(clicks) AS clicks,
                SUM(spend) AS spend,
                SUM(conversions) AS conversions,
                SUM(conversion_value) AS conversion_value
            FROM `tabAd Insight`
            WHERE reference_doctype = 'Ad Campaign'
              AND reference_name IN %(camp_names)s
              AND granularity IN ('DAY', 'WEEK')
              AND COALESCE(breakdown_key, '') = ''
        """,
			{"camp_names": tuple(ad_campaign_names)},
			as_dict=True,
		)
		if rows and rows[0]:
			r = rows[0]
			ads_stats["impressions"] = int(r.get("impressions") or 0)
			ads_stats["reach"] = int(r.get("reach") or 0)  # None reach means non-additive across multi-row aggregates
			ads_stats["clicks"] = int(r.get("clicks") or 0)
			ads_stats["spend"] = float(r.get("spend") or 0.0)
			ads_stats["conversions"] = int(r.get("conversions") or 0)
			ads_stats["conversion_value"] = float(r.get("conversion_value") or 0.0)

	# 2. Social lifetime (all post destinations metrics)
	social_stats = {
		"impressions": 0,
		"reach": 0,
		"clicks": 0,
		"engagement": 0,
		"reactions": 0,
		"comments": 0,
		"shares": 0,
		"saves": 0,
		"video_views": 0,
		"video_completions": 0,
		"posts": 0,
	}
	if social_post_destinations:
		social_stats["posts"] = len(social_post_destinations)
		rows = frappe.db.sql(
			"""
            SELECT
                SUM(impressions) AS impressions,
                SUM(reach) AS reach,
                SUM(clicks) AS clicks,
                SUM(engagement) AS engagement,
                SUM(reactions) AS reactions,
                SUM(comments) AS comments,
                SUM(shares) AS shares,
                SUM(saves) AS saves,
                SUM(video_views) AS video_views,
                SUM(video_completions) AS video_completions
            FROM `tabSocial Insight`
            WHERE reference_doctype = 'Social Post Destination'
              AND reference_name IN %(destinations)s
              AND granularity = %(granularity)s
        """,
			{"destinations": tuple(social_post_destinations), "granularity": GRANULARITY_LIFETIME},
			as_dict=True,
		)
		if rows and rows[0]:
			r = rows[0]
			social_stats["impressions"] = int(r.get("impressions") or 0)
			social_stats["reach"] = int(r.get("reach") or 0)
			social_stats["clicks"] = int(r.get("clicks") or 0)
			social_stats["engagement"] = int(r.get("engagement") or 0)
			social_stats["reactions"] = int(r.get("reactions") or 0)
			social_stats["comments"] = int(r.get("comments") or 0)
			social_stats["shares"] = int(r.get("shares") or 0)
			social_stats["saves"] = int(r.get("saves") or 0)
			social_stats["video_views"] = int(r.get("video_views") or 0)
			social_stats["video_completions"] = int(r.get("video_completions") or 0)

	# 3. WhatsApp lifetime
	whatsapp_stats = {"recipients": 0, "sent": 0, "delivered": 0, "read": 0, "failed": 0}
	if whatsapp_blasts:
		whatsapp_stats["recipients"] = (
			frappe.db.sql(
				"""
            SELECT COALESCE(SUM(recipient_count), 0)
            FROM `tabBulk WhatsApp Message`
            WHERE name IN %(blasts)s
        """,
				{"blasts": tuple(whatsapp_blasts)},
			)[0][0]
			or 0
		)

		msg_stats = frappe.db.sql(
			"""
            SELECT
                SUM(CASE WHEN status IN ('sent','delivered','read') THEN 1 ELSE 0 END) AS sent,
                SUM(CASE WHEN status IN ('delivered','read') THEN 1 ELSE 0 END) AS delivered,
                SUM(CASE WHEN status = 'read' THEN 1 ELSE 0 END) AS read_count,
                SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed
            FROM `tabChannel Message`
            WHERE reference_doctype = 'Bulk WhatsApp Message'
              AND reference_name IN %(blasts)s
        """,
			{"blasts": tuple(whatsapp_blasts)},
			as_dict=True,
		)
		if msg_stats and msg_stats[0]:
			m = msg_stats[0]
			whatsapp_stats["sent"] = int(m.get("sent") or 0)
			whatsapp_stats["delivered"] = int(m.get("delivered") or 0)
			whatsapp_stats["read"] = int(m.get("read_count") or 0)
			whatsapp_stats["failed"] = int(m.get("failed") or 0)

	# 4. Attribution lifetime
	leads_count = 0
	if frappe.db.exists("DocType", "CRM Lead") and frappe.get_meta("CRM Lead").has_field(
		"marketing_campaign"
	):
		leads_count = frappe.db.count("CRM Lead", {"marketing_campaign": campaign})

	conversations_count = 0
	if frappe.db.exists("DocType", "Conversation") and frappe.get_meta("Conversation").has_field("campaign"):
		conversations_count = frappe.db.count("Conversation", {"campaign": campaign})

	today_date = getdate(today())

	doc_dict = {
		"doctype": "Marketing Campaign Insight",
		"marketing_campaign": campaign,
		"date": today_date.isoformat(),
		"granularity": GRANULARITY_LIFETIME,
		# KPI Summary
		"total_spend": ads_stats["spend"],
		"total_reach": ads_stats["reach"] + social_stats["reach"] + whatsapp_stats["delivered"],
		"total_engagement": ads_stats["clicks"] + social_stats["engagement"] + whatsapp_stats["read"],
		"total_leads": leads_count,
		"total_conversations": conversations_count,
		# Ads Breakdown
		"ads_spend": ads_stats["spend"],
		"ads_impressions": ads_stats["impressions"],
		"ads_clicks": ads_stats["clicks"],
		"ads_reach": ads_stats["reach"],
		"ads_conversions": ads_stats["conversions"],
		"ads_conversion_value": ads_stats["conversion_value"],
		"ads_ctr": _safe_div(ads_stats["clicks"], ads_stats["impressions"]) * 100,
		"ads_cpc": _safe_div(ads_stats["spend"], ads_stats["clicks"]),
		"ads_cpm": _safe_div(ads_stats["spend"], ads_stats["impressions"]) * 1000,
		"ads_roas": _safe_div(ads_stats["conversion_value"], ads_stats["spend"]),
		"ads_cost_per_conversion": _safe_div(ads_stats["spend"], ads_stats["conversions"]),
		"ads_frequency": _safe_div(ads_stats["impressions"], ads_stats["reach"]),
		"ads_conversion_rate": _safe_div(ads_stats["conversions"], ads_stats["clicks"]),
		# Social Breakdown
		"social_impressions": social_stats["impressions"],
		"social_reach": social_stats["reach"],
		"social_clicks": social_stats["clicks"],
		"social_engagement": social_stats["engagement"],
		"social_reactions": social_stats["reactions"],
		"social_comments": social_stats["comments"],
		"social_shares": social_stats["shares"],
		"social_saves": social_stats["saves"],
		"social_video_views": social_stats["video_views"],
		"social_video_completions": social_stats["video_completions"],
		"social_engagement_rate": _safe_div(social_stats["engagement"], social_stats["reach"]),
		"social_posts": social_stats["posts"],
		# WhatsApp Breakdown
		"whatsapp_recipients": whatsapp_stats["recipients"],
		"whatsapp_sent": whatsapp_stats["sent"],
		"whatsapp_delivered": whatsapp_stats["delivered"],
		"whatsapp_read": whatsapp_stats["read"],
		"whatsapp_failed": whatsapp_stats["failed"],
		"whatsapp_delivery_rate": _safe_div(whatsapp_stats["delivered"], whatsapp_stats["sent"]),
		"whatsapp_read_rate": _safe_div(whatsapp_stats["read"], whatsapp_stats["delivered"]),
		# Attribution
		"leads": leads_count,
		"conversations": conversations_count,
	}

	_upsert_campaign_insight(doc_dict)


_ADDITIVE_INT_COLUMNS = (
	"total_engagement",
	"total_leads",
	"total_conversations",
	"ads_impressions",
	"ads_clicks",
	"ads_conversions",
	"social_impressions",
	"social_clicks",
	"social_engagement",
	"social_reactions",
	"social_comments",
	"social_shares",
	"social_saves",
	"social_video_views",
	"social_video_completions",
	"social_posts",
	"whatsapp_recipients",
	"whatsapp_sent",
	"whatsapp_delivered",
	"whatsapp_read",
	"whatsapp_failed",
	"leads",
	"conversations",
)

_NON_ADDITIVE_INT_COLUMNS = (
	"total_reach",
	"ads_reach",
	"social_reach",
)

_ADDITIVE_FLOAT_COLUMNS = (
	"total_spend",
	"ads_spend",
	"ads_conversion_value",
)


def _upsert_campaign_insight(doc_dict: dict) -> None:
	"""Upsert Marketing Campaign Insight row by deterministic insight_key.

	Args:
		doc_dict: Dictionary containing field values for Marketing Campaign Insight.

	Returns:
		None.

	Raises:
		Exception: If database insertion or document update fails.

	Side effects:
		Writes or updates a Marketing Campaign Insight record.
	"""
	key = make_insight_key(
		"Marketing Campaign",
		doc_dict["marketing_campaign"],
		doc_dict["granularity"],
		doc_dict.get("date") or "",
	)
	doc_dict["insight_key"] = key

	existing = frappe.db.get_value("Marketing Campaign Insight", {"insight_key": key}, "name")
	if not existing:
		legacy_filters = {
			"marketing_campaign": doc_dict["marketing_campaign"],
			"granularity": doc_dict["granularity"],
		}
		if doc_dict["granularity"] not in GRANULARITIES_WITHOUT_DATE and doc_dict.get("date"):
			legacy_filters["date"] = doc_dict["date"]
		existing = frappe.db.get_value("Marketing Campaign Insight", legacy_filters, "name")

	if existing:
		doc = frappe.get_doc("Marketing Campaign Insight", existing)
		doc.update(doc_dict)
		doc.save(ignore_permissions=True)
	else:
		doc = frappe.new_doc("Marketing Campaign Insight")
		doc.update(doc_dict)
		try:
			doc.insert(ignore_permissions=True)
		except (frappe.exceptions.DuplicateEntryError, frappe.exceptions.UniqueValidationError):
			existing = frappe.db.get_value("Marketing Campaign Insight", {"insight_key": key}, "name")
			if existing:
				doc = frappe.get_doc("Marketing Campaign Insight", existing)
				doc.update(doc_dict)
				doc.save(ignore_permissions=True)
			else:
				raise


def _compute_campaign_ratios(stats: dict) -> dict:
	"""Compute ratio metrics from aggregated count and spend statistics.

	Args:
		stats: Dictionary containing aggregated sums.

	Returns:
		dict: Dictionary of recomputed ratio metrics.

	Raises:
		None.

	Side effects:
		None.
	"""
	return {
		"ads_ctr": _safe_div(stats["ads_clicks"], stats["ads_impressions"]) * 100,
		"ads_cpc": _safe_div(stats["ads_spend"], stats["ads_clicks"]),
		"ads_cpm": _safe_div(stats["ads_spend"], stats["ads_impressions"]) * 1000,
		"ads_roas": _safe_div(stats["ads_conversion_value"], stats["ads_spend"]),
		"ads_cost_per_conversion": _safe_div(stats["ads_spend"], stats["ads_conversions"]),
		"ads_frequency": _safe_div(stats["ads_impressions"], stats["ads_reach"]),
		"ads_conversion_rate": _safe_div(stats["ads_conversions"], stats["ads_clicks"]),
		"social_engagement_rate": _safe_div(stats["social_engagement"], stats["social_reach"]),
		"whatsapp_delivery_rate": _safe_div(stats["whatsapp_delivered"], stats["whatsapp_sent"]),
		"whatsapp_read_rate": _safe_div(stats["whatsapp_read"], stats["whatsapp_delivered"]),
	}


def rebuild_campaign_weeks(
	campaign: str | None = None,
	as_of: str | _date | None = None,
	since: str | _date | None = None,
) -> int:
	"""Aggregate DAY insight rows older than INSIGHT_DAY_KEEP_DAYS into ISO Monday-start WEEK rows.

	Only rolls up complete weeks whose Sunday (Monday + 6 days) is strictly older
	than the retention cutoff (cutoff = as_of - INSIGHT_DAY_KEEP_DAYS). A week that
	straddles the cutoff remains as DAY rows until the entire week is older than the
	cutoff, preventing data loss from partial overwrites.

	Args:
		campaign: Optional campaign name. If provided, processes only this campaign;
			otherwise processes all campaigns with candidate DAY rows.
		as_of: Reference date for retention cutoff (defaults to today()).
		since: Optional start date for rebuild lookback.

	Returns:
		int: Number of WEEK rows written or updated.

	Raises:
		Exception: If database query or document save fails.

	Side effects:
		Inserts or updates Marketing Campaign Insight WEEK rows, and deletes
		underlying DAY rows only after corresponding WEEK row exists.
	"""
	today_date = getdate(as_of) if as_of else getdate(today())
	cutoff = getdate(add_days(today_date, -INSIGHT_DAY_KEEP_DAYS))

	filters: list[list[Any]] = [
		["granularity", "=", GRANULARITY_DAY],
		["date", "<", cutoff],
	]
	if campaign:
		filters.append(["marketing_campaign", "=", campaign])
	if since:
		since_monday = week_monday(since)
		if since_monday < cutoff:
			filters.append(["date", ">=", since_monday])

	day_rows = frappe.get_all(
		"Marketing Campaign Insight",
		filters=filters,
		fields=["*"],
		order_by="date asc",
	)
	if not day_rows:
		return 0

	# Group rows by (marketing_campaign, week_monday_iso)
	groups: dict[tuple[str, str], list[dict]] = {}
	for r in day_rows:
		r_date = getdate(r.date)
		monday = week_monday(r_date)
		group_key = (r.marketing_campaign, monday.isoformat())
		groups.setdefault(group_key, []).append(r)

	weeks_written = 0

	for (camp, monday_str), rows_in_week in groups.items():
		monday_date = getdate(monday_str)
		sunday_date = monday_date + timedelta(days=6)
		# Only roll up complete weeks whose entire span is strictly older than cutoff
		if sunday_date >= cutoff:
			continue

		agg: dict[str, Any] = {col: 0 for col in _ADDITIVE_INT_COLUMNS}
		agg.update({col: 0 for col in _NON_ADDITIVE_INT_COLUMNS})
		agg.update({col: 0.0 for col in _ADDITIVE_FLOAT_COLUMNS})

		for row in rows_in_week:
			for col in _ADDITIVE_INT_COLUMNS:
				agg[col] += int(row.get(col) or 0)
			for col in _ADDITIVE_FLOAT_COLUMNS:
				agg[col] += float(row.get(col) or 0.0)

		ratios = _compute_campaign_ratios(agg)

		week_doc_dict = {
			"doctype": "Marketing Campaign Insight",
			"marketing_campaign": camp,
			"date": monday_str,
			"granularity": GRANULARITY_WEEK,
			**agg,
			**ratios,
		}

		_upsert_campaign_insight(week_doc_dict)

		week_key = week_doc_dict.get("insight_key")
		# Delete DAY rows only when the WEEK row exists
		if week_key and frappe.db.exists("Marketing Campaign Insight", {"insight_key": week_key}):
			day_names = [r.name for r in rows_in_week]
			delete_in_batches("Marketing Campaign Insight", day_names, commit=False)
			weeks_written += 1

	if weeks_written > 0:
		frappe.db.commit()

	return weeks_written


def _safe_div(n: float | int, d: float | int) -> float:
	"""Safely perform division avoiding ZeroDivisionError.

	Args:
		n: Numerator.
		d: Denominator.

	Returns:
		float: Division result or 0.0 if denominator is falsy.

	Raises:
		None.

	Side effects:
		None.
	"""
	return float(n) / float(d) if d else 0.0

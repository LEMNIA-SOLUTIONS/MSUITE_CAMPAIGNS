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

from typing import Any

import frappe
from frappe.utils import getdate, today

from msuite_campaigns.campaign.services import attribution
from msuite_campaigns.campaign.services.events import CampaignEvent, emit
from msuite_campaigns.campaign.services.relationships import get_adapters
from msuite_core.utils.insight_keys import make_key

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


def get_campaign_dashboard(campaign: str, days: int = 30) -> dict:
	"""Everything the Performance tab needs in one shot. Each adapter's
	rollup is independently tolerant of missing data; wrapped anyway so
	one channel's exception doesn't take the whole dashboard down.
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
		"totals": {
			"total_spend": summary["spend"],
			"total_reach": summary["reach"],
			"total_engagement": summary["engagement"],
			"total_leads": attribution_summary["leads"],
			"total_conversations": attribution_summary["conversations"],
		},
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
		int(ads.get("reach") or 0)
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


def get_campaigns_overview(days: int = 30, filters: dict | None = None) -> dict:
	"""Cross-campaign rollup for the Campaigns Dashboard (`/campaigns`) —
	distinct from `get_campaign_dashboard` above, which is scoped to one
	campaign's Mission Control page.

	No materialized rollup table (same constraint noted at the top of
	this file) — computed live from `Marketing Campaign` plus the
	existing per-campaign adapter `preview()` calls for channel
	distribution, and `CRM Lead.marketing_campaign` for leads, following
	the same precedent `attribution.py` already set for that field.
	"""
	days = int(days)
	filters = dict(filters or {})

	rows = frappe.get_all(
		"Marketing Campaign",
		filters=filters,
		fields=[
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
		],
		order_by="modified desc",
		limit=500,
	)
	names = [r["name"] for r in rows]

	by_status: dict[str, int] = {}
	total_budget = 0.0
	total_spent = 0.0
	for r in rows:
		by_status[r["status"]] = by_status.get(r["status"], 0) + 1
		total_budget += float(r.get("budget") or 0)
		total_spent += float(r.get("spent") or 0)

	leads_total = 0
	leads_by_campaign: dict[str, int] = {}
	leads_trend: dict[str, int] = {}
	if (
		names
		and frappe.db.exists("DocType", "CRM Lead")
		and frappe.get_meta("CRM Lead").has_field("marketing_campaign")
	):
		lead_rows = frappe.get_all(
			"CRM Lead",
			filters={
				"marketing_campaign": ["in", names],
				"creation": [">=", frappe.utils.add_days(frappe.utils.now_datetime(), -days)],
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

	# Channel distribution — reuses each registered channel's own
	# `preview()` (same call `check_campaign_setup` makes per-campaign),
	# looped here across the visible set rather than duplicating
	# per-channel doctype names in this file (see relationships.py's
	# decoupling note).
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
	for r in rows:
		if not r.get("creation"):
			continue
		day_key = frappe.utils.getdate(r["creation"]).isoformat()
		spend_trend[day_key] = spend_trend.get(day_key, 0) + float(r.get("spent") or 0)

	today = frappe.utils.getdate(frappe.utils.now_datetime())
	recent = sorted(rows, key=lambda r: r["modified"], reverse=True)[:5]
	upcoming = sorted(
		[
			r
			for r in rows
			if r.get("start_date")
			and r["status"] in ("Draft", "Active")
			and frappe.utils.getdate(r["start_date"]) >= today
		],
		key=lambda r: r["start_date"],
	)[:5]
	top_performing = sorted(
		rows,
		key=lambda r: leads_by_campaign.get(r["name"], 0),
		reverse=True,
	)[:5]

	leads = leads_total  # for cost_per_lead readability below
	return {
		"days": days,
		"kpis": {
			"total_campaigns": len(rows),
			"active_campaigns": by_status.get("Active", 0),
			"draft_campaigns": by_status.get("Draft", 0),
			"completed_campaigns": by_status.get("Completed", 0),
			"total_budget": total_budget,
			"total_spent": total_spent,
			"leads_generated": leads_total,
			# Honest substitute for a fictional "ROI" — there's no revenue
			# field on this doctype and no Campaign↔Deal link to compute
			# real ROI from (see plan notes). Cost per lead is real math
			# on real numbers; null (not 0) when there's no spend or leads
			# yet so the UI can show "—" instead of a misleading ₹0.
			"cost_per_lead": (total_spent / leads) if leads else None,
		},
		"status_distribution": by_status,
		"channel_distribution": channel_counts,
		"spend_trend": spend_trend,
		"leads_trend": leads_trend,
		"recent_campaigns": recent,
		"upcoming_campaigns": upcoming,
		"top_performing_campaigns": [
			{**r, "leads": leads_by_campaign.get(r["name"], 0)} for r in top_performing
		],
	}


def _safe(fn, label: str) -> dict[str, Any]:
	try:
		return fn()
	except Exception as e:
		frappe.log_error(title=f"Campaign rollup for {label} failed", message=frappe.get_traceback())
		return {"module": label, "error": str(e)[:200], "headline": {}, "totals": {}}


def sync_marketing_campaign_insights(campaign: str) -> None:
	"""Compute and store daily and lifetime campaign-level insights."""
	doc = frappe.get_doc("Marketing Campaign", campaign)
	if not doc:
		return

	# Fetch child entity names
	# Ads
	ad_campaigns = frappe.get_all("Ad Campaign", filters={"campaign": campaign}, fields=["name"])
	ad_names = []
	if ad_campaigns:
		ad_names = frappe.get_all(
			"Ad", filters={"ad_campaign": ["in", [c.name for c in ad_campaigns]]}, pluck="name"
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
	start_date = getdate(doc.start_date)
	end_date = getdate(doc.end_date)
	today_date = getdate(today())
	sync_end = min(end_date, today_date)

	import datetime

	current_date = start_date
	delta = datetime.timedelta(days=1)

	while current_date <= sync_end:
		d_str = current_date.isoformat()
		_sync_campaign_day(campaign, d_str, ad_names, social_post_destinations, whatsapp_blasts)
		current_date += delta

	# Sync LIFETIME row
	_sync_campaign_lifetime(campaign, ad_names, social_post_destinations, whatsapp_blasts)


def _sync_campaign_day(
	campaign: str,
	date_str: str,
	ad_names: list[str],
	social_post_destinations: list[str],
	whatsapp_blasts: list[str],
) -> None:
	# 1. Ads daily
	ads_stats = {
		"spend": 0.0,
		"impressions": 0,
		"clicks": 0,
		"reach": 0,
		"conversions": 0,
		"conversion_value": 0.0,
	}
	if ad_names:
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
            WHERE reference_doctype = 'Ad'
              AND reference_name IN %(ad_names)s
              AND date = %(date)s
              AND granularity = 'DAY'
              AND COALESCE(breakdown_key, '') IN ('', 'unattributed')
        """,
			{"ad_names": tuple(ad_names), "date": date_str},
			as_dict=True,
		)
		if rows and rows[0]:
			r = rows[0]
			ads_stats["impressions"] = int(r.get("impressions") or 0)
			ads_stats["reach"] = int(r.get("reach") or 0)
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
                  AND granularity = 'LIFETIME'
            """,
				{"destinations": tuple(published_destinations)},
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
		"granularity": "DAY",
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
	campaign: str, ad_names: list[str], social_post_destinations: list[str], whatsapp_blasts: list[str]
) -> None:
	# 1. Ads lifetime
	ads_stats = {
		"spend": 0.0,
		"impressions": 0,
		"clicks": 0,
		"reach": 0,
		"conversions": 0,
		"conversion_value": 0.0,
	}
	if ad_names:
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
            WHERE reference_doctype = 'Ad'
              AND reference_name IN %(ad_names)s
              AND granularity = 'DAY'
              AND COALESCE(breakdown_key, '') IN ('', 'unattributed')
        """,
			{"ad_names": tuple(ad_names)},
			as_dict=True,
		)
		if rows and rows[0]:
			r = rows[0]
			ads_stats["impressions"] = int(r.get("impressions") or 0)
			ads_stats["reach"] = int(r.get("reach") or 0)
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
              AND granularity = 'LIFETIME'
        """,
			{"destinations": tuple(social_post_destinations)},
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
		"granularity": "LIFETIME",
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


def _upsert_campaign_insight(doc_dict: dict) -> None:
	key = make_key(doc_dict["marketing_campaign"], doc_dict["date"], doc_dict["granularity"])
	doc_dict["insight_key"] = key

	existing = frappe.db.get_value("Marketing Campaign Insight", {"insight_key": key}, "name")
	if existing:
		doc = frappe.get_doc("Marketing Campaign Insight", existing)
		doc.update(doc_dict)
		doc.save(ignore_permissions=True)
	else:
		doc = frappe.new_doc("Marketing Campaign Insight")
		doc.update(doc_dict)
		try:
			doc.insert(ignore_permissions=True)
		except frappe.exceptions.UniqueValidationError:
			existing = frappe.db.get_value("Marketing Campaign Insight", {"insight_key": key}, "name")
			if existing:
				doc = frappe.get_doc("Marketing Campaign Insight", existing)
				doc.update(doc_dict)
				doc.save(ignore_permissions=True)


def _safe_div(n: float | int, d: float | int) -> float:
	return float(n) / float(d) if d else 0.0

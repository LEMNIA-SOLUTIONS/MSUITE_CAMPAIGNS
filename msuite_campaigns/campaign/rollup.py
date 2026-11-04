"""
Top-level Campaign unified rollup — thin `@frappe.whitelist()` wrappers
over `campaign/services/analytics.py`.

Architecture correction: this file used to import `ads.domain.insights
.query`, `social_post.insights.query`, and `whatsapp.analytics.query`
directly — Campaign depended on every channel by name. The aggregation
logic now lives in `campaign/services/analytics.py`, which only knows
the generic adapter shape from `campaign/services/relationships.py`.
Function names below are unchanged so existing callers (the Vue SPA,
`ads/tests/e2e.py`) don't need to change.
"""

from __future__ import annotations

import frappe

from msuite_campaigns.campaign.services import analytics, attribution


@frappe.whitelist()
def check_campaign_setup(campaign: str) -> dict:
	if not frappe.has_permission("Marketing Campaign", "read", doc=campaign):
		frappe.throw("Not permitted", frappe.PermissionError)
	return analytics.check_campaign_setup(campaign)


@frappe.whitelist()
def refresh_campaign_insights(campaign: str) -> dict:
	if not frappe.has_permission("Marketing Campaign", "read", doc=campaign):
		frappe.throw("Not permitted", frappe.PermissionError)
	return analytics.refresh_campaign_insights(campaign)


@frappe.whitelist()
def get_campaign_dashboard(campaign: str, days: int = 30) -> dict:
	if not frappe.has_permission("Marketing Campaign", "read", doc=campaign):
		frappe.throw("Not permitted", frappe.PermissionError)
	return analytics.get_campaign_dashboard(campaign, days)


# ── Attribution — Leads / Inbox tabs on Campaign Mission Control ──────────
#
# `campaign/services/attribution.py` already does the query work (added
# in Phase 2 / Attribution Foundation); these were never exposed over RPC
# because nothing in the SPA needed them yet. Thin wrappers only — same
# permission-check shape as the rest of this file.


@frappe.whitelist()
def get_campaign_leads(campaign: str) -> list[dict]:
	if not frappe.has_permission("Marketing Campaign", "read", doc=campaign):
		frappe.throw("Not permitted", frappe.PermissionError)
	return attribution.get_leads_for_campaign(campaign)


@frappe.whitelist()
def get_campaign_conversations(campaign: str) -> list[dict]:
	if not frappe.has_permission("Marketing Campaign", "read", doc=campaign):
		frappe.throw("Not permitted", frappe.PermissionError)
	return attribution.get_conversations_for_campaign(campaign)


@frappe.whitelist()
def get_campaign_attribution_summary(campaign: str) -> dict:
	if not frappe.has_permission("Marketing Campaign", "read", doc=campaign):
		frappe.throw("Not permitted", frappe.PermissionError)
	return attribution.get_attribution_summary(campaign)

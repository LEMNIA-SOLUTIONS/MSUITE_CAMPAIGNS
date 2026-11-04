"""
Campaigns Dashboard — the cross-campaign overview behind `/campaigns`
(distinct from `rollup.py`, which is scoped to one campaign's Mission
Control page). Thin `@frappe.whitelist()` wrapper over
`campaign/services/analytics.py:get_campaigns_overview`, same shape as
the rest of this module's API surface.
"""

from __future__ import annotations

import json

import frappe

from msuite_campaigns.campaign.services import analytics


@frappe.whitelist()
def get_campaigns_overview_dashboard(days: int = 30, filters: str = "{}") -> dict:
	if not frappe.has_permission("Marketing Campaign", "read"):
		frappe.throw("Not permitted", frappe.PermissionError)
	parsed_filters = json.loads(filters or "{}")
	return analytics.get_campaigns_overview(int(days), parsed_filters)

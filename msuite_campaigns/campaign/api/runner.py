"""
Run-Campaign RPC surface — thin `@frappe.whitelist()` wrappers over
`campaign/services/lifecycle.py`.

Architecture correction: this file used to contain the actual
orchestration logic (per-channel readiness checks, Meta/WhatsApp
status enums, direct imports of `ads.publish.jobs` and `social_post
.scheduler.runner`). That logic has moved to
`campaign/services/lifecycle.py` (channel-agnostic) and each channel's
own `<module>/campaign_adapter.py` (channel-specific). This file now
only does the RPC-boundary work: permission checks and translating
between the wire and the service layer. See CAMPAIGN_ARCHITECTURE
_AUDIT.md §10 (Service Layer Audit) for why this split exists.
"""

from __future__ import annotations

import frappe
from frappe import _

from msuite_campaigns.campaign.services import lifecycle


@frappe.whitelist()
def preview_campaign_run(campaign: str) -> dict:
	if not frappe.has_permission("Marketing Campaign", "read", doc=campaign):
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	return lifecycle.preview_campaign_run(campaign)


@frappe.whitelist()
def run_campaign(campaign: str) -> dict:
	if not frappe.has_permission("Marketing Campaign", "write", doc=campaign):
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	return lifecycle.run_campaign(campaign)


@frappe.whitelist()
def pause_campaign(campaign: str) -> dict:
	if not frappe.has_permission("Marketing Campaign", "write", doc=campaign):
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	return lifecycle.pause_campaign(campaign)


@frappe.whitelist()
def resume_campaign(campaign: str) -> dict:
	if not frappe.has_permission("Marketing Campaign", "write", doc=campaign):
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	return lifecycle.resume_campaign(campaign)


@frappe.whitelist()
def stop_campaign(campaign: str) -> dict:
	if not frappe.has_permission("Marketing Campaign", "write", doc=campaign):
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	return lifecycle.stop_campaign(campaign)


@frappe.whitelist()
def complete_campaign(campaign: str) -> dict:
	if not frappe.has_permission("Marketing Campaign", "write", doc=campaign):
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	return lifecycle.complete_campaign(campaign)


@frappe.whitelist()
def archive_campaign(campaign: str) -> dict:
	if not frappe.has_permission("Marketing Campaign", "write", doc=campaign):
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	return lifecycle.archive_campaign(campaign)

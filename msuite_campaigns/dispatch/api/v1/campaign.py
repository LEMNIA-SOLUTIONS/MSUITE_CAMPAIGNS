# Copyright (c) 2026, MSuite and contributors
# For license information, please see license.txt

"""
Whitelisted surface for the unified dispatch hub.

Thin — parses JSON, delegates to `dispatch.hub`, returns. Mirrors the shape of
`campaign/api/v1/composer.py` so the SPA's service layer stays consistent.
"""

from __future__ import annotations

import json

import frappe
from frappe import _

from msuite_campaigns.dispatch import hub


def _payload(raw) -> dict:
	if isinstance(raw, dict):
		return raw
	try:
		return json.loads(raw or "{}")
	except (json.JSONDecodeError, TypeError):
		frappe.throw(frappe._("Invalid payload — expected JSON."))


@frappe.whitelist()
def get_available_channels() -> dict:
	"""Which channels this site can actually dispatch to. The wizard renders
	its channel picker from this rather than a hard-coded list, so Phase 3's
	SMS module appears with no frontend change."""
	from msuite_campaigns.dispatch import materializer

	return {
		"channels": [
			c
			for c in hub.available_channels()
			if frappe.has_permission(materializer.CHANNEL_DOCTYPE[c], "create")
		]
	}


@frappe.whitelist()
def preview(payload: str = "") -> dict:
	# perm: authorized via hub.preview/_authorize
	return hub.preview(_payload(payload))


@frappe.whitelist()
def launch(payload: str = "") -> dict:
	# perm: authorized via hub.prepare/_authorize
	"""Non-blocking launch: validate synchronously, then enqueue the heavy
	materialisation + submit work as a background job. Returns instantly
	with the campaign name so the frontend can poll for status."""
	parsed = _payload(payload)
	if parsed.get("dry_run"):
		return hub.preview(parsed)

	result = hub.prepare(parsed)
	campaign = result["campaign"]

	frappe.enqueue(
		"msuite_campaigns.dispatch.hub.launch_async",
		queue="long",
		timeout=600,
		job_name=f"campaign_launch_{campaign}",
		payload=parsed,
		campaign=campaign,
	)

	return {
		"campaign": campaign,
		"audience": result["audience"],
		"status": "queued",
	}


@frappe.whitelist()
def get_launch_status(campaign: str) -> dict:
	if not frappe.has_permission("Marketing Campaign", "read", doc=campaign):
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	return hub.get_launch_status(campaign)


@frappe.whitelist()
def get_unified_progress(campaign: str) -> dict:
	if not frappe.has_permission("Marketing Campaign", "read", doc=campaign):
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	return hub.unified_progress(campaign)


@frappe.whitelist()
def pause_all(campaign: str) -> dict:
	if not frappe.has_permission("Marketing Campaign", "write", doc=campaign):
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	return hub.fan_out(campaign, "pause")


@frappe.whitelist()
def resume_all(campaign: str) -> dict:
	if not frappe.has_permission("Marketing Campaign", "write", doc=campaign):
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	return hub.fan_out(campaign, "resume")


@frappe.whitelist()
def stop_all(campaign: str) -> dict:
	if not frappe.has_permission("Marketing Campaign", "write", doc=campaign):
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	return hub.fan_out(campaign, "stop")


@frappe.whitelist()
def retry_all(campaign: str) -> dict:
	if not frappe.has_permission("Marketing Campaign", "write", doc=campaign):
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	return hub.retry_campaign(campaign)



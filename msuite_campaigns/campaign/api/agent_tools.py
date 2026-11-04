"""Agent endpoints for Marketing Campaigns. Old path: msuite_workspace.api.v1.agent.agent_facade."""

from __future__ import annotations

import json
import re

import frappe

from msuite_core.utils.agent_results import (
	call_safely as _call_safely,
	fail as _fail,
	ok as _ok,
	strip_frappe_internals as _strip_frappe_internals,
)
from msuite_campaigns.campaign.api import runner as campaign_runner
from msuite_campaigns.campaign.api.v1 import composer as campaign_composer
from msuite_core.exceptions import PublishError as _AdPublishError


def _slugify(text: str) -> str:
	"""Convert a display name to a safe UTM-compatible string.
	'Summer Launch 2026' → 'summer_launch_2026'
	"""
	return re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")


_CAMPAIGN_FIELDS = {
	"campaign_name",
	"objective",
	"goal",
	"start_date",
	"end_date",
	"budget",
	"utm_source",
	"utm_medium",
	"utm_campaign",
	"utm_term",
	"first_comment_template",
	"recurrence_pattern",
	"recurrence_interval",
	"recurrence_end_at",
}


@frappe.whitelist(methods=["POST"])
def create_campaign(
	campaign_name: str,
	objective: str = "",
	goal: str = "",
	start_date: str = "",
	end_date: str = "",
	budget: float = 0,
	utm_source: str = "",
	utm_medium: str = "social",
	utm_campaign: str = "",
	utm_term: str = "",
) -> dict:
	# perm: delegated to campaign_composer.save_campaign (checks create)
	"""1. POST — Create a Marketing Campaign. Always starts as Draft.

	utm_campaign auto-fill: if not provided, defaults to a slugified
	version of campaign_name (e.g. "Summer Launch" → "summer_launch").
	Override by passing utm_campaign explicitly.
	"""
	if not campaign_name:
		return _fail("validation_error: 'campaign_name' is required.")
	if not start_date:
		return _fail("validation_error: 'start_date' is required.")
	if not end_date:
		return _fail("validation_error: 'end_date' is required.")

	# Auto-fill utm_campaign from campaign_name when not provided.
	if not utm_campaign:
		utm_campaign = _slugify(campaign_name)

	fields = {
		"campaign_name": campaign_name,
		"objective": objective,
		"goal": goal,
		"start_date": start_date,
		"end_date": end_date,
		"budget": budget,
		"utm_source": utm_source,
		"utm_medium": utm_medium,
		"utm_campaign": utm_campaign,
		"utm_term": utm_term,
	}
	# Strip empty / zero values — composer treats absent keys differently
	# from empty-string keys for some fields.
	fields = {k: v for k, v in fields.items() if v not in ("", None, 0)}

	success, result = _call_safely(campaign_composer.save_campaign, name="", payload=json.dumps(fields))
	if not success:
		return _fail(result)
	campaign_id = result.get("name") if isinstance(result, dict) else ""
	return _ok({"id": campaign_id}, "Campaign created.")


@frappe.whitelist(methods=["PATCH"])
def update_campaign(campaign_id: str, **fields) -> dict:
	# perm: delegated to campaign_composer.save_campaign (checks write)
	"""2. PATCH — Update fields on an existing Marketing Campaign.

	Only the fields you send are changed — absent fields are left
	untouched. `status` is blocked: use
	campaign.api.v1.composer.update_campaign_status for status changes,
	which runs transition validation and fires the lifecycle event.

	utm_campaign: not auto-updated here. If you rename the campaign and
	want utm_campaign to stay in sync, pass utm_campaign explicitly.
	"""
	if not campaign_id:
		return _fail("'campaign_id' is required.")

	# Strip cmd and other Frappe-injected internals before field validation.
	fields = _strip_frappe_internals(fields)

	if "status" in fields:
		return _fail(
			"Do not set 'status' via update_campaign — it bypasses "
			"validate_status_transition() and emit_for_status_change(). "
			"Use campaign.api.v1.composer.update_campaign_status instead."
		)

	unknown = set(fields.keys()) - _CAMPAIGN_FIELDS
	if unknown:
		return _fail(f"Unknown field(s): {sorted(unknown)}. Allowed: {sorted(_CAMPAIGN_FIELDS)}")

	success, result = _call_safely(
		campaign_composer.save_campaign, name=campaign_id, payload=json.dumps(fields)
	)
	if not success:
		return _fail(result)
	return _ok({"id": campaign_id, "updated": fields}, "Campaign updated.")


@frappe.whitelist(methods=["GET"])
def list_campaigns(status: str = "", limit: int = 100, start: int = 0) -> dict:
	# perm: delegated to campaign_composer.list_marketing_campaigns (checks read)
	"""3. GET — List Marketing Campaigns.

	All params are query string parameters:
	  GET /api/method/...list_campaigns?status=Active&limit=20&start=0
	"""
	filters = [["status", "=", status]] if status else []

	success, result = _call_safely(
		campaign_composer.list_marketing_campaigns,
		filters=json.dumps(filters),
		fields=json.dumps([]),
		order_by="modified desc",
		limit=int(limit),
		start=int(start),
	)
	if not success:
		return _fail(result)
	return _ok(result)


@frappe.whitelist(methods=["GET"])
def get_campaign(campaign_id: str) -> dict:
	# perm: delegated to campaign_composer.get_campaign (checks read)
	"""4. GET — Get a single Marketing Campaign's full state.

	Query string: GET /api/method/...get_campaign?campaign_id=MC-2026-00001
	"""
	if not campaign_id:
		return _fail("'campaign_id' is required.")

	success, result = _call_safely(campaign_composer.get_campaign, name=campaign_id)
	if not success:
		return _fail(result)
	return _ok(result)


@frappe.whitelist(methods=["POST"])
def publish_campaign(campaign_id: str, dry_run: bool = False) -> dict:
	"""9. POST — Publish the parent Marketing Campaign and fire all children.

	Prerequisites:
	  Social Posts:   PATCH update_social_post(step="schedule", publish_on_campaign_run=1)
	  Ad Campaigns:   PATCH update_ad(step="budget", launch_on_run=1)

	Children without these flags are skipped and reported in preflight.

	dry_run=True returns a preflight summary without firing anything.

	Failure semantics:
	  - Per-child failures are reported in data.runner_result, not as
	    ok=False — partial success is a valid state.
	  - A failed Ad Campaign is rolled back to Draft automatically.
	    Social Posts that already published are NOT rolled back.
	  - ok=False only for runner-level errors (permission, campaign not
	    found, runner crash).
	"""
	if not campaign_id:
		return _fail("'campaign_id' is required.")

	if not frappe.has_permission("Marketing Campaign", "write", doc=campaign_id):
		return _fail(f"permission_denied: No write permission for Marketing Campaign {campaign_id}.")

	if not frappe.db.exists("Marketing Campaign", campaign_id):
		return _fail(f"Marketing Campaign '{campaign_id}' does not exist.")

	campaign = frappe.get_doc("Marketing Campaign", campaign_id)

	if campaign.status not in ("Draft", "Paused"):
		return _fail(
			f"Campaign status is '{campaign.status}'. "
			f"Only Draft or Paused campaigns can be published. "
			f"Use update_campaign_status to reset to Draft if needed."
		)

	# Preflight: read all children and classify which will fire.
	success, channels = _call_safely(campaign_composer.list_channels, campaign=campaign_id)
	if not success:
		return _fail(f"Could not read campaign children: {channels}")

	social_posts = channels.get("social", [])
	ad_campaigns = channels.get("ads", [])

	social_will_fire = [sp for sp in social_posts if sp.get("status") in ("Draft", "Scheduled")]
	ads_will_fire = [
		ac
		for ac in ad_campaigns
		if ac.get("status") == "Draft" and frappe.db.get_value("Ad Campaign", ac["name"], "launch_on_run")
	]
	social_skipped = [sp for sp in social_posts if sp not in social_will_fire]
	ads_skipped = [ac for ac in ad_campaigns if ac not in ads_will_fire]

	preflight = {
		"social_posts_will_fire": len(social_will_fire),
		"social_posts_skipped": len(social_skipped),
		"ad_campaigns_will_fire": len(ads_will_fire),
		"ad_campaigns_skipped": len(ads_skipped),
		"skip_reasons": {
			"social": [
				{
					"name": sp["name"],
					"reason": f"status='{sp.get('status')}' — not Draft or Scheduled",
				}
				for sp in social_skipped
			],
			"ads": [
				{
					"name": ac["name"],
					"reason": "launch_on_run=0 — PATCH budget step with launch_on_run=1",
				}
				for ac in ads_skipped
			],
		},
	}

	if preflight["social_posts_will_fire"] == 0 and preflight["ad_campaigns_will_fire"] == 0:
		return _fail(
			"validation_error: No children are configured to fire. "
			"Social Posts need PATCH update_social_post(step='schedule', publish_on_campaign_run=1). "
			"Ad Campaigns need PATCH update_ad(step='budget', launch_on_run=1)."
		)

	if dry_run:
		return _ok(
			{"preflight": preflight, "fired": False},
			"Dry run complete — no children fired.",
		)

	# Fire the campaign runner.
	try:
		result = campaign_runner.run_campaign(campaign_id)
	except _AdPublishError as e:
		return _fail(
			f"Ad Campaign launch failed at step '{e.step}': {e.cause}. "
			f"The failed Ad Campaign was rolled back to Draft automatically. "
			f"Social Posts that already fired are NOT rolled back.",
			data={"preflight": preflight},
		)
	except frappe.PermissionError as e:
		return _fail(f"permission_denied: {e}", data={"preflight": preflight})
	except frappe.ValidationError as e:
		return _fail(f"validation_error: {e}", data={"preflight": preflight})

	# Check for child failures.
	failures = []
	if isinstance(result, dict):
		outcomes = result.get("outcomes") or {}
		for _platform_key, rows in outcomes.items():
			for row in rows:
				if row.get("outcome") == "failed":
					title = row.get("title") or row.get("name") or "Child"
					msg = row.get("message") or "Unknown error"
					failures.append(f"{title}: {msg}")

	if failures:
		error_msg = f"validation_error: Publish failed for children: {'; '.join(failures)}"
		return _fail(
			error_msg,
			data={
				"preflight": preflight,
				"fired": True,
				"runner_result": result,
				"campaign_status": frappe.db.get_value("Marketing Campaign", campaign_id, "status"),
			},
		)

	return _ok(
		{
			"preflight": preflight,
			"fired": True,
			"runner_result": result,
			"campaign_status": frappe.db.get_value("Marketing Campaign", campaign_id, "status"),
		},
		f"Campaign '{campaign_id}' published. "
		f"{preflight['social_posts_will_fire']} social post(s) and "
		f"{preflight['ad_campaigns_will_fire']} ad campaign(s) fired.",
	)

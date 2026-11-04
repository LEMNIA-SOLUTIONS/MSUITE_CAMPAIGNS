"""
Post-hoc link / unlink between a Marketing Campaign and its channel
children (Social Post, Ad Campaign).

Mirrors the Bulk WhatsApp Message pattern in `composer.py`
(`link_whatsapp_blast` / `unlink_whatsapp_blast`): the child's own
`campaign` Link field is the single source of truth (1:N — re-linking
moves ownership), and the write goes through `frappe.db.set_value`,
deliberately bypassing the child's `validate()`.

Why bypass validate: linking an already-PUBLISHED post / launched ad
is historical attribution — `_validate_schedule_window` would reject
a published post whose slots fall outside the new parent's window,
which is exactly the case linking-after-the-fact exists for. For
children that still have a future (Draft / Scheduled / pre-publish),
the same window + parent-Active conditions are evaluated here and
returned as WARNINGS, not errors: the link always succeeds, the
marketer is told why it won't dispatch yet. The dispatch-time gates
(`dispatch_due_posts`, `launch_due_campaigns`,
`assert_parent_campaign_active`) remain the enforcement layer — a
linked child under a non-Active umbrella simply doesn't fire.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import get_datetime

from msuite_campaigns.campaign.services.relationships import register_with_campaign
from msuite_core.constants import CampaignStatus as SocialStatus

# Social Post statuses that still have un-published work ahead of them —
# these get the "will this actually dispatch?" warnings on link.
# (`msuite_core.constants` is app-level shared vocabulary, not a
# channel module, so importing it here keeps the campaign→channel
# dependency rule intact — see services/relationships.py.)
_SOCIAL_FUTURE_STATUSES = {
	SocialStatus.DRAFT,
	SocialStatus.SCHEDULED,
}

# Ad Campaign statuses that haven't gone live yet. Literal strings, not
# an import of `ads.core.constants` — campaign/api must not import
# channel modules by name (mirror of `CampaignStatus.PRE_PUBLISH`).
_AD_FUTURE_STATUSES = {"Draft", "Pending"}


# ── Social Post ─────────────────────────────────────────────────────────


@frappe.whitelist()
def link_social_post(campaign: str, social_post: str) -> dict:
	"""Link an existing Social Post to a Marketing Campaign.

	Always succeeds (given permissions + existence); returns
	`warnings` describing any condition that will keep the post from
	dispatching under the new parent (parent not Active, scheduled
	slots outside the parent window). Re-linking moves the post from
	its previous campaign — 1:N, same rule as WhatsApp blasts.
	"""
	_require_write("Marketing Campaign", campaign)
	_require_write("Social Post", social_post)

	warnings = _social_link_warnings(campaign, social_post)

	frappe.db.set_value("Social Post", social_post, "campaign", campaign, update_modified=True)
	frappe.db.commit()
	register_with_campaign("Social Post", social_post, campaign)
	return {
		"status": "success",
		"social_post": social_post,
		"campaign": campaign,
		"warnings": warnings,
	}


@frappe.whitelist()
def unlink_social_post(social_post: str) -> dict:
	"""Detach a Social Post from whichever Marketing Campaign it's
	linked to — it reverts to standalone (own schedule, no campaign
	gates)."""
	_require_write("Social Post", social_post)
	frappe.db.set_value("Social Post", social_post, "campaign", "", update_modified=True)
	frappe.db.commit()
	return {"status": "success", "social_post": social_post}


@frappe.whitelist()
def list_linkable_social_posts(campaign: str = "", search: str = "", limit: int = 50) -> dict:
	"""Picker feed for "Link existing Social Post". Same response shape
	as `composer.list_whatsapp_blasts`:

	  {
	    "current": [<name>, ...],   # posts already linked to this campaign
	    "rows": [{name, title, status, scheduled, linked_to}]
	  }

	`linked_to` names the OTHER campaign when a post is already owned
	elsewhere — the picker shows it as a "used by X" hint since
	re-linking moves ownership.
	"""
	if campaign:
		_require_read("Marketing Campaign", campaign)
	if not frappe.db.exists("DocType", "Social Post"):
		return {"current": [], "rows": []}

	filters: dict = {}
	if search:
		filters["title"] = ["like", f"%{search}%"]

	raw = frappe.get_all(
		"Social Post",
		filters=filters,
		fields=["name", "title", "status", "scheduled_at", "campaign"],
		order_by="modified desc",
		limit=int(limit or 50),
	)

	rows, current = [], []
	for r in raw:
		if campaign and r.get("campaign") == campaign:
			current.append(r["name"])
		rows.append(
			{
				"name": r["name"],
				"title": r.get("title") or "",
				"status": r.get("status") or SocialStatus.DRAFT,
				"scheduled": _iso(r.get("scheduled_at")),
				"linked_to": (r.get("campaign") or "") if r.get("campaign") != campaign else "",
			}
		)
	return {"current": current, "rows": rows}


def _social_link_warnings(campaign: str, social_post: str) -> list[str]:
	"""Advisory checks for a Social Post that still has a future.

	Published / Cancelled / Failed posts get no warnings — linking them
	is pure attribution and nothing will (or should) dispatch.
	"""
	post = frappe.db.get_value(
		"Social Post",
		social_post,
		["status", "scheduled_at", "title"],
		as_dict=True,
	)
	if not post or post.status not in _SOCIAL_FUTURE_STATUSES:
		return []

	warnings = []
	parent = (
		frappe.db.get_value(
			"Marketing Campaign",
			campaign,
			["campaign_name", "status", "start_date", "end_date"],
			as_dict=True,
		)
		or {}
	)

	if parent.get("status") != "Active":
		warnings.append(
			_("Campaign '{0}' is {1} — this post won't dispatch until the campaign is Active.").format(
				parent.get("campaign_name") or campaign, parent.get("status") or "Draft"
			)
		)

	warnings += _window_warnings(
		parent,
		campaign,
		slots=_social_slots(social_post, post.get("scheduled_at")),
	)
	return warnings


def _social_slots(social_post: str, campaign_level_slot) -> list:
	"""Every schedule slot the post carries: the campaign-level
	`scheduled_at` plus each destination row's override — the same set
	`_validate_schedule_window` enforces at save time."""
	slots = [campaign_level_slot] if campaign_level_slot else []
	overrides = frappe.get_all(
		"Social Post Destination",
		filters={"parent": social_post, "scheduled_at_override": ["is", "set"]},
		pluck="scheduled_at_override",
	)
	return slots + overrides


# ── Ad Campaign ─────────────────────────────────────────────────────────


@frappe.whitelist()
def link_ad_campaign(campaign: str, ad_campaign: str) -> dict:
	"""Link an existing Ad Campaign to a Marketing Campaign. Same
	contract as `link_social_post`: always succeeds, returns advisory
	`warnings` for pre-publish ads whose auto-launch the new parent
	would gate."""
	_require_write("Marketing Campaign", campaign)
	_require_write("Ad Campaign", ad_campaign)

	warnings = _ad_link_warnings(campaign, ad_campaign)

	frappe.db.set_value("Ad Campaign", ad_campaign, "campaign", campaign, update_modified=True)
	frappe.db.commit()
	register_with_campaign("Ad Campaign", ad_campaign, campaign)
	return {
		"status": "success",
		"ad_campaign": ad_campaign,
		"campaign": campaign,
		"warnings": warnings,
	}


@frappe.whitelist()
def unlink_ad_campaign(ad_campaign: str) -> dict:
	"""Detach an Ad Campaign from its Marketing Campaign — reverts to
	standalone (its own start_date drives auto-launch, no umbrella
	gate)."""
	_require_write("Ad Campaign", ad_campaign)
	frappe.db.set_value("Ad Campaign", ad_campaign, "campaign", "", update_modified=True)
	frappe.db.commit()
	return {"status": "success", "ad_campaign": ad_campaign}


@frappe.whitelist()
def list_linkable_ad_campaigns(campaign: str = "", search: str = "", limit: int = 50) -> dict:
	"""Picker feed for "Link existing Ad Campaign" — same shape as
	`list_linkable_social_posts`."""
	if campaign:
		_require_read("Marketing Campaign", campaign)
	if not frappe.db.exists("DocType", "Ad Campaign"):
		return {"current": [], "rows": []}

	filters: dict = {}
	if search:
		filters["campaign_name"] = ["like", f"%{search}%"]

	raw = frappe.get_all(
		"Ad Campaign",
		filters=filters,
		fields=["name", "campaign_name", "status", "start_date", "campaign"],
		order_by="modified desc",
		limit=int(limit or 50),
	)

	rows, current = [], []
	for r in raw:
		if campaign and r.get("campaign") == campaign:
			current.append(r["name"])
		rows.append(
			{
				"name": r["name"],
				"title": r.get("campaign_name") or "",
				"status": r.get("status") or "Draft",
				"scheduled": _iso(r.get("start_date")),
				"linked_to": (r.get("campaign") or "") if r.get("campaign") != campaign else "",
			}
		)
	return {"current": current, "rows": rows}


def _ad_link_warnings(campaign: str, ad_campaign: str) -> list[str]:
	"""Advisory checks for a pre-publish (Draft/Pending) Ad Campaign.

	Live / finished ads get no warnings — Meta is already running (or
	done with) them; the link is attribution only."""
	ad = frappe.db.get_value(
		"Ad Campaign",
		ad_campaign,
		["status", "start_date", "end_date"],
		as_dict=True,
	)
	if not ad or ad.status not in _AD_FUTURE_STATUSES:
		return []

	warnings = []
	parent = (
		frappe.db.get_value(
			"Marketing Campaign",
			campaign,
			["campaign_name", "status", "start_date", "end_date"],
			as_dict=True,
		)
		or {}
	)

	if parent.get("status") != "Active":
		warnings.append(
			_(
				"Campaign '{0}' is {1} — this ad campaign won't auto-launch until the campaign is Active."
			).format(parent.get("campaign_name") or campaign, parent.get("status") or "Draft")
		)

	slots = [s for s in (ad.get("start_date"), ad.get("end_date")) if s]
	warnings += _window_warnings(parent, campaign, slots=slots)
	return warnings


# ── Shared helpers ──────────────────────────────────────────────────────


def _window_warnings(parent: dict, campaign: str, slots: list) -> list[str]:
	"""One warning per schedule slot that falls outside the parent's
	start→end window. No window (either bound missing) → no warnings,
	matching `_validate_schedule_window`'s behaviour."""
	start, end = parent.get("start_date"), parent.get("end_date")
	if not (start and end and slots):
		return []
	start_dt, end_dt = get_datetime(start), get_datetime(end)
	label = parent.get("campaign_name") or campaign

	out = []
	for slot in slots:
		s = get_datetime(slot)
		if s < start_dt or s > end_dt:
			out.append(
				_(
					"Scheduled time {0} is outside the campaign '{1}' window "
					"({2} → {3}) — it won't dispatch unless rescheduled or the "
					"window is widened."
				).format(
					s.strftime("%Y-%m-%d %H:%M"),
					label,
					start_dt.strftime("%Y-%m-%d %H:%M"),
					end_dt.strftime("%Y-%m-%d %H:%M"),
				)
			)
	return out


def _require_write(doctype: str, name: str) -> None:
	_require(doctype, name, "write")


def _require_read(doctype: str, name: str) -> None:
	_require(doctype, name, "read")


def _require(doctype: str, name: str, perm_type: str) -> None:
	"""Existence first, then permission — `has_permission(doc=...)`
	loads the doc internally, so a missing name would raise
	DoesNotExistError instead of the clean 403/404 callers expect."""
	if not name or not frappe.db.exists(doctype, name):
		frappe.throw(_("{0} {1} not found.").format(doctype, name), frappe.DoesNotExistError)
	if not frappe.has_permission(doctype, perm_type, doc=name):
		frappe.throw(
			_("No {0} permission for {1} {2}").format(perm_type, doctype, name),
			frappe.PermissionError,
		)


def _iso(v) -> str:
	if not v:
		return ""
	return v.isoformat() if hasattr(v, "isoformat") else str(v)

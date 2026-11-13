"""
Campaign composer REST surface.

Thin wrapper around `frappe.client` and the existing `campaign.runner`
RPCs. The Vue SPA at `/campaign?name=<X>` reads through here so it
doesn't need to talk to the doctype directly. Mirrors the
`ads.api.v1.composer` shape so the two composers feel consistent on
the wire.

Endpoints:
  • get_campaign(name="")     — load full state + counts of children
  • save_campaign(name, payload) — patch the umbrella doctype
  • check_campaign_name(name, exclude) — is this campaign name already taken?
  • list_channels(campaign)   — Social Posts + Ad Campaigns + Bulk WA
                                rows linked to this Campaign, with status
"""

from __future__ import annotations

import json

import frappe
from frappe import _

from msuite_campaigns.campaign.doctype.marketing_campaign.marketing_campaign import campaign_name_exists

# ── Load + save ─────────────────────────────────────────────────────────


@frappe.whitelist()
def get_campaign(name: str = "") -> dict:
	"""Hydrated state for the composer. Returns a blank skeleton when
	`name` is empty so the new-campaign flow has the same shape as
	editing an existing one.
	"""
	if not name:
		return _blank_state()
	if not frappe.has_permission("Marketing Campaign", "read", doc=name):
		frappe.throw(_("No read permission for Marketing Campaign {0}").format(name), frappe.PermissionError)
	doc = frappe.get_doc("Marketing Campaign", name)
	return _serialize(doc)


@frappe.whitelist()
def save_campaign(name: str = "", payload: str = "") -> dict:
	"""Insert (name="") or patch an existing Campaign. The Vue composer
	sends only the fields it owns; everything else stays untouched.
	"""
	data = json.loads(payload or "{}")
	if not name:
		if not frappe.has_permission("Marketing Campaign", "create"):
			frappe.throw(_("No create permission for Marketing Campaign"), frappe.PermissionError)
		# New campaign — create as Draft by default.
		doc = frappe.new_doc("Marketing Campaign")
		doc.status = "Draft"
	else:
		if not frappe.has_permission("Marketing Campaign", "write", doc=name):
			frappe.throw(
				_("No write permission for Marketing Campaign {0}").format(name), frappe.PermissionError
			)
		doc = frappe.get_doc("Marketing Campaign", name)

	for fld in _EDITABLE_FIELDS:
		if fld in data:
			setattr(doc, fld, data[fld])

	if not name:
		doc.insert(ignore_permissions=True)
	else:
		doc.save(ignore_permissions=True)
	return _serialize(doc)


@frappe.whitelist()
def check_campaign_name(name: str = "", exclude: str = "") -> dict:
	"""Tell the composer whether a campaign name is already taken, so the
	drawer can warn before the user submits. Same trimmed, case-insensitive
	rule that `save_campaign` enforces, which stays the final guard.

	Args:
		name: Campaign name typed by the user.
		exclude: Optional Marketing Campaign id to ignore (the campaign being renamed).

	Returns:
		dict: `{"exists": bool}`; False for an empty or whitespace-only name.

	Raises:
		frappe.PermissionError: If the user cannot read Marketing Campaign.
	"""
	if not frappe.has_permission("Marketing Campaign", "read"):
		frappe.throw(_("No read permission for Marketing Campaign"), frappe.PermissionError)
	return {"exists": campaign_name_exists(name, exclude_name=exclude or None)}


# ── Children ────────────────────────────────────────────────────────────


@frappe.whitelist()
def list_channels(campaign: str = "") -> dict:
	"""Children linked to this Campaign, grouped by channel type.

	Returns:
	  {
	    "social":   [{name, title, status, platform_count, scheduled_at}],
	    "ads":      [{name, ad_campaign_name, status, platform, objective}],
	    "whatsapp": [{name, status, recipients_count, scheduled_at}]
	  }

	Used by the Channels tab card grid — clicking a row navigates to
	the relevant composer; clicking "+ New" creates one scoped to this
	Campaign via the existing `?parent_campaign=` query param.
	"""
	if not campaign:
		return {"social": [], "ads": [], "whatsapp": []}

	if not frappe.has_permission("Marketing Campaign", "read", doc=campaign):
		frappe.throw(
			_("No read permission for Marketing Campaign {0}").format(campaign), frappe.PermissionError
		)

	# Social Posts — one Social Post per platform push. The
	# user-visible label is the `title` field (Social Post's
	# `title_field`); we ship it as `title` and the SPA falls back to
	# `name` for unsaved rows.
	social = []
	if frappe.db.exists("DocType", "Social Post"):
		social = frappe.get_all(
			"Social Post",
			filters={"campaign": campaign},
			# `is_paused` powers the SPA's hasPausableChildren /
			# hasResumableChildren getters so the Pause / Resume buttons
			# surface based on real per-child state, not just Ad status.
			fields=["name", "title", "status", "scheduled_at", "is_paused"],
			order_by="creation desc",
			limit=50,
		)
		# Enrich each Social Post with the per-account post rollup so
		# the Campaign mission-control page can show counts + clickable
		# URLs without a per-row round-trip.
		social_names = [row["name"] for row in social]
		all_posts = []
		if social_names:
			all_posts = frappe.get_all(
				"Social Post Destination",
				filters={"parent": ["in", social_names], "parenttype": "Social Post"},
				fields=[
					"parent",
					"idx",
					"platform",
					"social_account",
					"status",
					"platform_post_id",
					"platform_post_url",
					"failure_reason",
				],
				order_by="idx asc",
			)

		account_names = list({p.social_account for p in all_posts if p.social_account})
		handle_by_name = {}
		if account_names:
			handle_by_name = {
				r.name: (r.account_name or r.name)
				for r in frappe.get_all(
					"Social Account",
					filters={"name": ["in", account_names]},
					fields=["name", "account_name"],
				)
			}

		posts_by_campaign = {}
		for p in all_posts:
			parent = p.parent
			if parent not in posts_by_campaign:
				posts_by_campaign[parent] = []
			posts_by_campaign[parent].append(p)

		for row in social:
			posts = posts_by_campaign.get(row["name"], [])
			row["platform_count"] = len(posts)
			row["post_summary"] = {
				"total": len(posts),
				"published": sum(1 for p in posts if p.status == "Published"),
				"scheduled": sum(1 for p in posts if p.status == "Scheduled"),
				"publishing": sum(1 for p in posts if p.status == "Publishing"),
				"failed": sum(1 for p in posts if p.status == "Failed"),
				"draft": sum(1 for p in posts if p.status == "Draft"),
			}
			row["posts"] = [
				{
					"idx": p.idx,
					"platform": p.platform,
					"account": p.social_account,
					"account_label": handle_by_name.get(p.social_account, p.social_account),
					"status": p.status,
					"platform_post_id": p.platform_post_id or "",
					"platform_post_url": p.platform_post_url or "",
					"failure_reason": (p.failure_reason or "")[:200],
				}
				for p in posts
			]

	# Ad Campaigns — one per Meta/Google launch.
	ads = []
	if frappe.db.exists("DocType", "Ad Campaign"):
		ads = frappe.get_all(
			"Ad Campaign",
			filters={"campaign": campaign},
			fields=["name", "campaign_name", "status", "platform", "objective", "platform_campaign_id"],
			order_by="creation desc",
			limit=50,
		)

	# WhatsApp Broadcasts — 1:N via `Bulk WhatsApp Message.campaign`
	# (replaces the old single `Marketing Campaign.whatsapp_blast`
	# link; see patches.add_campaign_link_to_bulk_whatsapp_message).
	# Schedule column name drifts across app variants (current
	# `scheduled_time` vs legacy `scheduled_at`), so resolve it from
	# meta rather than hard-coding — otherwise the whole channels list
	# 500s on installs that don't have the column we picked.
	whatsapp = []
	if frappe.db.exists("DocType", "Bulk WhatsApp Message"):
		wa_meta = frappe.get_meta("Bulk WhatsApp Message")
		schedule_field = (
			"scheduled_time"
			if wa_meta.has_field("scheduled_time")
			else ("scheduled_at" if wa_meta.has_field("scheduled_at") else None)
		)
		wa_fields = ["name", "status", "recipient_count"]
		if wa_meta.has_field("title"):
			wa_fields.append("title")
		if schedule_field:
			wa_fields.append(schedule_field)
		if wa_meta.has_field("is_paused"):
			wa_fields.append("is_paused")

		blasts = frappe.get_all(
			"Bulk WhatsApp Message",
			filters={"campaign": campaign},
			fields=wa_fields,
			order_by="creation desc",
			limit=50,
		)
		whatsapp = [
			{
				"name": wa.get("name"),
				"title": wa.get("title") or "",
				"status": wa.get("status") or "Draft",
				# SPA reads `scheduled_at`; normalise here so the
				# frontend never sees the schema drift.
				"scheduled_at": _iso(wa.get(schedule_field)) if schedule_field else "",
				"recipients_count": wa.get("recipient_count") or 0,
				"is_paused": int(wa.get("is_paused") or 0),
			}
			for wa in blasts
		]

	return {"social": social, "ads": ads, "whatsapp": whatsapp}


@frappe.whitelist()
def list_whatsapp_blasts(campaign: str = "", search: str = "", limit: int = 50) -> dict:
	"""List Bulk WhatsApp Message rows the marketer can link to this
	Campaign — a Campaign can now have MANY broadcasts (1:N via
	`Bulk WhatsApp Message.campaign`), replacing the old single-slot
	`Marketing Campaign.whatsapp_blast` link.

	Why a picker instead of an inline composer? Bulk WhatsApp Message
	has a heavy creation flow (recipient list, template variables,
	schedule) that's already polished in the Desk form. The Campaign
	Mission Control just needs to LINK an existing blast — creation
	stays in Desk.

	Returns:
	  {
	    "current":  [<name>, ...],         # blasts already linked to this Campaign
	    "rows": [
	      {name, title, status, recipient_count, scheduled, linked_to}
	    ]
	  }

	`linked_to` is the *other* Campaign's name when a blast is already
	linked elsewhere — surfaces in the picker as a "used by X" hint so
	the marketer doesn't accidentally steal it.
	"""
	if campaign:
		if not frappe.has_permission("Marketing Campaign", "read", doc=campaign):
			frappe.throw(
				_("No read permission for Marketing Campaign {0}").format(campaign), frappe.PermissionError
			)

	if not frappe.db.exists("DocType", "Bulk WhatsApp Message"):
		return {"current": [], "rows": []}

	meta = frappe.get_meta("Bulk WhatsApp Message")

	# Some app variants name the schedule field `scheduled_time` (current),
	# others `scheduled_at` (legacy value). Pick whichever exists.
	schedule_field = (
		"scheduled_time"
		if meta.has_field("scheduled_time")
		else ("scheduled_at" if meta.has_field("scheduled_at") else None)
	)
	has_recipient_count = meta.has_field("recipient_count")

	fields = ["name", "title", "status", "modified", "campaign"]
	if has_recipient_count:
		fields.append("recipient_count")
	if schedule_field:
		fields.append(schedule_field)

	filters: dict = {}
	if search:
		# Match on title OR name — marketers usually remember the title
		# ("Summer launch blast"), not the auto-id.
		filters = {"title": ["like", f"%{search}%"]}

	raw = frappe.get_all(
		"Bulk WhatsApp Message",
		filters=filters,
		fields=fields,
		order_by="modified desc",
		limit=int(limit or 50),
	)

	rows = []
	current = []
	for r in raw:
		if campaign and r.get("campaign") == campaign:
			current.append(r.get("name"))
		rows.append(
			{
				"name": r.get("name") or "",
				"title": r.get("title") or "",
				"status": r.get("status") or "Draft",
				"recipient_count": r.get("recipient_count") or 0,
				"scheduled": _iso(r.get(schedule_field)) if schedule_field else "",
				# `linked_to` is the *other* campaign's name when this blast
				# is already linked elsewhere. Self-link is empty — the SPA
				# marks it via `current` instead.
				"linked_to": (r.get("campaign") or "") if r.get("campaign") != campaign else "",
			}
		)

	return {"current": current, "rows": rows}


@frappe.whitelist()
def link_whatsapp_blast(campaign: str, blast: str) -> dict:
	"""Link an existing Bulk WhatsApp Message to this Campaign — sets
	the broadcast's own `campaign` field (the real, 1:N relationship).
	A blast can only belong to one Campaign at a time; re-linking moves
	it.
	"""
	if not frappe.has_permission("Marketing Campaign", "write", doc=campaign):
		frappe.throw(
			_("No write permission for Marketing Campaign {0}").format(campaign), frappe.PermissionError
		)
	if not frappe.has_permission("Bulk WhatsApp Message", "write", doc=blast):
		frappe.throw(
			_("No write permission for Bulk WhatsApp Message {0}").format(blast), frappe.PermissionError
		)

	frappe.db.set_value("Bulk WhatsApp Message", blast, "campaign", campaign, update_modified=True)
	frappe.db.commit()
	return {"status": "success", "blast": blast, "campaign": campaign}


@frappe.whitelist()
def unlink_whatsapp_blast(blast: str) -> dict:
	"""Detach a Bulk WhatsApp Message from whichever Campaign it's
	currently linked to."""
	if not frappe.has_permission("Bulk WhatsApp Message", "write", doc=blast):
		frappe.throw(
			_("No write permission for Bulk WhatsApp Message {0}").format(blast), frappe.PermissionError
		)

	frappe.db.set_value("Bulk WhatsApp Message", blast, "campaign", "", update_modified=True)
	frappe.db.commit()
	return {"status": "success", "blast": blast}


def _iso(v) -> str:
	"""Datetime → ISO string, blank when unset."""
	if not v:
		return ""
	if hasattr(v, "isoformat"):
		return v.isoformat()
	return str(v)


# ── Helpers ─────────────────────────────────────────────────────────────


# Fields the composer is allowed to write. Mirrors the Overview-tab
# field order in `campaign.json` so we don't accidentally expose
# read-only computed fields (spent, next_run_at, parent_recurring).
_EDITABLE_FIELDS = (
	"campaign_name",
	"objective",
	"goal",
	# `status` flips via the Hero's Activate / Cancel buttons —
	# accepted here so the composer doesn't bounce to Desk just for
	# a single-field state change.
	"status",
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
	# `whatsapp_blast` deliberately excluded — it's the deprecated
	# single-Link field (read-only on the doctype now). WhatsApp
	# broadcasts are linked via `link_whatsapp_blast` /
	# `unlink_whatsapp_blast`, which set `Bulk WhatsApp Message
	# .campaign` directly (1:N).
)


def _blank_state() -> dict:
	"""Defaults for a never-saved Campaign — matches `Campaign` defaults
	so the Vue form binds cleanly on mount."""
	return {
		"name": "",
		"campaign_name": "",
		"objective": "",
		"goal": "",
		"status": "Draft",
		"start_date": "",
		"end_date": "",
		"budget": 0,
		"spent": 0,
		"utm_source": "",
		"utm_medium": "social",
		"utm_campaign": "",
		"utm_term": "",
		"first_comment_template": "",
		"recurrence_pattern": "",
		"recurrence_interval": 1,
		"recurrence_end_at": "",
		"next_run_at": "",
		"parent_recurring_campaign": "",
		"whatsapp_blast": "",
	}


def _serialize(doc) -> dict:
	"""Doc → plain dict shape the SPA reads. Coerces dates to ISO so
	the Vue datetime-local inputs round-trip cleanly."""
	out = {fld: doc.get(fld) for fld in _EDITABLE_FIELDS}
	out["name"] = doc.name
	out["status"] = doc.status
	out["spent"] = doc.spent or 0
	out["next_run_at"] = doc.next_run_at.isoformat() if doc.next_run_at else ""
	out["parent_recurring_campaign"] = doc.parent_recurring_campaign or ""
	# Legacy, read-only display value — not in _EDITABLE_FIELDS (see
	# comment there), but kept in the response shape so it matches
	# `_blank_state()` and any SPA code still reading it defensively.
	out["whatsapp_blast"] = doc.whatsapp_blast or ""
	# Stringify any date / datetime values
	for k in ("start_date", "end_date", "recurrence_end_at"):
		v = out.get(k)
		if hasattr(v, "isoformat"):
			out[k] = v.isoformat()
		elif v is None:
			out[k] = ""
	return out


@frappe.whitelist()
def duplicate_campaign(name: str, duplicate_children: int = 0) -> dict:
	"""Duplicate a Marketing Campaign.
	If duplicate_children is 1, also duplicates associated child campaigns (Social & Ads & WhatsApp blasts).
	"""
	if not frappe.has_permission("Marketing Campaign", "read", doc=name):
		frappe.throw(_("You do not have permission to duplicate this campaign."), frappe.PermissionError)
	if not frappe.has_permission("Marketing Campaign", "create"):
		frappe.throw(_("You do not have permission to create campaigns."), frappe.PermissionError)

	# 1. Copy the Marketing Campaign doc
	from msuite_campaigns.campaign.doctype.marketing_campaign.marketing_campaign import (
		make_unique_campaign_name,
	)

	old_mc = frappe.get_doc("Marketing Campaign", name)
	new_mc = frappe.copy_doc(old_mc)
	new_mc.campaign_name = make_unique_campaign_name(f"{old_mc.campaign_name} (Copy)")
	new_mc.status = "Draft"
	new_mc.spent = 0
	new_mc.next_run_at = None
	new_mc.parent_recurring_campaign = ""
	new_mc.whatsapp_blast = ""  # reset WhatsApp blast link first, we will set it if we duplicate it
	new_mc.insert(ignore_permissions=True)

	# 2. Duplicate child records if requested
	if int(duplicate_children):
		# A. Duplicate Social Posts
		if frappe.db.exists("DocType", "Social Post"):
			social_posts = frappe.get_all("Social Post", filters={"campaign": name}, pluck="name")
			for sc_name in social_posts:
				old_sc = frappe.get_doc("Social Post", sc_name)
				new_sc = frappe.copy_doc(old_sc)
				new_sc.campaign = new_mc.name
				new_sc.status = "Draft"
				# Reset post fields
				for p in getattr(new_sc, "posts", []):
					p.status = "Draft"
					p.platform_post_id = ""
					p.platform_post_url = ""
					p.failure_reason = ""
				new_sc.insert(ignore_permissions=True)

		# Helper to deep-clone configuration docs so duplicate does not share mutable config with original
		def _clone_config_link(doc, fieldname: str, doctype: str) -> None:
			old_name = getattr(doc, fieldname, None)
			if old_name and frappe.db.exists(doctype, old_name):
				old_cfg = frappe.get_doc(doctype, old_name)
				new_cfg = frappe.copy_doc(old_cfg)
				new_cfg.insert(ignore_permissions=True)
				setattr(doc, fieldname, new_cfg.name)

		# B. Duplicate Ad Campaigns
		if frappe.db.exists("DocType", "Ad Campaign"):
			ad_campaigns = frappe.get_all("Ad Campaign", filters={"campaign": name}, pluck="name")
			for ac_name in ad_campaigns:
				old_ac = frappe.get_doc("Ad Campaign", ac_name)
				new_ac = frappe.copy_doc(old_ac)
				new_ac.campaign = new_mc.name
				new_ac.platform_campaign_id = ""
				new_ac.effective_status = ""
				new_ac.last_published_at = None
				new_ac.last_synced_at = None
				new_ac.error_message = ""
				new_ac.status = "Draft"
				new_ac.launch_on_run = 0
				_clone_config_link(new_ac, "budget_configuration", "Budget Configuration")
				_clone_config_link(new_ac, "conversion_configuration", "Conversion Configuration")
				new_ac.insert(ignore_permissions=True)

				# Duplicate associated Ad Sets and Ads
				ad_sets = frappe.get_all("Ad Set", filters={"ad_campaign": ac_name}, fields=["name"])
				for old_as_meta in ad_sets:
					old_as = frappe.get_doc("Ad Set", old_as_meta.name)
					new_as = frappe.copy_doc(old_as)
					new_as.ad_campaign = new_ac.name
					_clone_config_link(new_as, "targeting_configuration", "Targeting Configuration")
					_clone_config_link(new_as, "budget_configuration", "Budget Configuration")
					_clone_config_link(new_as, "conversion_configuration", "Conversion Configuration")
					_clone_config_link(new_as, "tracking_configuration", "Tracking Configuration")
					new_as.insert(ignore_permissions=True)

					# Duplicate Ads under this Ad Set
					ads = frappe.get_all("Ad", filters={"ad_set": old_as.name}, fields=["name"])
					for old_ad_meta in ads:
						old_ad = frappe.get_doc("Ad", old_ad_meta.name)
						new_ad = frappe.copy_doc(old_ad)
						new_ad.ad_campaign = new_ac.name
						new_ad.ad_set = new_as.name
						_clone_config_link(new_ad, "tracking_configuration", "Tracking Configuration")
						new_ad.insert(ignore_permissions=True)

		# C. Duplicate every Bulk WhatsApp Message under the campaign
		# (1:N via Bulk WhatsApp Message.campaign).
		if frappe.db.exists("DocType", "Bulk WhatsApp Message"):
			old_blasts = frappe.get_all("Bulk WhatsApp Message", filters={"campaign": name}, pluck="name")
			for old_blast_name in old_blasts:
				old_wa = frappe.get_doc("Bulk WhatsApp Message", old_blast_name)
				new_wa = frappe.copy_doc(old_wa)
				new_wa.campaign = new_mc.name
				new_wa.status = "Draft"
				new_wa.sent_count = 0
				new_wa.is_paused = 0
				new_wa.scheduled_time = None
				new_wa.insert(ignore_permissions=True)

		# D. Duplicate Email Campaigns
		if frappe.db.exists("DocType", "Email Campaign"):
			email_campaigns = frappe.get_all("Email Campaign", filters={"campaign_name": name}, pluck="name")
			for ec_name in email_campaigns:
				old_ec = frappe.get_doc("Email Campaign", ec_name)
				new_ec = frappe.copy_doc(old_ec)
				new_ec.campaign_name = new_mc.name
				new_ec.status = "Draft"
				new_ec.insert(ignore_permissions=True)

	frappe.db.commit()
	return _serialize(new_mc)


@frappe.whitelist()
def delete_campaign(name: str, cascade: int = 0) -> dict:
	"""Delete a Marketing Campaign. If cascade is 0 and child records exist,
	we warn the user. If cascade is 1, we perform cascade delete.
	"""
	if not frappe.has_permission("Marketing Campaign", "delete", doc=name):
		frappe.throw(_("You do not have permission to delete this campaign."), frappe.PermissionError)

	counts = _get_relationship_counts(name)
	has_children = any(v > 0 for v in counts.values())

	if has_children and not int(cascade):
		return {"status": "warning", "message": "This campaign contains child assets.", "counts": counts}

	# Cascade delete
	if int(cascade):
		# Delete Social Posts
		if frappe.db.exists("DocType", "Social Post"):
			social_posts = frappe.get_all("Social Post", filters={"campaign": name}, pluck="name")
			for sc in social_posts:
				frappe.delete_doc("Social Post", sc, ignore_permissions=True, force=True)

		# Delete Ad Campaigns (this invokes on_trash which deletes all child Ad assets!)
		if frappe.db.exists("DocType", "Ad Campaign"):
			ad_campaigns = frappe.get_all("Ad Campaign", filters={"campaign": name}, pluck="name")
			for ac in ad_campaigns:
				frappe.delete_doc("Ad Campaign", ac, ignore_permissions=True, force=True)

		# Delete every Bulk WhatsApp Message under the campaign (1:N)
		if frappe.db.exists("DocType", "Bulk WhatsApp Message"):
			blasts = frappe.get_all("Bulk WhatsApp Message", filters={"campaign": name}, pluck="name")
			for blast in blasts:
				frappe.delete_doc("Bulk WhatsApp Message", blast, ignore_permissions=True, force=True)

		# Delete Email Campaigns
		if frappe.db.exists("DocType", "Email Campaign"):
			email_campaigns = frappe.get_all("Email Campaign", filters={"campaign_name": name}, pluck="name")
			for ec in email_campaigns:
				frappe.delete_doc("Email Campaign", ec, ignore_permissions=True, force=True)

	# Delete parent campaign itself
	frappe.delete_doc("Marketing Campaign", name, ignore_permissions=True, force=True)
	frappe.db.commit()

	return {"status": "success", "message": "Campaign deleted successfully."}


def _get_relationship_counts(campaign_name: str) -> dict:
	social_posts = 0
	ad_campaigns = 0
	email_campaigns = 0
	whatsapp_blasts = 0

	if frappe.db.exists("DocType", "Social Post"):
		posts = frappe.get_all("Social Post", filters={"campaign": campaign_name}, pluck="name")
		if posts:
			social_posts = frappe.db.count(
				"Social Post", {"parent": ["in", posts], "parenttype": "Social Post"}
			)

	if frappe.db.exists("DocType", "Ad Campaign"):
		ad_campaigns = frappe.db.count("Ad Campaign", {"campaign": campaign_name})

	if frappe.db.exists("DocType", "Email Campaign"):
		email_campaigns = frappe.db.count("Email Campaign", {"campaign_name": campaign_name})

	if frappe.db.exists("DocType", "Bulk WhatsApp Message"):
		whatsapp_blasts = frappe.db.count("Bulk WhatsApp Message", {"campaign": campaign_name})

	return {
		"social_posts": social_posts,
		"ad_campaigns": ad_campaigns,
		"email_campaigns": email_campaigns,
		"whatsapp_blasts": whatsapp_blasts,
	}


@frappe.whitelist()
def list_marketing_campaigns(
	filters: str = "[]", fields: str = "[]", order_by: str = "modified desc", limit: int = 100, start: int = 0
) -> list:
	if not frappe.has_permission("Marketing Campaign", "read"):
		frappe.throw(_("No read permission for Marketing Campaign"), frappe.PermissionError)

	parsed_filters = json.loads(filters or "[]")
	parsed_fields = json.loads(fields or "[]")
	if not parsed_fields:
		parsed_fields = [
			"name",
			"campaign_name",
			"status",
			"objective",
			"start_date",
			"end_date",
			"budget",
			"spent",
			"modified",
		]

	return frappe.get_all(
		"Marketing Campaign",
		filters=parsed_filters,
		fields=parsed_fields,
		order_by=order_by,
		limit=limit,
		start=start,
	)


@frappe.whitelist()
def count_marketing_campaigns(filters: str = "[]") -> dict:
	"""Status-tab counts for the List page — one query, grouped by status,
	plus the unfiltered total. Kept separate from `list_marketing_campaigns`
	so adding pagination there didn't need a return-shape change (still a
	plain list, not `{rows, total}`) — existing callers keep working.
	"""
	if not frappe.has_permission("Marketing Campaign", "read"):
		frappe.throw(_("No read permission for Marketing Campaign"), frappe.PermissionError)

	parsed_filters = json.loads(filters or "[]")
	# Raw SQL function strings (e.g. "count(name) as count") aren't
	# permitted in `fields` on this Frappe version — fetch just the
	# status column and count in Python instead.
	rows = frappe.get_all("Marketing Campaign", filters=parsed_filters, fields=["status"])
	counts: dict[str, int] = {}
	for row in rows:
		counts[row["status"]] = counts.get(row["status"], 0) + 1
	return {"total": len(rows), "by_status": counts}


@frappe.whitelist()
def update_campaign_status(name: str, status: str) -> dict:
	"""Generic status-editor RPC behind the form's Hero buttons — a
	bare status flip, deliberately with no channel fan-out (use
	`campaign.api.runner.run_campaign`/`pause_campaign`/etc. for that).

	Still emits the matching lifecycle event (Phase 4 / lifecycle
	standardization) via `lifecycle.emit_for_status_change` — this RPC
	used to be a silent bypass of the event architecture entirely.
	"""
	if not frappe.has_permission("Marketing Campaign", "write", doc=name):
		frappe.throw(_("No write permission for Marketing Campaign {0}").format(name), frappe.PermissionError)

	from msuite_campaigns.campaign.services.lifecycle import (
		emit_for_status_change,
		validate_status_transition,
	)

	doc = frappe.get_doc("Marketing Campaign", name)
	old_status = doc.status
	validate_status_transition(old_status, status)
	doc.status = status
	doc.save()
	frappe.db.commit()
	emit_for_status_change(doc.name, old_status, doc.status)
	return doc.as_dict()


CAMPAIGN_ACTIVITY_LIMIT = 8


@frappe.whitelist()
def get_campaign_activity(campaign: str) -> list:
	"""Fetch recent audit log (Version rows) for a Marketing Campaign.

	Includes both current 'Marketing Campaign' and legacy 'Campaign'
	ref_doctype rows. Requires read permission on Marketing Campaign.

	:param campaign: Name of the Marketing Campaign.
	:return: List of dicts with fields name, owner, creation, data.
	"""
	if not frappe.db.exists("Marketing Campaign", campaign):
		if not frappe.has_permission("Marketing Campaign", "read"):
			frappe.throw(
				_("No read permission for Marketing Campaign {0}").format(campaign), frappe.PermissionError
			)
		return []

	if not frappe.has_permission("Marketing Campaign", "read", doc=campaign):
		frappe.throw(
			_("No read permission for Marketing Campaign {0}").format(campaign), frappe.PermissionError
		)

	return frappe.get_all(
		"Version",
		filters={"ref_doctype": ["in", ["Campaign", "Marketing Campaign"]], "docname": campaign},
		fields=["name", "owner", "creation", "data"],
		order_by="creation desc",
		limit=CAMPAIGN_ACTIVITY_LIMIT,
	)


@frappe.whitelist()
def bulk_delete_campaigns(names: str, cascade: int = 0) -> list[dict]:
	import json

	campaign_names = json.loads(names)
	results = []
	for name in campaign_names:
		try:
			if not frappe.has_permission("Marketing Campaign", "delete", doc=name):
				results.append(
					{
						"name": name,
						"deleted": False,
						"failed": True,
						"reason": _("You do not have permission to delete this campaign."),
					}
				)
				continue

			counts = _get_relationship_counts(name)
			has_children = any(v > 0 for v in counts.values())
			if has_children and not int(cascade):
				linked_types = []
				if counts.get("social_posts"):
					linked_types.append(_("Social Posts"))
				if counts.get("ad_campaigns"):
					linked_types.append(_("Ads"))
				if counts.get("whatsapp_blasts"):
					linked_types.append(_("WhatsApp Blasts"))
				if counts.get("email_campaigns"):
					linked_types.append(_("Email Campaigns"))

				if len(linked_types) > 1:
					linked_str = ", ".join(linked_types[:-1]) + _(" or ") + linked_types[-1]
				elif len(linked_types) == 1:
					linked_str = linked_types[0]
				else:
					linked_str = _("linked records")

				results.append(
					{
						"name": name,
						"deleted": False,
						"failed": True,
						"reason": _("contains linked {0}.").format(linked_str),
					}
				)
				continue

			# Perform cascade delete if cascade=1
			if int(cascade):
				if frappe.db.exists("DocType", "Social Post"):
					social_posts = frappe.get_all("Social Post", filters={"campaign": name}, pluck="name")
					for sc in social_posts:
						frappe.delete_doc("Social Post", sc, ignore_permissions=True, force=True)

				if frappe.db.exists("DocType", "Ad Campaign"):
					ad_campaigns = frappe.get_all("Ad Campaign", filters={"campaign": name}, pluck="name")
					for ac in ad_campaigns:
						frappe.delete_doc("Ad Campaign", ac, ignore_permissions=True, force=True)

				if frappe.db.exists("DocType", "Bulk WhatsApp Message"):
					blasts = frappe.get_all("Bulk WhatsApp Message", filters={"campaign": name}, pluck="name")
					for blast in blasts:
						frappe.delete_doc("Bulk WhatsApp Message", blast, ignore_permissions=True, force=True)

				if frappe.db.exists("DocType", "Email Campaign"):
					email_campaigns = frappe.get_all(
						"Email Campaign", filters={"campaign_name": name}, pluck="name"
					)
					for ec in email_campaigns:
						frappe.delete_doc("Email Campaign", ec, ignore_permissions=True, force=True)

			frappe.delete_doc("Marketing Campaign", name, ignore_permissions=True, force=True)
			frappe.db.commit()

			results.append({"name": name, "deleted": True, "failed": False, "reason": ""})
		except Exception as e:
			frappe.db.rollback()
			results.append({"name": name, "deleted": False, "failed": True, "reason": str(e)})

	return results


@frappe.whitelist()
def bulk_update_campaign_status(names: str, status: str) -> list[dict]:
	"""Same bare-status-flip contract as `update_campaign_status`, looped
	over multiple campaigns — see that function's docstring for why
	`emit_for_status_change` is called per row (Phase 4 / lifecycle
	standardization)."""
	import json

	from msuite_campaigns.campaign.services.lifecycle import (
		emit_for_status_change,
		validate_status_transition,
	)

	campaign_names = json.loads(names)
	results = []
	for name in campaign_names:
		try:
			if not frappe.has_permission("Marketing Campaign", "write", doc=name):
				results.append({"name": name, "success": False, "reason": _("No write permission.")})
				continue
			doc = frappe.get_doc("Marketing Campaign", name)
			old_status = doc.status
			validate_status_transition(old_status, status)
			doc.status = status
			doc.save(ignore_permissions=True)
			emit_for_status_change(doc.name, old_status, doc.status)
			results.append({"name": name, "success": True, "reason": ""})
		except Exception as e:
			results.append({"name": name, "success": False, "reason": str(e)})
	frappe.db.commit()
	return results


@frappe.whitelist()
def bulk_duplicate_campaigns(names: str, duplicate_children: int = 0) -> list[dict]:
	# perm: delegated to duplicate_campaign
	import json

	campaign_names = json.loads(names)
	results = []
	for name in campaign_names:
		try:
			res = duplicate_campaign(name, duplicate_children)
			results.append({"name": name, "success": True, "new_name": res.get("name"), "reason": ""})
		except Exception as e:
			results.append({"name": name, "success": False, "new_name": "", "reason": str(e)})
	return results


@frappe.whitelist()
def bulk_pause_campaigns(names: str) -> list[dict]:
	# perm: delegated to runner.pause_campaign
	import json

	from msuite_campaigns.campaign.api.runner import pause_campaign

	campaign_names = json.loads(names)
	results = []
	for name in campaign_names:
		try:
			res = pause_campaign(name)
			results.append({"name": name, "success": True, "result": res, "reason": ""})
		except Exception as e:
			results.append({"name": name, "success": False, "result": {}, "reason": str(e)})
	return results


@frappe.whitelist()
def bulk_resume_campaigns(names: str) -> list[dict]:
	# perm: delegated to runner.resume_campaign
	import json

	from msuite_campaigns.campaign.api.runner import resume_campaign

	campaign_names = json.loads(names)
	results = []
	for name in campaign_names:
		try:
			res = resume_campaign(name)
			results.append({"name": name, "success": True, "result": res, "reason": ""})
		except Exception as e:
			results.append({"name": name, "success": False, "result": {}, "reason": str(e)})
	return results

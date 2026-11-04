"""
Bulk CSV import — creates ONE Social Post with N posts under a
parent Campaign, from a row-per-post CSV.

This is campaign-shaped on purpose: a marketer planning 20 posts for
"Summer Sale" wants them under one Social Post so per-account
caption overrides, shared media, and scheduling stay coherent.
Importing 20 standalone Social Posts would scatter the work.

Accepted CSV columns (first row is header, case-insensitive):

  account       — Social Account name OR handle. REQUIRED.
  caption       — post caption. Falls back to the campaign's shared
                  caption if blank. OPTIONAL.
  scheduled_at  — ISO datetime; sets the per-post override. Blank →
                  publishes at the campaign's main scheduled_at.
                  OPTIONAL.
  media_url     — file URL (already uploaded to Frappe). OPTIONAL.
  post_type     — Text / Image / Video / etc. Falls back to platform
                  default. OPTIONAL.

Unknown columns are tolerated and ignored so marketers can paste
spreadsheet exports without scrubbing them.

Returns a per-row outcome list — marketer sees exactly which rows
succeeded and which were rejected (with reason). No partial-success
silent dropping.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Iterable

import frappe
from frappe import _
from frappe.utils import get_datetime

from msuite_core.constants import CampaignStatus, SocialPostStatus

_DEFAULT_POST_TYPE = {
	"Facebook": "Text",
	"Instagram": "Image",
	"LinkedIn": "Text",
	"X": "Text",
	"TikTok": "Video",
	"YouTube": "Video",
}


@frappe.whitelist()
def import_csv_to_campaign(
	parent_campaign: str,
	social_post_title: str,
	file_url: str,
	shared_caption: str = "",
) -> dict:
	"""Read a CSV from `file_url`, create one Social Post under
	`parent_campaign`, append one post per row.

	Args:
	    parent_campaign:        Top-level Campaign name to link under.
	    social_post_title:  Title for the new Social Post.
	    file_url:               Frappe file path of the uploaded CSV.
	    shared_caption:         Default caption for posts that leave
	                            their `caption` cell blank.
	"""
	# Plan gate first — `social_bulk_posts` separates Business+ users
	# (who can mass-import) from everyone else. Refuse before we even
	# validate the args so a Trial user's accidental click doesn't get
	# past the door.
	from msuite_core.services.plans.plan_enforcer import check_feature_or_throw

	check_feature_or_throw("social_bulk_posts", "Bulk CSV Upload")

	if not (parent_campaign and social_post_title and file_url):
		frappe.throw(_("parent_campaign, social_post_title and file_url are all required"))

	if not frappe.db.exists("Marketing Campaign", parent_campaign):
		frappe.throw(_(f"Campaign {parent_campaign} not found"))
	if not frappe.has_permission("Marketing Campaign", "write", doc=parent_campaign):
		frappe.throw(
			_("No permission to modify campaign {0}").format(parent_campaign), frappe.PermissionError
		)

	rows = list(_read_csv(file_url))
	if not rows:
		frappe.throw(_("CSV is empty (no data rows after the header)"))

	sc = _create_social_post(parent_campaign, social_post_title, shared_caption)

	# Build one Destination per account + per-row override dicts. The
	# per-row overrides are stamped onto those rows AFTER save.
	overrides: dict[str, dict] = {}
	outcomes = []
	for row_idx, row in enumerate(rows, start=2):  # row 1 was header
		outcome = _resolve_row(sc, row, row_idx, overrides)
		outcomes.append(outcome)

	# The parent Social Post carries `post_type` now. CSV rows can
	# specify per-row `post_type` legacy-style; we pick the most common
	# across the rows and apply it to the parent (matches the migration
	# patch's backfill heuristic).
	if overrides:
		chosen = _majority_post_type(overrides)
		if chosen:
			sc.post_type = chosen
	sc.save(ignore_permissions=True)

	# Apply per-row overrides AFTER save — caption / media / schedule.
	_apply_row_overrides(sc.name, outcomes, overrides)
	frappe.db.commit()

	return {
		"social_post": sc.name,
		"rows": len(rows),
		"added": sum(1 for o in outcomes if o["status"] == "added"),
		"skipped": sum(1 for o in outcomes if o["status"] == "skipped"),
		"outcomes": outcomes,
	}


def _apply_row_overrides(
	sc_name: str,
	outcomes: list[dict],
	overrides: dict[str, dict],
) -> None:
	"""Stamp per-row caption / media / schedule onto the matching Social
	Post rows after the controller's validate hook has built them.
	Keyed by account so a marketer staggering posts gets exactly the
	times they put in the CSV.
	"""
	schedule_by_account = {
		o["account"]: o["scheduled_at_override"]
		for o in outcomes
		if o.get("status") == "added" and o.get("scheduled_at_override")
	}

	posts = frappe.get_all(
		"Social Post",
		filters={"parent": sc_name, "parenttype": "Social Post"},
		fields=["name", "social_account"],
	)
	for p in posts:
		ov = overrides.get(p.social_account) or {}
		patch: dict[str, object] = {}
		if ov.get("caption_override"):
			patch["caption_override"] = ov["caption_override"]
		if ov.get("media_override"):
			patch["media_override"] = ov["media_override"]
		slot = schedule_by_account.get(p.social_account)
		if slot:
			patch["scheduled_at_override"] = slot
		if patch:
			for field, value in patch.items():
				frappe.db.set_value(
					"Social Post",
					p.name,
					field,
					value,
					update_modified=False,
				)


@frappe.whitelist()
def sample_csv() -> str:
	# perm: static template download
	"""Return a small sample CSV the marketer can download from the
	dialog as a starting template."""
	return (
		"account,caption,scheduled_at,media_url,post_type\n"
		"@yourbrand,Big launch tomorrow!,2026-05-25 09:00:00,,Text\n"
		"yourbrand-ig,Sneak peek 👀,,/files/teaser.jpg,Image\n"
		"u4c8fe15v2,,,,Text\n"
	)


# ----------------------------------------------------------------------
# CSV reading
# ----------------------------------------------------------------------


def _read_csv(file_url: str) -> Iterable[dict]:
	"""Yield dicts keyed by lower-cased header for each non-empty row."""
	path = _resolve_file_path(file_url)
	with open(path, encoding="utf-8-sig", newline="") as f:
		reader = csv.DictReader(f)
		for row in reader:
			# Normalise: strip + lower keys, drop wholly-empty rows.
			normalized = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
			if any(normalized.values()):
				yield normalized


def _resolve_file_path(file_url: str) -> str:
	"""Convert a Frappe file URL like `/files/foo.csv` or
	`/private/files/foo.csv` to an absolute filesystem path.

	Frappe ships two helpers and the choice depends on whether the
	File is private — `get_file()` returns `(name, content)` for
	private files via the framework's permission gate, while
	`get_files_path()` is the public-files filesystem root.
	"""
	file_doc = frappe.db.get_value(
		"File",
		{"file_url": file_url},
		["name", "is_private", "file_name"],
		as_dict=True,
	)
	if not file_doc:
		frappe.throw(_(f"File {file_url} not found"))
	if not frappe.has_permission("File", "read", doc=file_doc.name):
		frappe.throw(_("No permission to access file {0}").format(file_url), frappe.PermissionError)

	from frappe.utils.file_manager import get_files_path

	base = get_files_path(is_private=bool(file_doc.is_private))
	return f"{base}/{file_doc.file_name}"


# ----------------------------------------------------------------------
# Social Post construction
# ----------------------------------------------------------------------


def _create_social_post(parent_campaign: str, title: str, shared_caption: str):
	"""Build a blank Social Post linked to the parent Campaign."""
	sc = frappe.new_doc("Social Post")
	sc.title = title
	sc.campaign = parent_campaign
	sc.status = CampaignStatus.DRAFT
	sc.primary_caption = shared_caption or ""
	sc.insert(ignore_permissions=True)
	return sc


def _resolve_row(sc, row: dict, row_idx: int, overrides: dict) -> dict:
	"""Resolve one CSV row into:
	• a Destination row on the Social Post for the account.
	• a per-account override dict (caption / media) the caller stamps
	  onto the post AFTER save via `_apply_row_overrides`.
	"""
	account_ref = row.get("account", "")
	if not account_ref:
		return _skipped(row_idx, "Missing 'account'")

	account = _resolve_account(account_ref)
	if not account:
		return _skipped(row_idx, f"Account '{account_ref}' not found (try name or handle)")

	scheduled_at = ""
	if row.get("scheduled_at"):
		try:
			scheduled_at = get_datetime(row["scheduled_at"])
		except Exception:
			return _skipped(row_idx, f"Bad scheduled_at '{row['scheduled_at']}'")

	legacy_post_type = (row.get("post_type") or "").strip() or _DEFAULT_POST_TYPE.get(
		account.platform, "Text"
	)

	if not any(d.social_account == account.name for d in sc.posts):
		sc.append(
			"posts",
			{"social_account": account.name, "platform": account.platform, "status": "Draft"},
		)
	overrides[account.name] = {
		"legacy_post_type": legacy_post_type,
		"caption_override": row.get("caption", "") or "",
		"media_override": row.get("media_url", "") or "",
	}

	return {
		"row": row_idx,
		"status": "added",
		"account": account.name,
		"platform": account.platform,
		"scheduled_at_override": str(scheduled_at) if scheduled_at else "",
	}


# Map legacy CSV post_type values to the new parent-level post_type.
_LEGACY_POST_TYPE_TO_NEW = {
	"Text": "Text Post",
	"Image": "Image Post",
	"Video": "Video Post",
	"Carousel": "Carousel Post",
	"Reel": "Reel Post",
	"Story": "Story Post",
	"Short": "Reel Post",
}


def _majority_post_type(overrides: dict[str, dict]) -> str:
	"""Pick the most common legacy post_type across rows and map it to
	the new parent-level naming. Empty string when no rows resolved."""
	counts: dict[str, int] = {}
	for ov in overrides.values():
		legacy = ov.get("legacy_post_type") or "Image"
		counts[legacy] = counts.get(legacy, 0) + 1
	if not counts:
		return ""
	winner = max(counts.items(), key=lambda kv: kv[1])[0]
	return _LEGACY_POST_TYPE_TO_NEW.get(winner, "Image Post")


def _resolve_account(ref: str):
	"""Match an account by name OR handle (with optional leading '@')."""
	ref = ref.strip().lstrip("@")
	if not ref:
		return None

	# Try direct name match first (uuid-style refs).
	direct = frappe.db.get_value(
		"Social Account",
		ref,
		["name", "platform"],
		as_dict=True,
	)
	if direct:
		return direct

	# Then handle match (most human-readable for spreadsheets).
	by_handle = frappe.db.get_value(
		"Social Account",
		{"account_handle": ref},
		["name", "platform"],
		as_dict=True,
	)
	if by_handle:
		return by_handle

	# Last shot: case-insensitive name match (covers "@MyBrand" → "mybrand").
	rows = frappe.get_all(
		"Social Account",
		filters={"account_handle": ["like", ref]},
		fields=["name", "platform"],
		limit=1,
	)
	return rows[0] if rows else None


def _skipped(row_idx: int, reason: str) -> dict:
	return {"row": row_idx, "status": "skipped", "reason": reason}

"""
Campaign controller — top-level marketing initiative orchestrator.

Groups N channel entities under one umbrella:
  - Social Post(s) → organic social posts (1:N via Social Post.campaign)
  - Bulk WhatsApp Message → messaging blasts (legacy single-Link)
  - Ad Campaign(s) → paid advertising (1:N via Ad Campaign.campaign)

The Campaign doesn't own the channel's lifecycle — it references it. The
child entities run independently; the Campaign provides grouping +
UTM defaults + first-comment template + recurrence cadence + rollup.

Recurrence: when `recurrence_pattern` is set the campaign acts as a
template. The `scheduled_tasks.cron_clone_recurring_campaigns` job
(daily) finds Active campaigns whose `next_run_at` has elapsed,
clones the whole umbrella (Campaign + child Social Posts + their
posts), and advances `next_run_at` to the next slot — capped by
`recurrence_end_at`.
"""

from __future__ import annotations

import re

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import add_days, add_months, flt, get_datetime, now_datetime

# Maximum allowed budget for a Marketing Campaign (owner: 5 lakh).
MAX_CAMPAIGN_BUDGET = 500_000


def _get_field_max_length(doctype: str, fieldname: str) -> int:
	"""Derive the character limit for a field from docfield meta or frappe.db.VARCHAR_LEN.

	Args:
		doctype: DocType name (e.g. 'Marketing Campaign').
		fieldname: Fieldname to check.

	Returns:
		int: Maximum character length allowed for this field.
	"""
	df = frappe.get_meta(doctype).get_field(fieldname)
	length = df.length if df else 0
	return length or getattr(frappe.db, "VARCHAR_LEN", 140)


def slugify_utm(text: str | None, max_len: int | None = None) -> str:
	"""Convert text to URL-safe UTM parameter: lowercase, non-alphanumeric runs to single underscore,
	trim leading/trailing underscores, cap at field length.

	Args:
		text: Input string to convert to slug.
		max_len: Optional max length cap (defaults to utm_campaign field length / VARCHAR_LEN).

	Returns:
		str: Slugified UTM string capped at max_len.
	"""
	if text is None:
		return ""
	if max_len is None:
		max_len = _get_field_max_length("Marketing Campaign", "utm_campaign")
	s = re.sub(r"[^a-z0-9]+", "_", str(text).lower()).strip("_")
	return s[:max_len]


def campaign_name_exists(name: str, exclude_name: str | None = None) -> bool:
	"""Check if any Marketing Campaign exists with the same name (trimmed, case-insensitive).

	Args:
		name: Campaign name to check.
		exclude_name: Optional doc name (id) to exclude (for update checks).

	Returns:
		bool: True if another campaign exists with this trimmed, case-insensitive name.
	"""
	trimmed = (name or "").strip()
	if not trimmed:
		return False
	if exclude_name:
		return bool(
			frappe.db.sql(
				"SELECT 1 FROM `tabMarketing Campaign` WHERE LOWER(TRIM(campaign_name)) = LOWER(%s) AND name != %s LIMIT 1",
				(trimmed, exclude_name),
			)
		)
	return bool(
		frappe.db.sql(
			"SELECT 1 FROM `tabMarketing Campaign` WHERE LOWER(TRIM(campaign_name)) = LOWER(%s) LIMIT 1",
			(trimmed,),
		)
	)


def make_unique_campaign_name(base: str, exclude_name: str | None = None) -> str:
	"""Generate a unique campaign name by appending a suffix until unique, capping at max length.

	Suffixing rules:
	- If base ends with '(Copy)' or '(Copy N)', continues with '(Copy 2)', '(Copy 3)', ...
	- Otherwise, tries base, then '<base> (2)', '<base> (3)', ...
	- Truncates base (not suffix) to fit within the field's varchar length (140).

	Args:
		base: Starting campaign name string.
		exclude_name: Optional campaign document name to exclude from existence checks.

	Returns:
		str: Unique campaign name adhering to length and uniqueness limits.
	"""
	max_len = _get_field_max_length("Marketing Campaign", "campaign_name")
	trimmed = (base or "").strip()

	copy_match = re.match(r"^(.*?)\s*\(Copy(?:\s*(\d+))?\)$", trimmed, flags=re.IGNORECASE)
	if copy_match:
		root = copy_match.group(1).strip()
		num_str = copy_match.group(2)
		start_num = int(num_str) if num_str else 1

		counter = start_num
		while True:
			suffix = " (Copy)" if counter == 1 else f" (Copy {counter})"
			max_root_len = max(1, max_len - len(suffix))
			candidate = f"{root[:max_root_len]}{suffix}"
			if not campaign_name_exists(candidate, exclude_name=exclude_name):
				return candidate
			counter += 1
	else:
		candidate = trimmed[:max_len]
		if not campaign_name_exists(candidate, exclude_name=exclude_name):
			return candidate

		counter = 2
		while True:
			suffix = f" ({counter})"
			max_root_len = max(1, max_len - len(suffix))
			candidate = f"{trimmed[:max_root_len]}{suffix}"
			if not campaign_name_exists(candidate, exclude_name=exclude_name):
				return candidate
			counter += 1


class MarketingCampaign(Document):
	def validate(self):
		self._validate_window()
		self._slugify_utm_fields()
		self._validate_name_and_utm_lengths()
		self._validate_unique_name()
		self._validate_amounts()
		self._backlink_whatsapp()
		self._normalise_recurrence()

	def after_insert(self):
		# Event Architecture (Phase 4) — the one lifecycle event that
		# belongs on the doctype itself rather than in
		# `campaign/services/lifecycle.py`: every Campaign-creating path
		# (composer, Desk, recurring cloner, demo seeding) goes through
		# `Document.insert()`, so this is the single point that sees all
		# of them, instead of duplicating an emit() call in each caller.
		from msuite_campaigns.campaign.services.events import CampaignEvent, emit

		emit(CampaignEvent.CREATED, self.name, status=self.status)

	def _slugify_utm_fields(self) -> None:
		"""Auto-slug UTM parameters: lowercase, non-alphanumeric runs to single underscore,
		trim leading/trailing underscores, capped at field length.
		"""
		for field in ("utm_campaign", "utm_term", "utm_source", "utm_medium"):
			val = self.get(field)
			if val:
				self.set(field, slugify_utm(val))

	def _validate_name_and_utm_lengths(self) -> None:
		"""Validate field lengths with a short plain message that does not echo the value,
		running before Frappe's generic _validate_length.
		"""
		for field in ("campaign_name", "utm_campaign", "utm_term"):
			val = self.get(field)
			if val:
				max_len = _get_field_max_length(self.doctype, field)
				if len(str(val)) > max_len:
					df = self.meta.get_field(field)
					label = _(df.label) if df else field
					frappe.throw(_("{0} cannot exceed {1} characters.").format(label, max_len))

	def _validate_unique_name(self) -> None:
		"""Ensure campaign_name is unique (trimmed, case-insensitive) across all campaigns."""
		trimmed = (self.campaign_name or "").strip()
		if not trimmed:
			frappe.throw(_("Campaign Name is required."))
		self.campaign_name = trimmed

		exclude_name = None if self.is_new() else self.name
		if campaign_name_exists(self.campaign_name, exclude_name=exclude_name):
			frappe.throw(_("A campaign with this name already exists"))

	def _validate_window(self) -> None:
		"""Campaign drives the schedule window for every child channel —
		Social posts and Ads must schedule inside `start_date → end_date`.
		Enforcing both as mandatory + ordered here removes three downstream
		"what if dates are missing" branches in the child composers.
		"""
		if not self.start_date:
			frappe.throw(_("Start date is required."))
		if not self.end_date:
			frappe.throw(_("End date is required."))
		if get_datetime(self.end_date) <= get_datetime(self.start_date):
			frappe.throw(_("End date must be after the start date."))

	def _validate_amounts(self) -> None:
		"""A negative budget or spend skews every dashboard total, so refuse it.
		Budget is also coerced via flt and capped at MAX_CAMPAIGN_BUDGET
		(spent is not capped as ad sync writes it).
		"""
		self.budget = flt(self.budget)
		for field in ("budget", "spent"):
			val = flt(self.get(field))
			if val < 0:
				frappe.throw(_("{0} must be 0 or more.").format(_(self.meta.get_label(field))))

		if self.budget > MAX_CAMPAIGN_BUDGET:
			frappe.throw(_("Budget cannot exceed {0}.").format(MAX_CAMPAIGN_BUDGET))

	def _backlink_whatsapp(self) -> None:
		"""`whatsapp_blast` is the deprecated single-Link field — kept
		read-only for backward compatibility with installs that still
		read it. The real, current relationship is 1:N via
		`Bulk WhatsApp Message.campaign` (the child sets it directly,
		same as Social Post and Ad Campaign), so this only mirrors an
		already-set legacy value onto the new field for installs that
		still write `whatsapp_blast` through old API callers.
		"""
		if self.whatsapp_blast:
			frappe.db.set_value(
				"Bulk WhatsApp Message",
				self.whatsapp_blast,
				"campaign",
				self.name,
			)

	def _normalise_recurrence(self) -> None:
		"""Compute `next_run_at` on save when recurrence is enabled
		and it's blank. Subsequent cron runs advance it themselves.
		"""
		if not self.recurrence_pattern:
			self.next_run_at = None
			return
		if not self.recurrence_interval or self.recurrence_interval < 1:
			self.recurrence_interval = 1
		if not self.next_run_at:
			# First fire = the campaign's start_date if set, else now +
			# one interval. Saves the marketer from typing the next
			# run time by hand.
			base = get_datetime(self.start_date) if self.start_date else now_datetime()
			self.next_run_at = compute_next_run(base, self.recurrence_pattern, self.recurrence_interval)


# ----------------------------------------------------------------------
# Recurrence math
# ----------------------------------------------------------------------


def compute_next_run(base, pattern: str, interval: int):
	"""Add one interval of the given pattern to `base`."""
	base = get_datetime(base)
	step = max(1, int(interval or 1))
	if pattern == "Daily":
		return add_days(base, step)
	if pattern == "Weekly":
		return add_days(base, 7 * step)
	if pattern == "Monthly":
		return add_months(base, step)
	return None

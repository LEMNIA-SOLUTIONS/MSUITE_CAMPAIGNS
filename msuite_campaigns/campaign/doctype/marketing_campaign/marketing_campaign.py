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

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import add_days, add_months, get_datetime, now_datetime


class MarketingCampaign(Document):
	def validate(self):
		self._validate_window()
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
		"""A negative budget or spend skews every dashboard total, so refuse it."""
		for field in ("budget", "spent"):
			if (self.get(field) or 0) < 0:
				frappe.throw(_("{0} must be 0 or more.").format(_(self.meta.get_label(field))))

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

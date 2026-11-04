"""
Daily cron that clones recurring Campaigns + their child Social
Campaigns when `next_run_at` elapses.

Why clone (vs. re-using the original): keeps every cycle's
performance discoverable as its own record. "Weekly newsletter
2026-05-24" stays grep-able by date and has its own UTM_campaign
value (slug + ISO date), so analytics don't smear across weeks.

Cloning shape per cycle:
  • Campaign         — new doc, `parent_recurring_campaign` set
                      to the original, status="Draft" (marketer
                      reviews + activates), new `campaign_name`
                      suffixed with the cycle date so it sorts
                      alphabetically and finds easily.
  • Social Posts — every child where `campaign = <original>`
                      gets cloned, posts reset to "Draft", scheduled
                      times shifted relative to the cycle start.
  • Ad / WhatsApp    — NOT cloned (yet). Those entities have their
                      own cadence semantics (running ads, message
                      queues) where blind cloning is unsafe.
                      Future: opt-in per-channel.

Marketers can disable per-campaign by clearing `recurrence_pattern`
or by flipping the Campaign to Cancelled / Completed — the cron
filters on Active + recurrence_pattern + next_run_at.

Hook in `hooks.py::scheduler_events.daily`:
    msuite_campaigns.campaign.scheduled_tasks.recurring_cloner.run
"""

from __future__ import annotations

import frappe
from frappe.utils import add_to_date, get_datetime, now_datetime

from msuite_campaigns.campaign.doctype.marketing_campaign.marketing_campaign import compute_next_run
from msuite_campaigns.campaign.services.events import CampaignEvent, emit
from msuite_core.constants import LOGGER_NAME

logger = frappe.logger(LOGGER_NAME)


def run() -> dict:
	"""Cron entry point — returns a summary for log aggregation."""
	summary = {"cloned": 0, "completed": 0, "skipped": 0, "failed": 0}

	due = frappe.get_all(
		"Marketing Campaign",
		filters={
			"status": "Active",
			"recurrence_pattern": ["in", ["Daily", "Weekly", "Monthly"]],
			"next_run_at": ["<=", now_datetime()],
		},
		fields=[
			"name",
			"campaign_name",
			"recurrence_pattern",
			"recurrence_interval",
			"recurrence_end_at",
			"next_run_at",
		],
	)

	for row in due:
		try:
			# Past the end-date? Mark original Completed and stop.
			if row.recurrence_end_at and get_datetime(row.recurrence_end_at) <= now_datetime():
				frappe.db.set_value(
					"Marketing Campaign", row.name, "status", "Completed", update_modified=False
				)
				emit(CampaignEvent.COMPLETED, row.name)
				summary["completed"] += 1
				continue

			_clone_one_cycle(row)
			_advance_next_run(row)
			summary["cloned"] += 1

		except Exception as e:
			logger.error(f"Recurring clone failed for Campaign {row.name}: {e}")
			summary["failed"] += 1

	frappe.db.commit()
	logger.info(f"Recurring campaign cloner: {summary}")
	return summary


# ----------------------------------------------------------------------
# Cloning
# ----------------------------------------------------------------------


def _clone_one_cycle(original_row) -> str:
	"""Clone the Campaign + every linked Social Post + their
	posts. Returns the new Campaign's name.
	"""
	original = frappe.get_doc("Marketing Campaign", original_row.name)
	cycle_date = now_datetime().strftime("%Y-%m-%d")

	clone = frappe.copy_doc(original, ignore_no_copy=False)
	clone.campaign_name = f"{original.campaign_name} — {cycle_date}"
	clone.status = "Draft"
	clone.parent_recurring_campaign = original.name
	# Don't carry over the recurrence settings — the clone is one
	# instance, not a new template. Marketer can re-enable on the
	# clone if they want a sub-series.
	clone.recurrence_pattern = ""
	clone.recurrence_interval = None
	clone.recurrence_end_at = None
	clone.next_run_at = None
	# Reset spent — the original cumulative number doesn't apply.
	clone.spent = 0
	# Channel links don't make sense to inherit because we're going
	# to clone the children below; clear them on the umbrella.
	if hasattr(clone, "whatsapp_blast"):
		clone.whatsapp_blast = ""
	clone.insert(ignore_permissions=True)

	# Clone every Social Post that pointed at the original.
	child_names = frappe.get_all(
		"Social Post",
		filters={"campaign": original.name},
		pluck="name",
	)
	for child_name in child_names:
		_clone_social_post(child_name, clone.name)

	logger.info(f"Cloned Campaign {original.name} → {clone.name} ({len(child_names)} child Social Post(s))")
	return clone.name


def _clone_social_post(original_name: str, new_parent_campaign: str) -> str:
	"""Clone one Social Post, point it at the new parent Campaign,
	and reset every post to Draft so the cycle starts fresh.
	"""
	original = frappe.get_doc("Social Post", original_name)
	clone = frappe.copy_doc(original, ignore_no_copy=False)
	clone.campaign = new_parent_campaign
	clone.status = "Draft"
	clone.scheduled_at = None  # marketer re-schedules on the cycle
	# Wipe runtime fields on every child post — the clone is fresh.
	for post in clone.posts or []:
		post.status = "Draft"
		post.platform_post_id = ""
		post.platform_post_url = ""
		post.published_at = None
		post.retry_count = 0
		post.last_attempt_at = None
		post.next_retry_at = None
		post.failure_reason = ""
		post.last_failure_kind = ""
		post.failure_detail = None
		post.idempotency_key = ""
		post.scheduled_at_override = None
	clone.insert(ignore_permissions=True)
	return clone.name


# ----------------------------------------------------------------------
# Schedule advancement
# ----------------------------------------------------------------------


def _advance_next_run(original_row) -> None:
	"""Push next_run_at forward by one interval from the previous
	next_run_at (NOT from now — drift would accumulate otherwise).
	"""
	next_at = compute_next_run(
		original_row.next_run_at,
		original_row.recurrence_pattern,
		original_row.recurrence_interval or 1,
	)

	# If the new next_run is past the end date, mark completed.
	if original_row.recurrence_end_at and next_at and next_at > get_datetime(original_row.recurrence_end_at):
		frappe.db.set_value(
			"Marketing Campaign",
			original_row.name,
			{"status": "Completed", "next_run_at": None},
			update_modified=False,
		)
		emit(CampaignEvent.COMPLETED, original_row.name)
		return

	frappe.db.set_value(
		"Marketing Campaign", original_row.name, "next_run_at", next_at, update_modified=False
	)

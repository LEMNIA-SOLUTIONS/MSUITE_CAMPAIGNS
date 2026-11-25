# Copyright (c) 2026, MSuite and contributors
# For license information, please see license.txt

"""Broadcast scheduler for scheduled broadcasts.

Scans for Scheduled channel rows whose effective scheduled_time has elapsed and whose
linked campaign is active or empty, moving them to Queued and initiating dispatch.
"""

from __future__ import annotations

import frappe
from frappe.utils import add_to_date, now_datetime

from msuite_campaigns.broadcast.constants import (
	ERROR_NO_RECIPIENTS_QUEUED,
	SCHEDULE_PER_CHANNEL,
	SCHEDULER_BATCH_LIMIT,
	STALE_QUEUED_MINUTES,
	STATUS_FAILED,
	STATUS_PARTIALLY_FAILED,
	STATUS_QUEUED,
)
from msuite_campaigns.broadcast.hub import MAX_LAUNCH_ERROR_LENGTH
from msuite_campaigns.broadcast.lifecycle import rollup_status, transition_channel
from msuite_campaigns.campaign.services.relationships import is_campaign_active


def fail_stale_queued_channels() -> int:
	"""Identify and fail stale Queued broadcast channel rows with zero recipients.

	Scans for child Broadcast Channel rows with status 'Queued' whose recipient_count
	is 0 and whose modified timestamp is older than STALE_QUEUED_MINUTES (30 min).
	Marks both the channel row and the parent Broadcast as 'Failed' with launch_error
	set to ERROR_NO_RECIPIENTS_QUEUED ("No recipients were queued").

	Uses lifecycle.transition_channel to transition the channel row to 'Failed'.

	Params:
		None.

	Returns:
		int: Number of stale channel rows marked Failed.

	Raises:
		None.

	Side effects:
		Transitions channel and parent broadcast status to 'Failed' in database.
		Updates launch_error on tabBroadcast Channel and tabBroadcast.
	"""
	cutoff = add_to_date(now_datetime(), minutes=-STALE_QUEUED_MINUTES)
	stale_rows = frappe.db.sql(
		"""
		SELECT
			bc.name AS channel_row_name,
			bc.channel AS channel,
			bc.parent AS broadcast
		FROM `tabBroadcast Channel` bc
		JOIN `tabBroadcast` b ON b.name = bc.parent
		WHERE bc.status = %(status_queued)s
		  AND COALESCE(bc.recipient_count, 0) = 0
		  AND bc.modified <= %(cutoff)s
		ORDER BY bc.modified ASC
		""",
		{
			"status_queued": STATUS_QUEUED,
			"cutoff": cutoff,
		},
		as_dict=True,
	)

	failed_count = 0
	for row in stale_rows:
		try:
			transition_channel(row.broadcast, row.channel, STATUS_FAILED)
			frappe.db.set_value(
				"Broadcast Channel",
				row.channel_row_name,
				"launch_error",
				ERROR_NO_RECIPIENTS_QUEUED,
				update_modified=False,
			)
			parent_status = frappe.db.get_value("Broadcast", row.broadcast, "status")
			if parent_status in (STATUS_FAILED, STATUS_PARTIALLY_FAILED):
				frappe.db.set_value(
					"Broadcast",
					row.broadcast,
					"launch_error",
					ERROR_NO_RECIPIENTS_QUEUED,
					update_modified=False,
				)
			failed_count += 1
		except Exception as exc:
			frappe.log_error(
				title=f"Failed to mark stale queued channel {row.broadcast} {row.channel} as Failed",
				message=frappe.get_traceback() if hasattr(frappe, "get_traceback") else str(exc),
			)

	return failed_count


def start_due_channels() -> int:
	"""Scan for due Scheduled channel rows, move them to Queued, and start them.

	Finds channel rows with status 'Scheduled' whose effective scheduled_time <= now
	(using bc.scheduled_time when mode is 'Per channel', parent scheduled_time otherwise)
	and whose campaign is empty or active (via is_campaign_active).
	Bounds the candidate scan to SCHEDULER_BATCH_LIMIT earliest due rows.

	Transitions each matching row to Queued via lifecycle.transition_channel
	and starts dispatch via hub.start_channel.

	Idempotent: running repeatedly will not re-start already moved rows.
	Racing a manual launch is a silent no-op (no duplicate dispatch, no error logged).
	Failure in one row is rolled back and logged via frappe.log_error without
	preventing remaining rows from starting. Successfully started rows are committed.

	Params:
		None.

	Returns:
		int: Number of channel rows successfully started.

	Raises:
		None.

	Side effects:
		Updates channel and broadcast statuses in DB.
		Commits transaction after each successfully started row.
		Rolls back per-row transaction on failure.
		Enqueues background jobs.
		Logs errors to tabError Log if individual channel dispatch fails.
	"""
	fail_stale_queued_channels()
	now = now_datetime()
	limit = SCHEDULER_BATCH_LIMIT

	candidate_rows = frappe.db.sql(
		"""
		SELECT
			bc.name AS channel_row_name,
			bc.channel AS channel,
			bc.parent AS broadcast,
			b.campaign AS campaign,
			CASE
				WHEN b.schedule_mode = %(mode_per_channel)s THEN bc.scheduled_time
				ELSE b.scheduled_time
			END AS scheduled_time
		FROM `tabBroadcast Channel` bc
		JOIN `tabBroadcast` b ON b.name = bc.parent
		WHERE bc.status = 'Scheduled'
		  AND (
				CASE
					WHEN b.schedule_mode = %(mode_per_channel)s THEN bc.scheduled_time
					ELSE b.scheduled_time
				END
		  ) IS NOT NULL
		  AND (
				CASE
					WHEN b.schedule_mode = %(mode_per_channel)s THEN bc.scheduled_time
					ELSE b.scheduled_time
				END
		  ) <= %(now)s
		ORDER BY scheduled_time ASC, bc.creation ASC
		LIMIT %(limit)s
		""",
		{"now": now, "mode_per_channel": SCHEDULE_PER_CHANNEL, "limit": limit},
		as_dict=True,
	)

	started_count = 0
	for row in candidate_rows:
		if not is_campaign_active(row.campaign):
			continue

		# If already transitioned (e.g. manual launch racing the scheduler), skip silently
		current_status = frappe.db.get_value("Broadcast Channel", row.channel_row_name, "status")
		if current_status != "Scheduled":
			continue

		try:
			transition_channel(row.broadcast, row.channel, "Queued")
		except frappe.ValidationError:
			frappe.db.rollback()
			# Silent no-op if manual launch concurrently transitioned the row out of Scheduled
			current_status = frappe.db.get_value("Broadcast Channel", row.channel_row_name, "status")
			if current_status != "Scheduled":
				continue
			frappe.log_error(
				title=f"Broadcast scheduler failed for {row.broadcast} {row.channel}",
				message=frappe.get_traceback() if hasattr(frappe, "get_traceback") else "",
			)
			continue
		except Exception as exc:
			frappe.db.rollback()
			frappe.log_error(
				title=f"Broadcast scheduler failed for {row.broadcast} {row.channel}",
				message=frappe.get_traceback() if hasattr(frappe, "get_traceback") else str(exc),
			)
			continue

		try:
			from msuite_campaigns.broadcast import hub

			hub.start_channel(row.broadcast, row.channel)
			frappe.db.commit()
			started_count += 1
		except Exception as exc:
			frappe.db.rollback()
			frappe.log_error(
				title=f"Broadcast scheduler failed for {row.broadcast} {row.channel}",
				message=frappe.get_traceback() if hasattr(frappe, "get_traceback") else str(exc),
			)
			try:
				err_msg = str(exc)[:MAX_LAUNCH_ERROR_LENGTH]
				frappe.db.set_value("Broadcast Channel", row.channel_row_name, "status", "Failed", update_modified=False)
				frappe.db.set_value("Broadcast Channel", row.channel_row_name, "launch_error", err_msg, update_modified=False)
				parent_doc = frappe.get_doc("Broadcast", row.broadcast)
				parent_status = rollup_status(parent_doc)
				frappe.db.set_value("Broadcast", row.broadcast, "status", parent_status, update_modified=False)
				if parent_status in (STATUS_FAILED, STATUS_PARTIALLY_FAILED):
					frappe.db.set_value("Broadcast", row.broadcast, "launch_error", err_msg, update_modified=False)
				frappe.db.commit()
			except Exception:
				frappe.db.rollback()

	return started_count

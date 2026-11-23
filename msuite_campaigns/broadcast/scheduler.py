# Copyright (c) 2026, MSuite and contributors
# For license information, please see license.txt

"""Broadcast scheduler for scheduled broadcasts.

Scans for Scheduled channel rows whose effective scheduled_time has elapsed and whose
linked campaign is active or empty, moving them to Queued and initiating dispatch.
"""

from __future__ import annotations

import frappe
from frappe.utils import now_datetime

from msuite_campaigns.broadcast.constants import SCHEDULE_PER_CHANNEL
from msuite_campaigns.broadcast.lifecycle import transition_channel
from msuite_campaigns.campaign.services.relationships import is_campaign_active


def start_due_channels() -> int:
	"""Scan for due Scheduled channel rows, move them to Queued, and start them.

	Finds channel rows with status 'Scheduled' whose effective scheduled_time <= now
	(using bc.scheduled_time when mode is 'Per channel', parent scheduled_time otherwise)
	and whose campaign is empty or active (via is_campaign_active).

	Transitions each matching row to Queued via lifecycle.transition_channel
	and starts dispatch via hub.start_channel.

	Idempotent: running repeatedly will not re-start already moved rows.
	Racing a manual launch is a silent no-op (no duplicate dispatch, no error logged).
	Failure in one row is logged via frappe.log_error and does not prevent
	remaining rows from starting.

	Returns:
		int: Number of channel rows successfully started.

	Raises:
		None.

	Side effects:
		Updates channel and broadcast statuses in DB.
		Enqueues background jobs.
		Logs errors to tabError Log if individual channel dispatch fails.
	"""
	now = now_datetime()
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
		""",
		{"now": now, "mode_per_channel": SCHEDULE_PER_CHANNEL},
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
			frappe.log_error(
				title=f"Broadcast scheduler failed for {row.broadcast} {row.channel}",
				message=frappe.get_traceback() if hasattr(frappe, "get_traceback") else str(exc),
			)
			continue

		try:
			from msuite_campaigns.broadcast import hub

			hub.start_channel(row.broadcast, row.channel)
			started_count += 1
		except Exception as exc:
			frappe.log_error(
				title=f"Broadcast scheduler failed for {row.broadcast} {row.channel}",
				message=frappe.get_traceback() if hasattr(frappe, "get_traceback") else str(exc),
			)

	return started_count

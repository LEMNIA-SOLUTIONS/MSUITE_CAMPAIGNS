# Copyright (c) 2026, MSuite and contributors
# For license information, please see license.txt

"""Broadcast scheduler for scheduled broadcasts.

Scans for Scheduled channel rows whose scheduled_time has elapsed and whose
linked campaign is active or empty, moving them to Queued and initiating dispatch.
"""

from __future__ import annotations

import frappe
from frappe.utils import now_datetime

from msuite_campaigns.broadcast.lifecycle import transition_channel
from msuite_campaigns.campaign.services.relationships import is_campaign_active


def start_due_channels() -> int:
	"""Scan for due Scheduled channel rows, move them to Queued, and start them.

	Finds channel rows with status 'Scheduled' whose Broadcast has scheduled_time <= now
	and whose campaign is empty or active (via is_campaign_active).
	Transitions each matching row to Queued via lifecycle.transition_channel
	and starts dispatch via hub.start_channel.

	Idempotent: running repeatedly will not re-start already moved rows.
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
			b.scheduled_time AS scheduled_time
		FROM `tabBroadcast Channel` bc
		JOIN `tabBroadcast` b ON b.name = bc.parent
		WHERE bc.status = 'Scheduled'
		  AND b.scheduled_time IS NOT NULL
		  AND b.scheduled_time <= %(now)s
		ORDER BY b.scheduled_time ASC, bc.creation ASC
		""",
		{"now": now},
		as_dict=True,
	)

	started_count = 0
	for row in candidate_rows:
		if not is_campaign_active(row.campaign):
			continue

		try:
			transition_channel(row.broadcast, row.channel, "Queued")
			from msuite_campaigns.broadcast import hub

			hub.start_channel(row.broadcast, row.channel)
			started_count += 1
		except Exception as exc:
			frappe.log_error(
				title=f"Broadcast scheduler failed for {row.broadcast} {row.channel}",
				message=frappe.get_traceback() if hasattr(frappe, "get_traceback") else str(exc),
			)

	return started_count

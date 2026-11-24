# Copyright (c) 2026, MSuite and contributors
# For license information, please see license.txt

"""Broadcast counters and counter refresh service.

Calculates recipient engagement and delivery metrics with grouped SQL queries,
updates child channel rows, and advances channels from In Progress to their
terminal completion statuses (Completed, Partially Failed, or Failed).
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe.utils import cint

from msuite_campaigns.broadcast.constants import (
	ALLOWED_TRANSITIONS,
	PENDING_RECIPIENT_STATUSES,
	STATUS_COMPLETED,
	STATUS_FAILED,
	STATUS_IN_PROGRESS,
	STATUS_PARTIALLY_FAILED,
	STATUS_QUEUED,
)

_PENDING_KEY = "_pending_counter_refreshes"


def refresh_counters(broadcast: str, channel: str) -> dict[str, int]:
	"""Recompute counters for a channel row and advance completed channels.

	Runs a single grouped SQL query over `tabBroadcast Recipient` to compute
	recipient, sent, delivered, read, failed, bounced, opened, clicked,
	unsubscribed, and retry counts.

	When the channel row is `Queued` or `In Progress`, recipient_count > 0, and
	no recipients remain in `Queued`, `Retrying`, or `Sending`, transitions the
	channel row via `lifecycle.transition_channel`:
	- All Failed -> `Failed`
	- Any Failed or Bounced -> `Partially Failed`
	- Else -> `Completed`

	If a transition directly from `Queued` to the target status is not permitted
	by `ALLOWED_TRANSITIONS` (e.g. Queued -> Completed), steps through `In Progress`
	first. Calling on an already completed channel is an idempotent no-op.

	Args:
		broadcast: Name of the parent Broadcast document.
		channel: Channel name ('WhatsApp', 'Email', or 'SMS').

	Returns:
		dict[str, int]: Calculated counters dictionary.

	Raises:
		None.

	Side effects:
		Updates counter columns on `tabBroadcast Channel`.
		May update `total_recipients` on `tabBroadcast`.
		May transition channel and broadcast statuses via `transition_channel`.
	"""
	sql = """
		SELECT
			COUNT(*) AS recipient_count,
			SUM(CASE WHEN status IN ('Sent', 'Delivered', 'Read', 'Bounced') THEN 1 ELSE 0 END) AS sent_count,
			SUM(CASE WHEN status IN ('Delivered', 'Read') THEN 1 ELSE 0 END) AS delivered_count,
			SUM(CASE WHEN status = 'Read' THEN 1 ELSE 0 END) AS read_count,
			SUM(CASE WHEN status = 'Failed' THEN 1 ELSE 0 END) AS failed_count,
			SUM(CASE WHEN status = 'Bounced' THEN 1 ELSE 0 END) AS bounced_count,
			SUM(CASE WHEN opened_at IS NOT NULL THEN 1 ELSE 0 END) AS opened_count,
			SUM(CASE WHEN clicked_at IS NOT NULL THEN 1 ELSE 0 END) AS clicked_count,
			SUM(CASE WHEN status = 'Unsubscribed' THEN 1 ELSE 0 END) AS unsubscribed_count,
			COALESCE(SUM(retry_count), 0) AS retry_count,
			SUM(CASE WHEN status IN %(pending_statuses)s THEN 1 ELSE 0 END) AS queued_count
		FROM `tabBroadcast Recipient`
		WHERE `broadcast` = %(broadcast)s
		  AND `channel` = %(channel)s
	"""
	raw_counts = frappe.db.sql(
		sql,
		{
			"broadcast": broadcast,
			"channel": channel,
			"pending_statuses": PENDING_RECIPIENT_STATUSES,
		},
		as_dict=True,
	)
	row_data = raw_counts[0] if raw_counts else {}

	counts = {
		"recipient_count": cint(row_data.get("recipient_count")),
		"sent_count": cint(row_data.get("sent_count")),
		"delivered_count": cint(row_data.get("delivered_count")),
		"read_count": cint(row_data.get("read_count")),
		"failed_count": cint(row_data.get("failed_count")),
		"bounced_count": cint(row_data.get("bounced_count")),
		"opened_count": cint(row_data.get("opened_count")),
		"clicked_count": cint(row_data.get("clicked_count")),
		"unsubscribed_count": cint(row_data.get("unsubscribed_count")),
		"retry_count": cint(row_data.get("retry_count")),
	}
	queued_count = cint(row_data.get("queued_count"))

	ch_row = frappe.db.get_value(
		"Broadcast Channel",
		{"parent": broadcast, "channel": channel},
		["name", "status"],
		as_dict=True,
	)

	if ch_row:
		frappe.db.set_value("Broadcast Channel", ch_row.name, counts, update_modified=False)

		all_counts = frappe.db.get_values(
			"Broadcast Channel",
			{"parent": broadcast},
			"recipient_count",
			as_dict=True,
		)
		total_recipients = max([c.get("recipient_count") or 0 for c in all_counts] or [0])
		frappe.db.set_value("Broadcast", broadcast, "total_recipients", total_recipients, update_modified=False)

		# Transition when Queued or In Progress, recipient_count > 0, and all recipients finished
		if ch_row.status in (STATUS_QUEUED, STATUS_IN_PROGRESS) and queued_count == 0 and counts["recipient_count"] > 0:
			if counts["failed_count"] == counts["recipient_count"]:
				target_status = STATUS_FAILED
			elif counts["failed_count"] > 0 or counts["bounced_count"] > 0:
				target_status = STATUS_PARTIALLY_FAILED
			else:
				target_status = STATUS_COMPLETED

			from msuite_campaigns.broadcast.lifecycle import transition_channel

			# If direct transition from current status is not permitted, step through In Progress first
			allowed_from_current = ALLOWED_TRANSITIONS.get(ch_row.status, set())
			if target_status not in allowed_from_current:
				transition_channel(broadcast, channel, STATUS_IN_PROGRESS)

			transition_channel(broadcast, channel, target_status)

	pending = getattr(frappe.local, _PENDING_KEY, None)
	if pending is not None:
		pending.discard(f"refresh_counters::{broadcast}::{channel}")

	return counts


def request_counter_refresh(broadcast: str, channel: str) -> None:
	"""Enqueue counter recalculation with de-duplicating job id.

	Ensures multiple incoming webhooks or events within a short window coalesce
	into a single background job.

	Args:
		broadcast: Name of the Broadcast document.
		channel: Channel name ('WhatsApp', 'Email', or 'SMS').

	Returns:
		None.

	Raises:
		None.

	Side effects:
		Calls `frappe.enqueue` with `job_id` and `deduplicate=True`.
	"""
	job_id = f"refresh_counters::{broadcast}::{channel}"

	pending = getattr(frappe.local, _PENDING_KEY, None)
	if pending is None:
		pending = set()
		setattr(frappe.local, _PENDING_KEY, pending)

	if job_id in pending:
		return

	pending.add(job_id)

	frappe.enqueue(
		refresh_counters,
		queue="default",
		job_id=job_id,
		deduplicate=True,
		enqueue_after_commit=True,
		broadcast=broadcast,
		channel=channel,
	)

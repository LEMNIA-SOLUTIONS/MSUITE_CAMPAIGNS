# Copyright (c) 2026, MSuite and contributors
# For license information, please see license.txt

"""Patch: Backfill default schedule_mode on existing Broadcast records."""

from __future__ import annotations

import frappe

from msuite_campaigns.broadcast.constants import SCHEDULE_SAME


def execute() -> None:
	"""Set default schedule_mode on existing Broadcast records with empty mode.

	Ensures all existing Broadcasts have schedule_mode set to SCHEDULE_SAME
	('Same for all channels'). Idempotent: rows already having a schedule_mode
	are untouched.

	Params: None.
	Returns: None.
	Raises: None.
	Side effects: Updates tabBroadcast records where schedule_mode is empty or null.
	"""
	if not frappe.db.table_exists("Broadcast"):
		return

	frappe.db.sql(
		"""
		UPDATE `tabBroadcast`
		SET schedule_mode = %s
		WHERE schedule_mode IS NULL OR schedule_mode = ''
		""",
		(SCHEDULE_SAME,),
	)

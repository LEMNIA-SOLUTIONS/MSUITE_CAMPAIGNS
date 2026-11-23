# Copyright (c) 2026, MSuite and contributors
# For license information, please see license.txt

"""Patch: Migrate legacy bulk messages, recipient lists and audiences to unified doctypes.

Calls migration.run(dry_run=False) and logs the summary. Wrapped so a failure
is logged and re-raised, keeping the patch re-runnable.
"""

from __future__ import annotations

import frappe
from msuite_campaigns.broadcast import migration


def execute() -> dict:
	"""Execute the data migration patch.

	Migrates legacy Audience, WhatsApp Recipient List, Email Recipient List to
	Recipient List (+ Recipient Contact), and legacy Bulk WhatsApp/Email/SMS Message
	to Broadcast (+ Broadcast Channel, Broadcast Product, Broadcast Recipient).

	Returns:
		dict: Migration summary report.

	Raises:
		Exception: Logs error and re-raises to ensure patch failure is tracked
			and re-runnable.
	"""
	logger = frappe.logger("broadcast_migration")
	logger.info("Starting migrate_bulk_to_broadcast patch execution")

	try:
		summary = migration.run(dry_run=False)
		logger.info(
			"migrate_bulk_to_broadcast patch completed successfully: "
			f"migrated lists={summary.get('recipient_lists')}, "
			f"migrated broadcasts={summary.get('broadcasts')}, "
			f"recipients={summary.get('recipients')}"
		)
		return summary
	except Exception as e:
		logger.error(f"migrate_bulk_to_broadcast patch failed: {e}", exc_info=True)
		raise

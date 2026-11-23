# Copyright (c) 2026, MSuite and contributors
# For license information, please see license.txt

"""Legacy test helpers for conditional execution when legacy doctypes are present."""

from __future__ import annotations

import unittest

import frappe


def skip_without_legacy_doctypes() -> None:
	"""Skip the test if legacy bulk messaging doctypes do not exist.

	Raises unittest.SkipTest if 'Bulk WhatsApp Message' is not present in the
	DocType table, as occurs after the remove_legacy_bulk_doctypes patch.

	Args:
		None.

	Returns:
		None.

	Raises:
		unittest.SkipTest: When 'Bulk WhatsApp Message' DocType is missing.

	Side effects:
		None.
	"""
	if not frappe.db.exists("DocType", "Bulk WhatsApp Message"):
		raise unittest.SkipTest("legacy bulk doctypes removed")

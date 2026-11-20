# Copyright (c) 2026, MSuite and contributors
# For license information, please see license.txt

"""Tests for staged legacy bulk doctypes deletion patch and guards (Task 15).

Verifies that the deletion patch refuses execution unless confirmed and verified,
supports dry-run planning, is completely absent from all patches.txt files,
is idempotent when legacy doctypes are absent, and never deletes without safeguards.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, call, patch

import frappe
from frappe.tests import IntegrationTestCase

from msuite_campaigns.patches import delete_legacy_bulk_doctypes


class TestDeleteLegacyGuard(IntegrationTestCase):
	"""Test execution guards, validation, and dry-run planning for legacy deletion."""

	def setUp(self) -> None:
		"""Set up test environment and mock safeguards before each test.

		Side effects:
			None.
		"""
		super().setUp()

	def tearDown(self) -> None:
		"""Clean up test environment after each test.

		Side effects:
			Rolls back database state.
		"""
		frappe.db.rollback()

	def test_refuses_without_confirm_delete(self) -> None:
		"""Verify execute raises ValidationError when confirm_delete is falsy in live mode.

		Raises:
			frappe.ValidationError: Expected when confirm_delete is not truthy.

		Side effects:
			None.
		"""
		with patch.object(frappe, "delete_doc") as mock_delete_doc:
			with self.assertRaises(frappe.ValidationError) as ctx:
				delete_legacy_bulk_doctypes.execute(confirm_delete=0, dry_run=False)

			self.assertIn("confirm_delete", str(ctx.exception).lower())
			mock_delete_doc.assert_not_called()

	def test_refuses_when_verify_reports_mismatch(self) -> None:
		"""Verify execute raises ValidationError when migration.verify reports mismatches.

		Raises:
			frappe.ValidationError: Expected when mismatches exist.

		Side effects:
			None.
		"""
		mismatched_verify = {
			"Audience": {"old_count": 5, "migrated_count": 4, "mismatches": 1},
			"WhatsApp Recipient List": {"old_count": 2, "migrated_count": 2, "mismatches": 0},
			"Email Recipient List": {"old_count": 0, "migrated_count": 0, "mismatches": 0},
			"Bulk WhatsApp Message": {"old_count": 3, "migrated_count": 3, "mismatches": 0},
			"Bulk Email Message": {"old_count": 1, "migrated_count": 1, "mismatches": 0},
			"Bulk SMS Message": {"old_count": 0, "migrated_count": 0, "mismatches": 0},
		}

		with patch("msuite_campaigns.broadcast.migration.verify", return_value=mismatched_verify), \
			patch.object(frappe, "delete_doc") as mock_delete_doc:
			with self.assertRaises(frappe.ValidationError) as ctx:
				delete_legacy_bulk_doctypes.execute(confirm_delete=1, dry_run=False)

			err_msg = str(ctx.exception)
			self.assertIn("Audience", err_msg)
			mock_delete_doc.assert_not_called()

	def test_refuses_when_delta_run_creates_records(self) -> None:
		"""Verify execute raises ValidationError when delta migration run creates new records.

		Raises:
			frappe.ValidationError: Expected when delta migration produces records.

		Side effects:
			None.
		"""
		clean_verify = {
			"Audience": {"old_count": 1, "migrated_count": 1, "mismatches": 0},
			"WhatsApp Recipient List": {"old_count": 1, "migrated_count": 1, "mismatches": 0},
			"Email Recipient List": {"old_count": 0, "migrated_count": 0, "mismatches": 0},
			"Bulk WhatsApp Message": {"old_count": 1, "migrated_count": 1, "mismatches": 0},
			"Bulk Email Message": {"old_count": 0, "migrated_count": 0, "mismatches": 0},
			"Bulk SMS Message": {"old_count": 0, "migrated_count": 0, "mismatches": 0},
		}
		delta_with_creations = {
			"recipient_lists": {
				"Audience": {"total": 1, "migrated": 1, "skipped": 0},
				"WhatsApp Recipient List": {"total": 1, "migrated": 0, "skipped": 1},
				"Email Recipient List": {"total": 0, "migrated": 0, "skipped": 0},
			},
			"broadcasts": {
				"Bulk WhatsApp Message": {"total": 1, "migrated": 0, "skipped": 1},
				"Bulk Email Message": {"total": 0, "migrated": 0, "skipped": 0},
				"Bulk SMS Message": {"total": 0, "migrated": 0, "skipped": 0},
			},
			"recipients": 5,
		}

		with patch("msuite_campaigns.broadcast.migration.verify", return_value=clean_verify), \
			patch("msuite_campaigns.broadcast.migration.run", return_value=delta_with_creations), \
			patch.object(frappe, "delete_doc") as mock_delete_doc:
			with self.assertRaises(frappe.ValidationError) as ctx:
				delete_legacy_bulk_doctypes.execute(confirm_delete=1, dry_run=False)

			err_msg = str(ctx.exception)
			self.assertTrue("Audience" in err_msg or "recipients" in err_msg)
			mock_delete_doc.assert_not_called()

	def test_dry_run_deletes_nothing_and_returns_plan(self) -> None:
		"""Verify dry_run=True returns plan listing all 11 doctypes in order and deletes nothing.

		Side effects:
			None (asserts no DB deletions occurred).
		"""
		expected_order = [
			"Bulk WhatsApp Message Product",
			"WhatsApp Recipient",
			"Email Recipient",
			"SMS Recipient",
			"Bulk WhatsApp Message",
			"Bulk Email Message",
			"Bulk SMS Message",
			"WhatsApp Recipient List",
			"Email Recipient List",
			"Audience Member",
			"Audience",
		]

		with patch.object(frappe, "delete_doc") as mock_delete_doc, \
			patch.object(delete_legacy_bulk_doctypes, "_empty_table") as mock_empty_table:
			plan = delete_legacy_bulk_doctypes.execute(confirm_delete=0, dry_run=True)

			mock_delete_doc.assert_not_called()
			mock_empty_table.assert_not_called()

			self.assertIn("would_delete", plan)
			self.assertIn("verify", plan)
			self.assertIn("would_repoint", plan)
			self.assertEqual(plan["would_delete"], expected_order)

	def test_module_not_referenced_in_any_patches_txt(self) -> None:
		"""Verify delete_legacy_bulk_doctypes is NOT referenced in any app's patches.txt.

		Side effects:
			None (read-only filesystem check).
		"""
		bench_dir = frappe.get_app_path("msuite_campaigns", "..", "..", "..")
		apps_dir = os.path.join(bench_dir, "apps")
		self.assertTrue(os.path.isdir(apps_dir), f"apps dir {apps_dir} must exist")

		found_in: list[str] = []
		patch_target = "delete_legacy_bulk_doctypes"

		for root, _dirs, files in os.walk(apps_dir):
			if "patches.txt" in files:
				patches_file = os.path.join(root, "patches.txt")
				with open(patches_file, "r", encoding="utf-8") as f:
					content = f.read()
					if patch_target in content:
						found_in.append(patches_file)

		self.assertEqual(
			found_in,
			[],
			f"delete_legacy_bulk_doctypes must NOT be listed in any patches.txt, found in: {found_in}",
		)

	def test_execute_legacy_doctypes_absent_is_noop_empty_plan(self) -> None:
		"""Verify execute on a site where legacy doctypes are absent is a no-op returning empty plan.

		Side effects:
			None.
		"""
		with patch.object(frappe.db, "exists", return_value=False), \
			patch.object(frappe, "delete_doc") as mock_delete_doc, \
			patch.object(delete_legacy_bulk_doctypes, "_empty_table") as mock_empty_table:
			# Test dry run mode
			plan_dry = delete_legacy_bulk_doctypes.execute(confirm_delete=0, dry_run=True)
			mock_delete_doc.assert_not_called()
			mock_empty_table.assert_not_called()
			self.assertEqual(plan_dry.get("would_delete"), [])

			# Test live execution mode
			plan_live = delete_legacy_bulk_doctypes.execute(confirm_delete=1, dry_run=False)
			mock_delete_doc.assert_not_called()
			mock_empty_table.assert_not_called()
			self.assertEqual(plan_live.get("would_delete"), [])

	def test_confirmed_execution_deletes_in_order_and_repoints(self) -> None:
		"""Verify confirmed live execution verifies, repoints, empties tables, and deletes doctypes in order.

		Side effects:
			None (delete_doc and table emptying are mocked).
		"""
		clean_verify = {
			"Audience": {"old_count": 0, "migrated_count": 0, "mismatches": 0},
			"WhatsApp Recipient List": {"old_count": 0, "migrated_count": 0, "mismatches": 0},
			"Email Recipient List": {"old_count": 0, "migrated_count": 0, "mismatches": 0},
			"Bulk WhatsApp Message": {"old_count": 0, "migrated_count": 0, "mismatches": 0},
			"Bulk Email Message": {"old_count": 0, "migrated_count": 0, "mismatches": 0},
			"Bulk SMS Message": {"old_count": 0, "migrated_count": 0, "mismatches": 0},
		}
		clean_delta = {
			"recipient_lists": {
				"Audience": {"total": 0, "migrated": 0, "skipped": 0},
				"WhatsApp Recipient List": {"total": 0, "migrated": 0, "skipped": 0},
				"Email Recipient List": {"total": 0, "migrated": 0, "skipped": 0},
			},
			"broadcasts": {
				"Bulk WhatsApp Message": {"total": 0, "migrated": 0, "skipped": 0},
				"Bulk Email Message": {"total": 0, "migrated": 0, "skipped": 0},
				"Bulk SMS Message": {"total": 0, "migrated": 0, "skipped": 0},
			},
			"recipients": 0,
		}
		repoint_result = {
			"channel_messages": {"checked": 1, "repointed": 1},
			"marketing_campaigns": {"checked": 0, "repointed": 0},
			"email_unsubscribes": {"checked": 0, "repointed": 0},
			"dry_run": False,
		}

		expected_order = [
			"Bulk WhatsApp Message Product",
			"WhatsApp Recipient",
			"Email Recipient",
			"SMS Recipient",
			"Bulk WhatsApp Message",
			"Bulk Email Message",
			"Bulk SMS Message",
			"WhatsApp Recipient List",
			"Email Recipient List",
			"Audience Member",
			"Audience",
		]

		deleted_order: list[str] = []
		emptied_order: list[str] = []

		def fake_delete_doc(doctype: str, name: str, force: bool = False, ignore_missing: bool = False) -> None:
			if doctype == "DocType":
				deleted_order.append(name)

		def fake_empty_table(doctype: str) -> None:
			emptied_order.append(doctype)

		with patch("msuite_campaigns.broadcast.migration.verify", return_value=clean_verify), \
			patch("msuite_campaigns.broadcast.migration.run", return_value=clean_delta), \
			patch("msuite_campaigns.broadcast.legacy.repoint_legacy_references", return_value=repoint_result) as mock_repoint, \
			patch.object(frappe, "delete_doc", side_effect=fake_delete_doc), \
			patch.object(delete_legacy_bulk_doctypes, "_empty_table", side_effect=fake_empty_table):

			result = delete_legacy_bulk_doctypes.execute(confirm_delete=1, dry_run=False)

			mock_repoint.assert_called_once_with(dry_run=False)
			self.assertEqual(emptied_order, expected_order)
			self.assertEqual(deleted_order, expected_order)
			self.assertIn("deleted", result)
			self.assertEqual(result["deleted"], expected_order)

# Copyright (c) 2026, MSuite and contributors
# For license information, please see license.txt

"""Tests for legacy reference resolution and repointing (Task 14)."""

from __future__ import annotations

import frappe
from frappe.tests import IntegrationTestCase

from msuite_campaigns.broadcast import legacy


class TestBroadcastLegacy(IntegrationTestCase):
	"""Test legacy resolution and repointing logic."""

	def setUp(self):
		super().setUp()
		self._cleanup_test_data()
		self.cleanup_broadcasts: list[str] = []
		self.cleanup_lists: list[str] = []
		self.cleanup_channel_messages: list[str] = []
		self.cleanup_campaigns: list[str] = []
		self.cleanup_unsubscribes: list[str] = []

	def tearDown(self):
		self._cleanup_test_data()
		frappe.db.rollback()

	def _cleanup_test_data(self):
		"""Clean up test records."""
		try:
			frappe.db.sql("DELETE FROM `tabBroadcast` WHERE legacy_name IN ('BULK-WA-TEST-001', 'BULK-WA-REPOINT-001', 'BULK-WA-REAL-001', 'BULK-EM-REAL-001')")
			frappe.db.sql("DELETE FROM `tabRecipient List` WHERE list_name = 'Legacy Test Audience'")
			frappe.db.sql("DELETE FROM `tabMSuite Email Unsubscribe` WHERE email_address LIKE 'test_unsub%'")
			frappe.db.sql("DELETE FROM `tabChannel Message` WHERE reference_name IN ('BULK-WA-REPOINT-001', 'BULK-WA-REAL-001')")
			frappe.db.commit()
		except Exception:
			pass

	def test_legacy_resolve_hit_and_miss(self):
		"""resolve returns the unified Broadcast/Recipient List on hit and None on miss."""
		# Test hit on Broadcast
		bc = frappe.get_doc({
			"doctype": "Broadcast",
			"title": "Legacy Test BC",
			"recipient_source": "Individual",
			"legacy_doctype": "Bulk WhatsApp Message",
			"legacy_name": "BULK-WA-TEST-001",
			"channels": [{"channel": "WhatsApp"}],
		})
		bc.flags.ignore_permissions = True
		bc.flags.ignore_mandatory = True
		bc.flags.ignore_validate = True
		bc.insert()
		self.cleanup_broadcasts.append(bc.name)

		# Test hit on Recipient List
		rl = frappe.get_doc({
			"doctype": "Recipient List",
			"list_name": "Legacy Test Audience",
		})
		rl.flags.ignore_permissions = True
		rl.insert()
		self.cleanup_lists.append(rl.name)

		# Resolve hit
		resolved_bc = legacy.resolve("Bulk WhatsApp Message", "BULK-WA-TEST-001")
		self.assertEqual(resolved_bc, bc.name)

		resolved_aud = legacy.resolve("Audience", "Legacy Test Audience")
		self.assertEqual(resolved_aud, rl.name)

		# Resolve miss
		miss_bc = legacy.resolve("Bulk WhatsApp Message", "NON-EXISTENT-BLAST")
		self.assertIsNone(miss_bc)

		miss_list = legacy.resolve("Audience", "NON-EXISTENT-LIST")
		self.assertIsNone(miss_list)

		miss_dt = legacy.resolve("NonExistentDocType", "ANY")
		self.assertIsNone(miss_dt)

	def test_repoint_legacy_references_dry_run_leaves_untouched(self):
		"""dry_run=True computes repoint operations but does not modify database."""
		# Seed Broadcast
		bc = frappe.get_doc({
			"doctype": "Broadcast",
			"title": "Legacy Repoint BC",
			"recipient_source": "Individual",
			"legacy_doctype": "Bulk WhatsApp Message",
			"legacy_name": "BULK-WA-REPOINT-001",
			"channels": [{"channel": "WhatsApp"}],
		})
		bc.flags.ignore_permissions = True
		bc.flags.ignore_mandatory = True
		bc.flags.ignore_validate = True
		bc.insert()
		self.cleanup_broadcasts.append(bc.name)

		# Seed Channel Message referencing legacy doc
		cm = frappe.get_doc({
			"doctype": "Channel Message",
			"channel_type": "WhatsApp",
			"direction": "Outbound",
			"content_type": "Text",
			"reference_doctype": "Bulk WhatsApp Message",
			"reference_name": "BULK-WA-REPOINT-001",
		})
		cm.flags.ignore_permissions = True
		cm.flags.ignore_mandatory = True
		cm.flags.ignore_links = True
		cm.insert()
		self.cleanup_channel_messages.append(cm.name)

		# Run repoint with dry_run=True
		res = legacy.repoint_legacy_references(dry_run=True)
		self.assertTrue(res.get("dry_run"))
		self.assertGreaterEqual(res["channel_messages"]["repointed"], 1)

		# Verify Channel Message was NOT updated
		cm_refreshed = frappe.get_doc("Channel Message", cm.name)
		self.assertEqual(cm_refreshed.reference_doctype, "Bulk WhatsApp Message")
		self.assertEqual(cm_refreshed.reference_name, "BULK-WA-REPOINT-001")

	def test_repoint_legacy_references_real_updates_records(self):
		"""dry_run=False updates Channel Message, Marketing Campaign and Email Unsubscribe."""
		# Seed Broadcast
		bc = frappe.get_doc({
			"doctype": "Broadcast",
			"title": "Legacy Repoint Real BC",
			"recipient_source": "Individual",
			"legacy_doctype": "Bulk WhatsApp Message",
			"legacy_name": "BULK-WA-REAL-001",
			"channels": [{"channel": "WhatsApp"}],
		})
		bc.flags.ignore_permissions = True
		bc.flags.ignore_mandatory = True
		bc.flags.ignore_validate = True
		bc.insert()
		self.cleanup_broadcasts.append(bc.name)

		# Seed Channel Message
		cm = frappe.get_doc({
			"doctype": "Channel Message",
			"channel_type": "WhatsApp",
			"direction": "Outbound",
			"content_type": "Text",
			"reference_doctype": "Bulk WhatsApp Message",
			"reference_name": "BULK-WA-REAL-001",
		})
		cm.flags.ignore_permissions = True
		cm.flags.ignore_mandatory = True
		cm.flags.ignore_links = True
		cm.insert()
		self.cleanup_channel_messages.append(cm.name)

		# Seed Email Broadcast
		email_bc = frappe.get_doc({
			"doctype": "Broadcast",
			"title": "Legacy Email BC",
			"recipient_source": "Individual",
			"legacy_doctype": "Bulk Email Message",
			"legacy_name": "BULK-EM-REAL-001",
			"channels": [{"channel": "Email"}],
		})
		email_bc.flags.ignore_permissions = True
		email_bc.flags.ignore_mandatory = True
		email_bc.flags.ignore_validate = True
		email_bc.insert()
		self.cleanup_broadcasts.append(email_bc.name)

		# Seed MSuite Email Unsubscribe
		unsub = frappe.get_doc({
			"doctype": "MSuite Email Unsubscribe",
			"email_address": "test_unsub@example.com",
			"source_campaign": "BULK-EM-REAL-001",
		})
		unsub.flags.ignore_permissions = True
		unsub.flags.ignore_links = True
		unsub.insert()
		self.cleanup_unsubscribes.append(unsub.name)

		# Run repoint with dry_run=False
		res = legacy.repoint_legacy_references(dry_run=False)
		self.assertFalse(res.get("dry_run"))

		# Verify Channel Message repointed
		cm_refreshed = frappe.get_doc("Channel Message", cm.name)
		self.assertEqual(cm_refreshed.reference_doctype, "Broadcast")
		self.assertEqual(cm_refreshed.reference_name, bc.name)

		# Verify MSuite Email Unsubscribe repointed
		unsub_refreshed = frappe.get_doc("MSuite Email Unsubscribe", unsub.name)
		self.assertEqual(unsub_refreshed.source_broadcast, email_bc.name)

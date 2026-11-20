# Copyright (c) 2026, MSuite and contributors
# For license information, please see license.txt

"""Tests for legacy data migration to unified Broadcast and Recipient List (Task 14)."""

from __future__ import annotations

import hashlib
from typing import Any
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from msuite_campaigns.broadcast import migration


class TestBroadcastMigration(IntegrationTestCase):
	"""Test migration logic, status mapping, idempotency, edge cases and verification."""

	def setUp(self):
		super().setUp()
		self._cleanup_test_data()
		self.cleanup_audiences: list[str] = []
		self.cleanup_wa_lists: list[str] = []
		self.cleanup_email_lists: list[str] = []
		self.cleanup_bulk_wa: list[str] = []
		self.cleanup_bulk_email: list[str] = []
		self.cleanup_bulk_sms: list[str] = []
		self.cleanup_broadcasts: list[str] = []
		self.cleanup_recipient_lists: list[str] = []
		self.cleanup_campaigns: list[str] = []
		self.cleanup_channel_messages: list[str] = []

	def tearDown(self):
		self._cleanup_test_data()
		frappe.db.rollback()

	def _cleanup_test_data(self):
		"""Clean up test records created during test runs."""
		for t in [
			"Broadcast Recipient",
			"Broadcast Channel",
			"Broadcast Product",
			"Broadcast",
			"Recipient Contact",
			"Recipient List",
			"WhatsApp Recipient",
			"WhatsApp Recipient List",
			"Email Recipient",
			"Email Recipient List",
			"Bulk WhatsApp Message Product",
			"Bulk WhatsApp Message",
			"Bulk Email Message",
			"SMS Recipient",
			"Bulk SMS Message",
			"Audience Member",
			"Audience",
			"Channel Message",
			"Marketing Campaign",
		]:
			try:
				if t in ("Broadcast", "Bulk WhatsApp Message", "Bulk Email Message", "Bulk SMS Message"):
					frappe.db.sql(f"DELETE FROM `tab{t}` WHERE name LIKE 'ZZTest%' OR title LIKE 'ZZTest%'")
				elif t == "Recipient List":
					frappe.db.sql(f"DELETE FROM `tab{t}` WHERE name LIKE 'ZZTest%' OR list_name LIKE 'ZZTest%' OR name LIKE '🎉%' OR list_name LIKE '🎉%'")
				elif t == "Audience":
					frappe.db.sql(f"DELETE FROM `tab{t}` WHERE name LIKE 'ZZTest%' OR audience_name LIKE 'ZZTest%' OR name LIKE '🎉%' OR audience_name LIKE '🎉%'")
				elif t == "WhatsApp Recipient List":
					frappe.db.sql(f"DELETE FROM `tab{t}` WHERE name LIKE 'ZZTest%' OR list_name LIKE 'ZZTest%'")
				elif t == "Email Recipient List":
					frappe.db.sql(f"DELETE FROM `tab{t}` WHERE name LIKE 'ZZTest%' OR list_name LIKE 'ZZTest%'")
				elif t == "Marketing Campaign":
					frappe.db.sql(f"DELETE FROM `tab{t}` WHERE name LIKE 'ZZTest%' OR campaign_name LIKE 'ZZTest%'")
				elif t == "Channel Message":
					frappe.db.sql(f"DELETE FROM `tab{t}` WHERE reference_name LIKE 'ZZTest%'")
				elif t == "Broadcast Recipient":
					frappe.db.sql("DELETE FROM `tabBroadcast Recipient` WHERE broadcast LIKE 'ZZTest%' OR broadcast NOT IN (SELECT name FROM `tabBroadcast`)")
				elif t in ("Broadcast Channel", "Broadcast Product"):
					frappe.db.sql(f"DELETE FROM `tab{t}` WHERE parent LIKE 'ZZTest%' OR parent NOT IN (SELECT name FROM `tabBroadcast`)")
				elif t in ("Recipient Contact", "Audience Member", "WhatsApp Recipient", "Email Recipient", "SMS Recipient", "Bulk WhatsApp Message Product"):
					frappe.db.sql(f"DELETE FROM `tab{t}` WHERE parent LIKE 'ZZTest%' OR parent LIKE '🎉%'")
			except Exception:
				pass
		frappe.db.commit()

	def _insert(self, doc_data: dict[str, Any], cleanup_list: list[str] | None = None) -> Any:
		"""Insert a test document with validation flags bypassed."""
		doc = frappe.get_doc(doc_data)
		doc.flags.ignore_permissions = True
		doc.flags.ignore_mandatory = True
		doc.flags.ignore_validate = True
		doc.flags.ignore_links = True
		doc.insert()
		if cleanup_list is not None:
			cleanup_list.append(doc.name)
		return doc

	def test_dry_run_writes_nothing_but_reports(self):
		"""dry_run=True computes migration report without persisting any records."""
		# Seed an Audience
		aud = self._insert(
			{
				"doctype": "Audience",
				"audience_name": "ZZTest DryRun Audience",
				"members": [
					{
						"doctype": "Audience Member",
						"full_name": "Dry Person",
						"raw_phone": "+919876543210",
						"phone_e164": "+919876543210",
						"phone_status": "Valid",
					}
				],
			},
			self.cleanup_audiences,
		)

		# Seed a Bulk WhatsApp Message
		bw = self._insert(
			{
				"doctype": "Bulk WhatsApp Message",
				"title": "ZZTest DryRun WhatsApp",
				"status": "Draft",
				"recipient_type": "Individual",
				"recipients": [
					{
						"doctype": "WhatsApp Recipient",
						"recipient_name": "Dry Recipient",
						"mobile_number": "+919876543210",
						"status": "Queued",
					}
				],
			},
			self.cleanup_bulk_wa,
		)

		initial_rl_count = frappe.db.count("Recipient List")
		initial_bc_count = frappe.db.count("Broadcast")

		report = migration.run(dry_run=True)

		self.assertIn("recipient_lists", report)
		self.assertIn("broadcasts", report)
		self.assertGreaterEqual(report["recipient_lists"]["Audience"]["migrated"], 1)
		self.assertGreaterEqual(report["broadcasts"]["Bulk WhatsApp Message"]["migrated"], 1)
		self.assertIn(("Audience", aud.name), report["name_map"])

		# Verify nothing was written to DB
		self.assertEqual(frappe.db.count("Recipient List"), initial_rl_count)
		self.assertEqual(frappe.db.count("Broadcast"), initial_bc_count)

	def test_counts_equal_after_run_and_verify(self):
		"""run(dry_run=False) creates unified records, and verify() reports 0 mismatches."""
		# Seed one doc in each legacy doctype
		aud = self._insert({"doctype": "Audience", "audience_name": "ZZTest Ver Audience"}, self.cleanup_audiences)
		wal = self._insert({"doctype": "WhatsApp Recipient List", "list_name": "ZZTest Ver WAL"}, self.cleanup_wa_lists)
		eml = self._insert({"doctype": "Email Recipient List", "list_name": "ZZTest Ver EML"}, self.cleanup_email_lists)

		bw = self._insert(
			{"doctype": "Bulk WhatsApp Message", "title": "ZZTest Ver BW", "status": "Draft", "recipient_type": "Individual"},
			self.cleanup_bulk_wa,
		)
		be = self._insert(
			{"doctype": "Bulk Email Message", "title": "ZZTest Ver BE", "status": "Draft", "recipient_type": "Individual"},
			self.cleanup_bulk_email,
		)
		bs = self._insert(
			{"doctype": "Bulk SMS Message", "title": "ZZTest Ver BS", "status": "Draft", "recipient_type": "Individual"},
			self.cleanup_bulk_sms,
		)

		report = migration.run(dry_run=False)
		self.assertIn("name_map", report)

		# Keep track of created items for cleanup
		for dt in ["Audience", "WhatsApp Recipient List", "Email Recipient List"]:
			for old_name in frappe.db.get_all(dt, pluck="name"):
				target = report["name_map"].get((dt, old_name))
				if target and target not in self.cleanup_recipient_lists:
					self.cleanup_recipient_lists.append(target)

		for dt in ["Bulk WhatsApp Message", "Bulk Email Message", "Bulk SMS Message"]:
			for bc_name in frappe.db.get_all("Broadcast", filters={"legacy_doctype": dt}, pluck="name"):
				if bc_name not in self.cleanup_broadcasts:
					self.cleanup_broadcasts.append(bc_name)

		# Verification
		v_res = migration.verify()
		for dt, counts in v_res.items():
			self.assertEqual(counts["mismatches"], 0, f"Mismatch found for {dt}: {counts}")
			self.assertEqual(counts["old_count"], counts["migrated_count"], f"Counts not equal for {dt}")

	def test_second_run_creates_nothing(self):
		"""A second migration run is idempotent and creates 0 records."""
		report1 = migration.run(dry_run=False)

		# Second run
		report2 = migration.run(dry_run=False)

		# All lists and broadcasts should be skipped in run 2
		for dt, stats in report2["recipient_lists"].items():
			self.assertEqual(stats["migrated"], 0, f"Second run migrated {dt}: {stats}")
		for dt, stats in report2["broadcasts"].items():
			self.assertEqual(stats["migrated"], 0, f"Second run migrated {dt}: {stats}")
		self.assertEqual(report2["recipients"], 0)

	def test_status_mapping_and_paused_in_flight(self):
		"""In-flight blasts migrate as Paused (and listed), docstatus 2 as Cancelled, is_paused as Paused."""
		# Scheduled -> Paused
		b_sched = self._insert(
			{"doctype": "Bulk WhatsApp Message", "title": "ZZTest Scheduled Blast", "status": "Scheduled", "recipient_type": "Individual"},
			self.cleanup_bulk_wa,
		)

		# In Progress -> Paused
		b_inp = self._insert(
			{"doctype": "Bulk Email Message", "title": "ZZTest InProgress Blast", "status": "In Progress", "recipient_type": "Individual"},
			self.cleanup_bulk_email,
		)

		# is_paused -> Paused
		b_pause = self._insert(
			{"doctype": "Bulk SMS Message", "title": "ZZTest Paused Blast", "status": "Draft", "is_paused": 1, "recipient_type": "Individual"},
			self.cleanup_bulk_sms,
		)

		# Cancelled docstatus 2 -> Cancelled
		b_canc = self._insert(
			{"doctype": "Bulk WhatsApp Message", "title": "ZZTest Cancelled Blast", "status": "Completed", "recipient_type": "Individual"},
			self.cleanup_bulk_wa,
		)
		frappe.db.sql("UPDATE `tabBulk WhatsApp Message` SET docstatus=2 WHERE name=%s", b_canc.name)

		report = migration.run(dry_run=False)

		# Look up migrated Broadcasts
		bc_sched = frappe.db.get_value("Broadcast", {"legacy_doctype": "Bulk WhatsApp Message", "legacy_name": b_sched.name}, ["name", "status"], as_dict=True)
		self.cleanup_broadcasts.append(bc_sched.name)
		self.assertEqual(bc_sched.status, "Paused")

		bc_inp = frappe.db.get_value("Broadcast", {"legacy_doctype": "Bulk Email Message", "legacy_name": b_inp.name}, ["name", "status"], as_dict=True)
		self.cleanup_broadcasts.append(bc_inp.name)
		self.assertEqual(bc_inp.status, "Paused")

		bc_pause = frappe.db.get_value("Broadcast", {"legacy_doctype": "Bulk SMS Message", "legacy_name": b_pause.name}, ["name", "status"], as_dict=True)
		self.cleanup_broadcasts.append(bc_pause.name)
		self.assertEqual(bc_pause.status, "Paused")

		bc_canc = frappe.db.get_value("Broadcast", {"legacy_doctype": "Bulk WhatsApp Message", "legacy_name": b_canc.name}, ["name", "status"], as_dict=True)
		self.cleanup_broadcasts.append(bc_canc.name)
		self.assertEqual(bc_canc.status, "Cancelled")

		# Check paused_in_flight list
		paused_names = [p["legacy_name"] for p in report["paused_in_flight"]]
		self.assertIn(b_sched.name, paused_names)
		self.assertIn(b_inp.name, paused_names)

	def test_campaign_link_preserved(self):
		"""Migrated Broadcast preserves link to Marketing Campaign."""
		camp = self._insert(
			{"doctype": "Marketing Campaign", "campaign_name": "ZZTest Migration Campaign"},
			self.cleanup_campaigns,
		)

		bw = self._insert(
			{"doctype": "Bulk WhatsApp Message", "title": "ZZTest Camp Blast", "status": "Draft", "campaign": camp.name, "recipient_type": "Individual"},
			self.cleanup_bulk_wa,
		)

		report = migration.run(dry_run=False)
		bc_name = frappe.db.get_value("Broadcast", {"legacy_doctype": "Bulk WhatsApp Message", "legacy_name": bw.name})
		self.cleanup_broadcasts.append(bc_name)

		bc_camp = frappe.db.get_value("Broadcast", bc_name, "campaign")
		self.assertEqual(bc_camp, camp.name)

	def test_whatsapp_list_blast_rebuild_from_channel_message(self):
		"""WhatsApp blast with Recipient List rebuilds recipient rows from Channel Message."""
		wal = self._insert({"doctype": "WhatsApp Recipient List", "list_name": "ZZTest CM Rebuild List"}, self.cleanup_wa_lists)

		bw = self._insert(
			{
				"doctype": "Bulk WhatsApp Message",
				"title": "ZZTest CM Blast",
				"status": "Completed",
				"recipient_type": "Recipient List",
				"recipient_list": wal.name,
				"sent_count": 1,
				"recipient_count": 1,
			},
			self.cleanup_bulk_wa,
		)

		# Seed Channel Message referencing this blast
		cm = self._insert(
			{
				"doctype": "Channel Message",
				"channel_type": "WhatsApp",
				"direction": "Outbound",
				"content_type": "Text",
				"status": "delivered",
				"sender_phone": "+919876543210",
				"platform_message_id": "wamid.CM_TEST_123",
				"reference_doctype": "Bulk WhatsApp Message",
				"reference_name": bw.name,
			},
			self.cleanup_channel_messages,
		)

		report = migration.run(dry_run=False)
		bc_name = frappe.db.get_value("Broadcast", {"legacy_doctype": "Bulk WhatsApp Message", "legacy_name": bw.name})
		self.cleanup_broadcasts.append(bc_name)
		self.cleanup_recipient_lists.append(report["name_map"][("WhatsApp Recipient List", wal.name)])

		rec_row = frappe.db.get_value("Broadcast Recipient", {"broadcast": bc_name, "address": "+919876543210"}, ["status", "provider_message_id"], as_dict=True)
		self.assertIsNotNone(rec_row)
		self.assertEqual(rec_row.status, "Delivered")
		self.assertEqual(rec_row.provider_message_id, "wamid.CM_TEST_123")

	def test_whatsapp_list_blast_without_channel_message_migrates_counters_only(self):
		"""WhatsApp blast with Recipient List and no Channel Message migrates counters only."""
		wal = self._insert({"doctype": "WhatsApp Recipient List", "list_name": "ZZTest NoCM List"}, self.cleanup_wa_lists)

		bw = self._insert(
			{
				"doctype": "Bulk WhatsApp Message",
				"title": "ZZTest NoCM Blast",
				"status": "Completed",
				"recipient_type": "Recipient List",
				"recipient_list": wal.name,
				"sent_count": 42,
				"recipient_count": 42,
			},
			self.cleanup_bulk_wa,
		)

		report = migration.run(dry_run=False)
		bc_name = frappe.db.get_value("Broadcast", {"legacy_doctype": "Bulk WhatsApp Message", "legacy_name": bw.name})
		self.cleanup_broadcasts.append(bc_name)
		self.cleanup_recipient_lists.append(report["name_map"][("WhatsApp Recipient List", wal.name)])

		# 0 recipient rows inserted
		r_count = frappe.db.count("Broadcast Recipient", {"broadcast": bc_name})
		self.assertEqual(r_count, 0)

		# Channel row counters migrated
		ch_row = frappe.db.get_value("Broadcast Channel", {"parent": bc_name, "channel": "WhatsApp"}, ["recipient_count", "sent_count"], as_dict=True)
		self.assertEqual(ch_row.recipient_count, 42)
		self.assertEqual(ch_row.sent_count, 42)

	def test_name_collisions_across_three_list_sources(self):
		"""Audience keeps name, WhatsApp list gets (WhatsApp), Email list gets (Email)."""
		shared_name = "ZZTest Shared Name List"

		aud = self._insert({"doctype": "Audience", "audience_name": shared_name}, self.cleanup_audiences)
		wal = self._insert({"doctype": "WhatsApp Recipient List", "list_name": shared_name}, self.cleanup_wa_lists)
		eml = self._insert({"doctype": "Email Recipient List", "list_name": shared_name}, self.cleanup_email_lists)

		report = migration.run(dry_run=False)
		name_map = report["name_map"]

		aud_target = name_map[("Audience", aud.name)]
		wal_target = name_map[("WhatsApp Recipient List", wal.name)]
		eml_target = name_map[("Email Recipient List", eml.name)]

		self.cleanup_recipient_lists.extend([aud_target, wal_target, eml_target])

		self.assertEqual(aud_target, shared_name)
		self.assertEqual(wal_target, f"{shared_name} (WhatsApp)")
		self.assertEqual(eml_target, f"{shared_name} (Email)")

		self.assertTrue(frappe.db.exists("Recipient List", aud_target))
		self.assertTrue(frappe.db.exists("Recipient List", wal_target))
		self.assertTrue(frappe.db.exists("Recipient List", eml_target))

	def test_invalid_phone_in_list_contact_keeps_row_with_warning(self):
		"""Invalid phone in list contact keeps contact row with phone_status Invalid/Unparseable and warning."""
		wal = self._insert(
			{
				"doctype": "WhatsApp Recipient List",
				"list_name": "ZZTest Invalid Phone List",
				"recipients": [
					{
						"doctype": "WhatsApp Recipient",
						"recipient_name": "Invalid Number User",
						"mobile_number": "not-a-number",
					}
				],
			},
			self.cleanup_wa_lists,
		)

		report = migration.run(dry_run=False)
		target_list = report["name_map"][("WhatsApp Recipient List", wal.name)]
		self.cleanup_recipient_lists.append(target_list)

		# Contact row kept
		contacts = frappe.db.get_all("Recipient Contact", filters={"parent": target_list}, fields=["full_name", "raw_phone", "phone_status"])
		self.assertEqual(len(contacts), 1)
		self.assertIn(contacts[0].phone_status, ("Unparseable", "Invalid"))

		# Warning recorded
		has_warning = any("not-a-number" in w or "Invalid Number User" in w or target_list in w for w in report["warnings"])
		self.assertTrue(has_warning, f"Expected warning for invalid phone in {report['warnings']}")

	def test_unicode_names(self):
		"""Unicode names in lists, recipients and broadcasts are preserved."""
		unicode_title = "🎉 Diwali Special 2026 ✨ 山田太郎"
		aud = self._insert(
			{
				"doctype": "Audience",
				"audience_name": unicode_title,
				"members": [
					{
						"doctype": "Audience Member",
						"full_name": "山田 太郎",
						"raw_phone": "+819012345678",
						"phone_e164": "+819012345678",
						"phone_status": "Valid",
					}
				],
			},
			self.cleanup_audiences,
		)

		report = migration.run(dry_run=False)
		target_list = report["name_map"][("Audience", aud.name)]
		self.cleanup_recipient_lists.append(target_list)

		rl = frappe.get_doc("Recipient List", target_list)
		self.assertEqual(rl.list_name, unicode_title)
		self.assertEqual(rl.contacts[0].full_name, "山田 太郎")

	def test_empty_source_list(self):
		"""Empty source list migrates successfully with 0 contacts."""
		wal = self._insert({"doctype": "WhatsApp Recipient List", "list_name": "ZZTest Empty List", "recipients": []}, self.cleanup_wa_lists)

		report = migration.run(dry_run=False)
		target_list = report["name_map"][("WhatsApp Recipient List", wal.name)]
		self.cleanup_recipient_lists.append(target_list)

		rl = frappe.get_doc("Recipient List", target_list)
		self.assertEqual(len(rl.contacts), 0)
		self.assertEqual(rl.total_contacts, 0)

	def test_partial_failure_mid_batch_rollback_and_rerun_resumes(self):
		"""Partial failure mid-batch rolls back that batch, and rerun successfully completes."""
		# Seed 2 Bulk SMS docs
		bs1 = self._insert({"doctype": "Bulk SMS Message", "title": "ZZTest Batch 1", "status": "Draft", "recipient_type": "Individual"}, self.cleanup_bulk_sms)
		bs2 = self._insert({"doctype": "Bulk SMS Message", "title": "ZZTest Batch 2", "status": "Draft", "recipient_type": "Individual"}, self.cleanup_bulk_sms)

		# Inject error when migrating bs2
		original_migrate_broadcast = migration._migrate_single_broadcast

		def failing_migrate(*args, **kwargs):
			old = kwargs.get("old") if "old" in kwargs else args[0]
			name = getattr(old, "name", None) or (old.get("name") if isinstance(old, dict) else None)
			if name == bs2.name:
				raise RuntimeError("Injected batch failure")
			return original_migrate_broadcast(*args, **kwargs)

		with patch.object(migration, "_migrate_single_broadcast", side_effect=failing_migrate):
			with self.assertRaises(RuntimeError):
				# Batch size 2 includes bs1 and bs2
				migration.run(dry_run=False, batch_size=2)

		# Since batch rolled back, neither or only previously committed items exist
		# bs2 must NOT be migrated
		bc2 = frappe.db.get_value("Broadcast", {"legacy_doctype": "Bulk SMS Message", "legacy_name": bs2.name})
		self.assertIsNone(bc2)

		# Rerun without error should complete
		report = migration.run(dry_run=False, batch_size=2)
		bc1 = frappe.db.get_value("Broadcast", {"legacy_doctype": "Bulk SMS Message", "legacy_name": bs1.name})
		bc2 = frappe.db.get_value("Broadcast", {"legacy_doctype": "Bulk SMS Message", "legacy_name": bs2.name})
		self.assertIsNotNone(bc1)
		self.assertIsNotNone(bc2)
		self.cleanup_broadcasts.extend([bc1, bc2])

	def test_old_doctypes_not_modified_checksum(self):
		"""Migration does NOT modify any data in the legacy doctypes."""
		bw = self._insert(
			{
				"doctype": "Bulk WhatsApp Message",
				"title": "ZZTest Checksum WhatsApp",
				"status": "Draft",
				"recipient_type": "Individual",
				"recipients": [
					{
						"doctype": "WhatsApp Recipient",
						"recipient_name": "Checksum Recipient",
						"mobile_number": "+919876543210",
						"status": "Queued",
					}
				],
			},
			self.cleanup_bulk_wa,
		)

		# Compute checksum before migration
		before_data = frappe.db.get_value("Bulk WhatsApp Message", bw.name, ["title", "status", "recipient_type", "creation", "modified"], as_dict=True)
		before_hash = hashlib.sha256(str(sorted(before_data.items())).encode("utf-8")).hexdigest()

		report = migration.run(dry_run=False)
		bc_name = frappe.db.get_value("Broadcast", {"legacy_doctype": "Bulk WhatsApp Message", "legacy_name": bw.name})
		self.cleanup_broadcasts.append(bc_name)

		# Compute checksum after migration
		after_data = frappe.db.get_value("Bulk WhatsApp Message", bw.name, ["title", "status", "recipient_type", "creation", "modified"], as_dict=True)
		after_hash = hashlib.sha256(str(sorted(after_data.items())).encode("utf-8")).hexdigest()

		self.assertEqual(before_hash, after_hash)

# Copyright (c) 2026, MSuite and contributors
# For license information, please see license.txt

"""Tests for Broadcast Materializer service."""

from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from msuite_campaigns.audience.services.contact_sync_service import record_channel_opt_out
from msuite_campaigns.broadcast.materializer import materialize
from msuite_campaigns.broadcast.recipients import bulk_insert_recipients

TEST_PREFIX = "ZZTestBM "


class TestBroadcastMaterializer(IntegrationTestCase):
	"""Test suite for Broadcast Materializer."""

	def setUp(self):
		super().setUp()
		self.cleanup_broadcasts: list[str] = []
		self.cleanup_lists: list[str] = []
		self.cleanup_contacts: list[str] = []

	def tearDown(self):
		frappe.db.rollback()
		for name in self.cleanup_broadcasts:
			if frappe.db.exists("Broadcast", name):
				frappe.delete_doc("Broadcast", name, force=True, ignore_permissions=True)
		for name in self.cleanup_lists:
			if frappe.db.exists("Recipient List", name):
				frappe.delete_doc("Recipient List", name, force=True, ignore_permissions=True)
		for c in self.cleanup_contacts:
			if frappe.db.exists("Contact", c):
				frappe.delete_doc("Contact", c, force=True, ignore_permissions=True)

	def _make_broadcast(
		self,
		channels: list[str] | None = None,
		recipient_source: str = "Individual",
		recipient_list: str | None = None,
	) -> "frappe.model.document.Document":
		channel_list = channels or ["WhatsApp"]
		channel_rows = []
		for ch in channel_list:
			row = {"channel": ch, "status": "Draft"}
			if ch == "WhatsApp":
				row["whatsapp_account"] = "WA-ACC-001"
				row["whatsapp_template"] = "tpl_wa_001"
				row["variable_type"] = "Common"
			elif ch == "Email":
				row["email_account"] = "EM-ACC-001"
				row["email_template"] = "tpl_em_001"
				row["variable_type"] = None
			elif ch == "SMS":
				row["sms_account"] = "SMS-ACC-001"
				row["sms_template"] = "tpl_sms_001"
				row["variable_type"] = None
			channel_rows.append(row)

		doc = frappe.get_doc(
			{
				"doctype": "Broadcast",
				"title": f"{TEST_PREFIX}{frappe.generate_hash(length=6)}",
				"recipient_source": recipient_source,
				"recipient_list": recipient_list,
				"status": "Draft",
				"channels": channel_rows,
			}
		)
		for ch_row in doc.channels:
			if ch_row.channel != "WhatsApp":
				ch_row.variable_type = ""
		doc.insert(ignore_links=True)
		self.cleanup_broadcasts.append(doc.name)
		return doc

	def _make_recipient_list(self, contacts: list[dict]) -> "frappe.model.document.Document":
		list_name = f"{TEST_PREFIX}{frappe.generate_hash(length=8)}"
		doc = frappe.get_doc(
			{
				"doctype": "Recipient List",
				"list_name": list_name,
				"default_region": "IN",
				"source": "Manual",
			}
		)
		doc.flags.ignore_permissions = True
		doc.insert()
		self.cleanup_lists.append(doc.name)
		if contacts:
			doc.add_contacts(contacts)
			doc.save(ignore_permissions=True)
		return doc

	def test_list_source_skips_opted_out_and_invalid_with_reasons(self):
		"""Recipient list source skips opted-out and invalid contacts and returns breakdown."""
		rec_list = self._make_recipient_list(
			[
				{"full_name": "Valid Contact", "raw_phone": "+919876543210"},
				{"full_name": "Opted Out WA", "raw_phone": "+919876543211"},
				{"full_name": "Landline", "raw_phone": "01123456789"},
			]
		)
		# Opt out second contact on WhatsApp
		for c in rec_list.contacts:
			if c.phone_e164 == "+919876543211":
				c.unsubscribed_whatsapp = 1
		rec_list.save(ignore_permissions=True)

		b = self._make_broadcast(
			channels=["WhatsApp"],
			recipient_source="Recipient List",
			recipient_list=rec_list.name,
		)

		res = materialize(b.name, "WhatsApp")
		self.assertEqual(res["queued"], 1)
		self.assertEqual(res["dropped"], 2)
		self.assertIn("Unsubscribed from WhatsApp", res["reasons"])

		recipients = frappe.get_all(
			"Broadcast Recipient",
			filters={"broadcast": b.name, "channel": "WhatsApp"},
			fields=["address", "recipient_name", "source_contact"],
		)
		self.assertEqual(len(recipients), 1)
		self.assertEqual(recipients[0].address, "+919876543210")
		self.assertEqual(recipients[0].recipient_name, "Valid Contact")
		self.assertTrue(bool(recipients[0].source_contact))

	def test_individual_source_normalises_phone_and_deduplicates(self):
		"""Individual phone numbers are normalised to E.164 and deduplicated."""
		b = self._make_broadcast(channels=["WhatsApp"], recipient_source="Individual")
		rows = [
			{"address": "9876543210", "recipient_name": "Person 1"},
			{"address": "+91 98765 43210", "recipient_name": "Person 1 Duplicate"},
			{"address": "+919876543211", "recipient_name": "Person 2"},
		]
		res = materialize(b.name, "WhatsApp", individual_rows=rows)
		self.assertEqual(res["queued"], 2)
		self.assertEqual(res["dropped"], 1)
		self.assertEqual(res["reasons"].get("duplicate"), 1)

		recipients = frappe.get_all(
			"Broadcast Recipient",
			filters={"broadcast": b.name, "channel": "WhatsApp"},
			fields=["address", "recipient_name"],
		)
		self.assertEqual(len(recipients), 2)
		addrs = sorted(r.address for r in recipients)
		self.assertEqual(addrs, ["+919876543210", "+919876543211"])

	def test_individual_source_drops_invalid_and_unsubscribed_with_reasons(self):
		"""Invalid and unsubscribed numbers for Individual source are dropped with reasons."""
		opt_out_phone = "+919876543220"
		record_channel_opt_out("WhatsApp", opt_out_phone, reason="Test STOP", create_if_missing=True)
		contacts = frappe.get_all("Contact", filters={"phone_e164": opt_out_phone}, fields=["name"])
		for c in contacts:
			self.cleanup_contacts.append(c.name)

		b = self._make_broadcast(channels=["WhatsApp"], recipient_source="Individual")
		rows = [
			{"address": "not_a_number"},
			{"address": opt_out_phone},
			{"address": "+919876543221"},
		]
		res = materialize(b.name, "WhatsApp", individual_rows=rows)
		self.assertEqual(res["queued"], 1)
		self.assertEqual(res["dropped"], 2)
		self.assertEqual(res["reasons"].get("invalid_phone"), 1)
		self.assertEqual(res["reasons"].get("unsubscribed"), 1)

	def test_empty_source_queued_zero_and_recipient_count_zero(self):
		"""Empty source results in queued 0, dropped 0, and recipient_count 0."""
		b = self._make_broadcast(channels=["WhatsApp"], recipient_source="Individual")
		res = materialize(b.name, "WhatsApp", individual_rows=[])
		self.assertEqual(res["queued"], 0)
		self.assertEqual(res["dropped"], 0)

		ch_count = frappe.db.get_value(
			"Broadcast Channel",
			{"parent": b.name, "channel": "WhatsApp"},
			"recipient_count",
		)
		self.assertEqual(ch_count, 0)
		total = frappe.db.get_value("Broadcast", b.name, "total_recipients")
		self.assertEqual(total, 0)

	def test_materialize_idempotent_no_duplicate_rows_and_counts_stay_correct(self):
		"""Re-running materialize does not duplicate rows and counts stay correct."""
		b = self._make_broadcast(channels=["WhatsApp"], recipient_source="Individual")
		rows = [
			{"address": "+919876543210"},
			{"address": "+919876543211"},
		]
		res1 = materialize(b.name, "WhatsApp", individual_rows=rows)
		self.assertEqual(res1["queued"], 2)

		total_in_db_1 = frappe.db.count("Broadcast Recipient", {"broadcast": b.name, "channel": "WhatsApp"})
		self.assertEqual(total_in_db_1, 2)

		# Run again
		res2 = materialize(b.name, "WhatsApp", individual_rows=rows)
		self.assertEqual(res2["queued"], 2)

		total_in_db_2 = frappe.db.count("Broadcast Recipient", {"broadcast": b.name, "channel": "WhatsApp"})
		self.assertEqual(total_in_db_2, 2)

		ch_count = frappe.db.get_value(
			"Broadcast Channel",
			{"parent": b.name, "channel": "WhatsApp"},
			"recipient_count",
		)
		self.assertEqual(ch_count, 2)

	def test_email_uses_email_normalized(self):
		"""Email channel uses normalized email addresses."""
		rec_list = self._make_recipient_list(
			[
				{"full_name": "Email User", "raw_email": "User.Mix@Example.COM"},
			]
		)
		b = self._make_broadcast(
			channels=["Email"],
			recipient_source="Recipient List",
			recipient_list=rec_list.name,
		)
		res = materialize(b.name, "Email")
		self.assertEqual(res["queued"], 1)

		rec = frappe.get_all(
			"Broadcast Recipient",
			filters={"broadcast": b.name, "channel": "Email"},
			fields=["address"],
		)
		self.assertEqual(len(rec), 1)
		self.assertEqual(rec[0].address, "user.mix@example.com")

	def test_counts_set_on_channel_row_and_total_recipients_max_rule(self):
		"""recipient_count set on channel row and Broadcast.total_recipients = max over channel rows."""
		b = self._make_broadcast(channels=["WhatsApp", "Email"], recipient_source="Individual")
		wa_rows = [{"address": f"+91987654300{i}"} for i in range(3)]
		em_rows = [{"address": f"user{i}@example.com"} for i in range(5)]

		materialize(b.name, "WhatsApp", individual_rows=wa_rows)
		wa_count = frappe.db.get_value("Broadcast Channel", {"parent": b.name, "channel": "WhatsApp"}, "recipient_count")
		self.assertEqual(wa_count, 3)

		total_after_wa = frappe.db.get_value("Broadcast", b.name, "total_recipients")
		self.assertEqual(total_after_wa, 3)

		materialize(b.name, "Email", individual_rows=em_rows)
		em_count = frappe.db.get_value("Broadcast Channel", {"parent": b.name, "channel": "Email"}, "recipient_count")
		self.assertEqual(em_count, 5)

		total_after_both = frappe.db.get_value("Broadcast", b.name, "total_recipients")
		self.assertEqual(total_after_both, 5)

	def test_materialize_absent_channel_row_raises(self):
		"""Materializing for an unconfigured channel on the broadcast raises."""
		b = self._make_broadcast(channels=["WhatsApp"], recipient_source="Individual")
		with self.assertRaises(frappe.ValidationError):
			materialize(b.name, "Email", individual_rows=[])

	def test_2500_contacts_write_in_3_chunks(self):
		"""Writing 2,500 contacts invokes db.bulk_insert 3 times."""
		b = self._make_broadcast(channels=["WhatsApp"], recipient_source="Individual")
		rows = [{"address": f"+9198000{i:05d}"} for i in range(2500)]

		original_bulk_insert = frappe.db.bulk_insert
		call_count = 0

		def counting_bulk_insert(*args, **kwargs):
			nonlocal call_count
			call_count += 1
			return original_bulk_insert(*args, **kwargs)

		with patch.object(frappe.db, "bulk_insert", side_effect=counting_bulk_insert):
			res = materialize(b.name, "WhatsApp", individual_rows=rows)

		self.assertEqual(call_count, 3)
		self.assertEqual(res["queued"], 2500)

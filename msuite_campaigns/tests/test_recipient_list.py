# Copyright (c) 2026, MSuite and contributors
# For license information, please see license.txt

"""Tests for Recipient List and Recipient Contact doctypes.

Covers:
- Filtering reachability per channel (valid mobile, landline, email)
- Invalid phone excluded
- Empty phone_e164 excluded
- Opted-out per channel excluded
- Duplicate numbers/emails deduplicated in contacts_for
- Unicode names preserved
- Zero contacts returning empty list and empty reasons
- Rejection breakdown sums equal dropped count
- Permission denied for a role without access
- Import / sync stays under one query per chunk (chunking scalability)
- Opt-out / opt-in propagation to Recipient Contact
- Plan gate enforcement for Recipient List
"""

from __future__ import annotations

import random
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from msuite_campaigns.broadcast.constants import CHANNELS
from msuite_campaigns.audience.services.contact_sync_service import (
	record_channel_opt_in,
	record_channel_opt_out,
	sync_recipient_list_to_contacts,
)
from msuite_core.services.validation import email as email_validator
from msuite_core.services.validation import phone as phone_validator

TEST_PREFIX = "ZZTestRL "


class TestRecipientList(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		self.cleanup_lists: list[str] = []
		self.cleanup_contacts: list[str] = []
		self.cleanup_users: list[str] = []

	def tearDown(self):
		frappe.db.rollback()
		frappe.set_user("Administrator")
		for name in self.cleanup_lists:
			if frappe.db.exists("Recipient List", name):
				frappe.delete_doc("Recipient List", name, force=True, ignore_permissions=True)
		for c in self.cleanup_contacts:
			if frappe.db.exists("Contact", c):
				frappe.delete_doc("Contact", c, force=True, ignore_permissions=True)
		for u in self.cleanup_users:
			if frappe.db.exists("User", u):
				frappe.delete_doc("User", u, force=True, ignore_permissions=True)

	def _make_list(self, name_suffix: str = "", contacts: list[dict] | None = None) -> "frappe.model.document.Document":
		list_name = f"{TEST_PREFIX}{name_suffix or frappe.generate_hash(length=8)}"
		doc = frappe.get_doc({
			"doctype": "Recipient List",
			"list_name": list_name,
			"default_region": "IN",
			"source": "Manual",
		})
		doc.flags.ignore_permissions = True
		doc.insert()
		self.cleanup_lists.append(doc.name)
		if contacts:
			doc.add_contacts(contacts)
			doc.save()
		return doc

	def test_invalid_phone_excluded(self):
		"""Invalid or unparseable phones must be excluded from WhatsApp and SMS."""
		contacts = [
			{
				"full_name": "Valid Contact",
				"raw_phone": "9876543210",
				"raw_email": "valid@example.com",
			},
			{
				"full_name": "Invalid Contact",
				"raw_phone": "12345",
				"raw_email": "invalid_phone@example.com",
			},
			{
				"full_name": "Unparseable Contact",
				"raw_phone": "bad-number",
				"raw_email": "junk_phone@example.com",
			},
		]
		rl = self._make_list("InvalidPhoneTest", contacts)
		for c in rl.contacts:
			if c.full_name == "Invalid Contact":
				c.phone_status = phone_validator.INVALID
			elif c.full_name == "Unparseable Contact":
				c.phone_status = phone_validator.UNPARSEABLE
		rl.save()

		wa_contacts = rl.contacts_for("WhatsApp")
		sms_contacts = rl.contacts_for("SMS")

		self.assertEqual(len(wa_contacts), 1)
		self.assertEqual(wa_contacts[0]["full_name"], "Valid Contact")
		self.assertEqual(len(sms_contacts), 1)
		self.assertEqual(sms_contacts[0]["full_name"], "Valid Contact")

	def test_empty_phone_e164_excluded(self):
		"""Valid status but empty phone_e164 (data desync) must be excluded and explained."""
		contacts = [
			{
				"full_name": "Good",
				"raw_phone": "9876543210",
				"raw_email": "good@example.com",
			},
			{
				"full_name": "Desync",
				"raw_phone": "9812345678",
				"raw_email": "desync@example.com",
			},
		]
		rl = self._make_list("DesyncTest", contacts)
		desync = next(c for c in rl.contacts if c.full_name == "Desync")
		# Simulate data desync where phone_e164 was cleared directly in database
		frappe.db.set_value("Recipient Contact", desync.name, "phone_e164", "")
		rl.reload()

		wa_names = {c["full_name"] for c in rl.contacts_for("WhatsApp")}
		self.assertIn("Good", wa_names)
		self.assertNotIn("Desync", wa_names)

		reasons = rl.rejection_breakdown("WhatsApp")
		self.assertIn("No phone number", reasons)

	def test_opted_out_per_channel_excluded(self):
		"""Opting out of WhatsApp does not exclude from Email or SMS, and vice versa."""
		contacts = [
			{
				"full_name": "WA OptOut",
				"raw_phone": "9876543210",
				"raw_email": "wa_out@example.com",
			},
			{
				"full_name": "Email OptOut",
				"raw_phone": "9812345678",
				"raw_email": "email_out@example.com",
			},
			{
				"full_name": "SMS OptOut",
				"raw_phone": "9899887766",
				"raw_email": "sms_out@example.com",
			},
		]
		rl = self._make_list("OptOutTest", contacts)
		for c in rl.contacts:
			c.phone_status = phone_validator.VALID
			c.phone_type = "Mobile"
			c.email_status = "ok"
			if c.full_name == "WA OptOut":
				c.unsubscribed_whatsapp = 1
			elif c.full_name == "Email OptOut":
				c.unsubscribed_email = 1
			elif c.full_name == "SMS OptOut":
				c.unsubscribed_sms = 1
		rl.save()

		wa_names = {c["full_name"] for c in rl.contacts_for("WhatsApp")}
		email_names = {c["full_name"] for c in rl.contacts_for("Email")}
		sms_names = {c["full_name"] for c in rl.contacts_for("SMS")}

		self.assertNotIn("WA OptOut", wa_names)
		self.assertIn("WA OptOut", email_names)
		self.assertIn("WA OptOut", sms_names)

		self.assertNotIn("Email OptOut", email_names)
		self.assertIn("Email OptOut", wa_names)
		self.assertIn("Email OptOut", sms_names)

		self.assertNotIn("SMS OptOut", sms_names)
		self.assertIn("SMS OptOut", wa_names)
		self.assertIn("SMS OptOut", email_names)

	def test_duplicate_numbers_deduplicated_in_contacts_for(self):
		"""contacts_for must deduplicate addresses so a recipient is not messaged twice."""
		rl = self._make_list("DedupeTest")
		rl.append("contacts", {
			"full_name": "Alice 1",
			"raw_phone": "9876543210",
			"phone_e164": "+919876543210",
			"phone_status": phone_validator.VALID,
			"phone_type": "Mobile",
			"raw_email": "alice1@example.com",
			"email_normalized": "alice1@example.com",
			"email_status": "ok",
		})
		rl.append("contacts", {
			"full_name": "Alice 2",
			"raw_phone": "9876543210",
			"phone_e164": "+919876543210",
			"phone_status": phone_validator.VALID,
			"phone_type": "Mobile",
			"raw_email": "alice2@example.com",
			"email_normalized": "alice2@example.com",
			"email_status": "ok",
		})
		rl.append("contacts", {
			"full_name": "Bob 1",
			"raw_phone": "9812345678",
			"phone_e164": "+919812345678",
			"phone_status": phone_validator.VALID,
			"phone_type": "Mobile",
			"raw_email": "bob@example.com",
			"email_normalized": "bob@example.com",
			"email_status": "ok",
		})
		rl.append("contacts", {
			"full_name": "Bob 2",
			"raw_phone": "9811122233",
			"phone_e164": "+919811122233",
			"phone_status": phone_validator.VALID,
			"phone_type": "Mobile",
			"raw_email": "bob@example.com",
			"email_normalized": "bob@example.com",
			"email_status": "ok",
		})
		rl.save()

		wa = rl.contacts_for("WhatsApp")
		wa_phones = [c["phone_e164"] for c in wa]
		self.assertEqual(len(wa), 3)
		self.assertEqual(wa_phones.count("+919876543210"), 1)

		emails = rl.contacts_for("Email")
		email_addrs = [c["email"] for c in emails]
		self.assertEqual(len(emails), 3)
		self.assertEqual(email_addrs.count("bob@example.com"), 1)

	def test_unicode_names(self):
		"""Contacts with unicode names are handled cleanly without encoding issues."""
		contacts = [
			{
				"full_name": "山田太郎 / Über / François / हिन्दी",
				"raw_phone": "9876543210",
				"raw_email": "unicode@example.com",
			}
		]
		rl = self._make_list("UnicodeTest", contacts)
		wa = rl.contacts_for("WhatsApp")
		self.assertEqual(len(wa), 1)
		self.assertEqual(wa[0]["full_name"], "山田太郎 / Über / François / हिन्दी")

	def test_zero_contacts_returns_empty(self):
		"""Empty contact list returns empty results and empty rejection breakdown."""
		rl = self._make_list("EmptyTest")
		for ch in CHANNELS:
			self.assertEqual(rl.contacts_for(ch), [])
			self.assertEqual(rl.rejection_breakdown(ch), {})

	def test_rejection_breakdown_sums_equal_dropped_count(self):
		"""rejection_breakdown sums must equal dropped count for every channel."""
		contacts = [
			# Reachable everywhere
			{"full_name": "Both", "raw_phone": "9876543210", "raw_email": "both@example.com"},
			# Mobile, invalid email -> WhatsApp/SMS only
			{"full_name": "PhoneOnly", "raw_phone": "9812345678", "raw_email": "bad@example.com"},
			# Landline: Valid number, cannot receive SMS/WA -> Email only
			{"full_name": "Landline", "raw_phone": "+912223456789", "raw_email": "land@example.com"},
			# No phone -> Email only
			{"full_name": "EmailOnly", "raw_phone": "", "raw_email": "only@example.com"},
			# Junk phone, no email -> reachable nowhere
			{"full_name": "Neither", "raw_phone": "junk", "raw_email": ""},
		]
		rl = self._make_list("BreakdownTest", contacts)
		for c in rl.contacts:
			if c.full_name == "PhoneOnly":
				c.email_status = "invalid"
			elif c.full_name in ("Both", "Landline", "EmailOnly"):
				c.email_status = "ok"

		# Also add a duplicate phone row
		rl.append("contacts", {
			"full_name": "Duplicate Both",
			"raw_phone": "9876543210",
			"phone_e164": "+919876543210",
			"phone_status": phone_validator.VALID,
			"phone_type": "Mobile",
			"raw_email": "dup@example.com",
			"email_normalized": "dup@example.com",
			"email_status": "ok",
		})
		rl.save()

		for ch in CHANNELS:
			reachable = rl.contacts_for(ch)
			reasons = rl.rejection_breakdown(ch)
			dropped = len(rl.contacts) - len(reachable)
			self.assertEqual(
				sum(reasons.values()),
				dropped,
				f"Rejection breakdown sum {sum(reasons.values())} != dropped {dropped} for channel {ch}. Reasons: {reasons}",
			)

	def test_permission_denied_for_role_without_access(self):
		"""A user without campaign roles must be denied access to Recipient List."""
		test_user_email = f"guest_test_{frappe.generate_hash(length=6)}@example.com"
		user = frappe.get_doc({
			"doctype": "User",
			"email": test_user_email,
			"first_name": "Guest",
			"roles": [{"role": "MSuite Viewer"}],
		}).insert(ignore_permissions=True)
		self.cleanup_users.append(user.name)

		# MSuite Viewer has read only; write/create must be False
		can_create = frappe.has_permission("Recipient List", "create", user=user.name)
		can_write = frappe.has_permission("Recipient List", "write", user=user.name)
		self.assertFalse(can_create)
		self.assertFalse(can_write)

	def test_import_from_linked_doctype_requires_write_permission(self):
		"""Calling import_from_linked_doctype without write permission raises PermissionError."""
		rl = self._make_list("ImportPermTest")
		test_user_email = f"viewer_{frappe.generate_hash(length=6)}@example.com"
		user = frappe.get_doc({
			"doctype": "User",
			"email": test_user_email,
			"first_name": "Viewer",
			"roles": [{"role": "MSuite Viewer"}],
		}).insert(ignore_permissions=True)
		self.cleanup_users.append(user.name)

		frappe.set_user(user.name)
		try:
			rl_user = frappe.get_doc("Recipient List", rl.name)
			with self.assertRaises(frappe.PermissionError):
				rl_user.import_from_linked_doctype()
		finally:
			frappe.set_user("Administrator")

	def test_plan_gate_recipient_list_size(self):
		"""Adding contacts beyond campaigns_audience_size limit raises ValidationError."""
		from msuite_campaigns.audience.plan_gates import check_recipient_list_size_gate

		rl = self._make_list("GateTest")
		rl.append("contacts", {
			"full_name": "Contact 1",
			"raw_phone": "+919876543210",
			"phone_e164": "+919876543210",
		})
		rl.append("contacts", {
			"full_name": "Contact 2",
			"raw_phone": "+919876543211",
			"phone_e164": "+919876543211",
		})

		with patch("msuite_campaigns.audience.plan_gates.is_feature_in_plan", return_value=True):
			with patch("msuite_campaigns.audience.plan_gates.get_feature_limit", return_value=1):
				with self.assertRaises(frappe.ValidationError):
					check_recipient_list_size_gate(rl)

			with patch("msuite_campaigns.audience.plan_gates.get_feature_limit", return_value=5):
				check_recipient_list_size_gate(rl)

	def test_import_stays_under_one_query_per_chunk(self):
		"""Syncing contacts against tabContact must batch queries in chunks, not query per row."""
		rl = self._make_list("ChunkTest")
		batch_size = 2500
		rows = []
		for i in range(batch_size):
			rows.append({
				"full_name": f"Contact {i}",
				"raw_phone": f"+9198000{i:05d}",
				"phone_e164": f"+9198000{i:05d}",
				"phone_status": phone_validator.VALID,
				"phone_type": "Mobile",
				"raw_email": f"c{i}@example.com",
				"email_normalized": f"c{i}@example.com",
			})
		for r in rows:
			rl.append("contacts", r)

		original_sql = frappe.db.sql
		contact_queries = 0

		def counting_sql(query, *args, **kwargs):
			nonlocal contact_queries
			if "tabContact" in str(query) and "SELECT" in str(query).upper():
				contact_queries += 1
			return original_sql(query, *args, **kwargs)

		with patch("frappe.new_doc") as mock_new_doc:
			with patch("frappe.db.sql", side_effect=counting_sql):
				sync_recipient_list_to_contacts(rl)

		# 2500 rows with chunk size 1000 should take ~3 phone chunks + ~3 email chunks = ~6 queries,
		# definitely << 100 queries (nowhere near 2500).
		self.assertLessEqual(contact_queries, 10)
		self.assertGreater(contact_queries, 0)

	def test_opt_out_and_opt_in_propagate_to_recipient_contact(self):
		"""Setting and clearing Contact WhatsApp consent propagates to Recipient Contact.unsubscribed_whatsapp."""
		phone = f"+9194{random.randint(10000000, 99999999)}"
		email = f"user_{frappe.generate_hash(length=6)}@example.com"

		c = frappe.get_doc({
			"doctype": "Contact",
			"first_name": "RL Consent",
			"last_name": "Test",
			"unsubscribed_whatsapp": 0,
		})
		c.add_phone(phone, is_primary_mobile_no=1)
		c.insert(ignore_permissions=True)
		self.cleanup_contacts.append(c.name)

		rl = self._make_list("RLConsentTest")
		rl.append("contacts", {
			"full_name": "Consent Test",
			"raw_phone": phone,
			"phone_e164": phone,
			"raw_email": email,
			"email_normalized": email,
			"unsubscribed_whatsapp": 0,
		})
		rl.save()

		# 1. Opt out
		res_out = record_channel_opt_out("WhatsApp", phone, reason="User opted out")
		self.assertTrue(res_out)

		contact_out = frappe.db.get_value(
			"Recipient Contact",
			{"parent": rl.name, "phone_e164": phone},
			"unsubscribed_whatsapp",
		)
		self.assertEqual(contact_out, 1)

		# 2. Opt in
		res_in = record_channel_opt_in("WhatsApp", phone, reason="User opted back in")
		self.assertTrue(res_in)

		contact_in = frappe.db.get_value(
			"Recipient Contact",
			{"parent": rl.name, "phone_e164": phone},
			"unsubscribed_whatsapp",
		)
		self.assertEqual(contact_in, 0)

import random
import unittest

import frappe
from frappe.tests import IntegrationTestCase

from msuite_campaigns.audience.services.contact_sync_service import (
	record_channel_opt_in,
	record_channel_opt_out,
)


class TestChannelConsentSync(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		self.cleanup_contacts = []
		self.cleanup_audiences = []

	def tearDown(self):
		frappe.db.rollback()
		for a in self.cleanup_audiences:
			if frappe.db.exists("Audience", a):
				frappe.delete_doc("Audience", a, force=True, ignore_permissions=True)
		for c in self.cleanup_contacts:
			if frappe.db.exists("Contact", c):
				frappe.delete_doc("Contact", c, force=True, ignore_permissions=True)

	def test_opt_out_create_if_missing_false(self):
		"""When create_if_missing is False and no contact matches, returns False and creates nothing."""
		phone = f"+9191{random.randint(10000000, 99999999)}"
		res = record_channel_opt_out("WhatsApp", phone, reason="STOP", create_if_missing=False)
		self.assertFalse(res)
		self.assertFalse(frappe.db.exists("Contact", {"phone_e164": phone}))

	def test_opt_out_create_if_missing_true_creates_contact(self):
		"""When create_if_missing is True and no contact matches, creates minimal Contact with opt-out flags."""
		phone = f"+9192{random.randint(10000000, 99999999)}"
		res = record_channel_opt_out("WhatsApp", phone, reason="Inbound STOP keyword", create_if_missing=True)
		self.assertTrue(res)

		contact_name = frappe.db.get_value("Contact", {"phone_e164": phone}, "name")
		self.assertIsNotNone(contact_name)
		self.cleanup_contacts.append(contact_name)

		c = frappe.get_doc("Contact", contact_name)
		self.assertEqual(c.unsubscribed_whatsapp, 1)
		self.assertEqual(c.opt_out_reason, "Inbound STOP keyword")
		self.assertIsNotNone(c.consent_updated_at)

	def test_opt_out_and_opt_in_propagate_to_audience_member(self):
		"""Setting and clearing Contact WhatsApp consent propagates to Audience Member.unsubscribed_whatsapp."""
		phone = f"+9193{random.randint(10000000, 99999999)}"
		email = f"user_{frappe.generate_hash(length=6)}@example.com"

		c = frappe.get_doc({
			"doctype": "Contact",
			"first_name": "Consent",
			"last_name": "Test",
			"mobile_no": phone,
			"phone_e164": phone,
			"unsubscribed_whatsapp": 0,
		}).insert(ignore_permissions=True)
		self.cleanup_contacts.append(c.name)

		aud = frappe.get_doc({
			"doctype": "Audience",
			"audience_name": f"Aud_Consent_{frappe.generate_hash(length=6)}",
			"default_region": "IN",
			"members": [
				{
					"full_name": "Consent Test",
					"raw_phone": phone,
					"phone_e164": phone,
					"raw_email": email,
					"unsubscribed_whatsapp": 0,
				}
			],
		}).insert(ignore_permissions=True)
		self.cleanup_audiences.append(aud.name)

		# 1. Opt out via record_channel_opt_out
		res_out = record_channel_opt_out("WhatsApp", phone, reason="User opted out")
		self.assertTrue(res_out)

		# Verify Contact
		c_reloaded = frappe.get_doc("Contact", c.name)
		self.assertEqual(c_reloaded.unsubscribed_whatsapp, 1)

		# Verify Audience Member propagated
		member_out = frappe.db.get_value(
			"Audience Member",
			{"parent": aud.name, "phone_e164": phone},
			"unsubscribed_whatsapp",
		)
		self.assertEqual(member_out, 1)

		# 2. Opt in via record_channel_opt_in
		res_in = record_channel_opt_in("WhatsApp", phone, reason="User opted back in")
		self.assertTrue(res_in)

		# Verify Contact
		c_reloaded_in = frappe.get_doc("Contact", c.name)
		self.assertEqual(c_reloaded_in.unsubscribed_whatsapp, 0)

		# Verify Audience Member propagated
		member_in = frappe.db.get_value(
			"Audience Member",
			{"parent": aud.name, "phone_e164": phone},
			"unsubscribed_whatsapp",
		)
		self.assertEqual(member_in, 0)

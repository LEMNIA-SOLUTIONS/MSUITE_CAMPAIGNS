# Copyright (c) 2026, MSuite and contributors
# For license information, please see license.txt

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from msuite_campaigns.audience.api.v1 import audience as audience_api
from msuite_core.services.permissions.roles import (
	ADMIN,
	CAMPAIGN_MANAGER,
	CAMPAIGN_USER,
	SMS_USER,
	VIEWER,
	WA_USER,
)


def _user(role: str | None) -> str:
	email = f"perm-{(role or 'none').lower().replace(' ', '-')}@example.com"
	if not frappe.db.exists("User", email):
		u = frappe.get_doc({
			"doctype": "User",
			"email": email,
			"first_name": "Perm",
			"send_welcome_email": 0,
		}).insert(ignore_permissions=True)
		if role:
			u.add_roles(role)
	elif role:
		u = frappe.get_doc("User", email)
		if role not in [r.role for r in u.roles]:
			u.add_roles(role)
	return email


class TestAudienceApiPermissions(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")

		_user(CAMPAIGN_MANAGER)
		_user(CAMPAIGN_USER)
		_user(WA_USER)
		_user(SMS_USER)
		_user(VIEWER)
		_user(ADMIN)
		_user(None)

		cls.audience_name = "Perm Test Audience"
		if frappe.db.exists("Audience", cls.audience_name):
			frappe.delete_doc("Audience", cls.audience_name, force=True)
		cls.audience_doc = frappe.get_doc({
			"doctype": "Audience",
			"audience_name": cls.audience_name,
			"description": "For permission testing",
			"source": "Manual",
			"members": [
				{
					"full_name": "Test Person",
					"raw_phone": "+1234567890",
					"raw_email": "test@example.com",
				}
			],
		}).insert(ignore_permissions=True)
		cls.audience = cls.audience_doc.name

		cls.account_name = "Perm Test SMS Account"
		if not frappe.db.exists("SMS Account", cls.account_name):
			frappe.get_doc({
				"doctype": "SMS Account",
				"account_name": cls.account_name,
				"gateway": "Twilio",
				"status": "Active",
				"is_default_outgoing": 0,
			}).insert(ignore_permissions=True)

		cls.template_name = "Perm Test SMS Template"
		if not frappe.db.exists("SMS Template", cls.template_name):
			frappe.get_doc({
				"doctype": "SMS Template",
				"template_name": cls.template_name,
				"sms_account": cls.account_name,
				"category": "Promotional",
				"status": "Approved",
				"body": "Hello {{first_name}}, testing permissions",
			}).insert(ignore_permissions=True)

		cls.draft_sms = frappe.get_doc({
			"doctype": "Bulk SMS Message",
			"title": "Perm Test Draft SMS",
			"sms_account": cls.account_name,
			"template": cls.template_name,
			"status": "Draft",
			"owner": _user(SMS_USER),
			"recipients": [{"mobile_number": "+1234567890"}],
		}).insert(ignore_permissions=True)

	def tearDown(self):
		frappe.set_user("Administrator")
		super().tearDown()

	def test_role_less_user_endpoints_raise_permission_error(self):
		frappe.set_user(_user(None))
		self.assertRaises(frappe.PermissionError, audience_api.list_audiences)
		self.assertRaises(frappe.PermissionError, audience_api.get_audience, self.audience)
		self.assertRaises(frappe.PermissionError, audience_api.get_members, self.audience)
		self.assertRaises(frappe.PermissionError, audience_api.fetch_doctype_members, "User", phone_field="phone")
		self.assertRaises(frappe.PermissionError, audience_api.get_doctype_fields, "User")
		self.assertRaises(frappe.PermissionError, audience_api.get_broadcast_detail, self.draft_sms.name)
		self.assertRaises(frappe.PermissionError, audience_api.pause_broadcast, self.draft_sms.name)
		self.assertRaises(frappe.PermissionError, audience_api.resume_broadcast, self.draft_sms.name)
		self.assertRaises(frappe.PermissionError, audience_api.get_validation_summary, self.audience)
		self.assertRaises(frappe.PermissionError, audience_api.preview_file, "/files/test.csv")
		self.assertRaises(frappe.PermissionError, audience_api.parse_file_rows, "/files/test.csv")
		results = audience_api.search_doctypes(query="User")
		self.assertNotIn("User", results)

	def test_h1_fetch_doctype_members_permission_gate(self):
		frappe.set_user(_user(WA_USER))
		can_read_contact = frappe.has_permission("Contact", "read")
		if can_read_contact:
			res = audience_api.fetch_doctype_members("Contact", phone_field="mobile_no")
			self.assertIsInstance(res, list)
		else:
			self.assertRaises(frappe.PermissionError, audience_api.fetch_doctype_members, "Contact")

		# DocType with no read right (Social Post) must raise
		self.assertRaises(frappe.PermissionError, audience_api.fetch_doctype_members, "Social Post")

		# Linked doc method import must also raise for unreadable DocType
		aud = frappe.get_doc("Audience", self.audience)
		aud.import_from_doctype = 1
		aud.doctype_to_import = "Social Post"
		aud.phone_field = "name"
		self.assertRaises(frappe.PermissionError, aud.import_from_linked_doctype)

	def test_h2_bulk_delete_broadcasts_cascade_gate(self):
		frappe.set_user("Administrator")
		camp = frappe.get_doc({
			"doctype": "Marketing Campaign",
			"campaign_name": "Perm H2 Delete Campaign",
			"start_date": "2026-10-06 12:00:00",
			"end_date": "2026-10-07 12:00:00",
			"owner": _user(CAMPAIGN_MANAGER),
		}).insert(ignore_permissions=True)

		frappe.set_user(_user(WA_USER))
		res = audience_api.bulk_delete_broadcasts([camp.name])
		self.assertEqual(res.get("deleted"), 0)
		self.assertTrue(frappe.db.exists("Marketing Campaign", camp.name))

		frappe.set_user("Administrator")
		res_admin = audience_api.bulk_delete_broadcasts([camp.name])
		self.assertEqual(res_admin.get("deleted"), 1)
		self.assertFalse(frappe.db.exists("Marketing Campaign", camp.name))

	def test_h3_viewer_retry_broadcast_draft_raises(self):
		frappe.set_user(_user(VIEWER))
		with patch("msuite_sms.sms.doctype.bulk_sms_message.bulk_sms_message.BulkSMSMessage.submit") as mock_submit:
			self.assertRaises(frappe.PermissionError, audience_api.retry_broadcast, self.draft_sms.name)
			mock_submit.assert_not_called()
		self.assertEqual(frappe.db.get_value("Bulk SMS Message", self.draft_sms.name, "docstatus"), 0)

	def test_h4_viewer_add_members_manual_raises_and_campaign_user_succeeds(self):
		frappe.set_user(_user(VIEWER))
		self.assertRaises(
			frappe.PermissionError,
			audience_api.add_members_manual,
			self.audience,
			[{"full_name": "H4 Test", "raw_phone": "+1987654321"}],
		)

		frappe.set_user(_user(CAMPAIGN_USER))
		with patch("msuite_campaigns.audience.doctype.audience.audience.Audience.validate_members") as mock_val:
			mock_val.return_value = {"total": 1}
			res = audience_api.add_members_manual(
				self.audience,
				[{"full_name": "H4 Member OK", "raw_phone": "+1987654322"}],
			)
			self.assertEqual(res.get("added"), 1)

	def test_campaign_user_permissions(self):
		frappe.set_user(_user(CAMPAIGN_USER))
		with patch("msuite_campaigns.audience.doctype.audience.audience.Audience.validate_members") as mock_val:
			mock_val.return_value = {"total": 1}
			res = audience_api.validate_audience(self.audience)
			self.assertIn("queued", res)

		self.assertRaises(frappe.PermissionError, audience_api.delete_audience, self.audience)

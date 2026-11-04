# Copyright (c) 2026, MSuite and contributors
# For license information, please see license.txt

import json
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from msuite_campaigns.dispatch.api.v1 import campaign as campaign_api
from msuite_campaigns.dispatch import hub
from msuite_core.services.permissions.roles import ADMIN, CAMPAIGN_USER, EMAIL_USER, SMS_USER, VIEWER, WA_USER


def _user(role: str | None, email: str | None = None) -> str:
	email = email or f"perm-{(role or 'none').lower().replace(' ', '-')}@example.com"
	if not frappe.db.exists("User", email):
		u = frappe.get_doc({
			"doctype": "User",
			"email": email,
			"first_name": "Perm",
			"send_welcome_email": 0,
		}).insert(ignore_permissions=True)
		if role:
			u.add_roles(role)
	else:
		u = frappe.get_doc("User", email)
		current_roles = [r.role for r in u.roles]
		if role:
			if role not in current_roles:
				u.add_roles(role)
		else:
			roles_to_remove = [r.role for r in u.roles if r.role not in ("Desk User", "All")]
			if roles_to_remove:
				u.remove_roles(*roles_to_remove)
	return email


class TestDispatchApiPermissions(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")

		cls.campaign_user = _user(CAMPAIGN_USER)
		cls.wa_user = _user(WA_USER)
		cls.other_wa_user = _user(WA_USER, email="perm-other-wa@example.com")
		cls.sms_user = _user(SMS_USER)
		cls.email_user = _user(EMAIL_USER)
		cls.viewer = _user(VIEWER)
		cls.role_less = _user(None)
		cls.admin_user = _user(ADMIN)

		cls.audience_name = "Perm Dispatch Audience"
		if frappe.db.exists("Audience", cls.audience_name):
			frappe.delete_doc("Audience", cls.audience_name, force=True)
		aud = frappe.get_doc({
			"doctype": "Audience",
			"audience_name": cls.audience_name,
			"source": "Manual",
			"members": [
				{
					"full_name": "Test Person",
					"raw_phone": "+1234567890",
					"raw_email": "test@example.com",
				}
			],
		}).insert(ignore_permissions=True)
		cls.audience = aud.name

	def test_get_available_channels_filters_by_permission(self):
		frappe.set_user(self.wa_user)
		channels = campaign_api.get_available_channels()["channels"]
		self.assertIn("whatsapp", channels)
		self.assertNotIn("sms", channels)
		self.assertNotIn("email", channels)

		frappe.set_user(self.role_less)
		channels = campaign_api.get_available_channels()["channels"]
		self.assertEqual(channels, [])

		frappe.set_user(self.viewer)
		channels = campaign_api.get_available_channels()["channels"]
		self.assertEqual(channels, [])

	def test_role_less_preview_raises(self):
		frappe.set_user(self.role_less)
		payload = {
			"audience": self.audience,
			"channels": {"whatsapp": {"template": "T1"}},
		}
		with patch("msuite_core.services.plans.plan_enforcer.is_feature_enabled", return_value=True):
			with self.assertRaises(frappe.PermissionError):
				campaign_api.preview(json.dumps(payload))

	def test_role_less_launch_raises_and_creates_no_campaign_or_enqueues(self):
		frappe.set_user(self.role_less)
		payload = {
			"audience": self.audience,
			"channels": {"whatsapp": {"template": "T1"}},
			"campaign_name": "Roleless Hijack Attempt",
		}
		before_count = frappe.db.count("Marketing Campaign", {"campaign_name": "Roleless Hijack Attempt"})
		with patch("msuite_core.services.plans.plan_enforcer.is_feature_enabled", return_value=True), \
			 patch("frappe.enqueue") as mock_enqueue:
			with self.assertRaises(frappe.PermissionError):
				campaign_api.launch(json.dumps(payload))
			self.assertFalse(mock_enqueue.called)

		after_count = frappe.db.count("Marketing Campaign", {"campaign_name": "Roleless Hijack Attempt"})
		self.assertEqual(before_count, after_count)

	def test_whatsapp_user_can_launch_single_channel(self):
		frappe.set_user(self.wa_user)
		payload = {
			"audience": self.audience,
			"channels": {"whatsapp": {"template": "T1"}},
			"campaign_name": "WA User Single Launch",
		}
		with patch("msuite_core.services.plans.plan_enforcer.is_feature_enabled", return_value=True), \
			 patch("frappe.enqueue") as mock_enqueue:
			res = campaign_api.launch(json.dumps(payload))
			self.assertTrue(mock_enqueue.called)
			self.assertEqual(res["status"], "queued")
			self.assertTrue(frappe.db.exists("Marketing Campaign", res["campaign"]))

	def test_whatsapp_user_launching_multi_channel_raises(self):
		frappe.set_user(self.wa_user)
		payload = {
			"audience": self.audience,
			"channels": {
				"whatsapp": {"template": "T1"},
				"sms": {"template": "T2", "account": "A1"},
			},
			"campaign_name": "WA User Multi Launch Attempt",
		}
		with patch("msuite_core.services.plans.plan_enforcer.is_feature_enabled", return_value=True), \
			 patch("frappe.enqueue") as mock_enqueue:
			with self.assertRaises(frappe.PermissionError):
				campaign_api.launch(json.dumps(payload))
			self.assertFalse(mock_enqueue.called)

	def test_campaign_owned_by_someone_else_without_write_raises(self):
		# Create a campaign owned by campaign_user
		frappe.set_user("Administrator")
		mc = frappe.get_doc({
			"doctype": "Marketing Campaign",
			"campaign_name": "Protected Campaign",
			"owner": self.campaign_user,
			"status": "In Progress",
			"start_date": frappe.utils.today(),
			"end_date": frappe.utils.add_days(frappe.utils.today(), 10),
		}).insert(ignore_permissions=True)

		# wa_user attempts to launch into it
		frappe.set_user(self.wa_user)
		payload = {
			"audience": self.audience,
			"marketing_campaign": mc.name,
			"channels": {"whatsapp": {"template": "T1"}},
		}
		with patch("msuite_core.services.plans.plan_enforcer.is_feature_enabled", return_value=True), \
			 patch("frappe.enqueue") as mock_enqueue:
			with self.assertRaises(frappe.PermissionError):
				campaign_api.launch(json.dumps(payload))
			self.assertFalse(mock_enqueue.called)

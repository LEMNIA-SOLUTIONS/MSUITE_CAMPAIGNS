# Copyright (c) 2026, MSuite and contributors
# For license information, please see license.txt

import json
import frappe
from frappe.tests import IntegrationTestCase

from msuite_campaigns.campaign.api.v1.composer import get_campaign_activity
from msuite_core.services.permissions.roles import CAMPAIGN_USER


def _user(role: str | None, email: str | None = None) -> str:
	email = email or f"activity-perm-{(role or 'none').lower().replace(' ', '-')}@example.com"
	if not frappe.db.exists("User", email):
		u = frappe.get_doc({
			"doctype": "User",
			"email": email,
			"first_name": "ActivityPerm",
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


class TestCampaignActivity(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")

		cls.campaign_user = _user(CAMPAIGN_USER)
		cls.role_less = _user(None)

		# Create a test Marketing Campaign
		cls.campaign_doc = frappe.get_doc({
			"doctype": "Marketing Campaign",
			"campaign_name": "Activity Test Campaign",
			"status": "Draft",
			"start_date": "2026-07-01",
			"end_date": "2026-07-31",
		}).insert(ignore_permissions=True)

		# Insert Version records: one current 'Marketing Campaign', one legacy 'Campaign'
		cls.v_current = frappe.get_doc({
			"doctype": "Version",
			"ref_doctype": "Marketing Campaign",
			"docname": cls.campaign_doc.name,
			"data": json.dumps({"changed": [["status", "Draft", "Active"]]}),
		}).insert(ignore_permissions=True)

		cls.v_legacy = frappe.get_doc({
			"doctype": "Version",
			"ref_doctype": "Campaign",
			"docname": cls.campaign_doc.name,
			"data": json.dumps({"changed": [["budget", 0, 1000]]}),
		}).insert(ignore_permissions=True)

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		if hasattr(cls, "v_current"):
			cls.v_current.delete(ignore_permissions=True)
		if hasattr(cls, "v_legacy"):
			cls.v_legacy.delete(ignore_permissions=True)
		if hasattr(cls, "campaign_doc"):
			cls.campaign_doc.delete(ignore_permissions=True)
		super().tearDownClass()

	def test_user_with_read_permission_gets_rows(self):
		frappe.set_user(self.campaign_user)
		rows = get_campaign_activity(self.campaign_doc.name)
		self.assertIsInstance(rows, list)
		names = [r["name"] for r in rows]
		self.assertIn(self.v_current.name, names)

	def test_user_without_read_permission_is_refused(self):
		frappe.set_user(self.role_less)
		with self.assertRaises(frappe.PermissionError):
			get_campaign_activity(self.campaign_doc.name)

	def test_legacy_campaign_ref_doctype_rows_included(self):
		frappe.set_user(self.campaign_user)
		rows = get_campaign_activity(self.campaign_doc.name)
		names = [r["name"] for r in rows]
		self.assertIn(self.v_legacy.name, names)

	def test_nonexistent_campaign_returns_empty_list(self):
		frappe.set_user(self.campaign_user)
		rows = get_campaign_activity("nonexistent-test-campaign-xyz-123")
		self.assertEqual(rows, [])

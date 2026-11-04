# Copyright (c) 2026, MSuite and contributors
# For license information, please see license.txt

import frappe
from frappe.tests import IntegrationTestCase

from msuite_campaigns.audience.api.v1 import audience as audience_api


class TestAudienceMemberCrud(IntegrationTestCase):
	def setUp(self):
		self.audience = frappe.get_doc(
			{
				"doctype": "Audience",
				"audience_name": f"CRUD Test {frappe.generate_hash(length=6)}",
				"default_region": "IN",
				"source": "Manual",
			}
		)
		self.audience.append(
			"members",
			{
				"full_name": "Original Name",
				"raw_phone": "9876543210",
				"raw_email": "original@example.com",
			},
		)
		self.audience.insert(ignore_permissions=True)

	def tearDown(self):
		frappe.delete_doc("Audience", self.audience.name, force=True, ignore_permissions=True)

	def test_update_member_revalidates_and_updates_rollups(self):
		member_name = self.audience.members[0].name

		result = audience_api.update_member(
			member_name,
			full_name="New Name",
			raw_phone="9876543210",
			raw_email="changed@example.com",
		)

		self.assertEqual(result["full_name"], "New Name")
		self.assertEqual(result["raw_email"], "changed@example.com")
		self.assertEqual(result["email_normalized"], "changed@example.com")

		refreshed = frappe.get_doc("Audience", self.audience.name)
		self.assertEqual(refreshed.members[0].full_name, "New Name")

	def test_delete_member_removes_row_and_updates_rollups(self):
		member_name = self.audience.members[0].name

		result = audience_api.delete_member(member_name)

		self.assertTrue(result["ok"])
		refreshed = frappe.get_doc("Audience", self.audience.name)
		self.assertEqual(len(refreshed.members or []), 0)
		self.assertEqual(refreshed.total_members, 0)

	def test_delete_audience_cascades_members(self):
		member_name = self.audience.members[0].name
		frappe.delete_doc("Audience", self.audience.name, ignore_permissions=True)

		self.assertFalse(frappe.db.exists("Audience Member", member_name))

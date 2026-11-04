# Copyright (c) 2026, MSuite and contributors
# For license information, please see license.txt

import frappe
from frappe.tests import IntegrationTestCase

from msuite_campaigns.campaign.api.bulk_import import sample_csv
from msuite_campaigns.campaign.api.v1.calendar import get_calendar_events
from msuite_core.services.permissions.roles import ADS_USER, CAMPAIGN_USER, SOCIAL_USER


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


class TestCampaignApiPermissions(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")

		cls.social_user = _user(SOCIAL_USER)
		cls.ads_user = _user(ADS_USER)
		cls.campaign_user = _user(CAMPAIGN_USER)
		cls.role_less = _user(None)

		# Insert sample Campaign Calendar Events
		cls.sp_event = frappe.get_doc({
			"doctype": "Campaign Calendar Event",
			"event_type": "Social Post",
			"source_name": "test-social-post-01",
			"title": "Perm Test Social Post",
			"start_date": "2026-07-01 10:00:00",
			"end_date": "2026-07-01 10:00:00",
			"status": "Scheduled",
			"platform": "Facebook",
		}).insert(ignore_permissions=True)

		cls.ad_event = frappe.get_doc({
			"doctype": "Campaign Calendar Event",
			"event_type": "Ad Campaign",
			"source_name": "test-ad-campaign-01",
			"title": "Perm Test Ad Campaign",
			"start_date": "2026-07-01 11:00:00",
			"end_date": "2026-07-01 18:00:00",
			"status": "Active",
			"platform": "Meta",
		}).insert(ignore_permissions=True)

	def test_sample_csv_accessible(self):
		frappe.set_user(self.role_less)
		csv_data = sample_csv()
		self.assertIsInstance(csv_data, str)
		self.assertIn("account,caption", csv_data)

	def test_get_calendar_events_role_less_sees_no_marketing_events(self):
		frappe.set_user(self.role_less)
		events = get_calendar_events("2026-07-01 00:00:00", "2026-07-02 00:00:00")
		types = [e["event_type"] for e in events]
		self.assertNotIn("Social Post", types)
		self.assertNotIn("Ad Campaign", types)
		self.assertNotIn("Campaign", types)
		self.assertNotIn("WhatsApp Message", types)

	def test_get_calendar_events_social_user_sees_social_not_ads(self):
		frappe.set_user(self.social_user)
		events = get_calendar_events("2026-07-01 00:00:00", "2026-07-02 00:00:00")
		types = [e["event_type"] for e in events]
		self.assertIn("Social Post", types)
		self.assertNotIn("Ad Campaign", types)

	def test_get_calendar_events_ads_user_sees_ads_not_social(self):
		frappe.set_user(self.ads_user)
		events = get_calendar_events("2026-07-01 00:00:00", "2026-07-02 00:00:00")
		types = [e["event_type"] for e in events]
		self.assertIn("Ad Campaign", types)
		self.assertNotIn("Social Post", types)

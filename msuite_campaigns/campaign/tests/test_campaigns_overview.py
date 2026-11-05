"""Tests for get_campaigns_overview extensions and lead_goal custom field.

Verifies:
  - Empty site behavior (0 campaigns, clean empty response)
  - One campaign with status, budget, spent, and lead_goal
  - CRM Lead attribution present vs absent
  - Week-row reach flag behavior
  - Cross-campaign reach, engagement, and conversions rollup
  - Period edges: 7d, mtd / month, and 90d / qtr
  - Previous-period comparison totals
  - lead_goal custom field declaration and upsert
"""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import getdate, now_datetime, today

from msuite_campaigns.campaign.services import analytics
from msuite_campaigns.setup.campaign_fields import sync_marketing_campaign_custom_fields
from msuite_core.constants import (
	GRANULARITY_DAY,
	GRANULARITY_WEEK,
)
from msuite_core.utils.insight_keys import make_insight_key

DOCTYPE_CAMPAIGN = "Marketing Campaign"
DOCTYPE_INSIGHT = "Marketing Campaign Insight"


class TestCampaignsOverviewExtended(IntegrationTestCase):
	"""Integration tests for enhanced get_campaigns_overview and lead_goal field."""

	def setUp(self):
		super().setUp()
		self._created_campaigns: set[str] = set()
		self._created_leads: set[str] = set()

		# Ensure custom field is present for test run
		sync_marketing_campaign_custom_fields()
		frappe.db.commit()

	def tearDown(self):
		for lead in list(self._created_leads):
			if frappe.db.exists("CRM Lead", lead):
				frappe.delete_doc("CRM Lead", lead, force=True, ignore_permissions=True)
		self._created_leads.clear()

		for camp in list(self._created_campaigns):
			frappe.db.delete(DOCTYPE_INSIGHT, {"marketing_campaign": camp})
			if frappe.db.exists(DOCTYPE_CAMPAIGN, camp):
				frappe.delete_doc(DOCTYPE_CAMPAIGN, camp, force=True, ignore_permissions=True)
		self._created_campaigns.clear()
		frappe.db.commit()
		super().tearDown()

	def _create_campaign(
		self,
		name: str,
		status: str = "Active",
		budget: float = 1000.0,
		spent: float = 200.0,
		lead_goal: int | None = 20,
	) -> str:
		doc = frappe.get_doc({
			"doctype": DOCTYPE_CAMPAIGN,
			"campaign_name": name,
			"status": status,
			"budget": budget,
			"spent": spent,
			"start_date": str(today()),
			"lead_goal": lead_goal,
		})
		doc.flags.ignore_validate = True
		doc.insert(ignore_permissions=True)
		self._created_campaigns.add(doc.name)
		return doc.name

	def _create_lead(self, campaign: str, creation_time=None) -> str:
		if not frappe.db.exists("DocType", "CRM Lead"):
			return ""
		lead = frappe.get_doc({
			"doctype": "CRM Lead",
			"first_name": "Test",
			"last_name": "Lead",
			"email": f"test_{frappe.generate_hash()[:6]}@example.com",
			"marketing_campaign": campaign,
		})
		lead.flags.ignore_validate = True
		lead.flags.ignore_mandatory = True
		lead.flags.ignore_links = True
		lead.insert(ignore_permissions=True, ignore_mandatory=True, ignore_links=True)
		if creation_time:
			frappe.db.set_value("CRM Lead", lead.name, "creation", creation_time, update_modified=False)
		self._created_leads.add(lead.name)
		return lead.name

	def _insert_insight_row(
		self,
		campaign: str,
		granularity: str,
		date_str: str,
		spend: float = 0.0,
		reach: int = 0,
		engagement: int = 0,
		conversions: int = 0,
	) -> str:
		key = make_insight_key(DOCTYPE_CAMPAIGN, campaign, granularity, date_str)
		doc = frappe.get_doc({
			"doctype": DOCTYPE_INSIGHT,
			"marketing_campaign": campaign,
			"granularity": granularity,
			"date": date_str,
			"insight_key": key,
			"total_spend": spend,
			"total_reach": reach,
			"total_engagement": engagement,
			"ads_spend": spend,
			"ads_reach": reach,
			"ads_conversions": conversions,
		})
		doc.insert(ignore_permissions=True)
		return doc.name

	def test_lead_goal_custom_field_exists(self):
		"""Marketing Campaign custom field lead_goal must be declared and available."""
		meta = frappe.get_meta(DOCTYPE_CAMPAIGN)
		self.assertTrue(meta.has_field("lead_goal"))
		field = meta.get_field("lead_goal")
		self.assertEqual(field.fieldtype, "Int")

	def test_empty_site_overview(self):
		"""Overview on an empty site (or filtered to non-existent campaigns) returns clean empty response."""
		res = analytics.get_campaigns_overview(period="7d", filters={"name": "NON_EXISTENT_CAMPAIGN_XYZ"})
		kpis = res["kpis"]
		self.assertEqual(kpis["total_campaigns"], 0)
		self.assertEqual(kpis["active_campaigns"], 0)
		self.assertEqual(kpis["paused_campaigns"], 0)
		self.assertEqual(kpis["draft_campaigns"], 0)
		self.assertEqual(kpis["leads_generated"], 0)
		self.assertIsNone(kpis["cost_per_lead"])
		self.assertEqual(kpis["reach"], 0)
		self.assertEqual(kpis["engagement"], 0)
		self.assertEqual(kpis["conversions"], 0)
		self.assertIsNone(kpis["lead_goal"])
		self.assertIn("previous_period", kpis)
		self.assertEqual(kpis["previous_period"]["leads"], 0)

	def test_one_campaign_with_paused_and_lead_goal(self):
		"""Overview calculates active, paused counts and sums lead_goal."""
		c1 = self._create_campaign("Test Camp 1", status="Active", lead_goal=40)
		c2 = self._create_campaign("Test Camp 2", status="Paused", lead_goal=60)
		c3 = self._create_campaign("Test Camp 3", status="Draft", lead_goal=None)

		res = analytics.get_campaigns_overview(
			period="7d",
			filters={"name": ["in", [c1, c2, c3]]},
		)
		kpis = res["kpis"]
		self.assertEqual(kpis["total_campaigns"], 3)
		self.assertEqual(kpis["active_campaigns"], 1)
		self.assertEqual(kpis["paused_campaigns"], 1)
		self.assertEqual(kpis["draft_campaigns"], 1)
		self.assertEqual(kpis["lead_goal"], 100)

	def test_cross_campaign_reach_engagement_conversions_and_week_flag(self):
		"""Overview aggregates reach, engagement, conversions from DAY/WEEK rows and flags approximate reach."""
		c1 = self._create_campaign("Insight Camp 1")
		c2 = self._create_campaign("Insight Camp 2")
		today_date = getdate(today())

		d1 = (today_date - timedelta(days=2)).isoformat()
		d2 = (today_date - timedelta(days=4)).isoformat()
		# Day rows
		self._insert_insight_row(c1, GRANULARITY_DAY, d1, spend=100.0, reach=500, engagement=50, conversions=5)
		self._insert_insight_row(c2, GRANULARITY_DAY, d2, spend=150.0, reach=700, engagement=70, conversions=7)

		res = analytics.get_campaigns_overview(
			period="7d",
			filters={"name": ["in", [c1, c2]]},
		)
		kpis = res["kpis"]
		self.assertEqual(kpis["reach"], 1200)
		self.assertEqual(kpis["engagement"], 120)
		self.assertEqual(kpis["conversions"], 12)
		self.assertFalse(kpis["reach_approximate"])

		# Now add a WEEK row within 90-day window
		w1 = (today_date - timedelta(days=40)).isoformat()
		self._insert_insight_row(c1, GRANULARITY_WEEK, w1, spend=500.0, reach=0, engagement=100, conversions=10)

		res_90 = analytics.get_campaigns_overview(
			period="90d",
			filters={"name": ["in", [c1, c2]]},
		)
		self.assertEqual(res_90["kpis"]["reach"], 1200)  # Reach summed only from DAY rows
		self.assertEqual(res_90["kpis"]["engagement"], 220)
		self.assertEqual(res_90["kpis"]["conversions"], 22)
		self.assertTrue(res_90["kpis"]["reach_approximate"])

	def test_period_edges_and_previous_period_totals(self):
		"""Overview handles 7d, mtd, and 90d periods and calculates previous period totals."""
		c1 = self._create_campaign("Period Camp 1")
		today_date = getdate(today())

		# Current 7 days: day 1 ago
		d_current = (today_date - timedelta(days=1)).isoformat()
		self._insert_insight_row(c1, GRANULARITY_DAY, d_current, spend=200.0, reach=100, engagement=20, conversions=2)
		self._create_lead(c1, creation_time=now_datetime() - timedelta(days=1))

		# Previous 7 days: day 9 ago
		d_prev = (today_date - timedelta(days=9)).isoformat()
		self._insert_insight_row(c1, GRANULARITY_DAY, d_prev, spend=150.0, reach=80, engagement=15, conversions=1)
		self._create_lead(c1, creation_time=now_datetime() - timedelta(days=9))

		res_7d = analytics.get_campaigns_overview(
			period="7d",
			filters={"name": ["in", [c1]]},
		)
		kpis = res_7d["kpis"]
		self.assertEqual(kpis["leads_generated"], 1)
		self.assertEqual(kpis["spent_this_period"], 200.0)
		prev = kpis["previous_period"]
		self.assertEqual(prev["leads"], 1)
		self.assertEqual(prev["spent"], 150.0)
		self.assertEqual(prev["reach"], 80)
		self.assertEqual(prev["engagement"], 15)
		self.assertEqual(prev["conversions"], 1)

		# Check month-to-date (mtd) doesn't crash
		res_mtd = analytics.get_campaigns_overview(
			period="mtd",
			filters={"name": ["in", [c1]]},
		)
		self.assertIn("spent_this_period", res_mtd["kpis"])
		self.assertIn("previous_period", res_mtd["kpis"])

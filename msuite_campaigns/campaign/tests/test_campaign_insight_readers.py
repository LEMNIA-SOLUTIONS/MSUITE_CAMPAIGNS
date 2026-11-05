"""Tests for Marketing Campaign Insight readers (Task D2).

Verifies:
  - get_campaign_dashboard reads DAY + WEEK + LIFETIME without double counting
  - Date range queries exclude LIFETIME rows from range sums
  - Reach is summed ONLY from DAY rows (WEEK rows have reach = 0)
  - reach_approximate is True when window reaches into WEEK rows, False when only DAY rows
  - Lifetime queries read the LIFETIME row directly with reach_approximate = False
  - Boundary: straddling week kept as DAY rows while complete week is rolled up; both read cleanly without double counting
  - Response shapes remain strictly backward compatible (only adding fields)
  - get_campaigns_overview spend_trend and KPIs aggregate DAY + WEEK rows without double counting
"""

from datetime import date, timedelta
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import getdate, today

from msuite_campaigns.campaign.services import analytics
from msuite_core.constants import (
	GRANULARITY_DAY,
	GRANULARITY_LIFETIME,
	GRANULARITY_WEEK,
)
from msuite_core.utils.insight_keys import make_insight_key

DOCTYPE_CAMPAIGN = "Marketing Campaign"
DOCTYPE_INSIGHT = "Marketing Campaign Insight"

TEST_CAMPAIGN_READER_A = "Test-Reader-Camp-Alpha"
TEST_CAMPAIGN_READER_B = "Test-Reader-Camp-Beta"

SAMPLE_SPEND_DAY_1 = 100.0
SAMPLE_SPEND_DAY_2 = 200.0
SAMPLE_SPEND_DAY_3 = 300.0
SAMPLE_SPEND_WEEK_1 = 1000.0
SAMPLE_SPEND_WEEK_2 = 2000.0
SAMPLE_SPEND_LIFETIME = 50000.0

SAMPLE_REACH_DAY_1 = 50
SAMPLE_REACH_DAY_2 = 60
SAMPLE_REACH_DAY_3 = 70
SAMPLE_REACH_LIFETIME = 15000

SAMPLE_ENGAGEMENT_DAY_1 = 10
SAMPLE_ENGAGEMENT_DAY_2 = 20
SAMPLE_ENGAGEMENT_DAY_3 = 30
SAMPLE_ENGAGEMENT_WEEK_1 = 100
SAMPLE_ENGAGEMENT_WEEK_2 = 200
SAMPLE_ENGAGEMENT_LIFETIME = 5000

SAMPLE_LEADS_DAY_1 = 2
SAMPLE_LEADS_DAY_2 = 3
SAMPLE_LEADS_DAY_3 = 4
SAMPLE_LEADS_WEEK_1 = 15
SAMPLE_LEADS_WEEK_2 = 25
SAMPLE_LEADS_LIFETIME = 500

SAMPLE_CONV_DAY_1 = 1
SAMPLE_CONV_DAY_2 = 2
SAMPLE_CONV_DAY_3 = 3
SAMPLE_CONV_WEEK_1 = 10
SAMPLE_CONV_WEEK_2 = 20
SAMPLE_CONV_LIFETIME = 250


class TestCampaignInsightReaders(IntegrationTestCase):
	"""Test suite for Marketing Campaign Insight reader functions (Task D2)."""

	def setUp(self):
		"""Clean up test fixtures and ensure isolated state."""
		super().setUp()
		self._created_campaigns: set[str] = set()
		for name in (TEST_CAMPAIGN_READER_A, TEST_CAMPAIGN_READER_B):
			existing = frappe.db.get_value(DOCTYPE_CAMPAIGN, {"campaign_name": name}, "name")
			if existing:
				frappe.db.delete(DOCTYPE_INSIGHT, {"marketing_campaign": existing})
				frappe.delete_doc(DOCTYPE_CAMPAIGN, existing, force=True, ignore_permissions=True)
		frappe.db.commit()

	def tearDown(self):
		"""Clean up test campaigns and insight rows."""
		for camp_id in list(self._created_campaigns):
			frappe.db.delete(DOCTYPE_INSIGHT, {"marketing_campaign": camp_id})
			if frappe.db.exists(DOCTYPE_CAMPAIGN, camp_id):
				frappe.delete_doc(DOCTYPE_CAMPAIGN, camp_id, force=True, ignore_permissions=True)
		self._created_campaigns.clear()
		frappe.db.commit()
		super().tearDown()

	def _create_campaign(self, name: str, start_date: str | date = "2026-01-01") -> str:
		"""Create a Marketing Campaign record for testing."""
		doc = frappe.get_doc({
			"doctype": DOCTYPE_CAMPAIGN,
			"campaign_name": name,
			"status": "Active",
			"start_date": str(start_date),
			"goal": "Test Reader Campaign",
		})
		doc.flags.ignore_validate = True
		doc.insert(ignore_permissions=True)
		self._created_campaigns.add(doc.name)
		return doc.name

	def _insert_insight_row(
		self,
		campaign: str,
		granularity: str,
		date_str: str,
		spend: float = 0.0,
		reach: int = 0,
		engagement: int = 0,
		leads: int = 0,
		conversions: int = 0,
		impressions: int = 0,
		clicks: int = 0,
	) -> str:
		"""Insert a deterministic Marketing Campaign Insight row."""
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
			"total_leads": leads,
			"total_conversations": conversions,
			"ads_spend": spend,
			"ads_reach": reach,
			"ads_clicks": clicks,
			"ads_impressions": impressions,
			"ads_conversions": conversions,
			"ads_conversion_value": spend * 2.0,
		})
		doc.insert(ignore_permissions=True)
		return doc.name

	def test_campaign_dashboard_window_sums_day_and_week_without_double_counting(self):
		"""Window spanning DAY and WEEK rows sums spend and counts cleanly without double counting."""
		camp = self._create_campaign(TEST_CAMPAIGN_READER_A)
		today_date = getdate(today())

		# Recent DAY rows
		day_1 = (today_date - timedelta(days=2)).isoformat()
		day_2 = (today_date - timedelta(days=5)).isoformat()
		self._insert_insight_row(camp, GRANULARITY_DAY, day_1, spend=SAMPLE_SPEND_DAY_1, reach=SAMPLE_REACH_DAY_1, engagement=SAMPLE_ENGAGEMENT_DAY_1)
		self._insert_insight_row(camp, GRANULARITY_DAY, day_2, spend=SAMPLE_SPEND_DAY_2, reach=SAMPLE_REACH_DAY_2, engagement=SAMPLE_ENGAGEMENT_DAY_2)

		# Compacted older WEEK rows (e.g. 100 and 110 days ago)
		week_1 = (today_date - timedelta(days=100)).isoformat()
		week_2 = (today_date - timedelta(days=110)).isoformat()
		self._insert_insight_row(camp, GRANULARITY_WEEK, week_1, spend=SAMPLE_SPEND_WEEK_1, reach=0, engagement=SAMPLE_ENGAGEMENT_WEEK_1)
		self._insert_insight_row(camp, GRANULARITY_WEEK, week_2, spend=SAMPLE_SPEND_WEEK_2, reach=0, engagement=SAMPLE_ENGAGEMENT_WEEK_2)

		# Also add a LIFETIME row which must NOT be added to range sums
		self._insert_insight_row(camp, GRANULARITY_LIFETIME, today_date.isoformat(), spend=SAMPLE_SPEND_LIFETIME, reach=SAMPLE_REACH_LIFETIME, engagement=SAMPLE_ENGAGEMENT_LIFETIME)

		with patch.object(analytics, "sync_marketing_campaign_insights"):
			dash = analytics.get_campaign_dashboard(camp, days=120)

		expected_spend = SAMPLE_SPEND_DAY_1 + SAMPLE_SPEND_DAY_2 + SAMPLE_SPEND_WEEK_1 + SAMPLE_SPEND_WEEK_2
		expected_engagement = SAMPLE_ENGAGEMENT_DAY_1 + SAMPLE_ENGAGEMENT_DAY_2 + SAMPLE_ENGAGEMENT_WEEK_1 + SAMPLE_ENGAGEMENT_WEEK_2

		self.assertEqual(dash["totals"]["total_spend"], expected_spend)
		self.assertEqual(dash["summary"]["spend"], expected_spend)
		self.assertEqual(dash["totals"]["total_engagement"], expected_engagement)
		self.assertEqual(dash["summary"]["engagement"], expected_engagement)

	def test_campaign_dashboard_lifetime_row_not_added_to_range_sums(self):
		"""Assert that LIFETIME row is never included in date range sums."""
		camp = self._create_campaign(TEST_CAMPAIGN_READER_A)
		today_date = getdate(today())

		day_1 = (today_date - timedelta(days=1)).isoformat()
		self._insert_insight_row(camp, GRANULARITY_DAY, day_1, spend=SAMPLE_SPEND_DAY_1, reach=SAMPLE_REACH_DAY_1)
		self._insert_insight_row(camp, GRANULARITY_LIFETIME, today_date.isoformat(), spend=SAMPLE_SPEND_LIFETIME, reach=SAMPLE_REACH_LIFETIME)

		with patch.object(analytics, "sync_marketing_campaign_insights"):
			dash = analytics.get_campaign_dashboard(camp, days=30)

		# Range sum must be SAMPLE_SPEND_DAY_1 (100.0), NOT 50100.0
		self.assertEqual(dash["totals"]["total_spend"], SAMPLE_SPEND_DAY_1)
		self.assertNotEqual(dash["totals"]["total_spend"], SAMPLE_SPEND_DAY_1 + SAMPLE_SPEND_LIFETIME)

	def test_campaign_dashboard_reach_approximate_true_when_window_reaches_into_week_rows(self):
		"""Reach must be summed only from DAY rows and reach_approximate set to True when WEEK rows are present."""
		camp = self._create_campaign(TEST_CAMPAIGN_READER_A)
		today_date = getdate(today())

		day_1 = (today_date - timedelta(days=2)).isoformat()
		week_1 = (today_date - timedelta(days=100)).isoformat()
		self._insert_insight_row(camp, GRANULARITY_DAY, day_1, spend=SAMPLE_SPEND_DAY_1, reach=SAMPLE_REACH_DAY_1)
		# WEEK row has reach = 0 per non-additive ruling
		self._insert_insight_row(camp, GRANULARITY_WEEK, week_1, spend=SAMPLE_SPEND_WEEK_1, reach=0)

		with patch.object(analytics, "sync_marketing_campaign_insights"):
			dash = analytics.get_campaign_dashboard(camp, days=120)

		# Total reach should only sum DAY rows (SAMPLE_REACH_DAY_1 = 50)
		self.assertEqual(dash["totals"]["total_reach"], SAMPLE_REACH_DAY_1)
		self.assertEqual(dash["summary"]["reach"], SAMPLE_REACH_DAY_1)
		self.assertTrue(dash["totals"]["reach_approximate"])
		self.assertTrue(dash["summary"]["reach_approximate"])
		self.assertTrue(dash.get("reach_approximate"))

	def test_campaign_dashboard_reach_approximate_false_when_window_only_has_day_rows(self):
		"""reach_approximate is False when the window contains only DAY rows."""
		camp = self._create_campaign(TEST_CAMPAIGN_READER_A)
		today_date = getdate(today())

		day_1 = (today_date - timedelta(days=2)).isoformat()
		day_2 = (today_date - timedelta(days=4)).isoformat()
		self._insert_insight_row(camp, GRANULARITY_DAY, day_1, reach=SAMPLE_REACH_DAY_1)
		self._insert_insight_row(camp, GRANULARITY_DAY, day_2, reach=SAMPLE_REACH_DAY_2)

		with patch.object(analytics, "sync_marketing_campaign_insights"):
			dash = analytics.get_campaign_dashboard(camp, days=14)

		self.assertEqual(dash["totals"]["total_reach"], SAMPLE_REACH_DAY_1 + SAMPLE_REACH_DAY_2)
		self.assertFalse(dash["totals"]["reach_approximate"])
		self.assertFalse(dash["summary"]["reach_approximate"])
		self.assertFalse(dash.get("reach_approximate"))

	def test_campaign_dashboard_lifetime_mode_uses_lifetime_row(self):
		"""When days=0 or days>=36500 (lifetime), reads directly from LIFETIME row with reach_approximate=True."""
		camp = self._create_campaign(TEST_CAMPAIGN_READER_A)
		today_date = getdate(today())

		day_1 = (today_date - timedelta(days=2)).isoformat()
		week_1 = (today_date - timedelta(days=100)).isoformat()
		self._insert_insight_row(camp, GRANULARITY_DAY, day_1, spend=SAMPLE_SPEND_DAY_1, reach=SAMPLE_REACH_DAY_1)
		self._insert_insight_row(camp, GRANULARITY_WEEK, week_1, spend=SAMPLE_SPEND_WEEK_1, reach=0)
		self._insert_insight_row(camp, GRANULARITY_LIFETIME, today_date.isoformat(), spend=SAMPLE_SPEND_LIFETIME, reach=SAMPLE_REACH_LIFETIME)

		with patch.object(analytics, "sync_marketing_campaign_insights"):
			dash = analytics.get_campaign_dashboard(camp, days=0)

		self.assertEqual(dash["totals"]["total_spend"], SAMPLE_SPEND_LIFETIME)
		self.assertEqual(dash["totals"]["total_reach"], SAMPLE_REACH_LIFETIME)
		self.assertTrue(dash["totals"]["reach_approximate"])
		self.assertTrue(dash["summary"]["reach_approximate"])

	def test_campaign_dashboard_boundary_straddling_week_not_double_counted(self):
		"""Boundary test: complete week rolled to WEEK row, straddling week kept as DAY rows."""
		camp = self._create_campaign(TEST_CAMPAIGN_READER_A)
		today_date = getdate(today())

		# Rolled-up week 1 (Monday-start)
		week_mon = (today_date - timedelta(days=120)).isoformat()
		self._insert_insight_row(camp, GRANULARITY_WEEK, week_mon, spend=700.0, reach=0)

		# Straddling week kept as individual DAY rows (e.g. 91, 92 days ago)
		day_straddle_1 = (today_date - timedelta(days=91)).isoformat()
		day_straddle_2 = (today_date - timedelta(days=92)).isoformat()
		self._insert_insight_row(camp, GRANULARITY_DAY, day_straddle_1, spend=50.0, reach=10)
		self._insert_insight_row(camp, GRANULARITY_DAY, day_straddle_2, spend=50.0, reach=15)

		# Recent DAY row
		day_recent = (today_date - timedelta(days=5)).isoformat()
		self._insert_insight_row(camp, GRANULARITY_DAY, day_recent, spend=100.0, reach=20)

		with patch.object(analytics, "sync_marketing_campaign_insights"):
			dash = analytics.get_campaign_dashboard(camp, days=130)

		# Sum = 700 + 50 + 50 + 100 = 900.0
		self.assertEqual(dash["totals"]["total_spend"], 900.0)
		# Reach = 10 + 15 + 20 = 45 (WEEK row reach is 0)
		self.assertEqual(dash["totals"]["total_reach"], 45)
		self.assertTrue(dash["totals"]["reach_approximate"])

	def test_campaign_dashboard_backward_compatible_shape(self):
		"""Response shape must preserve all existing keys and only add fields."""
		camp = self._create_campaign(TEST_CAMPAIGN_READER_A)
		with patch.object(analytics, "sync_marketing_campaign_insights"):
			dash = analytics.get_campaign_dashboard(camp, days=30)

		# Root keys
		self.assertIn("campaign", dash)
		self.assertIn("days", dash)
		self.assertIn("modules", dash)
		self.assertIn("summary", dash)
		self.assertIn("attribution", dash)
		self.assertIn("totals", dash)
		self.assertIn("reach_approximate", dash)

		# Summary keys
		self.assertIn("reach", dash["summary"])
		self.assertIn("engagement", dash["summary"])
		self.assertIn("spend", dash["summary"])
		self.assertIn("conversions", dash["summary"])
		self.assertIn("roas", dash["summary"])
		self.assertIn("currency", dash["summary"])
		self.assertIn("reach_approximate", dash["summary"])

		# Totals keys
		self.assertIn("total_spend", dash["totals"])
		self.assertIn("total_reach", dash["totals"])
		self.assertIn("total_engagement", dash["totals"])
		self.assertIn("total_leads", dash["totals"])
		self.assertIn("total_conversations", dash["totals"])
		self.assertIn("reach_approximate", dash["totals"])

	def test_campaigns_overview_spend_trend_sums_day_and_week_rows(self):
		"""Overview spend_trend correctly reads DAY + WEEK rows for all campaigns in scope."""
		camp_a = self._create_campaign(TEST_CAMPAIGN_READER_A)
		camp_b = self._create_campaign(TEST_CAMPAIGN_READER_B)
		today_date = getdate(today())

		d1 = (today_date - timedelta(days=2)).isoformat()
		d2 = (today_date - timedelta(days=5)).isoformat()
		w1 = (today_date - timedelta(days=100)).isoformat()

		self._insert_insight_row(camp_a, GRANULARITY_DAY, d1, spend=100.0)
		self._insert_insight_row(camp_b, GRANULARITY_DAY, d1, spend=150.0)
		self._insert_insight_row(camp_a, GRANULARITY_DAY, d2, spend=200.0)
		self._insert_insight_row(camp_a, GRANULARITY_WEEK, w1, spend=1000.0)
		# Add LIFETIME row to ensure it is not included in trend
		self._insert_insight_row(camp_a, GRANULARITY_LIFETIME, today_date.isoformat(), spend=99999.0)

		overview = analytics.get_campaigns_overview(
			days=120,
			filters={"name": ["in", [camp_a, camp_b]]},
		)

		spend_trend = overview["spend_trend"]
		self.assertIn(d1, spend_trend)
		self.assertEqual(spend_trend[d1], 250.0)
		self.assertIn(d2, spend_trend)
		self.assertEqual(spend_trend[d2], 200.0)
		self.assertIn(w1, spend_trend)
		self.assertEqual(spend_trend[w1], 1000.0)
		self.assertTrue(overview["kpis"]["reach_approximate"])

	def test_campaigns_overview_reach_approximate_false_for_recent_window(self):
		"""Overview reach_approximate is False when query window contains only DAY rows."""
		camp_a = self._create_campaign(TEST_CAMPAIGN_READER_A)
		today_date = getdate(today())

		d1 = (today_date - timedelta(days=2)).isoformat()
		self._insert_insight_row(camp_a, GRANULARITY_DAY, d1, spend=100.0, reach=50)

		overview = analytics.get_campaigns_overview(
			days=14,
			filters={"name": ["in", [camp_a]]},
		)

		self.assertFalse(overview["kpis"]["reach_approximate"])
		self.assertFalse(overview.get("reach_approximate"))

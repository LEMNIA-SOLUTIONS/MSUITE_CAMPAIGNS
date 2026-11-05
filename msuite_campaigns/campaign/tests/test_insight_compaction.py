"""Tests for Marketing Campaign Insight compaction, bounded refresh, and rollup (Task D1).

All external calls are avoided/mocked; zero live HTTP/LLM calls.
"""

from datetime import date, timedelta
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, getdate, today

from msuite_campaigns.campaign.services import analytics
from msuite_core.constants import (
	CAMPAIGN_DAY_REFRESH_DAYS,
	GRANULARITY_DAY,
	GRANULARITY_LIFETIME,
	GRANULARITY_WEEK,
	INSIGHT_DAY_KEEP_DAYS,
)
from msuite_core.utils.insight_keys import make_insight_key

DOCTYPE_CAMPAIGN = "Marketing Campaign"
DOCTYPE_INSIGHT = "Marketing Campaign Insight"
TEST_CAMPAIGN_NAME = "Test-Compaction-Camp-Alpha"
TEST_CAMPAIGN_ENDED = "Test-Compaction-Camp-Ended"
TEST_CAMPAIGN_FUTURE = "Test-Compaction-Camp-Future"
TEST_CAMPAIGN_NO_END = "Test-Compaction-Camp-NoEnd"


class TestCampaignInsightCompaction(IntegrationTestCase):
	"""Test suite for Marketing Campaign Insight compaction and bounded refresh."""

	def setUp(self):
		"""Clean up test campaign insights and ensure clean state."""
		super().setUp()
		self._created_campaigns: set[str] = set()
		for name in (
			TEST_CAMPAIGN_NAME,
			TEST_CAMPAIGN_ENDED,
			TEST_CAMPAIGN_FUTURE,
			TEST_CAMPAIGN_NO_END,
		):
			existing = frappe.db.get_value(DOCTYPE_CAMPAIGN, {"campaign_name": name}, "name")
			if existing:
				frappe.db.delete(DOCTYPE_INSIGHT, {"marketing_campaign": existing})
				frappe.delete_doc(DOCTYPE_CAMPAIGN, existing, force=True, ignore_permissions=True)
		frappe.db.commit()

	def tearDown(self):
		"""Clean up created test campaigns and insights."""
		for camp_id in list(self._created_campaigns):
			frappe.db.delete(DOCTYPE_INSIGHT, {"marketing_campaign": camp_id})
			if frappe.db.exists(DOCTYPE_CAMPAIGN, camp_id):
				frappe.delete_doc(DOCTYPE_CAMPAIGN, camp_id, force=True, ignore_permissions=True)
		self._created_campaigns.clear()
		frappe.db.commit()
		super().tearDown()

	def _create_campaign(
		self,
		name: str,
		start_date: str | date,
		end_date: str | date | None = None,
		status: str = "Active",
	) -> str:
		"""Helper to create or get a Marketing Campaign with specific dates."""
		existing = frappe.db.get_value(DOCTYPE_CAMPAIGN, {"campaign_name": name}, "name")
		if existing:
			frappe.db.delete(DOCTYPE_INSIGHT, {"marketing_campaign": existing})
			doc = frappe.get_doc(DOCTYPE_CAMPAIGN, existing)
			doc.start_date = str(start_date)
			doc.end_date = str(end_date) if end_date else None
			doc.status = status
			if not end_date:
				doc.flags.ignore_validate = True
			doc.save(ignore_permissions=True)
			self._created_campaigns.add(doc.name)
			return doc.name

		doc = frappe.get_doc({
			"doctype": DOCTYPE_CAMPAIGN,
			"campaign_name": name,
			"status": status,
			"start_date": str(start_date),
			"end_date": str(end_date) if end_date else None,
			"goal": "Compaction Test Campaign",
		})
		if not end_date or (end_date and str(end_date) < str(start_date)):
			doc.flags.ignore_validate = True
		doc.insert(ignore_permissions=True)
		frappe.db.delete(DOCTYPE_INSIGHT, {"marketing_campaign": doc.name})
		self._created_campaigns.add(doc.name)
		return doc.name

	def test_lifetime_row_is_one_per_campaign_across_days(self):
		"""Simulate running sync across 3 distinct calendar days; verify exactly 1 LIFETIME row."""
		camp = self._create_campaign(
			TEST_CAMPAIGN_NAME,
			start_date="2026-10-01",
			end_date="2026-10-31",
		)

		day_1 = "2026-10-10"
		day_2 = "2026-10-15"
		day_3 = "2026-10-20"

		for current_day in (day_1, day_2, day_3):
			with patch("msuite_campaigns.campaign.services.analytics.today", return_value=current_day):
				analytics.sync_marketing_campaign_insights(camp)

		lifetime_rows = frappe.get_all(
			DOCTYPE_INSIGHT,
			filters={"marketing_campaign": camp, "granularity": GRANULARITY_LIFETIME},
			fields=["name", "insight_key", "date", "granularity"],
		)
		self.assertEqual(len(lifetime_rows), 1)
		expected_key = make_insight_key(DOCTYPE_CAMPAIGN, camp, GRANULARITY_LIFETIME, "")
		self.assertEqual(lifetime_rows[0].insight_key, expected_key)

	def test_hourly_run_touches_only_recent_days(self):
		"""Hourly sync on an ongoing campaign with existing rows touches only the last CAMPAIGN_DAY_REFRESH_DAYS days."""
		frozen_today = "2026-11-04"
		start_date = "2026-10-01"  # > 30 days before today
		camp = self._create_campaign(
			TEST_CAMPAIGN_NAME,
			start_date=start_date,
			end_date="2026-11-30",
		)

		with patch("msuite_campaigns.campaign.services.analytics.today", return_value=frozen_today):
			# First run: backfill all days so DAY rows exist
			analytics.sync_marketing_campaign_insights(camp)

		# Now simulate an hourly sync; spy _sync_campaign_day calls
		with patch("msuite_campaigns.campaign.services.analytics.today", return_value=frozen_today):
			with patch.object(analytics, "_sync_campaign_day", wraps=analytics._sync_campaign_day) as spy_day:
				analytics.sync_marketing_campaign_insights(camp)

				called_dates = [call_args[0][1] for call_args in spy_day.call_args_list]
				self.assertGreater(len(called_dates), 0)

				earliest_refreshed = add_days(frozen_today, -CAMPAIGN_DAY_REFRESH_DAYS)
				for d in called_dates:
					self.assertGreaterEqual(
						d,
						earliest_refreshed,
						f"Hourly sync unexpectedly touched old date {d} (earliest allowed: {earliest_refreshed})",
					)
				# 2026-10-01 should definitely NOT be touched
				self.assertNotIn("2026-10-01", called_dates)

	def test_first_run_backfills_all_days(self):
		"""When no DAY rows exist yet (or full=True), backfill all days from start_date to today."""
		frozen_today = "2026-10-25"
		start_date = "2026-10-10"  # 15 days of history
		camp = self._create_campaign(
			TEST_CAMPAIGN_NAME,
			start_date=start_date,
			end_date="2026-10-31",
		)

		with patch("msuite_campaigns.campaign.services.analytics.today", return_value=frozen_today):
			with patch.object(analytics, "_sync_campaign_day", wraps=analytics._sync_campaign_day) as spy_day:
				analytics.sync_marketing_campaign_insights(camp)

				called_dates = [call_args[0][1] for call_args in spy_day.call_args_list]
				self.assertEqual(len(called_dates), 16)  # 10th to 25th inclusive
				self.assertIn("2026-10-10", called_dates)
				self.assertIn("2026-10-25", called_dates)

		# If full=True is passed, it should backfill all days even if DAY rows already exist
		with patch("msuite_campaigns.campaign.services.analytics.today", return_value=frozen_today):
			with patch.object(analytics, "_sync_campaign_day", wraps=analytics._sync_campaign_day) as spy_full:
				analytics.sync_marketing_campaign_insights(camp, full=True)

				full_dates = [call_args[0][1] for call_args in spy_full.call_args_list]
				self.assertEqual(len(full_dates), 16)
				self.assertIn("2026-10-10", full_dates)

	def test_ended_campaign_not_recomputed(self):
		"""An ended campaign older than CAMPAIGN_DAY_REFRESH_DAYS with a LIFETIME row is skipped."""
		frozen_today = "2026-11-04"
		end_date = add_days(frozen_today, -(CAMPAIGN_DAY_REFRESH_DAYS + 10))
		start_date = add_days(end_date, -20)
		camp = self._create_campaign(
			TEST_CAMPAIGN_ENDED,
			start_date=start_date,
			end_date=end_date,
			status="Completed",
		)

		# Seed a LIFETIME row for this ended campaign
		key = make_insight_key(DOCTYPE_CAMPAIGN, camp, GRANULARITY_LIFETIME, "")
		frappe.get_doc({
			"doctype": DOCTYPE_INSIGHT,
			"marketing_campaign": camp,
			"date": end_date,
			"granularity": GRANULARITY_LIFETIME,
			"insight_key": key,
			"total_spend": 150.0,
		}).insert(ignore_permissions=True)

		with patch("msuite_campaigns.campaign.services.analytics.today", return_value=frozen_today):
			with patch.object(analytics, "_sync_campaign_day") as mock_day:
				with patch.object(analytics, "_sync_campaign_lifetime") as mock_life:
					analytics.sync_marketing_campaign_insights(camp)
					mock_day.assert_not_called()
					mock_life.assert_not_called()

	def test_campaign_with_no_end_date(self):
		"""Campaign without an end_date syncs safely up to today."""
		frozen_today = "2026-11-04"
		start_date = "2026-11-01"
		camp = self._create_campaign(
			TEST_CAMPAIGN_NO_END,
			start_date=start_date,
			end_date=None,
		)

		with patch("msuite_campaigns.campaign.services.analytics.today", return_value=frozen_today):
			with patch.object(analytics, "_sync_campaign_day", wraps=analytics._sync_campaign_day) as spy_day:
				analytics.sync_marketing_campaign_insights(camp)
				called_dates = [call_args[0][1] for call_args in spy_day.call_args_list]
				self.assertIn("2026-11-01", called_dates)
				self.assertIn(frozen_today, called_dates)

	def test_start_after_today_writes_nothing(self):
		"""Campaign whose start_date is in the future produces 0 rows."""
		frozen_today = "2026-11-04"
		camp = self._create_campaign(
			TEST_CAMPAIGN_FUTURE,
			start_date="2026-11-10",
			end_date="2026-11-20",
		)

		with patch("msuite_campaigns.campaign.services.analytics.today", return_value=frozen_today):
			analytics.sync_marketing_campaign_insights(camp)

		rows = frappe.get_all(DOCTYPE_INSIGHT, filters={"marketing_campaign": camp})
		self.assertEqual(len(rows), 0)

	def test_week_rows_replace_old_days(self):
		"""rebuild_campaign_weeks aggregates DAY rows older than INSIGHT_DAY_KEEP_DAYS into Monday-start WEEK rows and prunes DAY rows."""
		frozen_today = "2026-11-04"
		camp = self._create_campaign(
			TEST_CAMPAIGN_NAME,
			start_date="2026-05-01",
			end_date="2026-11-30",
		)

		old_date = date.fromisoformat(add_days(frozen_today, -(INSIGHT_DAY_KEEP_DAYS + 14)))
		monday = old_date - timedelta(days=old_date.weekday())
		# Insert 7 daily rows for that week
		for i in range(7):
			d = monday + timedelta(days=i)
			key = make_insight_key(DOCTYPE_CAMPAIGN, camp, GRANULARITY_DAY, d.isoformat())
			frappe.get_doc({
				"doctype": DOCTYPE_INSIGHT,
				"marketing_campaign": camp,
				"date": d.isoformat(),
				"granularity": GRANULARITY_DAY,
				"insight_key": key,
				"ads_spend": 10.0,
				"ads_impressions": 100,
				"ads_clicks": 5,
				"ads_reach": 80,
				"ads_conversions": 2,
				"ads_conversion_value": 30.0,
				"social_impressions": 50,
				"social_reach": 40,
				"social_engagement": 10,
				"social_posts": 1,
				"whatsapp_sent": 20,
				"whatsapp_delivered": 18,
				"whatsapp_read": 12,
				"total_spend": 10.0,
				"total_reach": 80 + 40 + 18,
				"total_engagement": 5 + 10 + 12,
				"total_leads": 1,
				"total_conversations": 1,
			}).insert(ignore_permissions=True)

		# Insert a recent DAY row (yesterday, not older than retention window)
		recent_date = "2026-11-03"
		recent_key = make_insight_key(DOCTYPE_CAMPAIGN, camp, GRANULARITY_DAY, recent_date)
		frappe.get_doc({
			"doctype": DOCTYPE_INSIGHT,
			"marketing_campaign": camp,
			"date": recent_date,
			"granularity": GRANULARITY_DAY,
			"insight_key": recent_key,
			"total_spend": 5.0,
		}).insert(ignore_permissions=True)

		with patch("msuite_campaigns.campaign.services.analytics.today", return_value=frozen_today):
			rebuilt_count = analytics.rebuild_campaign_weeks(camp)

		self.assertGreaterEqual(rebuilt_count, 1)

		# 1. WEEK row must exist for Monday
		week_rows = frappe.get_all(
			DOCTYPE_INSIGHT,
			filters={"marketing_campaign": camp, "granularity": GRANULARITY_WEEK},
			fields=["*"],
		)
		self.assertEqual(len(week_rows), 1)
		w = week_rows[0]
		self.assertEqual(str(w.date), monday.isoformat())
		expected_week_key = make_insight_key(DOCTYPE_CAMPAIGN, camp, GRANULARITY_WEEK, monday.isoformat())
		self.assertEqual(w.insight_key, expected_week_key)

		# Check additive totals: 7 days * daily amounts
		self.assertAlmostEqual(float(w.ads_spend), 70.0)
		self.assertEqual(int(w.ads_impressions), 700)
		self.assertEqual(int(w.ads_clicks), 35)
		# Non-additive reach stays 0 on WEEK rows
		self.assertEqual(int(w.ads_reach), 0)
		self.assertEqual(int(w.social_reach), 0)
		self.assertEqual(int(w.total_reach), 0)
		self.assertEqual(int(w.ads_conversions), 14)
		self.assertAlmostEqual(float(w.ads_conversion_value), 210.0)
		self.assertEqual(int(w.social_posts), 7)
		self.assertEqual(int(w.whatsapp_sent), 140)
		self.assertEqual(int(w.whatsapp_delivered), 126)
		self.assertEqual(int(w.whatsapp_read), 84)

		# Check recomputed ratio columns
		# ads_ctr: (35 / 700) * 100 = 5.0%
		self.assertAlmostEqual(float(w.ads_ctr), 5.0)
		# ads_cpc: 70.0 / 35 = 2.0
		self.assertAlmostEqual(float(w.ads_cpc), 2.0)
		# ads_cpm: (70.0 / 700) * 1000 = 100.0
		self.assertAlmostEqual(float(w.ads_cpm), 100.0)
		# ads_roas: 210.0 / 70.0 = 3.0
		self.assertAlmostEqual(float(w.ads_roas), 3.0)
		# Reach-dependent ratios stay 0.0 on WEEK rows
		self.assertAlmostEqual(float(w.ads_frequency), 0.0)
		self.assertAlmostEqual(float(w.social_engagement_rate), 0.0)

		# 2. Old 7 DAY rows should be deleted
		old_day_rows = frappe.get_all(
			DOCTYPE_INSIGHT,
			filters={
				"marketing_campaign": camp,
				"granularity": GRANULARITY_DAY,
				"date": ["between", ["2026-06-01", "2026-06-07"]],
			},
		)
		self.assertEqual(len(old_day_rows), 0)

		# 3. Recent DAY row (yesterday) must still exist
		recent_row = frappe.db.get_value(
			DOCTYPE_INSIGHT,
			{"marketing_campaign": camp, "granularity": GRANULARITY_DAY, "date": recent_date},
			"name",
		)
		self.assertIsNotNone(recent_row)

	def test_patch_compacts_lifetime_rows(self):
		"""Patch campaign_compact_insight_rows retains newest LIFETIME row per campaign and re-keys it."""
		from msuite_campaigns.patches.campaign_compact_insight_rows import execute as run_patch

		camp = self._create_campaign(
			TEST_CAMPAIGN_NAME,
			start_date="2026-09-01",
			end_date="2026-10-31",
		)

		# Create 3 legacy LIFETIME rows with different dates and old-style keys
		legacy_keys = [
			f"old-key-1-{camp}",
			f"old-key-2-{camp}",
			f"old-key-3-{camp}",
		]
		docs = []
		for idx, key in enumerate(legacy_keys):
			d = frappe.get_doc({
				"doctype": DOCTYPE_INSIGHT,
				"marketing_campaign": camp,
				"date": f"2026-10-{10 + idx}",
				"granularity": GRANULARITY_LIFETIME,
				"insight_key": key,
				"total_spend": 100.0 + idx,
			})
			d.insert(ignore_permissions=True)
			docs.append(d)

		self.assertEqual(
			frappe.db.count(
				DOCTYPE_INSIGHT,
				{"marketing_campaign": camp, "granularity": GRANULARITY_LIFETIME},
			),
			3,
		)

		run_patch()

		remaining = frappe.get_all(
			DOCTYPE_INSIGHT,
			filters={"marketing_campaign": camp, "granularity": GRANULARITY_LIFETIME},
			fields=["name", "insight_key", "total_spend"],
		)
		self.assertEqual(len(remaining), 1)
		expected_key = make_insight_key(DOCTYPE_CAMPAIGN, camp, GRANULARITY_LIFETIME, "")
		self.assertEqual(remaining[0].insight_key, expected_key)
		# Newest row retained (docs[-1])
		self.assertEqual(remaining[0].name, docs[-1].name)
		self.assertAlmostEqual(float(remaining[0].total_spend), docs[-1].total_spend)

	def test_week_straddling_cutoff_not_rolled_partially(self):
		"""A week straddling the retention cutoff stays as DAY rows until the whole week is old."""
		camp = self._create_campaign(
			TEST_CAMPAIGN_NAME,
			start_date="2026-05-01",
			end_date="2026-11-30",
		)

		# Monday 2026-08-03 to Sunday 2026-08-09
		monday = date(2026, 8, 3)
		sunday = monday + timedelta(days=6)
		week_dates = [monday + timedelta(days=i) for i in range(7)]

		for d in week_dates:
			key = make_insight_key(DOCTYPE_CAMPAIGN, camp, GRANULARITY_DAY, d.isoformat())
			frappe.get_doc({
				"doctype": DOCTYPE_INSIGHT,
				"marketing_campaign": camp,
				"date": d.isoformat(),
				"granularity": GRANULARITY_DAY,
				"insight_key": key,
				"ads_spend": 10.0,
				"ads_impressions": 100,
				"ads_clicks": 5,
				"ads_reach": 80,
				"ads_conversions": 2,
				"ads_conversion_value": 30.0,
				"social_impressions": 50,
				"social_reach": 40,
				"social_engagement": 10,
				"social_posts": 1,
				"whatsapp_sent": 20,
				"whatsapp_delivered": 18,
				"whatsapp_read": 12,
				"total_spend": 10.0,
				"total_reach": 80 + 40 + 18,
				"total_engagement": 5 + 10 + 12,
				"total_leads": 1,
				"total_conversations": 1,
			}).insert(ignore_permissions=True)

		# Cutoff 1: Thursday 2026-08-06 (straddles the week: Mon..Wed < cutoff, Thu..Sun >= cutoff)
		cutoff_1 = date(2026, 8, 6)
		as_of_1 = add_days(cutoff_1, INSIGHT_DAY_KEEP_DAYS)

		rebuilt_count_1 = analytics.rebuild_campaign_weeks(camp, as_of=as_of_1)
		self.assertEqual(rebuilt_count_1, 0)

		# Cutoff 2: Friday 2026-08-07 (one day apart, still straddles the week)
		as_of_2 = add_days(as_of_1, 1)

		rebuilt_count_2 = analytics.rebuild_campaign_weeks(camp, as_of=as_of_2)
		self.assertEqual(rebuilt_count_2, 0)

		# Assert the straddling week still has all 7 DAY rows and no WEEK row exists
		day_rows_count = frappe.db.count(
			DOCTYPE_INSIGHT,
			{
				"marketing_campaign": camp,
				"granularity": GRANULARITY_DAY,
				"date": ["between", [monday.isoformat(), sunday.isoformat()]],
			},
		)
		self.assertEqual(day_rows_count, 7)

		week_rows_count = frappe.db.count(
			DOCTYPE_INSIGHT,
			{
				"marketing_campaign": camp,
				"granularity": GRANULARITY_WEEK,
				"date": monday.isoformat(),
			},
		)
		self.assertEqual(week_rows_count, 0)

		# Advance as_of so the whole week is older than the cutoff:
		# Sunday 2026-08-09 < cutoff requires cutoff to be at least 2026-08-10 (Monday)
		cutoff_3 = date(2026, 8, 10)
		as_of_3 = add_days(cutoff_3, INSIGHT_DAY_KEEP_DAYS)

		rebuilt_count_3 = analytics.rebuild_campaign_weeks(camp, as_of=as_of_3)
		self.assertEqual(rebuilt_count_3, 1)

		# Assert WEEK row exists and equals the sum of all 7 days
		week_rows = frappe.get_all(
			DOCTYPE_INSIGHT,
			filters={"marketing_campaign": camp, "granularity": GRANULARITY_WEEK, "date": monday.isoformat()},
			fields=["*"],
		)
		self.assertEqual(len(week_rows), 1)
		w = week_rows[0]
		self.assertAlmostEqual(float(w.ads_spend), 70.0)
		self.assertEqual(int(w.ads_impressions), 700)
		self.assertEqual(int(w.ads_clicks), 35)
		self.assertEqual(int(w.ads_reach), 0)
		self.assertEqual(int(w.social_reach), 0)
		self.assertEqual(int(w.total_reach), 0)
		self.assertEqual(int(w.ads_conversions), 14)
		self.assertAlmostEqual(float(w.ads_conversion_value), 210.0)
		self.assertEqual(int(w.social_posts), 7)
		self.assertEqual(int(w.whatsapp_sent), 140)
		self.assertEqual(int(w.whatsapp_delivered), 126)
		self.assertEqual(int(w.whatsapp_read), 84)

		# Assert DAY rows are now pruned
		remaining_day_count = frappe.db.count(
			DOCTYPE_INSIGHT,
			{
				"marketing_campaign": camp,
				"granularity": GRANULARITY_DAY,
				"date": ["between", [monday.isoformat(), sunday.isoformat()]],
			},
		)
		self.assertEqual(remaining_day_count, 0)

	def test_bounded_refresh_with_zero_day_rows_and_existing_week_row(self):
		"""Campaign with existing WEEK row and 0 DAY rows only refreshes last CAMPAIGN_DAY_REFRESH_DAYS."""
		camp = self._create_campaign(
			TEST_CAMPAIGN_NAME,
			start_date="2026-05-01",
			end_date="2026-11-30",
		)

		# Seed a WEEK row for an old week, but 0 DAY rows
		week_monday = "2026-05-04"
		week_key = make_insight_key(DOCTYPE_CAMPAIGN, camp, GRANULARITY_WEEK, week_monday)
		frappe.get_doc({
			"doctype": DOCTYPE_INSIGHT,
			"marketing_campaign": camp,
			"date": week_monday,
			"granularity": GRANULARITY_WEEK,
			"insight_key": week_key,
			"total_spend": 50.0,
		}).insert(ignore_permissions=True)

		self.assertEqual(
			frappe.db.count(
				DOCTYPE_INSIGHT,
				{"marketing_campaign": camp, "granularity": GRANULARITY_DAY},
			),
			0,
		)

		mock_today = "2026-10-20"
		with patch("msuite_campaigns.campaign.services.analytics.today", return_value=mock_today):
			analytics.sync_marketing_campaign_insights(camp, full=False)

		# If bounded refresh is respected, only dates from max(start_date, mock_today - 7d) are synced
		# That is: 2026-10-13 to 2026-10-20 (8 days). No DAY rows from May/June should be inserted!
		day_rows = frappe.get_all(
			DOCTYPE_INSIGHT,
			filters={"marketing_campaign": camp, "granularity": GRANULARITY_DAY},
			fields=["date"],
			order_by="date asc",
		)
		self.assertGreater(len(day_rows), 0)
		earliest_synced_day = str(day_rows[0].date)
		cutoff_str = add_days(mock_today, -CAMPAIGN_DAY_REFRESH_DAYS)
		self.assertGreaterEqual(earliest_synced_day, cutoff_str)

	def test_upsert_fallback_on_unique_validation_error(self):
		"""Simulate UniqueValidationError on insert falling back to update when row exists."""
		camp = self._create_campaign(
			TEST_CAMPAIGN_NAME,
			start_date="2026-10-01",
			end_date="2026-10-31",
		)
		key = make_insight_key(DOCTYPE_CAMPAIGN, camp, GRANULARITY_DAY, "2026-10-15")
		existing_doc = frappe.get_doc({
			"doctype": DOCTYPE_INSIGHT,
			"marketing_campaign": camp,
			"date": "2026-10-15",
			"granularity": GRANULARITY_DAY,
			"insight_key": key,
			"total_spend": 25.0,
		})
		existing_doc.insert(ignore_permissions=True)

		doc_dict = {
			"doctype": DOCTYPE_INSIGHT,
			"marketing_campaign": camp,
			"date": "2026-10-15",
			"granularity": GRANULARITY_DAY,
			"total_spend": 55.0,
		}

		orig_get_value = frappe.db.get_value
		call_count = {"count": 0}

		def mock_get_value(doctype, filters, *args, **kwargs):
			if doctype == DOCTYPE_INSIGHT and isinstance(filters, dict) and "insight_key" in filters:
				call_count["count"] += 1
				if call_count["count"] == 1:
					return None
			return orig_get_value(doctype, filters, *args, **kwargs)

		with patch("frappe.db.get_value", side_effect=mock_get_value):
			with patch.object(frappe.model.document.Document, "insert", side_effect=frappe.exceptions.UniqueValidationError):
				analytics._upsert_campaign_insight(doc_dict)

		updated = frappe.db.get_value(
			DOCTYPE_INSIGHT,
			{"insight_key": key},
			["name", "total_spend"],
			as_dict=True,
		)
		self.assertEqual(updated.name, existing_doc.name)
		self.assertAlmostEqual(float(updated.total_spend), 55.0)

	def test_upsert_raises_if_fallback_finds_no_existing(self):
		"""If insert raises UniqueValidationError but no existing row is found, re-raises."""
		doc_dict = {
			"doctype": DOCTYPE_INSIGHT,
			"marketing_campaign": "NonExistentCamp",
			"date": "2026-10-15",
			"granularity": GRANULARITY_DAY,
			"total_spend": 10.0,
		}

		with patch("frappe.db.get_value", return_value=None):
			with patch.object(frappe.model.document.Document, "insert", side_effect=frappe.exceptions.UniqueValidationError):
				with self.assertRaises(frappe.exceptions.UniqueValidationError):
					analytics._upsert_campaign_insight(doc_dict)

	def test_inverted_dates_return_early(self):
		"""Campaign with end_date < start_date exits early and writes no insight rows."""
		camp = self._create_campaign(
			TEST_CAMPAIGN_NAME,
			start_date="2026-10-15",
			end_date="2026-10-05",
		)
		analytics.sync_marketing_campaign_insights(camp)
		self.assertEqual(
			frappe.db.count(DOCTYPE_INSIGHT, {"marketing_campaign": camp}),
			0,
		)

	def test_patch_dedupes_and_rekeys_day_and_week_rows(self):
		"""Patch rekeys legacy DAY and WEEK rows and deduplicates rows with same expected key."""
		from msuite_campaigns.patches.campaign_compact_insight_rows import execute as run_patch

		camp = self._create_campaign(
			TEST_CAMPAIGN_NAME,
			start_date="2026-09-01",
			end_date="2026-10-31",
		)

		# Legacy DAY row 1 with old key
		d1 = frappe.get_doc({
			"doctype": DOCTYPE_INSIGHT,
			"marketing_campaign": camp,
			"date": "2026-09-10",
			"granularity": GRANULARITY_DAY,
			"insight_key": "legacy-day-key-1",
			"total_spend": 20.0,
		})
		d1.insert(ignore_permissions=True)

		# Duplicate legacy DAY row 2 for same campaign, date, granularity
		d2 = frappe.get_doc({
			"doctype": DOCTYPE_INSIGHT,
			"marketing_campaign": camp,
			"date": "2026-09-10",
			"granularity": GRANULARITY_DAY,
			"insight_key": "legacy-day-key-2",
			"total_spend": 30.0,
		})
		d2.insert(ignore_permissions=True)

		# Legacy WEEK row with unkeyed insight_key
		w1 = frappe.get_doc({
			"doctype": DOCTYPE_INSIGHT,
			"marketing_campaign": camp,
			"date": "2026-09-08",
			"granularity": GRANULARITY_WEEK,
			"insight_key": "",
			"total_spend": 140.0,
		})
		w1.insert(ignore_permissions=True)

		run_patch()

		# Exactly 1 DAY row retained for 2026-09-10 with expected key
		day_rows = frappe.get_all(
			DOCTYPE_INSIGHT,
			filters={"marketing_campaign": camp, "granularity": GRANULARITY_DAY, "date": "2026-09-10"},
			fields=["name", "insight_key", "total_spend"],
		)
		self.assertEqual(len(day_rows), 1)
		expected_day_key = make_insight_key(DOCTYPE_CAMPAIGN, camp, GRANULARITY_DAY, "2026-09-10")
		self.assertEqual(day_rows[0].insight_key, expected_day_key)
		self.assertEqual(day_rows[0].name, d2.name)

		# WEEK row re-keyed
		week_rows = frappe.get_all(
			DOCTYPE_INSIGHT,
			filters={"marketing_campaign": camp, "granularity": GRANULARITY_WEEK, "date": "2026-09-08"},
			fields=["name", "insight_key"],
		)
		self.assertEqual(len(week_rows), 1)
		expected_week_key = make_insight_key(DOCTYPE_CAMPAIGN, camp, GRANULARITY_WEEK, "2026-09-08")
		self.assertEqual(week_rows[0].insight_key, expected_week_key)

	def test_campaign_rebuild_weeks_with_recent_since_rolls_old_complete_weeks(self):
		"""rebuild_campaign_weeks rolls old complete weeks even when since is recent (e.g. 7 days ago)."""
		today_date = getdate(today())
		cutoff = today_date - timedelta(days=INSIGHT_DAY_KEEP_DAYS)

		# Choose a complete week older than cutoff
		past_ref = cutoff - timedelta(days=21)
		monday = past_ref - timedelta(days=past_ref.weekday())
		camp = self._create_campaign(f"cmp-recent-since-{frappe.generate_hash(length=6)}", start_date=monday.isoformat())

		for day_idx in range(7):
			d = monday + timedelta(days=day_idx)
			doc = frappe.get_doc({
				"doctype": DOCTYPE_INSIGHT,
				"marketing_campaign": camp,
				"date": d.isoformat(),
				"granularity": GRANULARITY_DAY,
				"insight_key": make_insight_key(DOCTYPE_CAMPAIGN, camp, GRANULARITY_DAY, d.isoformat()),
				"ads_spend": 10.0,
				"ads_impressions": 100,
				"ads_clicks": 5,
			})
			doc.flags.ignore_permissions = True
			doc.insert()

		recent_since = today_date - timedelta(days=7)
		rebuilt = analytics.rebuild_campaign_weeks(camp, since=recent_since)
		self.assertEqual(rebuilt, 1)

		week_rows = frappe.get_all(
			DOCTYPE_INSIGHT,
			filters={"marketing_campaign": camp, "granularity": GRANULARITY_WEEK, "date": monday.isoformat()},
		)
		self.assertEqual(len(week_rows), 1)

		remaining_days = frappe.db.count(
			DOCTYPE_INSIGHT,
			{
				"marketing_campaign": camp,
				"granularity": GRANULARITY_DAY,
				"date": ("between", [monday.isoformat(), (monday + timedelta(days=6)).isoformat()]),
			},
		)
		self.assertEqual(remaining_days, 0)




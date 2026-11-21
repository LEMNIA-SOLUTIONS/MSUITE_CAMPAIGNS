# Copyright (c) 2026, MSuite and contributors
# For license information, please see license.txt

"""Tests for Broadcast Scheduler service."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, add_to_date, now_datetime

from msuite_campaigns.broadcast.scheduler import start_due_channels

TEST_PREFIX = "ZZTestBS "


class TestBroadcastScheduler(IntegrationTestCase):
	"""Test suite for broadcast/scheduler.py."""

	def setUp(self):
		super().setUp()
		self.cleanup_broadcasts: list[str] = []
		self.cleanup_campaigns: list[str] = []

	def tearDown(self):
		frappe.db.rollback()
		for name in self.cleanup_broadcasts:
			if frappe.db.exists("Broadcast", name):
				frappe.delete_doc("Broadcast", name, force=True, ignore_permissions=True)
		for name in self.cleanup_campaigns:
			if frappe.db.exists("Marketing Campaign", name):
				frappe.delete_doc("Marketing Campaign", name, force=True, ignore_permissions=True)

	def _make_campaign(self, status: str = "Active") -> str:
		camp = frappe.get_doc(
			{
				"doctype": "Marketing Campaign",
				"campaign_name": f"{TEST_PREFIX}Camp {frappe.generate_hash(length=5)}",
				"status": status,
				"start_date": now_datetime(),
				"end_date": add_to_date(now_datetime(), days=7),
			}
		)
		camp.flags.ignore_permissions = True
		camp.insert(ignore_links=True)
		self.cleanup_campaigns.append(camp.name)
		return camp.name

	def _make_broadcast(
		self,
		channel: str = "WhatsApp",
		status: str = "Scheduled",
		channel_status: str = "Scheduled",
		scheduled_time=None,
		campaign: str | None = None,
	) -> "frappe.model.document.Document":
		future_time = add_to_date(now_datetime(), hours=2)
		doc = frappe.get_doc(
			{
				"doctype": "Broadcast",
				"title": f"{TEST_PREFIX}{frappe.generate_hash(length=6)}",
				"recipient_source": "Individual",
				"status": status,
				"scheduled_time": future_time if scheduled_time else None,
				"campaign": campaign,
				"channels": [
					{
						"channel": channel,
						"status": channel_status,
						"whatsapp_account": "WA-ACC-001" if channel == "WhatsApp" else None,
						"whatsapp_template": "tpl_wa_001" if channel == "WhatsApp" else None,
					}
				],
			}
		)
		doc.insert(ignore_links=True)
		if scheduled_time:
			frappe.db.set_value("Broadcast", doc.name, "scheduled_time", scheduled_time, update_modified=False)
			doc.scheduled_time = scheduled_time
		self.cleanup_broadcasts.append(doc.name)
		return doc

	def test_due_vs_future_vs_inactive_campaign(self):
		"""start_due_channels starts only due rows whose campaign is active or empty."""
		now = now_datetime()
		due_time = add_to_date(now, minutes=-10)
		future_time = add_to_date(now, hours=2)

		active_camp = self._make_campaign("Active")
		inactive_camp = self._make_campaign("Draft")

		# 1. Due, no campaign -> should start
		b1 = self._make_broadcast(scheduled_time=due_time)
		# 2. Due, active campaign -> should start
		b2 = self._make_broadcast(scheduled_time=due_time, campaign=active_camp)
		# 3. Future scheduled_time -> should NOT start
		b3 = self._make_broadcast(scheduled_time=future_time)
		# 4. Due, but inactive campaign -> should NOT start
		b4 = self._make_broadcast(scheduled_time=due_time, campaign=inactive_camp)

		started_calls = []

		def fake_start_channel(broadcast, channel):
			started_calls.append((broadcast, channel))

		with patch("msuite_campaigns.broadcast.hub.start_channel", side_effect=fake_start_channel):
			started_count = start_due_channels()

		self.assertEqual(started_count, 2)
		self.assertEqual(len(started_calls), 2)
		started_broadcasts = {call[0] for call in started_calls}
		self.assertIn(b1.name, started_broadcasts)
		self.assertIn(b2.name, started_broadcasts)
		self.assertNotIn(b3.name, started_broadcasts)
		self.assertNotIn(b4.name, started_broadcasts)

	def test_idempotent(self):
		"""Running start_due_channels twice starts each row only once."""
		now = now_datetime()
		due_time = add_to_date(now, minutes=-5)
		b = self._make_broadcast(scheduled_time=due_time)

		with patch("msuite_campaigns.broadcast.hub.start_channel"):
			first_run = start_due_channels()
			second_run = start_due_channels()

		self.assertEqual(first_run, 1)
		self.assertEqual(second_run, 0)

	def test_one_failure_isolated(self):
		"""Failure in one channel does not stop subsequent channels from starting."""
		now = now_datetime()
		due_time = add_to_date(now, minutes=-5)
		b1 = self._make_broadcast(scheduled_time=due_time)
		b2 = self._make_broadcast(scheduled_time=due_time)

		def fake_start(broadcast, channel):
			if broadcast == b1.name:
				raise RuntimeError("Simulated launch failure")

		with patch("msuite_campaigns.broadcast.hub.start_channel", side_effect=fake_start):
			with patch("frappe.log_error") as mock_log:
				started_count = start_due_channels()

				# b2 should have successfully started
				self.assertEqual(started_count, 1)
				mock_log.assert_called()

	def test_scheduler_channel_start_failure_leaves_row_failed_and_sets_launch_error(self):
		"""Channel dispatch failure during scheduler run marks row Failed with launch_error set."""
		now = now_datetime()
		due_time = add_to_date(now, minutes=-5)
		b1 = self._make_broadcast(scheduled_time=due_time)
		b2 = self._make_broadcast(scheduled_time=due_time)

		# Make b1 use AWS and have start_channel fail inside engine dispatch
		with patch(
			"msuite_campaigns.broadcast.enqueue.enqueue_broadcast_channel",
			side_effect=RuntimeError("Engine dispatch fatal failure"),
		):
			with patch("frappe.log_error") as mock_log:
				# start_due_channels runs both b1 and b2
				# b1 will raise inside start_channel, get marked Failed with launch_error,
				# scheduler catches exception, logs, and proceeds to b2 (which will also raise if enqueue fails,
				# so let's make enqueue only fail for b1)
				pass

		def fake_enqueue(broadcast, channel):
			if broadcast == b1.name:
				raise RuntimeError("b1 engine failure")
			return 1

		with patch("msuite_campaigns.broadcast.enqueue.enqueue_broadcast_channel", side_effect=fake_enqueue):
			with patch("frappe.log_error") as mock_log:
				started = start_due_channels()
				self.assertEqual(started, 1)  # b2 succeeded
				mock_log.assert_called()

		b1_st = frappe.db.get_value("Broadcast Channel", {"parent": b1.name, "channel": "WhatsApp"}, "status")
		b1_err = frappe.db.get_value("Broadcast Channel", {"parent": b1.name, "channel": "WhatsApp"}, "launch_error")
		self.assertEqual(b1_st, "Failed")
		self.assertIn("b1 engine failure", b1_err)

		b2_st = frappe.db.get_value("Broadcast Channel", {"parent": b2.name, "channel": "WhatsApp"}, "status")
		self.assertEqual(b2_st, "In Progress")


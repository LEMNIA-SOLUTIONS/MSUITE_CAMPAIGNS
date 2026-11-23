# Copyright (c) 2026, MSuite and contributors
# For license information, please see license.txt

"""Tests for Broadcast launch and scheduler effective time handling (Task 3).

Validates:
- Launch in Per channel mode moves future rows to Scheduled, past/None rows to Queued.
- Launch in Same mode evaluates schedule from parent scheduled_time.
- Scheduler starts only due channel rows in Per channel mode.
- Scheduler uses parent scheduled_time in Same mode.
- Scheduler double-run starts each channel row only once (idempotent).
- Rollup status calculation with mixed Scheduled and Queued rows.
- Scheduler racing manual launch is a silent no-op with no duplicate dispatch.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_to_date, now_datetime

from msuite_campaigns.broadcast.constants import (
	CHANNEL_EMAIL,
	CHANNEL_SMS,
	CHANNEL_WHATSAPP,
	SCHEDULE_PER_CHANNEL,
	SCHEDULE_SAME,
	STATUS_COMPLETED,
	STATUS_DRAFT,
	STATUS_IN_PROGRESS,
	STATUS_QUEUED,
	STATUS_SCHEDULED,
)
from msuite_campaigns.broadcast.lifecycle import launch, rollup_status
from msuite_campaigns.broadcast.scheduler import start_due_channels

TEST_PREFIX = "ZZTestLS "


class TestLaunchSchedule(IntegrationTestCase):
	"""Test suite for launch and scheduler effective time behavior."""

	def setUp(self) -> None:
		"""Initialize test environment and document cleanup tracker.

		Params:
			None.

		Returns:
			None.

		Raises:
			None.

		Side effects:
			Sets current user to Administrator and initializes cleanup tracking.
		"""
		super().setUp()
		frappe.set_user("Administrator")
		self.cleanup_broadcasts: list[str] = []

	def tearDown(self) -> None:
		"""Rollback transaction and purge test documents.

		Params:
			None.

		Returns:
			None.

		Raises:
			None.

		Side effects:
			Deletes created Broadcast documents and rolls back database state.
		"""
		frappe.db.rollback()
		for name in self.cleanup_broadcasts:
			if frappe.db.exists("Broadcast", name):
				frappe.delete_doc("Broadcast", name, force=True, ignore_permissions=True)
		super().tearDown()

	def _make_broadcast(self, **kwargs) -> "frappe.model.document.Document":
		"""Helper to construct and insert a Broadcast document with channel rows.

		Params:
			**kwargs: Overrides for Broadcast document fields.

		Returns:
			frappe.model.document.Document: Inserted Broadcast document.

		Raises:
			frappe.ValidationError: If document invariants are violated.

		Side effects:
			Inserts rows into tabBroadcast and tabBroadcast Channel.
		"""
		channels = kwargs.pop(
			"channels",
			[
				{
					"channel": CHANNEL_WHATSAPP,
					"whatsapp_account": "WA-ACC-001",
					"whatsapp_template": "tpl_wa_001",
					"status": STATUS_DRAFT,
				}
			],
		)
		payload = {
			"doctype": "Broadcast",
			"title": f"{TEST_PREFIX}{frappe.generate_hash(length=6)}",
			"recipient_source": "Individual",
			"status": STATUS_DRAFT,
			"channels": channels,
		}
		payload.update(kwargs)
		for ch in payload.get("channels") or []:
			if ch.get("channel") != CHANNEL_WHATSAPP:
				ch.setdefault("variable_type", "")
		doc = frappe.get_doc(payload)
		doc.flags.ignore_links = True
		doc.insert(ignore_permissions=True)
		self.cleanup_broadcasts.append(doc.name)
		return doc

	def test_launch_per_row_future_goes_scheduled_past_or_none_goes_queued(self) -> None:
		"""Test launch in Per channel mode sets future rows to Scheduled and past/None to Queued.

		Params:
			None.

		Returns:
			None.

		Raises:
			AssertionError: If channel statuses or overall status do not match expectation.

		Side effects:
			Transitions channel rows in database.
		"""
		now = now_datetime()
		future_time = add_to_date(now, hours=2)
		past_time = add_to_date(now, minutes=-10)

		doc = self._make_broadcast(
			schedule_mode=SCHEDULE_PER_CHANNEL,
			channels=[
				{
					"channel": CHANNEL_WHATSAPP,
					"whatsapp_account": "WA-ACC-001",
					"whatsapp_template": "tpl_wa_001",
					"scheduled_time": future_time,
					"status": STATUS_DRAFT,
				},
				{
					"channel": CHANNEL_EMAIL,
					"email_account": "mail@test.com",
					"email_template": "tpl_email_001",
					"scheduled_time": future_time,
					"status": STATUS_DRAFT,
				},
				{
					"channel": CHANNEL_SMS,
					"sms_account": "SMS-ACC-001",
					"sms_template": "tpl_sms_001",
					"scheduled_time": None,
					"status": STATUS_DRAFT,
				},
			],
		)
		# Update Email to past time via set_value (bypassing future validation at insert)
		email_row = doc.channel_row(CHANNEL_EMAIL)
		frappe.db.set_value("Broadcast Channel", email_row.name, "scheduled_time", past_time)

		# Mock check_broadcast_launch to prevent plan enforcer network calls
		with patch("msuite_campaigns.broadcast.lifecycle.check_broadcast_launch"):
			res = launch(doc.name)

		self.assertEqual(res.get(CHANNEL_WHATSAPP), STATUS_SCHEDULED)
		self.assertEqual(res.get(CHANNEL_EMAIL), STATUS_QUEUED)
		self.assertEqual(res.get(CHANNEL_SMS), STATUS_QUEUED)

		doc.reload()
		self.assertEqual(doc.channel_row(CHANNEL_WHATSAPP).status, STATUS_SCHEDULED)
		self.assertEqual(doc.channel_row(CHANNEL_EMAIL).status, STATUS_QUEUED)
		self.assertEqual(doc.channel_row(CHANNEL_SMS).status, STATUS_QUEUED)
		self.assertEqual(doc.status, STATUS_QUEUED)
		self.assertIsNotNone(doc.launched_at)

	def test_launch_same_mode_uses_parent_time(self) -> None:
		"""Test launch in Same mode uses parent scheduled_time for all channel rows.

		Params:
			None.

		Returns:
			None.

		Raises:
			AssertionError: If row statuses do not match parent scheduled_time calculation.

		Side effects:
			Transitions channel rows in database.
		"""
		now = now_datetime()
		future_time = add_to_date(now, hours=2)

		# Case 1: Parent scheduled_time is future -> all rows go to Scheduled
		doc_future = self._make_broadcast(
			schedule_mode=SCHEDULE_SAME,
			scheduled_time=future_time,
			channels=[
				{
					"channel": CHANNEL_WHATSAPP,
					"whatsapp_account": "WA-ACC-001",
					"whatsapp_template": "tpl_wa_001",
					"status": STATUS_DRAFT,
				},
				{
					"channel": CHANNEL_EMAIL,
					"email_account": "mail@test.com",
					"email_template": "tpl_email_001",
					"status": STATUS_DRAFT,
				},
			],
		)

		with patch("msuite_campaigns.broadcast.lifecycle.check_broadcast_launch"):
			res_future = launch(doc_future.name)

		self.assertEqual(res_future.get(CHANNEL_WHATSAPP), STATUS_SCHEDULED)
		self.assertEqual(res_future.get(CHANNEL_EMAIL), STATUS_SCHEDULED)
		doc_future.reload()
		self.assertEqual(doc_future.status, STATUS_SCHEDULED)
		self.assertIsNone(doc_future.launched_at)

		# Case 2: Parent scheduled_time is None -> all rows go to Queued
		doc_none = self._make_broadcast(
			schedule_mode=SCHEDULE_SAME,
			scheduled_time=None,
			channels=[
				{
					"channel": CHANNEL_WHATSAPP,
					"whatsapp_account": "WA-ACC-001",
					"whatsapp_template": "tpl_wa_001",
					"status": STATUS_DRAFT,
				},
				{
					"channel": CHANNEL_EMAIL,
					"email_account": "mail@test.com",
					"email_template": "tpl_email_001",
					"status": STATUS_DRAFT,
				},
			],
		)

		with patch("msuite_campaigns.broadcast.lifecycle.check_broadcast_launch"):
			res_none = launch(doc_none.name)

		self.assertEqual(res_none.get(CHANNEL_WHATSAPP), STATUS_QUEUED)
		self.assertEqual(res_none.get(CHANNEL_EMAIL), STATUS_QUEUED)
		doc_none.reload()
		self.assertEqual(doc_none.status, STATUS_QUEUED)
		self.assertIsNotNone(doc_none.launched_at)

	def test_scheduler_starts_only_due_rows_per_channel(self) -> None:
		"""Test scheduler starts only channel rows whose scheduled_time is due in Per channel mode.

		Params:
			None.

		Returns:
			None.

		Raises:
			AssertionError: If future row is started or due row is not started.

		Side effects:
			Transitions due channel rows to Queued.
		"""
		now = now_datetime()
		past_time = add_to_date(now, minutes=-15)
		future_time = add_to_date(now, hours=2)

		doc = self._make_broadcast(
			schedule_mode=SCHEDULE_PER_CHANNEL,
			status=STATUS_SCHEDULED,
			channels=[
				{
					"channel": CHANNEL_WHATSAPP,
					"whatsapp_account": "WA-ACC-001",
					"whatsapp_template": "tpl_wa_001",
					"scheduled_time": future_time,
					"status": STATUS_SCHEDULED,
				},
				{
					"channel": CHANNEL_EMAIL,
					"email_account": "mail@test.com",
					"email_template": "tpl_email_001",
					"scheduled_time": future_time,
					"status": STATUS_SCHEDULED,
				},
			],
		)
		# Set WhatsApp scheduled_time to past and parent scheduled_time to past
		wa_row = doc.channel_row(CHANNEL_WHATSAPP)
		frappe.db.set_value("Broadcast Channel", wa_row.name, "scheduled_time", past_time)
		frappe.db.set_value("Broadcast", doc.name, "scheduled_time", past_time)

		started_calls: list[tuple[str, str]] = []

		def fake_start_channel(broadcast_name: str, channel_name: str) -> None:
			started_calls.append((broadcast_name, channel_name))

		with patch("msuite_campaigns.broadcast.hub.start_channel", side_effect=fake_start_channel):
			count = start_due_channels()

		self.assertGreaterEqual(count, 1)
		started_this_broadcast = [call for call in started_calls if call[0] == doc.name]
		self.assertEqual(len(started_this_broadcast), 1)
		self.assertEqual(started_this_broadcast[0][1], CHANNEL_WHATSAPP)

		doc.reload()
		self.assertEqual(doc.channel_row(CHANNEL_WHATSAPP).status, STATUS_QUEUED)
		self.assertEqual(doc.channel_row(CHANNEL_EMAIL).status, STATUS_SCHEDULED)

	def test_scheduler_uses_parent_time_in_same_mode(self) -> None:
		"""Test scheduler starts all scheduled channel rows when parent time is due in Same mode.

		Params:
			None.

		Returns:
			None.

		Raises:
			AssertionError: If channel rows are not started despite parent scheduled_time being due.

		Side effects:
			Transitions channel rows to Queued.
		"""
		now = now_datetime()
		past_time = add_to_date(now, minutes=-15)
		future_time = add_to_date(now, hours=2)

		doc = self._make_broadcast(
			schedule_mode=SCHEDULE_SAME,
			scheduled_time=future_time,
			status=STATUS_SCHEDULED,
			channels=[
				{
					"channel": CHANNEL_WHATSAPP,
					"whatsapp_account": "WA-ACC-001",
					"whatsapp_template": "tpl_wa_001",
					"status": STATUS_SCHEDULED,
				},
				{
					"channel": CHANNEL_EMAIL,
					"email_account": "mail@test.com",
					"email_template": "tpl_email_001",
					"status": STATUS_SCHEDULED,
				},
			],
		)
		# Update parent scheduled_time to past
		frappe.db.set_value("Broadcast", doc.name, "scheduled_time", past_time)

		started_calls: list[tuple[str, str]] = []

		def fake_start_channel(broadcast_name: str, channel_name: str) -> None:
			started_calls.append((broadcast_name, channel_name))

		with patch("msuite_campaigns.broadcast.hub.start_channel", side_effect=fake_start_channel):
			count = start_due_channels()

		self.assertGreaterEqual(count, 2)
		started_this_broadcast = [call for call in started_calls if call[0] == doc.name]
		self.assertEqual(len(started_this_broadcast), 2)
		started_channels = {call[1] for call in started_this_broadcast}
		self.assertIn(CHANNEL_WHATSAPP, started_channels)
		self.assertIn(CHANNEL_EMAIL, started_channels)

		doc.reload()
		self.assertEqual(doc.channel_row(CHANNEL_WHATSAPP).status, STATUS_QUEUED)
		self.assertEqual(doc.channel_row(CHANNEL_EMAIL).status, STATUS_QUEUED)

	def test_scheduler_double_run_starts_each_row_once(self) -> None:
		"""Test running scheduler twice starts each due channel row only once.

		Params:
			None.

		Returns:
			None.

		Raises:
			AssertionError: If second run starts already transitioned rows.

		Side effects:
			Transitions channel row on first run.
		"""
		now = now_datetime()
		past_time = add_to_date(now, minutes=-10)
		future_time = add_to_date(now, hours=2)

		doc = self._make_broadcast(
			schedule_mode=SCHEDULE_PER_CHANNEL,
			status=STATUS_SCHEDULED,
			channels=[
				{
					"channel": CHANNEL_WHATSAPP,
					"whatsapp_account": "WA-ACC-001",
					"whatsapp_template": "tpl_wa_001",
					"scheduled_time": future_time,
					"status": STATUS_SCHEDULED,
				}
			],
		)
		wa_row = doc.channel_row(CHANNEL_WHATSAPP)
		frappe.db.set_value("Broadcast Channel", wa_row.name, "scheduled_time", past_time)
		frappe.db.set_value("Broadcast", doc.name, "scheduled_time", past_time)

		started_calls: list[tuple[str, str]] = []

		def fake_start_channel(broadcast_name: str, channel_name: str) -> None:
			started_calls.append((broadcast_name, channel_name))

		with patch("msuite_campaigns.broadcast.hub.start_channel", side_effect=fake_start_channel):
			first_run = start_due_channels()
			second_run = start_due_channels()

		self.assertGreaterEqual(first_run, 1)
		started_this_broadcast = [call for call in started_calls if call[0] == doc.name]
		self.assertEqual(len(started_this_broadcast), 1)

	def test_rollup_with_mixed_scheduled_and_queued_rows(self) -> None:
		"""Test pure rollup_status calculation with mixed Scheduled, Queued, and other statuses.

		Params:
			None.

		Returns:
			None.

		Raises:
			AssertionError: If rollup status precedence is violated.

		Side effects:
			None. Pure function test.
		"""
		# Rule 4 (Queued) takes precedence over Rule 6 (Scheduled)
		self.assertEqual(
			rollup_status({"channels": [{"status": STATUS_SCHEDULED}, {"status": STATUS_QUEUED}]}),
			STATUS_QUEUED,
		)
		# Rule 3 (In Progress) takes precedence over Queued and Scheduled
		self.assertEqual(
			rollup_status(
				{
					"channels": [
						{"status": STATUS_SCHEDULED},
						{"status": STATUS_QUEUED},
						{"status": STATUS_IN_PROGRESS},
					]
				}
			),
			STATUS_IN_PROGRESS,
		)
		# Only Scheduled rows -> Scheduled
		self.assertEqual(
			rollup_status({"channels": [{"status": STATUS_SCHEDULED}, {"status": STATUS_SCHEDULED}]}),
			STATUS_SCHEDULED,
		)
		# Completed and Scheduled -> Scheduled
		self.assertEqual(
			rollup_status({"channels": [{"status": STATUS_COMPLETED}, {"status": STATUS_SCHEDULED}]}),
			STATUS_SCHEDULED,
		)
		# Completed, Scheduled, and Queued -> Queued
		self.assertEqual(
			rollup_status(
				{
					"channels": [
						{"status": STATUS_COMPLETED},
						{"status": STATUS_SCHEDULED},
						{"status": STATUS_QUEUED},
					]
				}
			),
			STATUS_QUEUED,
		)

	def test_scheduler_racing_manual_launch_is_silent_noop(self) -> None:
		"""Test scheduler racing manual launch is a silent no-op without duplicate dispatch.

		Params:
			None.

		Returns:
			None.

		Raises:
			AssertionError: If scheduler logs error or duplicates dispatch when row was launched.

		Side effects:
			None.
		"""
		now = now_datetime()
		past_time = add_to_date(now, minutes=-10)
		future_time = add_to_date(now, hours=2)

		doc = self._make_broadcast(
			schedule_mode=SCHEDULE_PER_CHANNEL,
			status=STATUS_SCHEDULED,
			channels=[
				{
					"channel": CHANNEL_WHATSAPP,
					"whatsapp_account": "WA-ACC-001",
					"whatsapp_template": "tpl_wa_001",
					"scheduled_time": future_time,
					"status": STATUS_SCHEDULED,
				}
			],
		)
		wa_row = doc.channel_row(CHANNEL_WHATSAPP)
		frappe.db.set_value("Broadcast Channel", wa_row.name, "scheduled_time", past_time)
		frappe.db.set_value("Broadcast", doc.name, "scheduled_time", past_time)

		# Simulate manual launch racing: manual launch already moved row to Queued before scheduler processes it
		frappe.db.set_value("Broadcast Channel", wa_row.name, "status", STATUS_QUEUED)
		frappe.db.set_value("Broadcast", doc.name, "status", STATUS_QUEUED)

		started_calls: list[tuple[str, str]] = []

		def fake_start_channel(broadcast_name: str, channel_name: str) -> None:
			started_calls.append((broadcast_name, channel_name))

		with patch("msuite_campaigns.broadcast.hub.start_channel", side_effect=fake_start_channel):
			with patch("frappe.log_error") as mock_log:
				count = start_due_channels()
				# Must NOT have logged an error for this broadcast
				error_calls = [c for c in mock_log.call_args_list if doc.name in str(c)]
				self.assertEqual(len(error_calls), 0)

		# Must NOT have started this broadcast row again
		started_this_broadcast = [call for call in started_calls if call[0] == doc.name]
		self.assertEqual(len(started_this_broadcast), 0)

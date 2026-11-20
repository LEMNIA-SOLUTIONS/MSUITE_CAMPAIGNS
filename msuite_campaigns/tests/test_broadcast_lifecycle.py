# Copyright (c) 2026, MSuite and contributors
# For license information, please see license.txt

"""Tests for Broadcast lifecycle state machine, status rollup, and actions."""

from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, now_datetime

from msuite_campaigns.broadcast.constants import (
	ALLOWED_TRANSITIONS,
	CHANNEL_STATUSES,
)
from msuite_campaigns.broadcast.lifecycle import (
	cancel,
	launch,
	pause,
	resume,
	retry_failed,
	rollup_status,
	transition_channel,
)
from msuite_campaigns.broadcast.recipients import requeue_failed_recipients


class TestBroadcastLifecycle(IntegrationTestCase):
	"""Test suite for Broadcast lifecycle state machine and rollup."""

	def setUp(self):
		super().setUp()
		self.cleanup_broadcasts: list[str] = []

	def tearDown(self):
		frappe.db.rollback()
		for name in self.cleanup_broadcasts:
			if frappe.db.exists("Broadcast", name):
				frappe.delete_doc("Broadcast", name, force=True, ignore_permissions=True)

	def _make_broadcast(self, **kwargs) -> "frappe.model.document.Document":
		"""Helper to construct and insert a Broadcast document with channel rows."""
		channels = kwargs.pop(
			"channels",
			[
				{
					"channel": "WhatsApp",
					"whatsapp_account": "WA-ACC-001",
					"whatsapp_template": "tpl_wa_001",
					"status": "Draft",
				}
			],
		)
		for ch in channels:
			if ch.get("channel") != "WhatsApp":
				ch.setdefault("variable_type", "")
		payload = {
			"doctype": "Broadcast",
			"title": "Lifecycle Test Broadcast",
			"recipient_source": "Individual",
			"status": "Draft",
			"channels": channels,
		}
		payload.update(kwargs)
		doc = frappe.get_doc(payload)
		doc.insert(ignore_links=True)
		self.cleanup_broadcasts.append(doc.name)
		return doc

	def _make_recipient(
		self,
		broadcast: str,
		channel: str,
		address: str,
		status: str = "Failed",
		is_retryable: int = 1,
		retry_count: int = 0,
	) -> "frappe.model.document.Document":
		"""Helper to insert a Broadcast Recipient row."""
		rec = frappe.get_doc(
			{
				"doctype": "Broadcast Recipient",
				"broadcast": broadcast,
				"channel": channel,
				"address": address,
				"status": status,
				"is_retryable": is_retryable,
				"retry_count": retry_count,
			}
		)
		rec.insert(ignore_links=True)
		return rec

	def test_rollup_status_table(self):
		"""Validate pure rollup_status calculation across all ordered decision rules."""
		test_cases = [
			# (channel_statuses, expected_status, description)
			([], "Draft", "no rows -> Draft"),
			([{"status": "Draft"}, {"status": "Draft"}], "Draft", "all Draft -> Draft"),
			([{"status": "Cancelled"}, {"status": "Cancelled"}], "Cancelled", "both Cancelled -> Cancelled"),
			([{"status": "In Progress"}, {"status": "Paused"}], "In Progress", "WA In Progress + Email Paused -> In Progress"),
			([{"status": "In Progress"}, {"status": "Queued"}], "In Progress", "In Progress + Queued -> In Progress"),
			([{"status": "Queued"}, {"status": "Paused"}], "Queued", "any Queued -> Queued"),
			([{"status": "Queued"}, {"status": "Completed"}], "Queued", "Queued + Completed -> Queued"),
			([{"status": "Paused"}, {"status": "Completed"}], "Paused", "one Paused + other Completed -> Paused"),
			([{"status": "Paused"}, {"status": "Scheduled"}], "Paused", "any Paused -> Paused"),
			([{"status": "Scheduled"}, {"status": "Completed"}], "Scheduled", "any Scheduled -> Scheduled"),
			([{"status": "Draft"}, {"status": "Completed"}], "Draft", "Draft + Completed -> Draft"),
			([{"status": "Completed"}, {"status": "Failed"}], "Partially Failed", "WA Completed + Email Failed -> Partially Failed"),
			([{"status": "Completed"}, {"status": "Partially Failed"}], "Partially Failed", "Completed + Partially Failed -> Partially Failed"),
			([{"status": "Failed"}, {"status": "Cancelled"}], "Partially Failed", "Failed + Cancelled -> Partially Failed"),
			([{"status": "Failed"}, {"status": "Failed"}], "Failed", "all Failed -> Failed"),
			([{"status": "Completed"}, {"status": "Completed"}], "Completed", "all Completed -> Completed"),
			([{"status": "Completed"}, {"status": "Cancelled"}], "Completed", "Completed + Cancelled -> Completed"),
		]

		for channels, expected, desc in test_cases:
			with self.subTest(desc=desc):
				doc_mock = {"channels": channels}
				self.assertEqual(rollup_status(doc_mock), expected, f"Failed for case: {desc}")

	def test_transitions_legal_and_illegal(self):
		"""Validate every legal and illegal transition parametrized from ALLOWED_TRANSITIONS."""
		for from_status in CHANNEL_STATUSES:
			allowed_targets = ALLOWED_TRANSITIONS.get(from_status, set())
			for to_status in CHANNEL_STATUSES:
				with self.subTest(from_status=from_status, to_status=to_status):
					doc = self._make_broadcast(
						channels=[
							{
								"channel": "WhatsApp",
								"whatsapp_account": "WA-ACC-001",
								"whatsapp_template": "tpl_wa_001",
								"status": from_status,
							}
						]
					)
					# Ensure child row status in DB matches from_status
					channel_name = doc.channels[0].name
					frappe.db.set_value("Broadcast Channel", channel_name, "status", from_status)

					if to_status in allowed_targets:
						transition_channel(doc.name, "WhatsApp", to_status)
						updated_channel_status = frappe.db.get_value("Broadcast Channel", channel_name, "status")
						self.assertEqual(updated_channel_status, to_status)
					else:
						with self.assertRaises(frappe.ValidationError) as ctx:
							transition_channel(doc.name, "WhatsApp", to_status)
						self.assertIn("Cannot move WhatsApp", str(ctx.exception))

	def test_transition_channel_absent_channel_raises(self):
		"""transition_channel raises ValidationError when channel row does not exist."""
		doc = self._make_broadcast(
			channels=[
				{
					"channel": "WhatsApp",
					"whatsapp_account": "WA-ACC-001",
					"whatsapp_template": "tpl_wa_001",
					"status": "Draft",
				}
			]
		)
		with self.assertRaises(frappe.ValidationError):
			transition_channel(doc.name, "Email", "Queued")

	def test_double_launch_is_idempotent(self):
		"""Double launch must be idempotent and a silent no-op on the second call."""
		doc = self._make_broadcast(
			channels=[
				{
					"channel": "WhatsApp",
					"whatsapp_account": "WA-ACC-001",
					"whatsapp_template": "tpl_wa_001",
					"status": "Draft",
				},
				{
					"channel": "Email",
					"email_account": "acc@test.com",
					"email_template": "tpl_email",
					"status": "Draft",
				},
			]
		)

		first_res = launch(doc.name)
		self.assertEqual(first_res, {"WhatsApp": "Queued", "Email": "Queued"})
		doc.reload()
		self.assertEqual(doc.status, "Queued")
		self.assertEqual(doc.channels[0].status, "Queued")
		self.assertEqual(doc.channels[1].status, "Queued")
		self.assertIsNotNone(doc.launched_at)
		first_launched_at = doc.launched_at

		# Second call is idempotent no-op
		second_res = launch(doc.name)
		self.assertEqual(second_res, {})
		doc.reload()
		self.assertEqual(doc.status, "Queued")
		self.assertEqual(doc.channels[0].status, "Queued")
		self.assertEqual(doc.channels[1].status, "Queued")
		self.assertEqual(doc.launched_at, first_launched_at)

	def test_launch_calls_check_broadcast_launch_and_handles_error(self):
		"""launch must call check_broadcast_launch; if it raises, all rows remain unchanged."""
		doc = self._make_broadcast(
			channels=[
				{
					"channel": "WhatsApp",
					"whatsapp_account": "WA-ACC-001",
					"whatsapp_template": "tpl_wa_001",
					"status": "Draft",
				}
			]
		)

		with patch(
			"msuite_campaigns.broadcast.lifecycle.check_broadcast_launch",
			side_effect=frappe.ValidationError("Plan quota exceeded"),
		) as mock_gate:
			with self.assertRaises(frappe.ValidationError) as ctx:
				launch(doc.name)
			mock_gate.assert_called_once()
			self.assertIn("Plan quota exceeded", str(ctx.exception))

		# Document and channel status must remain unchanged
		doc.reload()
		self.assertEqual(doc.status, "Draft")
		self.assertEqual(doc.channels[0].status, "Draft")
		self.assertIsNone(doc.launched_at)

	def test_launch_future_scheduled_time_vs_past_or_none(self):
		"""Future scheduled_time moves to Scheduled; past or None moves to Queued."""
		# Case 1: Future scheduled_time -> Scheduled
		future_time = add_days(now_datetime(), 2)
		doc_future = self._make_broadcast(
			status="Scheduled",
			scheduled_time=future_time,
			channels=[
				{
					"channel": "WhatsApp",
					"whatsapp_account": "WA-ACC-001",
					"whatsapp_template": "tpl_wa_001",
					"status": "Draft",
				}
			],
		)
		res_future = launch(doc_future.name)
		self.assertEqual(res_future, {"WhatsApp": "Scheduled"})
		doc_future.reload()
		self.assertEqual(doc_future.status, "Scheduled")
		self.assertEqual(doc_future.channels[0].status, "Scheduled")
		self.assertIsNone(doc_future.launched_at)

		# Case 2: Past scheduled_time -> Queued
		doc_past = self._make_broadcast(
			channels=[
				{
					"channel": "WhatsApp",
					"whatsapp_account": "WA-ACC-001",
					"whatsapp_template": "tpl_wa_001",
					"status": "Draft",
				}
			]
		)
		past_time = add_days(now_datetime(), -1)
		frappe.db.set_value("Broadcast", doc_past.name, "scheduled_time", past_time)
		res_past = launch(doc_past.name)
		self.assertEqual(res_past, {"WhatsApp": "Queued"})
		doc_past.reload()
		self.assertEqual(doc_past.status, "Queued")
		self.assertEqual(doc_past.channels[0].status, "Queued")
		self.assertIsNotNone(doc_past.launched_at)

		# Case 3: None scheduled_time -> Queued
		doc_none = self._make_broadcast(
			channels=[
				{
					"channel": "WhatsApp",
					"whatsapp_account": "WA-ACC-001",
					"whatsapp_template": "tpl_wa_001",
					"status": "Draft",
				}
			]
		)
		res_none = launch(doc_none.name)
		self.assertEqual(res_none, {"WhatsApp": "Queued"})
		doc_none.reload()
		self.assertEqual(doc_none.status, "Queued")
		self.assertEqual(doc_none.channels[0].status, "Queued")
		self.assertIsNotNone(doc_none.launched_at)

	def test_pause_queues_and_in_progress_only(self):
		"""pause moves only Queued and In Progress rows to Paused; others skipped."""
		doc = self._make_broadcast(
			channels=[
				{
					"channel": "WhatsApp",
					"whatsapp_account": "WA-ACC-001",
					"whatsapp_template": "tpl_wa_001",
					"status": "In Progress",
				},
				{
					"channel": "Email",
					"email_account": "acc@test.com",
					"email_template": "tpl_email",
					"status": "Completed",
				},
			]
		)
		frappe.db.set_value("Broadcast Channel", doc.channels[0].name, "status", "In Progress")
		frappe.db.set_value("Broadcast Channel", doc.channels[1].name, "status", "Completed")

		res = pause(doc.name)
		self.assertEqual(res, {"WhatsApp": "Paused"})
		doc.reload()
		self.assertEqual(doc.channels[0].status, "Paused")
		self.assertEqual(doc.channels[1].status, "Completed")
		self.assertEqual(doc.status, "Paused")  # Paused + Completed -> Paused

	def test_resume_sent_count_zero_vs_greater(self):
		"""resume returns to Queued if sent_count is 0, else In Progress."""
		# sent_count == 0 -> Queued
		doc_zero = self._make_broadcast(
			channels=[
				{
					"channel": "WhatsApp",
					"whatsapp_account": "WA-ACC-001",
					"whatsapp_template": "tpl_wa_001",
					"status": "Paused",
					"sent_count": 0,
				}
			]
		)
		frappe.db.set_value("Broadcast Channel", doc_zero.channels[0].name, "status", "Paused")
		frappe.db.set_value("Broadcast Channel", doc_zero.channels[0].name, "sent_count", 0)

		res_zero = resume(doc_zero.name)
		self.assertEqual(res_zero, {"WhatsApp": "Queued"})
		doc_zero.reload()
		self.assertEqual(doc_zero.channels[0].status, "Queued")
		self.assertEqual(doc_zero.status, "Queued")

		# sent_count > 0 -> In Progress
		doc_sent = self._make_broadcast(
			channels=[
				{
					"channel": "WhatsApp",
					"whatsapp_account": "WA-ACC-001",
					"whatsapp_template": "tpl_wa_001",
					"status": "Paused",
					"sent_count": 10,
				}
			]
		)
		frappe.db.set_value("Broadcast Channel", doc_sent.channels[0].name, "status", "Paused")
		frappe.db.set_value("Broadcast Channel", doc_sent.channels[0].name, "sent_count", 10)

		res_sent = resume(doc_sent.name)
		self.assertEqual(res_sent, {"WhatsApp": "In Progress"})
		doc_sent.reload()
		self.assertEqual(doc_sent.channels[0].status, "In Progress")
		self.assertEqual(doc_sent.status, "In Progress")

	def test_cancel_skips_terminal_rows(self):
		"""cancel moves non-terminal rows to Cancelled and skips terminal rows."""
		doc = self._make_broadcast(
			channels=[
				{
					"channel": "WhatsApp",
					"whatsapp_account": "WA-ACC-001",
					"whatsapp_template": "tpl_wa_001",
					"status": "In Progress",
				},
				{
					"channel": "Email",
					"email_account": "acc@test.com",
					"email_template": "tpl_email",
					"status": "Completed",
				},
			]
		)
		frappe.db.set_value("Broadcast Channel", doc.channels[0].name, "status", "In Progress")
		frappe.db.set_value("Broadcast Channel", doc.channels[1].name, "status", "Completed")

		res = cancel(doc.name)
		self.assertEqual(res, {"WhatsApp": "Cancelled"})
		doc.reload()
		self.assertEqual(doc.channels[0].status, "Cancelled")
		self.assertEqual(doc.channels[1].status, "Completed")
		self.assertEqual(doc.status, "Completed")  # Completed + Cancelled -> Completed

		# If both are terminal Cancelled, second cancel skips both
		res_again = cancel(doc.name)
		self.assertEqual(res_again, {})

	def test_retry_failed_and_requeue_failed_recipients(self):
		"""retry_failed moves Failed/Partially Failed to Queued and requeues retryable recipients."""
		doc = self._make_broadcast(
			channels=[
				{
					"channel": "WhatsApp",
					"whatsapp_account": "WA-ACC-001",
					"whatsapp_template": "tpl_wa_001",
					"status": "Failed",
				},
				{
					"channel": "Email",
					"email_account": "acc@test.com",
					"email_template": "tpl_email",
					"status": "Completed",
				},
			]
		)
		frappe.db.set_value("Broadcast Channel", doc.channels[0].name, "status", "Failed")
		frappe.db.set_value("Broadcast Channel", doc.channels[1].name, "status", "Completed")

		# Create recipients: 1 retryable Failed, 1 non-retryable Failed, 1 Sent
		rec1 = self._make_recipient(doc.name, "WhatsApp", "+1234567890", status="Failed", is_retryable=1, retry_count=0)
		rec2 = self._make_recipient(doc.name, "WhatsApp", "+1234567891", status="Failed", is_retryable=0, retry_count=0)
		rec3 = self._make_recipient(doc.name, "WhatsApp", "+1234567892", status="Sent", is_retryable=0, retry_count=0)

		res = retry_failed(doc.name)
		self.assertEqual(res, {"WhatsApp": "Queued"})

		doc.reload()
		self.assertEqual(doc.channels[0].status, "Queued")
		self.assertEqual(doc.channels[1].status, "Completed")
		self.assertEqual(doc.status, "Queued")  # Queued + Completed -> Queued

		# Check recipients
		rec1.reload()
		self.assertEqual(rec1.status, "Retrying")
		self.assertEqual(rec1.retry_count, 1)

		rec2.reload()
		self.assertEqual(rec2.status, "Failed")
		self.assertEqual(rec2.retry_count, 0)

		rec3.reload()
		self.assertEqual(rec3.status, "Sent")

	def test_requeue_failed_recipients_bypasses_rank_guard(self):
		"""requeue_failed_recipients sets status=Retrying and increments retry_count directly."""
		doc = self._make_broadcast()
		rec1 = self._make_recipient(doc.name, "WhatsApp", "+1987654321", status="Failed", is_retryable=1, retry_count=2)
		rec2 = self._make_recipient(doc.name, "WhatsApp", "+1987654322", status="Failed", is_retryable=0, retry_count=1)

		affected = requeue_failed_recipients(doc.name, "WhatsApp")
		self.assertEqual(affected, 1)

		rec1.reload()
		self.assertEqual(rec1.status, "Retrying")
		self.assertEqual(rec1.retry_count, 3)

		rec2.reload()
		self.assertEqual(rec2.status, "Failed")
		self.assertEqual(rec2.retry_count, 1)

	def test_launched_at_set_once(self):
		"""launched_at is set on the first transition leaving Draft/Scheduled and never overwritten."""
		doc = self._make_broadcast(
			channels=[
				{
					"channel": "WhatsApp",
					"whatsapp_account": "WA-ACC-001",
					"whatsapp_template": "tpl_wa_001",
					"status": "Draft",
				},
				{
					"channel": "Email",
					"email_account": "acc@test.com",
					"email_template": "tpl_email",
					"status": "Draft",
				},
			]
		)
		self.assertIsNone(doc.launched_at)

		# Transition WhatsApp Draft -> Queued (leaves Draft/Scheduled)
		transition_channel(doc.name, "WhatsApp", "Queued")
		doc.reload()
		self.assertIsNotNone(doc.launched_at)
		initial_launched_at = doc.launched_at

		# Transition Email Draft -> Queued (doc.launched_at already set)
		transition_channel(doc.name, "Email", "Queued")
		doc.reload()
		self.assertEqual(doc.launched_at, initial_launched_at)

		# Transition WhatsApp Queued -> In Progress
		transition_channel(doc.name, "WhatsApp", "In Progress")
		doc.reload()
		self.assertEqual(doc.launched_at, initial_launched_at)

	def test_parent_status_always_equals_rollup_status_after_each_action(self):
		"""Parent Broadcast status must match rollup_status after each action and transition."""
		doc = self._make_broadcast(
			channels=[
				{
					"channel": "WhatsApp",
					"whatsapp_account": "WA-ACC-001",
					"whatsapp_template": "tpl_wa_001",
					"status": "Draft",
				},
				{
					"channel": "Email",
					"email_account": "acc@test.com",
					"email_template": "tpl_email",
					"status": "Draft",
				},
			]
		)
		self.assertEqual(doc.status, rollup_status(doc))

		# Launch WhatsApp only
		launch(doc.name, channel="WhatsApp")
		doc.reload()
		self.assertEqual(doc.channels[0].status, "Queued")
		self.assertEqual(doc.channels[1].status, "Draft")
		self.assertEqual(doc.status, rollup_status(doc))
		self.assertEqual(doc.status, "Queued")

		# Pause WhatsApp
		pause(doc.name, channel="WhatsApp")
		doc.reload()
		self.assertEqual(doc.channels[0].status, "Paused")
		self.assertEqual(doc.channels[1].status, "Draft")
		self.assertEqual(doc.status, rollup_status(doc))
		self.assertEqual(doc.status, "Paused")

		# Resume WhatsApp
		resume(doc.name, channel="WhatsApp")
		doc.reload()
		self.assertEqual(doc.channels[0].status, "Queued")
		self.assertEqual(doc.status, rollup_status(doc))

		# Cancel Email
		cancel(doc.name, channel="Email")
		doc.reload()
		self.assertEqual(doc.channels[1].status, "Cancelled")
		self.assertEqual(doc.status, rollup_status(doc))

	def test_channel_specific_action_does_not_change_other_channels(self):
		"""Pausing WhatsApp on a two-channel broadcast does not change the Email row."""
		doc = self._make_broadcast(
			channels=[
				{
					"channel": "WhatsApp",
					"whatsapp_account": "WA-ACC-001",
					"whatsapp_template": "tpl_wa_001",
					"status": "In Progress",
				},
				{
					"channel": "Email",
					"email_account": "acc@test.com",
					"email_template": "tpl_email",
					"status": "In Progress",
				},
			]
		)
		frappe.db.set_value("Broadcast Channel", doc.channels[0].name, "status", "In Progress")
		frappe.db.set_value("Broadcast Channel", doc.channels[1].name, "status", "In Progress")

		res = pause(doc.name, channel="WhatsApp")
		self.assertEqual(res, {"WhatsApp": "Paused"})

		doc.reload()
		self.assertEqual(doc.channels[0].status, "Paused")
		self.assertEqual(doc.channels[1].status, "In Progress")
		# Rollup: WA Paused + Email In Progress -> In Progress
		self.assertEqual(doc.status, "In Progress")

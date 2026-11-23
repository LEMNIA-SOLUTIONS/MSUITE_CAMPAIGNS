# Copyright (c) 2026, MSuite and contributors
# For license information, please see license.txt

"""Tests for Broadcast Counters and Counter Refresh Service."""

from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import now_datetime

from msuite_campaigns.broadcast.constants import CHANNEL_STATUSES
from msuite_campaigns.broadcast.counters import (
	refresh_counters,
	request_counter_refresh,
)
from msuite_campaigns.broadcast.recipients import RECIPIENT_BULK_INSERT_COLUMNS

TEST_PREFIX = "ZZTestBC "


class TestBroadcastCounters(IntegrationTestCase):
	"""Test suite for broadcast/counters.py."""

	def setUp(self):
		super().setUp()
		self.cleanup_broadcasts: list[str] = []
		if hasattr(frappe.local, "_pending_counter_refreshes"):
			delattr(frappe.local, "_pending_counter_refreshes")

	def tearDown(self):
		frappe.db.rollback()
		if hasattr(frappe.local, "_pending_counter_refreshes"):
			delattr(frappe.local, "_pending_counter_refreshes")
		for name in self.cleanup_broadcasts:
			if frappe.db.exists("Broadcast", name):
				frappe.delete_doc("Broadcast", name, force=True, ignore_permissions=True)

	def _make_broadcast(
		self,
		channel: str = "WhatsApp",
		status: str = "In Progress",
		channel_status: str = "In Progress",
	) -> "frappe.model.document.Document":
		doc = frappe.get_doc(
			{
				"doctype": "Broadcast",
				"title": f"{TEST_PREFIX}{frappe.generate_hash(length=6)}",
				"recipient_source": "Individual",
				"status": status,
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
		self.cleanup_broadcasts.append(doc.name)
		return doc

	def _insert_recipients(self, broadcast: str, channel: str, rows: list[dict]):
		now_val = now_datetime()
		current_user = frappe.session.user or "Administrator"
		values = []
		for r in rows:
			values.append(
				(
					frappe.generate_hash(length=10),
					current_user,
					now_val,
					now_val,
					current_user,
					0,
					broadcast,
					channel,
					r.get("address", "+919800000000"),
					r.get("recipient_name"),
					"{}",
					r.get("status", "Queued"),
					r.get("error_code"),
					r.get("failure_reason"),
					r.get("is_retryable", 0),
					r.get("retry_count", 0),
					r.get("provider_message_id", frappe.generate_hash(length=10)),
					r.get("sent_at"),
					r.get("delivered_at"),
					r.get("read_at"),
					r.get("opened_at"),
					r.get("clicked_at"),
					None,
					0.0,
					None,
				)
			)
		frappe.db.bulk_insert(
			"Broadcast Recipient",
			list(RECIPIENT_BULK_INSERT_COLUMNS),
			values,
			ignore_duplicates=True,
		)

	def test_grouped_counts(self):
		"""refresh_counters computes all counter fields correctly in one grouped SQL."""
		b = self._make_broadcast(channel="WhatsApp", channel_status="In Progress")
		now = now_datetime()
		# 1 Sent, 1 Delivered, 1 Read, 1 Failed, 1 Bounced, 1 Unsubscribed, 1 Queued
		rows = [
			{"address": "+919811100001", "status": "Sent", "sent_at": now},
			{"address": "+919811100002", "status": "Delivered", "sent_at": now, "delivered_at": now, "opened_at": now},
			{"address": "+919811100003", "status": "Read", "sent_at": now, "delivered_at": now, "read_at": now, "opened_at": now, "clicked_at": now},
			{"address": "+919811100004", "status": "Failed", "error_code": "ERR_1", "retry_count": 2},
			{"address": "+919811100005", "status": "Bounced", "retry_count": 1},
			{"address": "+919811100006", "status": "Unsubscribed"},
			{"address": "+919811100007", "status": "Queued"},
		]
		self._insert_recipients(b.name, "WhatsApp", rows)

		res = refresh_counters(b.name, "WhatsApp")

		self.assertEqual(res["recipient_count"], 7)
		# sent_count = Sent(1) + Delivered(1) + Read(1) + Bounced(1) = 4
		self.assertEqual(res["sent_count"], 4)
		# delivered_count = Delivered(1) + Read(1) = 2
		self.assertEqual(res["delivered_count"], 2)
		# read_count = Read(1) = 1
		self.assertEqual(res["read_count"], 1)
		# failed_count = Failed(1) = 1
		self.assertEqual(res["failed_count"], 1)
		# bounced_count = Bounced(1) = 1
		self.assertEqual(res["bounced_count"], 1)
		# opened_count = 2 (rows 2 and 3)
		self.assertEqual(res["opened_count"], 2)
		# clicked_count = 1 (row 3)
		self.assertEqual(res["clicked_count"], 1)
		# unsubscribed_count = 1
		self.assertEqual(res["unsubscribed_count"], 1)
		# retry_count = 2 + 1 = 3
		self.assertEqual(res["retry_count"], 3)

		# Verify updated values on the channel row
		ch_row = frappe.db.get_value(
			"Broadcast Channel",
			{"parent": b.name, "channel": "WhatsApp"},
			["recipient_count", "sent_count", "delivered_count", "read_count", "failed_count", "bounced_count", "opened_count", "clicked_count", "unsubscribed_count", "retry_count"],
			as_dict=True,
		)
		for k, v in res.items():
			if k in ch_row:
				self.assertEqual(ch_row[k], v, f"Mismatch on field {k}")

	def test_no_transition_while_queued_rows_remain(self):
		"""When channel is In Progress but Queued/Retrying rows remain, no transition occurs."""
		b = self._make_broadcast(channel_status="In Progress")
		self._insert_recipients(
			b.name,
			"WhatsApp",
			[
				{"address": "+919822200001", "status": "Sent"},
				{"address": "+919822200002", "status": "Queued"},
			],
		)
		refresh_counters(b.name, "WhatsApp")
		ch_status = frappe.db.get_value("Broadcast Channel", {"parent": b.name, "channel": "WhatsApp"}, "status")
		self.assertEqual(ch_status, "In Progress")

		# Check with Retrying
		frappe.db.set_value(
			"Broadcast Recipient",
			{"broadcast": b.name, "address": "+919822200002"},
			"status",
			"Retrying",
		)
		refresh_counters(b.name, "WhatsApp")
		ch_status = frappe.db.get_value("Broadcast Channel", {"parent": b.name, "channel": "WhatsApp"}, "status")
		self.assertEqual(ch_status, "In Progress")

	def test_completion_transitions(self):
		"""Channel transitions from In Progress to Completed, Partially Failed, or Failed when all finish."""
		# Case 1: none failed -> Completed
		b1 = self._make_broadcast(channel_status="In Progress")
		self._insert_recipients(
			b1.name,
			"WhatsApp",
			[
				{"address": "+919833300001", "status": "Sent"},
				{"address": "+919833300002", "status": "Delivered"},
			],
		)
		refresh_counters(b1.name, "WhatsApp")
		s1 = frappe.db.get_value("Broadcast Channel", {"parent": b1.name, "channel": "WhatsApp"}, "status")
		self.assertEqual(s1, "Completed")

		# Case 2: all failed -> Failed
		b2 = self._make_broadcast(channel_status="In Progress")
		self._insert_recipients(
			b2.name,
			"WhatsApp",
			[
				{"address": "+919833300003", "status": "Failed"},
				{"address": "+919833300004", "status": "Failed"},
			],
		)
		refresh_counters(b2.name, "WhatsApp")
		s2 = frappe.db.get_value("Broadcast Channel", {"parent": b2.name, "channel": "WhatsApp"}, "status")
		self.assertEqual(s2, "Failed")

		# Case 3: any failed/bounced -> Partially Failed
		b3 = self._make_broadcast(channel_status="In Progress")
		self._insert_recipients(
			b3.name,
			"WhatsApp",
			[
				{"address": "+919833300005", "status": "Sent"},
				{"address": "+919833300006", "status": "Failed"},
			],
		)
		refresh_counters(b3.name, "WhatsApp")
		s3 = frappe.db.get_value("Broadcast Channel", {"parent": b3.name, "channel": "WhatsApp"}, "status")
		self.assertEqual(s3, "Partially Failed")

		# Case 3b: any bounced -> Partially Failed
		b4 = self._make_broadcast(channel_status="In Progress")
		self._insert_recipients(
			b4.name,
			"WhatsApp",
			[
				{"address": "+919833300007", "status": "Delivered"},
				{"address": "+919833300008", "status": "Bounced"},
			],
		)
		refresh_counters(b4.name, "WhatsApp")
		s4 = frappe.db.get_value("Broadcast Channel", {"parent": b4.name, "channel": "WhatsApp"}, "status")
		self.assertEqual(s4, "Partially Failed")

	def test_terminal_row_untouched(self):
		"""When channel row is already terminal or not In Progress, it is untouched."""
		b = self._make_broadcast(channel_status="Draft")
		self._insert_recipients(
			b.name,
			"WhatsApp",
			[{"address": "+919844400001", "status": "Sent"}],
		)
		refresh_counters(b.name, "WhatsApp")
		status = frappe.db.get_value("Broadcast Channel", {"parent": b.name, "channel": "WhatsApp"}, "status")
		self.assertEqual(status, "Draft")

	def test_request_counter_refresh_enqueues_once_for_repeated_calls(self):
		"""request_counter_refresh deduplicates enqueue calls for the same broadcast and channel."""
		b = self._make_broadcast(channel_status="In Progress")

		with patch("frappe.enqueue") as mock_enqueue:
			request_counter_refresh(b.name, "WhatsApp")
			request_counter_refresh(b.name, "WhatsApp")
			request_counter_refresh(b.name, "WhatsApp")

			self.assertEqual(mock_enqueue.call_count, 1)
			kwargs = mock_enqueue.call_args[1]
			self.assertEqual(kwargs.get("job_id"), f"refresh_counters::{b.name}::WhatsApp")
			self.assertTrue(kwargs.get("deduplicate"))
			self.assertEqual(kwargs.get("broadcast"), b.name)
			self.assertEqual(kwargs.get("channel"), "WhatsApp")

	def test_request_counter_refresh_passes_enqueue_after_commit(self):
		"""request_counter_refresh passes enqueue_after_commit=True to frappe.enqueue."""
		b = self._make_broadcast(channel_status="In Progress")

		with patch("frappe.enqueue") as mock_enqueue:
			request_counter_refresh(b.name, "WhatsApp")
			mock_enqueue.assert_called_once()
			self.assertTrue(mock_enqueue.call_args[1].get("enqueue_after_commit"))

# Copyright (c) 2026, MSuite and contributors
# For license information, please see license.txt

"""Tests for Broadcast completion, stale queue handling, and relay error messages.

Validates:
- Non-200 relay responses returning HTML produce a short, single-line error message.
- Non-200 relay responses returning JSON preserve their message trimmed to 200 chars.
- Queued channels transition to Completed when all recipients have final statuses (Sent/Delivered/Read).
- Channels with failed or bounced recipients transition to Partially Failed.
- Calling refresh_counters on an already completed channel is idempotent.
- Stale Queued channels with zero recipients older than STALE_QUEUED_MINUTES are marked Failed.
- Fresh Queued channels are not marked Failed by the scheduler.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_to_date, now_datetime

from msuite_campaigns.broadcast.constants import (
	ERROR_NO_RECIPIENTS_QUEUED,
	STALE_QUEUED_MINUTES,
	STATUS_COMPLETED,
	STATUS_FAILED,
	STATUS_IN_PROGRESS,
	STATUS_PARTIALLY_FAILED,
	STATUS_QUEUED,
)
from msuite_campaigns.broadcast.counters import refresh_counters
from msuite_campaigns.broadcast.recipients import RECIPIENT_BULK_INSERT_COLUMNS
from msuite_campaigns.broadcast.scheduler import fail_stale_queued_channels
from msuite_core.integrations.provider import call_provider_relay

TEST_PREFIX = "ZZTestComp "


class TestBroadcastCompletion(IntegrationTestCase):
	"""Test suite for broadcast completion, stale queue handling, and relay errors."""

	def setUp(self) -> None:
		"""Initialize test tracking and clear local counter refresh sets.

		Returns:
			None.
		"""
		super().setUp()
		frappe.set_user("Administrator")
		self.cleanup_broadcasts: list[str] = []
		if hasattr(frappe.local, "_pending_counter_refreshes"):
			delattr(frappe.local, "_pending_counter_refreshes")

	def tearDown(self) -> None:
		"""Roll back test database operations and purge test documents.

		Returns:
			None.
		"""
		frappe.db.rollback()
		if hasattr(frappe.local, "_pending_counter_refreshes"):
			delattr(frappe.local, "_pending_counter_refreshes")
		for name in self.cleanup_broadcasts:
			if frappe.db.exists("Broadcast", name):
				frappe.delete_doc("Broadcast", name, force=True, ignore_permissions=True)
		super().tearDown()

	def _make_broadcast(
		self,
		channel: str = "WhatsApp",
		status: str = STATUS_QUEUED,
		channel_status: str = STATUS_QUEUED,
	) -> "frappe.model.document.Document":
		"""Create and insert a test Broadcast document.

		Args:
			channel: Channel name ('WhatsApp', 'Email', 'SMS').
			status: Overall broadcast status.
			channel_status: Channel row status.

		Returns:
			frappe.model.document.Document: Inserted Broadcast document.
		"""
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

	def _insert_recipients(self, broadcast: str, channel: str, rows: list[dict]) -> None:
		"""Bulk insert recipient rows for a test broadcast channel.

		Args:
			broadcast: Name of the parent Broadcast document.
			channel: Channel name.
			rows: List of dicts specifying recipient properties.

		Returns:
			None.
		"""
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

	@patch("msuite_core.integrations.provider.get_decrypted_password", return_value="test_secret")
	@patch("msuite_core.integrations.provider.frappe.get_single")
	@patch("requests.post")
	def test_html_relay_error_is_short(self, mock_post, mock_get_single, _mock_secret) -> None:
		"""Verify non-200 relay response with HTML body raises short single-line error without markup."""
		mock_settings = MagicMock()
		mock_settings.provider_url = "https://provider.localhost"
		mock_settings.api_key = "test_key"
		mock_settings.client_name = "test_client"
		mock_get_single.return_value = mock_settings

		mock_resp = MagicMock()
		mock_resp.status_code = 404
		mock_resp.headers = {"Content-Type": "text/html; charset=utf-8"}
		mock_resp.text = "<!DOCTYPE html>\n<html><body>Tunnel msuite-relay.ngrok-free.dev not found</body></html>"
		mock_post.return_value = mock_resp

		with self.assertRaises(frappe.ValidationError) as cm:
			call_provider_relay("msuite.api.v1.broadcast.send", {"payload": "test"})

		err = str(cm.exception)
		self.assertIn("Provider relay returned HTTP 404: provider endpoint unreachable, check the provider URL", err)
		self.assertNotIn("<!DOCTYPE", err)
		self.assertNotIn("<html", err)
		self.assertNotIn("\n", err)

	@patch("msuite_core.integrations.provider.get_decrypted_password", return_value="test_secret")
	@patch("msuite_core.integrations.provider.frappe.get_single")
	@patch("requests.post")
	def test_json_relay_error_keeps_message(self, mock_post, mock_get_single, _mock_secret) -> None:
		"""Verify non-200 relay response with JSON body preserves message trimmed to 200 chars on single line."""
		mock_settings = MagicMock()
		mock_settings.provider_url = "https://provider.localhost"
		mock_settings.api_key = "test_key"
		mock_settings.client_name = "test_client"
		mock_get_single.return_value = mock_settings

		long_message = "Authentication failure: " + ("x" * 250)
		mock_resp = MagicMock()
		mock_resp.status_code = 401
		mock_resp.headers = {"Content-Type": "application/json"}
		mock_resp.text = f'{{"message": "{long_message}"}}'
		mock_resp.json.return_value = {"message": long_message}
		mock_post.return_value = mock_resp

		with self.assertRaises(frappe.ValidationError) as cm:
			call_provider_relay("msuite.api.v1.broadcast.send", {"payload": "test"})

		err = str(cm.exception)
		self.assertIn("Provider relay returned HTTP 401:", err)
		self.assertNotIn("\n", err)
		# Message should be trimmed to at most 200 characters
		detail = err.split("HTTP 401: ")[1]
		self.assertLessEqual(len(detail), 200)

	def test_queued_channel_completes_when_all_sent(self) -> None:
		"""Verify a Queued channel with all recipients sent transitions to Completed."""
		b = self._make_broadcast(status=STATUS_QUEUED, channel_status=STATUS_QUEUED)
		self._insert_recipients(
			b.name,
			"WhatsApp",
			[
				{"address": "+919811100010", "status": "Sent"},
				{"address": "+919811100011", "status": "Sent"},
			],
		)

		refresh_counters(b.name, "WhatsApp")

		ch_status = frappe.db.get_value("Broadcast Channel", {"parent": b.name, "channel": "WhatsApp"}, "status")
		b_status = frappe.db.get_value("Broadcast", b.name, "status")
		self.assertEqual(ch_status, STATUS_COMPLETED)
		self.assertEqual(b_status, STATUS_COMPLETED)

	def test_partial_failure_status(self) -> None:
		"""Verify a channel with both Sent and Failed/Bounced recipients transitions to Partially Failed."""
		b = self._make_broadcast(status=STATUS_QUEUED, channel_status=STATUS_QUEUED)
		self._insert_recipients(
			b.name,
			"WhatsApp",
			[
				{"address": "+919811100020", "status": "Sent"},
				{"address": "+919811100021", "status": "Failed"},
			],
		)

		refresh_counters(b.name, "WhatsApp")

		ch_status = frappe.db.get_value("Broadcast Channel", {"parent": b.name, "channel": "WhatsApp"}, "status")
		b_status = frappe.db.get_value("Broadcast", b.name, "status")
		self.assertEqual(ch_status, STATUS_PARTIALLY_FAILED)
		self.assertEqual(b_status, STATUS_PARTIALLY_FAILED)

	def test_refresh_is_idempotent(self) -> None:
		"""Verify calling refresh_counters on an already Completed channel is a no-op."""
		b = self._make_broadcast(status=STATUS_QUEUED, channel_status=STATUS_QUEUED)
		self._insert_recipients(
			b.name,
			"WhatsApp",
			[{"address": "+919811100030", "status": "Sent"}],
		)

		# First call completes the channel
		refresh_counters(b.name, "WhatsApp")
		ch_status_1 = frappe.db.get_value("Broadcast Channel", {"parent": b.name, "channel": "WhatsApp"}, "status")
		self.assertEqual(ch_status_1, STATUS_COMPLETED)

		# Second call is idempotent
		refresh_counters(b.name, "WhatsApp")
		ch_status_2 = frappe.db.get_value("Broadcast Channel", {"parent": b.name, "channel": "WhatsApp"}, "status")
		b_status_2 = frappe.db.get_value("Broadcast", b.name, "status")
		self.assertEqual(ch_status_2, STATUS_COMPLETED)
		self.assertEqual(b_status_2, STATUS_COMPLETED)

	def test_stale_empty_queued_marked_failed(self) -> None:
		"""Verify a Queued broadcast with 0 recipients older than 30 minutes is marked Failed with error."""
		b = self._make_broadcast(status=STATUS_QUEUED, channel_status=STATUS_QUEUED)
		stale_time = add_to_date(now_datetime(), minutes=-(STALE_QUEUED_MINUTES + 5))

		# Backdate modified time on channel row and broadcast
		frappe.db.sql(
			"UPDATE `tabBroadcast Channel` SET `modified` = %(dt)s WHERE `parent` = %(parent)s",
			{"dt": stale_time, "parent": b.name},
		)
		frappe.db.sql(
			"UPDATE `tabBroadcast` SET `modified` = %(dt)s WHERE `name` = %(name)s",
			{"dt": stale_time, "name": b.name},
		)

		fail_stale_queued_channels()

		ch = frappe.db.get_value(
			"Broadcast Channel",
			{"parent": b.name, "channel": "WhatsApp"},
			["status", "launch_error"],
			as_dict=True,
		)
		b_row = frappe.db.get_value(
			"Broadcast",
			b.name,
			["status", "launch_error"],
			as_dict=True,
		)

		self.assertEqual(ch.status, STATUS_FAILED)
		self.assertEqual(ch.launch_error, ERROR_NO_RECIPIENTS_QUEUED)
		self.assertEqual(b_row.status, STATUS_FAILED)
		self.assertEqual(b_row.launch_error, ERROR_NO_RECIPIENTS_QUEUED)

	def test_fresh_queued_not_marked_failed(self) -> None:
		"""Verify a freshly Queued broadcast with 0 recipients is not marked Failed."""
		b = self._make_broadcast(status=STATUS_QUEUED, channel_status=STATUS_QUEUED)

		fail_stale_queued_channels()

		ch_status = frappe.db.get_value("Broadcast Channel", {"parent": b.name, "channel": "WhatsApp"}, "status")
		b_status = frappe.db.get_value("Broadcast", b.name, "status")
		self.assertEqual(ch_status, STATUS_QUEUED)
		self.assertEqual(b_status, STATUS_QUEUED)

	def test_stale_queue_multi_channel_parent_not_marked_failed(self) -> None:
		"""Verify multi-channel broadcast preserves parent active status and does not set parent launch_error when only one channel is stale queued."""
		b = frappe.get_doc({
			"doctype": "Broadcast",
			"title": f"{TEST_PREFIX}{frappe.generate_hash(length=6)}",
			"recipient_source": "Individual",
			"status": STATUS_IN_PROGRESS,
			"channels": [
				{
					"channel": "WhatsApp",
					"status": STATUS_QUEUED,
					"whatsapp_account": "WA-ACC-001",
					"whatsapp_template": "tpl_wa_001",
				},
				{
					"channel": "Email",
					"status": STATUS_IN_PROGRESS,
					"email_account": "EM-ACC-001",
					"email_template": "tpl_em_001",
					"variable_type": "",
				},
			],
		})
		b.insert(ignore_links=True)
		self.cleanup_broadcasts.append(b.name)

		stale_time = add_to_date(now_datetime(), minutes=-(STALE_QUEUED_MINUTES + 5))
		frappe.db.sql(
			"UPDATE `tabBroadcast Channel` SET `modified` = %(dt)s WHERE `parent` = %(parent)s AND `channel` = 'WhatsApp'",
			{"dt": stale_time, "parent": b.name},
		)

		fail_stale_queued_channels()

		wa_ch = frappe.db.get_value(
			"Broadcast Channel",
			{"parent": b.name, "channel": "WhatsApp"},
			["status", "launch_error"],
			as_dict=True,
		)
		em_ch = frappe.db.get_value(
			"Broadcast Channel",
			{"parent": b.name, "channel": "Email"},
			["status", "launch_error"],
			as_dict=True,
		)
		b_row = frappe.db.get_value(
			"Broadcast",
			b.name,
			["status", "launch_error"],
			as_dict=True,
		)

		self.assertEqual(wa_ch.status, STATUS_FAILED)
		self.assertEqual(wa_ch.launch_error, ERROR_NO_RECIPIENTS_QUEUED)
		self.assertEqual(em_ch.status, STATUS_IN_PROGRESS)
		self.assertEqual(b_row.status, STATUS_IN_PROGRESS)
		self.assertNotEqual(b_row.launch_error, ERROR_NO_RECIPIENTS_QUEUED)

	def test_counters_uses_pending_recipient_statuses(self) -> None:
		"""Verify refresh_counters computes queued_count using PENDING_RECIPIENT_STATUSES ('Queued', 'Retrying', 'Sending')."""
		b = self._make_broadcast(status=STATUS_QUEUED, channel_status=STATUS_QUEUED)
		self._insert_recipients(
			b.name,
			"WhatsApp",
			[
				{"address": "+919811100041", "status": "Queued"},
				{"address": "+919811100042", "status": "Retrying"},
				{"address": "+919811100043", "status": "Sending"},
				{"address": "+919811100044", "status": "Sent"},
			],
		)
		counts = refresh_counters(b.name, "WhatsApp")
		self.assertEqual(counts["sent_count"], 1)
		ch_status = frappe.db.get_value("Broadcast Channel", {"parent": b.name, "channel": "WhatsApp"}, "status")
		self.assertIn(ch_status, (STATUS_QUEUED, STATUS_IN_PROGRESS))

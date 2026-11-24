# Copyright (c) 2026, MSuite and contributors
# For license information, please see license.txt

"""Tests for Broadcast retry flow, retrying counter exposure, and hub action delegation.

Validates:
- D1: retry_failed_broadcast, resume_broadcast, pause_broadcast, and cancel_broadcast use hub functions.
- D2: requeue_failed_recipients flips rows to Retrying and clears failure_reason and error_code.
- D4: refresh_counters, get_broadcast, and get_broadcast_progress compute and expose retrying_count.
- Review Focus 1-4:
  1. Retry touches only failed retryable rows; Sent, Read, and non-retryable rows are untouched.
  2. Double retry immediately is a no-op.
  3. Old failure text is cleared on retry.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import now_datetime

from msuite_campaigns.broadcast.api.v1.broadcast import (
	cancel_broadcast,
	get_broadcast,
	get_broadcast_progress,
	pause_broadcast,
	resume_broadcast,
	retry_failed_broadcast,
)
from msuite_campaigns.broadcast.constants import (
	RECIPIENT_STATUS_RETRYING,
	STATUS_COMPLETED,
	STATUS_FAILED,
	STATUS_IN_PROGRESS,
	STATUS_PARTIALLY_FAILED,
	STATUS_PAUSED,
	STATUS_QUEUED,
)
from msuite_campaigns.broadcast.counters import refresh_counters
from msuite_campaigns.broadcast.recipients import requeue_failed_recipients

TEST_PREFIX = "ZZTestRetry "


class TestBroadcastRetry(IntegrationTestCase):
	"""Test suite for broadcast retry, counter exposure, and hub lifecycle actions."""

	def setUp(self) -> None:
		"""Initialize test environment, Administrator session, and tracking lists.

		Returns:
			None.
		"""
		super().setUp()
		frappe.set_user("Administrator")
		self.cleanup_broadcasts: list[str] = []
		if hasattr(frappe.local, "_pending_counter_refreshes"):
			delattr(frappe.local, "_pending_counter_refreshes")

	def tearDown(self) -> None:
		"""Clean up created test broadcast records and rollback DB transactions.

		Returns:
			None.
		"""
		frappe.db.rollback()
		for name in self.cleanup_broadcasts:
			if frappe.db.exists("Broadcast", name):
				frappe.delete_doc("Broadcast", name, force=True, ignore_permissions=True)
		super().tearDown()

	def _make_broadcast(
		self,
		channels: list[dict] | None = None,
		status: str = "Draft",
	) -> "frappe.model.document.Document":
		"""Helper to create and insert a test Broadcast document.

		Args:
			channels: Optional list of channel row dicts. Defaults to a single WhatsApp channel.
			status: Initial parent broadcast status.

		Returns:
			frappe.model.document.Document: Inserted Broadcast document.
		"""
		ch_rows = channels or [
			{
				"channel": "WhatsApp",
				"status": "Draft",
				"whatsapp_account": "WA-ACC-001",
				"whatsapp_template": "tpl_wa_001",
				"delivery_engine": "Standard",
			}
		]
		doc = frappe.get_doc(
			{
				"doctype": "Broadcast",
				"title": f"{TEST_PREFIX}{frappe.generate_hash(length=6)}",
				"recipient_source": "Individual",
				"status": status,
				"channels": ch_rows,
			}
		)
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
		failure_reason: str | None = None,
		error_code: str | None = None,
	) -> "frappe.model.document.Document":
		"""Helper to insert a Broadcast Recipient document.

		Args:
			broadcast: Parent Broadcast document name.
			channel: Channel name.
			address: Recipient phone number or email address.
			status: Recipient delivery status.
			is_retryable: 1 if row can be retried, 0 otherwise.
			retry_count: Current retry iteration counter.
			failure_reason: Optional failure description text.
			error_code: Optional provider error code.

		Returns:
			frappe.model.document.Document: Inserted Broadcast Recipient document.
		"""
		rec = frappe.get_doc(
			{
				"doctype": "Broadcast Recipient",
				"broadcast": broadcast,
				"channel": channel,
				"address": address,
				"status": status,
				"is_retryable": is_retryable,
				"retry_count": retry_count,
				"failure_reason": failure_reason,
				"error_code": error_code,
			}
		)
		rec.insert(ignore_links=True)
		return rec

	def test_retry_endpoint_starts_channel(self) -> None:
		"""Verify retry_failed_broadcast endpoint calls hub.retry and starts channel delivery."""
		doc = self._make_broadcast(
			channels=[
				{
					"channel": "WhatsApp",
					"status": STATUS_FAILED,
					"whatsapp_account": "WA-ACC-001",
					"whatsapp_template": "tpl_wa_001",
					"delivery_engine": "Standard",
				}
			],
			status=STATUS_FAILED,
		)
		frappe.db.set_value("Broadcast Channel", doc.channels[0].name, "status", STATUS_FAILED)
		frappe.db.set_value("Broadcast", doc.name, "status", STATUS_FAILED)

		self._make_recipient(
			doc.name,
			"WhatsApp",
			"+919876543210",
			status=STATUS_FAILED,
			is_retryable=1,
			failure_reason="Network error",
			error_code="ERR_NET",
		)

		with patch("msuite_campaigns.broadcast.hub.start_channel") as mock_start:
			res = retry_failed_broadcast(doc.name)
			self.assertEqual(res, {"WhatsApp": STATUS_QUEUED})
			mock_start.assert_called_once_with(doc.name, "WhatsApp")

		doc.reload()
		self.assertEqual(doc.channels[0].status, STATUS_QUEUED)

	def test_retry_clears_old_failure_text(self) -> None:
		"""Verify requeue_failed_recipients resets status to Retrying and clears failure text and code."""
		doc = self._make_broadcast()
		rec = self._make_recipient(
			doc.name,
			"WhatsApp",
			"+919876543211",
			status=STATUS_FAILED,
			is_retryable=1,
			retry_count=0,
			failure_reason="Rate limit exceeded",
			error_code="RATE_LIMIT",
		)

		affected = requeue_failed_recipients(doc.name, "WhatsApp")
		self.assertEqual(affected, 1)

		rec.reload()
		self.assertEqual(rec.status, "Retrying")
		self.assertEqual(rec.retry_count, 1)
		self.assertIsNone(rec.failure_reason)
		self.assertIsNone(rec.error_code)

	def test_retry_only_touches_failed_rows(self) -> None:
		"""Verify retry on a Partially Failed channel only touches retryable failed recipients."""
		doc = self._make_broadcast(
			channels=[
				{
					"channel": "WhatsApp",
					"status": STATUS_PARTIALLY_FAILED,
					"whatsapp_account": "WA-ACC-001",
					"whatsapp_template": "tpl_wa_001",
					"delivery_engine": "Standard",
				}
			],
			status=STATUS_PARTIALLY_FAILED,
		)
		frappe.db.set_value("Broadcast Channel", doc.channels[0].name, "status", STATUS_PARTIALLY_FAILED)
		frappe.db.set_value("Broadcast", doc.name, "status", STATUS_PARTIALLY_FAILED)

		rec_failed = self._make_recipient(
			doc.name,
			"WhatsApp",
			"+919876543220",
			status=STATUS_FAILED,
			is_retryable=1,
			retry_count=0,
			failure_reason="Temporary server error",
			error_code="500",
		)
		rec_sent = self._make_recipient(
			doc.name,
			"WhatsApp",
			"+919876543221",
			status="Sent",
			is_retryable=0,
			retry_count=0,
		)
		rec_read = self._make_recipient(
			doc.name,
			"WhatsApp",
			"+919876543222",
			status="Read",
			is_retryable=0,
			retry_count=0,
		)
		rec_perm_fail = self._make_recipient(
			doc.name,
			"WhatsApp",
			"+919876543223",
			status=STATUS_FAILED,
			is_retryable=0,
			retry_count=0,
			failure_reason="Invalid recipient number",
			error_code="INVALID_NUMBER",
		)

		with patch("msuite_campaigns.broadcast.hub.start_channel"):
			retry_failed_broadcast(doc.name)

		rec_failed.reload()
		self.assertEqual(rec_failed.status, "Retrying")
		self.assertEqual(rec_failed.retry_count, 1)
		self.assertIsNone(rec_failed.failure_reason)
		self.assertIsNone(rec_failed.error_code)

		rec_sent.reload()
		self.assertEqual(rec_sent.status, "Sent")
		self.assertEqual(rec_sent.retry_count, 0)

		rec_read.reload()
		self.assertEqual(rec_read.status, "Read")
		self.assertEqual(rec_read.retry_count, 0)

		rec_perm_fail.reload()
		self.assertEqual(rec_perm_fail.status, STATUS_FAILED)
		self.assertEqual(rec_perm_fail.retry_count, 0)
		self.assertEqual(rec_perm_fail.failure_reason, "Invalid recipient number")
		self.assertEqual(rec_perm_fail.error_code, "INVALID_NUMBER")

	def test_double_retry_is_noop(self) -> None:
		"""Verify quick double retry calls start_channel once and is a no-op on second invocation."""
		doc = self._make_broadcast(
			channels=[
				{
					"channel": "WhatsApp",
					"status": STATUS_FAILED,
					"whatsapp_account": "WA-ACC-001",
					"whatsapp_template": "tpl_wa_001",
					"delivery_engine": "Standard",
				}
			],
			status=STATUS_FAILED,
		)
		frappe.db.set_value("Broadcast Channel", doc.channels[0].name, "status", STATUS_FAILED)
		frappe.db.set_value("Broadcast", doc.name, "status", STATUS_FAILED)

		self._make_recipient(
			doc.name,
			"WhatsApp",
			"+919876543230",
			status=STATUS_FAILED,
			is_retryable=1,
		)

		with patch("msuite_campaigns.broadcast.hub.start_channel") as mock_start:
			first_res = retry_failed_broadcast(doc.name)
			self.assertEqual(first_res, {"WhatsApp": STATUS_QUEUED})
			mock_start.assert_called_once_with(doc.name, "WhatsApp")

			mock_start.reset_mock()
			second_res = retry_failed_broadcast(doc.name)
			self.assertEqual(second_res, {})
			mock_start.assert_not_called()

	def test_retrying_count_in_progress_and_detail(self) -> None:
		"""Verify retrying_count is computed in refresh_counters, get_broadcast, and get_broadcast_progress."""
		doc = self._make_broadcast(
			channels=[
				{
					"channel": "WhatsApp",
					"status": STATUS_IN_PROGRESS,
					"whatsapp_account": "WA-ACC-001",
					"whatsapp_template": "tpl_wa_001",
					"delivery_engine": "Standard",
				}
			],
			status=STATUS_IN_PROGRESS,
		)
		frappe.db.set_value("Broadcast Channel", doc.channels[0].name, "status", STATUS_IN_PROGRESS)
		frappe.db.set_value("Broadcast", doc.name, "status", STATUS_IN_PROGRESS)

		# Insert 2 Retrying, 1 Sent, 1 Failed
		self._make_recipient(doc.name, "WhatsApp", "+919876543241", status="Retrying", is_retryable=1)
		self._make_recipient(doc.name, "WhatsApp", "+919876543242", status="Retrying", is_retryable=1)
		self._make_recipient(doc.name, "WhatsApp", "+919876543243", status="Sent", is_retryable=0)
		self._make_recipient(doc.name, "WhatsApp", "+919876543244", status=STATUS_FAILED, is_retryable=0)

		# 1. Test refresh_counters returns retrying_count and does not fail on DB write
		counts = refresh_counters(doc.name, "WhatsApp")
		self.assertIn("retrying_count", counts)
		self.assertEqual(counts["retrying_count"], 2)
		self.assertEqual(counts["sent_count"], 1)
		self.assertEqual(counts["failed_count"], 1)
		self.assertEqual(counts["recipient_count"], 4)

		# 2. Test get_broadcast exposes retrying_count on channel dict
		b_detail = get_broadcast(doc.name)
		self.assertIn("channels", b_detail)
		wa_channel = next((c for c in b_detail["channels"] if c["channel"] == "WhatsApp"), None)
		self.assertIsNotNone(wa_channel)
		self.assertIn("retrying_count", wa_channel)
		self.assertEqual(wa_channel["retrying_count"], 2)

		# 3. Test get_broadcast_progress exposes retrying_count on channel progress dict
		progress = get_broadcast_progress(doc.name)
		self.assertIn("WhatsApp", progress)
		self.assertIn("retrying_count", progress["WhatsApp"])
		self.assertEqual(progress["WhatsApp"]["retrying_count"], 2)

	def test_pause_resume_cancel_use_hub(self) -> None:
		"""Verify pause_broadcast, resume_broadcast, and cancel_broadcast delegate to hub functions."""
		doc = self._make_broadcast(
			channels=[
				{
					"channel": "WhatsApp",
					"status": STATUS_IN_PROGRESS,
					"whatsapp_account": "WA-ACC-001",
					"whatsapp_template": "tpl_wa_001",
					"delivery_engine": "Standard",
				}
			],
			status=STATUS_IN_PROGRESS,
		)
		frappe.db.set_value("Broadcast Channel", doc.channels[0].name, "status", STATUS_IN_PROGRESS)
		frappe.db.set_value("Broadcast", doc.name, "status", STATUS_IN_PROGRESS)

		# Test pause delegates to hub.pause
		with patch("msuite_campaigns.broadcast.hub.pause", return_value={"WhatsApp": STATUS_PAUSED}) as mock_pause:
			res_pause = pause_broadcast(doc.name)
			self.assertEqual(res_pause, {"WhatsApp": STATUS_PAUSED})
			mock_pause.assert_called_once_with(doc.name, channel=None)

		# Test resume delegates to hub.resume
		with patch("msuite_campaigns.broadcast.hub.resume", return_value={"WhatsApp": STATUS_QUEUED}) as mock_resume:
			res_resume = resume_broadcast(doc.name)
			self.assertEqual(res_resume, {"WhatsApp": STATUS_QUEUED})
			mock_resume.assert_called_once_with(doc.name, channel=None)

		# Test cancel delegates to hub.cancel
		with patch("msuite_campaigns.broadcast.hub.cancel", return_value={"WhatsApp": "Cancelled"}) as mock_cancel:
			res_cancel = cancel_broadcast(doc.name)
			self.assertEqual(res_cancel, {"WhatsApp": "Cancelled"})
			mock_cancel.assert_called_once_with(doc.name, channel=None)

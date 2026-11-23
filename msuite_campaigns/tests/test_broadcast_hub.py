# Copyright (c) 2026, MSuite and contributors
# For license information, please see license.txt

"""Tests for Broadcast Hub service."""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import now_datetime

from msuite_campaigns.broadcast.hub import (
	cancel,
	launch,
	pause,
	preview,
	resume,
	retry,
	start_channel,
)

TEST_PREFIX = "ZZTestBH "


class TestBroadcastHub(IntegrationTestCase):
	"""Test suite for broadcast/hub.py."""

	def setUp(self):
		super().setUp()
		self.cleanup_broadcasts: list[str] = []
		self.cleanup_recipient_lists: list[str] = []
		if hasattr(frappe.local, "broadcast_channel_adapters"):
			delattr(frappe.local, "broadcast_channel_adapters")
		try:
			frappe.cache.delete_keys("broadcast_aws_triggered:*")
		except Exception:
			pass

	def tearDown(self):
		frappe.db.rollback()
		if hasattr(frappe.local, "broadcast_channel_adapters"):
			delattr(frappe.local, "broadcast_channel_adapters")
		try:
			frappe.cache.delete_keys("broadcast_aws_triggered:*")
		except Exception:
			pass
		for name in self.cleanup_broadcasts:
			if frappe.db.exists("Broadcast", name):
				frappe.delete_doc("Broadcast", name, force=True, ignore_permissions=True)
		for name in self.cleanup_recipient_lists:
			if frappe.db.exists("Recipient List", name):
				frappe.delete_doc("Recipient List", name, force=True, ignore_permissions=True)

	def _make_broadcast(
		self,
		channels: list[str] | None = None,
		recipient_source: str = "Individual",
		recipient_list: str | None = None,
	) -> "frappe.model.document.Document":
		ch_list = channels or ["WhatsApp"]
		channel_rows = []
		for ch in ch_list:
			row = {"channel": ch, "status": "Draft"}
			if ch == "WhatsApp":
				row["whatsapp_account"] = "WA-ACC-001"
				row["whatsapp_template"] = "tpl_wa_001"
			elif ch == "Email":
				row["email_account"] = "EMAIL-ACC-001"
				row["email_template"] = "tpl_email_001"
				row["variable_type"] = ""
			elif ch == "SMS":
				row["sms_account"] = "SMS-ACC-001"
				row["sms_template"] = "tpl_sms_001"
				row["variable_type"] = ""
			channel_rows.append(row)

		doc = frappe.get_doc(
			{
				"doctype": "Broadcast",
				"title": f"{TEST_PREFIX}{frappe.generate_hash(length=6)}",
				"recipient_source": recipient_source,
				"recipient_list": recipient_list,
				"status": "Draft",
				"channels": channel_rows,
			}
		)
		doc.insert(ignore_links=True)
		self.cleanup_broadcasts.append(doc.name)
		return doc

	def _make_recipient_list(self, contacts: list[dict]) -> "frappe.model.document.Document":
		doc = frappe.get_doc(
			{
				"doctype": "Recipient List",
				"list_name": f"{TEST_PREFIX}RL {frappe.generate_hash(length=5)}",
				"contacts": contacts,
			}
		)
		doc.flags.ignore_permissions = True
		doc.insert(ignore_links=True)
		self.cleanup_recipient_lists.append(doc.name)
		return doc

	def test_preview_counts_per_channel_and_writes_nothing(self):
		"""preview calculates reachable and dropped counts without writing to the database."""
		rec_list = self._make_recipient_list(
			[
				{
					"full_name": "Contact 1",
					"phone_e164": "+919811100001",
					"phone_type": "Mobile",
					"phone_status": "Valid",
					"email_normalized": "c1@example.com",
					"email_status": "ok",
				},
				{
					"full_name": "Contact 2",
					"phone_e164": "+919811100002",
					"phone_type": "Mobile",
					"phone_status": "Valid",
					"email_normalized": "",
					"email_status": "",
				},
			]
		)

		recs_before = frappe.db.count("Broadcast Recipient")

		# Preview for Recipient List source
		payload_list = {
			"recipient_source": "Recipient List",
			"recipient_list": rec_list.name,
			"channels": ["WhatsApp", "Email"],
		}
		res_list = preview(payload_list)

		self.assertEqual(res_list["total"], 2)
		self.assertEqual(res_list["channels"]["WhatsApp"]["reachable"], 2)
		self.assertEqual(res_list["channels"]["WhatsApp"]["dropped"], 0)
		self.assertEqual(res_list["channels"]["Email"]["reachable"], 1)
		self.assertEqual(res_list["channels"]["Email"]["dropped"], 1)

		# Preview for Individual source
		payload_ind = {
			"recipient_source": "Individual",
			"channels": ["WhatsApp", "Email"],
			"individual_rows": [
				{"address": "+919822200001", "recipient_name": "A"},
				{"address": "invalid_phone", "recipient_name": "B"},
				{"address": "valid@example.com", "recipient_name": "C"},
			],
		}
		res_ind = preview(payload_ind)
		self.assertEqual(res_ind["total"], 3)
		self.assertEqual(res_ind["channels"]["WhatsApp"]["reachable"], 1)
		self.assertEqual(res_ind["channels"]["WhatsApp"]["dropped"], 2)
		self.assertEqual(res_ind["channels"]["Email"]["reachable"], 1)
		self.assertEqual(res_ind["channels"]["Email"]["dropped"], 2)

		recs_after = frappe.db.count("Broadcast Recipient")
		self.assertEqual(recs_before, recs_after)

	def test_launch_two_channels_where_one_validate_fails(self):
		"""One channel validate_channel failure records launch_error; other channel still launches."""
		b = self._make_broadcast(channels=["WhatsApp", "Email"])

		def wa_validate(broadcast_doc, channel_row):
			frappe.throw("WhatsApp template not approved", frappe.ValidationError)

		wa_adapter = {"channel": "WhatsApp", "validate_channel": wa_validate, "send_one": MagicMock()}
		email_adapter = {"channel": "Email", "validate_channel": lambda b, c: None, "send_one": MagicMock()}

		def mock_get_adapter(channel: str):
			if channel == "WhatsApp":
				return wa_adapter
			if channel == "Email":
				return email_adapter
			return None

		with patch("msuite_campaigns.broadcast.adapters.get_channel_adapter", side_effect=mock_get_adapter):
			with patch("msuite_campaigns.broadcast.hub.start_channel") as mock_start:
				res = launch(
					b.name,
					individual_rows=[
						{"address": "+919811100001"},
						{"address": "email@example.com"},
					],
				)

		self.assertIn("WhatsApp", res)
		self.assertIn("Email", res)
		self.assertIsNotNone(res["WhatsApp"]["error"])
		self.assertIn("not approved", res["WhatsApp"]["error"])
		self.assertIsNone(res["Email"]["error"])
		self.assertEqual(res["Email"]["queued"], 1)

		# Check channel row launch_error in DB
		wa_err = frappe.db.get_value(
			"Broadcast Channel",
			{"parent": b.name, "channel": "WhatsApp"},
			"launch_error",
		)
		self.assertIn("not approved", wa_err)

		# Check broadcast doc launch_error in DB
		b_err = frappe.db.get_value("Broadcast", b.name, "launch_error")
		self.assertIn("not approved", b_err)

		mock_start.assert_called_once_with(b.name, "Email")

	def test_zero_reachable_channel_stays_draft(self):
		"""A channel with 0 reachable recipients stays Draft and reports dropped."""
		b = self._make_broadcast(channels=["WhatsApp"])
		wa_adapter = {"channel": "WhatsApp", "send_one": MagicMock()}

		with patch("msuite_campaigns.broadcast.adapters.get_channel_adapter", return_value=wa_adapter):
			with patch("msuite_campaigns.broadcast.hub.start_channel") as mock_start:
				# All invalid numbers -> 0 reachable
				res = launch(
					b.name,
					individual_rows=[{"address": "invalid"}],
				)

		self.assertEqual(res["WhatsApp"]["status"], "Draft")
		self.assertEqual(res["WhatsApp"]["queued"], 0)
		self.assertEqual(res["WhatsApp"]["dropped"], 1)
		self.assertIsNone(res["WhatsApp"]["error"])

		ch_status = frappe.db.get_value("Broadcast Channel", {"parent": b.name, "channel": "WhatsApp"}, "status")
		self.assertEqual(ch_status, "Draft")
		mock_start.assert_not_called()

	def test_savepoint_isolation(self):
		"""Exception during channel launch is rolled back cleanly via savepoint."""
		b = self._make_broadcast(channels=["WhatsApp", "Email"])

		def email_validate(broadcast_doc, channel_row):
			raise RuntimeError("Fatal unexpected error in Email channel")

		wa_adapter = {"channel": "WhatsApp", "send_one": MagicMock()}
		email_adapter = {"channel": "Email", "validate_channel": email_validate}

		def mock_get_adapter(channel: str):
			return wa_adapter if channel == "WhatsApp" else email_adapter

		with patch("msuite_campaigns.broadcast.adapters.get_channel_adapter", side_effect=mock_get_adapter):
			with patch("msuite_campaigns.broadcast.hub.start_channel"):
				res = launch(
					b.name,
					individual_rows=[
						{"address": "+919811100001"},
						{"address": "email@example.com"},
					],
				)

		self.assertIsNone(res["WhatsApp"]["error"])
		self.assertEqual(res["WhatsApp"]["queued"], 1)
		self.assertIn("Fatal unexpected error", res["Email"]["error"])

	def test_start_channel_standard_vs_aws_stub(self):
		"""start_channel invokes aws.export_and_enqueue_channel when AWS is active, else enqueue."""
		b = self._make_broadcast(channels=["WhatsApp"])
		frappe.db.set_value(
			"Broadcast Channel",
			{"parent": b.name, "channel": "WhatsApp"},
			"status",
			"Queued",
			update_modified=False,
		)
		frappe.db.set_value("Broadcast", b.name, "status", "Queued", update_modified=False)

		# 1. Standard engine path
		with patch("msuite_campaigns.broadcast.enqueue.enqueue_broadcast_channel") as mock_enqueue:
			start_channel(b.name, "WhatsApp")
			mock_enqueue.assert_called_once_with(b.name, "WhatsApp")
			st = frappe.db.get_value("Broadcast Channel", {"parent": b.name, "channel": "WhatsApp"}, "status")
			self.assertEqual(st, "In Progress")

		# Reset status back to Queued
		frappe.db.set_value(
			"Broadcast Channel",
			{"parent": b.name, "channel": "WhatsApp"},
			"status",
			"Queued",
			update_modified=False,
		)

		# 2. AWS engine path via stub module
		stub_aws = types.ModuleType("msuite_campaigns.broadcast.aws")
		stub_aws.should_use_aws = MagicMock(return_value=True)
		stub_aws.export_and_enqueue_channel = MagicMock()

		with patch.dict(sys.modules, {"msuite_campaigns.broadcast.aws": stub_aws}):
			with patch("msuite_campaigns.broadcast.enqueue.enqueue_broadcast_channel") as mock_enqueue:
				start_channel(b.name, "WhatsApp")
				stub_aws.should_use_aws.assert_called_once()
				stub_aws.export_and_enqueue_channel.assert_called_once_with(b.name, "WhatsApp")
				mock_enqueue.assert_not_called()
				st = frappe.db.get_value("Broadcast Channel", {"parent": b.name, "channel": "WhatsApp"}, "status")
				self.assertEqual(st, "In Progress")

	def test_pause_resume_retry_wrappers(self):
		"""hub pause, resume, retry, and cancel delegate to lifecycle and start queued rows."""
		b = self._make_broadcast(channels=["WhatsApp"])
		frappe.db.set_value(
			"Broadcast Channel",
			{"parent": b.name, "channel": "WhatsApp"},
			"status",
			"In Progress",
			update_modified=False,
		)
		frappe.db.set_value("Broadcast", b.name, "status", "In Progress", update_modified=False)

		# Pause
		pause_res = pause(b.name, "WhatsApp")
		self.assertEqual(pause_res.get("WhatsApp"), "Paused")
		st = frappe.db.get_value("Broadcast Channel", {"parent": b.name, "channel": "WhatsApp"}, "status")
		self.assertEqual(st, "Paused")

		# Resume (with sent_count = 0 -> moves to Queued, which triggers start_channel)
		with patch("msuite_campaigns.broadcast.hub.start_channel") as mock_start:
			resume_res = resume(b.name, "WhatsApp")
			self.assertEqual(resume_res.get("WhatsApp"), "Queued")
			mock_start.assert_called_once_with(b.name, "WhatsApp")

		# Retry (move to Failed, then retry_failed moves to Queued, which triggers start_channel)
		frappe.db.set_value(
			"Broadcast Channel",
			{"parent": b.name, "channel": "WhatsApp"},
			"status",
			"Failed",
			update_modified=False,
		)
		frappe.db.set_value("Broadcast", b.name, "status", "Failed", update_modified=False)

		with patch("msuite_campaigns.broadcast.hub.start_channel") as mock_start:
			retry_res = retry(b.name, "WhatsApp")
			self.assertEqual(retry_res.get("WhatsApp"), "Queued")
			mock_start.assert_called_once_with(b.name, "WhatsApp")

		# Cancel
		frappe.db.set_value(
			"Broadcast Channel",
			{"parent": b.name, "channel": "WhatsApp"},
			"status",
			"Queued",
			update_modified=False,
		)
		frappe.db.set_value("Broadcast", b.name, "status", "Queued", update_modified=False)
		cancel_res = cancel(b.name, "WhatsApp")
		self.assertEqual(cancel_res.get("WhatsApp"), "Cancelled")

	def test_pause_aws_in_progress_row_raises(self):
		"""hub.pause on an AWS-engine row that is already In Progress raises ValidationError."""
		b = self._make_broadcast(channels=["WhatsApp"])
		frappe.db.set_value(
			"Broadcast Channel",
			{"parent": b.name, "channel": "WhatsApp"},
			{"status": "In Progress", "delivery_engine": "AWS"},
			update_modified=False,
		)
		frappe.db.set_value("Broadcast", b.name, "status", "In Progress", update_modified=False)

		with patch("msuite_campaigns.broadcast.aws.should_use_aws", return_value=True):
			with self.assertRaises(frappe.ValidationError) as cm:
				pause(b.name, "WhatsApp")
			self.assertIn("cannot pause after hand-off to AWS", str(cm.exception))

	def test_resume_restarts_sending_for_in_progress_row_standard(self):
		"""hub.resume restarts sending when channel row is In Progress with sent_count > 0."""
		from msuite_campaigns.broadcast.recipients import bulk_insert_recipients

		b = self._make_broadcast(channels=["WhatsApp"])
		rec_rows = [{"address": f"+9198111{i:05d}"} for i in range(1000)]
		bulk_insert_recipients(b.name, "WhatsApp", rec_rows)

		# Simulate 100 already sent
		recs = frappe.get_all(
			"Broadcast Recipient",
			filters={"broadcast": b.name, "channel": "WhatsApp"},
			fields=["name"],
			order_by="name asc",
			limit=100,
		)
		for r in recs:
			frappe.db.set_value("Broadcast Recipient", r.name, "status", "Sent", update_modified=False)

		frappe.db.set_value(
			"Broadcast Channel",
			{"parent": b.name, "channel": "WhatsApp"},
			{"status": "Paused", "sent_count": 100},
			update_modified=False,
		)
		frappe.db.set_value("Broadcast", b.name, "status", "Paused", update_modified=False)

		with patch("msuite_campaigns.broadcast.enqueue.enqueue_broadcast_channel") as mock_enqueue:
			res = resume(b.name, "WhatsApp")
			self.assertEqual(res.get("WhatsApp"), "In Progress")
			mock_enqueue.assert_called_once_with(b.name, "WhatsApp")

	def test_resume_never_reexports_aws_row(self):
		"""hub.resume never re-exports an AWS row."""
		b = self._make_broadcast(channels=["WhatsApp"])
		frappe.db.set_value(
			"Broadcast Channel",
			{"parent": b.name, "channel": "WhatsApp"},
			{"status": "Paused", "delivery_engine": "AWS", "sent_count": 0},
			update_modified=False,
		)
		frappe.db.set_value("Broadcast", b.name, "status", "Paused", update_modified=False)

		stub_aws = types.ModuleType("msuite_campaigns.broadcast.aws")
		stub_aws.should_use_aws = MagicMock(return_value=True)
		stub_aws.export_and_enqueue_channel = MagicMock()

		with patch.dict(sys.modules, {"msuite_campaigns.broadcast.aws": stub_aws}):
			res = resume(b.name, "WhatsApp")
			stub_aws.export_and_enqueue_channel.assert_not_called()

	def test_start_channel_idempotent_when_in_progress(self):
		"""start_channel skips Queued->In Progress transition if already In Progress and dispatches."""
		b = self._make_broadcast(channels=["WhatsApp"])
		frappe.db.set_value(
			"Broadcast Channel",
			{"parent": b.name, "channel": "WhatsApp"},
			{"status": "In Progress", "delivery_engine": "Standard"},
			update_modified=False,
		)
		frappe.db.set_value("Broadcast", b.name, "status", "In Progress", update_modified=False)

		with patch("msuite_campaigns.broadcast.enqueue.enqueue_broadcast_channel") as mock_enqueue:
			start_channel(b.name, "WhatsApp")
			mock_enqueue.assert_called_once_with(b.name, "WhatsApp")
			st = frappe.db.get_value("Broadcast Channel", {"parent": b.name, "channel": "WhatsApp"}, "status")
			self.assertEqual(st, "In Progress")

	def test_start_channel_dispatch_failure_transitions_to_failed_and_sets_launch_error(self):
		"""start_channel wraps engine dispatch: failure transitions row to Failed and sets launch_error."""
		b = self._make_broadcast(channels=["WhatsApp"])
		frappe.db.set_value(
			"Broadcast Channel",
			{"parent": b.name, "channel": "WhatsApp"},
			{"status": "Queued", "delivery_engine": "Standard"},
			update_modified=False,
		)
		frappe.db.set_value("Broadcast", b.name, "status", "Queued", update_modified=False)

		with patch(
			"msuite_campaigns.broadcast.enqueue.enqueue_broadcast_channel",
			side_effect=RuntimeError("SQS or worker queue failure"),
		):
			with self.assertRaises(RuntimeError):
				start_channel(b.name, "WhatsApp")

		ch_status = frappe.db.get_value("Broadcast Channel", {"parent": b.name, "channel": "WhatsApp"}, "status")
		ch_err = frappe.db.get_value("Broadcast Channel", {"parent": b.name, "channel": "WhatsApp"}, "launch_error")
		b_status = frappe.db.get_value("Broadcast", b.name, "status")

		self.assertEqual(ch_status, "Failed")
		self.assertEqual(b_status, "Failed")
		self.assertIn("SQS or worker queue failure", ch_err)


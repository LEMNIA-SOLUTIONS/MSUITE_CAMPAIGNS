# Copyright (c) 2026, MSuite and contributors
# For license information, please see license.txt

"""Tests for Broadcast Rollup and Channel Metrics service (msuite_campaigns/broadcast/rollup.py)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

PREFIX = "ZZTestRollup"


class TestBroadcastRollup(IntegrationTestCase):
	"""Test suite for broadcast/rollup.py."""

	def setUp(self):
		super().setUp()
		self.cleanup_broadcasts: list[str] = []

	def tearDown(self):
		frappe.db.rollback()
		for name in self.cleanup_broadcasts:
			if frappe.db.exists("Broadcast", name):
				frappe.delete_doc("Broadcast", name, force=True, ignore_permissions=True)

	def _make_broadcast(
		self,
		channels: list[dict],
		status: str = "Draft",
		campaign: str | None = None,
		legacy_name: str | None = None,
	) -> "frappe.model.document.Document":
		for ch in channels:
			channel_name = ch.get("channel")
			if channel_name == "WhatsApp":
				ch.setdefault("whatsapp_account", "WA-ACC-001")
				ch.setdefault("whatsapp_template", "tpl_wa_001")
			elif channel_name == "Email":
				ch.setdefault("email_account", "test@example.com")
				ch.setdefault("email_template", "tpl_email_001")
				ch["variable_type"] = ""
			elif channel_name == "SMS":
				ch.setdefault("sms_account", "SMS-ACC-001")
				ch.setdefault("sms_template", "tpl_sms_001")
				ch["variable_type"] = ""

		doc = frappe.get_doc(
			{
				"doctype": "Broadcast",
				"title": f"{PREFIX}_{frappe.generate_hash(length=6)}",
				"recipient_source": "Individual",
				"status": status,
				"campaign": campaign,
				"legacy_name": legacy_name,
				"channels": channels,
			}
		)
		for ch_doc in doc.channels:
			if ch_doc.channel != "WhatsApp":
				ch_doc.variable_type = ""
		doc.insert(ignore_links=True)
		self.cleanup_broadcasts.append(doc.name)
		return doc

	def test_standard_rows_read_counters(self):
		"""Standard-engine channel rows read counters directly from the channel row."""
		from msuite_campaigns.broadcast.rollup import channel_metrics

		b = self._make_broadcast(
			channels=[
				{
					"channel": "WhatsApp",
					"status": "Completed",
					"delivery_engine": "Standard",
					"recipient_count": 100,
					"sent_count": 90,
					"delivered_count": 85,
					"read_count": 70,
					"failed_count": 5,
					"retry_count": 2,
				}
			]
		)

		with patch("msuite_campaigns.aws_pipeline.rollup.fetch") as mock_fetch:
			res = channel_metrics(b.name, "WhatsApp")

		mock_fetch.assert_not_called()
		self.assertEqual(res["channel"], "WhatsApp")
		self.assertEqual(res["label"], "WhatsApp")
		self.assertEqual(res["engine"], "standard")
		self.assertFalse(res["stale"])
		self.assertEqual(res["blasts"], 1)
		self.assertEqual(res["retried"], 2)

		m = res["metrics"]
		self.assertEqual(m["audience"], 100)
		self.assertEqual(m["sent"], 90)
		self.assertEqual(m["delivered"], 85)
		self.assertEqual(m["read"], 70)
		self.assertEqual(m["failed"], 5)
		self.assertEqual(m["credits"], 0)
		self.assertEqual(m["delivery_rate"], 94.4)

	def test_standard_sms_reads_actual_credits(self):
		"""Standard-engine SMS channel rows read actual_credits."""
		from msuite_campaigns.broadcast.rollup import channel_metrics

		b = self._make_broadcast(
			channels=[
				{
					"channel": "SMS",
					"status": "Completed",
					"delivery_engine": "Standard",
					"recipient_count": 50,
					"sent_count": 50,
					"delivered_count": 48,
					"actual_credits": 25.5,
				}
			]
		)

		res = channel_metrics(b.name, "SMS")
		self.assertEqual(res["metrics"]["credits"], 25.5)

	def test_rollup_soft_fail_returns_zeros(self):
		"""AWS-engine rows soft-fail to zeros when provider analytics fetch is unreachable."""
		from msuite_campaigns.broadcast.rollup import channel_metrics

		b = self._make_broadcast(
			channels=[
				{
					"channel": "WhatsApp",
					"status": "In Progress",
					"delivery_engine": "AWS",
					"recipient_count": 150,
				}
			],
			campaign="CAMP-AWS-FAIL",
		)

		# Case 1: fetch returns None
		with patch("msuite_campaigns.aws_pipeline.rollup.fetch", return_value=None):
			res = channel_metrics(b.name, "WhatsApp")

		self.assertEqual(res["engine"], "aws")
		self.assertTrue(res["stale"])
		m = res["metrics"]
		self.assertEqual(m["audience"], 150)
		self.assertEqual(m["sent"], 0)
		self.assertEqual(m["delivered"], 0)
		self.assertEqual(m["failed"], 0)
		self.assertEqual(m["read"], 0)
		self.assertEqual(m["delivery_rate"], 0)
		self.assertEqual(res["retried"], 0)

		# Case 2: fetch raises an exception
		with patch("msuite_campaigns.aws_pipeline.rollup.fetch", side_effect=RuntimeError("Provider down")):
			res_exc = channel_metrics(b.name, "WhatsApp")

		self.assertEqual(res_exc["engine"], "aws")
		self.assertTrue(res_exc["stale"])
		self.assertEqual(res_exc["metrics"]["sent"], 0)

	def test_aws_engine_fetches_dynamo_metrics(self):
		"""AWS-engine rows query fetch(dynamo_campaign_id) and map totals properly."""
		from msuite_campaigns.broadcast.rollup import channel_metrics

		b = self._make_broadcast(
			channels=[
				{
					"channel": "WhatsApp",
					"status": "Completed",
					"delivery_engine": "AWS",
					"recipient_count": 200,
				}
			],
			campaign="CAMP-AWS-OK",
		)

		mock_dynamo_data = {
			"sent_count": 180,
			"delivered_count": 170,
			"failed_count": 10,
			"read_count": 140,
			"retried_count": 4,
		}

		with patch("msuite_campaigns.aws_pipeline.rollup.fetch", return_value=mock_dynamo_data) as mock_fetch:
			res = channel_metrics(b.name, "WhatsApp")

		mock_fetch.assert_called_once_with("CAMP-AWS-OK__WhatsApp")
		self.assertEqual(res["engine"], "aws")
		self.assertFalse(res["stale"])
		self.assertEqual(res["retried"], 4)

		m = res["metrics"]
		self.assertEqual(m["audience"], 200)
		self.assertEqual(m["sent"], 180)
		self.assertEqual(m["delivered"], 170)
		self.assertEqual(m["failed"], 10)
		self.assertEqual(m["read"], 140)
		# 170 / 180 * 100 = 94.4%
		self.assertEqual(m["delivery_rate"], 94.4)

	def test_campaign_metrics_merges_two_channels(self):
		"""campaign_metrics merges channels for all broadcasts belonging to a campaign."""
		from msuite_campaigns.broadcast.rollup import campaign_metrics

		campaign_name = f"CAMP_{frappe.generate_hash(length=8)}"

		# Broadcast 1 with WhatsApp
		self._make_broadcast(
			campaign=campaign_name,
			channels=[
				{
					"channel": "WhatsApp",
					"delivery_engine": "Standard",
					"recipient_count": 100,
					"sent_count": 90,
					"delivered_count": 80,
					"read_count": 60,
					"failed_count": 10,
					"retry_count": 1,
				}
			],
		)

		# Broadcast 2 with Email
		self._make_broadcast(
			campaign=campaign_name,
			channels=[
				{
					"channel": "Email",
					"delivery_engine": "Standard",
					"recipient_count": 200,
					"sent_count": 180,
					"delivered_count": 170,
					"failed_count": 10,
					"retry_count": 0,
				}
			],
		)

		res = campaign_metrics(campaign_name)

		self.assertIn("WhatsApp", res)
		self.assertIn("Email", res)

		wa = res["WhatsApp"]
		self.assertEqual(wa["channel"], "WhatsApp")
		self.assertEqual(wa["metrics"]["audience"], 100)
		self.assertEqual(wa["metrics"]["sent"], 90)
		self.assertEqual(wa["metrics"]["delivered"], 80)
		self.assertEqual(wa["metrics"]["read"], 60)

		email = res["Email"]
		self.assertEqual(email["channel"], "Email")
		self.assertEqual(email["metrics"]["audience"], 200)
		self.assertEqual(email["metrics"]["sent"], 180)
		self.assertEqual(email["metrics"]["delivered"], 170)

	def test_campaign_metrics_merges_same_channel_broadcasts(self):
		"""campaign_metrics merges two broadcasts on the same channel (Standard + AWS)."""
		from msuite_campaigns.broadcast.rollup import campaign_metrics

		campaign_name = f"CAMP_MERGE_{frappe.generate_hash(length=8)}"

		# Broadcast 1: Standard WhatsApp
		self._make_broadcast(
			campaign=campaign_name,
			channels=[
				{
					"channel": "WhatsApp",
					"delivery_engine": "Standard",
					"recipient_count": 100,
					"sent_count": 90,
					"delivered_count": 80,
					"read_count": 50,
					"failed_count": 10,
					"retry_count": 1,
				}
			],
		)

		# Broadcast 2: AWS WhatsApp
		self._make_broadcast(
			campaign=campaign_name,
			channels=[
				{
					"channel": "WhatsApp",
					"delivery_engine": "AWS",
					"recipient_count": 200,
				}
			],
		)

		mock_dynamo_data = {
			"sent_count": 180,
			"delivered_count": 170,
			"failed_count": 10,
			"read_count": 120,
			"retried_count": 3,
		}

		with patch("msuite_campaigns.aws_pipeline.rollup.fetch", return_value=mock_dynamo_data):
			res = campaign_metrics(campaign_name)

		self.assertIn("WhatsApp", res)
		wa = res["WhatsApp"]
		self.assertEqual(wa["blasts"], 2)
		self.assertEqual(wa["engine"], "mixed")
		self.assertEqual(wa["retried"], 4)  # 1 + 3

		m = wa["metrics"]
		self.assertEqual(m["audience"], 300)  # 100 + 200
		self.assertEqual(m["sent"], 270)  # 90 + 180
		self.assertEqual(m["delivered"], 250)  # 80 + 170
		self.assertEqual(m["read"], 170)  # 50 + 120
		self.assertEqual(m["failed"], 20)  # 10 + 10
		# 250 / 270 * 100 = 92.6%
		self.assertEqual(m["delivery_rate"], 92.6)

	def test_campaign_metrics_reuses_dynamo_fetch_and_avoids_n_plus_one_get_doc(self):
		"""campaign_metrics avoids N+1 get_doc and reuses DynamoDB fetch results across same-campaign broadcasts."""
		from msuite_campaigns.broadcast.rollup import campaign_metrics

		campaign_name = f"CAMP_OPT_{frappe.generate_hash(length=8)}"

		# Broadcast 1: AWS WhatsApp
		self._make_broadcast(
			campaign=campaign_name,
			channels=[
				{
					"channel": "WhatsApp",
					"delivery_engine": "AWS",
					"recipient_count": 100,
				}
			],
		)

		# Broadcast 2: AWS WhatsApp
		self._make_broadcast(
			campaign=campaign_name,
			channels=[
				{
					"channel": "WhatsApp",
					"delivery_engine": "AWS",
					"recipient_count": 150,
				}
			],
		)

		mock_dynamo_data = {
			"sent_count": 80,
			"delivered_count": 75,
			"failed_count": 5,
			"read_count": 50,
			"retried_count": 1,
		}

		real_get_doc = frappe.get_doc
		get_doc_broadcast_count = 0

		def counting_get_doc(doctype, *args, **kwargs):
			nonlocal get_doc_broadcast_count
			if doctype == "Broadcast":
				get_doc_broadcast_count += 1
			return real_get_doc(doctype, *args, **kwargs)

		with patch("msuite_campaigns.aws_pipeline.rollup.fetch", return_value=mock_dynamo_data) as mock_fetch, patch(
			"frappe.get_doc", side_effect=counting_get_doc
		):
			res = campaign_metrics(campaign_name)

		# 2 broadcasts in the campaign -> get_doc("Broadcast") called 0 times (batch loaded in one query per I-03)
		self.assertEqual(get_doc_broadcast_count, 0)
		# Same campaign key -> aws_rollup.fetch called exactly 1 time
		mock_fetch.assert_called_once_with(f"{campaign_name}__WhatsApp")
		self.assertIn("WhatsApp", res)
		self.assertEqual(res["WhatsApp"]["metrics"]["audience"], 250)

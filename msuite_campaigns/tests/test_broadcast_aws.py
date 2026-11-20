# Copyright (c) 2026, MSuite and contributors
# For license information, please see license.txt

"""Tests for Broadcast AWS hand-off service (msuite_campaigns/broadcast/aws.py)."""

from __future__ import annotations

import json
import sys
import types
from unittest.mock import MagicMock, call, patch

import frappe
from frappe.tests import IntegrationTestCase

from msuite_campaigns.broadcast.constants import RECIPIENT_INSERT_BATCH
from msuite_campaigns.broadcast.recipients import bulk_insert_recipients

PREFIX = "ZZTestAws"


class TestBroadcastAws(IntegrationTestCase):
	"""Test suite for broadcast/aws.py."""

	def setUp(self):
		super().setUp()
		self.cleanup_broadcasts: list[str] = []

		# Ensure mock adapters module exists in sys.modules so lazy imports work
		if "msuite_campaigns.broadcast.adapters" not in sys.modules:
			mock_adapters_mod = types.ModuleType("msuite_campaigns.broadcast.adapters")
			sys.modules["msuite_campaigns.broadcast.adapters"] = mock_adapters_mod

		# Clear any cached trigger entries from previous test runs
		try:
			frappe.cache.delete_keys("broadcast_aws_triggered:*")
		except Exception:
			pass

	def tearDown(self):
		frappe.db.rollback()
		for name in self.cleanup_broadcasts:
			for ch in ("WhatsApp", "Email", "SMS"):
				try:
					frappe.cache.delete_value(f"broadcast_aws_triggered:{name}:{ch}")
				except Exception:
					pass
			if frappe.db.exists("Broadcast", name):
				frappe.delete_doc("Broadcast", name, force=True, ignore_permissions=True)

	def _make_broadcast(
		self,
		channel: str = "WhatsApp",
		status: str = "Draft",
		delivery_engine: str = "AWS",
		campaign: str | None = None,
		legacy_name: str | None = None,
	) -> "frappe.model.document.Document":
		doc = frappe.get_doc(
			{
				"doctype": "Broadcast",
				"title": f"{PREFIX}_{frappe.generate_hash(length=6)}",
				"recipient_source": "Individual",
				"status": status,
				"campaign": campaign,
				"legacy_name": legacy_name,
				"channels": [
					{
						"channel": channel,
						"status": "Queued",
						"delivery_engine": delivery_engine,
						"whatsapp_account": "WA-ACC-001" if channel == "WhatsApp" else None,
						"whatsapp_template": "tpl_wa_001" if channel == "WhatsApp" else None,
					}
				],
			}
		)
		doc.insert(ignore_links=True)
		self.cleanup_broadcasts.append(doc.name)
		return doc

	def test_dynamo_campaign_id_uses_campaign_else_legacy_name_else_name(self):
		"""dynamo_campaign_id uses campaign, else legacy_name, else name."""
		from msuite_campaigns.broadcast.aws import dynamo_campaign_id

		# 1. With campaign set
		doc1 = frappe._dict({"name": "BC-00001", "campaign": "CAMP-001", "legacy_name": "LEG-001"})
		self.assertEqual(dynamo_campaign_id(doc1, "WhatsApp"), "CAMP-001__WhatsApp")

		# 2. Without campaign, with legacy_name
		doc2 = frappe._dict({"name": "BC-00002", "campaign": None, "legacy_name": "LEG-002"})
		self.assertEqual(dynamo_campaign_id(doc2, "Email"), "LEG-002__Email")

		# 3. Without campaign and without legacy_name, uses name
		doc3 = frappe._dict({"name": "BC-00003", "campaign": None, "legacy_name": None})
		self.assertEqual(dynamo_campaign_id(doc3, "SMS"), "BC-00003__SMS")

	def test_dynamo_campaign_id_two_channels_distinct_keys(self):
		"""Two channels on the same broadcast produce distinct dynamo campaign keys."""
		from msuite_campaigns.broadcast.aws import dynamo_campaign_id

		doc = frappe._dict({"name": "BC-SHARED", "campaign": "CAMP-SHARED"})
		wa_key = dynamo_campaign_id(doc, "WhatsApp")
		email_key = dynamo_campaign_id(doc, "Email")

		self.assertEqual(wa_key, "CAMP-SHARED__WhatsApp")
		self.assertEqual(email_key, "CAMP-SHARED__Email")
		self.assertNotEqual(wa_key, email_key)

	def test_should_use_aws_truthy_falsy_cases(self):
		"""should_use_aws checks delivery_engine, min_recipient_threshold, and is_ready."""
		from msuite_campaigns.broadcast.aws import should_use_aws

		broadcast_doc = frappe._dict({"name": "BC-TEST"})

		# 1. Engine is Standard -> False
		row_standard = frappe._dict({"channel": "WhatsApp", "delivery_engine": "Standard", "recipient_count": 100})
		with patch("msuite_campaigns.broadcast.aws.is_ready", return_value=True), patch(
			"msuite_campaigns.broadcast.aws.get_min_recipient_threshold", return_value=0
		):
			self.assertFalse(should_use_aws(broadcast_doc, row_standard))

		# 2. Below threshold (threshold=50, recipient_count=10) -> False
		row_aws_low = frappe._dict({"channel": "WhatsApp", "delivery_engine": "AWS", "recipient_count": 10})
		with patch("msuite_campaigns.broadcast.aws.is_ready", return_value=True), patch(
			"msuite_campaigns.broadcast.aws.get_min_recipient_threshold", return_value=50
		):
			self.assertFalse(should_use_aws(broadcast_doc, row_aws_low))

		# 3. Not ready (is_ready returns False) -> False
		row_aws_ready_false = frappe._dict({"channel": "WhatsApp", "delivery_engine": "AWS", "recipient_count": 100})
		with patch("msuite_campaigns.broadcast.aws.is_ready", return_value=False), patch(
			"msuite_campaigns.broadcast.aws.get_min_recipient_threshold", return_value=50
		):
			self.assertFalse(should_use_aws(broadcast_doc, row_aws_ready_false))

		# 4. Truthy case: engine AWS, recipient_count >= threshold, is_ready is True -> True
		row_aws_ok = frappe._dict({"channel": "WhatsApp", "delivery_engine": "AWS", "recipient_count": 100})
		with patch("msuite_campaigns.broadcast.aws.is_ready", return_value=True), patch(
			"msuite_campaigns.broadcast.aws.get_min_recipient_threshold", return_value=50
		):
			self.assertTrue(should_use_aws(broadcast_doc, row_aws_ok))

	def test_export_order_presign_upload_sync_trigger_record(self):
		"""export_and_enqueue_channel enforces exact order: presign < upload < sync < trigger < record."""
		from msuite_campaigns.broadcast.aws import export_and_enqueue_channel

		b = self._make_broadcast(channel="WhatsApp", delivery_engine="AWS")
		bulk_insert_recipients(b.name, "WhatsApp", [{"address": "+919800000001"}])

		call_order = []

		def mock_presign(channel, bulk_doctype, bulk_doc_name):
			call_order.append("presign")
			return {"url": "https://s3.example.com/put", "key": "manifests/bc.ndjson", "max_bytes": 1000000}

		def mock_upload(url, body):
			call_order.append("upload")

		def mock_sync_secret(broadcast_doc, channel_row):
			call_order.append("sync")

		def mock_trigger(key, channel, bulk_doctype, bulk_doc_name, campaign_id, recipient_count):
			call_order.append("trigger")
			return {"message_id": "msg-aws-123"}

		def mock_record_outbound(broadcast_doc, channel_row):
			call_order.append("record")

		mock_adapter = {
			"channel": "WhatsApp",
			"manifest_header": lambda doc, row: {"channel": "WhatsApp"},
			"manifest_row": lambda doc, row, r: {"phone": r["address"]},
			"sync_secret": mock_sync_secret,
			"record_outbound": mock_record_outbound,
		}

		with patch("msuite_campaigns.aws_pipeline.client.request_upload_url", side_effect=mock_presign), patch(
			"msuite_campaigns.aws_pipeline.client.upload_manifest", side_effect=mock_upload
		), patch("msuite_campaigns.aws_pipeline.client.trigger_campaign", side_effect=mock_trigger), patch(
			"msuite_campaigns.broadcast.aws._require_feature"
		), patch(
			"msuite_campaigns.broadcast.aws._get_channel_adapter", return_value=mock_adapter
		):
			result = export_and_enqueue_channel(b.name, "WhatsApp")

		self.assertEqual(call_order, ["presign", "upload", "sync", "trigger", "record"])
		self.assertEqual(result["s3_key"], "manifests/bc.ndjson")
		self.assertEqual(result["message_id"], "msg-aws-123")
		self.assertEqual(result["queued"], 1)

	def test_sync_secret_failure_aborts_before_trigger(self):
		"""sync_secret failure aborts export before trigger_campaign is invoked."""
		from msuite_campaigns.broadcast.aws import export_and_enqueue_channel

		b = self._make_broadcast(channel="WhatsApp", delivery_engine="AWS")
		bulk_insert_recipients(b.name, "WhatsApp", [{"address": "+919800000002"}])

		mock_trigger = MagicMock()

		def failing_sync(doc, row):
			raise RuntimeError("AWS Secrets Manager unreachable")

		mock_adapter = {
			"channel": "WhatsApp",
			"manifest_header": lambda doc, row: {"channel": "WhatsApp"},
			"manifest_row": lambda doc, row, r: {"phone": r["address"]},
			"sync_secret": failing_sync,
			"record_outbound": MagicMock(),
		}

		with patch("msuite_campaigns.aws_pipeline.client.request_upload_url", return_value={"url": "http://put", "key": "k", "max_bytes": 1000}), patch(
			"msuite_campaigns.aws_pipeline.client.upload_manifest"
		), patch("msuite_campaigns.aws_pipeline.client.trigger_campaign", mock_trigger), patch(
			"msuite_campaigns.broadcast.aws._require_feature"
		), patch(
			"msuite_campaigns.broadcast.aws._get_channel_adapter", return_value=mock_adapter
		):
			with self.assertRaises(frappe.ValidationError):
				export_and_enqueue_channel(b.name, "WhatsApp")

		mock_trigger.assert_not_called()

	def test_ndjson_manifest_has_header_and_queued_rows_skipping_sent(self):
		"""NDJSON manifest has header + one line per Queued/Retrying recipient and skips Sent ones."""
		from msuite_campaigns.broadcast.aws import export_and_enqueue_channel

		b = self._make_broadcast(channel="WhatsApp", delivery_engine="AWS", campaign="CAMP-MANIFEST")
		bulk_insert_recipients(
			b.name,
			"WhatsApp",
			[
				{"address": "+919800000010"},  # Queued
				{"address": "+919800000011"},  # Will be changed to Sent
				{"address": "+919800000012"},  # Retrying
			],
		)
		# Update second recipient to Sent
		recipients = frappe.get_all(
			"Broadcast Recipient",
			filters={"broadcast": b.name, "address": "+919800000011"},
			pluck="name",
		)
		self.assertTrue(recipients)
		frappe.db.set_value("Broadcast Recipient", recipients[0], "status", "Sent", update_modified=False)

		captured_body = {}

		def mock_upload(url, body):
			captured_body["body"] = body

		mock_adapter = {
			"channel": "WhatsApp",
			"manifest_header": lambda doc, row: {"template_name": "welcome_tpl"},
			"manifest_row": lambda doc, row, r: {"phone": r["address"], "status": r["status"]},
		}

		with patch("msuite_campaigns.aws_pipeline.client.request_upload_url", return_value={"url": "http://put", "key": "k1", "max_bytes": 100000}), patch(
			"msuite_campaigns.aws_pipeline.client.upload_manifest", side_effect=mock_upload
		), patch("msuite_campaigns.aws_pipeline.client.trigger_campaign", return_value={"message_id": "m1"}), patch(
			"msuite_campaigns.broadcast.aws._require_feature"
		), patch(
			"msuite_campaigns.broadcast.aws._get_channel_adapter", return_value=mock_adapter
		):
			result = export_and_enqueue_channel(b.name, "WhatsApp")

		body = captured_body.get("body", "")
		lines = body.strip().split("\n")

		# Exactly 3 lines: 1 header + 2 sendable recipients (+919800000010, +919800000012)
		self.assertEqual(len(lines), 3)

		header = json.loads(lines[0])
		self.assertEqual(header.get("bulk_doctype"), "Broadcast")
		self.assertEqual(header.get("bulk_doc_name"), b.name)
		self.assertEqual(header.get("campaign_id"), "CAMP-MANIFEST__WhatsApp")
		self.assertEqual(header.get("template_name"), "welcome_tpl")

		row1 = json.loads(lines[1])
		row2 = json.loads(lines[2])
		addresses = {row1["phone"], row2["phone"]}
		self.assertEqual(addresses, {"+919800000010", "+919800000012"})
		self.assertNotIn("+919800000011", body)
		self.assertEqual(result["queued"], 2)

	def test_2500_recipients_paginate_in_3_queries(self):
		"""2,500 recipients paginate in exactly 3 keyset queries using RECIPIENT_INSERT_BATCH."""
		from msuite_campaigns.broadcast.aws import _recipients_generator

		b = self._make_broadcast(channel="WhatsApp", delivery_engine="AWS")
		# Insert 2,500 rows in batches
		rows = [{"address": f"+9198333{i:05d}"} for i in range(2500)]
		bulk_insert_recipients(b.name, "WhatsApp", rows)

		real_get_all = frappe.get_all
		query_count = 0

		def counting_get_all(doctype, *args, **kwargs):
			nonlocal query_count
			if doctype == "Broadcast Recipient":
				query_count += 1
			return real_get_all(doctype, *args, **kwargs)

		with patch("frappe.get_all", side_effect=counting_get_all):
			recipients = list(_recipients_generator(b.name, "WhatsApp"))

		self.assertEqual(len(recipients), 2500)
		# 2500 recipients with RECIPIENT_INSERT_BATCH=1000:
		# Query 1: 1000 rows
		# Query 2: 1000 rows
		# Query 3: 500 rows (< 1000, generator terminates)
		self.assertEqual(query_count, 3)

	def test_size_limit_enforced(self):
		"""Manifest exceeding max_bytes limit raises ValidationError via _check_size."""
		from msuite_campaigns.broadcast.aws import export_and_enqueue_channel

		b = self._make_broadcast(channel="WhatsApp", delivery_engine="AWS")
		bulk_insert_recipients(b.name, "WhatsApp", [{"address": "+919800000099"}])

		mock_adapter = {
			"channel": "WhatsApp",
			"manifest_header": lambda doc, row: {"header": "large_header_string" * 10},
			"manifest_row": lambda doc, row, r: {"phone": r["address"]},
		}

		# max_bytes = 10, will definitely exceed
		with patch("msuite_campaigns.aws_pipeline.client.request_upload_url", return_value={"url": "http://put", "key": "k", "max_bytes": 10}), patch(
			"msuite_campaigns.broadcast.aws._require_feature"
		), patch(
			"msuite_campaigns.broadcast.aws._get_channel_adapter", return_value=mock_adapter
		):
			with self.assertRaises(frappe.ValidationError):
				export_and_enqueue_channel(b.name, "WhatsApp")

	def test_second_call_after_successful_trigger_does_not_trigger_again(self):
		"""Second call after successful trigger does not re-trigger trigger_campaign."""
		from msuite_campaigns.broadcast.aws import export_and_enqueue_channel

		b = self._make_broadcast(channel="WhatsApp", delivery_engine="AWS")
		bulk_insert_recipients(b.name, "WhatsApp", [{"address": "+919800000088"}])

		mock_adapter = {
			"channel": "WhatsApp",
			"manifest_header": lambda doc, row: {},
			"manifest_row": lambda doc, row, r: {"phone": r["address"]},
		}

		mock_trigger = MagicMock(return_value={"message_id": "sqs-msg-unique"})

		with patch("msuite_campaigns.aws_pipeline.client.request_upload_url", return_value={"url": "http://put", "key": "s3_k", "max_bytes": 100000}), patch(
			"msuite_campaigns.aws_pipeline.client.upload_manifest"
		), patch("msuite_campaigns.aws_pipeline.client.trigger_campaign", mock_trigger), patch(
			"msuite_campaigns.broadcast.aws._require_feature"
		), patch(
			"msuite_campaigns.broadcast.aws._get_channel_adapter", return_value=mock_adapter
		):
			# First call triggers
			res1 = export_and_enqueue_channel(b.name, "WhatsApp")
			self.assertEqual(res1["message_id"], "sqs-msg-unique")
			self.assertEqual(mock_trigger.call_count, 1)

			# Second call must NOT trigger again
			res2 = export_and_enqueue_channel(b.name, "WhatsApp")
			self.assertEqual(mock_trigger.call_count, 1)
			self.assertEqual(res2["s3_key"], "s3_k")
			self.assertEqual(res2["message_id"], "sqs-msg-unique")

	def test_export_succeeds_when_channel_row_already_in_progress_and_second_call_idempotent(self):
		"""export_and_enqueue_channel succeeds when channel row is already In Progress (hub ordering) and guards against re-trigger."""
		from msuite_campaigns.broadcast.aws import export_and_enqueue_channel

		b = self._make_broadcast(channel="WhatsApp", delivery_engine="AWS")
		# Transition channel row to 'In Progress' as hub.start_channel does prior to calling export
		frappe.db.set_value(
			"Broadcast Channel",
			b.channels[0].name,
			"status",
			"In Progress",
			update_modified=False,
		)
		b.reload()
		self.assertEqual(b.channels[0].status, "In Progress")

		bulk_insert_recipients(b.name, "WhatsApp", [{"address": "+919800000077"}])

		mock_adapter = {
			"channel": "WhatsApp",
			"manifest_header": lambda doc, row: {},
			"manifest_row": lambda doc, row, r: {"phone": r["address"]},
		}

		mock_trigger = MagicMock(return_value={"message_id": "sqs-msg-hub-order"})

		with patch("msuite_campaigns.aws_pipeline.client.request_upload_url", return_value={"url": "http://put", "key": "s3_k_hub", "max_bytes": 100000}), patch(
			"msuite_campaigns.aws_pipeline.client.upload_manifest"
		), patch("msuite_campaigns.aws_pipeline.client.trigger_campaign", mock_trigger), patch(
			"msuite_campaigns.broadcast.aws._require_feature"
		), patch(
			"msuite_campaigns.broadcast.aws._get_channel_adapter", return_value=mock_adapter
		):
			# First call triggers even though channel row status is already 'In Progress'
			res1 = export_and_enqueue_channel(b.name, "WhatsApp")
			self.assertEqual(res1["message_id"], "sqs-msg-hub-order")
			self.assertEqual(res1["s3_key"], "s3_k_hub")
			self.assertEqual(res1["queued"], 1)
			self.assertEqual(mock_trigger.call_count, 1)

			# Second call uses cache and does NOT trigger again
			res2 = export_and_enqueue_channel(b.name, "WhatsApp")
			self.assertEqual(mock_trigger.call_count, 1)
			self.assertEqual(res2["message_id"], "sqs-msg-hub-order")
			self.assertEqual(res2["s3_key"], "s3_k_hub")
			self.assertEqual(res2["queued"], 1)

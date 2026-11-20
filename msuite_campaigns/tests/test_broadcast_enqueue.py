# Copyright (c) 2026, MSuite and contributors
# For license information, please see license.txt

"""Tests for Keyset-Chunked Broadcast Enqueue service."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from msuite_campaigns.broadcast.constants import (
	SEND_CHUNK_SIZE,
	SEND_QUEUE,
	SEND_TIMEOUT,
	SENDABLE_RECIPIENT_STATUSES,
)
from msuite_campaigns.broadcast.enqueue import enqueue_broadcast_channel, run_chunk
from msuite_campaigns.broadcast.recipients import bulk_insert_recipients

TEST_PREFIX = "ZZTestBE "


class TestBroadcastEnqueue(IntegrationTestCase):
	"""Test suite for broadcast/enqueue.py."""

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
		channel: str = "WhatsApp",
		status: str = "Draft",
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

	def test_enqueue_splits_1200_recipients_into_3_chunks_contiguous_keyset(self):
		"""Splits 1,200 recipients into 3 chunks with non-overlapping contiguous key ranges."""
		b = self._make_broadcast()
		rows = [{"address": f"+9198111{i:05d}"} for i in range(1200)]
		bulk_insert_recipients(b.name, "WhatsApp", rows)

		enqueued_jobs = []

		def capture_enqueue(fn, **kwargs):
			enqueued_jobs.append(kwargs)

		with patch("frappe.enqueue", side_effect=capture_enqueue):
			chunk_count = enqueue_broadcast_channel(b.name, "WhatsApp")

		self.assertEqual(chunk_count, 3)
		self.assertEqual(len(enqueued_jobs), 3)

		for job in enqueued_jobs:
			self.assertEqual(job.get("queue"), SEND_QUEUE)
			self.assertEqual(job.get("timeout"), SEND_TIMEOUT)
			self.assertEqual(job.get("broadcast"), b.name)
			self.assertEqual(job.get("channel"), "WhatsApp")

		first_bounds = [(j["first_name"], j["last_name"]) for j in enqueued_jobs]
		# Check non-overlapping and contiguous ordering
		self.assertTrue(first_bounds[0][0] <= first_bounds[0][1])
		self.assertTrue(first_bounds[0][1] < first_bounds[1][0])
		self.assertTrue(first_bounds[1][0] <= first_bounds[1][1])
		self.assertTrue(first_bounds[1][1] < first_bounds[2][0])
		self.assertTrue(first_bounds[2][0] <= first_bounds[2][1])

	def test_chunk_skips_non_sendable_statuses(self):
		"""run_chunk only processes recipients whose status is in SENDABLE_RECIPIENT_STATUSES."""
		b = self._make_broadcast()
		rows = [
			{"address": "+919822200001"},  # Queued
			{"address": "+919822200002"},  # Will be changed to Sent
			{"address": "+919822200003"},  # Retrying
		]
		bulk_insert_recipients(b.name, "WhatsApp", rows)

		recs = frappe.get_all(
			"Broadcast Recipient",
			filters={"broadcast": b.name, "channel": "WhatsApp"},
			fields=["name", "address"],
			order_by="name asc",
		)
		first_name = recs[0].name
		last_name = recs[-1].name
		rec_by_addr = {r.address: r.name for r in recs}

		# Mark +919822200002 as Sent (non-sendable) and +919822200003 as Retrying (sendable)
		frappe.db.set_value("Broadcast Recipient", rec_by_addr["+919822200002"], "status", "Sent", update_modified=False)
		frappe.db.set_value("Broadcast Recipient", rec_by_addr["+919822200003"], "status", "Retrying", update_modified=False)

		send_calls = []

		def fake_send_one(broadcast_doc, channel_row, recipient_dict):
			send_calls.append(recipient_dict["address"])
			return {"outcome": "fired"}

		fake_adapter = {"channel": "WhatsApp", "send_one": fake_send_one}
		with patch(
			"msuite_campaigns.campaign.services.relationships.get_adapters",
			return_value={"whatsapp": fake_adapter},
		):
			res = run_chunk(b.name, "WhatsApp", first_name, last_name)

		self.assertEqual(res["sent"], 2)
		self.assertEqual(res["failed"], 0)
		self.assertFalse(res["stopped"])
		self.assertEqual(sorted(send_calls), ["+919822200001", "+919822200003"])

	def test_chunk_stops_when_channel_is_paused(self):
		"""run_chunk stops when channel row status is Paused."""
		b = self._make_broadcast(channel_status="Paused")
		rows = [{"address": "+919833300001"}, {"address": "+919833300002"}]
		bulk_insert_recipients(b.name, "WhatsApp", rows)

		recs = frappe.get_all(
			"Broadcast Recipient",
			filters={"broadcast": b.name, "channel": "WhatsApp"},
			fields=["name"],
			order_by="name asc",
		)
		first_name = recs[0].name
		last_name = recs[-1].name

		fake_send = MagicMock()
		fake_adapter = {"channel": "WhatsApp", "send_one": fake_send}
		with patch(
			"msuite_campaigns.campaign.services.relationships.get_adapters",
			return_value={"whatsapp": fake_adapter},
		):
			res = run_chunk(b.name, "WhatsApp", first_name, last_name)

		self.assertTrue(res["stopped"])
		self.assertEqual(res["sent"], 0)
		fake_send.assert_not_called()

	def test_one_failing_recipient_marked_failed_and_does_not_stop_chunk(self):
		"""One failing recipient is marked Failed via apply_delivery_status without stopping the chunk."""
		b = self._make_broadcast()
		rows = [{"address": "+919844400001"}, {"address": "+919844400002"}]
		bulk_insert_recipients(b.name, "WhatsApp", rows)

		recs = frappe.get_all(
			"Broadcast Recipient",
			filters={"broadcast": b.name, "channel": "WhatsApp"},
			fields=["name", "address"],
			order_by="name asc",
		)
		first_name = recs[0].name
		last_name = recs[-1].name
		rec_by_addr = {r.address: r.name for r in recs}

		def fake_send_one(broadcast_doc, channel_row, recipient_dict):
			if recipient_dict["address"] == "+919844400001":
				raise RuntimeError("WhatsApp API 500: Rate limit exceeded")
			return {"outcome": "fired"}

		fake_adapter = {"channel": "WhatsApp", "send_one": fake_send_one}
		with patch(
			"msuite_campaigns.campaign.services.relationships.get_adapters",
			return_value={"whatsapp": fake_adapter},
		):
			res = run_chunk(b.name, "WhatsApp", first_name, last_name)

		self.assertEqual(res["sent"], 1)
		self.assertEqual(res["failed"], 1)
		self.assertFalse(res["stopped"])

		# Verify DB status for the failed recipient (+919844400001)
		failed_rec = frappe.db.get_value(
			"Broadcast Recipient",
			rec_by_addr["+919844400001"],
			["status", "failure_reason"],
			as_dict=True,
		)
		self.assertEqual(failed_rec.status, "Failed")
		self.assertIn("WhatsApp API 500", failed_rec.failure_reason)

		# Verify DB status for the successful recipient (+919844400002)
		succ_rec = frappe.db.get_value("Broadcast Recipient", rec_by_addr["+919844400002"], "status")
		self.assertEqual(succ_rec, "Queued")

	def test_no_adapter_raises_clear_error(self):
		"""run_chunk raises a clear error when no adapter is registered for the channel."""
		b = self._make_broadcast()
		rows = [{"address": "+919855500001"}]
		bulk_insert_recipients(b.name, "WhatsApp", rows)
		rec = frappe.get_all("Broadcast Recipient", filters={"broadcast": b.name}, fields=["name"])[0]

		with patch(
			"msuite_campaigns.campaign.services.relationships.get_adapters",
			return_value={},
		):
			with self.assertRaises(frappe.ValidationError):
				run_chunk(b.name, "WhatsApp", rec.name, rec.name)

# Copyright (c) 2026, MSuite and contributors
# For license information, please see license.txt

"""Tests for Broadcast Recipient doctype and recipient service utilities."""

from __future__ import annotations

from unittest.mock import MagicMock

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, now_datetime

from msuite_campaigns.broadcast.constants import (
	CHANNELS,
	RECIPIENT_INSERT_BATCH,
	RECIPIENT_STATUS_RANK,
	RECIPIENT_STATUSES,
)
from msuite_campaigns.broadcast.recipients import (
	ALLOWED_DELIVERY_STATUS_FIELDS,
	apply_delivery_status,
	bulk_insert_recipients,
)


class TestBroadcastRecipients(IntegrationTestCase):
	"""Test suite for Broadcast Recipient doctype and recipients.py utilities."""

	def setUp(self):
		super().setUp()
		self.cleanup_broadcasts: list[str] = []

	def tearDown(self):
		frappe.db.rollback()
		for name in self.cleanup_broadcasts:
			if frappe.db.exists("Broadcast", name):
				frappe.delete_doc("Broadcast", name, force=True, ignore_permissions=True)

	def _make_broadcast(self, title: str = "Test Broadcast") -> "Document":
		"""Helper to create and insert a valid Broadcast for testing."""
		doc = frappe.get_doc(
			{
				"doctype": "Broadcast",
				"title": title,
				"recipient_source": "Individual",
				"status": "Draft",
				"channels": [
					{
						"channel": "WhatsApp",
						"whatsapp_account": "WA-ACC-001",
						"whatsapp_template": "tpl_wa_001",
					},
				],
			}
		)
		doc.insert(ignore_links=True)
		self.cleanup_broadcasts.append(doc.name)
		return doc

	def test_indexes_exist(self):
		"""Verify unique key on (broadcast, channel, address) and index on (broadcast, channel, status)."""
		indices = frappe.db.sql("SHOW INDEX FROM `tabBroadcast Recipient`", as_dict=True)
		index_columns: dict[str, list[str]] = {}
		index_unique: dict[str, bool] = {}

		for row in indices:
			key_name = row["Key_name"]
			col_name = row["Column_name"]
			index_columns.setdefault(key_name, []).append(col_name)
			index_unique[key_name] = not bool(row["Non_unique"])

		# Check unique constraint on (broadcast, channel, address)
		found_unique = False
		for key_name, cols in index_columns.items():
			if cols == ["broadcast", "channel", "address"] and index_unique[key_name]:
				found_unique = True
				break
		self.assertTrue(found_unique, "Composite unique constraint on (broadcast, channel, address) not found")

		# Check index on (broadcast, channel, status)
		found_composite_idx = False
		for key_name, cols in index_columns.items():
			if cols == ["broadcast", "channel", "status"]:
				found_composite_idx = True
				break
		self.assertTrue(found_composite_idx, "Composite index on (broadcast, channel, status) not found")

		# Check index on provider_message_id
		found_provider_idx = any("provider_message_id" in cols for cols in index_columns.values())
		self.assertTrue(found_provider_idx, "Index on provider_message_id not found")

	def test_duplicate_address_in_list_inserted_once(self):
		"""In-list duplicate addresses are inserted only once."""
		b = self._make_broadcast()
		rows = [
			{"address": "+919876543210", "recipient_name": "First"},
			{"address": "+919876543210", "recipient_name": "Duplicate In-list"},
			{"address": "+919876543211", "recipient_name": "Second"},
		]
		inserted = bulk_insert_recipients(b.name, "WhatsApp", rows)
		self.assertEqual(inserted, 2)

		recipients = frappe.get_all(
			"Broadcast Recipient",
			filters={"broadcast": b.name, "channel": "WhatsApp"},
			fields=["address", "recipient_name"],
		)
		self.assertEqual(len(recipients), 2)
		addresses = [r.address for r in recipients]
		self.assertEqual(sorted(addresses), ["+919876543210", "+919876543211"])

	def test_second_identical_call_returns_zero(self):
		"""Second identical bulk_insert call returns 0 newly inserted rows."""
		b = self._make_broadcast()
		rows = [
			{"address": "+919876543210"},
			{"address": "+919876543211"},
		]
		first_count = bulk_insert_recipients(b.name, "WhatsApp", rows)
		self.assertEqual(first_count, 2)

		second_count = bulk_insert_recipients(b.name, "WhatsApp", rows)
		self.assertEqual(second_count, 0)

	def test_whitespace_and_empty_address_skipped(self):
		"""Empty or whitespace-only addresses are skipped."""
		b = self._make_broadcast()
		rows = [
			{"address": ""},
			{"address": "   "},
			{"address": None},
			{"address": "  user@example.com  "},
		]
		inserted = bulk_insert_recipients(b.name, "Email", rows)
		self.assertEqual(inserted, 1)

		rec = frappe.get_all(
			"Broadcast Recipient",
			filters={"broadcast": b.name, "channel": "Email"},
			fields=["address"],
		)
		self.assertEqual(len(rec), 1)
		self.assertEqual(rec[0].address, "user@example.com")

	def test_invalid_channel_raises(self):
		"""bulk_insert_recipients throws on invalid channel."""
		b = self._make_broadcast()
		with self.assertRaises(frappe.ValidationError):
			bulk_insert_recipients(b.name, "Slack", [{"address": "123"}])

	def test_unknown_broadcast_raises(self):
		"""bulk_insert_recipients throws on non-existent broadcast."""
		with self.assertRaises(frappe.ValidationError):
			bulk_insert_recipients("BC-NON-EXISTENT", "WhatsApp", [{"address": "+919876543210"}])

	def test_chunking_2500_rows_uses_3_chunks(self):
		"""2,500 rows insert in 3 chunks using RECIPIENT_INSERT_BATCH."""
		b = self._make_broadcast()
		rows = [{"address": f"+9198000{i:05d}"} for i in range(2500)]

		original_bulk_insert = frappe.db.bulk_insert
		call_count = 0

		def counting_bulk_insert(*args, **kwargs):
			nonlocal call_count
			call_count += 1
			return original_bulk_insert(*args, **kwargs)

		frappe.db.bulk_insert = counting_bulk_insert
		try:
			inserted = bulk_insert_recipients(b.name, "WhatsApp", rows)
		finally:
			frappe.db.bulk_insert = original_bulk_insert

		self.assertEqual(call_count, 3)
		self.assertEqual(inserted, 2500)

	def test_out_of_order_delivered_then_sent_keeps_delivered(self):
		"""Out-of-order webhook: Delivered arriving before Sent preserves Delivered."""
		b = self._make_broadcast()
		pm_id = "msg_wa_001"
		bulk_insert_recipients(
			b.name,
			"WhatsApp",
			[{"address": "+919876543210", "provider_message_id": pm_id}],
		)

		now = now_datetime()
		# Delivered arrives first
		applied_del = apply_delivery_status(b.name, "WhatsApp", pm_id, "Delivered", delivered_at=now)
		self.assertTrue(applied_del)
		rec = frappe.db.get_value(
			"Broadcast Recipient",
			{"broadcast": b.name, "channel": "WhatsApp", "provider_message_id": pm_id},
			["status", "delivered_at", "sent_at"],
			as_dict=True,
		)
		self.assertEqual(rec.status, "Delivered")
		self.assertIsNotNone(rec.delivered_at)

		# Later Sent arrives (lower rank 2 vs 3)
		applied_sent = apply_delivery_status(b.name, "WhatsApp", pm_id, "Sent", sent_at=now)
		self.assertFalse(applied_sent)

		# State has not regressed
		rec_after = frappe.db.get_value(
			"Broadcast Recipient",
			{"broadcast": b.name, "channel": "WhatsApp", "provider_message_id": pm_id},
			["status", "delivered_at"],
			as_dict=True,
		)
		self.assertEqual(rec_after.status, "Delivered")

	def test_read_then_delivered_stays_read(self):
		"""Read status does not regress to Delivered."""
		b = self._make_broadcast()
		pm_id = "msg_wa_002"
		bulk_insert_recipients(
			b.name,
			"WhatsApp",
			[{"address": "+919876543211", "provider_message_id": pm_id}],
		)

		now = now_datetime()
		# Update to Read (rank 4)
		applied_read = apply_delivery_status(b.name, "WhatsApp", pm_id, "Read", read_at=now)
		self.assertTrue(applied_read)

		# Delivered arrives (rank 3 < 4)
		applied_del = apply_delivery_status(b.name, "WhatsApp", pm_id, "Delivered", delivered_at=now)
		self.assertFalse(applied_del)

		rec = frappe.db.get_value(
			"Broadcast Recipient",
			{"broadcast": b.name, "channel": "WhatsApp", "provider_message_id": pm_id},
			"status",
		)
		self.assertEqual(rec, "Read")

	def test_failed_then_sent_ignored(self):
		"""Sent cannot overwrite terminal Failed state."""
		b = self._make_broadcast()
		pm_id = "msg_sms_001"
		bulk_insert_recipients(
			b.name,
			"SMS",
			[{"address": "+919876543212", "provider_message_id": pm_id}],
		)

		applied_failed = apply_delivery_status(
			b.name,
			"SMS",
			pm_id,
			"Failed",
			error_code="ERR_101",
			failure_reason="Invalid Number",
			is_retryable=0,
		)
		self.assertTrue(applied_failed)

		applied_sent = apply_delivery_status(b.name, "SMS", pm_id, "Sent")
		self.assertFalse(applied_sent)

		rec = frappe.db.get_value(
			"Broadcast Recipient",
			{"broadcast": b.name, "channel": "SMS", "provider_message_id": pm_id},
			["status", "error_code", "failure_reason"],
			as_dict=True,
		)
		self.assertEqual(rec.status, "Failed")
		self.assertEqual(rec.error_code, "ERR_101")
		self.assertEqual(rec.failure_reason, "Invalid Number")

	def test_sent_then_failed_applies(self):
		"""Sent can transition to Failed (higher rank)."""
		b = self._make_broadcast()
		pm_id = "msg_wa_003"
		bulk_insert_recipients(
			b.name,
			"WhatsApp",
			[{"address": "+919876543213", "provider_message_id": pm_id}],
		)

		apply_delivery_status(b.name, "WhatsApp", pm_id, "Sent")
		applied_failed = apply_delivery_status(
			b.name,
			"WhatsApp",
			pm_id,
			"Failed",
			error_code="131026",
			failure_reason="Undeliverable",
		)
		self.assertTrue(applied_failed)

		rec = frappe.db.get_value(
			"Broadcast Recipient",
			{"broadcast": b.name, "channel": "WhatsApp", "provider_message_id": pm_id},
			["status", "error_code"],
			as_dict=True,
		)
		self.assertEqual(rec.status, "Failed")
		self.assertEqual(rec.error_code, "131026")

	def test_unknown_provider_message_id_returns_false(self):
		"""Unknown provider_message_id returns False without raising."""
		b = self._make_broadcast()
		result = apply_delivery_status(b.name, "WhatsApp", "unknown_id_999", "Delivered")
		self.assertFalse(result)

	def test_two_sequential_identical_updates_second_returns_false(self):
		"""Consecutive identical status updates: first True, second False."""
		b = self._make_broadcast()
		pm_id = "msg_wa_004"
		bulk_insert_recipients(
			b.name,
			"WhatsApp",
			[{"address": "+919876543214", "provider_message_id": pm_id}],
		)

		first = apply_delivery_status(b.name, "WhatsApp", pm_id, "Sent")
		self.assertTrue(first)

		second = apply_delivery_status(b.name, "WhatsApp", pm_id, "Sent")
		self.assertFalse(second)

	def test_engagement_first_wins(self):
		"""When new_status is None, engagement fields apply with first-value-wins."""
		b = self._make_broadcast()
		pm_id = "msg_email_001"
		bulk_insert_recipients(
			b.name,
			"Email",
			[{"address": "user1@example.com", "provider_message_id": pm_id}],
		)

		t1 = now_datetime()
		t2 = add_days(t1, 1)

		# First open applies
		r1 = apply_delivery_status(b.name, "Email", pm_id, None, opened_at=t1)
		self.assertTrue(r1)

		# Second open ignored (first wins)
		r2 = apply_delivery_status(b.name, "Email", pm_id, None, opened_at=t2)
		self.assertFalse(r2)

		# First click applies
		r3 = apply_delivery_status(b.name, "Email", pm_id, None, clicked_at=t1)
		self.assertTrue(r3)

		# Second click ignored
		r4 = apply_delivery_status(b.name, "Email", pm_id, None, clicked_at=t2)
		self.assertFalse(r4)

		rec = frappe.db.get_value(
			"Broadcast Recipient",
			{"broadcast": b.name, "channel": "Email", "provider_message_id": pm_id},
			["opened_at", "clicked_at"],
			as_dict=True,
		)
		self.assertEqual(str(rec.opened_at)[:19], str(t1)[:19])
		self.assertEqual(str(rec.clicked_at)[:19], str(t1)[:19])

	def test_disallowed_field_raises(self):
		"""Passing a disallowed field name raises frappe.ValidationError."""
		b = self._make_broadcast()
		with self.assertRaises(frappe.ValidationError):
			apply_delivery_status(
				b.name,
				"WhatsApp",
				"msg_wa_005",
				"Sent",
				malicious_column="drop_table",
			)

	def test_broadcast_on_trash_removes_only_own_recipients(self):
		"""Deleting a Broadcast removes only its own recipients in batches."""
		b1 = self._make_broadcast("Broadcast 1")
		b2 = self._make_broadcast("Broadcast 2")

		bulk_insert_recipients(
			b1.name,
			"WhatsApp",
			[{"address": f"+9199000{i:03d}"} for i in range(15)],
		)
		bulk_insert_recipients(
			b2.name,
			"WhatsApp",
			[{"address": f"+9199000{i:03d}"} for i in range(10)],
		)

		self.assertEqual(
			frappe.db.count("Broadcast Recipient", {"broadcast": b1.name}),
			15,
		)
		self.assertEqual(
			frappe.db.count("Broadcast Recipient", {"broadcast": b2.name}),
			10,
		)

		frappe.delete_doc("Broadcast", b1.name, force=True, ignore_permissions=True)
		self.cleanup_broadcasts.remove(b1.name)

		self.assertEqual(
			frappe.db.count("Broadcast Recipient", {"broadcast": b1.name}),
			0,
		)
		self.assertEqual(
			frappe.db.count("Broadcast Recipient", {"broadcast": b2.name}),
			10,
		)

	def test_concurrent_single_statement_guard(self):
		"""Assert single atomic statement contains rank guard without prior SELECT."""
		b = self._make_broadcast()
		pm_id = "msg_wa_guard"
		bulk_insert_recipients(
			b.name,
			"WhatsApp",
			[{"address": "+919876543299", "provider_message_id": pm_id}],
		)

		# Spy on frappe.db.sql to inspect query and ensure no SELECT before UPDATE
		queries_executed: list[str] = []
		original_sql = frappe.db.sql

		def sql_spy(query, *args, **kwargs):
			q_str = str(query).strip().upper()
			queries_executed.append(q_str)
			return original_sql(query, *args, **kwargs)

		frappe.db.sql = sql_spy
		try:
			applied = apply_delivery_status(b.name, "WhatsApp", pm_id, "Sent")
		finally:
			frappe.db.sql = original_sql

		self.assertTrue(applied)
		# Ensure no SELECT queries were run inside apply_delivery_status
		select_queries = [q for q in queries_executed if q.startswith("SELECT")]
		self.assertEqual(select_queries, [], "apply_delivery_status must not run SELECT before UPDATE")

		# Ensure UPDATE contains the rank guard
		update_queries = [q for q in queries_executed if q.startswith("UPDATE")]
		self.assertEqual(len(update_queries), 1)
		self.assertIn("CASE `STATUS`", update_queries[0])
		self.assertIn("<", update_queries[0])

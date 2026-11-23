# Copyright (c) 2026, MSuite and contributors
# For license information, please see license.txt

"""Tests for schedule mode and per-channel schedule schema (Task 1).

Validates:
- Broadcast.schedule_mode field options and default.
- Broadcast Channel.scheduled_time field type (Datetime) and no depends_on.
- Patch set_broadcast_schedule_mode sets SCHEDULE_SAME on empty rows and is idempotent.
"""

from __future__ import annotations

import frappe
from frappe.tests import IntegrationTestCase

from msuite_campaigns.broadcast.constants import (
	SCHEDULE_PER_CHANNEL,
	SCHEDULE_SAME,
	WRITABLE_BROADCAST_FIELDS,
	WRITABLE_CHANNEL_FIELDS,
)
from msuite_campaigns.patches.set_broadcast_schedule_mode import execute as execute_patch


class TestScheduleSchema(IntegrationTestCase):
	"""Test suite for Broadcast schedule mode and per-channel scheduled time schema."""

	def setUp(self) -> None:
		"""Set up test environment and clean up list.

		Params: None.
		Returns: None.
		Raises: None.
		Side effects: Resets test user to Administrator and prepares cleanup list.
		"""
		super().setUp()
		frappe.set_user("Administrator")
		self.cleanup_broadcasts: list[str] = []

	def tearDown(self) -> None:
		"""Tear down and clean up created documents.

		Params: None.
		Returns: None.
		Raises: None.
		Side effects: Deletes test documents and rolls back database.
		"""
		frappe.db.rollback()
		for name in self.cleanup_broadcasts:
			if frappe.db.exists("Broadcast", name):
				frappe.delete_doc("Broadcast", name, force=True, ignore_permissions=True)
		super().tearDown()

	def _make_broadcast(self, **kwargs) -> "Document":
		"""Helper to construct and insert a valid Broadcast document.

		Params:
			**kwargs: Overrides for the Broadcast document payload.

		Returns:
			Document: Inserted Broadcast document.

		Raises:
			frappe.ValidationError: If validation fails.

		Side effects:
			Inserts a row into tabBroadcast and tabBroadcast Channel.
		"""
		payload = {
			"doctype": "Broadcast",
			"title": f"ZZTestScheduleSchema {frappe.generate_hash(length=6)}",
			"recipient_source": "Individual",
			"status": "Draft",
			"channels": [
				{
					"channel": "WhatsApp",
					"whatsapp_account": "WA-ACC-001",
					"whatsapp_template": "tpl_wa_001",
				}
			],
		}
		payload.update(kwargs)
		doc = frappe.get_doc(payload)
		doc.flags.ignore_links = True
		doc.insert(ignore_permissions=True)
		self.cleanup_broadcasts.append(doc.name)
		return doc

	def test_schedule_mode_field_options_and_default(self) -> None:
		"""Validate Broadcast.schedule_mode field configuration and constants.

		Params: None.
		Returns: None.
		Raises: AssertionError if field or constants do not match requirements.
		Side effects: None.
		"""
		self.assertEqual(SCHEDULE_SAME, "Same for all channels")
		self.assertEqual(SCHEDULE_PER_CHANNEL, "Per channel")
		self.assertIn("schedule_mode", WRITABLE_BROADCAST_FIELDS)

		meta = frappe.get_meta("Broadcast")
		field = meta.get_field("schedule_mode")
		self.assertIsNotNone(field, "Broadcast must have a 'schedule_mode' field")
		self.assertEqual(field.fieldtype, "Select")
		options = [opt.strip() for opt in (field.options or "").split("\n") if opt.strip()]
		self.assertEqual(options, [SCHEDULE_SAME, SCHEDULE_PER_CHANNEL])
		self.assertEqual(field.default, SCHEDULE_SAME)

	def test_channel_scheduled_time_is_datetime(self) -> None:
		"""Validate Broadcast Channel.scheduled_time field type and options.

		Params: None.
		Returns: None.
		Raises: AssertionError if field is missing, not Datetime, or has depends_on.
		Side effects: None.
		"""
		self.assertIn("scheduled_time", WRITABLE_CHANNEL_FIELDS)

		meta = frappe.get_meta("Broadcast Channel")
		field = meta.get_field("scheduled_time")
		self.assertIsNotNone(field, "Broadcast Channel must have a 'scheduled_time' field")
		self.assertEqual(field.fieldtype, "Datetime")
		self.assertFalse(field.depends_on, "Broadcast Channel.scheduled_time must not have depends_on")

	def test_patch_sets_same_mode_on_existing_broadcasts_and_is_idempotent(self) -> None:
		"""Validate that the patch sets SCHEDULE_SAME on empty schedule_mode and is idempotent.

		Params: None.
		Returns: None.
		Raises: AssertionError if patch fails to backfill or mutates existing values.
		Side effects: Mutates tabBroadcast records.
		"""
		b1 = self._make_broadcast()
		b2 = self._make_broadcast()

		# Simulate legacy or unpopulated records
		frappe.db.sql("UPDATE `tabBroadcast` SET schedule_mode = '' WHERE name = %s", (b1.name,))
		frappe.db.sql("UPDATE `tabBroadcast` SET schedule_mode = %s WHERE name = %s", (SCHEDULE_PER_CHANNEL, b2.name))

		self.assertEqual(frappe.db.get_value("Broadcast", b1.name, "schedule_mode"), "")
		self.assertEqual(frappe.db.get_value("Broadcast", b2.name, "schedule_mode"), SCHEDULE_PER_CHANNEL)

		# Run patch
		execute_patch()

		self.assertEqual(frappe.db.get_value("Broadcast", b1.name, "schedule_mode"), SCHEDULE_SAME)
		self.assertEqual(frappe.db.get_value("Broadcast", b2.name, "schedule_mode"), SCHEDULE_PER_CHANNEL)

		# Idempotency check: run patch again
		execute_patch()

		self.assertEqual(frappe.db.get_value("Broadcast", b1.name, "schedule_mode"), SCHEDULE_SAME)
		self.assertEqual(frappe.db.get_value("Broadcast", b2.name, "schedule_mode"), SCHEDULE_PER_CHANNEL)

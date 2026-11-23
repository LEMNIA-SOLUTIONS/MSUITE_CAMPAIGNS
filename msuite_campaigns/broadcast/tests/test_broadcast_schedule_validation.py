# Copyright (c) 2026, MSuite and contributors
# For license information, please see license.txt

"""Tests for Broadcast schedule controller validation (Task 2).

Validates:
- Same mode rejects scheduled_time on channel rows.
- Per channel mode derives parent scheduled_time as earliest row time.
- Past row time is rejected naming the violating channel.
- Row without scheduled_time means send at launch (effective_time is None).
- Launched channel row scheduled_time cannot be modified.
- Launched channel row cannot be removed.
- Last channel row cannot be removed.
- Mode switch clears row times (Per channel -> Same) or seeds them (Same -> Per channel).
- Email, WhatsApp, SMS rows each accept their own scheduled_time.
"""

from __future__ import annotations

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_to_date, get_datetime, now_datetime

from msuite_campaigns.broadcast.constants import (
	CHANNEL_EMAIL,
	CHANNEL_SMS,
	CHANNEL_WHATSAPP,
	SCHEDULE_PER_CHANNEL,
	SCHEDULE_SAME,
	STATUS_COMPLETED,
	STATUS_DRAFT,
	STATUS_QUEUED,
	STATUS_SCHEDULED,
)


class TestBroadcastScheduleValidation(IntegrationTestCase):
	"""Test suite for Broadcast schedule mode and row validation rules."""

	def setUp(self) -> None:
		"""Set up test environment and initialize document cleanup tracking.

		Params: None.
		Returns: None.
		Raises: None.
		Side effects: Sets current user to Administrator and clears cleanup list.
		"""
		super().setUp()
		frappe.set_user("Administrator")
		self.cleanup_broadcasts: list[str] = []

	def tearDown(self) -> None:
		"""Tear down test state, delete test documents, and rollback database.

		Params: None.
		Returns: None.
		Raises: None.
		Side effects: Deletes test documents and rolls back database transaction.
		"""
		frappe.db.rollback()
		for name in self.cleanup_broadcasts:
			if frappe.db.exists("Broadcast", name):
				frappe.delete_doc("Broadcast", name, force=True, ignore_permissions=True)
		super().tearDown()

	def _make_broadcast(self, **kwargs) -> "Document":
		"""Helper to build and insert a test Broadcast document.

		Params:
			**kwargs: Overrides for Broadcast document fields.

		Returns:
			Document: The inserted Broadcast document.

		Raises:
			frappe.ValidationError: If document invariants fail.

		Side effects:
			Inserts rows into tabBroadcast and tabBroadcast Channel.
		"""
		payload = {
			"doctype": "Broadcast",
			"title": f"ZZTestScheduleValidation {frappe.generate_hash(length=6)}",
			"recipient_source": "Individual",
			"status": STATUS_DRAFT,
			"schedule_mode": SCHEDULE_SAME,
			"channels": [
				{
					"channel": CHANNEL_WHATSAPP,
					"whatsapp_account": "WA-ACC-001",
					"whatsapp_template": "tpl_wa_001",
				}
			],
		}
		payload.update(kwargs)
		for ch in payload.get("channels") or []:
			if ch.get("channel") != CHANNEL_WHATSAPP:
				ch.setdefault("variable_type", "")
		doc = frappe.get_doc(payload)
		doc.flags.ignore_links = True
		doc.insert(ignore_permissions=True)
		self.cleanup_broadcasts.append(doc.name)
		return doc

	def test_same_mode_rejects_row_times(self) -> None:
		"""Ensure Same for all channels mode rejects scheduled_time set on channel rows.

		Params: None.
		Returns: None.
		Raises: AssertionError if ValidationError with expected message is not raised.
		Side effects: None.
		"""
		future_time = add_to_date(now_datetime(), hours=2)
		doc = frappe.get_doc(
			{
				"doctype": "Broadcast",
				"title": f"ZZTestSameMode {frappe.generate_hash(length=6)}",
				"recipient_source": "Individual",
				"status": STATUS_DRAFT,
				"schedule_mode": SCHEDULE_SAME,
				"channels": [
					{
						"channel": CHANNEL_WHATSAPP,
						"whatsapp_account": "WA-ACC-001",
						"whatsapp_template": "tpl_wa_001",
						"scheduled_time": future_time,
					}
				],
			}
		)
		doc.flags.ignore_links = True
		with self.assertRaises(frappe.ValidationError) as ctx:
			doc.insert(ignore_permissions=True)
		self.assertIn("cannot have a scheduled time when schedule mode is Same for all channels", str(ctx.exception))

	def test_per_channel_parent_time_is_earliest_row_time(self) -> None:
		"""Ensure parent scheduled_time derives as the earliest row time in Per channel mode.

		Params: None.
		Returns: None.
		Raises: AssertionError if parent time or row effective times do not match.
		Side effects: Inserts test Broadcast in database.
		"""
		time_wa = add_to_date(now_datetime(), hours=3)
		time_em = add_to_date(now_datetime(), hours=1)
		time_sms = add_to_date(now_datetime(), hours=2)

		doc = self._make_broadcast(
			schedule_mode=SCHEDULE_PER_CHANNEL,
			channels=[
				{
					"channel": CHANNEL_WHATSAPP,
					"whatsapp_account": "WA-ACC-001",
					"whatsapp_template": "tpl_wa_001",
					"scheduled_time": time_wa,
				},
				{
					"channel": CHANNEL_EMAIL,
					"email_account": "EM-ACC-001",
					"email_template": "tpl_em_001",
					"scheduled_time": time_em,
				},
				{
					"channel": CHANNEL_SMS,
					"sms_account": "SMS-ACC-001",
					"sms_template": "tpl_sms_001",
					"scheduled_time": time_sms,
				},
			],
		)
		doc.reload()
		# Parent scheduled_time should be the earliest (time_em)
		self.assertEqual(get_datetime(doc.scheduled_time), get_datetime(time_em))

		wa_row = doc.channel_row(CHANNEL_WHATSAPP)
		em_row = doc.channel_row(CHANNEL_EMAIL)
		sms_row = doc.channel_row(CHANNEL_SMS)

		self.assertEqual(doc.effective_time(wa_row), get_datetime(time_wa))
		self.assertEqual(doc.effective_time(em_row), get_datetime(time_em))
		self.assertEqual(doc.effective_time(sms_row), get_datetime(time_sms))

	def test_past_row_time_rejected_naming_channel(self) -> None:
		"""Ensure Per channel mode rejects a row scheduled_time in the past, naming the channel.

		Params: None.
		Returns: None.
		Raises: AssertionError if ValidationError naming channel is not raised.
		Side effects: None.
		"""
		past_time = add_to_date(now_datetime(), hours=-2)
		doc = frappe.get_doc(
			{
				"doctype": "Broadcast",
				"title": f"ZZTestPastTime {frappe.generate_hash(length=6)}",
				"recipient_source": "Individual",
				"status": STATUS_DRAFT,
				"schedule_mode": SCHEDULE_PER_CHANNEL,
				"channels": [
					{
						"channel": CHANNEL_WHATSAPP,
						"whatsapp_account": "WA-ACC-001",
						"whatsapp_template": "tpl_wa_001",
						"scheduled_time": past_time,
					}
				],
			}
		)
		doc.flags.ignore_links = True
		with self.assertRaises(frappe.ValidationError) as ctx:
			doc.insert(ignore_permissions=True)

		err_msg = str(ctx.exception)
		self.assertIn(CHANNEL_WHATSAPP, err_msg)
		self.assertIn("cannot be in the past", err_msg)

	def test_row_without_time_means_send_at_launch(self) -> None:
		"""Ensure in Per channel mode a row without scheduled_time has effective_time None.

		Params: None.
		Returns: None.
		Raises: AssertionError if validation fails or effective_time is not None.
		Side effects: Inserts test Broadcast in database.
		"""
		future_time = add_to_date(now_datetime(), hours=2)
		doc = self._make_broadcast(
			schedule_mode=SCHEDULE_PER_CHANNEL,
			channels=[
				{
					"channel": CHANNEL_WHATSAPP,
					"whatsapp_account": "WA-ACC-001",
					"whatsapp_template": "tpl_wa_001",
					"scheduled_time": future_time,
				},
				{
					"channel": CHANNEL_EMAIL,
					"email_account": "EM-ACC-001",
					"email_template": "tpl_em_001",
					"scheduled_time": None,
				},
			],
		)
		doc.reload()
		wa_row = doc.channel_row(CHANNEL_WHATSAPP)
		em_row = doc.channel_row(CHANNEL_EMAIL)

		self.assertEqual(doc.effective_time(wa_row), get_datetime(future_time))
		self.assertIsNone(doc.effective_time(em_row))
		self.assertEqual(get_datetime(doc.scheduled_time), get_datetime(future_time))

	def test_launched_row_time_cannot_change(self) -> None:
		"""Ensure modifying scheduled_time on a launched channel row is rejected.

		Params: None.
		Returns: None.
		Raises: AssertionError if editing launched row time does not throw ValidationError.
		Side effects: Updates channel status in database.
		"""
		future_time = add_to_date(now_datetime(), hours=2)
		doc = self._make_broadcast(
			schedule_mode=SCHEDULE_PER_CHANNEL,
			channels=[
				{
					"channel": CHANNEL_WHATSAPP,
					"whatsapp_account": "WA-ACC-001",
					"whatsapp_template": "tpl_wa_001",
					"scheduled_time": future_time,
				}
			],
		)
		row = doc.channels[0]
		# Simulate row having launched by setting status to Queued in DB
		frappe.db.set_value("Broadcast Channel", row.name, "status", STATUS_QUEUED)
		doc.reload()
		self.assertEqual(doc.channels[0].status, STATUS_QUEUED)

		# Attempt to change scheduled_time on the launched row
		new_time = add_to_date(now_datetime(), hours=5)
		doc.channels[0].scheduled_time = new_time
		with self.assertRaises(frappe.ValidationError) as ctx:
			doc.save(ignore_permissions=True)

		err_msg = str(ctx.exception)
		self.assertIn("Cannot change scheduled time for channel WhatsApp", err_msg)
		self.assertIn(STATUS_QUEUED, err_msg)

	def test_launched_row_cannot_be_removed(self) -> None:
		"""Ensure removing a launched channel row is rejected.

		Params: None.
		Returns: None.
		Raises: AssertionError if removing launched row does not throw ValidationError.
		Side effects: Updates channel status in database.
		"""
		future_time = add_to_date(now_datetime(), hours=2)
		doc = self._make_broadcast(
			schedule_mode=SCHEDULE_PER_CHANNEL,
			channels=[
				{
					"channel": CHANNEL_WHATSAPP,
					"whatsapp_account": "WA-ACC-001",
					"whatsapp_template": "tpl_wa_001",
					"scheduled_time": future_time,
				},
				{
					"channel": CHANNEL_EMAIL,
					"email_account": "EM-ACC-001",
					"email_template": "tpl_em_001",
				},
			],
		)
		wa_row = doc.channel_row(CHANNEL_WHATSAPP)
		# Set WhatsApp to Queued
		frappe.db.set_value("Broadcast Channel", wa_row.name, "status", STATUS_QUEUED)
		doc.reload()

		# Remove WhatsApp row, keep Email
		doc.channels = [r for r in doc.channels if r.channel != CHANNEL_WHATSAPP]
		with self.assertRaises(frappe.ValidationError) as ctx:
			doc.save(ignore_permissions=True)

		err_msg = str(ctx.exception)
		self.assertIn("Cannot remove channel WhatsApp", err_msg)
		self.assertIn(STATUS_QUEUED, err_msg)

	def test_last_row_cannot_be_removed(self) -> None:
		"""Ensure removing the last channel row from a Broadcast is rejected.

		Params: None.
		Returns: None.
		Raises: AssertionError if clearing channels does not throw ValidationError.
		Side effects: Creates Broadcast doc.
		"""
		doc = self._make_broadcast()
		doc.channels = []
		with self.assertRaises(frappe.ValidationError) as ctx:
			doc.save(ignore_permissions=True)

		self.assertIn("At least one channel is required", str(ctx.exception))

	def test_mode_switch_clears_or_seeds_row_times(self) -> None:
		"""Ensure switching schedule_mode clears row times (to Same) or seeds them (to Per channel).

		Params: None.
		Returns: None.
		Raises: AssertionError if row times are not cleared or seeded on mode switch.
		Side effects: Updates Broadcast in database.
		"""
		future_time = add_to_date(now_datetime(), hours=3)

		# 1. Start in Per channel mode with a row time
		doc = self._make_broadcast(
			schedule_mode=SCHEDULE_PER_CHANNEL,
			channels=[
				{
					"channel": CHANNEL_WHATSAPP,
					"whatsapp_account": "WA-ACC-001",
					"whatsapp_template": "tpl_wa_001",
					"scheduled_time": future_time,
				}
			],
		)
		self.assertIsNotNone(doc.channels[0].scheduled_time)

		# Switch to Same for all channels -> row times must be cleared
		doc.schedule_mode = SCHEDULE_SAME
		doc.save(ignore_permissions=True)
		doc.reload()
		self.assertEqual(doc.schedule_mode, SCHEDULE_SAME)
		self.assertIsNone(doc.channels[0].scheduled_time)

		# 2. Set parent scheduled_time and switch Same -> Per channel -> row times must be seeded
		doc.scheduled_time = future_time
		doc.schedule_mode = SCHEDULE_PER_CHANNEL
		doc.save(ignore_permissions=True)
		doc.reload()
		self.assertEqual(doc.schedule_mode, SCHEDULE_PER_CHANNEL)
		self.assertEqual(get_datetime(doc.channels[0].scheduled_time), get_datetime(future_time))

	def test_email_whatsapp_sms_rows_each_accept_own_time(self) -> None:
		"""Ensure WhatsApp, Email, and SMS rows each accept and retain their own scheduled_time.

		Params: None.
		Returns: None.
		Raises: AssertionError if row times or parent time derivation do not match.
		Side effects: Inserts test Broadcast in database.
		"""
		time_wa = add_to_date(now_datetime(), hours=2)
		time_em = add_to_date(now_datetime(), hours=4)
		time_sms = add_to_date(now_datetime(), hours=6)

		doc = self._make_broadcast(
			schedule_mode=SCHEDULE_PER_CHANNEL,
			channels=[
				{
					"channel": CHANNEL_WHATSAPP,
					"whatsapp_account": "WA-ACC-001",
					"whatsapp_template": "tpl_wa_001",
					"scheduled_time": time_wa,
				},
				{
					"channel": CHANNEL_EMAIL,
					"email_account": "EM-ACC-001",
					"email_template": "tpl_em_001",
					"scheduled_time": time_em,
				},
				{
					"channel": CHANNEL_SMS,
					"sms_account": "SMS-ACC-001",
					"sms_template": "tpl_sms_001",
					"scheduled_time": time_sms,
				},
			],
		)
		doc.reload()

		wa_row = doc.channel_row(CHANNEL_WHATSAPP)
		em_row = doc.channel_row(CHANNEL_EMAIL)
		sms_row = doc.channel_row(CHANNEL_SMS)

		self.assertEqual(get_datetime(wa_row.scheduled_time), get_datetime(time_wa))
		self.assertEqual(get_datetime(em_row.scheduled_time), get_datetime(time_em))
		self.assertEqual(get_datetime(sms_row.scheduled_time), get_datetime(time_sms))

		self.assertEqual(doc.effective_time(wa_row), get_datetime(time_wa))
		self.assertEqual(doc.effective_time(em_row), get_datetime(time_em))
		self.assertEqual(doc.effective_time(sms_row), get_datetime(time_sms))

		self.assertEqual(get_datetime(doc.scheduled_time), get_datetime(time_wa))

# Copyright (c) 2026, MSuite and contributors
# For license information, please see license.txt

"""Tests for Broadcast, Broadcast Channel, and Broadcast Product doctypes."""

from __future__ import annotations

import json
from pathlib import Path

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, now_datetime

from msuite_campaigns.broadcast.constants import (
	BROADCAST_STATUSES,
	CHANNEL_STATUSES,
	CHANNELS,
)


class TestBroadcastDocType(IntegrationTestCase):
	"""Test validation, naming, helper methods and schema constraints for Broadcast."""

	def setUp(self):
		super().setUp()
		self.cleanup_broadcasts: list[str] = []

	def tearDown(self):
		frappe.db.rollback()
		for name in self.cleanup_broadcasts:
			if frappe.db.exists("Broadcast", name):
				frappe.delete_doc("Broadcast", name, force=True, ignore_permissions=True)

	def _make_broadcast(self, **kwargs) -> "Broadcast":
		"""Helper to construct a Broadcast document with valid defaults."""
		payload = {
			"doctype": "Broadcast",
			"title": "Test Broadcast",
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
		return doc

	def test_empty_channels_rejected(self):
		"""Broadcast with no channels must be rejected."""
		doc = self._make_broadcast(channels=[])
		with self.assertRaises(frappe.ValidationError):
			doc.validate()

	def test_duplicate_channel_rejected(self):
		"""Duplicate rows for the same channel must be rejected naming the channel."""
		doc = self._make_broadcast(
			channels=[
				{
					"channel": "WhatsApp",
					"whatsapp_account": "WA-ACC-001",
					"whatsapp_template": "tpl_wa_001",
				},
				{
					"channel": "WhatsApp",
					"whatsapp_account": "WA-ACC-002",
					"whatsapp_template": "tpl_wa_002",
				},
			]
		)
		with self.assertRaises(frappe.ValidationError) as ctx:
			doc.validate()
		self.assertIn("WhatsApp", str(ctx.exception))

	def test_missing_required_fields_per_channel(self):
		"""Each channel requires its account and template fields."""
		cases = [
			("WhatsApp", {"whatsapp_account": "", "whatsapp_template": "tpl1"}, "whatsapp_account"),
			("WhatsApp", {"whatsapp_account": "acc1", "whatsapp_template": ""}, "whatsapp_template"),
			("Email", {"email_account": "", "email_template": "tpl2"}, "email_account"),
			("Email", {"email_account": "acc2", "email_template": ""}, "email_template"),
			("SMS", {"sms_account": "", "sms_template": "tpl3"}, "sms_account"),
			("SMS", {"sms_account": "acc3", "sms_template": ""}, "sms_template"),
		]
		for channel, fields, missing_field in cases:
			with self.subTest(channel=channel, missing=missing_field):
				channel_row = {"channel": channel}
				channel_row.update(fields)
				doc = self._make_broadcast(channels=[channel_row])
				with self.assertRaises(frappe.ValidationError):
					doc.validate()

	def test_foreign_channel_column_rejected(self):
		"""Setting another channel's field on a channel row must be rejected naming the field."""
		# WhatsApp field on Email row
		email_row = {
			"channel": "Email",
			"email_account": "email@example.com",
			"email_template": "tpl_email",
			"whatsapp_account": "WA-ACC-001",
		}
		doc = self._make_broadcast(channels=[email_row])
		with self.assertRaises(frappe.ValidationError) as ctx:
			doc.validate()
		err_str = str(ctx.exception)
		self.assertTrue("whatsapp_account" in err_str or "WhatsApp Account" in err_str)

		# Email field on SMS row
		sms_row = {
			"channel": "SMS",
			"sms_account": "SMS-ACC-001",
			"sms_template": "tpl_sms",
			"email_account": "email@example.com",
		}
		doc = self._make_broadcast(channels=[sms_row])
		with self.assertRaises(frappe.ValidationError) as ctx:
			doc.validate()
		err_str = str(ctx.exception)
		self.assertTrue("email_account" in err_str or "Email Account" in err_str)

		# SMS field on WhatsApp row
		wa_row = {
			"channel": "WhatsApp",
			"whatsapp_account": "WA-ACC-001",
			"whatsapp_template": "tpl_wa",
			"sms_account": "SMS-ACC-001",
		}
		doc = self._make_broadcast(channels=[wa_row])
		with self.assertRaises(frappe.ValidationError) as ctx:
			doc.validate()
		err_str = str(ctx.exception)
		self.assertTrue("sms_account" in err_str or "SMS Account" in err_str)

	def test_naming_series_regex(self):
		"""Inserted Broadcast name must match ^BC-\\d{4}-\\d{5}$."""
		doc = self._make_broadcast()
		doc.insert(ignore_links=True)
		self.cleanup_broadcasts.append(doc.name)
		self.assertRegex(doc.name, r"^BC-\d{4}-\d{5}$")

	def test_recipient_list_source_rules(self):
		"""recipient_list is required when source is Recipient List, empty when Individual."""
		# Missing recipient_list when source is Recipient List
		doc = self._make_broadcast(recipient_source="Recipient List", recipient_list="")
		with self.assertRaises(frappe.ValidationError):
			doc.validate()

		# Present recipient_list when source is Individual
		doc = self._make_broadcast(recipient_source="Individual", recipient_list="Some List")
		with self.assertRaises(frappe.ValidationError):
			doc.validate()

		# Valid recipient_list when source is Recipient List
		doc = self._make_broadcast(recipient_source="Recipient List", recipient_list="Valid List")
		doc.validate()

	def test_products_rules(self):
		"""products child rows only allowed when a WhatsApp channel is present."""
		# Products with Email only -> rejected
		doc = self._make_broadcast(
			channels=[
				{
					"channel": "Email",
					"email_account": "acc@test.com",
					"email_template": "tpl_email",
				}
			],
			products=[{"item_code": "PROD-123"}],
		)
		with self.assertRaises(frappe.ValidationError):
			doc.validate()

		# Products with WhatsApp channel -> allowed
		doc = self._make_broadcast(
			channels=[
				{
					"channel": "WhatsApp",
					"whatsapp_account": "WA-ACC-001",
					"whatsapp_template": "tpl_wa",
				}
			],
			products=[{"item_code": "PROD-123"}],
		)
		doc.validate()

	def test_schedule_time_rules(self):
		"""scheduled_time cannot be in past when status is Scheduled."""
		# In the past
		doc = self._make_broadcast(
			status="Scheduled",
			scheduled_time=add_days(now_datetime(), -1),
		)
		with self.assertRaises(frappe.ValidationError):
			doc.validate()

		# In the future -> allowed
		doc = self._make_broadcast(
			status="Scheduled",
			scheduled_time=add_days(now_datetime(), 1),
		)
		doc.validate()

	def test_channel_row_helper(self):
		"""channel_row helper returns the matching row or None."""
		doc = self._make_broadcast(
			channels=[
				{
					"channel": "WhatsApp",
					"whatsapp_account": "WA-ACC-001",
					"whatsapp_template": "tpl_wa",
				},
				{
					"channel": "Email",
					"email_account": "acc@test.com",
					"email_template": "tpl_email",
				},
			]
		)
		wa_row = doc.channel_row("WhatsApp")
		self.assertIsNotNone(wa_row)
		self.assertEqual(wa_row.channel, "WhatsApp")

		sms_row = doc.channel_row("SMS")
		self.assertIsNone(sms_row)

	def test_unicode_title(self):
		"""Unicode title is preserved on insert."""
		unicode_title = "🚀 Diwali Promo 2026 🎉 - महा ऑफर"
		doc = self._make_broadcast(title=unicode_title)
		doc.insert(ignore_links=True)
		self.cleanup_broadcasts.append(doc.name)
		self.assertEqual(doc.title, unicode_title)

	def test_status_json_constants_parity(self):
		"""Doctype JSON Select options must match broadcast/constants.py constants."""
		apps_dir = Path(frappe.get_app_path("msuite_campaigns"))
		broadcast_json_path = (
			apps_dir / "broadcast" / "doctype" / "broadcast" / "broadcast.json"
		)
		channel_json_path = (
			apps_dir / "broadcast" / "doctype" / "broadcast_channel" / "broadcast_channel.json"
		)

		with open(broadcast_json_path) as f:
			b_data = json.load(f)
		status_field = next(f for f in b_data["fields"] if f["fieldname"] == "status")
		b_statuses = [s for s in status_field["options"].split("\n") if s]
		self.assertEqual(b_statuses, list(BROADCAST_STATUSES))

		with open(channel_json_path) as f:
			c_data = json.load(f)
		c_status_field = next(f for f in c_data["fields"] if f["fieldname"] == "status")
		c_statuses = [s for s in c_status_field["options"].split("\n") if s]
		self.assertEqual(c_statuses, list(CHANNEL_STATUSES))

		c_channel_field = next(f for f in c_data["fields"] if f["fieldname"] == "channel")
		c_channels = [c for c in c_channel_field["options"].split("\n") if c]
		self.assertEqual(c_channels, list(CHANNELS))

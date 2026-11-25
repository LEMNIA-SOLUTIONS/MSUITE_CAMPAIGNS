# Copyright (c) 2026, MSuite and contributors
# For license information, please see license.txt

"""Tests for planned recipient counts and draft recipient preview (Task 1).

Validates:
- planned_recipient_count per channel row for Draft/Scheduled broadcasts.
- WhatsApp/SMS using valid_phone_count and Email using valid_email_count.
- Individual source planned count is 0 when no rows are saved.
- Stored recipient counts are preserved on launched broadcasts.
- Live recalculation when recipient list is updated after broadcast save.
- Missing or unreadable recipient lists return 0 without raising exceptions.
- get_broadcast_draft_recipients permissions, search, pagination, and status gating.
- get_broadcast_metrics returns 'paused' count.
- list_broadcasts date_range filtering for 'month' and 'quarter'.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, getdate, now_datetime, today

from msuite_campaigns.broadcast.api.v1 import broadcast as broadcast_api
from msuite_campaigns.broadcast.constants import (
	CHANNEL_EMAIL,
	CHANNEL_SMS,
	CHANNEL_WHATSAPP,
	STATUS_COMPLETED,
	STATUS_DRAFT,
	STATUS_FAILED,
	STATUS_PARTIALLY_FAILED,
	STATUS_PAUSED,
	STATUS_SCHEDULED,
)

TEST_PREFIX = "ZZTestPlanRecip "


class TestBroadcastPlannedRecipients(IntegrationTestCase):
	"""Test suite for planned recipient counts and draft recipients API."""

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		self.cleanup_broadcasts: list[str] = []
		self.cleanup_recipient_lists: list[str] = []

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()
		for name in self.cleanup_broadcasts:
			if frappe.db.exists("Broadcast", name):
				frappe.delete_doc("Broadcast", name, force=True, ignore_permissions=True)
		for name in self.cleanup_recipient_lists:
			if frappe.db.exists("Recipient List", name):
				frappe.delete_doc("Recipient List", name, force=True, ignore_permissions=True)

	def _make_recipient_list(
		self,
		valid_phone_count: int = 0,
		valid_email_count: int = 0,
		total_contacts: int = 0,
		contacts: list[dict] | None = None,
	) -> "frappe.model.document.Document":
		"""Create a test Recipient List document."""
		doc = frappe.get_doc({
			"doctype": "Recipient List",
			"list_name": f"{TEST_PREFIX}RL {frappe.generate_hash(length=6)}",
			"valid_phone_count": valid_phone_count,
			"valid_email_count": valid_email_count,
			"total_contacts": total_contacts or len(contacts or []),
			"contacts": contacts or [],
		})
		doc.flags.ignore_permissions = True
		doc.insert(ignore_links=True)
		if valid_phone_count or valid_email_count:
			# Directly ensure the stored counts are as specified
			frappe.db.set_value(
				"Recipient List",
				doc.name,
				{
					"valid_phone_count": valid_phone_count,
					"valid_email_count": valid_email_count,
					"total_contacts": total_contacts or len(contacts or []),
				},
				update_modified=False,
			)
			doc.reload()
		self.cleanup_recipient_lists.append(doc.name)
		return doc

	def _make_broadcast(
		self,
		recipient_source: str | None = None,
		recipient_list: str | None = None,
		channels: list[dict] | None = None,
		status: str = STATUS_DRAFT,
		total_recipients: int = 0,
	) -> "frappe.model.document.Document":
		"""Create a test Broadcast document."""
		if recipient_source is None:
			recipient_source = "Recipient List" if recipient_list else "Individual"

		if not channels:
			channels = [{"channel": CHANNEL_WHATSAPP, "status": status, "recipient_count": total_recipients}]

		formatted_channels = []
		for ch in channels:
			c = dict(ch)
			channel_type = c.get("channel")
			if channel_type == CHANNEL_WHATSAPP:
				c.setdefault("whatsapp_account", "Test WA Account")
				c.setdefault("whatsapp_template", "test_wa_template")
			elif channel_type == CHANNEL_EMAIL:
				c.setdefault("email_account", "Test Email Account")
				c.setdefault("email_template", "test_email_template")
			elif channel_type == CHANNEL_SMS:
				c.setdefault("sms_account", "Test SMS Account")
				c.setdefault("sms_template", "test_sms_template")
			if channel_type != CHANNEL_WHATSAPP:
				c.setdefault("variable_type", "")
			formatted_channels.append(c)

		doc = frappe.get_doc({
			"doctype": "Broadcast",
			"title": f"{TEST_PREFIX}BC {frappe.generate_hash(length=6)}",
			"status": status,
			"recipient_source": recipient_source,
			"recipient_list": recipient_list,
			"total_recipients": total_recipients,
			"channels": formatted_channels,
		})
		doc.flags.ignore_permissions = True
		doc.insert(ignore_links=True)
		self.cleanup_broadcasts.append(doc.name)
		return doc

	def test_draft_list_source_reports_valid_phone_count_for_whatsapp(self):
		"""WhatsApp and SMS channels on draft broadcasts report valid_phone_count."""
		rl = self._make_recipient_list(valid_phone_count=13, valid_email_count=5, total_contacts=13)
		bc = self._make_broadcast(
			recipient_source="Recipient List",
			recipient_list=rl.name,
			channels=[
				{"channel": CHANNEL_WHATSAPP, "status": STATUS_DRAFT, "recipient_count": 0},
				{"channel": CHANNEL_SMS, "status": STATUS_DRAFT, "recipient_count": 0},
			],
			status=STATUS_DRAFT,
		)

		# get_broadcast checks
		detail = broadcast_api.get_broadcast(bc.name)
		channels_map = {c["channel"]: c for c in detail.get("channels", [])}
		self.assertEqual(channels_map[CHANNEL_WHATSAPP].get("planned_recipient_count"), 13)
		self.assertEqual(channels_map[CHANNEL_SMS].get("planned_recipient_count"), 13)
		self.assertEqual(detail.get("planned_recipient_count"), 13)

		# list_broadcasts checks
		list_res = broadcast_api.list_broadcasts(filters=json.dumps([["name", "=", bc.name]]))
		self.assertEqual(list_res["total"], 1)
		row = list_res["rows"][0]
		self.assertEqual(row.get("planned_recipient_count"), 13)
		self.assertEqual(row.get("recipient_count"), 13)
		row_channels = {c["channel"]: c for c in row.get("channels", [])}
		self.assertEqual(row_channels[CHANNEL_WHATSAPP].get("planned_recipient_count"), 13)
		self.assertEqual(row_channels[CHANNEL_SMS].get("planned_recipient_count"), 13)

	def test_email_row_uses_valid_email_count(self):
		"""Email channel on draft broadcast uses valid_email_count from recipient list."""
		rl = self._make_recipient_list(valid_phone_count=13, valid_email_count=8, total_contacts=15)
		bc = self._make_broadcast(
			recipient_source="Recipient List",
			recipient_list=rl.name,
			channels=[
				{"channel": CHANNEL_EMAIL, "status": STATUS_DRAFT, "recipient_count": 0},
			],
			status=STATUS_DRAFT,
		)

		detail = broadcast_api.get_broadcast(bc.name)
		ch = detail["channels"][0]
		self.assertEqual(ch.get("planned_recipient_count"), 8)
		self.assertEqual(detail.get("planned_recipient_count"), 8)

		list_res = broadcast_api.list_broadcasts(filters=json.dumps([["name", "=", bc.name]]))
		row = list_res["rows"][0]
		self.assertEqual(row.get("planned_recipient_count"), 8)
		self.assertEqual(row.get("recipient_count"), 8)
		self.assertEqual(row["channels"][0].get("planned_recipient_count"), 8)

	def test_individual_source_counts_saved_rows(self):
		"""Individual source reports 0 when no individual rows are stored before launch."""
		bc = self._make_broadcast(
			recipient_source="Individual",
			channels=[
				{"channel": CHANNEL_WHATSAPP, "status": STATUS_DRAFT, "recipient_count": 0},
			],
			status=STATUS_DRAFT,
		)

		detail = broadcast_api.get_broadcast(bc.name)
		self.assertEqual(detail["channels"][0].get("planned_recipient_count"), 0)
		self.assertEqual(detail.get("planned_recipient_count"), 0)

		list_res = broadcast_api.list_broadcasts(filters=json.dumps([["name", "=", bc.name]]))
		row = list_res["rows"][0]
		self.assertEqual(row.get("planned_recipient_count"), 0)
		self.assertEqual(row.get("recipient_count"), 0)
		self.assertEqual(row["channels"][0].get("planned_recipient_count"), 0)

	def test_launched_row_keeps_stored_count(self):
		"""Launched broadcast channels keep their real stored recipient_count."""
		rl = self._make_recipient_list(valid_phone_count=50, valid_email_count=40)
		bc = self._make_broadcast(
			recipient_source="Recipient List",
			recipient_list=rl.name,
			channels=[
				{"channel": CHANNEL_WHATSAPP, "status": STATUS_COMPLETED, "recipient_count": 25},
			],
			status=STATUS_COMPLETED,
			total_recipients=25,
		)

		detail = broadcast_api.get_broadcast(bc.name)
		self.assertEqual(detail["channels"][0].get("planned_recipient_count"), 25)
		self.assertEqual(detail.get("planned_recipient_count"), 25)

		list_res = broadcast_api.list_broadcasts(filters=json.dumps([["name", "=", bc.name]]))
		row = list_res["rows"][0]
		self.assertEqual(row.get("planned_recipient_count"), 25)
		self.assertEqual(row.get("recipient_count"), 25)
		self.assertEqual(row["channels"][0].get("planned_recipient_count"), 25)

	def test_list_edit_after_save_changes_planned_count(self):
		"""Planned count follows Recipient List live when the list is edited after save."""
		rl = self._make_recipient_list(valid_phone_count=10, valid_email_count=5)
		bc = self._make_broadcast(
			recipient_source="Recipient List",
			recipient_list=rl.name,
			channels=[
				{"channel": CHANNEL_WHATSAPP, "status": STATUS_DRAFT, "recipient_count": 0},
			],
			status=STATUS_DRAFT,
		)

		detail = broadcast_api.get_broadcast(bc.name)
		self.assertEqual(detail["channels"][0].get("planned_recipient_count"), 10)

		# Now edit the Recipient List directly
		frappe.db.set_value("Recipient List", rl.name, "valid_phone_count", 17, update_modified=False)

		detail_updated = broadcast_api.get_broadcast(bc.name)
		self.assertEqual(detail_updated["channels"][0].get("planned_recipient_count"), 17)
		self.assertEqual(detail_updated.get("planned_recipient_count"), 17)

		list_res = broadcast_api.list_broadcasts(filters=json.dumps([["name", "=", bc.name]]))
		row = list_res["rows"][0]
		self.assertEqual(row.get("planned_recipient_count"), 17)
		self.assertEqual(row.get("recipient_count"), 17)

	def test_missing_recipient_list_returns_zero_not_error(self):
		"""Missing or unreadable Recipient List returns 0 without raising exceptions."""
		bc = self._make_broadcast(
			recipient_source="Recipient List",
			recipient_list="NonExistentRecipientList999",
			channels=[
				{"channel": CHANNEL_WHATSAPP, "status": STATUS_DRAFT, "recipient_count": 0},
			],
			status=STATUS_DRAFT,
		)

		detail = broadcast_api.get_broadcast(bc.name)
		self.assertEqual(detail["channels"][0].get("planned_recipient_count"), 0)
		self.assertEqual(detail.get("planned_recipient_count"), 0)

		list_res = broadcast_api.list_broadcasts(filters=json.dumps([["name", "=", bc.name]]))
		row = list_res["rows"][0]
		self.assertEqual(row.get("planned_recipient_count"), 0)
		self.assertEqual(row.get("recipient_count"), 0)

	def test_draft_recipients_requires_recipient_list_read_permission(self):
		"""Caller without read permission on Recipient List cannot access draft preview."""
		rl = self._make_recipient_list(
			valid_phone_count=1,
			contacts=[{"full_name": "Secret Contact", "phone_e164": "+15551234567"}],
		)
		bc = self._make_broadcast(
			recipient_source="Recipient List",
			recipient_list=rl.name,
			channels=[{"channel": CHANNEL_WHATSAPP, "status": STATUS_DRAFT}],
			status=STATUS_DRAFT,
		)

		original_has_perm = frappe.has_permission

		def mock_has_perm(doctype, ptype="read", doc=None, user=None, verbose=False, throw=False):
			if doctype == "Broadcast":
				return True
			if doctype == "Recipient List":
				if throw:
					frappe.throw("No permission on Recipient List", frappe.PermissionError)
				return False
			return original_has_perm(doctype, ptype, doc=doc, user=user, verbose=verbose, throw=throw)

		with patch("frappe.has_permission", side_effect=mock_has_perm):
			with self.assertRaises(frappe.PermissionError):
				broadcast_api.get_broadcast_draft_recipients(name=bc.name)

	def test_draft_recipients_search_and_paging(self):
		"""Draft recipients endpoint supports search, pagination, and Draft status restriction."""
		contacts = [
			{"full_name": "Alice Adams", "phone_e164": "+15550001", "email_normalized": "alice@example.com"},
			{"full_name": "Bob Brown", "phone_e164": "+15550002", "email_normalized": "bob@example.com"},
			{"full_name": "Charlie Clark", "phone_e164": "+15550003", "email_normalized": "charlie@example.com"},
			{"full_name": "David Davis", "phone_e164": "+15550004", "email_normalized": "david@example.com"},
			{"full_name": "Eve Evans", "phone_e164": "+15550005", "email_normalized": "eve@example.com"},
		]
		rl = self._make_recipient_list(contacts=contacts)
		bc = self._make_broadcast(
			recipient_source="Recipient List",
			recipient_list=rl.name,
			channels=[{"channel": CHANNEL_WHATSAPP, "status": STATUS_DRAFT}],
			status=STATUS_DRAFT,
		)

		# Page 1 (limit 2)
		res1 = broadcast_api.get_broadcast_draft_recipients(name=bc.name, start=0, page_length=2)
		self.assertEqual(res1["total"], 5)
		self.assertEqual(len(res1["rows"]), 2)
		self.assertEqual(res1["rows"][0]["recipient_name"], "Alice Adams")
		self.assertEqual(res1["rows"][0]["address"], "+15550001")

		# Page 2 (offset 2, limit 2)
		res2 = broadcast_api.get_broadcast_draft_recipients(name=bc.name, start=2, page_length=2)
		self.assertEqual(res2["total"], 5)
		self.assertEqual(len(res2["rows"]), 2)
		self.assertEqual(res2["rows"][0]["recipient_name"], "Charlie Clark")

		# Search by name
		search_res = broadcast_api.get_broadcast_draft_recipients(name=bc.name, search="Charlie")
		self.assertEqual(search_res["total"], 1)
		self.assertEqual(search_res["rows"][0]["recipient_name"], "Charlie Clark")
		self.assertEqual(search_res["rows"][0]["address"], "+15550003")

		# Search by address
		search_addr = broadcast_api.get_broadcast_draft_recipients(name=bc.name, search="5550002")
		self.assertEqual(search_addr["total"], 1)
		self.assertEqual(search_addr["rows"][0]["recipient_name"], "Bob Brown")

		# Non-draft broadcast returns empty
		frappe.db.set_value("Broadcast", bc.name, "status", STATUS_COMPLETED, update_modified=False)
		empty_res = broadcast_api.get_broadcast_draft_recipients(name=bc.name)
		self.assertEqual(empty_res, {"rows": [], "total": 0})

	def test_draft_recipients_mixed_channel_address_selection(self):
		"""get_broadcast_draft_recipients formats address based on the requested channel parameter."""
		contacts = [
			{
				"full_name": "Multi Channel User",
				"phone_e164": "+15551234567",
				"email_normalized": "multi@example.com",
			},
			{
				"full_name": "Email Only User",
				"phone_e164": "",
				"email_normalized": "emailonly@example.com",
			},
			{
				"full_name": "Phone Only User",
				"phone_e164": "+15559876543",
				"email_normalized": "",
			},
		]
		rl = self._make_recipient_list(contacts=contacts)
		bc = self._make_broadcast(
			recipient_source="Recipient List",
			recipient_list=rl.name,
			channels=[
				{"channel": CHANNEL_WHATSAPP, "status": STATUS_DRAFT},
				{"channel": CHANNEL_EMAIL, "status": STATUS_DRAFT},
			],
			status=STATUS_DRAFT,
		)

		# 1. Querying with channel=CHANNEL_EMAIL returns email address for multi contact
		email_res = broadcast_api.get_broadcast_draft_recipients(name=bc.name, channel=CHANNEL_EMAIL)
		self.assertEqual(email_res["rows"][0]["address"], "multi@example.com")
		self.assertEqual(email_res["rows"][1]["address"], "emailonly@example.com")
		# Fallback to phone when email is missing
		self.assertEqual(email_res["rows"][2]["address"], "+15559876543")

		# 2. Querying with channel=CHANNEL_WHATSAPP returns phone address for multi contact
		wa_res = broadcast_api.get_broadcast_draft_recipients(name=bc.name, channel=CHANNEL_WHATSAPP)
		self.assertEqual(wa_res["rows"][0]["address"], "+15551234567")
		# Fallback to email when phone is missing
		self.assertEqual(wa_res["rows"][1]["address"], "emailonly@example.com")
		self.assertEqual(wa_res["rows"][2]["address"], "+15559876543")

		# 3. Querying with channel="" (empty) preserves multi-channel default (prefers phone when non-email channel exists)
		empty_ch_res = broadcast_api.get_broadcast_draft_recipients(name=bc.name, channel="")
		self.assertEqual(empty_ch_res["rows"][0]["address"], "+15551234567")

	def test_metrics_include_paused(self):
		"""get_broadcast_metrics includes 'paused' key and counts STATUS_PAUSED broadcasts."""
		bc = self._make_broadcast(status=STATUS_PAUSED)
		metrics = broadcast_api.get_broadcast_metrics()
		self.assertIn("paused", metrics)
		self.assertGreaterEqual(metrics["paused"], 1)
		self.assertGreaterEqual(metrics["total"], metrics["paused"])

	def test_date_range_month_and_quarter(self):
		"""list_broadcasts filters correctly with date_range='month' and date_range='quarter'."""
		# bc_recent modified today
		bc_recent = self._make_broadcast(status=STATUS_DRAFT)

		# bc_old modified 65 days ago
		bc_old = self._make_broadcast(status=STATUS_DRAFT)
		past_65_days = str(add_days(today(), -65))
		frappe.db.set_value("Broadcast", bc_old.name, "modified", past_65_days, update_modified=False)

		# bc_ancient modified 200 days ago
		bc_ancient = self._make_broadcast(status=STATUS_DRAFT)
		past_200_days = str(add_days(today(), -200))
		frappe.db.set_value("Broadcast", bc_ancient.name, "modified", past_200_days, update_modified=False)

		# Query month
		month_res = broadcast_api.list_broadcasts(
			date_range="month",
			filters=json.dumps([["name", "in", [bc_recent.name, bc_old.name, bc_ancient.name]]]),
		)
		month_names = [r["name"] for r in month_res["rows"]]
		self.assertIn(bc_recent.name, month_names)
		self.assertNotIn(bc_old.name, month_names)
		self.assertNotIn(bc_ancient.name, month_names)

		# Query quarter
		quarter_res = broadcast_api.list_broadcasts(
			date_range="quarter",
			filters=json.dumps([["name", "in", [bc_recent.name, bc_old.name, bc_ancient.name]]]),
		)
		quarter_names = [r["name"] for r in quarter_res["rows"]]
		self.assertIn(bc_recent.name, quarter_names)
		self.assertNotIn(bc_ancient.name, quarter_names)

	def test_list_broadcasts_respects_doc_level_recipient_list_read_permission(self):
		"""list_broadcasts planned_recipient_count evaluates to 0 when user lacks doc-level permission on recipient list."""
		rl = self._make_recipient_list(valid_phone_count=42, valid_email_count=42)
		bc = self._make_broadcast(
			recipient_source="Recipient List",
			recipient_list=rl.name,
			channels=[{"channel": CHANNEL_WHATSAPP, "status": STATUS_DRAFT}],
			status=STATUS_DRAFT,
		)

		original_has_perm = frappe.has_permission

		def mock_has_perm(doctype, ptype="read", *args, **kwargs):
			if doctype == "Recipient List":
				doc = kwargs.get("doc") or (args[0] if args else None)
				if doc and (doc == rl.name or getattr(doc, "name", None) == rl.name):
					return False
				return True
			return original_has_perm(doctype, ptype, *args, **kwargs)

		with patch("frappe.has_permission", side_effect=mock_has_perm):
			res = broadcast_api.list_broadcasts(
				filters=json.dumps([["name", "=", bc.name]])
			)
			self.assertEqual(len(res["rows"]), 1)
			row = res["rows"][0]
			self.assertEqual(row["planned_recipient_count"], 0)
			self.assertEqual(row["recipient_count"], 0)
			self.assertEqual(row["channels"][0]["planned_recipient_count"], 0)

	def test_list_broadcasts_failed_filter_includes_partially_failed(self):
		"""list_broadcasts status='Failed' includes both Failed and Partially Failed broadcasts."""
		bc_failed = self._make_broadcast(status=STATUS_FAILED)
		bc_partial = self._make_broadcast(status=STATUS_PARTIALLY_FAILED)
		bc_comp = self._make_broadcast(status=STATUS_COMPLETED)

		# Query status="Failed"
		res_failed = broadcast_api.list_broadcasts(
			status="Failed",
			filters=json.dumps([["name", "in", [bc_failed.name, bc_partial.name, bc_comp.name]]]),
		)
		failed_names = [r["name"] for r in res_failed["rows"]]
		self.assertIn(bc_failed.name, failed_names)
		self.assertIn(bc_partial.name, failed_names)
		self.assertNotIn(bc_comp.name, failed_names)

		# Query status="Partially Failed" directly
		res_partial = broadcast_api.list_broadcasts(
			status="Partially Failed",
			filters=json.dumps([["name", "in", [bc_failed.name, bc_partial.name, bc_comp.name]]]),
		)
		partial_names = [r["name"] for r in res_partial["rows"]]
		self.assertIn(bc_partial.name, partial_names)
		self.assertNotIn(bc_failed.name, partial_names)

	def test_metrics_with_date_range(self):
		"""get_broadcast_metrics respects date_range parameter."""
		bc_recent = self._make_broadcast(status=STATUS_DRAFT)
		bc_old = self._make_broadcast(status=STATUS_DRAFT)
		past_60_days = str(add_days(today(), -60))
		frappe.db.set_value("Broadcast", bc_old.name, "modified", past_60_days, update_modified=False)

		all_metrics = broadcast_api.get_broadcast_metrics()
		filtered_metrics = broadcast_api.get_broadcast_metrics(date_range="7days")
		self.assertGreaterEqual(all_metrics["draft"], filtered_metrics["draft"])
		self.assertGreaterEqual(filtered_metrics["draft"], 1)

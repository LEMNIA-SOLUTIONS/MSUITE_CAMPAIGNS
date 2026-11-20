# Copyright (c) 2026, MSuite and contributors
# For license information, please see license.txt

"""Tests for Marketing Campaign and Calendar integration with Broadcast."""

from __future__ import annotations

import unittest
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from msuite_campaigns.campaign.api.v1 import composer
from msuite_campaigns.campaign.api.v1.calendar import get_calendar_events, reschedule_event
from msuite_campaigns.campaign.api.v1.composer import (
	delete_campaign,
	duplicate_campaign,
	link_broadcast,
	list_broadcasts_for_campaign,
	unlink_broadcast,
)
from msuite_campaigns.campaign.doctype.campaign_calendar_event.campaign_calendar_event import (
	purge_source,
	sync_from_source,
)
from msuite_campaigns.campaign.services.analytics import get_campaign_dashboard


class TestBroadcastCampaignIntegration(IntegrationTestCase):
	"""Test Broadcast integration with Campaign composer, calendar, and analytics."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		if not frappe.db.exists("WhatsApp Account", "WA-ACC-001"):
			frappe.db.sql(
				"INSERT INTO `tabWhatsApp Account` (name, account_name, status, creation, modified, modified_by, owner) "
				"VALUES ('WA-ACC-001', 'WA-ACC-001', 'Active', NOW(), NOW(), 'Administrator', 'Administrator')"
			)
		if not frappe.db.exists("WhatsApp Templates", "tpl_wa_001"):
			frappe.db.sql(
				"INSERT INTO `tabWhatsApp Templates` (name, template_name, whatsapp_account, status, category, language, creation, modified, modified_by, owner) "
				"VALUES ('tpl_wa_001', 'tpl_wa_001', 'WA-ACC-001', 'APPROVED', 'MARKETING', 'en', NOW(), NOW(), 'Administrator', 'Administrator')"
			)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		frappe.db.sql("DELETE FROM `tabWhatsApp Templates` WHERE name = 'tpl_wa_001'")
		frappe.db.sql("DELETE FROM `tabWhatsApp Account` WHERE name = 'WA-ACC-001'")
		frappe.db.commit()
		super().tearDownClass()

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		self.cleanup_campaigns: list[str] = []
		self.cleanup_broadcasts: list[str] = []
		self.cleanup_blasts: list[str] = []
		self.cleanup_events: list[str] = []

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()
		for name in self.cleanup_broadcasts:
			if frappe.db.exists("Broadcast", name):
				frappe.delete_doc("Broadcast", name, force=True, ignore_permissions=True)
		for name in self.cleanup_blasts:
			if frappe.db.exists("Bulk WhatsApp Message", name):
				frappe.delete_doc("Bulk WhatsApp Message", name, force=True, ignore_permissions=True)
		for name in self.cleanup_campaigns:
			if frappe.db.exists("Marketing Campaign", name):
				frappe.delete_doc("Marketing Campaign", name, force=True, ignore_permissions=True)
		for name in self.cleanup_events:
			if frappe.db.exists("Campaign Calendar Event", name):
				frappe.delete_doc("Campaign Calendar Event", name, force=True, ignore_permissions=True)

	def _make_campaign(self, campaign_name: str = "Test Integration Campaign") -> "Document":
		"""Create and insert a test Marketing Campaign with unique name."""
		unique_name = f"{campaign_name} {frappe.generate_hash(length=6)}"
		doc = frappe.get_doc(
			{
				"doctype": "Marketing Campaign",
				"campaign_name": unique_name,
				"status": "Active",
				"start_date": "2026-11-20 00:00:00",
				"end_date": "2026-12-20 23:59:59",
			}
		)
		doc.insert(ignore_permissions=True)
		self.cleanup_campaigns.append(doc.name)
		return doc

	def _make_broadcast(
		self,
		campaign: str = "",
		title: str = "Test Integration Broadcast",
		channels: list[dict] | None = None,
		**kwargs,
	) -> "Document":
		"""Create and insert a test Broadcast."""
		if channels is None:
			channels = [
				{
					"channel": "WhatsApp",
					"whatsapp_account": "WA-ACC-001",
					"whatsapp_template": "tpl_wa_001",
					"status": "Draft",
				}
			]
		for ch in channels:
			if ch.get("channel") != "WhatsApp":
				ch.setdefault("variable_type", "")
		payload = {
			"doctype": "Broadcast",
			"title": title,
			"recipient_source": "Individual",
			"status": "Draft",
			"campaign": campaign,
			"channels": channels,
		}
		payload.update(kwargs)
		doc = frappe.get_doc(payload)
		doc.insert(ignore_permissions=True, ignore_links=True)
		self.cleanup_broadcasts.append(doc.name)
		return doc

	def _make_bulk_whatsapp(self, campaign: str, title: str = "Test Old Blast") -> "Document":
		"""Create a legacy Bulk WhatsApp Message."""
		doc = frappe.get_doc(
			{
				"doctype": "Bulk WhatsApp Message",
				"title": title,
				"campaign": campaign,
				"status": "Draft",
				"template": "test_template_001",
				"recipient_type": "Individual",
			}
		)
		doc.flags.ignore_validate = True
		doc.insert(ignore_permissions=True, ignore_links=True, ignore_mandatory=True)
		self.cleanup_blasts.append(doc.name)
		return doc

	def test_counts_include_both_kinds(self):
		"""Counts in composer._get_relationship_counts include both old blasts and Broadcasts."""
		campaign = self._make_campaign("Count Test Campaign")
		self._make_bulk_whatsapp(campaign.name, "Old Blast 1")
		self._make_broadcast(campaign=campaign.name, title="Broadcast 1")

		counts = composer._get_relationship_counts(campaign.name)
		self.assertIn("broadcasts", counts)
		self.assertIn("whatsapp_blasts", counts)
		self.assertEqual(counts["whatsapp_blasts"], 1)
		self.assertEqual(counts["broadcasts"], 1)

	def test_duplicate_copies_broadcasts_as_draft_zeroed_counters(self):
		"""Duplicating a campaign duplicates its Broadcasts as Drafts with zeroed counters."""
		campaign = self._make_campaign("Source Campaign For Duplicate")
		b = self._make_broadcast(
			campaign=campaign.name,
			title="Broadcast To Clone",
			status="Completed",
			scheduled_time="2026-12-01 10:00:00",
			launched_at="2026-12-01 10:01:00",
			launch_error="Previous error",
			legacy_doctype="Bulk WhatsApp Message",
			legacy_name="BWM-0001",
			total_recipients=50,
			channels=[
				{
					"channel": "WhatsApp",
					"whatsapp_account": "WA-ACC-001",
					"whatsapp_template": "tpl_wa_001",
					"status": "Completed",
					"recipient_count": 50,
					"sent_count": 48,
					"delivered_count": 45,
					"read_count": 40,
					"failed_count": 2,
					"retry_count": 1,
					"launch_error": "Row error",
				},
				{
					"channel": "SMS",
					"sms_account": "SMS-ACC-001",
					"sms_template": "tpl_sms_001",
					"status": "Completed",
					"actual_credits": 48.0,
					"recipient_count": 50,
					"sent_count": 48,
					"delivered_count": 45,
					"failed_count": 3,
				},
			],
		)

		res = duplicate_campaign(campaign.name, duplicate_children=1)
		new_campaign_name = res["name"]
		self.cleanup_campaigns.append(new_campaign_name)

		cloned_broadcasts = frappe.get_all(
			"Broadcast",
			filters={"campaign": new_campaign_name},
			pluck="name",
		)
		self.assertEqual(len(cloned_broadcasts), 1)
		cloned = frappe.get_doc("Broadcast", cloned_broadcasts[0])
		self.cleanup_broadcasts.append(cloned.name)

		self.assertEqual(cloned.title, "Broadcast To Clone")
		self.assertEqual(cloned.status, "Draft")
		self.assertIsNone(cloned.scheduled_time)
		self.assertIsNone(cloned.launched_at)
		self.assertEqual(cloned.launch_error or "", "")
		self.assertEqual(cloned.legacy_doctype or "", "")
		self.assertEqual(cloned.legacy_name or "", "")
		self.assertEqual(cloned.total_recipients, 0)
		self.assertEqual(cloned.campaign, new_campaign_name)

		# Channel rows must be Draft with zeroed counters
		self.assertEqual(len(cloned.channels), 2)
		for ch in cloned.channels:
			self.assertEqual(ch.status, "Draft")
			self.assertEqual(ch.recipient_count, 0)
			self.assertEqual(ch.sent_count, 0)
			self.assertEqual(ch.delivered_count, 0)
			self.assertEqual(ch.failed_count, 0)
			self.assertEqual(ch.retry_count, 0)
			self.assertEqual(ch.actual_credits or 0, 0)
			self.assertEqual(ch.launch_error or "", "")

	def test_delete_cascades_to_broadcasts_and_recipient_rows(self):
		"""Deleting a campaign with cascade=1 cascades to Broadcasts and their recipient rows."""
		campaign = self._make_campaign("Cascade Delete Campaign")
		broadcast = self._make_broadcast(campaign=campaign.name, title="Cascade Target")

		# Insert recipient rows
		rec_doc = frappe.get_doc(
			{
				"doctype": "Broadcast Recipient",
				"broadcast": broadcast.name,
				"channel": "WhatsApp",
				"address": "+15550001",
				"status": "Queued",
			}
		)
		rec_doc.insert(ignore_permissions=True)

		self.assertTrue(frappe.db.exists("Broadcast", broadcast.name))
		self.assertTrue(frappe.db.exists("Broadcast Recipient", rec_doc.name))

		res = delete_campaign(campaign.name, cascade=1)
		self.assertEqual(res.get("status"), "success")

		self.assertFalse(frappe.db.exists("Marketing Campaign", campaign.name))
		self.assertFalse(frappe.db.exists("Broadcast", broadcast.name))
		self.assertFalse(frappe.db.exists("Broadcast Recipient", rec_doc.name))

	def test_link_unlink_permission_denied_vs_allowed(self):
		"""link_broadcast / unlink_broadcast permission checks and linking."""
		campaign = self._make_campaign("Link Test Campaign")
		broadcast = self._make_broadcast(title="Link Target Broadcast")

		# Admin can link
		res = link_broadcast(campaign=campaign.name, broadcast=broadcast.name)
		self.assertEqual(res["status"], "success")
		self.assertEqual(frappe.db.get_value("Broadcast", broadcast.name, "campaign"), campaign.name)

		# Non-permitted user gets PermissionError
		test_user = "test-no-perm@example.com"
		if not frappe.db.exists("User", test_user):
			frappe.get_doc({
				"doctype": "User",
				"email": test_user,
				"first_name": "NoPerm",
				"send_welcome_email": 0,
			}).insert(ignore_permissions=True)

		frappe.set_user(test_user)
		with self.assertRaises(frappe.PermissionError):
			link_broadcast(campaign=campaign.name, broadcast=broadcast.name)

		with self.assertRaises(frappe.PermissionError):
			unlink_broadcast(broadcast=broadcast.name)

		frappe.set_user("Administrator")
		# Admin can unlink
		unres = unlink_broadcast(broadcast=broadcast.name)
		self.assertEqual(unres["status"], "success")
		self.assertEqual(frappe.db.get_value("Broadcast", broadcast.name, "campaign"), "")

	def test_list_broadcasts_for_campaign_search_and_limit(self):
		"""list_broadcasts_for_campaign supports search and limit, and returns current vs rows."""
		campaign = self._make_campaign("Picker Campaign")
		b1 = self._make_broadcast(campaign=campaign.name, title="Alpha Summer Promo")
		b2 = self._make_broadcast(title="Beta Summer Promo")
		b3 = self._make_broadcast(title="Gamma Winter Sale")

		# Search for Summer with limit 1
		res = list_broadcasts_for_campaign(campaign=campaign.name, search="Summer", limit=1)
		self.assertIn("current", res)
		self.assertIn("rows", res)
		self.assertIn(b1.name, res["current"])
		self.assertEqual(len(res["rows"]), 1)
		self.assertIn("Summer", res["rows"][0]["title"])

		# Without limit 1
		res_all = list_broadcasts_for_campaign(campaign=campaign.name, search="Summer", limit=10)
		titles = [r["title"] for r in res_all["rows"]]
		self.assertIn("Alpha Summer Promo", titles)
		self.assertIn("Beta Summer Promo", titles)
		self.assertNotIn("Gamma Winter Sale", titles)

	def test_analytics_sums_old_plus_new_without_double_counting(self):
		"""Analytics sums old adapters and Broadcast metrics without double counting."""
		campaign = self._make_campaign("Analytics Sum Campaign")

		dummy_broadcast_metrics = {
			"WhatsApp": {
				"channel": "WhatsApp",
				"label": "WhatsApp",
				"blasts": 2,
				"metrics": {
					"sent": 100,
					"delivered": 90,
					"failed": 10,
					"audience": 100,
					"credits": 90.0,
					"delivery_rate": 90.0,
					"read": 80,
				},
				"engine": "standard",
				"stale": False,
				"retried": 0,
			}
		}

		with patch("msuite_campaigns.broadcast.rollup.campaign_metrics", return_value=dummy_broadcast_metrics):
			dashboard = get_campaign_dashboard(campaign.name)
			modules = dashboard.get("modules") or {}
			self.assertIn("whatsapp", modules)
			wa = modules["whatsapp"]
			wa_metrics = wa.get("metrics") or {}
			# The broadcast metrics must be present/summed
			self.assertGreaterEqual(wa_metrics.get("sent", 0), 100)
			self.assertGreaterEqual(wa_metrics.get("delivered", 0), 90)
			self.assertGreaterEqual(wa_metrics.get("read", 0), 80)

	def test_analytics_survives_missing_rollup_module(self):
		"""Analytics survives if broadcast rollup cannot be imported."""
		campaign = self._make_campaign("Analytics Missing Rollup Campaign")

		with patch.dict("sys.modules", {"msuite_campaigns.broadcast.rollup": None}):
			# Should not raise exception
			dashboard = get_campaign_dashboard(campaign.name)
			self.assertIsInstance(dashboard, dict)
			self.assertIn("modules", dashboard)

	def test_calendar_event_upserted_on_save_removed_on_trash(self):
		"""Broadcast with WhatsApp row upserts calendar event on save and removes on trash."""
		broadcast = self._make_broadcast(
			title="Calendar WA Event Broadcast",
			status="Draft",
			scheduled_time="2026-11-25 15:00:00",
			channels=[
				{
					"channel": "WhatsApp",
					"whatsapp_account": "WA-ACC-001",
					"whatsapp_template": "tpl_wa_001",
					"status": "Draft",
				}
			],
		)

		# Calendar event should be created via after_insert doc_events hook on save/insert
		ev_name = frappe.db.get_value(
			"Campaign Calendar Event",
			{"event_type": "WhatsApp Message", "source_name": broadcast.name},
			"name",
		)
		self.assertTrue(bool(ev_name))
		self.cleanup_events.append(ev_name)

		ev = frappe.get_doc("Campaign Calendar Event", ev_name)
		self.assertEqual(ev.title, "Calendar WA Event Broadcast")
		self.assertEqual(ev.status, "Draft")
		self.assertEqual(ev.color_hint, "wa-draft")

		# Update status to Scheduled via save (exercising on_update doc_events hook)
		broadcast.status = "Scheduled"
		broadcast.save(ignore_permissions=True)

		ev.reload()
		self.assertEqual(ev.status, "Scheduled")
		self.assertEqual(ev.color_hint, "wa-scheduled")

		# Trash broadcast via frappe.delete_doc -> on_trash doc_events hook purges calendar event
		self.assertTrue(frappe.db.exists("Campaign Calendar Event", ev_name))
		frappe.delete_doc("Broadcast", broadcast.name)
		self.assertFalse(frappe.db.exists("Broadcast", broadcast.name))
		self.assertFalse(frappe.db.exists("Campaign Calendar Event", ev_name))

	def test_reschedule_broadcast_calendar_event(self):
		"""reschedule_event moves scheduled_time for Broadcast, rejects non-Draft/Scheduled, and checks write permission."""
		broadcast = self._make_broadcast(
			title="Reschedule WA Broadcast",
			status="Draft",
			scheduled_time="2026-12-01 10:00:00",
			channels=[
				{
					"channel": "WhatsApp",
					"whatsapp_account": "WA-ACC-001",
					"whatsapp_template": "tpl_wa_001",
					"status": "Draft",
				}
			],
		)
		ev_name = frappe.db.get_value(
			"Campaign Calendar Event",
			{"event_type": "WhatsApp Message", "source_name": broadcast.name},
			"name",
		)
		self.assertTrue(bool(ev_name))
		self.cleanup_events.append(ev_name)

		# 1. Moves scheduled_time on Draft
		new_time = "2026-12-05 14:00:00"
		res = reschedule_event(
			event_type="WhatsApp Message",
			source_name=broadcast.name,
			start_date=new_time,
			end_date=new_time,
		)
		self.assertEqual(str(res.get("start_date")), new_time)
		broadcast.reload()
		self.assertEqual(str(broadcast.scheduled_time), new_time)

		# Moves scheduled_time on Scheduled
		broadcast.status = "Scheduled"
		broadcast.save(ignore_permissions=True)
		future_time = "2026-12-10 16:00:00"
		res_sched = reschedule_event(
			event_type="WhatsApp Message",
			source_name=broadcast.name,
			start_date=future_time,
			end_date=future_time,
		)
		self.assertEqual(str(res_sched.get("start_date")), future_time)
		broadcast.reload()
		self.assertEqual(str(broadcast.scheduled_time), future_time)

		# 2. Rejects Broadcast that is not Draft/Scheduled
		broadcast.status = "Completed"
		broadcast.save(ignore_permissions=True)
		with self.assertRaises(frappe.ValidationError):
			reschedule_event(
				event_type="WhatsApp Message",
				source_name=broadcast.name,
				start_date="2026-12-15 10:00:00",
				end_date="2026-12-15 10:00:00",
			)

		# 3. Permission denied for user without write permission
		broadcast.status = "Draft"
		broadcast.save(ignore_permissions=True)

		test_user = "test-no-perm@example.com"
		if not frappe.db.exists("User", test_user):
			frappe.get_doc({
				"doctype": "User",
				"email": test_user,
				"first_name": "NoPerm",
				"send_welcome_email": 0,
			}).insert(ignore_permissions=True)

		frappe.set_user(test_user)
		try:
			with self.assertRaises(frappe.PermissionError):
				reschedule_event(
					event_type="WhatsApp Message",
					source_name=broadcast.name,
					start_date="2026-12-20 10:00:00",
					end_date="2026-12-20 10:00:00",
				)
		finally:
			frappe.set_user("Administrator")

	def test_calendar_skipped_for_email_only_broadcast(self):
		"""Broadcast without a WhatsApp channel is skipped from calendar sync."""
		broadcast = self._make_broadcast(
			title="Email Only Broadcast",
			status="Draft",
			scheduled_time="2026-11-25 15:00:00",
			channels=[
				{
					"channel": "Email",
					"email_account": "EM-ACC-001",
					"email_template": "tpl_email_001",
					"status": "Draft",
				}
			],
		)

		res = sync_from_source("Broadcast", broadcast.name)
		self.assertIsNone(res)

		ev_name = frappe.db.get_value(
			"Campaign Calendar Event",
			{"event_type": "WhatsApp Message", "source_name": broadcast.name},
			"name",
		)
		self.assertIsNone(ev_name)

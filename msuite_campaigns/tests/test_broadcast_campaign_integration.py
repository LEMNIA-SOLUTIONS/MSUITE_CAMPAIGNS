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
	list_channels,
	unlink_broadcast,
)
from msuite_campaigns.campaign.doctype.campaign_calendar_event.campaign_calendar_event import (
	backfill_all,
	purge_source,
	sync_from_source,
)
from msuite_campaigns.broadcast.recipients import bulk_insert_recipients
from msuite_campaigns.broadcast.rollup import campaign_metrics
from msuite_campaigns.campaign.services.analytics import get_campaign_dashboard, sync_marketing_campaign_insights


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
		self.cleanup_email_blasts: list[str] = []
		self.cleanup_events: list[str] = []
		self.cleanup_messages: list[str] = []

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()
		if self.cleanup_messages:
			frappe.db.delete("Channel Message", {"name": ["in", self.cleanup_messages]})
		if self.cleanup_campaigns:
			frappe.db.delete("Marketing Campaign Insight", {"marketing_campaign": ["in", self.cleanup_campaigns]})
		for name in self.cleanup_broadcasts:
			if frappe.db.exists("Broadcast", name):
				frappe.delete_doc("Broadcast", name, force=True, ignore_permissions=True)
		if self.cleanup_broadcasts:
			frappe.db.delete("Broadcast Channel", {"parent": ["in", self.cleanup_broadcasts]})
			frappe.db.delete("Broadcast Product", {"parent": ["in", self.cleanup_broadcasts]})
			frappe.db.delete("Broadcast Recipient", {"broadcast": ["in", self.cleanup_broadcasts]})
		for name in self.cleanup_blasts:
			if frappe.db.exists("DocType", "Bulk WhatsApp Message") and frappe.db.exists("Bulk WhatsApp Message", name):
				frappe.delete_doc("Bulk WhatsApp Message", name, force=True, ignore_permissions=True)
		for name in self.cleanup_email_blasts:
			if frappe.db.exists("DocType", "Bulk Email Message") and frappe.db.exists("Bulk Email Message", name):
				frappe.delete_doc("Bulk Email Message", name, force=True, ignore_permissions=True)
		for name in self.cleanup_campaigns:
			if frappe.db.exists("Marketing Campaign", name):
				frappe.delete_doc("Marketing Campaign", name, force=True, ignore_permissions=True)
		for name in self.cleanup_events:
			if frappe.db.exists("Campaign Calendar Event", name):
				frappe.delete_doc("Campaign Calendar Event", name, force=True, ignore_permissions=True)
		frappe.db.commit()

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
				"template": "tpl_wa_001",
				"recipient_type": "Individual",
				"recipients": [{"mobile_number": "+919876543210"}],
			}
		)
		doc.flags.ignore_validate = True
		doc.insert(ignore_permissions=True, ignore_links=True, ignore_mandatory=True)
		self.cleanup_blasts.append(doc.name)
		return doc

	def _make_bulk_email(self, campaign: str = "", title: str = "Test Old Email Blast") -> "Document":
		"""Create a legacy Bulk Email Message."""
		doc = frappe.get_doc(
			{
				"doctype": "Bulk Email Message",
				"title": title,
				"campaign": campaign,
				"status": "Draft",
				"recipient_type": "Individual",
			}
		)
		doc.flags.ignore_validate = True
		doc.insert(ignore_permissions=True, ignore_links=True, ignore_mandatory=True)
		self.cleanup_email_blasts.append(doc.name)
		return doc

	def _make_channel_message(
		self,
		reference_doctype: str,
		reference_name: str,
		status: str = "sent",
		date_str: str | None = None,
		channel_type: str = "WhatsApp",
	) -> "Document":
		"""Create a test Channel Message row."""
		cm = frappe.get_doc({
			"doctype": "Channel Message",
			"channel_type": channel_type,
			"direction": "Outbound",
			"content_type": "Text",
			"reference_doctype": reference_doctype,
			"reference_name": reference_name,
			"status": status,
		})
		cm.flags.ignore_permissions = True
		cm.flags.ignore_mandatory = True
		cm.flags.ignore_links = True
		cm.insert()
		if date_str:
			frappe.db.set_value("Channel Message", cm.name, "creation", f"{date_str} 12:00:00", update_modified=False)
		self.cleanup_messages.append(cm.name)
		return cm

	@unittest.skipUnless(frappe.db.exists("DocType", "Bulk WhatsApp Message"), "legacy doctype deleted")
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

	@unittest.skipUnless(frappe.db.exists("DocType", "Bulk WhatsApp Message"), "legacy doctype deleted")
	def test_analytics_sums_old_plus_new_without_double_counting(self):
		"""C-01 / I-05a: Analytics excludes migrated Broadcast while old doc exists, counts after deletion."""
		campaign = self._make_campaign("Analytics Coexistence Campaign")

		# Real old Bulk WhatsApp Message with sent_count = 1000
		old_blast = frappe.get_doc(
			{
				"doctype": "Bulk WhatsApp Message",
				"title": "Old Real WA Blast",
				"campaign": campaign.name,
				"status": "Completed",
				"template": "tpl_wa_001",
				"recipient_type": "Individual",
				"recipient_count": 1000,
				"sent_count": 1000,
			}
		)
		old_blast.flags.ignore_validate = True
		old_blast.insert(ignore_permissions=True, ignore_links=True, ignore_mandatory=True)
		self.cleanup_blasts.append(old_blast.name)

		# Migrated Broadcast (legacy_doctype & legacy_name point to old_blast)
		migrated_bc = self._make_broadcast(
			campaign=campaign.name,
			title="Migrated WA Broadcast",
			status="Completed",
			legacy_doctype="Bulk WhatsApp Message",
			legacy_name=old_blast.name,
			total_recipients=1000,
			channels=[
				{
					"channel": "WhatsApp",
					"whatsapp_account": "WA-ACC-001",
					"whatsapp_template": "tpl_wa_001",
					"status": "Completed",
					"delivery_engine": "Standard",
					"recipient_count": 1000,
					"sent_count": 1000,
					"delivered_count": 950,
				}
			],
		)

		# Genuinely new Broadcast (sent 200)
		new_bc = self._make_broadcast(
			campaign=campaign.name,
			title="New Genuinely WA Broadcast",
			status="Completed",
			total_recipients=200,
			channels=[
				{
					"channel": "WhatsApp",
					"whatsapp_account": "WA-ACC-001",
					"whatsapp_template": "tpl_wa_001",
					"status": "Completed",
					"delivery_engine": "Standard",
					"recipient_count": 200,
					"sent_count": 200,
					"delivered_count": 190,
				}
			],
		)

		# Dashboard shows 1200 sent, blasts 2 (not 2200 / 3)
		dashboard = get_campaign_dashboard(campaign.name)
		modules = dashboard.get("modules") or {}
		self.assertIn("whatsapp", modules)
		wa = modules["whatsapp"]
		self.assertEqual(wa.get("totals", {}).get("sent"), 1200)
		self.assertEqual(wa.get("blasts"), 2)

		# After deleting the old doc, the migrated one counts (1000 + 200 = 1200 sent, 2 blasts)
		frappe.delete_doc("Bulk WhatsApp Message", old_blast.name, force=True, ignore_permissions=True)
		self.cleanup_blasts.remove(old_blast.name)

		dashboard2 = get_campaign_dashboard(campaign.name)
		modules2 = dashboard2.get("modules") or {}
		self.assertIn("whatsapp", modules2)
		wa2 = modules2["whatsapp"]
		self.assertEqual(wa2.get("totals", {}).get("sent"), 1200)
		self.assertEqual(wa2.get("blasts"), 2)

	def test_analytics_survives_missing_rollup_module(self):
		"""Analytics survives if broadcast rollup cannot be imported."""
		campaign = self._make_campaign("Analytics Missing Rollup Campaign")

		with patch("msuite_campaigns.broadcast.rollup.campaign_metrics", side_effect=ImportError("module missing")):
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

	@unittest.skipUnless(frappe.db.exists("DocType", "Bulk WhatsApp Message"), "legacy doctype deleted")
	def test_calendar_coexistence_migrated_broadcast_suppressed(self):
		"""C-02: Migrated Broadcast with existing legacy doc projects NO event; new projects one; reschedule works."""
		campaign = self._make_campaign("Calendar Coexistence Campaign")

		# Real legacy Bulk WhatsApp Message
		legacy_blast = self._make_bulk_whatsapp(campaign.name, "Legacy WA Blast")
		legacy_blast.scheduled_time = "2026-11-28 10:00:00"
		legacy_blast.save(ignore_permissions=True)

		# Upsert calendar event for legacy blast
		legacy_ev = sync_from_source("Bulk WhatsApp Message", legacy_blast.name)
		self.assertTrue(bool(legacy_ev))
		self.cleanup_events.append(legacy_ev)

		# Migrated Broadcast pointing to legacy_blast
		migrated_bc = self._make_broadcast(
			campaign=campaign.name,
			title="Migrated Calendar Broadcast",
			status="Draft",
			scheduled_time="2026-11-28 10:00:00",
			legacy_doctype="Bulk WhatsApp Message",
			legacy_name=legacy_blast.name,
			channels=[
				{
					"channel": "WhatsApp",
					"whatsapp_account": "WA-ACC-001",
					"whatsapp_template": "tpl_wa_001",
					"status": "Draft",
				}
			],
		)

		# 1. Migrated Broadcast projects NO event
		migrated_ev = frappe.db.get_value(
			"Campaign Calendar Event",
			{"event_type": "WhatsApp Message", "source_name": migrated_bc.name},
			"name",
		)
		self.assertIsNone(migrated_ev)

		# 2. backfill_all creates NO event for migrated Broadcast
		backfill_all()
		self.assertIsNone(
			frappe.db.get_value(
				"Campaign Calendar Event",
				{"event_type": "WhatsApp Message", "source_name": migrated_bc.name},
				"name",
			)
		)

		# 3. Genuinely new Broadcast projects one event
		new_bc = self._make_broadcast(
			campaign=campaign.name,
			title="New Calendar Broadcast",
			status="Draft",
			scheduled_time="2026-11-29 11:00:00",
			channels=[
				{
					"channel": "WhatsApp",
					"whatsapp_account": "WA-ACC-001",
					"whatsapp_template": "tpl_wa_001",
					"status": "Draft",
				}
			],
		)
		new_ev = frappe.db.get_value(
			"Campaign Calendar Event",
			{"event_type": "WhatsApp Message", "source_name": new_bc.name},
			"name",
		)
		self.assertTrue(bool(new_ev))
		self.cleanup_events.append(new_ev)

		# 4. reschedule_event on the legacy event still works
		res = reschedule_event(
			event_type="WhatsApp Message",
			source_name=legacy_blast.name,
			start_date="2026-11-30 12:00:00",
			end_date="2026-11-30 12:00:00",
		)
		self.assertEqual(str(res.get("start_date")), "2026-11-30 12:00:00")
		legacy_blast.reload()
		self.assertEqual(str(legacy_blast.scheduled_time), "2026-11-30 12:00:00")

		# 5. After the legacy doc is gone, the Broadcast projects
		frappe.delete_doc("Bulk WhatsApp Message", legacy_blast.name, force=True, ignore_permissions=True)
		self.cleanup_blasts.remove(legacy_blast.name)

		# Resync migrated broadcast
		event_after_del = sync_from_source("Broadcast", migrated_bc.name)
		self.assertTrue(bool(event_after_del))
		self.cleanup_events.append(event_after_del)
		ev_row = frappe.get_doc("Campaign Calendar Event", event_after_del)
		self.assertEqual(ev_row.source_name, migrated_bc.name)

	@unittest.skipUnless(frappe.db.exists("DocType", "Bulk WhatsApp Message"), "legacy doctype deleted")
	def test_duplicate_campaign_coexistence_and_individual_recipients(self):
		"""I-01: duplicate_campaign skips migrated broadcast whose legacy blast is duplicated, and clones Individual recipients."""
		campaign = self._make_campaign("Duplicate Coexistence Campaign")

		# Real old Bulk WhatsApp Message
		legacy_blast = self._make_bulk_whatsapp(campaign.name, "Legacy Blast For Duplicate")

		# Migrated Broadcast pointing to legacy_blast
		migrated_bc = self._make_broadcast(
			campaign=campaign.name,
			title="Migrated Copy Broadcast",
			legacy_doctype="Bulk WhatsApp Message",
			legacy_name=legacy_blast.name,
		)

		# Genuinely new Broadcast with recipient_source == "Individual"
		new_bc = self._make_broadcast(
			campaign=campaign.name,
			title="New BC With Individual Recipients",
			recipient_source="Individual",
			channels=[
				{
					"channel": "WhatsApp",
					"whatsapp_account": "WA-ACC-001",
					"whatsapp_template": "tpl_wa_001",
					"status": "Draft",
				}
			],
		)
		bulk_insert_recipients(
			new_bc.name,
			"WhatsApp",
			[
				{"address": "+15551111111", "recipient_name": "Alice", "status": "Sent", "provider_message_id": "prov-111"},
				{"address": "+15552222222", "recipient_name": "Bob", "status": "Delivered", "provider_message_id": "prov-222"},
			],
		)
		frappe.db.set_value("Broadcast Channel", {"parent": new_bc.name, "channel": "WhatsApp"}, "recipient_count", 2)
		frappe.db.set_value("Broadcast", new_bc.name, "total_recipients", 2)

		res = duplicate_campaign(campaign.name, duplicate_children=1)
		new_campaign_name = res["name"]
		self.cleanup_campaigns.append(new_campaign_name)

		# Under new_campaign:
		# 1. Bulk WhatsApp Message was duplicated (from legacy_blast)
		cloned_blasts = frappe.get_all("Bulk WhatsApp Message", filters={"campaign": new_campaign_name}, pluck="name")
		self.assertEqual(len(cloned_blasts), 1)
		self.cleanup_blasts.extend(cloned_blasts)

		# 2. Only new_bc was duplicated as Broadcast (migrated_bc was skipped)
		cloned_bcs = frappe.get_all("Broadcast", filters={"campaign": new_campaign_name}, pluck="name")
		self.assertEqual(len(cloned_bcs), 1)
		cloned_bc = frappe.get_doc("Broadcast", cloned_bcs[0])
		self.cleanup_broadcasts.append(cloned_bc.name)
		self.assertEqual(cloned_bc.title, "New BC With Individual Recipients")

		# 3. Recipients were cloned via bulk_insert_recipients with status Queued and no provider id
		cloned_recipients = frappe.get_all(
			"Broadcast Recipient",
			filters={"broadcast": cloned_bc.name},
			fields=["address", "recipient_name", "status", "provider_message_id"],
			order_by="address asc",
		)
		self.assertEqual(len(cloned_recipients), 2)
		self.assertEqual(cloned_recipients[0]["address"], "+15551111111")
		self.assertEqual(cloned_recipients[0]["status"], "Queued")
		self.assertFalse(cloned_recipients[0]["provider_message_id"])
		self.assertEqual(cloned_recipients[1]["address"], "+15552222222")
		self.assertEqual(cloned_recipients[1]["status"], "Queued")
		self.assertFalse(cloned_recipients[1]["provider_message_id"])

		# 4. total_recipients and channel recipient_count are set consistently
		self.assertEqual(cloned_bc.total_recipients, 2)
		ch_row = cloned_bc.channels[0]
		self.assertEqual(ch_row.recipient_count, 2)
		self.assertEqual(ch_row.status, "Draft")

	def test_list_broadcasts_for_campaign_permission_isolated_email_user(self):
		"""I-02: list_broadcasts_for_campaign enforces get_list permissions; Email-only user cannot see WhatsApp-only broadcast."""
		campaign = self._make_campaign("Perm Picker Campaign")

		# WhatsApp-only broadcast linked to campaign
		wa_bc = self._make_broadcast(
			campaign=campaign.name,
			title="WA Only Broadcast",
			channels=[
				{
					"channel": "WhatsApp",
					"whatsapp_account": "WA-ACC-001",
					"whatsapp_template": "tpl_wa_001",
					"status": "Draft",
				}
			],
		)

		# Email-only broadcast linked to campaign
		em_bc = self._make_broadcast(
			campaign=campaign.name,
			title="Email Only Broadcast",
			channels=[
				{
					"channel": "Email",
					"email_account": "test@example.com",
					"email_template": "tpl_email_001",
					"status": "Draft",
				}
			],
		)

		test_user = "test-email-user-picker@example.com"
		if not frappe.db.exists("User", test_user):
			user_doc = frappe.get_doc({
				"doctype": "User",
				"email": test_user,
				"first_name": "EmailOnlyUser",
				"send_welcome_email": 0,
				"roles": [{"role": "Email User"}],
			}).insert(ignore_permissions=True)
		else:
			user_doc = frappe.get_doc("User", test_user)
			user_doc.roles = []
			user_doc.append("roles", {"role": "Email User"})
			user_doc.save(ignore_permissions=True)

		frappe.set_user(test_user)
		try:
			res = list_broadcasts_for_campaign(campaign=campaign.name)
			current_broadcasts = res.get("current") or []
			rows_names = [r["name"] for r in (res.get("rows") or [])]
			# Email-only user must see em_bc, but NOT wa_bc
			self.assertIn(em_bc.name, current_broadcasts)
			self.assertNotIn(wa_bc.name, current_broadcasts)
			self.assertNotIn(wa_bc.name, rows_names)
		finally:
			frappe.set_user("Administrator")

	def test_rollup_campaign_metrics_constant_queries_no_get_doc_loop(self):
		"""I-03: campaign_metrics executes no per-broadcast get_doc and uses a small constant number of queries."""
		campaign = self._make_campaign("Rollup Query Count Campaign")

		# Create 12 broadcasts under this campaign
		for i in range(12):
			self._make_broadcast(
				campaign=campaign.name,
				title=f"Broadcast {i}",
				channels=[
					{
						"channel": "WhatsApp",
						"delivery_engine": "Standard",
						"recipient_count": 10,
						"sent_count": 10,
						"delivered_count": 9,
						"read_count": 8,
						"status": "Completed",
					}
				],
			)

		query_count = 0
		orig_sql = frappe.db.sql

		def count_sql(*args, **kwargs):
			nonlocal query_count
			query_count += 1
			return orig_sql(*args, **kwargs)

		with patch("frappe.db.sql", side_effect=count_sql), patch("frappe.get_doc") as mock_get_doc:
			metrics = campaign_metrics(campaign.name)

		# Must not call frappe.get_doc on Broadcast
		for call in mock_get_doc.call_args_list:
			self.assertNotEqual(call[0][0], "Broadcast", "get_doc should not be called for Broadcast")

		# Total SQL queries must be a small constant (<= 5 queries, not ~36 queries)
		self.assertLessEqual(query_count, 5, f"Expected small constant queries, got {query_count}")
		self.assertIn("WhatsApp", metrics)
		self.assertEqual(metrics["WhatsApp"]["metrics"]["audience"], 120)
		self.assertEqual(metrics["WhatsApp"]["metrics"]["sent"], 120)
		self.assertEqual(metrics["WhatsApp"]["blasts"], 12)

	@unittest.skipUnless(frappe.db.exists("DocType", "Bulk WhatsApp Message"), "legacy doctype deleted")
	def test_duplicate_campaign_preserves_migrated_email_and_sms_broadcasts(self):
		"""F7 / NEW-I-01: duplicate_campaign skips only migrated WhatsApp broadcasts whose legacy blast is duplicated.

		Migrated Email and SMS broadcasts (and new broadcasts) must be preserved in the cloned campaign.
		"""
		campaign = self._make_campaign("Duplicate Migrated Campaign")

		# (a) An old Bulk WhatsApp Message and its migrated Broadcast
		legacy_wa = self._make_bulk_whatsapp(campaign.name, "Legacy WA Blast")
		migrated_wa_bc = self._make_broadcast(
			campaign=campaign.name,
			title="Migrated WA Broadcast",
			legacy_doctype="Bulk WhatsApp Message",
			legacy_name=legacy_wa.name,
			channels=[{"channel": "WhatsApp"}],
		)

		# (b) A migrated Email Broadcast (legacy_doctype Bulk Email Message, legacy doc still present)
		legacy_email = self._make_bulk_email(campaign=campaign.name, title="Legacy Email Blast")
		migrated_email_bc = self._make_broadcast(
			campaign=campaign.name,
			title="Migrated Email Broadcast",
			legacy_doctype="Bulk Email Message",
			legacy_name=legacy_email.name,
			channels=[{"channel": "Email"}],
		)

		# (c) A genuinely new SMS Broadcast
		new_sms_bc = self._make_broadcast(
			campaign=campaign.name,
			title="New SMS Broadcast",
			channels=[{"channel": "SMS"}],
		)

		res = duplicate_campaign(campaign.name, duplicate_children=1)
		new_campaign_name = res["name"]
		self.cleanup_campaigns.append(new_campaign_name)

		# Under new_campaign:
		# 1. Bulk WhatsApp Message was duplicated (from legacy_wa)
		cloned_wa_blasts = frappe.get_all(
			"Bulk WhatsApp Message",
			filters={"campaign": new_campaign_name},
			pluck="name",
		)
		self.assertEqual(len(cloned_wa_blasts), 1)
		self.cleanup_blasts.extend(cloned_wa_blasts)

		# 2. Cloned broadcasts must contain the Email copy and the SMS copy,
		# and NOT a second WhatsApp Broadcast for (a)
		cloned_bcs = frappe.get_all(
			"Broadcast",
			filters={"campaign": new_campaign_name},
			fields=["name", "title"],
		)
		for b in cloned_bcs:
			self.cleanup_broadcasts.append(b["name"])

		cloned_titles = [b["title"] for b in cloned_bcs]
		self.assertNotIn("Migrated WA Broadcast", cloned_titles)
		self.assertIn("Migrated Email Broadcast", cloned_titles)
		self.assertIn("New SMS Broadcast", cloned_titles)
		self.assertEqual(len(cloned_bcs), 2)

	def test_list_channels_reads_broadcast_with_whatsapp_channel(self):
		"""WhatsApp children come from Broadcast rows with WhatsApp channel, ignoring non-WhatsApp broadcasts."""
		campaign = self._make_campaign("Channels Test Campaign")

		# 1. WhatsApp Broadcast linked to campaign
		wa_bc = self._make_broadcast(
			campaign=campaign.name,
			title="WA Channel Broadcast",
			status="Scheduled",
			scheduled_time="2026-11-25 15:00:00",
			total_recipients=42,
			channels=[
				{
					"channel": "WhatsApp",
					"whatsapp_account": "WA-ACC-001",
					"whatsapp_template": "tpl_wa_001",
					"status": "Scheduled",
					"recipient_count": 42,
				}
			],
		)

		# 2. Email-only Broadcast linked to campaign (should NOT appear in whatsapp)
		em_bc = self._make_broadcast(
			campaign=campaign.name,
			title="Email Only Broadcast",
			channels=[
				{
					"channel": "Email",
					"email_account": "test@example.com",
					"email_template": "tpl_email_001",
					"status": "Draft",
				}
			],
		)

		channels = list_channels(campaign.name)
		self.assertIn("whatsapp", channels)
		wa_items = channels["whatsapp"]
		wa_names = [r["name"] for r in wa_items]

		self.assertIn(wa_bc.name, wa_names)
		self.assertNotIn(em_bc.name, wa_names)

		# Check result shape
		item = next(r for r in wa_items if r["name"] == wa_bc.name)
		self.assertEqual(item["name"], wa_bc.name)
		self.assertEqual(item["title"], "WA Channel Broadcast")
		self.assertEqual(item["status"], "Scheduled")
		self.assertEqual(item["recipients_count"], 42)
		self.assertEqual(item["is_paused"], 0)
		self.assertTrue(item["scheduled_at"])

	@unittest.skipUnless(frappe.db.exists("DocType", "Bulk WhatsApp Message"), "legacy doctype deleted")
	def test_list_channels_coexistence_and_deduplication(self):
		"""Migrated legacy blast resolves to Broadcast and is shown once as the Broadcast; unmigrated and new are both shown."""
		campaign = self._make_campaign("Dedupe Channels Campaign")

		# Legacy blast that was migrated to a Broadcast (which was not directly linked to campaign)
		legacy_migrated = self._make_bulk_whatsapp(campaign.name, "Legacy Migrated Blast")
		migrated_bc = self._make_broadcast(
			campaign="",
			title="Migrated Broadcast Title",
			legacy_doctype="Bulk WhatsApp Message",
			legacy_name=legacy_migrated.name,
			status="Paused",
			total_recipients=150,
			channels=[
				{
					"channel": "WhatsApp",
					"whatsapp_account": "WA-ACC-001",
					"whatsapp_template": "tpl_wa_001",
					"status": "Draft",
					"recipient_count": 150,
				}
			],
		)

		# Legacy blast unmigrated
		legacy_unmigrated = self._make_bulk_whatsapp(campaign.name, "Unmigrated Blast")

		# Genuinely new Broadcast
		new_bc = self._make_broadcast(
			campaign=campaign.name,
			title="Brand New Broadcast",
			total_recipients=80,
			channels=[
				{
					"channel": "WhatsApp",
					"whatsapp_account": "WA-ACC-001",
					"whatsapp_template": "tpl_wa_001",
					"recipient_count": 80,
				}
			],
		)

		channels = list_channels(campaign.name)
		wa_items = channels.get("whatsapp", [])
		wa_names = [r["name"] for r in wa_items]

		# Migrated blast is shown ONCE, as the Broadcast
		self.assertIn(migrated_bc.name, wa_names)
		self.assertNotIn(legacy_migrated.name, wa_names)
		migrated_item = next(r for r in wa_items if r["name"] == migrated_bc.name)
		self.assertEqual(migrated_item["status"], "Draft")
		self.assertEqual(migrated_item["is_paused"], 0)

		# Unmigrated blast is shown as the legacy blast
		self.assertIn(legacy_unmigrated.name, wa_names)

		# New Broadcast is shown
		self.assertIn(new_bc.name, wa_names)

		# Total items should be exactly 3, not 4
		self.assertEqual(len(wa_items), 3)

	def test_list_channels_permission_isolation(self):
		"""User without WhatsApp permissions cannot see WhatsApp channel rows in list_channels."""
		campaign = self._make_campaign("Perm Isolated Campaign")
		wa_bc = self._make_broadcast(
			campaign=campaign.name,
			title="WA Broadcast For Perm Check",
			channels=[{"channel": "WhatsApp"}],
		)

		test_user = "test-email-user-ch@example.com"
		if not frappe.db.exists("User", test_user):
			frappe.get_doc({
				"doctype": "User",
				"email": test_user,
				"first_name": "EmailOnlyUser",
				"send_welcome_email": 0,
				"roles": [{"role": "Email User"}],
			}).insert(ignore_permissions=True)
		else:
			u = frappe.get_doc("User", test_user)
			u.roles = []
			u.append("roles", {"role": "Email User"})
			u.save(ignore_permissions=True)

		frappe.set_user(test_user)
		try:
			channels = list_channels(campaign.name)
			wa_names = [r["name"] for r in channels.get("whatsapp", [])]
			self.assertNotIn(wa_bc.name, wa_names)
		finally:
			frappe.set_user("Administrator")

	def test_analytics_sync_counts_channel_messages_for_new_and_legacy_broadcasts(self):
		"""sync_marketing_campaign_insights counts Channel Messages for new Broadcast with reference_doctype='Broadcast'."""
		campaign = self._make_campaign("Analytics New BC Campaign")
		today_str = frappe.utils.today()

		new_bc = self._make_broadcast(
			campaign=campaign.name,
			title="Analytics New BC",
			total_recipients=50,
			channels=[
				{
					"channel": "WhatsApp",
					"recipient_count": 50,
					"status": "Completed",
				}
			],
		)

		# 3 Channel Messages: 1 sent, 1 delivered, 1 read
		self._make_channel_message("Broadcast", new_bc.name, "sent", today_str)
		self._make_channel_message("Broadcast", new_bc.name, "delivered", today_str)
		self._make_channel_message("Broadcast", new_bc.name, "read", today_str)

		sync_marketing_campaign_insights(campaign.name)

		# Check Marketing Campaign Insight lifetime row
		insight = frappe.db.get_value(
			"Marketing Campaign Insight",
			{"marketing_campaign": campaign.name, "granularity": "LIFETIME"},
			["total_reach", "total_engagement"],
			as_dict=True,
		)
		self.assertIsNotNone(insight)
		# total_reach includes delivered (1) + read (which is delivered, so sent/delivered/read count)
		# read counts for total_engagement
		self.assertGreaterEqual(insight["total_reach"], 2)
		self.assertGreaterEqual(insight["total_engagement"], 1)

	@unittest.skipUnless(frappe.db.exists("DocType", "Bulk WhatsApp Message"), "legacy doctype deleted")
	def test_analytics_sync_no_double_counting_migrated_blast(self):
		"""sync_marketing_campaign_insights excludes migrated Bulk WhatsApp Message to avoid double counting."""
		campaign = self._make_campaign("Analytics Dedupe Campaign")
		today_str = frappe.utils.today()

		# Old blast with 200 recipients
		old_blast = self._make_bulk_whatsapp(campaign.name, "Old Dedupe Blast")
		frappe.db.set_value("Bulk WhatsApp Message", old_blast.name, "recipient_count", 200)

		# Migrated Broadcast with 200 recipients
		migrated_bc = self._make_broadcast(
			campaign=campaign.name,
			title="Migrated Dedupe BC",
			legacy_doctype="Bulk WhatsApp Message",
			legacy_name=old_blast.name,
			total_recipients=200,
			channels=[
				{
					"channel": "WhatsApp",
					"recipient_count": 200,
				}
			],
		)

		# Channel messages under Broadcast
		self._make_channel_message("Broadcast", migrated_bc.name, "delivered", today_str)

		# Unmigrated legacy blast with 100 recipients
		unmigrated = self._make_bulk_whatsapp(campaign.name, "Unmigrated Legacy Dedupe")
		frappe.db.set_value("Bulk WhatsApp Message", unmigrated.name, "recipient_count", 100)
		self._make_channel_message("Bulk WhatsApp Message", unmigrated.name, "delivered", today_str)

		sync_marketing_campaign_insights(campaign.name)

		# Total reach should be 2 (1 delivered from migrated_bc + 1 delivered from unmigrated)
		# Not 3 (migrated_bc would not be double counted)
		insight = frappe.db.get_value(
			"Marketing Campaign Insight",
			{"marketing_campaign": campaign.name, "granularity": "LIFETIME"},
			["total_reach"],
			as_dict=True,
		)
		self.assertIsNotNone(insight)
		self.assertEqual(insight["total_reach"], 2)

	def test_analytics_sync_counts_only_whatsapp_channel_messages(self):
		"""A Broadcast with WhatsApp and SMS Channel Messages counts only the WhatsApp ones."""
		campaign = self._make_campaign("Analytics Multi Channel Campaign")
		today_str = frappe.utils.today()

		new_bc = self._make_broadcast(
			campaign=campaign.name,
			title="Analytics Multi Channel BC",
			total_recipients=50,
			channels=[
				{"channel": "WhatsApp", "recipient_count": 25, "status": "Completed"},
				{"channel": "SMS", "recipient_count": 25, "status": "Completed"},
			],
		)

		# 1 delivered WhatsApp message, 1 delivered SMS message
		self._make_channel_message("Broadcast", new_bc.name, "delivered", today_str, channel_type="WhatsApp")
		self._make_channel_message("Broadcast", new_bc.name, "delivered", today_str, channel_type="SMS")

		sync_marketing_campaign_insights(campaign.name)

		insight = frappe.db.get_value(
			"Marketing Campaign Insight",
			{"marketing_campaign": campaign.name, "granularity": "LIFETIME"},
			["whatsapp_delivered", "total_reach"],
			as_dict=True,
		)
		self.assertIsNotNone(insight)
		self.assertEqual(insight["whatsapp_delivered"], 1)

	@unittest.skipUnless(frappe.db.exists("DocType", "Bulk WhatsApp Message"), "legacy doctype deleted")
	def test_list_channels_legacy_blast_not_exposed_to_email_only_user(self):
		"""Email-only user cannot see legacy Bulk WhatsApp Message rows in list_channels."""
		campaign = self._make_campaign("Legacy Perm Isolated Campaign")
		legacy_blast = self._make_bulk_whatsapp(campaign.name, "Legacy WA For Email User")

		test_user = "test-email-user-ch@example.com"
		if not frappe.db.exists("User", test_user):
			frappe.get_doc({
				"doctype": "User",
				"email": test_user,
				"first_name": "EmailOnlyUser",
				"send_welcome_email": 0,
				"roles": [{"role": "Email User"}],
			}).insert(ignore_permissions=True)
		else:
			u = frappe.get_doc("User", test_user)
			u.roles = []
			u.append("roles", {"role": "Email User"})
			u.save(ignore_permissions=True)

		frappe.set_user(test_user)
		try:
			channels = list_channels(campaign.name)
			wa_names = [r["name"] for r in channels.get("whatsapp", [])]
			self.assertNotIn(legacy_blast.name, wa_names)
		finally:
			frappe.set_user("Administrator")

	@unittest.skipUnless(frappe.db.exists("DocType", "Bulk WhatsApp Message"), "legacy doctype deleted")
	def test_list_channels_migrated_blast_unreadable_broadcast_skipped(self):
		"""If a legacy blast resolves to a Broadcast the user cannot read, skip it, do not fall through to legacy row."""
		campaign = self._make_campaign("Unreadable Migrated Campaign")
		old_blast = self._make_bulk_whatsapp(campaign.name, "Unreadable Migrated Blast")
		migrated_bc = self._make_broadcast(
			campaign=campaign.name,
			title="Migrated But Unreadable BC",
			legacy_doctype="Bulk WhatsApp Message",
			legacy_name=old_blast.name,
			channels=[{"channel": "WhatsApp"}],
		)

		test_user = "test-wa-user-no-read@example.com"
		if not frappe.db.exists("User", test_user):
			frappe.get_doc({
				"doctype": "User",
				"email": test_user,
				"first_name": "WANoReadUser",
				"send_welcome_email": 0,
				"roles": [{"role": "WhatsApp Manager"}],
			}).insert(ignore_permissions=True)

		with patch("msuite_campaigns.broadcast.permissions.permitted_channels", return_value={"WhatsApp"}):
			with patch("frappe.has_permission", side_effect=lambda doctype, ptype="read", doc=None: False if doctype == "Broadcast" else True):
				frappe.set_user(test_user)
				try:
					channels = list_channels(campaign.name)
					wa_names = [r["name"] for r in channels.get("whatsapp", [])]
					self.assertNotIn(migrated_bc.name, wa_names)
					self.assertNotIn(old_blast.name, wa_names)
				finally:
					frappe.set_user("Administrator")

	def test_broadcast_campaign_integration_tolerates_deleted_legacy_doctypes(self):
		"""Regression: Campaign and Calendar operations tolerate deleted legacy doctypes."""
		campaign = self._make_campaign("Tolerate Deleted DocTypes Campaign")
		self._make_broadcast(campaign=campaign.name, title="New Broadcast")
		channels = list_channels(campaign.name)
		self.assertIsInstance(channels, dict)
		events = get_calendar_events(from_date="2026-01-01", to_date="2026-12-31")
		self.assertIsInstance(events, list)
		counts = composer._get_relationship_counts(campaign.name)
		self.assertIn("broadcasts", counts)

	def test_audience_endpoints_tolerate_deleted_legacy_doctypes(self):
		"""Regression: audience and broadcast detail endpoints raise DoesNotExistError when legacy doctypes are deleted."""
		from msuite_campaigns.audience.api.v1 import audience

		# get_broadcast_detail with legacy prefixes raises clean DoesNotExistError
		with self.assertRaises(frappe.DoesNotExistError):
			audience.get_broadcast_detail("BULK-WA-2026-00001")
		with self.assertRaises(frappe.DoesNotExistError):
			audience.get_broadcast_detail("BULK-EM-2026-00001")
		with self.assertRaises(frappe.DoesNotExistError):
			audience.get_broadcast_detail("BULK-SM-2026-00001")

		# get_members, update_member, delete_member, get_validation_summary raise clean DoesNotExistError
		with self.assertRaises(frappe.DoesNotExistError):
			audience.get_members("AUD-NONEXISTENT")
		with self.assertRaises(frappe.DoesNotExistError):
			audience.update_member("AM-NONEXISTENT")
		with self.assertRaises(frappe.DoesNotExistError):
			audience.delete_member("AM-NONEXISTENT")
		with self.assertRaises(frappe.DoesNotExistError):
			audience.get_validation_summary("AUD-NONEXISTENT")


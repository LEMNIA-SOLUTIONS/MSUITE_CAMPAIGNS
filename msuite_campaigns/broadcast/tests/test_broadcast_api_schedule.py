# Copyright (c) 2026, MSuite and contributors
# For license information, please see license.txt

"""Tests for Broadcast schedule API integration (Task 4).

Validates:
- save_broadcast stores schedule_mode and per-channel row scheduled_time and ignores unknown fields.
- get_broadcast returns schedule_mode and per-channel row scheduled_time.
- Staged flow: save WhatsApp draft -> save again adding Email with its own time -> launch_broadcast
  transitions WhatsApp to Queued and Email to Scheduled.
- Same-for-all flow: save with one shared time -> launch_broadcast transitions all to Scheduled.
- Channel removal flow: removing a channel from a draft then launching sends only the remaining channel.
- clone_broadcast copies schedule_mode and scheduled times (both parent and row).
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_to_date, get_datetime, now_datetime

from msuite_campaigns.broadcast.api.v1.broadcast import (
	clone_broadcast,
	get_broadcast,
	launch_broadcast,
	save_broadcast,
)
from msuite_campaigns.broadcast.constants import (
	CHANNEL_EMAIL,
	CHANNEL_WHATSAPP,
	SCHEDULE_PER_CHANNEL,
	SCHEDULE_SAME,
	STATUS_COMPLETED,
	STATUS_DRAFT,
	STATUS_QUEUED,
	STATUS_SCHEDULED,
)

TEST_PREFIX = "ZZTestAPISched "


class TestBroadcastApiSchedule(IntegrationTestCase):
	"""Test suite for Broadcast scheduling API and staged lifecycle workflows."""

	@classmethod
	def setUpClass(cls) -> None:
		"""Provision linked account and template fixtures required by Broadcast Channel link validations.

		Params:
			None.

		Returns:
			None.

		Raises:
			None.

		Side effects:
			Inserts WhatsApp Account, WhatsApp Template, Email Account, and Email Template documents.
		"""
		super().setUpClass()
		frappe.set_user("Administrator")

		from frappe.utils.password import set_encrypted_password

		cls.wa_account = "Test WA Account APISched"
		if not frappe.db.exists("WhatsApp Account", cls.wa_account):
			wa_acc = frappe.get_doc({
				"doctype": "WhatsApp Account",
				"account_name": cls.wa_account,
				"phone_number": "+1234567890",
				"status": "Active",
			}).insert(ignore_permissions=True, ignore_links=True)
			set_encrypted_password("WhatsApp Account", wa_acc.name, "dummy_token_123", "token")

		cls.wa_template_id = "test_wa_tpl_apisched"
		tpl_name = f"{cls.wa_template_id}-en"
		if not frappe.db.exists("WhatsApp Templates", tpl_name):
			try:
				from msuite_whatsapp.whatsapp.doctype.whatsapp_templates.whatsapp_templates import WhatsAppTemplates

				with patch.object(WhatsAppTemplates, "push_to_meta", lambda self: None):
					tpl_doc = frappe.get_doc({
						"doctype": "WhatsApp Templates",
						"template_name": cls.wa_template_id,
						"whatsapp_account": cls.wa_account,
						"category": "MARKETING",
						"status": "APPROVED",
						"language": "en",
					}).insert(ignore_permissions=True, ignore_links=True)
					cls.wa_template = tpl_doc.name
			except Exception:
				cls.wa_template = tpl_name
		else:
			cls.wa_template = tpl_name

		cls.wa_template_changed_id = "test_wa_tpl_apisched_ch"
		tpl_ch_name = f"{cls.wa_template_changed_id}-en"
		if not frappe.db.exists("WhatsApp Templates", tpl_ch_name):
			try:
				from msuite_whatsapp.whatsapp.doctype.whatsapp_templates.whatsapp_templates import WhatsAppTemplates

				with patch.object(WhatsAppTemplates, "push_to_meta", lambda self: None):
					tpl_doc2 = frappe.get_doc({
						"doctype": "WhatsApp Templates",
						"template_name": cls.wa_template_changed_id,
						"whatsapp_account": cls.wa_account,
						"category": "MARKETING",
						"status": "APPROVED",
						"language": "en",
					}).insert(ignore_permissions=True, ignore_links=True)
					cls.wa_template_changed = tpl_doc2.name
			except Exception:
				cls.wa_template_changed = tpl_ch_name
		else:
			cls.wa_template_changed = tpl_ch_name

		cls.email_account = "Test Email Account APISched"
		if not frappe.db.exists("Email Account", cls.email_account):
			with patch("frappe.utils.global_search.sync_value_in_queue", lambda val: None):
				frappe.get_doc({
					"doctype": "Email Account",
					"email_id": "test-apisched@example.com",
					"email_account_name": cls.email_account,
					"enable_outgoing": 1,
				}).insert(ignore_permissions=True, ignore_links=True)

		cls.email_template = "Test Email Template APISched"
		if not frappe.db.exists("Email Template", cls.email_template):
			frappe.get_doc({
				"doctype": "Email Template",
				"name": cls.email_template,
				"subject": "Test Subject",
				"response": "Hello World",
			}).insert(ignore_permissions=True, ignore_links=True)

		frappe.db.commit()

	@classmethod
	def tearDownClass(cls) -> None:
		"""Clean up test fixtures created during setUpClass.

		Params:
			None.

		Returns:
			None.

		Raises:
			None.

		Side effects:
			Deletes created WhatsApp and Email test accounts/templates and commits transaction.
		"""
		tpl_name = getattr(cls, "wa_template", None) or f"{cls.wa_template_id}-en"
		if frappe.db.exists("WhatsApp Templates", tpl_name):
			frappe.delete_doc("WhatsApp Templates", tpl_name, force=True, ignore_permissions=True)

		tpl_ch_name = getattr(cls, "wa_template_changed", None) or f"{cls.wa_template_changed_id}-en"
		if frappe.db.exists("WhatsApp Templates", tpl_ch_name):
			frappe.delete_doc("WhatsApp Templates", tpl_ch_name, force=True, ignore_permissions=True)

		wa_acc = getattr(cls, "wa_account", None)
		if wa_acc and frappe.db.exists("WhatsApp Account", wa_acc):
			frappe.delete_doc("WhatsApp Account", wa_acc, force=True, ignore_permissions=True)

		email_tpl = getattr(cls, "email_template", None)
		if email_tpl and frappe.db.exists("Email Template", email_tpl):
			frappe.delete_doc("Email Template", email_tpl, force=True, ignore_permissions=True)

		email_acc = getattr(cls, "email_account", None)
		if email_acc and frappe.db.exists("Email Account", email_acc):
			frappe.delete_doc("Email Account", email_acc, force=True, ignore_permissions=True)

		frappe.db.commit()
		super().tearDownClass()

	def setUp(self) -> None:
		"""Initialize test environment, permissions, and test document tracker.

		Params:
			None.

		Returns:
			None.

		Raises:
			None.

		Side effects:
			Sets current user to Administrator, initializes adapter stubs, and tracks cleanup.
		"""
		super().setUp()
		frappe.set_user("Administrator")
		self.cleanup_broadcasts: list[str] = []

		fake_wa = {
			"channel": CHANNEL_WHATSAPP,
			"validate_channel": lambda b_doc, c_row: None,
		}
		fake_em = {
			"channel": CHANNEL_EMAIL,
			"validate_channel": lambda b_doc, c_row: None,
		}
		setattr(
			frappe.local,
			"broadcast_channel_adapters",
			{
				CHANNEL_WHATSAPP: fake_wa,
				CHANNEL_EMAIL: fake_em,
			},
		)

	def tearDown(self) -> None:
		"""Clean up test documents and rollback transaction.

		Params:
			None.

		Returns:
			None.

		Raises:
			None.

		Side effects:
			Deletes created Broadcast records and rolls back database state.
		"""
		frappe.db.rollback()
		for name in self.cleanup_broadcasts:
			if frappe.db.exists("Broadcast", name):
				frappe.delete_doc("Broadcast", name, force=True, ignore_permissions=True)
		super().tearDown()

	def test_save_broadcast_stores_mode_and_row_times_and_ignores_unknown_fields(self) -> None:
		"""Ensure save_broadcast stores schedule_mode and row times while ignoring unknown fields.

		Params:
			None.

		Returns:
			None.

		Raises:
			AssertionError: If schedule fields are not stored or unknown fields leak into DB.

		Side effects:
			Inserts a Broadcast record in tabBroadcast and child rows in tabBroadcast Channel.
		"""
		now = now_datetime()
		future_wa = add_to_date(now, hours=2)
		future_em = add_to_date(now, hours=4)

		payload = {
			"title": f"{TEST_PREFIX}ModeAndTimes",
			"schedule_mode": SCHEDULE_PER_CHANNEL,
			"recipient_source": "Individual",
			"forged_status": STATUS_COMPLETED,
			"unknown_top_field": "injected_value",
			"channels": [
				{
					"channel": CHANNEL_WHATSAPP,
					"whatsapp_account": self.wa_account,
					"whatsapp_template": self.wa_template,
					"scheduled_time": str(future_wa),
					"unknown_channel_field": "injected_child",
					"status": STATUS_QUEUED,
					"sent_count": 500,
				},
				{
					"channel": CHANNEL_EMAIL,
					"email_account": self.email_account,
					"email_template": self.email_template,
					"scheduled_time": str(future_em),
					"status": STATUS_COMPLETED,
				},
			],
		}

		res = save_broadcast(doc=json.dumps(payload))
		b_name = res["name"]
		self.cleanup_broadcasts.append(b_name)

		self.assertEqual(res["status"], STATUS_DRAFT)

		doc = frappe.get_doc("Broadcast", b_name)
		self.assertEqual(doc.schedule_mode, SCHEDULE_PER_CHANNEL)
		self.assertEqual(get_datetime(doc.scheduled_time), get_datetime(future_wa))
		self.assertFalse(doc.get("unknown_top_field"))

		wa_row = doc.channel_row(CHANNEL_WHATSAPP)
		self.assertIsNotNone(wa_row)
		self.assertEqual(get_datetime(wa_row.scheduled_time), get_datetime(future_wa))
		self.assertEqual(wa_row.status, STATUS_DRAFT)
		self.assertEqual(wa_row.sent_count, 0)
		self.assertFalse(wa_row.get("unknown_channel_field"))

		em_row = doc.channel_row(CHANNEL_EMAIL)
		self.assertIsNotNone(em_row)
		self.assertEqual(get_datetime(em_row.scheduled_time), get_datetime(future_em))
		self.assertEqual(em_row.status, STATUS_DRAFT)

	def test_get_broadcast_returns_schedule_mode_and_row_times(self) -> None:
		"""Ensure get_broadcast returns schedule_mode and per-row scheduled_time fields.

		Params:
			None.

		Returns:
			None.

		Raises:
			AssertionError: If returned dictionary lacks schedule_mode or row scheduled_time.

		Side effects:
			Inserts a Broadcast record in database.
		"""
		now = now_datetime()
		future_wa = add_to_date(now, hours=2)
		future_em = add_to_date(now, hours=4)

		payload = {
			"title": f"{TEST_PREFIX}GetBroadcast",
			"schedule_mode": SCHEDULE_PER_CHANNEL,
			"recipient_source": "Individual",
			"channels": [
				{
					"channel": CHANNEL_WHATSAPP,
					"whatsapp_account": self.wa_account,
					"whatsapp_template": self.wa_template,
					"scheduled_time": str(future_wa),
				},
				{
					"channel": CHANNEL_EMAIL,
					"email_account": self.email_account,
					"email_template": self.email_template,
					"scheduled_time": str(future_em),
				},
			],
		}

		res = save_broadcast(doc=json.dumps(payload))
		b_name = res["name"]
		self.cleanup_broadcasts.append(b_name)

		data = get_broadcast(b_name)
		self.assertEqual(data["schedule_mode"], SCHEDULE_PER_CHANNEL)
		self.assertEqual(get_datetime(data["scheduled_time"]), get_datetime(future_wa))

		channels = {ch["channel"]: ch for ch in data.get("channels", [])}
		self.assertIn(CHANNEL_WHATSAPP, channels)
		self.assertIn(CHANNEL_EMAIL, channels)

		self.assertEqual(get_datetime(channels[CHANNEL_WHATSAPP]["scheduled_time"]), get_datetime(future_wa))
		self.assertEqual(get_datetime(channels[CHANNEL_EMAIL]["scheduled_time"]), get_datetime(future_em))

	def test_staged_flow_save_draft_add_channel_then_launch(self) -> None:
		"""Validate staged flow: save WhatsApp draft -> save again adding Email with time -> launch.

		WhatsApp has no scheduled_time (sends at launch -> Queued).
		Email has its own future scheduled_time (-> Scheduled).

		Params:
			None.

		Returns:
			None.

		Raises:
			AssertionError: If channels do not transition to Queued and Scheduled respectively.

		Side effects:
			Inserts and updates Broadcast and child tables; materializes recipient rows in DB.
		"""
		now = now_datetime()
		future_em = add_to_date(now, hours=3)

		# 1. Save WhatsApp draft alone
		payload_wa = {
			"title": f"{TEST_PREFIX}StagedFlow",
			"schedule_mode": SCHEDULE_PER_CHANNEL,
			"recipient_source": "Individual",
			"channels": [
				{
					"channel": CHANNEL_WHATSAPP,
					"whatsapp_account": self.wa_account,
					"whatsapp_template": self.wa_template,
				}
			],
		}
		res_wa = save_broadcast(doc=json.dumps(payload_wa))
		b_name = res_wa["name"]
		self.cleanup_broadcasts.append(b_name)
		self.assertEqual(res_wa["status"], STATUS_DRAFT)

		# 2. Save again adding Email with its own future scheduled_time
		payload_staged = {
			"schedule_mode": SCHEDULE_PER_CHANNEL,
			"channels": [
				{
					"channel": CHANNEL_WHATSAPP,
					"whatsapp_account": self.wa_account,
					"whatsapp_template": self.wa_template,
				},
				{
					"channel": CHANNEL_EMAIL,
					"email_account": self.email_account,
					"email_template": self.email_template,
					"scheduled_time": str(future_em),
				},
			],
		}
		save_broadcast(name=b_name, doc=json.dumps(payload_staged))

		doc = frappe.get_doc("Broadcast", b_name)
		self.assertEqual(len(doc.channels), 2)
		self.assertEqual(doc.status, STATUS_DRAFT)

		# 3. Launch broadcast with recipients
		recipients = json.dumps([
			{"address": "+919876543210"},
			{"address": "staged@example.com"},
		])

		with patch("msuite_campaigns.broadcast.hub.start_channel") as mock_start:
			launch_res = launch_broadcast(b_name, individual_rows=recipients)

		self.assertEqual(launch_res.get(CHANNEL_WHATSAPP), STATUS_QUEUED)
		self.assertEqual(launch_res.get(CHANNEL_EMAIL), STATUS_SCHEDULED)

		# Verify database state
		doc.reload()
		wa_row = doc.channel_row(CHANNEL_WHATSAPP)
		em_row = doc.channel_row(CHANNEL_EMAIL)
		self.assertEqual(wa_row.status, STATUS_QUEUED)
		self.assertEqual(em_row.status, STATUS_SCHEDULED)
		self.assertEqual(doc.status, STATUS_QUEUED)

		# start_channel should have been called for WhatsApp only, not Email
		mock_start.assert_called_once_with(b_name, CHANNEL_WHATSAPP)

	def test_same_for_all_flow_with_one_time(self) -> None:
		"""Validate same-for-all flow: one shared time transitions all channels to Scheduled.

		Params:
			None.

		Returns:
			None.

		Raises:
			AssertionError: If channels are not both transitioned to Scheduled.

		Side effects:
			Inserts Broadcast and child rows; materializes recipient rows in DB.
		"""
		future_time = add_to_date(now_datetime(), hours=2)

		payload = {
			"title": f"{TEST_PREFIX}SameForAllFlow",
			"schedule_mode": SCHEDULE_SAME,
			"scheduled_time": str(future_time),
			"recipient_source": "Individual",
			"channels": [
				{
					"channel": CHANNEL_WHATSAPP,
					"whatsapp_account": self.wa_account,
					"whatsapp_template": self.wa_template,
				},
				{
					"channel": CHANNEL_EMAIL,
					"email_account": self.email_account,
					"email_template": self.email_template,
				},
			],
		}
		res = save_broadcast(doc=json.dumps(payload))
		b_name = res["name"]
		self.cleanup_broadcasts.append(b_name)

		recipients = json.dumps([
			{"address": "+919876543210"},
			{"address": "same@example.com"},
		])

		with patch("msuite_campaigns.broadcast.hub.start_channel") as mock_start:
			launch_res = launch_broadcast(b_name, individual_rows=recipients)

		self.assertEqual(launch_res.get(CHANNEL_WHATSAPP), STATUS_SCHEDULED)
		self.assertEqual(launch_res.get(CHANNEL_EMAIL), STATUS_SCHEDULED)

		doc = frappe.get_doc("Broadcast", b_name)
		self.assertEqual(doc.channel_row(CHANNEL_WHATSAPP).status, STATUS_SCHEDULED)
		self.assertEqual(doc.channel_row(CHANNEL_EMAIL).status, STATUS_SCHEDULED)
		self.assertEqual(doc.status, STATUS_SCHEDULED)

		# Neither channel was due, so start_channel was not invoked
		mock_start.assert_not_called()

	def test_remove_channel_from_draft_then_launch_sends_only_remaining(self) -> None:
		"""Validate that removing a channel from a draft then launching sends only the remaining one.

		Params:
			None.

		Returns:
			None.

		Raises:
			AssertionError: If removed channel is launched or present in results.

		Side effects:
			Mutates Broadcast channel rows and materializes recipients for remaining channel.
		"""
		# 1. Save draft with WhatsApp and Email
		payload = {
			"title": f"{TEST_PREFIX}RemoveChannelThenLaunch",
			"schedule_mode": SCHEDULE_SAME,
			"recipient_source": "Individual",
			"channels": [
				{
					"channel": CHANNEL_WHATSAPP,
					"whatsapp_account": self.wa_account,
					"whatsapp_template": self.wa_template,
				},
				{
					"channel": CHANNEL_EMAIL,
					"email_account": self.email_account,
					"email_template": self.email_template,
				},
			],
		}
		res = save_broadcast(doc=json.dumps(payload))
		b_name = res["name"]
		self.cleanup_broadcasts.append(b_name)

		# 2. Update draft to remove Email, keeping only WhatsApp
		payload_updated = {
			"channels": [
				{
					"channel": CHANNEL_WHATSAPP,
					"whatsapp_account": self.wa_account,
					"whatsapp_template": self.wa_template,
				}
			],
		}
		save_broadcast(name=b_name, doc=json.dumps(payload_updated))

		doc = frappe.get_doc("Broadcast", b_name)
		self.assertEqual(len(doc.channels), 1)
		self.assertEqual(doc.channels[0].channel, CHANNEL_WHATSAPP)

		# 3. Launch
		recipients = json.dumps([{"address": "+919876543210"}])
		with patch("msuite_campaigns.broadcast.hub.start_channel") as mock_start:
			launch_res = launch_broadcast(b_name, individual_rows=recipients)

		self.assertEqual(launch_res, {CHANNEL_WHATSAPP: STATUS_QUEUED})
		mock_start.assert_called_once_with(b_name, CHANNEL_WHATSAPP)

	def test_clone_broadcast_copies_mode_and_times(self) -> None:
		"""Validate that clone_broadcast copies schedule_mode and scheduled times.

		Params:
			None.

		Returns:
			None.

		Raises:
			AssertionError: If cloned Broadcast loses schedule_mode or scheduled_time.

		Side effects:
			Inserts original and cloned Broadcast docs into database.
		"""
		now = now_datetime()
		future_wa = add_to_date(now, hours=2)
		future_em = add_to_date(now, hours=5)

		# 1. Per channel mode
		payload_per_channel = {
			"title": f"{TEST_PREFIX}ClonePerChannel",
			"schedule_mode": SCHEDULE_PER_CHANNEL,
			"recipient_source": "Individual",
			"channels": [
				{
					"channel": CHANNEL_WHATSAPP,
					"whatsapp_account": self.wa_account,
					"whatsapp_template": self.wa_template,
					"scheduled_time": str(future_wa),
				},
				{
					"channel": CHANNEL_EMAIL,
					"email_account": self.email_account,
					"email_template": self.email_template,
					"scheduled_time": str(future_em),
				},
			],
		}
		res1 = save_broadcast(doc=json.dumps(payload_per_channel))
		b1_name = res1["name"]
		self.cleanup_broadcasts.append(b1_name)

		cloned1 = clone_broadcast(b1_name)
		cloned1_name = cloned1["name"]
		self.cleanup_broadcasts.append(cloned1_name)

		self.assertNotEqual(cloned1_name, b1_name)
		self.assertEqual(cloned1["title"], f"{TEST_PREFIX}ClonePerChannel (copy)")
		self.assertEqual(cloned1["status"], STATUS_DRAFT)
		self.assertEqual(cloned1["schedule_mode"], SCHEDULE_PER_CHANNEL)
		self.assertEqual(get_datetime(cloned1["scheduled_time"]), get_datetime(future_wa))

		cloned1_doc = frappe.get_doc("Broadcast", cloned1_name)
		wa1 = cloned1_doc.channel_row(CHANNEL_WHATSAPP)
		em1 = cloned1_doc.channel_row(CHANNEL_EMAIL)
		self.assertEqual(get_datetime(wa1.scheduled_time), get_datetime(future_wa))
		self.assertEqual(get_datetime(em1.scheduled_time), get_datetime(future_em))
		self.assertEqual(wa1.status, STATUS_DRAFT)
		self.assertEqual(em1.status, STATUS_DRAFT)

		# 2. Same for all channels mode
		future_same = add_to_date(now, hours=3)
		payload_same = {
			"title": f"{TEST_PREFIX}CloneSame",
			"schedule_mode": SCHEDULE_SAME,
			"scheduled_time": str(future_same),
			"recipient_source": "Individual",
			"channels": [
				{
					"channel": CHANNEL_WHATSAPP,
					"whatsapp_account": self.wa_account,
					"whatsapp_template": self.wa_template,
				},
				{
					"channel": CHANNEL_EMAIL,
					"email_account": self.email_account,
					"email_template": self.email_template,
				},
			],
		}
		res2 = save_broadcast(doc=json.dumps(payload_same))
		b2_name = res2["name"]
		self.cleanup_broadcasts.append(b2_name)

		cloned2 = clone_broadcast(b2_name)
		cloned2_name = cloned2["name"]
		self.cleanup_broadcasts.append(cloned2_name)

		self.assertEqual(cloned2["schedule_mode"], SCHEDULE_SAME)
		self.assertEqual(get_datetime(cloned2["scheduled_time"]), get_datetime(future_same))
		cloned2_doc = frappe.get_doc("Broadcast", cloned2_name)
		for r in cloned2_doc.channels:
			self.assertIsNone(r.scheduled_time)

	def test_clone_with_past_row_times_clears_them_and_succeeds(self) -> None:
		"""Validate that clone_broadcast clears past row and parent times in Per channel mode and succeeds.

		Params:
			None.

		Returns:
			None.

		Raises:
			AssertionError: If cloning fails or past times are not cleared.

		Side effects:
			Inserts and updates Broadcast and child rows; clones Broadcast.
		"""
		now = now_datetime()
		future_wa = add_to_date(now, hours=2)
		future_em = add_to_date(now, hours=4)

		payload = {
			"title": f"{TEST_PREFIX}ClonePastPerChannel",
			"schedule_mode": SCHEDULE_PER_CHANNEL,
			"recipient_source": "Individual",
			"channels": [
				{
					"channel": CHANNEL_WHATSAPP,
					"whatsapp_account": self.wa_account,
					"whatsapp_template": self.wa_template,
					"scheduled_time": str(future_wa),
				},
				{
					"channel": CHANNEL_EMAIL,
					"email_account": self.email_account,
					"email_template": self.email_template,
					"scheduled_time": str(future_em),
				},
			],
		}
		res = save_broadcast(doc=json.dumps(payload))
		b_name = res["name"]
		self.cleanup_broadcasts.append(b_name)

		# Move row times and parent time into the past via db.set_value and set status to Completed
		past_time = add_to_date(now, hours=-2)
		frappe.db.set_value("Broadcast", b_name, "status", STATUS_COMPLETED)
		frappe.db.set_value("Broadcast", b_name, "scheduled_time", past_time)
		for ch_name in frappe.get_all("Broadcast Channel", filters={"parent": b_name}, pluck="name"):
			frappe.db.set_value("Broadcast Channel", ch_name, "scheduled_time", past_time)
			frappe.db.set_value("Broadcast Channel", ch_name, "status", STATUS_COMPLETED)

		# Now clone the completed broadcast with past schedule times
		cloned = clone_broadcast(b_name)
		cloned_name = cloned["name"]
		self.cleanup_broadcasts.append(cloned_name)

		self.assertEqual(cloned["status"], STATUS_DRAFT)
		self.assertEqual(cloned["schedule_mode"], SCHEDULE_PER_CHANNEL)
		self.assertIsNone(cloned.get("scheduled_time"))

		cloned_doc = frappe.get_doc("Broadcast", cloned_name)
		self.assertIsNone(cloned_doc.scheduled_time)
		self.assertEqual(cloned_doc.schedule_mode, SCHEDULE_PER_CHANNEL)
		for r in cloned_doc.channels:
			self.assertIsNone(r.scheduled_time)
			self.assertEqual(r.status, STATUS_DRAFT)

	def test_clone_with_future_row_times_keeps_them(self) -> None:
		"""Validate that clone_broadcast preserves future row times and parent times.

		In Per channel mode, future row times are preserved.
		In Same mode, future parent scheduled_time is preserved.

		Params:
			None.

		Returns:
			None.

		Raises:
			AssertionError: If future row times or parent times are lost during cloning.

		Side effects:
			Inserts and clones Broadcast records.
		"""
		now = now_datetime()
		future_wa = add_to_date(now, hours=2)
		future_em = add_to_date(now, hours=5)

		# 1. Per channel mode
		payload_per_ch = {
			"title": f"{TEST_PREFIX}CloneFuturePerCh",
			"schedule_mode": SCHEDULE_PER_CHANNEL,
			"recipient_source": "Individual",
			"channels": [
				{
					"channel": CHANNEL_WHATSAPP,
					"whatsapp_account": self.wa_account,
					"whatsapp_template": self.wa_template,
					"scheduled_time": str(future_wa),
				},
				{
					"channel": CHANNEL_EMAIL,
					"email_account": self.email_account,
					"email_template": self.email_template,
					"scheduled_time": str(future_em),
				},
			],
		}
		res_per = save_broadcast(doc=json.dumps(payload_per_ch))
		b_per = res_per["name"]
		self.cleanup_broadcasts.append(b_per)

		cloned_per = clone_broadcast(b_per)
		self.cleanup_broadcasts.append(cloned_per["name"])
		self.assertEqual(cloned_per["status"], STATUS_DRAFT)
		self.assertEqual(cloned_per["schedule_mode"], SCHEDULE_PER_CHANNEL)
		cloned_per_doc = frappe.get_doc("Broadcast", cloned_per["name"])
		cloned_wa = cloned_per_doc.channel_row(CHANNEL_WHATSAPP)
		cloned_em = cloned_per_doc.channel_row(CHANNEL_EMAIL)
		self.assertEqual(cloned_wa.status, STATUS_DRAFT)
		self.assertEqual(cloned_em.status, STATUS_DRAFT)
		self.assertEqual(get_datetime(cloned_wa.scheduled_time), get_datetime(future_wa))
		self.assertEqual(get_datetime(cloned_em.scheduled_time), get_datetime(future_em))

		# 2. Same mode
		payload_same = {
			"title": f"{TEST_PREFIX}CloneFutureSame",
			"schedule_mode": SCHEDULE_SAME,
			"scheduled_time": str(future_wa),
			"recipient_source": "Individual",
			"channels": [
				{
					"channel": CHANNEL_WHATSAPP,
					"whatsapp_account": self.wa_account,
					"whatsapp_template": self.wa_template,
				},
			],
		}
		res_same = save_broadcast(doc=json.dumps(payload_same))
		b_same = res_same["name"]
		self.cleanup_broadcasts.append(b_same)

		cloned_same = clone_broadcast(b_same)
		self.cleanup_broadcasts.append(cloned_same["name"])
		self.assertEqual(cloned_same["status"], STATUS_DRAFT)
		self.assertEqual(cloned_same["schedule_mode"], SCHEDULE_SAME)
		self.assertEqual(get_datetime(cloned_same["scheduled_time"]), get_datetime(future_wa))
		cloned_same_doc = frappe.get_doc("Broadcast", cloned_same["name"])
		self.assertEqual(get_datetime(cloned_same_doc.scheduled_time), get_datetime(future_wa))
		self.assertIsNone(cloned_same_doc.channel_row(CHANNEL_WHATSAPP).scheduled_time)

	def test_clone_same_mode_with_past_parent_time_clears_it(self) -> None:
		"""Validate that clone_broadcast clears past parent scheduled_time in Same for all channels mode.

		Params:
			None.

		Returns:
			None.

		Raises:
			AssertionError: If past scheduled_time is not cleared in cloned Broadcast.

		Side effects:
			Inserts and updates Broadcast and child rows; clones Broadcast.
		"""
		now = now_datetime()
		future_time = add_to_date(now, hours=2)

		payload = {
			"title": f"{TEST_PREFIX}ClonePastSame",
			"schedule_mode": SCHEDULE_SAME,
			"scheduled_time": str(future_time),
			"recipient_source": "Individual",
			"channels": [
				{
					"channel": CHANNEL_WHATSAPP,
					"whatsapp_account": self.wa_account,
					"whatsapp_template": self.wa_template,
				},
				{
					"channel": CHANNEL_EMAIL,
					"email_account": self.email_account,
					"email_template": self.email_template,
				},
			],
		}
		res = save_broadcast(doc=json.dumps(payload))
		b_name = res["name"]
		self.cleanup_broadcasts.append(b_name)

		# Move parent scheduled_time into the past and set status to Completed
		past_time = add_to_date(now, hours=-3)
		frappe.db.set_value("Broadcast", b_name, "status", STATUS_COMPLETED)
		frappe.db.set_value("Broadcast", b_name, "scheduled_time", past_time)

		cloned = clone_broadcast(b_name)
		cloned_name = cloned["name"]
		self.cleanup_broadcasts.append(cloned_name)

		self.assertEqual(cloned["status"], STATUS_DRAFT)
		self.assertEqual(cloned["schedule_mode"], SCHEDULE_SAME)
		self.assertIsNone(cloned.get("scheduled_time"))

		cloned_doc = frappe.get_doc("Broadcast", cloned_name)
		self.assertIsNone(cloned_doc.scheduled_time)
		self.assertEqual(cloned_doc.schedule_mode, SCHEDULE_SAME)
		for r in cloned_doc.channels:
			self.assertIsNone(r.scheduled_time)
			self.assertEqual(r.status, STATUS_DRAFT)

	def test_save_keeps_locked_row_untouched_and_scheduled_row_scheduled(self) -> None:
		"""Validate save_broadcast keeps locked rows untouched and preserves scheduled row status.

		Params:
			None.

		Returns:
			None.

		Raises:
			AssertionError: If locked row fields mutate or scheduled row loses its status.

		Side effects:
			Inserts and updates Broadcast and child rows.
		"""
		now = now_datetime()
		future_time_1 = add_to_date(now, hours=2)
		future_time_2 = add_to_date(now, hours=4)

		payload = {
			"title": f"{TEST_PREFIX}LockedUntouched",
			"schedule_mode": SCHEDULE_PER_CHANNEL,
			"recipient_source": "Individual",
			"channels": [
				{
					"channel": CHANNEL_WHATSAPP,
					"whatsapp_account": self.wa_account,
					"whatsapp_template": self.wa_template,
					"scheduled_time": str(future_time_1),
				},
				{
					"channel": CHANNEL_EMAIL,
					"email_account": self.email_account,
					"email_template": self.email_template,
					"scheduled_time": str(future_time_1),
				},
			],
		}
		res = save_broadcast(doc=json.dumps(payload))
		b_name = res["name"]
		self.cleanup_broadcasts.append(b_name)

		# WhatsApp set Completed with counters via db.set_value; Email Scheduled with future time
		wa_row_name = frappe.db.get_value("Broadcast Channel", {"parent": b_name, "channel": CHANNEL_WHATSAPP}, "name")
		frappe.db.set_value("Broadcast Channel", wa_row_name, {
			"status": STATUS_COMPLETED,
			"sent_count": 42,
			"delivered_count": 40,
		})
		email_row_name = frappe.db.get_value("Broadcast Channel", {"parent": b_name, "channel": CHANNEL_EMAIL}, "name")
		frappe.db.set_value("Broadcast Channel", email_row_name, {
			"status": STATUS_SCHEDULED,
			"scheduled_time": future_time_1,
		})
		frappe.db.set_value("Broadcast", b_name, {
			"status": STATUS_SCHEDULED,
			"scheduled_time": future_time_1,
		})

		# Save with new Email time and changed WhatsApp template
		update_payload = {
			"title": f"{TEST_PREFIX}LockedUntouched Updated",
			"channels": [
				{
					"channel": CHANNEL_WHATSAPP,
					"whatsapp_account": self.wa_account,
					"whatsapp_template": self.wa_template_changed,
					"sent_count": 999,
				},
				{
					"channel": CHANNEL_EMAIL,
					"email_account": self.email_account,
					"email_template": self.email_template,
					"scheduled_time": str(future_time_2),
				},
			],
		}
		save_broadcast(doc=json.dumps(update_payload), name=b_name)

		doc = frappe.get_doc("Broadcast", b_name)
		wa_row = doc.channel_row(CHANNEL_WHATSAPP)
		self.assertEqual(wa_row.name, wa_row_name)
		self.assertEqual(wa_row.status, STATUS_COMPLETED)
		self.assertEqual(wa_row.sent_count, 42)
		self.assertEqual(wa_row.delivered_count, 40)
		self.assertEqual(wa_row.whatsapp_template, self.wa_template)

		email_row = doc.channel_row(CHANNEL_EMAIL)
		self.assertEqual(email_row.name, email_row_name)
		self.assertEqual(email_row.status, STATUS_SCHEDULED)
		self.assertEqual(get_datetime(email_row.scheduled_time), get_datetime(future_time_2))

	def test_save_omitting_locked_row_is_rejected_naming_channel(self) -> None:
		"""Validate save_broadcast rejects omitting a locked row, naming the channel.

		Params:
			None.

		Returns:
			None.

		Raises:
			AssertionError: If validation does not reject omitted locked row with expected message.

		Side effects:
			Inserts and updates Broadcast and child rows.
		"""
		now = now_datetime()
		future_time = add_to_date(now, hours=2)

		payload = {
			"title": f"{TEST_PREFIX}OmitLocked",
			"schedule_mode": SCHEDULE_PER_CHANNEL,
			"recipient_source": "Individual",
			"channels": [
				{
					"channel": CHANNEL_WHATSAPP,
					"whatsapp_account": self.wa_account,
					"whatsapp_template": self.wa_template,
				},
				{
					"channel": CHANNEL_EMAIL,
					"email_account": self.email_account,
					"email_template": self.email_template,
					"scheduled_time": str(future_time),
				},
			],
		}
		res = save_broadcast(doc=json.dumps(payload))
		b_name = res["name"]
		self.cleanup_broadcasts.append(b_name)

		# WhatsApp Completed, Email Scheduled
		wa_row_name = frappe.db.get_value("Broadcast Channel", {"parent": b_name, "channel": CHANNEL_WHATSAPP}, "name")
		frappe.db.set_value("Broadcast Channel", wa_row_name, "status", STATUS_COMPLETED)
		email_row_name = frappe.db.get_value("Broadcast Channel", {"parent": b_name, "channel": CHANNEL_EMAIL}, "name")
		frappe.db.set_value("Broadcast Channel", email_row_name, "status", STATUS_SCHEDULED)
		frappe.db.set_value("Broadcast", b_name, "status", STATUS_SCHEDULED)

		# Omit locked WhatsApp row from payload
		update_payload = {
			"channels": [
				{
					"channel": CHANNEL_EMAIL,
					"email_account": self.email_account,
					"email_template": self.email_template,
					"scheduled_time": str(future_time),
				},
			],
		}
		with self.assertRaises(frappe.ValidationError) as ctx:
			save_broadcast(doc=json.dumps(update_payload), name=b_name)
		self.assertIn("Cannot remove channel WhatsApp because it is in status Completed.", str(ctx.exception))

	def test_save_removes_omitted_draft_row(self) -> None:
		"""Validate save_broadcast removes an omitted Draft row.

		Params:
			None.

		Returns:
			None.

		Raises:
			AssertionError: If omitted Draft row is not removed.

		Side effects:
			Inserts and updates Broadcast and child rows.
		"""
		payload = {
			"title": f"{TEST_PREFIX}RemoveDraft",
			"recipient_source": "Individual",
			"channels": [
				{
					"channel": CHANNEL_WHATSAPP,
					"whatsapp_account": self.wa_account,
					"whatsapp_template": self.wa_template,
				},
				{
					"channel": CHANNEL_EMAIL,
					"email_account": self.email_account,
					"email_template": self.email_template,
				},
			],
		}
		res = save_broadcast(doc=json.dumps(payload))
		b_name = res["name"]
		self.cleanup_broadcasts.append(b_name)

		# Omit Email row
		update_payload = {
			"channels": [
				{
					"channel": CHANNEL_WHATSAPP,
					"whatsapp_account": self.wa_account,
					"whatsapp_template": self.wa_template,
				},
			],
		}
		save_broadcast(doc=json.dumps(update_payload), name=b_name)

		doc = frappe.get_doc("Broadcast", b_name)
		self.assertEqual(len(doc.channels), 1)
		self.assertEqual(doc.channels[0].channel, CHANNEL_WHATSAPP)
		self.assertIsNone(doc.channel_row(CHANNEL_EMAIL))

	def test_save_adds_new_channel_as_draft(self) -> None:
		"""Validate save_broadcast adds a new channel with status Draft and zeroed counters.

		Params:
			None.

		Returns:
			None.

		Raises:
			AssertionError: If added channel is not Draft or counters are non-zero.

		Side effects:
			Inserts and updates Broadcast and child rows.
		"""
		payload = {
			"title": f"{TEST_PREFIX}AddNewChannel",
			"recipient_source": "Individual",
			"channels": [
				{
					"channel": CHANNEL_WHATSAPP,
					"whatsapp_account": self.wa_account,
					"whatsapp_template": self.wa_template,
				},
			],
		}
		res = save_broadcast(doc=json.dumps(payload))
		b_name = res["name"]
		self.cleanup_broadcasts.append(b_name)

		# Add Email channel
		update_payload = {
			"channels": [
				{
					"channel": CHANNEL_WHATSAPP,
					"whatsapp_account": self.wa_account,
					"whatsapp_template": self.wa_template,
				},
				{
					"channel": CHANNEL_EMAIL,
					"email_account": self.email_account,
					"email_template": self.email_template,
					"status": STATUS_COMPLETED,
					"sent_count": 100,
				},
			],
		}
		save_broadcast(doc=json.dumps(update_payload), name=b_name)

		doc = frappe.get_doc("Broadcast", b_name)
		self.assertEqual(len(doc.channels), 2)
		em_row = doc.channel_row(CHANNEL_EMAIL)
		self.assertIsNotNone(em_row)
		self.assertEqual(em_row.status, STATUS_DRAFT)
		self.assertEqual(em_row.sent_count, 0)


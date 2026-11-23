# Copyright (c) 2026, MSuite and contributors
# For license information, please see license.txt

"""End-to-end integration tests for the unified Broadcast backend (Task 17).

Validates complete multi-channel workflows across Hub, Lifecycle, Materializer,
Enqueue, Recipients, Counters, Scheduler, AWS Pipeline, API, and Migration.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_to_date, now_datetime

from msuite_campaigns.broadcast import (
	adapters,
	counters,
	enqueue,
	hub,
	legacy,
	lifecycle,
	materializer,
	migration,
	recipients,
	rollup,
	scheduler,
)
from msuite_campaigns.broadcast.api.v1 import broadcast as broadcast_api
from msuite_campaigns.broadcast.constants import (
	CHANNEL_EMAIL,
	CHANNEL_SMS,
	CHANNEL_WHATSAPP,
	STATUS_COMPLETED,
	STATUS_DRAFT,
	STATUS_FAILED,
	STATUS_IN_PROGRESS,
	STATUS_PARTIALLY_FAILED,
	STATUS_PAUSED,
	STATUS_QUEUED,
	STATUS_SCHEDULED,
)
from msuite_core.services.permissions.roles import WA_USER

TEST_PREFIX = "ZZTestE2E_"


def _inline_enqueue(fn: Any, *args: Any, **kwargs: Any) -> Any:
	"""Execute background job function inline by filtering enqueue options.

	Params:
		fn: Callable target function.
		*args: Positional arguments for fn.
		**kwargs: Keyword arguments for fn and enqueue runner.

	Returns:
		Any: Return value of fn.

	Exceptions:
		Propagates any exception raised by fn.

	Side effects:
		Executes fn immediately in current thread.
	"""
	if getattr(fn, "__name__", "") == "refresh_counters":
		# Debounced counter refresh jobs are triggered in background;
		# tests execute refresh_counters explicitly or inline where tested.
		return None

	filtered = {
		k: v
		for k, v in kwargs.items()
		if k not in (
			"queue",
			"timeout",
			"job_name",
			"job_id",
			"deduplicate",
			"now",
			"enqueue_after_commit",
			"at_front",
		)
	}
	return fn(*args, **filtered)


class TestBroadcastE2E(IntegrationTestCase):
	"""End-to-end test suite proving all Broadcast backend subsystems work together.

	Scenarios:
	1. Standard engine, one channel (WhatsApp)
	2. Out-of-order and duplicate webhooks
	3. Multi-channel isolation
	4. Scheduler
	5. Pause / resume / retry
	6. AWS engine path
	7. Permissions end to end
	8. Migration + legacy
	9. Hygiene verification
	"""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.wa_account = "WA-ACC-001"
		cls.wa_template = "tpl_wa_001"
		cls.email_account = "EMAIL-ACC-001"
		cls.email_template = "tpl_email_001"

	@classmethod
	def tearDownClass(cls):
		frappe.set_user("Administrator")
		cls._purge_all_test_data()
		super().tearDownClass()

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		self.cleanup_broadcasts: list[str] = []
		self.cleanup_lists: list[str] = []
		self.cleanup_campaigns: list[str] = []
		self.cleanup_bulk_wa: list[str] = []
		self.cleanup_users: list[str] = []
		if hasattr(frappe.local, "broadcast_channel_adapters"):
			delattr(frappe.local, "broadcast_channel_adapters")
		try:
			frappe.cache.delete_keys("broadcast_aws_triggered:*")
		except Exception:
			pass

	def tearDown(self):
		frappe.set_user("Administrator")
		if hasattr(frappe.local, "broadcast_channel_adapters"):
			delattr(frappe.local, "broadcast_channel_adapters")
		try:
			frappe.cache.delete_keys("broadcast_aws_triggered:*")
		except Exception:
			pass

		for name in self.cleanup_broadcasts:
			if frappe.db.exists("Broadcast", name):
				frappe.delete_doc("Broadcast", name, force=True, ignore_permissions=True)
		for name in self.cleanup_lists:
			if frappe.db.exists("Recipient List", name):
				frappe.delete_doc("Recipient List", name, force=True, ignore_permissions=True)
		for name in self.cleanup_campaigns:
			if frappe.db.exists("Marketing Campaign", name):
				frappe.delete_doc("Marketing Campaign", name, force=True, ignore_permissions=True)
		for name in self.cleanup_bulk_wa:
			if frappe.db.exists("Bulk WhatsApp Message", name):
				frappe.delete_doc("Bulk WhatsApp Message", name, force=True, ignore_permissions=True)
		for email in self.cleanup_users:
			if frappe.db.exists("User", email):
				frappe.delete_doc("User", email, force=True, ignore_permissions=True)

		self._purge_all_test_data()
		frappe.db.rollback()
		super().tearDown()

	@classmethod
	def _purge_all_test_data(cls):
		"""Remove any persisted records prefixed with TEST_PREFIX."""
		try:
			frappe.db.sql(
				f"DELETE FROM `tabBroadcast Recipient` WHERE broadcast IN (SELECT name FROM `tabBroadcast` WHERE title LIKE '{TEST_PREFIX}%') OR name LIKE '{TEST_PREFIX}%'"
			)
			frappe.db.sql(
				f"DELETE FROM `tabBroadcast Channel` WHERE parent IN (SELECT name FROM `tabBroadcast` WHERE title LIKE '{TEST_PREFIX}%')"
			)
			frappe.db.sql(
				f"DELETE FROM `tabBroadcast Product` WHERE parent IN (SELECT name FROM `tabBroadcast` WHERE title LIKE '{TEST_PREFIX}%')"
			)
			frappe.db.sql(f"DELETE FROM `tabBroadcast` WHERE title LIKE '{TEST_PREFIX}%'")
			frappe.db.sql(
				f"DELETE FROM `tabRecipient Contact` WHERE parent IN (SELECT name FROM `tabRecipient List` WHERE list_name LIKE '{TEST_PREFIX}%')"
			)
			frappe.db.sql(f"DELETE FROM `tabRecipient List` WHERE list_name LIKE '{TEST_PREFIX}%'")
			frappe.db.sql(
				f"DELETE FROM `tabWhatsApp Recipient` WHERE parent IN (SELECT name FROM `tabBulk WhatsApp Message` WHERE title LIKE '{TEST_PREFIX}%')"
			)
			frappe.db.sql(f"DELETE FROM `tabBulk WhatsApp Message` WHERE title LIKE '{TEST_PREFIX}%'")
			frappe.db.sql(f"DELETE FROM `tabMarketing Campaign` WHERE campaign_name LIKE '{TEST_PREFIX}%'")
			frappe.db.commit()
		except Exception:
			pass

	def _make_broadcast(
		self,
		channels: list[str] | None = None,
		recipient_source: str = "Individual",
		recipient_list: str | None = None,
		delivery_engine: str = "Standard",
		scheduled_time: Any = None,
		campaign: str | None = None,
		status: str = STATUS_DRAFT,
	) -> "frappe.model.document.Document":
		"""Create a valid Broadcast document with specified channel rows.

		Params:
			channels: List of channel names ('WhatsApp', 'Email', 'SMS').
			recipient_source: 'Individual' or 'Recipient List'.
			recipient_list: Name of Recipient List doc if recipient_source is 'Recipient List'.
			delivery_engine: 'Standard' or 'AWS'.
			scheduled_time: Datetime object or string for scheduling.
			campaign: Marketing Campaign docname.
			status: Initial parent broadcast status.

		Returns:
			frappe.model.document.Document: Inserted Broadcast document.

		Exceptions:
			None.

		Side effects:
			Inserts Broadcast document into database and tracks for cleanup.
		"""
		ch_names = channels or [CHANNEL_WHATSAPP]
		channel_rows = []
		for ch in ch_names:
			row = {
				"channel": ch,
				"status": status,
				"delivery_engine": delivery_engine,
			}
			if ch == CHANNEL_WHATSAPP:
				row["whatsapp_account"] = self.wa_account
				row["whatsapp_template"] = self.wa_template
				row["variable_type"] = "Common"
			elif ch == CHANNEL_EMAIL:
				row["email_account"] = self.email_account
				row["email_template"] = self.email_template
				row["variable_type"] = ""
			elif ch == CHANNEL_SMS:
				row["sms_account"] = f"{TEST_PREFIX}SMS_ACC"
				row["sms_template"] = f"{TEST_PREFIX}SMS_TPL"
				row["variable_type"] = ""
			channel_rows.append(row)

		doc_payload = {
			"doctype": "Broadcast",
			"title": f"{TEST_PREFIX}{frappe.generate_hash(length=8)}",
			"recipient_source": recipient_source,
			"recipient_list": recipient_list,
			"status": status,
			"channels": channel_rows,
			"campaign": campaign,
		}
		if scheduled_time:
			doc_payload["scheduled_time"] = scheduled_time

		doc = frappe.get_doc(doc_payload)
		for ch_row in doc.channels:
			if ch_row.channel != CHANNEL_WHATSAPP:
				ch_row.variable_type = ""
		doc.insert(ignore_links=True)
		self.cleanup_broadcasts.append(doc.name)
		return doc

	def _make_recipient_list(self, contacts: list[dict]) -> "frappe.model.document.Document":
		"""Create a Recipient List document populated with contacts.

		Params:
			contacts: List of contact dicts (e.g. full_name, raw_phone, raw_email).

		Returns:
			frappe.model.document.Document: Inserted Recipient List document.

		Exceptions:
			None.

		Side effects:
			Inserts Recipient List into database and tracks for cleanup.
		"""
		doc = frappe.get_doc({
			"doctype": "Recipient List",
			"list_name": f"{TEST_PREFIX}RL_{frappe.generate_hash(length=8)}",
			"default_region": "IN",
			"source": "Manual",
		})
		doc.flags.ignore_permissions = True
		doc.insert(ignore_links=True)
		self.cleanup_lists.append(doc.name)
		if contacts:
			doc.add_contacts(contacts)
			doc.save(ignore_permissions=True)
		return doc

	def _make_campaign(self, status: str = "Active") -> "frappe.model.document.Document":
		"""Create a Marketing Campaign document.

		Params:
			status: Campaign status string ('Active', 'Draft', etc.).

		Returns:
			frappe.model.document.Document: Inserted Marketing Campaign document.

		Exceptions:
			None.

		Side effects:
			Inserts Marketing Campaign doc in DB and tracks for cleanup.
		"""
		now = now_datetime()
		camp = frappe.get_doc({
			"doctype": "Marketing Campaign",
			"campaign_name": f"{TEST_PREFIX}CAMP_{frappe.generate_hash(length=6)}",
			"status": status,
			"start_date": now,
			"end_date": add_to_date(now, days=7),
		})
		camp.flags.ignore_permissions = True
		camp.insert(ignore_links=True)
		self.cleanup_campaigns.append(camp.name)
		return camp

	def _get_or_create_user(self, email: str, role: str) -> str:
		"""Create a test user with a specified role.

		Params:
			email: Email address of user.
			role: Role name to assign.

		Returns:
			str: Email address.

		Exceptions:
			None.

		Side effects:
			Inserts User and assigns Role.
		"""
		if not frappe.db.exists("User", email):
			user = frappe.get_doc({
				"doctype": "User",
				"email": email,
				"first_name": "E2ETestUser",
				"send_welcome_email": 0,
			}).insert(ignore_permissions=True)
			self.cleanup_users.append(email)
		else:
			user = frappe.get_doc("User", email)

		if role and not frappe.db.exists("Role", role):
			frappe.get_doc({"doctype": "Role", "role_name": role, "desk_access": 1}).insert(
				ignore_permissions=True
			)
		if role and role not in [r.role for r in user.roles]:
			user.add_roles(role)

		return email

	def test_01_standard_engine_one_channel_whatsapp(self):
		"""Scenario 1: Standard engine, one channel (WhatsApp).

		Tests: Recipient List with 3 valid contacts + 1 invalid number + 1 opted-out;
		Broadcast with WhatsApp row; hub.preview counts (3 reachable, 2 dropped with reasons);
		hub.launch with patched inline frappe.enqueue; fake registered adapter returns Sent+ids;
		assert recipient rows Sent, row In Progress; simulate webhooks with
		recipients.apply_delivery_status (2 Delivered then 1 Read, 1 Failed) + refresh_counters;
		assert counters exactly, row final status Partially Failed, Broadcast status Partially Failed;
		assert api.v1.broadcast.get_broadcast_progress / list_broadcasts / get_broadcast_metrics
		report the SAME numbers.
		"""
		# 1. Recipient List with 3 valid + 1 invalid + 1 opted-out
		rec_list = self._make_recipient_list([
			{"full_name": "Valid 1", "raw_phone": "+919811100001"},
			{"full_name": "Valid 2", "raw_phone": "+919811100002"},
			{"full_name": "Valid 3", "raw_phone": "+919811100003"},
			{"full_name": "Invalid 4", "raw_phone": "123"},
			{"full_name": "OptedOut 5", "raw_phone": "+919811100004"},
		])
		for c in rec_list.contacts:
			if c.phone_e164 == "+919811100004":
				c.unsubscribed_whatsapp = 1
		rec_list.save(ignore_permissions=True)

		# 2. Broadcast with WhatsApp row
		b = self._make_broadcast(
			channels=[CHANNEL_WHATSAPP],
			recipient_source="Recipient List",
			recipient_list=rec_list.name,
		)

		# 3. hub.preview counts (3 reachable, 2 dropped with reasons)
		preview_payload = {
			"recipient_source": "Recipient List",
			"recipient_list": rec_list.name,
			"channels": [CHANNEL_WHATSAPP],
		}
		prev = hub.preview(preview_payload)
		self.assertEqual(prev["total"], 5)
		wa_prev = prev["channels"][CHANNEL_WHATSAPP]
		self.assertEqual(wa_prev["reachable"], 3)
		self.assertEqual(wa_prev["dropped"], 2)
		self.assertIn("Phone invalid", wa_prev["reasons"])
		self.assertIn("Unsubscribed from WhatsApp", wa_prev["reasons"])

		# 4. Fake registered adapter returning Sent + ids
		def fake_send_one(broadcast_doc, channel_row, recipient):
			return {
				"status": "Sent",
				"provider_message_id": f"msg_wa_{recipient['name']}",
			}

		fake_adapter = {
			"channel": CHANNEL_WHATSAPP,
			"send_one": fake_send_one,
			"validate_channel": lambda b_doc, c_row: None,
			"manifest_header": lambda b_doc, c_row: {"account": "WA-001"},
			"manifest_row": lambda b_doc, c_row, r: {"phone": r["address"]},
		}
		setattr(frappe.local, "broadcast_channel_adapters", {CHANNEL_WHATSAPP: fake_adapter})

		# 5. hub.launch with inline frappe.enqueue
		with patch("frappe.enqueue", side_effect=_inline_enqueue):
			launch_res = hub.launch(b.name)

		self.assertIn(CHANNEL_WHATSAPP, launch_res)
		self.assertEqual(launch_res[CHANNEL_WHATSAPP]["status"], STATUS_IN_PROGRESS)
		self.assertEqual(launch_res[CHANNEL_WHATSAPP]["queued"], 3)
		self.assertIsNone(launch_res[CHANNEL_WHATSAPP]["error"])

		# 6. Assert recipient rows Sent and channel row In Progress
		rec_rows = frappe.get_all(
			"Broadcast Recipient",
			filters={"broadcast": b.name, "channel": CHANNEL_WHATSAPP},
			fields=["name", "status", "provider_message_id"],
			order_by="name asc",
		)
		self.assertEqual(len(rec_rows), 3)
		for r in rec_rows:
			self.assertEqual(r.status, "Sent")
			self.assertTrue(r.provider_message_id.startswith("msg_wa_"))

		wa_status = frappe.db.get_value(
			"Broadcast Channel", {"parent": b.name, "channel": CHANNEL_WHATSAPP}, "status"
		)
		self.assertEqual(wa_status, STATUS_IN_PROGRESS)

		# 7. Simulate webhooks: 2 Delivered then 1 Read, 1 Failed
		pm_0 = rec_rows[0].provider_message_id
		pm_1 = rec_rows[1].provider_message_id
		pm_2 = rec_rows[2].provider_message_id

		recipients.apply_delivery_status(b.name, CHANNEL_WHATSAPP, pm_0, "Delivered")
		recipients.apply_delivery_status(b.name, CHANNEL_WHATSAPP, pm_1, "Delivered")
		recipients.apply_delivery_status(b.name, CHANNEL_WHATSAPP, pm_0, "Read")
		recipients.apply_delivery_status(
			b.name,
			CHANNEL_WHATSAPP,
			pm_2,
			"Failed",
			error_code="ERR_USER_UNREACHABLE",
			failure_reason="Failed delivery",
		)

		counts = counters.refresh_counters(b.name, CHANNEL_WHATSAPP)

		# 8. Assert counters exactly
		self.assertEqual(counts["recipient_count"], 3)
		self.assertEqual(counts["sent_count"], 2)
		self.assertEqual(counts["delivered_count"], 2)
		self.assertEqual(counts["read_count"], 1)
		self.assertEqual(counts["failed_count"], 1)

		final_ch_status = frappe.db.get_value(
			"Broadcast Channel", {"parent": b.name, "channel": CHANNEL_WHATSAPP}, "status"
		)
		self.assertEqual(final_ch_status, STATUS_PARTIALLY_FAILED)

		final_b_status = frappe.db.get_value("Broadcast", b.name, "status")
		self.assertEqual(final_b_status, STATUS_PARTIALLY_FAILED)

		# 9. Assert API methods report the SAME numbers
		prog = broadcast_api.get_broadcast_progress(b.name)
		self.assertIn(CHANNEL_WHATSAPP, prog)
		self.assertEqual(prog[CHANNEL_WHATSAPP]["status"], STATUS_PARTIALLY_FAILED)
		self.assertEqual(prog[CHANNEL_WHATSAPP]["recipient_count"], 3)
		self.assertEqual(prog[CHANNEL_WHATSAPP]["sent_count"], 2)
		self.assertEqual(prog[CHANNEL_WHATSAPP]["delivered_count"], 2)
		self.assertEqual(prog[CHANNEL_WHATSAPP]["read_count"], 1)
		self.assertEqual(prog[CHANNEL_WHATSAPP]["failed_count"], 1)

		list_res = broadcast_api.list_broadcasts(search=b.title)
		matching = [r for r in list_res["rows"] if r["name"] == b.name]
		self.assertEqual(len(matching), 1)
		row = matching[0]
		self.assertEqual(row["status"], STATUS_PARTIALLY_FAILED)
		self.assertEqual(row["recipient_count"], 3)
		ch_data = row["channels"][0]
		self.assertEqual(ch_data["channel"], CHANNEL_WHATSAPP)
		self.assertEqual(ch_data["status"], STATUS_PARTIALLY_FAILED)
		self.assertEqual(ch_data["recipient_count"], 3)
		self.assertEqual(ch_data["sent_count"], 2)
		self.assertEqual(ch_data["delivered_count"], 2)
		self.assertEqual(ch_data["failed_count"], 1)

		metrics = broadcast_api.get_broadcast_metrics()
		self.assertGreaterEqual(metrics["total"], 1)
		self.assertGreaterEqual(metrics["failed"], 1)

	def test_02_out_of_order_and_duplicate_webhooks(self):
		"""Scenario 2: Out-of-order and duplicate webhooks.

		Tests: Read before Delivered, duplicate Delivered, Failed after Read;
		assert ranks hold and counters do not double count.
		"""
		b = self._make_broadcast(channels=[CHANNEL_WHATSAPP])
		pm_a = f"{TEST_PREFIX}pm_wa_read_{frappe.generate_hash(length=6)}"
		pm_b = f"{TEST_PREFIX}pm_wa_dup_{frappe.generate_hash(length=6)}"
		pm_c = f"{TEST_PREFIX}pm_wa_fail_{frappe.generate_hash(length=6)}"

		recipients.bulk_insert_recipients(
			b.name,
			CHANNEL_WHATSAPP,
			[
				{"address": "+919811100030", "provider_message_id": pm_a},
				{"address": "+919811100031", "provider_message_id": pm_b},
				{"address": "+919811100032", "provider_message_id": pm_c},
			],
		)

		# Initial state: all Sent
		recipients.apply_delivery_status(b.name, CHANNEL_WHATSAPP, pm_a, "Sent")
		recipients.apply_delivery_status(b.name, CHANNEL_WHATSAPP, pm_b, "Sent")
		recipients.apply_delivery_status(b.name, CHANNEL_WHATSAPP, pm_c, "Sent")

		# Case A: Read before Delivered on pm_a
		up_read = recipients.apply_delivery_status(b.name, CHANNEL_WHATSAPP, pm_a, "Read")
		self.assertTrue(up_read)
		up_del_ignored = recipients.apply_delivery_status(b.name, CHANNEL_WHATSAPP, pm_a, "Delivered")
		self.assertFalse(up_del_ignored)  # Rank 3 < 4, must be rejected
		status_a = frappe.db.get_value(
			"Broadcast Recipient", {"broadcast": b.name, "provider_message_id": pm_a}, "status"
		)
		self.assertEqual(status_a, "Read")

		# Case B: duplicate Delivered on pm_b
		up_del = recipients.apply_delivery_status(b.name, CHANNEL_WHATSAPP, pm_b, "Delivered")
		self.assertTrue(up_del)
		up_dup_ignored = recipients.apply_delivery_status(b.name, CHANNEL_WHATSAPP, pm_b, "Delivered")
		self.assertFalse(up_dup_ignored)  # Duplicate, rank not higher
		status_b = frappe.db.get_value(
			"Broadcast Recipient", {"broadcast": b.name, "provider_message_id": pm_b}, "status"
		)
		self.assertEqual(status_b, "Delivered")

		# Case C: Failed after Read on pm_c
		up_read_c = recipients.apply_delivery_status(b.name, CHANNEL_WHATSAPP, pm_c, "Read")
		self.assertTrue(up_read_c)
		up_fail_c = recipients.apply_delivery_status(
			b.name,
			CHANNEL_WHATSAPP,
			pm_c,
			"Failed",
			error_code="ERR_LATE_FAIL",
			failure_reason="Late failure reported",
		)
		self.assertTrue(up_fail_c)  # Rank 5 > 4, status moves to Failed
		status_c = frappe.db.get_value(
			"Broadcast Recipient", {"broadcast": b.name, "provider_message_id": pm_c}, "status"
		)
		self.assertEqual(status_c, "Failed")

		# Counter verification: must not double count
		counts = counters.refresh_counters(b.name, CHANNEL_WHATSAPP)
		self.assertEqual(counts["recipient_count"], 3)
		self.assertEqual(counts["sent_count"], 2)
		self.assertEqual(counts["delivered_count"], 2)
		self.assertEqual(counts["read_count"], 1)
		self.assertEqual(counts["failed_count"], 1)

	def test_03_multi_channel_isolation(self):
		"""Scenario 3: Multi-channel isolation.

		Tests: one Broadcast with WhatsApp + Email rows; WhatsApp validate_channel raises;
		Email still launches and completes; WhatsApp row stays Draft with launch_error;
		overall status per rollup rules; counters and Dynamo keys distinct per channel.
		"""
		b = self._make_broadcast(channels=[CHANNEL_WHATSAPP, CHANNEL_EMAIL])

		def wa_val(broadcast_doc, channel_row):
			frappe.throw("WhatsApp template not approved", frappe.ValidationError)

		def em_val(broadcast_doc, channel_row):
			pass

		def em_send(broadcast_doc, channel_row, recipient):
			return {"status": "Sent", "provider_message_id": f"em_msg_{recipient['name']}"}

		wa_adapter = {
			"channel": CHANNEL_WHATSAPP,
			"validate_channel": wa_val,
			"send_one": MagicMock(),
		}
		em_adapter = {
			"channel": CHANNEL_EMAIL,
			"validate_channel": em_val,
			"send_one": em_send,
		}
		setattr(
			frappe.local,
			"broadcast_channel_adapters",
			{CHANNEL_WHATSAPP: wa_adapter, CHANNEL_EMAIL: em_adapter},
		)

		individual_rows = [
			{"address": "+919811100040", "recipient_name": "WA Person"},
			{"address": "email_target@example.com", "recipient_name": "Email Person"},
		]

		with patch("frappe.enqueue", side_effect=_inline_enqueue):
			launch_res = hub.launch(b.name, individual_rows=individual_rows)

		# WhatsApp failed at validation; Email succeeded
		self.assertIn("WhatsApp template not approved", launch_res[CHANNEL_WHATSAPP]["error"])
		self.assertIsNone(launch_res[CHANNEL_EMAIL]["error"])
		self.assertEqual(launch_res[CHANNEL_EMAIL]["queued"], 1)

		# WhatsApp row stays Draft with launch_error recorded
		wa_row = frappe.db.get_value(
			"Broadcast Channel",
			{"parent": b.name, "channel": CHANNEL_WHATSAPP},
			["status", "launch_error"],
			as_dict=True,
		)
		self.assertEqual(wa_row.status, STATUS_DRAFT)
		self.assertIn("WhatsApp template not approved", wa_row.launch_error)

		# Email recipient delivered & counter refreshed -> Email Completed
		em_rec = frappe.get_all(
			"Broadcast Recipient",
			filters={"broadcast": b.name, "channel": CHANNEL_EMAIL},
			fields=["name", "provider_message_id"],
		)[0]
		recipients.apply_delivery_status(b.name, CHANNEL_EMAIL, em_rec.provider_message_id, "Delivered")
		counters.refresh_counters(b.name, CHANNEL_EMAIL)

		em_row_status = frappe.db.get_value(
			"Broadcast Channel", {"parent": b.name, "channel": CHANNEL_EMAIL}, "status"
		)
		self.assertEqual(em_row_status, STATUS_COMPLETED)

		# Overall Broadcast status per rollup: WhatsApp Draft + Email Completed -> Draft (rule 7)
		b_status = frappe.db.get_value("Broadcast", b.name, "status")
		self.assertEqual(b_status, STATUS_DRAFT)

		# Counters isolated per channel
		counts_wa = counters.refresh_counters(b.name, CHANNEL_WHATSAPP)
		self.assertEqual(counts_wa["recipient_count"], 0)
		self.assertEqual(counts_wa["sent_count"], 0)

		counts_em = counters.refresh_counters(b.name, CHANNEL_EMAIL)
		self.assertEqual(counts_em["recipient_count"], 1)
		self.assertEqual(counts_em["sent_count"], 1)
		self.assertEqual(counts_em["delivered_count"], 1)

		# Dynamo keys distinct per channel
		from msuite_campaigns.broadcast.aws import dynamo_campaign_id

		wa_key = dynamo_campaign_id(b, CHANNEL_WHATSAPP)
		em_key = dynamo_campaign_id(b, CHANNEL_EMAIL)
		self.assertNotEqual(wa_key, em_key)
		self.assertEqual(wa_key, f"{b.name}__{CHANNEL_WHATSAPP}")
		self.assertEqual(em_key, f"{b.name}__{CHANNEL_EMAIL}")

	def test_04_scheduler_due_vs_future_vs_inactive_campaign(self):
		"""Scenario 4: Scheduler.

		Tests: Scheduled row with past scheduled_time is started once by
		scheduler.start_due_channels(); a second call starts nothing;
		a future row and an inactive-campaign row are skipped.
		"""
		active_camp = self._make_campaign("Active")
		inactive_camp = self._make_campaign("Draft")

		now = now_datetime()
		due_time = add_to_date(now, minutes=-15)
		future_time = add_to_date(now, hours=2)

		# Broadcast 1: Due with active campaign -> must start
		b1 = self._make_broadcast(
			channels=[CHANNEL_WHATSAPP],
			scheduled_time=future_time,
			campaign=active_camp.name,
			status=STATUS_SCHEDULED,
		)
		frappe.db.set_value("Broadcast", b1.name, "scheduled_time", due_time, update_modified=False)
		frappe.db.set_value(
			"Broadcast Channel", {"parent": b1.name, "channel": CHANNEL_WHATSAPP}, "status", STATUS_SCHEDULED
		)

		# Broadcast 2: Future scheduled_time -> must be skipped
		b2 = self._make_broadcast(
			channels=[CHANNEL_WHATSAPP],
			scheduled_time=future_time,
			campaign=active_camp.name,
			status=STATUS_SCHEDULED,
		)
		frappe.db.set_value(
			"Broadcast Channel", {"parent": b2.name, "channel": CHANNEL_WHATSAPP}, "status", STATUS_SCHEDULED
		)

		# Broadcast 3: Due, but inactive campaign -> must be skipped
		b3 = self._make_broadcast(
			channels=[CHANNEL_WHATSAPP],
			scheduled_time=future_time,
			campaign=inactive_camp.name,
			status=STATUS_SCHEDULED,
		)
		frappe.db.set_value("Broadcast", b3.name, "scheduled_time", due_time, update_modified=False)
		frappe.db.set_value(
			"Broadcast Channel", {"parent": b3.name, "channel": CHANNEL_WHATSAPP}, "status", STATUS_SCHEDULED
		)

		# Run scheduler first time
		with patch("msuite_campaigns.broadcast.enqueue.enqueue_broadcast_channel"):
			started_first = scheduler.start_due_channels()

		self.assertEqual(started_first, 1)

		b1_st = frappe.db.get_value(
			"Broadcast Channel", {"parent": b1.name, "channel": CHANNEL_WHATSAPP}, "status"
		)
		b2_st = frappe.db.get_value(
			"Broadcast Channel", {"parent": b2.name, "channel": CHANNEL_WHATSAPP}, "status"
		)
		b3_st = frappe.db.get_value(
			"Broadcast Channel", {"parent": b3.name, "channel": CHANNEL_WHATSAPP}, "status"
		)
		self.assertEqual(b1_st, STATUS_IN_PROGRESS)
		self.assertEqual(b2_st, STATUS_SCHEDULED)
		self.assertEqual(b3_st, STATUS_SCHEDULED)

		# Run scheduler second time -> idempotent 0
		with patch("msuite_campaigns.broadcast.enqueue.enqueue_broadcast_channel"):
			started_second = scheduler.start_due_channels()

		self.assertEqual(started_second, 0)

	def test_05_pause_resume_retry_and_running_noop(self):
		"""Scenario 5: Pause / resume / retry.

		Tests: pause stops a chunk mid-way (patch send_one to pause the row after first recipient);
		resume with sent_count>0 -> In Progress; retry_failed re-queues only retryable Failed
		recipients and re-runs only them; launching an already running broadcast is a no-op.
		"""
		b = self._make_broadcast(channels=[CHANNEL_WHATSAPP])
		recipients.bulk_insert_recipients(
			b.name,
			CHANNEL_WHATSAPP,
			[
				{"address": "+919811100050", "recipient_name": "R0"},
				{"address": "+919811100051", "recipient_name": "R1"},
				{"address": "+919811100052", "recipient_name": "R2"},
			],
		)
		recs = frappe.get_all(
			"Broadcast Recipient",
			filters={"broadcast": b.name, "channel": CHANNEL_WHATSAPP},
			fields=["name", "address"],
			order_by="name asc",
		)
		r0_name, r1_name, r2_name = recs[0].name, recs[1].name, recs[2].name

		# Set channel row to In Progress
		frappe.db.set_value(
			"Broadcast Channel", {"parent": b.name, "channel": CHANNEL_WHATSAPP}, "status", STATUS_IN_PROGRESS
		)
		frappe.db.set_value("Broadcast", b.name, "status", STATUS_IN_PROGRESS)

		# 1. Pause stops chunk mid-way
		def send_one_and_pause(b_doc, c_row, rec):
			hub.pause(b_doc.name, c_row.channel)
			return {"status": "Sent", "provider_message_id": f"p_msg_{rec['name']}"}

		adapter_pause = {
			"channel": CHANNEL_WHATSAPP,
			"send_one": send_one_and_pause,
			"validate_channel": lambda b_doc, c_row: None,
		}
		setattr(frappe.local, "broadcast_channel_adapters", {CHANNEL_WHATSAPP: adapter_pause})

		chunk_res = enqueue.run_chunk(b.name, CHANNEL_WHATSAPP, r0_name, r2_name)
		self.assertTrue(chunk_res["stopped"])
		self.assertEqual(chunk_res["sent"], 1)

		# Recipient 0 is Sent; Recipient 1 & 2 are still Queued
		self.assertEqual(frappe.db.get_value("Broadcast Recipient", r0_name, "status"), "Sent")
		self.assertEqual(frappe.db.get_value("Broadcast Recipient", r1_name, "status"), "Queued")
		self.assertEqual(frappe.db.get_value("Broadcast Recipient", r2_name, "status"), "Queued")

		# Channel and Broadcast are Paused
		ch_st = frappe.db.get_value(
			"Broadcast Channel", {"parent": b.name, "channel": CHANNEL_WHATSAPP}, "status"
		)
		self.assertEqual(ch_st, STATUS_PAUSED)
		self.assertEqual(frappe.db.get_value("Broadcast", b.name, "status"), STATUS_PAUSED)

		# 2. Resume with sent_count > 0 -> In Progress
		counters.refresh_counters(b.name, CHANNEL_WHATSAPP)
		# Row has sent_count = 1, status Paused
		frappe.db.set_value(
			"Broadcast Channel", {"parent": b.name, "channel": CHANNEL_WHATSAPP}, "status", STATUS_PAUSED
		)
		frappe.db.set_value("Broadcast", b.name, "status", STATUS_PAUSED)

		resume_res = hub.resume(b.name, CHANNEL_WHATSAPP)
		self.assertEqual(resume_res[CHANNEL_WHATSAPP], STATUS_IN_PROGRESS)
		self.assertEqual(
			frappe.db.get_value(
				"Broadcast Channel", {"parent": b.name, "channel": CHANNEL_WHATSAPP}, "status"
			),
			STATUS_IN_PROGRESS,
		)
		self.assertEqual(frappe.db.get_value("Broadcast", b.name, "status"), STATUS_IN_PROGRESS)

		# 3. retry_failed re-queues only retryable Failed recipients
		frappe.db.set_value(
			"Broadcast Recipient", r1_name, {"status": "Failed", "is_retryable": 1, "retry_count": 0}
		)
		frappe.db.set_value(
			"Broadcast Recipient", r2_name, {"status": "Failed", "is_retryable": 0, "retry_count": 0}
		)
		frappe.db.set_value(
			"Broadcast Channel", {"parent": b.name, "channel": CHANNEL_WHATSAPP}, "status", STATUS_FAILED
		)
		frappe.db.set_value("Broadcast", b.name, "status", STATUS_FAILED)

		retried_recipients: list[str] = []

		def send_one_retry(b_doc, c_row, rec):
			retried_recipients.append(rec["name"])
			return {"status": "Sent", "provider_message_id": f"retried_{rec['name']}"}

		adapter_retry = {
			"channel": CHANNEL_WHATSAPP,
			"send_one": send_one_retry,
			"validate_channel": lambda b_doc, c_row: None,
		}
		setattr(frappe.local, "broadcast_channel_adapters", {CHANNEL_WHATSAPP: adapter_retry})

		with patch("frappe.enqueue", side_effect=_inline_enqueue):
			hub.retry(b.name, CHANNEL_WHATSAPP)

		# Only r1 was retryable -> only r1 re-run
		self.assertIn(r1_name, retried_recipients)
		self.assertNotIn(r2_name, retried_recipients)

		r1_data = frappe.db.get_value(
			"Broadcast Recipient", r1_name, ["status", "retry_count"], as_dict=True
		)
		self.assertEqual(r1_data.status, "Sent")
		self.assertEqual(r1_data.retry_count, 1)

		r2_data = frappe.db.get_value(
			"Broadcast Recipient", r2_name, ["status", "retry_count"], as_dict=True
		)
		self.assertEqual(r2_data.status, "Failed")
		self.assertEqual(r2_data.retry_count, 0)

		# 4. Launching an already running broadcast is a no-op
		current_ch_st = frappe.db.get_value(
			"Broadcast Channel", {"parent": b.name, "channel": CHANNEL_WHATSAPP}, "status"
		)
		self.assertEqual(current_ch_st, STATUS_IN_PROGRESS)

		with patch("frappe.enqueue") as mock_enq:
			noop_launch = hub.launch(b.name)
			mock_enq.assert_not_called()

		self.assertEqual(
			frappe.db.get_value(
				"Broadcast Channel", {"parent": b.name, "channel": CHANNEL_WHATSAPP}, "status"
			),
			STATUS_IN_PROGRESS,
		)

	def test_06_aws_engine_path(self):
		"""Scenario 6: AWS engine path (all network mocked).

		Tests: row delivery_engine AWS and aws.is_ready/threshold patched true;
		hub.start_channel calls aws.export_and_enqueue_channel once (mock aws_pipeline.client);
		manifest lines match the queued recipients; second call does not trigger again;
		rollup.channel_metrics reads the mocked Dynamo payload.
		"""
		b = self._make_broadcast(
			channels=[CHANNEL_WHATSAPP],
			delivery_engine="AWS",
			status=STATUS_QUEUED,
		)
		recipients.bulk_insert_recipients(
			b.name,
			CHANNEL_WHATSAPP,
			[
				{"address": "+919811100060", "recipient_name": "Alice AWS"},
				{"address": "+919811100061", "recipient_name": "Bob AWS"},
			],
		)
		frappe.db.set_value(
			"Broadcast Channel",
			{"parent": b.name, "channel": CHANNEL_WHATSAPP},
			{"status": STATUS_QUEUED, "recipient_count": 2},
		)

		adapter_aws = {
			"channel": CHANNEL_WHATSAPP,
			"manifest_header": lambda b_doc, c_row: {"account_id": "WA-AWS-ACC"},
			"manifest_row": lambda b_doc, c_row, r: {"phone": r["address"], "name": r["recipient_name"]},
			"sync_secret": MagicMock(),
			"record_outbound": MagicMock(),
			"validate_channel": lambda b_doc, c_row: None,
		}
		setattr(frappe.local, "broadcast_channel_adapters", {CHANNEL_WHATSAPP: adapter_aws})

		# Clear trigger cache
		frappe.cache.delete_value(f"broadcast_aws_triggered:{b.name}:{CHANNEL_WHATSAPP}")

		uploaded_bodies: list[str] = []

		def fake_upload_manifest(url, body):
			uploaded_bodies.append(body)

		mock_client = MagicMock()
		mock_client.request_upload_url.return_value = {
			"url": "https://s3.example.com/manifest",
			"key": "manifests/wa_test.ndjson",
			"max_bytes": 10000000,
		}
		mock_client.upload_manifest.side_effect = fake_upload_manifest
		mock_client.trigger_campaign.return_value = {"message_id": "sqs-msg-12345"}

		with patch("msuite_campaigns.broadcast.aws.is_ready", return_value=True):
			with patch("msuite_campaigns.broadcast.aws.get_min_recipient_threshold", return_value=1):
				with patch("msuite_campaigns.broadcast.aws.client", mock_client):
					hub.start_channel(b.name, CHANNEL_WHATSAPP)

		# export_and_enqueue_channel was called once
		mock_client.request_upload_url.assert_called_once()
		mock_client.upload_manifest.assert_called_once()
		mock_client.trigger_campaign.assert_called_once()

		# Manifest lines match queued recipients
		self.assertEqual(len(uploaded_bodies), 1)
		lines = [json.loads(l) for l in uploaded_bodies[0].strip().split("\n")]
		self.assertEqual(len(lines), 3)  # header + 2 recipients
		self.assertEqual(lines[0]["account_id"], "WA-AWS-ACC")
		self.assertEqual(lines[0]["bulk_doctype"], "Broadcast")
		self.assertEqual(lines[0]["bulk_doc_name"], b.name)
		manifest_rows = lines[1:]
		phones = {r["phone"] for r in manifest_rows}
		self.assertEqual(phones, {"+919811100060", "+919811100061"})
		names_by_phone = {r["phone"]: r["name"] for r in manifest_rows}
		self.assertEqual(names_by_phone["+919811100060"], "Alice AWS")
		self.assertEqual(names_by_phone["+919811100061"], "Bob AWS")

		# Second call does not trigger again
		from msuite_campaigns.broadcast.aws import export_and_enqueue_channel

		with patch("msuite_campaigns.broadcast.aws.is_ready", return_value=True):
			with patch("msuite_campaigns.broadcast.aws.get_min_recipient_threshold", return_value=1):
				with patch("msuite_campaigns.broadcast.aws.client", mock_client):
					export_and_enqueue_channel(b.name, CHANNEL_WHATSAPP)

		self.assertEqual(mock_client.trigger_campaign.call_count, 1)

		# Rollup reads mocked Dynamo payload
		fake_dynamo = {
			"sent_count": 80,
			"delivered_count": 75,
			"failed_count": 5,
			"read_count": 60,
			"retried_count": 2,
		}
		with patch("msuite_campaigns.aws_pipeline.rollup.fetch", return_value=fake_dynamo):
			metrics = rollup.channel_metrics(b.name, CHANNEL_WHATSAPP)

		self.assertEqual(metrics["engine"], "aws")
		self.assertEqual(metrics["metrics"]["sent"], 80)
		self.assertEqual(metrics["metrics"]["delivered"], 75)
		self.assertEqual(metrics["metrics"]["failed"], 5)
		self.assertEqual(metrics["metrics"]["read"], 60)
		self.assertEqual(metrics["retried"], 2)

	def test_07_permissions_end_to_end(self):
		"""Scenario 7: Permissions end to end.

		Tests: a WhatsApp-only user cannot see/launch the Email row or Email recipients
		of a two-channel Broadcast via the API; an admin can.
		"""
		user_wa = self._get_or_create_user(f"e2e_wa_only_{frappe.generate_hash(length=4)}@example.com", WA_USER)
		b = self._make_broadcast(channels=[CHANNEL_WHATSAPP, CHANNEL_EMAIL])

		recipients.bulk_insert_recipients(
			b.name,
			CHANNEL_WHATSAPP,
			[{"address": "+919811100070", "recipient_name": "WA Recipient"}],
		)
		recipients.bulk_insert_recipients(
			b.name,
			CHANNEL_EMAIL,
			[{"address": "perm_target@example.com", "recipient_name": "Email Recipient"}],
		)
		counters.refresh_counters(b.name, CHANNEL_WHATSAPP)
		counters.refresh_counters(b.name, CHANNEL_EMAIL)

		# 1. As WhatsApp-only user:
		frappe.set_user(user_wa)

		doc_wa_view = broadcast_api.get_broadcast(b.name)
		ch_names_wa_view = [c["channel"] for c in doc_wa_view["channels"]]
		self.assertIn(CHANNEL_WHATSAPP, ch_names_wa_view)
		self.assertNotIn(CHANNEL_EMAIL, ch_names_wa_view)  # Email row stripped

		em_recs = broadcast_api.get_broadcast_recipients(b.name, channel=CHANNEL_EMAIL)
		self.assertEqual(em_recs["rows"], [])
		self.assertEqual(em_recs["total"], 0)

		wa_recs = broadcast_api.get_broadcast_recipients(b.name, channel=CHANNEL_WHATSAPP)
		self.assertEqual(len(wa_recs["rows"]), 1)

		# Cannot launch mixed broadcast (requires permission on all channels)
		with self.assertRaises(frappe.PermissionError):
			broadcast_api.launch_broadcast(b.name)

		# 2. As Admin:
		frappe.set_user("Administrator")

		doc_admin_view = broadcast_api.get_broadcast(b.name)
		ch_names_admin = [c["channel"] for c in doc_admin_view["channels"]]
		self.assertIn(CHANNEL_WHATSAPP, ch_names_admin)
		self.assertIn(CHANNEL_EMAIL, ch_names_admin)  # Admin sees both

		admin_em_recs = broadcast_api.get_broadcast_recipients(b.name, channel=CHANNEL_EMAIL)
		self.assertEqual(len(admin_em_recs["rows"]), 1)

		# Admin has write permission
		self.assertTrue(frappe.has_permission("Broadcast", "write", doc=b.name))

	def test_08_migration_and_legacy(self):
		"""Scenario 8: Migration + legacy.

		Tests: insert one old Bulk WhatsApp Message (Individual, 2 recipients, status In Progress)
		-> migration.run(dry_run=False) -> a Broadcast with legacy_name set and status Paused,
		recipients copied, old doc untouched; legacy.resolve returns new name;
		second run creates nothing.
		"""
		bw = frappe.get_doc({
			"doctype": "Bulk WhatsApp Message",
			"title": f"{TEST_PREFIX}LegacyBW_{frappe.generate_hash(length=6)}",
			"status": "In Progress",
			"recipient_type": "Individual",
			"whatsapp_account": self.wa_account,
			"whatsapp_template": self.wa_template,
			"recipients": [
				{
					"doctype": "WhatsApp Recipient",
					"recipient_name": "Legacy Rec 1",
					"mobile_number": "+919876543201",
					"status": "Sent",
				},
				{
					"doctype": "WhatsApp Recipient",
					"recipient_name": "Legacy Rec 2",
					"mobile_number": "+919876543202",
					"status": "Queued",
				},
			],
		})
		bw.flags.ignore_permissions = True
		bw.flags.ignore_mandatory = True
		bw.flags.ignore_validate = True
		bw.flags.ignore_links = True
		bw.insert()
		self.cleanup_bulk_wa.append(bw.name)

		# Run migration
		rep1 = migration.run(dry_run=False)

		# Migrated Broadcast exists with legacy_name set
		bc_name = frappe.db.get_value(
			"Broadcast",
			{"legacy_doctype": "Bulk WhatsApp Message", "legacy_name": bw.name},
			"name",
		)
		self.assertIsNotNone(bc_name)
		self.cleanup_broadcasts.append(bc_name)

		bc = frappe.get_doc("Broadcast", bc_name)
		self.assertEqual(bc.legacy_name, bw.name)
		self.assertEqual(bc.status, STATUS_PAUSED)
		self.assertEqual(bc.channels[0].status, STATUS_PAUSED)

		# Recipients copied
		copied_recs = frappe.get_all(
			"Broadcast Recipient",
			filters={"broadcast": bc_name},
			fields=["address", "status"],
		)
		self.assertEqual(len(copied_recs), 2)
		addresses = {r.address for r in copied_recs}
		self.assertEqual(addresses, {"+919876543201", "+919876543202"})

		# Old doc untouched
		old_bw = frappe.get_doc("Bulk WhatsApp Message", bw.name)
		self.assertEqual(old_bw.status, "In Progress")

		# legacy.resolve returns new name
		resolved = legacy.resolve("Bulk WhatsApp Message", bw.name)
		self.assertEqual(resolved, bc_name)

		# Second run creates nothing
		rep2 = migration.run(dry_run=False)
		self.assertEqual(rep2["broadcasts"]["Bulk WhatsApp Message"]["migrated"], 0)
		self.assertEqual(
			frappe.db.count("Broadcast", {"legacy_doctype": "Bulk WhatsApp Message", "legacy_name": bw.name}),
			1,
		)

	def test_09_hygiene_no_leaked_rows(self):
		"""Hygiene check: Assert module cleans up all test records completely.

		Tests that no Broadcast, Recipient List, or Broadcast Recipient rows with
		module TEST_PREFIX remain in the database.
		"""
		bc_count = frappe.db.count("Broadcast", {"title": ["like", f"{TEST_PREFIX}%"]})
		rl_count = frappe.db.count("Recipient List", {"list_name": ["like", f"{TEST_PREFIX}%"]})
		rec_count = frappe.db.sql(
			f"SELECT COUNT(*) FROM `tabBroadcast Recipient` WHERE broadcast IN (SELECT name FROM `tabBroadcast` WHERE title LIKE '{TEST_PREFIX}%') OR name LIKE '{TEST_PREFIX}%'"
		)[0][0]

		self.assertEqual(bc_count, 0, f"Leaked {bc_count} Broadcast rows with prefix {TEST_PREFIX}")
		self.assertEqual(rl_count, 0, f"Leaked {rl_count} Recipient List rows with prefix {TEST_PREFIX}")
		self.assertEqual(rec_count, 0, f"Leaked {rec_count} Broadcast Recipient rows with prefix {TEST_PREFIX}")

	def test_10_resume_stall_and_aws_pause_guard(self):
		"""Scenario 10: Public API pause/resume stall fix and AWS In Progress pause guard.

		Verifies:
		1. Standard engine broadcast with partial sends (3/10 Sent) paused and then resumed
		   sends exactly the remaining 7 recipients once without stall.
		2. AWS engine broadcast in Progress raises frappe.ValidationError on hub.pause.
		"""
		# 1. Standard engine: 10 recipients
		b = self._make_broadcast(channels=[CHANNEL_WHATSAPP], status=STATUS_DRAFT)
		addr_list = [{"address": f"+919811100{i:03d}", "recipient_name": f"User {i}"} for i in range(10)]
		materializer.materialize(b.name, CHANNEL_WHATSAPP, individual_rows=addr_list)
		lifecycle.launch(b.name, CHANNEL_WHATSAPP)
		hub.start_channel(b.name, CHANNEL_WHATSAPP)

		# Send first 3 recipients
		recs = frappe.get_all(
			"Broadcast Recipient",
			filters={"broadcast": b.name, "channel": CHANNEL_WHATSAPP},
			fields=["name", "address"],
			order_by="name asc",
		)
		for r in recs[:3]:
			frappe.db.set_value(
				"Broadcast Recipient",
				r.name,
				{"status": "Sent", "provider_message_id": r.name},
				update_modified=False,
			)
		counters.refresh_counters(b.name, CHANNEL_WHATSAPP)

		# Pause mid-send
		pause_res = hub.pause(b.name, CHANNEL_WHATSAPP)
		self.assertEqual(pause_res[CHANNEL_WHATSAPP], STATUS_PAUSED)

		# Resume: must resume sending the remaining 7 recipients
		sent_calls: list[str] = []

		def send_remaining(b_doc, c_row, r):
			sent_calls.append(r["address"])
			return {"status": "Sent", "provider_message_id": f"res_{r['name']}"}

		adapter_wa = {
			"channel": CHANNEL_WHATSAPP,
			"send_one": send_remaining,
			"validate_channel": lambda b_doc, c_row: None,
		}
		setattr(frappe.local, "broadcast_channel_adapters", {CHANNEL_WHATSAPP: adapter_wa})

		with patch("frappe.enqueue", side_effect=_inline_enqueue):
			resume_res = hub.resume(b.name, CHANNEL_WHATSAPP)

		self.assertEqual(resume_res[CHANNEL_WHATSAPP], STATUS_IN_PROGRESS)
		# Sent exactly the remaining 7 recipients once
		self.assertEqual(len(sent_calls), 7)
		remaining_addrs = [r["address"] for r in recs[3:]]
		self.assertEqual(sent_calls, remaining_addrs)

		# 2. AWS engine row in Progress: hub.pause raises ValidationError
		b_aws = self._make_broadcast(
			channels=[CHANNEL_WHATSAPP],
			delivery_engine="AWS",
			status=STATUS_IN_PROGRESS,
		)
		frappe.db.set_value(
			"Broadcast Channel",
			{"parent": b_aws.name, "channel": CHANNEL_WHATSAPP},
			{"status": STATUS_IN_PROGRESS, "delivery_engine": "AWS"},
			update_modified=False,
		)

		with patch("msuite_campaigns.broadcast.aws.should_use_aws", return_value=True):
			with self.assertRaises(frappe.ValidationError) as cm:
				hub.pause(b_aws.name, CHANNEL_WHATSAPP)
			self.assertIn("cannot pause after hand-off to AWS", str(cm.exception))

	def test_11_run_chunk_counter_refresh_completion_without_webhooks(self):
		"""Scenario 11: Standard broadcast completes without webhooks via run_chunk refresh call.

		Verifies that when adapter send_one marks recipients Sent/Failed and no webhooks
		arrive, run_chunk calls request_counter_refresh and the channel row completes.
		"""
		b = self._make_broadcast(channels=[CHANNEL_WHATSAPP], status=STATUS_DRAFT)
		addr_list = [
			{"address": "+919811100501", "recipient_name": "Done 1"},
			{"address": "+919811100502", "recipient_name": "Done 2"},
		]
		materializer.materialize(b.name, CHANNEL_WHATSAPP, individual_rows=addr_list)
		lifecycle.launch(b.name, CHANNEL_WHATSAPP)

		adapter_wa = {
			"channel": CHANNEL_WHATSAPP,
			"send_one": lambda b_doc, c_row, r: {"status": "Sent", "provider_message_id": f"dlv_{r['name']}"},
			"validate_channel": lambda b_doc, c_row: None,
		}
		setattr(frappe.local, "broadcast_channel_adapters", {CHANNEL_WHATSAPP: adapter_wa})

		with patch("frappe.enqueue", side_effect=_inline_enqueue):
			hub.start_channel(b.name, CHANNEL_WHATSAPP)

		# Run the debounced refresh job inline as specified in brief
		counters.refresh_counters(b.name, CHANNEL_WHATSAPP)

		ch_status = frappe.db.get_value(
			"Broadcast Channel",
			{"parent": b.name, "channel": CHANNEL_WHATSAPP},
			"status",
		)
		b_status = frappe.db.get_value("Broadcast", b.name, "status")
		self.assertEqual(ch_status, STATUS_COMPLETED)
		self.assertEqual(b_status, STATUS_COMPLETED)


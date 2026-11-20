# Copyright (c) 2026, MSuite and contributors
# For license information, please see license.txt

"""Tests for Broadcast whitelisted API (Task 12).

Validates all whitelisted methods across roles (WhatsApp User, Email User,
MSuite Viewer, Campaign Manager, Guest), pagination, filtering, injection safety,
capping, partial deletions, export truncation flag, and status guards.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, now_datetime, today

from msuite_campaigns.broadcast.api.v1 import broadcast as broadcast_api
from msuite_core.services.permissions.roles import (
	CAMPAIGN_MANAGER,
	EMAIL_USER,
	VIEWER,
	WA_USER,
)


def _user(role: str | None) -> str:
	"""Helper to create or update a test user with a given role."""
	email = f"perm-api-{(role or 'none').lower().replace(' ', '-')}@example.com"
	if not frappe.db.exists("User", email):
		user = frappe.get_doc({
			"doctype": "User",
			"email": email,
			"first_name": "PermApi",
			"send_welcome_email": 0,
		}).insert(ignore_permissions=True)
	else:
		user = frappe.get_doc("User", email)

	if role:
		if not frappe.db.exists("Role", role):
			frappe.get_doc({"doctype": "Role", "role_name": role, "desk_access": 1}).insert(
				ignore_permissions=True
			)
		if role not in [r.role for r in user.roles]:
			user.add_roles(role)
	return email


class TestBroadcastApi(IntegrationTestCase):
	"""Test suite for Broadcast whitelisted API."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")

		# Ensure test roles exist and users are provisioned
		cls.user_wa = _user(WA_USER)
		cls.user_wa_manager = _user("WhatsApp Manager")
		cls.user_email = _user(EMAIL_USER)
		cls.user_viewer = _user(VIEWER)
		cls.user_cm = _user(CAMPAIGN_MANAGER)
		cls.user_none = _user(None)

		# Ensure accounts and templates exist
		cls.wa_account = "Perm Test WhatsApp Account"
		if not frappe.db.exists("WhatsApp Account", cls.wa_account):
			frappe.get_doc({
				"doctype": "WhatsApp Account",
				"account_name": cls.wa_account,
				"phone_number": "+1234567890",
				"status": "Active",
			}).insert(ignore_permissions=True, ignore_links=True)

		cls.wa_template = "perm_test_template-en"
		if not frappe.db.exists("WhatsApp Templates", cls.wa_template):
			frappe.get_doc({
				"doctype": "WhatsApp Templates",
				"template_name": cls.wa_template,
				"whatsapp_account": cls.wa_account,
				"category": "MARKETING",
				"status": "APPROVED",
				"language": "en",
			}).insert(ignore_permissions=True, ignore_links=True)

		cls.email_account = "Test Email Account"
		if not frappe.db.exists("Email Account", cls.email_account):
			frappe.get_doc({
				"doctype": "Email Account",
				"email_id": "test-broadcast-api@example.com",
				"email_account_name": cls.email_account,
			}).insert(ignore_permissions=True, ignore_links=True)

		cls.email_template = "Test Email Template"
		if not frappe.db.exists("Email Template", cls.email_template):
			frappe.get_doc({
				"doctype": "Email Template",
				"name": cls.email_template,
				"subject": "Test Subject",
				"response": "Hello World",
			}).insert(ignore_permissions=True, ignore_links=True)

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		self.cleanup_broadcasts: list[str] = []

	def tearDown(self):
		frappe.set_user("Administrator")
		for name in self.cleanup_broadcasts:
			if frappe.db.exists("Broadcast", name):
				frappe.delete_doc("Broadcast", name, force=True, ignore_permissions=True)
		super().tearDown()

	def _create_broadcast(
		self,
		title: str = "Test Broadcast",
		channels: list[dict] | None = None,
		status: str = "Draft",
		scheduled_time: str | None = None,
		products: list[dict] | None = None,
		owner: str = "Administrator",
	) -> "frappe.model.document.Document":
		"""Helper to create a test Broadcast document."""
		if channels is None:
			channels = [
				{
					"channel": "WhatsApp",
					"whatsapp_account": self.wa_account,
					"whatsapp_template": self.wa_template,
					"status": status,
					"recipient_count": 10,
					"sent_count": 0,
					"delivered_count": 0,
					"failed_count": 0,
				}
			]
		for ch in channels:
			if ch.get("channel") != "WhatsApp":
				ch.setdefault("variable_type", "")
		payload = {
			"doctype": "Broadcast",
			"title": title,
			"status": status,
			"recipient_source": "Individual",
			"channels": channels,
			"owner": owner,
		}
		if scheduled_time:
			payload["scheduled_time"] = scheduled_time
		if products:
			payload["products"] = products

		doc = frappe.get_doc(payload)
		doc.insert(ignore_permissions=True, ignore_links=True)
		self.cleanup_broadcasts.append(doc.name)
		return doc

	def _create_recipient(
		self,
		broadcast: str,
		channel: str,
		address: str,
		status: str = "Queued",
		recipient_name: str = "Recipient",
	) -> "frappe.model.document.Document":
		"""Helper to insert a Broadcast Recipient."""
		rec = frappe.get_doc({
			"doctype": "Broadcast Recipient",
			"broadcast": broadcast,
			"channel": channel,
			"address": address,
			"status": status,
			"recipient_name": recipient_name,
		})
		rec.insert(ignore_permissions=True, ignore_links=True)
		return rec

	def test_guest_rejected_from_all_methods(self):
		"""Unauthenticated Guest user must be rejected with PermissionError from all methods."""
		b = self._create_broadcast("Guest Test Broadcast")
		frappe.set_user("Guest")

		self.assertRaises(frappe.PermissionError, broadcast_api.list_broadcasts)
		self.assertRaises(frappe.PermissionError, broadcast_api.get_broadcast_metrics)
		self.assertRaises(frappe.PermissionError, broadcast_api.get_broadcast, b.name)
		self.assertRaises(frappe.PermissionError, broadcast_api.save_broadcast, json.dumps({"title": "Test"}))
		self.assertRaises(frappe.PermissionError, broadcast_api.launch_broadcast, b.name)
		self.assertRaises(frappe.PermissionError, broadcast_api.pause_broadcast, b.name)
		self.assertRaises(frappe.PermissionError, broadcast_api.resume_broadcast, b.name)
		self.assertRaises(frappe.PermissionError, broadcast_api.cancel_broadcast, b.name)
		self.assertRaises(frappe.PermissionError, broadcast_api.retry_failed_broadcast, b.name)
		self.assertRaises(frappe.PermissionError, broadcast_api.delete_broadcasts, json.dumps([b.name]))
		self.assertRaises(frappe.PermissionError, broadcast_api.get_broadcast_progress, b.name)
		self.assertRaises(frappe.PermissionError, broadcast_api.get_broadcast_recipients, b.name)
		self.assertRaises(frappe.PermissionError, broadcast_api.clone_broadcast, b.name)

	def test_viewer_role_access(self):
		"""MSuite Viewer has read-only access: can read/list/progress but cannot mutate."""
		b = self._create_broadcast("Viewer Test Broadcast")
		frappe.set_user(self.user_viewer)

		# Read methods work
		res_list = broadcast_api.list_broadcasts()
		self.assertIsInstance(res_list, dict)
		self.assertIn("rows", res_list)

		res_metrics = broadcast_api.get_broadcast_metrics()
		self.assertIsInstance(res_metrics, dict)
		self.assertIn("total", res_metrics)

		res_get = broadcast_api.get_broadcast(b.name)
		self.assertIsInstance(res_get, dict)
		self.assertEqual(res_get["name"], b.name)

		res_prog = broadcast_api.get_broadcast_progress(b.name)
		self.assertIsInstance(res_prog, dict)

		res_rec = broadcast_api.get_broadcast_recipients(b.name)
		self.assertIsInstance(res_rec, dict)

		# Write/mutation methods raise PermissionError
		self.assertRaises(
			frappe.PermissionError,
			broadcast_api.save_broadcast,
			json.dumps({"title": "Viewer New BC", "channels": [{"channel": "WhatsApp", "whatsapp_account": self.wa_account, "whatsapp_template": self.wa_template}]}),
		)
		self.assertRaises(frappe.PermissionError, broadcast_api.launch_broadcast, b.name)
		self.assertRaises(frappe.PermissionError, broadcast_api.pause_broadcast, b.name)
		self.assertRaises(frappe.PermissionError, broadcast_api.resume_broadcast, b.name)
		self.assertRaises(frappe.PermissionError, broadcast_api.cancel_broadcast, b.name)
		self.assertRaises(frappe.PermissionError, broadcast_api.retry_failed_broadcast, b.name)
		self.assertRaises(frappe.PermissionError, broadcast_api.clone_broadcast, b.name)

		# delete_broadcasts reports permission failure in failed list
		del_res = broadcast_api.delete_broadcasts(json.dumps([b.name]))
		self.assertEqual(len(del_res["deleted"]), 0)
		self.assertEqual(len(del_res["failed"]), 1)
		self.assertIn("Permission", del_res["failed"][0]["reason"])

	def test_channel_user_permissions(self):
		"""WhatsApp User can read/write WhatsApp broadcasts; Email User cannot read or write them."""
		b_wa = self._create_broadcast(
			"WA Broadcast",
			channels=[{
				"channel": "WhatsApp",
				"whatsapp_account": self.wa_account,
				"whatsapp_template": self.wa_template,
				"status": "Draft",
			}],
		)
		b_em = self._create_broadcast(
			"Email Broadcast",
			channels=[{
				"channel": "Email",
				"email_account": self.email_account,
				"email_template": self.email_template,
				"status": "Draft",
			}],
		)

		# WhatsApp User
		frappe.set_user(self.user_wa)
		wa_doc = broadcast_api.get_broadcast(b_wa.name)
		self.assertEqual(wa_doc["name"], b_wa.name)
		self.assertRaises(frappe.PermissionError, broadcast_api.get_broadcast, b_em.name)

		# Email User
		frappe.set_user(self.user_email)
		em_doc = broadcast_api.get_broadcast(b_em.name)
		self.assertEqual(em_doc["name"], b_em.name)
		self.assertRaises(frappe.PermissionError, broadcast_api.get_broadcast, b_wa.name)

	def test_mixed_channel_broadcast_partial_roles_p1(self):
		"""Partial-role user sees only their permitted channel rows and recipients (P1)."""
		mixed = self._create_broadcast(
			"Mixed WA and Email",
			channels=[
				{
					"channel": "WhatsApp",
					"whatsapp_account": self.wa_account,
					"whatsapp_template": self.wa_template,
					"status": "Draft",
				},
				{
					"channel": "Email",
					"email_account": self.email_account,
					"email_template": self.email_template,
					"status": "Draft",
				},
			],
		)
		self._create_recipient(mixed.name, "WhatsApp", "+19876543210")
		self._create_recipient(mixed.name, "Email", "mixed@example.com")

		# WhatsApp User
		frappe.set_user(self.user_wa)
		wa_view = broadcast_api.get_broadcast(mixed.name)
		channel_names = [ch["channel"] for ch in wa_view["channels"]]
		self.assertIn("WhatsApp", channel_names)
		self.assertNotIn("Email", channel_names)

		wa_recipients = broadcast_api.get_broadcast_recipients(mixed.name)
		self.assertEqual(wa_recipients["total"], 1)
		self.assertEqual(wa_recipients["rows"][0]["channel"], "WhatsApp")

		# WhatsApp User lacks full permissions to write/mutate mixed broadcast
		self.assertRaises(frappe.PermissionError, broadcast_api.launch_broadcast, mixed.name)

		# Campaign Manager sees both channels and recipients
		frappe.set_user(self.user_cm)
		cm_view = broadcast_api.get_broadcast(mixed.name)
		cm_channels = [ch["channel"] for ch in cm_view["channels"]]
		self.assertIn("WhatsApp", cm_channels)
		self.assertIn("Email", cm_channels)

		cm_recipients = broadcast_api.get_broadcast_recipients(mixed.name)
		self.assertEqual(cm_recipients["total"], 2)

	def test_list_broadcasts_filters_and_search(self):
		"""Validate search by title, status filter, template substring, and date ranges."""
		frappe.set_user(self.user_cm)
		b1 = self._create_broadcast("Alpha Promo Summer", status="Draft")
		b2 = self._create_broadcast("Beta Launch Winter", status="Completed")
		b3 = self._create_broadcast("Gamma Queued Blast", status="Queued")

		# Search by title
		res_search = broadcast_api.list_broadcasts(search="Alpha Promo")
		names = [r["name"] for r in res_search["rows"]]
		self.assertIn(b1.name, names)
		self.assertNotIn(b2.name, names)

		# Status filter
		res_status = broadcast_api.list_broadcasts(status="Completed")
		status_names = [r["name"] for r in res_status["rows"]]
		self.assertIn(b2.name, status_names)
		self.assertNotIn(b1.name, status_names)

		# Status filter 'inProgress' includes Queued and In Progress
		res_inprogress = broadcast_api.list_broadcasts(status="inProgress")
		ip_names = [r["name"] for r in res_inprogress["rows"]]
		self.assertIn(b3.name, ip_names)
		self.assertNotIn(b1.name, ip_names)

		# Template filter
		res_tpl = broadcast_api.list_broadcasts(template=self.wa_template)
		self.assertTrue(any(r["name"] == b1.name for r in res_tpl["rows"]))

		# Date range filters: today, 7days, 30days, custom
		res_today = broadcast_api.list_broadcasts(date_range="today")
		self.assertIn(b1.name, [r["name"] for r in res_today["rows"]])

		res_7days = broadcast_api.list_broadcasts(date_range="7days")
		self.assertIn(b1.name, [r["name"] for r in res_7days["rows"]])

		res_custom = broadcast_api.list_broadcasts(
			date_range="custom",
			date_from=str(add_days(today(), -1)),
			date_to=str(add_days(today(), 1)),
		)
		self.assertIn(b1.name, [r["name"] for r in res_custom["rows"]])

		# Check row shape
		first_row = res_search["rows"][0]
		expected_keys = {
			"name", "title", "status", "campaign", "recipient_source", "recipient_list",
			"recipient_count", "scheduled_time", "creation", "modified", "owner", "channels"
		}
		self.assertTrue(expected_keys.issubset(set(first_row.keys())))
		ch_keys = {"channel", "status", "template", "account", "recipient_count", "sent_count", "delivered_count", "failed_count"}
		self.assertTrue(ch_keys.issubset(set(first_row["channels"][0].keys())))

	def test_list_broadcasts_pagination_and_capping(self):
		"""Validate pagination offsets, over-the-end page, and limit capping."""
		frappe.set_user(self.user_cm)
		# Create 5 broadcasts
		created = [self._create_broadcast(f"Page BC {i}") for i in range(5)]

		# Page 1
		p1 = broadcast_api.list_broadcasts(start=0, limit=2)
		self.assertEqual(len(p1["rows"]), 2)
		self.assertGreaterEqual(p1["total"], 5)

		# Page 2
		p2 = broadcast_api.list_broadcasts(start=2, limit=2)
		self.assertEqual(len(p2["rows"]), 2)
		self.assertNotEqual(p1["rows"][0]["name"], p2["rows"][0]["name"])

		# Over-the-end page
		pend = broadcast_api.list_broadcasts(start=10000, limit=10)
		self.assertEqual(len(pend["rows"]), 0)
		self.assertGreaterEqual(pend["total"], 5)

		# Limit capping: passing 500 should be capped to MAX_PAGE_LENGTH
		capped = broadcast_api.list_broadcasts(start=0, limit=500)
		self.assertLessEqual(len(capped["rows"]), broadcast_api.MAX_PAGE_LENGTH)

	def test_filter_injection_safe(self):
		"""Filter injection strings (SQL injection, wildcards, unicode) neither error nor leak."""
		frappe.set_user(self.user_cm)
		self._create_broadcast("Injection Test Broadcast")

		injection_strings = [
			"'; DROP TABLE tabBroadcast; --",
			"%",
			"\\",
			"🎉 测试 unicode filter 🚀",
			"' OR '1'='1",
		]
		for s in injection_strings:
			# Should execute safely without error
			res = broadcast_api.list_broadcasts(search=s)
			self.assertIsInstance(res, dict)
			self.assertIn("rows", res)

			res_rec = broadcast_api.get_broadcast_recipients(self.cleanup_broadcasts[0], search=s)
			self.assertIsInstance(res_rec, dict)
			self.assertIn("rows", res_rec)

		# Ensure table still exists
		self.assertTrue(frappe.db.table_exists("Broadcast"))

	def test_get_broadcast_metrics(self):
		"""Metrics count only permitted broadcasts and categorize statuses correctly."""
		# Create one of each key status under Administrator
		frappe.set_user("Administrator")
		self._create_broadcast("Metrics Draft", status="Draft")
		future_time = str(add_days(now_datetime(), 2))
		self._create_broadcast("Metrics Scheduled", status="Scheduled", scheduled_time=future_time)
		self._create_broadcast("Metrics Queued", status="Queued")
		self._create_broadcast("Metrics In Progress", status="In Progress")
		self._create_broadcast("Metrics Completed", status="Completed")
		self._create_broadcast("Metrics Failed", status="Failed")
		self._create_broadcast("Metrics Partially Failed", status="Partially Failed")
		self._create_broadcast("Metrics Cancelled", status="Cancelled")

		# Email-only broadcast
		self._create_broadcast(
			"Metrics Email Only",
			channels=[{
				"channel": "Email",
				"email_account": self.email_account,
				"email_template": self.email_template,
				"status": "Draft",
			}],
		)

		frappe.set_user(self.user_cm)
		metrics_cm = broadcast_api.get_broadcast_metrics()
		expected_keys = {"total", "draft", "scheduled", "inProgress", "completed", "failed", "cancelled"}
		self.assertEqual(set(metrics_cm.keys()), expected_keys)
		self.assertGreaterEqual(metrics_cm["total"], 9)
		self.assertGreaterEqual(metrics_cm["inProgress"], 2)  # Queued + In Progress
		self.assertGreaterEqual(metrics_cm["failed"], 2)  # Failed + Partially Failed

		# WhatsApp User should not count the Email-only broadcast
		frappe.set_user(self.user_wa)
		metrics_wa = broadcast_api.get_broadcast_metrics()
		self.assertLess(metrics_wa["total"], metrics_cm["total"])

	def test_get_broadcast_with_metrics_mock(self):
		"""get_broadcast returns full doc, channels, products, launch_error, and channel_metrics."""
		b = self._create_broadcast(
			"Full Broadcast Doc",
			products=[{"item_code": "PROD-001"}],
		)
		frappe.set_user(self.user_cm)

		# Mock broadcast.rollup if imported
		mock_metrics = {"WhatsApp": {"sent": 10, "delivered": 9}}
		with patch.dict("sys.modules", {"msuite_campaigns.broadcast.rollup": MagicMock(get_broadcast_channel_metrics=MagicMock(return_value=mock_metrics))}):
			doc = broadcast_api.get_broadcast(b.name)
			self.assertEqual(doc["name"], b.name)
			self.assertIn("channels", doc)
			self.assertIn("products", doc)
			self.assertIn("channel_metrics", doc)
			self.assertIn("launch_error", doc)

	def test_save_broadcast_create_and_update(self):
		"""save_broadcast creates Draft, updates Draft/Scheduled, and refuses update when In Progress."""
		frappe.set_user(self.user_cm)
		doc_data = {
			"title": "API Created Broadcast",
			"recipient_source": "Individual",
			"channels": [
				{
					"channel": "WhatsApp",
					"whatsapp_account": self.wa_account,
					"whatsapp_template": self.wa_template,
					"status": "Draft",
				}
			],
		}
		# Create
		res = broadcast_api.save_broadcast(json.dumps(doc_data))
		self.assertIn("name", res)
		self.assertEqual(res["status"], "Draft")
		self.cleanup_broadcasts.append(res["name"])

		# Update Draft
		doc_data["title"] = "API Updated Broadcast"
		res_update = broadcast_api.save_broadcast(json.dumps(doc_data), name=res["name"])
		self.assertEqual(res_update["name"], res["name"])
		updated_doc = frappe.get_doc("Broadcast", res["name"])
		self.assertEqual(updated_doc.title, "API Updated Broadcast")

		# Refuse update when In Progress
		frappe.db.set_value("Broadcast", res["name"], "status", "In Progress")
		self.assertRaises(
			frappe.ValidationError,
			broadcast_api.save_broadcast,
			json.dumps(doc_data),
			name=res["name"],
		)

	def test_save_broadcast_with_launch(self):
		"""save_broadcast with launch=1 calls hub.launch and returns channel results."""
		frappe.set_user(self.user_cm)
		doc_data = {
			"title": "Launchable Broadcast",
			"recipient_source": "Individual",
			"channels": [
				{
					"channel": "WhatsApp",
					"whatsapp_account": self.wa_account,
					"whatsapp_template": self.wa_template,
					"status": "Draft",
				}
			],
		}
		mock_hub = MagicMock()
		mock_hub.launch.return_value = {"WhatsApp": {"status": "Queued"}}
		with patch.dict("sys.modules", {"msuite_campaigns.broadcast.hub": mock_hub}):
			res = broadcast_api.save_broadcast(
				json.dumps(doc_data),
				launch=1,
				individual_rows=json.dumps([{"phone": "+1234567890"}]),
			)
			self.assertIn("name", res)
			self.cleanup_broadcasts.append(res["name"])
			self.assertIn("results", res)
			mock_hub.launch.assert_called_once()

	def test_launch_pause_resume_cancel_retry(self):
		"""Lifecycle actions transition channels and return {channel: status}."""
		frappe.set_user(self.user_cm)
		b = self._create_broadcast("Lifecycle Actions Test", status="Draft")

		# Mock hub.launch
		mock_hub = MagicMock()
		mock_hub.launch.return_value = {"WhatsApp": "Queued"}
		with patch.dict("sys.modules", {"msuite_campaigns.broadcast.hub": mock_hub}):
			res_launch = broadcast_api.launch_broadcast(b.name)
			self.assertEqual(res_launch, {"WhatsApp": "Queued"})

		# Transition to Queued in DB for pause/resume testing
		frappe.db.set_value("Broadcast Channel", {"parent": b.name}, "status", "Queued")
		frappe.db.set_value("Broadcast", b.name, "status", "Queued")

		# Pause
		res_pause = broadcast_api.pause_broadcast(b.name)
		self.assertEqual(res_pause.get("WhatsApp"), "Paused")

		# Resume
		res_resume = broadcast_api.resume_broadcast(b.name)
		self.assertEqual(res_resume.get("WhatsApp"), "Queued")

		# Cancel
		res_cancel = broadcast_api.cancel_broadcast(b.name)
		self.assertEqual(res_cancel.get("WhatsApp"), "Cancelled")

		# Retry Failed
		frappe.db.set_value("Broadcast Channel", {"parent": b.name}, "status", "Failed")
		frappe.db.set_value("Broadcast", b.name, "status", "Failed")
		res_retry = broadcast_api.retry_failed_broadcast(b.name)
		self.assertEqual(res_retry.get("WhatsApp"), "Queued")

	def test_delete_broadcasts(self):
		"""delete_broadcasts enforces size limits, status guards, permissions, and returns partial results."""
		frappe.set_user(self.user_cm)
		b_draft = self._create_broadcast("Delete Draft", status="Draft")
		b_in_prog = self._create_broadcast("Delete In Progress", status="In Progress")
		b_em = self._create_broadcast(
			"Delete Email",
			channels=[{
				"channel": "Email",
				"email_account": self.email_account,
				"email_template": self.email_template,
				"status": "Draft",
			}],
		)

		# Oversized list rejected
		oversized = [f"BC-FAKE-{i}" for i in range(broadcast_api.MAX_BULK_DELETE_LIMIT + 1)]
		self.assertRaises(
			frappe.ValidationError,
			broadcast_api.delete_broadcasts,
			json.dumps(oversized),
		)

		# WhatsApp Manager deleting mix:
		# b_draft (WhatsApp) -> ok
		# b_in_prog (WhatsApp, In Progress) -> refused (status)
		# b_em (Email) -> refused (no permission)
		frappe.set_user(self.user_wa_manager)
		res = broadcast_api.delete_broadcasts(json.dumps([b_draft.name, b_in_prog.name, b_em.name]))
		self.assertIn(b_draft.name, res["deleted"])
		failed_map = {item["name"]: item["reason"] for item in res["failed"]}
		self.assertIn(b_in_prog.name, failed_map)
		self.assertIn("status", failed_map[b_in_prog.name].lower())
		self.assertIn(b_em.name, failed_map)
		self.assertIn("permission", failed_map[b_em.name].lower())

	def test_get_broadcast_progress(self):
		"""get_broadcast_progress reads child channel rows and computes percent_sent without table scan."""
		b = self._create_broadcast(
			"Progress Test",
			channels=[{
				"channel": "WhatsApp",
				"whatsapp_account": self.wa_account,
				"whatsapp_template": self.wa_template,
				"status": "In Progress",
				"recipient_count": 200,
				"sent_count": 100,
				"delivered_count": 90,
				"read_count": 80,
				"failed_count": 5,
			}],
		)
		frappe.set_user(self.user_cm)
		prog = broadcast_api.get_broadcast_progress(b.name)
		self.assertIn("WhatsApp", prog)
		wa_prog = prog["WhatsApp"]
		self.assertEqual(wa_prog["status"], "In Progress")
		self.assertEqual(wa_prog["recipient_count"], 200)
		self.assertEqual(wa_prog["sent_count"], 100)
		self.assertEqual(wa_prog["delivered_count"], 90)
		self.assertEqual(wa_prog["read_count"], 80)
		self.assertEqual(wa_prog["failed_count"], 5)
		self.assertEqual(wa_prog["percent_sent"], 50.0)

	def test_get_broadcast_recipients_and_export(self):
		"""get_broadcast_recipients supports filtering, pagination, and export with truncation flag."""
		b = self._create_broadcast("Recipients Test")
		for i in range(15):
			self._create_recipient(b.name, "WhatsApp", f"+100000000{i:02d}", status="Queued" if i < 10 else "Failed")

		frappe.set_user(self.user_cm)

		# Pagination
		p1 = broadcast_api.get_broadcast_recipients(b.name, start=0, length=5)
		self.assertEqual(len(p1["rows"]), 5)
		self.assertEqual(p1["total"], 15)

		# Filter by status
		failed_recs = broadcast_api.get_broadcast_recipients(b.name, status="Failed")
		self.assertEqual(failed_recs["total"], 5)

		# Export mode with mock EXPORT_LIMIT = 10
		with patch.object(broadcast_api, "EXPORT_LIMIT", 10):
			exp = broadcast_api.get_broadcast_recipients(b.name, export=1)
			self.assertEqual(len(exp["rows"]), 10)
			self.assertTrue(exp["truncated"])

	def test_clone_broadcast(self):
		"""clone_broadcast creates a Draft Broadcast with '<title> (copy)', no schedule, and reset counters."""
		future_time = str(add_days(now_datetime(), 1))
		b = self._create_broadcast(
			"Original Broadcast",
			channels=[{
				"channel": "WhatsApp",
				"whatsapp_account": self.wa_account,
				"whatsapp_template": self.wa_template,
				"status": "Completed",
				"recipient_count": 100,
				"sent_count": 100,
				"delivered_count": 95,
				"failed_count": 5,
			}],
			scheduled_time=future_time,
		)
		frappe.set_user(self.user_cm)
		cloned = broadcast_api.clone_broadcast(b.name)
		self.cleanup_broadcasts.append(cloned["name"])

		self.assertEqual(cloned["title"], "Original Broadcast (copy)")
		self.assertEqual(cloned["status"], "Draft")
		self.assertIsNone(cloned.get("scheduled_time"))

		ch = cloned["channels"][0]
		self.assertEqual(ch["status"], "Draft")
		self.assertEqual(ch["recipient_count"], 0)
		self.assertEqual(ch["sent_count"], 0)
		self.assertEqual(ch["delivered_count"], 0)
		self.assertEqual(ch["failed_count"], 0)

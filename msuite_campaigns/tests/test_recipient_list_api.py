# Copyright (c) 2026, MSuite and contributors
# For license information, please see license.txt

"""Integration tests for Recipient List whitelisted API endpoints.

Tests:
- Role-based permissions (Campaign Manager, Campaign User, WhatsApp/Email/SMS read-only, Guest/no-role denied)
- Happy paths, permission denied, and not found for all endpoints
- Contact CRUD operations (get_contacts, update_contact, delete_contact, add_contacts_manual)
- Pagination (first, last, over-end)
- get_reachable_contacts reachability filtering (excludes opted-out, invalid), channel rejection, address formatting
- Input validation (injection strings in search, oversized payloads rejected)
- File preview & parsing, DocType import endpoints
"""

from __future__ import annotations

import json
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from msuite_campaigns.audience.api.v1 import recipient_list as rl_api
from msuite_campaigns.broadcast.constants import (
	CHANNEL_EMAIL,
	CHANNEL_SMS,
	CHANNEL_WHATSAPP,
)
from msuite_core.services.permissions.roles import (
	ADMIN,
	CAMPAIGN_MANAGER,
	CAMPAIGN_USER,
	EMAIL_USER,
	SMS_USER,
	VIEWER,
	WA_USER,
)
from msuite_core.services.validation import phone as phone_validator

TEST_PREFIX = "ZZApiRL "


def _user(role: str | None) -> str:
	"""Get or create a test user with the specified role."""
	email = f"perm-rl-{(role or 'none').lower().replace(' ', '-')}@example.com"
	if not frappe.db.exists("User", email):
		u = frappe.get_doc({
			"doctype": "User",
			"email": email,
			"first_name": "PermRL",
			"send_welcome_email": 0,
		}).insert(ignore_permissions=True)
		if role:
			u.add_roles(role)
	elif role:
		u = frappe.get_doc("User", email)
		if role not in [r.role for r in u.roles]:
			u.add_roles(role)
	return email


class TestRecipientListApi(IntegrationTestCase):
	"""Test suite for Recipient List whitelisted API methods."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.user_mgr = _user(CAMPAIGN_MANAGER)
		cls.user_usr = _user(CAMPAIGN_USER)
		cls.user_wa = _user(WA_USER)
		cls.user_email = _user(EMAIL_USER)
		cls.user_sms = _user(SMS_USER)
		cls.user_viewer = _user(VIEWER)
		cls.user_none = _user(None)

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		self.cleanup_lists: list[str] = []

	def tearDown(self):
		frappe.db.rollback()
		frappe.set_user("Administrator")
		for name in self.cleanup_lists:
			if frappe.db.exists("Recipient List", name):
				frappe.delete_doc("Recipient List", name, force=True, ignore_permissions=True)
		super().tearDown()

	def _create_recipient_list(self, name_suffix: str = "", contacts: list[dict] | None = None):
		"""Helper to create a Recipient List document for testing."""
		list_name = f"{TEST_PREFIX}{name_suffix or frappe.generate_hash(length=8)}"
		doc = frappe.get_doc({
			"doctype": "Recipient List",
			"list_name": list_name,
			"default_region": "IN",
			"source": "Manual",
		})
		doc.flags.ignore_permissions = True
		doc.insert()
		self.cleanup_lists.append(doc.name)
		if contacts:
			doc.add_contacts(contacts)
			doc.save()
		return doc

	# ── Role Permissions Tests ──────────────────────────────────────────

	def test_guest_and_roleless_user_denied_all_endpoints(self):
		"""Role-less user must get PermissionError across endpoints."""
		rl = self._create_recipient_list("RolelessTest")
		frappe.set_user(self.user_none)

		with self.assertRaises(frappe.PermissionError):
			rl_api.list_recipient_lists()

		with self.assertRaises(frappe.PermissionError):
			rl_api.get_recipient_list(rl.name)

		with self.assertRaises(frappe.PermissionError):
			rl_api.save_recipient_list(json.dumps({"list_name": "DeniedRL"}))

		with self.assertRaises(frappe.PermissionError):
			rl_api.delete_recipient_list(rl.name)

		with self.assertRaises(frappe.PermissionError):
			rl_api.bulk_delete_recipient_lists(json.dumps([rl.name]))

		with self.assertRaises(frappe.PermissionError):
			rl_api.get_contacts(rl.name)

		with self.assertRaises(frappe.PermissionError):
			rl_api.add_contacts_manual(rl.name, [{"raw_phone": "9876543210"}])

		with self.assertRaises(frappe.PermissionError):
			rl_api.validate_recipient_list(rl.name)

		with self.assertRaises(frappe.PermissionError):
			rl_api.get_validation_summary(rl.name)

		with self.assertRaises(frappe.PermissionError):
			rl_api.get_reachable_contacts(rl.name, "WhatsApp")

		with self.assertRaises(frappe.PermissionError):
			rl_api.preview_file("/files/dummy.csv")

		with self.assertRaises(frappe.PermissionError):
			rl_api.parse_file_rows("/files/dummy.csv")

		with self.assertRaises(frappe.PermissionError):
			rl_api.search_doctypes(query="User")

		with self.assertRaises(frappe.PermissionError):
			rl_api.get_doctype_fields("User")

	def test_channel_roles_are_read_only(self):
		"""WhatsApp, SMS, and Email users can read but cannot write or delete."""
		contacts = [{"full_name": "Test Contact", "raw_phone": "9876543210", "raw_email": "t@example.com"}]
		rl = self._create_recipient_list("ChannelRoleTest", contacts)
		c_name = rl.contacts[0].name

		for user in (self.user_wa, self.user_email, self.user_sms, self.user_viewer):
			frappe.set_user(user)

			# Read operations allowed
			data = rl_api.get_recipient_list(rl.name)
			self.assertEqual(data["name"], rl.name)

			res = rl_api.get_contacts(rl.name)
			self.assertIn("rows", res)

			summary = rl_api.get_validation_summary(rl.name)
			self.assertIn("channels", summary)

			reachable = rl_api.get_reachable_contacts(rl.name, "WhatsApp")
			self.assertIsInstance(reachable, list)

			# Write / Delete operations denied
			with self.assertRaises(frappe.PermissionError):
				rl_api.save_recipient_list(json.dumps({"name": rl.name, "description": "Hacked"}))

			with self.assertRaises(frappe.PermissionError):
				rl_api.update_contact(c_name, full_name="Updated By WA User")

			with self.assertRaises(frappe.PermissionError):
				rl_api.delete_contact(c_name)

			with self.assertRaises(frappe.PermissionError):
				rl_api.add_contacts_manual(rl.name, [{"raw_phone": "9876543219"}])

			with self.assertRaises(frappe.PermissionError):
				rl_api.delete_recipient_list(rl.name)

	def test_campaign_user_can_create_read_write_but_not_delete(self):
		"""Campaign User can create/edit but cannot delete recipient lists."""
		rl = self._create_recipient_list("CampUserTest")
		frappe.set_user(self.user_usr)

		# Can read
		doc_dict = rl_api.get_recipient_list(rl.name)
		self.assertEqual(doc_dict["name"], rl.name)

		# Can edit
		save_res = rl_api.save_recipient_list({
			"name": rl.name,
			"description": "Updated by Campaign User",
		})
		self.assertEqual(save_res["name"], rl.name)

		# Cannot delete doc
		with self.assertRaises(frappe.PermissionError):
			rl_api.delete_recipient_list(rl.name)

		with self.assertRaises(frappe.PermissionError):
			rl_api.bulk_delete_recipient_lists(json.dumps([rl.name]))

	def test_campaign_manager_has_full_crud(self):
		"""Campaign Manager can create, read, update, and delete."""
		frappe.set_user(self.user_mgr)

		# Create
		unique_name = f"{TEST_PREFIX}MgrCreate {frappe.generate_hash(length=6)}"
		res = rl_api.save_recipient_list({
			"list_name": unique_name,
			"description": "Created by Manager",
			"contacts": [{"full_name": "Contact 1", "raw_phone": "9876543210"}],
		})
		doc_name = res["name"]
		self.cleanup_lists.append(doc_name)

		# Read
		doc = rl_api.get_recipient_list(doc_name)
		self.assertEqual(doc["list_name"], unique_name)

		# Update
		rl_api.save_recipient_list({"name": doc_name, "description": "Updated description"})
		doc = rl_api.get_recipient_list(doc_name)
		self.assertEqual(doc["description"], "Updated description")

		# Delete
		del_res = rl_api.delete_recipient_list(doc_name)
		self.assertTrue(del_res["ok"])
		self.assertFalse(frappe.db.exists("Recipient List", doc_name))

	# ── Recipient List CRUD & Not Found Tests ───────────────────────────

	def test_not_found_handling(self):
		"""Endpoints must throw DoesNotExistError when doc or contact is not found."""
		non_existent = "Non-Existent-Recipient-List-12345"
		with self.assertRaises(frappe.DoesNotExistError):
			rl_api.get_recipient_list(non_existent)

		with self.assertRaises(frappe.DoesNotExistError):
			rl_api.delete_recipient_list(non_existent)

		with self.assertRaises(frappe.DoesNotExistError):
			rl_api.get_contacts(non_existent)

		with self.assertRaises(frappe.DoesNotExistError):
			rl_api.add_contacts_manual(non_existent, [{"raw_phone": "9876543210"}])

		with self.assertRaises(frappe.DoesNotExistError):
			rl_api.validate_recipient_list(non_existent)

		with self.assertRaises(frappe.DoesNotExistError):
			rl_api.get_validation_summary(non_existent)

		with self.assertRaises(frappe.DoesNotExistError):
			rl_api.get_reachable_contacts(non_existent, "WhatsApp")

		with self.assertRaises(frappe.DoesNotExistError):
			rl_api.update_contact("non-existent-contact-id", full_name="Ghost")

		with self.assertRaises(frappe.DoesNotExistError):
			rl_api.delete_contact("non-existent-contact-id")

	def test_list_recipient_lists_and_search_injection(self):
		"""list_recipient_lists must handle search filters and resist SQL injection attempts."""
		rl1 = self._create_recipient_list("SearchAlpha")
		rl2 = self._create_recipient_list("SearchBeta")

		# Normal search
		items = rl_api.list_recipient_lists(search="SearchAlpha", limit=50)
		names = [item["name"] for item in items]
		self.assertIn(rl1.name, names)
		self.assertNotIn(rl2.name, names)

		# SQL injection attack string in search
		injection_search = "' OR '1'='1' -- "
		inj_items = rl_api.list_recipient_lists(search=injection_search, limit=50)
		self.assertIsInstance(inj_items, list)

	def test_bulk_delete_recipient_lists(self):
		"""bulk_delete_recipient_lists must delete multiple lists safely."""
		frappe.set_user(self.user_mgr)
		rl1 = self._create_recipient_list("BulkDel1")
		rl2 = self._create_recipient_list("BulkDel2")

		res = rl_api.bulk_delete_recipient_lists(json.dumps([rl1.name, rl2.name]))
		self.assertTrue(res["ok"])
		self.assertEqual(res["deleted"], 2)
		self.assertFalse(frappe.db.exists("Recipient List", rl1.name))
		self.assertFalse(frappe.db.exists("Recipient List", rl2.name))

	def test_oversized_payloads_rejected(self):
		"""bulk_delete and contacts addition must reject payloads exceeding max limits."""
		frappe.set_user(self.user_mgr)
		rl = self._create_recipient_list("OversizedTest")

		# Oversized bulk delete
		oversized_names = [f"Item-{i}" for i in range(101)]
		with self.assertRaises(frappe.ValidationError):
			rl_api.bulk_delete_recipient_lists(json.dumps(oversized_names))

		# Oversized contacts in add_contacts_manual
		oversized_contacts = [{"raw_phone": f"987654{i:04d}"} for i in range(5001)]
		with self.assertRaises(frappe.ValidationError):
			rl_api.add_contacts_manual(rl.name, oversized_contacts)

		# Oversized contacts in save_recipient_list
		with self.assertRaises(frappe.ValidationError):
			rl_api.save_recipient_list({
				"list_name": f"{TEST_PREFIX}HugeList",
				"contacts": oversized_contacts,
			})

	# ── Contact CRUD & Pagination Tests ─────────────────────────────────

	def test_contact_crud_operations(self):
		"""update_contact, delete_contact, and add_contacts_manual must work and maintain rollups."""
		frappe.set_user(self.user_mgr)
		rl = self._create_recipient_list("ContactCrudTest", [
			{"full_name": "First Contact", "raw_phone": "9876543210", "raw_email": "first@example.com"}
		])
		self.assertEqual(len(rl.contacts), 1)
		c_name = rl.contacts[0].name

		# Update contact
		updated = rl_api.update_contact(
			c_name,
			full_name="First Updated",
			raw_phone="9876543211",
			raw_email="updated@example.com",
		)
		self.assertEqual(updated["full_name"], "First Updated")
		self.assertEqual(updated["phone_e164"], "+919876543211")
		self.assertEqual(updated["email_normalized"], "updated@example.com")

		# Add contacts manual (with deduplication)
		res = rl_api.add_contacts_manual(rl.name, [
			{"full_name": "Second Contact", "raw_phone": "9876543212", "raw_email": "second@example.com"},
			# Duplicate phone and email of First Updated
			{"full_name": "Duplicate Contact", "raw_phone": "9876543211", "raw_email": "updated@example.com"},
		])
		self.assertEqual(res["added"], 1)
		self.assertEqual(res["skipped"], 1)

		# Delete contact
		del_res = rl_api.delete_contact(c_name)
		self.assertTrue(del_res["ok"])
		rl.reload()
		self.assertEqual(len(rl.contacts), 1)
		self.assertEqual(rl.total_contacts, 1)

	def test_get_contacts_pagination_and_status(self):
		"""get_contacts must paginate properly (first, last, over-end) and support status filter."""
		contacts = [
			{"full_name": f"Contact {i}", "raw_phone": f"98765432{i:02d}", "raw_email": f"c{i}@example.com"}
			for i in range(5)
		]
		rl = self._create_recipient_list("PaginationTest", contacts)

		# First page
		p1 = rl_api.get_contacts(rl.name, start=0, page_length=2)
		self.assertEqual(len(p1["rows"]), 2)
		self.assertEqual(p1["total"], 5)
		self.assertEqual(p1["rows"][0]["full_name"], "Contact 0")

		# Next page
		p2 = rl_api.get_contacts(rl.name, start=2, page_length=2)
		self.assertEqual(len(p2["rows"]), 2)
		self.assertEqual(p2["rows"][0]["full_name"], "Contact 2")

		# Last page
		p3 = rl_api.get_contacts(rl.name, start=4, page_length=2)
		self.assertEqual(len(p3["rows"]), 1)
		self.assertEqual(p3["rows"][0]["full_name"], "Contact 4")

		# Over-end page
		p_over = rl_api.get_contacts(rl.name, start=10, page_length=2)
		self.assertEqual(len(p_over["rows"]), 0)
		self.assertEqual(p_over["total"], 5)

		# Status filtering
		# Mark one contact as phone_invalid
		first_c = rl.contacts[0]
		first_c.phone_status = "Invalid"
		first_c.save()

		invalid_p = rl_api.get_contacts(rl.name, status="phone_invalid")
		self.assertEqual(len(invalid_p["rows"]), 1)
		self.assertEqual(invalid_p["rows"][0]["name"], first_c.name)

	def test_get_contacts_search_pagination_total(self):
		"""get_contacts total must equal number of matches when search is set, across pages."""
		contacts = [
			{"full_name": "Alice Smith", "raw_phone": "9876543200", "raw_email": "alice1@example.com"},
			{"full_name": "Bob Jones", "raw_phone": "9876543201", "raw_email": "bob@example.com"},
			{"full_name": "Alice Cooper", "raw_phone": "9876543202", "raw_email": "alice2@example.com"},
			{"full_name": "Charlie Brown", "raw_phone": "9876543203", "raw_email": "charlie@example.com"},
			{"full_name": "Alice Wonderland", "raw_phone": "9876543204", "raw_email": "alice3@example.com"},
		]
		rl = self._create_recipient_list("SearchTotalTest", contacts)

		# Search matches 3 out of 5 contacts ("Alice")
		# Page 1: start=0, page_length=2
		p1 = rl_api.get_contacts(rl.name, search="Alice", start=0, page_length=2)
		self.assertEqual(len(p1["rows"]), 2)
		self.assertEqual(p1["total"], 3, "Total must reflect search matches (3), not all list contacts (5)")

		# Page 2: start=2, page_length=2
		p2 = rl_api.get_contacts(rl.name, search="Alice", start=2, page_length=2)
		self.assertEqual(len(p2["rows"]), 1)
		self.assertEqual(p2["total"], 3)

		# Page 3: over-end start=4, page_length=2
		p3 = rl_api.get_contacts(rl.name, search="Alice", start=4, page_length=2)
		self.assertEqual(len(p3["rows"]), 0)
		self.assertEqual(p3["total"], 3)

	# ── get_reachable_contacts Tests ────────────────────────────────────

	def test_get_reachable_contacts(self):
		"""get_reachable_contacts returns [{'address', 'recipient_name'}] filtered by channel reachability."""
		contacts = [
			{
				"full_name": "Valid WhatsApp & SMS",
				"raw_phone": "9876543210",
				"raw_email": "wa@example.com",
			},
			{
				"full_name": "Opted Out WA",
				"raw_phone": "9876543211",
				"raw_email": "wa_out@example.com",
			},
			{
				"full_name": "Invalid Phone",
				"raw_phone": "12345",
				"raw_email": "invalid_p@example.com",
			},
			{
				"full_name": "Email Only",
				"raw_phone": "",
				"raw_email": "email_only@example.com",
			},
		]
		rl = self._create_recipient_list("ReachableTest", contacts)
		for c in rl.contacts:
			if c.full_name == "Opted Out WA":
				c.unsubscribed_whatsapp = 1
			elif c.full_name == "Invalid Phone":
				c.phone_status = phone_validator.INVALID
			elif c.full_name == "Email Only":
				c.email_status = "ok"
		rl.save()

		# WhatsApp
		wa = rl_api.get_reachable_contacts(rl.name, "WhatsApp")
		wa_addresses = [item["address"] for item in wa]
		self.assertEqual(len(wa), 1)
		self.assertEqual(wa[0]["recipient_name"], "Valid WhatsApp & SMS")
		self.assertEqual(wa[0]["address"], "+919876543210")
		self.assertNotIn("+919876543211", wa_addresses)

		# SMS
		sms = rl_api.get_reachable_contacts(rl.name, "SMS")
		self.assertEqual(len(sms), 2)  # "Valid WhatsApp & SMS" + "Opted Out WA" (since only opted out of WA)

		# Email
		em = rl_api.get_reachable_contacts(rl.name, "Email")
		em_addresses = [item["address"] for item in em]
		self.assertIn("email_only@example.com", em_addresses)

		# Unknown channel rejection
		with self.assertRaises(frappe.ValidationError):
			rl_api.get_reachable_contacts(rl.name, "Telegram")

	# ── DocType & File Import Endpoints ─────────────────────────────────

	def test_search_doctypes_and_fields(self):
		"""search_doctypes and get_doctype_fields inspect allow-listed doctypes."""
		results = rl_api.search_doctypes(query="Contact")
		self.assertIn("Contact", results)

		fields_info = rl_api.get_doctype_fields("Contact")
		self.assertIn("fields", fields_info)
		self.assertIn("defaults", fields_info)
		self.assertIn("mobile_no", fields_info["fields"])

	def test_fetch_doctype_contacts(self):
		"""fetch_doctype_contacts and alias fetch_doctype_members pull records in memory."""
		# Test fetch_doctype_contacts
		contacts = rl_api.fetch_doctype_contacts("Contact", phone_field="mobile_no", limit=5)
		self.assertIsInstance(contacts, list)

		# Test alias fetch_doctype_members
		members = rl_api.fetch_doctype_members("Contact", phone_field="mobile_no", limit=5)
		self.assertEqual(len(contacts), len(members))

		# Unreadable or disallowed DocType must raise PermissionError
		with self.assertRaises(frappe.PermissionError):
			rl_api.fetch_doctype_contacts("Social Post")

	def test_fetch_doctype_contacts_limit_cap(self):
		"""fetch_doctype_contacts must clamp limit to MAX_LIST_LIMIT when positive and exceeding cap."""
		with patch("frappe.get_list", return_value=[]) as mock_get_list:
			# Positive limit above cap must be clamped to MAX_LIST_LIMIT
			rl_api.fetch_doctype_contacts("Contact", limit=rl_api.MAX_LIST_LIMIT + 500)
			contact_calls = [c for c in mock_get_list.call_args_list if c.args and c.args[0] == "Contact"]
			self.assertTrue(contact_calls)
			self.assertEqual(contact_calls[-1].kwargs.get("limit"), rl_api.MAX_LIST_LIMIT)

		with patch("frappe.get_list", return_value=[]) as mock_get_list:
			# Limit below cap remains unchanged
			rl_api.fetch_doctype_contacts("Contact", limit=50)
			contact_calls = [c for c in mock_get_list.call_args_list if c.args and c.args[0] == "Contact"]
			self.assertTrue(contact_calls)
			self.assertEqual(contact_calls[-1].kwargs.get("limit"), 50)

		with patch("frappe.get_list", return_value=[]) as mock_get_list:
			# Zero limit remains 0 (meaning all)
			rl_api.fetch_doctype_contacts("Contact", limit=0)
			contact_calls = [c for c in mock_get_list.call_args_list if c.args and c.args[0] == "Contact"]
			self.assertTrue(contact_calls)
			self.assertEqual(contact_calls[-1].kwargs.get("limit"), 0)

	def test_file_preview_parse_and_import(self):
		"""preview_file, parse_file_rows, and import_contacts handle file parsing adapter calls."""
		frappe.set_user(self.user_mgr)
		rl = self._create_recipient_list("FileImportRL")

		with patch("msuite_campaigns.audience.validation_import_adapter.preview", return_value={"columns": ["Phone", "Email"]}):
			prev = rl_api.preview_file("/files/mock.csv")
			self.assertEqual(prev["columns"], ["Phone", "Email"])

		mock_rows = [{"raw_phone": "9876543210", "raw_email": "file@example.com"}]
		with patch("msuite_campaigns.audience.validation_import_adapter.rows_from_file", return_value=mock_rows):
			parsed = rl_api.parse_file_rows("/files/mock.csv")
			self.assertEqual(len(parsed), 1)

			imp = rl_api.import_contacts(rl.name, "/files/mock.csv", validate_now=1)
			self.assertEqual(imp["added"], 1)

		rl.reload()
		self.assertEqual(rl.source, "CSV Upload")
		self.assertEqual(len(rl.contacts), 1)

	def test_validate_recipient_list_enqueues_past_inline_limit(self):
		"""validate_recipient_list must enqueue a background task when row count exceeds INLINE_LIMIT."""
		frappe.set_user(self.user_mgr)
		rl = self._create_recipient_list("LargeListRL")

		with patch("frappe.db.count", return_value=15000), patch("frappe.enqueue") as mock_enqueue:
			res = rl_api.validate_recipient_list(rl.name, force=1)
			self.assertTrue(res["queued"])
			self.assertEqual(res["total"], 15000)
			mock_enqueue.assert_called_once_with(
				"msuite_campaigns.audience.api.v1.recipient_list._validate_job",
				queue="long",
				timeout=3600,
				job_name=f"validate_recipient_list_{rl.name}",
				recipient_list=rl.name,
				force=1,
			)

	def test_validation_and_summary(self):
		"""validate_recipient_list and get_validation_summary work as expected."""
		rl = self._create_recipient_list("ValSummaryTest", [
			{"full_name": "Val User", "raw_phone": "9876543210", "raw_email": "val@example.com"}
		])
		val_res = rl_api.validate_recipient_list(rl.name)
		self.assertFalse(val_res["queued"])

		summary = rl_api.get_validation_summary(rl.name)
		self.assertEqual(summary["total"], 1)
		self.assertIn("channels", summary)
		self.assertIn("WhatsApp", summary["channels"])
		self.assertEqual(summary["channels"]["WhatsApp"]["reachable"], 1)

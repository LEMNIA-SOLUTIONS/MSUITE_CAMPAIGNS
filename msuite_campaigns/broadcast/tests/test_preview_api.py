# Copyright (c) 2026, MSuite and contributors
# For license information, please see license.txt

"""Tests for preview_broadcast whitelisted API (Task R0).

Validates preview_broadcast authorization, payload JSON parsing, channel whitelisting,
unknown recipient list rejection, individual source reachability calculation,
and zero-reachable non-error behaviour.
"""

from __future__ import annotations

import json

import frappe
from frappe.tests import IntegrationTestCase

from msuite_campaigns.broadcast.api.v1 import broadcast as broadcast_api

TEST_PREFIX = "ZZTestPrevAPI "


def _user(role: str | None) -> str:
	"""Helper to create or update a test user with a given role."""
	email = f"perm-prev-{(role or 'none').lower().replace(' ', '-')}@example.com"
	if not frappe.db.exists("User", email):
		user = frappe.get_doc({
			"doctype": "User",
			"email": email,
			"first_name": "PermPrevApi",
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


class TestPreviewBroadcastApi(IntegrationTestCase):
	"""Test suite for broadcast.api.v1.broadcast.preview_broadcast."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.user_none = _user(None)

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		self.cleanup_recipient_lists: list[str] = []

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()
		for name in self.cleanup_recipient_lists:
			if frappe.db.exists("Recipient List", name):
				frappe.delete_doc("Recipient List", name, force=True, ignore_permissions=True)

	def _make_recipient_list(self, contacts: list[dict]) -> "frappe.model.document.Document":
		doc = frappe.get_doc({
			"doctype": "Recipient List",
			"list_name": f"{TEST_PREFIX}RL {frappe.generate_hash(length=5)}",
			"contacts": contacts,
		})
		doc.flags.ignore_permissions = True
		doc.insert(ignore_links=True)
		self.cleanup_recipient_lists.append(doc.name)
		return doc

	def test_permission_denied_without_read_permission(self):
		"""User without read permission on Broadcast receives frappe.PermissionError."""
		frappe.set_user(self.user_none)
		payload = json.dumps({"channels": ["WhatsApp"]})
		with self.assertRaises(frappe.PermissionError):
			broadcast_api.preview_broadcast(payload=payload)

	def test_guest_rejected(self):
		"""Guest user receives frappe.PermissionError."""
		frappe.set_user("Guest")
		payload = json.dumps({"channels": ["WhatsApp"]})
		with self.assertRaises(frappe.PermissionError):
			broadcast_api.preview_broadcast(payload=payload)

	def test_bad_json_raises_validation_error(self):
		"""Invalid JSON payload string raises frappe.ValidationError."""
		frappe.set_user("Administrator")
		with self.assertRaises(frappe.ValidationError):
			broadcast_api.preview_broadcast(payload="not-valid-json{")

		with self.assertRaises(frappe.ValidationError):
			broadcast_api.preview_broadcast(payload="123")

	def test_unknown_recipient_list_raises_error(self):
		"""Targeting a nonexistent Recipient List raises frappe.DoesNotExistError."""
		frappe.set_user("Administrator")
		payload = json.dumps({
			"recipient_source": "Recipient List",
			"recipient_list": "NON_EXISTENT_LIST_9999",
			"channels": ["WhatsApp"],
		})
		with self.assertRaises(frappe.DoesNotExistError):
			broadcast_api.preview_broadcast(payload=payload)

	def test_channel_not_in_supported_channels_rejected(self):
		"""A channel not in WhatsApp/Email/SMS is rejected with ValidationError."""
		frappe.set_user("Administrator")
		payload = json.dumps({
			"recipient_source": "Individual",
			"channels": ["WhatsApp", "Telegram"],
			"individual_rows": [{"address": "+919811100001", "recipient_name": "Alice"}],
		})
		with self.assertRaises(frappe.ValidationError):
			broadcast_api.preview_broadcast(payload=payload)

	def test_individual_source_with_rows(self):
		"""Individual source calculates reachable vs dropped correctly without writing to DB."""
		frappe.set_user("Administrator")
		payload = json.dumps({
			"recipient_source": "Individual",
			"channels": ["WhatsApp", "Email"],
			"individual_rows": [
				{"address": "+919811100001", "recipient_name": "User 1"},
				{"address": "user2@example.com", "recipient_name": "User 2"},
			],
		})
		res = broadcast_api.preview_broadcast(payload=payload)
		self.assertIn("channels", res)
		self.assertIn("total", res)
		self.assertEqual(res["total"], 2)
		self.assertIn("WhatsApp", res["channels"])
		self.assertIn("Email", res["channels"])
		self.assertEqual(res["channels"]["WhatsApp"]["reachable"], 1)
		self.assertEqual(res["channels"]["WhatsApp"]["dropped"], 1)
		self.assertEqual(res["channels"]["Email"]["reachable"], 1)
		self.assertEqual(res["channels"]["Email"]["dropped"], 1)

	def test_recipient_list_zero_reachable_contacts_returns_reachable_zero(self):
		"""Recipient List with zero reachable contacts returns reachable 0 without error."""
		frappe.set_user("Administrator")
		rec_list = self._make_recipient_list([
			{
				"full_name": "Email Only Contact",
				"phone_e164": "",
				"phone_type": "",
				"phone_status": "",
				"email_normalized": "emailonly@example.com",
				"email_status": "ok",
			}
		])
		payload = json.dumps({
			"recipient_source": "Recipient List",
			"recipient_list": rec_list.name,
			"channels": ["WhatsApp"],
		})
		res = broadcast_api.preview_broadcast(payload=payload)
		self.assertEqual(res["total"], 1)
		self.assertIn("WhatsApp", res["channels"])
		self.assertEqual(res["channels"]["WhatsApp"]["reachable"], 0)
		self.assertEqual(res["channels"]["WhatsApp"]["dropped"], 1)

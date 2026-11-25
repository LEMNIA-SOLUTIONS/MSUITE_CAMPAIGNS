# Copyright (c) 2026, MSuite and contributors
# For license information, please see license.txt

"""Tests for launch_ai_call_broadcast whitelisted API (Task R0b).

Validates write permissions on Broadcast, payload JSON decoding, unknown
recipient list rejection, unregistered adapter handling, no-reachable-phone
validation error, member list mapping, whitelist sanitization of configuration,
optional campaign and scheduled_time handling, and delegation to adapter build & launch.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from msuite_campaigns.broadcast.api.v1 import broadcast as broadcast_api

TEST_PREFIX = "ZZTestAiCall "


def _user(role: str | None) -> str:
	"""Helper to create or update a test user with a given role."""
	email = f"perm-aicall-{(role or 'none').lower().replace(' ', '-')}@example.com"
	if not frappe.db.exists("User", email):
		user = frappe.get_doc({
			"doctype": "User",
			"email": email,
			"first_name": "PermAiCallApi",
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


class TestAiCallLaunchApi(IntegrationTestCase):
	"""Test suite for broadcast.api.v1.broadcast.launch_ai_call_broadcast."""

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

	def test_permission_denied_without_write_permission(self):
		"""User without write permission on Broadcast receives frappe.PermissionError."""
		frappe.set_user(self.user_none)
		payload = json.dumps({"recipient_list": "RL-DUMMY", "config": {}})
		with self.assertRaises(frappe.PermissionError):
			broadcast_api.launch_ai_call_broadcast(payload=payload)

	def test_guest_rejected(self):
		"""Guest user receives frappe.PermissionError."""
		frappe.set_user("Guest")
		payload = json.dumps({"recipient_list": "RL-DUMMY", "config": {}})
		with self.assertRaises(frappe.PermissionError):
			broadcast_api.launch_ai_call_broadcast(payload=payload)

	def test_bad_json_raises_validation_error(self):
		"""Invalid JSON payload string or non-dict raises frappe.ValidationError."""
		frappe.set_user("Administrator")
		with self.assertRaises(frappe.ValidationError):
			broadcast_api.launch_ai_call_broadcast(payload="not-json{")

		with self.assertRaises(frappe.ValidationError):
			broadcast_api.launch_ai_call_broadcast(payload="[1, 2, 3]")

	def test_unknown_recipient_list_raises_error(self):
		"""Targeting an unknown Recipient List raises DoesNotExistError or ValidationError."""
		frappe.set_user("Administrator")
		payload = json.dumps({
			"recipient_list": "NON_EXISTENT_RECIPIENT_LIST_9999",
			"config": {"assistant": "test-assistant"},
		})
		with self.assertRaises((frappe.DoesNotExistError, frappe.ValidationError)):
			broadcast_api.launch_ai_call_broadcast(payload=payload)

	def test_adapter_not_registered_raises_validation_error(self):
		"""Unregistered ai_call adapter (ai_calling not installed) raises ValidationError."""
		frappe.set_user("Administrator")
		rec_list = self._make_recipient_list([
			{
				"full_name": "Valid Phone Contact",
				"phone_e164": "+919811100001",
				"phone_type": "Mobile",
				"phone_status": "Valid",
			}
		])
		payload = json.dumps({
			"recipient_list": rec_list.name,
			"config": {"assistant": "test-assistant"},
		})
		with patch("msuite_campaigns.campaign.services.relationships.get_adapters", return_value={}):
			with self.assertRaises(frappe.ValidationError):
				broadcast_api.launch_ai_call_broadcast(payload=payload)

	def test_no_reachable_phone_contacts_raises_validation_error(self):
		"""Recipient List with zero reachable phone contacts raises ValidationError."""
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
		mock_adapter = {
			"key": "ai_call",
			"build": MagicMock(),
			"launch": MagicMock(),
		}
		payload = json.dumps({
			"recipient_list": rec_list.name,
			"config": {"assistant": "test-assistant"},
		})
		with patch(
			"msuite_campaigns.campaign.services.relationships.get_adapters",
			return_value={"ai_call": mock_adapter},
		):
			with self.assertRaises(frappe.ValidationError):
				broadcast_api.launch_ai_call_broadcast(payload=payload)
		mock_adapter["build"].assert_not_called()
		mock_adapter["launch"].assert_not_called()

	def test_happy_path_calls_build_and_launch(self):
		"""Happy path maps members, filters config, calls build then launch, and returns name."""
		frappe.set_user("Administrator")
		rec_list = self._make_recipient_list([
			{
				"full_name": "Alice Smith",
				"phone_e164": "+919811100001",
				"phone_type": "Mobile",
				"phone_status": "Valid",
			},
			{
				"full_name": "Bob Jones",
				"phone_e164": "+919811100002",
				"phone_type": "Mobile",
				"phone_status": "Valid",
			},
		])

		mock_adapter = {
			"key": "ai_call",
			"build": MagicMock(return_value={"name": "AICB-TEST-001", "queued": 2}),
			"launch": MagicMock(return_value="Running"),
		}

		payload = {
			"recipient_list": rec_list.name,
			"campaign": "CAMP-2026-0001",
			"config": {
				"assistant": "asst_123",
				"phone_number": "+1234567890",
				"end_date": "2026-12-10",
				"calling_start_time": "10:00",
				"calling_end_time": "18:00",
				"timezone": "Asia/Kolkata",
				"max_calls_per_hour": 30,
				"retry_count": 2,
				"instructions": "Be polite and concise.",
			},
		}

		with patch(
			"msuite_campaigns.campaign.services.relationships.get_adapters",
			return_value={"ai_call": mock_adapter},
		):
			res = broadcast_api.launch_ai_call_broadcast(payload=json.dumps(payload))

		self.assertEqual(res, {"name": "AICB-TEST-001"})
		mock_adapter["build"].assert_called_once()
		call_args, _ = mock_adapter["build"].call_args
		campaign_arg, audience_doc_arg, members_arg, config_arg = call_args

		self.assertEqual(campaign_arg, "CAMP-2026-0001")
		self.assertEqual(audience_doc_arg.name, rec_list.name)
		self.assertEqual(
			members_arg,
			[
				{"phone_e164": "+919811100001", "full_name": "Alice Smith"},
				{"phone_e164": "+919811100002", "full_name": "Bob Jones"},
			],
		)
		self.assertEqual(
			config_arg,
			{
				"assistant": "asst_123",
				"phone_number": "+1234567890",
				"end_date": "2026-12-10",
				"calling_start_time": "10:00",
				"calling_end_time": "18:00",
				"timezone": "Asia/Kolkata",
				"max_calls_per_hour": 30,
				"retry_count": 2,
				"instructions": "Be polite and concise.",
			},
		)

		mock_adapter["launch"].assert_called_once_with("AICB-TEST-001", config_arg)

	def test_campaign_optional_and_passed_through(self):
		"""When campaign is omitted, None is passed to adapter build."""
		frappe.set_user("Administrator")
		rec_list = self._make_recipient_list([
			{
				"full_name": "Alice Smith",
				"phone_e164": "+919811100001",
				"phone_type": "Mobile",
				"phone_status": "Valid",
			}
		])

		mock_adapter = {
			"key": "ai_call",
			"build": MagicMock(return_value={"name": "AICB-TEST-002", "queued": 1}),
			"launch": MagicMock(return_value="Running"),
		}

		payload = {
			"recipient_list": rec_list.name,
			"config": {"assistant": "asst_123"},
		}

		with patch(
			"msuite_campaigns.campaign.services.relationships.get_adapters",
			return_value={"ai_call": mock_adapter},
		):
			res = broadcast_api.launch_ai_call_broadcast(payload=json.dumps(payload))

		self.assertEqual(res, {"name": "AICB-TEST-002"})
		mock_adapter["build"].assert_called_once()
		campaign_arg = mock_adapter["build"].call_args[0][0]
		self.assertIsNone(campaign_arg)

	def test_config_whitelist_drops_unsupported_keys(self):
		"""Config keys outside the whitelist are dropped before passing to build and launch."""
		frappe.set_user("Administrator")
		rec_list = self._make_recipient_list([
			{
				"full_name": "Alice Smith",
				"phone_e164": "+919811100001",
				"phone_type": "Mobile",
				"phone_status": "Valid",
			}
		])

		mock_adapter = {
			"key": "ai_call",
			"build": MagicMock(return_value={"name": "AICB-TEST-003", "queued": 1}),
			"launch": MagicMock(return_value="Running"),
		}

		payload = {
			"recipient_list": rec_list.name,
			"config": {
				"assistant": "asst_123",
				"phone_number": "+1234567890",
				"unsupported_key": "drop_me",
				"extra_param": 12345,
				"malicious_injection": "DROP TABLE tabUser",
			},
		}

		with patch(
			"msuite_campaigns.campaign.services.relationships.get_adapters",
			return_value={"ai_call": mock_adapter},
		):
			broadcast_api.launch_ai_call_broadcast(payload=json.dumps(payload))

		config_arg = mock_adapter["build"].call_args[0][3]
		self.assertEqual(
			config_arg,
			{
				"assistant": "asst_123",
				"phone_number": "+1234567890",
			},
		)
		self.assertNotIn("unsupported_key", config_arg)
		self.assertNotIn("extra_param", config_arg)
		self.assertNotIn("malicious_injection", config_arg)

	def test_scheduled_time_optional(self):
		"""Optional scheduled_time in payload maps to start_date in config."""
		frappe.set_user("Administrator")
		rec_list = self._make_recipient_list([
			{
				"full_name": "Alice Smith",
				"phone_e164": "+919811100001",
				"phone_type": "Mobile",
				"phone_status": "Valid",
			}
		])

		mock_adapter = {
			"key": "ai_call",
			"build": MagicMock(return_value={"name": "AICB-TEST-004", "queued": 1}),
			"launch": MagicMock(return_value="Scheduled"),
		}

		payload = {
			"recipient_list": rec_list.name,
			"scheduled_time": "2026-12-15 14:00:00",
			"config": {
				"assistant": "asst_123",
			},
		}

		with patch(
			"msuite_campaigns.campaign.services.relationships.get_adapters",
			return_value={"ai_call": mock_adapter},
		):
			broadcast_api.launch_ai_call_broadcast(payload=json.dumps(payload))

		config_arg = mock_adapter["build"].call_args[0][3]
		self.assertEqual(config_arg.get("start_date"), "2026-12-15")

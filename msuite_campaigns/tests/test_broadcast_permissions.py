"""Unit and integration tests for Broadcast permissions and query conditions (P1).

Validates role x channel matrix, mixed-channel partial roles, query condition SQL
fragments and execution via get_list, and roles MATRIX entries.
"""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from msuite_campaigns.broadcast.permissions import (
	broadcast_query_conditions,
	has_permission,
	permitted_channels,
	recipient_has_permission,
	recipient_query_conditions,
)
from msuite_core.services.permissions.roles import MATRIX


def _get_or_create_user(email: str, *roles: str) -> str:
	"""Helper to create or update a test user with specified roles.

	Params:
		email: Email address of user.
		*roles: Roles to assign to user.

	Returns:
		str: User email.

	Exceptions:
		None.

	Side effects:
		Inserts or updates User doc in DB.
	"""
	if not frappe.db.exists("User", email):
		user = frappe.get_doc({
			"doctype": "User",
			"email": email,
			"first_name": "Test",
			"send_welcome_email": 0,
		}).insert(ignore_permissions=True)
	else:
		user = frappe.get_doc("User", email)

	for role in roles:
		if role and not frappe.db.exists("Role", role):
			frappe.get_doc({"doctype": "Role", "role_name": role, "desk_access": 1}).insert(
				ignore_permissions=True
			)
		if role and role not in [r.role for r in user.roles]:
			user.add_roles(role)

	return email


class TestBroadcastPermissions(IntegrationTestCase):
	"""Test suite validating permission logic and query conditions for Broadcast doctypes.

	Side effects:
		May query and write temporary test documents.
	"""

	def test_roles_matrix_has_three_new_keys(self):
		"""Validate that MATRIX has Recipient List, Broadcast, and Broadcast Recipient keys.

		Params: None.
		Returns: None.
		Raises: AssertionError if MATRIX entries drift from specification.
		Side effects: None.
		"""
		self.assertIn("Recipient List", MATRIX)
		self.assertIn("Broadcast", MATRIX)
		self.assertIn("Broadcast Recipient", MATRIX)

		expected_recipient_list = {
			"Campaign Manager": "RWCDrEPMH",
			"Campaign User": "RWCrPM",
			"MSuite Viewer": "Rr",
			"WhatsApp Manager": "Rr",
			"WhatsApp User": "Rr",
			"SMS Manager": "Rr",
			"SMS User": "Rr",
			"Email Manager": "Rr",
			"Email User": "Rr",
		}
		self.assertEqual(MATRIX["Recipient List"], expected_recipient_list)

		expected_broadcast = {
			"Campaign Manager": "RWCDrEPMH",
			"Campaign User": "RWCrPM",
			"WhatsApp Manager": "RWCDrEPMH",
			"WhatsApp User": "RWCrPM",
			"Email Manager": "RWCDrEPMH",
			"Email User": "RWCrPM",
			"SMS Manager": "RWCDrEPMH",
			"SMS User": "RWCrPM",
			"MSuite Viewer": "Rr",
		}
		self.assertEqual(MATRIX["Broadcast"], expected_broadcast)
		self.assertEqual(MATRIX["Broadcast Recipient"], expected_broadcast)

	def test_permitted_channels_per_role(self):
		"""Validate permitted_channels for each role type.

		Params: None.
		Returns: None.
		Raises: AssertionError if permitted channels do not match.
		Side effects: None.
		"""
		with patch("frappe.get_roles", return_value=["Guest"]):
			self.assertEqual(permitted_channels("Guest"), set())

		with patch("frappe.get_roles", return_value=["Some Other Role"]):
			self.assertEqual(permitted_channels("other@example.com"), set())

		with patch("frappe.get_roles", return_value=["WhatsApp User"]):
			self.assertEqual(permitted_channels("wa@example.com"), {"WhatsApp"})

		with patch("frappe.get_roles", return_value=["WhatsApp Manager"]):
			self.assertEqual(permitted_channels("wam@example.com"), {"WhatsApp"})

		with patch("frappe.get_roles", return_value=["Email User"]):
			self.assertEqual(permitted_channels("em@example.com"), {"Email"})

		with patch("frappe.get_roles", return_value=["SMS User"]):
			self.assertEqual(permitted_channels("sms@example.com"), {"SMS"})

		with patch("frappe.get_roles", return_value=["WhatsApp User", "Email User"]):
			self.assertEqual(permitted_channels("wa_em@example.com"), {"WhatsApp", "Email"})

		with patch("frappe.get_roles", return_value=["MSuite Viewer"]):
			self.assertEqual(permitted_channels("viewer@example.com"), {"WhatsApp", "Email", "SMS"})

		with patch("frappe.get_roles", return_value=["Campaign Manager"]):
			self.assertEqual(permitted_channels("cm@example.com"), {"WhatsApp", "Email", "SMS"})

		with patch("frappe.get_roles", return_value=["System Manager"]):
			self.assertEqual(permitted_channels("sm@example.com"), {"WhatsApp", "Email", "SMS"})

		self.assertEqual(permitted_channels("Administrator"), {"WhatsApp", "Email", "SMS"})

	def test_role_x_channel_matrix(self):
		"""Validate permission checks across role x channel matrix for CRUD operations.

		Roles: WhatsApp User, Email User, SMS User, MSuite Viewer, Campaign Manager, Guest.
		Actions: create, read, write, delete.

		Params: None.
		Returns: None.
		Raises: AssertionError if permission grants violate matrix.
		Side effects: None.
		"""
		channels = ["WhatsApp", "Email", "SMS"]
		role_users = {
			"WhatsApp User": ["WhatsApp User"],
			"Email User": ["Email User"],
			"SMS User": ["SMS User"],
			"MSuite Viewer": ["MSuite Viewer"],
			"Campaign Manager": ["Campaign Manager"],
			"Guest": ["Guest"],
		}

		for ch in channels:
			doc = frappe._dict({
				"doctype": "Broadcast",
				"name": f"test-bc-{ch.lower()}",
				"channels": [frappe._dict({"channel": ch})],
			})

			for role_name, roles in role_users.items():
				user = "Guest" if role_name == "Guest" else f"test_{role_name.lower().replace(' ', '_')}@example.com"
				with patch("frappe.get_roles", return_value=roles):
					can_read = has_permission(doc, ptype="read", user=user)
					can_write = has_permission(doc, ptype="write", user=user)
					can_create = has_permission(doc, ptype="create", user=user)
					can_delete = has_permission(doc, ptype="delete", user=user)

					if role_name == "Guest":
						self.assertFalse(can_read)
						self.assertFalse(can_write)
						self.assertFalse(can_create)
						self.assertFalse(can_delete)
					elif role_name == "Campaign Manager":
						self.assertTrue(can_read)
						self.assertTrue(can_write)
						self.assertTrue(can_create)
						self.assertTrue(can_delete)
					elif role_name == "MSuite Viewer":
						self.assertTrue(can_read)
						self.assertFalse(can_write)
						self.assertFalse(can_create)
						self.assertFalse(can_delete)
					elif role_name.startswith(ch):
						# Matching channel user
						self.assertTrue(can_read, f"{role_name} should read {ch}")
						self.assertTrue(can_write, f"{role_name} should write {ch}")
						self.assertTrue(can_create, f"{role_name} should create {ch}")
						self.assertTrue(can_delete, f"{role_name} should delete {ch}")
					else:
						# Non-matching channel user
						self.assertFalse(can_read, f"{role_name} should not read {ch}")
						self.assertFalse(can_write, f"{role_name} should not write {ch}")
						self.assertFalse(can_create, f"{role_name} should not create {ch}")
						self.assertFalse(can_delete, f"{role_name} should not delete {ch}")

	def test_mixed_channel_broadcast_partial_roles_p1(self):
		"""Validate P1 mixed-channel broadcast: read allowed with partial role, write denied.

		A Broadcast spanning WhatsApp and Email requires BOTH channel roles to write/delete/create,
		but only AT LEAST ONE role to read.

		Params: None.
		Returns: None.
		Raises: AssertionError if P1 rules are violated.
		Side effects: None.
		"""
		doc = frappe._dict({
			"doctype": "Broadcast",
			"name": "test-bc-wa-email",
			"channels": [
				frappe._dict({"channel": "WhatsApp"}),
				frappe._dict({"channel": "Email"}),
			],
		})

		# WhatsApp User only: can read, but cannot write or delete
		with patch("frappe.get_roles", return_value=["WhatsApp User"]):
			user = "wa_only@example.com"
			self.assertTrue(has_permission(doc, ptype="read", user=user))
			self.assertFalse(has_permission(doc, ptype="write", user=user))
			self.assertFalse(has_permission(doc, ptype="delete", user=user))
			self.assertFalse(has_permission(doc, ptype="create", user=user))

		# Email User only: can read, but cannot write or delete
		with patch("frappe.get_roles", return_value=["Email User"]):
			user = "em_only@example.com"
			self.assertTrue(has_permission(doc, ptype="read", user=user))
			self.assertFalse(has_permission(doc, ptype="write", user=user))
			self.assertFalse(has_permission(doc, ptype="delete", user=user))
			self.assertFalse(has_permission(doc, ptype="create", user=user))

		# SMS User: neither channel: cannot read, cannot write
		with patch("frappe.get_roles", return_value=["SMS User"]):
			user = "sms_only@example.com"
			self.assertFalse(has_permission(doc, ptype="read", user=user))
			self.assertFalse(has_permission(doc, ptype="write", user=user))

		# User with BOTH WhatsApp User and Email User: can read AND write
		with patch("frappe.get_roles", return_value=["WhatsApp User", "Email User"]):
			user = "both@example.com"
			self.assertTrue(has_permission(doc, ptype="read", user=user))
			self.assertTrue(has_permission(doc, ptype="write", user=user))
			self.assertTrue(has_permission(doc, ptype="delete", user=user))
			self.assertTrue(has_permission(doc, ptype="create", user=user))

		# Campaign Manager: admin can read and write
		with patch("frappe.get_roles", return_value=["Campaign Manager"]):
			user = "cm@example.com"
			self.assertTrue(has_permission(doc, ptype="read", user=user))
			self.assertTrue(has_permission(doc, ptype="write", user=user))

		# MSuite Viewer: can read, cannot write
		with patch("frappe.get_roles", return_value=["MSuite Viewer"]):
			user = "viewer@example.com"
			self.assertTrue(has_permission(doc, ptype="read", user=user))
			self.assertFalse(has_permission(doc, ptype="write", user=user))

		# Guest: denied
		with patch("frappe.get_roles", return_value=["Guest"]):
			self.assertFalse(has_permission(doc, ptype="read", user="Guest"))
			self.assertFalse(has_permission(doc, ptype="write", user="Guest"))

	def test_broadcast_with_no_channels_yet(self):
		"""Validate doc with no channel rows allows create for any channel role or admin.

		Params: None.
		Returns: None.
		Raises: AssertionError if doc creation rules with empty channels are violated.
		Side effects: None.
		"""
		doc = frappe._dict({
			"doctype": "Broadcast",
			"name": "test-bc-empty",
			"channels": [],
		})

		with patch("frappe.get_roles", return_value=["WhatsApp User"]):
			self.assertTrue(has_permission(doc, ptype="create", user="wa@example.com"))
			self.assertTrue(has_permission(doc, ptype="read", user="wa@example.com"))

		with patch("frappe.get_roles", return_value=["Campaign Manager"]):
			self.assertTrue(has_permission(doc, ptype="create", user="cm@example.com"))

		with patch("frappe.get_roles", return_value=["Unrelated Role"]):
			self.assertFalse(has_permission(doc, ptype="create", user="unrelated@example.com"))

		with patch("frappe.get_roles", return_value=["Guest"]):
			self.assertFalse(has_permission(doc, ptype="create", user="Guest"))

	def test_broadcast_recipient_permission(self):
		"""Validate recipient_has_permission filtering on recipient channel column.

		Params: None.
		Returns: None.
		Raises: AssertionError if recipient permissions differ from expectations.
		Side effects: None.
		"""
		doc = frappe._dict({
			"doctype": "Broadcast Recipient",
			"channel": "WhatsApp",
		})

		with patch("frappe.get_roles", return_value=["WhatsApp User"]):
			self.assertTrue(recipient_has_permission(doc, ptype="read", user="wa@example.com"))
			self.assertTrue(recipient_has_permission(doc, ptype="write", user="wa@example.com"))

		with patch("frappe.get_roles", return_value=["Email User"]):
			self.assertFalse(recipient_has_permission(doc, ptype="read", user="em@example.com"))
			self.assertFalse(recipient_has_permission(doc, ptype="write", user="em@example.com"))

		with patch("frappe.get_roles", return_value=["MSuite Viewer"]):
			self.assertTrue(recipient_has_permission(doc, ptype="read", user="viewer@example.com"))
			self.assertFalse(recipient_has_permission(doc, ptype="write", user="viewer@example.com"))

		with patch("frappe.get_roles", return_value=["Campaign Manager"]):
			self.assertTrue(recipient_has_permission(doc, ptype="read", user="cm@example.com"))
			self.assertTrue(recipient_has_permission(doc, ptype="write", user="cm@example.com"))

		with patch("frappe.get_roles", return_value=["Guest"]):
			self.assertFalse(recipient_has_permission(doc, ptype="read", user="Guest"))

	def test_broadcast_query_conditions_sql_fragments(self):
		"""Validate exact SQL fragments produced by broadcast_query_conditions.

		Params: None.
		Returns: None.
		Raises: AssertionError if SQL condition string does not match specification.
		Side effects: None.
		"""
		with patch("frappe.get_roles", return_value=["Guest"]):
			self.assertEqual(broadcast_query_conditions("Guest"), "1=0")

		with patch("frappe.get_roles", return_value=["Unrelated Role"]):
			self.assertEqual(broadcast_query_conditions("user@example.com"), "1=0")

		with patch("frappe.get_roles", return_value=["Campaign Manager"]):
			self.assertEqual(broadcast_query_conditions("cm@example.com"), "")

		with patch("frappe.get_roles", return_value=["MSuite Viewer"]):
			self.assertEqual(broadcast_query_conditions("viewer@example.com"), "")

		self.assertEqual(broadcast_query_conditions("Administrator"), "")

		with patch("frappe.get_roles", return_value=["WhatsApp User"]):
			expected_wa = (
				"exists (select 1 from `tabBroadcast Channel` bc "
				"where bc.parent = `tabBroadcast`.name and bc.channel in ('WhatsApp'))"
			)
			self.assertEqual(broadcast_query_conditions("wa@example.com"), expected_wa)

		with patch("frappe.get_roles", return_value=["WhatsApp User", "Email User"]):
			expected_wa_em = (
				"exists (select 1 from `tabBroadcast Channel` bc "
				"where bc.parent = `tabBroadcast`.name and bc.channel in ('Email', 'WhatsApp'))"
			)
			self.assertEqual(broadcast_query_conditions("wa_em@example.com"), expected_wa_em)

	def test_recipient_query_conditions(self):
		"""Validate SQL conditions for Broadcast Recipient.

		Params: None.
		Returns: None.
		Raises: AssertionError if SQL conditions do not match specification.
		Side effects: None.
		"""
		with patch("frappe.get_roles", return_value=["Guest"]):
			self.assertEqual(recipient_query_conditions("Guest"), "1=0")

		with patch("frappe.get_roles", return_value=["MSuite Viewer"]):
			self.assertEqual(recipient_query_conditions("viewer@example.com"), "")

		with patch("frappe.get_roles", return_value=["WhatsApp User"]):
			expected = "`tabBroadcast Recipient`.channel in ('WhatsApp')"
			self.assertEqual(recipient_query_conditions("wa@example.com"), expected)

	def test_broadcast_query_conditions_real_get_list(self):
		"""Validate that broadcast_query_conditions executes cleanly against the real table.

		Executes frappe.get_list against Broadcast as a user with WhatsApp User role.

		Params: None.
		Returns: None.
		Raises: Exception if SQL syntax error or execution failure occurs.
		Side effects:
			Sets frappe.session.user during test execution.
		"""
		user = _get_or_create_user("test_bc_user_wa@example.com", "WhatsApp User")
		orig_user = frappe.session.user
		try:
			frappe.set_user(user)
			# Should execute without SQL syntax error
			result = frappe.get_list("Broadcast", fields=["name"], limit=5)
			self.assertIsInstance(result, list)
		finally:
			frappe.set_user(orig_user)

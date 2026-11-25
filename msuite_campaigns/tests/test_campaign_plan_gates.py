"""Tests for MSuite Campaigns plan gates (campaigns_per_month, campaigns_audiences, campaigns_audience_size)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch
import frappe
from frappe.tests import IntegrationTestCase

from msuite_core.services.plans.plan_enforcer import (
	enforce_gate,
	get_gates,
)


class TestCampaignPlanGates(IntegrationTestCase):
	"""Test suite for CAMPAIGNS product gates."""

	def setUp(self):
		super().setUp()
		self.orig_in_test = getattr(frappe.flags, "in_test", False)
		self.created_audiences = []
		if hasattr(frappe.local, "_plan_gate_warnings"):
			delattr(frappe.local, "_plan_gate_warnings")

	def tearDown(self):
		frappe.flags.in_test = self.orig_in_test
		for name in self.created_audiences:
			if frappe.db.exists("Audience", name):
				frappe.delete_doc("Audience", name, force=True, ignore_permissions=True)
		super().tearDown()

	def _make_audience(self, members_count: int, **kwargs):
		name = f"TestAud_{frappe.generate_hash(length=8)}"
		members = [
			{
				"full_name": f"User {i}",
				"raw_phone": f"+91987654321{i}",
				"raw_email": f"u{i}_{frappe.generate_hash(length=4)}@example.com",
			}
			for i in range(members_count)
		]
		doc = frappe.get_doc({
			"doctype": "Audience",
			"audience_name": name,
			"default_region": "IN",
			"source": "Manual",
			"members": members,
			**kwargs,
		})
		return doc

	def test_campaigns_per_month_gate_registered(self):
		"""Marketing Campaign must be registered in get_gates()."""
		gates = get_gates()
		self.assertIn("Marketing Campaign", gates)
		gate = gates["Marketing Campaign"]
		self.assertEqual(gate.feature, "campaigns_per_month")
		self.assertEqual(gate.product, "CAMPAIGNS")
		self.assertEqual(gate.period, "month")
		self.assertEqual(gate.count_doctype, "Marketing Campaign")

	def test_campaigns_per_month_at_cap_blocks_insert(self):
		"""When monthly campaign usage is at cap, new campaign insert is blocked."""
		frappe.flags.in_test = False
		plan_payload = {
			"plan_name": "CAMPAIGNS-BASIC",
			"products": ["CAMPAIGNS"],
			"features": {"campaigns_per_month": {"enabled": True, "limit": 5}},
		}
		with patch("msuite_core.services.plans.plan_enforcer.get_plan_data", return_value=plan_payload):
			with patch("msuite_core.services.plans.plan_enforcer.get_current_usage", return_value=5):
				doc = MagicMock()
				doc.doctype = "Marketing Campaign"
				doc.name = "CAMP-2026-0001"
				doc.flags.ignore_plan_gate = False
				doc.is_new.return_value = True

				with self.assertRaises(frappe.ValidationError) as ctx:
					enforce_gate(doc, "before_insert")
				self.assertIn("Campaigns limit reached (5/5 this month)", str(ctx.exception))

	def test_campaigns_per_month_under_cap_allows_insert(self):
		"""When monthly campaign usage is under cap, new campaign insert succeeds."""
		frappe.flags.in_test = False
		plan_payload = {
			"plan_name": "CAMPAIGNS-BASIC",
			"products": ["CAMPAIGNS"],
			"features": {"campaigns_per_month": {"enabled": True, "limit": 5}},
		}
		with patch("msuite_core.services.plans.plan_enforcer.get_plan_data", return_value=plan_payload):
			with patch("msuite_core.services.plans.plan_enforcer.get_current_usage", return_value=3):
				doc = MagicMock()
				doc.doctype = "Marketing Campaign"
				doc.name = "CAMP-2026-0001"
				doc.flags.ignore_plan_gate = False
				doc.is_new.return_value = True

				# Must not raise
				enforce_gate(doc, "before_insert")

	def test_campaign_edit_at_cap_is_allowed(self):
		"""Editing an existing campaign when at cap must not be blocked."""
		frappe.flags.in_test = False
		plan_payload = {
			"plan_name": "CAMPAIGNS-BASIC",
			"products": ["CAMPAIGNS"],
			"features": {"campaigns_per_month": {"enabled": True, "limit": 5}},
		}
		with patch("msuite_core.services.plans.plan_enforcer.get_plan_data", return_value=plan_payload):
			with patch("msuite_core.services.plans.plan_enforcer.get_current_usage", return_value=5):
				doc = MagicMock()
				doc.doctype = "Marketing Campaign"
				doc.name = "CAMP-2026-0001"
				doc.flags.ignore_plan_gate = False
				doc.is_new.return_value = False

				# before_insert or before_save on existing row
				enforce_gate(doc, "before_insert")

	def test_audiences_gate_registered(self):
		"""Audience gates include campaigns_audiences check."""
		gates = get_gates()
		self.assertIn("Audience", gates)
		gate = gates["Audience"]
		# Either primary or extra_gates checks campaigns_audiences
		all_features = [gate.feature] + [eg.feature for eg in getattr(gate, "extra_gates", ())]
		self.assertIn("campaigns_audiences", all_features)

	def test_audience_size_over_limit_blocks_insert(self):
		"""Creating an Audience with 4 members when limit is 3 must raise ValidationError."""
		frappe.flags.in_test = False
		plan_payload = {
			"plan_name": "CAMPAIGNS-BASIC",
			"products": ["CAMPAIGNS", "WA"],
			"features": {
				"bulk_messaging": {"enabled": True, "limit": None},
				"campaigns_audiences": {"enabled": True, "limit": 10},
				"campaigns_audience_size": {"enabled": True, "limit": 3},
			},
		}
		with patch("msuite_core.services.plans.plan_enforcer.get_plan_data", return_value=plan_payload):
			doc = self._make_audience(4)
			with self.assertRaises(frappe.ValidationError) as ctx:
				doc.insert(ignore_permissions=True)
			self.created_audiences.append(doc.name)
			self.assertIn("Audience Size limit reached", str(ctx.exception))

	def test_audience_size_at_limit_allows_insert(self):
		"""Creating an Audience with 3 members when limit is 3 succeeds."""
		frappe.flags.in_test = False
		plan_payload = {
			"plan_name": "CAMPAIGNS-BASIC",
			"products": ["CAMPAIGNS", "WA"],
			"features": {
				"bulk_messaging": {"enabled": True, "limit": None},
				"campaigns_audiences": {"enabled": True, "limit": 10},
				"campaigns_audience_size": {"enabled": True, "limit": 3},
			},
		}
		with patch("msuite_core.services.plans.plan_enforcer.get_plan_data", return_value=plan_payload):
			doc = self._make_audience(3)
			doc.insert(ignore_permissions=True)
			self.created_audiences.append(doc.name)
			self.assertTrue(frappe.db.exists("Audience", doc.name))
			self.assertEqual(len(doc.members), 3)

	def test_audience_size_unlimited_allows_insert(self):
		"""Creating an Audience with 4 members when limit is None (unlimited) succeeds."""
		frappe.flags.in_test = False
		plan_payload = {
			"plan_name": "CAMPAIGNS-ENTERPRISE",
			"products": ["CAMPAIGNS", "WA"],
			"features": {
				"bulk_messaging": {"enabled": True, "limit": None},
				"campaigns_audiences": {"enabled": True, "limit": 10},
				"campaigns_audience_size": {"enabled": True, "limit": None},
			},
		}
		with patch("msuite_core.services.plans.plan_enforcer.get_plan_data", return_value=plan_payload):
			doc = self._make_audience(4)
			doc.insert(ignore_permissions=True)
			self.created_audiences.append(doc.name)
			self.assertTrue(frappe.db.exists("Audience", doc.name))

	def test_audience_size_missing_from_payload_allows_insert(self):
		"""Transitional rule: when campaigns_audience_size is missing from plan payload, cap is not enforced."""
		frappe.flags.in_test = False
		plan_payload = {
			"plan_name": "CAMPAIGNS-TRANSITIONAL",
			"products": ["CAMPAIGNS", "WA"],
			"features": {
				"bulk_messaging": {"enabled": True, "limit": None},
				"campaigns_audiences": {"enabled": True, "limit": 10},
			},
		}
		with patch("msuite_core.services.plans.plan_enforcer.get_plan_data", return_value=plan_payload):
			doc = self._make_audience(4)
			doc.insert(ignore_permissions=True)
			self.created_audiences.append(doc.name)
			self.assertTrue(frappe.db.exists("Audience", doc.name))

	def test_audience_size_edit_existing_over_cap_allows_save_unless_rows_added(self):
		"""An existing audience already over the cap can be edited/saved unless member rows are added."""
		frappe.flags.in_test = False
		# Step 1: Create audience with 4 members under uncapped/higher plan
		high_plan = {
			"plan_name": "CAMPAIGNS-HIGH",
			"products": ["CAMPAIGNS", "WA"],
			"features": {
				"bulk_messaging": {"enabled": True, "limit": None},
				"campaigns_audiences": {"enabled": True, "limit": 10},
				"campaigns_audience_size": {"enabled": True, "limit": 10},
			},
		}
		with patch("msuite_core.services.plans.plan_enforcer.get_plan_data", return_value=high_plan):
			doc = self._make_audience(4)
			doc.insert(ignore_permissions=True)
			self.created_audiences.append(doc.name)

		# Step 2: Now plan cap drops to 3 (audience has 4, so it is over the cap)
		restricted_plan = {
			"plan_name": "CAMPAIGNS-BASIC",
			"products": ["CAMPAIGNS", "WA"],
			"features": {
				"bulk_messaging": {"enabled": True, "limit": None},
				"campaigns_audiences": {"enabled": True, "limit": 10},
				"campaigns_audience_size": {"enabled": True, "limit": 3},
			},
		}
		with patch("msuite_core.services.plans.plan_enforcer.get_plan_data", return_value=restricted_plan):
			# Editing metadata without adding rows must succeed
			doc.reload()
			doc.description = "Updated description without adding members"
			doc.save(ignore_permissions=True)

			# Deleting a row (now 3 members) must succeed
			doc.members.pop()
			doc.save(ignore_permissions=True)
			self.assertEqual(len(doc.members), 3)

			# Now re-test adding rows while at or over cap:
			# Add two members so count goes to 5 (> 3)
			doc.append("members", {
				"full_name": "New User 1",
				"raw_phone": "+919876543291",
				"raw_email": "new1@example.com",
			})
			doc.append("members", {
				"full_name": "New User 2",
				"raw_phone": "+919876543292",
				"raw_email": "new2@example.com",
			})
			with self.assertRaises(frappe.ValidationError) as ctx:
				doc.save(ignore_permissions=True)
			self.assertIn("Audience Size limit reached", str(ctx.exception))

	def test_audience_size_ignore_plan_gate_flag(self):
		"""When doc.flags.ignore_plan_gate is True, audience size gate is skipped."""
		frappe.flags.in_test = False
		plan_payload = {
			"plan_name": "CAMPAIGNS-BASIC",
			"products": ["CAMPAIGNS", "WA"],
			"features": {
				"bulk_messaging": {"enabled": True, "limit": None},
				"campaigns_audiences": {"enabled": True, "limit": 10},
				"campaigns_audience_size": {"enabled": True, "limit": 3},
			},
		}
		with patch("msuite_core.services.plans.plan_enforcer.get_plan_data", return_value=plan_payload):
			doc = self._make_audience(4)
			doc.flags.ignore_plan_gate = True
			doc.insert(ignore_permissions=True)
			self.created_audiences.append(doc.name)
			self.assertTrue(frappe.db.exists("Audience", doc.name))

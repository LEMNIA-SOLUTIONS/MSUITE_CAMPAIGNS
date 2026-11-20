"""Unit and integration tests for Broadcast plan gates and quota enforcement.

Validates feature gates per channel, launch quota enforcement, and channel usage calculation.
"""

from unittest.mock import MagicMock, call, patch

import frappe
from frappe.tests import IntegrationTestCase

from msuite_core.services.plans.plan_enforcer import (
	check_broadcast,
	check_broadcast_launch,
	usage_for_channel,
)


class TestBroadcastPlanGate(IntegrationTestCase):
	"""Test suite validating plan feature gates and quota limits for Broadcast.

	Side effects:
		May query the DB for row counts.
	"""

	def test_check_broadcast_whatsapp_gate(self):
		"""Validate check_broadcast blocks WhatsApp if bulk messaging is disabled.

		Params: None.
		Returns: None.
		Raises: AssertionError if ValidationError is not raised or message differs.
		Side effects: None.
		"""
		doc = frappe._dict({
			"doctype": "Broadcast",
			"name": "bc-wa-test",
			"channels": [frappe._dict({"channel": "WhatsApp"})],
		})

		with patch("msuite_core.services.plans.plan_enforcer.is_bulk_enabled", return_value=False):
			with self.assertRaises(frappe.ValidationError) as ctx:
				check_broadcast(doc)
			self.assertIn("Bulk Messaging is not available on your current plan", str(ctx.exception))

		with patch("msuite_core.services.plans.plan_enforcer.is_bulk_enabled", return_value=True):
			# Should not raise
			check_broadcast(doc)

	def test_check_broadcast_sms_gate(self):
		"""Validate check_broadcast blocks SMS if product or feature is disabled.

		Params: None.
		Returns: None.
		Raises: AssertionError if ValidationError is not raised or message differs.
		Side effects: None.
		"""
		doc = frappe._dict({
			"doctype": "Broadcast",
			"name": "bc-sms-test",
			"channels": [frappe._dict({"channel": "SMS"})],
		})

		# SMS product disabled
		with patch("msuite_core.services.plans.plan_enforcer.is_product_enabled", return_value=False):
			with self.assertRaises(frappe.ValidationError) as ctx:
				check_broadcast(doc)
			self.assertIn("SMS is not included in your subscription", str(ctx.exception))

		# SMS product enabled, but bulk feature disabled
		with patch("msuite_core.services.plans.plan_enforcer.is_product_enabled", return_value=True), \
			patch("msuite_core.services.plans.plan_enforcer.is_feature_enabled", return_value=False):
			with self.assertRaises(frappe.ValidationError) as ctx:
				check_broadcast(doc)
			self.assertIn("Bulk Messaging is not available on your current plan", str(ctx.exception))

		# Both enabled
		with patch("msuite_core.services.plans.plan_enforcer.is_product_enabled", return_value=True), \
			patch("msuite_core.services.plans.plan_enforcer.is_feature_enabled", return_value=True):
			check_broadcast(doc)

	def test_check_broadcast_email_gate(self):
		"""Validate check_broadcast blocks Email if product or feature is disabled.

		Params: None.
		Returns: None.
		Raises: AssertionError if ValidationError is not raised or message differs.
		Side effects: None.
		"""
		doc = frappe._dict({
			"doctype": "Broadcast",
			"name": "bc-email-test",
			"channels": [frappe._dict({"channel": "Email"})],
		})

		# Email product disabled
		with patch("msuite_core.services.plans.plan_enforcer.is_product_enabled", return_value=False):
			with self.assertRaises(frappe.ValidationError) as ctx:
				check_broadcast(doc)
			self.assertIn("Email is not included in your subscription", str(ctx.exception))

		# Email product enabled, but bulk feature disabled
		with patch("msuite_core.services.plans.plan_enforcer.is_product_enabled", return_value=True), \
			patch("msuite_core.services.plans.plan_enforcer.is_feature_enabled", return_value=False):
			with self.assertRaises(frappe.ValidationError) as ctx:
				check_broadcast(doc)
			self.assertIn("Bulk Email is not available on your current plan", str(ctx.exception))

		# Both enabled
		with patch("msuite_core.services.plans.plan_enforcer.is_product_enabled", return_value=True), \
			patch("msuite_core.services.plans.plan_enforcer.is_feature_enabled", return_value=True):
			check_broadcast(doc)

	def test_check_broadcast_launch_skips_zero_recipients(self):
		"""Validate check_broadcast_launch skips rows with recipient_count == 0.

		Params: None.
		Returns: None.
		Raises: AssertionError if enforce or limits are checked for zero-recipient rows.
		Side effects: None.
		"""
		doc = frappe._dict({
			"doctype": "Broadcast",
			"name": "bc-launch-zero",
			"channels": [
				frappe._dict({"channel": "WhatsApp", "recipient_count": 0}),
				frappe._dict({"channel": "Email", "recipient_count": 0}),
				frappe._dict({"channel": "SMS", "recipient_count": 0}),
			],
		})

		with patch("msuite_core.services.plans.plan_enforcer._enforce") as mock_enforce, \
			patch("msuite_core.services.plans.plan_enforcer.get_feature_limit") as mock_limit:
			check_broadcast_launch(doc)
			mock_enforce.assert_not_called()
			mock_limit.assert_not_called()

	def test_check_broadcast_launch_whatsapp_and_sms_enforce(self):
		"""Validate check_broadcast_launch calls _enforce for WhatsApp and SMS rows.

		Params: None.
		Returns: None.
		Raises: AssertionError if _enforce parameters do not match specification.
		Side effects: None.
		"""
		doc = frappe._dict({
			"doctype": "Broadcast",
			"name": "bc-launch-wa-sms",
			"channels": [
				frappe._dict({"channel": "WhatsApp", "recipient_count": 250}),
				frappe._dict({"channel": "SMS", "recipient_count": 100}),
			],
		})

		with patch("msuite_core.services.plans.plan_enforcer._enforce") as mock_enforce:
			check_broadcast_launch(doc)
			self.assertEqual(mock_enforce.call_count, 2)
			mock_enforce.assert_has_calls([
				call(
					feature_key="bulk_messaging",
					label="Bulk Messaging",
					product="WA",
					count_doctype="Channel Message",
					period="month",
					filters={"direction": "Outbound", "channel_type": "WhatsApp"},
					additional=250,
				),
				call(
					feature_key="sms_bulk_messaging",
					label="Bulk SMS Messaging",
					product="SMS",
					count_doctype="Channel Message",
					period="month",
					filters={"direction": "Outbound", "channel_type": "SMS"},
					additional=100,
				),
			])

	def test_check_broadcast_launch_email_per_blast_cap(self):
		"""Validate check_broadcast_launch enforces per-blast limit for Email.

		Params: None.
		Returns: None.
		Raises: AssertionError if Email cap is not enforced as expected.
		Side effects: None.
		"""
		doc = frappe._dict({
			"doctype": "Broadcast",
			"name": "bc-launch-email",
			"channels": [
				frappe._dict({"channel": "Email", "recipient_count": 150}),
			],
		})

		# Over cap
		with patch("msuite_core.services.plans.plan_enforcer.get_feature_limit", return_value="100"):
			with self.assertRaises(frappe.ValidationError) as ctx:
				check_broadcast_launch(doc)
			self.assertIn("Bulk Email is limited to 100 recipients per send on your plan; this send has 150", str(ctx.exception))

		# Under cap
		with patch("msuite_core.services.plans.plan_enforcer.get_feature_limit", return_value="200"):
			check_broadcast_launch(doc)

	def test_usage_for_channel_counts_monthly_rows(self):
		"""Validate usage_for_channel queries Broadcast Channel rows for the current month.

		Params: None.
		Returns: None.
		Raises: AssertionError if count is not returned from get_current_usage.
		Side effects: None.
		"""
		with patch("msuite_core.services.plans.plan_enforcer.get_current_usage", return_value=42) as mock_count:
			result = usage_for_channel("WhatsApp")
			self.assertEqual(result, 42)
			mock_count.assert_called_once_with(
				"Broadcast Channel",
				"month",
				filters={"channel": "WhatsApp"},
			)

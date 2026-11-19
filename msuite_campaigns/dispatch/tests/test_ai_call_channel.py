# Copyright (c) 2026, MSuite and contributors
# For license information, please see license.txt

"""
The AI call channel in the dispatch hub: offered only when the channel app's adapter can
build and launch it, built and launched through that adapter (an AI Calling Broadcast is
started, never submitted), retried through its `run`, and reaching any valid phone line.

A fake adapter stands in for ai_calling, so these run on a site without it.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from msuite_campaigns.audience.doctype.audience.audience import Audience
from msuite_campaigns.dispatch import hub, materializer

ADAPTERS = "msuite_campaigns.campaign.services.relationships.get_adapters"


def _adapter(**overrides):
	adapter = {
		"key": "ai_call",
		"build": MagicMock(return_value={"name": "AICB-1", "queued": 2}),
		"launch": MagicMock(return_value="Running"),
		"run": MagicMock(return_value={"outcome": "fired", "message": ""}),
	}
	adapter.update(overrides)
	return adapter


class TestAiCallChannel(IntegrationTestCase):
	def test_offered_only_with_an_adapter_that_builds_and_launches(self):
		with patch("frappe.db.exists", return_value=True):
			with patch(ADAPTERS, return_value={}):
				self.assertNotIn("ai_call", hub.available_channels())
			with patch(ADAPTERS, return_value={"ai_call": _adapter(launch=None)}):
				self.assertNotIn("ai_call", hub.available_channels())
			with patch(ADAPTERS, return_value={"ai_call": _adapter()}):
				self.assertIn("ai_call", hub.available_channels())

	def test_build_goes_to_the_adapter_with_the_reachable_members(self):
		adapter = _adapter()
		audience = MagicMock()
		audience.members_for.return_value = [{"phone_e164": "+919800000001"}, {"phone_e164": "+919800000002"}]
		audience.rejection_breakdown.return_value = {"No phone number": 1}
		with patch("frappe.db.exists", return_value=True), patch(ADAPTERS, return_value={"ai_call": adapter}):
			res = materializer.build("ai_call", "MC-1", audience, {"assistant": "A"})
		audience.members_for.assert_called_with("AI Call", None)
		adapter["build"].assert_called_once()
		self.assertEqual((res["name"], res["queued"], res["dropped"]), ("AICB-1", 2, 1))

	def test_launch_starts_through_the_adapter_and_never_submits(self):
		adapter = _adapter()
		audience = MagicMock()
		audience.members_for.return_value = [{"phone_e164": "+919800000001"}]
		audience.rejection_breakdown.return_value = {}
		payload = {"channels": {"ai_call": {"assistant": "A", "phone_number": "+918000000000"}}}
		with patch("frappe.db.exists", return_value=True), patch(ADAPTERS, return_value={"ai_call": adapter}), \
				patch("frappe.db.savepoint"), patch("frappe.get_doc") as get_doc:
			res = hub._launch_one("ai_call", "MC-1", audience, payload)
		self.assertEqual(res["status"], "Running")
		adapter["launch"].assert_called_once_with("AICB-1", payload["channels"]["ai_call"] | {
			"risk_policy": None, "title": None, "start_date": None})
		get_doc.assert_not_called()                     # no doc.submit()

	def test_missing_assistant_or_number_is_refused_up_front(self):
		with self.assertRaisesRegex(frappe.ValidationError, "an assistant"):
			hub._validate_channel_configs(["ai_call"], {"channels": {"ai_call": {"phone_number": "x"}}})

	def test_retry_runs_the_broadcast_through_the_adapter(self):
		adapter = _adapter()
		mc = frappe._dict(whatsapp_blast=None, email_blast=None, sms_blast=None, ai_call_blast="AICB-1")
		with patch("frappe.db.exists", return_value=True), patch(ADAPTERS, return_value={"ai_call": adapter}), \
				patch("frappe.get_doc", return_value=mc), patch("frappe.db.set_value"), \
				patch("frappe.db.get_value", return_value="Running"), patch("frappe.db.commit"):
			out = hub.retry_campaign("MC-1")
		adapter["run"].assert_called_once_with("AICB-1")
		self.assertEqual(out["ai_call"]["outcome"], "fired")


class TestAudienceReachForCalls(IntegrationTestCase):
	"""A call reaches any valid line. AI Call used to fall through to the WhatsApp rules,
	dropping landlines and anyone unsubscribed from WhatsApp."""

	def _audience(self, *members):
		doc = frappe.new_doc("Audience")
		doc.members = [frappe._dict(m) for m in members]
		return doc

	def test_landlines_and_whatsapp_unsubscribers_can_be_called(self):
		from msuite_core.services.validation import phone as pv

		doc = self._audience(
			{"phone_status": pv.VALID, "phone_e164": "+918012345678", "phone_type": pv.FIXED_LINE,
			 "unsubscribed_whatsapp": 1, "full_name": "Desk"},
			{"phone_status": "Invalid", "phone_e164": "", "raw_phone": "123", "full_name": "Bad"},
		)
		self.assertEqual([m["full_name"] for m in Audience.members_for(doc, "AI Call")], ["Desk"])
		self.assertEqual(Audience.rejection_breakdown(doc, "AI Call"), {"Phone invalid": 1})

# Copyright (c) 2026, MSuite and contributors
# For license information, please see license.txt

"""Tests for broadcast.api.v1.broadcast.get_available_channels.

Channels come from the adapter registries, filtered by the caller's roles.
Guest and role-less users get nothing; a registered AI Call adapter adds
`ai_call` only for users who may write Broadcasts.
"""

from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from msuite_campaigns.broadcast.api.v1 import broadcast as broadcast_api

ADAPTERS = "msuite_campaigns.broadcast.adapters.get_channel_adapters"
CAMPAIGN_ADAPTERS = "msuite_campaigns.campaign.services.relationships.get_adapters"
ALL_ADAPTERS = {"WhatsApp": {}, "Email": {}, "SMS": {}}


class TestAvailableChannels(IntegrationTestCase):
	"""Role and adapter combinations for get_available_channels."""

	def test_administrator_gets_every_registered_channel_and_ai_call(self):
		frappe.set_user("Administrator")
		with patch(ADAPTERS, return_value=ALL_ADAPTERS), patch(CAMPAIGN_ADAPTERS, return_value={"ai_call": {"key": "ai_call"}}):
			result = broadcast_api.get_available_channels()
		self.assertEqual(result, {"channels": ["whatsapp", "email", "sms", "ai_call"]})

	def test_unregistered_channels_are_left_out(self):
		frappe.set_user("Administrator")
		with patch(ADAPTERS, return_value={"WhatsApp": {}}), patch(CAMPAIGN_ADAPTERS, return_value={}):
			result = broadcast_api.get_available_channels()
		self.assertEqual(result, {"channels": ["whatsapp"]})

	def test_guest_gets_nothing(self):
		frappe.set_user("Guest")
		try:
			with patch(ADAPTERS, return_value=ALL_ADAPTERS), patch(CAMPAIGN_ADAPTERS, return_value={"ai_call": {"key": "ai_call"}}):
				result = broadcast_api.get_available_channels()
		finally:
			frappe.set_user("Administrator")
		self.assertEqual(result, {"channels": []})


class TestAvailableChannelsByRole(IntegrationTestCase):
	"""Non-admin callers are offered only the channels they may create on."""

	def _channels_for(self, roles: list[str]) -> list[str]:
		"""Run the endpoint as a non-admin user holding exactly `roles`."""
		frappe.set_user("test@example.com")
		try:
			with (
				patch(ADAPTERS, return_value=ALL_ADAPTERS),
				patch(CAMPAIGN_ADAPTERS, return_value={"ai_call": {"key": "ai_call"}}),
				patch.object(frappe, "get_roles", return_value=roles),
			):
				return broadcast_api.get_available_channels()["channels"]
		finally:
			frappe.set_user("Administrator")

	def test_channel_user_gets_only_own_channel_and_ai_call(self):
		self.assertEqual(self._channels_for(["WhatsApp User"]), ["whatsapp", "ai_call"])

	def test_campaign_user_gets_every_channel(self):
		self.assertEqual(self._channels_for(["Campaign User"]), ["whatsapp", "email", "sms", "ai_call"])

	def test_read_only_viewer_gets_nothing(self):
		self.assertEqual(self._channels_for(["MSuite Viewer"]), [])

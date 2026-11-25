# Copyright (c) 2026, MSuite and contributors
# For license information, please see license.txt

"""Tests for Broadcast Channel Adapter Registry."""

from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from msuite_campaigns.broadcast.adapters import (
	get_channel_adapter,
	get_channel_adapters,
	require_channel_adapter,
)


class TestBroadcastAdapters(IntegrationTestCase):
	"""Test suite for broadcast channel adapter registry."""

	def setUp(self):
		super().setUp()
		if hasattr(frappe.local, "broadcast_channel_adapters"):
			delattr(frappe.local, "broadcast_channel_adapters")

	def tearDown(self):
		if hasattr(frappe.local, "broadcast_channel_adapters"):
			delattr(frappe.local, "broadcast_channel_adapters")

	def test_registry_resolves_by_channel(self):
		"""Registry resolves adapters keyed by channel and caches in frappe.local."""
		wa_adapter = {
			"channel": "WhatsApp",
			"send_one": lambda b, c, r: {"status": "Sent"},
		}
		email_adapter = {
			"channel": "Email",
			"send_one": lambda b, c, r: {"status": "Sent"},
		}

		with patch(
			"frappe.get_hooks",
			return_value=["mock_app.wa.ADAPTER", "mock_app.email.ADAPTER"],
		):
			with patch(
				"frappe.get_attr",
				side_effect=lambda p: wa_adapter if "wa" in p else email_adapter,
			):
				adapters = get_channel_adapters()
				self.assertIn("WhatsApp", adapters)
				self.assertIn("Email", adapters)
				self.assertEqual(adapters["WhatsApp"], wa_adapter)
				self.assertEqual(adapters["Email"], email_adapter)

				# Test request caching: second call returns cached dict without calling get_hooks
				with patch("frappe.get_hooks", side_effect=AssertionError("Should use cache")):
					cached = get_channel_adapters()
					self.assertEqual(cached, adapters)

				# Test get_channel_adapter
				self.assertEqual(get_channel_adapter("WhatsApp"), wa_adapter)
				self.assertIsNone(get_channel_adapter("NonExistent"))

	def test_bad_dotted_path_skipped(self):
		"""Bad dotted path or malformed adapter is logged and skipped."""
		valid_adapter = {
			"channel": "SMS",
			"send_one": lambda b, c, r: {"status": "Sent"},
		}

		def mock_get_attr(path: str):
			if path == "bad.path.ADAPTER":
				raise ImportError("No module named bad")
			if path == "no_channel.ADAPTER":
				return {"invalid": "missing channel key"}
			return valid_adapter

		with patch(
			"frappe.get_hooks",
			return_value=["bad.path.ADAPTER", "no_channel.ADAPTER", "good.sms.ADAPTER"],
		):
			with patch("frappe.get_attr", side_effect=mock_get_attr):
				adapters = get_channel_adapters()
				self.assertEqual(len(adapters), 1)
				self.assertIn("SMS", adapters)
				self.assertEqual(adapters["SMS"], valid_adapter)

	def test_missing_channel_raises(self):
		"""require_channel_adapter raises frappe.ValidationError when channel is missing."""
		with patch("frappe.get_hooks", return_value=[]):
			with self.assertRaises(frappe.ValidationError) as cm:
				require_channel_adapter("WhatsApp")

			self.assertIn("WhatsApp is not installed on this site", str(cm.exception))

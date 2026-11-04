# Copyright (c) 2026, MSuite and contributors
# For license information, please see license.txt

"""
`dispatch.api.v1.campaign`: permission guards on every endpoint that reads
or acts on an existing Marketing Campaign by name.

Before this fix, none of these six `@frappe.whitelist()` endpoints checked
permission at all — any authenticated user, regardless of role, could
pause/resume/stop/retry any campaign in the system, or read its status.
Each test asserts BOTH that the underlying `hub` call never fires when
permission is denied, AND the exact permission type (`"read"` vs `"write"`)
checked — the audit's own named failure mode is checking `"read"` where
`"write"` is required, which would still block guests but not any
authenticated user with mere read access.
"""

from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from msuite_campaigns.dispatch import hub
from msuite_campaigns.dispatch.api.v1 import campaign


class TestCampaignEndpointPermissions(IntegrationTestCase):
	CAMPAIGN = "MC-PERM-TEST"

	def test_get_launch_status_requires_read_permission(self):
		with patch("frappe.has_permission", return_value=False) as has_perm, patch.object(
			hub, "get_launch_status"
		) as call:
			with self.assertRaises(frappe.PermissionError):
				campaign.get_launch_status(self.CAMPAIGN)
		has_perm.assert_called_once_with("Marketing Campaign", "read", doc=self.CAMPAIGN)
		call.assert_not_called()

	def test_get_launch_status_succeeds_with_read_permission(self):
		with patch("frappe.has_permission", return_value=True), patch.object(
			hub, "get_launch_status", return_value={"launch_status": "Launched"}
		) as call:
			result = campaign.get_launch_status(self.CAMPAIGN)
		call.assert_called_once_with(self.CAMPAIGN)
		self.assertEqual(result, {"launch_status": "Launched"})

	def test_get_unified_progress_requires_read_permission(self):
		with patch("frappe.has_permission", return_value=False) as has_perm, patch.object(
			hub, "unified_progress"
		) as call:
			with self.assertRaises(frappe.PermissionError):
				campaign.get_unified_progress(self.CAMPAIGN)
		has_perm.assert_called_once_with("Marketing Campaign", "read", doc=self.CAMPAIGN)
		call.assert_not_called()

	def test_get_unified_progress_succeeds_with_read_permission(self):
		with patch("frappe.has_permission", return_value=True), patch.object(
			hub, "unified_progress", return_value={"campaign": self.CAMPAIGN, "channels": {}}
		) as call:
			result = campaign.get_unified_progress(self.CAMPAIGN)
		call.assert_called_once_with(self.CAMPAIGN)
		self.assertEqual(result, {"campaign": self.CAMPAIGN, "channels": {}})

	def test_pause_all_requires_write_permission(self):
		with patch("frappe.has_permission", return_value=False) as has_perm, patch.object(
			hub, "fan_out"
		) as call:
			with self.assertRaises(frappe.PermissionError):
				campaign.pause_all(self.CAMPAIGN)
		has_perm.assert_called_once_with("Marketing Campaign", "write", doc=self.CAMPAIGN)
		call.assert_not_called()

	def test_pause_all_succeeds_with_write_permission(self):
		with patch("frappe.has_permission", return_value=True), patch.object(
			hub, "fan_out", return_value={"whatsapp": {"outcome": "paused"}}
		) as call:
			result = campaign.pause_all(self.CAMPAIGN)
		call.assert_called_once_with(self.CAMPAIGN, "pause")
		self.assertEqual(result, {"whatsapp": {"outcome": "paused"}})

	def test_resume_all_requires_write_permission(self):
		with patch("frappe.has_permission", return_value=False) as has_perm, patch.object(
			hub, "fan_out"
		) as call:
			with self.assertRaises(frappe.PermissionError):
				campaign.resume_all(self.CAMPAIGN)
		has_perm.assert_called_once_with("Marketing Campaign", "write", doc=self.CAMPAIGN)
		call.assert_not_called()

	def test_resume_all_succeeds_with_write_permission(self):
		with patch("frappe.has_permission", return_value=True), patch.object(
			hub, "fan_out", return_value={"whatsapp": {"outcome": "resumed"}}
		) as call:
			result = campaign.resume_all(self.CAMPAIGN)
		call.assert_called_once_with(self.CAMPAIGN, "resume")
		self.assertEqual(result, {"whatsapp": {"outcome": "resumed"}})

	def test_stop_all_requires_write_permission(self):
		with patch("frappe.has_permission", return_value=False) as has_perm, patch.object(
			hub, "fan_out"
		) as call:
			with self.assertRaises(frappe.PermissionError):
				campaign.stop_all(self.CAMPAIGN)
		has_perm.assert_called_once_with("Marketing Campaign", "write", doc=self.CAMPAIGN)
		call.assert_not_called()

	def test_stop_all_succeeds_with_write_permission(self):
		with patch("frappe.has_permission", return_value=True), patch.object(
			hub, "fan_out", return_value={"whatsapp": {"outcome": "stopped"}}
		) as call:
			result = campaign.stop_all(self.CAMPAIGN)
		call.assert_called_once_with(self.CAMPAIGN, "stop")
		self.assertEqual(result, {"whatsapp": {"outcome": "stopped"}})

	def test_retry_all_requires_write_permission(self):
		with patch("frappe.has_permission", return_value=False) as has_perm, patch.object(
			hub, "retry_campaign"
		) as call:
			with self.assertRaises(frappe.PermissionError):
				campaign.retry_all(self.CAMPAIGN)
		has_perm.assert_called_once_with("Marketing Campaign", "write", doc=self.CAMPAIGN)
		call.assert_not_called()

	def test_retry_all_succeeds_with_write_permission(self):
		with patch("frappe.has_permission", return_value=True), patch.object(
			hub, "retry_campaign", return_value={"campaign": self.CAMPAIGN, "channels": {}}
		) as call:
			result = campaign.retry_all(self.CAMPAIGN)
		call.assert_called_once_with(self.CAMPAIGN)
		self.assertEqual(result, {"campaign": self.CAMPAIGN, "channels": {}})

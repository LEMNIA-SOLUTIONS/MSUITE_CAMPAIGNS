# Copyright (c) 2026, MSuite and contributors
# For license information, please see license.txt

"""Tests for shared campaign bridge (Task P3-1).

Validates:
- has_broadcast_rows(campaign, channel) -> bool fallback decision (R16).
- rows_for(campaign, channel) filtering, permissions, and channel roles.
- preview_rows(campaign, channel) row shape and readiness (ready only for Draft/Scheduled).
- run(name, channel) delegating to hub.launch once and enforcing channel roles.
- pause/resume/stop(campaign, channel) conditional hub calls, per-row outcomes,
  and AWS ValidationError handling.
- ALL hub sends mocked (nothing is queued or sent).
"""

from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from msuite_campaigns.broadcast.constants import (
	CHANNEL_EMAIL,
	CHANNEL_SMS,
	CHANNEL_WHATSAPP,
	STATUS_CANCELLED,
	STATUS_COMPLETED,
	STATUS_DRAFT,
	STATUS_IN_PROGRESS,
	STATUS_PAUSED,
	STATUS_QUEUED,
	STATUS_SCHEDULED,
)


class TestCampaignBridge(IntegrationTestCase):
	"""Test suite for msuite_campaigns.broadcast.campaign_bridge."""

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		self.campaign = self._make_campaign("Bridge Campaign")

	def tearDown(self):
		frappe.set_user("Administrator")
		super().tearDown()

	def _make_campaign(self, title: str) -> "Document":
		name = f"ZZTestBridge {title} {frappe.generate_hash(length=6)}"
		return frappe.get_doc({
			"doctype": "Marketing Campaign",
			"campaign_name": name,
			"status": "Active",
			"start_date": "2026-11-20 00:00:00",
			"end_date": "2026-12-20 23:59:59",
		}).insert(ignore_permissions=True)

	def _make_broadcast(
		self,
		campaign: str = "",
		title: str = "Test Bridge BC",
		channels: list[dict] | None = None,
		status: str = STATUS_DRAFT,
	) -> "Document":
		bc = frappe.new_doc("Broadcast")
		bc.title = title
		bc.campaign = campaign
		bc.recipient_source = "Individual"
		bc.status = status
		if channels:
			for ch in channels:
				ch_row = bc.append("channels", {})
				for k, v in ch.items():
					setattr(ch_row, k, v)
				if ch.get("channel") == CHANNEL_WHATSAPP:
					if not getattr(ch_row, "whatsapp_account", None):
						ch_row.whatsapp_account = "WA-ACC-001"
					if not getattr(ch_row, "whatsapp_template", None):
						ch_row.whatsapp_template = "tpl_wa_001"
				elif ch.get("channel") == CHANNEL_EMAIL:
					if not getattr(ch_row, "email_account", None):
						ch_row.email_account = "test@example.com"
					if not getattr(ch_row, "email_template", None):
						ch_row.email_template = "tpl_email_001"
					ch_row.variable_type = ""
		bc.insert(ignore_permissions=True, ignore_links=True)
		return bc

	def _make_user(self, email: str, roles: list[str]) -> str:
		if not frappe.db.exists("User", email):
			user = frappe.get_doc({
				"doctype": "User",
				"email": email,
				"first_name": "TestUser",
				"send_welcome_email": 0,
			}).insert(ignore_permissions=True)
		else:
			user = frappe.get_doc("User", email)

		for role in roles:
			if not frappe.db.exists("Role", role):
				frappe.get_doc({"doctype": "Role", "role_name": role, "desk_access": 1}).insert(
					ignore_permissions=True
				)
		user.set("roles", [{"role": r} for r in roles])
		user.save(ignore_permissions=True)
		return email

	# ------------------------------------------------------------------
	# has_broadcast_rows
	# ------------------------------------------------------------------

	def test_has_broadcast_rows(self):
		"""Returns True when linked Broadcast with channel exists; False otherwise."""
		from msuite_campaigns.broadcast.campaign_bridge import has_broadcast_rows

		self.assertFalse(has_broadcast_rows(self.campaign.name, CHANNEL_WHATSAPP))
		self.assertFalse(has_broadcast_rows(self.campaign.name, CHANNEL_EMAIL))

		self._make_broadcast(
			campaign=self.campaign.name,
			title="WA BC",
			channels=[{"channel": CHANNEL_WHATSAPP, "status": STATUS_DRAFT}],
		)

		self.assertTrue(has_broadcast_rows(self.campaign.name, CHANNEL_WHATSAPP))
		self.assertFalse(has_broadcast_rows(self.campaign.name, CHANNEL_EMAIL))
		self.assertFalse(has_broadcast_rows(self.campaign.name, CHANNEL_SMS))
		self.assertFalse(has_broadcast_rows("NonExistentCampaign", CHANNEL_WHATSAPP))

	# ------------------------------------------------------------------
	# rows_for
	# ------------------------------------------------------------------

	def test_rows_for_returns_matching_rows(self):
		"""rows_for returns linked broadcasts matching the channel."""
		from msuite_campaigns.broadcast.campaign_bridge import rows_for

		bc = self._make_broadcast(
			campaign=self.campaign.name,
			title="WA Broadcast 1",
			channels=[{"channel": CHANNEL_WHATSAPP, "status": STATUS_QUEUED, "recipient_count": 50}],
		)

		rows = rows_for(self.campaign.name, CHANNEL_WHATSAPP)
		self.assertEqual(len(rows), 1)
		row = rows[0]
		self.assertEqual(row["name"], bc.name)
		self.assertEqual(row["title"], "WA Broadcast 1")
		self.assertEqual(row["status"], STATUS_QUEUED)
		self.assertEqual(row["recipients"], 50)

		# SMS has no rows
		self.assertEqual(rows_for(self.campaign.name, CHANNEL_SMS), [])

	def test_rows_for_permission_and_channel_role_enforcement(self):
		"""Caller without channel role or read permission receives empty rows."""
		from msuite_campaigns.broadcast.campaign_bridge import rows_for

		self._make_broadcast(
			campaign=self.campaign.name,
			title="WA For Perm Check",
			channels=[{"channel": CHANNEL_WHATSAPP, "status": STATUS_DRAFT}],
		)

		# Email-only user
		email_user = self._make_user("bridge-email-only@example.com", ["Email User"])
		frappe.set_user(email_user)
		try:
			self.assertEqual(rows_for(self.campaign.name, CHANNEL_WHATSAPP), [])
		finally:
			frappe.set_user("Administrator")

		# User denied read on Broadcast
		with patch("frappe.has_permission", side_effect=lambda doctype, ptype="read", doc=None: False if doctype == "Broadcast" else True):
			self.assertEqual(rows_for(self.campaign.name, CHANNEL_WHATSAPP), [])

	def test_rows_for_empty_when_no_rows(self):
		"""rows_for returns [] when no broadcasts exist."""
		from msuite_campaigns.broadcast.campaign_bridge import rows_for

		self.assertEqual(rows_for("NonExistentCampaign", CHANNEL_WHATSAPP), [])

	# ------------------------------------------------------------------
	# preview_rows
	# ------------------------------------------------------------------

	def test_preview_rows_shape_and_readiness(self):
		"""preview_rows returns exact adapter shape with ready True only for Draft/Scheduled."""
		from msuite_campaigns.broadcast.campaign_bridge import preview_rows

		bc_draft = self._make_broadcast(
			campaign=self.campaign.name,
			title="Draft WA",
			channels=[{"channel": CHANNEL_WHATSAPP, "status": STATUS_DRAFT, "recipient_count": 10}],
		)
		bc_sched = self._make_broadcast(
			campaign=self.campaign.name,
			title="Scheduled WA",
			channels=[{"channel": CHANNEL_WHATSAPP, "status": STATUS_SCHEDULED, "recipient_count": 20}],
		)
		bc_queued = self._make_broadcast(
			campaign=self.campaign.name,
			title="Queued WA",
			channels=[{"channel": CHANNEL_WHATSAPP, "status": STATUS_QUEUED, "recipient_count": 30}],
		)
		bc_paused = self._make_broadcast(
			campaign=self.campaign.name,
			title="Paused WA",
			channels=[{"channel": CHANNEL_WHATSAPP, "status": STATUS_PAUSED, "recipient_count": 40}],
		)
		bc_cancelled = self._make_broadcast(
			campaign=self.campaign.name,
			title="Cancelled WA",
			channels=[{"channel": CHANNEL_WHATSAPP, "status": STATUS_CANCELLED, "recipient_count": 5}],
		)

		previews = preview_rows(self.campaign.name, CHANNEL_WHATSAPP)
		self.assertEqual(len(previews), 5)

		by_name = {p["name"]: p for p in previews}

		# Verify exact keys
		expected_keys = {"name", "title", "status", "recipients", "ready", "reason"}
		for p in previews:
			self.assertEqual(set(p.keys()), expected_keys)

		# Draft is ready
		self.assertTrue(by_name[bc_draft.name]["ready"])
		self.assertEqual(by_name[bc_draft.name]["reason"], "")
		self.assertEqual(by_name[bc_draft.name]["recipients"], 10)

		# Scheduled is ready
		self.assertTrue(by_name[bc_sched.name]["ready"])
		self.assertEqual(by_name[bc_sched.name]["reason"], "")
		self.assertEqual(by_name[bc_sched.name]["recipients"], 20)

		# Queued is not ready
		self.assertFalse(by_name[bc_queued.name]["ready"])
		self.assertEqual(by_name[bc_queued.name]["reason"], f"Already {STATUS_QUEUED}")

		# Paused is not ready
		self.assertFalse(by_name[bc_paused.name]["ready"])
		self.assertEqual(by_name[bc_paused.name]["reason"], f"Already {STATUS_PAUSED}")

		# Cancelled is not ready
		self.assertFalse(by_name[bc_cancelled.name]["ready"])
		self.assertEqual(by_name[bc_cancelled.name]["reason"], "Broadcast was cancelled")

	def test_preview_rows_empty_when_no_rows(self):
		"""preview_rows returns [] when no broadcasts exist."""
		from msuite_campaigns.broadcast.campaign_bridge import preview_rows

		self.assertEqual(preview_rows(self.campaign.name, CHANNEL_EMAIL), [])

	# ------------------------------------------------------------------
	# run
	# ------------------------------------------------------------------

	@patch("msuite_campaigns.broadcast.hub.launch")
	def test_run_success(self, mock_launch):
		"""run calls hub.launch once and returns adapter outcome shape."""
		from msuite_campaigns.broadcast.campaign_bridge import run

		bc = self._make_broadcast(
			campaign=self.campaign.name,
			title="Run WA",
			channels=[{"channel": CHANNEL_WHATSAPP, "status": STATUS_DRAFT, "recipient_count": 42}],
		)

		mock_launch.return_value = {
			CHANNEL_WHATSAPP: {"status": STATUS_QUEUED, "queued": 42, "dropped": 0, "error": None}
		}

		result = run(bc.name, CHANNEL_WHATSAPP)
		mock_launch.assert_called_once_with(broadcast=bc.name, channels=[CHANNEL_WHATSAPP])
		self.assertEqual(result["outcome"], "fired")
		self.assertIn("42", result["message"])

	def test_run_permission_error_when_user_lacks_channel_role(self):
		"""run raises PermissionError when caller lacks the channel role."""
		from msuite_campaigns.broadcast.campaign_bridge import run

		bc = self._make_broadcast(
			campaign=self.campaign.name,
			title="Run Perm WA",
			channels=[{"channel": CHANNEL_WHATSAPP, "status": STATUS_DRAFT}],
		)

		email_user = self._make_user("bridge-email-runner@example.com", ["Email User"])
		frappe.set_user(email_user)
		try:
			with self.assertRaises((frappe.PermissionError, PermissionError)):
				run(bc.name, CHANNEL_WHATSAPP)
		finally:
			frappe.set_user("Administrator")

	# ------------------------------------------------------------------
	# pause
	# ------------------------------------------------------------------

	@patch("msuite_campaigns.broadcast.hub.pause")
	def test_pause_only_calls_hub_for_queued_and_in_progress(self, mock_pause):
		"""pause only calls hub.pause for Queued/In Progress rows; others are skipped."""
		from msuite_campaigns.broadcast.campaign_bridge import pause

		bc_queued = self._make_broadcast(
			campaign=self.campaign.name,
			title="Pause Queued",
			channels=[{"channel": CHANNEL_WHATSAPP, "status": STATUS_QUEUED}],
		)
		bc_in_prog = self._make_broadcast(
			campaign=self.campaign.name,
			title="Pause In Prog",
			channels=[{"channel": CHANNEL_WHATSAPP, "status": STATUS_IN_PROGRESS}],
		)
		bc_draft = self._make_broadcast(
			campaign=self.campaign.name,
			title="Pause Draft",
			channels=[{"channel": CHANNEL_WHATSAPP, "status": STATUS_DRAFT}],
		)
		bc_paused = self._make_broadcast(
			campaign=self.campaign.name,
			title="Pause Paused",
			channels=[{"channel": CHANNEL_WHATSAPP, "status": STATUS_PAUSED}],
		)

		mock_pause.return_value = {CHANNEL_WHATSAPP: STATUS_PAUSED}

		results = pause(self.campaign.name, CHANNEL_WHATSAPP)
		self.assertEqual(len(results), 4)

		by_name = {r["name"]: r for r in results}

		# Queued was paused
		self.assertEqual(by_name[bc_queued.name]["outcome"], "fired")
		self.assertEqual(by_name[bc_queued.name]["status"], STATUS_PAUSED)

		# In Progress was paused
		self.assertEqual(by_name[bc_in_prog.name]["outcome"], "fired")
		self.assertEqual(by_name[bc_in_prog.name]["status"], STATUS_PAUSED)

		# Draft was skipped
		self.assertEqual(by_name[bc_draft.name]["outcome"], "skipped")
		self.assertIn("Draft", by_name[bc_draft.name]["reason"])

		# Paused was skipped as already paused
		self.assertEqual(by_name[bc_paused.name]["outcome"], "skipped")
		self.assertIn("Already paused", by_name[bc_paused.name]["reason"])

		# hub.pause called exactly twice
		self.assertEqual(mock_pause.call_count, 2)
		called_bc_names = [call.kwargs.get("broadcast") for call in mock_pause.call_args_list]
		self.assertIn(bc_queued.name, called_bc_names)
		self.assertIn(bc_in_prog.name, called_bc_names)

	@patch("msuite_campaigns.broadcast.hub.pause")
	def test_pause_aws_validation_error_skipped_and_loop_continues(self, mock_pause):
		"""AWS ValidationError becomes outcome skipped and loop continues with next row."""
		from msuite_campaigns.broadcast.campaign_bridge import pause

		bc_aws = self._make_broadcast(
			campaign=self.campaign.name,
			title="AWS In Prog",
			channels=[{"channel": CHANNEL_WHATSAPP, "status": STATUS_IN_PROGRESS}],
		)
		bc_std = self._make_broadcast(
			campaign=self.campaign.name,
			title="Std In Prog",
			channels=[{"channel": CHANNEL_WHATSAPP, "status": STATUS_IN_PROGRESS}],
		)

		def _pause_side_effect(broadcast, channel):
			if broadcast == bc_aws.name:
				frappe.throw("cannot pause after hand-off to AWS", frappe.ValidationError)
			return {channel: STATUS_PAUSED}

		mock_pause.side_effect = _pause_side_effect

		results = pause(self.campaign.name, CHANNEL_WHATSAPP)
		self.assertEqual(len(results), 2)

		by_name = {r["name"]: r for r in results}
		self.assertEqual(by_name[bc_aws.name]["outcome"], "skipped")
		self.assertIn("cannot pause after hand-off to AWS", by_name[bc_aws.name]["reason"])

		self.assertEqual(by_name[bc_std.name]["outcome"], "fired")
		self.assertEqual(by_name[bc_std.name]["status"], STATUS_PAUSED)

	def test_pause_empty_when_no_rows(self):
		"""pause returns [] when no broadcasts exist."""
		from msuite_campaigns.broadcast.campaign_bridge import pause

		self.assertEqual(pause("NonExistentCampaign", CHANNEL_WHATSAPP), [])

	# ------------------------------------------------------------------
	# resume
	# ------------------------------------------------------------------

	@patch("msuite_campaigns.broadcast.hub.resume")
	def test_resume_only_calls_hub_for_paused_rows(self, mock_resume):
		"""resume only calls hub.resume for Paused rows; others are skipped."""
		from msuite_campaigns.broadcast.campaign_bridge import resume

		bc_paused = self._make_broadcast(
			campaign=self.campaign.name,
			title="Resume Paused",
			channels=[{"channel": CHANNEL_WHATSAPP, "status": STATUS_PAUSED}],
		)
		bc_queued = self._make_broadcast(
			campaign=self.campaign.name,
			title="Resume Queued",
			channels=[{"channel": CHANNEL_WHATSAPP, "status": STATUS_QUEUED}],
		)
		bc_draft = self._make_broadcast(
			campaign=self.campaign.name,
			title="Resume Draft",
			channels=[{"channel": CHANNEL_WHATSAPP, "status": STATUS_DRAFT}],
		)

		mock_resume.return_value = {CHANNEL_WHATSAPP: STATUS_QUEUED}

		results = resume(self.campaign.name, CHANNEL_WHATSAPP)
		self.assertEqual(len(results), 3)

		by_name = {r["name"]: r for r in results}
		self.assertEqual(by_name[bc_paused.name]["outcome"], "fired")
		self.assertEqual(by_name[bc_queued.name]["outcome"], "skipped")
		self.assertEqual(by_name[bc_draft.name]["outcome"], "skipped")

		mock_resume.assert_called_once_with(broadcast=bc_paused.name, channel=CHANNEL_WHATSAPP)

	def test_resume_empty_when_no_rows(self):
		"""resume returns [] when no broadcasts exist."""
		from msuite_campaigns.broadcast.campaign_bridge import resume

		self.assertEqual(resume("NonExistentCampaign", CHANNEL_WHATSAPP), [])

	# ------------------------------------------------------------------
	# stop
	# ------------------------------------------------------------------

	@patch("msuite_campaigns.broadcast.hub.cancel")
	def test_stop_calls_hub_for_stoppable_statuses(self, mock_cancel):
		"""stop calls hub.cancel for Draft/Scheduled/Queued/In Progress/Paused; others skipped."""
		from msuite_campaigns.broadcast.campaign_bridge import stop

		bc_draft = self._make_broadcast(
			campaign=self.campaign.name,
			title="Stop Draft",
			channels=[{"channel": CHANNEL_WHATSAPP, "status": STATUS_DRAFT}],
		)
		bc_sched = self._make_broadcast(
			campaign=self.campaign.name,
			title="Stop Scheduled",
			channels=[{"channel": CHANNEL_WHATSAPP, "status": STATUS_SCHEDULED}],
		)
		bc_queued = self._make_broadcast(
			campaign=self.campaign.name,
			title="Stop Queued",
			channels=[{"channel": CHANNEL_WHATSAPP, "status": STATUS_QUEUED}],
		)
		bc_in_prog = self._make_broadcast(
			campaign=self.campaign.name,
			title="Stop In Prog",
			channels=[{"channel": CHANNEL_WHATSAPP, "status": STATUS_IN_PROGRESS}],
		)
		bc_paused = self._make_broadcast(
			campaign=self.campaign.name,
			title="Stop Paused",
			channels=[{"channel": CHANNEL_WHATSAPP, "status": STATUS_PAUSED}],
		)
		bc_completed = self._make_broadcast(
			campaign=self.campaign.name,
			title="Stop Completed",
			channels=[{"channel": CHANNEL_WHATSAPP, "status": STATUS_COMPLETED}],
		)
		bc_cancelled = self._make_broadcast(
			campaign=self.campaign.name,
			title="Stop Cancelled",
			channels=[{"channel": CHANNEL_WHATSAPP, "status": STATUS_CANCELLED}],
		)

		mock_cancel.return_value = {CHANNEL_WHATSAPP: STATUS_CANCELLED}

		results = stop(self.campaign.name, CHANNEL_WHATSAPP)
		self.assertEqual(len(results), 7)

		by_name = {r["name"]: r for r in results}

		# Pre-flight and paused: fired with "Cancelled."
		self.assertEqual(by_name[bc_draft.name]["outcome"], "fired")
		self.assertEqual(by_name[bc_draft.name]["message"], "Cancelled.")
		self.assertEqual(by_name[bc_sched.name]["outcome"], "fired")
		self.assertEqual(by_name[bc_paused.name]["outcome"], "fired")

		# In-flight: fired with provider notice
		self.assertEqual(by_name[bc_queued.name]["outcome"], "fired")
		self.assertIn("cannot be recalled", by_name[bc_queued.name]["message"])
		self.assertEqual(by_name[bc_in_prog.name]["outcome"], "fired")
		self.assertIn("cannot be recalled", by_name[bc_in_prog.name]["message"])

		# Completed and Cancelled: skipped
		self.assertEqual(by_name[bc_completed.name]["outcome"], "skipped")
		self.assertEqual(by_name[bc_cancelled.name]["outcome"], "skipped")

		self.assertEqual(mock_cancel.call_count, 5)

	def test_stop_empty_when_no_rows(self):
		"""stop returns [] when no broadcasts exist."""
		from msuite_campaigns.broadcast.campaign_bridge import stop

		self.assertEqual(stop("NonExistentCampaign", CHANNEL_WHATSAPP), [])

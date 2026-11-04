# Copyright (c) 2026, MSuite and contributors
# For license information, please see license.txt

"""
`dispatch.hub.retry_campaign`: the AWS-branch retry gate.

`export_and_enqueue(doc)` called with no `rows` re-derives the FULL sendable
recipient set every time, and AWS-routed rows never leave "sendable" status
(no per-recipient write-back) -- so a retry gate that accepts "Queued", this
engine's PERMANENT resting status even on total success, resends to 100% of
the original recipients on every single click. Only a genuine, durably
recorded "AWS Dispatch Failed" may be retried.
"""

from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from msuite_campaigns.dispatch import hub


class TestRetryCampaignAwsGate(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		self.campaign = "MC-RETRY-GATE-TEST"
		self.blast = frappe._dict(
			{
				"doctype": "Bulk WhatsApp Message",
				"name": "BWA-RETRY-GATE-TEST",
				"docstatus": 1,
				"delivery_engine": "AWS",
				"status": "Queued",
			}
		)
		self.status_writes = []

		def fake_db_set(field, value, update_modified=None, **kwargs):
			self.status_writes.append((field, value))
			self.blast[field] = value

		self.blast.db_set = fake_db_set

		self.mc = frappe._dict(
			{"whatsapp_blast": self.blast.name, "email_blast": None, "sms_blast": None}
		)

	def _run_retry(self):
		with patch("frappe.db.exists", return_value=True), patch(
			"frappe.get_doc",
			side_effect=lambda doctype, name=None: self.mc if doctype == "Marketing Campaign" else self.blast,
		), patch("frappe.db.set_value"), patch("frappe.db.commit"), patch("frappe.log_error"):
			return hub.retry_campaign(self.campaign)

	def test_queued_campaign_is_rejected_not_resent(self):
		"""The exact bug: 'Queued' is the permanent success-resting status
		for an AWS-routed blast, not a failure signal. It must never be
		treated as retryable."""
		from msuite_campaigns.aws_pipeline import dispatch as aws_dispatch

		with patch.object(aws_dispatch, "export_and_enqueue") as export_call:
			out = self._run_retry()

		export_call.assert_not_called()
		self.assertEqual(out["channels"]["whatsapp"]["outcome"], "skipped")
		self.assertNotIn(("status", "Queued"), self.status_writes)

	def test_aws_dispatch_failed_campaign_is_retried(self):
		"""The one status that IS safe to retry: a genuinely failed dispatch,
		durably recorded as such (Tasks 1-3)."""
		self.blast.status = "AWS Dispatch Failed"
		from msuite_campaigns.aws_pipeline import dispatch as aws_dispatch

		with patch.object(aws_dispatch, "export_and_enqueue") as export_call:
			out = self._run_retry()

		export_call.assert_called_once_with(self.blast)
		self.assertEqual(out["channels"]["whatsapp"]["outcome"], "fired")
		self.assertIn(("status", "Queued"), self.status_writes)

	def test_failed_export_on_retry_re_marks_aws_dispatch_failed(self):
		"""A retry that itself fails again must land back on the one status
		that keeps it retryable, not silently strand the blast at 'Queued'
		as today's code does (`doc.db_set('status', 'Queued', ...)` runs
		BEFORE the call, unconditionally, today)."""
		self.blast.status = "AWS Dispatch Failed"
		from msuite_campaigns.aws_pipeline import dispatch as aws_dispatch

		with patch.object(aws_dispatch, "export_and_enqueue", side_effect=RuntimeError("still down")):
			out = self._run_retry()

		self.assertEqual(out["channels"]["whatsapp"]["outcome"], "failed")
		self.assertIn(("status", "AWS Dispatch Failed"), self.status_writes)
		self.assertNotIn(("status", "Queued"), self.status_writes)

	def test_healthy_aws_channel_is_skipped_in_multichannel_retry(self):
		"""Important #1: In a multi-channel campaign with a healthy AWS channel
		(e.g. WhatsApp status='Queued') and a failed non-AWS channel (e.g. Email
		status='Partially Failed'), retrying the campaign must skip the healthy
		AWS channel without recording a false failure, allowing the failed channel
		to be retried and the campaign status to become 'In Progress'."""
		from unittest.mock import MagicMock
		from msuite_campaigns.aws_pipeline import dispatch as aws_dispatch

		email_blast = frappe._dict(
			{
				"doctype": "Bulk Email Message",
				"name": "BEM-RETRY-GATE-TEST",
				"docstatus": 1,
				"delivery_engine": "Standard",
				"status": "Partially Failed",
				"retry_failed": MagicMock(),
			}
		)
		self.mc.email_blast = email_blast.name

		def get_doc_mock(doctype, name=None):
			if doctype == "Marketing Campaign":
				return self.mc
			if doctype == "Bulk WhatsApp Message":
				return self.blast
			if doctype == "Bulk Email Message":
				return email_blast
			return None

		set_value_calls = []

		def fake_set_value(doctype, name, values, update_modified=None):
			set_value_calls.append((doctype, name, values))

		with patch("frappe.db.exists", return_value=True), patch(
			"frappe.get_doc", side_effect=get_doc_mock
		), patch("frappe.db.set_value", side_effect=fake_set_value), patch(
			"frappe.db.commit"
		), patch("frappe.log_error") as log_error_mock, patch.object(
			aws_dispatch, "export_and_enqueue"
		) as export_call:
			out = hub.retry_campaign(self.campaign)

		export_call.assert_not_called()
		email_blast.retry_failed.assert_called_once()
		log_error_mock.assert_not_called()
		self.assertEqual(out["channels"]["whatsapp"]["outcome"], "skipped")
		self.assertEqual(out["channels"]["email"]["outcome"], "retried")
		campaign_updates = [v for dt, name, v in set_value_calls if dt == "Marketing Campaign"]
		self.assertTrue(any(v.get("status") == "In Progress" for v in campaign_updates))
		self.assertFalse(any(v.get("status") == "Partially Failed" for v in campaign_updates))

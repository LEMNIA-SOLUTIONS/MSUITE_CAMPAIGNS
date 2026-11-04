# Copyright (c) 2026, MSuite and contributors
# For license information, please see license.txt

"""
`audience.api.v1.audience.retry_broadcast`: the AWS-branch retry gate.

A second, independent copy of the exact bug fixed in `dispatch/hub.py` --
this endpoint retries a single blast directly and, before this fix, applies
NO status gate at all before calling `export_and_enqueue(doc)` with no
`rows`, which always resends to 100% of the original recipients (see
`dispatch/hub.py`'s test module docstring for the full mechanism).
"""

from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from msuite_campaigns.audience.api.v1 import audience


class TestRetryBroadcastAwsGate(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		self.blast = frappe._dict(
			{
				"doctype": "Bulk WhatsApp Message",
				"name": "BWA-RETRY-BROADCAST-TEST",
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

	def _run_retry(self):
		def fake_exists(doctype, name=None):
			if doctype == "Marketing Campaign":
				return False
			return doctype == "Bulk WhatsApp Message"

		with patch("frappe.db.exists", side_effect=fake_exists), patch(
			"frappe.get_doc", return_value=self.blast
		):
			return audience.retry_broadcast(self.blast.name)

	def test_queued_broadcast_is_rejected_not_resent(self):
		from msuite_campaigns.aws_pipeline import dispatch as aws_dispatch

		with patch.object(aws_dispatch, "export_and_enqueue") as export_call:
			with self.assertRaises(frappe.ValidationError):
				self._run_retry()

		export_call.assert_not_called()
		self.assertNotIn(("status", "Queued"), self.status_writes)

	def test_aws_dispatch_failed_broadcast_is_retried(self):
		self.blast.status = "AWS Dispatch Failed"
		from msuite_campaigns.aws_pipeline import dispatch as aws_dispatch

		with patch.object(aws_dispatch, "export_and_enqueue") as export_call:
			out = self._run_retry()

		export_call.assert_called_once_with(self.blast)
		self.assertEqual(out["whatsapp"][0]["outcome"], "retried")
		self.assertIn(("status", "Queued"), self.status_writes)

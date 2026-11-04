# Copyright (c) 2026, MSuite and contributors
# For license information, please see license.txt

"""
`aws_pipeline.dispatch`: routing decisions and the export/trigger/mark
handoff. Transport is never exercised — `client` calls are patched — these
tests pin down bookkeeping and error-handling contracts, not sends.

  1. `should_use_aws` routes the exact threshold count to Standard, not AWS
     (`count <= threshold`, not `count < threshold`).
  2. `_ensure_channel_secret_synced` aborts the send (raises) on a sync
     failure instead of logging and letting `trigger_campaign` proceed
     against stale/missing credentials.
  3. `export_and_enqueue` does not let a `_mark_queued` failure that happens
     *after* `trigger_campaign` already succeeded pass silently: it retries
     once, and only propagates if the retry also fails.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from msuite_campaigns.aws_pipeline import dispatch


class TestShouldUseAws(IntegrationTestCase):
	def test_count_at_exact_threshold_routes_to_standard(self):
		"""count <= threshold must route Standard, not AWS — this is the
		boundary a `count < threshold` typo would silently flip."""
		doc = frappe._dict({"delivery_engine": "AWS", "doctype": "Bulk SMS Message", "name": "SMS-THRESH"})
		rows = [f"row-{i}" for i in range(5)]  # len(rows) == threshold below

		with patch.object(dispatch, "get_min_recipient_threshold", return_value=5):
			self.assertFalse(dispatch.should_use_aws(doc, rows))


class TestEnsureChannelSecretSynced(IntegrationTestCase):
	def test_secrets_manager_sync_failure_aborts_instead_of_warning(self):
		"""The exact bug: a live token is found, but the push to Secrets
		Manager itself fails. That must abort the send, not just warn — a
		swallowed failure here used to let trigger_campaign fire against
		stale/missing credentials."""
		bulk_doc = frappe._dict({"whatsapp_account": "ACC-1", "name": "BWA-SECRET-FAIL"})
		fake_account = MagicMock()
		fake_account.get_password.return_value = "live-token"

		with patch(
			"msuite_whatsapp.queries.account_repository.get_account_doc",
			return_value=fake_account,
		), patch(
			"msuite_campaigns.aws_pipeline.client.sync_channel_secret",
			side_effect=RuntimeError("secrets manager unreachable"),
		) as sync_secret:
			with self.assertRaises(frappe.ValidationError):
				dispatch._ensure_channel_secret_synced("whatsapp", bulk_doc)

		sync_secret.assert_called_once()

	def test_account_lookup_failure_also_aborts(self):
		"""General contract check: any failure inside the pre-flight sync
		(not just the Secrets Manager push itself) must abort, not warn."""
		bulk_doc = frappe._dict({"whatsapp_account": None, "name": "BWA-LOOKUP-FAIL"})

		with patch(
			"msuite_whatsapp.queries.account_repository.find_default_account",
			side_effect=RuntimeError("provider unreachable"),
		), patch("msuite_campaigns.aws_pipeline.client.sync_channel_secret") as sync_secret:
			with self.assertRaises(frappe.ValidationError):
				dispatch._ensure_channel_secret_synced("whatsapp", bulk_doc)

		sync_secret.assert_not_called()


class TestExportAndEnqueueMarkQueuedRetry(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		self.bulk_doc = frappe._dict({"doctype": "Bulk SMS Message", "name": "BSM-RETRY-TEST"})
		self.rows = [frappe._dict(name="R1"), frappe._dict(name="R2")]

		self.patches = [
			patch.object(dispatch, "_require_feature"),
			patch.object(dispatch, "_ensure_channel_secret_synced"),
			patch.object(
				dispatch.manifest, "build", return_value=({"campaign_id": "C1"}, ["header", "row1", "row2"])
			),
			patch.object(
				dispatch.client,
				"request_upload_url",
				return_value={"url": "https://upload.example/x", "key": "k1", "max_bytes": 0},
			),
			patch.object(dispatch.client, "upload_manifest"),
			patch.object(dispatch.client, "trigger_campaign", return_value={"message_id": "mid-1"}),
		]
		for p in self.patches:
			p.start()
			self.addCleanup(p.stop)

	def test_mark_queued_failure_is_logged_and_retried_once(self):
		with patch.object(
			dispatch, "_mark_queued", side_effect=[Exception("db blip"), 2]
		) as mark_queued, patch("frappe.log_error") as log_error:
			result = dispatch.export_and_enqueue(self.bulk_doc, self.rows)

		self.assertEqual(mark_queued.call_count, 2)
		log_error.assert_called_once()
		self.assertEqual(result["queued"], 2)

	def test_mark_queued_failure_propagates_after_retry_also_fails(self):
		"""trigger_campaign already fired (mocked above) — the caller must
		still learn the rows never got flipped, not have it swallowed."""
		with patch.object(
			dispatch, "_mark_queued", side_effect=[Exception("db blip"), Exception("still down")]
		) as mark_queued, patch("frappe.log_error") as log_error:
			with self.assertRaises(Exception):
				dispatch.export_and_enqueue(self.bulk_doc, self.rows)

		self.assertEqual(mark_queued.call_count, 2)
		log_error.assert_called_once()


class TestExportAndEnqueueStreamedCount(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		self.bulk_doc = frappe._dict({"doctype": "Bulk SMS Message", "name": "BSM-COUNT-TEST"})

	def _run_with_recipient_lines(self, recipient_lines: list[str]):
		"""recipient_lines excludes the header — this helper prepends it,
		matching manifest._lines()'s real shape (header first, then one line
		per recipient), and returns a genuine one-shot iterator."""
		all_lines = iter(["header"] + recipient_lines)
		patches = [
			patch.object(dispatch, "_require_feature"),
			patch.object(dispatch, "_ensure_channel_secret_synced"),
			patch.object(dispatch.manifest, "build", return_value=({"campaign_id": "C1"}, all_lines)),
			patch.object(
				dispatch.client,
				"request_upload_url",
				return_value={"url": "https://upload.example/x", "key": "k1", "max_bytes": 0},
			),
			patch.object(dispatch.client, "upload_manifest"),
			patch.object(dispatch.client, "trigger_campaign", return_value={"message_id": "mid-1"}),
		]
		for p in patches:
			p.start()
			self.addCleanup(p.stop)
		return dispatch.export_and_enqueue(self.bulk_doc)

	def test_recipient_count_matches_recipient_line_count(self):
		result = self._run_with_recipient_lines(["row1", "row2", "row3"])
		trigger_call_kwargs = dispatch.client.trigger_campaign.call_args.kwargs
		self.assertEqual(trigger_call_kwargs["recipient_count"], 3)
		self.assertEqual(result["queued"], 3)

	def test_recipient_count_is_zero_for_header_only_manifest(self):
		result = self._run_with_recipient_lines([])
		trigger_call_kwargs = dispatch.client.trigger_campaign.call_args.kwargs
		self.assertEqual(trigger_call_kwargs["recipient_count"], 0)
		self.assertEqual(result["queued"], 0)

	def test_body_uploaded_contains_all_lines_joined(self):
		self._run_with_recipient_lines(["row1", "row2"])
		uploaded_body = dispatch.client.upload_manifest.call_args.args[1]
		self.assertEqual(uploaded_body, "header\nrow1\nrow2")


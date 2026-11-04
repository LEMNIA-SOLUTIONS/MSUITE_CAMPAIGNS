# Copyright (c) 2026, MSuite and contributors
# For license information, please see license.txt

"""
`aws_pipeline.manifest._dynamo_campaign_id` and `aws_pipeline.rollup.rollup`:
the write-key and read-key halves of one contract.

The analytics table's SUMMARY item is keyed by campaign_id alone -- no
channel dimension (aws-bulk-messaging/src/common/python/dynamo.py). A
Marketing Campaign that runs both a WhatsApp and an Email blast used to send
both channels' counters into the SAME DynamoDB item, so a failed email
inflated the WhatsApp card's `failed` count and vice versa -- confirmed live
against a real campaign (WhatsApp: 5/5 delivered per Meta's own wamids;
Email: 5/5 rejected by SES; the shared counter reported sent=5, failed=5
either way you sliced it). The fix makes the key channel-specific on both
sides and this test pins the contract between them.
"""

from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from msuite_campaigns.aws_pipeline import manifest, rollup


class TestDynamoCampaignId(IntegrationTestCase):
	def test_composes_campaign_and_channel(self):
		doc = frappe._dict({"campaign": "CAMP-2026-00002", "name": "BULK-WA-2026-00002"})
		self.assertEqual(manifest._dynamo_campaign_id(doc, "whatsapp"), "CAMP-2026-00002__whatsapp")
		self.assertEqual(manifest._dynamo_campaign_id(doc, "email"), "CAMP-2026-00002__email")

	def test_falls_back_to_doc_name_when_not_campaign_linked(self):
		doc = frappe._dict({"campaign": None, "name": "BULK-WA-STANDALONE"})
		self.assertEqual(manifest._dynamo_campaign_id(doc, "whatsapp"), "BULK-WA-STANDALONE__whatsapp")

	def test_no_url_unsafe_characters(self):
		"""`campaign_id` is embedded raw into a URL path by the provider's
		`campaign_analytics` broker (`GET /analytics/campaign/{id}`) -- a `#`
		would be parsed as a URL fragment and never reach the Lambda at all,
		silently truncating the key back to the collision this fix removes."""
		doc = frappe._dict({"campaign": "CAMP-2026-00002", "name": "X"})
		key = manifest._dynamo_campaign_id(doc, "whatsapp")
		self.assertTrue(key.replace("__", "").replace("-", "").isalnum())


class TestRollupReadsChannelScopedKey(IntegrationTestCase):
	def test_fetches_composite_key_not_blast_name(self):
		"""The exact bug: rollup() used to call fetch(blast_name), which never
		matches what manifest.py writes -- confirmed live, both blasts'
		per-blast fetches returned zeros while the bare-campaign key held the
		real totals."""
		with patch.object(
			rollup, "aws_blasts", return_value=["BULK-WA-2026-00002"]
		), patch.object(
			rollup, "fetch", return_value={"sent_count": 5, "delivered_count": 0, "failed_count": 0}
		) as fetch, patch.object(rollup, "_audience", return_value=5):
			result = rollup.rollup("CAMP-2026-00002", "whatsapp")

		fetch.assert_called_once_with("CAMP-2026-00002__whatsapp")
		self.assertEqual(result["metrics"]["sent"], 5)

	def test_two_blasts_same_channel_read_the_shared_counter_once(self):
		"""Two WhatsApp blasts under one campaign already write into the SAME
		DynamoDB item (manifest.py's key has no per-blast component) -- reading
		it once, not once per blast, is what keeps the total from being
		doubled relative to what actually happened."""
		with patch.object(
			rollup, "aws_blasts", return_value=["BULK-WA-A", "BULK-WA-B"]
		), patch.object(
			rollup, "fetch", return_value={"sent_count": 5, "delivered_count": 0, "failed_count": 0}
		) as fetch, patch.object(rollup, "_audience", return_value=10):
			result = rollup.rollup("CAMP-2026-00002", "whatsapp")

		self.assertEqual(fetch.call_count, 1)
		self.assertEqual(result["metrics"]["sent"], 5)

	def test_email_and_whatsapp_under_the_same_campaign_read_different_keys(self):
		"""The exact symptom from the live campaign: email's failures must not
		bleed into whatsapp's card, or vice versa."""
		calls = []

		def fake_fetch(key):
			calls.append(key)
			if key.endswith("__whatsapp"):
				return {"sent_count": 5, "delivered_count": 0, "failed_count": 0}
			return {"sent_count": 0, "delivered_count": 0, "failed_count": 5}

		with patch.object(rollup, "aws_blasts", return_value=["BLAST-1"]), patch.object(
			rollup, "fetch", side_effect=fake_fetch
		), patch.object(rollup, "_audience", return_value=5):
			wa = rollup.rollup("CAMP-2026-00002", "whatsapp")
			email = rollup.rollup("CAMP-2026-00002", "email")

		self.assertEqual(calls, ["CAMP-2026-00002__whatsapp", "CAMP-2026-00002__email"])
		self.assertEqual(wa["metrics"]["sent"], 5)
		self.assertEqual(wa["metrics"]["failed"], 0)
		self.assertEqual(email["metrics"]["sent"], 0)
		self.assertEqual(email["metrics"]["failed"], 5)

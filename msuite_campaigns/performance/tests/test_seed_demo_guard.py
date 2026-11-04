"""Tests for seed_demo_campaign developer_mode guard and required destination_url."""

import unittest
from unittest.mock import MagicMock, patch

import frappe
from msuite_campaigns.performance.tools.seed_demo_campaign import _ensure_ad, run


class TestSeedDemoGuard(unittest.TestCase):
	"""Test seed_demo_campaign environment gates and parameters."""

	def test_run_raises_when_developer_mode_disabled(self):
		"""run() must raise PermissionError if developer_mode is disabled in site config."""
		with patch.dict(frappe.conf, {"developer_mode": 0}):
			with self.assertRaises(frappe.PermissionError) as ctx:
				run(destination_url="https://valid-domain.org/landing")
			self.assertIn("developer_mode", str(ctx.exception))

	def test_run_raises_when_destination_url_missing_or_empty(self):
		"""run() must raise ValidationError if destination_url is omitted or blank."""
		with patch.dict(frappe.conf, {"developer_mode": 1}):
			with self.assertRaises(frappe.ValidationError) as ctx1:
				run(destination_url="")
			self.assertIn("destination_url", str(ctx1.exception))

			with self.assertRaises(frappe.ValidationError) as ctx2:
				run()
			self.assertIn("destination_url", str(ctx2.exception))

	@patch("frappe.db.commit")
	@patch("msuite_campaigns.performance.tools.seed_demo_campaign._seed_whatsapp_messages", return_value=10)
	@patch("msuite_campaigns.performance.tools.seed_demo_campaign._seed_social_insights", return_value=10)
	@patch("msuite_campaigns.performance.tools.seed_demo_campaign._seed_ad_insights", return_value=10)
	@patch("msuite_campaigns.performance.tools.seed_demo_campaign._ensure_whatsapp_blast", return_value="BLAST-1")
	@patch("msuite_campaigns.performance.tools.seed_demo_campaign._ensure_social_posts", return_value=[])
	@patch("msuite_campaigns.performance.tools.seed_demo_campaign._ensure_social_post", return_value="SP-1")
	@patch("msuite_campaigns.performance.tools.seed_demo_campaign._ensure_ad", return_value="AD-1")
	@patch("msuite_campaigns.performance.tools.seed_demo_campaign._ensure_ad_campaign", return_value="ADC-1")
	@patch("msuite_campaigns.performance.tools.seed_demo_campaign._ensure_campaign", return_value="MC-1")
	def test_run_passes_destination_url_to_ensure_ad(
		self,
		mock_ensure_campaign,
		mock_ensure_ad_campaign,
		mock_ensure_ad,
		mock_ensure_social_post,
		mock_ensure_social_posts,
		mock_ensure_blast,
		mock_seed_ad_insights,
		mock_seed_social_insights,
		mock_seed_wa_messages,
		mock_commit,
	):
		"""When developer_mode and destination_url are provided, _ensure_ad receives the URL."""
		with patch.dict(frappe.conf, {"developer_mode": 1}):
			result = run(
				campaign_name="Test Campaign",
				destination_url="https://acme-store.com/promo",
			)
			self.assertEqual(result["marketing_campaign"], "MC-1")
			mock_ensure_ad.assert_called_once_with("ADC-1", destination_url="https://acme-store.com/promo")

	@patch("frappe.db.get_value")
	@patch("frappe.get_doc")
	def test_ensure_ad_inserts_ad_with_provided_destination_url(self, mock_get_doc, mock_db_get_value):
		"""_ensure_ad must create an Ad document with destination_url matching argument."""
		mock_db_get_value.side_effect = lambda doctype, filters, *args, **kwargs: (
			None if doctype == "Ad" else ("ADSET-001" if doctype == "Ad Set" else "CREATIVE-001")
		)
		mock_ad_doc = MagicMock()
		mock_ad_doc.name = "AD-CREATED-1"
		mock_get_doc.return_value.insert.return_value = mock_ad_doc

		ad_name = _ensure_ad("ADC-1", destination_url="https://acme-store.com/special-deal")
		self.assertEqual(ad_name, "AD-CREATED-1")

		mock_get_doc.assert_called_once()
		ad_payload = mock_get_doc.call_args[0][0]
		self.assertEqual(ad_payload["doctype"], "Ad")
		self.assertEqual(ad_payload["destination_url"], "https://acme-store.com/special-deal")

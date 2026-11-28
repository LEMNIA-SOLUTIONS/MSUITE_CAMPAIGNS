"""No DocType in the Suite may link to a DocType that no longer exists."""

from __future__ import annotations

import frappe
from frappe.tests import IntegrationTestCase

MARKETING_CAMPAIGN_DOCTYPE = "Marketing Campaign"
EMAIL_UNSUBSCRIBE_DOCTYPE = "MSuite Email Unsubscribe"
# DocTypes removed when broadcasts moved to the unified Broadcast model.
DELETED_DOCTYPES = ("Bulk WhatsApp Message", "Bulk Email Message", "Bulk SMS Message", "Audience")
LEGACY_REFERENCE_FIELDS = ("whatsapp_blast", "email_blast", "sms_blast", "audience")


def _links_to_deleted_doctypes(doctype: str) -> list[str]:
	"""Return fieldnames on `doctype` whose Link options name a deleted DocType."""
	return [
		field.fieldname
		for field in frappe.get_meta(doctype).fields
		if field.fieldtype == "Link" and field.options in DELETED_DOCTYPES
	]


class TestMarketingCampaignLinks(IntegrationTestCase):
	def test_marketing_campaign_has_no_link_to_a_deleted_doctype(self):
		"""Regression: a Link to a deleted DocType broke Frappe's test-record setup for every app reaching Marketing Campaign."""
		self.assertEqual(_links_to_deleted_doctypes(MARKETING_CAMPAIGN_DOCTYPE), [])

	def test_email_unsubscribe_has_no_link_to_a_deleted_doctype(self):
		self.assertEqual(_links_to_deleted_doctypes(EMAIL_UNSUBSCRIBE_DOCTYPE), [])

	def test_legacy_reference_fields_stay_as_read_text(self):
		"""The old values stay on the record for the legacy broadcast code that reads them."""
		meta = frappe.get_meta(MARKETING_CAMPAIGN_DOCTYPE)
		for fieldname in LEGACY_REFERENCE_FIELDS:
			field = meta.get_field(fieldname)
			self.assertIsNotNone(field, fieldname)
			self.assertEqual(field.fieldtype, "Data", fieldname)

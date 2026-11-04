import frappe
from frappe.tests.utils import FrappeTestCase
from msuite_campaigns.audience.services.contact_normalize import normalize_contact


class TestContactNormalize(FrappeTestCase):
	def _doc(self, **kw):
		base = dict(
			doctype="Contact",
			mobile_no="",
			email_id="",
			phone_e164="",
			email_normalized="",
			phone_status="Pending",
			phone_type="",
			phone_country="",
			get_doc_before_save=lambda: None,
		)
		base.update(kw)
		return frappe._dict(base)

	def test_mobile_change_rederives_e164(self):
		d = self._doc(mobile_no="+919812340001", phone_e164="+919000000000")
		normalize_contact(d)
		self.assertEqual(d.phone_e164, "+919812340001")
		self.assertEqual(d.phone_status, "Valid")

	def test_email_change_rederives_normalized(self):
		d = self._doc(email_id="  Foo@Example.COM ")
		normalize_contact(d)
		self.assertEqual(d.email_normalized, "foo@example.com")

	def test_cleared_phone_clears_derived(self):
		d = self._doc(mobile_no="", phone_e164="+919812340001", phone_status="Valid")
		normalize_contact(d)
		self.assertEqual(d.phone_e164, "")
		self.assertEqual(d.phone_status, "Pending")

	def test_unusable_phone_is_flagged_not_raised(self):
		d = self._doc(mobile_no="abc")
		normalize_contact(d)
		self.assertEqual(d.phone_e164, "")
		self.assertIn(d.phone_status, ("Invalid", "Unparseable"))

	def test_email_change_resets_status(self):
		d = self._doc(
			email_id="new@example.com",
			email_normalized="old@example.com",
			email_status="ok",
			get_doc_before_save=lambda: frappe._dict(email_normalized="old@example.com"),
		)
		normalize_contact(d)
		self.assertEqual(d.email_normalized, "new@example.com")
		self.assertEqual(d.email_status, "Pending")

	def test_new_contact_keeps_preverified_email_status(self):
		d = self._doc(
			email_id="verified@example.com",
			email_status="ok",
			get_doc_before_save=lambda: None,
		)
		normalize_contact(d)
		self.assertEqual(d.email_normalized, "verified@example.com")
		self.assertEqual(d.email_status, "ok")

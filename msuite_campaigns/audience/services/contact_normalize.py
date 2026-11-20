"""Contact.validate hook: keep the derived phone_e164 / email_normalized columns in step
with the raw mobile_no / email_id, so edits made in MSuite or desk can't leave a stale
number that bulk senders then use."""
from msuite_core.services.validation import phone as phone_validator


def normalize_contact(doc, method=None):
	raw_phone = (doc.get("mobile_no") or "").strip()
	if raw_phone:
		# national numbers without '+' are parsed with the system default region (intentional; Audience sync uses its own region).
		v = phone_validator.normalize(raw_phone)
		doc.phone_e164 = v["e164"]
		doc.phone_country = v["country"]
		doc.phone_type = v["number_type"] if v["status"] == "Valid" else ""
		doc.phone_status = v["status"]
	else:
		doc.phone_e164 = ""
		doc.phone_country = ""
		doc.phone_type = ""
		doc.phone_status = "Pending"

	email = (doc.get("email_id") or "").strip().lower()
	before = doc.get_doc_before_save() if hasattr(doc, "get_doc_before_save") else None
	if before is not None:
		if email != (before.get("email_normalized") or ""):
			doc.email_status = "Pending"
	doc.email_normalized = email

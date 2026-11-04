# Copyright (c) 2026, MSuite and contributors
# For license information, please see license.txt

"""
Contact Sync Service.

Central synchronizer between Audience Members (bulk marketing lists) and
Frappe's master `Contact` doctype (used by FCRM and MSuite Desk).

Invariants:
1. Native Platform Reuse (Ponytail Principle): Uses `tabContact` as the single
   source of truth for contact details, E.164 phone numbers, and global opt-in/opt-out.
2. Strict Global Opt-Out Protection: If a contact was previously marked as
   unsubscribed for a channel (WhatsApp, SMS, Email), any newly uploaded/imported
   audience member automatically inherits that unsubscribed state.
3. High-Speed Batch Processing: Chunked queries and in-memory indexing prevent
   N+1 query bottlenecks during large audience imports.
"""

from __future__ import annotations

import frappe
from frappe.utils import now_datetime

from msuite_core.services.validation import phone as phone_validator


def _split_full_name(full_name: str) -> tuple[str, str]:
	"""Split full name into (first_name, last_name)."""
	parts = (full_name or "").strip().split(maxsplit=1)
	if not parts:
		return "", ""
	if len(parts) == 1:
		return parts[0], ""
	return parts[0], parts[1]


def sync_audience_to_contacts(audience_doc) -> dict:
	"""Synchronize all members of an Audience with `tabContact`.

	1. Parses and normalizes phone (E.164) and email if not already present.
	2. Performs batch lookups against `tabContact` by `phone_e164` or `email_normalized`.
	3. Propagates existing opt-out flags from `tabContact` to `Audience Member`.
	4. Upserts new contacts into `tabContact` so master contact book is updated.

	Returns summary: {"total": int, "matched": int, "created": int, "opt_outs_inherited": int}
	"""
	if not audience_doc.members:
		return {"total": 0, "matched": 0, "created": 0, "opt_outs_inherited": 0}

	region = audience_doc.region() if hasattr(audience_doc, "region") else "IN"

	# Normalize members first if unparsed
	for m in audience_doc.members:
		if not m.phone_e164 and m.raw_phone:
			parsed = phone_validator.normalize(m.raw_phone, region)
			m.phone_e164 = parsed["e164"]
			m.phone_country = parsed["country"]
			m.phone_type = parsed["number_type"]
			m.phone_status = parsed["status"]
		if not m.email_normalized and m.raw_email:
			m.email_normalized = m.raw_email.strip().lower()

	# Collect non-empty search keys
	phones = {m.phone_e164 for m in audience_doc.members if m.phone_e164}
	emails = {m.email_normalized for m in audience_doc.members if m.email_normalized}

	phone_to_contact: dict[str, dict] = {}
	email_to_contact: dict[str, dict] = {}

	# Batch query tabContact in chunks of 1000
	chunk_size = 1000

	# Query by phone_e164 / mobile_no / phone
	if phones:
		phone_list = list(phones)
		for i in range(0, len(phone_list), chunk_size):
			chunk = phone_list[i : i + chunk_size]
			contacts = frappe.db.sql(
				"""
				SELECT name, first_name, last_name, email_id, email_normalized,
				       mobile_no, phone, phone_e164,
				       unsubscribed_whatsapp, unsubscribed_sms, unsubscribed_email, unsubscribed
				FROM `tabContact`
				WHERE phone_e164 IN %(chunk)s OR mobile_no IN %(chunk)s OR phone IN %(chunk)s
				""",
				{"chunk": chunk},
				as_dict=True,
			)
			for c in contacts:
				if c.get("phone_e164"):
					phone_to_contact[c["phone_e164"]] = c
				if c.get("mobile_no"):
					phone_to_contact[c["mobile_no"]] = c
				if c.get("phone"):
					phone_to_contact[c["phone"]] = c
				if c.get("email_normalized"):
					email_to_contact[c["email_normalized"]] = c
				elif c.get("email_id"):
					email_to_contact[c["email_id"].strip().lower()] = c

	# Query by email_normalized / email_id
	if emails:
		email_list = list(emails)
		for i in range(0, len(email_list), chunk_size):
			chunk = email_list[i : i + chunk_size]
			contacts = frappe.db.sql(
				"""
				SELECT name, first_name, last_name, email_id, email_normalized,
				       mobile_no, phone, phone_e164,
				       unsubscribed_whatsapp, unsubscribed_sms, unsubscribed_email, unsubscribed
				FROM `tabContact`
				WHERE email_normalized IN %(chunk)s OR email_id IN %(chunk)s
				""",
				{"chunk": chunk},
				as_dict=True,
			)
			for c in contacts:
				if c.get("email_normalized"):
					email_to_contact[c["email_normalized"]] = c
				elif c.get("email_id"):
					email_to_contact[c["email_id"].strip().lower()] = c
				if c.get("phone_e164"):
					phone_to_contact[c["phone_e164"]] = c

	matched_count = 0
	created_count = 0
	opt_outs_inherited = 0

	# Process members: inherit opt-out flags and prepare new contacts
	contacts_to_create = []
	seen_new_keys = set()

	for m in audience_doc.members:
		contact = None
		if m.phone_e164 and m.phone_e164 in phone_to_contact:
			contact = phone_to_contact[m.phone_e164]
		elif m.email_normalized and m.email_normalized in email_to_contact:
			contact = email_to_contact[m.email_normalized]

		if contact:
			matched_count += 1
			# Strict Global Opt-Out Inheritance
			if contact.get("unsubscribed_whatsapp"):
				if not m.unsubscribed_whatsapp:
					m.unsubscribed_whatsapp = 1
					opt_outs_inherited += 1
			if contact.get("unsubscribed_sms"):
				if not m.unsubscribed_sms:
					m.unsubscribed_sms = 1
					opt_outs_inherited += 1
			if contact.get("unsubscribed_email") or contact.get("unsubscribed"):
				if not m.unsubscribed_email:
					m.unsubscribed_email = 1
					opt_outs_inherited += 1

			# Enrich Contact if missing normalized fields
			fields_to_update = {}
			if m.phone_e164 and not contact.get("phone_e164"):
				fields_to_update["phone_e164"] = m.phone_e164
			if m.email_normalized and not contact.get("email_normalized"):
				fields_to_update["email_normalized"] = m.email_normalized
			if m.full_name and not (contact.get("first_name") or contact.get("last_name")):
				fn, ln = _split_full_name(m.full_name)
				if fn:
					fields_to_update["first_name"] = fn
				if ln:
					fields_to_update["last_name"] = ln
			if fields_to_update:
				frappe.db.set_value("Contact", contact["name"], fields_to_update, update_modified=False)
		else:
			# Dedup new contacts within the same batch
			key = m.phone_e164 or m.email_normalized
			if key and key not in seen_new_keys:
				seen_new_keys.add(key)
				fn, ln = _split_full_name(m.full_name)
				contacts_to_create.append({
					"first_name": fn or key,
					"last_name": ln,
					"mobile_no": m.phone_e164 or m.raw_phone or "",
					"phone_e164": m.phone_e164 or "",
					"phone_type": m.phone_type or "Unknown",
					"phone_country": m.phone_country or "",
					"phone_status": m.phone_status or "Pending",
					"email_id": m.email_normalized or m.raw_email or "",
					"email_normalized": m.email_normalized or "",
					"email_status": m.email_status or "Pending",
					"unsubscribed_whatsapp": 1 if m.unsubscribed_whatsapp else 0,
					"unsubscribed_sms": 1 if m.unsubscribed_sms else 0,
					"unsubscribed_email": 1 if m.unsubscribed_email else 0,
					"unsubscribed": 1 if m.unsubscribed_email else 0,
					"status": "Passive",
				})

	# Insert new contacts
	for c_data in contacts_to_create:
		try:
			c_doc = frappe.new_doc("Contact")
			c_doc.first_name = c_data["first_name"]
			c_doc.last_name = c_data["last_name"]
			if c_data["mobile_no"]:
				c_doc.mobile_no = c_data["mobile_no"]
				c_doc.add_phone(c_data["mobile_no"], is_primary_mobile_no=1)
			if c_data["email_id"]:
				c_doc.email_id = c_data["email_id"]
				c_doc.add_email(c_data["email_id"], is_primary=1)
			c_doc.phone_e164 = c_data["phone_e164"]
			c_doc.phone_type = c_data["phone_type"]
			c_doc.phone_country = c_data["phone_country"]
			c_doc.phone_status = c_data["phone_status"]
			c_doc.email_normalized = c_data["email_normalized"]
			c_doc.email_status = c_data["email_status"]
			c_doc.unsubscribed_whatsapp = c_data["unsubscribed_whatsapp"]
			c_doc.unsubscribed_sms = c_data["unsubscribed_sms"]
			c_doc.unsubscribed_email = c_data["unsubscribed_email"]
			c_doc.unsubscribed = c_data["unsubscribed"]
			c_doc.status = c_data["status"]
			c_doc.insert(ignore_permissions=True)
			created_count += 1
		except Exception as e:
			frappe.logger("msuite_workspace.audience").warning(
				f"Failed to auto-create Contact for {c_data.get('phone_e164') or c_data.get('email_normalized')}: {e}"
			)

	return {
		"total": len(audience_doc.members),
		"matched": matched_count,
		"created": created_count,
		"opt_outs_inherited": opt_outs_inherited,
	}


def _sync_audience_member_consent(channel: str, e164: str, raw_id: str, unsubscribed: int) -> None:
	"""Keep Audience Member rows in sync with Contact consent."""
	if channel in ("WhatsApp", "SMS"):
		col = "unsubscribed_whatsapp" if channel == "WhatsApp" else "unsubscribed_sms"
		frappe.db.sql(
			f"""
			UPDATE `tabAudience Member`
			SET {col} = %(val)s
			WHERE (phone_e164 IS NOT NULL AND phone_e164 != '' AND phone_e164 = %(e164)s)
			   OR (raw_phone IS NOT NULL AND raw_phone != '' AND raw_phone = %(raw)s)
			""",
			{"val": unsubscribed, "e164": e164, "raw": raw_id},
		)
	elif channel == "Email":
		norm = (raw_id or "").strip().lower()
		frappe.db.sql(
			"""
			UPDATE `tabAudience Member`
			SET unsubscribed_email = %(val)s
			WHERE (email_normalized IS NOT NULL AND email_normalized != '' AND email_normalized = %(norm)s)
			   OR (raw_email IS NOT NULL AND raw_email != '' AND raw_email = %(raw)s)
			""",
			{"val": unsubscribed, "norm": norm, "raw": raw_id},
		)


def record_channel_opt_out(
	channel: str, identifier: str, reason: str = "", create_if_missing: bool = False
) -> bool:
	"""Update Contact opt-out status for a specific channel.

	Args:
	    channel: "WhatsApp", "SMS", or "Email"
	    identifier: Phone number (E.164/raw) or Email address
	    reason: Origin/trigger of opt-out (e.g. "Inbound STOP message")
	    create_if_missing: When True and channel is WhatsApp/SMS and no Contact matches,
	                       create a minimal Contact with opt-out flags.

	Returns:
	    True if at least one Contact was updated or created, False otherwise.
	"""
	if not identifier:
		return False

	clean_id = identifier.strip()
	now = now_datetime()

	filters = []
	e164 = ""
	if channel in ("WhatsApp", "SMS"):
		parsed = phone_validator.normalize(clean_id, "IN")
		e164 = parsed["e164"] or clean_id
		filters = ["phone_e164 = %(e164)s OR mobile_no = %(id)s OR phone = %(id)s", {"e164": e164, "id": clean_id}]
	else:
		norm_email = clean_id.lower()
		filters = ["email_normalized = %(norm)s OR email_id = %(id)s", {"norm": norm_email, "id": clean_id}]

	where_clause, params = filters[0], filters[1]
	contacts = frappe.db.sql(
		f"SELECT name FROM `tabContact` WHERE {where_clause}",
		params,
		as_dict=True,
	)

	if not contacts:
		if create_if_missing and channel in ("WhatsApp", "SMS"):
			c_doc = frappe.new_doc("Contact")
			c_doc.first_name = clean_id
			# Core Contact.validate rebuilds mobile_no from phone_nos and blanks it
			# when there is no child row, so the number must go in via add_phone.
			c_doc.add_phone(clean_id, is_primary_mobile_no=1)
			c_doc.phone_e164 = e164
			c_doc.status = "Passive"
			c_doc.consent_updated_at = now
			if reason:
				c_doc.opt_out_reason = reason
			if channel == "WhatsApp":
				c_doc.unsubscribed_whatsapp = 1
			elif channel == "SMS":
				c_doc.unsubscribed_sms = 1
			c_doc.insert(ignore_permissions=True)
			_sync_audience_member_consent(channel, e164, clean_id, 1)
			return True
		return False

	update_fields = {"consent_updated_at": now}
	if reason:
		update_fields["opt_out_reason"] = reason

	if channel == "WhatsApp":
		update_fields["unsubscribed_whatsapp"] = 1
	elif channel == "SMS":
		update_fields["unsubscribed_sms"] = 1
	elif channel == "Email":
		update_fields["unsubscribed_email"] = 1
		update_fields["unsubscribed"] = 1

	for c in contacts:
		frappe.db.set_value("Contact", c["name"], update_fields, update_modified=True)

	_sync_audience_member_consent(channel, e164, clean_id, 1)
	return True


def record_channel_opt_in(channel: str, identifier: str, reason: str = "") -> bool:
	"""Update Contact opt-in status for a specific channel (clears unsubscribed flag).

	Args:
	    channel: "WhatsApp", "SMS", or "Email"
	    identifier: Phone number (E.164/raw) or Email address
	    reason: Origin/trigger of opt-in (e.g. "Inbound START message")

	Returns:
	    True if at least one Contact was updated, False otherwise.
	"""
	if not identifier:
		return False

	clean_id = identifier.strip()
	now = now_datetime()

	filters = []
	e164 = ""
	if channel in ("WhatsApp", "SMS"):
		parsed = phone_validator.normalize(clean_id, "IN")
		e164 = parsed["e164"] or clean_id
		filters = ["phone_e164 = %(e164)s OR mobile_no = %(id)s OR phone = %(id)s", {"e164": e164, "id": clean_id}]
	else:
		norm_email = clean_id.lower()
		filters = ["email_normalized = %(norm)s OR email_id = %(id)s", {"norm": norm_email, "id": clean_id}]

	where_clause, params = filters[0], filters[1]
	contacts = frappe.db.sql(
		f"SELECT name FROM `tabContact` WHERE {where_clause}",
		params,
		as_dict=True,
	)

	if not contacts:
		return False

	update_fields = {"consent_updated_at": now, "opt_out_reason": ""}

	if channel == "WhatsApp":
		update_fields["unsubscribed_whatsapp"] = 0
	elif channel == "SMS":
		update_fields["unsubscribed_sms"] = 0
	elif channel == "Email":
		update_fields["unsubscribed_email"] = 0
		update_fields["unsubscribed"] = 0

	for c in contacts:
		frappe.db.set_value("Contact", c["name"], update_fields, update_modified=True)

	_sync_audience_member_consent(channel, e164, clean_id, 0)
	return True

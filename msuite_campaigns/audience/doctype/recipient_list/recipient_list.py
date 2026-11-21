# Copyright (c) 2026, MSuite and contributors
# For license information, please see license.txt

"""Recipient List — unified contact list for WhatsApp, Email, and SMS.

Holds recipient contacts, validation verdicts, and reachability rollups.
Exposes reachability filtering via `contacts_for` and detailed exclusion metrics
via `rejection_breakdown`.
"""

from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime

try:
	from msuite_campaigns.audience.doctype.audience.audience import existing_reason_from_verdict
except (ImportError, ModuleNotFoundError):
	def existing_reason_from_verdict(verdict: dict) -> str:
		"""Format why a recipient was flagged as existing from validation verdict.

		Args:
			verdict: Dictionary containing contact validation verdict flags.

		Returns:
			str: Formatted string indicating which identifier matched ("phone", "email", or "phone+email").
		"""
		matched = []
		if verdict.get("phone_e164") and verdict.get("phone_existing"):
			matched.append("phone")
		if verdict.get("email_normalized") and verdict.get("email_existing"):
			matched.append("email")
		return "+".join(matched)
from msuite_campaigns.audience.plan_gates import check_recipient_list_size_gate
from msuite_campaigns.audience.services.contact_sync_service import sync_recipient_list_to_contacts
from msuite_campaigns.broadcast.constants import CHANNELS
from msuite_core.services.validation import email as email_validator
from msuite_core.services.validation import phone as phone_validator


class RecipientList(Document):
	"""Recipient List document managing contacts across messaging channels."""

	def validate(self) -> None:
		"""Validate Recipient List: enforce plan gates, sync contacts to master, recompute rollups.

		Side effects:
			- May raise frappe.ValidationError if plan size limit is exceeded.
			- Synchronizes contacts with tabContact.
			- Updates total_contacts, valid_phone_count, and valid_email_count on self.
		"""
		check_recipient_list_size_gate(self)
		if self.contacts:
			sync_recipient_list_to_contacts(self)
		self.recompute_rollups()

	def recompute_rollups(self) -> None:
		"""Recompute total and reachable counts across contacts.

		Public entry point for callers modifying self.contacts outside validate().

		Side effects:
			Updates self.total_contacts, self.valid_phone_count, and self.valid_email_count.
		"""
		self.total_contacts = len(self.contacts or [])
		self._recompute_rollups()

	def _recompute_rollups(self) -> None:
		"""Calculate reachable phone and deliverable email counts."""
		reachable = 0
		deliverable = 0
		for c in self.contacts or []:
			if (
				c.phone_status == phone_validator.VALID
				and c.phone_type in phone_validator.WHATSAPP_REACHABLE_TYPES
				and not c.unsubscribed_whatsapp
			):
				reachable += 1
			if (
				c.email_normalized
				and email_validator.accepts(c.email_status)
				and not c.unsubscribed_email
			):
				deliverable += 1
		self.valid_phone_count = reachable
		self.valid_email_count = deliverable

	def region(self) -> str:
		"""Return default phone parsing region in uppercase.

		Returns:
			str: ISO 3166-1 alpha-2 country code.
		"""
		if self.default_region:
			return self.default_region.upper()
		return (
			frappe.db.get_single_value("MSuite Settings", "default_phone_region")
			or phone_validator.DEFAULT_REGION
		).upper()

	@frappe.whitelist()
	def validate_contacts(self, force: int = 0) -> dict:
		"""Run the Phase 1 validation pipeline over every contact and persist verdicts.

		Args:
			force: When 1, ignores cached verdicts and re-validates.

		Returns:
			dict: Pipeline summary dict.

		Raises:
			frappe.PermissionError: If caller lacks write permission.
		"""
		self.check_permission("write")
		from msuite_core.services.validation import pipeline

		if not self.contacts:
			return {"total": 0}

		rows = [
			{"phone": c.raw_phone or c.phone_e164, "email": c.raw_email or c.email_normalized}
			for c in self.contacts
		]
		out = pipeline.validate_rows(rows, self.region(), force=bool(force))

		now = now_datetime()
		for contact, verdict in zip(self.contacts, out["rows"], strict=True):
			self.apply_verdict(contact, verdict, now)

		self.last_validated_at = now
		self.save(ignore_permissions=True)
		return out["summary"]

	def apply_verdict(self, contact, verdict: dict, now) -> None:
		"""Write one pipeline verdict onto one contact row.

		Args:
			contact: Recipient Contact child document.
			verdict: Verdict dict returned by validation pipeline.
			now: Timestamp of validation.
		"""
		contact.phone_e164 = verdict["phone_e164"]
		contact.phone_country = verdict["phone_country"]
		contact.phone_type = verdict["phone_type"]
		contact.phone_status = verdict["phone_status"]
		contact.email_normalized = verdict["email_normalized"]
		contact.email_status = verdict["email_status"] or ("Pending" if not verdict["email_normalized"] else "")
		if verdict["email_normalized"]:
			contact.email_checked_at = now
		contact.validation_error = verdict["phone_reason"] or verdict["email_reason"] or ""
		contact.is_existing = bool(verdict.get("is_existing"))
		contact.existing_reason = existing_reason_from_verdict(verdict)

	@frappe.whitelist()
	def import_from_linked_doctype(self) -> int:
		"""Pull contacts from another DocType (CRM Lead, Contact, etc.).

		Returns:
			int: Number of contacts imported and added.

		Raises:
			frappe.PermissionError: If caller lacks write permission.
			frappe.ValidationError: If configuration or selected fields are invalid.
		"""
		self.check_permission("write")
		if not self.import_from_doctype or not self.doctype_to_import:
			frappe.throw(_("Enable 'Import From DocType' and pick a DocType first."))
		if not (self.phone_field or self.email_field):
			frappe.throw(_("Set at least one of Phone Field or Email Field."))

		if not frappe.db.exists("DocType", self.doctype_to_import):
			frappe.throw(_("DocType '{0}' does not exist.").format(self.doctype_to_import))
		from msuite_core.utils.import_guard import assert_importable

		assert_importable(self.doctype_to_import)

		frappe.has_permission(self.doctype_to_import, "read", throw=True, ignore_share_permissions=True)

		meta = frappe.get_meta(self.doctype_to_import)
		valid_fields = {df.fieldname for df in meta.fields} | {"name", "owner", "creation", "modified", "modified_by"}

		for field_label, f in [
			("Phone Field", self.phone_field),
			("Email Field", self.email_field),
			("Name Field", self.name_field),
		]:
			if f and f not in valid_fields:
				frappe.throw(
					_("{0} '{1}' does not exist on DocType '{2}'.").format(
						field_label, f, self.doctype_to_import
					)
				)

		filters = _loads(self.import_filters, default={})
		extra = _loads(self.data_fields, default=[])

		for f in extra:
			if f and f not in valid_fields:
				frappe.throw(
					_("Data Field '{0}' does not exist on DocType '{1}'.").format(
						f, self.doctype_to_import
					)
				)

		fields = ["name"]
		for f in (self.phone_field, self.email_field, self.name_field):
			if f and f not in fields:
				fields.append(f)
		for f in extra:
			if f and f not in fields:
				fields.append(f)

		rows = frappe.get_list(
			self.doctype_to_import,
			filters=filters,
			fields=fields,
			limit=self.import_limit or 0,
		)

		added = self.add_contacts(
			[
				{
					"full_name": r.get(self.name_field) if self.name_field else None,
					"raw_phone": r.get(self.phone_field) if self.phone_field else None,
					"raw_email": r.get(self.email_field) if self.email_field else None,
					"recipient_data": {k: r.get(k) for k in extra if r.get(k) is not None},
				}
				for r in rows
			]
		)
		self.source = "DocType Import"
		self.save(ignore_permissions=True)
		return added

	def add_contacts(self, rows: list[dict]) -> int:
		"""Append contact rows, skipping duplicates present in the list.

		Deduplication runs on normalised values (E.164 phone and lowercase email).

		Args:
			rows: List of dicts with full_name, raw_phone, raw_email, recipient_data.

		Returns:
			int: Number of new contacts appended.
		"""
		region = self.region()
		seen = {
			(c.phone_e164 or "", (c.email_normalized or "").lower())
			for c in (self.contacts or [])
		}

		added = 0
		for row in rows:
			raw_phone = (row.get("raw_phone") or "").strip()
			raw_email = (row.get("raw_email") or "").strip()

			if not raw_phone and not raw_email:
				continue

			verdict = phone_validator.normalize(raw_phone, region)
			email = raw_email.lower()
			key = (verdict["e164"], email)

			if key != ("", ""):
				if key in seen:
					continue
				seen.add(key)

			data = row.get("recipient_data") or {}
			self.append(
				"contacts",
				{
					"full_name": row.get("full_name"),
					"raw_phone": raw_phone,
					"phone_e164": verdict["e164"],
					"phone_country": verdict["country"],
					"phone_type": verdict["number_type"],
					"phone_status": verdict["status"],
					"raw_email": raw_email,
					"email_normalized": email,
					"email_status": "Pending" if email else "",
					"validation_error": verdict["reason"],
					"recipient_data": json.dumps(data) if isinstance(data, dict) else (data or "{}"),
				},
			)
			added += 1

		return added

	def contacts_for(self, channel: str, risk_policy: dict | None = None) -> list[dict]:
		"""Return contacts reachable on the specified channel as plain dicts.

		Deduplicates by recipient address (phone_e164 for phone channels, email_normalized for Email)
		so each unique address is messaged only once per broadcast.

		Args:
			channel: Channel name ("WhatsApp", "Email", or "SMS"). Must be in CHANNELS.
			risk_policy: Optional dict with email risk acceptance rules (e.g. {"email_accept": ["ok"]}).

		Returns:
			list[dict]: List of recipient dicts (full_name, phone_e164, email, recipient_data).

		Raises:
			frappe.ValidationError: If channel is not in CHANNELS.
		"""
		if channel not in CHANNELS:
			frappe.throw(_("Invalid channel: {0}. Must be one of {1}.").format(channel, ", ".join(CHANNELS)))

		policy = risk_policy or {}
		accepted = set(policy.get("email_accept") or []) or None

		out = []
		seen_addresses: set[str] = set()

		for c in self.contacts or []:
			if channel == "Email":
				if not c.email_normalized or c.unsubscribed_email:
					continue
				if not email_validator.accepts(c.email_status, accepted):
					continue
				email_addr = c.email_normalized.lower()
				if email_addr in seen_addresses:
					continue
				seen_addresses.add(email_addr)
				out.append(_as_dict(c))
				continue

			# Phone channels: WhatsApp, SMS
			if c.phone_status != phone_validator.VALID:
				continue
			if not c.phone_e164:
				continue
			if channel == "SMS":
				if c.unsubscribed_sms or c.phone_type not in phone_validator.SMS_REACHABLE_TYPES:
					continue
			else:  # WhatsApp
				if c.unsubscribed_whatsapp or c.phone_type not in phone_validator.WHATSAPP_REACHABLE_TYPES:
					continue

			if c.phone_e164 in seen_addresses:
				continue
			seen_addresses.add(c.phone_e164)
			out.append(_as_dict(c))

		return out

	def rejection_breakdown(self, channel: str, risk_policy: dict | None = None) -> dict[str, int]:
		"""Explain why contacts were dropped for a channel, keyed by reason.

		The sum of all values exactly equals the dropped count:
		len(self.contacts) - len(self.contacts_for(channel, risk_policy)).

		Args:
			channel: Channel name ("WhatsApp", "Email", or "SMS"). Must be in CHANNELS.
			risk_policy: Optional dict with email risk acceptance rules.

		Returns:
			dict[str, int]: Mapping from rejection reason string to count of dropped contacts.

		Raises:
			frappe.ValidationError: If channel is not in CHANNELS.
		"""
		if channel not in CHANNELS:
			frappe.throw(_("Invalid channel: {0}. Must be one of {1}.").format(channel, ", ".join(CHANNELS)))

		policy = risk_policy or {}
		accepted = set(policy.get("email_accept") or []) or None
		reasons: dict[str, int] = {}
		seen_addresses: set[str] = set()

		def bump(reason: str) -> None:
			reasons[reason] = reasons.get(reason, 0) + 1

		for c in self.contacts or []:
			if channel == "Email":
				if not c.email_normalized:
					bump("No email address")
				elif c.unsubscribed_email:
					bump("Unsubscribed from email")
				elif not email_validator.accepts(c.email_status, accepted):
					bump(f"Email verdict: {c.email_status or 'unchecked'}")
				else:
					email_addr = c.email_normalized.lower()
					if email_addr in seen_addresses:
						bump("Duplicate email address")
					else:
						seen_addresses.add(email_addr)
				continue

			# Phone channels: WhatsApp, SMS
			if not c.raw_phone and not c.phone_e164:
				bump("No phone number")
			elif c.phone_status != phone_validator.VALID:
				bump(f"Phone {(c.phone_status or 'Pending').lower()}")
			elif not c.phone_e164:
				bump("No phone number")
			elif channel == "SMS" and c.unsubscribed_sms:
				bump("Unsubscribed from SMS")
			elif channel == "WhatsApp" and c.unsubscribed_whatsapp:
				bump("Unsubscribed from WhatsApp")
			elif c.phone_type not in (
				phone_validator.SMS_REACHABLE_TYPES
				if channel == "SMS"
				else phone_validator.WHATSAPP_REACHABLE_TYPES
			):
				bump(f"{c.phone_type or 'Unknown'} cannot receive {channel}")
			else:
				if c.phone_e164 in seen_addresses:
					bump("Duplicate phone number")
				else:
					seen_addresses.add(c.phone_e164)

		return reasons


def _as_dict(contact) -> dict:
	"""Convert a Recipient Contact doc to plain recipient dictionary format.

	Args:
		contact: Recipient Contact child document.

	Returns:
		dict: Dictionary with full_name, phone_e164, email, and recipient_data.
	"""
	return {
		"full_name": contact.full_name or "",
		"phone_e164": contact.phone_e164 or "",
		"email": contact.email_normalized or "",
		"recipient_data": contact.recipient_data or "{}",
	}


def _loads(raw, default):
	"""Safely decode JSON string or return default value."""
	if not raw:
		return default
	try:
		return json.loads(raw) if isinstance(raw, str) else raw
	except (json.JSONDecodeError, TypeError):
		return default

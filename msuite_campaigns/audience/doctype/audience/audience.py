# Copyright (c) 2026, MSuite and contributors
# For license information, please see license.txt

"""
Audience — one contact list, every channel.

Replaces the need to curate the same people three times across
`WhatsApp Recipient List` and `Email Recipient List`. Those two doctypes are
NOT touched and remain authoritative for every blast that already references
them; this is a parallel model, per the dual-path directive.

An audience holds validation verdicts (written by `validation.pipeline`) but
does not hold send state. Send state is per-campaign and lives on each blast's
own recipient rows — the same reason `email_bulk_send_service` snapshots a
list rather than writing back to it.
"""

import json

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime

from msuite_campaigns.audience.plan_gates import check_audience_size_gate
from msuite_campaigns.audience.services.contact_sync_service import sync_audience_to_contacts
from msuite_core.services.validation import email as email_validator
from msuite_core.services.validation import phone as phone_validator


class Audience(Document):
	def validate(self):
		check_audience_size_gate(self)
		if self.members:
			sync_audience_to_contacts(self)
		self.recompute_rollups()

	def recompute_rollups(self) -> None:
		"""Public entry point for callers that changed `self.members` outside
		`validate()` — e.g. after appending/removing one child row via the
		CRUD API, where a full `save()` (and thus `validate()`) doesn't run
		on every single-row admin action."""
		self.total_members = len(self.members or [])
		self._recompute_rollups()

	def _recompute_rollups(self) -> None:
		"""Counts are reachability, not just validity — a Fixed Line number is
		Valid and still cannot receive anything."""
		reachable = 0
		deliverable = 0
		for m in self.members or []:
			if (
				m.phone_status == phone_validator.VALID
				and m.phone_type in phone_validator.WHATSAPP_REACHABLE_TYPES
				and not m.unsubscribed_whatsapp
			):
				reachable += 1
			if (
				m.email_normalized
				and email_validator.accepts(m.email_status)
				and not m.unsubscribed_email
			):
				deliverable += 1
		self.valid_phone_count = reachable
		self.valid_email_count = deliverable

	def region(self) -> str:
		if self.default_region:
			return self.default_region.upper()
		return (
			frappe.db.get_single_value("MSuite Settings", "default_phone_region")
			or phone_validator.DEFAULT_REGION
		).upper()

	@frappe.whitelist()
	def validate_members(self, force: int = 0) -> dict:
		"""Run the Phase 1 pipeline over every member and persist the verdicts.

		Idempotent and cache-served, so re-running costs nothing in MV credits
		unless `force` is set.
		"""
		self.check_permission("write")
		from msuite_core.services.validation import pipeline

		if not self.members:
			return {"total": 0}

		rows = [
			{"phone": m.raw_phone or m.phone_e164, "email": m.raw_email or m.email_normalized}
			for m in self.members
		]
		out = pipeline.validate_rows(rows, self.region(), force=bool(force))

		now = now_datetime()
		for member, verdict in zip(self.members, out["rows"], strict=True):
			self.apply_verdict(member, verdict, now)

		self.last_validated_at = now
		self.save(ignore_permissions=True)
		return out["summary"]

	def apply_verdict(self, member, verdict: dict, now) -> None:
		"""Write one pipeline verdict onto one member row.

		Shared by `validate_members`'s per-row loop and the CRUD API's
		`update_member` so both paths write exactly the same fields — see
		the `email_checked_at` drift this fixed for why this isn't
		duplicated per caller.
		"""
		member.phone_e164 = verdict["phone_e164"]
		member.phone_country = verdict["phone_country"]
		member.phone_type = verdict["phone_type"]
		member.phone_status = verdict["phone_status"]
		member.email_normalized = verdict["email_normalized"]
		member.email_status = verdict["email_status"] or ("Pending" if not verdict["email_normalized"] else "")
		if verdict["email_normalized"]:
			member.email_checked_at = now
		member.validation_error = verdict["phone_reason"] or verdict["email_reason"] or ""
		member.is_existing = bool(verdict.get("is_existing"))
		member.existing_reason = existing_reason_from_verdict(verdict)

	@frappe.whitelist()
	def import_from_linked_doctype(self) -> int:
		"""Pull members from another DocType (CRM Lead, Contact, ...).

		Mirrors `WhatsApp Recipient List.import_list_from_doctype`, but reads
		a phone AND an email column in one pass — which is the whole point of
		a unified audience.
		"""
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

		rows = frappe.get_list(  # honour the caller's record-level permissions
			self.doctype_to_import,
			filters=filters,
			fields=fields,
			limit=self.import_limit or 0,
		)


		added = self.add_members(
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

	def add_members(self, rows: list[dict]) -> int:
		"""Append rows, skipping ones already present.

		Dedupe runs on the *normalised* values so "+91 98765 43210" and
		"9876543210" collapse — raw-string dedupe upstream would miss that,
		and a duplicate costs a real send.
		"""
		region = self.region()
		seen = {
			(m.phone_e164 or "", (m.email_normalized or "").lower())
			for m in (self.members or [])
		}

		added = 0
		for row in rows:
			raw_phone = (row.get("raw_phone") or "").strip()
			raw_email = (row.get("raw_email") or "").strip()

			# A row with neither identifier is genuinely empty — nothing to
			# message, nothing to report. Anything the marketer actually typed
			# is kept even when it fails to parse, so the validation summary
			# can tell them "1 unparseable" instead of silently losing it.
			if not raw_phone and not raw_email:
				continue

			verdict = phone_validator.normalize(raw_phone, region)
			email = raw_email.lower()
			key = (verdict["e164"], email)

			# Unparseable-and-emailless rows all collapse to ("", "") — that's
			# not a duplicate, it's several distinct bad rows. Only dedupe on a
			# key that actually identifies someone.
			if key != ("", ""):
				if key in seen:
					continue
				seen.add(key)

			data = row.get("recipient_data") or {}
			self.append(
				"members",
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

	def members_for(self, channel: str, risk_policy: dict | None = None) -> list[dict]:
		"""Members this channel can actually reach, as plain dicts.

		The single place the "who gets this message" question is answered —
		the dispatch hub and the preview both call it, so a preview can never
		disagree with what is actually sent.
		"""
		policy = risk_policy or {}
		accepted = set(policy.get("email_accept") or []) or None

		out = []
		for m in self.members or []:
			if channel == "Email":
				if not m.email_normalized or m.unsubscribed_email:
					continue
				if not email_validator.accepts(m.email_status, accepted):
					continue
				out.append(_as_dict(m))
				continue

			# Phone channels
			if m.phone_status != phone_validator.VALID:
				continue
			if not m.phone_e164:
				# Status says Valid but the address itself is empty (data
				# desync) — `_attach_recipients` in the materializer would
				# silently drop this member with no matching entry in
				# `rejection_breakdown`. Excluding it here keeps `preview()`
				# and `build()` looking at the same set.
				continue
			if channel == "SMS":
				if m.unsubscribed_sms or m.phone_type not in phone_validator.SMS_REACHABLE_TYPES:
					continue
			else:  # WhatsApp
				if m.unsubscribed_whatsapp or m.phone_type not in phone_validator.WHATSAPP_REACHABLE_TYPES:
					continue
			out.append(_as_dict(m))

		return out

	def rejection_breakdown(self, channel: str, risk_policy: dict | None = None) -> dict[str, int]:
		"""Why members were dropped, keyed by reason — so the wizard can show
		"412 dropped: 300 invalid email, 112 landline" instead of a bare count."""
		policy = risk_policy or {}
		accepted = set(policy.get("email_accept") or []) or None
		reasons: dict[str, int] = {}

		def bump(reason):
			reasons[reason] = reasons.get(reason, 0) + 1

		for m in self.members or []:
			if channel == "Email":
				if not m.email_normalized:
					bump("No email address")
				elif m.unsubscribed_email:
					bump("Unsubscribed from email")
				elif not email_validator.accepts(m.email_status, accepted):
					bump(f"Email verdict: {m.email_status or 'unchecked'}")
				continue

			if not m.raw_phone and not m.phone_e164:
				bump("No phone number")
			elif m.phone_status != phone_validator.VALID:
				bump(f"Phone {(m.phone_status or 'Pending').lower()}")
			elif not m.phone_e164:
				# Same desync guarded against in members_for() — status is
				# Valid but the address field is empty.
				bump("No phone number")
			elif channel == "SMS" and m.unsubscribed_sms:
				bump("Unsubscribed from SMS")
			elif channel == "WhatsApp" and m.unsubscribed_whatsapp:
				bump("Unsubscribed from WhatsApp")
			elif m.phone_type not in (
				phone_validator.SMS_REACHABLE_TYPES
				if channel == "SMS"
				else phone_validator.WHATSAPP_REACHABLE_TYPES
			):
				bump(f"{m.phone_type or 'Unknown'} cannot receive {channel}")

		return reasons


def _as_dict(member) -> dict:
	return {
		"full_name": member.full_name or "",
		"phone_e164": member.phone_e164 or "",
		"email": member.email_normalized or "",
		"recipient_data": member.recipient_data or "{}",
	}


def existing_reason_from_verdict(verdict: dict) -> str:
	matched = []
	if verdict.get("phone_e164") and verdict.get("phone_existing"):
		matched.append("phone")
	if verdict.get("email_normalized") and verdict.get("email_existing"):
		matched.append("email")
	return "+".join(matched)


def _loads(raw, default):
	if not raw:
		return default
	try:
		return json.loads(raw) if isinstance(raw, str) else raw
	except (json.JSONDecodeError, TypeError):
		return default

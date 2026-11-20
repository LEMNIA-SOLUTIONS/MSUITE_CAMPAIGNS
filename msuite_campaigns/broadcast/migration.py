# Copyright (c) 2026, MSuite and contributors
# For license information, please see license.txt

"""Data migration from legacy bulk message and recipient list doctypes to unified models (Task 14).

Handles one-time migration and continuous synchronization of:
- Audience (+ Audience Member) -> Recipient List (+ Recipient Contact)
- WhatsApp Recipient List (+ WhatsApp Recipient) -> Recipient List (+ Recipient Contact)
- Email Recipient List (+ Email Recipient) -> Recipient List (+ Recipient Contact)
- Bulk WhatsApp Message -> Broadcast (+ Broadcast Channel, Broadcast Product, Broadcast Recipient)
- Bulk Email Message -> Broadcast (+ Broadcast Channel, Broadcast Recipient)
- Bulk SMS Message -> Broadcast (+ Broadcast Channel, Broadcast Recipient)

Supports dry-run validation, per-batch transactional commits, resumable execution,
collision resolution, and verification.
"""

from __future__ import annotations

import json
from typing import Any

import frappe
from frappe import _
from frappe.utils import cint, flt, now_datetime

from msuite_campaigns.broadcast.constants import (
	BROADCAST_STATUSES,
	LEGACY_BROADCAST_DOCTYPES,
	LEGACY_LIST_DOCTYPES,
	MIGRATION_BATCH,
	RECIPIENT_STATUSES,
)
from msuite_campaigns.broadcast.recipients import bulk_insert_recipients
from msuite_core.services.validation.phone import normalize


class NameMap(dict):
	"""Mapping of (legacy_doctype, legacy_name) to new unified recipient list name.

	Subclasses dict to support both tuple indexing and JSON serialization
	for bench execute compatibility.
	"""

	def _key_str(self, key: Any) -> str:
		if isinstance(key, tuple) and len(key) == 2:
			return f"{key[0]}::{key[1]}"
		return str(key)

	def __getitem__(self, key: Any) -> str:
		return super().__getitem__(self._key_str(key))

	def __setitem__(self, key: Any, value: str) -> None:
		super().__setitem__(self._key_str(key), value)

	def get(self, key: Any, default: Any = None) -> Any:
		return super().get(self._key_str(key), default)

	def __contains__(self, key: Any) -> bool:
		return super().__contains__(self._key_str(key))


def build_recipient_list_name_map() -> NameMap:
	"""Construct a deterministic mapping of legacy list names to unified Recipient List names.

	Resolves collisions across Audience, WhatsApp Recipient List, and Email Recipient List:
	- Audience retains its exact name.
	- WhatsApp lists that collide become '<name> (WhatsApp)'.
	- Email lists that collide become '<name> (Email)'.
	- Numeric suffixes (2, 3...) are appended if still colliding.

	Returns:
		NameMap: Mapping from (legacy_doctype, legacy_name) to target list_name.

	Side effects:
		None (read-only queries).
	"""
	name_map = NameMap()
	allocated: dict[str, tuple[str, str]] = {}

	# 1. Audience records get primary name priority
	audiences = frappe.db.get_all(
		"Audience",
		fields=["name", "audience_name"],
		order_by="creation asc, name asc",
	)
	for aud in audiences:
		target = aud.audience_name or aud.name
		allocated[target] = ("Audience", aud.name)
		name_map[("Audience", aud.name)] = target

	# 2. WhatsApp Recipient Lists
	wa_lists = frappe.db.get_all(
		"WhatsApp Recipient List",
		fields=["name", "list_name"],
		order_by="creation asc, name asc",
	)
	for wa in wa_lists:
		base = wa.list_name or wa.name
		if base in allocated:
			candidate = f"{base} (WhatsApp)"
			suffix = 2
			while candidate in allocated:
				candidate = f"{base} (WhatsApp) {suffix}"
				suffix += 1
			target = candidate
		else:
			target = base
		allocated[target] = ("WhatsApp Recipient List", wa.name)
		name_map[("WhatsApp Recipient List", wa.name)] = target

	# 3. Email Recipient Lists
	em_lists = frappe.db.get_all(
		"Email Recipient List",
		fields=["name", "list_name"],
		order_by="creation asc, name asc",
	)
	for em in em_lists:
		base = em.list_name or em.name
		if base in allocated:
			candidate = f"{base} (Email)"
			suffix = 2
			while candidate in allocated:
				candidate = f"{base} (Email) {suffix}"
				suffix += 1
			target = candidate
		else:
			target = base
		allocated[target] = ("Email Recipient List", em.name)
		name_map[("Email Recipient List", em.name)] = target

	return name_map


def _map_broadcast_status(doc: Any) -> tuple[str, bool]:
	"""Map legacy blast status to unified Broadcast status.

	Rules:
	- docstatus == 2 -> Cancelled
	- is_paused -> Paused
	- Scheduled, Queued, In Progress -> Paused (R10a in-flight guard)
	- AWS Dispatch Failed -> Failed
	- empty / Draft -> Draft
	- terminal Broadcast statuses -> preserved

	Args:
		doc: Legacy blast document or dict.

	Returns:
		tuple[str, bool]: (mapped_status, is_in_flight)
	"""
	docstatus = cint(getattr(doc, "docstatus", 0))
	is_paused = cint(getattr(doc, "is_paused", 0))
	raw_status = getattr(doc, "status", None) or "Draft"

	if docstatus == 2:
		return ("Cancelled", False)
	if is_paused:
		return ("Paused", False)
	if raw_status in ("Scheduled", "Queued", "In Progress"):
		return ("Paused", True)
	if raw_status == "AWS Dispatch Failed":
		return ("Failed", False)
	if raw_status == "Draft" or not raw_status:
		return ("Draft", False)
	if raw_status in BROADCAST_STATUSES:
		return (raw_status, False)

	return ("Draft", False)


def _migrate_single_broadcast(
	old: Any,
	channel_name: str,
	name_map: NameMap,
	dry_run: bool,
	legacy_doctype: str | None = None,
) -> tuple[int, list[str]]:
	"""Migrate a single legacy bulk message document to Broadcast and child records.

	Args:
		old: Legacy bulk message document or dict.
		channel_name: Channel name ('WhatsApp', 'Email', or 'SMS').
		name_map: Resolved name map for recipient lists.
		dry_run: Whether to simulate without writing.
		legacy_doctype: Optional doctype name (inferred from channel if omitted).

	Returns:
		tuple[int, list[str]]: (recipient_count, warnings)

	Raises:
		Exception: Re-raises DB errors to trigger batch-level rollback.

	Side effects:
		When dry_run=False: inserts Broadcast, Broadcast Channel, Broadcast Product,
		Broadcast Recipient records into database (no commit; batch commits).
	"""
	warnings: list[str] = []
	if not legacy_doctype:
		if channel_name == "WhatsApp":
			legacy_doctype = "Bulk WhatsApp Message"
		elif channel_name == "Email":
			legacy_doctype = "Bulk Email Message"
		else:
			legacy_doctype = "Bulk SMS Message"

	old = frappe._dict(old)
	old_doctype = legacy_doctype
	old_name = old.name

	mapped_status, _in_flight = _map_broadcast_status(old)

	# Determine recipient source and list link
	rec_type = old.get("recipient_type")
	rec_source = "Individual"
	rec_list = None

	if rec_type == "Recipient List" and old.get("recipient_list"):
		list_doctype = "WhatsApp Recipient List" if channel_name == "WhatsApp" else "Email Recipient List"
		rec_list = name_map.get((list_doctype, old.recipient_list))
		if rec_list:
			rec_source = "Recipient List"
	elif rec_type == "Audience" or (old.get("audience") and not old.get("recipient_list")):
		rec_list = name_map.get(("Audience", old.audience))
		if rec_list:
			rec_source = "Recipient List"

	# Build child recipients
	prepared_recipients: list[dict[str, Any]] = []

	if old_doctype == "Bulk WhatsApp Message":
		if rec_type == "Recipient List":
			# Rebuild from Channel Message when present
			cms = frappe.db.get_all(
				"Channel Message",
				filters={"reference_doctype": old_doctype, "reference_name": old_name},
				fields=[
					"name",
					"sender_phone",
					"channel_handle",
					"status",
					"platform_message_id",
					"sent_at",
					"delivered_at",
					"read_at",
				],
			)
			if cms:
				for cm in cms:
					address = cm.sender_phone or cm.channel_handle
					if address:
						cm_status = (cm.status or "Queued").capitalize()
						if cm_status not in RECIPIENT_STATUSES:
							cm_status = "Queued"
						prepared_recipients.append({
							"address": address,
							"status": cm_status,
							"provider_message_id": cm.platform_message_id,
							"sent_at": cm.sent_at,
							"delivered_at": cm.delivered_at,
							"read_at": cm.read_at,
						})
			else:
				warnings.append(
					f"WhatsApp blast '{old_name}' with recipient list has no Channel Message records; migrating counters only."
				)
		else:
			# Individual WhatsApp recipients
			wa_recs = frappe.db.get_all(
				"WhatsApp Recipient",
				filters={"parent": old_name, "parenttype": old_doctype},
				fields=[
					"mobile_number",
					"recipient_name",
					"recipient_data",
					"status",
					"error_code",
					"failure_reason",
					"is_retryable",
					"retry_count",
				],
			)
			for r in wa_recs:
				st = r.status or "Queued"
				if st not in RECIPIENT_STATUSES:
					st = "Failed" if "fail" in st.lower() or "invalid" in st.lower() else "Queued"
				prepared_recipients.append({
					"address": r.mobile_number,
					"recipient_name": r.recipient_name,
					"recipient_data": r.recipient_data or "{}",
					"status": st,
					"error_code": r.error_code,
					"failure_reason": r.failure_reason,
					"is_retryable": cint(r.is_retryable),
					"retry_count": cint(r.retry_count),
				})

	elif old_doctype == "Bulk Email Message":
		em_recs = frappe.db.get_all(
			"Email Recipient",
			filters={"parent": old_name, "parenttype": old_doctype},
			fields=[
				"email_address",
				"recipient_name",
				"recipient_data",
				"status",
				"error",
				"opened",
				"opened_at",
				"clicked",
				"clicked_at",
				"unsubscribed",
			],
		)
		for r in em_recs:
			st = r.status or "Queued"
			if st == "Sending":
				st = "Queued"
			elif st not in RECIPIENT_STATUSES:
				st = "Queued"
			if cint(r.unsubscribed):
				st = "Unsubscribed"
			prepared_recipients.append({
				"address": r.email_address,
				"recipient_name": r.recipient_name,
				"recipient_data": r.recipient_data or "{}",
				"status": st,
				"failure_reason": r.error,
				"opened_at": r.opened_at if cint(r.opened) else None,
				"clicked_at": r.clicked_at if cint(r.clicked) else None,
			})

	elif old_doctype == "Bulk SMS Message":
		sms_recs = frappe.db.get_all(
			"SMS Recipient",
			filters={"parent": old_name, "parenttype": old_doctype},
			fields=[
				"mobile_number",
				"recipient_name",
				"recipient_data",
				"status",
				"gateway_message_id",
				"error_code",
				"failure_reason",
				"is_retryable",
				"retry_count",
				"segments",
				"credits",
			],
		)
		for r in sms_recs:
			st = r.status or "Queued"
			if st not in RECIPIENT_STATUSES:
				st = "Queued"
			prepared_recipients.append({
				"address": r.mobile_number,
				"recipient_name": r.recipient_name,
				"recipient_data": r.recipient_data or "{}",
				"status": st,
				"provider_message_id": r.gateway_message_id,
				"error_code": r.error_code,
				"failure_reason": r.failure_reason,
				"is_retryable": cint(r.is_retryable),
				"retry_count": cint(r.retry_count),
				"segments": str(r.segments) if r.segments is not None else None,
				"credits": flt(r.credits),
			})

	if dry_run:
		return (len(prepared_recipients), warnings)

	# Create parent Broadcast
	bc = frappe.new_doc("Broadcast")
	bc.title = old.title or old_name
	bc.description = old.get("description")
	bc.campaign = old.get("campaign")
	bc.status = mapped_status
	bc.recipient_source = rec_source
	bc.recipient_list = rec_list
	bc.scheduled_time = old.get("scheduled_time")
	bc.total_recipients = cint(old.get("recipient_count") or 0) or len(prepared_recipients)
	bc.legacy_doctype = old_doctype
	bc.legacy_name = old_name

	# Build Broadcast Channel child row
	if channel_name == "WhatsApp":
		ch_row = {
			"channel": "WhatsApp",
			"status": mapped_status,
			"whatsapp_account": old.get("whatsapp_account"),
			"whatsapp_template": old.get("template"),
			"template_variables": old.get("template_variables"),
			"variable_type": old.get("variable_type") or "Common",
			"from_number": old.get("from_number"),
			"attach": old.get("attach"),
			"is_mpm": cint(old.get("is_mpm") or 0),
			"mpm_header": old.get("mpm_header"),
			"thumbnail_product_retailer_id": old.get("thumbnail_product_retailer_id"),
			"is_carousel_template": cint(old.get("is_carousel_template") or 0),
			"carousel_catalog": old.get("carousel_catalog"),
			"offer_expires_at": old.get("offer_expires_at"),
			"coupon_code": old.get("coupon_code"),
			"tap_target_url": old.get("tap_target_url"),
			"tap_target_title": old.get("tap_target_title"),
			"is_lead_automation_broadcast": cint(old.get("is_lead_automation_broadcast") or 0),
			"lead_automation_rule": old.get("lead_automation_rule"),
			"delivery_engine": old.get("delivery_engine") or "Standard",
			"use_batched_enqueue": cint(old.get("use_batched_enqueue") or 0),
			"recipient_count": cint(old.get("recipient_count") or 0),
			"sent_count": cint(old.get("sent_count") or 0),
			"retry_count": cint(old.get("retry_count") or 0),
		}
		bc.append("channels", ch_row)

		# Products
		prods = frappe.db.get_all(
			"Bulk WhatsApp Message Product",
			filters={"parent": old_name, "parenttype": old_doctype},
			fields=["item_code"],
		)
		for p in prods:
			bc.append("products", {"item_code": p.item_code})

	elif channel_name == "Email":
		ch_row = {
			"channel": "Email",
			"status": mapped_status,
			"email_account": old.get("email_account"),
			"email_template": old.get("template"),
			"subject_override": old.get("subject_override"),
			"from_name": old.get("from_name"),
			"delivery_engine": old.get("delivery_engine") or "Standard",
			"use_batched_enqueue": cint(old.get("use_batched_enqueue") or 0),
			"recipient_count": cint(old.get("recipient_count") or 0),
			"sent_count": cint(old.get("sent_count") or 0),
			"retry_count": cint(old.get("retry_count") or 0),
			"opened_count": cint(old.get("opened_count") or 0),
			"clicked_count": cint(old.get("clicked_count") or 0),
			"bounced_count": cint(old.get("bounced_count") or 0),
		}
		bc.append("channels", ch_row)

	elif channel_name == "SMS":
		ch_row = {
			"channel": "SMS",
			"status": mapped_status,
			"sms_account": old.get("sms_account"),
			"sms_template": old.get("template"),
			"template_variables": old.get("template_variables"),
			"delivery_engine": old.get("delivery_engine") or "Standard",
			"use_batched_enqueue": cint(old.get("use_batched_enqueue") or 0),
			"estimated_credits": flt(old.get("estimated_credits") or 0),
			"actual_credits": flt(old.get("actual_credits") or 0),
			"recipient_count": cint(old.get("recipient_count") or 0),
			"sent_count": cint(old.get("sent_count") or 0),
			"delivered_count": cint(old.get("delivered_count") or 0),
			"failed_count": cint(old.get("failed_count") or 0),
			"retry_count": cint(old.get("retry_count") or 0),
		}
		bc.append("channels", ch_row)

	bc.flags.ignore_permissions = True
	bc.flags.ignore_mandatory = True
	bc.flags.ignore_validate = True
	bc.flags.ignore_links = True
	bc.insert()

	# Preserve creation, modified, owner
	frappe.db.sql(
		"UPDATE `tabBroadcast` SET creation=%s, modified=%s, owner=%s, modified_by=%s WHERE name=%s",
		(old.creation, old.modified, old.owner, old.get("modified_by") or old.owner, bc.name),
	)

	# Ingest recipient rows through bulk_insert_recipients helper
	inserted = 0
	if prepared_recipients:
		inserted = bulk_insert_recipients(bc.name, channel_name, prepared_recipients)
		# Bucket addresses by target status for non-Queued rows and issue grouped updates
		status_to_addresses: dict[str, list[str]] = {}
		for rec in prepared_recipients:
			target_st = rec.get("status")
			if target_st and target_st != "Queued":
				addr = str(rec["address"]).strip()
				status_to_addresses.setdefault(target_st, []).append(addr)

		for target_st, addresses in status_to_addresses.items():
			for i in range(0, len(addresses), MIGRATION_BATCH):
				chunk = addresses[i : i + MIGRATION_BATCH]
				if not chunk:
					continue
				placeholders = ", ".join(["%s"] * len(chunk))
				frappe.db.sql(
					f"UPDATE `tabBroadcast Recipient` SET status=%s WHERE broadcast=%s AND channel=%s AND address IN ({placeholders})",
					(target_st, bc.name, channel_name, *chunk),
				)

	return (inserted, warnings)


def run(
	dry_run: bool = True,
	batch_size: int = MIGRATION_BATCH,
	refresh_counters: bool = False,
) -> dict:
	"""Run data migration from legacy bulk doctypes to unified models.

	Migrates:
	1. Recipient Lists (Audience, WhatsApp Recipient List, Email Recipient List)
	2. Broadcasts (Bulk WhatsApp Message, Bulk Email Message, Bulk SMS Message)
	3. Recipient rows (Broadcast Recipient)

	Args:
		dry_run: When True (default), writes NOTHING to the database and reports
			what would be migrated. When False, commits per batch and is resumable.
		batch_size: Number of records processed per transaction chunk (default 200).
		refresh_counters: When True, refreshes counter fields on already migrated Broadcasts.

	Returns:
		dict: Summary of migration results:
			{
				"recipient_lists": {<doctype>: {"total": int, "migrated": int, "skipped": int}},
				"broadcasts": {<doctype>: {"total": int, "migrated": int, "skipped": int}},
				"recipients": int,
				"paused_in_flight": list[dict],
				"warnings": list[str],
				"name_map": NameMap,
			}

	Raises:
		Exception: Propagates unexpected errors after rolling back the failing batch.

	Side effects:
		When dry_run=False: inserts records into tabRecipient List, tabRecipient Contact,
		tabBroadcast, tabBroadcast Channel, tabBroadcast Product, tabBroadcast Recipient;
		commits transactions per batch.
	"""
	report = {
		"recipient_lists": {dt: {"total": 0, "migrated": 0, "skipped": 0} for dt in LEGACY_LIST_DOCTYPES},
		"broadcasts": {dt: {"total": 0, "migrated": 0, "skipped": 0} for dt in LEGACY_BROADCAST_DOCTYPES},
		"recipients": 0,
		"paused_in_flight": [],
		"warnings": [],
		"name_map": build_recipient_list_name_map(),
	}

	name_map = report["name_map"]

	# =========================================================================
	# Phase 1: Migrate Recipient Lists
	# =========================================================================

	# 1.1 Audience -> Recipient List (1:1)
	audiences = frappe.db.get_all(
		"Audience",
		fields=["*"],
		order_by="creation asc, name asc",
	)
	report["recipient_lists"]["Audience"]["total"] = len(audiences)

	for i in range(0, len(audiences), batch_size):
		chunk = audiences[i : i + batch_size]
		if not dry_run:
			frappe.db.savepoint(f"aud_batch_{i}")
		try:
			for aud in chunk:
				target_name = name_map[("Audience", aud.name)]
				if frappe.db.exists("Recipient List", target_name):
					report["recipient_lists"]["Audience"]["skipped"] += 1
					continue

				if dry_run:
					report["recipient_lists"]["Audience"]["migrated"] += 1
					continue

				rl = frappe.new_doc("Recipient List")
				rl.list_name = target_name
				rl.description = aud.description
				rl.source = aud.source or "Manual"
				rl.default_region = aud.default_region
				rl.import_from_doctype = aud.import_from_doctype
				rl.doctype_to_import = aud.doctype_to_import
				rl.phone_field = aud.phone_field
				rl.email_field = aud.email_field
				rl.name_field = aud.name_field
				rl.import_filters = aud.import_filters
				rl.data_fields = aud.data_fields
				rl.import_limit = aud.import_limit
				rl.total_contacts = cint(aud.total_members)
				rl.valid_phone_count = cint(aud.valid_phone_count)
				rl.valid_email_count = cint(aud.valid_email_count)
				rl.last_validated_at = aud.last_validated_at

				members = frappe.db.get_all(
					"Audience Member",
					filters={"parent": aud.name, "parenttype": "Audience"},
					fields=["*"],
				)
				for m in members:
					rl.append("contacts", {
						"full_name": m.get("full_name"),
						"raw_phone": m.get("raw_phone"),
						"phone_e164": m.get("phone_e164"),
						"phone_country": m.get("phone_country"),
						"phone_type": m.get("phone_type"),
						"phone_status": m.get("phone_status"),
						"raw_email": m.get("raw_email"),
						"email_normalized": m.get("email_normalized"),
						"email_status": m.get("email_status"),
						"email_checked_at": m.get("email_checked_at"),
						"validation_error": m.get("validation_error"),
						"is_existing": m.get("is_existing"),
						"existing_reason": m.get("existing_reason"),
						"recipient_data": m.get("recipient_data") or "{}",
						"unsubscribed_whatsapp": cint(m.get("unsubscribed_whatsapp")),
						"unsubscribed_sms": cint(m.get("unsubscribed_sms")),
						"unsubscribed_email": cint(m.get("unsubscribed_email")),
					})

				rl.flags.ignore_permissions = True
				rl.flags.ignore_mandatory = True
				rl.flags.ignore_validate = True
				rl.flags.ignore_links = True
				rl.insert()

				frappe.db.sql(
					"UPDATE `tabRecipient List` SET creation=%s, modified=%s, owner=%s, modified_by=%s WHERE name=%s",
					(aud.creation, aud.modified, aud.owner, getattr(aud, "modified_by", None) or aud.owner, rl.name),
				)
				report["recipient_lists"]["Audience"]["migrated"] += 1

			if not dry_run:
				frappe.db.commit()
		except Exception:
			if not dry_run:
				frappe.db.rollback(save_point=f"aud_batch_{i}")
			raise

	# 1.2 WhatsApp Recipient List -> Recipient List
	wa_lists = frappe.db.get_all(
		"WhatsApp Recipient List",
		fields=["*"],
		order_by="creation asc, name asc",
	)
	report["recipient_lists"]["WhatsApp Recipient List"]["total"] = len(wa_lists)

	for i in range(0, len(wa_lists), batch_size):
		chunk = wa_lists[i : i + batch_size]
		if not dry_run:
			frappe.db.savepoint(f"wa_list_batch_{i}")
		try:
			for wa in chunk:
				target_name = name_map[("WhatsApp Recipient List", wa.name)]
				if frappe.db.exists("Recipient List", target_name):
					report["recipient_lists"]["WhatsApp Recipient List"]["skipped"] += 1
					continue

				recs = frappe.db.get_all(
					"WhatsApp Recipient",
					filters={"parent": wa.name, "parenttype": "WhatsApp Recipient List"},
					fields=["*"],
				)

				valid_phones = 0
				contact_rows = []
				for r in recs:
					raw_phone = r.get("mobile_number") or ""
					c_code = r.get("country_code") or ""
					reg = c_code if len(c_code) == 2 else None
					norm = normalize(raw_phone, default_region=reg)

					if norm.get("status") == "Valid":
						valid_phones += 1
					else:
						report["warnings"].append(
							f"WhatsApp list '{wa.name}' contact '{r.get('recipient_name')}' invalid phone '{raw_phone}': {norm.get('reason')}"
						)

					contact_rows.append({
						"full_name": r.get("recipient_name"),
						"raw_phone": raw_phone,
						"phone_e164": norm.get("e164") or "",
						"phone_country": norm.get("country") or "",
						"phone_type": norm.get("number_type") or "",
						"phone_status": norm.get("status") or "Unparseable",
						"raw_email": "",
						"email_normalized": "",
						"email_status": "",
						"recipient_data": r.get("recipient_data") or "{}",
						"unsubscribed_whatsapp": 0,
						"unsubscribed_sms": 0,
						"unsubscribed_email": 0,
					})

				if dry_run:
					report["recipient_lists"]["WhatsApp Recipient List"]["migrated"] += 1
					continue

				rl = frappe.new_doc("Recipient List")
				rl.list_name = target_name
				rl.description = wa.description
				rl.source = "DocType Import" if wa.import_from_doctype else "Manual"
				rl.import_from_doctype = wa.import_from_doctype
				rl.doctype_to_import = wa.doctype_to_import
				rl.phone_field = wa.mobile_field
				rl.name_field = wa.name_field
				rl.import_filters = wa.import_filters
				rl.data_fields = wa.data_fields
				rl.import_limit = wa.import_limit
				rl.total_contacts = len(recs)
				rl.valid_phone_count = valid_phones
				rl.valid_email_count = 0

				for c_row in contact_rows:
					rl.append("contacts", c_row)

				rl.flags.ignore_permissions = True
				rl.flags.ignore_mandatory = True
				rl.flags.ignore_validate = True
				rl.flags.ignore_links = True
				rl.insert()

				frappe.db.sql(
					"UPDATE `tabRecipient List` SET creation=%s, modified=%s, owner=%s, modified_by=%s WHERE name=%s",
					(wa.creation, wa.modified, wa.owner, getattr(wa, "modified_by", None) or wa.owner, rl.name),
				)
				report["recipient_lists"]["WhatsApp Recipient List"]["migrated"] += 1

			if not dry_run:
				frappe.db.commit()
		except Exception:
			if not dry_run:
				frappe.db.rollback(save_point=f"wa_list_batch_{i}")
			raise

	# 1.3 Email Recipient List -> Recipient List
	em_lists = frappe.db.get_all(
		"Email Recipient List",
		fields=["*"],
		order_by="creation asc, name asc",
	)
	report["recipient_lists"]["Email Recipient List"]["total"] = len(em_lists)

	for i in range(0, len(em_lists), batch_size):
		chunk = em_lists[i : i + batch_size]
		if not dry_run:
			frappe.db.savepoint(f"em_list_batch_{i}")
		try:
			for em in chunk:
				target_name = name_map[("Email Recipient List", em.name)]
				if frappe.db.exists("Recipient List", target_name):
					report["recipient_lists"]["Email Recipient List"]["skipped"] += 1
					continue

				recs = frappe.db.get_all(
					"Email Recipient",
					filters={"parent": em.name, "parenttype": "Email Recipient List"},
					fields=["*"],
				)

				valid_emails = 0
				contact_rows = []
				for r in recs:
					raw_email = r.get("email_address") or ""
					norm_email = raw_email.strip().lower()
					if norm_email:
						valid_emails += 1
					contact_rows.append({
						"full_name": r.get("recipient_name"),
						"raw_phone": "",
						"phone_e164": "",
						"phone_country": "",
						"phone_type": "",
						"phone_status": "",
						"raw_email": raw_email,
						"email_normalized": norm_email,
						"email_status": "Pending",
						"recipient_data": r.get("recipient_data") or "{}",
						"unsubscribed_whatsapp": 0,
						"unsubscribed_sms": 0,
						"unsubscribed_email": 1 if cint(r.get("unsubscribed")) else 0,
					})

				if dry_run:
					report["recipient_lists"]["Email Recipient List"]["migrated"] += 1
					continue

				rl = frappe.new_doc("Recipient List")
				rl.list_name = target_name
				rl.description = em.description
				rl.source = "DocType Import" if em.import_from_doctype else "Manual"
				rl.import_from_doctype = em.import_from_doctype
				rl.doctype_to_import = em.doctype_to_import
				rl.email_field = em.email_field
				rl.name_field = em.name_field
				rl.import_filters = em.import_filters
				rl.data_fields = em.data_fields
				rl.import_limit = em.import_limit
				rl.total_contacts = len(recs)
				rl.valid_phone_count = 0
				rl.valid_email_count = valid_emails

				for c_row in contact_rows:
					rl.append("contacts", c_row)

				rl.flags.ignore_permissions = True
				rl.flags.ignore_mandatory = True
				rl.flags.ignore_validate = True
				rl.flags.ignore_links = True
				rl.insert()

				frappe.db.sql(
					"UPDATE `tabRecipient List` SET creation=%s, modified=%s, owner=%s, modified_by=%s WHERE name=%s",
					(em.creation, em.modified, em.owner, getattr(em, "modified_by", None) or em.owner, rl.name),
				)
				report["recipient_lists"]["Email Recipient List"]["migrated"] += 1

			if not dry_run:
				frappe.db.commit()
		except Exception:
			if not dry_run:
				frappe.db.rollback(save_point=f"em_list_batch_{i}")
			raise

	# =========================================================================
	# Phase 2: Migrate Broadcasts
	# =========================================================================
	channel_configs = [
		("Bulk WhatsApp Message", "WhatsApp"),
		("Bulk Email Message", "Email"),
		("Bulk SMS Message", "SMS"),
	]

	for legacy_dt, channel_name in channel_configs:
		docs = frappe.db.get_all(
			legacy_dt,
			fields=["*"],
			order_by="creation asc, name asc",
		)
		report["broadcasts"][legacy_dt]["total"] = len(docs)

		for i in range(0, len(docs), batch_size):
			chunk = docs[i : i + batch_size]
			if not dry_run:
				frappe.db.savepoint(f"bc_{channel_name}_{i}")
			try:
				for old in chunk:
					mapped_status, is_in_flight = _map_broadcast_status(old)
					if is_in_flight:
						report["paused_in_flight"].append({
							"legacy_doctype": legacy_dt,
							"legacy_name": old.name,
							"old_status": getattr(old, "status", None),
							"new_status": "Paused",
						})

					existing_bc = frappe.db.get_value(
						"Broadcast",
						{"legacy_doctype": legacy_dt, "legacy_name": old.name},
						["name", "status"],
						as_dict=True,
					)

					if existing_bc:
						report["broadcasts"][legacy_dt]["skipped"] += 1
						if refresh_counters and not dry_run:
							# Update counters from legacy doc
							rec_cnt = cint(getattr(old, "recipient_count", 0))
							sent_cnt = cint(getattr(old, "sent_count", 0))
							retry_cnt = cint(getattr(old, "retry_count", 0))

							ch_updates = {
								"recipient_count": rec_cnt,
								"sent_count": sent_cnt,
								"retry_count": retry_cnt,
							}
							if channel_name == "Email":
								ch_updates["opened_count"] = cint(getattr(old, "opened_count", 0))
								ch_updates["clicked_count"] = cint(getattr(old, "clicked_count", 0))
								ch_updates["bounced_count"] = cint(getattr(old, "bounced_count", 0))
							elif channel_name == "SMS":
								ch_updates["delivered_count"] = cint(getattr(old, "delivered_count", 0))
								ch_updates["failed_count"] = cint(getattr(old, "failed_count", 0))

							frappe.db.set_value(
								"Broadcast Channel",
								{"parent": existing_bc.name, "channel": channel_name},
								ch_updates,
								update_modified=False,
							)
							frappe.db.set_value(
								"Broadcast",
								existing_bc.name,
								"total_recipients",
								rec_cnt,
								update_modified=False,
							)
						continue

					# Migrate the single broadcast record
					migrated_recs, single_warnings = _migrate_single_broadcast(
						old=old,
						channel_name=channel_name,
						name_map=name_map,
						dry_run=dry_run,
						legacy_doctype=legacy_dt,
					)
					report["warnings"].extend(single_warnings)
					report["broadcasts"][legacy_dt]["migrated"] += 1
					report["recipients"] += migrated_recs

				if not dry_run:
					frappe.db.commit()
			except Exception:
				if not dry_run:
					frappe.db.rollback(save_point=f"bc_{channel_name}_{i}")
				raise

	return report


def verify() -> dict:
	"""Verify migration completeness by comparing legacy counts against unified records.

	Returns:
		dict: Mapping for each legacy doctype:
			{
				<legacy_doctype>: {
					"old_count": int,
					"migrated_count": int,
					"mismatches": int,
				}
			}

	Side effects:
		None (read-only queries).
	"""
	name_map = build_recipient_list_name_map()
	results: dict[str, dict[str, int]] = {}

	# 1. Recipient Lists
	for list_dt in LEGACY_LIST_DOCTYPES:
		old_names = frappe.db.get_all(list_dt, pluck="name")
		old_count = len(old_names)
		migrated_count = 0
		for old_name in old_names:
			target_name = name_map.get((list_dt, old_name))
			if target_name and frappe.db.exists("Recipient List", target_name):
				migrated_count += 1
		results[list_dt] = {
			"old_count": old_count,
			"migrated_count": migrated_count,
			"mismatches": old_count - migrated_count,
		}

	# 2. Broadcasts
	for bc_dt in LEGACY_BROADCAST_DOCTYPES:
		old_names = frappe.db.get_all(bc_dt, pluck="name")
		old_count = len(old_names)
		migrated_count = 0
		if old_names:
			migrated_count = frappe.db.count(
				"Broadcast",
				filters={"legacy_doctype": bc_dt, "legacy_name": ["in", old_names]},
			)
		results[bc_dt] = {
			"old_count": old_count,
			"migrated_count": migrated_count,
			"mismatches": old_count - migrated_count,
		}

	return results

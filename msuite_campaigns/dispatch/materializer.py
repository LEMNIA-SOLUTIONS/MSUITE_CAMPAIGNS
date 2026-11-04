# Copyright (c) 2026, MSuite and contributors
# For license information, please see license.txt

"""
Audience -> one channel blast.

The materializer's whole job is to turn audience members into the recipient
rows each existing engine already understands, then hand the blast over. It
writes no send logic: `doc.submit()` triggers the engine's own `on_submit`,
which is the code path that has been running in production all along.

Why it copies rather than links: send state (queued/sent/failed, opens,
clicks, retry counts) is per-campaign. Writing it back onto a shared audience
would let two campaigns using the same audience corrupt each other's status —
exactly the bug `email_bulk_send_service._snapshot_list_recipients` already
exists to prevent. Same reasoning, applied one level up.

A note on `recipient_type`: blasts are created as "Individual" with the
`recipients` child table populated. That is deliberate — both engines already
treat any non-"Recipient List" value as "read the inline table", so no new
Select option and no change to either engine's recipient resolution was
needed. The `audience` link is provenance only.
"""

from __future__ import annotations

import json

import frappe

# Per-channel wiring. Adding SMS in Phase 3 is one row here plus one in
# CHANNEL_DOCTYPE — no branching inside the functions below.
CHANNEL_DOCTYPE = {
	"whatsapp": "Bulk WhatsApp Message",
	"email": "Bulk Email Message",
	"sms": "Bulk SMS Message",
}

CHANNEL_LABEL = {"whatsapp": "WhatsApp", "email": "Email", "sms": "SMS"}

# Which Marketing Campaign field links back to each channel's blast.
CAMPAIGN_LINK_FIELD = {
	"whatsapp": "whatsapp_blast",
	"email": "email_blast",
	"sms": "sms_blast",
}

# Recipient rows are written inside the caller's transaction, with no
# intermediate commit. An earlier version committed every 500 rows to keep the
# transaction small — that was wrong: the hub wraps each channel in a savepoint
# so one channel failing cannot undo another, and committing mid-build silently
# releases that savepoint. Do NOT reintroduce mid-build commits.


def build(channel: str, campaign: str | None, audience_doc, config: dict) -> dict:
	"""Create (but do not submit) one blast for `channel`.

	Returns {"name", "queued", "dropped", "reasons"}.
	"""
	if channel not in CHANNEL_DOCTYPE:
		frappe.throw(f"Unknown channel '{channel}'")

	doctype = CHANNEL_DOCTYPE[channel]
	if not frappe.db.exists("DocType", doctype):
		# SMS lands in Phase 3. Until then a caller selecting it gets a clear
		# answer rather than a DoesNotExistError from deep inside Frappe.
		frappe.throw(f"{doctype} does not exist yet — {CHANNEL_LABEL[channel]} is not installed.")

	label = CHANNEL_LABEL[channel]
	members = audience_doc.members_for(label, config.get("risk_policy"))
	reasons = audience_doc.rejection_breakdown(label, config.get("risk_policy"))

	if not members:
		return {"name": None, "queued": 0, "dropped": sum(reasons.values()), "reasons": reasons}

	doc = frappe.new_doc(doctype)
	doc.title = config.get("title") or f"{audience_doc.name} — {label}"
	doc.campaign = campaign
	doc.audience = audience_doc.name
	doc.recipient_type = "Individual"
	doc.use_batched_enqueue = 1
	doc.delivery_engine = "AWS"
	doc.template = config.get("template")
	if config.get("scheduled_time"):
		doc.scheduled_time = config["scheduled_time"]

	_apply_channel_config(doc, channel, config)

	# Recipients must be attached BEFORE insert: Bulk WhatsApp Message's
	# `validate` runs `bulk_message_validator.validate_recipients`, which
	# throws "Add at least one recipient" on an empty table. Inserting the
	# parent first and streaming children in afterwards therefore cannot work
	# without weakening that validator — which is an existing code path, so
	# off-limits under the zero-disruption directive.
	#
	# ponytail: whole audience held in memory during insert. Fine to ~50k
	# members; past that, move materialization to a per-channel background
	# job rather than relaxing the validator.
	attached = _attach_recipients(doc, channel, members)

	doc.flags.ignore_permissions = True
	doc.insert()

	return {
		"name": doc.name,
		"queued": attached,
		"dropped": sum(reasons.values()),
		"reasons": reasons,
	}


def _apply_channel_config(doc, channel: str, config: dict) -> None:
	"""Channel-specific fields. Each engine has its own account field name."""
	account = config.get("account")

	if channel == "whatsapp":
		if account:
			doc.whatsapp_account = account
		if config.get("variables") is not None:
			doc.template_variables = _dump(config["variables"])

	elif channel == "email":
		if account:
			doc.email_account = account
		if config.get("subject_override"):
			doc.subject_override = config["subject_override"]
		if config.get("from_name"):
			doc.from_name = config["from_name"]

	elif channel == "sms":
		if account:
			doc.sms_account = account
		if config.get("variables") is not None:
			doc.template_variables = _dump(config["variables"])


# Child doctype + the field that carries the address, per channel.
_ROW_SPEC = {
	"whatsapp": ("WhatsApp Recipient", "recipients", "mobile_number", "phone_e164"),
	"sms": ("SMS Recipient", "recipients", "mobile_number", "phone_e164"),
	"email": ("Email Recipient", "recipients", "email_address", "email"),
}


def _attach_recipients(doc, channel: str, members: list[dict]) -> int:
	"""Append one child row per member. Returns how many were attached."""
	_, parentfield, address_field, member_key = _ROW_SPEC[channel]

	attached = 0
	for m in members:
		address = m.get(member_key)
		if not address:
			continue
		doc.append(
			parentfield,
			{
				address_field: address,
				"recipient_name": m.get("full_name") or "",
				"recipient_data": m.get("recipient_data") or "{}",
				"status": "Queued",
			},
		)
		attached += 1

	return attached


def _dump(value) -> str:
	return value if isinstance(value, str) else json.dumps(value)

# Copyright (c) 2026, MSuite and contributors
# For license information, please see license.txt

"""Broadcast Materializer service.

Extracts recipients from a Recipient List or Individual contact list, validates
channel reachability, filters out opt-outs and invalid addresses, and bulk-inserts
rows into `tabBroadcast Recipient`. Refreshes recipient counts on Broadcast Channel
and Broadcast documents.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import validate_email_address

from msuite_campaigns.broadcast.recipients import bulk_insert_recipients
from msuite_core.services.validation import phone as phone_validator


def materialize(
	broadcast: str,
	channel: str,
	individual_rows: list[dict] | None = None,
) -> dict:
	"""Materialize recipient rows for a given broadcast and channel.

	Args:
		broadcast: Name of the Broadcast document.
		channel: Channel name ("WhatsApp", "Email", or "SMS").
		individual_rows: Optional list of dicts with 'address' and optional 'recipient_name'
			when Broadcast.recipient_source is 'Individual'.

	Returns:
		dict: {"queued": int, "dropped": int, "reasons": dict[str, int]}

	Raises:
		frappe.ValidationError: If channel is not configured on the Broadcast or source is invalid.

	Side effects:
		Inserts recipient rows into tabBroadcast Recipient (no commit; caller commits).
		Updates recipient_count on tabBroadcast Channel and total_recipients on tabBroadcast.
	"""
	broadcast_doc = frappe.get_doc("Broadcast", broadcast)
	channel_row = None
	for ch in broadcast_doc.channels or []:
		if ch.channel == channel:
			channel_row = ch
			break

	if not channel_row:
		frappe.throw(_("Channel {0} is not configured on Broadcast {1}.").format(channel, broadcast))

	reasons: dict[str, int] = {}

	def bump(reason: str) -> None:
		reasons[reason] = reasons.get(reason, 0) + 1

	rows_to_insert: list[dict] = []

	# ponytail: whole list is held in memory (~50k ceiling, same as today); upgrade path = stream contacts by SQL when lists exceed that.

	if broadcast_doc.recipient_source == "Recipient List":
		if not broadcast_doc.recipient_list:
			frappe.throw(_("Recipient List is required when recipient source is 'Recipient List'."))

		rec_list = frappe.get_doc("Recipient List", broadcast_doc.recipient_list)
		contacts = rec_list.contacts_for(channel)
		reasons = rec_list.rejection_breakdown(channel)

		child_row_map: dict[str, str] = {}
		for c in rec_list.contacts or []:
			addr = (
				c.email_normalized.lower() if channel == "Email" and c.email_normalized else (c.phone_e164 or "")
			)
			if addr and addr not in child_row_map:
				child_row_map[addr] = c.name

		for m in contacts:
			if channel == "Email":
				addr = (m.get("email_normalized") or m.get("email") or "").strip().lower()
			else:
				addr = (m.get("phone_e164") or "").strip()

			source_contact = m.get("source_contact") or child_row_map.get(addr)
			rows_to_insert.append(
				{
					"address": addr,
					"recipient_name": m.get("full_name") or m.get("recipient_name") or "",
					"recipient_data": m.get("recipient_data") or "{}",
					"source_contact": source_contact,
				}
			)

	elif broadcast_doc.recipient_source == "Individual":
		if individual_rows:
			region = phone_validator.get_system_default_region()
			seen_addresses: set[str] = set()
			valid_rows: list[dict] = []

			for item in individual_rows:
				raw_addr = str(item.get("address") or "").strip()
				if channel == "Email":
					clean_email = raw_addr.lower()
					if not clean_email or not validate_email_address(clean_email, throw=False):
						bump("invalid_email")
						continue
					norm_addr = clean_email
				else:
					parsed = phone_validator.normalize(raw_addr, region)
					if not parsed.get("e164") or parsed.get("status") != phone_validator.VALID:
						bump("invalid_phone")
						continue
					norm_addr = parsed["e164"]

				if norm_addr in seen_addresses:
					bump("duplicate")
					continue
				seen_addresses.add(norm_addr)

				valid_rows.append(
					{
						"address": norm_addr,
						"recipient_name": item.get("recipient_name") or "",
						"recipient_data": item.get("recipient_data") or "{}",
					}
				)

			if valid_rows:
				opted_out_addrs = _get_opted_out_addresses(channel, [r["address"] for r in valid_rows])
				for r in valid_rows:
					if r["address"] in opted_out_addrs:
						bump("unsubscribed")
					else:
						rows_to_insert.append(r)

	if rows_to_insert:
		bulk_insert_recipients(broadcast, channel, rows_to_insert)

	db_recipient_count = frappe.db.count("Broadcast Recipient", {"broadcast": broadcast, "channel": channel})

	channel_row_name = frappe.db.get_value("Broadcast Channel", {"parent": broadcast, "channel": channel}, "name")
	if channel_row_name:
		frappe.db.set_value(
			"Broadcast Channel",
			channel_row_name,
			"recipient_count",
			db_recipient_count,
			update_modified=False,
		)

	all_channel_counts = frappe.db.get_values(
		"Broadcast Channel",
		{"parent": broadcast},
		"recipient_count",
		as_dict=True,
	)
	total_recipients = max([c.get("recipient_count") or 0 for c in all_channel_counts] or [0])
	frappe.db.set_value("Broadcast", broadcast, "total_recipients", total_recipients, update_modified=False)

	dropped_count = sum(reasons.values())
	return {
		"queued": len(rows_to_insert),
		"dropped": dropped_count,
		"reasons": reasons,
	}


def _get_opted_out_addresses(channel: str, addresses: list[str]) -> set[str]:
	"""Look up unsubscribed addresses against tabContact.

	Args:
		channel: Channel name ("WhatsApp", "Email", or "SMS").
		addresses: List of normalized addresses to check.

	Returns:
		set[str]: Set of matching addresses that have opted out on that channel.

	Side effects:
		Executes SELECT queries on tabContact (no writes).
	"""
	opted_out: set[str] = set()
	if not addresses:
		return opted_out

	chunk_size = 1000
	if channel in ("WhatsApp", "SMS"):
		field_name = "unsubscribed_whatsapp" if channel == "WhatsApp" else "unsubscribed_sms"
		for i in range(0, len(addresses), chunk_size):
			chunk = addresses[i : i + chunk_size]
			contacts = frappe.db.sql(
				f"""
				SELECT phone_e164, mobile_no, phone
				FROM `tabContact`
				WHERE `{field_name}` = 1
				  AND (phone_e164 IN %(chunk)s OR mobile_no IN %(chunk)s OR phone IN %(chunk)s)
				""",
				{"chunk": chunk},
				as_dict=True,
			)
			for c in contacts:
				for k in ("phone_e164", "mobile_no", "phone"):
					val = c.get(k)
					if val:
						opted_out.add(val)
	else:
		for i in range(0, len(addresses), chunk_size):
			chunk = addresses[i : i + chunk_size]
			contacts = frappe.db.sql(
				"""
				SELECT email_normalized, email_id
				FROM `tabContact`
				WHERE (unsubscribed_email = 1 OR unsubscribed = 1)
				  AND (email_normalized IN %(chunk)s OR email_id IN %(chunk)s)
				""",
				{"chunk": chunk},
				as_dict=True,
			)
			for c in contacts:
				if c.get("email_normalized"):
					opted_out.add(c["email_normalized"].lower())
				if c.get("email_id"):
					opted_out.add(c["email_id"].strip().lower())

	return opted_out

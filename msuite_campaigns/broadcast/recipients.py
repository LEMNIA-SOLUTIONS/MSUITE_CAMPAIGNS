# Copyright (c) 2026, MSuite and contributors
# For license information, please see license.txt

"""Recipient ingestion and atomic status update utilities for Broadcasts."""

from __future__ import annotations

from typing import Any

import frappe
from frappe import _
from frappe.utils import cint, flt, now_datetime

from msuite_campaigns.broadcast.constants import (
	CHANNELS,
	RECIPIENT_INSERT_BATCH,
	RECIPIENT_STATUS_RANK,
)

# Allowed field names for apply_delivery_status
ALLOWED_DELIVERY_STATUS_FIELDS: frozenset[str] = frozenset(
	{
		"sent_at",
		"delivered_at",
		"read_at",
		"opened_at",
		"clicked_at",
		"error_code",
		"failure_reason",
		"is_retryable",
		"retry_count",
		"credits",
		"segments",
	}
)

# Table columns for bulk_insert into tabBroadcast Recipient
RECIPIENT_BULK_INSERT_COLUMNS: tuple[str, ...] = (
	"name",
	"owner",
	"creation",
	"modified",
	"modified_by",
	"docstatus",
	"broadcast",
	"channel",
	"address",
	"recipient_name",
	"recipient_data",
	"status",
	"error_code",
	"failure_reason",
	"is_retryable",
	"retry_count",
	"provider_message_id",
	"sent_at",
	"delivered_at",
	"read_at",
	"opened_at",
	"clicked_at",
	"segments",
	"credits",
	"source_contact",
)


def bulk_insert_recipients(broadcast: str, channel: str, rows: list[dict[str, Any]]) -> int:
	"""Bulk insert recipient rows for a given broadcast and channel.

	Validates channel and broadcast existence, skips rows with empty or whitespace
	addresses, drops in-list duplicates by address, and inserts in chunks of
	RECIPIENT_INSERT_BATCH with ignore_duplicates=True.

	Args:
		broadcast: Name of the parent Broadcast document.
		channel: Channel name ('WhatsApp', 'Email', or 'SMS').
		rows: List of dicts with recipient data (must contain 'address').

	Returns:
		int: Number of rows actually newly inserted (existing keys skipped).

	Raises:
		frappe.ValidationError: If channel is invalid or broadcast does not exist.

	Side effects:
		Inserts records into tabBroadcast Recipient (no commit; caller commits).
	"""
	if channel not in CHANNELS:
		frappe.throw(_("Invalid channel: {0}. Must be one of {1}.").format(channel, ", ".join(CHANNELS)))

	if not frappe.db.exists("Broadcast", broadcast):
		frappe.throw(_("Broadcast {0} does not exist.").format(broadcast))

	# Filter empty/whitespace address and deduplicate in-list
	seen_addresses: set[str] = set()
	filtered_rows: list[dict[str, Any]] = []
	for r in rows:
		raw_addr = r.get("address")
		if raw_addr is None:
			continue
		cleaned_addr = str(raw_addr).strip()
		if not cleaned_addr:
			continue
		if cleaned_addr in seen_addresses:
			continue
		seen_addresses.add(cleaned_addr)
		row_copy = dict(r)
		row_copy["address"] = cleaned_addr
		filtered_rows.append(row_copy)

	if not filtered_rows:
		return 0

	count_before = frappe.db.count("Broadcast Recipient", {"broadcast": broadcast, "channel": channel})

	now_val = now_datetime()
	current_user = frappe.session.user if getattr(frappe.session, "user", None) else "Administrator"

	for i in range(0, len(filtered_rows), RECIPIENT_INSERT_BATCH):
		chunk = filtered_rows[i : i + RECIPIENT_INSERT_BATCH]
		values_chunk = []
		for row in chunk:
			recipient_data = row.get("recipient_data")
			if isinstance(recipient_data, (dict, list)):
				recipient_data_str = frappe.as_json(recipient_data)
			else:
				recipient_data_str = recipient_data

			row_values = (
				frappe.generate_hash(length=10),
				current_user,
				now_val,
				now_val,
				current_user,
				0,
				broadcast,
				channel,
				row["address"],
				row.get("recipient_name"),
				recipient_data_str,
				"Queued",
				row.get("error_code"),
				row.get("failure_reason"),
				cint(row.get("is_retryable", 0)),
				cint(row.get("retry_count", 0)),
				row.get("provider_message_id"),
				row.get("sent_at"),
				row.get("delivered_at"),
				row.get("read_at"),
				row.get("opened_at"),
				row.get("clicked_at"),
				row.get("segments"),
				flt(row.get("credits", 0)),
				row.get("source_contact"),
			)
			values_chunk.append(row_values)

		frappe.db.bulk_insert(
			"Broadcast Recipient",
			list(RECIPIENT_BULK_INSERT_COLUMNS),
			values_chunk,
			ignore_duplicates=True,
		)

	count_after = frappe.db.count("Broadcast Recipient", {"broadcast": broadcast, "channel": channel})
	return count_after - count_before


def apply_delivery_status(
	broadcast: str,
	channel: str,
	provider_message_id: str,
	new_status: str | None,
	**fields: Any,
) -> bool:
	"""Apply atomic delivery or engagement status update to a Broadcast Recipient.

	Guarded by RECIPIENT_STATUS_RANK to prevent status regression under concurrent
	or out-of-order webhooks. Never executes a SELECT before UPDATE. When new_status
	is None, only applies engagement timestamps (opened_at, clicked_at) with first-value-wins
	semantics.

	Args:
		broadcast: Name of the Broadcast document.
		channel: Channel name ('WhatsApp', 'Email', or 'SMS').
		provider_message_id: External identifier from the delivery provider.
		new_status: New recipient status string, or None for engagement updates.
		**fields: Allowed additional fields (e.g. sent_at, delivered_at, read_at,
			opened_at, clicked_at, error_code, failure_reason, is_retryable).

	Returns:
		bool: True if a recipient row was updated, False if ignored (unknown ID,
			lower/equal rank, or already set engagement timestamp).

	Raises:
		frappe.ValidationError: If an unknown field name is provided or channel/status is invalid.

	Side effects:
		Executes single atomic UPDATE on tabBroadcast Recipient (no commit; caller commits).
	"""
	disallowed = set(fields.keys()) - ALLOWED_DELIVERY_STATUS_FIELDS
	if disallowed:
		frappe.throw(
			_("Disallowed field(s) in delivery status update: {0}").format(", ".join(sorted(disallowed)))
		)

	if not provider_message_id or not broadcast or not channel:
		return False

	if channel not in CHANNELS:
		frappe.throw(_("Invalid channel: {0}. Must be one of {1}.").format(channel, ", ".join(CHANNELS)))

	now_val = now_datetime()

	# Engagement updates when new_status is None
	if new_status is None:
		opened_val = fields.get("opened_at")
		clicked_val = fields.get("clicked_at")

		if not opened_val and not clicked_val:
			return False

		params = {
			"broadcast": broadcast,
			"channel": channel,
			"provider_message_id": provider_message_id,
			"now": now_val,
		}

		if opened_val and not clicked_val:
			params["opened_at"] = opened_val
			sql = """
				UPDATE `tabBroadcast Recipient`
				SET `opened_at` = %(opened_at)s,
					`modified` = %(now)s
				WHERE `broadcast` = %(broadcast)s
				  AND `channel` = %(channel)s
				  AND `provider_message_id` = %(provider_message_id)s
				  AND `opened_at` IS NULL
			"""
		elif clicked_val and not opened_val:
			params["clicked_at"] = clicked_val
			sql = """
				UPDATE `tabBroadcast Recipient`
				SET `clicked_at` = %(clicked_at)s,
					`modified` = %(now)s
				WHERE `broadcast` = %(broadcast)s
				  AND `channel` = %(channel)s
				  AND `provider_message_id` = %(provider_message_id)s
				  AND `clicked_at` IS NULL
			"""
		else:
			params["opened_at"] = opened_val
			params["clicked_at"] = clicked_val
			sql = """
				UPDATE `tabBroadcast Recipient`
				SET `opened_at` = COALESCE(`opened_at`, %(opened_at)s),
					`clicked_at` = COALESCE(`clicked_at`, %(clicked_at)s),
					`modified` = %(now)s
				WHERE `broadcast` = %(broadcast)s
				  AND `channel` = %(channel)s
				  AND `provider_message_id` = %(provider_message_id)s
				  AND (`opened_at` IS NULL OR `clicked_at` IS NULL)
			"""

		frappe.db.sql(sql, params)
		return bool(frappe.db._cursor.rowcount > 0)

	# Status transitions when new_status is provided
	if new_status not in RECIPIENT_STATUS_RANK:
		frappe.throw(_("Invalid recipient status: {0}").format(new_status))

	new_rank = RECIPIENT_STATUS_RANK[new_status]

	params = {
		"broadcast": broadcast,
		"channel": channel,
		"provider_message_id": provider_message_id,
		"new_status": new_status,
		"new_rank": new_rank,
		"now": now_val,
	}

	set_clauses = ["`status` = %(new_status)s", "`modified` = %(now)s"]

	if new_status == "Sent":
		params["sent_at"] = fields.get("sent_at") or now_val
		set_clauses.append("`sent_at` = COALESCE(`sent_at`, %(sent_at)s)")
	elif new_status == "Delivered":
		params["delivered_at"] = fields.get("delivered_at") or now_val
		set_clauses.append("`delivered_at` = COALESCE(`delivered_at`, %(delivered_at)s)")
		if "sent_at" in fields:
			params["sent_at"] = fields["sent_at"]
			set_clauses.append("`sent_at` = COALESCE(`sent_at`, %(sent_at)s)")
	elif new_status == "Read":
		params["read_at"] = fields.get("read_at") or now_val
		set_clauses.append("`read_at` = COALESCE(`read_at`, %(read_at)s)")
		if "delivered_at" in fields:
			params["delivered_at"] = fields["delivered_at"]
			set_clauses.append("`delivered_at` = COALESCE(`delivered_at`, %(delivered_at)s)")
		if "sent_at" in fields:
			params["sent_at"] = fields["sent_at"]
			set_clauses.append("`sent_at` = COALESCE(`sent_at`, %(sent_at)s)")
	elif new_status in ("Failed", "Bounced"):
		if "error_code" in fields:
			params["error_code"] = fields["error_code"]
			set_clauses.append("`error_code` = %(error_code)s")
		if "failure_reason" in fields:
			params["failure_reason"] = fields["failure_reason"]
			set_clauses.append("`failure_reason` = %(failure_reason)s")
		if "is_retryable" in fields:
			params["is_retryable"] = 1 if fields["is_retryable"] else 0
			set_clauses.append("`is_retryable` = %(is_retryable)s")

	# Pass through any other allowed fields if present
	for f_name in ("opened_at", "clicked_at"):
		if f_name in fields:
			params[f_name] = fields[f_name]
			set_clauses.append(f"`{f_name}` = COALESCE(`{f_name}`, %({f_name})s)")

	for f_name in ("retry_count", "credits", "segments"):
		if f_name in fields:
			params[f_name] = fields[f_name]
			set_clauses.append(f"`{f_name}` = %({f_name})s")

	case_branches = " ".join(f"WHEN '{st}' THEN {rank}" for st, rank in RECIPIENT_STATUS_RANK.items())
	rank_guard_expr = f"(CASE `status` {case_branches} ELSE 0 END) < %(new_rank)s"

	set_clause_str = ",\n\t\t\t".join(set_clauses)
	sql = f"""
		UPDATE `tabBroadcast Recipient`
		SET {set_clause_str}
		WHERE `broadcast` = %(broadcast)s
		  AND `channel` = %(channel)s
		  AND `provider_message_id` = %(provider_message_id)s
		  AND {rank_guard_expr}
	"""

	frappe.db.sql(sql, params)
	return bool(frappe.db._cursor.rowcount > 0)

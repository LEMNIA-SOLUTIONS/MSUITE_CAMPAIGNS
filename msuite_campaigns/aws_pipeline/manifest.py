# Copyright (c) 2026, MSuite and contributors
# For license information, please see license.txt

"""
NDJSON manifest export — the handoff format between the two planes.

Layout: **line 1 is a header block, every line after it is one recipient.**
That is what lets `BatchSplitterLambda` stream the object instead of loading
it: read one line, keep it, then fan the rest out in batches of 10 without
ever holding the body in memory.

Everything a send needs that is the SAME for every recipient — the template
source, the sender identity, the DLT registration — lives in the header
exactly once. Repeating a 20 KB HTML body per row would turn a 100k blast
into a 2 GB object.

Templates are shipped as SOURCE and rendered in the Lambda, not here.
Rendering 100k bodies in `on_submit` would trade the DB load this project
removes for CPU load on the same box. Note the two channels render
differently and always did: email templates are Jinja, SMS templates are a
`{{tag}}` regex substitution (`SMSTemplate.render`).

The one thing Lambda cannot compute is the email tracking token: it is Fernet
sealed with the site's encryption key, which never leaves Frappe. So it is
minted per row here (~50 µs, no DB) and carried in the row.

**No credential, and no reference to one, enters a manifest.** This site does
not know the name of the Secrets Manager secret holding its own gateway keys,
let alone the keys. The provider derives that name at trigger time from its
own convention and injects it into the SQS message; `BatchSplitterLambda`
merges it into each recipient job, and only the sender Lambda ever resolves it.

What the header does carry is non-secret sending configuration the client
legitimately owns: which gateway, which sender ID, which DLT registration,
which template. Knowing you send through MSG91 is not a credential.
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import frappe
from frappe import _


# Per bulk doctype: the row doctype, its address column, the statuses a
# (re)queue run will attempt, and the extra filter that keeps opted-out rows
# out. Each mirrors the engine's OWN sendable filter, not a shared guess —
# email's `_SENDABLE_STATUSES` includes "Failed" so a retry re-sends only the
# rows that errored, and SMS additionally treats "" as sendable (a row created
# through the API without an explicit status). A manifest must never carry a
# row that already went out.
_SPEC = {
	"Bulk Email Message": {
		"channel": "email",
		"row_doctype": "Email Recipient",
		"address": "email_address",
		"sendable": ("Queued", "Failed"),
		"extra_filters": {"unsubscribed": 0},
	},
	"Bulk SMS Message": {
		"channel": "sms",
		"row_doctype": "SMS Recipient",
		"address": "mobile_number",
		"sendable": ("", "Queued", "Failed"),
		"extra_filters": {},
	},
	"Bulk WhatsApp Message": {
		"channel": "whatsapp",
		"row_doctype": "WhatsApp Recipient",
		"address": "mobile_number",
		"sendable": ("", "Queued", "Failed", "Retrying"),
		"extra_filters": {},
	},
}

# Kept for callers that imported the Phase 1 name.
SENDABLE_STATUSES = _SPEC["Bulk Email Message"]["sendable"]


def spec(doctype: str) -> dict:
	found = _SPEC.get(doctype)
	if not found:
		frappe.throw(_("{0} cannot be routed through the AWS delivery engine.").format(doctype))
	return found


def _channel(doctype: str) -> dict:
	"""The adapter of the app that sends this DocType."""
	from msuite_campaigns.campaign.services.relationships import get_bulk_adapter

	adapter = get_bulk_adapter(doctype)
	if not adapter:
		frappe.throw(_("The app that sends {0} is not installed on this site.").format(doctype))
	return adapter


def row_parent(bulk_doc) -> tuple[str, str]:
	"""Where this blast's recipient rows actually live.

	Email snapshots a Recipient List into its own child table before sending
	(`_snapshot_list_recipients`), so its rows are always under the blast.
	**WhatsApp does not** — a list-backed blast reads straight off the shared
	`WhatsApp Recipient List`. Querying the blast for those rows returns
	nothing, and the manifest would come out empty with no error anywhere.
	"""
	resolve = (_channel(bulk_doc.doctype)).get("row_parent")
	return resolve(bulk_doc) if resolve else (bulk_doc.name, bulk_doc.doctype)


def build(bulk_doc, rows: list | None = None) -> tuple[dict, Iterator[str]]:
	"""Return `(header, lines)` for one bulk message doc.

	`rows` is the caller's already-deduped, already-suppression-filtered list
	(what `queue_messages` computes). Passing it in means the AWS path reuses
	that work byte-for-byte instead of re-deriving a set that could disagree.
	When omitted the sendable rows are read fresh — used by tests and by any
	standalone re-export.
	"""
	sp = spec(bulk_doc.doctype)

	if rows is None:
		parent, parenttype = row_parent(bulk_doc)
		rows = frappe.get_all(
			sp["row_doctype"],
			filters={
				"parent": parent,
				"parenttype": parenttype,
				"status": ["in", sp["sendable"]],
				**sp["extra_filters"],
			},
			fields=["name", sp["address"], "recipient_name", "recipient_data"],
			order_by="idx asc",
		)

	header = _channel(bulk_doc.doctype)["manifest_header"](bulk_doc)
	return header, _lines(header, bulk_doc, rows)


def _lines(header: dict, bulk_doc, rows) -> Iterator[str]:
	row_builder = _channel(bulk_doc.doctype)["manifest_row"]
	yield json.dumps(header, separators=(",", ":"))
	for row in rows:
		yield json.dumps(row_builder(bulk_doc, row), separators=(",", ":"))


def _dynamo_campaign_id(doc, channel: str) -> str:
	"""Key for this send's counters in the analytics table.

	Two blasts sharing a Marketing Campaign (a WhatsApp + Email broadcast,
	the normal case) would otherwise write into the SAME counters, so a
	failed email shows up as a failed WhatsApp send and vice versa. See
	`aws_pipeline.rollup.dynamo_key` -- the read side -- for the full story
	and why the format is what it is.
	"""
	from msuite_campaigns.aws_pipeline.rollup import dynamo_key

	return dynamo_key(doc.campaign or doc.name, channel)

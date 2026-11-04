# Copyright (c) 2026, MSuite and contributors
# For license information, please see license.txt

"""
Chunked enqueue — one RQ job per 500 recipients instead of one per recipient.

The existing engines enqueue a job per recipient. At the 10k–100k scale this
project targets, that puts 100k jobs into Redis for a single blast: heavy on
memory, and it starves every other job sharing the queue.

This module does NOT reimplement sending. Each chunk job loops its slice and
calls the engine's own existing per-recipient function, so retry semantics,
error classification, rate limiting, pause gates and status bookkeeping are
byte-for-byte the behaviour that already ships. The only thing that changes is
how many RQ jobs carry the same work.

Dual-path, per the zero-disruption directive: this runs only when the blast
carries `use_batched_enqueue = 1`. Frappe backfills a new Check as 0, so every
existing and in-flight blast keeps the per-recipient path untouched.

Two measured decisions, both from profiling a 50k-recipient blast (see
implementation_plan.md §7 Phase 4):

  1. The parent is loaded WITHOUT its recipients table. `frappe.get_doc` on a
     50k-row blast costs 426 ms and materialises 50k child objects; doing that
     once per chunk is ~43 s of pure waste per 50k blast and a memory spike in
     every worker. Scalars come from one `get_value`, at 0.6 ms.

  2. Slices are addressed by `idx` RANGE, not LIMIT/OFFSET. OFFSET re-scans
     everything before it — the same profile showed 145 ms to fetch the last
     500 of 50k, and that cost grows with every chunk.
"""

from __future__ import annotations

import frappe

CHUNK_SIZE = 500
QUEUE = "long"
TIMEOUT = 4000

# Rows a chunk will actually attempt. `queue_messages` flips exactly the
# sendable rows to Queued before any chunk runs, so this is what partitions
# "to send" from "already handled".
#
# This filter is load-bearing on retries: chunks are addressed by idx range
# over the whole table, so without it a re-run would re-send recipients that
# already succeeded. The WhatsApp engine has its own duplicate guard; the
# email engine does not.
SENDABLE_STATUSES = ("Queued", "Retrying")

# Child tables a handler genuinely needs, per parent doctype. Everything else
# — above all `recipients` — is deliberately left unloaded.
_SMALL_CHILD_TABLES = {
	"Bulk WhatsApp Message": (("product_ids", "Bulk WhatsApp Message Product"),),
}

_ROW_SPEC = {
	"Bulk WhatsApp Message": ("WhatsApp Recipient", "mobile_number"),
	"Bulk Email Message": ("Email Recipient", "email_address"),
	"Bulk SMS Message": ("SMS Recipient", "mobile_number"),
}


def _row_parent(doctype: str, name: str) -> tuple[str, str]:
	"""Where this blast's recipient rows actually live — `(parent, parenttype)`.

	SMS and Email always own their rows (Email snapshots a Recipient List
	into its own child table before sending), so they resolve to exactly
	`(name, doctype)` — byte-identical to the hardcoded filter this replaces.

	WhatsApp diverges: a `recipient_type == "Recipient List"` blast reads
	straight off the SHARED `WhatsApp Recipient List`, so filtering those
	rows by the blast's own name matched nothing and batched enqueue
	silently processed zero recipients. Same split as
	`whatsapp/services/bulk_send_service.py::_row_parent` and
	`aws_pipeline/manifest.py::row_parent` — ask the channel for its real
	parent instead of assuming `parent == doc.name`.

	Kept as a scalar lookup (0.6 ms) rather than a doc load, per the module
	docstring's first measured decision.
	"""
	from msuite_campaigns.campaign.services.relationships import get_bulk_adapter

	resolve = (get_bulk_adapter(doctype) or {}).get("row_parent")
	return resolve(name) if resolve else (name, doctype)


def enqueue_chunks(doctype: str, name: str) -> int:
	"""Split a blast's recipients into chunk jobs. Returns the job count.

	Chunk boundaries come from `max(idx)`, not from a row count: if any row was
	ever deleted the two disagree, and a count-derived upper bound would leave
	the tail of the table unqueued.
	"""
	spec = _ROW_SPEC.get(doctype)
	if not spec:
		frappe.log_error(title="Batched enqueue: unknown doctype", message=doctype)
		return 0

	child_doctype, _ = spec
	parent, parenttype = _row_parent(doctype, name)
	max_idx = (
		frappe.db.sql(
			f"SELECT MAX(idx) FROM `tab{child_doctype}` WHERE parent = %s AND parenttype = %s",
			(parent, parenttype),
		)[0][0]
		or 0
	)
	if not max_idx:
		return 0

	jobs = 0
	for start in range(1, max_idx + 1, CHUNK_SIZE):
		frappe.enqueue(
			"msuite_campaigns.dispatch.batched_enqueue.run_chunk",
			queue=QUEUE,
			timeout=TIMEOUT,
			job_name=f"bulk_chunk_{name}_{start}",
			doctype=doctype,
			name=name,
			start_idx=start,
			end_idx=min(start + CHUNK_SIZE - 1, max_idx),
		)
		jobs += 1
	return jobs


def run_chunk(doctype: str, name: str, start_idx: int, end_idx: int) -> None:
	"""Process one slice of a blast's recipients inline.

	Pause is re-read from the DB here as well as inside each per-recipient
	call: once per chunk so a paused blast abandons the remaining ~500
	immediately, and once per recipient because the engines' own gates already
	do it and that is what makes a mid-chunk pause responsive.
	"""
	if frappe.db.get_value(doctype, name, "is_paused"):
		return

	from msuite_campaigns.campaign.services.relationships import get_bulk_adapter

	handler = (get_bulk_adapter(doctype) or {}).get("send_one")
	if handler is None:
		frappe.log_error(title="Batched enqueue: no handler", message=f"{doctype} has no chunk handler")
		return

	doc = load_parent(doctype, name)
	if doc is None:
		return

	for recipient in _slice_recipients(doctype, name, start_idx, end_idx):
		# Per-recipient isolation: one bad number must not abandon the other
		# 499. The engines already log and record per-recipient failures, so
		# this is a backstop for anything that escapes them.
		try:
			handler(doc, recipient)
		except Exception:
			frappe.log_error(
				title=f"Batched send failed: {doctype} {name}",
				message=frappe.get_traceback(),
			)


def load_parent(doctype: str, name: str):
	"""The parent blast WITHOUT its recipients table.

	Built from a scalar row rather than `frappe.get_doc` — see the module
	docstring for the measurement. Small child tables the handlers actually
	read are loaded explicitly.
	"""
	row = frappe.db.get_value(doctype, name, "*", as_dict=True)
	if not row:
		return None

	row["doctype"] = doctype
	doc = frappe.get_doc(row)

	for fieldname, child_doctype in _SMALL_CHILD_TABLES.get(doctype, ()):
		try:
			doc.set(
				fieldname,
				frappe.get_all(
					child_doctype,
					filters={"parent": name, "parenttype": doctype},
					fields=["*"],
					order_by="idx asc",
				),
			)
		except Exception:
			# A missing optional child table must not abort the chunk.
			doc.set(fieldname, [])

	return doc


def _slice_recipients(doctype: str, name: str, start_idx: int, end_idx: int) -> list[dict]:
	"""Sendable rows whose `idx` falls in this chunk's range."""
	child_doctype, address_field = _ROW_SPEC[doctype]
	parent, parenttype = _row_parent(doctype, name)
	rows = frappe.get_all(
		child_doctype,
		filters={
			"parent": parent,
			"parenttype": parenttype,
			"idx": ["between", [start_idx, end_idx]],
			"status": ["in", SENDABLE_STATUSES],
		},
		fields=["name", "idx", address_field, "recipient_name", "recipient_data", "status"],
		order_by="idx asc",
	)
	return [dict(r) for r in rows]


# Back-compat alias — tests and earlier callers used the private name.
_load_parent = load_parent

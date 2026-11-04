# Copyright (c) 2026, MSuite and contributors
# For license information, please see license.txt

"""
Audience API.

Method names deliberately mirror `whatsapp/api/recipients.py` so the SPA's
service layer is the same shape for both models during the co-existence
period.
"""

from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.utils import now_datetime

DOCTYPE = "Audience"


def _loads(raw, default):
	if not raw:
		return default
	try:
		return json.loads(raw) if isinstance(raw, str) else raw
	except (json.JSONDecodeError, TypeError):
		return default


@frappe.whitelist()
def list_audiences(search: str = "", limit: int = 100) -> list:
	filters = {}
	if search:
		filters["audience_name"] = ["like", f"%{search}%"]
	return frappe.get_list(
		DOCTYPE,
		filters=filters,
		fields=[
			"name",
			"audience_name",
			"description",
			"source",
			"total_members",
			"valid_phone_count",
			"valid_email_count",
			"last_validated_at",
			"modified",
		],
		order_by="modified desc",
		limit=limit,
	)


@frappe.whitelist()
def get_audience(name: str) -> dict:
	return frappe.get_doc(DOCTYPE, name, check_permission=True).as_dict()


@frappe.whitelist()
def save_audience(doc: str) -> dict:
	# perm: Document.save() enforces create and write permissions
	data = _loads(doc, {})
	valid_sources = {"Manual", "CSV Upload", "DocType Import", "Segment"}
	source = data.get("source")
	if source not in valid_sources:
		source = "Manual"

	members_data = data.get("members") or []
	cleaned_members = []
	for m in members_data:
		rec_data = m.get("recipient_data")
		if isinstance(rec_data, (dict, list)):
			rec_data_str = json.dumps(rec_data)
		elif isinstance(rec_data, str) and rec_data.strip():
			rec_data_str = rec_data.strip()
		else:
			rec_data_str = "{}"
		cleaned_members.append({
			"full_name": (m.get("full_name") or "").strip(),
			"raw_phone": (m.get("raw_phone") or "").strip(),
			"raw_email": (m.get("raw_email") or "").strip(),
			"recipient_data": rec_data_str,
		})

	if data.get("name") and frappe.db.exists(DOCTYPE, data["name"]):
		audience = frappe.get_doc(DOCTYPE, data["name"])
		audience.description = data.get("description") or ""
		audience.source = source
		audience.default_region = data.get("default_region") or "IN"
		if members_data:
			audience.members = []
			audience.add_members(cleaned_members)
	else:
		audience = frappe.new_doc(DOCTYPE)
		audience.audience_name = data.get("audience_name") or data.get("name")
		audience.description = data.get("description") or ""
		audience.source = source
		audience.default_region = data.get("default_region") or "IN"
		audience.add_members(cleaned_members)

	audience.save()
	return {"name": audience.name}


@frappe.whitelist()
def delete_audience(name: str) -> dict:
	# perm: frappe.delete_doc enforces delete permissions
	frappe.delete_doc(DOCTYPE, name)
	return {"ok": True}


@frappe.whitelist()
def bulk_delete_audiences(names: str) -> dict:
	"""Batch delete audiences by list of names."""
	# perm: frappe.delete_doc enforces delete permissions
	name_list = _loads(names, [])
	if not isinstance(name_list, list):
		name_list = [names]

	deleted = 0
	for name in name_list:
		if name and frappe.db.exists(DOCTYPE, name):
			frappe.delete_doc(DOCTYPE, name, ignore_permissions=False)
			deleted += 1
	return {"ok": True, "deleted": deleted}


_CASCADE_DOCTYPES = (
	"Bulk WhatsApp Message",
	"Bulk Email Message",
	"Bulk SMS Message",
	"Social Post",
	"Ad Campaign",
	"Email Campaign",
)


def _broadcast_doctype(name: str) -> str | None:
	"""Resolve broadcast doctype by record lookup or known naming prefix."""
	if not name:
		return None
	if frappe.db.exists("Marketing Campaign", name):
		return "Marketing Campaign"
	if frappe.db.exists("Bulk WhatsApp Message", name):
		return "Bulk WhatsApp Message"
	if frappe.db.exists("Bulk Email Message", name):
		return "Bulk Email Message"
	if frappe.db.exists("Bulk SMS Message", name):
		return "Bulk SMS Message"
	if name.startswith("BULK-WA"):
		return "Bulk WhatsApp Message"
	if name.startswith("BULK-EM"):
		return "Bulk Email Message"
	if name.startswith("BULK-SM"):
		return "Bulk SMS Message"
	if name.startswith("CAMP-"):
		return "Marketing Campaign"
	return None


@frappe.whitelist()
def bulk_delete_broadcasts(items: str) -> dict:
	"""Batch delete broadcasts across Marketing Campaign, Bulk WhatsApp Message, Bulk Email Message, Bulk SMS Message."""
	item_list = _loads(items, [])
	if not isinstance(item_list, list):
		item_list = [items]

	deleted = 0
	denied = 0
	for item in item_list:
		name = None
		doctype = None
		if isinstance(item, str):
			name = item
		elif isinstance(item, dict):
			name = item.get("name")
			doctype = item.get("doctype")

		if not name:
			continue

		# If doctype is not explicitly known, detect it
		if not doctype or not frappe.db.exists(doctype, name):
			doctype = _broadcast_doctype(name)

		if not doctype or not frappe.db.exists(doctype, name):
			continue

		if not frappe.has_permission(doctype, "delete", name):
			denied += 1
			continue

		if doctype == "Marketing Campaign":
			allowed_cascade = True
			for child_dt in _CASCADE_DOCTYPES:
				if frappe.db.exists("DocType", child_dt) and not frappe.has_permission(child_dt, "delete"):
					allowed_cascade = False
					break
			if not allowed_cascade:
				denied += 1
				continue

			# Delete child broadcasts and related assets under this campaign
			for child_dt in _CASCADE_DOCTYPES:
				if frappe.db.exists("DocType", child_dt):
					field = "campaign_name" if child_dt == "Email Campaign" else "campaign"
					if frappe.get_meta(child_dt).has_field(field):
						for child in frappe.get_all(child_dt, filters={field: name}, pluck="name"):
							try:
								c_doc = frappe.get_doc(child_dt, child)
								if c_doc.docstatus == 1 and hasattr(c_doc, "cancel"):
									c_doc.flags.ignore_permissions = True
									c_doc.cancel()
								frappe.delete_doc(child_dt, child, ignore_permissions=True, force=True)
							except Exception:
								pass

		# perm: delete permission verified above for doctype and all cascade targets
		try:
			doc = frappe.get_doc(doctype, name)
			if doc.docstatus == 1 and hasattr(doc, "cancel"):
				doc.flags.ignore_permissions = True
				doc.cancel()
			frappe.delete_doc(doctype, name, ignore_permissions=True, force=True)
			deleted += 1
		except Exception as e:
			frappe.log_error(f"Failed to delete {doctype} {name}: {e}", "bulk_delete_broadcasts")

	frappe.db.commit()
	return {"ok": True, "deleted": deleted, "denied": denied}


_BROADCAST_DOCTYPES = ("Marketing Campaign", "Bulk WhatsApp Message", "Bulk SMS Message", "Bulk Email Message")


def _require_broadcast_write(name: str) -> None:
	"""pause/resume/retry act on real sends — require write on the blast
	(audit H8); these endpoints previously checked nothing."""
	for doctype in _BROADCAST_DOCTYPES:
		if frappe.db.exists(doctype, name):
			frappe.has_permission(doctype, "write", doc=name, throw=True)
			return


@frappe.whitelist()
def pause_broadcast(name: str) -> dict:
	"""Universal pause for Marketing Campaign or individual blast."""
	if not name:
		frappe.throw(_("Broadcast name is required."))
	_require_broadcast_write(name)
	doctype = _broadcast_doctype(name)
	if not doctype or not frappe.db.exists(doctype, name):
		frappe.throw(_("Unknown broadcast '{0}'.").format(name))

	doc = frappe.get_doc(doctype, name)
	if callable(getattr(doc, "check_permission", None)):
		doc.check_permission("write")

	if doctype == "Marketing Campaign":
		from msuite_campaigns.dispatch import hub
		return hub.fan_out(name, "pause")
	elif doctype == "Bulk WhatsApp Message":
		doc.pause_sending()
		return {"whatsapp": [{"outcome": "fired", "status": "Paused"}]}
	elif doctype == "Bulk SMS Message":
		doc.pause()
		return {"sms": [{"outcome": "fired", "status": "Paused"}]}
	elif doctype == "Bulk Email Message":
		frappe.db.set_value("Bulk Email Message", name, {"is_paused": 1, "status": "Paused"}, update_modified=False)
		frappe.db.commit()
		return {"email": [{"outcome": "fired", "status": "Paused"}]}
	frappe.throw(_("Unknown broadcast '{0}'.").format(name))


@frappe.whitelist()
def resume_broadcast(name: str) -> dict:
	"""Universal resume for Marketing Campaign or individual blast."""
	if not name:
		frappe.throw(_("Broadcast name is required."))
	_require_broadcast_write(name)
	doctype = _broadcast_doctype(name)
	if not doctype or not frappe.db.exists(doctype, name):
		frappe.throw(_("Unknown broadcast '{0}'.").format(name))

	doc = frappe.get_doc(doctype, name)
	if callable(getattr(doc, "check_permission", None)):
		doc.check_permission("write")

	if doctype == "Marketing Campaign":
		from msuite_campaigns.dispatch import hub
		return hub.fan_out(name, "resume")
	elif doctype == "Bulk WhatsApp Message":
		doc.resume_sending()
		return {"whatsapp": [{"outcome": "fired", "status": "In Progress"}]}
	elif doctype == "Bulk SMS Message":
		doc.resume()
		return {"sms": [{"outcome": "fired", "status": "In Progress"}]}
	elif doctype == "Bulk Email Message":
		from msuite_campaigns.campaign.services.relationships import get_bulk_adapter

		frappe.db.set_value("Bulk Email Message", name, {"is_paused": 0, "status": "Queued"}, update_modified=False)
		frappe.enqueue(
			get_bulk_adapter("Bulk Email Message")["queue_messages_path"],
			queue="default",
			job_name=f"resume_queue_messages_{name}",
			doc=name,
		)
		frappe.db.commit()
		return {"email": [{"outcome": "fired", "status": "Queued"}]}
	frappe.throw(_("Unknown broadcast '{0}'.").format(name))


@frappe.whitelist()
def retry_broadcast(name: str) -> dict:
	"""Universal retry for Marketing Campaign or individual blast."""
	if not name:
		frappe.throw(_("Broadcast name is required."))
	_require_broadcast_write(name)
	doctype = _broadcast_doctype(name)
	if not doctype or not frappe.db.exists(doctype, name):
		frappe.throw(_("Unknown broadcast '{0}'.").format(name))

	doc = frappe.get_doc(doctype, name)
	if callable(getattr(doc, "check_permission", None)):
		doc.check_permission("write")

	if doctype == "Marketing Campaign":
		from msuite_campaigns.dispatch import hub
		return hub.retry_campaign(name)
	elif doctype == "Bulk WhatsApp Message":
		if doc.docstatus == 0:
			# perm: Bulk WhatsApp Message allows submit for WhatsApp Manager/User (matrix F13); write already checked
			doc.flags.ignore_permissions = True
			doc.submit()
		elif doc.get("delivery_engine") == "AWS":
			# Same fix as dispatch/hub.py::retry_campaign: "Queued" is this
			# engine's permanent resting status even on total success, so
			# accepting it here means every call resends to 100% of the
			# original recipients. Only a genuine, durably recorded dispatch
			# failure may be retried.
			if doc.get("status") != "AWS Dispatch Failed":
				frappe.throw(_("{0} is not in a retryable state.").format(doc.doctype))
			from msuite_campaigns.aws_pipeline import dispatch as aws_dispatch
			try:
				aws_dispatch.export_and_enqueue(doc)
			except Exception:
				doc.db_set("status", "AWS Dispatch Failed", update_modified=False, commit=True)
				raise
			doc.db_set("status", "Queued", update_modified=False)
		else:
			doc.retry_failed()
		return {"whatsapp": [{"outcome": "retried", "status": doc.status}]}
	elif doctype == "Bulk Email Message":
		if doc.docstatus == 0:
			# perm: no role has submit on Bulk Email Message (parent plan F13); write already checked
			doc.flags.ignore_permissions = True
			doc.submit()
		elif doc.get("delivery_engine") == "AWS":
			# Same fix as dispatch/hub.py::retry_campaign: "Queued" is this
			# engine's permanent resting status even on total success, so
			# accepting it here means every call resends to 100% of the
			# original recipients. Only a genuine, durably recorded dispatch
			# failure may be retried.
			if doc.get("status") != "AWS Dispatch Failed":
				frappe.throw(_("{0} is not in a retryable state.").format(doc.doctype))
			from msuite_campaigns.aws_pipeline import dispatch as aws_dispatch
			try:
				aws_dispatch.export_and_enqueue(doc)
			except Exception:
				doc.db_set("status", "AWS Dispatch Failed", update_modified=False, commit=True)
				raise
			doc.db_set("status", "Queued", update_modified=False)
		else:
			doc.retry_failed()
		return {"email": [{"outcome": "retried", "status": doc.status}]}
	elif doctype == "Bulk SMS Message":
		if doc.docstatus == 0:
			# perm: no role has submit on Bulk SMS Message (parent plan F13); write already checked
			doc.flags.ignore_permissions = True
			doc.submit()
		elif doc.get("delivery_engine") == "AWS":
			# Same fix as dispatch/hub.py::retry_campaign: "Queued" is this
			# engine's permanent resting status even on total success, so
			# accepting it here means every call resends to 100% of the
			# original recipients. Only a genuine, durably recorded dispatch
			# failure may be retried.
			if doc.get("status") != "AWS Dispatch Failed":
				frappe.throw(_("{0} is not in a retryable state.").format(doc.doctype))
			from msuite_campaigns.aws_pipeline import dispatch as aws_dispatch
			try:
				aws_dispatch.export_and_enqueue(doc)
			except Exception:
				doc.db_set("status", "AWS Dispatch Failed", update_modified=False, commit=True)
				raise
			doc.db_set("status", "Queued", update_modified=False)
		else:
			doc.process_scheduled_send()
		return {"sms": [{"outcome": "retried", "status": doc.status}]}
	frappe.throw(_("Unknown broadcast '{0}'.").format(name))




@frappe.whitelist()
def get_members(
	audience: str, search: str = "", start: int = 0, page_length: int = 50, status: str = ""
) -> dict:
	"""Paged member list. Reads the child table directly — loading the parent
	Document pulls every row, which is unusable past a few thousand members."""
	frappe.has_permission(DOCTYPE, "read", audience, throw=True)
	filters = {"parent": audience, "parenttype": DOCTYPE}
	if status == "phone_invalid":
		filters["phone_status"] = ["!=", "Valid"]
	elif status == "email_rejected":
		filters["email_status"] = ["in", ["invalid", "disposable", "unknown"]]

	or_filters = {}
	if search:
		or_filters = {
			"full_name": ["like", f"%{search}%"],
			"phone_e164": ["like", f"%{search}%"],
			"email_normalized": ["like", f"%{search}%"],
		}

	rows = frappe.get_all(
		"Audience Member",
		filters=filters,
		or_filters=or_filters or None,
		fields=[
			"name",
			"idx",
			"full_name",
			"raw_phone",
			"phone_e164",
			"phone_country",
			"phone_type",
			"phone_status",
			"raw_email",
			"email_normalized",
			"email_status",
			"validation_error",
			"is_existing",
			"existing_reason",
			"unsubscribed_whatsapp",
			"unsubscribed_sms",
			"unsubscribed_email",
		],
		order_by="idx asc",
		start=int(start),
		page_length=int(page_length),
	)
	total = frappe.db.count("Audience Member", filters)
	return {"rows": rows, "total": total}


@frappe.whitelist()
def update_member(
	member_name: str, full_name: str = "", raw_phone: str = "", raw_email: str = ""
) -> dict:
	"""Edit one member's identity fields and re-validate just that row.

	Re-validation is cache-served (see `validation.pipeline`), so this costs
	nothing unless the phone/email actually changed to something uncached.
	"""
	from msuite_core.services.validation import pipeline

	parent_name = frappe.db.get_value("Audience Member", member_name, "parent")
	if not parent_name:
		frappe.throw(_("Member {0} not found.").format(member_name))

	doc = frappe.get_doc(DOCTYPE, parent_name)
	doc.check_permission("write")
	member = next((m for m in doc.members if m.name == member_name), None)
	if not member:
		frappe.throw(_("Member {0} not found.").format(member_name))

	member.full_name = full_name
	member.raw_phone = raw_phone
	member.raw_email = raw_email

	out = pipeline.validate_rows(
		[{"phone": member.raw_phone, "email": member.raw_email}], doc.region()
	)
	doc.apply_verdict(member, out["rows"][0], now_datetime())

	doc.recompute_rollups()
	doc.save()

	return member.as_dict()


@frappe.whitelist()
def delete_member(member_name: str) -> dict:
	"""Remove one member row and refresh the parent's rollup counts."""
	parent_name = frappe.db.get_value("Audience Member", member_name, "parent")
	if not parent_name:
		frappe.throw(_("Member {0} not found.").format(member_name))

	doc = frappe.get_doc(DOCTYPE, parent_name)
	doc.check_permission("write")
	doc.members = [m for m in doc.members if m.name != member_name]
	doc.recompute_rollups()
	doc.save()

	return {"ok": True}


@frappe.whitelist()
def preview_file(file_url: str) -> dict:
	"""Parse an uploaded CSV/XLSX and report what the columns look like,
	without writing anything."""
	frappe.has_permission(DOCTYPE, "create", throw=True)
	from msuite_campaigns.audience import validation_import_adapter as import_adapter

	return import_adapter.preview(file_url)


@frappe.whitelist()
def parse_file_rows(
	file_url: str,
	phone_column: str = "",
	email_column: str = "",
	name_column: str = "",
	data_columns: str = "",
) -> list[dict]:
	"""Parse uploaded CSV/Excel file into standard audience member rows in memory."""
	frappe.has_permission(DOCTYPE, "create", throw=True)
	from msuite_campaigns.audience import validation_import_adapter as import_adapter

	return import_adapter.rows_from_file(
		file_url,
		phone_column=phone_column,
		email_column=email_column,
		name_column=name_column,
		data_columns=_loads(data_columns, []),
	)


@frappe.whitelist()
def fetch_doctype_members(
	doctype_to_import: str,
	phone_field: str = "",
	email_field: str = "",
	name_field: str = "",
	filters: str = "",
	limit: int = 0,
) -> list[dict]:
	"""Fetch member rows from a specified DocType in memory."""
	from msuite_core.utils.import_guard import assert_importable

	assert_importable(doctype_to_import)

	frappe.has_permission(doctype_to_import, "read", throw=True, ignore_share_permissions=True)

	meta = frappe.get_meta(doctype_to_import)
	valid_fields = {df.fieldname for df in meta.fields} | {"name", "owner", "creation", "modified"}

	fields = ["name"]
	for f in (phone_field, email_field, name_field):
		if f and f in valid_fields and f not in fields:
			fields.append(f)

	parsed_filters = _loads(filters, {})
	rows = frappe.get_list(  # get_list: honour the caller's record-level permissions
		doctype_to_import,
		filters=parsed_filters,
		fields=fields,
		limit=int(limit or 0),
	)

	return [
		{
			"full_name": (r.get(name_field) if name_field else "") or "",
			"raw_phone": (r.get(phone_field) if phone_field else "") or "",
			"raw_email": (r.get(email_field) if email_field else "") or "",
			"recipient_data": "{}",
		}
		for r in rows
	]



@frappe.whitelist()
def import_members(
	audience: str,
	file_url: str = "",
	phone_column: str = "",
	email_column: str = "",
	name_column: str = "",
	data_columns: str = "",
	validate_now: int = 1,
) -> dict:
	"""Import rows from an uploaded file into an audience, then validate."""
	from msuite_campaigns.audience import validation_import_adapter as import_adapter

	doc = frappe.get_doc(DOCTYPE, audience)
	doc.check_permission("write")
	rows = import_adapter.rows_from_file(
		file_url,
		phone_column=phone_column,
		email_column=email_column,
		name_column=name_column,
		data_columns=_loads(data_columns, []),
	)
	added = doc.add_members(rows)
	doc.source = "CSV Upload"
	doc.save()

	summary = {}
	if int(validate_now or 0):
		summary = doc.validate_members()

	return {"added": added, "skipped": len(rows) - added, "summary": summary}


@frappe.whitelist()
def import_from_doctype(
	audience: str,
	doctype_to_import: str = "",
	phone_field: str = "",
	email_field: str = "",
	name_field: str = "",
) -> dict:
	"""Pull members from a specified DocType into an existing audience."""
	doc = frappe.get_doc(DOCTYPE, audience, check_permission="write")
	if doctype_to_import:
		doc.import_from_doctype = 1
		doc.doctype_to_import = doctype_to_import
		doc.phone_field = phone_field
		doc.email_field = email_field
		doc.name_field = name_field
		doc.save()
	added = doc.import_from_linked_doctype()
	return {"added": added, "summary": doc.validate_members()}


@frappe.whitelist()
def add_members_manual(audience: str, members: str | list) -> dict:
	"""Append manual contact rows to an existing audience and trigger validation."""
	doc = frappe.get_doc(DOCTYPE, audience)
	doc.check_permission("write")
	member_rows = _loads(members, [])
	added = doc.add_members(member_rows)
	doc.save()
	summary = doc.validate_members()
	return {"added": added, "skipped": len(member_rows) - added, "summary": summary}



@frappe.whitelist()
def validate_audience(audience: str, force: int = 0) -> dict:
	"""Re-run validation. Enqueued past a threshold — email verification is
	network-bound and will outlive a web request on a large audience."""
	doc = frappe.get_doc(DOCTYPE, audience)
	doc.check_permission("write")
	from msuite_core.services.validation.pipeline import INLINE_LIMIT

	total = frappe.db.count("Audience Member", {"parent": audience, "parenttype": DOCTYPE})
	if total > INLINE_LIMIT:
		frappe.enqueue(
			"msuite_campaigns.audience.api.v1.audience._validate_job",
			queue="long",
			timeout=3600,
			job_name=f"validate_audience_{audience}",
			audience=audience,
			force=int(force),
		)
		return {"queued": True, "total": total}

	return {"queued": False, "summary": doc.validate_members(force=int(force))}


def _validate_job(audience: str, force: int = 0) -> None:
	frappe.get_doc(DOCTYPE, audience).validate_members(force=force)


@frappe.whitelist()
def get_validation_summary(audience: str) -> dict:
	"""Per-bucket counts for the audience detail page's summary card.

	Counted in Python off the loaded document rather than with a GROUP BY:
	`members_for` and `rejection_breakdown` below already need the full child
	table, so a separate aggregate query would read the same rows twice for
	no benefit. (Frappe also rejects `count(name)` in `fields` — SQL functions
	must go through the query builder — so the DB route is not the cheap
	one-liner it looks like.)
	"""
	frappe.has_permission(DOCTYPE, "read", audience, throw=True)
	doc = frappe.get_doc(DOCTYPE, audience)

	phone: dict[str, int] = {}
	email: dict[str, int] = {}
	for m in doc.members or []:
		p = m.phone_status or "unset"
		phone[p] = phone.get(p, 0) + 1
		e = m.email_status or "unset"
		email[e] = email.get(e, 0) + 1

	channels = {}
	for label in ("WhatsApp", "SMS", "Email"):
		channels[label] = {
			"reachable": len(doc.members_for(label)),
			"reasons": doc.rejection_breakdown(label),
		}

	return {
		"total": len(doc.members or []),
		"phone": phone,
		"email": email,
		"channels": channels,
		"last_validated_at": doc.last_validated_at,
	}


# ── Broadcast detail ────────────────────────────────────────────────────

# Map of broadcast ID prefix → (doctype, channel label, recipient child doctype)
_BROADCAST_TYPES = {
	"BULK-WA": ("Bulk WhatsApp Message", "WhatsApp", "WhatsApp Recipient"),
	"BULK-EM": ("Bulk Email Message", "Email", "Email Recipient"),
	"BULK-SM": ("Bulk SMS Message", "SMS", "SMS Recipient"),
}

# Fields to pull from each blast doctype.
_BLAST_FIELDS = [
	"name", "title", "status", "from_number", "template", "template_variables",
	"recipient_type", "recipient_count", "sent_count", "retry_count",
	"scheduled_time", "audience", "campaign", "attach", "whatsapp_account",
	"email_account", "sms_account", "is_mpm", "is_paused", "creation", "modified",
]

# Recipient fields (WhatsApp / Email / SMS Recipient).
_RECIPIENT_FIELDS = [
	"name", "mobile_number", "email_address", "recipient_name", "status",
	"failure_reason", "error_code", "retry_count", "opened", "opened_at", "unsubscribed",
]



def _detect_broadcast_type(name: str):
	"""Return (doctype, channel, recipient_doctype) from prefix or DB lookup."""
	if not name:
		return None

	for prefix, info in _BROADCAST_TYPES.items():
		if name.startswith(prefix):
			return info

	if frappe.db.exists("Bulk Email Message", name):
		return ("Bulk Email Message", "Email", "Email Recipient")
	if frappe.db.exists("Bulk WhatsApp Message", name):
		return ("Bulk WhatsApp Message", "WhatsApp", "WhatsApp Recipient")
	if frappe.db.exists("Bulk SMS Message", name):
		return ("Bulk SMS Message", "SMS", "SMS Recipient")
	if frappe.db.exists("Marketing Campaign", name):
		mc = frappe.get_doc("Marketing Campaign", name)
		if mc.email_blast and frappe.db.exists("Bulk Email Message", mc.email_blast):
			return ("Bulk Email Message", "Email", "Email Recipient")
		if mc.whatsapp_blast and frappe.db.exists("Bulk WhatsApp Message", mc.whatsapp_blast):
			return ("Bulk WhatsApp Message", "WhatsApp", "WhatsApp Recipient")
		if mc.sms_blast and frappe.db.exists("Bulk SMS Message", mc.sms_blast):
			return ("Bulk SMS Message", "SMS", "SMS Recipient")
		return ("Marketing Campaign", "Unified", "Email Recipient")

	return None


@frappe.whitelist()
def get_broadcast_detail(
	name: str,
	channel: str = "",
	recipient_start: int = 0,
	recipient_length: int = 50,
	recipient_status: str = "",
) -> dict:
	"""Full broadcast detail: configuration, delivery stats, per-platform breakdowns, and paged recipients."""
	if not name:
		frappe.throw(_("Broadcast name is required."))

	doctype = _broadcast_doctype(name)
	if not doctype or not frappe.db.exists(doctype, name):
		frappe.throw(_("Broadcast '{0}' not found.").format(name))

	frappe.has_permission(doctype, "read", name, throw=True)

	if doctype == "Marketing Campaign":
		mc = frappe.get_doc("Marketing Campaign", name, check_permission="read")
		return _get_marketing_campaign_detail(
			mc,
			channel=channel,
			recipient_start=recipient_start,
			recipient_length=recipient_length,
			recipient_status=recipient_status,
		)

	type_info = _detect_broadcast_type(name)
	if not type_info:
		frappe.throw(_("Unknown broadcast type for '{0}'.").format(name))

	doctype, ch_name, recipient_dt = type_info
	ch_key = ch_name.lower()

	# Recipients (phones/emails) are returned below — enforce read access
	# on the blast itself (audit H2); the reads that follow bypass perms.
	blast_doc = frappe.get_doc(doctype, name, check_permission="read")
	existing_fields = {f.fieldname for f in frappe.get_meta(doctype).fields}
	fields_to_read = [f for f in _BLAST_FIELDS if f in existing_fields]
	if "name" not in fields_to_read:
		fields_to_read.insert(0, "name")

	blast = frappe.get_all(
		doctype,
		filters={"name": name},
		fields=fields_to_read,
		limit=1,
	)
	if not blast:
		frappe.throw(_("Broadcast '{0}' not found.").format(name))

	data = blast[0]
	data["doctype"] = doctype
	data["channel"] = f"{ch_name} Broadcast"
	data["channel_type"] = ch_name

	tv = data.get("template_variables")
	if tv and isinstance(tv, str):
		try:
			data["template_variables"] = json.loads(tv)
		except (json.JSONDecodeError, TypeError):
			pass

	campaign_name = data.get("campaign")
	data["campaign_name"] = ""
	if campaign_name and frappe.db.exists("Marketing Campaign", campaign_name):
		data["campaign_name"] = (
			frappe.db.get_value("Marketing Campaign", campaign_name, "campaign_name") or ""
		)

	total = data.get("recipient_count") or 0
	sent = data.get("sent_count") or 0
	delivered_count = 0
	failed_count = 0
	read_count = 0
	opened_count = data.get("opened_count") or 0
	bounced_count = data.get("bounced_count") or 0

	# First compute local MariaDB recipient status counts
	status_rows = frappe.db.sql(
		f"""
		SELECT status, count(*) as count
		FROM `tab{recipient_dt}`
		WHERE parent = %s AND parenttype = %s
		GROUP BY status
		""",
		(name, doctype),
		as_dict=True,
	)
	st_sent = 0
	st_delivered = 0
	st_read = 0
	st_failed = 0
	for r in status_rows:
		st = (r.status or "Queued").lower()
		if st in ("sent", "delivered", "read", "opened"):
			st_sent += r.count
		if st in ("delivered", "read", "opened"):
			st_delivered += r.count
		if st in ("read",):
			st_read += r.count
		if st in ("failed", "rejected", "bounced"):
			st_failed += r.count

	st_opened = frappe.db.count(recipient_dt, {"parent": name, "opened": 1}) if ch_name == "Email" else 0

	# Check DynamoDB if AWS delivery engine is enabled
	aws_metrics = None
	if data.get("delivery_engine") == "AWS":
		try:
			from msuite_campaigns.aws_pipeline import rollup as aws_rollup
			d_key = aws_rollup.dynamo_key(data.get("campaign") or data["name"], ch_key)
			aws_metrics = aws_rollup.fetch(d_key)
			if not aws_metrics:
				aws_metrics = aws_rollup.fetch(data["name"])
		except Exception:
			pass

	if aws_metrics:
		sent = max(int(aws_metrics.get("sent_count") or 0), st_sent, data.get("sent_count") or 0)
		delivered_count = max(int(aws_metrics.get("delivered_count") or 0), st_delivered)
		failed_count = max(int(aws_metrics.get("failed_count") or 0), st_failed)
		if ch_name == "Email":
			opened_count = max(int(aws_metrics.get("read_count") or 0), st_opened)
			read_count = 0
		else:
			read_count = max(int(aws_metrics.get("read_count") or 0), st_read)
	else:
		sent = st_sent or sent
		delivered_count = st_delivered
		read_count = st_read
		failed_count = st_failed
		if ch_name == "Email":
			opened_count = st_opened

	read_opened_total = read_count + opened_count
	delivery_rate = (
		round((delivered_count / sent * 100), 1)
		if sent
		else (round((sent / total * 100), 1) if total else 0)
	)

	# Auto-settle completed status if all recipients processed
	if total > 0 and (sent + failed_count >= total) and data.get("status") in ("In Progress", "Queued", "Draft"):
		new_status = "Partially Failed" if failed_count > 0 else "Completed"
		frappe.db.set_value(doctype, name, "status", new_status, update_modified=False)
		data["status"] = new_status

	# If messages have been sent, sync Queued rows to Sent in MariaDB
	if sent > 0:
		frappe.db.sql(
			f"UPDATE `tab{recipient_dt}` SET status = 'Sent' WHERE parent = %s AND parenttype = %s AND (status = 'Queued' OR status = '' OR status IS NULL)",
			(name, doctype),
		)
		frappe.db.commit()

	# Sync individual recipient statuses from DynamoDB AWS metrics if present
	if aws_metrics and aws_metrics.get("recipients"):
		recip_map = aws_metrics.get("recipients") or {}
		for recip_name, rinfo in recip_map.items():
			rst = rinfo.get("status")
			if rst and rst != "Queued":
				if frappe.db.exists(recipient_dt, recip_name):
					frappe.db.set_value(
						recipient_dt,
						recip_name,
						{
							"status": rst,
							"error_code": rinfo.get("error_code") or "",
							"failure_reason": rinfo.get("failure_reason") or "",
						},
						update_modified=False,
					)
		frappe.db.commit()

	status_counts = {
		"All": total,
		"Queued": max(0, total - sent - failed_count),
		"Sent": sent,
		"Delivered": delivered_count,
		"Read": read_opened_total,
		"Failed": failed_count,
	}

	recipient_filters = {"parent": name, "parenttype": doctype}
	if recipient_status and recipient_status.lower() != "all":
		recipient_filters["status"] = recipient_status

	recip_meta_fields = {f.fieldname for f in frappe.get_meta(recipient_dt).fields}
	recip_fields = [f for f in _RECIPIENT_FIELDS if f in recip_meta_fields]
	if "name" not in recip_fields:
		recip_fields.insert(0, "name")

	rows = frappe.get_all(
		recipient_dt,
		filters=recipient_filters,
		fields=recip_fields,
		order_by="idx asc",
		start=int(recipient_start),
		limit_page_length=int(recipient_length),
	)
	recipient_total = frappe.db.count(recipient_dt, recipient_filters)

	normalized_recipients = []
	for r in rows:
		contact_val = r.get("mobile_number") or r.get("email_address") or ""
		r_status = r.get("status") or "Queued"
		if r_status in ("Queued", "") and sent > 0:
			r_status = "Sent"
		if ch_name == "Email" and r.get("opened"):
			r_status = "Opened"
		normalized_recipients.append({
			"name": r.get("name"),
			"channel": ch_name,
			"channel_key": ch_key,
			"recipient_name": r.get("recipient_name") or contact_val,
			"contact": contact_val,
			"status": r_status,
			"error_code": r.get("error_code") or "",
			"failure_reason": r.get("failure_reason") or r.get("error") or "",
			"creation": r.get("creation"),
		})


	data["recipients"] = normalized_recipients
	data["recipient_total"] = recipient_total
	data["recipient_status_counts"] = status_counts
	data["sent_count"] = sent
	data["delivered_count"] = delivered_count
	data["read_opened_count"] = read_opened_total
	data["failed_count"] = failed_count
	data["delivery_rate"] = delivery_rate
	tpl_details = _get_template_details(ch_key, data.get("template") or "", blast_doc=blast_doc)

	data["channel_breakdowns"] = [
		{
			"key": ch_key,
			"channel": ch_name,
			"template": data.get("template") or "",
			"template_content": tpl_details.get("content") or "",
			"template_subject": tpl_details.get("subject") or "",
			"template_header": tpl_details.get("header") or "",
			"template_footer": tpl_details.get("footer") or "",
			"account": data.get("from_number") or data.get("whatsapp_account") or data.get("email_account") or "",
			"total": total,
			"sent": sent,
			"delivered": delivered_count,
			"read": read_count,
			"opened": opened_count,
			"bounced": bounced_count,
			"failed": failed_count,
			"delivery_rate": delivery_rate,
			"status_counts": status_counts,
		}
	]

	return data


def _get_template_details(channel_key: str, template_name: str, blast_doc=None) -> dict:
	"""Fetch content, subject, and preview metadata for a broadcast template."""
	details = {"template_name": template_name, "content": "", "subject": "", "header": "", "footer": ""}
	if not template_name:
		if blast_doc and blast_doc.get("content"):
			details["content"] = blast_doc.get("content")
		elif blast_doc and blast_doc.get("message"):
			details["content"] = blast_doc.get("message")
		return details

	try:
		if channel_key == "whatsapp":
			wa_tpl = (
				frappe.db.get_value("WhatsApp Templates", {"name": template_name}, ["template", "footer"], as_dict=True)
				or frappe.db.get_value("WhatsApp Templates", {"actual_name": template_name}, ["template", "footer"], as_dict=True)
				or frappe.db.get_value("WhatsApp Templates", {"template_name": template_name}, ["template", "footer"], as_dict=True)
			)
			if wa_tpl:
				details["content"] = wa_tpl.get("template") or ""
				details["footer"] = wa_tpl.get("footer") or ""
		elif channel_key == "email":
			em_tpl = (
				frappe.db.get_value("Email Template", {"name": template_name}, ["subject", "response_html", "response"], as_dict=True)
				or (frappe.get_doc("Email Template", template_name) if frappe.db.exists("Email Template", template_name) else None)
			)
			if em_tpl:
				details["subject"] = (blast_doc.get("subject_override") if blast_doc else None) or em_tpl.get("subject") or ""
				details["content"] = em_tpl.get("response_html") or em_tpl.get("response") or ""
			elif blast_doc:
				details["subject"] = blast_doc.get("subject_override") or blast_doc.get("title") or ""
				details["content"] = blast_doc.get("content") or ""
		elif channel_key == "sms":
			sms_tpl = (
				frappe.db.get_value("SMS Template", {"name": template_name}, ["template"], as_dict=True)
				or frappe.db.get_value("SMS Template", {"template_name": template_name}, ["template"], as_dict=True)
			)
			if sms_tpl:
				details["content"] = sms_tpl.get("template") or ""
			elif blast_doc:
				details["content"] = blast_doc.get("message") or ""
	except Exception:
		pass
	return details


def _get_marketing_campaign_detail(
	mc, channel: str = "", recipient_start: int = 0, recipient_length: int = 50, recipient_status: str = ""
) -> dict:
	"""Aggregate configuration, delivery metrics, per-platform breakdowns, and recipients for a Unified Broadcast."""
	linked_blasts = []
	if mc.whatsapp_blast and frappe.db.exists("Bulk WhatsApp Message", mc.whatsapp_blast):
		linked_blasts.append(("Bulk WhatsApp Message", "WhatsApp", "whatsapp", mc.whatsapp_blast, "WhatsApp Recipient"))
	if mc.email_blast and frappe.db.exists("Bulk Email Message", mc.email_blast):
		linked_blasts.append(("Bulk Email Message", "Email", "email", mc.email_blast, "Email Recipient"))
	if mc.sms_blast and frappe.db.exists("Bulk SMS Message", mc.sms_blast):
		linked_blasts.append(("Bulk SMS Message", "SMS", "sms", mc.sms_blast, "SMS Recipient"))

	channel_label = "Unified Broadcast"
	if len(linked_blasts) == 1:
		channel_label = f"{linked_blasts[0][1]} Broadcast"

	channel_breakdowns = []
	templates = []
	accounts = []
	whatsapp_acc = ""
	email_acc = ""
	sms_acc = ""
	from_num = ""
	total_recipients = 0
	total_sent = 0
	total_delivered = 0
	total_failed = 0
	total_read_opened = 0
	template_vars = {}
	all_recipients = []
	total_matching_recipients = 0

	channel_filter = (channel or "").lower().strip()

	for dt, ch_name, ch_key, blast_name, recip_dt in linked_blasts:
		blast_doc = frappe.get_doc(dt, blast_name)
		ch_recipients = blast_doc.get("recipient_count") or blast_doc.get("total_recipients") or 0
		ch_template = blast_doc.get("template") or ""
		ch_account = ""
		ch_sent = blast_doc.get("sent_count") or 0
		ch_delivered = 0
		ch_read = 0
		ch_opened = 0
		ch_bounced = 0
		ch_failed = 0

		if ch_template:
			templates.append(f"{ch_template} ({ch_name})")

		if ch_name == "WhatsApp":
			whatsapp_acc = blast_doc.get("whatsapp_account") or ""
			from_num = whatsapp_acc
			ch_account = whatsapp_acc
			ch_delivered = blast_doc.get("delivered_count") or 0
			ch_read = blast_doc.get("read_count") or 0
			ch_failed = blast_doc.get("failed_count") or 0
			if whatsapp_acc:
				accounts.append(f"{whatsapp_acc} (WA)")
		elif ch_name == "Email":
			email_acc = blast_doc.get("email_account") or ""
			ch_account = email_acc
			ch_delivered = blast_doc.get("delivered_count") or 0
			ch_opened = blast_doc.get("opened_count") or 0
			ch_failed = blast_doc.get("failed_count") or 0
			if email_acc:
				accounts.append(f"{email_acc} (Email)")
		elif ch_name == "SMS":
			sms_acc = blast_doc.get("sms_account") or ""
			ch_account = sms_acc
			ch_delivered = blast_doc.get("delivered_count") or 0
			ch_failed = blast_doc.get("failed_count") or 0
			if sms_acc:
				accounts.append(f"{sms_acc} (SMS)")

		tv = blast_doc.get("template_variables")
		if tv:
			if isinstance(tv, str):
				try:
					tv = json.loads(tv)
				except Exception:
					pass
			if isinstance(tv, dict):
				template_vars.update(tv)

		total_recipients += ch_recipients

		# First compute local MariaDB status counts
		status_rows = frappe.db.sql(
			f"""
			SELECT status, count(*) as count
			FROM `tab{recip_dt}`
			WHERE parent = %s AND parenttype = %s
			GROUP BY status
			""",
			(blast_name, dt),
			as_dict=True,
		)
		st_sent = 0
		st_delivered = 0
		st_read = 0
		st_failed = 0
		for r in status_rows:
			st = (r.status or "Queued").lower()
			if st in ("sent", "delivered", "read", "opened"):
				st_sent += r.count
			if st in ("delivered", "read", "opened"):
				st_delivered += r.count
			if st in ("read",):
				st_read += r.count
			if st in ("failed", "rejected", "bounced"):
				st_failed += r.count

		st_opened = frappe.db.count(recip_dt, {"parent": blast_name, "opened": 1}) if ch_name == "Email" else 0

		# Check AWS DynamoDB rollup
		aws_metrics = None
		if blast_doc.get("delivery_engine") == "AWS":
			try:
				from msuite_campaigns.aws_pipeline import rollup as aws_rollup
				aws_metrics = aws_rollup.fetch(aws_rollup.dynamo_key(mc.name, ch_key))
				if not aws_metrics:
					aws_metrics = aws_rollup.fetch(blast_name)
			except Exception:
				pass

		if aws_metrics:
			ch_sent = max(int(aws_metrics.get("sent_count") or 0), st_sent, blast_doc.get("sent_count") or 0)
			ch_delivered = max(int(aws_metrics.get("delivered_count") or 0), st_delivered)
			ch_failed = max(int(aws_metrics.get("failed_count") or 0), st_failed)
			if ch_name == "Email":
				ch_opened = max(int(aws_metrics.get("read_count") or 0), st_opened)
				ch_read = 0
			else:
				ch_read = max(int(aws_metrics.get("read_count") or 0), st_read)
		else:
			ch_sent = st_sent or ch_sent
			ch_delivered = st_delivered
			ch_read = st_read
			ch_failed = st_failed
			if ch_name == "Email":
				ch_opened = st_opened


		# Auto-settle child blast status if all processed
		if ch_recipients > 0 and (ch_sent + ch_failed >= ch_recipients) and blast_doc.get("status") in ("In Progress", "Queued", "Draft"):
			b_st = "Partially Failed" if ch_failed > 0 else "Completed"
			frappe.db.set_value(dt, blast_name, "status", b_st, update_modified=False)
			frappe.db.set_value(dt, blast_name, "sent_count", ch_sent, update_modified=False)

		ch_rate = (
			round((ch_delivered / ch_sent * 100), 1)
			if ch_sent
			else (round((ch_sent / ch_recipients * 100), 1) if ch_recipients else 0)
		)

		ch_status_counts = {
			"All": ch_recipients,
			"Queued": max(0, ch_recipients - ch_sent - ch_failed),
			"Sent": ch_sent,
			"Delivered": ch_delivered,
			"Read" if ch_name == "WhatsApp" else "Opened": ch_opened if ch_name == "Email" else ch_read,
			"Failed": ch_failed,
		}

		tpl_details = _get_template_details(ch_key, ch_template, blast_doc=blast_doc)

		ch_breakdown = {
			"key": ch_key,
			"channel": ch_name,
			"template": ch_template,
			"template_content": tpl_details.get("content") or "",
			"template_subject": tpl_details.get("subject") or "",
			"template_header": tpl_details.get("header") or "",
			"template_footer": tpl_details.get("footer") or "",
			"account": ch_account,
			"total": ch_recipients,
			"sent": ch_sent,
			"delivered": ch_delivered,
			"read": ch_read,
			"opened": ch_opened,
			"bounced": ch_bounced,
			"failed": ch_failed,
			"delivery_rate": ch_rate,
			"status_counts": ch_status_counts,
		}
		channel_breakdowns.append(ch_breakdown)

		total_sent += ch_sent
		total_delivered += ch_delivered
		total_failed += ch_failed
		total_read_opened += (ch_read + ch_opened)

		# If messages have been sent, sync Queued rows to Sent in MariaDB
		if ch_sent > 0:
			frappe.db.sql(
				f"UPDATE `tab{recip_dt}` SET status = 'Sent' WHERE parent = %s AND parenttype = %s AND (status = 'Queued' OR status = '' OR status IS NULL)",
				(blast_name, dt),
			)
			frappe.db.commit()

		# Sync individual recipient statuses from DynamoDB AWS metrics if present
		if aws_metrics and aws_metrics.get("recipients"):
			recip_map = aws_metrics.get("recipients") or {}
			for recip_name, rinfo in recip_map.items():
				rst = rinfo.get("status")
				if rst and rst != "Queued":
					if frappe.db.exists(recip_dt, recip_name):
						frappe.db.set_value(
							recip_dt,
							recip_name,
							{
								"status": rst,
								"error_code": rinfo.get("error_code") or "",
								"failure_reason": rinfo.get("failure_reason") or "",
							},
							update_modified=False,
						)
			frappe.db.commit()

		# Paged recipients fetch
		if not channel_filter or channel_filter == ch_key:
			recip_filters = {"parent": blast_name, "parenttype": dt}
			if recipient_status and recipient_status.lower() != "all":
				recip_filters["status"] = recipient_status

			recip_meta_fields = {f.fieldname for f in frappe.get_meta(recip_dt).fields}
			recip_fields = [f for f in _RECIPIENT_FIELDS if f in recip_meta_fields]
			if "name" not in recip_fields:
				recip_fields.insert(0, "name")

			limit_len = int(recipient_length)
			start_pos = int(recipient_start)

			rows = frappe.get_all(
				recip_dt,
				filters=recip_filters,
				fields=recip_fields,
				order_by="idx asc",
				start=start_pos if channel_filter else 0,
				limit_page_length=limit_len + start_pos if not channel_filter else limit_len,
			)
			for r in rows:
				contact_val = r.get("mobile_number") or r.get("email_address") or ""
				r_status = r.get("status") or "Queued"
				if r_status in ("Queued", "") and ch_sent > 0:
					r_status = "Sent"
				if ch_name == "Email" and r.get("opened"):
					r_status = "Opened"
				r_notes = r.get("failure_reason") or r.get("error") or ""
				all_recipients.append({
					"name": r.get("name"),
					"channel": ch_name,
					"channel_key": ch_key,
					"recipient_name": r.get("recipient_name") or contact_val,
					"contact": contact_val,
					"status": r_status,
					"error_code": r.get("error_code") or "",
					"failure_reason": r_notes,
					"creation": r.get("creation"),
				})
			total_matching_recipients += frappe.db.count(recip_dt, recip_filters)


	if not channel_filter:
		start_idx = int(recipient_start)
		end_idx = start_idx + int(recipient_length)
		paged_recipients = all_recipients[start_idx:end_idx]
	else:
		paged_recipients = all_recipients

	overall_delivery_rate = (
		round((total_delivered / total_sent * 100), 1)
		if total_sent
		else (round((total_sent / total_recipients * 100), 1) if total_recipients else 0)
	)

	# Auto-settle umbrella Marketing Campaign status if all finished
	curr_status = mc.status or ("In Progress" if mc.launch_status == "Launched" else (mc.launch_status or "Draft"))
	if total_recipients > 0 and (total_sent + total_failed >= total_recipients) and curr_status in ("In Progress", "Active", "Queued", "Scheduled"):
		curr_status = "Partially Failed" if total_failed > 0 else "Completed"
		frappe.db.set_value("Marketing Campaign", mc.name, "status", curr_status, update_modified=False)

	status_counts = {
		"All": total_recipients,
		"Queued": max(0, total_recipients - total_sent - total_failed),
		"Sent": total_sent,
		"Delivered": total_delivered,
		"Read": total_read_opened,
		"Failed": total_failed,
	}

	return {
		"name": mc.name,
		"title": mc.campaign_name or mc.name,
		"doctype": "Marketing Campaign",
		"channel": channel_label,
		"channel_type": "Unified Broadcast" if len(linked_blasts) > 1 else (linked_blasts[0][1] if linked_blasts else "Unified Broadcast"),
		"status": curr_status,
		"campaign_name": mc.campaign_name or "",
		"audience": mc.audience or "",
		"template": ", ".join(templates) if templates else "",
		"from_number": ", ".join(accounts) if accounts else (from_num or whatsapp_acc or email_acc or sms_acc or ""),
		"accounts": accounts,
		"whatsapp_account": whatsapp_acc,
		"email_account": email_acc,
		"sms_account": sms_acc,
		"recipient_type": "Individual",
		"scheduled_time": mc.start_date,
		"launch_status": mc.launch_status or "",
		"launch_error": mc.launch_error or "",
		"recipient_count": total_recipients,
		"sent_count": total_sent,
		"delivered_count": total_delivered,
		"read_opened_count": total_read_opened,
		"failed_count": total_failed,
		"delivery_rate": overall_delivery_rate,
		"template_variables": template_vars,
		"channel_breakdowns": channel_breakdowns,
		"recipients": paged_recipients,
		"recipient_total": total_matching_recipients if channel_filter else (total_matching_recipients or total_recipients),
		"recipient_status_counts": status_counts,
		"creation": mc.creation,
		"modified": mc.modified,
	}



@frappe.whitelist()
def search_doctypes(query: str = "") -> list[str]:
	"""Search the allow-listed DocTypes for audience import."""
	# perm: importable_doctypes() keeps only DocTypes the user can read
	from msuite_core.utils.import_guard import importable_doctypes

	return importable_doctypes(query)


@frappe.whitelist()
def get_doctype_fields(doctype: str) -> dict:
	"""Inspect a DocType to suggest fields (phone, email, name) for column mapping."""
	from msuite_core.utils.import_guard import assert_importable

	if not doctype or not frappe.db.exists("DocType", doctype):
		return {"fields": [], "defaults": {}}
	assert_importable(doctype)

	frappe.has_permission(doctype, "read", throw=True, ignore_share_permissions=True)

	meta = frappe.get_meta(doctype)
	fields = [df.fieldname for df in meta.fields if df.fieldname]
	fields.extend(["name", "owner", "creation", "modified"])
	fields.sort()

	# Smart defaults detection
	phone_field = next((f for f in fields if any(k in f.lower() for k in ["mobile", "phone", "cell", "contact_no"])), "")
	email_field = next((f for f in fields if "email" in f.lower()), "")
	name_field = next((f for f in fields if any(k in f.lower() for k in ["full_name", "lead_name", "customer_name", "first_name", "contact_name", "title"])), "")

	return {
		"fields": fields,
		"defaults": {
			"phone_field": phone_field or ("mobile_no" if "mobile_no" in fields else ""),
			"email_field": email_field or ("email_id" if "email_id" in fields else ""),
			"name_field": name_field or ("name" if "name" in fields else ""),
		},
	}


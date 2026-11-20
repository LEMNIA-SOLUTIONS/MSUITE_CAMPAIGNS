# Copyright (c) 2026, MSuite and contributors
# For license information, please see license.txt

"""Recipient List Whitelisted API.

Provides CRUD, import, validation, and reachability query endpoints for
the unified Recipient List and Recipient Contact doctypes.
"""

from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.utils import now_datetime

from msuite_campaigns.audience.api.v1.audience import _loads
from msuite_campaigns.broadcast.constants import CHANNELS

DOCTYPE: str = "Recipient List"
CHILD_DOCTYPE: str = "Recipient Contact"

DEFAULT_LIST_LIMIT: int = 100
MAX_LIST_LIMIT: int = 1000
DEFAULT_PAGE_LENGTH: int = 50
MAX_PAGE_LENGTH: int = 500
MAX_BULK_DELETE_COUNT: int = 100
MAX_CONTACTS_PAYLOAD: int = 5000
DEFAULT_REGION: str = "IN"
VALID_SOURCES: set[str] = {"Manual", "CSV Upload", "DocType Import", "Segment"}


@frappe.whitelist()
def list_recipient_lists(search: str = "", limit: int = DEFAULT_LIST_LIMIT, start: int = 0) -> list[dict]:
	"""List Recipient Lists with optional search and pagination.

	Args:
		search: Optional text filter matching against list_name.
		limit: Number of records to return (capped by MAX_LIST_LIMIT).
		start: Offset for pagination.

	Returns:
		list[dict]: List of recipient list record summaries.

	Raises:
		frappe.PermissionError: If caller lacks read permission on Recipient List.

	Side effects:
		None (read only).
	"""
	frappe.has_permission(DOCTYPE, "read", throw=True)
	limit_val = max(1, min(int(limit or DEFAULT_LIST_LIMIT), MAX_LIST_LIMIT))
	start_val = max(0, int(start or 0))

	filters = {}
	if search:
		filters["list_name"] = ["like", f"%{search}%"]

	return frappe.get_list(
		DOCTYPE,
		filters=filters,
		fields=[
			"name",
			"list_name",
			"description",
			"source",
			"total_contacts",
			"valid_phone_count",
			"valid_email_count",
			"last_validated_at",
			"modified",
		],
		order_by="modified desc",
		limit=limit_val,
		start=start_val,
	)


@frappe.whitelist()
def get_recipient_list(name: str) -> dict:
	"""Fetch a full Recipient List document by name.

	Args:
		name: ID / name of the Recipient List document.

	Returns:
		dict: Serialized Recipient List document.

	Raises:
		frappe.PermissionError: If caller lacks read permission.
		frappe.DoesNotExistError: If document does not exist.

	Side effects:
		None (read only).
	"""
	if not name or not frappe.db.exists(DOCTYPE, name):
		frappe.throw(_("Recipient List {0} not found.").format(name), frappe.DoesNotExistError)
	return frappe.get_doc(DOCTYPE, name, check_permission=True).as_dict()


@frappe.whitelist()
def save_recipient_list(doc: str | dict) -> dict:
	"""Create or update a Recipient List document with optional contact rows.

	Args:
		doc: JSON string or dict payload representing the Recipient List.

	Returns:
		dict: {"name": str} with the saved document's name.

	Raises:
		frappe.PermissionError: If caller lacks create/write permission.
		frappe.ValidationError: If payload contacts exceed MAX_CONTACTS_PAYLOAD or is invalid.

	Side effects:
		Inserts or updates Recipient List and child Recipient Contact rows in database.
	"""
	data = _loads(doc, {})
	if not isinstance(data, dict):
		frappe.throw(_("Invalid document payload."), frappe.ValidationError)

	source = data.get("source")
	if source not in VALID_SOURCES:
		source = "Manual"

	contacts_data = data.get("contacts") or data.get("members") or []
	if not isinstance(contacts_data, list):
		frappe.throw(_("Contacts must be a list."), frappe.ValidationError)

	if len(contacts_data) > MAX_CONTACTS_PAYLOAD:
		frappe.throw(
			_("Contacts payload exceeds maximum limit of {0}.").format(MAX_CONTACTS_PAYLOAD),
			frappe.ValidationError,
		)

	cleaned_contacts = []
	for c in contacts_data:
		rec_data = c.get("recipient_data")
		if isinstance(rec_data, (dict, list)):
			rec_data_str = json.dumps(rec_data)
		elif isinstance(rec_data, str) and rec_data.strip():
			rec_data_str = rec_data.strip()
		else:
			rec_data_str = "{}"
		cleaned_contacts.append({
			"full_name": (c.get("full_name") or "").strip(),
			"raw_phone": (c.get("raw_phone") or "").strip(),
			"raw_email": (c.get("raw_email") or "").strip(),
			"recipient_data": rec_data_str,
		})

	name = data.get("name")
	if name and frappe.db.exists(DOCTYPE, name):
		frappe.has_permission(DOCTYPE, "write", name, throw=True)
		recipient_list = frappe.get_doc(DOCTYPE, name)
		recipient_list.check_permission("write")
		recipient_list.description = data.get("description") or ""
		recipient_list.source = source
		recipient_list.default_region = data.get("default_region") or DEFAULT_REGION
		if contacts_data:
			recipient_list.contacts = []
			recipient_list.add_contacts(cleaned_contacts)
	else:
		frappe.has_permission(DOCTYPE, "create", throw=True)
		recipient_list = frappe.new_doc(DOCTYPE)
		recipient_list.list_name = data.get("list_name") or data.get("name")
		recipient_list.description = data.get("description") or ""
		recipient_list.source = source
		recipient_list.default_region = data.get("default_region") or DEFAULT_REGION
		if cleaned_contacts:
			recipient_list.add_contacts(cleaned_contacts)

	recipient_list.save()
	return {"name": recipient_list.name}


@frappe.whitelist()
def delete_recipient_list(name: str) -> dict:
	"""Delete a single Recipient List document by name.

	Args:
		name: Name of the Recipient List document to delete.

	Returns:
		dict: {"ok": True} on success.

	Raises:
		frappe.PermissionError: If caller lacks delete permission.
		frappe.DoesNotExistError: If document does not exist.

	Side effects:
		Deletes Recipient List and cascading child rows from database.
	"""
	if not name or not frappe.db.exists(DOCTYPE, name):
		frappe.throw(_("Recipient List {0} not found.").format(name), frappe.DoesNotExistError)
	frappe.has_permission(DOCTYPE, "delete", name, throw=True)
	frappe.delete_doc(DOCTYPE, name, ignore_permissions=False)
	return {"ok": True}


@frappe.whitelist()
def bulk_delete_recipient_lists(names: str | list) -> dict:
	"""Batch delete recipient lists by list of names.

	Args:
		names: JSON array string or Python list of recipient list names.

	Returns:
		dict: {"ok": True, "deleted": int} with count of deleted documents.

	Raises:
		frappe.PermissionError: If caller lacks delete permission.
		frappe.ValidationError: If payload exceeds MAX_BULK_DELETE_COUNT.

	Side effects:
		Deletes documents and their child records from database.
	"""
	frappe.has_permission(DOCTYPE, "delete", throw=True)
	name_list = _loads(names, [])
	if not isinstance(name_list, list):
		name_list = [names]

	if len(name_list) > MAX_BULK_DELETE_COUNT:
		frappe.throw(
			_("Exceeded maximum bulk delete count of {0}.").format(MAX_BULK_DELETE_COUNT),
			frappe.ValidationError,
		)

	deleted = 0
	for name in name_list:
		if name and frappe.db.exists(DOCTYPE, name):
			frappe.has_permission(DOCTYPE, "delete", name, throw=True)
			frappe.delete_doc(DOCTYPE, name, ignore_permissions=False)
			deleted += 1
	return {"ok": True, "deleted": deleted}


@frappe.whitelist()
def get_contacts(
	recipient_list: str,
	search: str = "",
	start: int = 0,
	page_length: int = DEFAULT_PAGE_LENGTH,
	status: str = "",
) -> dict:
	"""Paged contact list querying the child table directly.

	Args:
		recipient_list: Name of the parent Recipient List document.
		search: Optional text filter on full_name, phone_e164, or email_normalized.
		start: Pagination offset.
		page_length: Number of rows to return (capped by MAX_PAGE_LENGTH).
		status: Optional filter ('phone_invalid' or 'email_rejected').

	Returns:
		dict: {"rows": list[dict], "total": int}

	Raises:
		frappe.PermissionError: If caller lacks read permission on Recipient List.
		frappe.DoesNotExistError: If recipient list does not exist.

	Side effects:
		None (read only).
	"""
	if not recipient_list or not frappe.db.exists(DOCTYPE, recipient_list):
		frappe.throw(_("Recipient List {0} not found.").format(recipient_list), frappe.DoesNotExistError)
	frappe.has_permission(DOCTYPE, "read", recipient_list, throw=True)

	start_val = max(0, int(start or 0))
	limit_val = max(1, min(int(page_length or DEFAULT_PAGE_LENGTH), MAX_PAGE_LENGTH))

	filters = {"parent": recipient_list, "parenttype": DOCTYPE}
	if status == "phone_invalid":
		filters["phone_status"] = ["!=", "Valid"]
	elif status == "email_rejected":
		filters["email_status"] = ["in", ["invalid", "disposable", "unknown"]]

	or_filters = {}
	if search:
		search_str = str(search).strip()
		or_filters = {
			"full_name": ["like", f"%{search_str}%"],
			"phone_e164": ["like", f"%{search_str}%"],
			"email_normalized": ["like", f"%{search_str}%"],
		}

	rows = frappe.get_all(
		CHILD_DOCTYPE,
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
		start=start_val,
		page_length=limit_val,
	)
	if or_filters:
		from frappe.query_builder.functions import Count

		total = int(
			frappe.qb.get_query(
				table=CHILD_DOCTYPE,
				filters=filters,
				or_filters=or_filters,
				fields=Count("*"),
			).run()[0][0]
			or 0
		)
	else:
		total = frappe.db.count(CHILD_DOCTYPE, filters)
	return {"rows": rows, "total": total}


@frappe.whitelist()
def update_contact(
	contact_name: str,
	full_name: str = "",
	raw_phone: str = "",
	raw_email: str = "",
) -> dict:
	"""Edit one contact's identity fields and re-validate just that row.

	Args:
		contact_name: Name (ID) of the child Recipient Contact row.
		full_name: New contact name.
		raw_phone: New contact phone string.
		raw_email: New contact email string.

	Returns:
		dict: Updated contact dictionary.

	Raises:
		frappe.PermissionError: If caller lacks write permission on parent.
		frappe.DoesNotExistError: If contact or parent does not exist.

	Side effects:
		Updates contact row, re-validates via pipeline, recomputes rollups, saves parent doc.
	"""
	from msuite_core.services.validation import pipeline

	if not contact_name:
		frappe.throw(_("Contact name is required."), frappe.DoesNotExistError)

	parent_name = frappe.db.get_value(CHILD_DOCTYPE, contact_name, "parent")
	if not parent_name or not frappe.db.exists(DOCTYPE, parent_name):
		frappe.throw(_("Contact {0} not found.").format(contact_name), frappe.DoesNotExistError)

	frappe.has_permission(DOCTYPE, "write", parent_name, throw=True)
	doc = frappe.get_doc(DOCTYPE, parent_name)
	doc.check_permission("write")

	contact = next((c for c in (doc.contacts or []) if c.name == contact_name), None)
	if not contact:
		frappe.throw(_("Contact {0} not found.").format(contact_name), frappe.DoesNotExistError)

	contact.full_name = full_name
	contact.raw_phone = raw_phone
	contact.raw_email = raw_email

	out = pipeline.validate_rows(
		[{"phone": contact.raw_phone, "email": contact.raw_email}], doc.region()
	)
	doc.apply_verdict(contact, out["rows"][0], now_datetime())
	doc.recompute_rollups()
	doc.save()

	return contact.as_dict()


@frappe.whitelist()
def delete_contact(contact_name: str) -> dict:
	"""Remove one contact row and refresh the parent's rollup counts.

	Args:
		contact_name: Name (ID) of the child Recipient Contact row to remove.

	Returns:
		dict: {"ok": True} on success.

	Raises:
		frappe.PermissionError: If caller lacks write permission on parent.
		frappe.DoesNotExistError: If contact or parent does not exist.

	Side effects:
		Deletes contact row, recomputes rollups, saves parent doc.
	"""
	if not contact_name:
		frappe.throw(_("Contact name is required."), frappe.DoesNotExistError)

	parent_name = frappe.db.get_value(CHILD_DOCTYPE, contact_name, "parent")
	if not parent_name or not frappe.db.exists(DOCTYPE, parent_name):
		frappe.throw(_("Contact {0} not found.").format(contact_name), frappe.DoesNotExistError)

	frappe.has_permission(DOCTYPE, "write", parent_name, throw=True)
	doc = frappe.get_doc(DOCTYPE, parent_name)
	doc.check_permission("write")

	contact_exists = any(c.name == contact_name for c in (doc.contacts or []))
	if not contact_exists:
		frappe.throw(_("Contact {0} not found.").format(contact_name), frappe.DoesNotExistError)

	doc.contacts = [c for c in doc.contacts if c.name != contact_name]
	doc.recompute_rollups()
	doc.save()

	return {"ok": True}


@frappe.whitelist()
def add_contacts_manual(recipient_list: str, contacts: str | list) -> dict:
	"""Append manual contact rows to an existing Recipient List and trigger validation.

	Args:
		recipient_list: Name of the Recipient List document.
		contacts: JSON string or list of contact dicts.

	Returns:
		dict: {"added": int, "skipped": int, "summary": dict}

	Raises:
		frappe.PermissionError: If caller lacks write permission.
		frappe.DoesNotExistError: If recipient list does not exist.
		frappe.ValidationError: If contacts payload is invalid or exceeds MAX_CONTACTS_PAYLOAD.

	Side effects:
		Appends contacts, saves parent doc, runs validation pipeline.
	"""
	if not recipient_list or not frappe.db.exists(DOCTYPE, recipient_list):
		frappe.throw(_("Recipient List {0} not found.").format(recipient_list), frappe.DoesNotExistError)

	frappe.has_permission(DOCTYPE, "write", recipient_list, throw=True)
	doc = frappe.get_doc(DOCTYPE, recipient_list)
	doc.check_permission("write")

	contact_rows = _loads(contacts, [])
	if not isinstance(contact_rows, list):
		frappe.throw(_("Contacts must be a list of contact objects."), frappe.ValidationError)

	if len(contact_rows) > MAX_CONTACTS_PAYLOAD:
		frappe.throw(
			_("Contacts payload exceeds maximum limit of {0}.").format(MAX_CONTACTS_PAYLOAD),
			frappe.ValidationError,
		)

	added = doc.add_contacts(contact_rows)
	doc.save()
	summary = doc.validate_contacts()
	return {"added": added, "skipped": len(contact_rows) - added, "summary": summary}


@frappe.whitelist()
def preview_file(file_url: str) -> dict:
	"""Parse an uploaded CSV/XLSX file and report detected columns.

	Args:
		file_url: URL / path to uploaded CSV or Excel file.

	Returns:
		dict: Column inspection preview mapping.

	Raises:
		frappe.PermissionError: If caller lacks create permission on Recipient List.
		frappe.ValidationError: If file_url is empty.

	Side effects:
		None (read only).
	"""
	frappe.has_permission(DOCTYPE, "create", throw=True)
	if not file_url:
		frappe.throw(_("File URL is required."), frappe.ValidationError)

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
	"""Parse uploaded CSV/Excel file into standard contact rows in memory.

	Args:
		file_url: URL to uploaded file.
		phone_column: Name of column containing phone numbers.
		email_column: Name of column containing email addresses.
		name_column: Name of column containing full names.
		data_columns: JSON string or list of extra column names to carry into recipient_data.

	Returns:
		list[dict]: Parsed contact row dictionaries.

	Raises:
		frappe.PermissionError: If caller lacks create permission on Recipient List.
		frappe.ValidationError: If file_url is empty.

	Side effects:
		None (read only).
	"""
	frappe.has_permission(DOCTYPE, "create", throw=True)
	if not file_url:
		frappe.throw(_("File URL is required."), frappe.ValidationError)

	from msuite_campaigns.audience import validation_import_adapter as import_adapter
	return import_adapter.rows_from_file(
		file_url,
		phone_column=phone_column,
		email_column=email_column,
		name_column=name_column,
		data_columns=_loads(data_columns, []),
	)


@frappe.whitelist()
def fetch_doctype_contacts(
	doctype_to_import: str,
	phone_field: str = "",
	email_field: str = "",
	name_field: str = "",
	filters: str = "",
	limit: int = 0,
) -> list[dict]:
	"""Fetch contact rows from a specified DocType into memory.

	Args:
		doctype_to_import: Name of the DocType to import from.
		phone_field: DocType fieldname containing phone numbers.
		email_field: DocType fieldname containing email addresses.
		name_field: DocType fieldname containing full names.
		filters: Optional JSON string or dict of filters.
		limit: Maximum number of rows to retrieve (0 for all, up to MAX_LIST_LIMIT).

	Returns:
		list[dict]: List of contact dictionaries.

	Raises:
		frappe.PermissionError: If caller lacks read permission on Recipient List or target DocType.
		frappe.ValidationError: If target DocType is not importable.

	Side effects:
		None (read only).
	"""
	frappe.has_permission(DOCTYPE, "read", throw=True)
	from msuite_core.utils.import_guard import assert_importable

	if not doctype_to_import:
		frappe.throw(_("DocType to import is required."), frappe.ValidationError)

	assert_importable(doctype_to_import)
	frappe.has_permission(doctype_to_import, "read", throw=True, ignore_share_permissions=True)

	meta = frappe.get_meta(doctype_to_import)
	valid_fields = {df.fieldname for df in meta.fields} | {"name", "owner", "creation", "modified"}

	fields = ["name"]
	for f in (phone_field, email_field, name_field):
		if f and f in valid_fields and f not in fields:
			fields.append(f)

	parsed_filters = _loads(filters, {})
	limit_val = max(0, int(limit or 0))
	if limit_val > 0:
		limit_val = min(limit_val, MAX_LIST_LIMIT)

	rows = frappe.get_list(
		doctype_to_import,
		filters=parsed_filters,
		fields=fields,
		limit=limit_val,
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
def fetch_doctype_members(
	doctype_to_import: str,
	phone_field: str = "",
	email_field: str = "",
	name_field: str = "",
	filters: str = "",
	limit: int = 0,
) -> list[dict]:
	"""Compatibility alias for fetch_doctype_contacts."""
	return fetch_doctype_contacts(
		doctype_to_import=doctype_to_import,
		phone_field=phone_field,
		email_field=email_field,
		name_field=name_field,
		filters=filters,
		limit=limit,
	)


@frappe.whitelist()
def import_contacts(
	recipient_list: str,
	file_url: str = "",
	phone_column: str = "",
	email_column: str = "",
	name_column: str = "",
	data_columns: str = "",
	validate_now: int = 1,
) -> dict:
	"""Import contact rows from an uploaded file into an existing Recipient List.

	Args:
		recipient_list: Name of the Recipient List document.
		file_url: URL to uploaded file.
		phone_column: Name of phone column.
		email_column: Name of email column.
		name_column: Name of name column.
		data_columns: JSON string or list of extra column names.
		validate_now: 1 to run validation immediately, 0 to skip.

	Returns:
		dict: {"added": int, "skipped": int, "summary": dict}

	Raises:
		frappe.PermissionError: If caller lacks write permission.
		frappe.DoesNotExistError: If recipient list does not exist.

	Side effects:
		Appends contacts, updates doc source to CSV Upload, saves doc, runs validation if validate_now=1.
	"""
	if not recipient_list or not frappe.db.exists(DOCTYPE, recipient_list):
		frappe.throw(_("Recipient List {0} not found.").format(recipient_list), frappe.DoesNotExistError)

	frappe.has_permission(DOCTYPE, "write", recipient_list, throw=True)
	doc = frappe.get_doc(DOCTYPE, recipient_list)
	doc.check_permission("write")

	from msuite_campaigns.audience import validation_import_adapter as import_adapter
	rows = import_adapter.rows_from_file(
		file_url,
		phone_column=phone_column,
		email_column=email_column,
		name_column=name_column,
		data_columns=_loads(data_columns, []),
	)
	added = doc.add_contacts(rows)
	doc.source = "CSV Upload"
	doc.save()

	summary = {}
	if int(validate_now or 0):
		summary = doc.validate_contacts()

	return {"added": added, "skipped": len(rows) - added, "summary": summary}


@frappe.whitelist()
def import_from_doctype(
	recipient_list: str,
	doctype_to_import: str = "",
	phone_field: str = "",
	email_field: str = "",
	name_field: str = "",
) -> dict:
	"""Pull contacts from a specified DocType into an existing Recipient List.

	Args:
		recipient_list: Name of target Recipient List.
		doctype_to_import: Name of source DocType.
		phone_field: Phone fieldname.
		email_field: Email fieldname.
		name_field: Name fieldname.

	Returns:
		dict: {"added": int, "summary": dict}

	Raises:
		frappe.PermissionError: If caller lacks write permission.
		frappe.DoesNotExistError: If recipient list does not exist.

	Side effects:
		Updates DocType import settings on parent, executes import, saves doc, runs validation.
	"""
	if not recipient_list or not frappe.db.exists(DOCTYPE, recipient_list):
		frappe.throw(_("Recipient List {0} not found.").format(recipient_list), frappe.DoesNotExistError)

	frappe.has_permission(DOCTYPE, "write", recipient_list, throw=True)
	doc = frappe.get_doc(DOCTYPE, recipient_list, check_permission="write")
	if doctype_to_import:
		doc.import_from_doctype = 1
		doc.doctype_to_import = doctype_to_import
		doc.phone_field = phone_field
		doc.email_field = email_field
		doc.name_field = name_field
		doc.save()
	added = doc.import_from_linked_doctype()
	return {"added": added, "summary": doc.validate_contacts()}


@frappe.whitelist()
def validate_recipient_list(recipient_list: str, force: int = 0) -> dict:
	"""Trigger validation for a Recipient List, enqueuing background job if large.

	Args:
		recipient_list: Name of the Recipient List document.
		force: 1 to bypass cached validation verdicts, 0 otherwise.

	Returns:
		dict: {"queued": bool, "summary": dict} or {"queued": True, "total": int}

	Raises:
		frappe.PermissionError: If caller lacks write permission.
		frappe.DoesNotExistError: If recipient list does not exist.

	Side effects:
		May enqueue background job or execute inline validation saving contact verdicts.
	"""
	if not recipient_list or not frappe.db.exists(DOCTYPE, recipient_list):
		frappe.throw(_("Recipient List {0} not found.").format(recipient_list), frappe.DoesNotExistError)

	frappe.has_permission(DOCTYPE, "write", recipient_list, throw=True)
	doc = frappe.get_doc(DOCTYPE, recipient_list)
	doc.check_permission("write")

	from msuite_core.services.validation.pipeline import INLINE_LIMIT
	total = frappe.db.count(CHILD_DOCTYPE, {"parent": recipient_list, "parenttype": DOCTYPE})
	if total > INLINE_LIMIT:
		frappe.enqueue(
			"msuite_campaigns.audience.api.v1.recipient_list._validate_job",
			queue="long",
			timeout=3600,
			job_name=f"validate_recipient_list_{recipient_list}",
			recipient_list=recipient_list,
			force=int(force),
		)
		return {"queued": True, "total": total}

	return {"queued": False, "summary": doc.validate_contacts(force=int(force))}


def _validate_job(recipient_list: str, force: int = 0) -> None:
	"""Background job executing validation on a Recipient List document.

	Args:
		recipient_list: Name of the Recipient List document.
		force: 1 to bypass cached verdicts.

	Side effects:
		Executes validate_contacts and commits verdicts to database.
	"""
	frappe.get_doc(DOCTYPE, recipient_list).validate_contacts(force=force)


@frappe.whitelist()
def get_validation_summary(recipient_list: str) -> dict:
	"""Per-bucket counts and reachability breakdown for the Recipient List detail page.

	Args:
		recipient_list: Name of the Recipient List document.

	Returns:
		dict: Summary with total, phone verdicts, email verdicts, and channel reachability breakdowns.

	Raises:
		frappe.PermissionError: If caller lacks read permission.
		frappe.DoesNotExistError: If recipient list does not exist.

	Side effects:
		None (read only).
	"""
	if not recipient_list or not frappe.db.exists(DOCTYPE, recipient_list):
		frappe.throw(_("Recipient List {0} not found.").format(recipient_list), frappe.DoesNotExistError)

	frappe.has_permission(DOCTYPE, "read", recipient_list, throw=True)
	doc = frappe.get_doc(DOCTYPE, recipient_list)

	phone: dict[str, int] = {}
	email: dict[str, int] = {}
	for c in doc.contacts or []:
		p = c.phone_status or "unset"
		phone[p] = phone.get(p, 0) + 1
		e = c.email_status or "unset"
		email[e] = email.get(e, 0) + 1

	channels = {}
	for label in CHANNELS:
		channels[label] = {
			"reachable": len(doc.contacts_for(label)),
			"reasons": doc.rejection_breakdown(label),
		}

	return {
		"total": len(doc.contacts or []),
		"phone": phone,
		"email": email,
		"channels": channels,
		"last_validated_at": doc.last_validated_at,
	}


@frappe.whitelist()
def search_doctypes(query: str = "") -> list[str]:
	"""Search allow-listed DocTypes for recipient list import.

	Args:
		query: Search string to filter DocType names.

	Returns:
		list[str]: Filtered list of allowed and readable DocType names.

	Raises:
		frappe.PermissionError: If caller lacks read permission on Recipient List.

	Side effects:
		None (read only).
	"""
	frappe.has_permission(DOCTYPE, "read", throw=True)
	from msuite_core.utils.import_guard import importable_doctypes
	return importable_doctypes(query)


@frappe.whitelist()
def get_doctype_fields(doctype: str = "") -> dict:
	"""Inspect a DocType to suggest fields (phone, email, name) for column mapping.

	Args:
		doctype: Name of the DocType to inspect.

	Returns:
		dict: {"fields": list[str], "defaults": dict[str, str]}

	Raises:
		frappe.PermissionError: If caller lacks read permission on Recipient List or target DocType.
		frappe.ValidationError: If DocType is not importable.

	Side effects:
		None (read only).
	"""
	frappe.has_permission(DOCTYPE, "read", throw=True)
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
	phone_field = next(
		(f for f in fields if any(k in f.lower() for k in ["mobile", "phone", "cell", "contact_no"])),
		"",
	)
	email_field = next((f for f in fields if "email" in f.lower()), "")
	name_field = next(
		(
			f
			for f in fields
			if any(k in f.lower() for k in ["full_name", "lead_name", "customer_name", "first_name", "contact_name", "title"])
		),
		"",
	)

	return {
		"fields": fields,
		"defaults": {
			"phone_field": phone_field or ("mobile_no" if "mobile_no" in fields else ""),
			"email_field": email_field or ("email_id" if "email_id" in fields else ""),
			"name_field": name_field or ("name" if "name" in fields else ""),
		},
	}


@frappe.whitelist()
def get_reachable_contacts(recipient_list: str, channel: str = "WhatsApp") -> list[dict]:
	"""Return reachable contacts for a channel formatted as address and recipient_name.

	Excludes invalid addresses and opted-out contacts, and deduplicates addresses.

	Args:
		recipient_list: Name of the Recipient List document.
		channel: Channel name ("WhatsApp", "Email", or "SMS"). Defaults to "WhatsApp".

	Returns:
		list[dict]: List of recipient dicts with 'address' and 'recipient_name'.

	Raises:
		frappe.PermissionError: If caller lacks read permission on Recipient List.
		frappe.DoesNotExistError: If recipient list does not exist.
		frappe.ValidationError: If channel is not one of CHANNELS.

	Side effects:
		None (read only).
	"""
	if not recipient_list or not frappe.db.exists(DOCTYPE, recipient_list):
		frappe.throw(_("Recipient List {0} not found.").format(recipient_list), frappe.DoesNotExistError)

	frappe.has_permission(DOCTYPE, "read", recipient_list, throw=True)

	if channel not in CHANNELS:
		frappe.throw(
			_("Invalid channel: {0}. Must be one of {1}.").format(channel, ", ".join(CHANNELS)),
			frappe.ValidationError,
		)

	doc = frappe.get_doc(DOCTYPE, recipient_list)
	contacts = doc.contacts_for(channel)

	is_email = channel == "Email"
	return [
		{
			"address": c.get("email") if is_email else c.get("phone_e164"),
			"recipient_name": c.get("full_name") or "",
		}
		for c in contacts
	]

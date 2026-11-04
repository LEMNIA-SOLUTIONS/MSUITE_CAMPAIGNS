# Copyright (c) 2026, MSuite and contributors
# For license information, please see license.txt

"""
File import for Audience.

Uses the plain file readers in `msuite_core.utils.files` rather than WhatsApp's
`parse_recipient_file`. That function returns a
phone-shaped result (`mobile_column`, per-row `clean_mobile` verdicts) that
`WhatsApp Recipient List` depends on — changing its signature or return shape
to also carry email would be a behaviour change on an existing path, which the
zero-disruption directive forbids.
"""

from __future__ import annotations

import frappe
from frappe import _

from msuite_core.utils.files import detect_column, parse_csv, parse_excel, resolve_file_path

_PHONE_HINTS = (
	"mobile_no",
	"mobile_number",
	"mobile",
	"phone_no",
	"phone_number",
	"phone",
	"whatsapp_number",
	"whatsapp",
	"contact_no",
	"contact_number",
	"contact",
	"work_mobile_no",
	"work_mobile",
	"msisdn",
	"cell",
)

_EMAIL_HINTS = (
	"email",
	"email_id",
	"email_address",
	"work_email",
	"e_mail",
	"mail",
	"emailid",
)

_NAME_HINTS = (
	"full_name",
	"lead_name",
	"customer_name",
	"contact_name",
	"recipient_name",
	"display_name",
	"name",
	"first_name",
)



def _read(file_url: str):
	path = resolve_file_path(file_url)
	ext = path.rsplit(".", 1)[-1].lower()
	if ext == "csv":
		rows, columns = parse_csv(path)
	elif ext in ("xlsx", "xls"):
		rows, columns = parse_excel(path)
	else:
		frappe.throw(_("Unsupported file type: {0}. Use CSV or Excel (.xlsx/.xls).").format(ext))

	if not columns:
		frappe.throw(_("File has no columns."))
	if not rows:
		frappe.throw(_("File has no data rows."))
	return rows, columns


def preview(file_url: str, sample: int = 10) -> dict:
	"""Detected columns plus a few sample rows, so the import UI can show a
	mapping step instead of guessing silently."""
	rows, columns = _read(file_url)

	phone_col = detect_column(columns, _PHONE_HINTS)
	email_col = detect_column(columns, _EMAIL_HINTS)
	name_col = detect_column(columns, _NAME_HINTS)

	if not phone_col and not email_col:
		frappe.throw(
			_("Could not find a phone or email column. Expected a header like 'mobile' or 'email'.")
		)

	used = {phone_col, email_col, name_col}
	return {
		"columns": columns,
		"phone_column": phone_col or "",
		"email_column": email_col or "",
		"name_column": name_col or "",
		"data_columns": [c for c in columns if c not in used],
		"total_rows": len(rows),
		"sample": rows[:sample],
	}


def rows_from_file(
	file_url: str,
	phone_column: str = "",
	email_column: str = "",
	name_column: str = "",
	data_columns: list[str] | None = None,
) -> list[dict]:
	"""Parse into the shape `CampaignAudience.add_members` expects.

	Columns may be passed explicitly (the UI's mapping step) or left blank to
	fall back on detection.
	"""
	rows, columns = _read(file_url)

	phone_column = phone_column or detect_column(columns, _PHONE_HINTS) or ""
	email_column = email_column or detect_column(columns, _EMAIL_HINTS) or ""
	name_column = name_column or detect_column(columns, _NAME_HINTS) or ""

	if not phone_column and not email_column:
		frappe.throw(_("Map at least one of phone or email before importing."))

	if data_columns is None:
		used = {phone_column, email_column, name_column}
		data_columns = [c for c in columns if c and c not in used]

	out = []
	for row in rows:
		out.append(
			{
				"full_name": row.get(name_column, "") if name_column else "",
				"raw_phone": row.get(phone_column, "") if phone_column else "",
				"raw_email": row.get(email_column, "") if email_column else "",
				"recipient_data": {c: row.get(c, "") for c in data_columns if c in row},
			}
		)
	return out

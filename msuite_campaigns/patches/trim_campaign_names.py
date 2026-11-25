"""Trim leading/trailing whitespace from stored Marketing Campaign names.

`campaign_name_exists` compares the bare indexed column, so legacy rows saved
before trimming was enforced must be trimmed. A row whose trimmed name would
collide with another campaign is skipped and logged: nothing is merged or renamed.
"""

import frappe

ERROR_TITLE = "trim_campaign_names: skipped colliding campaign"


def execute() -> None:
	"""Trim `campaign_name` on rows that have surrounding whitespace (idempotent).

	Returns:
		None.

	Side effects:
		Updates `campaign_name` via direct SQL (no modified bump, no hooks);
		logs each skipped collision with `frappe.log_error`.
	"""
	rows = frappe.db.sql(
		"SELECT name, campaign_name FROM `tabMarketing Campaign` WHERE LENGTH(campaign_name) != LENGTH(TRIM(campaign_name))",
		as_dict=True,
	)
	for row in rows:
		trimmed = row.campaign_name.strip()
		clash = frappe.db.sql(
			"SELECT name FROM `tabMarketing Campaign` WHERE campaign_name = %s AND name != %s LIMIT 1",
			(trimmed, row.name),
		)
		if clash:
			frappe.log_error(
				message=f"{row.name} ({row.campaign_name!r}) collides with {clash[0][0]}; left untouched.",
				title=ERROR_TITLE,
			)
			continue
		frappe.db.set_value("Marketing Campaign", row.name, "campaign_name", trimmed, update_modified=False)

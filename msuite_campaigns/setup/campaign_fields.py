"""Single owner of Marketing Campaign custom fields.

Declares and upserts custom fields on Marketing Campaign using create_custom_fields,
following the custom_crm/setup/crm_lead_fields.py pattern.
"""

from __future__ import annotations

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

MARKETING_CAMPAIGN_CUSTOM_FIELDS = [
	{
		"fieldname": "lead_goal",
		"label": "Lead Goal",
		"fieldtype": "Int",
		"insert_after": "spent",
		"description": "Target number of leads for this campaign.",
	},
]


def sync_marketing_campaign_custom_fields() -> None:
	"""Upsert declared custom fields onto Marketing Campaign if the DocType exists.

	Args:
		None.

	Returns:
		None.

	Raises:
		None. Catches and logs errors to prevent migration abortion.
	"""
	if not frappe.db.exists("DocType", "Marketing Campaign"):
		return

	payload = {"Marketing Campaign": MARKETING_CAMPAIGN_CUSTOM_FIELDS}
	try:
		create_custom_fields(payload, ignore_validate=True)
	except Exception as e:
		frappe.log_error(
			f"Failed to sync Marketing Campaign custom fields: {e}",
			"Marketing Campaign Setup",
		)

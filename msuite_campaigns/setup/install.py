"""Hooks called by hooks.py after_migrate.

Deliberately code-defined (no fixtures/*.json) — follow the pattern established
in apps/custom_crm/custom_crm/setup/install.py.
"""

from __future__ import annotations

import frappe

from msuite_campaigns.setup.campaign_fields import sync_marketing_campaign_custom_fields


def after_migrate() -> None:
	"""Post-migration hook to sync code-declared custom fields.

	Args:
		None.

	Returns:
		None.
	"""
	sync_marketing_campaign_custom_fields()
	frappe.db.commit()

# Copyright (c) 2026, MSuite and contributors
# For license information, please see license.txt

"""Registered patch: remove the legacy bulk message, recipient list and audience doctypes.

Approved by the owner on 2026-11-21. It runs the staged
``delete_legacy_bulk_doctypes.execute`` with ``confirm_delete=1``. The staged
function still verifies the migration first and raises when any legacy row has
no Broadcast or Recipient List twin, so a site with unmigrated data fails the
migrate instead of losing data. It does nothing when the doctypes are already gone.
"""

from msuite_campaigns.patches import delete_legacy_bulk_doctypes


def execute() -> None:
	"""Delete the legacy doctypes once the migration check passes.

	Raises:
		frappe.ValidationError: When the migration check finds mismatches.

	Side effects:
		Repoints references, empties tables, deletes 11 DocTypes and commits.
	"""
	delete_legacy_bulk_doctypes.execute(confirm_delete=1, dry_run=False)

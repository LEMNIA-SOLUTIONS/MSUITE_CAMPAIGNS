# Copyright (c) 2026, MSuite and contributors
# For license information, please see license.txt

"""Broadcast Recipient controller."""

from __future__ import annotations

import frappe
from frappe.model.document import Document


class BroadcastRecipient(Document):
	"""Broadcast Recipient document managing delivery state for individual recipients."""

	pass


def on_doctype_update() -> None:
	"""Create composite unique constraint and composite index on tabBroadcast Recipient.

	Creates UNIQUE constraint on (broadcast, channel, address) and composite index
	on (broadcast, channel, status) using idempotent database utilities.

	Raises:
		None.

	Side effects:
		Modifies MariaDB schema for tabBroadcast Recipient (commits transaction).
	"""
	frappe.db.add_unique("Broadcast Recipient", ["broadcast", "channel", "address"])
	frappe.db.add_index("Broadcast Recipient", ["broadcast", "channel", "status"])

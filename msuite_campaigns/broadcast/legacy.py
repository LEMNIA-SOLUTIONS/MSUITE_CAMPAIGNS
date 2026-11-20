# Copyright (c) 2026, MSuite and contributors
# For license information, please see license.txt

"""Legacy reference resolution and repointing utilities (Task 14).

Provides helpers to resolve legacy DocType records to their unified counterparts
(Broadcast or Recipient List) and to repoint external references (Channel Message,
Marketing Campaign, MSuite Email Unsubscribe) to the new unified models.
"""

from __future__ import annotations

import frappe
from frappe import _

from msuite_campaigns.broadcast.constants import LEGACY_BROADCAST_DOCTYPES


def resolve(legacy_doctype: str, legacy_name: str) -> str | None:
	"""Resolve a legacy document name to its unified counterpart name.

	Looks up the unified Broadcast name for legacy bulk message doctypes,
	or the Recipient List name for legacy list/audience doctypes.

	Args:
		legacy_doctype: The legacy doctype (e.g. 'Bulk WhatsApp Message',
			'Bulk Email Message', 'Bulk SMS Message', 'Audience',
			'WhatsApp Recipient List', 'Email Recipient List').
		legacy_name: Name of the legacy document.

	Returns:
		str | None: Name of the corresponding unified Broadcast or Recipient List,
			or None if not found or if the doctype is unsupported.

	Raises:
		None: All lookups are handled safely without raising exceptions.

	Side effects:
		None (read-only database queries).
	"""
	if not legacy_doctype or not legacy_name:
		return None

	# Bulk message resolution -> Broadcast
	if legacy_doctype in LEGACY_BROADCAST_DOCTYPES:
		return frappe.db.get_value(
			"Broadcast",
			{"legacy_doctype": legacy_doctype, "legacy_name": legacy_name},
			"name",
		)

	# Audience resolution -> Recipient List (exact match or list_name match)
	if legacy_doctype == "Audience":
		if frappe.db.exists("Recipient List", legacy_name):
			return legacy_name
		found = frappe.db.get_value("Recipient List", {"list_name": legacy_name}, "name")
		return found if found else None

	# WhatsApp Recipient List resolution -> Recipient List
	if legacy_doctype == "WhatsApp Recipient List":
		if frappe.db.exists("Recipient List", legacy_name):
			return legacy_name
		collided = f"{legacy_name} (WhatsApp)"
		if frappe.db.exists("Recipient List", collided):
			return collided
		candidates = frappe.db.get_all(
			"Recipient List",
			filters={"list_name": ["like", f"{legacy_name} (WhatsApp)%"]},
			pluck="name",
		)
		if candidates:
			return candidates[0]
		return None

	# Email Recipient List resolution -> Recipient List
	if legacy_doctype == "Email Recipient List":
		if frappe.db.exists("Recipient List", legacy_name):
			return legacy_name
		collided = f"{legacy_name} (Email)"
		if frappe.db.exists("Recipient List", collided):
			return collided
		candidates = frappe.db.get_all(
			"Recipient List",
			filters={"list_name": ["like", f"{legacy_name} (Email)%"]},
			pluck="name",
		)
		if candidates:
			return candidates[0]
		return None

	return None


def repoint_legacy_references(dry_run: bool = True) -> dict:
	"""Repoint external legacy references to the unified models.

	Updates:
	1. Channel Message: reference_doctype & reference_name repointed from
	   legacy bulk message doctypes to 'Broadcast'.
	2. Marketing Campaign: ensures the campaign relation is populated on
	   the corresponding Broadcast rows.
	3. MSuite Email Unsubscribe: populates source_broadcast link from source_campaign.

	Args:
		dry_run: When True (default), does NOT modify any database records and
			only returns the counts of records that would be updated.
			When False, commits the repointed changes.

	Returns:
		dict: Summary of checked and repointed records:
			{
				"channel_messages": {"checked": int, "repointed": int},
				"marketing_campaigns": {"checked": int, "repointed": int},
				"email_unsubscribes": {"checked": int, "repointed": int},
				"dry_run": bool,
			}

	Raises:
		None: Errors are logged or handled safely.

	Side effects:
		When dry_run=False: updates tabChannel Message, tabBroadcast,
		and tabMSuite Email Unsubscribe; commits changes.
		When dry_run=True: no side effects.
	"""
	summary = {
		"channel_messages": {"checked": 0, "repointed": 0},
		"marketing_campaigns": {"checked": 0, "repointed": 0},
		"email_unsubscribes": {"checked": 0, "repointed": 0},
		"dry_run": dry_run,
	}

	# 1. Repoint Channel Message
	cms = frappe.db.get_all(
		"Channel Message",
		filters={"reference_doctype": ["in", list(LEGACY_BROADCAST_DOCTYPES)]},
		fields=["name", "reference_doctype", "reference_name"],
	)
	summary["channel_messages"]["checked"] = len(cms)
	for cm in cms:
		new_bc = resolve(cm.reference_doctype, cm.reference_name)
		if new_bc:
			summary["channel_messages"]["repointed"] += 1
			if not dry_run:
				frappe.db.set_value(
					"Channel Message",
					cm.name,
					{"reference_doctype": "Broadcast", "reference_name": new_bc},
					update_modified=False,
				)

	# 2. Marketing Campaign blast links -> Broadcast.campaign
	campaigns = frappe.db.get_all(
		"Marketing Campaign",
		fields=["name", "whatsapp_blast", "email_blast", "sms_blast"],
	)
	for camp in campaigns:
		blast_pairs = [
			(camp.get("whatsapp_blast"), "Bulk WhatsApp Message"),
			(camp.get("email_blast"), "Bulk Email Message"),
			(camp.get("sms_blast"), "Bulk SMS Message"),
		]
		for blast_name, blast_doctype in blast_pairs:
			if blast_name:
				summary["marketing_campaigns"]["checked"] += 1
				bc_name = resolve(blast_doctype, blast_name)
				if bc_name:
					current_campaign = frappe.db.get_value("Broadcast", bc_name, "campaign")
					if current_campaign != camp.name:
						summary["marketing_campaigns"]["repointed"] += 1
						if not dry_run:
							frappe.db.set_value(
								"Broadcast",
								bc_name,
								"campaign",
								camp.name,
								update_modified=False,
							)

	# 3. MSuite Email Unsubscribe.source_broadcast
	unsubs = frappe.db.get_all(
		"MSuite Email Unsubscribe",
		filters={"source_campaign": ["is", "set"]},
		fields=["name", "source_campaign", "source_broadcast"],
	)
	summary["email_unsubscribes"]["checked"] = len(unsubs)
	for u in unsubs:
		bc_name = resolve("Bulk Email Message", u.source_campaign)
		if bc_name and u.source_broadcast != bc_name:
			summary["email_unsubscribes"]["repointed"] += 1
			if not dry_run:
				frappe.db.set_value(
					"MSuite Email Unsubscribe",
					u.name,
					"source_broadcast",
					bc_name,
					update_modified=False,
				)

	if not dry_run:
		frappe.db.commit()

	return summary

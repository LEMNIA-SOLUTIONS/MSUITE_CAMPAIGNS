"""
Campaign attribution service.

`Conversation.campaign` (inbox/doctype/conversation/conversation.json)
and `CRM Lead.marketing_campaign` (crm_lead_core.setup.crm_lead_fields, a Custom Field
since CRM Lead lives in the separate `crm` app) were added in Phase 2
/ Attribution Foundation. Both are nullable — most Conversations/Leads
have no campaign and that's expected (organic inbound, agent-initiated,
no resolvable trigger), not a data gap.

Every function below still checks `has_field` before querying, even
though both fields now exist on every install that's run
`bench migrate` since Phase 2 — kept defensive on purpose (same pattern
`campaign/api/v1/composer.py` uses for the WhatsApp schedule-field
drift) so a site that hasn't migrated yet gets a shape-stable empty
result instead of a 500, and so no call site needs to change again if
either field is ever renamed or dropped.
"""

from __future__ import annotations

import frappe


def get_conversations_for_campaign(campaign: str) -> list[dict]:
	"""Conversations attributed to `campaign` via `Conversation.campaign`
	— set at creation time from the entity that triggered the first
	outbound message (see `inbox/services/outbound.py
	:_resolve_campaign_from_reference`). Conversations with no resolvable
	trigger (organic inbound, agent-initiated) are not included — that's
	correct, not a gap.
	"""
	if not frappe.db.exists("DocType", "Conversation"):
		return []
	if not frappe.get_meta("Conversation").has_field("campaign"):
		return []
	return frappe.get_all(
		"Conversation",
		filters={"campaign": campaign},
		fields=["name", "status", "source", "modified"],
		order_by="modified desc",
		limit=50,
	)


def get_leads_for_campaign(campaign: str) -> list[dict]:
	"""CRM Leads attributed to `campaign` via `CRM Lead.marketing_campaign`
	— set at lead-creation time, either flattened from `Ad Campaign
	.campaign` (Meta Lead Gen Form ingestion, see `ads/domain/leads
	/ingest.py`) or inherited from the triggering Conversation's
	`campaign` (Inbox auto-lead-creation, see `inbox/services
	/auto_lead_creator.py:_resolve_campaign_for_handle`).
	"""
	if not frappe.db.exists("DocType", "CRM Lead"):
		return []
	if not frappe.get_meta("CRM Lead").has_field("marketing_campaign"):
		return []
	return frappe.get_all(
		"CRM Lead",
		filters={"marketing_campaign": campaign},
		fields=["name", "status", "lead_name", "modified"],
		order_by="modified desc",
		limit=50,
	)


def get_attribution_summary(campaign: str) -> dict:
	"""Counts for the future Campaign dashboard attribution card.
	Shape-stable regardless of whether the underlying fields exist yet.
	"""
	conversations = get_conversations_for_campaign(campaign)
	leads = get_leads_for_campaign(campaign)
	return {
		"conversations": len(conversations),
		"leads": len(leads),
		"ready": bool(
			frappe.get_meta("Conversation").has_field("campaign")
			if frappe.db.exists("DocType", "Conversation")
			else False
		)
		and bool(
			frappe.get_meta("CRM Lead").has_field("marketing_campaign")
			if frappe.db.exists("DocType", "CRM Lead")
			else False
		),
	}

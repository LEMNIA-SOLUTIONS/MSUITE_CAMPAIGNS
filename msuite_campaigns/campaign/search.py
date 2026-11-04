"""Marketing Campaigns in the global search."""

import frappe


def search(q: str, limit: int) -> dict | None:
	campaigns = frappe.db.get_list(
		"Marketing Campaign",
		filters=[["campaign_name", "like", f"%{q}%"]],
		fields=["name", "campaign_name", "status", "objective"],
		limit=limit,
	)
	if not campaigns:
		campaigns = frappe.db.get_list(
			"Marketing Campaign",
			filters=[["name", "like", f"%{q}%"]],
			fields=["name", "campaign_name", "status", "objective"],
			limit=limit,
		)
	if campaigns:
		return (
			{
				"category": "Campaigns",
				"icon": "speakerphone",
				"items": [
					{
						"name": r.name,
						"title": r.campaign_name or r.name,
						"subtitle": r.objective or r.status or "Campaign",
						"route": f"/campaign?name={r.name}",
					}
					for r in campaigns
				],
			}
		)

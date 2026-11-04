"""Campaigns and audiences in the Home page's recent list."""

from __future__ import annotations

import frappe


def recent_campaigns() -> list[dict]:
	recent_items = []
	for dt in ("Marketing Campaign", "Campaign"):
		if frappe.db.exists("DocType", dt) and frappe.has_permission(dt, "read"):
			c_name_field = "campaign_name" if frappe.db.has_column(dt, "campaign_name") else "name"
			status_field = "status" if frappe.db.has_column(dt, "status") else None
			fields = ["name", f"{c_name_field} as title", "modified"]
			if status_field:
				fields.append(status_field)

			campaigns = frappe.get_list(dt, fields=fields, order_by="modified desc", limit=3)
			for c in campaigns:
				status_val = c.get("status") or "Active"
				recent_items.append(
					{
						"id": c.name,
						"type": "Campaign",
						"title": c.get("title") or c.name,
						"status": status_val,
						"route_name": "Campaigns",
						"modified": str(c.modified),
					}
				)
			break
	return recent_items


def recent_audiences() -> list[dict]:
	recent_items = []
	for dt in ("Audience Segment", "Recipient List"):
		if frappe.db.exists("DocType", dt) and frappe.has_permission(dt, "read"):
			title_field = "segment_name" if frappe.db.has_column(dt, "segment_name") else ("list_name" if frappe.db.has_column(dt, "list_name") else "name")
			segments = frappe.get_list(
				dt,
				fields=["name", f"{title_field} as title", "modified"],
				order_by="modified desc",
				limit=3,
			)
			for s in segments:
				recent_items.append(
					{
						"id": s.name,
						"type": "Segment (List)",
						"title": s.get("title") or s.name,
						"status": "Active",
						"route_name": "AudienceList",
						"modified": str(s.modified),
					}
				)
			break
	return recent_items

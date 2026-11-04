"""What Campaigns shows on the Home page."""

from __future__ import annotations

import frappe
from frappe.utils import flt

from msuite_core.utils.formatting import _compact


def running_campaigns(symbol) -> list[dict]:
	rows = frappe.get_all(
		"Marketing Campaign", filters={"status": ["in", ["Active", "In Progress", "Scheduled"]]},
		fields=["name", "campaign_name", "status", "budget", "spent", "start_date"], order_by="modified desc", limit=3,
	)
	res = []
	for r in rows:
		budget, spent = flt(r.budget), flt(r.spent)
		pct = min(round(spent / budget * 100), 100) if budget else 0
		live = r.status != "Scheduled"
		res.append({
			"name": r.campaign_name or r.name, "status": "Live" if live else "Scheduled",
			"sub": str(r.start_date or ""), "pct": pct,
			"pace": f"{_compact(spent, symbol)} / {_compact(budget, symbol)} · {pct}%" if budget else "No budget set",
			"leads": "—", "cpl": "",
		})
	return res


def running_campaign_count() -> int:
	return frappe.db.count("Marketing Campaign", {"status": ["in", ["Active", "In Progress", "Scheduled"]]})

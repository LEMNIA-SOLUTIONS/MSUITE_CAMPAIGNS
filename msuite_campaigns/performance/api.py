"""
Performance — whitelisted endpoints for the dashboard SPA.

One endpoint today; more land here as Phases B–F bring funnels +
breakdowns + manual refresh online. Keeping a flat `api.py` keeps the
dotted RPC path short — `msuite_campaigns.performance.api.X`.
"""

from __future__ import annotations

import frappe

from msuite_campaigns.performance.metric_catalog import (
	TOP_STRIP_ORDER,
	as_payload,
)


@frappe.whitelist()
def get_metric_catalog() -> dict:
	# perm: static metric catalog metadata for dashboard
	"""Catalog snapshot for the dashboard's first paint.

	The SPA calls this once on Performance tab mount and caches the
	result for the session. Includes the tenant's label overrides so
	the frontend doesn't need a second xcall to merge two responses.

	Shape:
	  {
	    "metrics": [
	      {key, label, tooltip, format, aggregate, applies_to: [...]}
	      ...
	    ],
	    "top_strip_order": ["reach", "engagement", "spend", "roas"]
	  }
	"""
	return {
		"metrics": as_payload(),
		"top_strip_order": TOP_STRIP_ORDER,
	}

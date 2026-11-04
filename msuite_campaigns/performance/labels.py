"""
Tenant-level label overlay for the Metric Catalog.

Lets a tenant rename "Reach" → "Audience touched" (or any other
metric label) without touching the catalog's math. The underlying
source mapping stays fixed — only the display string changes — so
every report, alert, and integration continues to mean the same
thing while the marketer reads vocabulary that fits their business.

How it works:
  • A single-doctype "Performance Labels" stores the overrides.
    Field shape: `{metric_key}_label` for each catalog entry the
    tenant wants to rename. Empty/missing field → fall back to the
    catalog default.
  • `resolve_label(key, default)` is the only public reader. Every
    backend rollup + the SPA's catalog payload route through it.
  • Reads are cached for the duration of the request via
    `frappe.local` so a dashboard render doesn't hit the DB once
    per metric.

Why a single-doctype (not a per-row table):
  • Editing labels is rare — the overlay file is tiny (~8 string
    fields).
  • Single-doctype gives us a clean Settings page in Desk where the
    marketer can rename in one screen.
  • One DB row → one cache key → zero per-metric lookups at runtime.

Custom KPI definitions (Layer 3 — pick numerator/denominator to
build a custom metric) deliberately live elsewhere. This module
handles relabel-only.
"""

from __future__ import annotations

import frappe

SETTINGS_DOCTYPE = "Performance Labels"


def resolve_label(key: str, default: str) -> str:
	"""Return the tenant's label for `key`, or `default` when unset.

	Uses a request-scoped cache so a dashboard render with 8 metrics
	issues at most one read across the whole request.
	"""
	overlay = _load_overlay()
	return overlay.get(key) or default


def all_labels() -> dict[str, str]:
	"""All catalog keys → their resolved label. Useful for the SPA
	bootstrap when it wants the full map up-front.
	"""
	from msuite_campaigns.performance.metric_catalog import METRICS

	return {k: resolve_label(k, m.label) for k, m in METRICS.items()}


# ─────────────────────────────────────────────────────────────────────
# Internal — request-scoped cache + DB read
# ─────────────────────────────────────────────────────────────────────


def _load_overlay() -> dict[str, str]:
	"""Read the single-doctype settings row and return `{metric_key:
	label_override}`. Missing settings doctype or row = empty dict
	(no overrides → every label falls back to the catalog default).
	"""
	# Request-scoped cache — `frappe.local` is reset per HTTP request,
	# so the cache is naturally bounded.
	cached = getattr(frappe.local, "_performance_labels", None)
	if cached is not None:
		return cached

	overlay: dict[str, str] = {}
	try:
		# `Performance Labels` is a Single doctype — its rows live in
		# `tabSingles`, not a dedicated table. `frappe.get_cached_doc`
		# handles both single + non-single uniformly; the try/except
		# below catches the missing-doctype case (fresh install, doc
		# not yet migrated, etc.).
		settings = frappe.get_cached_doc(SETTINGS_DOCTYPE, SETTINGS_DOCTYPE)
		from msuite_campaigns.performance.metric_catalog import METRIC_KEYS

		for key in METRIC_KEYS:
			value = (getattr(settings, f"{key}_label", "") or "").strip()
			if value:
				overlay[key] = value
	except Exception:
		# Settings doc might not exist yet on a brand-new install.
		# Soft-fail to defaults — labels are cosmetic, never block
		# the dashboard render on a config issue.
		overlay = {}

	frappe.local._performance_labels = overlay
	return overlay


@frappe.whitelist()
def get_label_overrides() -> dict[str, str]:
	"""Whitelisted read for the Desk settings page + a future
	in-app Settings → Performance Labels card. Returns ONLY the
	overrides (not the resolved labels) so the form can show
	placeholders for unset fields.
	"""
	frappe.has_permission(SETTINGS_DOCTYPE, "read", throw=True)
	return _load_overlay()

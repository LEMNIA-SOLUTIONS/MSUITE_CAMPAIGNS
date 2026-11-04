"""
Metric Catalog — the single source of truth for what a marketing metric
means across Ads, Social, and WhatsApp.

The Performance Dashboard (campaign-level + per-entity) reads this
catalog so:
  • The label "Reach" maps to the same human meaning everywhere
    (unique people we put the campaign in front of).
  • The MATH behind a label is fixed — each metric declares which
    per-product field/doctype it sums or computes from. This stops
    two engineers from labelling the same number differently in two
    files, or two tenants from drifting on what "Engagement" means.
  • The TOOLTIP text is centralised so the SPA never hard-codes a
    description that goes stale when a metric definition changes.
  • The DISPLAY label can be overridden per-tenant via
    `performance/labels.py` — same data, different word ("Reach" →
    "Audience touched"). That overlay layer keeps math stable while
    letting marketers speak their own vocabulary.

Catalog scope is lean by design — eight v1 metrics that cover ~95%
of digital-marketer daily use:

    reach · impressions · engagement · clicks ·
    conversions · spend · ctr · roas

Add more metrics here only when a real customer asks. Every entry
becomes a tooltip + a formula + a contract — the cost compounds.

Source mapping notes:
  • `ads` rows come from `Ad Insight` (already normalised + indexed).
  • `social` rows come from `Social Insight` (indexed metric rows
    written by `social_post/insights/sync.py`).
  • `whatsapp` rows come from `Channel Message` (status counts) and
    `WhatsApp Analytics Snapshot` (Meta-side pricing / conversation
    breakdowns).
  • `None` for a source means the metric doesn't exist for that
    channel (e.g. organic Social has no Spend). The dashboard renders
    "—" rather than 0 in that cell so marketers don't infer "we spent
    zero" when the right answer is "this channel doesn't have a cost".

`aggregate` declares how cross-module rollups combine per-channel
values into one number. Values:
  • "sum"               — straight integer sum (Impressions, Clicks,
                          Engagement, Spend, Conversions).
  • "sum_with_caveat"   — sum but flag "may overlap" — used for Reach,
                          where the same human could see both an ad
                          and a WhatsApp. The tooltip surfaces the
                          per-channel split so the marketer trusts
                          the headline.
  • "weighted_average"  — for ratios (CTR, ROAS) — recompute from the
                          underlying numerators/denominators across
                          channels, not the sum of per-channel ratios.
                          The dashboard's cross-module summary uses
                          this; a plain `0.5 + 0.5 = 1.0` for two CTRs
                          would be nonsense.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional

# ─────────────────────────────────────────────────────────────────────
# Types
# ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Source:
	"""How one channel produces a metric.

	`doctype` + `field` describe the row Source the SUM runs over.
	For derived metrics (CTR, ROAS) we leave both empty and rely on
	`derived_from` to declare the numerator / denominator pair —
	`None` (not `()`) so type checkers + readers can distinguish
	"no derivation declared" from "empty 2-tuple supplied".
	"""

	doctype: str = ""
	field: str = ""
	derived_from: tuple[str, str] | None = None


@dataclass(frozen=True)
class Metric:
	"""One catalog entry.

	`key`        — stable id used in API responses + frontend code.
	`label`      — default human label (overridable per tenant).
	`tooltip`    — explainer text; surfaces in dashboard hover.
	`format`     — hint for the SPA renderer: int / money / pct / ratio.
	`aggregate`  — cross-channel combinator (see module docstring).
	`sources`    — per-channel mapping. None = "not applicable here".
	"""

	key: str
	label: str
	tooltip: str
	format: Literal["int", "money", "pct", "ratio"]
	aggregate: Literal["sum", "sum_with_caveat", "weighted_average"]
	sources: dict[str, Source | None] = field(default_factory=dict)


# ─────────────────────────────────────────────────────────────────────
# The catalog — eight v1 metrics
# ─────────────────────────────────────────────────────────────────────


METRICS: dict[str, Metric] = {
	# Top-of-funnel — how many unique humans we touched.
	"reach": Metric(
		key="reach",
		label="Reach",
		tooltip=(
			"Unique people who saw this campaign at least once. "
			"Sum across channels — same person may appear on more "
			"than one channel; treat as a directional ceiling."
		),
		format="int",
		aggregate="sum_with_caveat",
		sources={
			"ads": Source(doctype="Ad Insight", field="reach"),
			# Social's normalised Insight table (Phase B) flattens
			# `post_impressions_unique` from Meta into this column.
			"social": Source(doctype="Social Insight", field="reach"),
			# WhatsApp doesn't have a unique-recipient column on
			# Channel Message because every send IS to a unique phone
			# — `delivered` count == people reached.
			"whatsapp": Source(
				doctype="Channel Message",
				field="delivered_count",  # virtual: COUNT(status IN delivered/read)
			),
		},
	),
	# Volume — total views including repeats. Comparison vs Reach
	# yields Frequency (impressions / reach) which marketers eyeball
	# for ad fatigue.
	"impressions": Metric(
		key="impressions",
		label="Impressions",
		tooltip="Total times the content was shown, including repeats.",
		format="int",
		aggregate="sum",
		sources={
			"ads": Source(doctype="Ad Insight", field="impressions"),
			"social": Source(doctype="Social Insight", field="impressions"),
			# WhatsApp = one send = one "view". Same count as `sent`,
			# surfaced under Impressions for cross-channel comparability.
			"whatsapp": Source(
				doctype="Channel Message",
				field="sent_count",
			),
		},
	),
	# Engagement — did anyone DO anything?
	"engagement": Metric(
		key="engagement",
		label="Engagement",
		tooltip=("Meaningful actions on the content — clicks, likes, comments, shares, replies."),
		format="int",
		aggregate="sum",
		sources={
			# Ads: Meta's `actions: post_engagement` sums clicks +
			# reactions + comments + shares. We map to `clicks` for
			# v1 because that's what Ad Insight stores today; a
			# future Ad Insight column for engagement actions would
			# land here without changing the catalog signature.
			"ads": Source(doctype="Ad Insight", field="clicks"),
			"social": Source(doctype="Social Insight", field="engagement"),
			# Inbound replies are the "engagement" equivalent for WA.
			"whatsapp": Source(
				doctype="Channel Message",
				field="replied_count",
			),
		},
	),
	# Clicks specifically — the link-tap subset of engagement.
	# Separate from `engagement` because marketers benchmark CTR
	# against impressions, and CTR only counts link clicks.
	"clicks": Metric(
		key="clicks",
		label="Clicks",
		tooltip="People who tapped through to the destination.",
		format="int",
		aggregate="sum",
		sources={
			"ads": Source(doctype="Ad Insight", field="clicks"),
			"social": Source(doctype="Social Insight", field="clicks"),
			"whatsapp": None,
		},
	),
	# Bottom-of-funnel — Pixel/CAPI events for Ads; n/a for organic.
	"conversions": Metric(
		key="conversions",
		label="Conversions",
		tooltip=(
			"Tracked completions (purchases, sign-ups, leads). "
			"Requires the Meta Pixel or CAPI to be configured on "
			"the landing page."
		),
		format="int",
		aggregate="sum",
		sources={
			"ads": Source(doctype="Ad Insight", field="conversions"),
			"social": None,
			"whatsapp": None,
		},
	),
	# Money out.
	"spend": Metric(
		key="spend",
		label="Spend",
		tooltip="Money spent on this campaign in the period.",
		format="money",
		aggregate="sum",
		sources={
			"ads": Source(doctype="Ad Insight", field="spend"),
			"social": None,  # organic posts have no cost
			# WhatsApp pricing snapshot — derived per conversation
			# category. Real value lives on the Pricing snapshot;
			# the query layer reads it via `whatsapp.analytics.query`.
			"whatsapp": Source(
				doctype="WhatsApp Analytics Snapshot",
				field="conversation_cost",
			),
		},
	),
	# Derived ratio — clicks / impressions.
	"ctr": Metric(
		key="ctr",
		label="CTR",
		tooltip=(
			"Click-through rate — clicks divided by impressions. "
			"Higher means the creative is hooking the right audience."
		),
		format="pct",
		aggregate="weighted_average",
		sources={
			"ads": Source(derived_from=("clicks", "impressions")),
			"social": Source(derived_from=("clicks", "impressions")),
			"whatsapp": None,
		},
	),
	# Derived ratio — conversion_value / spend.
	"roas": Metric(
		key="roas",
		label="ROAS",
		tooltip=(
			"Return on ad spend — revenue divided by spend. 2.5× means ₹2.50 returned for every ₹1 spent."
		),
		format="ratio",
		aggregate="weighted_average",
		sources={
			"ads": Source(derived_from=("conversion_value", "spend")),
			"social": None,
			"whatsapp": None,
		},
	),
}


# Stable ordering for the dashboard's top strip. Hand-picked so the
# "tell the story" flow reads: who saw it → did they act → what did
# it cost → was it worth it.
TOP_STRIP_ORDER: list[str] = [
	"reach",
	"engagement",
	"spend",
	"roas",
]


METRIC_KEYS: list[str] = list(METRICS.keys())


# ─────────────────────────────────────────────────────────────────────
# Public helpers
# ─────────────────────────────────────────────────────────────────────


def get_metric(key: str) -> Metric:
	"""Lookup with a clear error when the catalog drifts from a caller."""
	try:
		return METRICS[key]
	except KeyError:
		raise KeyError(f"Unknown metric '{key}'. Known keys: {', '.join(METRIC_KEYS)}")


def applies_to(metric_key: str, channel: str) -> bool:
	"""True when the metric has a source for the given channel.

	Used by the dashboard to render '—' instead of '0' in cells where
	the metric simply doesn't exist (e.g. Spend on organic Social).
	"""
	return METRICS.get(metric_key, None) is not None and METRICS[metric_key].sources.get(channel) is not None


def as_payload() -> list[dict]:
	"""Catalog serialised for the frontend — one xcall on dashboard
	mount, cached client-side. Includes the per-tenant label overlay
	so the SPA doesn't have to merge two responses.
	"""
	from msuite_campaigns.performance.labels import resolve_label

	out: list[dict] = []
	for key, m in METRICS.items():
		out.append(
			{
				"key": key,
				"label": resolve_label(key, m.label),
				"tooltip": m.tooltip,
				"format": m.format,
				"aggregate": m.aggregate,
				"applies_to": [c for c, src in m.sources.items() if src is not None],
			}
		)
	return out

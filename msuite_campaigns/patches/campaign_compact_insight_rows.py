"""
Backfill deterministic `insight_key` on all Marketing Campaign Insight rows,
deduplicate legacy rows, and add the DB unique constraint.

Mirrors `ads_dedupe_and_key_ad_insight.py` and `whatsapp_compact_analytics_snapshots.py`.

Why:
  1. Multiple LIFETIME rows could exist per campaign due to prior unbounded daily sync.
  2. Legacy DAY and WEEK rows used legacy 3-tuple hash instead of 6-tuple `make_insight_key`.
  3. Deduplicates all granularities (LIFETIME, WEEK, DAY) to ensure at most 1 row per
     (campaign, granularity, date bucket).
  4. Commits every _BATCH = 2000 rows.
  5. Adds DB unique constraint on `insight_key`.
"""

from __future__ import annotations

from typing import Any

import frappe

from msuite_core.constants import (
	GRANULARITIES_WITHOUT_DATE,
	GRANULARITY_DAY,
	INSIGHT_DELETE_BATCH,
)
from msuite_core.utils.insight_keys import delete_in_batches, make_insight_key


def execute() -> None:
	"""Execute cleanup of duplicate campaign insight rows, re-key legacy rows, and add unique index.

	Args:
		None.

	Returns:
		None.

	Raises:
		Exception: If schema reload or database operation fails.

	Side effects:
		Deletes duplicate rows, updates insight_key on retained rows,
		commits database changes in batches, and creates unique DB constraint.
	"""
	frappe.reload_doc("campaign", "doctype", "marketing_campaign_insight")

	# Clean up any existing duplicates that already have insight_key populated
	duplicate_keys = frappe.db.sql(
		"""
		SELECT insight_key
		FROM `tabMarketing Campaign Insight`
		WHERE insight_key IS NOT NULL AND insight_key != ''
		GROUP BY insight_key
		HAVING COUNT(*) > 1
	""",
		as_dict=True,
	)

	for d in duplicate_keys:
		key = d["insight_key"]
		rows_with_key = frappe.get_all(
			"Marketing Campaign Insight",
			filters={"insight_key": key},
			fields=["name", "modified"],
			order_by="modified desc",
		)
		if len(rows_with_key) > 1:
			stale_names = [r.name for r in rows_with_key[1:]]
			delete_in_batches("Marketing Campaign Insight", stale_names, commit=False)

	frappe.db.commit()

	last_name = ""
	keyed = 0
	deleted = 0
	superseded = 0

	while True:
		conditions = []
		params: list[Any] = []
		if last_name:
			conditions.append("name > %s")
			params.append(last_name)

		where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
		query = f"""
			SELECT name, marketing_campaign, granularity, date, insight_key, modified, creation
			FROM `tabMarketing Campaign Insight`
			{where_clause}
			ORDER BY name ASC
			LIMIT %s
		"""
		params.append(INSIGHT_DELETE_BATCH)

		batch = frappe.db.sql(query, tuple(params), as_dict=True)
		if not batch:
			break

		for r in batch:
			granularity = r.granularity or GRANULARITY_DAY
			bucket = "" if granularity in GRANULARITIES_WITHOUT_DATE else (str(r.date) if r.date else "")
			expected_key = make_insight_key("Marketing Campaign", r.marketing_campaign, granularity, bucket)

			existing = frappe.db.get_value(
				"Marketing Campaign Insight",
				{"insight_key": expected_key},
				["name", "modified", "creation"],
				as_dict=True,
			)

			if existing and existing.name != r.name:
				r_ts = (
					str(r.get("modified") or ""),
					str(r.get("creation") or ""),
					str(r.name),
				)
				ex_ts = (
					str(existing.get("modified") or ""),
					str(existing.get("creation") or ""),
					str(existing.name),
				)
				if r_ts > ex_ts:
					delete_in_batches("Marketing Campaign Insight", [existing.name], commit=False)
					frappe.db.set_value(
						"Marketing Campaign Insight",
						r.name,
						"insight_key",
						expected_key,
						update_modified=False,
					)
					deleted += 1
					keyed += 1
				else:
					delete_in_batches("Marketing Campaign Insight", [r.name], commit=False)
					deleted += 1
			else:
				if r.insight_key != expected_key:
					frappe.db.set_value(
						"Marketing Campaign Insight",
						r.name,
						"insight_key",
						expected_key,
						update_modified=False,
					)
					keyed += 1

		frappe.db.commit()
		last_name = batch[-1].name
		if len(batch) < INSIGHT_DELETE_BATCH:
			break

	frappe.db.commit()

	frappe.db.add_unique("Marketing Campaign Insight", ["insight_key"])

	print(
		f"campaign_compact_insight_rows: keyed {keyed} rows, "
		f"removed {deleted} duplicate rows, "
		f"removed {superseded} rows superseded by a fresh row"
	)

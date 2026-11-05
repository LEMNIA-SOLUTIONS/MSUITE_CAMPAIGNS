# Campaign Module

The Campaign module provides multi-channel marketing campaign orchestration and aggregated analytics across Ads, Social Posts, and WhatsApp channels.

## Campaign Insights Compaction Model (`Marketing Campaign Insight`)

To prevent unbounded table growth from continuous cron execution, campaign insights use a compacted multi-tier storage model:

### Granularity Row Model

1. **`LIFETIME` (1 row per campaign)**
   - Identified deterministically by `insight_key = make_insight_key("Marketing Campaign", campaign, "LIFETIME", "")`.
   - Date is omitted from the key so that each sync run updates the single lifetime row in place rather than creating new rows daily.
   - Preserves cumulative totals across all linked child channels (Ads, Social, WhatsApp) and direct CRM lead/conversation attributions.

2. **`DAY` (Bounded refresh window)**
   - Hourly synchronization (`spend_sync.run` calling `sync_marketing_campaign_insights(campaign)`) recomputes only the most recent `CAMPAIGN_DAY_REFRESH_DAYS` (7) days.
   - Historical days older than the refresh window are final and skipped during routine runs.
   - First sync run (when no prior insight rows exist) or running with `full=True` backfills all days from `start_date` up to `today`.
   - Ended campaigns whose `end_date` is older than `CAMPAIGN_DAY_REFRESH_DAYS` and already possess a `LIFETIME` row are skipped entirely.
   - Future campaigns (`start_date > today`) or campaigns with inverted dates (`end_date < start_date`) write zero rows.

3. **`WEEK` (Retention rollup & compaction)**
   - Daily/hourly rollup via `rebuild_campaign_weeks(campaign)` aggregates `DAY` rows older than `INSIGHT_DAY_KEEP_DAYS` (90 days) into ISO Monday-start `WEEK` rows (`date = Monday`).
   - Only complete weeks (where Sunday = Monday + 6 days is strictly older than the keep cutoff) are rolled up. Weeks straddling the cutoff remain as `DAY` rows until the entire week is older than the retention threshold, preventing data loss from partial overwrites.
   - Additive count and currency columns (`total_engagement`, `total_leads`, `total_conversations`, `ads_*`, `social_*`, `whatsapp_*`, `leads`, `conversations`) are summed.
   - Non-additive reach metrics (`ads_reach`, `social_reach`, `total_reach`) and reach-dependent ratios (`ads_frequency`, `social_engagement_rate`) stay at 0 on `WEEK` rows to prevent reach inflation.
   - Ratio metrics (`ads_ctr`, `ads_cpc`, `ads_cpm`, `ads_roas`, `ads_cost_per_conversion`, `ads_conversion_rate`, `whatsapp_delivery_rate`, `whatsapp_read_rate`) are recomputed from the aggregated sums using safe division.
   - Underlying `DAY` rows are deleted only after the corresponding `WEEK` row has been verified in the database.

### Readers and Downstream Aggregations (Task D2)

Reader endpoints query the compacted multi-tier storage model cleanly without double counting:

1. **`get_campaign_dashboard(campaign, days)`**:
   - For lifetime views (`days=0` or `days >= _LIFETIME_DAYS`): reads directly from the single `LIFETIME` row for the campaign. Sets `reach_approximate=True` because ads reach is derived from DAY/WEEK compaction tiers where historical reach is approximate.
   - For windowed views (`0 < days < _LIFETIME_DAYS`): sums `DAY` and `WEEK` rows within the date window `[today - days, today]`.
   - `LIFETIME` rows are strictly excluded from date range aggregations.
   - Because completed weeks have their source daily rows deleted upon rollup, summing `DAY` and `WEEK` rows has zero double counting.
   - Non-additive reach metrics (`total_reach`, `ads_reach`) are summed **only from `DAY` rows** (`WEEK` rows carry 0 reach).
   - If the window spans into `WEEK` rows, `reach_approximate=True` is attached to `totals`, `summary`, and top-level response; otherwise `reach_approximate=False`.
   - Response shapes are 100% backward compatible with existing frontend consumers.

2. **`get_campaigns_overview(days, filters, period)`**:
   - Queries `DAY` and `WEEK` rows across all campaigns in scope for the requested date window or named period (`7d`, `mtd`/`month`, `90d`/`qtr`).
   - For `mtd`/`month`, calculates month-to-date window and an equal-length previous period before month start for comparative deltas.
   - Computes cross-campaign sums for `reach` (DAY rows only), `engagement`, and `conversions`.
   - Returns status breakdown including `paused_campaigns` and sums `lead_goal` across campaigns.
   - Generates daily/weekly `spend_trend` time series without double counting and without including `LIFETIME` rows.
   - Sets `reach_approximate=True` on `kpis` and root response if any `WEEK` rows fall within the queried window.

### Code-Defined Custom Fields

- **`lead_goal` (Int)**:
  - Custom field on `Marketing Campaign` declared in `msuite_campaigns/setup/campaign_fields.py`.
  - Idempotently synchronized on `bench migrate` via `after_migrate = "msuite_campaigns.setup.install.after_migrate"`.
  - Allows campaigns to track target lead acquisition against actual CRM Lead generation.

### Migrations and Patches

- Patch `campaign_compact_insight_rows`:
  - Deduplicates legacy duplicate rows across all granularities (`LIFETIME`, `WEEK`, `DAY`), retaining the newest row.
  - Re-keys legacy rows using `make_insight_key("Marketing Campaign", campaign, granularity, bucket)`.
  - Commits database changes in batches of 2000 rows.
  - Adds a database unique constraint on `tabMarketing Campaign Insight(insight_key)`.

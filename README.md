### MSuite Campaigns

MSuite Campaigns for the MSuite platform.

The Campaign module provides multi-channel marketing campaign orchestration and aggregated analytics across Ads, Social Posts, and WhatsApp channels.

#### Features & Architecture
- **Marketing Campaign Insight Compaction Model**: Prevents unbounded table growth with multi-tier storage:
  - `LIFETIME`: Single row per campaign using deterministic `insight_key` upserts.
  - `DAY`: Bounded 7-day refresh window for active campaign metrics.
  - `WEEK`: ISO Monday rollup compacting older daily rows beyond retention cutoff.
- **Deduplication Patch**: `campaign_compact_insight_rows` backfills deterministic `insight_key` and cleans up duplicate legacy rows.
- **Metric Catalog & Performance**: Canonical metric catalog definitions and readers without double-counting.


### Installation

You can install this app using the [bench](https://github.com/frappe/bench) CLI:

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app $URL_OF_THIS_REPO --branch dev
bench install-app msuite_campaigns
```

### Contributing

This app uses `pre-commit` for code formatting and linting. Please [install pre-commit](https://pre-commit.com/#installation) and enable it for this repository:

```bash
cd apps/msuite_campaigns
pre-commit install
```

Pre-commit is configured to use the following tools for checking and formatting your code:

- ruff
- eslint
- prettier
- pyupgrade

### License

mit

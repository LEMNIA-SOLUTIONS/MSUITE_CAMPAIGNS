### MSuite Campaigns

MSuite Campaigns for the MSuite platform.

The Campaign module provides multi-channel marketing campaign orchestration and aggregated analytics across Ads, Social Posts, and WhatsApp channels.

#### Features & Architecture
- **Marketing Campaign Insight Compaction Model**: Prevents unbounded table growth with multi-tier storage:
  - `LIFETIME`: Single row per campaign using deterministic `insight_key` upserts.
  - `DAY`: Bounded 7-day refresh window for active campaign metrics.
  - `WEEK`: ISO Monday rollup compacting older daily rows beyond retention cutoff.
- **Indexed name lookup**: `campaign_name` has a search index and `campaign_name_exists` compares the bare column (case and trailing-space insensitivity come from the `utf8mb4_unicode_ci` collation, which also ignores accents). The `trim_campaign_names` patch trims legacy padded names and skips, and logs via `frappe.log_error`, any row whose trimmed name would collide.
- **Deduplication Patch**: `campaign_compact_insight_rows` backfills deterministic `insight_key` and cleans up duplicate legacy rows.
- **Metric Catalog & Performance**: Canonical metric catalog definitions and readers without double-counting.
- **Cross-Campaign Overview & Dashboard**:
  - `get_campaigns_overview` supports flexible periods (`7d`, `mtd` / `month`, `90d` / `qtr`) with previous-period comparison totals.
  - Cross-campaign rollup for `reach`, `engagement`, `conversions`, and `paused_campaigns` count.
  - Code-defined `lead_goal` Int custom field on `Marketing Campaign`, auto-synced via `after_migrate`.
- **Plan Limits & Gating (`msuite_plan_gates`)**:
  - `Marketing Campaign`: Gated by `campaigns_per_month` (product `CAMPAIGNS`, `period="month"`), limiting new campaigns created per calendar month.
  - `Audience`: Gated by multi-channel feature gate (`bulk_messaging`) and `campaigns_audiences` (product `CAMPAIGNS`, `period="total"`), limiting total active audience lists. Audience member capacity is gated directly on the parent `Audience` by `campaigns_audience_size` (product `CAMPAIGNS`, `period="total"`), counting `members` rows held in document memory to enforce maximum contacts per audience (allowing edits to existing over-cap audiences unless member rows are added).
  - All gates support 80% proactive warnings on prospective writes, fail-open resiliency, and transitional unenforced bypass when features are not in the active plan payload.


- **Audience File Import (`validation_import_adapter`)**:
  - Imports audience members from Excel (`.xlsx`/`.xls`) and CSV files via `msuite_core.utils.files`.
  - Formatted Excel date cells (e.g. `dd-mmm-yy`) are preserved as text strings (e.g. `13-Nov-26`) in member `recipient_data` instead of turning into `2026-11-13 00:00:00`.
  - CSV midnight timestamps (`YYYY-MM-DD 00:00:00`) are normalized to date-only strings (`YYYY-MM-DD`).

- **Unified Broadcast & Legacy Coexistence**:
  - `list_channels(campaign)` (`campaign/api/v1/composer.py`): WhatsApp channel children read from `Broadcast` rows with WhatsApp channel, respecting caller permissions, and deduplicate against migrated `Bulk WhatsApp Message` records via `broadcast.legacy.resolve`.
  - Campaign rollups in `analytics.py` (`sync_marketing_campaign_insights`, `_sync_campaign_day`, `_sync_campaign_lifetime`): aggregate `Channel Message` rows where `reference_doctype IN ('Broadcast', 'Bulk WhatsApp Message')` and sum recipient counts without double-counting migrated blasts.

#### Campaign Validation & Activity (UAT fixes)
- **Unique names**: a campaign name must be unique (case-insensitive). `make_unique_campaign_name` adds a numeric suffix on automatic paths (duplicate, recurring clone, dispatch hub, agent tools); a manual duplicate name is rejected with `CampaignNameExistsError` (HTTP 409; clients match the status because the site's response sanitizer removes `exc_type`).
- **Limits**: budget is `0` to `MAX_CAMPAIGN_BUDGET` (500,000), validated in `marketing_campaign.py` and mirrored in `frontend/src/shared/constants/limits.js`. Name and UTM lengths are capped; UTM values are slugged with `slugify_utm` (lowercase, underscores).
- **`get_campaign_activity(campaign)`** (`campaign/api/v1/composer.py`): latest `CAMPAIGN_ACTIVITY_LIMIT` (8) activity rows, including legacy `Campaign` references; returns `[]` for a campaign with no history.
- **`check_campaign_name(name, exclude="")`** (`campaign/api/v1/composer.py`): returns `{"exists": bool}` for a campaign name using the same trimmed, case-insensitive rule as the save-time check (`campaign_name_exists`). `exclude` is the campaign id being renamed. Empty or blank names return `exists: false`. Needs read permission on Marketing Campaign. The Create campaign drawer calls it while the user types; `save_campaign` stays the final guard.

### Recipient Lists and Broadcasts

The Recipient Lists and Broadcasts system unifies list management and multi-channel message delivery across WhatsApp, Email, and SMS.

#### DocTypes

The architecture consists of six doctypes:
- `Recipient List`: Parent document that manages recipient lists. It uses `autoname: field:list_name`. It replaces `Audience`, `WhatsApp Recipient List`, and `Email Recipient List`.
- `Recipient Contact`: Child table of `Recipient List`. It stores individual contact details, channel opt-out flags, and custom JSON payload data.
- `Broadcast`: Central campaign broadcast document. It uses `autoname: BC-.YYYY.-.#####`. It is not submittable. It replaces `Bulk WhatsApp Message`, `Bulk Email Message`, and `Bulk SMS Message`.
- `Broadcast Channel`: Child table of `Broadcast`. It holds channel configuration and delivery metrics for one channel. Each broadcast allows at most one row per channel type.
- `Broadcast Product`: Child table of `Broadcast`. It references product items for WhatsApp catalog and product messages.
- `Broadcast Recipient`: Standalone recipient dispatch log. It uses hash names. It is not submittable. It has a unique constraint on `(broadcast, channel, address)` and indexes on `provider_message_id` and `(broadcast, channel, status)`.

##### Broadcast Fields

| Fieldname | Label | Type | Description |
|---|---|---|---|
| `title` | Title | Data | Title of the broadcast. |
| `status` | Status | Select | Derived lifecycle status. |
| `campaign` | Marketing Campaign | Link | Linked Marketing Campaign. |
| `recipient_source` | Recipient Source | Select | Recipient List or Individual. |
| `recipient_list` | Recipient List | Link | Linked Recipient List document. |
| `scheduled_time` | Scheduled Time | Datetime | Optional future dispatch time. |
| `launched_at` | Launched At | Datetime | Timestamp when launch started. |
| `total_recipients` | Total Recipients | Int | Total recipients across channels. |
| `launch_error` | Launch Error | Small Text | Error message if launch fails. |
| `description` | Description | Small Text | Description of the broadcast. |
| `channels` | Channels | Table | Child table of Broadcast Channel rows. |
| `products` | Products | Table | Child table of Broadcast Product rows. |
| `legacy_doctype` | Legacy DocType | Data | Source legacy doctype name. |
| `legacy_name` | Legacy Name | Data | Source legacy document name. |

##### Broadcast Channel Fields

| Fieldname | Label | Type | Description |
|---|---|---|---|
| `channel` | Channel | Select | WhatsApp, Email, or SMS. |
| `status` | Status | Select | Status of the channel execution. |
| `delivery_engine` | Delivery Engine | Select | AWS or Standard. |
| `use_batched_enqueue` | Use Batched Enqueue | Check | Enables batched background enqueue. |
| `template_variables` | Template Variables | Code | JSON mapping of template variables. |
| `recipient_count` | Recipient Count | Int | Total recipients for this channel. |
| `sent_count` | Sent Count | Int | Count of sent messages. |
| `delivered_count` | Delivered Count | Int | Count of delivered messages. |
| `read_count` | Read Count | Int | Count of read messages. |
| `failed_count` | Failed Count | Int | Count of failed messages. |
| `bounced_count` | Bounced Count | Int | Count of bounced email messages. |
| `opened_count` | Opened Count | Int | Count of opened email messages. |
| `clicked_count` | Clicked Count | Int | Count of clicked email links. |
| `unsubscribed_count` | Unsubscribed Count | Int | Count of unsubscribed recipients. |
| `retry_count` | Retry Count | Int | Count of delivery retry attempts. |
| `launch_error` | Launch Error | Small Text | Error text recorded during launch. |
| `whatsapp_account` | WhatsApp Account | Link | Linked WhatsApp Account document. |
| `whatsapp_template` | WhatsApp Template | Link | Linked WhatsApp Templates document. |
| `variable_type` | Variable Type | Select | Common or Unique template variables. |
| `from_number` | From WhatsApp Number | Data | WhatsApp sender phone number. |
| `attach` | Attach | Attach | Media file attachment for WhatsApp. |
| `is_mpm` | Enable MPM | Check | Multi-Product Message flag. |
| `mpm_header` | MPM Header | Data | Header text for Multi-Product Message. |
| `thumbnail_product_retailer_id` | Thumbnail Product Retailer ID | Data | Retailer ID for product thumbnail. |
| `is_carousel_template` | Is Carousel Template | Check | Carousel template flag. |
| `carousel_catalog` | Carousel Catalog | Link | Linked Meta Product Catalog. |
| `offer_expires_at` | Offer Expires At | Datetime | Expiration timestamp for offer. |
| `coupon_code` | Coupon Code | Data | Offer coupon code string. |
| `tap_target_url` | Tap Link URL | Data | Destination URL for tap link. |
| `tap_target_title` | Tap Link Title | Data | Display title for tap link. |
| `is_lead_automation_broadcast` | Is Lead Automation Broadcast | Check | Lead automation flag. |
| `lead_automation_rule` | Lead Automation Rule | Link | Linked Messaging Automation document. |
| `email_account` | Sender Email Account | Link | Linked Email Account document. |
| `email_template` | Email Template | Link | Linked Email Template document. |
| `subject_override` | Subject Override | Data | Subject line override for email. |
| `from_name` | From Name | Data | Sender display name for email. |
| `sms_account` | SMS Account | Link | Linked SMS Account document. |
| `sms_template` | SMS Template | Link | Linked SMS Template document. |
| `estimated_credits` | Estimated Credits | Float | Pre-send estimated SMS credit cost. |
| `actual_credits` | Actual Credits | Float | Total billed SMS credit usage. |

#### Lifecycle and Transitions

Broadcasts and channels use nine statuses: `Draft`, `Scheduled`, `Queued`, `In Progress`, `Paused`, `Completed`, `Partially Failed`, `Failed`, and `Cancelled`.

##### Allowed Transitions

| From Status | Allowed Target Statuses |
|---|---|
| `Draft` | `Scheduled`, `Queued`, `Cancelled` |
| `Scheduled` | `Draft`, `Queued`, `Cancelled` |
| `Queued` | `In Progress`, `Paused`, `Cancelled`, `Failed` |
| `In Progress` | `Paused`, `Completed`, `Partially Failed`, `Failed`, `Cancelled` |
| `Paused` | `Queued`, `In Progress`, `Cancelled` |
| `Partially Failed` | `Queued` |
| `Failed` | `Queued` |
| `Completed` | None (terminal) |
| `Cancelled` | None (terminal) |

##### Status Rollup Rules

The parent `Broadcast.status` is derived from child `Broadcast Channel` rows using fixed precedence:
1. If no channel rows exist or all rows are `Draft`, status is `Draft`.
2. If all channel rows are `Cancelled`, status is `Cancelled`.
3. If any channel row is `In Progress`, status is `In Progress`.
4. If any channel row is `Queued`, status is `Queued`.
5. If any channel row is `Paused`, status is `Paused`.
6. If any channel row is `Scheduled`, status is `Scheduled`.
7. If any channel row is `Draft` while others finished, status is `Draft`.
8. When all rows are terminal (`Completed`, `Cancelled`, `Failed`, `Partially Failed`):
   - If all channel rows are `Failed`, status is `Failed`.
   - If any channel row is `Failed` or `Partially Failed`, status is `Partially Failed`.
   - Otherwise, status is `Completed`.

#### Send Flow

Message delivery executes in seven stages:
1. **Save / Launch Request**: The user saves a `Broadcast` in `Draft` or `Scheduled` status via `api.v1.broadcast.save_broadcast`, or launches it via `api.v1.broadcast.launch_broadcast`. These whitelisted API methods enforce permission checks (`write` permission on `Broadcast`). When launching, execution delegates to `hub.launch`.
2. **Materialize**: Inside `hub.launch`, each channel executes with savepoint isolation. The hub validates the channel using the adapter's optional `validate_channel` function. It calls `materializer.materialize(broadcast, channel)` to load recipient contacts, normalize phone numbers and emails, exclude opted-out contacts, and write `Broadcast Recipient` rows in batches of 1000 with status `Queued`.
3. **Launch**: `hub.launch` calls `lifecycle.launch(broadcast, channel)`, which also runs `plan_enforcer.check_broadcast_launch` to enforce plan limits and channel monthly quotas. If `scheduled_time` is in the future, the channel transitions to `Scheduled`. Otherwise, the channel transitions to `Queued` and updates the parent status.
4. **Start Channel**: When a channel transitions to `Queued`, `hub.launch` calls `hub.start_channel(broadcast, channel)`. `hub.start_channel` advances channel status from `Queued` to `In Progress` via `lifecycle.transition_channel`. It routes delivery based on whether AWS delivery is enabled: it calls `aws.export_and_enqueue_channel` if AWS is configured, or `enqueue.enqueue_broadcast_channel` for the Standard engine.
5. **Standard Enqueue or AWS Hand-off**:
   - For `Standard`: `enqueue.enqueue_broadcast_channel` splits recipients into chunks of 500 and queues background jobs that run `enqueue.run_chunk`. Each chunk checks that the channel is still active, calls the adapter's `send_one` for each recipient, and applies the returned status through `recipients.apply_delivery_status`. An exception in `send_one` marks only that recipient `Failed`.
   - For `AWS`: `aws.export_and_enqueue_channel` builds one manifest line per queued recipient with the adapter's `manifest_header` and `manifest_row`. It requests an upload URL from the provider and uploads the manifest. It then calls the adapter's `sync_secret`, asks the provider to trigger the AWS campaign (`aws_pipeline.client.trigger_campaign`), and finally calls the adapter's `record_outbound`. AWS sends do not write per-recipient status back to the database. Counters come from DynamoDB through `rollup.channel_metrics`.
6. **Webhooks**: Gateways deliver status webhooks for sent, delivered, read, bounced, and failed events. The webhook looks up `Broadcast Recipient` by `provider_message_id`. It calls `recipients.apply_delivery_status` with monotonic status rank validation.
7. **Counters**: Webhook and worker events call `counters.request_counter_refresh(broadcast, channel)`. This queues a debounced refresh job. The job executes one grouped SQL query to update channel counter columns. When all recipients reach terminal statuses, the job transitions the channel to `Completed`, `Partially Failed`, or `Failed`.
8. **Completion and failure**: `enqueue.run_chunk` requests a counter refresh when it ends, so a Standard channel completes even when the gateway sends no status webhooks. If the hand-off in `hub.start_channel` raises an error, the channel moves from `In Progress` to `Failed` and `launch_error` holds the message. Background jobs start after the database commit (`enqueue_after_commit`).

#### Channel Adapter Contract

External channel apps integrate by registering dotted paths in `hooks.py`:
```python
broadcast_channel_adapters = ["<app_module>.broadcast_channel.ADAPTER"]
```

The adapter dictionary must contain the following keys:
- `channel`: Channel name string (`WhatsApp`, `Email`, or `SMS`).
- `send_one`: Function `(broadcast_doc, channel_row, recipient: dict) -> dict`. Sends one message. Returns `{"status": "Sent" | "Failed", "provider_message_id": str | None, "error_code": str | None, "failure_reason": str | None, "is_retryable": bool}`. Must not mutate database status directly.
- `manifest_header`: Function `(broadcast_doc, channel_row) -> dict`. Returns AWS manifest header dictionary with `bulk_doctype: "Broadcast"` and `bulk_doc_name: broadcast_doc.name`.
- `manifest_row`: Function `(broadcast_doc, channel_row, recipient: dict) -> dict`. Formats one recipient dictionary for AWS workers.
- `sync_secret`: Optional function `(broadcast_doc, channel_row) -> None`. Synchronizes credentials to AWS Secrets Manager.
- `record_outbound`: Optional function `(broadcast_doc, channel_row) -> None`. Creates outbound `Channel Message` rows using keyset pagination.
- `validate_channel`: Optional function `(broadcast_doc, channel_row) -> None`. Validates account connections and template status. Raises `frappe.ValidationError` if invalid.

#### Permissions and Plan Gates

##### Permission Rules (P1)
- **Create, edit and launch**: A user needs an administrator role (`System Manager`, `MSuite Admin`, `Campaign Manager`) or the channel role for every channel row on the broadcast (`WhatsApp User` or `WhatsApp Manager` for WhatsApp, `Email User` or `Email Manager` for Email, `SMS User` or `SMS Manager` for SMS). `Campaign User` counts as holding every channel for read, create, edit and launch.
- **Delete**: A user needs an administrator role or the manager role of every channel on the broadcast (`WhatsApp Manager`, `Email Manager`, `SMS Manager`). `Campaign User` and the plain channel user roles cannot delete.
- **Read and Inspect**: Viewing a broadcast requires an administrator role, `MSuite Viewer`, or at least one matching channel role. Users with partial roles only see channel rows and recipient logs that match their permissions. The system applies SQL filters via `permission_query_conditions`.

##### Plan Gates
- `check_broadcast`: Validates that the subscription plan includes the `bulk_messaging` feature before saving a broadcast.
- `check_broadcast_launch`: Evaluates monthly quota before starting channel delivery. It calls `usage_for_channel(channel, period="month")`. It counts `Broadcast Channel` rows created in the current month. If the count exceeds the limit, it raises `PlanLimitError`.

#### Whitelisted API

##### `msuite_campaigns.broadcast.api.v1.broadcast`

| Method | Arguments | Returns |
|---|---|---|
| `list_broadcasts` | `filters="", search="", status="", template="", date_range="", date_from="", date_to="", start=0, limit=20` | `{"rows": list[dict], "total": int}` |
| `get_broadcast_metrics` | None | `{"total": int, "draft": int, "scheduled": int, "inProgress": int, "completed": int, "failed": int, "cancelled": int}` |
| `get_broadcast` | `name` | `dict` (Broadcast document with child rows. Each channel row that the user may see carries `channel_metrics`.) |
| `save_broadcast` | `doc, launch=0, individual_rows="[]", name=""` | `{"name": str, "status": str, "results": dict}`. Only user-editable fields are accepted. Status, counters and timestamps are set by the system. A new broadcast always starts as `Draft`. |
| `launch_broadcast` | `name, individual_rows="[]"` | `dict[str, str]` (per-channel status) |
| `pause_broadcast` | `name, channel=""` | `dict[str, str]` (per-channel status). A channel that uses the AWS engine cannot be paused after the hand-off to AWS. |
| `resume_broadcast` | `name, channel=""` | `dict[str, str]` (per-channel status). A resumed Standard channel sends the recipients that are still `Queued`. |
| `cancel_broadcast` | `name, channel=""` | `dict[str, str]` (per-channel status) |
| `retry_failed_broadcast` | `name, channel=""` | `dict[str, str]` (per-channel status) |
| `delete_broadcasts` | `names` (JSON list string) | `{"deleted": list[str], "failed": list[dict]}` |
| `get_broadcast_progress` | `name` | `dict[str, dict]` (channel progress and percentages) |
| `get_broadcast_recipients` | `name, channel="", status="", search="", start=0, length=50, export=0` | `{"rows": list[dict], "total": int, "truncated": bool}` |
| `clone_broadcast` | `name` | `dict` (new Draft Broadcast document). The user needs create access to every channel on the source broadcast. |

##### `msuite_campaigns.audience.api.v1.recipient_list`

| Method | Arguments | Returns |
|---|---|---|
| `list_recipient_lists` | `search="", limit=100, start=0` | `list[dict]` (recipient list summaries) |
| `get_recipient_list` | `name` | `dict` (full Recipient List document) |
| `save_recipient_list` | `doc` | `dict` (saved document) |
| `delete_recipient_list` | `name` | `{"success": bool, "name": str}` |
| `bulk_delete_recipient_lists` | `names` | `{"deleted": list[str], "failed": list[dict]}` |
| `get_contacts` | `recipient_list, search="", start=0, page_length=50, status=""` | `{"rows": list[dict], "total": int}` |
| `update_contact` | `contact_name, full_name=None, raw_phone=None, raw_email=None` | `dict` (updated contact) |
| `delete_contact` | `contact_name` | `{"success": bool, "name": str}` |
| `add_contacts_manual` | `recipient_list, contacts` | `{"added": int, "total_contacts": int}` |
| `preview_file` | `file_url` | `{"columns": list[str], "sample_rows": list[dict], "total_rows": int}` |
| `parse_file_rows` | `file_url, phone_column="", email_column="", name_column="", data_columns=None` | `{"rows": list[dict], "total": int}` |
| `fetch_doctype_contacts` | `doctype_to_import, phone_field, email_field="", name_field="", filters=None, limit=0` | `{"rows": list[dict], "total": int}` |
| `fetch_doctype_members` | `doctype_to_import, phone_field, email_field="", name_field="", filters=None, limit=0` | `{"rows": list[dict], "total": int}` |
| `import_contacts` | `recipient_list, file_url, phone_column="", email_column="", name_column="", data_columns=None, validate_now=1` | `{"imported": int, "total_contacts": int, "validation": dict}` |
| `import_from_doctype` | `recipient_list, doctype_to_import, phone_field, email_field="", name_field=""` | `{"imported": int, "total_contacts": int}` |
| `validate_recipient_list` | `recipient_list, force=0` | `dict` (validation summary) |
| `get_validation_summary` | `recipient_list` | `dict` (validation summary breakdown) |
| `search_doctypes` | `query=""` | `list[dict]` (search results) |
| `get_doctype_fields` | `doctype` | `{"phone_fields": list, "email_fields": list, "name_fields": list, "data_fields": list}` |
| `get_reachable_contacts` | `recipient_list, channel` | `{"reachable": int, "dropped": int, "reasons": dict}` |

#### Central Scheduler

The broadcast scheduler initiates scheduled dispatches:
- Function: `msuite_campaigns.broadcast.scheduler.start_due_channels() -> int`.
- Schedule: Runs every five minutes via `scheduler_events["cron"]["*/5 * * * *"]` in `hooks.py`.
- Operation:
  1. Finds channel rows with status `Scheduled` where `scheduled_time <= now`.
  2. Verifies that the linked campaign is active via `is_campaign_active(campaign)`.
  3. Transitions each due channel row to `Queued`.
  4. Starts delivery via `hub.start_channel(broadcast, channel)`.
  5. Catches individual row errors, logs to `tabError Log`, and continues remaining broadcasts.

#### Data Migration

The migration module ports historical data into unified models:
- Module: `msuite_campaigns.broadcast.migration` and `msuite_campaigns.broadcast.legacy`.
- Coexistence rule: while the legacy record of a migrated `Broadcast` still exists, campaign analytics and the campaign calendar skip that `Broadcast`. The old code still counts and shows the legacy record. The check uses `legacy_doctype` and `legacy_name` at run time. After the legacy record is deleted, the `Broadcast` counts and shows on its own.
- In-flight rule: In-progress or paused legacy bulk messages migrate with status `Paused`. This prevents unexpected background delivery after migration.
- `migration.run(dry_run=True, batch_size=1000)`: Migrates lists and bulk messages in transactional batches. When `dry_run=True`, it performs validation without writing changes.
- `migration.verify() -> dict`: Compares source counts against target counts across all six legacy doctypes.
- `legacy.resolve(legacy_doctype, legacy_name) -> str | None`: Maps legacy document names to unified `Broadcast` or `Recipient List` names.
- `legacy.repoint_legacy_references(dry_run=False) -> dict`: Updates foreign key references on `Channel Message`, `Marketing Campaign`, and `Msuite Email Unsubscribe`.
- Site execution commands:
  ```bash
  bench --site lemnia.localhost execute msuite_campaigns.broadcast.migration.run --args "(True, 1000)"
  bench --site lemnia.localhost execute msuite_campaigns.broadcast.migration.run --args "(False, 1000)"
  bench --site lemnia.localhost execute msuite_campaigns.broadcast.migration.verify
  bench --site lemnia.localhost execute msuite_campaigns.broadcast.legacy.repoint_legacy_references --args "(False,)"
  ```

#### Staged Deletion

Legacy models will be removed in a later release after frontend cutover:
- Patch: `msuite_campaigns/patches/delete_legacy_bulk_doctypes.py`.
- State: Disabled and omitted from `patches.txt`.
- Guard conditions: The patch aborts unless three conditions are met:
  1. `migration.verify()` reports zero mismatches.
  2. `migration.run(dry_run=False)` produces zero new records.
  3. `confirm_delete=1` argument is passed.
- Order of deletion: `Bulk WhatsApp Message Product` -> `WhatsApp Recipient` -> `Email Recipient` -> `SMS Recipient` -> `Bulk WhatsApp Message` -> `Bulk Email Message` -> `Bulk SMS Message` -> `WhatsApp Recipient List` -> `Email Recipient List` -> `Audience Member` -> `Audience`.
- Checklist reference: See `/Users/abhishek/Frappe/docs/superpowers/plans/legacy-removal-checklist.md`.

#### Configuration

The unified broadcast system provides one configuration flag:
- Key: `msuite_broadcast_enabled` in `site_config.json` (defaults to `False`).
- Behaviour: When `False`, automation actions and lead updates route through legacy bulk messaging doctypes. When `True`, actions route through unified `Broadcast` and `Recipient List` models.

#### Known Limits

- Memory capacity: The materializer loads recipient lists into memory during chunking. Lists up to 50,000 contacts operate within standard limits. Lists over 100,000 contacts require increased worker memory.
- Database write overhead: `record_outbound` generates outbound `Channel Message` rows using keyset pagination. This step has O(N) database write complexity.
- WhatsApp list-type blast migration: Historical WhatsApp list blasts without `Channel Message` rows migrate delivery counters only. Individual recipient logs cannot be reconstructed without message rows.

#### Tests

```bash
bench --site lemnia.localhost run-tests --app msuite_campaigns --module msuite_campaigns.tests.test_broadcast_constants
bench --site lemnia.localhost run-tests --app msuite_campaigns --module msuite_campaigns.tests.test_recipient_list
bench --site lemnia.localhost run-tests --app msuite_campaigns --module msuite_campaigns.tests.test_broadcast_doctype
bench --site lemnia.localhost run-tests --app msuite_campaigns --module msuite_campaigns.tests.test_broadcast_recipients
bench --site lemnia.localhost run-tests --app msuite_campaigns --module msuite_campaigns.tests.test_broadcast_permissions
bench --site lemnia.localhost run-tests --app msuite_campaigns --module msuite_campaigns.tests.test_broadcast_lifecycle
bench --site lemnia.localhost run-tests --app msuite_campaigns --module msuite_campaigns.tests.test_broadcast_materializer
bench --site lemnia.localhost run-tests --app msuite_campaigns --module msuite_campaigns.tests.test_broadcast_adapters
bench --site lemnia.localhost run-tests --app msuite_campaigns --module msuite_campaigns.tests.test_broadcast_counters
bench --site lemnia.localhost run-tests --app msuite_campaigns --module msuite_campaigns.tests.test_broadcast_enqueue
bench --site lemnia.localhost run-tests --app msuite_campaigns --module msuite_campaigns.tests.test_broadcast_scheduler
bench --site lemnia.localhost run-tests --app msuite_campaigns --module msuite_campaigns.tests.test_broadcast_api
bench --site lemnia.localhost run-tests --app msuite_campaigns --module msuite_campaigns.tests.test_recipient_list_api
bench --site lemnia.localhost run-tests --app msuite_campaigns --module msuite_campaigns.tests.test_broadcast_campaign_integration
bench --site lemnia.localhost run-tests --app msuite_campaigns --module msuite_campaigns.tests.test_broadcast_legacy
bench --site lemnia.localhost run-tests --app msuite_campaigns --module msuite_campaigns.tests.test_broadcast_migration
bench --site lemnia.localhost run-tests --app msuite_campaigns --module msuite_campaigns.tests.test_delete_legacy_guard
```

Note: `test_dispatch_hub` and `test_aws_pipeline` are existing modules; they fail on a dev site where the AWS provider relay is disabled (pre-existing):
```bash
bench --site lemnia.localhost run-tests --app msuite_campaigns --module msuite_campaigns.tests.test_aws_pipeline
bench --site lemnia.localhost run-tests --app msuite_campaigns --module msuite_campaigns.tests.test_dispatch_hub
```

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

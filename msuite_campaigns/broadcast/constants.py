"""Shared constants and types for unified recipient broadcast.

Defines channel identifiers, broadcast and channel lifecycle statuses,
allowed status transitions, recipient statuses, and monotonic status ranks.
"""

CHANNELS: tuple[str, ...] = ("WhatsApp", "Email", "SMS")

BROADCAST_STATUSES: tuple[str, ...] = (
	"Draft",
	"Scheduled",
	"Queued",
	"In Progress",
	"Paused",
	"Completed",
	"Partially Failed",
	"Failed",
	"Cancelled",
)

CHANNEL_STATUSES: tuple[str, ...] = (
	"Draft",
	"Scheduled",
	"Queued",
	"In Progress",
	"Paused",
	"Completed",
	"Partially Failed",
	"Failed",
	"Cancelled",
)

ALLOWED_TRANSITIONS: dict[str, set[str]] = {
	"Draft": {"Scheduled", "Queued", "Cancelled"},
	"Scheduled": {"Draft", "Queued", "Cancelled"},
	"Queued": {"In Progress", "Paused", "Cancelled", "Failed"},
	"In Progress": {"Paused", "Completed", "Partially Failed", "Failed", "Cancelled"},
	"Paused": {"Queued", "In Progress", "Cancelled"},
	"Partially Failed": {"Queued"},
	"Failed": {"Queued"},
	"Completed": set(),
	"Cancelled": set(),
}

RECIPIENT_STATUSES: tuple[str, ...] = (
	"Queued",
	"Sent",
	"Delivered",
	"Read",
	"Failed",
	"Bounced",
	"Retrying",
	"Skipped",
	"Unsubscribed",
)

RECIPIENT_STATUS_RANK: dict[str, int] = {
	"Queued": 0,
	"Retrying": 1,
	"Sent": 2,
	"Delivered": 3,
	"Read": 4,
	"Failed": 5,
	"Bounced": 5,
	"Skipped": 5,
	"Unsubscribed": 5,
}

RECIPIENT_INSERT_BATCH: int = 1000

CHANNEL_ROLES: dict[str, tuple[str, ...]] = {
	"WhatsApp": ("WhatsApp Manager", "WhatsApp User"),
	"Email": ("Email Manager", "Email User"),
	"SMS": ("SMS Manager", "SMS User"),
}

CHANNEL_MANAGER_ROLES: dict[str, str] = {
	"WhatsApp": "WhatsApp Manager",
	"Email": "Email Manager",
	"SMS": "SMS Manager",
}

BROADCAST_ADMIN_ROLES: tuple[str, ...] = ("System Manager", "MSuite Admin", "Campaign Manager")

BROADCAST_READ_ALL_ROLES: tuple[str, ...] = BROADCAST_ADMIN_ROLES + ("MSuite Viewer",)

BROADCAST_ALL_CHANNEL_ROLES: tuple[str, ...] = ("Campaign User",)

SEND_CHUNK_SIZE: int = 500
SEND_QUEUE: str = "long"
SEND_TIMEOUT: int = 4000
SENDABLE_RECIPIENT_STATUSES: tuple[str, ...] = ("Queued", "Retrying")


CHANNEL_WHATSAPP: str = "WhatsApp"
CHANNEL_EMAIL: str = "Email"
CHANNEL_SMS: str = "SMS"

STATUS_SCHEDULED: str = "Scheduled"
STATUS_DRAFT: str = "Draft"
STATUS_QUEUED: str = "Queued"
STATUS_IN_PROGRESS: str = "In Progress"
STATUS_PAUSED: str = "Paused"
STATUS_COMPLETED: str = "Completed"
STATUS_PARTIALLY_FAILED: str = "Partially Failed"
STATUS_FAILED: str = "Failed"
STATUS_CANCELLED: str = "Cancelled"

RECIPIENT_STATUS_QUEUED: str = "Queued"
RECIPIENT_STATUS_SENT: str = "Sent"
RECIPIENT_STATUS_DELIVERED: str = "Delivered"
RECIPIENT_STATUS_READ: str = "Read"
RECIPIENT_STATUS_FAILED: str = "Failed"
RECIPIENT_STATUS_BOUNCED: str = "Bounced"
RECIPIENT_STATUS_RETRYING: str = "Retrying"
RECIPIENT_STATUS_SKIPPED: str = "Skipped"
RECIPIENT_STATUS_UNSUBSCRIBED: str = "Unsubscribed"

# DocType constants
BROADCAST_DOCTYPE: str = "Broadcast"
BROADCAST_CHANNEL_DOCTYPE: str = "Broadcast Channel"
RECIPIENT_LIST_DOCTYPE: str = "Recipient List"
BULK_WHATSAPP_MESSAGE_DOCTYPE: str = "Bulk WhatsApp Message"
BULK_EMAIL_MESSAGE_DOCTYPE: str = "Bulk Email Message"
BULK_SMS_MESSAGE_DOCTYPE: str = "Bulk SMS Message"
AUDIENCE_DOCTYPE: str = "Audience"
AUDIENCE_MEMBER_DOCTYPE: str = "Audience Member"

# Task 14: Data migration constants
MIGRATION_BATCH: int = 200
MIGRATION_VERIFY_CHUNK: int = 1000
LEGACY_LIST_DOCTYPES: tuple[str, ...] = (AUDIENCE_DOCTYPE, "WhatsApp Recipient List", "Email Recipient List")
LEGACY_BROADCAST_DOCTYPES: tuple[str, ...] = (BULK_WHATSAPP_MESSAGE_DOCTYPE, BULK_EMAIL_MESSAGE_DOCTYPE, BULK_SMS_MESSAGE_DOCTYPE)

# Schedule modes
SCHEDULE_SAME: str = "Same for all channels"
SCHEDULE_PER_CHANNEL: str = "Per channel"
SCHEDULE_MODES: tuple[str, ...] = (SCHEDULE_SAME, SCHEDULE_PER_CHANNEL)

# Scheduler constants
SCHEDULER_BATCH_LIMIT: int = 100

# Whitelists for save_broadcast to prevent mass-assignment vulnerabilities
WRITABLE_BROADCAST_FIELDS: tuple[str, ...] = (
	"title",
	"description",
	"campaign",
	"recipient_source",
	"recipient_list",
	"scheduled_time",
	"schedule_mode",
)

WRITABLE_CHANNEL_FIELDS: tuple[str, ...] = (
	"channel",
	"delivery_engine",
	"use_batched_enqueue",
	"template_variables",
	"scheduled_time",
	"whatsapp_account",
	"whatsapp_template",
	"variable_type",
	"from_number",
	"attach",
	"is_mpm",
	"mpm_header",
	"thumbnail_product_retailer_id",
	"is_carousel_template",
	"carousel_catalog",
	"offer_expires_at",
	"coupon_code",
	"tap_target_url",
	"tap_target_title",
	"is_lead_automation_broadcast",
	"lead_automation_rule",
	"email_account",
	"email_template",
	"subject_override",
	"from_name",
	"sms_account",
	"sms_template",
)

# Pagination limits for draft recipients preview
DEFAULT_DRAFT_PAGE_LENGTH: int = 20
MAX_DRAFT_PAGE_LENGTH: int = 100

# Date range presets for broadcast listing
DATE_RANGE_TODAY: str = "today"
DATE_RANGE_7DAYS: str = "7days"
DATE_RANGE_30DAYS: str = "30days"
DATE_RANGE_MONTH: str = "month"
DATE_RANGE_QUARTER: str = "quarter"
DATE_RANGE_CUSTOM: str = "custom"

# Stale queue limits and messages
STALE_QUEUED_MINUTES: int = 30
ERROR_NO_RECIPIENTS_QUEUED: str = "No recipients were queued"

# In-flight recipient statuses preventing completion
PENDING_RECIPIENT_STATUSES: tuple[str, ...] = ("Queued", "Retrying", "Sending")






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

BROADCAST_ADMIN_ROLES: tuple[str, ...] = ("System Manager", "MSuite Admin", "Campaign Manager")

BROADCAST_READ_ALL_ROLES: tuple[str, ...] = BROADCAST_ADMIN_ROLES + ("MSuite Viewer",)

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

# Task 14: Data migration constants
MIGRATION_BATCH: int = 200
LEGACY_LIST_DOCTYPES: tuple[str, ...] = ("Audience", "WhatsApp Recipient List", "Email Recipient List")
LEGACY_BROADCAST_DOCTYPES: tuple[str, ...] = ("Bulk WhatsApp Message", "Bulk Email Message", "Bulk SMS Message")




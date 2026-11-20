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


# Copyright (c) 2026, MSuite and contributors
# For license information, please see license.txt

"""Broadcast controller."""

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import get_datetime, now_datetime

from msuite_campaigns.broadcast.constants import (
	CHANNEL_EMAIL,
	CHANNEL_SMS,
	CHANNEL_WHATSAPP,
	CHANNELS,
	RECIPIENT_INSERT_BATCH,
	STATUS_SCHEDULED,
)

# Required fields per channel (fieldname, label lookup key)
REQUIRED_CHANNEL_FIELDS: dict[str, tuple[str, ...]] = {
	CHANNEL_WHATSAPP: ("whatsapp_account", "whatsapp_template"),
	CHANNEL_EMAIL: ("email_account", "email_template"),
	CHANNEL_SMS: ("sms_account", "sms_template"),
}

# Channel-specific fields that cannot appear on rows of a different channel
CHANNEL_SPECIFIC_FIELDS: dict[str, tuple[str, ...]] = {
	CHANNEL_WHATSAPP: (
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
	),
	CHANNEL_EMAIL: (
		"email_account",
		"email_template",
		"subject_override",
		"from_name",
	),
	CHANNEL_SMS: (
		"sms_account",
		"sms_template",
		"estimated_credits",
		"actual_credits",
	),
}


class Broadcast(Document):
	"""Broadcast document managing multi-channel outbound message campaigns."""

	def validate(self) -> None:
		"""Validate Broadcast document invariants.

		Order of validation:
		1. At least one channel row is present.
		2. At most one row per channel type (names duplicate channel on failure).
		3. Each channel row contains its required account and template fields.
		4. Channel rows do not carry fields belonging to another channel.
		5. Recipient list is required when recipient_source is 'Recipient List'
		   and must be empty when 'Individual'.
		6. Broadcast products child rows are only allowed when a WhatsApp channel is present.
		7. Scheduled time cannot be in the past when status is 'Scheduled'.

		Raises:
			frappe.ValidationError: If any of the invariants are violated.

		Side effects:
			None. Pure in-memory document validation.
		"""
		self._validate_channel_count()
		self._validate_unique_channels()
		self._validate_channel_required_fields()
		self._validate_foreign_channel_fields()
		self._validate_recipient_source()
		self._validate_products()
		self._validate_scheduled_time()

	def _validate_channel_count(self) -> None:
		"""Ensure at least one channel row is defined.

		Raises:
			frappe.ValidationError: If self.channels is empty.

		Side effects:
			None.
		"""
		if not self.channels:
			frappe.throw(_("At least one channel is required."))

	def _validate_unique_channels(self) -> None:
		"""Ensure only one row per valid channel type exists.

		Raises:
			frappe.ValidationError: If channel is missing, invalid, or duplicate rows
				are detected.

		Side effects:
			None.
		"""
		seen_channels: set[str] = set()
		for row in self.channels:
			channel = row.get("channel")
			if not channel:
				frappe.throw(_("Channel is required on each channel row."))
			if channel not in CHANNELS:
				frappe.throw(_("Invalid channel {0}.").format(channel))
			if channel in seen_channels:
				frappe.throw(_("Channel {0} cannot be added more than once.").format(channel))
			seen_channels.add(channel)

	def _validate_channel_required_fields(self) -> None:
		"""Ensure each channel row contains its required account and template.

		Raises:
			frappe.ValidationError: If required fields are missing on a channel row.

		Side effects:
			None.
		"""
		child_meta = frappe.get_meta("Broadcast Channel")
		for row in self.channels:
			channel = row.get("channel")
			required_fields = REQUIRED_CHANNEL_FIELDS.get(channel, ())
			for fieldname in required_fields:
				if not row.get(fieldname):
					label = child_meta.get_label(fieldname) or fieldname
					frappe.throw(_("{0} is required for channel {1}.").format(label, channel))

	def _validate_foreign_channel_fields(self) -> None:
		"""Ensure channel rows do not carry fields belonging to other channels.

		Raises:
			frappe.ValidationError: If a foreign-channel field is populated.

		Side effects:
			None.
		"""
		child_meta = frappe.get_meta("Broadcast Channel")
		for row in self.channels:
			current_channel = row.get("channel")
			for channel_type, fields in CHANNEL_SPECIFIC_FIELDS.items():
				if channel_type == current_channel:
					continue
				for fieldname in fields:
					val = row.get(fieldname)
					if val not in (None, "", 0, 0.0, "0"):
						label = child_meta.get_label(fieldname) or fieldname
						frappe.throw(
							_("Field {0} ({1}) is not allowed for channel {2}.").format(
								label, fieldname, current_channel
							)
						)

	def _validate_recipient_source(self) -> None:
		"""Ensure recipient list presence aligns with recipient source.

		Raises:
			frappe.ValidationError: If recipient_list is missing for 'Recipient List'
				or populated for 'Individual'.

		Side effects:
			None.
		"""
		meta = self.meta
		rl_label = meta.get_label("recipient_list") or "Recipient List"
		rs_label = meta.get_label("recipient_source") or "Recipient Source"

		if self.recipient_source == "Recipient List":
			if not self.recipient_list:
				frappe.throw(_("{0} is required when {1} is Recipient List.").format(rl_label, rs_label))
		elif self.recipient_source == "Individual":
			if self.recipient_list:
				frappe.throw(_("{0} must be empty when {1} is Individual.").format(rl_label, rs_label))

	def _validate_products(self) -> None:
		"""Ensure products child rows are only present when WhatsApp channel exists.

		Raises:
			frappe.ValidationError: If products exist without a WhatsApp channel row.

		Side effects:
			None.
		"""
		if self.products:
			has_whatsapp = any(row.get("channel") == CHANNEL_WHATSAPP for row in self.channels or [])
			if not has_whatsapp:
				p_label = self.meta.get_label("products") or "Products"
				frappe.throw(_("{0} are only allowed when a WhatsApp channel is present.").format(p_label))

	def _validate_scheduled_time(self) -> None:
		"""Ensure scheduled_time is not in the past when status is Scheduled.

		Raises:
			frappe.ValidationError: If status is Scheduled and scheduled_time is missing
				or earlier than the current time.

		Side effects:
			None.
		"""
		if self.status == STATUS_SCHEDULED:
			st_label = self.meta.get_label("scheduled_time") or "Scheduled Time"
			if not self.scheduled_time:
				frappe.throw(_("{0} is required when status is Scheduled.").format(st_label))
			if get_datetime(self.scheduled_time) < now_datetime():
				frappe.throw(_("{0} cannot be in the past.").format(st_label))

	def channel_row(self, channel: str) -> "Document | None":
		"""Return the child row for the given channel, or None if absent.

		Args:
			channel: Channel identifier (e.g. 'WhatsApp', 'Email', 'SMS').

		Returns:
			BroadcastChannel row document matching the channel, or None.

		Raises:
			None.

		Side effects:
			None.
		"""
		for row in self.channels or []:
			if row.get("channel") == channel:
				return row
		return None

	def on_trash(self) -> None:
		"""Delete associated Broadcast Recipient rows in batches.

		Deletes recipients belonging to this broadcast in chunks of
		RECIPIENT_INSERT_BATCH to prevent locking or memory pressure.

		Raises:
			None.

		Side effects:
			Deletes rows from tabBroadcast Recipient.
		"""
		while True:
			recipient_names = frappe.get_all(
				"Broadcast Recipient",
				filters={"broadcast": self.name},
				pluck="name",
				limit=RECIPIENT_INSERT_BATCH,
			)
			if not recipient_names:
				break
			frappe.db.delete("Broadcast Recipient", {"name": ["in", recipient_names]})

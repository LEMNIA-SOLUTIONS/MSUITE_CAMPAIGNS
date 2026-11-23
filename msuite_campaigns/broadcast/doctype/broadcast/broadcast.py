# Copyright (c) 2026, MSuite and contributors
# For license information, please see license.txt

"""Broadcast controller."""

from __future__ import annotations

from datetime import datetime

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
	SCHEDULE_MODES,
	SCHEDULE_PER_CHANNEL,
	SCHEDULE_SAME,
	STATUS_DRAFT,
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

	def before_validate(self) -> None:
		"""Prepare Broadcast fields before validation.

		1. Handle schedule mode switches:
		   - Per channel -> Same for all channels: clear row scheduled times.
		   - Same for all channels -> Per channel: seed row scheduled times from parent scheduled_time.
		2. In Per channel mode, derive parent scheduled_time as earliest row time.

		Raises:
			None.

		Side effects:
			Mutates child row scheduled_time or self.scheduled_time.
		"""
		self._handle_schedule_mode_switch()
		self._derive_parent_scheduled_time()

	def _handle_schedule_mode_switch(self) -> None:
		"""Handle schedule mode switch between Same and Per channel.

		Switching Per channel -> Same clears row scheduled times on Draft and Scheduled rows.
		Switching Same -> Per channel seeds row scheduled times from parent scheduled_time
		only when parent scheduled_time is strictly in the future.
		Locked rows (not Draft or Scheduled) are never mutated.

		Raises:
			None.

		Side effects:
			Modifies row.scheduled_time on child channel rows.
		"""
		if self.is_new():
			return

		prev_doc = self.get_doc_before_save()
		if not prev_doc and self.name and frappe.db.exists(self.doctype, self.name):
			prev_doc = frappe.get_doc(self.doctype, self.name)

		if not prev_doc:
			return

		prev_mode = getattr(prev_doc, "schedule_mode", None) or SCHEDULE_SAME
		curr_mode = self.schedule_mode or SCHEDULE_SAME

		if prev_mode != curr_mode:
			if curr_mode == SCHEDULE_SAME:
				for row in self.channels or []:
					status = row.get("status") if isinstance(row, dict) else getattr(row, "status", None)
					if status in (STATUS_DRAFT, STATUS_SCHEDULED, None):
						row.scheduled_time = None
			elif curr_mode == SCHEDULE_PER_CHANNEL:
				is_parent_future = False
				if self.scheduled_time:
					st_dt = get_datetime(self.scheduled_time)
					if st_dt and st_dt > now_datetime():
						is_parent_future = True

				for row in self.channels or []:
					status = row.get("status") if isinstance(row, dict) else getattr(row, "status", None)
					if status in (STATUS_DRAFT, STATUS_SCHEDULED, None):
						st = row.get("scheduled_time") if isinstance(row, dict) else getattr(row, "scheduled_time", None)
						if not st and is_parent_future:
							row.scheduled_time = self.scheduled_time

	def _derive_parent_scheduled_time(self) -> None:
		"""Derive parent scheduled_time in Per channel mode from earliest row time.

		In Per channel mode, parent scheduled_time is derived as the earliest
		non-empty scheduled_time among child channel rows. If no rows have
		scheduled_time, parent scheduled_time is set to None.

		Raises:
			None.

		Side effects:
			Sets self.scheduled_time.
		"""
		mode = self.schedule_mode or SCHEDULE_SAME
		if mode == SCHEDULE_PER_CHANNEL:
			row_times = []
			for row in self.channels or []:
				st = row.get("scheduled_time") if isinstance(row, dict) else getattr(row, "scheduled_time", None)
				if st:
					row_times.append(st)
			if row_times:
				self.scheduled_time = min(row_times, key=lambda t: get_datetime(t))
			else:
				self.scheduled_time = None

	def validate(self) -> None:
		"""Validate Broadcast document invariants.

		Order of validation:
		1. Derive parent scheduled_time in Per channel mode if not already run.
		2. Locked channel rows cannot be removed or have scheduled_time changed.
		3. At least one channel row is present.
		4. At most one row per channel type (names duplicate channel on failure).
		5. Each channel row contains its required account and template fields.
		6. Channel rows do not carry fields belonging to another channel.
		7. Recipient list is required when recipient_source is 'Recipient List'
		   and must be empty when 'Individual'.
		8. Broadcast products child rows are only allowed when a WhatsApp channel is present.
		9. Schedule mode and channel row scheduled times are valid.
		10. Scheduled time cannot be in the past when status is 'Scheduled'.

		Raises:
			frappe.ValidationError: If any of the invariants are violated.

		Side effects:
			None. Pure in-memory document validation.
		"""
		self._derive_parent_scheduled_time()
		self._validate_locked_rows()
		self._validate_channel_count()
		self._validate_unique_channels()
		self._validate_channel_required_fields()
		self._validate_foreign_channel_fields()
		self._validate_recipient_source()
		self._validate_products()
		self._validate_schedule_mode()
		self._validate_scheduled_time()

	def _validate_locked_rows(self) -> None:
		"""Ensure locked channel rows (not Draft or Scheduled) cannot be removed or rescheduled.

		Raises:
			frappe.ValidationError: If a launched/locked channel row was removed or its
				scheduled_time was modified.

		Side effects:
			None.
		"""
		if self.is_new():
			return

		prev_doc = self.get_doc_before_save()
		if not prev_doc and self.name and frappe.db.exists(self.doctype, self.name):
			prev_doc = frappe.get_doc(self.doctype, self.name)

		if not prev_doc:
			return

		curr_channels = {getattr(r, "channel", None) for r in self.channels or []}
		curr_names = {getattr(r, "name", None) for r in self.channels or [] if getattr(r, "name", None)}

		for prev_row in prev_doc.channels or []:
			prev_status = getattr(prev_row, "status", None)
			if prev_status not in (STATUS_DRAFT, STATUS_SCHEDULED):
				row_name = getattr(prev_row, "name", None)
				row_channel = getattr(prev_row, "channel", None)

				# Check if locked row was removed
				if (row_name and row_name not in curr_names) and (row_channel not in curr_channels):
					frappe.throw(
						_("Cannot remove channel {0} because it is in status {1}.").format(
							row_channel, prev_status
						)
					)

				# Find matching current row
				curr_row = None
				if row_name and row_name in curr_names:
					curr_row = next((r for r in self.channels or [] if getattr(r, "name", None) == row_name), None)
				if not curr_row and row_channel in curr_channels:
					curr_row = next((r for r in self.channels or [] if getattr(r, "channel", None) == row_channel), None)

				if curr_row:
					prev_time = get_datetime(getattr(prev_row, "scheduled_time", None)) if getattr(prev_row, "scheduled_time", None) else None
					curr_time = get_datetime(getattr(curr_row, "scheduled_time", None)) if getattr(curr_row, "scheduled_time", None) else None
					if prev_time != curr_time:
						frappe.throw(
							_("Cannot change scheduled time for channel {0} because it is in status {1}.").format(
								row_channel, prev_status
							)
						)

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

	def _validate_schedule_mode(self) -> None:
		"""Validate schedule mode and channel row scheduled times.

		Rules:
		1. schedule_mode must be one of SCHEDULE_MODES.
		2. In 'Same for all channels' mode, channel rows cannot have scheduled_time set.
		3. In 'Per channel' mode, channel row scheduled_time cannot be in the past for
		   unlaunched (Draft/Scheduled) rows.

		Raises:
			frappe.ValidationError: If schedule_mode is invalid, if row times are set
				in Same mode, or if any row time is in the past in Per channel mode.

		Side effects:
			None.
		"""
		mode = self.schedule_mode or SCHEDULE_SAME
		if mode not in SCHEDULE_MODES:
			frappe.throw(_("Invalid schedule mode {0}.").format(mode))

		if mode == SCHEDULE_SAME:
			for row in self.channels or []:
				status = row.get("status") if isinstance(row, dict) else getattr(row, "status", None)
				if status not in (STATUS_DRAFT, STATUS_SCHEDULED, None):
					continue
				st = row.get("scheduled_time") if isinstance(row, dict) else getattr(row, "scheduled_time", None)
				if st:
					channel = row.get("channel") if isinstance(row, dict) else getattr(row, "channel", None)
					frappe.throw(
						_("Channel {0} cannot have a scheduled time when schedule mode is {1}.").format(
							channel, SCHEDULE_SAME
						)
					)
		elif mode == SCHEDULE_PER_CHANNEL:
			for row in self.channels or []:
				st = row.get("scheduled_time") if isinstance(row, dict) else getattr(row, "scheduled_time", None)
				status = row.get("status") if isinstance(row, dict) else getattr(row, "status", None)
				if st and status in (STATUS_DRAFT, STATUS_SCHEDULED, None):
					if get_datetime(st) < now_datetime():
						channel = row.get("channel") if isinstance(row, dict) else getattr(row, "channel", None)
						frappe.throw(
							_("Scheduled time for channel {0} cannot be in the past.").format(channel)
						)

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

	def effective_time(self, row: dict | Document | None = None) -> datetime | None:
		"""Return the effective scheduled datetime for a channel row.

		In 'Same for all channels' mode, returns the parent Broadcast.scheduled_time.
		In 'Per channel' mode, returns the row's scheduled_time (None means send at launch).

		Args:
			row: Broadcast Channel child document or dict, or None.

		Returns:
			datetime | None: Effective scheduled time in site time zone, or None.

		Raises:
			None.

		Side effects:
			None.
		"""
		mode = self.schedule_mode or SCHEDULE_SAME
		if mode == SCHEDULE_PER_CHANNEL:
			if not row:
				return None
			val = row.get("scheduled_time") if isinstance(row, dict) else getattr(row, "scheduled_time", None)
			return get_datetime(val) if val else None

		return get_datetime(self.scheduled_time) if self.scheduled_time else None

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

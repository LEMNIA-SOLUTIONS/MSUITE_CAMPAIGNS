# Copyright (c) 2026, MSuite and contributors
# For license information, please see license.txt

"""Timezone conversion helper for broadcast scheduling."""

from __future__ import annotations

from datetime import datetime
from typing import Any
import zoneinfo

import frappe
from frappe import _
from frappe.utils import get_datetime, get_system_timezone


def to_site_datetime(value: Any) -> datetime | None:
	"""Convert a datetime value or ISO string to a naive datetime in site time zone.

	Empty strings and None values return None. Naive datetime objects are returned
	unchanged. Timezone-aware datetimes are converted to the site timezone
	(frappe.utils.get_system_timezone()) and returned with tzinfo stripped (naive).
	Invalid datetime strings raise frappe.ValidationError.

	Params:
		value: Datetime object, date string, or None.

	Returns:
		datetime | None: Naive datetime in site time zone, or None.

	Raises:
		frappe.ValidationError: If string cannot be parsed as a valid datetime.

	Side effects:
		None.
	"""
	if value is None:
		return None

	if isinstance(value, str):
		stripped = value.strip()
		if not stripped:
			return None
		try:
			dt = get_datetime(stripped)
		except Exception:
			frappe.throw(
				_("Invalid datetime string: {0}").format(value),
				frappe.ValidationError,
			)
	elif isinstance(value, datetime):
		dt = value
	else:
		try:
			dt = get_datetime(value)
		except Exception:
			frappe.throw(
				_("Invalid datetime string: {0}").format(value),
				frappe.ValidationError,
			)

	if dt is None:
		return None

	if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
		return dt

	tz_name = get_system_timezone() or "Asia/Kolkata"
	try:
		site_tz = zoneinfo.ZoneInfo(tz_name)
	except Exception:
		import pytz

		site_tz = pytz.timezone(tz_name)

	return dt.astimezone(site_tz).replace(tzinfo=None)

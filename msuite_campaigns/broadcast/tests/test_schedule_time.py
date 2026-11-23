# Copyright (c) 2026, MSuite and contributors
# For license information, please see license.txt

"""Tests for to_site_datetime schedule time helper (Task F3).

Validates:
- Naive datetime is returned unchanged.
- ISO string with offset (+05:30) converts to site timezone and returns naive datetime.
- ISO string with UTC 'Z' converts to site timezone and returns naive datetime.
- Empty string and None return None.
- Invalid datetime string raises frappe.ValidationError with a clear message.
"""

from __future__ import annotations

from datetime import datetime, timezone
import zoneinfo

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import get_datetime, get_system_timezone

from msuite_campaigns.broadcast.schedule_time import to_site_datetime


class TestScheduleTime(IntegrationTestCase):
	"""Test suite for to_site_datetime timezone conversion helper."""

	def test_to_site_datetime_empty_returns_none(self) -> None:
		"""Validate that empty string, None, and whitespace return None.

		Params:
			None.

		Returns:
			None.

		Raises:
			AssertionError: If result is not None.

		Side effects:
			None.
		"""
		self.assertIsNone(to_site_datetime(None))
		self.assertIsNone(to_site_datetime(""))
		self.assertIsNone(to_site_datetime("   "))

	def test_to_site_datetime_naive_returns_unchanged(self) -> None:
		"""Validate that a naive datetime is returned unchanged.

		Params:
			None.

		Returns:
			None.

		Raises:
			AssertionError: If returned datetime differs from input or has tzinfo.

		Side effects:
			None.
		"""
		naive = datetime(2026, 11, 24, 10, 30, 0)
		res = to_site_datetime(naive)
		self.assertEqual(res, naive)
		self.assertIsNone(res.tzinfo)

	def test_to_site_datetime_offset_string_converts_to_site_timezone(self) -> None:
		"""Validate that an ISO string with +05:30 offset converts to site timezone and is naive.

		Params:
			None.

		Returns:
			None.

		Raises:
			AssertionError: If converted datetime does not match site timezone representation.

		Side effects:
			None.
		"""
		site_tz_name = get_system_timezone() or "Asia/Kolkata"
		site_tz = zoneinfo.ZoneInfo(site_tz_name)

		# Example: 10:00 at +05:30
		raw_str = "2026-11-24T10:00:00+05:30"
		res = to_site_datetime(raw_str)
		self.assertIsNotNone(res)
		self.assertIsNone(res.tzinfo)

		expected = get_datetime(raw_str).astimezone(site_tz).replace(tzinfo=None)
		self.assertEqual(res, expected)

	def test_to_site_datetime_zulu_string_converts_to_site_timezone(self) -> None:
		"""Validate that an ISO string with Zulu 'Z' converts to site timezone and is naive.

		Params:
			None.

		Returns:
			None.

		Raises:
			AssertionError: If converted datetime does not match site timezone representation.

		Side effects:
			None.
		"""
		site_tz_name = get_system_timezone() or "Asia/Kolkata"
		site_tz = zoneinfo.ZoneInfo(site_tz_name)

		raw_str = "2026-11-24T10:00:00Z"
		res = to_site_datetime(raw_str)
		self.assertIsNotNone(res)
		self.assertIsNone(res.tzinfo)

		expected = get_datetime(raw_str).astimezone(site_tz).replace(tzinfo=None)
		self.assertEqual(res, expected)

	def test_to_site_datetime_invalid_string_raises_validation_error(self) -> None:
		"""Validate that an invalid datetime string raises frappe.ValidationError.

		Params:
			None.

		Returns:
			None.

		Raises:
			AssertionError: If ValidationError with clear message is not raised.

		Side effects:
			None.
		"""
		with self.assertRaises(frappe.ValidationError) as ctx:
			to_site_datetime("not-a-datetime-string")
		self.assertIn("Invalid datetime", str(ctx.exception))

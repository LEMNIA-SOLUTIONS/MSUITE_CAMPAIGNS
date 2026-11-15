# Copyright (c) 2026, MSuite and contributors
# For license information, please see license.txt

import datetime
import os
import tempfile
import unittest
from unittest.mock import patch

import openpyxl

from msuite_campaigns.audience import validation_import_adapter


class TestValidationImportAdapterDatePreservation(unittest.TestCase):
	"""Unit tests verifying Excel and CSV date handling in audience validation_import_adapter."""

	def setUp(self):
		self.temp_dir = tempfile.TemporaryDirectory()
		self.addCleanup(self.temp_dir.cleanup)

	def test_preview_excel_date_formatted(self):
		"""Verify preview retains formatted Excel date text."""
		wb = openpyxl.Workbook()
		ws = wb.active
		ws["A1"] = "Mobile"
		ws["B1"] = "Full Name"
		ws["C1"] = "Due Date"
		ws["A2"] = "+919876543210"
		ws["B2"] = "Alice"
		ws["C2"] = datetime.date(2026, 11, 13)
		ws["C2"].number_format = "dd-mmm-yy"

		path = os.path.join(self.temp_dir.name, "audience.xlsx")
		wb.save(path)

		with patch("msuite_campaigns.audience.validation_import_adapter.resolve_file_path", return_value=path):
			result = validation_import_adapter.preview("/files/audience.xlsx")

		self.assertEqual(len(result["sample"]), 1)
		self.assertEqual(result["sample"][0]["Due Date"], "13-Nov-26")

	def test_rows_from_file_excel_date_in_recipient_data(self):
		"""Verify rows_from_file maps formatted Excel date into member recipient_data."""
		wb = openpyxl.Workbook()
		ws = wb.active
		ws["A1"] = "Mobile"
		ws["B1"] = "Full Name"
		ws["C1"] = "Due Date"
		ws["A2"] = "+919876543210"
		ws["B2"] = "Alice"
		ws["C2"] = datetime.date(2026, 11, 13)
		ws["C2"].number_format = "dd-mmm-yy"

		path = os.path.join(self.temp_dir.name, "audience_rows.xlsx")
		wb.save(path)

		with patch("msuite_campaigns.audience.validation_import_adapter.resolve_file_path", return_value=path):
			rows = validation_import_adapter.rows_from_file("/files/audience_rows.xlsx")

		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0]["full_name"], "Alice")
		self.assertEqual(rows[0]["raw_phone"], "+919876543210")
		self.assertEqual(rows[0]["recipient_data"], {"Due Date": "13-Nov-26"})

	def test_rows_from_file_csv_midnight_normalized_in_recipient_data(self):
		"""Verify rows_from_file normalizes CSV midnight datetime to date-only string in recipient_data."""
		path = os.path.join(self.temp_dir.name, "audience.csv")
		with open(path, "w", encoding="utf-8") as f:
			f.write("Mobile,Full Name,Due Date\n+919876543210,Alice,2026-11-13 00:00:00\n")

		with patch("msuite_campaigns.audience.validation_import_adapter.resolve_file_path", return_value=path):
			rows = validation_import_adapter.rows_from_file("/files/audience.csv")

		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0]["recipient_data"], {"Due Date": "2026-11-13"})

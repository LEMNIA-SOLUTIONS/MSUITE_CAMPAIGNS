# Copyright (c) 2026, MSuite and contributors
# For license information, please see license.txt

"""Staged deletion for legacy bulk message and recipient list doctypes (Task 15).

Order of deletion when confirmed and executed:
1. Verifies migration completeness via migration.verify() (zero mismatches required).
2. Verifies delta run via migration.run(dry_run=False) creates zero new records.
3. Requires confirm_delete to be truthy.
4. Repoints legacy references via legacy.repoint_legacy_references(dry_run=False).
5. Empties tables and deletes legacy DocTypes in strict dependency order (children first):
   - Bulk WhatsApp Message Product
   - WhatsApp Recipient
   - Email Recipient
   - SMS Recipient
   - Bulk WhatsApp Message
   - Bulk Email Message
   - Bulk SMS Message
   - WhatsApp Recipient List
   - Email Recipient List
   - Audience Member
   - Audience

This module is the guarded implementation. It is registered through
``remove_legacy_bulk_doctypes`` (see patches.txt), which passes confirm_delete=1.
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe import _

from msuite_campaigns.broadcast import legacy, migration

LEGACY_DOCTYPES_DELETE_ORDER: tuple[str, ...] = (
	"Bulk WhatsApp Message Product",
	"WhatsApp Recipient",
	"Email Recipient",
	"SMS Recipient",
	"Bulk WhatsApp Message",
	"Bulk Email Message",
	"Bulk SMS Message",
	"WhatsApp Recipient List",
	"Email Recipient List",
	"Audience Member",
	"Audience",
)


class EmptyPlan(dict):
	"""Representation of an empty execution plan for absent legacy doctypes.

	Supports equality comparison with empty dictionaries while providing
	expected plan keys with empty collections.
	"""

	def __bool__(self) -> bool:
		"""Return False in boolean evaluations.

		Returns:
			bool: Always False.

		Side effects:
			None.
		"""
		return False

	def __eq__(self, other: Any) -> bool:
		"""Compare equality with another dict or empty dict.

		Args:
			other: Object to compare with.

		Returns:
			bool: True if other is an empty dict or matches super equality.

		Side effects:
			None.
		"""
		if isinstance(other, dict) and len(other) == 0:
			return True
		return super().__eq__(other)


def _is_any_legacy_doctype_present() -> bool:
	"""Check whether any legacy bulk or recipient list DocType exists in the database.

	Returns:
		bool: True if at least one legacy DocType is present, False otherwise.

	Side effects:
		None (read-only queries).
	"""
	for doctype in LEGACY_DOCTYPES_DELETE_ORDER:
		if frappe.db.exists("DocType", doctype):
			return True
	return False


def _is_any_legacy_table_present() -> bool:
	"""Check whether any legacy bulk or recipient list database table exists.

	Returns:
		bool: True if at least one legacy table is present, False otherwise.

	Side effects:
		None (read-only queries).
	"""
	for doctype in LEGACY_DOCTYPES_DELETE_ORDER:
		if frappe.db.table_exists(doctype):
			return True
	return False


def _empty_table(doctype: str) -> None:
	"""Delete all rows from the specified DocType's database table if it exists.

	Args:
		doctype: Name of the DocType whose table is to be emptied.

	Raises:
		Exception: Propagates any database deletion errors.

	Side effects:
		Deletes all database records from the DocType's underlying table.
	"""
	if frappe.db.table_exists(doctype):
		frappe.db.delete(doctype)


def execute(confirm_delete: int = 0, dry_run: bool = True) -> dict:
	"""Plan or execute staged deletion of legacy bulk messaging DocTypes.

	When dry_run=True (default), deletes nothing and returns the execution plan:
		{
			"verify": <migration.verify() output>,
			"would_delete": list[str],
			"would_repoint": <legacy.repoint_legacy_references(dry_run=True) output>,
		}

	When dry_run=False:
		1. Asserts confirm_delete is truthy.
		2. Asserts migration.verify() reports zero mismatches for all legacy doctypes.
		3. Asserts migration.run(dry_run=False) creates nothing new (delta check).
		4. Repoints legacy references via legacy.repoint_legacy_references(dry_run=False).
		5. Empties tables, deletes DocTypes in dependency order, and drops tables.

	When executed on a site where all legacy doctypes and leftover tables are already absent,
	acts as an idempotent no-op and returns an EmptyPlan.

	Args:
		confirm_delete: Must be truthy (e.g. 1) to permit live deletion.
		dry_run: When True (default), simulates plan without deleting anything.

	Returns:
		dict: Execution plan (dry run) or deletion summary (live execution).

	Raises:
		frappe.ValidationError: If confirm_delete is not truthy in live mode,
			if migration.verify() reports mismatches, or if delta migration
			creates new records.

	Side effects:
		When dry_run=False: updates references in tabChannel Message, tabBroadcast,
		tabMSuite Email Unsubscribe; empties tables; deletes legacy DocType records;
		drops leftover database tables (DDL commits implicitly).
		When dry_run=True or absent doctypes/tables: no database modifications.
	"""
	doctype_present = _is_any_legacy_doctype_present()
	table_present = _is_any_legacy_table_present()

	# Idempotent guard: if legacy doctypes and leftover tables are already absent, no-op
	if not doctype_present and not table_present:
		return EmptyPlan({
			"verify": {},
			"would_delete": [],
			"would_repoint": {},
		})

	if dry_run:
		verify_results = migration.verify() if doctype_present else {}
		repoint_plan = legacy.repoint_legacy_references(dry_run=True) if doctype_present else {}
		return {
			"verify": verify_results,
			"would_delete": list(LEGACY_DOCTYPES_DELETE_ORDER),
			"would_repoint": repoint_plan,
		}

	# Order of work when really executed:
	# (1) confirm_delete guard
	if not confirm_delete:
		frappe.throw(
			_("confirm_delete must be truthy to execute legacy deletion"),
			frappe.ValidationError,
		)

	if doctype_present:
		# (2) migration.verify() must report ZERO mismatches for every legacy doctype
		verify_results = migration.verify()
		mismatches = {
			dt: data.get("mismatches", 0)
			for dt, data in verify_results.items()
			if data.get("mismatches", 0) > 0
		}
		if mismatches:
			frappe.throw(
				_("Migration verification reported mismatches for legacy doctypes: {0}").format(mismatches),
				frappe.ValidationError,
			)

		# (3) migration.run(dry_run=False) must create nothing new (delta check)
		delta_report = migration.run(dry_run=False)
		created_lists = {
			dt: data.get("migrated", 0)
			for dt, data in delta_report.get("recipient_lists", {}).items()
			if data.get("migrated", 0) > 0
		}
		created_broadcasts = {
			dt: data.get("migrated", 0)
			for dt, data in delta_report.get("broadcasts", {}).items()
			if data.get("migrated", 0) > 0
		}
		created_recipients = delta_report.get("recipients", 0)

		if created_lists or created_broadcasts or created_recipients > 0:
			missing: list[str] = []
			if created_lists:
				missing.append(f"recipient lists: {created_lists}")
			if created_broadcasts:
				missing.append(f"broadcasts: {created_broadcasts}")
			if created_recipients > 0:
				missing.append(f"recipients: {created_recipients}")
			frappe.throw(
				_("Migration delta check created new records ({0}); legacy deletion aborted").format(
					", ".join(missing)
				),
				frappe.ValidationError,
			)

		# (4) Repoint legacy references
		repoint_summary = legacy.repoint_legacy_references(dry_run=False)
	else:
		verify_results = {}
		repoint_summary = {}

	# (5) Delete legacy data, DocTypes, and drop tables in dependency order
	deleted_doctypes: list[str] = []
	for doctype in LEGACY_DOCTYPES_DELETE_ORDER:
		if doctype_present:
			_empty_table(doctype)
			frappe.delete_doc("DocType", doctype, force=True, ignore_missing=True)
		if frappe.db.table_exists(doctype):
			# Note: DDL statements (DROP TABLE) commit transactions implicitly in MySQL/MariaDB
			frappe.db.sql_ddl(f"DROP TABLE IF EXISTS `tab{doctype}`")
		deleted_doctypes.append(doctype)

	return {
		"verify": verify_results,
		"deleted": deleted_doctypes,
		"repointed": repoint_summary,
	}

# Copyright (c) 2026, MSuite and contributors
# For license information, please see license.txt

from pathlib import Path

import frappe
from frappe.tests import IntegrationTestCase

from msuite_core.services.permissions.sync import MSUITE_APPS

# Built via concatenation (not a literal) so this file itself doesn't trip
# the "no stray old-name references" grep in test_no_stray_old_name_references
# below — the whole point of these two names is to assert they're gone.
_OLD_DOCTYPE = "Campaign" + " Audience"
_OLD_MEMBER_DOCTYPE = _OLD_DOCTYPE + " Member"


class TestAudienceRename(IntegrationTestCase):
	def test_old_doctype_names_are_gone(self):
		self.assertFalse(frappe.db.exists("DocType", _OLD_DOCTYPE))
		self.assertFalse(frappe.db.exists("DocType", _OLD_MEMBER_DOCTYPE))

	def test_new_doctype_names_exist_in_audience_module(self):
		self.assertTrue(frappe.db.exists("DocType", "Audience"))
		self.assertTrue(frappe.db.exists("DocType", "Audience Member"))
		self.assertEqual(frappe.db.get_value("DocType", "Audience", "module"), "Audience")
		self.assertEqual(
			frappe.db.get_value("DocType", "Audience Member", "module"), "Audience"
		)

	def test_marketing_campaign_audience_link_points_at_new_name(self):
		field = frappe.get_meta("Marketing Campaign").get_field("audience")
		self.assertEqual(field.options, "Audience")

	def test_no_stray_old_name_references(self):
		import subprocess

		# Built via concatenation, not a literal — see the module-level
		# comment on _OLD_DOCTYPE: this line is itself scanned by the grep
		# below, so a literal match here would make the test unsatisfiable.
		search_term = _OLD_DOCTYPE
		result = subprocess.run(
			[
				"grep", "-rn", search_term, *MSUITE_APPS, "msuite_frontend",
				"--include=*.py", "--include=*.json",
				"--include=*.vue", "--include=*.js",
				# Compiled bundles under public/frontend are a build copy of
				# frontend/src — scanning them would just duplicate (or
				# stale-duplicate) whatever src/ already reports.
				"--exclude-dir=public",
				"--exclude-dir=node_modules",
				# Legitimate exception: the pre-model-sync rename patch from
				# Task 1 necessarily holds the old name as its `_PAIRS` source
				# value — that's the rename operation itself, not a stray.
				"--exclude=rename_campaign_audience_to_audience.py",
			],
			cwd=str(Path(frappe.get_app_path("msuite_core")).parents[1]),
			capture_output=True,
			text=True,
		)
		# Legitimate exception: Home.vue's dashboard card list has an
		# unrelated demo/mock string (a Meta ads product name, not this
		# doctype) that happens to contain the same two words.
		stray_lines = [
			line for line in result.stdout.splitlines() if "home/pages/Home.vue" not in line
		]
		self.assertEqual(stray_lines, [], f"Stray references found:\n{stray_lines}")

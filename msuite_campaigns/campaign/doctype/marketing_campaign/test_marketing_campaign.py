# Copyright (c) 2026, MSuite and contributors
# For license information, please see license.txt

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, now_datetime

from unittest.mock import patch

from msuite_campaigns.campaign.api.v1.composer import check_campaign_name, duplicate_campaign
from msuite_campaigns.campaign.doctype.marketing_campaign.marketing_campaign import (
	MAX_CAMPAIGN_BUDGET,
	make_unique_campaign_name,
	slugify_utm,
)


SLUG_PARITY_CASES = [
	("Summer Launch 2026", "summer_launch_2026"),
	("  Spaces   around  and   between  ", "spaces_around_and_between"),
	("CAPITALS AND lower", "capitals_and_lower"),
	("query?param=1&foo=2#hash", "query_param_1_foo_2_hash"),
	("café au lait", "caf_au_lait"),
	("summer ☀️ sale 🚀", "summer_sale"),
	("", ""),
	(None, ""),
	("already_valid_slug_123", "already_valid_slug_123"),
	("___leading_and_trailing___", "leading_and_trailing"),
	("a" * 200, "a" * 140),
]


class TestMarketingCampaign(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		if not hasattr(frappe.local, "test_objects"):
			frappe.local.test_objects = {}
		frappe.local.test_objects["Marketing Campaign"] = []
		super().setUpClass()

	def setUp(self):
		super().setUp()
		frappe.set_user("Administrator")
		# Clean up any test campaigns created in previous runs
		frappe.db.delete("Marketing Campaign", {"campaign_name": ["like", "TestMC%"]})

	def _create_campaign(self, **kwargs) -> "frappe._dict":
		now = now_datetime()
		data = {
			"doctype": "Marketing Campaign",
			"campaign_name": f"TestMC Default {frappe.generate_hash(length=8)}",
			"status": "Draft",
			"start_date": now,
			"end_date": add_days(now, 7),
		}
		data.update(kwargs)
		doc = frappe.get_doc(data)
		doc.insert(ignore_permissions=True)
		return doc

	# ------------------------------------------------------------------
	# SOC-117: Unique campaign names
	# ------------------------------------------------------------------

	def test_duplicate_name_exact_rejected(self):
		"""Exact duplicate campaign name is rejected with plain message."""
		name = f"TestMC Unique {frappe.generate_hash(length=6)}"
		self._create_campaign(campaign_name=name)

		with self.assertRaises(frappe.ValidationError) as cm:
			self._create_campaign(campaign_name=name)
		self.assertIn("A campaign with this name already exists", str(cm.exception))

	def test_duplicate_name_differing_by_case_rejected(self):
		"""Case-insensitive duplicate campaign name is rejected."""
		base = f"TestMC Case {frappe.generate_hash(length=6)}"
		self._create_campaign(campaign_name=base.lower())

		with self.assertRaises(frappe.ValidationError) as cm:
			self._create_campaign(campaign_name=base.upper())
		self.assertIn("A campaign with this name already exists", str(cm.exception))

	def test_duplicate_name_surrounding_whitespace_rejected(self):
		"""Surrounding whitespace is trimmed before uniqueness check."""
		base = f"TestMC Whitespace {frappe.generate_hash(length=6)}"
		self._create_campaign(campaign_name=base)

		with self.assertRaises(frappe.ValidationError) as cm:
			self._create_campaign(campaign_name=f"   {base}   ")
		self.assertIn("A campaign with this name already exists", str(cm.exception))

	def test_duplicate_name_unicode_emoji_rejected(self):
		"""Unicode and emoji names are checked for uniqueness."""
		base = f"TestMC 🪔 Diwali ✨ {frappe.generate_hash(length=6)}"
		self._create_campaign(campaign_name=base)

		with self.assertRaises(frappe.ValidationError) as cm:
			self._create_campaign(campaign_name=base)
		self.assertIn("A campaign with this name already exists", str(cm.exception))

	def test_empty_or_whitespace_only_name_rejected(self):
		"""Empty or whitespace-only campaign name is rejected."""
		with self.assertRaises(frappe.ValidationError):
			self._create_campaign(campaign_name="")

		with self.assertRaises(frappe.ValidationError):
			self._create_campaign(campaign_name="     ")

	def test_rename_onto_existing_name_rejected(self):
		"""Renaming campaign_name to an existing name is rejected."""
		c1 = self._create_campaign(campaign_name=f"TestMC Existing {frappe.generate_hash(length=6)}")
		c2 = self._create_campaign(campaign_name=f"TestMC Target {frappe.generate_hash(length=6)}")

		c2.campaign_name = c1.campaign_name
		with self.assertRaises(frappe.ValidationError) as cm:
			c2.save(ignore_permissions=True)
		self.assertIn("A campaign with this name already exists", str(cm.exception))

	def test_resave_same_doc_succeeds(self):
		"""Re-saving the same doc (or editing other fields) does not trip uniqueness."""
		c = self._create_campaign(campaign_name=f"TestMC Resave {frappe.generate_hash(length=6)}", budget=100)
		c.budget = 200
		c.status = "In Progress"
		c.save(ignore_permissions=True)
		self.assertEqual(c.budget, 200)

	def test_duplicate_campaign_twice_and_thrice(self):
		"""duplicate_campaign creates '<name> (Copy)', then '(Copy 2)', then '(Copy 3)'."""
		orig_name = f"TestMC Dupe {frappe.generate_hash(length=6)}"
		c = self._create_campaign(campaign_name=orig_name)

		res1 = duplicate_campaign(c.name)
		c1 = frappe.get_doc("Marketing Campaign", res1["name"])
		self.assertEqual(c1.campaign_name, f"{orig_name} (Copy)")

		res2 = duplicate_campaign(c.name)
		c2 = frappe.get_doc("Marketing Campaign", res2["name"])
		self.assertEqual(c2.campaign_name, f"{orig_name} (Copy 2)")

		res3 = duplicate_campaign(c.name)
		c3 = frappe.get_doc("Marketing Campaign", res3["name"])
		self.assertEqual(c3.campaign_name, f"{orig_name} (Copy 3)")

	def test_truncation_when_suffixing_138_char_name(self):
		"""When suffixing a 138-character name, the base is truncated to fit 140 chars."""
		long_base = "TestMC_" + ("x" * 131)  # Total 138 chars
		self.assertEqual(len(long_base), 138)
		c = self._create_campaign(campaign_name=long_base)

		# Duplicate 1: suffix is ' (Copy)' (7 chars) -> base truncated to 133 chars
		res1 = duplicate_campaign(c.name)
		c1 = frappe.get_doc("Marketing Campaign", res1["name"])
		self.assertLessEqual(len(c1.campaign_name), 140)
		self.assertTrue(c1.campaign_name.endswith(" (Copy)"))
		self.assertEqual(len(c1.campaign_name), 140)

		# Duplicate 2: suffix is ' (Copy 2)' (9 chars) -> base truncated to 131 chars
		res2 = duplicate_campaign(c.name)
		c2 = frappe.get_doc("Marketing Campaign", res2["name"])
		self.assertLessEqual(len(c2.campaign_name), 140)
		self.assertTrue(c2.campaign_name.endswith(" (Copy 2)"))
		self.assertEqual(len(c2.campaign_name), 140)

	# ------------------------------------------------------------------
	# SOC-118: Name and UTM length limits
	# ------------------------------------------------------------------

	def test_name_length_exact_140_succeeds(self):
		"""140-char campaign name saves successfully."""
		name_140 = "TestMC_" + ("a" * 133)
		self.assertEqual(len(name_140), 140)
		c = self._create_campaign(campaign_name=name_140)
		self.assertEqual(c.campaign_name, name_140)

	def test_name_length_141_fails_with_short_plain_message(self):
		"""141-char campaign name throws short plain message without echoing value."""
		name_141 = "TestMC_" + ("a" * 134)
		self.assertEqual(len(name_141), 141)
		with self.assertRaises(frappe.ValidationError) as cm:
			self._create_campaign(campaign_name=name_141)
		err = str(cm.exception)
		self.assertIn("cannot exceed 140 characters", err)
		self.assertNotIn(name_141, err)

	def test_multibyte_140_char_name_succeeds(self):
		"""Multi-byte 140-char name (140 characters) saves successfully."""
		name_mb = "TestMC_✨" + ("🌟" * 132)
		self.assertEqual(len(name_mb), 140)
		c = self._create_campaign(campaign_name=name_mb)
		self.assertEqual(c.campaign_name, name_mb)

	# ------------------------------------------------------------------
	# SOC-119: Budget and spend validation
	# ------------------------------------------------------------------

	def test_budget_valid_values(self):
		"""0, 500000, 'abc', None, and '' are valid budget values."""
		c1 = self._create_campaign(budget=0)
		self.assertEqual(c1.budget, 0)

		c2 = self._create_campaign(budget=MAX_CAMPAIGN_BUDGET)
		self.assertEqual(c2.budget, 500000)

		c3 = self._create_campaign(budget="abc")
		self.assertEqual(c3.budget, 0)

		c4 = self._create_campaign(budget=None)
		self.assertEqual(c4.budget or 0, 0)

		c5 = self._create_campaign(budget="")
		self.assertEqual(c5.budget or 0, 0)

	def test_budget_negative_and_excess_rejected(self):
		"""Negative budget or budget exceeding MAX_CAMPAIGN_BUDGET is rejected."""
		with self.assertRaises(frappe.ValidationError) as cm:
			self._create_campaign(budget=-1)
		self.assertIn("must be 0 or more", str(cm.exception))

		with self.assertRaises(frappe.ValidationError) as cm:
			self._create_campaign(budget=-0.01)
		self.assertIn("must be 0 or more", str(cm.exception))

		with self.assertRaises(frappe.ValidationError) as cm:
			self._create_campaign(budget="-1")
		self.assertIn("must be 0 or more", str(cm.exception))

		with self.assertRaises(frappe.ValidationError) as cm:
			self._create_campaign(budget=500000.01)
		self.assertIn("cannot exceed 500000", str(cm.exception))

	def test_spent_large_saves_and_spent_negative_fails(self):
		"""Large spent amount saves without cap, but negative spent is rejected."""
		c = self._create_campaign(spent=10_000_000)
		self.assertEqual(c.spent, 10_000_000)

		with self.assertRaises(frappe.ValidationError):
			self._create_campaign(spent=-1)

	# ------------------------------------------------------------------
	# SOC-120: UTM auto-slugging and parity
	# ------------------------------------------------------------------

	def test_utm_autoslug_controller(self):
		"""Controller auto-slugs utm_campaign, utm_term, utm_source, utm_medium."""
		c = self._create_campaign(
			utm_campaign="Summer Sale 2026!",
			utm_term="Growth & Marketing #1",
			utm_source="Google Ads / Search",
			utm_medium="Social Media",
		)
		self.assertEqual(c.utm_campaign, "summer_sale_2026")
		self.assertEqual(c.utm_term, "growth_marketing_1")
		self.assertEqual(c.utm_source, "google_ads_search")
		self.assertEqual(c.utm_medium, "social_media")

	def test_utm_slug_parity_table(self):
		"""Shared parity table matches expected slug outputs."""
		for raw, expected in SLUG_PARITY_CASES:
			self.assertEqual(slugify_utm(raw), expected)

	# ------------------------------------------------------------------
	# SOC-117: check_campaign_name API endpoint
	# ------------------------------------------------------------------

	def test_check_campaign_name_duplicate(self):
		"""Duplicate campaign name returns exists=True."""
		name = f"TestMC DupeCheck {frappe.generate_hash(length=6)}"
		self._create_campaign(campaign_name=name)
		res = check_campaign_name(name=name)
		self.assertEqual(res, {"exists": True})

	def test_check_campaign_name_different_case(self):
		"""Case-insensitive duplicate returns exists=True."""
		base = f"TestMC CaseCheck {frappe.generate_hash(length=6)}"
		self._create_campaign(campaign_name=base.lower())
		res = check_campaign_name(name=base.upper())
		self.assertEqual(res, {"exists": True})

	def test_check_campaign_name_surrounding_whitespace(self):
		"""Surrounding whitespace is trimmed before checking and returns exists=True."""
		base = f"TestMC WsCheck {frappe.generate_hash(length=6)}"
		self._create_campaign(campaign_name=base)
		res = check_campaign_name(name=f"   {base}   ")
		self.assertEqual(res, {"exists": True})

	def test_check_campaign_name_empty_or_whitespace_only(self):
		"""Empty or whitespace-only name returns exists=False."""
		self.assertEqual(check_campaign_name(name=""), {"exists": False})
		self.assertEqual(check_campaign_name(name="     "), {"exists": False})
		self.assertEqual(check_campaign_name(), {"exists": False})

	def test_check_campaign_name_exclude_own_doc(self):
		"""Excluding own doc name returns exists=False, allowing doc renames."""
		name = f"TestMC ExcludeCheck {frappe.generate_hash(length=6)}"
		c = self._create_campaign(campaign_name=name)
		res = check_campaign_name(name=name, exclude=c.name)
		self.assertEqual(res, {"exists": False})
		res_without_exclude = check_campaign_name(name=name)
		self.assertEqual(res_without_exclude, {"exists": True})

	def test_check_campaign_name_no_permission_denied(self):
		"""User without read permission on Marketing Campaign raises frappe.PermissionError."""
		with patch("frappe.has_permission", return_value=False):
			with self.assertRaises(frappe.PermissionError):
				check_campaign_name(name="Any Campaign")

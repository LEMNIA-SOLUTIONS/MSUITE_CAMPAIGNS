# Copyright (c) 2026, Administrator and contributors
# For license information, please see license.txt

from __future__ import annotations

from frappe.model.document import Document

from msuite_core.utils.insight_keys import make_insight_key


class MarketingCampaignInsight(Document):
	def before_save(self) -> None:
		"""Automatically generate the deterministic insight_key hash if empty.

		Args:
			None.

		Returns:
			None.

		Raises:
			None.

		Side effects:
			Sets self.insight_key before database persistence.
		"""
		if not self.insight_key:
			self.insight_key = make_insight_key(
				"Marketing Campaign",
				self.marketing_campaign,
				self.granularity,
				self.date or "",
			)

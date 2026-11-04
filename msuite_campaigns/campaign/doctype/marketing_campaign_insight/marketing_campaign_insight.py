# Copyright (c) 2026, Administrator and contributors
# For license information, please see license.txt

from __future__ import annotations

import frappe
from frappe.model.document import Document

from msuite_core.utils.insight_keys import make_key


class MarketingCampaignInsight(Document):
	def before_save(self) -> None:
		"""Automatically generate the unique insight_key deterministic hash."""
		self.insight_key = make_key(self.marketing_campaign, self.date, self.granularity)

"""This app's plan limits. Core's plan_enforcer reads them through the msuite_plan_gates hook."""

from __future__ import annotations

from msuite_core.services.plans.plan_enforcer import Gate, any_bulk_feature

GATES = {
	# Curating an audience is cheap and spans channels — feature gate only,
	# no count and no `product` (a product code would tie a multi-channel
	# audience to one channel's entitlement). The cap is enforced on the
	# blast, same rule as WhatsApp Recipient List.
	"Audience": Gate(
		feature="bulk_messaging",
		feature_for=any_bulk_feature,
		label="Bulk Messaging",
		on_methods=("before_insert", "before_save"),
	),
}

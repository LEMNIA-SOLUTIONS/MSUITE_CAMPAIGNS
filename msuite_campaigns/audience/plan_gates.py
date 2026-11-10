"""This app's plan limits. Core's plan_enforcer reads them through the msuite_plan_gates hook."""

from __future__ import annotations

import frappe
from frappe import _
from msuite_core.services.plans.plan_enforcer import (
	Gate,
	USAGE_WARNING_THRESHOLD_PERCENT,
	any_bulk_feature,
	get_feature_limit,
	is_feature_in_plan,
)

GATES = {
	# Curating an audience spans channels — feature gate checks bulk messaging per channel.
	# Also gated by CAMPAIGNS product limits (audience count). Audience size is enforced
	# directly on the parent Audience document since Audience Member is a child table.
	"Audience": Gate(
		feature="bulk_messaging",
		feature_for=any_bulk_feature,
		label="Bulk Messaging",
		on_methods=("before_insert", "before_save"),
		extra_gates=(
			Gate(
				feature="campaigns_audiences",
				product="CAMPAIGNS",
				label="Audiences",
				count_doctype="Audience",
				period="total",
				skip_if=lambda doc: bool(getattr(doc, "name", None) and not doc.is_new()),
				on_methods=("before_insert",),
			),
		),
	),
	"Marketing Campaign": Gate(
		feature="campaigns_per_month",
		product="CAMPAIGNS",
		label="Campaigns",
		count_doctype="Marketing Campaign",
		period="month",
		skip_if=lambda doc: bool(getattr(doc, "name", None) and not doc.is_new()),
		on_methods=("before_insert",),
	),
}


def check_audience_size_gate(doc, method: str = "") -> None:
	"""Enforce campaigns_audience_size limit on parent Audience document.

	Audience Member is a child table (istable: 1) where doc_events never fire
	during parent document save/insert. We count child rows held in memory on
	the parent document (len(doc.members)).

	Rules:
	- Transitional rule: only enforced if campaigns_audience_size is in plan payload.
	- Fails open with a warning log on error.
	- Honours ignore_plan_gate flag.
	- Editing an existing audience already over the cap is allowed unless member rows are added.
	"""
	if (
		getattr(frappe.flags, "in_migrate", False)
		or getattr(frappe.flags, "in_install", False)
		or getattr(frappe.flags, "in_patch", False)
	):
		return
	if getattr(getattr(doc, "flags", None), "ignore_plan_gate", False):
		return

	if not is_feature_in_plan("campaigns_audience_size"):
		return

	limit = get_feature_limit("campaigns_audience_size")
	if limit is None:
		return

	try:
		limit = int(limit)
		if limit <= 0:
			frappe.throw(
				_("Audience Size is not available on your current plan."),
				frappe.ValidationError,
			)

		members = getattr(doc, "members", None) or []
		current_count = len(members)

		if current_count <= limit:
			is_new = bool(getattr(doc, "is_new", None) and doc.is_new())
			if is_new and current_count >= limit * (USAGE_WARNING_THRESHOLD_PERCENT / 100.0):
				try:
					warned = getattr(frappe.local, "_plan_gate_warnings", None)
					if warned is None:
						frappe.local._plan_gate_warnings = set()
						warned = frappe.local._plan_gate_warnings
					key = f"campaigns_audience_size_{doc.name or 'new'}"
					if warned is None or key not in warned:
						if warned is not None:
							warned.add(key)
						frappe.msgprint(
							f"You have used {current_count} of {limit} Audience Size. Upgrade to avoid interruption.",
							indicator="orange",
							alert=True,
						)
				except Exception:
					pass
			return

		# Over cap: check if existing rows were already over the cap and no new rows were added.
		is_new = bool(getattr(doc, "is_new", None) and doc.is_new())
		if not is_new and getattr(doc, "name", None) and frappe.db.exists("Audience", doc.name):
			previous_count = frappe.db.count("Audience Member", {"parent": doc.name})
			if current_count <= previous_count:
				return

		frappe.throw(
			_("Audience Size limit reached ({0}/{1} total).").format(current_count, limit),
			frappe.ValidationError,
		)
	except frappe.ValidationError:
		raise
	except Exception as exc:
		frappe.logger("plan_enforcer").warning(
			f"Plan enforcer counter error for Audience (campaigns_audience_size): {exc}"
		)
		return

"""
End-to-end tests for the WhatsApp feature-gating contract.

This file proves the following invariant: when `MSuite Settings.plan_data`
declares a feature as disabled (or numeric limit as 0/N), the client
ACTUALLY blocks the operation. The point isn't to test that the seed
file is correct — it's to test that the gate code refuses to let the
operation through, irrespective of how plan_data got there.

We do this by directly stuffing a synthetic `plan_data` into
MSuite Settings (no provider round-trip needed in tests), then asking
the doctype to insert/save/submit. Pass = ValidationError. Fail = the
operation succeeded, which means the gate is broken.

Coverage:
  - Boolean feature gates  (calling, flows, inbox_whatsapp, etc.)
  - Numeric feature limits (messaging, templates, bulk_messaging)
  - The "skip_if" branches  (inbound calls / non-WA channels are NEVER
    blocked even when the gate exists)
  - The shared `enforce_gate` dispatcher (one path, every gate)

The tests rebuild plan_data per-test so they can't leak state into each
other or into production data.
"""
from __future__ import annotations

import json
from contextlib import contextmanager

import frappe
from frappe.tests import IntegrationTestCase

from msuite_core.services.plans import plan_enforcer

# --- Plan-data fixtures ---------------------------------------------------


# Plans the tests synthesize directly into MSuite Settings.plan_data.
# These mirror the canonical shape that provider's build_client_plan_data
# pushes — so if the provider's output shape changes, this fixture has to
# change too. Intentional coupling: the tests fail loudly if they diverge.
_TRIAL_PLAN = {
    "plan_name": "WhatsApp Trial",
    "products": ["WA"],
    "features": {
        "messaging":      {"enabled": True,  "limit": 100,  "label": "Messaging"},
        "templates":      {"enabled": True,  "limit": 3,    "label": "Templates"},
        "calling":        {"enabled": False, "limit": 0,    "label": "Calling"},
        "flows":          {"enabled": False, "limit": 0,    "label": "Flows"},
        "bulk_messaging": {"enabled": False, "limit": 0,    "label": "Bulk Messaging"},
        "inbox_whatsapp": {"enabled": False, "limit": 0,    "label": "WhatsApp Inbox"},
        "chatbot":        {"enabled": False, "limit": 0,    "label": "Chatbot"},
        "analytics":      {"enabled": True,  "limit": None, "label": "Analytics"},
        "inbox_seats":    {"enabled": False, "limit": 0,    "label": "Inbox Seats"},
    },
}

_PRO_PLAN = {
    "plan_name": "WhatsApp Pro",
    "products": ["WA"],
    "features": {
        "messaging":      {"enabled": True,  "limit": 5000, "label": "Messaging"},
        "templates":      {"enabled": True,  "limit": 25,   "label": "Templates"},
        "calling":        {"enabled": True,  "limit": None, "label": "Calling"},
        # High limit (not a count boundary test anywhere in this file) —
        # a low fixed limit like 3 collides with real WhatsApp Flow rows
        # already on this dev site, since enforce_gate counts site-wide.
        "flows":          {"enabled": True,  "limit": 5000, "label": "Flows"},
        "bulk_messaging": {"enabled": True,  "limit": 5000, "label": "Bulk Messaging"},
        "inbox_whatsapp": {"enabled": True,  "limit": None, "label": "WhatsApp Inbox"},
        "chatbot":        {"enabled": False, "limit": 0,    "label": "Chatbot"},
        "analytics":      {"enabled": True,  "limit": None, "label": "Analytics"},
        "inbox_seats":    {"enabled": True,  "limit": 3,    "label": "Inbox Seats"},
    },
}


@contextmanager
def use_plan(plan: dict):
    """Swap MSuite Settings.plan_data to the given dict for the duration
    of the with-block; restore on exit. Avoids polluting the test site.

    Also drops out of test mode for the body. `is_product_enabled`,
    `is_feature_enabled` and `get_feature_limit` all short-circuit to
    "allow" when `frappe.flags.in_test` is set, and the runner sets it for
    the whole session (frappe/testing/environment.py → toggle_test_mode(True)).
    Without this every gate assertion in this file passes vacuously — an
    `assertRaises(ValidationError)` can't fire when the gate always says yes.

    Safe here because no test in this module writes a real document: they
    pass duck-typed stubs (`_fake_doc`) straight to the gate functions, so
    stepping out of test mode re-arms no side effects. Anything added later
    that does a genuine `.insert()` must not rely on that.
    """
    from frappe.tests.utils import toggle_test_mode

    settings = frappe.get_single("MSuite Settings")
    original = settings.plan_data
    settings.plan_data = json.dumps(plan)
    settings.save(ignore_permissions=True)
    frappe.db.commit()
    # Frappe caches the singleton across requests; force a re-read.
    frappe.clear_document_cache("MSuite Settings", "MSuite Settings")
    toggle_test_mode(False)
    try:
        yield
    finally:
        toggle_test_mode(True)
        settings = frappe.get_single("MSuite Settings")
        settings.plan_data = original
        settings.save(ignore_permissions=True)
        frappe.db.commit()
        frappe.clear_document_cache("MSuite Settings", "MSuite Settings")


# --- Pure-function tests (no DB writes) -----------------------------------


class TestReaders(IntegrationTestCase):
    """is_feature_enabled / get_feature_limit / get_plan_name."""

    def test_is_feature_enabled_reads_plan_data(self):
        with use_plan(_TRIAL_PLAN):
            self.assertTrue(plan_enforcer.is_feature_enabled("messaging"))
            self.assertFalse(plan_enforcer.is_feature_enabled("calling"))

    def test_unknown_feature_is_treated_as_disabled(self):
        with use_plan(_TRIAL_PLAN):
            self.assertFalse(plan_enforcer.is_feature_enabled("nonexistent_xyz"))

    def test_get_feature_limit_returns_none_for_unlimited(self):
        with use_plan(_PRO_PLAN):
            self.assertIsNone(plan_enforcer.get_feature_limit("calling"))

    def test_get_feature_limit_returns_zero_for_disabled(self):
        with use_plan(_TRIAL_PLAN):
            self.assertEqual(plan_enforcer.get_feature_limit("calling"), 0)

    def test_get_feature_limit_returns_int_for_numeric_cap(self):
        with use_plan(_TRIAL_PLAN):
            self.assertEqual(plan_enforcer.get_feature_limit("messaging"), 100)


# --- Gate dispatcher tests ------------------------------------------------


class TestEnforceGate(IntegrationTestCase):
    """The universal `enforce_gate` dispatcher — exercised once per
    registered doctype. We don't insert real docs (that requires
    full fixtures); we synthesise a fake document object and pass it
    in. The registry's skip_if + filters are still exercised."""

    def _fake_doc(self, doctype: str, **fields):
        """Build a duck-typed stand-in for a Frappe Document."""
        class _Stub:
            pass
        d = _Stub()
        d.doctype = doctype
        for k, v in fields.items():
            setattr(d, k, v)
        # is_new() is checked by some skip_if predicates.
        d.is_new = lambda: True
        d.name = None
        return d

    # Boolean-only gate: Messaging Automation
    def test_disabled_feature_blocks_insert(self):
        plan = {
            **_TRIAL_PLAN,
            "features": {
                **_TRIAL_PLAN["features"],
                "notifications": {"enabled": False, "limit": 0, "label": "Notifications"},
            },
        }
        with use_plan(plan):
            doc = self._fake_doc("Messaging Automation", notification_type="DocType Event")
            with self.assertRaises(frappe.ValidationError):
                plan_enforcer.enforce_gate(doc, "before_insert")

    def test_messaging_automation_feature_by_type(self):
        from unittest.mock import patch
        cases = {
            "DocType Event": "notifications",
            "Scheduler Event": "notifications",
            "Lead Status Event": "lead_automation",
            "Order Workflow Event": "order_automation",
        }
        for ntype, feature in cases.items():
            doc = self._fake_doc("Messaging Automation")
            doc.notification_type = ntype
            with patch.object(plan_enforcer, "_enforce") as enforce:
                plan_enforcer.enforce_gate(doc, "before_insert")
            self.assertEqual(enforce.call_args.kwargs["feature_key"], feature, ntype)
            self.assertEqual(enforce.call_args.kwargs["product"], "WA", ntype)

    def test_messaging_automation_gate_runs_on_before_save(self):
        plan = {
            **_TRIAL_PLAN,
            "features": {
                **_TRIAL_PLAN["features"],
                "lead_automation": {"enabled": False, "limit": 0, "label": "Lead Automation"},
            },
        }
        with use_plan(plan):
            doc = self._fake_doc(
                "Messaging Automation",
                triggers=[frappe._dict(trigger_type="Field Change", reference_doctype="CRM Lead")],
            )
            with self.assertRaises(frappe.ValidationError):
                plan_enforcer.enforce_gate(doc, "before_save")

    def test_messaging_automation_gate_returns_feature_per_trigger(self):
        gate = plan_enforcer.get_gates()["Messaging Automation"]
        doc = frappe._dict(
            triggers=[
                frappe._dict(trigger_type="Field Change", reference_doctype="CRM Lead"),
                frappe._dict(trigger_type="Field Change", reference_doctype="Sales Order"),
                frappe._dict(trigger_type="Document Event", reference_doctype="ToDo"),
            ]
        )
        self.assertEqual(sorted(gate.feature_for(doc)), ["lead_automation", "notifications", "order_automation"])

    def test_old_automation_gates_removed(self):
        for dt in ("WhatsApp Notification", "WhatsApp Lead Status Automation", "Order Automation Workflow"):
            self.assertNotIn(dt, plan_enforcer.get_gates())

    def test_enabled_feature_allows_insert(self):
        # Pro has notifications? It doesn't in our fixture, but flows is
        # enabled — use that.
        with use_plan(_PRO_PLAN):
            doc = self._fake_doc("WhatsApp Flow")
            try:
                plan_enforcer.enforce_gate(doc, "before_insert")
            except frappe.ValidationError as e:
                self.fail(f"Pro should allow flows insert, got: {e}")

    # skip_if branch: inbound calls aren't gated even when calling is off
    def test_inbound_call_skips_gate(self):
        from msuite_core.constants import CallDirection
        with use_plan(_TRIAL_PLAN):
            doc = self._fake_doc("WhatsApp Call", direction=CallDirection.INBOUND)
            # Should not raise even though calling=False
            plan_enforcer.enforce_gate(doc, "before_insert")

    def test_outbound_call_is_gated(self):
        from msuite_core.constants import CallDirection
        with use_plan(_TRIAL_PLAN):
            doc = self._fake_doc("WhatsApp Call", direction=CallDirection.OUTBOUND)
            with self.assertRaises(frappe.ValidationError):
                plan_enforcer.enforce_gate(doc, "before_insert")

    # skip_if branch: non-WhatsApp channel messages aren't gated by WA quota
    def test_instagram_message_skips_wa_quota(self):
        with use_plan(_TRIAL_PLAN):
            doc = self._fake_doc("Channel Message",
                                  direction="Outbound", channel_type="Instagram")
            plan_enforcer.enforce_gate(doc, "before_insert")  # no raise

    def test_wa_inbound_message_skips_quota(self):
        with use_plan(_TRIAL_PLAN):
            doc = self._fake_doc("Channel Message",
                                  direction="Inbound", channel_type="WhatsApp")
            plan_enforcer.enforce_gate(doc, "before_insert")  # no raise

    # Unregistered doctype: dispatcher is a no-op
    def test_unregistered_doctype_is_no_op(self):
        with use_plan(_TRIAL_PLAN):
            doc = self._fake_doc("Some Random Doctype")
            plan_enforcer.enforce_gate(doc, "before_insert")  # no raise


# --- Bulk-messaging gate (kept special because it counts the unsaved batch)


class TestBulkMessagingGate(IntegrationTestCase):
    """check_bulk_messaging combines the feature gate with a projected
    usage check (current outbound + this batch's recipients vs cap).
    Trial blocks at the feature gate; Pro blocks only if batch exceeds."""

    def _fake_bulk(self, recipient_count: int = 0):
        class _Stub: pass
        d = _Stub()
        d.doctype = "Bulk WhatsApp Message"
        d.recipients = [{"mobile_number": f"+1555000{i:04}"}
                        for i in range(recipient_count)]
        d.recipient_list = None
        d.name = None
        d.is_new = lambda: True
        return d

    def test_trial_blocks_bulk_outright(self):
        with use_plan(_TRIAL_PLAN):
            doc = self._fake_bulk(recipient_count=5)
            with self.assertRaises(frappe.ValidationError):
                plan_enforcer.check_bulk_messaging(doc, "before_submit")

    def test_pro_allows_bulk_under_cap(self):
        with use_plan(_PRO_PLAN):
            doc = self._fake_bulk(recipient_count=10)
            try:
                plan_enforcer.check_bulk_messaging(doc, "before_submit")
            except frappe.ValidationError as e:
                self.fail(f"Pro should allow 10-recipient bulk, got: {e}")

    def test_pro_blocks_bulk_over_cap(self):
        with use_plan(_PRO_PLAN):
            doc = self._fake_bulk(recipient_count=5001)  # cap is 5000
            with self.assertRaises(frappe.ValidationError):
                plan_enforcer.check_bulk_messaging(doc, "before_submit")

    def test_save_phase_only_checks_feature(self):
        """Before submit the count is irrelevant; only the feature gate
        runs. Pro has the feature so a draft save is fine even when
        empty. Trial doesn't → blocked even on save."""
        with use_plan(_PRO_PLAN):
            plan_enforcer.check_bulk_messaging(self._fake_bulk(), "before_save")
        with use_plan(_TRIAL_PLAN):
            with self.assertRaises(frappe.ValidationError):
                plan_enforcer.check_bulk_messaging(self._fake_bulk(), "before_save")


# --- check_feature_or_throw (API pre-flight pattern) ----------------------


class TestPreflightGate(IntegrationTestCase):
    """check_feature_or_throw is the lighter helper used in API code
    (not bound to a doctype). Same semantics — feature absent/disabled
    raises; enabled passes."""

    def test_disabled_feature_raises(self):
        with use_plan(_TRIAL_PLAN):
            with self.assertRaises(frappe.ValidationError):
                plan_enforcer.check_feature_or_throw("calling")

    def test_enabled_feature_passes(self):
        with use_plan(_PRO_PLAN):
            plan_enforcer.check_feature_or_throw("calling")  # no raise


# --- check_feature_status (whitelisted UI probe) --------------------------


class TestStatusProbe(IntegrationTestCase):
    """The SPA calls `check_feature_status` to decide whether to grey out
    a button. Must return enabled+limit faithfully + skip usage compute
    for disabled features."""

    def test_disabled_returns_zero_limit_zero_usage(self):
        with use_plan(_TRIAL_PLAN):
            status = plan_enforcer.check_feature_status("calling")
        self.assertEqual(status["enabled"], False)
        self.assertEqual(status["limit"], 0)
        self.assertEqual(status["usage"], 0)

    def test_enabled_with_limit_returns_usage(self):
        with use_plan(_PRO_PLAN):
            status = plan_enforcer.check_feature_status("messaging")
        self.assertTrue(status["enabled"])
        self.assertEqual(status["limit"], 5000)
        self.assertIsInstance(status["usage"], int)

    def test_unlimited_feature_returns_null_limit(self):
        with use_plan(_PRO_PLAN):
            status = plan_enforcer.check_feature_status("calling")
        self.assertTrue(status["enabled"])
        self.assertIsNone(status["limit"])


# --- Social Post fixtures + tests -----------------------------------------
#
# Social plan_data mirrors what provider's build_client_plan_data would
# push for SOCIAL-TRIAL / SOCIAL-PRO. Per-platform booleans, the
# inbox-channel gates, and the inbox_seats cross-product key are all
# exercised. If the provider seed and these fixtures drift, treat the
# test failure as a contract break — fix the side that's wrong.


_SOCIAL_TRIAL_PLAN = {
    "plan_name": "Social Post Trial",
    "features": {
        "social_facebook":        {"enabled": True,  "limit": None, "label": "Facebook"},
        "social_instagram":       {"enabled": True,  "limit": None, "label": "Instagram"},
        "social_linkedin":        {"enabled": False, "limit": 0,    "label": "LinkedIn"},
        "social_twitter":         {"enabled": False, "limit": 0,    "label": "Twitter"},
        "social_youtube":         {"enabled": False, "limit": 0,    "label": "YouTube"},
        "social_tiktok":          {"enabled": False, "limit": 0,    "label": "TikTok"},
        "inbox_facebook":         {"enabled": False, "limit": 0,    "label": "FB Inbox"},
        "inbox_instagram":        {"enabled": False, "limit": 0,    "label": "IG Inbox"},
        "social_scheduled_posts": {"enabled": False, "limit": 0,    "label": "Scheduling"},
        "social_video_posts":     {"enabled": False, "limit": 0,    "label": "Video"},
        "social_insights":        {"enabled": False, "limit": 0,    "label": "Insights"},
        "social_ai_assist":       {"enabled": False, "limit": 0,    "label": "AI Assist"},
        "social_bulk_posts":      {"enabled": False, "limit": 0,    "label": "Bulk Upload"},
        "posts_per_month":        {"enabled": True,  "limit": 10,   "label": "Posts/mo"},
        "accounts_per_platform":  {"enabled": True,  "limit": 1,    "label": "Accounts"},
        "media_storage_mb":       {"enabled": True,  "limit": 500,  "label": "Storage"},
        "inbox_seats":            {"enabled": False, "limit": 0,    "label": "Inbox Seats"},
    },
}

_SOCIAL_PRO_PLAN = {
    "plan_name": "Social Post Pro",
    "features": {
        "social_facebook":        {"enabled": True,  "limit": None, "label": "Facebook"},
        "social_instagram":       {"enabled": True,  "limit": None, "label": "Instagram"},
        "social_linkedin":        {"enabled": True,  "limit": None, "label": "LinkedIn"},
        "social_twitter":         {"enabled": True,  "limit": None, "label": "Twitter"},
        "social_youtube":         {"enabled": False, "limit": 0,    "label": "YouTube"},
        "social_tiktok":          {"enabled": False, "limit": 0,    "label": "TikTok"},
        "inbox_facebook":         {"enabled": True,  "limit": None, "label": "FB Inbox"},
        "inbox_instagram":        {"enabled": True,  "limit": None, "label": "IG Inbox"},
        "social_scheduled_posts": {"enabled": True,  "limit": None, "label": "Scheduling"},
        "social_video_posts":     {"enabled": True,  "limit": None, "label": "Video"},
        "social_insights":        {"enabled": True,  "limit": None, "label": "Insights"},
        "social_ai_assist":       {"enabled": True,  "limit": None, "label": "AI Assist"},
        "social_bulk_posts":      {"enabled": False, "limit": 0,    "label": "Bulk Upload"},
        "social_collaborations":  {"enabled": True,  "limit": None, "label": "Collaborations"},
        "posts_per_month":        {"enabled": True,  "limit": 300,  "label": "Posts/mo"},
        "accounts_per_platform":  {"enabled": True,  "limit": 5,    "label": "Accounts"},
        "media_storage_mb":       {"enabled": True,  "limit": 10000, "label": "Storage"},
        "inbox_seats":            {"enabled": True,  "limit": 3,    "label": "Inbox Seats"},
    },
}


class TestSocialPlatformGates(IntegrationTestCase):
    """Per-platform `social_{platform}` keys gate the publish path. Trial
    enables only FB+IG; Pro adds LinkedIn+Twitter; Business+ adds the
    rest. We probe the reader because the publish dispatcher composes the
    key dynamically (`f"social_{post.platform.lower()}"`)."""

    def test_trial_blocks_linkedin(self):
        with use_plan(_SOCIAL_TRIAL_PLAN):
            self.assertFalse(plan_enforcer.is_feature_enabled("social_linkedin"))
            with self.assertRaises(frappe.ValidationError):
                plan_enforcer.check_feature_or_throw("social_linkedin")

    def test_trial_allows_facebook(self):
        with use_plan(_SOCIAL_TRIAL_PLAN):
            self.assertTrue(plan_enforcer.is_feature_enabled("social_facebook"))
            plan_enforcer.check_feature_or_throw("social_facebook")  # no raise

    def test_social_collaborations_is_pro_and_up(self):
        """Trial blocks the collaborations feature; Pro enables it."""
        with use_plan(_SOCIAL_TRIAL_PLAN):
            self.assertFalse(plan_enforcer.is_feature_enabled("social_collaborations"))
        with use_plan(_SOCIAL_PRO_PLAN):
            self.assertTrue(plan_enforcer.is_feature_enabled("social_collaborations"))

    def test_pro_unlocks_linkedin_blocks_youtube(self):
        with use_plan(_SOCIAL_PRO_PLAN):
            self.assertTrue(plan_enforcer.is_feature_enabled("social_linkedin"))
            self.assertFalse(plan_enforcer.is_feature_enabled("social_youtube"))


class TestSocialInboxGates(IntegrationTestCase):
    """`inbox_facebook` / `inbox_instagram` are the keys the inbox
    dispatcher checks per channel. Trial blocks both (no inbox at all);
    Pro unlocks both."""

    def test_trial_blocks_fb_inbox(self):
        with use_plan(_SOCIAL_TRIAL_PLAN):
            self.assertFalse(plan_enforcer.is_feature_enabled("inbox_facebook"))

    def test_trial_blocks_ig_inbox(self):
        with use_plan(_SOCIAL_TRIAL_PLAN):
            self.assertFalse(plan_enforcer.is_feature_enabled("inbox_instagram"))

    def test_pro_unlocks_both_inboxes(self):
        with use_plan(_SOCIAL_PRO_PLAN):
            self.assertTrue(plan_enforcer.is_feature_enabled("inbox_facebook"))
            self.assertTrue(plan_enforcer.is_feature_enabled("inbox_instagram"))


class TestInboxSeatsCrossProduct(IntegrationTestCase):
    """`inbox_seats` is a Numeric feature shared across products. The
    provider's entitlement_service merges grants with max-wins per key,
    so the customer's effective seat cap = max(WA seats, Social seats).
    We test the client-side reader assuming the provider has already
    done the merge — i.e., we just check that the reader returns what's
    in plan_data, not that the client does merging."""

    def test_pro_wa_only_returns_three(self):
        # Customer has only WhatsApp Pro → 3 seats
        with use_plan(_PRO_PLAN):
            self.assertEqual(plan_enforcer.get_feature_limit("inbox_seats"), 3)

    def test_pro_social_only_returns_three(self):
        # Customer has only Social Pro → 3 seats
        with use_plan(_SOCIAL_PRO_PLAN):
            self.assertEqual(plan_enforcer.get_feature_limit("inbox_seats"), 3)

    def test_merged_business_pro_returns_ten(self):
        # Customer with WA Business + Social Pro → provider sends max(10, 3) = 10
        # We simulate the post-merge plan_data the provider would push.
        merged = {
            "plan_name": "WA Business + Social Pro",
            "features": {
                # Boolean: True wins (both products → access)
                "inbox_whatsapp":  {"enabled": True, "limit": None, "label": "WA Inbox"},
                "inbox_facebook":  {"enabled": True, "limit": None, "label": "FB Inbox"},
                "inbox_instagram": {"enabled": True, "limit": None, "label": "IG Inbox"},
                # Numeric: max wins → 10 (WA Business) beats 3 (Social Pro)
                "inbox_seats":     {"enabled": True, "limit": 10,   "label": "Seats"},
            },
        }
        with use_plan(merged):
            self.assertEqual(plan_enforcer.get_feature_limit("inbox_seats"), 10)
            self.assertTrue(plan_enforcer.is_feature_enabled("inbox_whatsapp"))
            self.assertTrue(plan_enforcer.is_feature_enabled("inbox_facebook"))


class TestSocialNewlyGated(IntegrationTestCase):
    """Gates we just wired for previously-ungated Social features.

    These tests stay at the plan_enforcer layer — they confirm the
    helpers behave correctly. The actual integration is at:
      • social_scheduled_posts → composer.schedule (mode != 'now')
      • social_video_posts     → publish dispatcher (_pre_publish_checks)
      • social_bulk_posts      → campaign.bulk_import.import_csv_to_campaign
      • media_storage_mb       → composer.save_step (step='content')
    Each call site routes through `check_feature_or_throw` /
    `check_media_storage_or_throw`, so if those raise correctly under
    the right plan_data, the integration is sound by construction.
    """

    def test_scheduled_posts_blocks_trial(self):
        with use_plan(_SOCIAL_TRIAL_PLAN):
            with self.assertRaises(frappe.ValidationError):
                plan_enforcer.check_feature_or_throw("social_scheduled_posts")

    def test_scheduled_posts_allows_basic(self):
        # Basic enables scheduled_posts but locks the inboxes etc.
        basic_plan = {**_SOCIAL_TRIAL_PLAN, "features": {**_SOCIAL_TRIAL_PLAN["features"]}}
        basic_plan["features"]["social_scheduled_posts"] = \
            {"enabled": True, "limit": None, "label": "Scheduling"}
        with use_plan(basic_plan):
            plan_enforcer.check_feature_or_throw("social_scheduled_posts")  # no raise

    def test_video_posts_blocks_trial(self):
        with use_plan(_SOCIAL_TRIAL_PLAN):
            with self.assertRaises(frappe.ValidationError):
                plan_enforcer.check_feature_or_throw("social_video_posts")

    def test_video_posts_allows_pro(self):
        with use_plan(_SOCIAL_PRO_PLAN):
            plan_enforcer.check_feature_or_throw("social_video_posts")  # no raise

    def test_bulk_posts_blocks_pro(self):
        # bulk_posts is Business+ only; Pro must be blocked
        with use_plan(_SOCIAL_PRO_PLAN):
            with self.assertRaises(frappe.ValidationError):
                plan_enforcer.check_feature_or_throw("social_bulk_posts")

    def test_bulk_posts_allows_business(self):
        # Synthesize a Business plan
        biz_plan = {
            "plan_name": "Social Post Business",
            "features": {
                **_SOCIAL_PRO_PLAN["features"],
                "social_bulk_posts": {"enabled": True, "limit": None, "label": "Bulk"},
                "social_youtube":    {"enabled": True, "limit": None, "label": "YouTube"},
                "social_tiktok":     {"enabled": True, "limit": None, "label": "TikTok"},
            },
        }
        with use_plan(biz_plan):
            plan_enforcer.check_feature_or_throw("social_bulk_posts")  # no raise


class TestMediaStorageGate(IntegrationTestCase):
    """`check_media_storage_or_throw` is the centralised cap check.
    Trial: 500 MB. Pro: 10 GB. We pass `used_mb` directly so the test
    doesn't depend on actual files on disk."""

    def test_trial_blocks_when_delta_exceeds_cap(self):
        with use_plan(_SOCIAL_TRIAL_PLAN):
            # Cap is 500 MB. Currently using 400, attempting to add 200 → blocked.
            with self.assertRaises(frappe.ValidationError):
                plan_enforcer.check_media_storage_or_throw(extra_mb=200, used_mb=400)

    def test_trial_allows_under_cap(self):
        with use_plan(_SOCIAL_TRIAL_PLAN):
            # 400 + 50 = 450 < 500 → allowed.
            plan_enforcer.check_media_storage_or_throw(extra_mb=50, used_mb=400)

    def test_pro_allows_large_upload(self):
        with use_plan(_SOCIAL_PRO_PLAN):
            # Pro: 10 GB cap. 5 GB used, adding 4 GB → allowed.
            plan_enforcer.check_media_storage_or_throw(extra_mb=4000, used_mb=5000)

    def test_unlimited_storage_never_blocks(self):
        ent_plan = {
            "plan_name": "Social Enterprise",
            "features": {
                "media_storage_mb": {"enabled": True, "limit": None, "label": "Storage"},
            },
        }
        with use_plan(ent_plan):
            # Massive delta, no cap → allowed.
            plan_enforcer.check_media_storage_or_throw(extra_mb=999999, used_mb=999999)

    def test_disabled_storage_blocks_any_upload(self):
        # Storage feature off entirely → any extra_mb is rejected.
        no_storage_plan = {
            "plan_name": "Locked",
            "features": {
                "media_storage_mb": {"enabled": False, "limit": 0, "label": "Storage"},
            },
        }
        with use_plan(no_storage_plan):
            with self.assertRaises(frappe.ValidationError):
                plan_enforcer.check_media_storage_or_throw(extra_mb=1, used_mb=0)


class TestRemovedFeaturesAreGone(IntegrationTestCase):
    """Regression guard: confirm the three Social features we deliberately
    removed from the seed don't accidentally creep back as gates anywhere.
    If a future contributor wires `social_approval_workflow` without
    re-adding the feature to seed, this test catches the silent failure
    (a key not in plan_data resolves to disabled, so the gate locks out
    everyone)."""

    REMOVED = (
        "social_carousel_posts",      # given to all tiers by default
        "social_approval_workflow",   # feature dropped; not built yet
        "social_comment_moderation",  # feature dropped; v2 inbox work
    )

    def test_removed_features_resolve_to_disabled_for_any_plan(self):
        # Across Trial AND Pro the readers should return enabled=False
        # for every removed key. (They'd return False whether the key is
        # missing or explicitly disabled — the point is they don't
        # accidentally appear as enabled because some stale fixture
        # still defines them.)
        for plan in (_SOCIAL_TRIAL_PLAN, _SOCIAL_PRO_PLAN):
            with use_plan(plan):
                for key in self.REMOVED:
                    self.assertFalse(
                        plan_enforcer.is_feature_enabled(key),
                        f"{key} unexpectedly enabled under plan {plan['plan_name']}",
                    )


# --- Product gating -------------------------------------------------------


_EMAIL_ONLY_PLAN = {
    "plan_name": "Email Pro",
    "products": ["EMAIL"],
    # `templates` is enabled — but it is EMAIL's templates, not WhatsApp's.
    # This is the whole reason product gating exists; see the collision list
    # in plan_enforcer's "Product gating" block.
    "features": {
        "templates": {"enabled": True, "limit": 50, "label": "Email Templates"},
        "campaigns": {"enabled": True, "limit": None, "label": "Email Campaigns"},
    },
}

# What a provider that predates product gating sends: no `products` key.
_LEGACY_PLAN = {
    "plan_name": "Legacy",
    "features": {"templates": {"enabled": True, "limit": 5, "label": "Templates"}},
}

_SUSPENDED_PLAN = {"plan_name": "No active plan", "products": [], "features": {}}


class TestProductGating(IntegrationTestCase):
    """get_products / is_product_enabled — the coarse gate that runs before
    any feature check."""

    def test_reads_products_from_plan_data(self):
        with use_plan(_PRO_PLAN):
            self.assertEqual(plan_enforcer.get_products(), {"WA"})
            self.assertTrue(plan_enforcer.is_product_enabled("WA"))
            self.assertFalse(plan_enforcer.is_product_enabled("ADS"))

    def test_empty_product_code_is_always_allowed(self):
        # Home, CRM, Settings … have no product in the catalog.
        with use_plan(_SUSPENDED_PLAN):
            self.assertTrue(plan_enforcer.is_product_enabled(""))

    def test_absent_products_key_does_not_gate(self):
        # A provider that hasn't shipped product gating yet must not lock
        # every module. None (not set()) is the "don't gate" signal.
        with use_plan(_LEGACY_PLAN):
            self.assertIsNone(plan_enforcer.get_products())
            self.assertTrue(plan_enforcer.is_product_enabled("ADS"))

    def test_empty_products_list_gates_everything(self):
        # Suspended / no active plan. Distinct from the absent key above.
        with use_plan(_SUSPENDED_PLAN):
            self.assertEqual(plan_enforcer.get_products(), set())
            self.assertFalse(plan_enforcer.is_product_enabled("WA"))

    def test_blank_plan_data_is_locked_not_legacy(self):
        # `connect.sync.suspend` writes "{}". That is a real "you have no
        # plan", so it must lock — NOT be mistaken for an old provider.
        with use_plan({}):
            self.assertEqual(plan_enforcer.get_products(), set())
            self.assertFalse(plan_enforcer.is_product_enabled("WA"))


def _stub_doc(doctype: str, **fields):
    """Duck-typed stand-in for a Frappe Document, same shape as
    TestEnforceGate._fake_doc but callable without an instance."""
    class _Stub:
        pass

    d = _Stub()
    d.doctype = doctype
    for k, v in fields.items():
        setattr(d, k, v)
    d.is_new = lambda: True
    d.name = None
    return d


class TestProductBeatsFeature(IntegrationTestCase):
    """The ordering invariant: a feature key shared across products must not
    unlock a product the customer never bought."""

    def test_email_templates_do_not_unlock_whatsapp_templates(self):
        with use_plan(_EMAIL_ONLY_PLAN):
            # The feature alone says yes...
            self.assertTrue(plan_enforcer.is_feature_enabled("templates"))
            # ...but the WhatsApp product says no, and the product wins.
            with self.assertRaises(frappe.ValidationError):
                plan_enforcer.check_feature_or_throw(
                    "templates", "WhatsApp Templates", product="WA"
                )

    def test_gate_registry_blocks_when_product_missing(self):
        with use_plan(_EMAIL_ONLY_PLAN):
            doc = _stub_doc("WhatsApp Templates")
            with self.assertRaises(frappe.ValidationError):
                plan_enforcer.enforce_gate(doc, "before_insert")

    def test_owned_product_still_honours_feature_gate(self):
        # WA is owned, but Trial has flows disabled — feature gate must
        # still fire. Product passing is not a bypass.
        with use_plan(_TRIAL_PLAN):
            self.assertTrue(plan_enforcer.is_product_enabled("WA"))
            doc = _stub_doc("WhatsApp Flow")
            with self.assertRaises(frappe.ValidationError):
                plan_enforcer.enforce_gate(doc, "before_insert")


class TestGetEntitlements(IntegrationTestCase):
    """The whitelisted probe the SPA calls once at boot."""

    def test_shape(self):
        with use_plan(_PRO_PLAN):
            data = plan_enforcer.get_entitlements()
            self.assertEqual(data["plan_name"], "WhatsApp Pro")
            self.assertEqual(data["products"], ["WA"])
            self.assertTrue(data["features"]["calling"]["enabled"])

    def test_products_is_null_for_legacy_payload(self):
        # The SPA keys off null to mean "show everything".
        with use_plan(_LEGACY_PLAN):
            self.assertIsNone(plan_enforcer.get_entitlements()["products"])


# --- Per-channel bulk keys (bulk_messaging collision fix) -----------------

_WA_PRO_SMS_BASIC_PLAN = {
    "plan_name": "WhatsApp Pro, SMS Basic",
    "products": ["WA", "SMS"],
    "features": {
        "bulk_messaging":     {"enabled": True, "limit": 5000, "label": "Bulk Messaging"},
        "sms_bulk_messaging": {"enabled": True, "limit": None, "label": "Bulk SMS Broadcasts"},
    },
}
_SMS_ONLY_PLAN = {
    "plan_name": "SMS Basic",
    "products": ["SMS"],
    "features": {"sms_bulk_messaging": {"enabled": True, "limit": None, "label": "Bulk SMS Broadcasts"}},
}
_EMAIL_BASIC_PLAN = {
    "plan_name": "Email Basic",
    "products": ["EMAIL"],
    "features": {"email_bulk_messaging": {"enabled": True, "limit": 2500, "label": "Bulk Email Sends"}},
}


class TestBulkPerChannel(IntegrationTestCase):
    def _wa_bulk(self, n):
        return _stub_doc(
            "Bulk WhatsApp Message",
            recipients=[{"mobile_number": f"+1555100{i:04}"} for i in range(n)],
            recipient_list=None,
        )

    def test_sms_plan_does_not_lift_whatsapp_cap(self):
        with use_plan(_WA_PRO_SMS_BASIC_PLAN):
            with self.assertRaises(frappe.ValidationError):
                plan_enforcer.check_bulk_messaging(self._wa_bulk(5001), "before_submit")

    def test_sms_only_cannot_bulk_whatsapp(self):
        with use_plan(_SMS_ONLY_PLAN):
            with self.assertRaises(frappe.ValidationError):
                plan_enforcer.check_bulk_messaging(self._wa_bulk(1), "before_save")

    def test_sms_only_can_bulk_sms(self):
        with use_plan(_SMS_ONLY_PLAN):
            plan_enforcer.check_bulk_sms(_stub_doc("Bulk SMS Message", recipients=[]), "before_save")

    def test_email_cap_is_per_blast(self):
        with use_plan(_EMAIL_BASIC_PLAN):
            plan_enforcer.check_bulk_email(_stub_doc("Bulk Email Message", recipient_count=2500), "before_submit")
            with self.assertRaises(frappe.ValidationError):
                plan_enforcer.check_bulk_email(_stub_doc("Bulk Email Message", recipient_count=2501), "before_submit")

    def test_email_needs_its_own_key(self):
        with use_plan(_WA_PRO_SMS_BASIC_PLAN):
            with self.assertRaises(frappe.ValidationError):
                plan_enforcer.check_bulk_email(_stub_doc("Bulk Email Message", recipient_count=1), "before_save")

    def test_any_channel(self):
        with use_plan(_SMS_ONLY_PLAN):
            self.assertTrue(plan_enforcer.is_bulk_enabled())
            self.assertTrue(plan_enforcer.is_bulk_enabled("sms"))
            self.assertFalse(plan_enforcer.is_bulk_enabled("whatsapp"))
        with use_plan(_SUSPENDED_PLAN):
            self.assertFalse(plan_enforcer.is_bulk_enabled())

    def test_audience_allows_any_bulk_channel(self):
        with use_plan(_SMS_ONLY_PLAN):
            plan_enforcer.enforce_gate(_stub_doc("Audience"), "before_insert")
        with use_plan(_SUSPENDED_PLAN):
            with self.assertRaises(frappe.ValidationError):
                plan_enforcer.enforce_gate(_stub_doc("Audience"), "before_insert")

    def test_recipient_list_is_whatsapp_only(self):
        with use_plan(_SMS_ONLY_PLAN):
            with self.assertRaises(frappe.ValidationError):
                plan_enforcer.check_recipient_list(_stub_doc("WhatsApp Recipient List"), "before_insert")

    def test_hub_gates_each_requested_channel(self):
        from msuite_campaigns.dispatch import hub
        with use_plan(_SMS_ONLY_PLAN):
            hub._check_plan(["sms"])
            with self.assertRaises(frappe.ValidationError):
                hub._check_plan(["sms", "whatsapp"])

    def test_aws_require_feature_is_per_channel(self):
        from msuite_campaigns.aws_pipeline import dispatch
        with use_plan(_SMS_ONLY_PLAN):
            dispatch._require_feature("sms")
            with self.assertRaises(frappe.ValidationError):
                dispatch._require_feature("whatsapp")





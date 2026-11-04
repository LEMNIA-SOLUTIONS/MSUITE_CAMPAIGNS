"""
Audience + unified dispatch hub.

Transport is never exercised — `queue_messages` is patched on both engines, so
these tests assert on what the hub *creates and hands over*, not on sends.

The invariants that matter here:

  1. one audience produces one blast per selected channel, all linked to one
     Marketing Campaign
  2. a member unreachable on one channel is still used on the others — the
     per-channel filter is the whole point of a unified audience
  3. preview and launch agree, because both call the same `members_for`
  4. a channel failing does not roll back the channels that succeeded
  5. blasts created the OLD way keep the OLD enqueue path (the dual-path
     guarantee — this is the test that would catch a regression there)
"""

from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from msuite_campaigns.dispatch import hub
from msuite_core.services.validation import email as email_validator

AUDIENCE = "Audience"


def _members():
	"""A deliberately mixed audience — every rejection reason is represented."""
	return [
		# reachable everywhere
		{"full_name": "Both", "raw_phone": "9876543210", "raw_email": "both@example.com"},
		# mobile, but email is invalid → WhatsApp/SMS only
		{"full_name": "PhoneOnly", "raw_phone": "9812345678", "raw_email": "bad@example.com"},
		# landline: Valid number, but cannot receive SMS or WhatsApp → email only
		{"full_name": "Landline", "raw_phone": "+912223456789", "raw_email": "land@example.com"},
		# no phone at all → email only
		{"full_name": "EmailOnly", "raw_phone": "", "raw_email": "only@example.com"},
		# unparseable phone, no email → reachable nowhere
		{"full_name": "Neither", "raw_phone": "junk", "raw_email": ""},
	]


_MV = {
	"both@example.com": {"result": "ok"},
	"bad@example.com": {"result": "invalid"},
	"land@example.com": {"result": "ok"},
	"only@example.com": {"result": "catch_all"},
}


def _ensure(doctype: str, filters: dict, values: dict) -> str:
	"""Find-or-create a fixture doc. Both engines mark `template` (and email's
	`email_account`) as reqd, so the hub cannot be exercised without them.

	Looked up by field filters rather than by name: several of these doctypes
	autoname from a slugified field ("dispatch-test" becomes
	"dispatch_test-en_US"), so guessing the resulting name is fragile.
	"""
	existing = frappe.db.get_value(doctype, filters, "name")
	if existing:
		return existing
	doc = frappe.get_doc({"doctype": doctype, **values})
	doc.flags.ignore_permissions = True
	doc.insert()
	return doc.name


# Every audience these tests create carries this prefix, and cleanup only
# ever touches rows that match it.
#
# An earlier version deleted EVERY Audience row in setUp. Because the
# bulk engines commit (the two-phase queue flip has to be visible to workers),
# those deletes were not rolled back — running the suite destroyed real
# audiences on the site. Tests clean up what they create, nothing else.
TEST_PREFIX = "ZZTest "


class DispatchTestBase(IntegrationTestCase):
	def setUp(self):
		self._purge_test_audiences()
		self.audience = self._make_audience()
		self._make_fixtures()
		self.addCleanup(self._purge_test_audiences)

	@staticmethod
	def _purge_test_audiences():
		for name in frappe.get_all(
			AUDIENCE, filters={"audience_name": ["like", f"{TEST_PREFIX}%"]}, pluck="name"
		):
			frappe.delete_doc(AUDIENCE, name, force=True, ignore_permissions=True)
		frappe.db.commit()

	def _make_fixtures(self):
		self.wa_template = _ensure(
			"WhatsApp Templates",
			{"template_name": "dispatch_test"},
			{
				"template_name": "dispatch_test",
				"actual_name": "dispatch_test",
				"template": "Hello {{1}}, welcome to our platform! We are excited to have you with us.",
				# `language` links to Frappe's Language doctype, whose names use
				# a hyphen ("en-US"); `language_code` is the Meta-side string.
				"language": "en-US",
				"language_code": "en_US",
				# Required whenever the body carries {{n}} placeholders.
				"sample_values": "Test",
				"category": "MARKETING",
				"status": "APPROVED",
			},
		)
		self.email_account = _ensure(
			"Email Account",
			{"email_id": "dispatch-test@example.com"},
			{
				"email_id": "dispatch-test@example.com",
				"email_account_name": "dispatch-test@example.com",
				"enable_incoming": 0,
				"enable_outgoing": 0,
				# Custom "MSuite Status" field (install.py) — the signal
				# `bulk_email_message.validate_template_and_account` gates
				# submit on. Unset here reads as "Not Set", which the check
				# correctly treats as unusable, same as a real disconnected
				# mailbox would be.
				"status": "Active",
			},
		)
		self.email_template = _ensure(
			"Email Template",
			{"name": "Dispatch Test Template"},
			{
				"name": "Dispatch Test Template",
				"subject": "Hello {{ recipient_name }}",
				"response": "<p>Hi {{ recipient_name }}</p>",
				"use_html": 1,
				"category": "Marketing",
				"status": "Active",
			},
		)

	def _make_audience(self, name=f"{TEST_PREFIX}Audience"):
		doc = frappe.get_doc({"doctype": AUDIENCE, "audience_name": name, "default_region": "IN"})
		doc.flags.ignore_permissions = True
		doc.insert()
		doc.add_members(_members())
		doc.save()
		with patch.object(
			email_validator, "_call_relay", side_effect=lambda e, p: {"results": _MV, "credits": 100}
		):
			doc.validate_members()
		doc.reload()
		return doc


class TestAudienceReachability(DispatchTestBase):
	def test_members_are_parsed_to_e164_on_add(self):
		by_name = {m.full_name: m for m in self.audience.members}
		self.assertEqual(by_name["Both"].phone_e164, "+919876543210")
		self.assertEqual(by_name["Landline"].phone_type, "Fixed Line")
		self.assertEqual(by_name["Neither"].phone_status, "Unparseable")

	def test_whatsapp_excludes_landline_and_phoneless(self):
		names = {m["full_name"] for m in self.audience.members_for("WhatsApp")}
		self.assertEqual(names, {"Both", "PhoneOnly"})

	def test_email_excludes_invalid_but_keeps_catch_all(self):
		names = {m["full_name"] for m in self.audience.members_for("Email")}
		# bad@ is invalid → dropped. only@ is catch_all → kept by default policy.
		self.assertEqual(names, {"Both", "Landline", "EmailOnly"})

	def test_strict_policy_drops_catch_all(self):
		names = {
			m["full_name"]
			for m in self.audience.members_for("Email", {"email_accept": ["ok"]})
		}
		self.assertEqual(names, {"Both", "Landline"})

	def test_the_same_member_can_be_reachable_on_one_channel_only(self):
		wa = {m["full_name"] for m in self.audience.members_for("WhatsApp")}
		em = {m["full_name"] for m in self.audience.members_for("Email")}
		self.assertIn("PhoneOnly", wa)
		self.assertNotIn("PhoneOnly", em)
		self.assertIn("Landline", em)
		self.assertNotIn("Landline", wa)

	def test_per_channel_unsubscribe_is_independent(self):
		member = next(m for m in self.audience.members if m.full_name == "Both")
		member.unsubscribed_email = 1
		self.audience.save()
		self.audience.reload()
		self.assertNotIn("Both", {m["full_name"] for m in self.audience.members_for("Email")})
		self.assertIn("Both", {m["full_name"] for m in self.audience.members_for("WhatsApp")})

	def test_rejection_breakdown_explains_every_drop(self):
		reasons = self.audience.rejection_breakdown("WhatsApp")
		self.assertEqual(sum(reasons.values()), 3)  # Landline, EmailOnly, Neither
		self.assertTrue(any("cannot receive" in k for k in reasons))

	def test_dedupe_collapses_differently_formatted_numbers(self):
		before = len(self.audience.members)
		added = self.audience.add_members(
			[{"full_name": "Dup", "raw_phone": "+91 98765 43210", "raw_email": "both@example.com"}]
		)
		self.assertEqual(added, 0)
		self.assertEqual(len(self.audience.members), before)

	def test_valid_status_but_empty_address_is_dropped_and_explained(self):
		"""A phone_status=Valid row whose phone_e164 was cleared out from
		under it (data desync — the class of gap `_attach_recipients` in the
		materializer used to drop silently) must be excluded by
		`members_for()` AND accounted for by `rejection_breakdown()`, not
		just quietly vanish at materialize time."""
		member = next(m for m in self.audience.members if m.full_name == "Both")
		member.phone_e164 = ""
		self.audience.save()
		self.audience.reload()

		names = {m["full_name"] for m in self.audience.members_for("WhatsApp")}
		self.assertNotIn("Both", names)

		reasons = self.audience.rejection_breakdown("WhatsApp")
		self.assertIn("No phone number", reasons)

	def test_rollups_count_reachability_not_validity(self):
		# Landline is a Valid number but not reachable → excluded from the
		# phone rollup, included in the email one.
		self.assertEqual(self.audience.valid_phone_count, 2)
		self.assertEqual(self.audience.valid_email_count, 3)


class TestDispatchHub(DispatchTestBase):
	def setUp(self):
		super().setUp()
		self.wa_patch = patch(
			"msuite_whatsapp.services.bulk.bulk_send_service.queue_messages"
		)
		self.em_patch = patch(
			"msuite_email.msuite_email.services.email_bulk_send_service.queue_messages"
		)
		self.wa_queue = self.wa_patch.start()
		self.em_queue = self.em_patch.start()
		self.addCleanup(self.wa_patch.stop)
		self.addCleanup(self.em_patch.stop)

	def _payload(self, **over):
		payload = {
			"campaign_name": "Diwali Test",
			"audience": self.audience.name,
			"channels": {
				"whatsapp": {"template": self.wa_template},
				"email": {"template": self.email_template, "account": self.email_account},
			},
		}
		payload.update(over)
		return payload

	def test_missing_template_is_refused_before_anything_is_written(self):
		before = frappe.db.count("Marketing Campaign")
		with self.assertRaises(frappe.ValidationError):
			hub.launch(self._payload(channels={"whatsapp": {}}))
		self.assertEqual(frappe.db.count("Marketing Campaign"), before)

	def test_available_channels_are_discovered_not_hard_coded(self):
		"""Updated in Phase 3.

		Through Phase 2 this asserted SMS was absent. Phase 3 installed the
		SMS module and it appeared here with no change to `hub.py` or the
		wizard — which is the property this test actually exists to protect:
		channel availability is derived from whether the bulk doctype exists.
		"""
		channels = hub.available_channels()
		self.assertEqual(channels, ["whatsapp", "sms", "email"])
		for key, doctype in (
			("whatsapp", "Bulk WhatsApp Message"),
			("sms", "Bulk SMS Message"),
			("email", "Bulk Email Message"),
		):
			self.assertTrue(frappe.db.exists("DocType", doctype), f"{key} doctype missing")

	def test_selecting_an_unavailable_channel_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			hub.preview(self._payload(channels={"telepathy": {}}))

	def test_preview_writes_nothing(self):
		before = frappe.db.count("Bulk WhatsApp Message"), frappe.db.count("Marketing Campaign")
		hub.preview(self._payload())
		after = frappe.db.count("Bulk WhatsApp Message"), frappe.db.count("Marketing Campaign")
		self.assertEqual(before, after)

	def test_preview_counts_match_launch_counts(self):
		preview = hub.preview(self._payload())
		result = hub.launch(self._payload())
		for channel in ("whatsapp", "email"):
			self.assertEqual(
				preview["channels"][channel]["queued"],
				result["channels"][channel]["queued"],
				f"preview and launch disagree on {channel}",
			)

	def test_launch_creates_one_blast_per_channel_under_one_campaign(self):
		result = hub.launch(self._payload())

		self.assertIsNotNone(result["channels"]["whatsapp"]["name"])
		self.assertIsNotNone(result["channels"]["email"]["name"])

		campaign = result["campaign"]
		wa = frappe.get_doc("Bulk WhatsApp Message", result["channels"]["whatsapp"]["name"])
		em = frappe.get_doc("Bulk Email Message", result["channels"]["email"]["name"])
		self.assertEqual(wa.campaign, campaign)
		self.assertEqual(em.campaign, campaign)
		self.assertEqual(wa.audience, self.audience.name)

		linked = frappe.db.get_value(
			"Marketing Campaign", campaign, ["whatsapp_blast", "email_blast"], as_dict=True
		)
		self.assertEqual(linked.whatsapp_blast, wa.name)
		self.assertEqual(linked.email_blast, em.name)

	def test_each_channel_gets_only_its_reachable_members(self):
		result = hub.launch(self._payload())
		self.assertEqual(result["channels"]["whatsapp"]["queued"], 2)
		self.assertEqual(result["channels"]["email"]["queued"], 3)

		wa = frappe.get_doc("Bulk WhatsApp Message", result["channels"]["whatsapp"]["name"])
		self.assertEqual(
			{r.mobile_number for r in wa.recipients}, {"+919876543210", "+919812345678"}
		)

	def test_recipient_data_carries_through_for_merge_tags(self):
		self.audience.add_members(
			[{"full_name": "Tagged", "raw_phone": "9811111111", "recipient_data": {"city": "Pune"}}]
		)
		self.audience.save()
		result = hub.launch(self._payload())
		wa = frappe.get_doc("Bulk WhatsApp Message", result["channels"]["whatsapp"]["name"])
		row = next(r for r in wa.recipients if r.mobile_number == "+919811111111")
		self.assertIn("Pune", row.recipient_data)

	def test_hub_created_blasts_use_the_batched_path(self):
		result = hub.launch(self._payload())
		wa = frappe.get_doc("Bulk WhatsApp Message", result["channels"]["whatsapp"]["name"])
		em = frappe.get_doc("Bulk Email Message", result["channels"]["email"]["name"])
		self.assertEqual(wa.use_batched_enqueue, 1)
		self.assertEqual(em.use_batched_enqueue, 1)

	def test_blasts_created_the_old_way_keep_the_old_path(self):
		"""The dual-path guarantee. A blast made without the hub must not
		acquire the batched behaviour just because the field now exists."""
		doc = frappe.get_doc(
			{
				"doctype": "Bulk WhatsApp Message",
				"title": "Legacy blast",
				"template": self.wa_template,
				"recipient_type": "Individual",
				"recipients": [{"mobile_number": "+919876543210", "recipient_name": "Legacy"}],
			}
		)
		doc.flags.ignore_permissions = True
		doc.insert()
		self.assertEqual(doc.use_batched_enqueue, 0)
		self.assertFalse(doc.audience)

	def test_launch_is_refused_without_an_audience(self):
		with self.assertRaises(frappe.ValidationError):
			hub.launch({"channels": {"whatsapp": {}}})

	def test_launch_is_refused_without_a_channel(self):
		with self.assertRaises(frappe.ValidationError):
			hub.launch({"audience": self.audience.name, "channels": {}})

	def test_one_channel_failing_does_not_undo_the_others(self):
		from msuite_campaigns.dispatch import materializer

		real_build = materializer.build

		def selective(channel, campaign, audience_doc, config):
			if channel == "whatsapp":
				raise ValueError("simulated WhatsApp failure")
			return real_build(channel, campaign, audience_doc, config)

		with patch.object(materializer, "build", side_effect=selective):
			result = hub.launch(self._payload())

		self.assertEqual(result["channels"]["whatsapp"]["status"], "Failed")
		self.assertIsNotNone(
			result["channels"]["email"]["name"], "email must survive a WhatsApp failure"
		)
		self.assertTrue(frappe.db.exists("Bulk Email Message", result["channels"]["email"]["name"]))

	def test_preview_and_build_agree_when_address_is_missing_despite_valid_status(self):
		"""Regression for the materializer/audience divergence: a member
		whose phone_status is Valid but whose phone_e164 is empty (data
		desync) must be dropped consistently by `hub.preview()` (which
		calls `members_for`/`rejection_breakdown`) and `materializer.build()`
		(which calls the same two functions) — same `dropped` count, same
		reason. `hub.launch()` isn't used here because it re-runs
		`validate_members()`, which would re-derive `phone_e164` from
		`raw_phone` and heal the very desync this test injects.
		"""
		from msuite_campaigns.dispatch import materializer

		member = next(m for m in self.audience.members if m.full_name == "Both")
		member.phone_e164 = ""
		self.audience.save()
		self.audience.reload()

		preview = hub.preview(self._payload())
		result = materializer.build("whatsapp", None, self.audience, {"template": self.wa_template})

		self.assertEqual(preview["channels"]["whatsapp"]["dropped"], result["dropped"])
		self.assertIn("No phone number", result["reasons"])
		self.assertIn("No phone number", preview["channels"]["whatsapp"]["reasons"])

	def test_reusing_an_existing_marketing_campaign(self):
		campaign = frappe.get_doc(
			{
				"doctype": "Marketing Campaign",
				"campaign_name": "Existing",
				"status": "Active",
				"start_date": frappe.utils.now_datetime(),
				"end_date": frappe.utils.add_to_date(frappe.utils.now_datetime(), days=7),
			}
		)
		campaign.flags.ignore_permissions = True
		campaign.insert()

		result = hub.launch(self._payload(marketing_campaign=campaign.name))
		self.assertEqual(result["campaign"], campaign.name)

	def test_channel_with_no_reachable_members_is_skipped_not_failed(self):
		empty = frappe.get_doc(
			{"doctype": AUDIENCE, "audience_name": f"{TEST_PREFIX}Empty", "default_region": "IN"}
		)
		empty.flags.ignore_permissions = True
		empty.insert()
		empty.add_members([{"full_name": "NoPhone", "raw_phone": "", "raw_email": "x@example.com"}])
		empty.save()

		with patch.object(
			email_validator, "_call_relay", side_effect=lambda e, p: {"results": {"x@example.com": {"result": "ok"}}}
		):
			result = hub.launch(self._payload(audience=empty.name))

		self.assertEqual(result["channels"]["whatsapp"]["status"], "Skipped")
		self.assertEqual(result["channels"]["whatsapp"]["queued"], 0)
		self.assertEqual(result["channels"]["email"]["queued"], 1)

	def test_prepare_creates_campaign_with_preparing_status(self):
		payload = self._payload()
		prepared = hub.prepare(payload)
		self.assertIn("campaign", prepared)
		self.assertIn("channels", prepared)
		self.assertEqual(prepared["audience"], payload["audience"])

		status = hub.get_launch_status(prepared["campaign"])
		self.assertEqual(status["launch_status"], "Preparing")

	def test_launch_async_updates_status_and_links(self):
		payload = self._payload()
		prepared = hub.prepare(payload)
		campaign = prepared["campaign"]

		with patch("msuite_whatsapp.services.bulk.bulk_send_service.queue_messages"), \
		     patch("msuite_email.msuite_email.services.email_bulk_send_service.queue_messages"):
			hub.launch_async(payload, campaign)

		status = hub.get_launch_status(campaign)
		self.assertEqual(status["launch_status"], "Launched")
		self.assertEqual(frappe.db.get_value("Marketing Campaign", campaign, "status"), "In Progress")
		self.assertGreater(status["queued_total"], 0)
		self.assertTrue(status.get("whatsapp_blast") or status.get("email_blast"))

	def test_launch_failure_sets_failed_status_and_error_message(self):
		payload = self._payload()
		prepared = hub.prepare(payload)
		campaign = prepared["campaign"]

		# Simulate channel failure
		with patch("msuite_campaigns.dispatch.hub._launch_one", return_value={"status": "Failed", "error": "AWS not configured"}):
			hub.launch_async(payload, campaign)

		campaign_doc = frappe.get_doc("Marketing Campaign", campaign)
		self.assertEqual(campaign_doc.status, "Failed")
		self.assertEqual(campaign_doc.launch_status, "Failed")
		self.assertIn("AWS not configured", campaign_doc.launch_error)

	def test_get_broadcast_detail_aggregates_campaign_and_channel_metadata(self):
		from msuite_campaigns.audience.api.v1 import audience as audience_api

		payload = self._payload()
		with patch("msuite_whatsapp.services.bulk.bulk_send_service.queue_messages"), \
		     patch("msuite_email.msuite_email.services.email_bulk_send_service.queue_messages"):
			result = hub.launch(payload)

		campaign = result["campaign"]
		detail = audience_api.get_broadcast_detail(campaign)

		self.assertEqual(detail["name"], campaign)
		self.assertEqual(detail["status"], "In Progress")
		self.assertEqual(detail["audience"], payload["audience"])
		self.assertIn("dispatch_test-en_US", detail["template"])
		self.assertGreater(detail["recipient_count"], 0)
		self.assertEqual(len(detail["recipients"]), detail["recipient_count"])
		self.assertEqual(detail["channel"], "Unified Broadcast")
		self.assertTrue(len(detail["channel_breakdowns"]) >= 2)

		# Channel filtering test
		wa_detail = audience_api.get_broadcast_detail(campaign, channel="whatsapp")
		self.assertTrue(all(r["channel"] == "WhatsApp" for r in wa_detail["recipients"]))

		em_detail = audience_api.get_broadcast_detail(campaign, channel="email")
		self.assertTrue(all(r["channel"] == "Email" for r in em_detail["recipients"]))

	def test_broadcast_detail_reads_aws_metrics_by_campaign_and_channel_key(self):
		"""The exact live bug: `_get_marketing_campaign_detail` used to call
		`aws_rollup.fetch(blast_name)` -- a key nothing in the analytics table
		is ever written under (see `manifest._dynamo_campaign_id` /
		`rollup.dynamo_key`) -- so a genuinely-delivered AWS blast still
		rendered Sent/Delivered as 0 on the broadcast detail page."""
		from msuite_campaigns.audience.api.v1 import audience as audience_api
		from msuite_campaigns.aws_pipeline import rollup as aws_rollup

		payload = self._payload()
		with patch("msuite_whatsapp.services.bulk.bulk_send_service.queue_messages"), \
		     patch("msuite_email.msuite_email.services.email_bulk_send_service.queue_messages"):
			result = hub.launch(payload)

		campaign = result["campaign"]
		mc = frappe.get_doc("Marketing Campaign", campaign)
		frappe.db.set_value("Bulk WhatsApp Message", mc.whatsapp_blast, "delivery_engine", "AWS")
		frappe.db.set_value("Bulk Email Message", mc.email_blast, "delivery_engine", "AWS")

		seen_keys = []

		def fake_fetch(key):
			seen_keys.append(key)
			if "whatsapp" in key:
				return {"sent_count": 5, "delivered_count": 4, "failed_count": 0, "read_count": 3}
			elif "email" in key:
				return {"sent_count": 5, "delivered_count": 5, "failed_count": 0, "read_count": 2}
			return {"sent_count": 0, "delivered_count": 0, "failed_count": 0, "read_count": 0}

		with patch.object(aws_rollup, "fetch", side_effect=fake_fetch):
			detail = audience_api.get_broadcast_detail(campaign)

		self.assertIn(f"{campaign}__whatsapp", seen_keys)
		self.assertIn(f"{campaign}__email", seen_keys)
		wa = next(c for c in detail["channel_breakdowns"] if c["channel"] == "WhatsApp")
		self.assertEqual(wa["sent"], 5)
		self.assertEqual(wa["delivered"], 4)
		self.assertEqual(wa["read"], 3)
		em = next(c for c in detail["channel_breakdowns"] if c["channel"] == "Email")
		self.assertEqual(em["sent"], 5)
		self.assertEqual(em["delivered"], 5)
		self.assertEqual(em["opened"], 2)

	def test_whatsapp_template_variable_resolution_formats(self):
		import json
		from msuite_whatsapp.services.bulk.bulk_send_service import build_body_param, _parse_recipient_data

		recipient_row = {
			"recipient_name": "Giorgi Kubiczek",
			"mobile_number": "98765432101",
			"recipient_data": json.dumps({"course": "Computer Science"}),
		}
		recipient_data = _parse_recipient_data(recipient_row)

		# Format 1: UnifiedBroadcastWizard dict format (type: 'field' & type: 'static')
		doc = frappe._dict({
			"template": self.wa_template,
			"template_variables": json.dumps({
				"1": {"type": "field", "value": "recipient_name"},
				"2": {"type": "static", "value": "Example University"},
			}),
		})
		body_param = json.loads(build_body_param(doc, recipient_data))
		self.assertEqual(body_param.get("1"), "Giorgi Kubiczek")
		self.assertEqual(body_param.get("2"), "Example University")

		# Format 2: Classic wizard list format (mode: 'unique' & mode: 'common')
		doc_list = frappe._dict({
			"template": self.wa_template,
			"template_variables": json.dumps([
				{"mode": "unique", "key": "recipient_name"},
				{"mode": "common", "value": "Example University"},
			]),
		})
		body_param_list = json.loads(build_body_param(doc_list, recipient_data))
		self.assertEqual(body_param_list.get("1"), "Giorgi Kubiczek")
		self.assertEqual(body_param_list.get("2"), "Example University")


class TestBatchedEnqueueRowParent(IntegrationTestCase):
	"""`dispatch/batched_enqueue.py` is shared by all three channels. It used
	to assume every recipient row is parented to the blast itself, which is
	false for a WhatsApp "Recipient List" blast (rows live under the shared
	`WhatsApp Recipient List`), so batched enqueue found zero rows and sent
	nothing.

	This is the SMS/Email side of that fix: their rows are ALWAYS parented to
	the blast, so parent resolution must still produce exactly
	`parent = doc.name, parenttype = doctype` — the same query as before.
	"""

	def test_sms_and_email_resolve_to_the_blast_itself(self):
		from msuite_campaigns.dispatch.batched_enqueue import _row_parent

		for doctype in ("Bulk SMS Message", "Bulk Email Message"):
			with self.subTest(doctype=doctype):
				self.assertEqual(_row_parent(doctype, "BLAST-0001"), ("BLAST-0001", doctype))

	def test_sms_slice_query_is_unchanged(self):
		"""The filters handed to the row query for a non-WhatsApp blast must
		be byte-identical to the pre-fix hardcoded ones."""
		from msuite_campaigns.dispatch import batched_enqueue

		with patch.object(batched_enqueue.frappe, "get_all", return_value=[]) as get_all:
			batched_enqueue._slice_recipients("Bulk SMS Message", "BLAST-0001", 1, 500)

		filters = get_all.call_args.kwargs["filters"]
		self.assertEqual(filters["parent"], "BLAST-0001")
		self.assertEqual(filters["parenttype"], "Bulk SMS Message")
		self.assertEqual(filters["status"], ["in", batched_enqueue.SENDABLE_STATUSES])
		self.assertEqual(filters["idx"], ["between", [1, 500]])

	def test_whatsapp_individual_blast_still_resolves_to_the_blast(self):
		"""Only Recipient List mode diverges — an Individual WhatsApp blast
		owns its rows exactly like SMS/Email."""
		from msuite_campaigns.dispatch import batched_enqueue

		with patch.object(
			batched_enqueue.frappe.db,
			"get_value",
			return_value={"recipient_type": "Individual", "recipient_list": None},
		):
			self.assertEqual(
				batched_enqueue._row_parent("Bulk WhatsApp Message", "BULK-WA-0001"),
				("BULK-WA-0001", "Bulk WhatsApp Message"),
			)


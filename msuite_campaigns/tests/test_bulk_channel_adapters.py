"""The bulk engines reach each channel through its campaign adapter. These run that path on real rows."""

import json
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from msuite_campaigns.aws_pipeline import dispatch, manifest
from msuite_campaigns.campaign.services.relationships import get_adapters, get_bulk_adapter
from msuite_campaigns.dispatch import batched_enqueue

WHATSAPP, SMS, EMAIL = "Bulk WhatsApp Message", "Bulk SMS Message", "Bulk Email Message"


def bulk(doctype, name, **values):
	return frappe._dict(doctype=doctype, name=name, **values)


def recipient(doctype, parent, parenttype, **values):
	row = frappe.get_doc({"doctype": doctype, "parent": parent, "parenttype": parenttype, "parentfield": "recipients", **values})
	row.db_insert()
	return row


def inbox(channel_type):
	name = f"ZZ Bulk {channel_type}"
	if not frappe.db.exists("Inbox", name):
		frappe.get_doc({"doctype": "Inbox", "name": name, "inbox_name": name, "channel_type": channel_type, "status": "Active"}).db_insert()
	return name


class TestAdapterShape(IntegrationTestCase):
	def test_each_channel_offers_what_the_engines_call(self):
		for doctype in (WHATSAPP, SMS, EMAIL):
			adapter = get_bulk_adapter(doctype)
			self.assertTrue(adapter, doctype)
			for key in ("manifest_header", "manifest_row", "send_one", "record_outbound"):
				self.assertTrue(callable(adapter.get(key)), f"{doctype}: {key}")

	def test_a_doctype_no_channel_sends_has_no_adapter(self):
		self.assertIsNone(get_bulk_adapter("Note"))

	def test_only_whatsapp_keeps_rows_away_from_the_blast(self):
		self.assertEqual(batched_enqueue._row_parent(SMS, "B-1"), ("B-1", SMS))
		self.assertEqual(batched_enqueue._row_parent(EMAIL, "B-1"), ("B-1", EMAIL))
		self.assertEqual(manifest.row_parent(bulk(SMS, "B-1")), ("B-1", SMS))

	def test_the_manifest_refuses_a_channel_that_is_not_installed(self):
		with self.assertRaises(frappe.ValidationError):
			manifest._channel("Note")


class TestSendOne(IntegrationTestCase):
	def test_each_channel_receives_its_own_row(self):
		row = {"name": "R-1", "mobile_number": "+919800000001", "email_address": " A@B.co ", "recipient_name": "A", "recipient_data": "{}"}
		with patch("msuite_whatsapp.services.bulk.bulk_send_service.create_single_message") as whatsapp:
			get_bulk_adapter(WHATSAPP)["send_one"](bulk(WHATSAPP, "B-1"), row)
		self.assertEqual(whatsapp.call_args.args[1]["mobile_number"], "+919800000001")
		with patch("msuite_sms.sms.services.bulk_send_service.send_single_message_job") as sms:
			get_bulk_adapter(SMS)["send_one"](bulk(SMS, "B-1"), row)
		self.assertEqual(sms.call_args.kwargs["mobile_number"], "+919800000001")
		with patch("msuite_email.msuite_email.services.email_bulk_send_service.send_single_message_job") as email:
			get_bulk_adapter(EMAIL)["send_one"](bulk(EMAIL, "B-1"), row)
		self.assertEqual(email.call_args.kwargs["recipient_email"], "a@b.co")

	def test_run_chunk_uses_the_adapter(self):
		send = MagicMock()
		rows = [{"name": "R-1"}, {"name": "R-2"}]
		with (
			patch.dict(get_bulk_adapter(SMS), {"send_one": send}),
			patch.object(batched_enqueue, "load_parent", return_value=bulk(SMS, "B-1")),
			patch.object(batched_enqueue, "_slice_recipients", return_value=rows),
			patch("frappe.db.get_value", return_value=0),
		):
			batched_enqueue.run_chunk(SMS, "B-1", 1, 2)
		self.assertEqual(send.call_count, 2)


class TestManifest(IntegrationTestCase):
	def test_whatsapp(self):
		account = MagicMock()
		doc = bulk(WHATSAPP, "B-1", whatsapp_account="ACC", template="T", template_variables="", attach="", campaign="", is_carousel_template=0)
		doc.get = dict.get.__get__(doc)
		with (
			patch("msuite_whatsapp.queries.account_repository.get_account_doc", return_value=account),
			patch("msuite_whatsapp.queries.account_repository.get_phone_number_id", return_value="PN"),
			patch("msuite_whatsapp.services.channels.inbox_channel._build_send_body", return_value={"to": "x"}) as build,
			patch("msuite_whatsapp.services.bulk.bulk_send_service.get_mpm_action_json", return_value=None),
		):
			header, lines = manifest.build(doc, [{"name": "R-1", "mobile_number": "+919800000001", "recipient_name": "A", "recipient_data": "{}"}])
			lines = [json.loads(line) for line in lines]
		self.assertEqual((header["channel"], header["phone_number_id"]), ("whatsapp", "PN"))
		self.assertEqual(lines[0], header)
		self.assertEqual(lines[1]["meta_body"], {"to": "x"})
		self.assertEqual(build.call_args.args[0], "+919800000001")

	def test_sms(self):
		doc = bulk(SMS, "B-1", sms_account="ACC", template="T", campaign="")
		template = frappe._dict(template_name="T", dlt_template_id="D1", dlt_entity_id="", sender_id="S", body="Hi {{name}}")
		account = frappe._dict(gateway="X", base_url="", sender_id="", dlt_entity_id="")
		with (
			patch("frappe.get_doc", return_value=template),
			patch("frappe.get_cached_doc", return_value=template),
			patch("msuite_sms.sms.doctype.sms_account.sms_account.require_account", return_value=account),
			patch("msuite_sms.sms.providers.requires_dlt", return_value=True),
			patch("msuite_sms.sms.services.bulk_send_service._merge_variables", return_value={"name": "A"}),
		):
			header, lines = manifest.build(doc, [{"name": "R-1", "mobile_number": " +919800000001 ", "recipient_name": "A"}])
			lines = [json.loads(line) for line in lines]
		self.assertEqual((header["channel"], header["dlt_template_id"]), ("sms", "D1"))
		self.assertEqual((lines[1]["to"], lines[1]["variables"]), ("+919800000001", {"name": "A"}))

	def test_sms_refuses_a_template_the_gateway_would_reject(self):
		doc = bulk(SMS, "B-1", sms_account="ACC", template="T", campaign="")
		with (
			patch("frappe.get_doc", return_value=frappe._dict(template_name="T", dlt_template_id="")),
			patch("msuite_sms.sms.doctype.sms_account.sms_account.require_account", return_value=frappe._dict(gateway="X")),
			patch("msuite_sms.sms.providers.requires_dlt", return_value=True),
			self.assertRaises(frappe.ValidationError),
		):
			manifest.build(doc, [])

	def test_email(self):
		doc = bulk(EMAIL, "B-1", template="T", email_account="ACC", subject_override="", from_name="F", campaign="")
		template = frappe._dict(subject="Sub", name="T", response_html="<b>x</b>", response="x")
		template.get = dict.get.__get__(template)
		account = frappe._dict(email_id="a@b.co")
		with (
			patch("frappe.db.exists", return_value=True),
			patch("frappe.get_doc", side_effect=[template, account]),
			patch("msuite_email.msuite_email.services.email_bulk_send_service._make_token", return_value="tok"),
		):
			header, lines = manifest.build(doc, [{"name": "R-1", "email_address": " A@B.co ", "recipient_name": "Ann Lee", "recipient_data": "{}"}])
			lines = [json.loads(line) for line in lines]
		self.assertEqual((header["channel"], header["subject"], header["from_email"]), ("email", "Sub", "a@b.co"))
		self.assertEqual((lines[1]["to"], lines[1]["token"], lines[1]["variables"]["first_name"]), ("a@b.co", "tok", "Ann"))


class TestSecretSync(IntegrationTestCase):
	def test_whatsapp_sends_the_live_token(self):
		account = MagicMock()
		account.get_password.return_value = "TOKEN"
		with (
			patch("msuite_whatsapp.queries.account_repository.get_account_doc", return_value=account),
			patch.object(dispatch.client, "sync_channel_secret") as sync,
		):
			dispatch._ensure_channel_secret_synced("whatsapp", frappe._dict(whatsapp_account="ACC"))
		sync.assert_called_once_with("whatsapp", {"access_token": "TOKEN"})

	def test_a_failed_sync_stops_the_send(self):
		with (
			patch("msuite_whatsapp.queries.account_repository.get_account_doc", side_effect=RuntimeError("boom")),
			self.assertRaises(frappe.ValidationError),
		):
			dispatch._ensure_channel_secret_synced("whatsapp", frappe._dict(whatsapp_account="ACC"))

	def test_a_channel_with_no_secret_does_nothing(self):
		self.assertIsNone((get_adapters().get("email") or {}).get("sync_secret"))
		dispatch._ensure_channel_secret_synced("email", frappe._dict())


class TestRecordOutbound(IntegrationTestCase):
	"""Real rows, real Inbox records. Only the plan gate on Channel Message is out of the way."""

	def setUp(self):
		frappe.set_user("Administrator")
		for target in ("msuite_core.services.plans.plan_enforcer.enforce_gate", "msuite_inbox.inbox.services.dispatcher._channel_enabled"):
			patcher = patch(target, return_value=True)
			patcher.start()
			self.addCleanup(patcher.stop)

	def _messages(self, doctype, name):
		return frappe.get_all(
			"Channel Message",
			filters={"reference_doctype": doctype, "reference_name": name},
			fields=["channel_type", "direction", "platform_message_id"],
		)

	def test_a_missing_blast_is_ignored(self):
		dispatch.record_bulk_outbound_messages(SMS, "ZZ-NO-SUCH")

	def test_a_doctype_with_no_channel_is_ignored(self):
		dispatch.record_bulk_outbound_messages("ToDo", frappe.generate_hash(length=8))

	def _blast(self, doctype, prefix, **values):
		name = f"{prefix}-" + frappe.generate_hash(length=6)
		frappe.get_doc({"doctype": doctype, "name": name, "title": name, **values}).db_insert()
		return name

	def test_whatsapp(self):
		name = self._blast(WHATSAPP, "ZZ-WA")
		recipient("WhatsApp Recipient", name, WHATSAPP, mobile_number="+919811100001", recipient_name="Ann", status="Sent")
		with patch("msuite_inbox.inbox.services.outbound.inbox_for_account", return_value=inbox("WhatsApp")), patch(
			"msuite_whatsapp.queries.account_repository.find_default_account", return_value=""
		):
			dispatch.record_bulk_outbound_messages(WHATSAPP, name)
		rows = self._messages(WHATSAPP, name)
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0].channel_type, "WhatsApp")
		self.assertTrue(rows[0].platform_message_id.startswith(f"aws-wa-{name}-"))

	def test_sms(self):
		name = self._blast(SMS, "ZZ-SMS", recipient_type="Individual")
		recipient("SMS Recipient", name, SMS, mobile_number="+919811100002", recipient_name="Bo")
		target = inbox("SMS")
		# Channel Handle has no SMS option, on dev as well, so a real handle cannot be saved yet. This
		# checks what the SMS channel hands the dispatcher, up to that point.
		handle = frappe._dict(name="H-1", platform_handle="919811100002", display_name="Bo")
		with (
			patch("msuite_inbox.inbox.services.outbound.inbox_for_account", return_value=target),
			patch("msuite_inbox.inbox.services.outbound._upsert_handle", return_value=handle),
			patch("msuite_inbox.inbox.services.outbound._upsert_outbound_conversation", return_value=frappe._dict(inbox=target)),
			patch("msuite_inbox.inbox.services.dispatcher.handle") as dispatched,
		):
			dispatch.record_bulk_outbound_messages(SMS, name)
		event = dispatched.call_args.args[0]
		self.assertEqual((event.channel_type, event.reference_name), ("SMS", name))
		self.assertTrue(event.platform_message_id.startswith(f"aws-sms-{name}-"))

	def test_email(self):
		name = self._blast(EMAIL, "ZZ-EM", recipient_type="Individual")
		recipient("Email Recipient", name, EMAIL, email_address="zz@example.com", recipient_name="Cy")
		with patch("msuite_inbox.inbox.services.outbound.inbox_for_account", return_value=inbox("Email")):
			dispatch.record_bulk_outbound_messages(EMAIL, name)
		rows = self._messages(EMAIL, name)
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0].channel_type, "Email")
		self.assertTrue(rows[0].platform_message_id.startswith(f"aws-em-{name}-"))

	def test_recording_twice_adds_nothing(self):
		name = self._blast(WHATSAPP, "ZZ-WA")
		recipient("WhatsApp Recipient", name, WHATSAPP, mobile_number="+919811100003", status="Sent")
		with patch("msuite_inbox.inbox.services.outbound.inbox_for_account", return_value=inbox("WhatsApp")), patch(
			"msuite_whatsapp.queries.account_repository.find_default_account", return_value=""
		):
			dispatch.record_bulk_outbound_messages(WHATSAPP, name)
			dispatch.record_bulk_outbound_messages(WHATSAPP, name)
		self.assertEqual(len(self._messages(WHATSAPP, name)), 1)

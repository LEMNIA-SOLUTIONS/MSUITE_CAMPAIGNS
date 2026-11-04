"""
The AWS delivery engine's control-plane half.

Nothing here talks to AWS, and — the point of this design — **neither does the
code under test**. The client has no AWS credentials, no bucket name and no
queue URL. It asks the provider for a presigned URL, PUTs to it, and asks the
provider to enqueue. All three calls are patched.

What is being guarded:

  1. the manifest carries exactly the rows the RQ path would have sent, in the
     RQ path's own shape — same dedupe, same suppression, same variables
  2. nothing secret, and no *reference* to anything secret, is in a manifest
  3. choosing `delivery_engine = AWS` does not touch the Standard path, and
     leaving it Standard does not touch the AWS path

Rendering, the gateway ports and the retry engine are checked on the Lambda
side, where they happen — in the `aws-bulk-messaging` repo (outside the
bench): `tests/test_render.py`, `tests/test_sms_providers.py`,
`tests/test_whatsapp_error_map.py`.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from msuite_campaigns.aws_pipeline import dispatch as aws_dispatch
from msuite_campaigns.aws_pipeline import manifest
from msuite_campaigns.aws_pipeline import rollup as aws_rollup
from msuite_campaigns.campaign.services import analytics
from msuite_email.msuite_email.services import email_bulk_send_service as svc

PREFIX = "ZZAws"

# aws_dispatch.AWS_MIN_RECIPIENT_THRESHOLD is 50 — a blast at or under that
# count always routes via Standard regardless of delivery_engine, so any test
# that means to exercise the AWS broker calls needs more recipients than this.
_MANY = 60

# Every credential-shaped string in this module's fixtures. A manifest that
# contains any of them has leaked.
_BANNED = (
	"AKIA",
	"secret_access_key",
	"access_key_id",
	"credentials_secret_id",
	"aws_secret_id",
	"api_secret",
	"authkey",
	"AC_zz_sid",
	"zz_token",
	"EAAzz_meta_token",
)


# Doctypes this module creates fixtures in.
_FIXTURE_DOCTYPES = (
	"Bulk SMS Message",
	"Bulk Email Message",
	"Bulk WhatsApp Message",
	"SMS Template",
	"Email Template",
	"SMS Account",
	"WhatsApp Account",
)


def _purge_fixtures() -> None:
	"""Delete this module's fixtures with raw SQL.

	`frappe.db.rollback()` is not enough: the code under test commits, so
	anything created survives the rollback and pollutes the next run.
	`test_sms_engine`'s shared `T-MSG91` account is what that looks like when
	it goes unnoticed — a committed `sender_id = ""` from one assertion
	silently broke nine other tests on every subsequent run.

	Raw DELETE rather than `frappe.delete_doc` because a submitted doc needs
	cancelling first and link validation would fight the teardown.
	"""
	# Children first, addressed by EXACT parent name rather than a
	# `parent LIKE 'ZZAws%'` scan. `tabSMS Recipient` is a table
	# `test_bulk_batching` fills with 20k rows, and this method commits, so a
	# LIKE scan here takes far wider locks than the handful of rows warrant.
	parents = {}
	for doctype in _FIXTURE_DOCTYPES:
		if not frappe.db.exists("DocType", doctype):
			continue
		# Both cases: `WhatsApp Templates` autonames from `template_name`,
		# which this module sets lowercase.
		names = []
		for prefix in (PREFIX, PREFIX.lower()):
			names += frappe.get_all(doctype, filters={"name": ["like", f"{prefix}%"]}, pluck="name")
		parents[doctype] = sorted(set(names))

	for child, parent_doctype in (
		("SMS Recipient", "Bulk SMS Message"),
		("Email Recipient", "Bulk Email Message"),
		("WhatsApp Recipient", "Bulk WhatsApp Message"),
	):
		names = parents.get(parent_doctype) or []
		if not names:
			continue
		frappe.db.sql(
			f"DELETE FROM `tab{child}` WHERE parenttype = %(parenttype)s AND parent IN %(names)s",
			{"parenttype": parent_doctype, "names": tuple(names)},
		)

	for doctype, names in parents.items():
		if names:
			frappe.db.sql(f"DELETE FROM `tab{doctype}` WHERE name IN %(names)s", {"names": tuple(names)})
	frappe.db.commit()


def broker():
	"""Patch the three provider calls, and expose what they were handed.

	Returns a context manager yielding `(presign, upload, trigger)` mocks.
	`upload.call_args[0][1]` is the manifest body.
	"""
	return (
		patch.object(
			aws_dispatch.client,
			"request_upload_url",
			return_value={"url": "https://s3.test/presigned", "key": "manifests/zz/k.ndjson", "max_bytes": 0},
		),
		patch.object(aws_dispatch.client, "upload_manifest", return_value=None),
		patch.object(aws_dispatch.client, "trigger_campaign", return_value={"message_id": "msg-1"}),
	)


def brokered(fn):
	"""Decorator applying all three broker patches, innermost-first.

	Also flips `aws_pipeline_force_ready`, because `dispatch.is_ready()`
	returns False under test by default — a submitted blast must not make
	outbound HTTP just because a suite happened to run. This module is the one
	that means to take the AWS path.
	"""

	def wrapper(*args, **kwargs):
		frappe.flags.aws_pipeline_force_ready = True
		try:
			return fn(*args, **kwargs)
		finally:
			frappe.flags.aws_pipeline_force_ready = False

	wrapper.__name__ = fn.__name__
	wrapper.__doc__ = fn.__doc__
	presign, upload, trigger = broker()
	return presign(upload(trigger(wrapper)))


# ── Email ────────────────────────────────────────────────────────────────


def _template() -> str:
	"""A core `Email Template` of this test's own.

	Email migrated from `MSuite Email Template` to Frappe's core
	`Email Template`, so the fixture follows. `use_html` matters: the exporter
	reads `response_html` when it is set and `response` otherwise, and the two
	fields hold different markup.
	"""
	name = f"{PREFIX} Template"
	if frappe.db.exists("Email Template", name):
		return name
	doc = frappe.get_doc(
		{
			"doctype": "Email Template",
			"name": name,
			"subject": "Hi {{ first_name }}",
			"use_html": 1,
			"response_html": "<p>Hello {{ recipient_name }} — code {{ code }}</p>",
			"response": "Hello {{ recipient_name }} — code {{ code }}",
		}
	)
	doc.insert(ignore_permissions=True)
	return doc.name


def _blast(emails: list[str], engine: str = "AWS") -> str:
	"""A submitted Bulk Email Message with `emails` as Individual recipients."""
	account = frappe.db.get_value("Email Account", {}, "name")
	if not account:
		raise RuntimeError("test needs one Email Account on the site")

	doc = frappe.get_doc(
		{
			"doctype": "Bulk Email Message",
			"title": f"{PREFIX} blast",
			"email_account": account,
			"template": _template(),
			"recipient_type": "Individual",
			"delivery_engine": engine,
			"recipients": [
				{
					"email_address": e,
					"recipient_name": f"User {i}",
					"recipient_data": json.dumps({"code": i}),
					"status": "Queued",
				}
				for i, e in enumerate(emails)
			],
		}
	)
	doc.insert(ignore_permissions=True)
	doc.submit()
	return doc.name


class TestAWSPipeline(IntegrationTestCase):
	def tearDown(self):
		frappe.db.rollback()
		_purge_fixtures()

	# ── manifest shape ───────────────────────────────────────────────────

	def test_header_is_line_one_and_rows_follow(self):
		name = _blast(["a@zz.test", "b@zz.test"], engine="Standard")
		doc = frappe.get_doc("Bulk Email Message", name)

		header, lines = manifest.build(doc)
		lines = list(lines)

		self.assertEqual(json.loads(lines[0]), header)
		self.assertEqual(len(lines), 3)  # header + 2 recipients
		self.assertEqual(header["channel"], "email")
		self.assertEqual(header["bulk_doc_name"], name)

	def test_row_carries_rendering_variables_and_token(self):
		name = _blast(["a@zz.test"], engine="Standard")
		doc = frappe.get_doc("Bulk Email Message", name)
		_, lines = manifest.build(doc)
		row = json.loads(list(lines)[1])

		self.assertEqual(row["to"], "a@zz.test")
		self.assertEqual(row["attempt_count"], 0)
		self.assertTrue(row["token"])
		# Same defaults send_single_message_job injects — the two engines must
		# render the same email from the same template.
		self.assertEqual(row["variables"]["email_address"], "a@zz.test")
		self.assertEqual(row["variables"]["first_name"], "User")
		self.assertEqual(row["variables"]["code"], 0)

	def test_sent_rows_are_never_re_exported(self):
		"""The filter that stops a re-run re-sending a delivered recipient."""
		name = _blast(["a@zz.test", "b@zz.test"], engine="Standard")
		row = frappe.db.get_value("Email Recipient", {"parent": name, "email_address": "a@zz.test"}, "name")
		frappe.db.set_value("Email Recipient", row, "status", "Sent")

		doc = frappe.get_doc("Bulk Email Message", name)
		_, lines = manifest.build(doc)
		addresses = [json.loads(line)["to"] for line in list(lines)[1:]]

		self.assertEqual(addresses, ["b@zz.test"])

	# ── the branch ───────────────────────────────────────────────────────

	@brokered
	def test_aws_engine_makes_one_of_each_broker_call(self, trigger, upload, presign):
		"""Three provider calls per campaign, not per recipient."""
		with patch.object(frappe, "enqueue") as rq:
			_blast([f"u{i}@zz.test" for i in range(_MANY)])

		self.assertEqual(presign.call_count, 1)
		self.assertEqual(upload.call_count, 1)
		self.assertEqual(trigger.call_count, 1)
		self.assertEqual(rq.call_count, 0, "AWS-routed blast must not enqueue RQ send jobs")

		self.assertEqual(len(upload.call_args[0][1].splitlines()), _MANY + 1)  # header + recipients
		self.assertEqual(trigger.call_args.kwargs["recipient_count"], _MANY)
		self.assertEqual(trigger.call_args.kwargs["channel"], "email")

	@brokered
	def test_the_key_comes_from_the_provider_not_from_here(self, trigger, upload, presign):
		"""The client never names an S3 key — the provider does, inside the
		client's own prefix, which is what makes its ownership check real."""
		_blast([f"u{i}@zz.test" for i in range(_MANY)])
		self.assertEqual(upload.call_args[0][0], "https://s3.test/presigned")
		self.assertEqual(trigger.call_args.kwargs["key"], "manifests/zz/k.ndjson")

	@brokered
	def test_upload_happens_before_trigger(self, trigger, upload, presign):
		"""Triggering first would point a live queue message at a key that
		does not exist yet."""
		order = []
		upload.side_effect = lambda *a, **k: order.append("upload")
		trigger.side_effect = lambda *a, **k: order.append("trigger") or {"message_id": "m"}

		_blast([f"u{i}@zz.test" for i in range(_MANY)])
		self.assertEqual(order, ["upload", "trigger"])

	@brokered
	def test_exported_rows_are_flipped_to_queued(self, trigger, upload, presign):
		name = _blast([f"u{i}@zz.test" for i in range(_MANY)])
		self.assertEqual(frappe.db.get_value("Email Recipient", {"parent": name, "email_address": "u0@zz.test"}, "status"), "Queued")

	def test_standard_engine_still_uses_rq(self):
		"""The regression this whole design is built around."""
		with patch.object(aws_dispatch, "export_and_enqueue") as aws, patch.object(frappe, "enqueue") as rq:
			_blast(["a@zz.test", "b@zz.test"], engine="Standard")

		self.assertEqual(aws.call_count, 0)
		self.assertEqual(rq.call_count, 2, "Standard blast must still enqueue one RQ job per recipient")

	@brokered
	def test_suppressed_and_duplicate_addresses_never_reach_the_manifest(self, trigger, upload, presign):
		"""Proof the AWS branch inherits queue_messages' dedupe/suppression.

		This is why the branch sits inside queue_messages and not in
		on_submit — an on_submit branch would export the raw child table.
		"""
		fillers = [f"filler{i}@zz.test" for i in range(_MANY)]
		with patch.object(svc, "is_suppressed", side_effect=lambda e: e == "sup@zz.test"):
			name = _blast(["a@zz.test", "A@zz.test", "sup@zz.test", *fillers])

		addresses = [json.loads(line)["to"] for line in upload.call_args[0][1].splitlines()[1:]]
		self.assertEqual(addresses.count("a@zz.test"), 1, "duplicate must be collapsed, not exported twice")
		self.assertNotIn("sup@zz.test", addresses, "suppressed address must not reach the manifest")
		self.assertEqual(len(addresses), 1 + len(fillers))
		self.assertEqual(
			frappe.db.get_value("Email Recipient", {"parent": name, "email_address": "sup@zz.test"}, "status"),
			"Unsubscribed",
		)


# ── SMS ──────────────────────────────────────────────────────────────────


def _sms_account(gateway: str = "Twilio") -> str:
	name = f"{PREFIX} SMS {gateway}"
	if frappe.db.exists("SMS Account", name):
		return name
	doc = frappe.get_doc(
		{
			"doctype": "SMS Account",
			"account_name": name,
			"gateway": gateway,
			"status": "Active",
			"api_key": "AC_zz_sid",
			"api_secret": "zz_token",
			"sender_id": "+15550001111" if gateway == "Twilio" else "MSUITE",
		}
	)
	doc.insert(ignore_permissions=True)
	return doc.name


def _sms_template(account: str, dlt_id: str = "") -> str:
	name = f"{PREFIX} SMS Template {account}"
	if frappe.db.exists("SMS Template", name):
		return name
	doc = frappe.get_doc(
		{
			"doctype": "SMS Template",
			"template_name": name,
			"sms_account": account,
			"category": "Transactional",
			"status": "Approved",
			"body": "Hi {{ first_name }}, code {{ code }}",
			"dlt_template_id": dlt_id,
		}
	)
	doc.insert(ignore_permissions=True)
	return doc.name


def _sms_blast(numbers: list[str], engine: str = "AWS", gateway: str = "Twilio") -> str:
	account = _sms_account(gateway)
	doc = frappe.get_doc(
		{
			"doctype": "Bulk SMS Message",
			"title": f"{PREFIX} sms blast",
			"sms_account": account,
			"template": _sms_template(account, dlt_id="FLOW123" if gateway == "MSG91" else ""),
			"recipient_type": "Individual",
			"delivery_engine": engine,
			"recipients": [
				{"mobile_number": n, "recipient_name": f"User {i}", "recipient_data": json.dumps({"code": i})}
				for i, n in enumerate(numbers)
			],
		}
	)
	doc.insert(ignore_permissions=True)
	doc.submit()
	return doc.name


class TestAWSPipelineSMS(IntegrationTestCase):
	def tearDown(self):
		frappe.db.rollback()
		_purge_fixtures()

	@brokered
	def test_header_carries_config_but_no_secret_and_no_secret_name(self, trigger, upload, presign):
		"""The client does not even know which Secrets Manager entry holds its
		own gateway keys — the provider derives that name at trigger time."""
		_sms_blast(["+15558675309"] + [f"+1555000{i:04d}" for i in range(_MANY)])
		body = upload.call_args[0][1]
		header = json.loads(body.splitlines()[0])

		self.assertEqual(header["channel"], "sms")
		self.assertEqual(header["gateway"], "Twilio")
		self.assertEqual(header["sender_id"], "+15550001111")
		for banned in _BANNED:
			self.assertNotIn(banned, body, f"{banned!r} leaked into the SMS manifest")

	@brokered
	def test_rows_carry_merged_variables(self, trigger, upload, presign):
		_sms_blast(["+15558675309"] + [f"+1555000{i:04d}" for i in range(_MANY)])
		row = json.loads(upload.call_args[0][1].splitlines()[1])

		self.assertEqual(row["to"], "+15558675309")
		self.assertEqual(row["attempt_count"], 0)
		# Built-ins from _merge_variables plus the recipient's own CSV columns
		self.assertEqual(row["variables"]["first_name"], "User")
		self.assertEqual(row["variables"]["mobile_number"], "+15558675309")
		self.assertEqual(row["variables"]["code"], 0)
		# SMS has no tracking token — that is an email concept
		self.assertNotIn("token", row)

	@brokered
	def test_one_of_each_broker_call_and_no_rq_jobs(self, trigger, upload, presign):
		with patch.object(frappe, "enqueue") as rq:
			_sms_blast([f"+1555000{i:04d}" for i in range(_MANY)])

		self.assertEqual(presign.call_count, 1)
		self.assertEqual(upload.call_count, 1)
		self.assertEqual(trigger.call_count, 1)
		self.assertEqual(rq.call_count, 0)
		self.assertEqual(len(upload.call_args[0][1].splitlines()), _MANY + 1)
		self.assertEqual(trigger.call_args.kwargs["channel"], "sms")

	def test_standard_engine_still_uses_rq(self):
		with patch.object(aws_dispatch, "export_and_enqueue") as aws, patch.object(frappe, "enqueue") as rq:
			_sms_blast(["+15558675309", "+15558675310"], engine="Standard")

		self.assertEqual(aws.call_count, 0)
		self.assertEqual(rq.call_count, 2)

	@brokered
	def test_exported_rows_are_flipped_to_queued(self, trigger, upload, presign):
		name = _sms_blast([f"+1555000{i:04d}" for i in range(_MANY)])
		self.assertEqual(frappe.db.get_value("SMS Recipient", {"parent": name, "mobile_number": "+15550000000"}, "status"), "Queued")

	@brokered
	def test_msg91_header_carries_the_dlt_registration(self, trigger, upload, presign):
		_sms_blast(["+919812345678"] + [f"+91981200{i:04d}" for i in range(_MANY)], gateway="MSG91")
		header = json.loads(upload.call_args[0][1].splitlines()[0])
		self.assertEqual(header["gateway"], "MSG91")
		self.assertEqual(header["dlt_template_id"], "FLOW123")


# ── WhatsApp ─────────────────────────────────────────────────────────────


def _wa_account() -> str:
	name = f"{PREFIX} WABA"
	if frappe.db.exists("WhatsApp Account", name):
		return name
	doc = frappe.get_doc(
		{
			"doctype": "WhatsApp Account",
			"account_name": name,
			"status": "Active",
			"waba_id": "ZZWABA1",
			"token": "EAAzz_meta_token",
			"phones": [{"phone_number_id": "1234567890", "status": "Active", "is_default_outgoing": 1}],
		}
	)
	doc.insert(ignore_permissions=True)
	return doc.name


def _wa_template() -> str:
	"""Pick a site template with no media header.

	Not a fixture of our own: `WhatsApp Templates` enforces sample values for
	every `{{n}}` and refuses a second language variant, so creating one here
	fights validations that have nothing to do with what is being tested.
	Borrowing is fine — but it must be a TEXT/no-header template, because
	building the Meta body for an IMAGE/DOCUMENT header uploads the media to
	Meta and fails on a test site with `Invalid OAuth access token`.
	"""
	name = frappe.db.sql(
		"""SELECT name FROM `tabWhatsApp Templates`
		   WHERE COALESCE(header_type, '') IN ('', 'TEXT')
		   ORDER BY (status = 'APPROVED') DESC LIMIT 1"""
	)
	if not name:
		raise RuntimeError("test needs one WhatsApp Templates row with no media header")
	return name[0][0]


def _wa_blast(numbers: list[str], engine: str = "AWS") -> str:
	template = _wa_template()

	doc = frappe.get_doc(
		{
			"doctype": "Bulk WhatsApp Message",
			"title": f"{PREFIX} wa blast",
			"whatsapp_account": _wa_account(),
			"use_template": 1,
			"template": template,
			"recipient_type": "Individual",
			"delivery_engine": engine,
			"recipients": [
				{"mobile_number": n, "recipient_name": f"User {i}", "recipient_data": "{}"}
				for i, n in enumerate(numbers)
			],
		}
	)
	doc.insert(ignore_permissions=True)
	doc.submit()
	return doc.name


class TestAWSPipelineWhatsApp(IntegrationTestCase):
	def tearDown(self):
		frappe.db.rollback()
		_purge_fixtures()

	@brokered
	def test_header_carries_no_token_and_no_token_reference(self, trigger, upload, presign):
		_wa_blast(["+919812345678"] + [f"+91981200{i:04d}" for i in range(_MANY)])
		body = upload.call_args[0][1]
		header = json.loads(body.splitlines()[0])

		self.assertEqual(header["channel"], "whatsapp")
		self.assertEqual(header["phone_number_id"], "1234567890")
		for banned in _BANNED:
			self.assertNotIn(banned, body, f"{banned!r} leaked into the WhatsApp manifest")

	@brokered
	def test_rows_carry_a_prebuilt_meta_body(self, trigger, upload, presign):
		"""The body is assembled by Frappe's own _build_send_body, so both
		engines put identical JSON on the wire."""
		_wa_blast(["+919812345678"] + [f"+91981200{i:04d}" for i in range(_MANY)])
		row = json.loads(upload.call_args[0][1].splitlines()[1])

		self.assertEqual(row["to"], "+919812345678")
		self.assertEqual(row["attempt_count"], 0)
		body = row["meta_body"]
		self.assertEqual(body["messaging_product"], "whatsapp")
		self.assertEqual(body["to"], "+919812345678")
		self.assertEqual(body["type"], "template")
		self.assertIn("name", body["template"])

	@brokered
	def test_one_of_each_broker_call_and_no_rq_jobs(self, trigger, upload, presign):
		with patch.object(frappe, "enqueue_doc") as rq:
			_wa_blast([f"+91981234{i:04d}" for i in range(_MANY)])

		self.assertEqual(presign.call_count, 1)
		self.assertEqual(upload.call_count, 1)
		self.assertEqual(trigger.call_count, 1)
		self.assertEqual(rq.call_count, 0)
		self.assertEqual(len(upload.call_args[0][1].splitlines()), _MANY + 1)
		self.assertEqual(trigger.call_args.kwargs["channel"], "whatsapp")

	def test_standard_engine_still_uses_rq(self):
		with patch.object(aws_dispatch, "export_and_enqueue") as aws, patch.object(frappe, "enqueue_doc") as rq:
			_wa_blast(["+919812345678", "+919812345679"], engine="Standard")

		self.assertEqual(aws.call_count, 0)
		self.assertEqual(rq.call_count, 2)

	def test_recipient_list_rows_are_read_from_the_list_not_the_blast(self):
		"""WhatsApp does not snapshot a Recipient List into the blast the way
		email does. Reading the blast would give an empty manifest silently."""
		doc = frappe._dict(
			doctype="Bulk WhatsApp Message",
			name="ZZAws-wa-1",
			recipient_type="Recipient List",
			recipient_list="ZZAws-list-1",
			get=lambda k, d=None: {"recipient_type": "Recipient List", "recipient_list": "ZZAws-list-1"}.get(k, d),
		)
		self.assertEqual(manifest.row_parent(doc), ("ZZAws-list-1", "WhatsApp Recipient List"))

		inline = frappe._dict(
			doctype="Bulk WhatsApp Message",
			name="ZZAws-wa-2",
			recipient_type="Individual",
			get=lambda k, d=None: {"recipient_type": "Individual"}.get(k, d),
		)
		self.assertEqual(manifest.row_parent(inline), ("ZZAws-wa-2", "Bulk WhatsApp Message"))


# ── The client holds nothing ─────────────────────────────────────────────


class TestNoAWSCredentialsOnClient(IntegrationTestCase):
	"""Standing guard on the property the whole redesign exists for.

	There is one AWS account behind every client workspace, so an AWS key is a
	cross-tenant credential — a client holding one could read another client's
	manifests or drain the shared queue. Same class as a Meta app secret,
	which this architecture already keeps provider-side.
	"""

	def test_the_credential_doctype_is_gone(self):
		self.assertFalse(frappe.db.exists("DocType", "AWS Messaging Account"))

	def test_no_bulk_doctype_links_to_an_aws_account(self):
		for doctype in ("Bulk Email Message", "Bulk SMS Message", "Bulk WhatsApp Message"):
			self.assertIsNone(
				frappe.get_meta(doctype).get_field("aws_account"),
				f"{doctype} still carries an aws_account link",
			)

	def test_no_account_doctype_names_a_secret(self):
		for doctype in ("SMS Account", "WhatsApp Account"):
			self.assertIsNone(
				frappe.get_meta(doctype).get_field("aws_secret_id"),
				f"{doctype} still names a Secrets Manager entry",
			)

	def test_the_client_does_not_import_boto3(self):
		"""No AWS SDK anywhere in the app. A presigned PUT is an ordinary PUT."""
		import subprocess
		from pathlib import Path

		from msuite_core.services.permissions.sync import MSUITE_APPS

		hits = subprocess.run(
			[
				"grep", "-rn", "--include=*.py",
				# This file names the string it is looking for; excluding the
				# suite keeps the assertion about the app, not about itself.
				"--exclude-dir=tests",
				"-w", "import boto3", *[frappe.get_app_path(app) for app in MSUITE_APPS],
			],
			capture_output=True,
			text=True,
		).stdout.strip()
		self.assertEqual(hits, "", f"boto3 imported in the client app:\n{hits}")

	def test_dispatch_only_reaches_aws_through_the_provider(self):
		"""Every outbound AWS operation is a provider call."""
		from msuite_campaigns.aws_pipeline import client

		for fn in ("request_upload_url", "upload_manifest", "trigger_campaign"):
			self.assertTrue(hasattr(client, fn), f"{fn} missing from the broker client")
		self.assertFalse(hasattr(client, "session"), "client must not build an AWS session")
		self.assertFalse(hasattr(client, "put_manifest"), "client must not write to S3 directly")
		self.assertFalse(hasattr(client, "enqueue_manifest"), "client must not write to SQS directly")


# ── Phase 4: dashboard rollup ────────────────────────────────────────────


COUNTERS = {
	"sent_count": 90,
	"delivered_count": 80,
	"read_count": 10,
	"failed_count": 10,
	"retried_count": 3,
}


class TestAWSRollup(IntegrationTestCase):
	"""Numbers for AWS-routed blasts come from DynamoDB via the provider.

	The adapters can't supply them: an AWS-routed send never writes
	per-recipient status back to MariaDB, so `rollup()` would report a campaign
	of permanently-Queued recipients.
	"""

	def tearDown(self):
		frappe.db.rollback()
		_purge_fixtures()
		frappe.db.sql("DELETE FROM `tabMarketing Campaign` WHERE name LIKE %s", (f"{PREFIX}%",))
		frappe.db.commit()

	def _campaign(self) -> str:
		name = f"{PREFIX} Campaign"
		if frappe.db.exists("Marketing Campaign", name):
			return name
		doc = frappe.get_doc(
			{
				"doctype": "Marketing Campaign",
				"campaign_name": name,
				"status": "Active",
				"start_date": frappe.utils.now_datetime(),
				"end_date": frappe.utils.add_to_date(frappe.utils.now_datetime(), days=30),
			}
		)
		doc.flags.ignore_permissions = True
		doc.insert()
		return doc.name

	@brokered
	def _aws_email_blast(self, campaign, trigger, upload, presign):
		name = _blast([f"u{i}@zz.test" for i in range(_MANY)])
		frappe.db.set_value("Bulk Email Message", name, "campaign", campaign, update_modified=False)
		return name

	def test_shape_matches_what_the_adapters_return(self):
		"""The SPA renders this unchanged — no new RPC, no frontend work."""
		campaign = self._campaign()
		self._aws_email_blast(campaign)

		with patch.object(aws_rollup, "fetch", return_value=COUNTERS):
			out = aws_rollup.rollup(campaign, "email")

		self.assertEqual(out["channel"], "email")
		self.assertEqual(out["label"], "Email")
		self.assertEqual(out["blasts"], 1)
		for key in ("sent", "delivered", "failed", "audience", "credits", "delivery_rate"):
			self.assertIn(key, out["metrics"], f"adapters return {key}; this must too")
		self.assertEqual(out["metrics"]["sent"], 90)
		self.assertEqual(out["metrics"]["delivery_rate"], 88.9)

	def test_no_aws_blast_means_no_override(self):
		"""A Standard-only campaign must not be touched at all."""
		campaign = self._campaign()
		self.assertIsNone(aws_rollup.rollup(campaign, "sms"))

		modules = {"sms": {"channel": "sms", "metrics": {"sent": 5}}}
		self.assertEqual(aws_rollup.merge(dict(modules), campaign), modules)

	def test_mixed_campaign_adds_rather_than_replaces(self):
		"""A campaign can hold both engines on one channel. Dropping either
		set of numbers would under-report the send."""
		campaign = self._campaign()
		self._aws_email_blast(campaign)

		standard = {
			"email": {
				"channel": "email",
				"label": "Email",
				"blasts": 1,
				"metrics": {"sent": 10, "delivered": 8, "failed": 2, "audience": 10, "credits": 0, "delivery_rate": 80.0},
			}
		}
		with patch.object(aws_rollup, "fetch", return_value=COUNTERS):
			merged = aws_rollup.merge(standard, campaign)["email"]

		self.assertEqual(merged["blasts"], 2)
		self.assertEqual(merged["metrics"]["sent"], 100)
		self.assertEqual(merged["metrics"]["delivered"], 88)
		self.assertEqual(merged["engine"], "mixed")
		# Rates are recomputed from the totals, never averaged
		self.assertEqual(merged["metrics"]["delivery_rate"], 88.0)

	def test_an_unreachable_analytics_api_is_soft(self):
		"""A dashboard is a read. One channel showing zeros beats a 500."""
		campaign = self._campaign()
		self._aws_email_blast(campaign)

		with patch.object(aws_rollup, "fetch", return_value=None):
			out = aws_rollup.rollup(campaign, "email")

		self.assertEqual(out["metrics"]["sent"], 0)
		self.assertTrue(out["stale"], "must distinguish 'nothing sent' from 'could not ask'")

	def test_the_dashboard_survives_a_broken_rollup(self):
		campaign = self._campaign()
		with patch.object(aws_rollup, "merge", side_effect=RuntimeError("boom")):
			dash = analytics.get_campaign_dashboard(campaign, 30)
		self.assertEqual(dash["campaign"], campaign)
		self.assertIn("modules", dash)

	def test_the_client_never_calls_the_analytics_api_directly(self):
		"""It has neither the URL nor the key — the provider brokers the read."""
		import inspect

		src = inspect.getsource(aws_rollup)
		self.assertIn("msuite.api.v1.bulk_relay.campaign_analytics", src)
		self.assertNotIn("execute-api", src)
		self.assertNotIn("x-api-key", src)


# ── AWS is the default, Standard is the fallback ─────────────────────────


class TestAWSIsTheDefault(IntegrationTestCase):
	"""`delivery_engine` defaults to AWS on all three bulk doctypes.

	That makes a misconfiguration far more expensive than it was when AWS was
	opt-in: it would break *every* blast rather than the ones that asked for
	it. So AWS is the PREFERRED engine and Standard is the fallback —
	`should_use_aws` returns False when the site cannot actually reach the
	provider, and the blast goes out the old way with a warning logged.
	"""

	def tearDown(self):
		frappe.flags.aws_pipeline_force_ready = False
		frappe.db.rollback()
		_purge_fixtures()

	def test_all_three_doctypes_default_to_aws(self):
		for doctype in ("Bulk Email Message", "Bulk SMS Message", "Bulk WhatsApp Message"):
			field = frappe.get_meta(doctype).get_field("delivery_engine")
			self.assertEqual(field.default, "AWS", f"{doctype} should default to AWS")

	def test_a_null_engine_is_treated_as_aws(self):
		"""Rows created before the field existed carry NULL. The intent for
		those is the current default, not Standard."""
		frappe.flags.aws_pipeline_force_ready = True
		doc = frappe._dict(doctype="Bulk Email Message", name="x", delivery_engine=None)
		# Over AWS_MIN_RECIPIENT_THRESHOLD, or the count-based Standard
		# fallback masks the engine-defaulting behaviour under test here.
		rows = [frappe._dict(name=f"r{i}") for i in range(_MANY)]
		self.assertTrue(aws_dispatch.should_use_aws(doc, rows))

	def test_explicit_standard_is_honoured_even_when_aws_is_ready(self):
		"""The escape hatch. Setting a blast to Standard must still work —
		that is the rollback path if AWS misbehaves."""
		frappe.flags.aws_pipeline_force_ready = True
		doc = frappe._dict(doctype="Bulk Email Message", name="x", delivery_engine="Standard")
		self.assertFalse(aws_dispatch.should_use_aws(doc))

	def test_an_unconfigured_site_falls_back_instead_of_failing(self):
		"""The whole point. Without this, a site that has not wired up AWS
		would fail every bulk send rather than quietly using RQ."""
		frappe.flags.aws_pipeline_force_ready = False   # is_ready() -> False under test
		doc = frappe._dict(doctype="Bulk Email Message", name="x", delivery_engine="AWS")
		rows = [frappe._dict(name=f"r{i}") for i in range(_MANY)]
		self.assertFalse(aws_dispatch.should_use_aws(doc, rows))

	def test_small_blast_uses_standard_even_when_aws_is_ready(self):
		"""AWS_MIN_RECIPIENT_THRESHOLD: three network round trips are not worth
		it for a handful of recipients — RQ is cheaper below the threshold."""
		frappe.flags.aws_pipeline_force_ready = True
		doc = frappe._dict(doctype="Bulk Email Message", name="x", delivery_engine="AWS")
		small = [frappe._dict(name=f"r{i}") for i in range(aws_dispatch.AWS_MIN_RECIPIENT_THRESHOLD)]
		self.assertFalse(aws_dispatch.should_use_aws(doc, small))

		big = [frappe._dict(name=f"r{i}") for i in range(aws_dispatch.AWS_MIN_RECIPIENT_THRESHOLD + 1)]
		self.assertTrue(aws_dispatch.should_use_aws(doc, big))

	def test_readiness_needs_a_provider_and_the_plan_feature(self):
		"""`is_ready` is local and cheap — no network. A check that needed the
		provider to be up in order to decide whether the provider is up would
		be self-defeating."""
		import inspect

		src = inspect.getsource(aws_dispatch.is_ready)
		self.assertIn("provider_url", src)
		self.assertIn("bulk_messaging", src)
		for networky in ("requests", "_call_provider", "http"):
			self.assertNotIn(networky, src, "is_ready must not do network I/O")

	def test_an_unconfigured_site_still_sends_via_rq(self):
		"""End to end: AWS default + not ready = the Standard path runs."""
		frappe.flags.aws_pipeline_force_ready = False
		with patch.object(aws_dispatch, "export_and_enqueue") as aws, patch.object(frappe, "enqueue") as rq:
			_blast(["a@zz.test", "b@zz.test"])          # no engine= -> defaults to AWS

		self.assertEqual(aws.call_count, 0, "must not have taken the AWS path")
		self.assertEqual(rq.call_count, 2, "must have fallen back to one RQ job per recipient")

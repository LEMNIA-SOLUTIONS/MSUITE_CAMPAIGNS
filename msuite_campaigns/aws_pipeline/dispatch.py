# Copyright (c) 2026, MSuite and contributors
# For license information, please see license.txt

"""
The AWS branch of `queue_messages`: export, hand off, return.

Three network calls per blast — presign, upload, trigger — regardless of
recipient count, and one bulk UPDATE per 1000 rows instead of one per row.
That O(1)-per-campaign write profile is the whole point of the AWS engine;
everything else here is bookkeeping.

Nothing in this module sends, and nothing in it touches AWS directly: the
provider brokers every AWS operation (see `client.py`). The Standard path is
untouched and stays the default — `dispatch/hub.py`,
`dispatch/materializer.py` and `dispatch/batched_enqueue.py` are not imported,
called or modified.
"""

from __future__ import annotations

import frappe
from frappe import _

from msuite_campaigns.aws_pipeline import client, manifest

# Rows flipped per UPDATE. Chunked only because MySQL's max_allowed_packet
# caps a single IN list, not for throughput — 100k rows is 100 statements.
_FLIP_CHUNK = 1000


def export_and_enqueue(bulk_doc, rows: list | None = None) -> dict:
	"""Hand a whole blast to the execution plane.

	Ordering is deliberate: presign, upload, then trigger. A crash between the
	upload and the trigger leaves an orphan object that the bucket's 30-day
	lifecycle rule reaps and no send happens — recoverable. Triggering first
	would point a live queue message at a key that does not exist, which is
	why the provider also refuses to enqueue a key it cannot `head_object`.

	A crash *after* `trigger_campaign` succeeds but before `_mark_queued`
	commits is the mirror-image risk: a retry would recompute the same
	sendable set and trigger a second AWS campaign for the same recipients.
	`_mark_queued` failing here is logged and retried once before the
	exception is allowed to propagate, so a transient DB blip does not turn
	into a silent duplicate-send risk on the next retry.
	"""
	sp = manifest.spec(bulk_doc.doctype)
	channel = sp["channel"]
	_require_feature(channel)

	header, lines = manifest.build(bulk_doc, rows)
	lines_list = list(lines)
	body = "\n".join(lines_list)
	count = max(len(lines_list) - 1, 0)  # minus the header line

	slot = client.request_upload_url(channel, bulk_doc.doctype, bulk_doc.name)
	_check_size(body, slot.get("max_bytes"))

	client.upload_manifest(slot["url"], body)

	_ensure_channel_secret_synced(channel, bulk_doc)

	result = client.trigger_campaign(
		key=slot["key"],
		channel=channel,
		bulk_doctype=bulk_doc.doctype,
		bulk_doc_name=bulk_doc.name,
		campaign_id=header.get("campaign_id") or bulk_doc.name,
		recipient_count=count,
	)

	flipped = 0
	if rows:
		names = [r.get("name") if isinstance(r, dict) else r.name for r in rows]
		try:
			flipped = _mark_queued(sp["row_doctype"], names)
		except Exception:
			# trigger_campaign already fired — a retry that recomputes the same
			# sendable set must not fire a second AWS campaign. Log loudly and
			# retry once before giving up; if this also fails, let it raise so
			# the caller (and whoever monitors log_error) knows the rows are
			# stuck un-flipped despite an already-triggered campaign.
			frappe.log_error(
				title=f"aws_pipeline: _mark_queued failed after trigger_campaign for {bulk_doc.name}",
				message=frappe.get_traceback(),
			)
			flipped = _mark_queued(sp["row_doctype"], names)

	# Synchronously record the outbound messages in the Inbox thread for each recipient
	# so agents immediately see what message/template was sent to the contact in their conversation.
	try:
		record_bulk_outbound_messages(bulk_doc.doctype, bulk_doc.name)
	except Exception as e:
		frappe.logger().warning(f"aws_pipeline: record_bulk_outbound_messages error for {bulk_doc.name}: {e}")

	frappe.logger().info(
		f"aws_pipeline: {bulk_doc.doctype} {bulk_doc.name} -> {slot['key']} "
		f"({count} recipients, sqs {result.get('message_id')})"
	)
	return {"s3_key": slot["key"], "message_id": result.get("message_id"), "queued": flipped or count}


def _ensure_channel_secret_synced(channel: str, bulk_doc) -> None:
	"""Pre-flight secret sync so AWS sender Lambdas find live credentials.

	A failure here must abort the send rather than merely warn: if the
	channel's credentials never make it to Secrets Manager, letting
	`trigger_campaign` proceed anyway means the execution plane sends against
	stale or missing credentials. There is no narrow, expected-failure case to
	swallow — any exception here means the sync did not happen, so it throws.
	"""
	from msuite_campaigns.campaign.services.relationships import get_adapters

	sync = (get_adapters().get(channel) or {}).get("sync_secret")
	try:
		if sync:
			sync(bulk_doc)
	except Exception as e:
		frappe.throw(
			_("Failed to sync {0} credentials for the AWS execution plane: {1}").format(channel, e)
		)


def is_ready(channel: str = "") -> bool:
	"""Can this site actually route a blast through the AWS engine?
	`channel` empty = bulk is on for any channel.

	`delivery_engine` now defaults to `AWS`, which means an unconfigured site
	would otherwise fail **every** bulk send rather than just the opted-in
	ones. So AWS is the *preferred* engine and Standard is the fallback: when
	this returns False the caller keeps its existing RQ path and logs why.

	Deliberately local and cheap — no network, no provider round trip. It runs
	on every blast, and a readiness check that itself needs the provider to be
	up would be self-defeating.
	"""
	from msuite_core.services.plans import plan_enforcer

	# Submitting a blast must not make outbound HTTP as a side effect of a
	# test run. A test site's `provider_url` is usually a localhost that either
	# is not listening or is a *different* bench — neither is a real provider.
	# Suites that mean to exercise the AWS path set the flag; `plan_enforcer`
	# takes the same shape for the same reason.
	if frappe.flags.in_test and not frappe.flags.get("aws_pipeline_force_ready"):
		return False

	try:
		settings = frappe.get_cached_doc("MSuite Settings")
	except Exception:
		return False

	if not (settings.get("provider_url") and settings.get("api_key")):
		return False

	return plan_enforcer.is_bulk_enabled(channel)


def get_min_recipient_threshold() -> int:
	"""Return the threshold under which bulk campaigns route via Frappe Standard engine.

	If the provider includes a custom threshold in plan_data or settings, that takes precedence.
	Defaults to 0 (no minimum threshold).
	"""
	try:
		from msuite_core.services.plans import plan_enforcer

		plan_data = plan_enforcer.get_plan_data()
		if plan_data:
			custom = plan_data.get("aws_min_recipient_threshold")
			if custom is not None:
				return int(custom)
			bulk_feature = plan_data.get("features", {}).get("bulk_messaging", {})
			if isinstance(bulk_feature, dict) and bulk_feature.get("aws_min_recipient_threshold") is not None:
				return int(bulk_feature["aws_min_recipient_threshold"])
	except Exception:
		pass
	return 0


def resolve_recipient_count(doc, rows: list | None = None) -> int:
	"""Determine the total sendable recipient count for a bulk message doc."""
	if rows is not None:
		return len(rows)

	if doc.doctype == "Bulk WhatsApp Message":
		parent, parenttype = manifest.row_parent(doc)
		return frappe.db.count("WhatsApp Recipient", {"parent": parent, "parenttype": parenttype})

	elif doc.doctype == "Bulk Email Message":
		if doc.get("recipients"):
			return len(doc.get("recipients"))
		elif doc.get("recipient_list"):
			count = frappe.db.count(
				"Email Recipient",
				{"parent": doc.name, "parenttype": doc.doctype, "unsubscribed": 0},
			)
			if count:
				return count
		return int(doc.get("total_recipients") or 0)

	elif doc.doctype == "Bulk SMS Message":
		if doc.get("recipients"):
			return len(doc.get("recipients"))
		elif doc.get("recipient_list"):
			return frappe.db.count(
				"SMS Recipient",
				{"parent": doc.recipient_list, "parenttype": "SMS Recipient List"},
			)
		return int(doc.get("total_recipients") or 0)

	return 0


def should_use_aws(doc, rows: list | None = None) -> bool:
	"""True when this blast should take the AWS path.

	One place for the decision, so the three engines cannot drift. Reads the
	field with an `AWS` fallback because rows created before the field existed
	carry NULL, and the intent for those is the current default, not Standard.

	If recipient count is <= provider threshold (when set > 0), it routes via
	Frappe Standard instead of AWS.
	"""
	if (doc.get("delivery_engine") or "AWS") != "AWS":
		return False

	count = resolve_recipient_count(doc, rows)
	threshold = get_min_recipient_threshold()
	if threshold > 0 and count <= threshold:
		frappe.logger().info(
			f"aws_pipeline: {doc.doctype} {doc.name} has {count} recipient(s) "
			f"(<= {threshold}) — routing via Frappe Standard engine"
		)
		if getattr(doc, "name", None) and hasattr(doc, "db_set") and doc.get("delivery_engine") == "AWS":
			try:
				doc.db_set("delivery_engine", "Standard", update_modified=False)
			except Exception:
				pass
		return False

	if is_ready(manifest.spec(doc.doctype)["channel"]):
		return True

	frappe.logger().warning(
		f"aws_pipeline: {doc.doctype} {doc.name} is set to AWS but this site is not "
		"configured for it (provider_url / api_key / plan feature) — falling back to Standard"
	)
	return False


def _require_feature(channel: str) -> None:
	"""Local gate, for a clear early failure.

	This is a courtesy check, not the enforcement point. The provider
	re-validates the plan in `trigger_campaign` before it will enqueue
	anything — that is the check that matters, because it runs where the plan
	data and the AWS credentials both live, on infrastructure the customer
	does not control. This one just fails fast, before building a manifest
	that would be refused.
	"""
	from msuite_core.services.plans import plan_enforcer

	if not plan_enforcer.is_bulk_enabled(channel):
		frappe.throw(_("Bulk Messaging is not available on your current plan."))


def _check_size(body: str, max_bytes) -> None:
	"""Fail before a long upload that S3 will reject at the end of it.

	The presigned URL carries the provider's ceiling regardless; checking here
	turns a confusing mid-upload 403 into a clear message, and does not waste
	the bandwidth first.
	"""
	limit = int(max_bytes or 0)
	if not limit:
		return
	size = len(body.encode("utf-8"))
	if size > limit:
		frappe.throw(
			_("Manifest is {0} bytes, over the {1} byte limit. Split the blast or use the Standard engine.").format(
				size, limit
			)
		)


def _mark_queued(row_doctype: str, names: list[str]) -> int:
	"""Flip the exported rows to Queued in bulk.

	Exact row names rather than a status predicate: a duplicate address is
	dropped from the manifest but still matches `status = 'Failed'`, and
	flipping it to Queued would strand it there forever.

	`row_doctype` comes from `manifest._SPEC`, never from caller input — it is
	interpolated into the table name, which no placeholder can parameterise.
	"""
	names = [n for n in names if n]
	for start in range(0, len(names), _FLIP_CHUNK):
		chunk = names[start : start + _FLIP_CHUNK]
		frappe.db.sql(
			f"UPDATE `tab{row_doctype}` SET status = 'Queued' WHERE name IN %(names)s",
			{"names": tuple(chunk)},
		)
	return len(names)


def record_bulk_outbound_messages(bulk_doctype: str, bulk_name: str) -> None:
	"""Record outbound Channel Message and Conversation rows for each recipient in a bulk campaign.

	Ensures that when a unified broadcast or bulk message is dispatched via AWS execution plane,
	agents opening the Inbox conversation with any recipient can see the exact template,
	rendered text, and message details sent to that contact.
	"""
	if not frappe.db.exists(bulk_doctype, bulk_name):
		return

	from msuite_campaigns.campaign.services.relationships import get_bulk_adapter

	record = (get_bulk_adapter(bulk_doctype) or {}).get("record_outbound")
	if record:
		record(frappe.get_doc(bulk_doctype, bulk_name))

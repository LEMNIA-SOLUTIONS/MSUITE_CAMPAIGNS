# Copyright (c) 2026, MSuite and contributors
# For license information, please see license.txt

"""
Unified dispatch hub — one audience, N channels, one action.

What this module does: resolve the umbrella campaign, re-validate the
audience, materialize one blast per selected channel, submit them.

What it deliberately does NOT do: queue anything, send anything, or manage a
blast's lifecycle. `doc.submit()` hands control to each engine's existing
`on_submit`, and pause/resume/stop/rollup route through the
`campaign_channel_adapters` registry that already exists in `hooks.py`. The
hub is a seam, not an engine — adding orchestration here would create the
second dispatcher the architecture notes warn about.

Channel availability is discovered, not hard-coded: a channel is offered when
its bulk doctype exists. That is what lets Phase 3 add SMS without editing
this file.
"""

from __future__ import annotations

import frappe
from frappe import _

from msuite_campaigns.dispatch import materializer

AUDIENCE_DOCTYPE = "Audience"

# Window given to a campaign the hub creates on the fly. Marketing Campaign
# validates end > start; a one-shot blast has no natural end date, so this is
# a nominal window rather than a meaningful schedule.
DEFAULT_WINDOW_DAYS = 30


def available_channels() -> list[str]:
	"""Channels installed on this site, in a stable order."""
	return [
		key
		for key in ("whatsapp", "sms", "email")
		if frappe.db.exists("DocType", materializer.CHANNEL_DOCTYPE[key])
	]


def _authorize(payload: dict, channels: list[str]) -> None:
	audience = payload.get("audience")
	if not audience:
		frappe.throw(_("An audience is required."))
	frappe.has_permission(AUDIENCE_DOCTYPE, "read", doc=audience, throw=True)

	for channel in channels:
		doctype = materializer.CHANNEL_DOCTYPE.get(channel)
		if doctype:
			frappe.has_permission(doctype, "create", throw=True)

	mc = payload.get("marketing_campaign")
	if mc:
		frappe.has_permission("Marketing Campaign", "write", doc=mc, throw=True)


def preview(payload: dict) -> dict:
	"""Everything `launch` would do, without writing anything.

	Powers the wizard's Review step. It calls the same
	`members_for` / `rejection_breakdown` the real dispatch uses, so the
	preview can never disagree with what actually gets sent.
	"""
	channels = _requested_channels(payload)
	_authorize(payload, channels)
	audience = _load_audience(payload)
	policy = payload.get("risk_policy") or {}

	out = {}
	for channel in channels:
		label = materializer.CHANNEL_LABEL[channel]
		members = audience.members_for(label, policy)
		out[channel] = {
			"queued": len(members),
			"reasons": audience.rejection_breakdown(label, policy),
		}
		out[channel]["dropped"] = sum(out[channel]["reasons"].values())

	return {
		"audience": audience.name,
		"total_members": len(audience.members or []),
		"channels": out,
	}


def prepare(payload: dict) -> dict:
	"""Fast, synchronous validation + campaign creation.

	Runs inside the web request so the user gets immediate feedback on bad
	input (missing template, unknown channel, plan gate). Returns everything
	the background job needs to materialise and submit the blasts.
	"""
	channels = _requested_channels(payload)
	_authorize(payload, channels)
	_load_audience(payload)  # validates existence
	_check_plan(channels)
	_validate_channel_configs(channels, payload)
	campaign = _resolve_campaign(payload)

	# Stamp the campaign as Preparing so the frontend can poll.
	frappe.db.set_value(
		"Marketing Campaign", campaign,
		{"launch_status": "Preparing", "launch_error": ""},
		update_modified=False,
	)

	return {
		"campaign": campaign,
		"audience": payload.get("audience"),
		"channels": channels,
	}


def launch(payload: dict) -> dict:
	"""Create and submit one blast per selected channel.

	Steps, in order:
	  1. gate on the plan, once, before any write
	  2. resolve or create the Marketing Campaign umbrella
	  3. re-validate the audience (cache-served, so effectively free)
	  4. materialize + submit each channel, each in its own savepoint
	  5. link the blasts back onto the campaign
	"""
	if payload.get("dry_run"):
		return preview(payload)

	channels = _requested_channels(payload)
	_authorize(payload, channels)
	audience = _load_audience(payload)

	_check_plan(channels)
	_validate_channel_configs(channels, payload)

	campaign = _resolve_campaign(payload)

	# Cheap because every verdict is cache-served — but an audience imported
	# weeks ago may carry stale verdicts, and this is the last point at which
	# a bad address can still be stopped for free.
	audience.validate_members()
	audience.reload()

	results = {}
	for channel in channels:
		results[channel] = _launch_one(channel, campaign, audience, payload)

	_link_blasts(campaign, results)

	queued_total = sum(r.get("queued", 0) for r in results.values())
	errors = [f"{channel}: {r.get('error')}" for channel, r in results.items() if r.get("error")]
	statuses = [r.get("status") for r in results.values()]

	if all(s == "Failed" for s in statuses):
		launch_status = "Failed"
		campaign_status = "Failed"
	elif any(s == "Failed" for s in statuses):
		launch_status = "Partially Failed"
		campaign_status = "Partially Failed"
	else:
		launch_status = "Launched"
		campaign_status = "Scheduled" if payload.get("start_date") and frappe.utils.get_datetime(payload["start_date"]) > frappe.utils.now_datetime() else "In Progress"

	error_msg = "; ".join(errors)[:500] if errors else ""

	frappe.db.set_value(
		"Marketing Campaign", campaign,
		{
			"status": campaign_status,
			"launch_status": launch_status,
			"launch_error": error_msg,
			"queued_total": queued_total,
		},
		update_modified=False,
	)

	return {
		"campaign": campaign,
		"audience": audience.name,
		"channels": results,
		"queued_total": queued_total,
	}


def launch_async(payload: dict, campaign: str) -> None:
	"""Background-job entry point: materialise + submit all channels.

	Called by `frappe.enqueue` from the API layer. The campaign already
	exists (created by `prepare`), so this picks it up and does the heavy
	work outside the web worker.
	"""
	try:
		channels = _requested_channels(payload)
		audience = _load_audience(payload)

		audience.validate_members()
		audience.reload()

		results = {}
		for channel in channels:
			results[channel] = _launch_one(channel, campaign, audience, payload)

		_link_blasts(campaign, results)

		queued_total = sum(r.get("queued", 0) for r in results.values())
		errors = [f"{channel}: {r.get('error')}" for channel, r in results.items() if r.get("error")]
		statuses = [r.get("status") for r in results.values()]

		if all(s == "Failed" for s in statuses):
			launch_status = "Failed"
			campaign_status = "Failed"
		elif any(s == "Failed" for s in statuses):
			launch_status = "Partially Failed"
			campaign_status = "Partially Failed"
		else:
			launch_status = "Launched"
			campaign_status = "Scheduled" if payload.get("start_date") and frappe.utils.get_datetime(payload["start_date"]) > frappe.utils.now_datetime() else "In Progress"

		error_msg = "; ".join(errors)[:500] if errors else ""

		frappe.db.set_value(
			"Marketing Campaign", campaign,
			{
				"status": campaign_status,
				"launch_status": launch_status,
				"launch_error": error_msg,
				"queued_total": queued_total,
			},
			update_modified=False,
		)
		frappe.db.commit()
	except Exception as e:
		frappe.db.rollback()
		frappe.log_error(
			title=f"Dispatch hub: async launch failed for {campaign}",
			message=frappe.get_traceback(),
		)
		frappe.db.set_value(
			"Marketing Campaign", campaign,
			{"status": "Failed", "launch_status": "Failed", "launch_error": str(e)[:500]},
			update_modified=False,
		)
		frappe.db.commit()


def get_launch_status(campaign: str) -> dict:
	"""Poll endpoint for the frontend to check async launch progress."""
	row = frappe.db.get_value(
		"Marketing Campaign", campaign,
		["launch_status", "launch_error", "queued_total",
		 "whatsapp_blast", "email_blast", "sms_blast"],
		as_dict=True,
	)
	if not row:
		frappe.throw(_("Campaign {0} not found.").format(campaign))
	return dict(row)


def _launch_one(channel: str, campaign: str, audience, payload: dict) -> dict:
	"""One channel, wrapped in a savepoint.

	A failure in one channel must not roll back the others — a marketer whose
	email blast went out should not discover it was silently undone because
	the WhatsApp template was rejected.
	"""
	config = dict((payload.get("channels") or {}).get(channel) or {})
	config.setdefault("risk_policy", payload.get("risk_policy"))
	config.setdefault("title", payload.get("campaign_name"))

	savepoint = f"dispatch_{channel}"
	frappe.db.savepoint(savepoint)
	try:
		result = materializer.build(channel, campaign, audience, config)
		if result.get("name"):
			doc = frappe.get_doc(materializer.CHANNEL_DOCTYPE[channel], result["name"])
			# perm: authorised in _authorize
			doc.flags.ignore_permissions = True
			doc.submit()
			result["status"] = doc.status
		else:
			result["status"] = "Skipped"
			result["error"] = "No reachable recipients for this channel"
		return result
	except Exception as e:
		frappe.db.rollback(save_point=savepoint)
		frappe.log_error(
			title=f"Dispatch hub: {channel} failed",
			message=frappe.get_traceback(),
		)
		return {"name": None, "queued": 0, "dropped": 0, "status": "Failed", "error": str(e)[:300]}


def _requested_channels(payload: dict) -> list[str]:
	requested = list((payload.get("channels") or {}).keys())
	if not requested:
		frappe.throw(_("Select at least one channel."))

	available = available_channels()
	unknown = [c for c in requested if c not in available]
	if unknown:
		frappe.throw(
			_("Channel(s) not available on this site: {0}").format(", ".join(unknown))
		)
	# Preserve the canonical order rather than dict insertion order, so two
	# identical launches produce identically ordered reports.
	return [c for c in available if c in requested]


# Fields each engine marks `reqd` on its bulk doctype. Checked up front so a
# missing template fails with one clear message instead of a Frappe
# MandatoryError raised after other channels have already been created.
_REQUIRED_CONFIG = {
	"whatsapp": (("template", "a WhatsApp template"),),
	"email": (("template", "an email template"), ("account", "a sender email account")),
	"sms": (("template", "an SMS template"), ("account", "an SMS account")),
}


def _validate_channel_configs(channels: list[str], payload: dict) -> None:
	problems = []
	for channel in channels:
		config = (payload.get("channels") or {}).get(channel) or {}
		for field, description in _REQUIRED_CONFIG.get(channel, ()):
			if not config.get(field):
				problems.append(f"{materializer.CHANNEL_LABEL[channel]} needs {description}.")
	if problems:
		frappe.throw("<br>".join(problems))


def _load_audience(payload: dict):
	name = payload.get("audience")
	if not name:
		frappe.throw(_("An audience is required."))
	if not frappe.db.exists(AUDIENCE_DOCTYPE, name):
		frappe.throw(_("Audience {0} not found.").format(name))
	return frappe.get_doc(AUDIENCE_DOCTYPE, name)


def _check_plan(channels: list[str]) -> None:
	"""Gate once, up front, before any document is written.

	Every requested channel must have bulk on its own product — a launch
	that would half-fail is refused whole. Each blast's own `before_submit`
	hook still runs its size check.
	"""
	from msuite_core.services.plans import plan_enforcer

	blocked = [c for c in channels if not plan_enforcer.is_bulk_enabled(c)]
	if blocked:
		frappe.throw(
			_("Bulk Messaging is not available on your current plan for: {0}.").format(
				", ".join(materializer.CHANNEL_LABEL[c] for c in blocked)
			)
		)


def _resolve_campaign(payload: dict) -> str:
	existing = payload.get("marketing_campaign")
	if existing:
		if not frappe.db.exists("Marketing Campaign", existing):
			frappe.throw(_("Marketing Campaign {0} not found.").format(existing))
		return existing

	from msuite_campaigns.campaign.doctype.marketing_campaign.marketing_campaign import (
		make_unique_campaign_name,
	)

	doc = frappe.new_doc("Marketing Campaign")
	doc.campaign_name = make_unique_campaign_name(payload.get("campaign_name") or f"Campaign {frappe.utils.now()}")
	start = payload.get("start_date") or frappe.utils.now_datetime()
	if payload.get("start_date") and frappe.utils.get_datetime(payload["start_date"]) > frappe.utils.now_datetime():
		doc.status = "Scheduled"
	else:
		doc.status = "In Progress"
	doc.audience = payload.get("audience")
	# Marketing Campaign requires start AND end, with end strictly after start.
	# A launch with no explicit schedule starts now — the campaign is being
	# fired, not planned — and gets a nominal window so the umbrella stays
	# In Progress while its blasts drain. The marketer can shorten it afterwards.
	doc.start_date = start
	doc.end_date = payload.get("end_date") or frappe.utils.add_to_date(start, days=DEFAULT_WINDOW_DAYS)
	# perm: authorised in _authorize
	doc.flags.ignore_permissions = True
	doc.insert()
	return doc.name


def _link_blasts(campaign: str, results: dict) -> None:
	"""Write each blast onto its Marketing Campaign link field."""
	values = {}
	for channel, result in results.items():
		field = materializer.CAMPAIGN_LINK_FIELD.get(channel)
		if field and result.get("name"):
			values[field] = result["name"]
	if values:
		frappe.db.set_value("Marketing Campaign", campaign, values, update_modified=False)


# ── Cross-channel lifecycle — thin fan-out over the existing adapters ──


def _adapters():
	from msuite_campaigns.campaign.services import relationships

	return relationships.get_adapters()


def fan_out(campaign: str, action: str) -> dict:
	"""Run `action` across every registered channel adapter.

	`pause` / `resume` / `stop` already exist per channel; this calls
	them and updates the umbrella Marketing Campaign status accordingly.
	"""
	if action not in ("pause", "resume", "stop", "retry"):
		frappe.throw(_("Unsupported action: {0}").format(action))

	if action == "retry":
		return retry_campaign(campaign)

	out = {}
	for key, adapter in _adapters().items():
		fn = adapter.get(action)
		if not fn:
			continue
		try:
			out[key] = fn(campaign)
		except Exception as e:
			frappe.log_error(title=f"Dispatch fan-out {action} failed for {key}", message=frappe.get_traceback())
			out[key] = [{"outcome": "failed", "message": str(e)[:200]}]

	# Update umbrella campaign status
	if frappe.db.exists("Marketing Campaign", campaign):
		if action == "pause":
			frappe.db.set_value("Marketing Campaign", campaign, "status", "Paused", update_modified=False)
			frappe.db.commit()
		elif action == "resume":
			mc = frappe.get_doc("Marketing Campaign", campaign)
			new_status = "Scheduled" if mc.start_date and frappe.utils.get_datetime(mc.start_date) > frappe.utils.now_datetime() else "In Progress"
			frappe.db.set_value("Marketing Campaign", campaign, "status", new_status, update_modified=False)
			frappe.db.commit()
		elif action == "stop":
			frappe.db.set_value("Marketing Campaign", campaign, "status", "Cancelled", update_modified=False)
			frappe.db.commit()

	return out


def retry_campaign(campaign: str) -> dict:
	"""Recover and re-trigger a failed or partially failed campaign and all child blasts."""
	if not frappe.db.exists("Marketing Campaign", campaign):
		frappe.throw(_("Marketing Campaign {0} not found.").format(campaign))

	mc = frappe.get_doc("Marketing Campaign", campaign)
	out = {}

	# Clear launch error upfront
	frappe.db.set_value(
		"Marketing Campaign", campaign,
		{"launch_error": "", "launch_status": "Launched", "status": "In Progress"},
		update_modified=False,
	)
	frappe.db.commit()

	linked = [
		("whatsapp", mc.whatsapp_blast, "Bulk WhatsApp Message"),
		("email", mc.email_blast, "Bulk Email Message"),
		("sms", mc.sms_blast, "Bulk SMS Message"),
	]

	for ch_key, blast_name, doctype in linked:
		if not blast_name or not frappe.db.exists(doctype, blast_name):
			continue
		try:
			doc = frappe.get_doc(doctype, blast_name)
			if doc.docstatus == 0:
				doc.flags.ignore_permissions = True
				doc.submit()
				out[ch_key] = {"outcome": "fired", "status": doc.status}
			elif doc.docstatus == 1:
				if doc.get("delivery_engine") == "AWS":
					# "Queued" is this engine's PERMANENT resting status, even on
					# total success (aws_pipeline/rollup.py: AWS-routed sends
					# never write per-recipient status back). Treating it, or
					# "Failed"/"Partially Failed"/"Draft", as retry-eligible means
					# export_and_enqueue(doc) with no `rows` re-derives the FULL
					# sendable set every time and resends to 100% of the original
					# recipients on every retry click. Only a genuine, durably
					# recorded dispatch failure is safe to retry.
					if doc.get("status") != "AWS Dispatch Failed":
						out[ch_key] = {"outcome": "skipped", "status": doc.status}
						continue
					from msuite_campaigns.aws_pipeline import dispatch as aws_dispatch
					try:
						aws_dispatch.export_and_enqueue(doc)
					except Exception:
						doc.db_set("status", "AWS Dispatch Failed", update_modified=False)
						raise
					doc.db_set("status", "Queued", update_modified=False)
					out[ch_key] = {"outcome": "fired", "status": "Queued"}
				elif hasattr(doc, "retry_failed"):
					doc.retry_failed()
					out[ch_key] = {"outcome": "retried", "status": doc.status}
				elif hasattr(doc, "queue_messages"):
					doc.db_set("status", "Queued", update_modified=False)
					doc.queue_messages()
					out[ch_key] = {"outcome": "fired", "status": "Queued"}
		except Exception as e:
			frappe.log_error(title=f"Retry failed for {ch_key} ({blast_name})", message=frappe.get_traceback())
			out[ch_key] = {"outcome": "failed", "error": str(e)[:300]}

	# Settle status based on results
	has_errors = any(r.get("outcome") == "failed" for r in out.values())
	if has_errors:
		errors = [f"{ch}: {r.get('error')}" for ch, r in out.items() if r.get("error")]
		frappe.db.set_value(
			"Marketing Campaign", campaign,
			{"launch_error": "; ".join(errors)[:500], "launch_status": "Partially Failed", "status": "Partially Failed"},
			update_modified=False,
		)
	else:
		frappe.db.set_value(
			"Marketing Campaign", campaign,
			{"launch_error": "", "launch_status": "Launched", "status": "In Progress"},
			update_modified=False,
		)
	frappe.db.commit()

	return {"campaign": campaign, "channels": out}


def unified_progress(campaign: str) -> dict:
	"""Merged rollup across every channel under one campaign."""
	out = {}
	for key, adapter in _adapters().items():
		fn = adapter.get("rollup")
		if not fn:
			continue
		try:
			out[key] = fn(campaign, 30)
		except Exception:
			frappe.log_error(title=f"Dispatch rollup failed for {key}", message=frappe.get_traceback())
			out[key] = {}
	return {"campaign": campaign, "channels": out}


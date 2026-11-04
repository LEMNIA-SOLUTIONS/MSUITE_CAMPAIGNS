# Copyright (c) 2026, MSuite and contributors
# For license information, please see license.txt

"""
Provider-brokered access to the AWS execution plane.

**This site holds no AWS credentials.** Not a key, not an STS token, not the
bucket name, not the queue URL. There is one AWS account behind every client
workspace, so an AWS credential is a *cross-tenant* credential: a client site
holding one could read another client's manifests or drain the shared queue.
That is the same reason Meta app secrets never reach a client while per-user
tokens do.

So the flow is brokered, in three steps:

  1. ask the provider for a presigned S3 PUT URL for one key
  2. upload the manifest straight to S3 with it — no credentials involved, and
     a 300 MB blast never crosses the provider
  3. ask the provider to enqueue it; the provider re-checks the plan, verifies
     the key is inside this client's own prefix, and does the SQS send with
     its own credentials

There is deliberately no boto3 here. A presigned PUT is an ordinary HTTP PUT,
and the only other call is a signed JSON POST the app already knows how to
make — so the AWS SDK is not a dependency of this app at all.

Provider endpoints: `msuite/api/v1/bulk_messaging.py`.
"""

from __future__ import annotations

import frappe
import requests
from frappe import _

UPLOAD_URL_ENDPOINT = "msuite.api.v1.bulk_messaging.request_upload_url"
TRIGGER_ENDPOINT = "msuite.api.v1.bulk_messaging.trigger_campaign"
SYNC_SECRET_ENDPOINT = "msuite.api.v1.bulk_messaging.sync_channel_secret"

# A manifest upload is a single large PUT; the provider's presign expiry
# (default 900s) is the real ceiling, this just stops a hung socket holding a
# worker forever.
UPLOAD_TIMEOUT = 600


def sync_channel_secret(channel: str, credentials: dict) -> dict:
	"""Ask the provider to store/update this client's channel credentials in AWS Secrets Manager."""
	return _call_provider(
		SYNC_SECRET_ENDPOINT,
		{
			"channel": channel,
			"credentials": credentials,
		},
	)



def request_upload_url(channel: str, bulk_doctype: str, bulk_doc_name: str) -> dict:
	"""`{url, key, expires_in, max_bytes}` for one manifest."""
	return _call_provider(
		UPLOAD_URL_ENDPOINT,
		{"channel": channel, "bulk_doctype": bulk_doctype, "bulk_doc_name": bulk_doc_name},
	)


def upload_manifest(url: str, body: str) -> None:
	"""PUT the NDJSON straight to S3.

	Content-Type must match what the presign was signed with, or S3 rejects
	the request with a signature mismatch that reads like an auth failure.
	"""
	try:
		resp = requests.put(
			url,
			data=body.encode("utf-8"),
			headers={"Content-Type": "application/x-ndjson"},
			timeout=UPLOAD_TIMEOUT,
		)
	except requests.RequestException as e:
		frappe.throw(_("Manifest upload failed: {0}").format(str(e)[:300]))

	if resp.status_code not in (200, 201):
		# S3 answers with an XML error body; the first 300 chars carry the code.
		frappe.throw(
			_("Manifest upload rejected by S3 (HTTP {0}): {1}").format(
				resp.status_code, (resp.text or "")[:300]
			)
		)


def trigger_campaign(
	key: str, channel: str, bulk_doctype: str, bulk_doc_name: str, campaign_id: str, recipient_count: int
) -> dict:
	"""Ask the provider to dispatch an uploaded manifest. `{message_id, ...}`."""
	return _call_provider(
		TRIGGER_ENDPOINT,
		{
			"key": key,
			"channel": channel,
			"bulk_doctype": bulk_doctype,
			"bulk_doc_name": bulk_doc_name,
			"campaign_id": campaign_id,
			"recipient_count": recipient_count,
		},
	)


def _call_provider(endpoint: str, data: dict) -> dict:
	"""Signed POST to the provider, same helper the Gmail relay uses.

	`client_name` is added here rather than by callers — the provider needs it
	to resolve which client is asking, and it is not a per-call decision.
	"""
	from msuite_core.integrations.provider import call_provider_relay, client_name

	return call_provider_relay(endpoint, {"client_name": client_name(), **data})

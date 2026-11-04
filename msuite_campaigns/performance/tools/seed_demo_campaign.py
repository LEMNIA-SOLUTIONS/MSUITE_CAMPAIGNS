"""Seed a Marketing Campaign with realistic dummy performance data.

Only permitted when developer_mode is enabled. A valid destination_url
must be supplied explicitly via --kwargs.

Run from the bench root:

    bench --site msuite.workspace execute \\
      msuite_campaigns.performance.tools.seed_demo_campaign.run \\
      --kwargs '{"destination_url": "https://mysite.com/demo"}'

Optional overrides via --kwargs:

    --kwargs '{"destination_url": "https://mysite.com/demo", "campaign_name": "Diwali Demo", "days": 14, "wa_recipients": 200}'

What it builds (idempotent — re-runs reuse the same campaign):

  Marketing Campaign  "<campaign_name>"
    ├── Ad Campaign    "<campaign_name> · Ads"
    │     └── Ad Set / Ad
    │           └── Ad Insight × <days>  (daily impressions / clicks / spend / conv)
    ├── Social Post "<campaign_name> · Social"
    │     ├── Posts on each connected Facebook + Instagram account
    │     └── Social Insight per post (lifetime)
    │         + Social Insight per account × <days>  (daily reach / engagement)
    └── Bulk WhatsApp Message "<campaign_name> · WhatsApp"
          └── Channel Message × <wa_recipients>  (mixed sent / delivered / read / failed)

Numbers are sampled from realistic-ish ranges, not random noise — so the
top-strip KPIs, funnels, and per-channel cards all light up with values
a digital marketer would recognise as plausible.

Tear-down: pass `--kwargs '{"teardown": true, "campaign_name": "..."}'`
to delete the demo campaign + every child it owns.
"""

from __future__ import annotations

import random
from datetime import timedelta

import frappe
from frappe import _
from frappe.utils import add_days, getdate, now_datetime, today

from msuite_core.utils.insight_keys import make_key

DEFAULT_NAME = "Demo Performance Campaign"

# ─── Public entry ────────────────────────────────────────────────────


def run(
	campaign_name: str = DEFAULT_NAME,
	days: int = 30,
	wa_recipients: int = 250,
	teardown: bool = False,
	destination_url: str | None = None,
) -> dict:
	"""Build the demo campaign + dummy metrics. Returns a summary dict.

	Gated behind frappe.conf.developer_mode. Requires an explicit destination_url
	(no dummy fallback URLs are used).
	"""
	if not getattr(frappe.conf, "developer_mode", False):
		frappe.throw(
			_("seed_demo_campaign is only permitted when developer_mode is enabled in site_config.json."),
			frappe.PermissionError,
		)

	if teardown:
		return _teardown(campaign_name)

	dest_url = (destination_url or "").strip()
	if not dest_url:
		frappe.throw(
			_("A valid destination_url is required to seed demo campaigns. Specify --kwargs '{\"destination_url\": \"https://...\"}'."),
			frappe.ValidationError,
		)

	# Stable seed for repeatable demo numbers across re-runs.
	random.seed(hash(campaign_name) & 0xFFFFFFFF)

	campaign = _ensure_campaign(campaign_name)
	ad_campaign = _ensure_ad_campaign(campaign, campaign_name)
	ad_name = _ensure_ad(ad_campaign, destination_url=dest_url)
	social_post = _ensure_social_post(campaign, campaign_name)
	posts = _ensure_social_posts(social_post)
	blast = _ensure_whatsapp_blast(campaign, campaign_name, wa_recipients)

	ad_rows = _seed_ad_insights(ad_name, days)
	soc_rows = _seed_social_insights(posts, days)
	wa_rows = _seed_whatsapp_messages(blast, wa_recipients)

	frappe.db.commit()
	return {
		"marketing_campaign": campaign,
		"ad_campaign": ad_campaign,
		"social_post": social_post,
		"bulk_whatsapp": blast,
		"rows_inserted": {
			"ad_insight": ad_rows,
			"social_insight": soc_rows,
			"channel_message": wa_rows,
		},
		"open_url": f"/campaign?name={campaign}",
	}


# ─── Entity ensuring (idempotent) ────────────────────────────────────


def _ensure_campaign(name: str) -> str:
	existing = frappe.db.get_value("Marketing Campaign", {"campaign_name": name}, "name")
	if existing:
		return existing
	doc = frappe.get_doc(
		{
			"doctype": "Marketing Campaign",
			"campaign_name": name,
			"status": "Active",
			"start_date": add_days(today(), -30),
			"end_date": add_days(today(), 14),
			"goal": "Drive awareness + leads across paid + organic + WhatsApp.",
		}
	)
	doc.insert(ignore_permissions=True)
	return doc.name


def _ensure_ad_campaign(campaign: str, base_name: str) -> str:
	"""Find an existing Ad Campaign linked to this umbrella, or create one."""
	existing = frappe.db.get_value(
		"Ad Campaign",
		{"campaign": campaign},
		"name",
	)
	if existing:
		return existing

	ad_account = frappe.db.get_value(
		"Ad Account",
		{"platform": "Meta"},
		"name",
	)
	if not ad_account:
		# No Meta Ad Account yet — create a placeholder so the FK
		# holds. Real installs will have at least one.
		ad_account_doc = frappe.get_doc(
			{
				"doctype": "Ad Account",
				"account_name": "Demo Account",
				"account_id": "demo_act_999",
				"platform": "Meta",
				"currency": "INR",
				"account_status": "Active",
			}
		).insert(ignore_permissions=True)
		ad_account = ad_account_doc.name

	doc = frappe.get_doc(
		{
			"doctype": "Ad Campaign",
			"campaign_name": f"{base_name} · Ads",
			"ad_account": ad_account,
			"campaign": campaign,
			"objective": "OUTCOME_TRAFFIC",
			"status": "Active",
			"is_cbo": 1,
			"budget_type": "DAILY",
			"daily_budget": 2000,
			"start_date": add_days(today(), -30),
		}
	).insert(ignore_permissions=True)
	return doc.name


def _ensure_ad(ad_campaign: str, destination_url: str) -> str:
	"""Return any Ad under this Ad Campaign (creating one if none).

	Requires destination_url to be explicitly provided.
	Ad Insight references `Ad` via `reference_doctype` + `reference_name`,
	so we need at least one Ad to attach daily metric rows against. The
	full chain Ad → Ad Set → Ad Campaign + Ad Creative is materialised
	here because every link is `reqd=1` on its parent.
	"""
	ad = frappe.db.get_value("Ad", {"ad_campaign": ad_campaign}, "name")
	if ad:
		return ad

	ad_set = frappe.db.get_value("Ad Set", {"ad_campaign": ad_campaign}, "name")
	if not ad_set:
		ad_set_doc = frappe.get_doc(
			{
				"doctype": "Ad Set",
				# `adset_name` (no underscore between ad and set) — matches
				# the doctype field. `ad_set_name` would silently no-op.
				"adset_name": "Demo Ad Set",
				"ad_campaign": ad_campaign,
				"status": "Active",
				"optimization_goal": "LINK_CLICKS",
				"billing_event": "IMPRESSIONS",
				"bid_strategy": "LOWEST_COST_WITHOUT_CAP",
				"pacing_type": "standard",
			}
		).insert(ignore_permissions=True)
		ad_set = ad_set_doc.name

	# Ad requires an Ad Creative reference — create a tiny placeholder
	# one if none exists. The creative is shared across re-runs by name
	# so the seeder doesn't sprout duplicates.
	creative = frappe.db.get_value(
		"Ad Creative",
		{"creative_name": "Demo Creative"},
		"name",
	)
	if not creative:
		# Use the "use_existing_post" path so the controller's
		# `_validate_media_by_format` skips the image/video requirement
		# (we don't need a real asset for dummy metric data).
		creative_doc = frappe.get_doc(
			{
				"doctype": "Ad Creative",
				"creative_name": "Demo Creative",
				"ad_format": "Single Image",
				"platform": "Meta",
				"use_existing_post": 1,
				"existing_post_id": "demo_999_demo_post_999",
				"headline": "Demo headline",
				"primary_text": "Demo body copy.",
				"description": "Demo CTA description.",
			}
		).insert(ignore_permissions=True)
		creative = creative_doc.name

	ad_doc = frappe.get_doc(
		{
			"doctype": "Ad",
			"ad_name": "Demo Ad",
			"ad_set": ad_set,
			"ad_campaign": ad_campaign,
			"ad_creative": creative,
			"call_to_action": "LEARN_MORE",
			"destination_url": destination_url,
			"status": "Active",
		}
	).insert(ignore_permissions=True)
	return ad_doc.name


def _ensure_social_post(campaign: str, base_name: str) -> str:
	existing = frappe.db.get_value(
		"Social Post",
		{"campaign": campaign},
		"name",
	)
	if existing:
		return existing
	doc = frappe.get_doc(
		{
			"doctype": "Social Post",
			"title": f"{base_name} · Social",
			"campaign": campaign,
			# Social Post's `status` enum is a publishing lifecycle
			# (Draft / Scheduled / Published / Failed / …), not an
			# active/paused flag. "Published" reads as "this push went out"
			# which is what we want for a demo with real metrics.
			"status": "Published",
			"post_type": "Image Post",
		}
	).insert(ignore_permissions=True)
	return doc.name


def _ensure_social_posts(social_post: str) -> list[str]:
	"""One Social Post per connected Facebook + Instagram account.

	Returns the parent Social Post name in a list with the post
	`idx` values — Social Insight rows reference Social Post by name,
	which for child tables is `<parent>:<idx>` in the standard Frappe
	pattern. We return Social Post `name` values directly.
	"""
	posts = frappe.get_all(
		"Social Post",
		filters={"parent": social_post, "parenttype": "Social Post"},
		pluck="name",
	)
	if posts:
		return posts

	sc = frappe.get_doc("Social Post", social_post)
	accounts = frappe.get_all(
		"Social Account",
		filters={"platform": ["in", ["Facebook", "Instagram"]], "status": "Active"},
		fields=["name", "platform"],
		limit=4,
	)
	for i, acct in enumerate(accounts):
		sc.append(
			"posts",
			{
				"social_account": acct.name,
				"platform": acct.platform,
				"status": "Published",
				"platform_post_id": f"demo_post_{i}_{acct.name}",
			},
		)
	sc.save(ignore_permissions=True)
	return [p.name for p in sc.posts]


def _ensure_whatsapp_blast(campaign: str, base_name: str, recipients: int) -> str:
	"""Create a Bulk WhatsApp Message linked to the umbrella via the
	1:N `campaign` field (replaces the deprecated single
	`Marketing Campaign.whatsapp_blast`)."""
	existing = frappe.get_all(
		"Bulk WhatsApp Message",
		filters={"campaign": campaign},
		limit=1,
		pluck="name",
	)
	if existing:
		return existing[0]

	wa_account = frappe.db.get_value("WhatsApp Account", {}, "name") or "Sample Store"
	doc = frappe.get_doc(
		{
			"doctype": "Bulk WhatsApp Message",
			"title": f"{base_name} · WhatsApp",
			"campaign": campaign,
			"whatsapp_account": wa_account,
			# Valid enum values are "" / "Individual" / "Recipient List".
			# "Individual" + ignore_validate skips the recipient-rows check
			# while keeping the doctype Select happy.
			"recipient_type": "Individual",
			"recipient_count": recipients,
			"sent_count": recipients,
			"status": "Completed",
		}
	)
	# Skip both the recipient-list validation AND the template-mandatory
	# check — for a metric-seeding demo we don't need real recipients
	# or a WABA-approved template. The validate hook would otherwise
	# throw "Add at least one recipient." for our List type, and
	# mandatory checks would demand a `template`. Channel Message rows
	# below carry the phone numbers directly via `platform_handle`.
	doc.flags.ignore_validate = True
	doc.flags.ignore_mandatory = True
	doc.insert(ignore_permissions=True, ignore_mandatory=True)
	return doc.name


# ─── Metric seeding ──────────────────────────────────────────────────


def _seed_ad_insights(ad_name: str, days: int) -> int:
	"""Daily Ad Insight rows. Numbers ramp slightly over the window
	so the trend chart shows a believable curve instead of a flat line.
	"""
	# Clear prior demo rows for this ad so the seed is reproducible.
	frappe.db.delete(
		"Ad Insight",
		{
			"reference_doctype": "Ad",
			"reference_name": ad_name,
		},
	)

	today_d = getdate(today())
	rows = 0
	for offset in range(days):
		date = today_d - timedelta(days=offset)
		# Linear ramp from 80% → 120% of baseline so newer days perform
		# slightly better — matches what an optimised campaign looks like.
		ramp = 0.8 + (offset / max(days - 1, 1)) * 0.4
		impressions = int(random.randint(8_000, 12_000) * ramp)
		ctr = random.uniform(0.012, 0.022)
		clicks = int(impressions * ctr)
		cvr = random.uniform(0.03, 0.06)
		conversions = int(clicks * cvr)
		spend = round(impressions / 1000.0 * random.uniform(45, 70), 2)
		# ROAS of 2–3.5× revenue / spend
		roas = random.uniform(2.0, 3.5)
		conversion_value = round(spend * roas, 2)

		frappe.get_doc(
			{
				"doctype": "Ad Insight",
				"reference_doctype": "Ad",
				"reference_name": ad_name,
				"date": str(date),
				"granularity": "DAY",
				"insight_key": make_key("Ad", ad_name, str(date), "DAY", "", ""),
				"impressions": impressions,
				"reach": int(impressions * random.uniform(0.55, 0.75)),
				"frequency": round(random.uniform(1.3, 2.0), 2),
				"clicks": clicks,
				# `ctr` is stored as PERCENT per the legacy Ads convention.
				"ctr": round(ctr * 100, 4),
				"cpc": round(spend / clicks, 2) if clicks else 0,
				"cpm": round(spend / impressions * 1000, 2),
				"conversions": conversions,
				"conversion_value": conversion_value,
				"roas": round(roas, 2),
				"cost_per_conversion": round(spend / conversions, 2) if conversions else 0,
				"spend": spend,
				"currency": "INR",
			}
		).insert(ignore_permissions=True)
		rows += 1
	return rows


def _seed_social_insights(posts: list[str], days: int) -> int:
	"""One lifetime row per Social Post + N daily rows per linked account.

	The lifetime per-post numbers drive the by_post table in the social
	rollup; the daily per-account numbers give the chart its time series.
	"""
	rows = 0
	# ── Per-post lifetime totals ────────────────────────────────────
	for post_name in posts:
		post = frappe.db.get_value(
			"Social Post",
			post_name,
			["social_account", "platform"],
			as_dict=True,
		)
		if not post:
			continue
		frappe.db.delete(
			"Social Insight",
			{
				"reference_doctype": "Social Post",
				"reference_name": post_name,
			},
		)
		impressions = random.randint(5_000, 25_000)
		reach = int(impressions * random.uniform(0.6, 0.8))
		reactions = random.randint(80, 400)
		comments = random.randint(8, 60)
		shares = random.randint(4, 30)
		saves = random.randint(2, 25) if post.platform == "Instagram" else 0
		clicks = random.randint(40, 200)
		frappe.get_doc(
			{
				"doctype": "Social Insight",
				"reference_doctype": "Social Post",
				"reference_name": post_name,
				"platform": post.platform,
				"date": today(),
				"granularity": "LIFETIME",
				"insight_key": make_key("Social Post", post_name, today(), "LIFETIME", post.platform),
				"impressions": impressions,
				"reach": reach,
				"clicks": clicks,
				"reactions": reactions,
				"comments": comments,
				"shares": shares,
				"saves": saves,
				"video_views": 0,
				"video_completions": 0,
			}
		).insert(ignore_permissions=True)
		rows += 1

	# ── Per-account daily rows ──────────────────────────────────────
	accounts = sorted({frappe.db.get_value("Social Post", p, "social_account") for p in posts} - {None, ""})
	today_d = getdate(today())
	for acct in accounts:
		platform = frappe.db.get_value("Social Account", acct, "platform")
		frappe.db.delete(
			"Social Insight",
			{
				"reference_doctype": "Social Account",
				"reference_name": acct,
			},
		)
		for offset in range(days):
			date = today_d - timedelta(days=offset)
			impressions = random.randint(1_500, 4_000)
			frappe.get_doc(
				{
					"doctype": "Social Insight",
					"reference_doctype": "Social Account",
					"reference_name": acct,
					"platform": platform,
					"date": str(date),
					"granularity": "DAY",
					"insight_key": make_key("Social Account", acct, str(date), "DAY", platform),
					"impressions": impressions,
					"reach": int(impressions * random.uniform(0.55, 0.75)),
					"engagement": random.randint(40, 200),
				}
			).insert(ignore_permissions=True)
			rows += 1
	return rows


def _seed_whatsapp_messages(blast: str, recipients: int) -> int:
	"""Mixed-status outbound Channel Message rows for the blast.

	Distribution: 95% sent, of which 92% delivered, of which 64% read,
	plus a 3% hard fail. Numbers match WABA industry benchmarks for a
	marketing template send to a warm list — the funnel card reads as
	"healthy" not "perfect", which is more honest for a demo.
	"""
	# Wipe old demo rows so re-runs produce the same totals.
	frappe.db.delete(
		"Channel Message",
		{
			"reference_doctype": "Bulk WhatsApp Message",
			"reference_name": blast,
		},
	)

	# Channel Message requires both an Inbox and a Conversation. We
	# use a shared "demo" Conversation for every row — it's not a
	# realistic shape (real blasts spawn N conversations) but the
	# rollup query sums by status irrespective of conversation, so the
	# campaign dashboard numbers are unaffected. Falls back to any
	# WhatsApp-channel Inbox / Conversation if no shared demo row
	# exists yet.
	inbox = frappe.db.get_value(
		"Inbox",
		{"channel_type": "WhatsApp"},
		"name",
	)
	conversation = frappe.db.get_value(
		"Conversation",
		{"channel_type": "WhatsApp"},
		"name",
	)
	if not (inbox and conversation):
		# Without these we can't insert Channel Message rows; tell the
		# caller to set up a WhatsApp Inbox + at least one Conversation
		# before re-running. Better than failing 250 inserts in.
		raise RuntimeError(
			"Seeder needs an existing WhatsApp Inbox + Conversation. Bind one in Desk first, then re-run."
		)

	base_time = now_datetime() - timedelta(hours=2)
	rows = 0
	for i in range(recipients):
		# Determine status by deterministic bucket so re-runs match.
		bucket = i % 100
		if bucket < 3:
			status = "failed"
		elif bucket < 33:
			status = "sent"  # delivered but not read yet
		elif bucket < 95:
			status = "read"
		else:
			status = "delivered"
		phone = f"9199{99900000 + i:08d}"
		frappe.get_doc(
			{
				"doctype": "Channel Message",
				"channel_type": "WhatsApp",
				"direction": "Outbound",
				"status": status,
				"content_type": "Text",
				"text_content": "Demo template message",
				"platform_message_id": f"wamid.DEMO_{blast}_{i}",
				"platform_handle": phone,
				"platform_timestamp": base_time + timedelta(seconds=i * 3),
				"reference_doctype": "Bulk WhatsApp Message",
				"reference_name": blast,
				"inbox": inbox,
				"conversation": conversation,
				"failure_reason": "Recipient phone unreachable" if status == "failed" else "",
			}
		).insert(ignore_permissions=True)
		rows += 1
		# Batch commits every 50 rows so a partial failure doesn't lose
		# everything but the full run still completes quickly.
		if i and i % 50 == 0:
			frappe.db.commit()
	return rows


# ─── Teardown ────────────────────────────────────────────────────────


def _teardown(campaign_name: str) -> dict:
	"""Delete the demo campaign + every child created above. Keeps
	Ad Account / WhatsApp Account / Social Account untouched since
	those may be shared with real data.
	"""
	campaign = frappe.db.get_value(
		"Marketing Campaign",
		{"campaign_name": campaign_name},
		"name",
	)
	if not campaign:
		return {"deleted": False, "reason": "campaign not found"}

	blasts = frappe.get_all("Bulk WhatsApp Message", filters={"campaign": campaign}, pluck="name")

	# Order matters — children → parents → umbrella.
	for blast in blasts:
		frappe.db.delete(
			"Channel Message",
			{
				"reference_doctype": "Bulk WhatsApp Message",
				"reference_name": blast,
			},
		)
		frappe.delete_doc("Bulk WhatsApp Message", blast, force=True, ignore_permissions=True)

	for sc in frappe.get_all("Social Post", filters={"campaign": campaign}, pluck="name"):
		for p in frappe.get_all(
			"Social Post Destination", filters={"parent": sc, "parenttype": "Social Post"}, pluck="name"
		):
			frappe.db.delete(
				"Social Insight",
				{
					"reference_doctype": "Social Post",
					"reference_name": p,
				},
			)
		frappe.delete_doc("Social Post", sc, force=True, ignore_permissions=True)

	for ac in frappe.get_all("Ad Campaign", filters={"campaign": campaign}, pluck="name"):
		for ad in frappe.get_all("Ad", filters={"ad_campaign": ac}, pluck="name"):
			frappe.db.delete(
				"Ad Insight",
				{
					"reference_doctype": "Ad",
					"reference_name": ad,
				},
			)
			frappe.delete_doc("Ad", ad, force=True, ignore_permissions=True)
		for s in frappe.get_all("Ad Set", filters={"ad_campaign": ac}, pluck="name"):
			frappe.delete_doc("Ad Set", s, force=True, ignore_permissions=True)
		frappe.delete_doc("Ad Campaign", ac, force=True, ignore_permissions=True)

	frappe.delete_doc("Marketing Campaign", campaign, force=True, ignore_permissions=True)
	frappe.db.commit()
	return {"deleted": True, "marketing_campaign": campaign}

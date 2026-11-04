import unittest
import frappe
from msuite_campaigns.campaign.api.agent_tools import create_campaign, update_campaign, get_campaign, publish_campaign
from msuite_social.social_post.api.v1.agent_tools import create_social_post, delete_social_post, update_social_post

class TestAgentFacadeErrors(unittest.TestCase):
    def setUp(self):
        # Reset response status code
        if "http_status_code" in frappe.local.response:
            del frappe.local.response["http_status_code"]

    def tearDown(self):
        if "http_status_code" in frappe.local.response:
            del frappe.local.response["http_status_code"]

    def test_create_campaign_validation_error(self):
        # Missing start_date will trigger ValidationError
        res = create_campaign(campaign_name="Test Facade Campaign")
        self.assertFalse(res["ok"])
        self.assertIn("validation_error", res["error"])
        self.assertEqual(frappe.local.response.get("http_status_code"), 400)

    def test_update_campaign_missing_name(self):
        res = update_campaign(campaign_id="")
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "'campaign_id' is required.")
        self.assertEqual(frappe.local.response.get("http_status_code"), 400)

    def test_update_campaign_invalid_status_change(self):
        res = update_campaign(campaign_id="MC-NON-EXISTENT", status="Active")
        self.assertFalse(res["ok"])
        self.assertIn("Do not set 'status'", res["error"])
        self.assertEqual(frappe.local.response.get("http_status_code"), 400)

    def test_get_campaign_not_found(self):
        res = get_campaign(campaign_id="MC-NON-EXISTENT-XYZ")
        self.assertFalse(res["ok"])
        self.assertIn("not_found", res["error"])
        self.assertEqual(frappe.local.response.get("http_status_code"), 404)

    def test_create_social_post_parent_campaign_not_found(self):
        res = create_social_post(
            is_type="campaign",
            parent_campaign_id="MC-NON-EXISTENT-XYZ",
            title="Test Post",
            post_type="text_post",
        )
        self.assertFalse(res["ok"])
        self.assertIn("does not exist", res["error"])
        self.assertEqual(frappe.local.response.get("http_status_code"), 404)

    def test_create_social_post_with_scheduling(self):
        # 1. Create a campaign first
        campaign_res = create_campaign(
            campaign_name="Facade Test Campaign 2",
            start_date="2026-07-01",
            end_date="2026-07-31",
            objective="Traffic"
        )
        self.assertTrue(campaign_res["ok"])
        campaign_name = campaign_res["data"]["id"]

        # 2. Create social post with schedule flag
        res = create_social_post(
            is_type="campaign",
            parent_campaign_id=campaign_name,
            title="Scheduled Post Test",
            post_type="text_post",
            primary_caption="Caption text",
            publish_on_campaign_run=1,
        )
        self.assertTrue(res["ok"])
        
        post_name = res["data"]["id"]
        # Retrieve post doc to confirm scheduling parameters
        post_doc = frappe.get_doc("Social Post", post_name)
        self.assertEqual(post_doc.publish_on_run, 1)
        self.assertIsNone(post_doc.scheduled_at)

    def test_update_social_post_not_found(self):
        res = update_social_post(post_id="SP-NON-EXISTENT-XYZ", step="brief", title="New Title")
        self.assertFalse(res["ok"])
        self.assertIn("does not exist", res["error"])
        self.assertEqual(frappe.local.response.get("http_status_code"), 404)

    def test_create_ad_parent_campaign_not_found(self):
        from msuite_ads.ads.api.v1.agent_tools import create_ad
        res = create_ad(
            parent_campaign_id="MC-NON-EXISTENT-XYZ",
            name="Test Ad",
            ad_account_id="ACT-META-12345",
            objective="OUTCOME_TRAFFIC",
        )
        self.assertFalse(res["ok"])
        self.assertEqual(frappe.local.response.get("http_status_code"), 404)

    def test_update_ad_not_found(self):
        from msuite_ads.ads.api.v1.agent_tools import update_ad
        res = update_ad(ad_id="AC-NON-EXISTENT-XYZ", step="brief", objective="OUTCOME_AWARENESS")
        self.assertFalse(res["ok"])
        self.assertEqual(frappe.local.response.get("http_status_code"), 404)

    def test_publish_campaign_not_found(self):
        res = publish_campaign(campaign_id="MC-NON-EXISTENT-XYZ")
        self.assertFalse(res["ok"])
        self.assertIn("does not exist", res["error"])
        self.assertEqual(frappe.local.response.get("http_status_code"), 404)

    def test_publish_campaign_permission_denied(self):
        # Temporarily mock has_permission to return False
        orig_has_permission = frappe.has_permission
        frappe.has_permission = lambda *args, **kwargs: False
        try:
            res = publish_campaign(campaign_id="MC-2026-00001")
            self.assertFalse(res["ok"])
            self.assertIn("permission_denied", res["error"])
            self.assertEqual(frappe.local.response.get("http_status_code"), 403)
        finally:
            frappe.has_permission = orig_has_permission

    def test_update_social_post_multi_step(self):
        # 1. Create test Social Account
        existing = frappe.get_all("Social Account", filters={"account_name": "Test Account"}, limit=1)
        if existing:
            account_name = existing[0].name
        else:
            doc = frappe.get_doc({
                "doctype": "Social Account",
                "platform": "Facebook",
                "account_name": "Test Account",
                "account_id": "12345",
                "status": "Active"
            })
            doc.insert()
            account_name = doc.name

        # 2. Create campaign
        campaign_res = create_campaign(
            campaign_name="Facade Test Campaign 3",
            start_date="2026-07-01",
            end_date="2026-07-31",
            objective="Traffic"
        )
        self.assertTrue(campaign_res["ok"])
        campaign_name = campaign_res["data"]["id"]

        # 3. Create social post
        post_res = create_social_post(
            is_type="campaign",
            parent_campaign_id=campaign_name,
            title="Multi-step Post",
            post_type="text_post",
        )
        self.assertTrue(post_res["ok"])
        post_name = post_res["data"]["id"]

        # 4. Perform multi-step PATCH update
        patch_res = update_social_post(
            post_id=post_name,
            step="brief",
            title="Updated Title",
            selected_accounts=[account_name],
            publish_on_campaign_run=1
        )
        self.assertTrue(patch_res["ok"])
        self.assertEqual(patch_res["data"]["id"], post_name)
        self.assertEqual(patch_res["data"]["updated"]["title"], "Updated Title")
        self.assertEqual(patch_res["data"]["updated"]["publish_on_run"], 1)

        # 5. Assert fields from all steps were updated
        post_doc = frappe.get_doc("Social Post", post_name)
        self.assertEqual(post_doc.title, "Updated Title")
        self.assertEqual(len(post_doc.posts), 1)
        self.assertEqual(post_doc.posts[0].social_account, account_name)
        self.assertEqual(post_doc.publish_on_run, 1)

    def test_publish_campaign_no_children_configured(self):
        # 1. Create a campaign first
        campaign_res = create_campaign(
            campaign_name="Empty Publish Campaign",
            start_date="2026-07-01",
            end_date="2026-07-31",
            objective="Traffic"
        )
        self.assertTrue(campaign_res["ok"])
        campaign_name = campaign_res["data"]["id"]

        # 2. Call publish_campaign
        res = publish_campaign(campaign_id=campaign_name)
        self.assertFalse(res["ok"])
        self.assertIn("No children are configured to fire", res["error"])
        self.assertEqual(frappe.local.response.get("http_status_code"), 400)

    def test_publish_campaign_child_fails(self):
        from msuite_campaigns.campaign.api import runner as campaign_runner
        from msuite_campaigns.campaign.api.v1 import composer as campaign_composer

        # 1. Mock run_campaign to return a dict with failed child outcome
        mock_runner_result = {
            "campaign": "MC-MOCK",
            "campaign_status": "Active",
            "outcomes": {
                "social": [
                    {
                        "title": "Failed Social Post",
                        "outcome": "failed",
                        "message": "Reconnection required."
                    }
                ],
                "ads": [],
                "whatsapp": []
            },
            "summary": {
                "fired": 0,
                "skipped": 0,
                "failed": 1
            }
        }
        orig_run_campaign = campaign_runner.run_campaign
        campaign_runner.run_campaign = lambda *args, **kwargs: mock_runner_result
        
        try:
            # Create a mock campaign to target
            campaign_res = create_campaign(
                campaign_name="Publish Child Fails Campaign",
                start_date="2026-07-01",
                end_date="2026-07-31",
                objective="Traffic"
            )
            self.assertTrue(campaign_res["ok"])
            campaign_name = campaign_res["data"]["id"]

            # We also need preflight checks in publish_campaign not to fail.
            # To bypass empty check, we mock list_channels to return a post that will fire
            orig_list_channels = campaign_composer.list_channels
            campaign_composer.list_channels = lambda *args, **kwargs: {
                "social": [{"name": "SP-MOCK", "status": "Scheduled"}],
                "ads": []
            }
            
            try:
                res = publish_campaign(campaign_id=campaign_name)
                self.assertFalse(res["ok"])
                self.assertIn("Publish failed for children", res["error"])
                self.assertIn("Reconnection required", res["error"])
                self.assertEqual(frappe.local.response.get("http_status_code"), 400)
            finally:
                campaign_composer.list_channels = orig_list_channels
        finally:
            campaign_runner.run_campaign = orig_run_campaign

    def test_delete_social_post_publishing_blocked_published_deleted(self):
        post_res = create_social_post(title="Delete Me", post_type="text_post")
        self.assertTrue(post_res["ok"])
        post_name = post_res["data"]["id"]

        # In-flight posts are protected.
        frappe.db.set_value("Social Post", post_name, "status", "Publishing")
        res = delete_social_post(post_id=post_name)
        self.assertFalse(res["ok"])
        self.assertEqual(frappe.local.response.get("http_status_code"), 400)
        self.assertTrue(frappe.db.exists("Social Post", post_name))

        # Published posts are removed locally (no destinations, so no platform call).
        frappe.db.set_value("Social Post", post_name, "status", "Published")
        res = delete_social_post(post_id=post_name)
        self.assertTrue(res["ok"])
        self.assertEqual(res["data"]["status_was"], "Published")
        self.assertFalse(frappe.db.exists("Social Post", post_name))

    def test_update_social_post_published_state(self):
        # 1. Create campaign
        campaign_res = create_campaign(
            campaign_name="Facade Test Campaign 5",
            start_date="2026-07-01",
            end_date="2026-07-31",
            objective="Traffic"
        )
        self.assertTrue(campaign_res["ok"])
        campaign_name = campaign_res["data"]["id"]

        # 2. Create social post
        post_res = create_social_post(
            is_type="campaign",
            parent_campaign_id=campaign_name,
            title="Published Post",
            post_type="text_post",
        )
        self.assertTrue(post_res["ok"])
        post_name = post_res["data"]["id"]

        # 3. Force status to Published in DB
        frappe.db.set_value("Social Post", post_name, "status", "Published")

        # 4. Try updating the post
        res = update_social_post(post_id=post_name, step="brief", title="New Title")
        self.assertFalse(res["ok"])
        self.assertIn("cannot be modified", res["error"])
        self.assertEqual(frappe.local.response.get("http_status_code"), 400)

    def test_create_ad_chained(self):
        # 1. Create/find test Ad Account
        ad_account_name = "Test Ad Account"
        existing_acc = frappe.get_all("Ad Account", filters={"account_name": "Test Account"}, limit=1)
        if existing_acc:
            ad_account_name = existing_acc[0].name
        else:
            doc = frappe.get_doc({
                "doctype": "Ad Account",
                "platform": "Meta",
                "account_name": "Test Account",
                "account_id": "12345",
                "status": "Active"
            })
            doc.insert()
            ad_account_name = doc.name

        # 2. Create/find Facebook Page (Social Account)
        fb_page_name = "Test Facebook Account"
        existing_page = frappe.get_all("Social Account", filters={"account_name": "Test Account"}, limit=1)
        if existing_page:
            fb_page_name = existing_page[0].name
        else:
            doc = frappe.get_doc({
                "doctype": "Social Account",
                "platform": "Facebook",
                "account_name": "Test Account",
                "account_id": "12345",
                "status": "Active"
            })
            doc.insert()
            fb_page_name = doc.name

        # 3. Create parent campaign
        campaign_res = create_campaign(
            campaign_name="Facade Test Campaign 6",
            start_date="2026-07-01",
            end_date="2026-07-31",
            objective="Traffic"
        )
        self.assertTrue(campaign_res["ok"])
        campaign_name = campaign_res["data"]["id"]

        # 4. Call create_ad with chained parameters (brief, audience, creative, budget)
        from msuite_ads.ads.api.v1.agent_tools import create_ad
        ad_res = create_ad(
            parent_campaign_id=campaign_name,
            name="End-to-End Ad",
            ad_account_id=ad_account_name,
            objective="OUTCOME_TRAFFIC",
            # audience step fields
            age_min=25,
            age_max=45,
            genders="All",
            audience_locations=[
                {"location_type": "country", "meta_key": "IN", "country_code": "IN", "display_name": "India"}
            ],
            # creative step fields
            creative_mode="new",
            headline="Headline text",
            primary_text="Primary text copy",
            call_to_action="SHOP_NOW",
            destination_url="https://example.com/summer",
            media_url="/files/summer.jpg",
            facebook_page=fb_page_name,
            # budget step fields
            budget_mode="daily",
            daily_budget=500,
            optimization_goal="LINK_CLICKS",
            billing_event="IMPRESSIONS",
            start_date="2026-07-01T00:00:00",
            end_date="2026-07-31T23:59:59",
            launch_on_run=1
        )
        self.assertTrue(ad_res["ok"])
        ac_name = ad_res["data"]["id"]

        # 5. Assert database state
        ac_doc = frappe.get_doc("Ad Campaign", ac_name)
        self.assertEqual(ac_doc.ad_account, ad_account_name)
        self.assertEqual(ac_doc.objective, "OUTCOME_TRAFFIC")

        # For a non-CBO campaign the budget lives on the Ad Set, not the
        # Campaign — ad_campaign.py's own validate() moves it there and
        # zeros ac.daily_budget (see test_ad_campaign.py's
        # test_budget_serialization for the same behavior, asserted via
        # `serialize(ac)["daily_budget"]` for the same reason).
        self.assertEqual(ac_doc.daily_budget, 0)
        self.assertEqual(ac_doc.launch_on_run, 1)

        # Verify Ad Set details (audience and budget)
        ad_sets = frappe.get_all("Ad Set", filters={"ad_campaign": ac_name})
        self.assertEqual(len(ad_sets), 1)
        ad_set = frappe.get_doc("Ad Set", ad_sets[0].name)
        self.assertEqual(ad_set.age_min, 25)
        self.assertEqual(ad_set.age_max, 45)
        self.assertEqual(ad_set.daily_budget, 500)

        # Verify that Advantage+ Audience is disabled in built targeting spec because age_max < 65
        from msuite_ads.ads.integrations.meta.targeting import build_targeting
        targeting_spec = build_targeting(ad_set)
        self.assertEqual(targeting_spec.get("targeting_automation", {}).get("advantage_audience"), 0)

        # Verify Ad and Ad Creative
        ads = frappe.get_all("Ad", filters={"ad_set": ad_set.name})
        self.assertEqual(len(ads), 1)
        ad_doc = frappe.get_doc("Ad", ads[0].name)
        self.assertEqual(ad_doc.facebook_page, fb_page_name)

    def test_update_ad_name_sync(self):
        # 1. Create/find test Ad Account
        ad_account_name = "Test Ad Account"
        existing_acc = frappe.get_all("Ad Account", filters={"account_name": "Test Account"}, limit=1)
        if existing_acc:
            ad_account_name = existing_acc[0].name
        else:
            doc = frappe.get_doc({
                "doctype": "Ad Account",
                "platform": "Meta",
                "account_name": "Test Account",
                "account_id": "12345",
                "status": "Active"
            })
            doc.insert()
            ad_account_name = doc.name

        # 2. Create/find Facebook Page (Social Account)
        fb_page_name = "Test Facebook Account"
        existing_page = frappe.get_all("Social Account", filters={"account_name": "Test Account"}, limit=1)
        if existing_page:
            fb_page_name = existing_page[0].name
        else:
            doc = frappe.get_doc({
                "doctype": "Social Account",
                "platform": "Facebook",
                "account_name": "Test Account",
                "account_id": "12345",
                "status": "Active"
            })
            doc.insert()
            fb_page_name = doc.name

        # 3. Create parent campaign
        campaign_res = create_campaign(
            campaign_name="Facade Test Campaign 7",
            start_date="2026-07-01",
            end_date="2026-07-31",
            objective="Traffic"
        )
        self.assertTrue(campaign_res["ok"])
        campaign_name = campaign_res["data"]["id"]

        # 4. Create ad with chained steps so we have an Ad Set and an Ad created
        from msuite_ads.ads.api.v1.agent_tools import create_ad, update_ad
        ad_res = create_ad(
            parent_campaign_id=campaign_name,
            name="Initial Name",
            ad_account_id=ad_account_name,
            objective="OUTCOME_TRAFFIC",
            creative_mode="new",
            headline="Headline text",
            call_to_action="SHOP_NOW",
            destination_url="https://example.com/summer",
            media_url="/files/summer.jpg",
            facebook_page=fb_page_name
        )
        self.assertTrue(ad_res["ok"])
        ac_name = ad_res["data"]["id"]

        # 5. PATCH update the ad campaign name
        patch_res = update_ad(
            ad_id=ac_name,
            step="brief",
            name="Updated Name"
        )
        self.assertTrue(patch_res["ok"])

        # 6. Assert child Ad Set and Ad names have updated
        ad_sets = frappe.get_all("Ad Set", filters={"ad_campaign": ac_name})
        self.assertEqual(len(ad_sets), 1)
        ad_set = frappe.get_doc("Ad Set", ad_sets[0].name)
        self.assertEqual(ad_set.adset_name, "Updated Name — set")

        ads = frappe.get_all("Ad", filters={"ad_set": ad_set.name})
        self.assertEqual(len(ads), 1)
        ad_doc = frappe.get_doc("Ad", ads[0].name)
        self.assertEqual(ad_doc.ad_name, "Updated Name — ad")

    def test_upload_media_no_input(self):
        from msuite_core.api.v1.agent.agent_facade import upload_media
        res = upload_media()
        self.assertFalse(res["ok"])
        self.assertIn("validation_error", res["error"])

    def test_upload_media_independent_of_campaign(self):
        from msuite_core.api.v1.agent.agent_facade import upload_media
        res = upload_media(
            file_content="iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==",
            file_name="test_independent.png",
            campaign_id="MC-NON-EXISTENT-XYZ"
        )
        self.assertTrue(res["ok"])
        self.assertTrue(res["data"]["file_name"].startswith("test_independent"))

    def test_upload_media_base64_success(self):
        from msuite_core.api.v1.agent.agent_facade import upload_media
        res = upload_media(
            file_content="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==",
            file_name="test_banner.png",
            source_module="social_post",
            tags="test,social"
        )
        self.assertTrue(res["ok"])
        self.assertTrue(res["data"]["file_name"].startswith("test_banner"))
        self.assertEqual(res["data"]["media_type"], "Image")
        self.assertTrue(res["data"]["file_url"].startswith("/files/"))

    def test_get_media_missing_params(self):
        from msuite_core.api.v1.agent.agent_facade import get_media
        res = get_media()
        self.assertFalse(res["ok"])
        self.assertIn("validation_error", res["error"])

    def test_get_media_not_found(self):
        from msuite_core.api.v1.agent.agent_facade import get_media
        res = get_media(file_id="FILE-NON-EXISTENT-XYZ")
        self.assertFalse(res["ok"])
        self.assertIn("not_found", res["error"])

    def test_get_media_and_list_media_success(self):
        from msuite_core.api.v1.agent.agent_facade import upload_media, get_media, list_media
        upload_res = upload_media(
            file_content="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==",
            file_name="get_media_test.png",
            source_module="agent_test"
        )
        self.assertTrue(upload_res["ok"])
        file_id = upload_res["data"]["id"]
        file_url = upload_res["data"]["file_url"]

        # Fetch by file_id
        get_res_id = get_media(file_id=file_id)
        self.assertTrue(get_res_id["ok"])
        self.assertEqual(get_res_id["data"]["id"], file_id)

        # Fetch by file_url
        get_res_url = get_media(file_url=file_url)
        self.assertTrue(get_res_url["ok"])
        self.assertEqual(get_res_url["data"]["id"], file_id)

        # List media
        list_res = list_media(source_module="agent_test")
        self.assertTrue(list_res["ok"])
        self.assertTrue(any(m["id"] == file_id for m in list_res["data"]))

    def test_upload_media_batch(self):
        from msuite_core.api.v1.agent.agent_facade import upload_media
        res = upload_media(
            files=[
                {"file_content": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==", "file_name": "carousel_1.png"},
                {"file_content": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==", "file_name": "carousel_2.png"}
            ],
            source_module="carousel_test"
        )
        self.assertTrue(res["ok"])
        self.assertTrue(isinstance(res["data"], list))
        self.assertEqual(len(res["data"]), 2)




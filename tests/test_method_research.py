from __future__ import annotations

import copy
import csv
import hashlib
import io
import json
import unittest

from bti.method_research import build_method_report, method_csv


PID = "0009223372036854775"
OTHER = "5194021166069388545"
CAMPAIGN = "https://www.binance.com/zh-CN/square/blockchain-100-2026"
PROFILE = "https://www.binance.com/en/square/profile/synthetic_author"
PORTFOLIO = "https://www.binance.com/zh-CN/copy-trading/lead-details/" + PID
POST = "https://www.binance.com/en/square/post/000123"


def inputs():
    candidate = {
        "id": "candidate-001", "display_name": 'Synthetic, "author"\nsecond line',
        "square_uid": "000author", "square_profile_url": PROFILE,
        "source_link_value": PORTFOLIO + "?timeRange=365D",
        "biography": "Only enter after confirmation. Risk one percent.", "followers": 0,
        "followers_source": {"field": "followers", "platform": None, "observed_at": "campaign-time"},
        "memberships": [{"category": "trader", "rank": 99, "votes": 1, "candidate_id": "001",
                         "source_url": CAMPAIGN, "observed_at": "campaign-time"}],
        "source": {"url": CAMPAIGN, "observed_at": "campaign-time", "verification_level": "operator_capture"},
    }
    catalog = {"schema": "bti-blockchain100-2026-dossiers-v1", "profiles": [candidate]}
    capture = {
        "schema": "bti-public-portfolio-ui-capture-v1", "mode": "public_overviews_only",
        "capture_method": "operator_authorized_normal_ui_public_fields_only",
        "portfolios": [{"portfolio_id": PID, "source_url": PORTFOLIO,
                        "access": {"url": PORTFOLIO + "?timeRange=7D", "observed_at": "access-time"},
                        "performance": {"observed_at": "performance-time", "text":
                            "Performance\n7 Days\nROI\nPnL\n-2.50%\n-1,234.50 BTC\nCopier PnL\n0.00 USDT\nMDD\n--\nSharpe Ratio\n--"},
                        "public_overview": {"observed_at": "overview-time", "text": "Exit on invalidation. Size positions conservatively.",
                                            "tags": ["Synthetic"], "original_toggle_used": True,
                                            "rendered_before_original_toggle": "Translated public description"},
                        "tabs": {}, "current_positions": {"private_notice": None, "capture_status": "not_read"}}],
    }
    reviews = {"schema": "bti-authored-method-review-v1", "public_posts": [], "reviews": []}
    return catalog, capture, reviews


def review(kind="campaign_biography", **updates):
    data = {"candidate_id": "candidate-001", "source_kind": kind, "source_url": CAMPAIGN,
            "post_id": None, "quote": "Only enter after confirmation.", "interpretation_vi": "Đợi xác nhận.",
            "rules": [{"kind": "risk", "quote": "Risk one percent.", "interpretation_vi": "Giới hạn rủi ro tự khai."}],
            "limitations_vi": ["Không có định nghĩa xác nhận."]}
    data.update(updates)
    return data


def add_post(reviews):
    reviews["public_posts"].append({"post_id": "000123", "source_url": POST,
                                    "author_profile_url": PROFILE, "observed_at": "post-time", "published_at": None,
                                    "text": "Use small size. Exit when invalidated.", "access_label": "public",
                                    "author_identity_basis": "exact_author_profile_link"})
    reviews["reviews"].append(review("square_post", source_url=POST, post_id="000123", quote="Use small size.",
                                     rules=[{"kind": "exit", "quote": "Exit when invalidated.", "interpretation_vi": "Thoát khi vô hiệu."}]))


class MethodResearchTests(unittest.TestCase):
    def test_loss_currency_null_and_period_reach_report_and_csv_unchanged(self):
        catalog, capture, reviews = inputs()
        result = build_method_report(catalog, capture, reviews)
        p = result["profiles"][0]
        self.assertEqual(p["portfolio_id"], PID)
        self.assertEqual(p["snapshot"]["period_label"], "7 Days")
        self.assertEqual(p["snapshot"]["roi_percent"], "-2.50")
        self.assertEqual(p["snapshot"]["leader_pnl"], "-1234.50")
        self.assertEqual(p["snapshot"]["leader_pnl_currency"], "BTC")
        self.assertEqual(p["snapshot"]["copier_pnl_currency"], "USDT")
        self.assertEqual(p["snapshot"]["copier_pnl"], "0.00")
        self.assertIsNone(p["snapshot"]["mdd_percent"])
        self.assertEqual(p["observed_at"], "performance-time")
        self.assertEqual(p["portfolio_url"], PORTFOLIO + "?timeRange=7D")
        self.assertFalse(p["link_evidence"]["ownership_verified"])
        rows = list(csv.DictReader(io.StringIO(method_csv(result))))
        self.assertEqual(rows[0]["portfolio_id"], PID)
        self.assertEqual(rows[0]["square_uid"], "000author")
        self.assertEqual(rows[0]["display_name"], catalog["profiles"][0]["display_name"])
        self.assertEqual(rows[0]["mdd_percent"], "")
        self.assertEqual(rows[0]["period_label"], "7 Days")
        self.assertEqual(rows[0]["leader_pnl_currency"], "BTC")
        self.assertEqual(rows[0]["ownership_verified"], "false")
        self.assertNotIn("\r", method_csv(result))

    def test_unlabelled_money_is_not_assumed_usdt_and_missing_capture_retained(self):
        catalog, capture, reviews = inputs()
        second = copy.deepcopy(catalog["profiles"][0])
        second.update(id="candidate-002", source_link_value="https://www.binance.com/en/copy-trading/lead-details/" + OTHER)
        catalog["profiles"].insert(0, second)
        capture["portfolios"][0]["performance"]["text"] = capture["portfolios"][0]["performance"]["text"].replace(" BTC", "")
        result = build_method_report(catalog, capture, reviews)
        self.assertEqual([p["candidate_id"] for p in result["profiles"]], ["candidate-002", "candidate-001"])
        self.assertIsNone(result["profiles"][0]["snapshot"])
        self.assertIsNone(result["profiles"][0]["public_overview"])
        self.assertIsNone(result["profiles"][0]["observed_at"])
        self.assertIsNone(result["profiles"][1]["snapshot"]["leader_pnl_currency"])
        self.assertEqual(result["counts"]["observed_portfolios"], 1)

    def test_shared_portfolio_is_explicit_dependency_not_two_observations(self):
        catalog, capture, reviews = inputs()
        second = copy.deepcopy(catalog["profiles"][0])
        second["id"] = "candidate-002"
        catalog["profiles"].append(second)
        result = build_method_report(catalog, capture, reviews)
        self.assertEqual(result["counts"]["distinct_portfolios"], 1)
        self.assertEqual(result["counts"]["observed_portfolios"], 1)
        for p in result["profiles"]:
            self.assertEqual(p["link_evidence"]["shared_portfolio_candidates"], ["candidate-001", "candidate-002"])
            self.assertFalse(p["link_evidence"]["ownership_verified"])

    def test_campaign_portfolio_post_each_use_own_source_and_rule_quotes(self):
        catalog, capture, reviews = inputs()
        reviews["reviews"] = [review(), review("portfolio_biography", source_url=PORTFOLIO,
            quote="Exit on invalidation.", rules=[{"kind": "sizing", "quote": "Size positions conservatively.", "interpretation_vi": "Quy mô thận trọng."}])]
        add_post(reviews)
        cards = build_method_report(catalog, capture, reviews)["profiles"][0]["method_cards"]
        self.assertEqual([c["observed_at"] for c in cards], ["campaign-time", "overview-time", "post-time"])
        self.assertEqual([c["rules"][0]["kind"] for c in cards], ["risk", "sizing", "exit"])
        self.assertEqual(cards[2]["post_id"], "000123")
        self.assertIsNone(cards[2]["published_at"])
        self.assertTrue(all(c["profit_cause_verified"] is False for c in cards))
        self.assertEqual(len(cards), 3)
        self.assertEqual(cards[1]["quote"], "Exit on invalidation.")

    def test_quote_or_rule_from_different_source_is_rejected(self):
        for key in ("quote", "rule"):
            with self.subTest(key=key):
                catalog, capture, reviews = inputs()
                r = review()
                if key == "quote":
                    r["quote"] = "Exit on invalidation."
                else:
                    r["rules"][0]["quote"] = "Exit on invalidation."
                reviews["reviews"] = [r]
                with self.assertRaises(ValueError):
                    build_method_report(catalog, capture, reviews)

    def test_post_identity_privacy_and_source_id_boundaries(self):
        for change in ("author", "private", "post_id", "duplicate", "locale"):
            with self.subTest(change=change):
                catalog, capture, reviews = inputs()
                add_post(reviews)
                post = reviews["public_posts"][0]
                if change == "author":
                    post["author_profile_url"] = PROFILE + "_other"
                elif change == "private":
                    post["access_label"] = "followers_only"
                elif change == "post_id":
                    post["post_id"] = "123"
                elif change == "duplicate":
                    reviews["public_posts"].append(copy.deepcopy(post))
                else:
                    post["author_profile_url"] = PROFILE.replace("/en/", "/zh-CN/")
                with self.assertRaises(ValueError):
                    build_method_report(catalog, capture, reviews)

    def test_financial_private_and_malformed_layouts_fail_not_silently_drop(self):
        for change in ("tabs", "current", "credentials", "layout", "wrong_navigation", "wrong_id", "duplicate", "unlinked"):
            with self.subTest(change=change):
                catalog, capture, reviews = inputs()
                p = capture["portfolios"][0]
                if change == "tabs":
                    p["tabs"] = {"Position History": {"pages": []}}
                elif change == "current":
                    p["current_positions"]["rows"] = []
                elif change == "credentials":
                    p["access"]["cookies"] = "secret"
                elif change == "layout":
                    p["performance"]["text"] = "Performance\n30 Days\nUnknown financial metric\n100"
                elif change == "wrong_navigation":
                    p["access"]["url"] = PORTFOLIO.replace(PID, OTHER)
                elif change == "wrong_id":
                    p["portfolio_id"] = int(PID)
                elif change == "duplicate":
                    capture["portfolios"].append(copy.deepcopy(p))
                else:
                    p["portfolio_id"] = OTHER
                with self.assertRaises(ValueError):
                    build_method_report(catalog, capture, reviews)

    def test_only_candidate_supplied_link_not_name_or_alternate_profile_url(self):
        catalog, capture, reviews = inputs()
        candidate = catalog["profiles"][0]
        candidate["profile_url"] = candidate["source_link_value"]
        candidate["source_link_value"] = None
        capture["portfolios"] = []
        self.assertEqual(build_method_report(catalog, capture, reviews)["profiles"], [])
        capture["portfolios"] = inputs()[1]["portfolios"]
        with self.assertRaises(ValueError):
            build_method_report(catalog, capture, reviews)

    def test_public_square_link_metadata_preserves_exact_portfolio_association(self):
        catalog, capture, reviews = inputs()
        link = PORTFOLIO + "?source_type=square&contentId=372296989278806"
        catalog["profiles"][0]["source_link_value"] = link
        result = build_method_report(catalog, capture, reviews)
        self.assertEqual(result["profiles"][0]["source_link_url"], link)
        self.assertEqual(result["profiles"][0]["portfolio_id"], PID)
        self.assertFalse(result["profiles"][0]["link_evidence"]["ownership_verified"])
        for query in ("source_type=owner", "contentId=not-a-post", "contentId=1&contentId=2", "source_type=square&token=secret"):
            with self.subTest(query=query), self.assertRaises(ValueError):
                catalog["profiles"][0]["source_link_value"] = PORTFOLIO + "?" + query
                build_method_report(catalog, capture, reviews)

    def test_explicit_source_blocks_cannot_promote_cached_financial_values(self):
        for access in ({"blocked_indicator": True}, {"navigation_status": 401}, {"navigation_status": 403}, {"navigation_status": 429}):
            with self.subTest(access=access):
                catalog, capture, reviews = inputs()
                capture["portfolios"][0]["access"].update(access)
                with self.assertRaises(ValueError):
                    build_method_report(catalog, capture, reviews)

    def test_unsafe_and_noncanonical_links_cannot_supply_association(self):
        for link in (PORTFOLIO + "/", PORTFOLIO + "0", PORTFOLIO + "?token=secret",
                     PORTFOLIO.replace("https:", "http:"), PORTFOLIO.replace("www.binance.com", "www.binance.com.evil.test"),
                     PORTFOLIO.replace("www.binance.com", "owner@www.binance.com"), PORTFOLIO + "#fragment"):
            with self.subTest(link=link):
                catalog, capture, reviews = inputs()
                catalog["profiles"][0]["source_link_value"] = link
                with self.assertRaises(ValueError):
                    build_method_report(catalog, capture, reviews)

    def test_dependencies_and_projection_hash_are_enforced(self):
        catalog, capture, reviews = inputs()
        record = capture["portfolios"][0]
        record["projection_sha256"] = hashlib.sha256(json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        build_method_report(catalog, capture, reviews)
        record["performance"]["text"] += "\nMDD\n1.00%"
        with self.assertRaises(ValueError):
            build_method_report(catalog, capture, reviews)
        for selection in ({"portfolio_ids": [OTHER]}, {"portfolio_ids": [PID, PID]}, {"catalog_sha256": "wrong"}):
            catalog, capture, reviews = inputs()
            capture["selection"] = selection
            with self.subTest(selection=selection), self.assertRaises(ValueError):
                build_method_report(catalog, capture, reviews)

    def test_receipts_are_public_whitelist_and_zero_followers_remain_known(self):
        catalog, capture, reviews = inputs()
        catalog["profiles"][0]["source"]["internal_operator_note"] = "do not export"
        catalog["profiles"][0]["followers_source"]["private_owner_note"] = "do not export"
        capture["stop"] = {"reason": "public navigation stopped", "observed_at": "stop-time", "internal_detail": "do not export"}
        capture["portfolios"][0]["public_overview"]["internal_note"] = "do not export"
        p = build_method_report(catalog, capture, reviews)["profiles"][0]
        self.assertNotIn("do not export", json.dumps(p))
        self.assertEqual(p["source_receipts"]["portfolio"]["collector_stop"], {"reason": "public navigation stopped", "observed_at": "stop-time"})
        self.assertEqual(p["three_pillars"]["engagement"]["followers"], 0)
        self.assertEqual(p["three_pillars"]["engagement"]["status"], "follower_snapshot_only")
        self.assertIsNone(p["three_pillars"]["engagement"]["followers_source"]["platform"])
        self.assertTrue(p["public_overview"]["original_toggle_used"])
        self.assertEqual(p["public_overview"]["rendered_before_original_toggle"], "Translated public description")

    def test_selected_but_unobserved_candidate_can_retain_campaign_method(self):
        catalog, capture, reviews = inputs()
        capture["portfolios"] = []
        capture["selection"] = {"portfolio_ids": [PID]}
        reviews["reviews"] = [review()]
        result = build_method_report(catalog, capture, reviews)
        p = result["profiles"][0]
        self.assertIsNone(p["snapshot"])
        self.assertEqual(p["three_pillars"]["performance"]["status"], "unavailable")
        self.assertEqual(p["method_cards"][0]["source_kind"], "campaign_biography")
        self.assertEqual(result["counts"]["method_cards"], 1)
        self.assertEqual(result["counts"]["observed_portfolios"], 0)
        self.assertEqual(list(csv.DictReader(io.StringIO(method_csv(result))))[0]["method_count"], "1")

    def test_missing_portfolio_source_cannot_be_replaced_with_campaign_quote(self):
        catalog, capture, reviews = inputs()
        capture["portfolios"] = []
        reviews["reviews"] = [review("portfolio_biography", source_url=PORTFOLIO)]
        with self.assertRaises(ValueError):
            build_method_report(catalog, capture, reviews)

    def test_malformed_or_inferred_rule_and_unselected_review_fail(self):
        for change in ("null_rule", "unknown_kind", "empty_quote", "unknown_candidate", "wrong_source"):
            with self.subTest(change=change):
                catalog, capture, reviews = inputs()
                r = review()
                if change == "null_rule":
                    r["rules"] = [None]
                elif change == "unknown_kind":
                    r["rules"][0]["kind"] = "inferred_setup"
                elif change == "empty_quote":
                    r["rules"][0]["quote"] = ""
                elif change == "unknown_candidate":
                    r["candidate_id"] = "candidate-999"
                else:
                    r["source_url"] = PORTFOLIO
                reviews["reviews"] = [r]
                with self.assertRaises(ValueError):
                    build_method_report(catalog, capture, reviews)

    def test_stable_identity_strings_and_duplicate_candidate_boundaries(self):
        for change in ("uid_number", "candidate_number", "duplicate_candidate"):
            with self.subTest(change=change):
                catalog, capture, reviews = inputs()
                if change == "uid_number":
                    catalog["profiles"][0]["square_uid"] = 123
                elif change == "candidate_number":
                    catalog["profiles"][0]["id"] = 1
                else:
                    catalog["profiles"].append(copy.deepcopy(catalog["profiles"][0]))
                with self.assertRaises(ValueError):
                    build_method_report(catalog, capture, reviews)


    def test_public_author_uid_matches_case_variant_links_but_never_names(self):
        for change in (None, "wrong_uid", "missing_dom_match", "wrong_requested_profile"):
            with self.subTest(change=change):
                catalog, capture, reviews = inputs()
                add_post(reviews)
                post = reviews["public_posts"][0]
                post.update(author_identity_basis="exact_author_square_uid", author_square_uid="000author",
                            author_profile_url=PROFILE.replace("synthetic_author", "SYNTHETIC_AUTHOR"),
                            expected_profile_url=PROFILE, dom_post_id_verified=True,
                            text_basis="public_component_original_bodyTextOnly",
                            engagement={"likes": 0, "comments": 2, "views": 10, "shares": 0})
                if change == "wrong_uid":
                    post["author_square_uid"] = "another_author"
                elif change == "missing_dom_match":
                    post["dom_post_id_verified"] = False
                elif change == "wrong_requested_profile":
                    post["expected_profile_url"] = PROFILE + "_someone_else"
                if change is not None:
                    with self.assertRaises(ValueError):
                        build_method_report(catalog, capture, reviews)
                else:
                    result = build_method_report(catalog, capture, reviews)
                    profile = result["profiles"][0]
                    self.assertEqual(profile["method_cards"][0]["source_attribution"]["author_square_uid"], "000author")
                    samples = profile["three_pillars"]["engagement"]["post_samples"]
                    self.assertEqual(samples[0]["engagement"], {"likes": 0, "comments": 2, "views": 10, "shares": 0})
                    self.assertEqual(samples[0]["source_url"], POST)
                    self.assertFalse(profile["link_evidence"]["ownership_verified"])

    def test_private_invitation_is_omitted_without_changing_source_method_evidence(self):
        catalog, capture, reviews = inputs()
        record = capture["portfolios"][0]
        source = "Exit on invalidation. 私域跟单码：syntheticPrivateCode"
        record["public_overview"]["text"] = source
        reviews["reviews"] = [review("portfolio_biography", source_url=PORTFOLIO,
                                     quote="Exit on invalidation.", rules=[])]
        result = build_method_report(catalog, capture, reviews)
        self.assertNotIn("syntheticPrivateCode", json.dumps(result, ensure_ascii=False))
        self.assertEqual(record["public_overview"]["text"], source)
        self.assertEqual(result["profiles"][0]["method_cards"][0]["quote"], "Exit on invalidation.")
        reviews["reviews"][0]["quote"] = "私域跟单码：syntheticPrivateCode"
        with self.assertRaises(ValueError):
            build_method_report(catalog, capture, reviews)


if __name__ == "__main__":
    unittest.main()

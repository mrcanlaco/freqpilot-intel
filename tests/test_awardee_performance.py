from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from bti.db import connect
from bti.ingest import FetchResult, ingest, ingest_public_capture, parse_copy_trading_home
from bti.public import health


CRYPTO_UID = "VAYGRi3YqX6T-PlzOHmeqQ"
PORTFOLIO_IDS = ["4925133974869249281", "5194021166069388545", "5133452281665177088"]
ROOT = Path(__file__).resolve().parents[1]


def response(payload: dict, *, url: str, observed_at: str = "2026-10-18T02:04:49Z") -> FetchResult:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return FetchResult(url, 200, "application/json", body, 3, observed_at)


def winner_response() -> FetchResult:
    winners = []
    for rank in range(1, 21):
        crypto = rank == 3
        winners.append({
            "squareUid": CRYPTO_UID if crypto else f"uid-{rank:02d}",
            "username": "CRYPTOMECHANIC" if crypto else f"trader{rank:02d}",
            "displayName": "Crypto Mechanic" if crypto else f"Trader {rank:02d}",
            "voteCount": 8_019 if crypto else 20_000 - rank,
            "rank": rank,
            "categoryType": 3,
            "shareLink": f"https://app.binance.com/uni-qr/cact25user/trader{rank:02d}",
        })
    payload = {"code": "000000", "success": True, "data": {"groupList": [
        {"categoryType": 2, "name": "Independent Researcher", "winners": []},
        {"categoryType": 3, "name": "Trader", "winners": winners},
    ]}}
    return response(payload, url="https://www.binance.com/bapi/winners")


def posts_response() -> FetchResult:
    rows = []
    for index, title in enumerate((
        "You Won’t Recover Your Trading Losses by Trading the Same Way",
        "Risk management begins with changing the process",
    )):
        rows.append({
            "id": f"post-{index}", "title": title,
            "bodyTextOnly": ("A substantive risk-management discussion with explicit uncertainty and process controls. " * 8),
            "contentType": 2 if index == 0 else 1,
            "squareUid": CRYPTO_UID, "username": "CRYPTOMECHANIC", "displayName": "CRYPTO MECHANIC",
            "createTime": 1_792_229_636_000 - index * 1_000,
            "lan": "en", "webLink": f"https://www.binance.com/en/square/post/post-{index}",
            "viewCount": 10_313 + index, "likeCount": 82, "commentCount": 23,
            "shareCount": 1, "replyCount": 0, "portfolioPerformance": None,
        })
    return response({"code": "000000", "success": True, "data": {
        "contents": rows, "timeOffset": 123, "isExistSecondPage": True,
    }}, url="https://www.binance.com/bapi/posts")


def performance_response() -> FetchResult:
    records = [
        {"leadPortfolioId": PORTFOLIO_IDS[0], "nickname": "Portfolio A", "portfolioType": "PUBLIC",
         "roi": 57.3, "pnl": 469817.5, "aum": 1986061.0, "mdd": 12.68, "winRate": 69.2,
         "copierPnl": 98839.0, "sharpRatio": 2.65, "startTime": 1771519261178,
         "currentCopyCount": 377, "chartItems": [{"value": 1}]},
        {"leadPortfolioId": PORTFOLIO_IDS[1], "nickname": "Portfolio B", "portfolioType": "PUBLIC",
         "roi": 84.5, "pnl": 335513.9, "aum": 953088.5, "mdd": 15.53, "winRate": 50,
         "copierPnl": -8279.38, "sharpRatio": 1.54, "startTime": 1787546186692,
         "currentCopyCount": 274, "chartItems": [{"value": 1}]},
        {"leadPortfolioId": PORTFOLIO_IDS[2], "nickname": "Portfolio C", "portfolioType": "PUBLIC",
         "roi": 21.3, "pnl": 248825.6, "aum": 500000.0, "mdd": 7.88, "winRate": 40.625,
         "copierPnl": 15676.8, "sharpRatio": 2.0, "startTime": 1783935999864,
         "currentCopyCount": 120, "chartItems": [{"value": 1}]},
    ]
    capture = {
        "url": "https://www.binance.com/en/copy-trading",
        "observed_at": "2026-10-17T19:04:49Z",
        "capture_method": "orca_public_anonymous_browser",
        "appData": {"appState": {"loader": {"dataByRouteId": {
            "route-is-discovered-not-hardcoded": {"dehydratedState": {"queries": [{"state": {"data": {"data": {
                "highestRoiLeads": [], "highestPnlLeads": records,
                "highestCopyPnlLeads": [records[0]], "mostPopularLeads": [],
            }}}}]}}
        }}}},
    }
    return response(capture, url="https://www.binance.com/en/copy-trading")


class AwardeePerformanceIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.db = root / "test.sqlite3"
        self.manifest = root / "sources.json"
        self.manifest.write_text(json.dumps({"sources": [
            {"slug": "winners", "name": "Winners", "url": "https://www.binance.com/en/square/blockchain-100-2025",
             "fetch_url": "https://www.binance.com/bapi/winners", "method": "POST", "json_body": {},
             "source_type": "binance_official_award_winners", "adapter": "award_winners",
             "access_level": "public", "distribution": "public_shareable", "expected_pillar": "recognition",
             "category_name": "Trader", "category_type": 3, "expected_count": 20, "delay_after_seconds": 0},
            {"slug": "posts", "name": "Posts", "url": "https://www.binance.com/en/square/profile/cryptomechanic",
             "fetch_url": "https://www.binance.com/bapi/posts", "source_type": "binance_square_creator_posts",
             "adapter": "square_user_posts", "access_level": "public", "distribution": "public_shareable",
             "expected_pillar": "engagement", "minimum_text_chars": 200, "max_items": 5,
             "entity": {"slug": "blockchain100-2025-cryptomechanic", "source_uid": CRYPTO_UID,
                        "display_name": "Crypto Mechanic", "username": "CRYPTOMECHANIC",
                        "category": "official_trader_awardee", "identity_status": "official_award_payload_uid",
                        "identity_evidence_url": "https://www.binance.com/en/square/blockchain-100-2025"},
             "delay_after_seconds": 0},
            {"slug": "performance", "name": "Performance", "url": "https://www.binance.com/en/copy-trading",
             "fetch_url": "https://www.binance.com/en/copy-trading",
             "source_type": "binance_copy_trading_public_home", "adapter": "copy_trading_home",
             "access_level": "public", "distribution": "public_shareable", "expected_pillar": "performance",
             "portfolio_ids": PORTFOLIO_IDS, "delay_after_seconds": 0},
        ]}), encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def fake_fetch(self, url: str, **kwargs: object) -> FetchResult:
        if url.endswith("/winners"):
            self.assertEqual(kwargs.get("method"), "POST")
            self.assertEqual(kwargs.get("json_body"), {})
            return winner_response()
        if url.endswith("/posts"):
            return posts_response()
        return performance_response()

    def test_winners_deep_posts_and_portfolios_remain_semantically_separate(self) -> None:
        with patch("bti.ingest.fetch_public", side_effect=self.fake_fetch):
            first = ingest(self.db, self.manifest)
            second = ingest(self.db, self.manifest)
        self.assertEqual(first["new_contents"], 25)
        self.assertEqual(second["duplicates"], 25)
        with connect(self.db) as conn:
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM entities WHERE category='official_trader_awardee'"
            ).fetchone()[0], 20)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM recognitions").fetchone()[0], 20)
            semantics = conn.execute(
                "SELECT DISTINCT program_name,category_type,category_name,result FROM recognitions"
            ).fetchall()
            self.assertEqual([tuple(row) for row in semantics], [("Blockchain 100 2025", "content", "Trader", "Awardee")])
            crypto = conn.execute("SELECT id FROM entities WHERE source_uid=?", (CRYPTO_UID,)).fetchone()[0]
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM contents WHERE entity_id=?", (crypto,)).fetchone()[0], 3)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM evidence WHERE entity_id=? AND pillar='performance' AND verification_level='platform'",
                (crypto,),
            ).fetchone()[0], 0)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM entities WHERE category='copy_trading_portfolio'"
            ).fetchone()[0], 3)
            rows = conn.execute(
                "SELECT value_json FROM evidence WHERE kind='copy_trading_30d_snapshot' ORDER BY id"
            ).fetchall()
            values = [json.loads(row[0]) for row in rows]
            self.assertEqual([value["portfolio_id"] for value in values], PORTFOLIO_IDS)
            negative = next(value for value in values if value["portfolio_id"] == PORTFOLIO_IDS[1])
            self.assertLess(negative["copier_pnl"], 0)
            self.assertGreater(negative["leader_pnl"], 0)
        public_health = health(self.db)
        self.assertEqual(public_health["trader_quality"]["official_trader_awardees"], 20)
        self.assertEqual(public_health["trader_quality"]["awardee_substantive_contents"], 2)
        self.assertEqual(public_health["trader_quality"]["awardees_with_platform_performance"], 0)
        self.assertEqual(public_health["trader_quality"]["unlinked_platform_performance_portfolios"], 0)
        self.assertEqual(public_health["trader_quality"]["unlinked_captured_performance_portfolios"], 3)
        self.assertEqual(public_health["trader_quality"]["status"], "not_established")

    def test_fabricated_capture_label_never_grants_platform_verification(self) -> None:
        runtime_root = ROOT / "runtime"
        runtime_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=runtime_root) as local_temp:
            local_root = Path(local_temp)
            capture = json.loads(performance_response().body)
            capture["capture_method"] = "orca_fabricated"
            capture_path = local_root / "fabricated.json"
            capture_path.write_text(json.dumps(capture), encoding="utf-8")
            db_path = local_root / "capture.sqlite3"
            result = ingest_public_capture(
                db_path, self.manifest, source_slug="performance", capture_path=capture_path,
            )
            self.assertEqual(result["item_count"], 3)
            with connect(db_path) as conn:
                levels = {row[0] for row in conn.execute(
                    "SELECT verification_level FROM evidence WHERE kind='copy_trading_30d_snapshot'"
                )}
                self.assertEqual(levels, {"operator_capture"})
                limitations = [row[0] for row in conn.execute(
                    "SELECT limitations FROM evidence WHERE kind='copy_trading_30d_snapshot'"
                )]
                self.assertTrue(all(
                    "operator-supplied, capture provenance not independently authenticated" in item
                    for item in limitations
                ))

    def test_future_capture_timestamp_is_rejected(self) -> None:
        runtime_root = ROOT / "runtime"
        runtime_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=runtime_root) as local_temp:
            local_root = Path(local_temp)
            capture = json.loads(performance_response().body)
            capture["observed_at"] = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
            capture_path = local_root / "future.json"
            capture_path.write_text(json.dumps(capture), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "more than five minutes in the future"):
                ingest_public_capture(
                    local_root / "future.sqlite3", self.manifest,
                    source_slug="performance", capture_path=capture_path,
                )

    def test_directly_fetched_app_data_is_platform_level(self) -> None:
        capture = json.loads(performance_response().body)
        html_body = (
            '<html><script id="__APP_DATA" type="application/json">'
            + json.dumps(capture["appData"])
            + "</script></html>"
        ).encode("utf-8")
        result = FetchResult(
            "https://www.binance.com/en/copy-trading", 200, "text/html", html_body, 3,
            "2026-10-18T00:00:00Z",
        )
        source = json.loads(self.manifest.read_text(encoding="utf-8"))["sources"][2]
        items = parse_copy_trading_home(result, source)
        self.assertEqual({item.metadata["performance_verification_level"] for item in items}, {"platform"})

    def test_multi_category_blockchain100_roster_preserves_20_trader_invariant_and_filters_by_category(self) -> None:
        from bti.ingest import parse_award_winners
        from bti.public import dossier_listing

        traders = json.loads(winner_response().body)["data"]["groupList"][1]["winners"]
        researchers = [
            {
                "squareUid": f"res-uid-{rank:02d}",
                "username": f"researcher{rank:02d}",
                "displayName": f"Researcher {rank:02d}",
                "biography": f"Independent macro & on-chain researcher #{rank}",
                "voteCount": 9000 - rank,
                "rank": rank,
                "categoryType": 2,
                "shareLink": f"https://app.binance.com/uni-qr/cact25user/researcher{rank:02d}",
            }
            for rank in range(1, 4)
        ]
        rising = [
            {
                "squareUid": "1N9O1JMlRZruf6gYx7RRhA",
                "username": "vietlinh1999",
                "displayName": "LinhCrypto",
                "biography": "Vietnamese crypto creator",
                "voteCount": 3524,
                "rank": 9,
                "categoryType": 7,
                "shareLink": "https://app.binance.com/uni-qr/cact25user/vietlinh1999",
            }
        ]
        multi_res = response(
            {
                "code": "000000",
                "success": True,
                "data": {
                    "groupList": [
                        {"categoryType": 2, "name": "Independent Researcher", "winners": researchers},
                        {"categoryType": 3, "name": "Trader", "winners": traders},
                        {"categoryType": 7, "name": "Rising Star", "winners": rising},
                    ]
                },
            },
            url="https://www.binance.com/bapi/winners",
        )
        source = {
            "slug": "b100-all",
            "name": "Blockchain 100 All",
            "url": "https://www.binance.com/en/square/blockchain-100-2025",
            "fetch_url": "https://www.binance.com/bapi/winners",
            "source_type": "binance_official_award_winners",
            "adapter": "award_winners",
            "access_level": "public",
            "distribution": "public_shareable",
            "expected_pillar": "recognition",
            "include_all_categories": True,
            "expected_count": 24,
        }
        items = parse_award_winners(multi_res, source)
        self.assertEqual(len(items), 24)
        self.assertEqual(sum(1 for i in items if i.entity_category == "official_trader_awardee"), 20)
        self.assertEqual(sum(1 for i in items if i.entity_category == "square_creator_candidate"), 4)

        self.manifest.write_text(json.dumps({"sources": [source]}), encoding="utf-8")
        with patch("bti.ingest.fetch_public", return_value=multi_res):
            ingest(self.db, self.manifest)
        pub_health = health(self.db)
        self.assertEqual(pub_health["trader_quality"]["official_trader_awardees"], 20)
        res_dossiers = dossier_listing(self.db, category="Independent Researcher")
        self.assertEqual(res_dossiers["sample_count"], 3)
        self.assertEqual(res_dossiers["items"][0]["bio"], "Independent macro & on-chain researcher #1")
        rising_dossiers = dossier_listing(self.db, category="Rising Star")
        self.assertEqual(rising_dossiers["sample_count"], 1)
        self.assertEqual(rising_dossiers["items"][0]["username"], "vietlinh1999")

        # Verify 2026 nominee voting snapshots store dated versions without projecting recognition/performance awards
        nominee_source = {
            **source,
            "slug": "b100-2026-nominees",
            "program_name": "Blockchain 100 2026",
            "period_label": "2026",
            "award_result": "Nominee",
            "expected_category_counts": {"Independent Researcher": 3, "Trader": 20, "Rising Star": 1},
        }
        day1_items = parse_award_winners(multi_res, nominee_source)
        self.assertTrue(all(i.metadata["recognition"] is None for i in day1_items))
        self.assertTrue(all(i.metadata["nomination_vote_snapshot"] is not None for i in day1_items))
        self.assertTrue(all(i.entity_category == "square_creator_candidate" for i in day1_items))


if __name__ == "__main__":
    unittest.main()

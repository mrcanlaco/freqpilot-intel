from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from bti.db import connect, init_db
from bti.historical import run_backfill_batch, seed_queue
from bti.ingest import FetchResult, parse_square_detail, parse_square_user_posts
from bti.public import analyze_vietnamese_context, article_detail, articles, dossier, dossier_listing, overview, public_export

TEST_TMP = Path(__file__).resolve().parents[1] / "runtime" / "test-tmp"
HISTORICAL_MANIFEST = Path(__file__).resolve().parent / "fixtures" / "historical_seeds.json"


def detail_result(data: dict[str, object], fetched_at: str = "2026-10-18T00:00:00Z") -> FetchResult:
    body = json.dumps({"code": "000000", "data": data}).encode("utf-8")
    return FetchResult("https://www.binance.com/bapi/detail", 200, "application/json", body, 1, fetched_at)


def historical_source(**overrides: object) -> dict[str, object]:
    source: dict[str, object] = {
        "slug": "history", "name": "Historical article",
        "url": "https://www.binance.com/en/square/post/123",
        "source_type": "binance_square_creator_posts", "historical_backfill": True,
        "expected_post_id": "123", "expected_source_uid": "uid-good",
        "minimum_text_chars": 1000, "required_content_type": 2,
        "earliest_published_at": "2024-01-01T00:00:00Z",
        "latest_published_at": "2026-12-31T23:59:59Z",
        "derived_title": "",
        "entity": {"display_name": "Creator", "username": "creator", "category": "official_trader_awardee"},
    }
    source.update(overrides)
    return source


def historical_payload(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "id": 123, "title": "Substantive article", "bodyTextOnly": "risk process " * 120,
        "body": "", "squareUid": "uid-good", "username": "creator",
        "displayName": "Creator", "contentType": 2, "firstReleaseTime": 1731844494000,
        "createTime": 1731840000000, "likeCount": 5, "commentCount": 2, "viewCount": 100,
    }
    data.update(overrides)
    return data


class HistoricalValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        TEST_TMP.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=TEST_TMP)
        self.db = Path(self.temp.name) / "history.sqlite3"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_first_release_time_and_substance_are_preserved(self) -> None:
        item = parse_square_detail(detail_result(historical_payload()), historical_source())[0]
        self.assertEqual(item.published_at, "2024-11-17T11:54:54Z")
        self.assertEqual(item.metadata["source_uid"], "uid-good")
        self.assertTrue(item.metadata["historical_seed"])

    def test_wrong_uid_unknown_date_outside_window_and_short_quote_are_rejected(self) -> None:
        cases = [
            historical_payload(squareUid="wrong"),
            historical_payload(firstReleaseTime=None, createTime=None),
            historical_payload(firstReleaseTime=1672531200000),
            historical_payload(bodyTextOnly="short", body="", title=""),
        ]
        for payload in cases:
            with self.subTest(payload=payload.get("squareUid"),), self.assertRaises(ValueError):
                parse_square_detail(detail_result(payload), historical_source())

    def test_loose_manifest_cannot_accept_before_or_after_actual_rolling_window(self) -> None:
        outside = [
            datetime(2024, 4, 30, tzinfo=timezone.utc),
            datetime(2026, 11, 1, tzinfo=timezone.utc),
        ]
        for released in outside:
            payload = historical_payload(firstReleaseTime=int(released.timestamp() * 1000))
            with self.subTest(released=released.isoformat()), self.assertRaises(ValueError):
                parse_square_detail(
                    detail_result(payload, fetched_at="2026-10-18T00:00:00Z"),
                    historical_source(
                        earliest_published_at="2024-01-01T00:00:00Z",
                        latest_published_at="2026-12-31T23:59:59Z",
                    ),
                )

    def test_verified_long_type_one_can_use_explicit_derived_title(self) -> None:
        payload = historical_payload(title="", contentType=1, squareUid="uid-jobs", bodyTextOnly="ETH structure " * 110)
        source = historical_source(
            expected_source_uid="uid-jobs", required_content_type=1,
            derived_title="ETH market structure analysis — Crypto_Jobs",
        )
        item = parse_square_detail(detail_result(payload), source)[0]
        self.assertEqual(item.title, "ETH market structure analysis — Crypto_Jobs")

    def test_initial_profile_skips_pinned_outside_future_and_unknown_dates(self) -> None:
        def row(post_id: str, released: datetime | None) -> dict[str, object]:
            return {
                "id": post_id, "title": f"Post {post_id}", "bodyTextOnly": "risk controls " * 40,
                "contentType": 2, "squareUid": "uid-good", "username": "creator",
                "displayName": "Creator", "firstReleaseTime": (
                    int(released.timestamp() * 1000) if released else None
                ), "createTime": None, "likeCount": 1000,
            }

        payload = {"code": "000000", "data": {"contents": [
            row("12831954927514", datetime(2024, 8, 29, 17, 46, 51, tzinfo=timezone.utc)),
            row("inside", datetime(2025, 6, 1, tzinfo=timezone.utc)),
            row("future", datetime(2026, 11, 1, tzinfo=timezone.utc)),
            row("unknown", None),
        ]}}
        result = FetchResult(
            "https://www.binance.com/bapi/posts", 200, "application/json",
            json.dumps(payload).encode("utf-8"), 1, "2026-10-17T20:00:00Z",
        )
        source = {
            "slug": "profile", "name": "Profile", "url": "https://www.binance.com/en/square/profile/creator",
            "minimum_text_chars": 200, "max_items": 5,
            "entity": {"source_uid": "uid-good", "username": "creator", "display_name": "Creator"},
        }
        items = parse_square_user_posts(result, source)
        self.assertEqual([item.external_id for item in items], ["inside"])
        self.assertEqual(items[0].metadata["coverage_window_start"], "2024-10-17T20:00:00Z")
        self.assertEqual(items[0].metadata["coverage_window_end"], "2026-10-17T20:00:00Z")

    def test_backfill_queue_is_bounded_and_resumable(self) -> None:
        current = datetime(2026, 10, 18, tzinfo=timezone.utc)
        self.assertEqual(seed_queue(self.db, HISTORICAL_MANIFEST, now=current), 4)
        with connect(self.db) as conn:
            window = conn.execute(
                "SELECT requested_window_start,requested_window_end FROM backfill_seeds ORDER BY id LIMIT 1"
            ).fetchone()
        self.assertEqual(tuple(window), ("2024-10-18T00:00:00Z", "2026-10-18T00:00:00Z"))
        event = {"events": [{"outcome": "success", "status_code": 200}]}
        with patch("bti.historical.ingest_source_configs", return_value=event):
            first = run_backfill_batch(self.db, HISTORICAL_MANIFEST, now=current)
            second = run_backfill_batch(self.db, HISTORICAL_MANIFEST, now=datetime(2026, 10, 18, 1, tzinfo=timezone.utc))
        self.assertEqual(first, {
            "attempted": 3, "accepted": 3, "rejected": 0, "cooldown": 0, "policy_paused": 0,
        })
        self.assertEqual(second["accepted"], 1)
        with connect(self.db) as conn:
            statuses = [row[0] for row in conn.execute("SELECT status FROM backfill_seeds ORDER BY id")]
        self.assertEqual(statuses.count("accepted"), 4)
        self.assertEqual(statuses.count("pending"), 0)


class ResearchProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        TEST_TMP.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=TEST_TMP)
        self.db = Path(self.temp.name) / "research.sqlite3"
        init_db(self.db)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def add_article(
        self, slug: str, canonical: str, *, published: str | None, fetched: str,
        likes: int | None, comments: int | None, views: int | None,
        public: bool = True, rank: int | None = None, votes: int | None = None,
    ) -> None:
        share = 1 if public else 0
        access, distribution = ("public", "public_shareable") if public else ("private", "internal_only")
        with connect(self.db) as conn:
            source_id = conn.execute(
                """INSERT INTO sources(slug,name,url,fetch_url,source_type,adapter,access_level,distribution,
                       capability_status,created_at,updated_at) VALUES(?,?,?,?,?,'square_detail',?,?,'available',?,?)""",
                (slug, slug, f"https://www.binance.com/en/square/source/{slug}", f"https://www.binance.com/bapi/{slug}",
                 "binance_square_creator_posts", access, distribution, fetched, fetched),
            ).lastrowid
            entity_id = conn.execute(
                """INSERT INTO entities(slug,display_name,category,username,identity_status,public_shareable,created_at,updated_at)
                   VALUES(?,?, 'official_trader_awardee',?,'official_award_payload_uid',?,?,?)""",
                (f"entity-{slug}", f"Author {slug}", slug, share, fetched, fetched),
            ).lastrowid
            content_id = conn.execute(
                """INSERT INTO contents(source_id,entity_id,external_id,canonical_url,title,author,published_at,
                       first_seen_at,public_shareable,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (source_id, entity_id, slug, canonical, f"Title {slug}", f"Author {slug}", published,
                 fetched, share, fetched, fetched),
            ).lastrowid
            version_id = conn.execute(
                """INSERT INTO content_versions(content_id,version_no,fetched_at,checksum,normalized_text,excerpt,metadata_json)
                   VALUES(?,1,?,?,?,?, '{}')""",
                (content_id, fetched, f"checksum-{slug}", f"Research body {slug}", f"Excerpt {slug}"),
            ).lastrowid
            conn.execute("UPDATE contents SET latest_version_id=? WHERE id=?", (version_id, content_id))
            if any(value is not None for value in (likes, comments, views)):
                conn.execute(
                    """INSERT INTO evidence(source_id,entity_id,version_id,pillar,kind,verification_level,observed_at,
                           value_json,methodology,limitations,dependency_group,public_shareable)
                       VALUES(?,?,?,'engagement','post_metrics','platform',?,?,?,?,?,?)""",
                    (source_id, entity_id, version_id, fetched,
                     json.dumps({"likes": likes, "comments": comments, "views": views}),
                     "platform snapshot", "snapshot only", f"post:{canonical}", share),
                )
            if rank is not None:
                evidence_id = conn.execute(
                    """INSERT INTO evidence(source_id,entity_id,version_id,pillar,kind,verification_level,observed_at,
                           period_label,value_json,methodology,limitations,dependency_group,public_shareable)
                       VALUES(?,?,?,'recognition','official_program','official',?,'2025',?,?,?,?,?)""",
                    (source_id, entity_id, version_id, fetched,
                     json.dumps({"program": "Blockchain 100 2025", "category_name": "Trader", "rank": rank,
                                 "award_vote_count": votes}), "official", "content award only", f"award:{slug}", share),
                ).lastrowid
                conn.execute(
                    """INSERT INTO recognitions(evidence_id,program_name,category_type,category_name,result,known_at,interpretation_limit)
                       VALUES(?,'Blockchain 100 2025','content','Trader','Awardee',?,'content award only')""",
                    (evidence_id, fetched),
                )
            conn.commit()

    def test_articles_dedupe_canonical_and_use_latest_snapshot_not_sum(self) -> None:
        canonical = "https://www.binance.com/en/square/post/42"
        self.add_article("older", canonical, published="2025-01-01T00:00:00Z", fetched="2026-01-01T00:00:00Z", likes=10, comments=2, views=100)
        self.add_article("newer", canonical, published="2025-01-01T00:00:00Z", fetched="2026-02-01T00:00:00Z", likes=15, comments=3, views=120)
        payload = articles(self.db, sort="likes")
        self.assertEqual(payload["sample_count"], 1)
        self.assertEqual(payload["items"][0]["engagement"]["likes"], 15)

    def test_article_filters_require_original_date_and_unknown_metrics_sort_last(self) -> None:
        self.add_article("known", "https://www.binance.com/en/square/post/1", published="2025-06-01T00:00:00Z", fetched="2026-01-01T00:00:00Z", likes=2, comments=1, views=10)
        self.add_article("unknown", "https://www.binance.com/en/square/post/2", published=None, fetched="2026-02-01T00:00:00Z", likes=None, comments=None, views=None)
        self.add_article("private-marker", "https://www.binance.com/en/square/post/3", published="2025-06-01T00:00:00Z", fetched="2026-03-01T00:00:00Z", likes=999, comments=999, views=999, public=False)
        ranged = articles(self.db, date_from="2025-01-01", date_to="2025-12-31")
        self.assertEqual([item["entity_slug"] for item in ranged["items"]], ["entity-known"])
        ranked = articles(self.db, sort="likes")
        self.assertEqual([item["entity_slug"] for item in ranked["items"]], ["entity-known"])
        self.assertEqual(ranked["date_window"]["mode"], "default_trailing_two_calendar_years")
        self.assertNotIn("private-marker", json.dumps(ranked))

        coverage = overview(self.db)["research_coverage"]
        self.assertEqual(coverage["status"], "partial")
        self.assertEqual(coverage["requested_window"]["basis"], "trailing_two_calendar_years")
        start = datetime.fromisoformat(coverage["requested_window"]["start"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(coverage["requested_window"]["end"].replace("Z", "+00:00"))
        self.assertEqual(
            (start.month, start.day, start.hour, start.minute, start.second),
            (end.month, end.day, end.hour, end.minute, end.second),
        )
        self.assertEqual(start.year + 2, end.year)

    def test_default_top_likes_excludes_outside_pinned_future_and_unknown_but_audit_can_include_old(self) -> None:
        current = datetime(2026, 10, 17, 20, 0, tzinfo=timezone.utc)
        self.add_article(
            "pinned", "https://www.binance.com/en/square/post/12831954927514",
            published="2024-08-29T17:46:51Z", fetched="2026-10-17T19:00:00Z",
            likes=50_000, comments=10, views=100_000,
        )
        self.add_article(
            "inside", "https://www.binance.com/en/square/post/inside",
            published="2025-06-01T00:00:00Z", fetched="2026-10-17T19:01:00Z",
            likes=5, comments=1, views=20,
        )
        self.add_article(
            "future", "https://www.binance.com/en/square/post/future",
            published="2026-11-01T00:00:00Z", fetched="2026-10-17T19:02:00Z",
            likes=60_000, comments=20, views=200_000,
        )
        self.add_article(
            "unknown-date", "https://www.binance.com/en/square/post/unknown",
            published=None, fetched="2026-10-17T19:03:00Z",
            likes=70_000, comments=30, views=300_000,
        )

        default = articles(self.db, sort="likes", now=current)
        self.assertEqual([item["entity_slug"] for item in default["items"]], ["entity-inside"])
        self.assertEqual(default["date_window"], {
            "mode": "default_trailing_two_calendar_years",
            "date_from": "2024-10-17", "date_to": "2026-10-17",
            "future_and_unknown_original_dates_excluded": True,
        })
        self.assertNotIn("12831954927514", json.dumps(default))

        audit = articles(
            self.db, sort="likes", date_from="2024-08-01", date_to="2024-09-30", now=current,
        )
        self.assertEqual([item["entity_slug"] for item in audit["items"]], ["entity-pinned"])
        self.assertEqual(audit["date_window"]["mode"], "explicit_audit")
        future_audit = articles(
            self.db, date_from="2026-11-01", date_to="2026-12-01", now=current,
        )
        self.assertEqual(future_audit["items"], [])

    def test_dossier_filters_and_noncombined_official_sorts(self) -> None:
        self.add_article("rank2", "https://www.binance.com/en/square/post/r2", published="2025-01-01T00:00:00Z", fetched="2026-01-01T00:00:00Z", likes=1, comments=1, views=1, rank=2, votes=10)
        self.add_article("rank1", "https://www.binance.com/en/square/post/r1", published="2025-01-02T00:00:00Z", fetched="2026-01-02T00:00:00Z", likes=1, comments=1, views=1, rank=1, votes=5)
        by_rank = dossier_listing(self.db, program="Blockchain 100", year="2025", category="Trader", sort="official_rank")
        self.assertEqual([item["slug"] for item in by_rank["items"]], ["entity-rank1", "entity-rank2"])
        by_votes = dossier_listing(self.db, sort="award_votes")
        self.assertEqual([item["slug"] for item in by_votes["items"]], ["entity-rank2", "entity-rank1"])
        self.assertIn("collected public dataset only", by_votes["scope_note"])


    def test_vietnamese_first_priority_filtering_and_article_detail_audit(self) -> None:
        current = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)
        self.add_article(
            "en-post", "https://www.binance.com/en/square/post/en101",
            published="2026-10-17T10:00:00Z", fetched="2026-10-17T11:00:00Z",
            likes=500, comments=40, views=5000,
        )
        self.add_article(
            "vi-post", "https://www.binance.com/en/square/post/vi202",
            published="2026-10-16T10:00:00Z", fetched="2026-10-16T11:00:00Z",
            likes=50, comments=5, views=600,
        )
        with connect(self.db) as conn:
            conn.execute(
                "UPDATE contents SET language='vi', title=? WHERE external_id='vi-post'",
                ("Phân tích cấu trúc giá $BTC và quản trị rủi ro cắt lỗ tại $62,000",),
            )
            conn.execute(
                "UPDATE content_versions SET excerpt=?, normalized_text=? WHERE content_id=(SELECT id FROM contents WHERE external_id='vi-post')",
                (
                    "Kịch bản điều kiện cho $BTC: nếu thủng vùng hỗ trợ thì cắt lỗ để bảo toàn vốn.",
                    "FULL_NORMALIZED_PRIVATE_BODY_DO_NOT_EXPOSE " + ("Kịch bản điều kiện cho $BTC: nếu thủng vùng hỗ trợ thì cắt lỗ để bảo toàn vốn. " * 6),
                ),
            )
            conn.execute(
                "UPDATE contents SET language='en', title=? WHERE external_id='en-post'",
                ("$ETH liquidity structure and risk management invalidation below $2,400",),
            )
            conn.commit()

        default_likes = articles(self.db, sort="likes", now=current)
        self.assertEqual([item["post_id"] for item in default_likes["items"]], ["en-post", "vi-post"])

        vi_first = articles(self.db, sort="likes", lang="vi_first", now=current)
        self.assertEqual([item["post_id"] for item in vi_first["items"]], ["vi-post", "en-post"])
        self.assertTrue(vi_first["items"][0]["vi_intel"]["is_vietnamese"])
        self.assertIn("BTC", vi_first["items"][0]["vi_intel"]["assets"])
        self.assertIn("risk", vi_first["items"][0]["vi_intel"]["topics"])
        self.assertTrue(vi_first["items"][0]["vi_intel"]["discipline_checklist"]["has_invalidation"])
        self.assertFalse(vi_first["items"][1]["vi_intel"]["is_vietnamese"])
        self.assertIn("Khung kiểm toán Tiếng Việt", vi_first["items"][1]["vi_intel"]["vi_summary"])

        vi_only = articles(self.db, lang="vi", now=current)
        self.assertEqual([item["post_id"] for item in vi_only["items"]], ["vi-post"])

        btc_only = articles(self.db, asset="BTC", now=current)
        self.assertEqual([item["post_id"] for item in btc_only["items"]], ["vi-post"])

        detail = article_detail(self.db, "vi-post")
        self.assertIsNotNone(detail)
        assert detail is not None
        self.assertEqual(detail["post_id"], "vi-post")
        self.assertTrue(detail["vi_intel"]["is_vietnamese"])
        self.assertEqual(detail["author_pillars"]["engagement"]["status"], "available")
        self.assertNotIn("FULL_NORMALIZED_PRIVATE_BODY_DO_NOT_EXPOSE", json.dumps(detail))
        self.assertNotIn("FULL_NORMALIZED_PRIVATE_BODY_DO_NOT_EXPOSE", json.dumps(public_export(self.db)))

        ambiguous = analyze_vietnamese_context(
            title="Click this LINK near the OP to see the DOT and UNI",
            excerpt="They remain persistent and explain the sentry charter after a ripple effect and avalanche.",
            language="en",
        )
        self.assertEqual(ambiguous["assets"], [])
        self.assertEqual(ambiguous["topics"], ["ecosystem"])
        pt_non_vi = analyze_vietnamese_context(
            title="Análise de mercado: ações e índices em São Paulo",
            excerpt="Visão geral com várias observações sobre liquidez.",
            language="pt",
        )
        self.assertFalse(pt_non_vi["is_vietnamese"])
        self.assertEqual(pt_non_vi["language_code"], "pt")
        explicit = analyze_vietnamese_context(
            title="$LINK and $NEAR setup with BTC/USDT and Polkadot",
            excerpt="Stop loss invalidation below $12.",
            language="en",
        )
        self.assertEqual(explicit["assets"], ["LINK", "NEAR", "BTC", "DOT"])

        ov = overview(self.db)
        self.assertEqual(ov["vietnamese_portal"]["vi_native_articles"], 1)
        self.assertEqual(ov["vietnamese_portal"]["keyword_tagged_articles"], 2)
        self.assertEqual(ov["vietnamese_portal"]["goal_and_backlog_vi"]["target_quality_bodies"], 1000)
if __name__ == "__main__":
    unittest.main()

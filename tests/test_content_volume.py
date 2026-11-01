from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from bti.content_volume import (
    QUALITY_GATE_VERSION,
    ARCHIVE_MANUAL_HOLD_UNTIL,
    _adaptive_archive_budget,
    _archive_cooldown,
    _archive_item,
    _build_content_fetcher,
    _cdx_url,
    _content_state,
    _partition_specs,
    _same_static_post_replay_redirect,
    _set_archive_cooldown,
    _store_result,
    consume_archive_candidates,
    content_volume_status,
    discover_cdx,
    fetch_archive,
)
from bti.db import connect, init_db
from bti.domain import build_drafts
from bti.ingest import AccessStopped, FetchCoordinator, FetchResult, ParsedItem
from bti.public import articles, drafts


TEST_TMP = Path("runtime/test-tmp")
TEST_TMP.mkdir(parents=True, exist_ok=True)


def archive_source() -> dict[str, object]:
    return {
        "slug": "test-public-archive",
        "name": "Synthetic static archive fixture",
        "url": "https://web.archive.org/cdx/search/cdx?url=fixture",
        "fetch_url": "https://web.archive.org/cdx/search/cdx?url=fixture",
        "source_type": "binance_square_public_archive",
        "adapter": "wayback_app_data",
        "access_level": "public",
        "distribution": "public_shareable",
        "expected_pillar": "engagement",
    }


def content_manifest(*, cdx_requests: int = 2) -> dict[str, object]:
    return {
        "baseline": {
            "started_at": "2026-10-18T03:32:17Z",
            "article_count": 19,
            "qualifying_post_ids": [],
        },
        "minimum_text_chars": 200,
        "max_archive_fetches_per_cycle": 40,
        "throughput": {
            "base_archive_fetches_per_cycle": 40,
            "max_archive_fetches_per_cycle": 80,
            "max_total_archive_requests_per_cycle": 100,
            "scale_queue_threshold": 80,
            "scale_minimum_acceptance_rate": 0.25,
        },
        "archive_source": archive_source(),
        "cdx": {"cadence_seconds": 600, "max_rows": 500, "max_requests_per_cycle": cdx_requests},
        "profile_snapshots": [],
        "cached_details": [],
    }


def parsed_item(post_id: str, uid: str = "uid-public") -> ParsedItem:
    text = (f"Evidence-bearing public article {post_id}. " * 12).strip()
    return ParsedItem(
        external_id=post_id,
        canonical_url=f"https://www.binance.com/en/square/post/{post_id}",
        title=f"Article {post_id}",
        text=text,
        author=f"Author {uid}",
        username=f"author-{uid}",
        published_at="2026-01-01T00:00:00Z",
        language="en",
        metadata={
            "source_uid": uid,
            "identity_status": "archive_embedded_source_uid",
            "provenance_type": "archive_snapshot",
            "archive_snapshot_url": f"https://web.archive.org/web/20260102000000id_/https://www.binance.com/en/square/post/{post_id}",
            "archive_observed_at": "2026-01-02T00:00:00Z",
            "content_volume_goal": True,
            "qualifying_body": True,
            "engagement": {"views": None, "likes": None, "comments": None, "shares": None},
            "engagement_verification_level": "secondary",
            "engagement_observed_at": "2026-01-02T00:00:00Z",
        },
        entity_category="square_creator_candidate",
    )


class ContentVolumeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(dir=TEST_TMP)
        self.root = Path(self.temp.name)
        self.db = self.root / "bti.sqlite3"
        init_db(self.db)
        self.fetch = FetchResult(
            url="https://web.archive.org/web/20260102000000id_/https://www.binance.com/en/square/post/12345678",
            status=200,
            content_type="text/html",
            body=b"fixture-response",
            latency_ms=1,
            fetched_at="2026-01-02T00:00:00Z",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_http200_block_page_stops_next_jobs_and_persists_shared_hold(self) -> None:
        current = datetime(2026, 10, 18, 4, 0, tzinfo=timezone.utc)
        manifest = content_manifest()
        _content_state(self.db, manifest, current)
        with connect(self.db) as conn:
            for post_id in ("12345678", "12345679"):
                original = f"https://www.binance.com/en/square/post/{post_id}"
                conn.execute(
                    """INSERT INTO content_archive_candidates(
                           post_id,canonical_url,original_url,snapshot_timestamp,archive_url,status,discovered_at,updated_at
                       ) VALUES(?,?,?,?,?,'pending',?,?)""",
                    (post_id, original, original, "20261018030000",
                     f"https://web.archive.org/web/20261018030000id_/{original}",
                     "2026-10-18T04:00:00Z", "2026-10-18T04:00:00Z"),
                )
            conn.commit()
        calls = []

        def blocked_fetch(url: str, **_kwargs: object) -> FetchResult:
            calls.append(url)
            return FetchResult(url, 200, "text/html", b"<html><title>GLOBE - Website Blocked</title></html>", 1, "2026-10-18T04:00:00Z")

        report = consume_archive_candidates(
            self.db, manifest, artifact_root=self.root / "artifacts", current=current,
            coordinator=FetchCoordinator(concurrency=1, minimum_spacing_seconds=1.0),
            max_fetch=2, fetcher=blocked_fetch,
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(report["blocked"], 1)
        self.assertEqual(report["errors"], 0)
        self.assertEqual(report["accepted_count"], 0)
        self.assertEqual(report["status"], "blocked")
        self.assertIsNotNone(_archive_cooldown(self.db, current))
        with connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM contents").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM content_archive_candidates WHERE status='pending'").fetchone()[0], 1)

    def test_block_phrase_in_article_body_is_not_a_network_gate(self) -> None:
        from bti.content_volume import _dispatch_archive_url
        result = FetchResult(self.fetch.url, 200, "text/html", b"<title>Market research</title><p>Website blocked and access denied are example messages.</p>", 1, self.fetch.fetched_at)
        self.assertIs(_dispatch_archive_url(lambda *a, **k: result, result.url,
                      coordinator=FetchCoordinator(concurrency=1, minimum_spacing_seconds=1.0)), result)

    def test_archive_parser_keeps_unknown_uid_unlinked_and_rejects_bad_dates(self) -> None:
        current = datetime(2026, 10, 18, tzinfo=timezone.utc)
        base = {
            "id": "12345678",
            "webLink": "https://www.binance.com/en/square/post/12345678",
            "bodyTextOnly": (
                "Risk management starts with a written invalidation level and a position size small enough "
                "to survive normal volatility. This archived research note compares liquidity, leverage, "
                "market structure, and downside scenarios before considering an entry. It records observable "
                "conditions, separates evidence from assumptions, and explains why capital preservation matters "
                "more than a confident prediction. Readers should verify the cited market data independently."
            ),
            "firstReleaseTime": 1767225600000,
            "username": "known-handle",
            "displayName": "Known Handle",
        }
        item, reason = _archive_item(
            base,
            archive_url="https://web.archive.org/web/20260102000000id_/https://www.binance.com/en/square/post/12345678",
            original_url=base["webLink"],
            response_sha256="a" * 64,
            observed_at="2026-01-02T00:00:00Z",
            current=current,
            minimum_text_chars=200,
        )
        self.assertIsNone(reason)
        self.assertIsNotNone(item)
        assert item is not None
        self.assertIsNone(item.metadata["source_uid"])
        self.assertEqual(item.metadata["identity_status"], "archive_handle_only")
        self.assertTrue(str(item.metadata["entity_slug"]).startswith("archive-handle-"))

        future = dict(base, firstReleaseTime=1793491200000)
        rejected, reason = _archive_item(
            future,
            archive_url="https://web.archive.org/web/20261019000000id_/https://www.binance.com/en/square/post/12345678",
            original_url=base["webLink"], response_sha256="b" * 64,
            observed_at="2026-10-19T00:00:00Z", current=current, minimum_text_chars=200,
        )
        self.assertIsNone(rejected)
        self.assertEqual(reason, "outside_rolling_window")

        promotional = dict(
            base,
            bodyTextOnly=(
                "This note names liquidity, leverage, volatility, invalidation, support, resistance, and risk. "
                "It presents several observations about position sizing and market structure for careful readers. "
                "Join my Telegram using this referral code for guaranteed profit and copy my trades today. "
                "The remaining context discusses entries, exits, drawdown limits, and independent verification."
            ),
        )
        rejected, reason = _archive_item(
            promotional,
            archive_url="https://web.archive.org/web/20260102000000id_/https://www.binance.com/en/square/post/12345678",
            original_url=base["webLink"], response_sha256="c" * 64,
            observed_at="2026-01-02T00:00:00Z", current=current, minimum_text_chars=200,
        )
        self.assertIsNone(rejected)
        self.assertEqual(reason, "promotional_or_referral")

    def test_redirect_is_denied_without_following_location(self) -> None:
        error = urllib.error.HTTPError(
            "https://web.archive.org/web/20260102000000id_/https://www.binance.com/en/square/post/12345678",
            302, "redirect", {"Location": "https://example.com/private"}, None,
        )
        opener = mock.Mock()
        opener.open.side_effect = error
        coordinator = FetchCoordinator(concurrency=1, minimum_spacing_seconds=1.0)
        with mock.patch("urllib.request.build_opener", return_value=opener):
            with self.assertRaises(AccessStopped) as caught:
                fetch_archive(error.url, coordinator=coordinator)
        self.assertEqual(caught.exception.status_code, 302)
        self.assertEqual(opener.open.call_count, 1)

    def test_revoked_owner_is_not_reapproved_or_publicly_projected(self) -> None:
        source = archive_source()
        first = _store_result(self.db, source, self.fetch, [parsed_item("12345678")], note="fixture")
        self.assertEqual(len(first["accepted"]), 1)
        with connect(self.db) as conn:
            conn.execute("UPDATE entities SET public_shareable=0 WHERE source_uid='uid-public'")
            conn.commit()
        second = _store_result(self.db, source, self.fetch, [parsed_item("12345679")], note="fixture")
        self.assertEqual(second["rejected_permission"], 1)
        with connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM contents").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT public_shareable FROM entities WHERE source_uid='uid-public'").fetchone()[0], 0)
        self.assertEqual(articles(self.db)["sample_count"], 0)

    def test_archive_uid_preserves_official_award_identity_and_nickname_cannot_merge(self) -> None:
        now = "2026-10-18T04:14:31Z"
        official_uid = "VAYGRi3YqX6T-PlzOHmeqQ"
        official_evidence = "https://www.binance.com/en/square/blockchain-100-2025"
        with connect(self.db) as conn:
            official_id = int(conn.execute(
                """INSERT INTO entities(
                       slug,display_name,category,source_uid,username,identity_status,
                       identity_evidence_url,public_shareable,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,1,?,?)""",
                ("square-cryptomechanic", "Crypto Mechanic", "official_trader_awardee",
                 official_uid, "cryptomechanic", "official_award_payload_uid",
                 official_evidence, now, now),
            ).lastrowid)
            conn.commit()

        archive = parsed_item("22345678", uid=official_uid)
        archive.author = "CRYPTO MECHANIC"
        archive.username = "cryptomechanic"
        archive.entity_category = "square_creator_candidate"
        archive.metadata.update({
            "entity_slug": "square-cryptomechanic",
            "identity_status": "archive_embedded_source_uid",
            "identity_evidence_url": (
                "https://web.archive.org/web/20250222235919id_/"
                "https://www.binance.com/en/square/profile/cryptomechanic"
            ),
        })
        first = _store_result(self.db, archive_source(), self.fetch, [archive], note="archive replay")
        self.assertEqual(len(first["accepted"]), 1)

        nickname_collision = parsed_item("22345679", uid="DIFFERENT-ARCHIVE-UID")
        nickname_collision.author = "Crypto Mechanic"
        nickname_collision.username = "cryptomechanic"
        nickname_collision.metadata["entity_slug"] = "square-cryptomechanic"
        second = _store_result(
            self.db, archive_source(), self.fetch, [nickname_collision], note="nickname collision",
        )
        self.assertEqual(len(second["accepted"]), 1)

        with connect(self.db) as conn:
            official = conn.execute(
                """SELECT id,slug,display_name,category,source_uid,username,identity_status,
                          identity_evidence_url,public_shareable
                   FROM entities WHERE source_uid=?""",
                (official_uid,),
            ).fetchone()
            self.assertEqual(
                tuple(official),
                (official_id, "square-cryptomechanic", "Crypto Mechanic", "official_trader_awardee",
                 official_uid, "cryptomechanic", "official_award_payload_uid", official_evidence, 1),
            )
            first_owner = conn.execute(
                "SELECT entity_id FROM contents WHERE external_id='22345678'"
            ).fetchone()[0]
            second_owner = conn.execute(
                "SELECT entity_id FROM contents WHERE external_id='22345679'"
            ).fetchone()[0]
            other = conn.execute(
                "SELECT slug,category FROM entities WHERE source_uid='DIFFERENT-ARCHIVE-UID'"
            ).fetchone()
            self.assertEqual(first_owner, official_id)
            self.assertNotEqual(second_owner, official_id)
            self.assertNotEqual(other["slug"], "square-cryptomechanic")
            self.assertEqual(other["category"], "square_creator_candidate")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0], 2)

    def test_public_archive_articles_and_derived_outputs_are_visible_with_citations(self) -> None:
        source = archive_source()
        for index in range(5):
            item = parsed_item(str(12345680 + index), uid=f"uid-{index}")
            item.text += " additional analysis context" * index
            outcome = _store_result(self.db, source, self.fetch, [item], note="fixture")
            self.assertEqual(len(outcome["accepted"]), 1)
        article_data = articles(self.db, now=datetime(2026, 10, 18, tzinfo=timezone.utc))
        self.assertEqual(article_data["sample_count"], 5)
        self.assertTrue(all(item["provenance_type"] == "archive_snapshot" for item in article_data["items"]))
        self.assertTrue(all("web.archive.org" in item["citation"]["source_url"] for item in article_data["items"]))

        build_drafts(self.db, now=datetime(2026, 10, 18, tzinfo=timezone.utc))
        visible = drafts(self.db)
        kinds = {item["body"]["output_kind"] for item in visible}
        self.assertEqual(kinds, {"source_excerpt", "research_backlog"})
        for item in visible:
            brief = item["body"]["research_brief"]
            self.assertFalse(brief["strategy_eligible"])
            self.assertFalse(brief["market_eligible"])
            self.assertIsNone(brief["trigger"])
            self.assertIsNone(brief["invalidation"])
        for item in visible:
            self.assertTrue(item["body"]["derived_output"])
            self.assertTrue(item["body"]["citations"])
            self.assertTrue(item["body"]["unknowns"])

    def test_status_counts_persisted_bodies_not_queue_rows(self) -> None:
        with connect(self.db) as conn:
            conn.execute(
                """INSERT INTO content_volume_state(
                       id,baseline_started_at,baseline_article_count,created_at,updated_at
                   ) VALUES(1,?,?,?,?)""",
                ("2026-10-18T03:32:17Z", 19, "2026-10-18T03:32:17Z", "2026-10-18T03:32:17Z"),
            )
            conn.execute(
                """INSERT INTO content_archive_candidates(
                       post_id,canonical_url,original_url,snapshot_timestamp,archive_url,status,discovered_at,updated_at
                   ) VALUES(?,?,?,?,?,'pending',?,?)""",
                ("12345678", "https://www.binance.com/en/square/post/12345678",
                 "https://www.binance.com/en/square/post/12345678", "20260102000000",
                 "https://web.archive.org/web/20260102000000id_/https://www.binance.com/en/square/post/12345678",
                 "2026-10-18T03:32:17Z", "2026-10-18T03:32:17Z"),
            )
            conn.commit()
        status = content_volume_status(self.db)
        self.assertEqual(status["new_qualifying_bodies_since_baseline"], 0)
        self.assertEqual(status["archive_candidate_status"], {"pending": 1})

    def test_partitioned_cdx_discovery_is_resumable_recent_first_and_deduped(self) -> None:
        current = datetime(2026, 10, 18, 4, 0, tzinfo=timezone.utc)
        manifest = content_manifest(cdx_requests=2)
        _content_state(self.db, manifest, current)
        specs = _partition_specs(current)
        self.assertEqual([item["coverage_class"] for item in specs if item["priority"] == 0][-1], "recent_7d")
        calls: list[str] = []
        ranges: list[tuple[str, str]] = []

        def fake_fetch(url: str, **_kwargs: object) -> FetchResult:
            calls.append(url)
            query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            day = query["from"][0]
            end_day = query["to"][0]
            ranges.append((day, end_day))
            self.assertEqual(query["showResumeKey"], ["true"])
            self.assertEqual(query["fl"], ["urlkey,timestamp,original,mimetype,statuscode,digest,length"])
            post_id = "99887766554433"
            original = f"https://www.binance.com/en/square/post/{post_id}"
            payload: list[list[str]] = [
                ["urlkey", "timestamp", "original", "mimetype", "statuscode", "digest", "length"],
                [f"com,binance)/en/square/post/{post_id}", end_day + "010203", original,
                 "text/html", "200", "ARCHIVEDIGEST", "4096"],
            ]
            if len(calls) == 1:
                payload.extend([[], ["resume-key-for-next-page"]])
            body = json.dumps(payload).encode("utf-8")
            return FetchResult(url=url, status=200, content_type="application/json", body=body,
                               latency_ms=1, fetched_at="2026-10-18T04:00:00Z")

        result = discover_cdx(
            self.db, manifest, artifact_root=self.root / "artifacts",
            coordinator=FetchCoordinator(concurrency=1, minimum_spacing_seconds=1.5),
            current=current, fetcher=fake_fetch,
        )
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["attempted_requests"], 2)
        self.assertEqual(result["requests"], 2)
        self.assertEqual(result["rows"], 2)
        self.assertEqual(result["new_candidates"], 1)
        self.assertEqual(len(calls), 2)
        self.assertTrue(any(start == end for start, end in ranges))
        self.assertTrue(any(start < end for start, end in ranges))
        with connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM content_archive_candidates").fetchone()[0], 1)
            resumable = conn.execute(
                "SELECT resume_key FROM content_discovery_partitions WHERE status='resumable'"
            ).fetchone()
            self.assertEqual(resumable["resume_key"], "resume-key-for-next-page")

        not_due_calls = 0

        def must_not_fetch(_url: str, **_kwargs: object) -> FetchResult:
            nonlocal not_due_calls
            not_due_calls += 1
            raise AssertionError("600-second CDX cadence should still be closed")

        not_due = discover_cdx(
            self.db, manifest, artifact_root=self.root / "artifacts",
            coordinator=FetchCoordinator(concurrency=1, minimum_spacing_seconds=1.5),
            current=current + timedelta(seconds=599), fetcher=must_not_fetch,
        )
        self.assertEqual(not_due["status"], "not_due")
        self.assertEqual(not_due_calls, 0)

    def test_empty_recent_cdx_is_complete_and_broad_partition_still_queues_real_row(self) -> None:
        current = datetime(2026, 10, 18, 4, 0, tzinfo=timezone.utc)
        manifest = content_manifest(cdx_requests=3)
        _content_state(self.db, manifest, current)
        calls: list[tuple[str, str]] = []
        empty_hash = hashlib.sha256(b"[]\n").hexdigest()

        def fake_fetch(url: str, **_kwargs: object) -> FetchResult:
            query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            date_from = query["from"][0]
            date_to = query["to"][0]
            calls.append((date_from, date_to))
            if len(calls) < 3:
                body = b"[]\n"
            else:
                post_id = "31043354196538"
                original = f"https://www.binance.com/en/square/post/{post_id}"
                body = json.dumps([
                    ["urlkey", "timestamp", "original", "mimetype", "statuscode", "digest", "length"],
                    [f"com,binance)/en/square/post/{post_id}", "20261017010203", original,
                     "text/html", "200", "REALSCHEMADIGEST", "6722"],
                ]).encode("utf-8")
            return FetchResult(
                url=url, status=200, content_type="application/json", body=body,
                latency_ms=1, fetched_at="2026-10-18T04:00:00Z",
            )

        result = discover_cdx(
            self.db, manifest, artifact_root=self.root / "artifacts",
            coordinator=FetchCoordinator(concurrency=1, minimum_spacing_seconds=1.5),
            current=current, fetcher=fake_fetch,
        )

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["attempted_requests"], 3)
        self.assertEqual(result["requests"], 3)
        self.assertEqual(result["rows"], 1)
        self.assertEqual(result["new_candidates"], 1)
        self.assertEqual(result["response_hashes"][:2], [empty_hash, empty_hash])
        self.assertEqual(len(result["raw_artifacts"]), 3)
        self.assertEqual(Path(result["raw_artifacts"][0]).read_bytes(), b"[]\n")
        self.assertEqual(calls[0][0], calls[0][1])
        self.assertLess(calls[-1][0], calls[-1][1])
        with connect(self.db) as conn:
            queued = conn.execute(
                "SELECT post_id,status FROM content_archive_candidates"
            ).fetchall()
            completed_empty = conn.execute(
                """SELECT COUNT(*) FROM content_discovery_partitions
                   WHERE status='complete' AND rows_seen=0 AND last_outcome='success'"""
            ).fetchone()[0]
        self.assertEqual([(row["post_id"], row["status"]) for row in queued], [
            ("31043354196538", "pending"),
        ])
        self.assertGreaterEqual(completed_empty, 2)

    def test_malformed_cdx_keeps_raw_provider_response_before_parse_error(self) -> None:
        current = datetime(2026, 10, 18, 4, 0, tzinfo=timezone.utc)
        manifest = content_manifest(cdx_requests=1)
        _content_state(self.db, manifest, current)
        raw_body = b'{"unexpected":true}\n'

        def malformed_fetch(url: str, **_kwargs: object) -> FetchResult:
            return FetchResult(
                url=url, status=200, content_type="application/json", body=raw_body,
                latency_ms=1, fetched_at="2026-10-18T04:00:00Z",
            )

        result = discover_cdx(
            self.db, manifest, artifact_root=self.root / "artifacts",
            coordinator=FetchCoordinator(concurrency=1, minimum_spacing_seconds=1.5),
            current=current, fetcher=malformed_fetch,
        )

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["attempted_requests"], 1)
        self.assertEqual(result["requests"], 1)
        self.assertEqual(len(result["raw_artifacts"]), 1)
        self.assertEqual(Path(result["raw_artifacts"][0]).read_bytes(), raw_body)
        self.assertEqual(result["response_sha256"], hashlib.sha256(raw_body).hexdigest())

    def test_archive_429_cooldown_survives_coordinator_restart(self) -> None:
        current = datetime(2026, 10, 18, 4, 0, tzinfo=timezone.utc)
        manifest = content_manifest(cdx_requests=1)
        _content_state(self.db, manifest, current)
        blocked_calls = 0

        def blocked_fetch(_url: str, **_kwargs: object) -> FetchResult:
            nonlocal blocked_calls
            blocked_calls += 1
            raise AccessStopped("fixture rate limit", status_code=429)

        first = discover_cdx(
            self.db, manifest, artifact_root=self.root / "artifacts",
            coordinator=FetchCoordinator(concurrency=1, minimum_spacing_seconds=1.5),
            current=current, fetcher=blocked_fetch,
        )
        self.assertEqual(first["status"], "blocked")
        self.assertEqual(first["attempted_requests"], 1)
        self.assertEqual(first["requests"], 0)

        during_calls = 0

        def must_not_fetch(_url: str, **_kwargs: object) -> FetchResult:
            nonlocal during_calls
            during_calls += 1
            raise AssertionError("host cooldown should prevent network access")

        during = discover_cdx(
            self.db, manifest, artifact_root=self.root / "artifacts",
            coordinator=FetchCoordinator(concurrency=1, minimum_spacing_seconds=1.5),
            current=current + timedelta(hours=1), fetcher=must_not_fetch,
        )
        self.assertEqual(during["status"], "host_cooldown")
        self.assertEqual(during_calls, 0)

        def empty_fetch(url: str, **_kwargs: object) -> FetchResult:
            header = [["urlkey", "timestamp", "original", "mimetype", "statuscode", "digest", "length"]]
            return FetchResult(url=url, status=200, content_type="application/json",
                               body=json.dumps(header).encode("utf-8"), latency_ms=1,
                               fetched_at="2026-10-18T11:00:00Z")

        resumed = discover_cdx(
            self.db, manifest, artifact_root=self.root / "artifacts",
            coordinator=FetchCoordinator(concurrency=1, minimum_spacing_seconds=1.5),
            current=current + timedelta(hours=7), fetcher=empty_fetch,
        )
        self.assertEqual(resumed["status"], "success")
        self.assertEqual(resumed["attempted_requests"], 1)
        self.assertEqual(blocked_calls, 1)

    def test_retry_after_over_24_hours_becomes_manual_hold_without_fake_status(self) -> None:
        current = datetime(2026, 10, 18, 4, 0, tzinfo=timezone.utc)
        _content_state(self.db, content_manifest(), current)
        until = _set_archive_cooldown(
            self.db, current, 429, retry_after_seconds=2 * 86400,
        )
        self.assertEqual(until, ARCHIVE_MANUAL_HOLD_UNTIL)
        state = _archive_cooldown(self.db, current + timedelta(days=30))
        self.assertEqual(state, {
            "until": ARCHIVE_MANUAL_HOLD_UNTIL,
            "status_code": 429,
            "manual_hold": True,
        })
        with connect(self.db) as conn:
            conn.execute(
                "UPDATE content_volume_state SET archive_last_blocked_status=NULL WHERE id=1"
            )
            conn.commit()
        unknown = _archive_cooldown(self.db, current)
        self.assertIsNone(unknown["status_code"])
        self.assertTrue(unknown["manual_hold"])

    def test_adaptive_budget_scales_only_for_measured_yield_and_respects_total_cap(self) -> None:
        current = datetime(2026, 10, 18, 4, 0, tzinfo=timezone.utc)
        manifest = content_manifest()
        _content_state(self.db, manifest, current)
        rows = []
        for index in range(120):
            status = "pending" if index < 80 else "accepted" if index < 110 else "rejected"
            post_id = str(70000000000000 + index)
            rows.append((
                post_id, f"https://www.binance.com/en/square/post/{post_id}",
                f"https://www.binance.com/en/square/post/{post_id}", "20261017000000",
                f"https://web.archive.org/web/20261017000000id_/https://www.binance.com/en/square/post/{post_id}",
                status, "2026-10-18T04:00:00Z", "2026-10-18T04:00:00Z",
            ))
        with connect(self.db) as conn:
            conn.executemany(
                """INSERT INTO content_archive_candidates(
                       post_id,canonical_url,original_url,snapshot_timestamp,archive_url,status,discovered_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                rows,
            )
            conn.commit()
        scaled = _adaptive_archive_budget(
            self.db, manifest, explicit_max_fetch=None, requests_remaining=50, current=current,
        )
        self.assertEqual(scaled["reason"], "scaled_for_productive_backlog")
        self.assertEqual(scaled["budget"], 50)
        self.assertAlmostEqual(scaled["historical_acceptance_rate"], 0.75)
        explicit = _adaptive_archive_budget(
            self.db, manifest, explicit_max_fetch=99, requests_remaining=10, current=current,
        )
        self.assertEqual(explicit["budget"], 10)

    def test_archive_consumer_persists_only_quality_gated_body_and_status_is_honest(self) -> None:
        current = datetime(2026, 10, 18, 4, 0, tzinfo=timezone.utc)
        manifest = content_manifest()
        _content_state(self.db, manifest, current)
        post_id = "88990011223344"
        archive_url = (
            f"https://web.archive.org/web/20261018030000id_/"
            f"https://www.binance.com/en/square/post/{post_id}"
        )
        with connect(self.db) as conn:
            conn.execute(
                """INSERT INTO content_archive_candidates(
                       post_id,canonical_url,original_url,snapshot_timestamp,archive_url,status,discovered_at,updated_at
                   ) VALUES(?,?,?,?,?,'pending',?,?)""",
                (post_id, f"https://www.binance.com/en/square/post/{post_id}",
                 f"https://www.binance.com/en/square/post/{post_id}", "20261018030000", archive_url,
                 "2026-10-18T04:00:00Z", "2026-10-18T04:00:00Z"),
            )
            conn.commit()
        body_text = (
            "A disciplined trading plan begins with liquidity mapping and a precise invalidation level. "
            "This research note compares market structure, volatility, leverage, and position sizing before entry. "
            "It describes a downside scenario, a neutral scenario, and an upside scenario without promising returns. "
            "Risk remains uncertain, so evidence should be checked independently and exposure kept within a fixed limit."
        )
        row = {
            "id": post_id,
            "webLink": f"https://www.binance.com/en/square/post/{post_id}",
            "bodyTextOnly": body_text,
            "firstReleaseTime": 1792209600000,
            "squareUid": "QUALITY-UID-1",
            "username": "quality-author",
            "displayName": "Quality Author",
            "likeCount": 12,
            "commentCount": 3,
            "shareCount": 2,
        }
        html_body = (
            '<html><script id="__APP_DATA" type="application/json">'
            + json.dumps({"data": {"post": row}})
            + "</script></html>"
        ).encode("utf-8")

        def fake_fetch(url: str, **_kwargs: object) -> FetchResult:
            return FetchResult(url=url, status=200, content_type="text/html", body=html_body,
                               latency_ms=1, fetched_at="2026-10-18T04:00:00Z")

        consumed = consume_archive_candidates(
            self.db, manifest, artifact_root=self.root / "artifacts",
            coordinator=FetchCoordinator(concurrency=1, minimum_spacing_seconds=1.5),
            current=current, max_fetch=5, fetcher=fake_fetch,
        )
        self.assertEqual(consumed["accepted_count"], 1)
        self.assertEqual(consumed["attempted"], 1)
        with connect(self.db) as conn:
            metadata = json.loads(conn.execute(
                "SELECT metadata_json FROM content_versions"
            ).fetchone()[0])
            self.assertEqual(metadata["quality_gate_version"], QUALITY_GATE_VERSION)
            self.assertEqual(conn.execute(
                "SELECT status FROM content_archive_candidates WHERE post_id=?", (post_id,)
            ).fetchone()[0], "accepted")
        status = content_volume_status(self.db, now=current)
        self.assertEqual(status["total_unique_qualifying_bodies"], 1)
        self.assertEqual(status["quality_audited_bodies"], 1)
        self.assertFalse(status["milestone_1000_quality_reached"])
        self.assertEqual(status["quality_audit_status"], "partial")

    def test_missing_archive_snapshot_rejects_candidate_without_blocking_next_valid_job(self) -> None:
        current = datetime(2026, 10, 18, 4, 0, tzinfo=timezone.utc)
        manifest = content_manifest()
        _content_state(self.db, manifest, current)
        missing_post_id = "19938834281146"
        valid_post_id = "31043354196538"
        archive_urls: dict[str, str] = {}
        with connect(self.db) as conn:
            for post_id, timestamp in (
                (missing_post_id, "20261018040000"),
                (valid_post_id, "20261018030000"),
            ):
                archive_url = (
                    f"https://web.archive.org/web/{timestamp}id_/"
                    f"https://www.binance.com/en/square/post/{post_id}"
                )
                archive_urls[post_id] = archive_url
                conn.execute(
                    """INSERT INTO content_archive_candidates(
                           post_id,canonical_url,original_url,snapshot_timestamp,archive_url,status,discovered_at,updated_at
                       ) VALUES(?,?,?,?,?,'pending',?,?)""",
                    (post_id, f"https://www.binance.com/en/square/post/{post_id}",
                     f"https://www.binance.com/en/square/post/{post_id}", timestamp, archive_url,
                     "2026-10-18T04:00:00Z", "2026-10-18T04:00:00Z"),
                )
            conn.commit()

        valid_row = {
            "id": valid_post_id,
            "webLink": f"https://www.binance.com/en/square/post/{valid_post_id}",
            "bodyTextOnly": (
                "A disciplined market study starts with liquidity, volatility, and a precise invalidation level. "
                "This research compares position sizing, leverage, market structure, and downside exposure before entry. "
                "It outlines bearish, neutral, and bullish scenarios with observable evidence for rejecting each thesis. "
                "No return is promised, and every conclusion remains conditional on independently verified public data."
            ),
            "firstReleaseTime": 1792209600000,
            "squareUid": "CRYPTO-JOBS-UID",
            "username": "crypto_jobs",
            "displayName": "Crypto Jobs",
        }
        valid_body = (
            '<html><script id="__APP_DATA" type="application/json">'
            + json.dumps({"post": valid_row}) + "</script></html>"
        ).encode("utf-8")
        calls: list[str] = []

        def fixture_fetch(url: str, *, coordinator: FetchCoordinator, **_kwargs: object) -> FetchResult:
            calls.append(url)
            coordinator.before_request("web.archive.org", url)
            if url == archive_urls[missing_post_id]:
                raise AccessStopped("Static archive snapshot is missing", status_code=404)
            self.assertEqual(url, archive_urls[valid_post_id])
            return FetchResult(
                url=url, status=200, content_type="text/html", body=valid_body,
                latency_ms=1, fetched_at="2026-10-18T04:00:00Z",
            )

        consumed = consume_archive_candidates(
            self.db, manifest, artifact_root=self.root / "artifacts",
            coordinator=FetchCoordinator(concurrency=1, minimum_spacing_seconds=1.0),
            current=current, max_fetch=2, fetcher=fixture_fetch,
        )

        self.assertEqual(calls, [archive_urls[missing_post_id], archive_urls[valid_post_id]])
        self.assertEqual(consumed["attempted"], 2)
        self.assertEqual(consumed["accepted_count"], 1)
        self.assertEqual(consumed["blocked"], 0)
        self.assertEqual(consumed["errors"], 0)
        self.assertEqual(consumed["rejected"], {"archive_snapshot_http_404": 1})
        self.assertEqual(consumed["status"], "productive")
        self.assertIsNone(_archive_cooldown(self.db, current))
        with connect(self.db) as conn:
            candidate_rows = {
                row["post_id"]: (row["status"], row["last_outcome"], row["attempt_count"])
                for row in conn.execute(
                    """SELECT post_id,status,last_outcome,attempt_count
                       FROM content_archive_candidates ORDER BY post_id"""
                ).fetchall()
            }
            state = conn.execute(
                """SELECT archive_cooldown_until,archive_last_blocked_status
                   FROM content_volume_state WHERE id=1"""
            ).fetchone()
        self.assertEqual(candidate_rows[missing_post_id], (
            "rejected", "archive_snapshot_http_404", 1,
        ))
        self.assertEqual(candidate_rows[valid_post_id], ("accepted", "accepted", 1))
        self.assertIsNone(state["archive_cooldown_until"])
        self.assertIsNone(state["archive_last_blocked_status"])

    def test_local_archive_302_preserves_location_without_following(self) -> None:
        requested = (
            "https://web.archive.org/web/20261008150208id_/"
            "https://www.binance.com/en/square/post/"
            "05-12-2026-bnb-chain-s-real-world-assets-tvl-surpasses-4-billion-322329882794049"
        )
        location = requested.replace("20261008150208id_", "20261008150207id_")
        opener = mock.Mock()
        opener.open.side_effect = urllib.error.HTTPError(
            requested, 302, "Found", {"Location": location, "Retry-After": "120"}, None,
        )
        coordinator = FetchCoordinator(concurrency=1, minimum_spacing_seconds=1.0)

        with mock.patch("urllib.request.build_opener", return_value=opener):
            with self.assertRaises(AccessStopped) as stopped:
                fetch_archive(requested, coordinator=coordinator)

        self.assertEqual(stopped.exception.status_code, 302)
        self.assertEqual(stopped.exception.redirect_location, location)
        self.assertEqual(stopped.exception.retry_after_seconds, 120)
        opener.open.assert_called_once()

    def test_safe_static_replay_302_rejects_only_candidate_then_imports_next_job(self) -> None:
        current = datetime(2026, 10, 18, 4, 0, tzinfo=timezone.utc)
        manifest = content_manifest()
        _content_state(self.db, manifest, current)
        redirected_post_id = "322329882794049"
        valid_post_id = "31043354196538"
        requested = (
            "https://web.archive.org/web/20261008150208id_/"
            "https://www.binance.com/en/square/post/"
            "05-12-2026-bnb-chain-s-real-world-assets-tvl-surpasses-4-billion-322329882794049"
        )
        safe_location = requested.replace("20261008150208id_", "20261008150207id_")
        valid_published_at = int(
            datetime(2026, 10, 7, 12, tzinfo=timezone.utc).timestamp() * 1000
        )
        valid_url = (
            "https://web.archive.org/web/20261008150206id_/"
            f"https://www.binance.com/en/square/post/{valid_post_id}"
        )
        with connect(self.db) as conn:
            for post_id, timestamp, archive_url in (
                (redirected_post_id, "20261008150208", requested),
                (valid_post_id, "20261008150206", valid_url),
            ):
                conn.execute(
                    """INSERT INTO content_archive_candidates(
                           post_id,canonical_url,original_url,snapshot_timestamp,archive_url,status,discovered_at,updated_at
                       ) VALUES(?,?,?,?,?,'pending',?,?)""",
                    (post_id, f"https://www.binance.com/en/square/post/{post_id}",
                     f"https://www.binance.com/en/square/post/{post_id}", timestamp, archive_url,
                     "2026-10-18T04:00:00Z", "2026-10-18T04:00:00Z"),
                )
            conn.commit()

        valid_row = {
            "id": valid_post_id,
            "webLink": f"https://www.binance.com/en/square/post/{valid_post_id}",
            "bodyTextOnly": (
                "A disciplined market study starts with liquidity, volatility, and a precise invalidation level. "
                "This research compares position sizing, leverage, market structure, and downside exposure before entry. "
                "It outlines bearish, neutral, and bullish scenarios with observable evidence for rejecting each thesis. "
                "No return is promised, and every conclusion remains conditional on independently verified public data."
            ),
            "firstReleaseTime": 1792209600000,
            "squareUid": "CRYPTO-JOBS-UID",
            "username": "crypto_jobs",
            "displayName": "Crypto Jobs",
        }
        valid_body = (
            '<html><script id="__APP_DATA" type="application/json">'
            + json.dumps({"post": dict(valid_row, firstReleaseTime=valid_published_at)})
            + "</script></html>"
        ).encode("utf-8")
        calls: list[str] = []

        def fixture_fetch(url: str, *, coordinator: FetchCoordinator, **_kwargs: object) -> FetchResult:
            calls.append(url)
            coordinator.before_request("web.archive.org", url)
            if url == requested:
                stopped = AccessStopped("Static replay points to nearest capture", status_code=302)
                stopped.redirect_location = safe_location
                raise stopped
            self.assertEqual(url, valid_url)
            return FetchResult(
                url=url, status=200, content_type="text/html", body=valid_body,
                latency_ms=1, fetched_at="2026-10-18T04:00:00Z",
            )

        consumed = consume_archive_candidates(
            self.db, manifest, artifact_root=self.root / "artifacts",
            coordinator=FetchCoordinator(concurrency=1, minimum_spacing_seconds=1.0),
            current=current, max_fetch=2, fetcher=fixture_fetch,
        )

        self.assertEqual(calls, [requested, valid_url])
        self.assertEqual(consumed["attempted"], 2)
        self.assertEqual(consumed["accepted_count"], 1)
        self.assertEqual(consumed["blocked"], 0)
        self.assertEqual(consumed["rejected"], {"archive_snapshot_redirect_not_followed": 1})
        self.assertIsNone(_archive_cooldown(self.db, current))
        with connect(self.db) as conn:
            redirected = conn.execute(
                """SELECT status,last_outcome,attempt_count FROM content_archive_candidates
                   WHERE post_id=?""",
                (redirected_post_id,),
            ).fetchone()
            valid = conn.execute(
                "SELECT status,last_outcome FROM content_archive_candidates WHERE post_id=?",
                (valid_post_id,),
            ).fetchone()
        self.assertEqual(tuple(redirected), (
            "rejected", "archive_snapshot_redirect_not_followed", 1,
        ))
        self.assertEqual(tuple(valid), ("accepted", "accepted"))

    def test_unknown_or_login_302_remains_global_fail_closed_hold(self) -> None:
        current = datetime(2026, 10, 18, 4, 0, tzinfo=timezone.utc)
        manifest = content_manifest()
        _content_state(self.db, manifest, current)
        blocked_post_id = "322329882794049"
        next_post_id = "31043354196538"
        blocked_url = (
            "https://web.archive.org/web/20261008150208id_/"
            f"https://www.binance.com/en/square/post/{blocked_post_id}"
        )
        next_url = (
            "https://web.archive.org/web/20261008150207id_/"
            f"https://www.binance.com/en/square/post/{next_post_id}"
        )
        unsafe_location = "https://accounts.binance.com/en/login"
        self.assertFalse(_same_static_post_replay_redirect(blocked_url, None))
        self.assertFalse(_same_static_post_replay_redirect(blocked_url, unsafe_location))
        self.assertFalse(_same_static_post_replay_redirect(
            blocked_url,
            "https://web.archive.org/web/20261008150207id_/"
            "https://www.binance.com/en/square/post/different-post-399999999999999",
        ))
        with connect(self.db) as conn:
            for post_id, timestamp, archive_url in (
                (blocked_post_id, "20261008150208", blocked_url),
                (next_post_id, "20261008150207", next_url),
            ):
                conn.execute(
                    """INSERT INTO content_archive_candidates(
                           post_id,canonical_url,original_url,snapshot_timestamp,archive_url,status,discovered_at,updated_at
                       ) VALUES(?,?,?,?,?,'pending',?,?)""",
                    (post_id, f"https://www.binance.com/en/square/post/{post_id}",
                     f"https://www.binance.com/en/square/post/{post_id}", timestamp, archive_url,
                     "2026-10-18T04:00:00Z", "2026-10-18T04:00:00Z"),
                )
            conn.commit()
        calls: list[str] = []

        def blocked_fetch(url: str, *, coordinator: FetchCoordinator, **_kwargs: object) -> FetchResult:
            calls.append(url)
            coordinator.before_request("web.archive.org", url)
            stopped = AccessStopped("Untrusted redirect", status_code=302)
            stopped.redirect_location = unsafe_location
            raise stopped

        consumed = consume_archive_candidates(
            self.db, manifest, artifact_root=self.root / "artifacts",
            coordinator=FetchCoordinator(concurrency=1, minimum_spacing_seconds=1.0),
            current=current, max_fetch=2, fetcher=blocked_fetch,
        )

        self.assertEqual(calls, [blocked_url])
        self.assertEqual(consumed["attempted"], 1)
        self.assertEqual(consumed["accepted_count"], 0)
        self.assertEqual(consumed["blocked"], 1)
        self.assertEqual(consumed["status"], "blocked")
        cooldown = _archive_cooldown(self.db, current)
        self.assertEqual(cooldown["status_code"], 302)
        with connect(self.db) as conn:
            statuses = {
                row["post_id"]: (row["status"], row["last_outcome"])
                for row in conn.execute(
                    "SELECT post_id,status,last_outcome FROM content_archive_candidates"
                ).fetchall()
            }
        self.assertEqual(statuses[blocked_post_id], ("cooldown", "access_stopped"))
        self.assertEqual(statuses[next_post_id], ("pending", None))

    def test_safe_static_replay_302_with_retry_after_remains_global_hold(self) -> None:
        current = datetime(2026, 10, 18, 4, 0, tzinfo=timezone.utc)
        manifest = content_manifest()
        _content_state(self.db, manifest, current)
        post_id = "322329882794049"
        requested = (
            "https://web.archive.org/web/20261008150208id_/"
            "https://www.binance.com/en/square/post/"
            "05-12-2026-bnb-chain-s-real-world-assets-tvl-surpasses-4-billion-322329882794049"
        )
        safe_location = requested.replace("20261008150208id_", "20261008150207id_")
        with connect(self.db) as conn:
            conn.execute(
                """INSERT INTO content_archive_candidates(
                       post_id,canonical_url,original_url,snapshot_timestamp,archive_url,status,discovered_at,updated_at
                   ) VALUES(?,?,?,?,?,'pending',?,?)""",
                (post_id, f"https://www.binance.com/en/square/post/{post_id}",
                 f"https://www.binance.com/en/square/post/{post_id}", "20261008150208", requested,
                 "2026-10-18T04:00:00Z", "2026-10-18T04:00:00Z"),
            )
            conn.commit()

        def retry_after_fetch(
            url: str, *, coordinator: FetchCoordinator, **_kwargs: object,
        ) -> FetchResult:
            coordinator.before_request("web.archive.org", url)
            stopped = AccessStopped(
                "Provider instructed delayed retry", status_code=302, retry_after_seconds=120,
            )
            stopped.redirect_location = safe_location
            raise stopped

        consumed = consume_archive_candidates(
            self.db, manifest, artifact_root=self.root / "artifacts",
            coordinator=FetchCoordinator(concurrency=1, minimum_spacing_seconds=1.0),
            current=current, max_fetch=1, fetcher=retry_after_fetch,
        )

        self.assertEqual(consumed["blocked"], 1)
        self.assertEqual(consumed["rejected"], {})
        self.assertEqual(consumed["status"], "blocked")
        cooldown = _archive_cooldown(self.db, current)
        self.assertEqual(cooldown["status_code"], 302)
        with connect(self.db) as conn:
            candidate = conn.execute(
                "SELECT status,last_outcome FROM content_archive_candidates WHERE post_id=?",
                (post_id,),
            ).fetchone()
        self.assertEqual(tuple(candidate), ("cooldown", "access_stopped"))

    def test_hybrid_prefetch_is_two_total_and_imports_are_serial_on_caller(self) -> None:
        current = datetime(2026, 10, 18, 4, 0, tzinfo=timezone.utc)
        manifest = content_manifest()
        _content_state(self.db, manifest, current)
        post_ids = ["88990011223345", "88990011223346"]
        with connect(self.db) as conn:
            for post_id in post_ids:
                archive_url = (
                    f"https://web.archive.org/web/20261018030000id_/"
                    f"https://www.binance.com/en/square/post/{post_id}"
                )
                conn.execute(
                    """INSERT INTO content_archive_candidates(
                           post_id,canonical_url,original_url,snapshot_timestamp,archive_url,status,discovered_at,updated_at
                       ) VALUES(?,?,?,?,?,'pending',?,?)""",
                    (post_id, f"https://www.binance.com/en/square/post/{post_id}",
                     f"https://www.binance.com/en/square/post/{post_id}", "20261018030000", archive_url,
                     "2026-10-18T04:00:00Z", "2026-10-18T04:00:00Z"),
                )
            conn.commit()

        caller_thread = threading.get_ident()
        lock = threading.Lock()
        second_started = threading.Event()
        active = maximum_active = calls = 0
        network_threads: list[int] = []
        imported: list[tuple[int, int]] = []

        class HybridFixture:
            def __call__(self, url: str, *, coordinator: FetchCoordinator, **_kwargs: object) -> FetchResult:
                nonlocal active, maximum_active, calls
                coordinator.before_request("web.archive.org", url)
                with lock:
                    ordinal = calls
                    calls += 1
                    active += 1
                    maximum_active = max(maximum_active, active)
                    network_threads.append(threading.get_ident())
                    if ordinal == 1:
                        second_started.set()
                if ordinal == 0:
                    self.assert_second_started()
                time.sleep(0.05)
                post_id = url.rsplit("/", 1)[-1]
                row = {
                    "id": post_id,
                    "webLink": f"https://www.binance.com/en/square/post/{post_id}",
                    "bodyTextOnly": (
                        f"Research case {post_id} maps liquidity and a precise invalidation level before entry. "
                        "It compares market structure, volatility, leverage, position sizing, and downside risk. "
                        "Three conditional scenarios explain what evidence would support or reject the hypothesis. "
                        "No return is promised, and every observation needs independent verification before exposure."
                    ),
                    "firstReleaseTime": 1792209600000,
                    "squareUid": f"QUALITY-{post_id}",
                    "username": f"author-{post_id}",
                    "displayName": f"Author {post_id}",
                }
                payload = (
                    '<script id="__APP_DATA" type="application/json">'
                    + json.dumps({"post": row}) + "</script>"
                ).encode("utf-8")
                with lock:
                    active -= 1
                result = FetchResult(url=url, status=200, content_type="text/html", body=payload,
                                     latency_ms=1050, fetched_at="2026-10-18T04:00:00Z")
                result.executor_metadata = {"node": "msi" if ordinal == 0 else "desktop"}
                return result

            def assert_second_started(self) -> None:
                if not second_started.wait(2.5):
                    raise AssertionError("second shared-pool fetch did not overlap")

            def record_imported(self, _result: FetchResult, count: int) -> None:
                imported.append((threading.get_ident(), count))

        fetched = consume_archive_candidates(
            self.db, manifest, artifact_root=self.root / "artifacts",
            coordinator=FetchCoordinator(concurrency=2, minimum_spacing_seconds=1.0),
            current=current, max_fetch=2, fetcher=HybridFixture(),
        )
        self.assertEqual(fetched["accepted_count"], 2)
        self.assertEqual(fetched["network_max_inflight"], 2)
        self.assertEqual(fetched["sqlite_writer"], "caller_thread_serial")
        self.assertEqual(maximum_active, 2)
        self.assertTrue(all(thread != caller_thread for thread in network_threads))
        self.assertEqual(imported, [(caller_thread, 1), (caller_thread, 1)])
        with connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM contents").fetchone()[0], 2)

    def test_shared_circuit_stops_next_hybrid_batch_without_node_fallback(self) -> None:
        current = datetime(2026, 10, 18, 4, 0, tzinfo=timezone.utc)
        manifest = content_manifest()
        _content_state(self.db, manifest, current)
        with connect(self.db) as conn:
            for index in range(3):
                post_id = str(99000000123450 + index)
                conn.execute(
                    """INSERT INTO content_archive_candidates(
                           post_id,canonical_url,original_url,snapshot_timestamp,archive_url,status,discovered_at,updated_at
                       ) VALUES(?,?,?,?,?,'pending',?,?)""",
                    (post_id, f"https://www.binance.com/en/square/post/{post_id}",
                     f"https://www.binance.com/en/square/post/{post_id}", "20261018030000",
                     f"https://web.archive.org/web/20261018030000id_/https://www.binance.com/en/square/post/{post_id}",
                     "2026-10-18T04:00:00Z", "2026-10-18T04:00:00Z"),
                )
            conn.commit()
        lock = threading.Lock()
        assignments = network_starts = 0

        def blocked_fetch(url: str, *, coordinator: FetchCoordinator, **_kwargs: object) -> FetchResult:
            nonlocal assignments, network_starts
            with lock:
                ordinal = assignments
                assignments += 1
            coordinator.before_request("web.archive.org", url)
            with lock:
                network_starts += 1
            if ordinal == 0:
                coordinator.trip(429)
                raise AccessStopped("fixture remote 429", status_code=429)
            raise AssertionError("second node must not start after the shared circuit trips")

        result = consume_archive_candidates(
            self.db, manifest, artifact_root=self.root / "artifacts",
            coordinator=FetchCoordinator(concurrency=2, minimum_spacing_seconds=1.0),
            current=current, max_fetch=3, fetcher=blocked_fetch,
        )
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["attempted"], 2)
        self.assertEqual(network_starts, 1)
        with connect(self.db) as conn:
            statuses = [row[0] for row in conn.execute(
                "SELECT status FROM content_archive_candidates ORDER BY post_id"
            ).fetchall()]
        self.assertEqual(statuses, ["cooldown", "cooldown", "pending"])

    def test_msi_bridge_activation_uses_persisted_authorization_for_both_nodes(self) -> None:
        current = datetime(2026, 10, 18, 4, 0, tzinfo=timezone.utc)
        manifest = content_manifest()
        _content_state(self.db, manifest, current)
        config_path = self.root / "msi-fetch-enabled.json"
        config_path.write_text(
            json.dumps({"enabled": True, "mode": "hybrid"}) + "\n", encoding="utf-8",
        )

        class BridgeFixture:
            def __init__(self, local_fetcher: object, *, authorization_check: object, mode: str) -> None:
                self.local_fetcher = local_fetcher
                self.authorization_check = authorization_check
                self.mode = mode

        def local_fetcher(*_args: object, **_kwargs: object) -> FetchResult:
            raise AssertionError("callsite test must not perform network")

        bridge = _build_content_fetcher(
            self.db, local_fetcher, config_path=config_path, bridge_factory=BridgeFixture,
            clock=lambda: current,
        )
        self.assertIsInstance(bridge, BridgeFixture)
        self.assertEqual(bridge.mode, "hybrid")
        allowed_url = (
            "https://web.archive.org/web/20261018030000id_/"
            "https://www.binance.com/en/square/post/123456789"
        )
        self.assertTrue(bridge.authorization_check(allowed_url))
        with connect(self.db) as conn:
            conn.execute(
                """UPDATE content_volume_state SET archive_cooldown_until=?,
                       archive_last_blocked_status=429,updated_at=? WHERE id=1""",
                ("2026-10-18T10:00:00Z", "2026-10-18T04:00:00Z"),
            )
            conn.commit()
        with self.assertRaises(AccessStopped) as denied:
            bridge.authorization_check(allowed_url)
        self.assertEqual(denied.exception.status_code, 429)
        with self.assertRaises(ValueError):
            bridge.authorization_check("https://example.com/not-allowed")

    def test_partial_commit_is_preserved_and_private_diagnostic_is_bounded_without_public_leak(self) -> None:
        current = datetime(2026, 10, 18, 4, 0, tzinfo=timezone.utc)
        manifest_path = self.root / "content_sources.json"
        manifest_path.write_text(json.dumps(content_manifest()), encoding="utf-8")
        post_ids = ["77000000111111", "77000000222222"]
        with connect(self.db) as conn:
            _content_state(self.db, content_manifest(), current)
            for post_id in post_ids:
                archive_url = (
                    f"https://web.archive.org/web/20261018030000id_/"
                    f"https://www.binance.com/en/square/post/{post_id}"
                )
                conn.execute(
                    """INSERT INTO content_archive_candidates(
                           post_id,canonical_url,original_url,snapshot_timestamp,archive_url,status,discovered_at,updated_at
                       ) VALUES(?,?,?,?,?,'pending',?,?)""",
                    (post_id, f"https://www.binance.com/en/square/post/{post_id}",
                     f"https://www.binance.com/en/square/post/{post_id}", "20261018030000", archive_url,
                     "2026-10-18T04:00:00Z", "2026-10-18T04:00:00Z"),
                )
            conn.commit()

        def archive_fetcher(url: str, *, coordinator: FetchCoordinator, **_kwargs: object) -> FetchResult:
            coordinator.before_request("web.archive.org", url)
            if "cdx/search/cdx" in url:
                return FetchResult(url, 200, "application/json", b"[]", 1, "2026-10-18T04:00:00Z")
            post_id = url.rsplit("/", 1)[-1]
            row = {
                "id": post_id,
                "webLink": f"https://www.binance.com/en/square/post/{post_id}",
                "bodyTextOnly": (
                    f"Substantive analysis {post_id} defines invalidation, position sizing, and downside scenarios. "
                    "Market structure and liquidity levels are compared against volatility and funding conditions. "
                    "Every thesis requires independent verification and strict risk controls before any action."
                ),
                "firstReleaseTime": 1760745600000,
                "squareUid": f"UID-{post_id}",
                "username": f"user-{post_id}",
                "displayName": f"User {post_id}",
            }
            body = (
                '<script id="__APP_DATA" type="application/json">'
                + json.dumps({"post": row}) + "</script>"
            ).encode("utf-8")
            return FetchResult(url, 200, "text/html", body, 10, "2026-10-18T04:00:00Z")

        from unittest.mock import patch
        from bti.content_volume import parse_archive_post as real_parse, run_content_volume_batch
        calls = 0

        def fail_after_first_commit(*args: object, **kwargs: object) -> tuple[ParsedItem | None, str | None]:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("SENSITIVE_TOKEN_SHOULD_NEVER_APPEAR_IN_DIAGNOSTIC")
            return real_parse(*args, **kwargs)

        with patch("bti.content_volume.parse_archive_post", side_effect=fail_after_first_commit):
            report = run_content_volume_batch(
                self.db, manifest_path, artifact_dir=self.root / "content-volume",
                max_fetch=2, now=current, archive_fetcher=archive_fetcher,
            )

        self.assertEqual(report["new_qualifying_bodies_this_batch"], 1)
        self.assertEqual(report["batch_status"], "partial_error")
        self.assertEqual(len(report["private_diagnostics"]), 1)
        diag = report["private_diagnostics"][0]
        self.assertEqual(diag["phase"], "archive_consumer_item")
        self.assertEqual(diag["error_class"], "RuntimeError")
        self.assertNotIn("SENSITIVE_TOKEN_SHOULD_NEVER_APPEAR_IN_DIAGNOSTIC", json.dumps(report))

        from bti.public import health
        pub_health = health(self.db)
        self.assertNotIn("private_diagnostics", json.dumps(pub_health))
        self.assertNotIn("RuntimeError", json.dumps(pub_health))
        self.assertEqual(pub_health["content_extraction"]["new_qualifying_bodies_since_baseline"], 1)
        self.assertEqual(report["status"]["current_extraction_status"], "productive")
        self.assertEqual(pub_health["content_extraction"]["lifetime_status"], "productive")
        self.assertEqual(pub_health["content_extraction"]["current_extraction_status"], "stalled")

        with connect(self.db) as conn:
            conn.execute(
                "UPDATE content_volume_state SET archive_cooldown_until='2026-10-18T10:00:00Z', archive_last_blocked_status=202 WHERE id=1"
            )
            conn.commit()
        cooled = content_volume_status(self.db, now=current)
        self.assertEqual(cooled["lifetime_status"], "productive")
        self.assertEqual(cooled["current_extraction_status"], "cooldown")
        self.assertTrue(cooled["archive_host_cooldown"]["active"])

        from bti.worker import run_cycle
        sources_manifest = self.root / "public_sources.json"
        sources_manifest.write_text(json.dumps({"sources": [{
            "slug": "disabled-source",
            "name": "Disabled source",
            "url": "https://www.binance.com/en/square/trending",
            "fetch_url": "https://www.binance.com/bapi/disabled",
            "source_type": "binance_square_public_feed",
            "adapter": "square_feed",
            "access_level": "public",
            "distribution": "public_shareable",
            "schedule": "disabled",
        }]}), encoding="utf-8")
        with connect(self.db) as conn:
            conn.execute("UPDATE content_volume_state SET archive_cooldown_until=NULL, archive_last_blocked_status=NULL WHERE id=1")
            for extra_id in ("77000000333333", "77000000444444"):
                conn.execute(
                    """INSERT INTO content_archive_candidates(
                           post_id,canonical_url,original_url,snapshot_timestamp,archive_url,status,discovered_at,updated_at
                       ) VALUES(?,?,?,?,?,'pending','2026-10-18T04:00:00Z','2026-10-18T04:00:00Z')""",
                    (
                        extra_id,
                        f"https://www.binance.com/en/square/post/{extra_id}",
                        f"https://www.binance.com/en/square/post/{extra_id}",
                        "20261018030000",
                        f"https://web.archive.org/web/20261018030000id_/https://www.binance.com/en/square/post/{extra_id}",
                    ),
                )
            conn.commit()
        calls = 0
        with patch("bti.content_volume.parse_archive_post", side_effect=fail_after_first_commit), patch("bti.worker.build_drafts"):
            cycle = run_cycle(
                self.db, sources_manifest, now=current, backup_dir=None,
                policy_checker_override=lambda _u: (True, "test_allow"),
                content_fetcher_override=archive_fetcher,
            )
        self.assertEqual(cycle["status"], "degraded")
        self.assertIn("content_volume", cycle["source_counts"]["safe_failure_phases"])
        self.assertEqual(cycle["source_counts"]["content_extraction"]["new_qualifying_bodies_this_batch"], 1)
        self.assertEqual(cycle["source_counts"]["content_extraction"]["status"], "partial_error")
        from bti.public import worker_public_status
        pub_worker = worker_public_status(self.db)
        self.assertNotIn("private_diagnostics", json.dumps(pub_worker))
        self.assertNotIn("SENSITIVE_TOKEN_SHOULD_NEVER_APPEAR_IN_DIAGNOSTIC", json.dumps(pub_worker))
        with connect(self.db) as conn:
            private_summary = json.loads(
                conn.execute("SELECT last_cycle_summary_json FROM worker_state WHERE id=1").fetchone()[0]
            )
        self.assertEqual(
            private_summary["content_extraction"]["private_diagnostics"][0]["error_class"],
            "RuntimeError",
        )

        def outer_commit_then_raise(db_path: object, *_args: object, **_kwargs: object) -> dict[str, object]:
            _store_result(
                self.db, archive_source(), self.fetch, [parsed_item("77000000555555", uid="UID-OUTER")],
                note="outer partial commit before raise",
            )
            raise RuntimeError("SENSITIVE_OUTER_WORKER_SECRET")

        with patch("bti.worker.run_content_volume_batch", side_effect=outer_commit_then_raise), patch("bti.worker.build_drafts"):
            outer_cycle = run_cycle(
                self.db, sources_manifest, now=current, backup_dir=None,
                policy_checker_override=lambda _u: (True, "test_allow"),
                content_fetcher_override=archive_fetcher,
            )
        self.assertEqual(outer_cycle["status"], "degraded")
        self.assertIn("content_volume", outer_cycle["source_counts"]["safe_failure_phases"])
        self.assertEqual(outer_cycle["source_counts"]["content_extraction"]["new_qualifying_bodies_this_batch"], 1)
        self.assertEqual(outer_cycle["source_counts"]["content_extraction"]["status"], "partial_error")
        self.assertNotIn("SENSITIVE_OUTER_WORKER_SECRET", json.dumps(worker_public_status(self.db)))
        self.assertNotIn("SENSITIVE_OUTER_WORKER_SECRET", json.dumps(health(self.db)))

    def test_snapshot_http_500_without_retry_after_does_not_lock_provider_and_rejects_on_second_failure(self) -> None:
        current = datetime(2026, 10, 21, 7, 0, tzinfo=timezone.utc)
        manifest = content_manifest()
        _content_state(self.db, manifest, current)
        corrupt_id = "13175903125178"
        valid_id = "320178370674002"
        with connect(self.db) as conn:
            conn.execute(
                """INSERT INTO content_archive_candidates(
                       post_id,canonical_url,original_url,snapshot_timestamp,archive_url,status,attempt_count,discovered_at,updated_at
                   ) VALUES
                   (?,?,?,?,?,'cooldown',1,'2026-10-19T11:00:00Z','2026-10-20T11:11:00Z'),
                   (?,?,?,?,?,'pending',0,'2026-10-20T11:00:00Z','2026-10-20T11:00:00Z')""",
                (
                    corrupt_id, f"https://www.binance.com/en/square/post/{corrupt_id}",
                    f"https://www.binance.com/en/square/post/{corrupt_id}", "20260704181012",
                    f"https://web.archive.org/web/20260704181012id_/https://www.binance.com/en/square/post/{corrupt_id}",
                    valid_id, f"https://www.binance.com/en/square/post/{valid_id}",
                    f"https://www.binance.com/en/square/post/{valid_id}", "20260601100930",
                    f"https://web.archive.org/web/20260601100930id_/https://www.binance.com/en/square/post/{valid_id}",
                ),
            )
            conn.commit()

        def mixed_fetcher(url: str, *, coordinator: FetchCoordinator, **_kwargs: object) -> FetchResult:
            coordinator.before_request("web.archive.org", url)
            if corrupt_id in url:
                raise AccessStopped("Archive stopped at HTTP 500", status_code=500)
            row = {
                "id": valid_id,
                "webLink": f"https://www.binance.com/en/square/post/{valid_id}",
                "bodyTextOnly": (
                    "Substantive market structure analysis with explicit risk management, stop loss invalidation, "
                    "and position sizing discipline. Conditional scenarios are evaluated against liquidity levels "
                    "and volatility regimes before any exposure is considered."
                ),
                "firstReleaseTime": 1748772570000,
                "squareUid": f"UID-{valid_id}",
                "username": "valid-trader",
                "displayName": "Valid Trader",
            }
            body = ('<script id="__APP_DATA" type="application/json">' + json.dumps({"post": row}) + "</script>").encode("utf-8")
            return FetchResult(url, 200, "text/html", body, 10, "2026-10-21T07:00:00Z")

        consumed = consume_archive_candidates(
            self.db, manifest, artifact_root=self.root / "artifacts",
            coordinator=FetchCoordinator(concurrency=1, minimum_spacing_seconds=1.0),
            current=current, max_fetch=2, fetcher=mixed_fetcher,
        )
        self.assertEqual(consumed["accepted_count"], 1)
        self.assertEqual(consumed["rejected"], {"archive_snapshot_http_500": 1})
        self.assertIsNone(_archive_cooldown(self.db, current))
        with connect(self.db) as conn:
            corrupt_row = conn.execute(
                "SELECT status,last_outcome,attempt_count FROM content_archive_candidates WHERE post_id=?",
                (corrupt_id,),
            ).fetchone()
        self.assertEqual(tuple(corrupt_row), ("rejected", "archive_snapshot_http_500", 2))
        from bti.content_volume import _quality_rejection
        zh_reason, _ = _quality_rejection(
            "币安新一期Megadrop参与教程：注册币安填写邀请码获永久返佣：10089328，一步步完成Web3任务并注意风险控制. " * 6
        )
        self.assertEqual(zh_reason, "promotional_or_referral")
        vi_reason, _ = _quality_rejection(
            "Hướng dẫn đăng ký tài khoản mới và nhập mã giới thiệu để nhận hoàn phí giao dịch trọn đời cho thành viên mới. " * 4
        )
        self.assertEqual(vi_reason, "promotional_or_referral")


if __name__ == "__main__":
    unittest.main()

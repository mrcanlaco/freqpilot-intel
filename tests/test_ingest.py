from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bti.db import connect
from bti.ingest import AccessStopped, FetchResult, ingest, structured_text


def square_response(title: str = "BTC thesis needs verification", views: int = 120) -> FetchResult:
    payload = {
        "code": "000000",
        "data": {
            "vos": [{
                "id": "synthetic-post-1",
                "title": title,
                "subTitle": "Synthetic fixture for deterministic integration testing only.",
                "authorName": "Synthetic Analyst",
                "username": "synthetic_analyst",
                "authorRole": 0,
                "sourceType": 0,
                "authorIsVerified": False,
                "authorVerificationType": 0,
                "webLink": "https://www.binance.com/en/square/post/synthetic-post-1",
                "date": 1_700_000_000,
                "language": "en",
                "viewCount": views,
                "likeCount": 12,
                "commentCount": 3,
                "shareCount": 1,
                "replyCount": 3,
                "portfolioPerformance": None,
            }]
        },
    }
    body = json.dumps(payload).encode()
    return FetchResult(
        url="https://www.binance.com/bapi/test",
        status=200,
        content_type="application/json",
        body=body,
        latency_ms=4,
        fetched_at="2026-10-18T00:00:00Z",
    )


class IngestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.db = root / "test.sqlite3"
        self.manifest = root / "sources.json"
        self.manifest.write_text(json.dumps({
            "sources": [{
                "slug": "synthetic-square-feed",
                "name": "Synthetic test fixture",
                "url": "https://www.binance.com/en/square/trending",
                "fetch_url": "https://www.binance.com/bapi/test",
                "source_type": "synthetic_test_fixture",
                "adapter": "square_feed",
                "access_level": "public",
                "distribution": "public_shareable",
                "expected_pillar": "engagement",
                "max_items": 1,
                "exclude_official": True,
                "delay_after_seconds": 0,
            }]
        }), encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_structured_text_extracts_nested_body(self) -> None:
        body = json.dumps({"node": "root", "child": [{"node": "text", "text": "Alpha"}, {"child": [{"text": "Beta"}]}]})
        self.assertEqual(structured_text(body), "Alpha Beta")

    def test_ingest_dedupes_and_versions_content(self) -> None:
        with patch("bti.ingest.fetch_public", return_value=square_response()):
            first = ingest(self.db, self.manifest)
            second = ingest(self.db, self.manifest)
        self.assertEqual(first["new_contents"], 1)
        self.assertEqual(second["duplicates"], 1)
        with patch("bti.ingest.fetch_public", return_value=square_response("BTC thesis changed — still a test", 150)):
            third = ingest(self.db, self.manifest)
        self.assertEqual(third["new_contents"], 0)
        self.assertEqual(third["new_versions"], 1)
        with connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM contents").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM content_versions").fetchone()[0], 2)
            levels = {row[0] for row in conn.execute("SELECT verification_level FROM evidence WHERE pillar='engagement'")}
            self.assertEqual(levels, {"platform"})

    def test_access_control_stop_is_recorded_without_content(self) -> None:
        with patch("bti.ingest.fetch_public", side_effect=AccessStopped("HTTP 429 stop")):
            result = ingest(self.db, self.manifest)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["errors"], 1)
        with connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT capability_status FROM sources").fetchone()[0], "blocked")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM contents").fetchone()[0], 0)

    def test_performance_language_is_self_claim_not_platform(self) -> None:
        fixture = square_response("My ROI profit claim needs independent verification")
        with patch("bti.ingest.fetch_public", return_value=fixture):
            ingest(self.db, self.manifest)
        with connect(self.db) as conn:
            row = conn.execute("SELECT pillar,kind,verification_level FROM evidence WHERE pillar='performance'").fetchone()
            self.assertEqual(tuple(row), ("performance", "performance_language", "self_claim"))


if __name__ == "__main__":
    unittest.main()

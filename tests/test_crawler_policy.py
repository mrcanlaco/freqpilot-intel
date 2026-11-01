from __future__ import annotations

import json
import tempfile
import unittest
import urllib.error
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from bti.crawler_policy import RobotsPolicy, prepare_crawler_cycle, public_crawler_status
from bti.db import connect
from bti.ingest import FetchCoordinator, FetchResult, USER_AGENT, _NoRedirect, ingest, ingest_source_configs
from bti.public import health, sources
from bti.worker import run_cycle

ROOT = Path(__file__).resolve().parents[1]
TEST_TMP = ROOT / "runtime" / "test-tmp"
DEFAULT_MANIFEST = ROOT / "tests" / "fixtures" / "public_sources.json"

ROBOTS = b"""User-agent: *
Allow: /
Disallow: */bapi/
Disallow: */api/
Disallow: */fapi/
Allow: */bapi/fe/
Allow: */sitemap_output/
"""

INDEX_URL = "https://www.binance.com/sitemap_output/domain=www.binance.com/sitemap_Square_index.xml"
CHILD_URL = "https://www.binance.com/sitemap_output/domain=www.binance.com/sitemap_Square_en_1.xml"
INDEX_XML = f"""<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>{CHILD_URL}</loc><lastmod>2026-10-17</lastmod></sitemap>
</sitemapindex>
""".encode()
CHILD_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://www.binance.com/en/square/post/100</loc><lastmod>2026-10-17</lastmod></url>
  <url><loc>https://www.binance.com/en/square/post/100</loc><lastmod>2026-10-17</lastmod></url>
  <url><loc>https://www.binance.com/en/square/post/200</loc></url>
  <url><loc>https://www.binance.com/vi/square/post/300</loc></url>
  <url><loc>https://www.binance.com/en/square/profile/creator</loc></url>
  <url><loc>https://www.binance.com/en/square/post/400?ref=sitemap</loc></url>
</urlset>
"""


def raw_source(slug: str = "raw-source") -> dict[str, object]:
    return {
        "slug": slug,
        "name": f"Raw source {slug}",
        "url": f"https://www.binance.com/en/square/profile/{slug}",
        "fetch_url": f"https://www.binance.com/bapi/composite/v3/friendly/{slug}",
        "source_type": "binance_square_creator_posts",
        "adapter": "html",
        "access_level": "public",
        "distribution": "public_shareable",
        "expected_pillar": "engagement",
        "schedule": "hourly",
    }


class FixtureFetcher:
    def __init__(self, fetched_at: str = "2026-10-18T00:00:00Z") -> None:
        self.fetched_at = fetched_at
        self.calls: list[str] = []

    def __call__(self, url: str, **kwargs: object) -> FetchResult:
        coordinator = kwargs.get("coordinator")
        if isinstance(coordinator, FetchCoordinator):
            coordinator.before_request(urllib.parse.urlparse(url).hostname or "", url)
        self.calls.append(url)
        body, content_type = {
            "https://www.binance.com/robots.txt": (ROBOTS, "text/plain"),
            INDEX_URL: (INDEX_XML, "application/xml"),
            CHILD_URL: (CHILD_XML, "application/xml"),
        }[url]
        return FetchResult(url, 200, content_type, body, 1, self.fetched_at)


class CrawlerPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        TEST_TMP.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=TEST_TMP)
        self.db = Path(self.temp.name) / "policy.sqlite3"
        self.manifest = json.loads(DEFAULT_MANIFEST.read_text(encoding="utf-8"))
        self.config = self.manifest["crawler_policy"]
        self.now = datetime(2026, 10, 18, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def prepare(self, *, now: datetime | None = None, fetcher: FixtureFetcher | None = None) -> tuple[FetchCoordinator, FixtureFetcher, dict[str, object]]:
        coordinator = FetchCoordinator(minimum_spacing_seconds=1.0)
        fixture = fetcher or FixtureFetcher()
        with patch("bti.ingest.time.sleep", return_value=None):
            status = prepare_crawler_cycle(
                self.db, self.config, coordinator, now=now or self.now, fetcher=fixture,
            )
        return coordinator, fixture, status

    def test_most_specific_robots_matching_and_default_manifest_policy(self) -> None:
        policy = RobotsPolicy.parse(
            ROBOTS,
            robots_url="https://www.binance.com/robots.txt",
            user_agent=USER_AGENT,
            fetched_at="2026-10-18T00:00:00Z",
        )
        self.assertEqual(policy.check("https://www.binance.com/bapi/composite/v3/item"), (False, "robots_disallow"))
        self.assertEqual(policy.check("https://www.binance.com/en/api/item"), (False, "robots_disallow"))
        self.assertEqual(policy.check("https://www.binance.com/fapi/item"), (False, "robots_disallow"))
        self.assertEqual(policy.check("https://www.binance.com/bapi/fe/content"), (True, "robots_allow_specific"))
        self.assertEqual(policy.check(CHILD_URL), (True, "robots_allow_specific"))
        self.assertEqual(policy.check("https://www.binance.com/en/square/post/100"), (True, "robots_allow_specific"))
        self.assertTrue(self.config["enabled"])
        for source in self.manifest["sources"]:
            if "/bapi/" in source["fetch_url"]:
                self.assertFalse(policy.check(source["fetch_url"])[0], source["slug"])

        specific = RobotsPolicy.parse(
            b"User-agent: *\nAllow: /\n\nUser-agent: BinanceTraderIntelligence\nDisallow: /\n",
            robots_url="https://www.binance.com/robots.txt",
            user_agent=USER_AGENT,
            fetched_at="2026-10-18T00:00:00Z",
        )
        self.assertEqual(
            specific.check("https://www.binance.com/sitemap_output/example.xml"),
            (False, "robots_disallow"),
        )

    def test_allowed_sitemap_is_deduped_into_unknown_candidate_queue(self) -> None:
        _coordinator, fetcher, status = self.prepare()
        self.assertEqual(fetcher.calls, ["https://www.binance.com/robots.txt", INDEX_URL, CHILD_URL])
        self.assertEqual(status["policy"]["status"], "enforced")
        self.assertEqual(status["policy"]["raw_api_collection"], "policy_paused")
        self.assertEqual(status["discovery"]["status"], "operational")
        self.assertEqual(status["discovery"]["observed_url_count"], 6)
        self.assertEqual(status["discovery"]["candidate_count"], 2)
        self.assertIn("không phải bài đã xác minh", status["discovery"]["definition"])
        with connect(self.db) as conn:
            rows = conn.execute(
                """SELECT candidate.*,observation.source_url,observation.source_sha256,observation.lastmod
                   FROM discovery_candidates candidate JOIN discovery_observations observation
                     ON observation.candidate_id=candidate.id ORDER BY candidate.canonical_url"""
            ).fetchall()
        self.assertEqual([row["canonical_url"] for row in rows], [
            "https://www.binance.com/en/square/post/100",
            "https://www.binance.com/en/square/post/200",
        ])
        self.assertTrue(all(row["original_published_at"] is None and row["author"] is None for row in rows))
        self.assertTrue(all(row["quality_status"] == "unknown" and row["queue_status"] == "pending" for row in rows))
        self.assertTrue(all(row["source_url"] == CHILD_URL and row["source_sha256"] for row in rows))

    def test_policy_denies_raw_all_lanes_after_restart_without_losing_snapshot(self) -> None:
        source = raw_source()
        body = b"<html><title>Retained</title><body>" + b"retained public evidence " * 12 + b"</body></html>"
        retained = FetchResult(source["fetch_url"], 200, "text/html", body, 1, "2026-10-17T00:00:00Z")
        with patch("bti.ingest.fetch_public", return_value=retained):
            ingest_source_configs(self.db, [source], fetch_coordinator=FetchCoordinator(
                policy_checker=lambda _url: (True, "explicit_fixture_allow")
            ))
        with connect(self.db) as conn:
            before = conn.execute("SELECT COUNT(*) FROM contents").fetchone()[0]
            retained_at = conn.execute("SELECT last_success_at FROM sources WHERE slug=?", (source["slug"],)).fetchone()[0]

        coordinator, fixture, _status = self.prepare()
        with patch("bti.ingest.urllib.request.urlopen") as direct, patch("bti.ingest.urllib.request.build_opener") as opener:
            denied = ingest_source_configs(self.db, [source], fetch_coordinator=coordinator)
        direct.assert_not_called()
        opener.assert_not_called()
        self.assertEqual(denied["events"][0]["skip_reason"], "crawler_policy")

        restarted, _fixture, cached = self.prepare(now=self.now + timedelta(minutes=30), fetcher=fixture)
        self.assertEqual(fixture.calls, ["https://www.binance.com/robots.txt", INDEX_URL, CHILD_URL])
        self.assertEqual(cached["discovery"]["status"], "not_due")
        lanes = [raw_source("dynamic-profile"), raw_source("historical-seed"), raw_source("new-source")]
        with patch("bti.ingest.urllib.request.urlopen") as direct, patch("bti.ingest.urllib.request.build_opener") as opener:
            result = ingest_source_configs(self.db, lanes, fetch_coordinator=restarted)
        direct.assert_not_called()
        opener.assert_not_called()
        self.assertEqual([event.get("skip_reason") for event in result["events"]], ["crawler_policy"] * 3)
        with connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM contents").fetchone()[0], before)
            self.assertEqual(
                conn.execute("SELECT last_success_at FROM sources WHERE slug=?", (source["slug"],)).fetchone()[0],
                retained_at,
            )
        projection = next(item for item in sources(self.db) if item["slug"] == source["slug"])
        self.assertEqual(projection["capability_status"], "policy_paused")
        self.assertEqual(projection["last_success_at"], retained_at)

    def test_default_manifest_cycle_only_requests_policy_permitted_resources(self) -> None:
        fixture = FixtureFetcher()
        with patch("bti.ingest.time.sleep", return_value=None), patch("bti.worker.build_drafts"), patch(
            "bti.ingest.urllib.request.urlopen"
        ) as direct, patch("bti.ingest.urllib.request.build_opener") as opener:
            result = run_cycle(
                self.db,
                DEFAULT_MANIFEST,
                now=self.now,
                crawler_fetcher_override=fixture,
            )
        direct.assert_not_called()
        opener.assert_not_called()
        self.assertEqual(fixture.calls, ["https://www.binance.com/robots.txt", INDEX_URL, CHILD_URL])
        self.assertEqual(result["status"], "success")
        summary = result["source_counts"]
        self.assertEqual(summary["attempted_source_count"], 0)
        self.assertGreater(summary["policy_paused_source_count"], 0)
        self.assertEqual(summary["historical_backfill"]["attempted"], 0)
        self.assertGreater(summary["historical_backfill"]["policy_paused"], 0)
        self.assertEqual(summary["crawler"]["discovery"]["candidate_count"], 2)

    def test_manual_default_manifest_ingest_uses_the_same_policy_gate(self) -> None:
        fixture = FixtureFetcher()
        with patch("bti.ingest.time.sleep", return_value=None), patch(
            "bti.ingest.urllib.request.urlopen"
        ) as direct, patch("bti.ingest.urllib.request.build_opener") as opener:
            result = ingest(
                self.db,
                DEFAULT_MANIFEST,
                only=["square-trending-creators"],
                crawler_fetcher_override=fixture,
            )
        direct.assert_not_called()
        opener.assert_not_called()
        self.assertEqual(fixture.calls, ["https://www.binance.com/robots.txt", INDEX_URL, CHILD_URL])
        self.assertEqual(result["events"][0]["skip_reason"], "crawler_policy")
        self.assertEqual(result["crawler"]["policy"]["raw_api_collection"], "policy_paused")

    def test_policy_refresh_failure_fails_closed_and_retains_discovery(self) -> None:
        self.prepare()

        def failed_fetch(url: str, **kwargs: object) -> FetchResult:
            coordinator = kwargs["coordinator"]
            assert isinstance(coordinator, FetchCoordinator)
            coordinator.before_request("www.binance.com", url)
            raise OSError("synthetic refresh failure private detail")

        coordinator = FetchCoordinator(minimum_spacing_seconds=1.0)
        status = prepare_crawler_cycle(
            self.db, self.config, coordinator, now=self.now + timedelta(hours=2), fetcher=failed_fetch,
        )
        self.assertEqual(status["policy"]["status"], "refresh_failed")
        self.assertEqual(status["policy"]["raw_api_collection"], "fail_closed")
        self.assertEqual(status["discovery"]["status"], "policy_paused")
        self.assertEqual(status["discovery"]["candidate_count"], 2)
        self.assertNotIn("synthetic refresh failure", json.dumps(status))
        with patch("bti.ingest.urllib.request.urlopen") as direct, patch("bti.ingest.urllib.request.build_opener") as opener:
            result = ingest_source_configs(self.db, [raw_source("after-failure")], fetch_coordinator=coordinator)
        direct.assert_not_called()
        opener.assert_not_called()
        self.assertEqual(result["events"][0]["policy_reason"], "policy_refresh_failed_fail_closed")

    def test_policy_controlled_fetch_does_not_follow_redirect(self) -> None:
        initial = INDEX_URL
        target = "https://www.binance.com/bapi/composite/v3/restricted"
        opened: list[str] = []

        class RedirectingOpener:
            def open(self, request: object, timeout: int) -> object:
                url = request.full_url  # type: ignore[attr-defined]
                opened.append(url)
                raise urllib.error.HTTPError(url, 302, "Found", {"Location": target}, None)

        coordinator = FetchCoordinator(policy_checker=lambda url: (url == initial, "robots_disallow"))
        with patch("bti.ingest.urllib.request.build_opener", return_value=RedirectingOpener()) as builder, patch(
            "bti.ingest.urllib.request.urlopen"
        ) as direct:
            with self.assertRaises(urllib.error.HTTPError):
                from bti.ingest import fetch_public

                fetch_public(initial, retries=0, coordinator=coordinator)
        direct.assert_not_called()
        self.assertEqual(opened, [initial])
        self.assertTrue(any(isinstance(handler, _NoRedirect) for handler in builder.call_args.args))

    def test_public_health_exposes_only_honest_policy_and_discovery_status(self) -> None:
        self.prepare()
        payload = health(self.db)
        crawler = payload["crawler"]
        self.assertEqual(crawler["policy"]["raw_api_collection"], "policy_paused")
        self.assertEqual(crawler["discovery"]["candidate_count"], 2)
        serialized = json.dumps(crawler)
        self.assertNotIn("body_text", serialized)
        self.assertNotIn("error_class", serialized)
        self.assertNotIn("/bapi/", serialized)


if __name__ == "__main__":
    unittest.main()

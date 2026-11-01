from __future__ import annotations

import threading
import time
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

import bti.ingest as ingest_module
from bti.db import connect
from bti.ingest import AccessStopped, FetchCoordinator, FetchResult, fetch_public, ingest_source_configs


TEST_TMP = Path(__file__).resolve().parents[1] / "runtime" / "test-tmp"


def source(slug: str) -> dict[str, object]:
    return {
        "slug": slug,
        "name": f"Source {slug}",
        "url": f"https://www.binance.com/en/square/profile/{slug}",
        "fetch_url": f"https://www.binance.com/bapi/{slug}",
        "source_type": "binance_square_profile_html",
        "adapter": "html",
        "access_level": "public",
        "distribution": "public_shareable",
        "expected_pillar": "engagement",
        "delay_after_seconds": 0,
    }


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class FetchCoordinatorTests(unittest.TestCase):
    def test_defaults_hard_max_and_shared_host_spacing(self) -> None:
        coordinator = FetchCoordinator()
        self.assertEqual(coordinator.concurrency, 2)
        self.assertEqual(coordinator.minimum_spacing_seconds, 1.5)
        with self.assertRaises(ValueError):
            FetchCoordinator(concurrency=4)
        with self.assertRaises(ValueError):
            FetchCoordinator(minimum_spacing_seconds=0.9)

        clock = FakeClock()
        with patch("bti.ingest.time.monotonic", side_effect=clock.monotonic), patch(
            "bti.ingest.time.sleep", side_effect=clock.sleep
        ):
            coordinator = FetchCoordinator(concurrency=3, minimum_spacing_seconds=1.5)
            coordinator.before_request("www.binance.com")
            coordinator.before_request("www.binance.com")
        self.assertEqual(clock.sleeps, [1.5])

    def test_429_trips_shared_circuit_and_prevents_probe(self) -> None:
        url = "https://www.binance.com/bapi/rate-test"
        error = urllib.error.HTTPError(url, 429, "rate", {"Retry-After": "120"}, None)
        coordinator = FetchCoordinator(minimum_spacing_seconds=1.0)
        with patch("bti.ingest.urllib.request.urlopen", side_effect=error) as request:
            with self.assertRaises(AccessStopped):
                fetch_public(url, retries=0, coordinator=coordinator)
            with self.assertRaises(AccessStopped):
                fetch_public(url, retries=0, coordinator=coordinator)
        self.assertEqual(request.call_count, 1)


class ParallelFetchSerialWriteTests(unittest.TestCase):
    def setUp(self) -> None:
        TEST_TMP.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=TEST_TMP)
        self.db = Path(self.temp.name) / "concurrency.sqlite3"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_network_fetches_are_bounded_but_writes_are_ordered_and_serial(self) -> None:
        active = 0
        maximum = 0
        guard = threading.Lock()
        completed: list[str] = []

        def fake_fetch(url: str, **kwargs: object) -> FetchResult:
            nonlocal active, maximum
            slug = url.rsplit("/", 1)[-1]
            with guard:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.05 if slug == "slow" else 0.005)
            with guard:
                active -= 1
                completed.append(slug)
            body = (
                f"<html><title>{slug}</title><body>"
                + ("substantive public research evidence " * 5)
                + "</body></html>"
            ).encode("utf-8")
            return FetchResult(url, 200, "text/html", body, 1, "2026-10-18T00:00:00Z")

        write_threads: list[str] = []
        original_store = ingest_module._store_item

        def tracked_store(*args: object, **kwargs: object) -> tuple[int, int, int]:
            write_threads.append(threading.current_thread().name)
            return original_store(*args, **kwargs)

        configs = [source("slow"), source("fast")]
        with patch("bti.ingest.fetch_public", side_effect=fake_fetch), patch(
            "bti.ingest._store_item", side_effect=tracked_store
        ):
            result = ingest_source_configs(self.db, configs, concurrency=2)

        self.assertEqual(maximum, 2)
        self.assertEqual(completed[0], "fast")
        self.assertEqual(result["fetch_success"], 2)
        self.assertTrue(write_threads)
        self.assertEqual(set(write_threads), {threading.current_thread().name})
        with connect(self.db) as conn:
            stored = [row[0] for row in conn.execute(
                "SELECT source.slug FROM contents content JOIN sources source ON source.id=content.source_id ORDER BY content.id"
            )]
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            foreign_keys = conn.execute("PRAGMA foreign_key_check").fetchall()
        self.assertEqual(stored, ["slow", "fast"])
        self.assertEqual(integrity, "ok")
        self.assertEqual(foreign_keys, [])


if __name__ == "__main__":
    unittest.main()

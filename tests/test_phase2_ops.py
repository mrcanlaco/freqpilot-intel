from __future__ import annotations

import importlib.util
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from bti.backup import apply_retention, create_backup
from bti.db import connect, init_db, utc_now
from bti.ingest import AccessStopped, FetchResult, ParsedItem, _upsert_entity, ingest_source_configs, seed_source_configs
from bti.public import public_export, worker_public_status
from bti.worker import (
    ProcessLock, WorkerAlreadyRunning, ensure_source_schedules, run_cycle,
    update_source_schedule, validate_interval,
)

TEST_TMP = Path(__file__).resolve().parents[1] / "runtime" / "test-tmp"


def source(slug: str = "source", *, schedule: str = "hourly") -> dict[str, object]:
    return {
        "slug": slug, "name": f"Source {slug}",
        "url": f"https://www.binance.com/en/square/profile/{slug}",
        "fetch_url": f"https://www.binance.com/bapi/{slug}",
        "source_type": "binance_square_profile_html", "adapter": "html",
        "access_level": "public", "distribution": "public_shareable",
        "expected_pillar": "engagement", "schedule": schedule, "delay_after_seconds": 0,
    }


def allow_fixture_policy(_url: str) -> tuple[bool, str]:
    return True, "explicit_fixture_allow"


class Phase2OpsTests(unittest.TestCase):
    def setUp(self) -> None:
        TEST_TMP.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=TEST_TMP)
        self.root = Path(self.temp.name)
        self.db = self.root / "state" / "bti.sqlite3"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_interval_and_process_lock_are_bounded(self) -> None:
        self.assertEqual(validate_interval(3600), 3600)
        with self.assertRaises(ValueError):
            validate_interval(5)
        self.assertEqual(validate_interval(5, allow_short=True), 5)
        lock_path = self.db.parent / "worker.lock"
        with ProcessLock(lock_path):
            with self.assertRaises(WorkerAlreadyRunning):
                ProcessLock(lock_path).acquire()

    def test_cadence_429_and_retry_after_cooldown(self) -> None:
        now = datetime(2026, 10, 18, tzinfo=timezone.utc)
        ensure_source_schedules(self.db, [source()], now=now)
        update_source_schedule(
            self.db, "source", {"outcome": "blocked", "status_code": 429, "retry_after_seconds": 30000}, now=now,
        )
        with connect(self.db) as conn:
            row = conn.execute("SELECT * FROM source_schedule").fetchone()
        self.assertEqual(row["consecutive_failures"], 1)
        self.assertEqual(row["next_due_at"], "2026-10-18T08:20:00Z")
        self.assertEqual(row["cooldown_until"], row["next_due_at"])

    def test_host_cooldown_survives_cycle_restart_and_gates_new_sources(self) -> None:
        manifest = self.root / "sources.json"
        first = source("first")
        second = source("new-profile")
        manifest.write_text(json.dumps({"sources": [first]}), encoding="utf-8")
        started = datetime(2026, 10, 18, tzinfo=timezone.utc)
        with patch(
            "bti.worker.ingest_source_configs",
            wraps=ingest_source_configs,
        ), patch(
            "bti.ingest.fetch_public",
            side_effect=AccessStopped("rate limited", status_code=429, retry_after_seconds=60),
        ) as blocked, patch("bti.worker.build_drafts"):
            first_cycle = run_cycle(self.db, manifest, now=started, policy_checker_override=allow_fixture_policy)
        self.assertEqual(first_cycle["source_counts"]["blocked_count"], 1)
        self.assertEqual(blocked.call_count, 1)

        manifest.write_text(json.dumps({"sources": [second]}), encoding="utf-8")
        with patch("bti.ingest.fetch_public") as no_request, patch("bti.worker.build_drafts"):
            second_cycle = run_cycle(
                self.db, manifest, now=started + timedelta(hours=1),
                policy_checker_override=allow_fixture_policy,
            )
        no_request.assert_not_called()
        self.assertEqual(second_cycle["source_counts"]["skipped_count"], 1)

        body = b"<html><title>Profile</title><body>" + (b"public research material " * 8) + b"</body></html>"
        resumed_result = FetchResult(second["fetch_url"], 200, "text/html", body, 1, "2026-10-18T06:00:01Z")
        with patch("bti.ingest.fetch_public", return_value=resumed_result) as resumed, patch("bti.worker.build_drafts"):
            third_cycle = run_cycle(
                self.db, manifest, now=started + timedelta(hours=6, seconds=1),
                policy_checker_override=allow_fixture_policy,
            )
        self.assertEqual(resumed.call_count, 1)
        self.assertEqual(third_cycle["source_counts"]["success_count"], 1)

    def test_revoked_source_and_owner_are_not_reapproved_or_fetched(self) -> None:
        config = source("revoked")
        config["entity"] = {
            "slug": "revoked-owner", "source_uid": "REVOKED-UID",
            "display_name": "SENSITIVE_MARKER", "username": "private",
            "category": "official_trader_awardee",
        }
        seed_source_configs(self.db, [config])
        now = utc_now()
        with connect(self.db) as conn:
            conn.execute("UPDATE sources SET access_level='private',distribution='internal_only' WHERE slug='revoked'")
            conn.execute(
                """INSERT INTO entities(slug,display_name,category,source_uid,username,identity_status,
                       public_shareable,created_at,updated_at) VALUES(?,?,?,?,?,'revoked',0,?,?)""",
                ("revoked-owner", "SENSITIVE_MARKER", "official_trader_awardee", "REVOKED-UID", "private", now, now),
            )
            conn.commit()
        with patch("bti.ingest.fetch_public") as fetch:
            result = ingest_source_configs(self.db, [config])
        fetch.assert_not_called()
        self.assertEqual(result["skipped"], 1)
        with connect(self.db) as conn:
            persisted = conn.execute("SELECT access_level,distribution FROM sources WHERE slug='revoked'").fetchone()
        self.assertEqual(tuple(persisted), ("private", "internal_only"))
        self.assertNotIn("SENSITIVE_MARKER", json.dumps(public_export(self.db)))

    def test_upsert_entity_never_raises_persisted_visibility(self) -> None:
        now = utc_now()
        item = ParsedItem(
            external_id="post", canonical_url="https://www.binance.com/en/square/post/1",
            title="Public manifest title", text="public body", author="Manifest name", username="manifest",
            published_at=now, language="en", metadata={"source_uid": "REVOKED-UID"},
            entity_category="official_trader_awardee",
        )
        manifest_source = source("public-manifest")
        manifest_source["entity"] = {
            "slug": "revoked-owner", "source_uid": "REVOKED-UID", "display_name": "Manifest name",
            "username": "manifest", "category": "official_trader_awardee",
            "identity_status": "official_award_payload_uid",
        }
        init_db(self.db)
        with connect(self.db) as conn:
            conn.execute(
                """INSERT INTO entities(slug,display_name,category,source_uid,username,identity_status,
                       public_shareable,created_at,updated_at) VALUES(?,?,?,?,?,'revoked',0,?,?)""",
                ("revoked-owner", "Private name", "official_trader_awardee", "REVOKED-UID", "private", now, now),
            )
            entity_id = _upsert_entity(conn, item, manifest_source, now)
            row = conn.execute(
                "SELECT public_shareable,identity_status FROM entities WHERE id=?", (entity_id,)
            ).fetchone()
            conn.commit()
        self.assertEqual(tuple(row), (0, "revoked"))

    def test_revoke_after_preflight_is_rechecked_before_atomic_storage(self) -> None:
        config = source("race")
        config["entity"] = {
            "slug": "race-owner", "source_uid": "RACE-UID", "display_name": "SENSITIVE_RACE_MARKER",
            "username": "race", "category": "official_trader_awardee",
        }
        seed_source_configs(self.db, [config])
        now = utc_now()
        with connect(self.db) as conn:
            conn.execute(
                """INSERT INTO entities(slug,display_name,category,source_uid,username,identity_status,
                       public_shareable,created_at,updated_at) VALUES(?,?,?,?,?,'source_profile_only',1,?,?)""",
                ("race-owner", "SENSITIVE_RACE_MARKER", "official_trader_awardee", "RACE-UID", "race", now, now),
            )
            conn.commit()

        def fetched_then_revoked(*args: object, **kwargs: object) -> FetchResult:
            with connect(self.db) as revoke:
                revoke.execute(
                    "UPDATE entities SET public_shareable=0,identity_status='revoked' WHERE source_uid='RACE-UID'"
                )
                revoke.commit()
            return FetchResult(
                config["fetch_url"], 200, "text/html", b"<html><title>SENSITIVE_RACE_MARKER</title><body>substantive public text</body></html>",
                1, utc_now(),
            )

        with patch("bti.ingest.fetch_public", side_effect=fetched_then_revoked):
            result = ingest_source_configs(self.db, [config])
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["new_contents"], 0)
        with connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM contents").fetchone()[0], 0)
        self.assertNotIn("SENSITIVE_RACE_MARKER", json.dumps(public_export(self.db)))

    def test_public_worker_status_excludes_private_schedules_and_stales_idle(self) -> None:
        public = source("public")
        private = source("private")
        ensure_source_schedules(self.db, [public, private])
        old = "2026-01-01T00:00:00Z"
        with connect(self.db) as conn:
            conn.execute("UPDATE sources SET access_level='private',distribution='internal_only' WHERE slug='private'")
            private_id = conn.execute("SELECT id FROM sources WHERE slug='private'").fetchone()[0]
            conn.execute("UPDATE source_schedule SET last_outcome='error',cooldown_until='2099-01-01T00:00:00Z' WHERE source_id=?", (private_id,))
            conn.execute(
                """UPDATE worker_state SET status='idle',heartbeat_at=?,interval_seconds=60,
                       last_cycle_summary_json=? WHERE id=1""",
                (old, json.dumps({"attempted_source_count": 999, "private_marker": "DO_NOT_EXPOSE"})),
            )
            conn.commit()
        payload = worker_public_status(self.db)
        self.assertEqual(payload["status"], "stale")
        self.assertEqual(payload["source_summary"]["cooldown"], 0)
        self.assertEqual(payload["source_summary"]["outage"], 0)
        self.assertNotIn("last_cycle_summary", payload)
        self.assertNotIn("DO_NOT_EXPOSE", json.dumps(payload))

    def test_failed_backup_records_retry_not_success_time(self) -> None:
        manifest = self.root / "sources.json"
        manifest.write_text(json.dumps({"sources": [source(schedule="disabled")]}), encoding="utf-8")
        with patch("bti.worker.create_backup", side_effect=OSError("synthetic lock")), patch("bti.worker.build_drafts"):
            result = run_cycle(
                self.db, manifest, now=datetime(2026, 10, 18, tzinfo=timezone.utc),
                backup_dir=self.root / "backups",
                policy_checker_override=allow_fixture_policy,
            )
        self.assertEqual(result["backup_status"], "failed")
        with connect(self.db) as conn:
            row = conn.execute("SELECT * FROM worker_state WHERE id=1").fetchone()
        self.assertIsNone(row["last_backup_at"])
        self.assertEqual(row["last_backup_status"], "failed")
        self.assertEqual(row["backup_next_attempt_at"], "2026-10-18T01:00:00Z")

    def test_windows_backup_closes_handles_and_retention_is_scoped(self) -> None:
        init_db(self.db)
        with connect(self.db) as conn:
            conn.execute("INSERT INTO schema_meta(key,value) VALUES('marker','windows-handle-test')"); conn.commit()
        backup_dir = self.db.parent / "backups"
        for day in range(9):
            create_backup(
                self.db, backup_dir, keep=7,
                now=datetime(2026, 10, 1 + day, tzinfo=timezone.utc),
            )
        unrelated = backup_dir / "keep-me.txt"; unrelated.write_text("safe", encoding="utf-8")
        apply_retention(backup_dir, keep=7)
        backups = sorted(backup_dir.glob("bti-backup-*.sqlite3"))
        self.assertEqual(len(backups), 7)
        moved = backups[-1].with_suffix(".moved")
        backups[-1].replace(moved)
        with closing(sqlite3.connect(moved)) as conn:
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(conn.execute("SELECT value FROM schema_meta WHERE key='marker'").fetchone()[0], "windows-handle-test")
        self.assertTrue(unrelated.is_file())

    def test_supervisor_worker_is_opt_in_and_uses_absolute_plan(self) -> None:
        module_path = Path(__file__).resolve().parents[1] / "ops" / "windows_supervisor.py"
        spec = importlib.util.spec_from_file_location("windows_supervisor_test", module_path)
        module = importlib.util.module_from_spec(spec); assert spec and spec.loader; spec.loader.exec_module(module)
        flag = self.root / "worker-enabled.json"
        self.assertFalse(module.worker_enabled(flag))
        flag.write_text('{"enabled":true}', encoding="utf-8")
        self.assertTrue(module.worker_enabled(flag))
        commands = module.build_commands(
            Path("C:/Python/python.exe"), self.db, self.root / "manifest.json",
            Path("C:/cloudflared.exe"), self.root / "cloudflared.yml", enable_worker=True,
        )
        self.assertEqual(set(commands), {"app", "tunnel", "worker"})
        worker = commands["worker"]
        self.assertIn(str(self.db.resolve()), worker)
        self.assertIn(str((self.db.parent / "backups").resolve()), worker)
        self.assertIn("3600", worker)

    def test_adaptive_pacing_self_healing_vietnamese_priority_and_safe_replication(self) -> None:
        from bti.content_volume import FetchCoordinator, _content_state, consume_archive_candidates
        from bti.scanner import (
            compute_adaptive_interval,
            replicate_scanner_instance,
            scanner_doctor,
            scanner_public_status,
            self_heal_database,
        )

        now = datetime(2026, 10, 21, 10, 0, tzinfo=timezone.utc)
        init_db(self.db)
        manifest_doc = {
            "baseline": {"started_at": "2026-10-18T00:00:00Z", "article_count": 19, "qualifying_post_ids": []},
            "minimum_text_chars": 200,
            "archive_source": {
                "slug": "wayback-square-cdx",
                "name": "Archive",
                "url": "https://web.archive.org/cdx/search/cdx?url=www.binance.com%2Fen%2Fsquare%2Fpost%2F%2A&output=json",
                "fetch_url": "https://web.archive.org/cdx/search/cdx?url=www.binance.com%2Fen%2Fsquare%2Fpost%2F%2A&output=json",
                "source_type": "binance_square_public_archive",
                "adapter": "wayback_app_data",
                "access_level": "public",
                "distribution": "public_shareable",
            },
            "cdx": {"cadence_seconds": 600, "max_rows": 500, "max_requests_per_cycle": 2, "include_vietnamese_locale": True},
            "profile_snapshots": [],
            "cached_details": [],
        }
        _content_state(self.db, manifest_doc, now)

        # 1. Empty queue -> discovery_patrol (1800s)
        idle_pacing = compute_adaptive_interval(self.db, now=now)
        self.assertEqual(idle_pacing["mode"], "discovery_patrol")
        self.assertEqual(idle_pacing["next_interval_seconds"], 1800)

        # 2. Insert 1 English candidate and 1 Vietnamese candidate + 1 expired transient cooldown + 1 access_stopped cooldown
        with connect(self.db) as conn:
            conn.execute(
                """INSERT INTO content_archive_candidates(
                       post_id,canonical_url,original_url,snapshot_timestamp,archive_url,status,discovered_at,updated_at
                   ) VALUES
                   ('91000001','https://www.binance.com/en/square/post/91000001','https://www.binance.com/en/square/post/91000001',
                    '20261020120000','https://web.archive.org/web/20261020120000id_/https://www.binance.com/en/square/post/91000001','pending','2026-10-21T09:00:00Z','2026-10-21T09:00:00Z'),
                   ('91000002','https://www.binance.com/en/square/post/91000002','https://www.binance.com/vi/square/post/91000002',
                    '20261019120000','https://web.archive.org/web/20261019120000id_/https://www.binance.com/vi/square/post/91000002','pending','2026-10-21T09:00:00Z','2026-10-21T09:00:00Z')"""
            )
            conn.execute(
                """INSERT INTO content_archive_candidates(
                       post_id,canonical_url,original_url,snapshot_timestamp,archive_url,status,next_attempt_at,last_outcome,discovered_at,updated_at
                   ) VALUES
                   ('91000003','https://www.binance.com/en/square/post/91000003','https://www.binance.com/en/square/post/91000003',
                    '20261018120000','https://web.archive.org/web/20261018120000id_/https://www.binance.com/en/square/post/91000003','cooldown','2026-10-21T08:00:00Z','TimeoutError','2026-10-21T07:00:00Z','2026-10-21T07:00:00Z'),
                   ('91000004','https://www.binance.com/en/square/post/91000004','https://www.binance.com/en/square/post/91000004',
                    '20261018110000','https://web.archive.org/web/20261018110000id_/https://www.binance.com/en/square/post/91000004','cooldown','2026-10-21T08:00:00Z','access_stopped','2026-10-21T07:00:00Z','2026-10-21T07:00:00Z')"""
            )
            conn.commit()

        heal = self_heal_database(self.db, now=now)
        self.assertEqual(heal["sqlite_quick_check"], "ok")
        self.assertEqual(heal["foreign_key_errors"], 0)
        # Vietnamese candidate triggers productive_fast (600s)
        vi_pacing = compute_adaptive_interval(self.db, now=now)
        self.assertEqual(vi_pacing["mode"], "productive_fast")
        self.assertEqual(vi_pacing["ready_vietnamese_candidates"], 1)

        # Verify consume_archive_candidates prioritizes /vi/square/post/91000002 BEFORE newer /en/square/post/91000001
        fetched_urls: list[str] = []

        def vi_first_fetcher(url: str, *, coordinator: FetchCoordinator, **_kwargs: object) -> FetchResult:
            coordinator.before_request("web.archive.org", url)
            fetched_urls.append(url)
            post_id = url.rsplit("/", 1)[-1]
            row = {
                "id": post_id,
                "webLink": f"https://www.binance.com/en/square/post/{post_id}",
                "bodyTextOnly": (
                    "Phân tích kịch bản thị trường tài sản số và phương pháp quản trị rủi ro chặt chẽ dành cho nhà đầu tư Việt Nam. "
                    "Trước khi vào lệnh cần xác định rõ vùng thanh khoản, ngưỡng hỗ trợ, điểm vô hiệu hóa luận điểm và giới hạn đòn bẩy hợp lý. "
                    "Mọi quan sát dữ liệu lịch sử đều phải đối chiếu độc lập, tuyệt đối không giao dịch cảm tính hoặc đuổi theo tâm lý đám đông."
                ),
                "firstReleaseTime": 1760832000000,
                "squareUid": f"UID-{post_id}",
                "username": f"vn-author-{post_id}",
                "displayName": f"VN Author {post_id}",
            }
            body = ('<script id="__APP_DATA" type="application/json">' + json.dumps({"post": row}) + "</script>").encode("utf-8")
            return FetchResult(url, 200, "text/html", body, 5, "2026-10-21T10:00:00Z")

        consumed = consume_archive_candidates(
            self.db, manifest_doc, artifact_root=self.root / "artifacts",
            coordinator=FetchCoordinator(concurrency=1, minimum_spacing_seconds=1.0),
            current=now, max_fetch=1, fetcher=vi_first_fetcher,
        )
        self.assertEqual(consumed["accepted_count"], 1)
        self.assertIn("/vi/square/post/91000002", fetched_urls[0])
        with connect(self.db) as conn:
            lang = conn.execute("SELECT language FROM contents WHERE external_id='91000002'").fetchone()[0]
        self.assertIsNone(lang)
        from bti.public import articles
        self.assertEqual(articles(self.db, lang="vi", now=now)["sample_count"], 1)

        # 3. Active archive cooldown -> cooldown_aligned & preserved in cold_standby replication
        with connect(self.db) as conn:
            conn.execute(
                "UPDATE content_volume_state SET archive_cooldown_until='2026-10-21T10:15:00Z', archive_last_blocked_status=202 WHERE id=1"
            )
            conn.execute(
                "INSERT INTO schema_meta(key,value) VALUES('host_cooldown:www.binance.com','{\"status_code\":429,\"until\":\"2026-10-21T16:00:00Z\"}')"
            )
            conn.commit()
        cd_pacing = compute_adaptive_interval(self.db, now=now)
        self.assertEqual(cd_pacing["mode"], "cooldown_aligned")
        self.assertEqual(cd_pacing["next_interval_seconds"], 905)

        # Replicate worker_node bundle
        worker_bundle = replicate_scanner_instance(
            self.root / "replica-worker-node", mode="worker_node", node_name="vn-worker-02",
        )
        self.assertEqual(worker_bundle["status"], "replicated")
        self.assertEqual(worker_bundle["mode"], "worker_node")
        self.assertTrue((self.root / "replica-worker-node" / "executor.py").is_file())
        self.assertTrue((self.root / "replica-worker-node" / "secrets" / "vn-worker-02-lease.key").is_file())

        # Replicate cold_standby DR bundle: must preserve cooldowns and block active dual-writer cycles
        standby_dir = self.root / "replica-standby"
        standby_bundle = replicate_scanner_instance(
            standby_dir, mode="cold_standby", node_name="vn-standby-01", source_db_path=self.db,
        )
        self.assertFalse(standby_bundle["worker_enabled"])
        self.assertFalse(standby_bundle["active_dispatch_allowed"])
        self.assertTrue(standby_bundle["preserved_cooldowns"]["full_online_backup"])
        self.assertEqual(
            standby_bundle["preserved_cooldowns"]["archive_cooldown_until"],
            "2026-10-21T10:15:00Z",
        )
        self.assertEqual(standby_bundle["preserved_cooldowns"]["host_cooldown_keys"], 1)
        self.assertEqual(standby_bundle["preserved_cooldowns"]["retained_contents"], 1)
        self.assertEqual(standby_bundle["preserved_cooldowns"]["retained_archive_candidates"], 4)
        self.assertTrue(standby_bundle["doctor"]["ready"])
        self.assertTrue(standby_bundle["doctor"]["standby_guarded"])
        #Also verify hybrid desktop-only routing for /vi/square/ URLs
        from bti.content_volume import _dispatch_archive_url, _record_fetch_import
        routed_calls: list[str] = []
        imported_nodes: list[tuple[str, int]] = []
        class DummyBridge:
            def __init__(self) -> None:
                self.local_fetcher = lambda u, **kw: (routed_calls.append("local:" + u), FetchResult(u, 200, "text/html", b"ok", 1, "2026-10-21T10:00:00Z"))[1]
                self.authorization_check = lambda u: True
            def __call__(self, u: str, **kw: object) -> FetchResult:
                routed_calls.append(f"bridge(cdx={bool(kw.get('allow_cdx'))}):" + u)
                res = FetchResult(u, 200, "text/html", b"ok", 1, "2026-10-21T10:00:00Z")
                res.executor_metadata = {"node": "msi"}
                return res
            def record_imported(self, res: FetchResult, count: int) -> None:
                imported_nodes.append((res.executor_metadata["node"], count))
        dummy = DummyBridge()
        r_vi = _dispatch_archive_url(dummy, "https://web.archive.org/web/20261019120000id_/https://www.binance.com/vi/square/post/91000002", coordinator=FetchCoordinator(concurrency=1, minimum_spacing_seconds=1.0))
        _record_fetch_import(dummy, r_vi, 1)
        r_en = _dispatch_archive_url(dummy, "https://web.archive.org/web/20261020120000id_/https://www.binance.com/en/square/post/91000001", coordinator=FetchCoordinator(concurrency=1, minimum_spacing_seconds=1.0))
        _record_fetch_import(dummy, r_en, 1)
        r_cdx = _dispatch_archive_url(dummy, "https://web.archive.org/cdx/search/cdx?url=www.binance.com%2Fen%2Fsquare%2Fpost%2F%2A&output=json", coordinator=FetchCoordinator(concurrency=1, minimum_spacing_seconds=1.0), allow_cdx=True)
        self.assertTrue(routed_calls[0].startswith("local:"))
        self.assertTrue(routed_calls[1].startswith("bridge(cdx=False):"))
        self.assertTrue(routed_calls[2].startswith("bridge(cdx=True):"))
        self.assertEqual(imported_nodes, [("desktop", 1), ("msi", 1)])

        standby_db = standby_dir / "runtime" / "bti.sqlite3"
        standby_manifest = standby_dir / "config" / "public_sources.json"
        with self.assertRaisesRegex(RuntimeError, "active_dispatch_allowed=false"):
            run_cycle(standby_db, standby_manifest, now=now, policy_checker_override=allow_fixture_policy)

        pub_scan = scanner_public_status(self.db, now=now)
        self.assertFalse(pub_scan["autonomous_operation"]["pre_login_reboot_guarantee"])
        self.assertNotIn("commands", json.dumps(pub_scan))


if __name__ == "__main__":
    unittest.main()

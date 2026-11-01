from __future__ import annotations

import hashlib
import json
import math
import os
import signal
import threading
import time
import urllib.parse
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from .backup import create_backup
from .content_volume import _bounded_private_diagnostic, content_volume_status, run_content_volume_batch
from .crawler_policy import prepare_crawler_cycle
from .db import connect, init_db
from .domain import build_drafts
from .ingest import FetchCoordinator, FetchResult, ingest_source_configs, load_manifest_document, seed_source_configs
from .historical import run_backfill_batch
from .scanner import check_standby_guard, compute_adaptive_interval, self_heal_database
DEFAULT_INTERVAL_SECONDS = 3600
MIN_PRODUCTION_INTERVAL_SECONDS = 60
MAX_ROTATED_PROFILES = 3
HOURLY_SECONDS = 3600
DAILY_SECONDS = 86400
MAX_COOLDOWN_SECONDS = 86400
WORKER_CYCLE_AUDIT_LIMIT = 1000
WORKER_RESULT_HISTORY_LIMIT = 20
BACKUP_RETRY_SECONDS = 3600


class WorkerAlreadyRunning(RuntimeError):
    pass


def _utc(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("Worker time must be timezone-aware")
    return current.astimezone(timezone.utc)


def _iso(value: datetime | None = None) -> str:
    return _utc(value).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def validate_interval(interval_seconds: int, *, allow_short: bool = False) -> int:
    if interval_seconds <= 0:
        raise ValueError("Worker interval must be positive")
    if interval_seconds < MIN_PRODUCTION_INTERVAL_SECONDS and not allow_short:
        raise ValueError("Intervals below 60 seconds require --allow-short-interval")
    return interval_seconds


class ProcessLock:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._handle: Any = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            handle.close()
            raise WorkerAlreadyRunning("Another BTI writer already holds the worker lock") from exc
        self._handle = handle

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            self._handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None

    def __enter__(self) -> "ProcessLock":
        self.acquire()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.release()


def default_lock_path(db_path: str | Path) -> Path:
    return Path(db_path).resolve().parent / "bti-worker.lock"


def default_backup_dir(db_path: str | Path) -> Path:
    return Path(db_path).resolve().parent / "backups"


def _schedule_policy(source: dict[str, Any]) -> tuple[str, int | None]:
    configured = source.get("schedule")
    if configured in {"hourly", "daily", "manual", "disabled", "dynamic"}:
        schedule_class = configured
    else:
        source_type = source.get("source_type")
        if source_type in {"binance_square_public_feed", "binance_square_creator_posts"}:
            schedule_class = "hourly"
        elif source_type in {"binance_official_award_winners", "binance_official_program", "binance_official_competition_rules"}:
            schedule_class = "daily"
        elif source_type == "binance_copy_trading_public_home":
            schedule_class = "manual"
        else:
            schedule_class = "disabled"
    cadence = {
        "hourly": HOURLY_SECONDS,
        "daily": DAILY_SECONDS,
        "dynamic": HOURLY_SECONDS,
        "manual": None,
        "disabled": None,
    }[schedule_class]
    return schedule_class, cadence


def ensure_source_schedules(
    db_path: str | Path,
    sources: Iterable[dict[str, Any]],
    *,
    now: datetime | None = None,
) -> None:
    source_list = list(sources)
    seed_source_configs(db_path, source_list)
    now_iso = _iso(now)
    with connect(db_path) as conn:
        for source in source_list:
            row = conn.execute("SELECT id FROM sources WHERE slug=?", (source["slug"],)).fetchone()
            schedule_class, cadence = _schedule_policy(source)
            initial_due = now_iso if cadence is not None else None
            conn.execute(
                """INSERT INTO source_schedule(
                       source_id,schedule_class,cadence_seconds,next_due_at,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?)
                   ON CONFLICT(source_id) DO UPDATE SET
                       schedule_class=excluded.schedule_class,
                       cadence_seconds=excluded.cadence_seconds,
                       next_due_at=CASE
                           WHEN excluded.cadence_seconds IS NULL THEN NULL
                           ELSE COALESCE(source_schedule.next_due_at,excluded.next_due_at)
                       END,
                       updated_at=excluded.updated_at""",
                (row["id"], schedule_class, cadence, initial_due, now_iso, now_iso),
            )
        conn.commit()


def _is_due(next_due: str | None, cooldown_until: str | None, now: datetime) -> bool:
    due_at = _parse(next_due)
    cooldown = _parse(cooldown_until)
    return due_at is not None and due_at <= now and (cooldown is None or cooldown <= now)


def due_manifest_sources(
    db_path: str | Path,
    sources: Iterable[dict[str, Any]],
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    current = _utc(now)
    selected: list[dict[str, Any]] = []
    with connect(db_path) as conn:
        for source in sources:
            row = conn.execute(
                """SELECT schedule.next_due_at,schedule.cooldown_until,schedule.cadence_seconds
                   FROM sources source JOIN source_schedule schedule ON schedule.source_id=source.id
                   WHERE source.slug=?""",
                (source["slug"],),
            ).fetchone()
            if row and row["cadence_seconds"] is not None and _is_due(row["next_due_at"], row["cooldown_until"], current):
                selected.append(source)
    return selected


def _cooldown_seconds(outcome: str, status_code: int | None, failures: int, cadence: int | None) -> int:
    if outcome in {"success", "skipped"}:
        return cadence or HOURLY_SECONDS
    if outcome == "blocked":
        if status_code == 429:
            return 6 * HOURLY_SECONDS
        return DAILY_SECONDS
    base = max(cadence or HOURLY_SECONDS, HOURLY_SECONDS * (2 ** min(failures, 5)))
    return min(base, MAX_COOLDOWN_SECONDS)


def update_source_schedule(
    db_path: str | Path,
    source_slug: str,
    event: dict[str, Any],
    *,
    now: datetime | None = None,
) -> None:
    current = _utc(now)
    with connect(db_path) as conn:
        row = conn.execute(
            """SELECT schedule.*,source.id AS source_id FROM sources source
               JOIN source_schedule schedule ON schedule.source_id=source.id WHERE source.slug=?""",
            (source_slug,),
        ).fetchone()
        if row is None:
            return
        outcome = event.get("outcome") if event.get("outcome") in {"success", "blocked", "error", "skipped"} else "error"
        failures = 0 if outcome in {"success", "skipped"} else int(row["consecutive_failures"]) + 1
        delay = _cooldown_seconds(outcome, event.get("status_code"), failures, row["cadence_seconds"])
        retry_after = event.get("retry_after_seconds")
        if isinstance(retry_after, int) and retry_after >= 0:
            delay = max(delay, min(retry_after, MAX_COOLDOWN_SECONDS))
        next_due = _iso(current + timedelta(seconds=delay))
        cooldown_until = None if outcome in {"success", "skipped"} else next_due
        conn.execute(
            """UPDATE source_schedule SET next_due_at=?,cooldown_until=?,consecutive_failures=?,
                   last_outcome=?,last_cycle_at=?,updated_at=? WHERE source_id=?""",
            (next_due, cooldown_until, failures, outcome, _iso(current), _iso(current), row["source_id"]),
        )
        conn.commit()


def official_awardee_candidates(db_path: str | Path) -> list[dict[str, Any]]:
    with connect(db_path) as conn:
        rows = conn.execute(
            """SELECT DISTINCT e.slug,e.display_name,e.username,e.source_uid,e.profile_url,
                      e.identity_evidence_url,ev.value_json
               FROM entities e
               JOIN evidence ev ON ev.entity_id=e.id
               JOIN recognitions r ON r.evidence_id=ev.id
               JOIN sources s ON s.id=ev.source_id
               JOIN content_versions cv ON cv.id=ev.version_id
               JOIN contents c ON c.id=cv.content_id
               WHERE e.category='official_trader_awardee' AND e.source_uid IS NOT NULL
                 AND e.public_shareable=1
                 AND ev.verification_level='official' AND ev.public_shareable=1
                 AND r.program_name='Blockchain 100 2025' AND r.category_type='content'
                 AND r.category_name='Trader' AND r.result='Awardee'
                 AND c.entity_id=e.id AND c.source_id=ev.source_id AND c.public_shareable=1
                 AND s.access_level='public' AND s.distribution='public_shareable'"""
        ).fetchall()
    candidates = []
    for row in rows:
        value = json.loads(row["value_json"] or "{}")
        candidates.append({**dict(row), "rank": value.get("rank")})
    candidates.sort(key=lambda item: (item["rank"] if isinstance(item["rank"], int) else 10_000, item["source_uid"]))
    return candidates


def _dynamic_slug(source_uid: str) -> str:
    return f"awardee-profile-{hashlib.sha256(source_uid.encode('utf-8')).hexdigest()[:16]}"


def dynamic_profile_source(candidate: dict[str, Any]) -> dict[str, Any]:
    source_uid = str(candidate["source_uid"])
    username = str(candidate["username"])
    profile_url = candidate.get("profile_url") or f"https://www.binance.com/en/square/profile/{urllib.parse.quote(username)}"
    query = urllib.parse.urlencode({"targetSquareUid": source_uid, "timeOffset": -1, "filterType": "ALL"})
    return {
        "slug": _dynamic_slug(source_uid),
        "name": f"Awardee public posts — {candidate['display_name']}",
        "url": profile_url,
        "fetch_url": "https://www.binance.com/bapi/composite/v2/friendly/pgc/content/queryUserProfilePageContentsWithFilter?" + query,
        "source_type": "binance_square_creator_posts",
        "adapter": "square_user_posts",
        "access_level": "public",
        "distribution": "public_shareable",
        "expected_pillar": "engagement",
        "minimum_text_chars": 200,
        "max_items": 3,
        "schedule": "dynamic",
        "entity": {
            "slug": candidate["slug"],
            "source_uid": source_uid,
            "display_name": candidate["display_name"],
            "username": username,
            "profile_url": profile_url,
            "category": "official_trader_awardee",
            "identity_status": "official_award_payload_uid",
            "identity_evidence_url": candidate["identity_evidence_url"],
        },
        "delay_after_seconds": 1,
    }


def select_profile_rotation(
    db_path: str | Path,
    manifest_sources: Iterable[dict[str, Any]],
    *,
    now: datetime | None = None,
    limit: int = MAX_ROTATED_PROFILES,
) -> tuple[list[dict[str, Any]], int]:
    bounded_limit = max(0, min(limit, MAX_ROTATED_PROFILES))
    manual_uids = {
        str(source.get("entity", {}).get("source_uid"))
        for source in manifest_sources
        if source.get("adapter") == "square_user_posts" and source.get("entity", {}).get("source_uid")
    }
    candidates = [item for item in official_awardee_candidates(db_path) if item["source_uid"] not in manual_uids]
    if not candidates or bounded_limit == 0:
        return [], 0
    with connect(db_path) as conn:
        state = conn.execute("SELECT rotation_cursor FROM worker_state WHERE id=1").fetchone()
        cursor = int(state["rotation_cursor"] if state else 0) % len(candidates)
        schedule_rows = {
            row["slug"]: (row["next_due_at"], row["cooldown_until"])
            for row in conn.execute(
                """SELECT source.slug,schedule.next_due_at,schedule.cooldown_until FROM sources source
                   JOIN source_schedule schedule ON schedule.source_id=source.id
                   WHERE schedule.schedule_class='dynamic'"""
            ).fetchall()
        }
    current = _utc(now)
    selected: list[dict[str, Any]] = []
    scanned = 0
    while scanned < len(candidates) and len(selected) < bounded_limit:
        candidate = candidates[(cursor + scanned) % len(candidates)]
        source = dynamic_profile_source(candidate)
        schedule = schedule_rows.get(source["slug"])
        if schedule is None or _is_due(schedule[0], schedule[1], current):
            selected.append(source)
        scanned += 1
    next_cursor = (cursor + max(scanned, 1)) % len(candidates)
    return selected, next_cursor


def _set_worker_running(db_path: str | Path, now: datetime, interval_seconds: int) -> int:
    now_iso = _iso(now)
    with connect(db_path) as conn:
        conn.execute(
            """UPDATE worker_state SET status='running',interval_seconds=?,heartbeat_at=?,
                   process_started_at=COALESCE(process_started_at,?),last_cycle_started_at=?,updated_at=?
               WHERE id=1""",
            (interval_seconds, now_iso, now_iso, now_iso, now_iso),
        )
        cycle_id = int(conn.execute(
            "INSERT INTO worker_cycles(started_at,status) VALUES(?,'running')", (now_iso,)
        ).lastrowid)
        conn.commit()
    return cycle_id


def _backup_due(last_backup_at: str | None, backup_next_attempt_at: str | None, now: datetime) -> bool:
    retry_at = _parse(backup_next_attempt_at)
    if retry_at is not None and retry_at > now:
        return False
    previous = _parse(last_backup_at)
    return previous is None or previous.date() < now.date()


def _next_due(db_path: str | Path, fallback: datetime) -> str:
    with connect(db_path) as conn:
        values = [
            _parse(row[0])
            for row in conn.execute(
                "SELECT next_due_at FROM source_schedule WHERE cadence_seconds IS NOT NULL AND next_due_at IS NOT NULL"
            ).fetchall()
        ]
        state = conn.execute("SELECT rotation_next_due_at FROM worker_state WHERE id=1").fetchone()
        values.append(_parse(state[0]) if state else None)
    concrete = [value for value in values if value is not None]
    return _iso(min(concrete) if concrete else fallback)


def run_cycle(
    db_path: str | Path,
    manifest_path: str | Path,
    *,
    interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
    allow_short_interval: bool = False,
    now: datetime | None = None,
    lock_path: str | Path | None = None,
    acquire_lock: bool = True,
    backup_dir: str | Path | None = None,
    backup_keep: int = 7,
    fetch_concurrency: int = 2,
    policy_checker_override: Callable[[str], tuple[bool, str]] | None = None,
    crawler_fetcher_override: Callable[..., FetchResult] | None = None,
    content_fetcher_override: Callable[..., FetchResult] | None = None,
) -> dict[str, Any]:
    interval = validate_interval(interval_seconds, allow_short=allow_short_interval)
    current = _utc(now)
    check_standby_guard(db_path)
    lock = ProcessLock(lock_path or default_lock_path(db_path)) if acquire_lock else nullcontext()
    with lock:
        manifest = load_manifest_document(manifest_path)
        manifest_sources = manifest["sources"]
        init_db(db_path)
        try:
            self_heal_database(db_path, now=current)
        except Exception:
            pass
        ensure_source_schedules(db_path, manifest_sources, now=current)
        cycle_id = _set_worker_running(db_path, current, interval)
        counts = {
            "attempted": 0, "success": 0, "blocked": 0, "error": 0,
            "skipped": 0, "policy_paused": 0, "rotated": 0,
        }
        backfill_counts = {"attempted": 0, "accepted": 0, "rejected": 0, "cooldown": 0, "policy_paused": 0}
        content_extraction: dict[str, Any] = {
            "status": "not_configured", "new_qualifying_bodies_this_batch": 0,
        }
        safe_failures: list[str] = []
        due: list[dict[str, Any]] = []
        backup_status = "disabled" if backup_dir is None else "not_due"
        crawler_status = {
            "policy": {"status": "not_checked", "reason": "policy_not_configured_fail_closed"},
            "discovery": {"status": "not_started", "reason": "discovery_not_due"},
        }
        fetch_coordinator = FetchCoordinator(
            concurrency=fetch_concurrency, policy_checker=policy_checker_override,
        )
        try:
            if policy_checker_override is None:
                crawler_kwargs: dict[str, Any] = {"now": current}
                if crawler_fetcher_override is not None:
                    crawler_kwargs["fetcher"] = crawler_fetcher_override
                crawler_status = prepare_crawler_cycle(
                    db_path, manifest.get("crawler_policy"), fetch_coordinator, **crawler_kwargs,
                )
                if crawler_status["policy"]["status"] in {"refresh_failed", "not_configured", "cooldown"}:
                    safe_failures.append("crawler_policy")
                if crawler_status["discovery"]["status"] in {"error", "policy_paused"}:
                    safe_failures.append("sitemap_discovery")
            else:
                crawler_status = {
                    "policy": {"status": "enforced", "reason": "explicit_test_policy"},
                    "discovery": {"status": "disabled", "reason": "explicit_test_policy"},
                }
            due = due_manifest_sources(db_path, manifest_sources, now=current)
            if due:
                result = ingest_source_configs(
                    db_path, due, run_note="unattended_manifest_cycle",
                    fetch_coordinator=fetch_coordinator, current_time=current,
                )
                for event in result["events"]:
                    update_source_schedule(db_path, event["source"], event, now=current)
                    if event.get("skip_reason") == "crawler_policy":
                        counts["policy_paused"] += 1
                    else:
                        counts["attempted"] += 1
                    outcome = event.get("outcome", "error")
                    counts[outcome if outcome in {"success", "blocked", "error", "skipped"} else "error"] += 1

            with connect(db_path) as conn:
                state = conn.execute("SELECT rotation_next_due_at,rotation_cursor,last_backup_at,backup_next_attempt_at FROM worker_state WHERE id=1").fetchone()
            rotation_due = _parse(state["rotation_next_due_at"]) if state else None
            if rotation_due is None or rotation_due <= current:
                dynamic_sources, next_cursor = select_profile_rotation(
                    db_path, manifest_sources, now=current, limit=MAX_ROTATED_PROFILES,
                )
                if dynamic_sources:
                    ensure_source_schedules(db_path, dynamic_sources, now=current)
                    dynamic_result = ingest_source_configs(
                        db_path, dynamic_sources, run_note="unattended_official_uid_rotation",
                        fetch_coordinator=fetch_coordinator, current_time=current,
                    )
                    for event in dynamic_result["events"]:
                        update_source_schedule(db_path, event["source"], event, now=current)
                        if event.get("skip_reason") == "crawler_policy":
                            counts["policy_paused"] += 1
                        else:
                            counts["attempted"] += 1
                            counts["rotated"] += 1
                        outcome = event.get("outcome", "error")
                        counts[outcome if outcome in {"success", "blocked", "error", "skipped"} else "error"] += 1
                with connect(db_path) as conn:
                    conn.execute(
                        "UPDATE worker_state SET rotation_cursor=?,rotation_next_due_at=?,updated_at=? WHERE id=1",
                        (next_cursor, _iso(current + timedelta(seconds=HOURLY_SECONDS)), _iso(current)),
                    )
                    conn.commit()

            historical_manifest = Path(manifest_path).with_name("historical_seeds.json")
            if historical_manifest.is_file():
                try:
                    backfill_counts = run_backfill_batch(
                        db_path, historical_manifest, now=current, fetch_coordinator=fetch_coordinator,
                    )
                    if backfill_counts["rejected"]:
                        safe_failures.append("historical_validation")
                except Exception:
                    safe_failures.append("historical_backfill")

            content_manifest = Path(manifest_path).with_name("content_sources.json")
            if content_manifest.is_file():
                try:
                    content_enabled = json.loads(content_manifest.read_text(encoding="utf-8")).get("enabled", True)
                except (OSError, ValueError, AttributeError):
                    content_enabled = True  # Existing validation reports malformed manifests below.
                if content_enabled is False:
                    content_extraction = {"status": "disabled", "new_qualifying_bodies_this_batch": 0}
                elif crawler_fetcher_override is not None and content_fetcher_override is None:
                    content_extraction = {
                        "status": "not_exercised_test_fetcher",
                        "new_qualifying_bodies_this_batch": 0,
                    }
                else:
                    before_qualifying: int | None = None
                    try:
                        before_volume_status = content_volume_status(db_path, now=current)
                        before_qualifying = int(before_volume_status.get("new_qualifying_bodies_since_baseline", 0))
                        volume_kwargs: dict[str, Any] = {}
                        if content_fetcher_override is not None:
                            volume_kwargs["archive_fetcher"] = content_fetcher_override
                        volume = run_content_volume_batch(
                            db_path,
                            content_manifest,
                            artifact_dir=Path(db_path).resolve().parent / "content-volume",
                            now=current,
                            **volume_kwargs,
                        )
                        volume_diagnostics = list(volume.get("private_diagnostics") or [])
                        extraction_status = (
                            "partial_error" if (volume_diagnostics and volume["new_qualifying_bodies_this_batch"] > 0)
                            else "error" if volume_diagnostics
                            else volume["status"]["status"]
                        )
                        content_extraction = {
                            "status": extraction_status,
                            "lifetime_status": volume["status"].get("lifetime_status", volume["status"]["status"]),
                            "current_extraction_status": volume["status"].get("current_extraction_status"),
                            "new_qualifying_bodies_this_batch": volume["new_qualifying_bodies_this_batch"],
                            "new_qualifying_bodies_since_baseline": volume["status"]["new_qualifying_bodies_since_baseline"],
                            "total_unique_qualifying_bodies": volume["status"]["total_unique_qualifying_bodies"],
                            "quality_audited_bodies": volume["status"]["quality_audited_bodies"],
                            "publication_coverage": volume["status"]["publication_coverage"],
                            "public_author_count": volume["status"]["public_author_count"],
                            "last_successful_content_extraction_at": volume["status"]["last_successful_content_extraction_at"],
                            "attempted_archive_snapshots": volume["archive_consumer"]["attempted"],
                            "blocked_archive_snapshots": volume["archive_consumer"]["blocked"],
                            "rejected_archive_snapshots": sum(volume["archive_consumer"]["rejected"].values()),
                            "duplicate_archive_snapshots": volume["archive_consumer"]["duplicates"],
                            "archive_requests_used": volume["throughput"]["total_archive_requests_used"],
                            "archive_request_budget": volume["throughput"]["total_archive_request_budget"],
                        }
                        if volume_diagnostics:
                            content_extraction["private_diagnostics"] = volume_diagnostics
                            safe_failures.append("content_volume")
                    except Exception as exc:
                        diag = _bounded_private_diagnostic(exc, "content_volume")
                        try:
                            after_volume_status = content_volume_status(db_path, now=current)
                            after_qualifying = int(after_volume_status.get("new_qualifying_bodies_since_baseline", 0))
                            partial_committed: int | None = (
                                max(0, after_qualifying - before_qualifying)
                                if before_qualifying is not None
                                else None
                            )
                            content_extraction = {
                                "status": "partial_error" if (partial_committed or 0) > 0 else "error",
                                "lifetime_status": after_volume_status.get("lifetime_status", after_volume_status.get("status")),
                                "current_extraction_status": after_volume_status.get("current_extraction_status"),
                                "new_qualifying_bodies_this_batch": partial_committed,
                                "new_qualifying_bodies_since_baseline": after_qualifying,
                                "total_unique_qualifying_bodies": after_volume_status.get("total_unique_qualifying_bodies", 0),
                                "quality_audited_bodies": after_volume_status.get("quality_audited_bodies", 0),
                                "publication_coverage": after_volume_status.get("publication_coverage"),
                                "public_author_count": after_volume_status.get("public_author_count", 0),
                                "last_successful_content_extraction_at": after_volume_status.get("last_successful_content_extraction_at"),
                                "private_diagnostics": [diag],
                            }
                        except Exception as status_exc:
                            content_extraction = {
                                "status": "error",
                                "new_qualifying_bodies_this_batch": None,
                                "private_diagnostics": [diag, _bounded_private_diagnostic(status_exc, "content_volume_status")],
                            }
                        safe_failures.append("content_volume")
            try:
                build_drafts(db_path)
            except Exception:
                safe_failures.append("draft_build")

            if backup_dir is not None and _backup_due(
                state["last_backup_at"] if state else None,
                state["backup_next_attempt_at"] if state else None,
                current,
            ):
                try:
                    create_backup(db_path, backup_dir, keep=backup_keep, now=current)
                    backup_status = "success"
                except Exception:
                    backup_status = "failed"
                    safe_failures.append("backup")
                with connect(db_path) as conn:
                    if backup_status == "success":
                        conn.execute(
                            """UPDATE worker_state SET last_backup_at=?,last_backup_attempt_at=?,
                                   backup_next_attempt_at=NULL,last_backup_status='success',updated_at=? WHERE id=1""",
                            (_iso(current), _iso(current), _iso(current)),
                        )
                    else:
                        conn.execute(
                            """UPDATE worker_state SET last_backup_attempt_at=?,backup_next_attempt_at=?,
                                   last_backup_status='failed',updated_at=? WHERE id=1""",
                            (_iso(current), _iso(current + timedelta(seconds=BACKUP_RETRY_SECONDS)), _iso(current)),
                        )
                    conn.commit()

            status = "success" if counts["blocked"] == 0 and counts["error"] == 0 and not safe_failures else "degraded"
        except Exception:
            status = "failed"
            safe_failures.append("cycle_internal")

        completed = _utc(now) if now is not None else datetime.now(timezone.utc)
        next_cycle = completed + timedelta(seconds=interval)
        summary = {
            "attempted_source_count": counts["attempted"],
            "success_count": counts["success"],
            "blocked_count": counts["blocked"],
            "error_count": counts["error"],
            "skipped_count": counts["skipped"],
            "policy_paused_source_count": counts["policy_paused"],
            "rotated_profile_count": counts["rotated"],
            "historical_backfill": backfill_counts,
            "content_extraction": content_extraction,
            "crawler": crawler_status,
            "safe_failure_phases": safe_failures,
        }
        with connect(db_path) as conn:
            conn.execute(
                """UPDATE worker_cycles SET completed_at=?,status=?,due_source_count=?,attempted_source_count=?,
                       success_count=?,blocked_count=?,error_count=?,rotated_profile_count=?,backup_status=?,summary_json=?
                   WHERE id=?""",
                (_iso(completed), status, len(due), counts["attempted"], counts["success"], counts["blocked"],
                 counts["error"], counts["rotated"], backup_status,
                 json.dumps(summary, separators=(",", ":"), sort_keys=True), cycle_id),
            )
            conn.execute(
                """UPDATE worker_state SET status=?,heartbeat_at=?,last_cycle_completed_at=?,next_cycle_due_at=?,
                       last_cycle_status=?,last_cycle_summary_json=?,cycle_count=cycle_count+1,updated_at=? WHERE id=1""",
                ("idle" if status == "success" else "degraded", _iso(completed), _iso(completed),
                 _iso(next_cycle), status, json.dumps(summary, separators=(",", ":"), sort_keys=True),
                 _iso(completed)),
            )
            conn.execute(
                """DELETE FROM worker_cycles WHERE id NOT IN (
                       SELECT id FROM worker_cycles ORDER BY id DESC LIMIT ?
                   )""",
                (WORKER_CYCLE_AUDIT_LIMIT,),
            )
            conn.commit()
        return {
            "cycle_id": cycle_id,
            "status": status,
            "started_at": _iso(current),
            "completed_at": _iso(completed),
            "next_cycle_due_at": _iso(next_cycle),
            "source_counts": summary,
            "backup_status": backup_status,
            "next_source_due_at": _next_due(db_path, next_cycle),
        }


def touch_heartbeat(db_path: str | Path, *, status: str | None = None) -> None:
    now_iso = _iso()
    with connect(db_path) as conn:
        if status:
            conn.execute("UPDATE worker_state SET status=?,heartbeat_at=?,updated_at=? WHERE id=1", (status, now_iso, now_iso))
        else:
            conn.execute("UPDATE worker_state SET heartbeat_at=?,updated_at=? WHERE id=1", (now_iso, now_iso))
        conn.commit()


def run_worker(
    db_path: str | Path,
    manifest_path: str | Path,
    *,
    interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
    allow_short_interval: bool = False,
    backup_dir: str | Path | None = None,
    backup_keep: int = 7,
    lock_path: str | Path | None = None,
    max_cycles: int | None = None,
    stop_event: threading.Event | None = None,
    fetch_concurrency: int = 2,
    adaptive_interval: bool = False,
) -> dict[str, Any]:
    interval = validate_interval(interval_seconds, allow_short=allow_short_interval)
    check_standby_guard(db_path)
    stop = stop_event or threading.Event()
    results: list[dict[str, Any]] = []
    completed_cycles = 0
    worker_lock = ProcessLock(lock_path or default_lock_path(db_path))
    with worker_lock:
        init_db(db_path)
        touch_heartbeat(db_path, status="running")

        def request_stop(signum: int, frame: Any) -> None:
            stop.set()

        previous_handlers: dict[int, Any] = {}
        if threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGINT, signal.SIGTERM):
                previous_handlers[signum] = signal.getsignal(signum)
                signal.signal(signum, request_stop)
        try:
            while not stop.is_set() and (max_cycles is None or completed_cycles < max_cycles):
                result = run_cycle(
                    db_path, manifest_path, interval_seconds=interval, allow_short_interval=allow_short_interval,
                    acquire_lock=False, backup_dir=backup_dir, backup_keep=backup_keep,
                    fetch_concurrency=fetch_concurrency,
                )
                results.append(result)
                if len(results) > WORKER_RESULT_HISTORY_LIMIT:
                    del results[:-WORKER_RESULT_HISTORY_LIMIT]
                completed_cycles += 1
                if max_cycles is not None and completed_cycles >= max_cycles:
                    break
                sleep_seconds = interval
                if adaptive_interval:
                    try:
                        adaptive = compute_adaptive_interval(
                            db_path, base_interval_seconds=interval, max_interval_seconds=max(interval, 3600)
                        )
                        sleep_seconds = int(adaptive["next_interval_seconds"])
                    except Exception:
                        sleep_seconds = interval
                deadline = time.monotonic() + sleep_seconds
                while not stop.is_set():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    if stop.wait(min(30.0, remaining)):
                        break
                    touch_heartbeat(db_path)
        finally:
            try:
                touch_heartbeat(db_path, status="stopping")
                touch_heartbeat(db_path, status="idle")
            finally:
                for signum, handler in previous_handlers.items():
                    signal.signal(signum, handler)
    return {"cycles_completed": completed_cycles, "recent_cycles": results}

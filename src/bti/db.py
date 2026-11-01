from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA_VERSION = 8
PILLARS = ("engagement", "performance", "recognition")


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def public_excerpt(text: str, *, max_words: int = 24, max_chars: int = 240) -> str:
    """Keep public excerpts short; full normalized text remains only in the ignored evidence DB."""
    words = text.split()
    value = " ".join(words[:max_words])
    if len(value) > max_chars:
        value = value[: max_chars - 1].rstrip()
    if len(words) > max_words or len(text) > len(value):
        value += "…"
    return value


class ClosingConnection(sqlite3.Connection):
    """A sqlite connection whose context manager also releases the Windows file handle."""

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        try:
            result = super().__exit__(exc_type, exc, traceback)
            return bool(result)
        finally:
            self.close()


def connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10, factory=ClosingConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try:
        conn.execute("BEGIN")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


SCHEMA = r"""
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    url TEXT NOT NULL UNIQUE,
    fetch_url TEXT NOT NULL,
    source_type TEXT NOT NULL,
    adapter TEXT NOT NULL,
    access_level TEXT NOT NULL CHECK(access_level IN ('public','authenticated','private')),
    distribution TEXT NOT NULL CHECK(distribution IN ('public_shareable','internal_only','unknown')),
    expected_pillar TEXT,
    capability_status TEXT NOT NULL DEFAULT 'untested',
    capability_detail TEXT,
    available_fields_json TEXT NOT NULL DEFAULT '[]',
    last_attempt_at TEXT,
    last_success_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS entities (
    id INTEGER PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    category TEXT NOT NULL,
    source_uid TEXT,
    username TEXT,
    profile_url TEXT,
    bio TEXT,
    identity_status TEXT NOT NULL DEFAULT 'source_profile_only',
    identity_evidence_url TEXT,
    public_shareable INTEGER NOT NULL DEFAULT 0 CHECK(public_shareable IN (0,1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ingest_runs (
    id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    source_count INTEGER NOT NULL DEFAULT 0,
    fetch_success INTEGER NOT NULL DEFAULT 0,
    new_contents INTEGER NOT NULL DEFAULT 0,
    new_versions INTEGER NOT NULL DEFAULT 0,
    duplicates INTEGER NOT NULL DEFAULT 0,
    errors INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS fetch_events (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES ingest_runs(id) ON DELETE CASCADE,
    source_id INTEGER NOT NULL REFERENCES sources(id),
    fetched_at TEXT NOT NULL,
    status_code INTEGER,
    outcome TEXT NOT NULL,
    latency_ms INTEGER NOT NULL,
    byte_count INTEGER NOT NULL DEFAULT 0,
    content_type TEXT,
    response_hash TEXT,
    error_class TEXT,
    error_message TEXT
);

CREATE TABLE IF NOT EXISTS contents (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id),
    entity_id INTEGER REFERENCES entities(id),
    external_id TEXT NOT NULL,
    canonical_url TEXT NOT NULL,
    title TEXT NOT NULL,
    author TEXT,
    language TEXT,
    published_at TEXT,
    first_seen_at TEXT NOT NULL,
    latest_version_id INTEGER,
    public_shareable INTEGER NOT NULL DEFAULT 0 CHECK(public_shareable IN (0,1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(source_id, external_id)
);

CREATE TABLE IF NOT EXISTS content_versions (
    id INTEGER PRIMARY KEY,
    content_id INTEGER NOT NULL REFERENCES contents(id) ON DELETE CASCADE,
    version_no INTEGER NOT NULL,
    fetched_at TEXT NOT NULL,
    checksum TEXT NOT NULL,
    normalized_text TEXT NOT NULL,
    excerpt TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(content_id, version_no),
    UNIQUE(content_id, checksum)
);

CREATE TABLE IF NOT EXISTS evidence (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id),
    entity_id INTEGER REFERENCES entities(id),
    version_id INTEGER REFERENCES content_versions(id),
    pillar TEXT NOT NULL CHECK(pillar IN ('engagement','performance','recognition')),
    kind TEXT NOT NULL,
    verification_level TEXT NOT NULL CHECK(verification_level IN ('platform','official','operator_capture','self_claim','secondary','unknown')),
    observed_at TEXT NOT NULL,
    period_label TEXT,
    value_json TEXT NOT NULL DEFAULT '{}',
    quote TEXT,
    methodology TEXT NOT NULL,
    limitations TEXT NOT NULL,
    dependency_group TEXT NOT NULL,
    public_shareable INTEGER NOT NULL DEFAULT 0 CHECK(public_shareable IN (0,1)),
    UNIQUE(entity_id, pillar, kind, version_id, dependency_group, value_json)
);

CREATE TABLE IF NOT EXISTS recognitions (
    id INTEGER PRIMARY KEY,
    evidence_id INTEGER NOT NULL UNIQUE REFERENCES evidence(id) ON DELETE CASCADE,
    program_name TEXT NOT NULL,
    category_type TEXT NOT NULL CHECK(category_type IN ('trading','content','community','marketing','identity','program_criteria')),
    category_name TEXT,
    result TEXT,
    criteria TEXT,
    period_label TEXT,
    recognized_at TEXT,
    known_at TEXT NOT NULL,
    interpretation_limit TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS claims (
    id INTEGER PRIMARY KEY,
    entity_id INTEGER REFERENCES entities(id),
    claim_type TEXT NOT NULL CHECK(claim_type IN ('fact','opinion','self_claim','system_inference')),
    statement TEXT NOT NULL,
    asset TEXT,
    horizon TEXT,
    trigger_text TEXT,
    invalidation_text TEXT,
    confidence_label TEXT NOT NULL,
    scorable INTEGER NOT NULL DEFAULT 0 CHECK(scorable IN (0,1)),
    public_shareable INTEGER NOT NULL DEFAULT 0 CHECK(public_shareable IN (0,1)),
    created_at TEXT NOT NULL,
    UNIQUE(entity_id, statement)
);

CREATE TABLE IF NOT EXISTS claim_evidence (
    claim_id INTEGER NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    evidence_id INTEGER NOT NULL REFERENCES evidence(id),
    dependency_group TEXT NOT NULL,
    PRIMARY KEY(claim_id, evidence_id)
);

CREATE TABLE IF NOT EXISTS claim_versions (
    claim_id INTEGER NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    version_id INTEGER NOT NULL REFERENCES content_versions(id),
    PRIMARY KEY(claim_id, version_id)
);

CREATE TABLE IF NOT EXISTS output_artifacts (
    id INTEGER PRIMARY KEY,
    artifact_type TEXT NOT NULL CHECK(artifact_type IN ('content','strategy_hypothesis','market_scenario')),
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    body_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('draft','approved','published')),
    distribution TEXT NOT NULL CHECK(distribution IN ('public_shareable','internal_only')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(artifact_type, title)
);

CREATE TABLE IF NOT EXISTS artifact_claims (
    artifact_id INTEGER NOT NULL REFERENCES output_artifacts(id) ON DELETE CASCADE,
    claim_id INTEGER NOT NULL REFERENCES claims(id),
    PRIMARY KEY(artifact_id, claim_id)
);

CREATE TABLE IF NOT EXISTS worker_state (
    id INTEGER PRIMARY KEY CHECK(id=1),
    status TEXT NOT NULL CHECK(status IN ('not_started','running','idle','degraded','stopping')),
    interval_seconds INTEGER NOT NULL DEFAULT 3600 CHECK(interval_seconds>0),
    heartbeat_at TEXT,
    process_started_at TEXT,
    last_cycle_started_at TEXT,
    last_cycle_completed_at TEXT,
    next_cycle_due_at TEXT,
    last_cycle_status TEXT CHECK(last_cycle_status IN ('success','degraded','failed') OR last_cycle_status IS NULL),
    last_cycle_summary_json TEXT NOT NULL DEFAULT '{}',
    cycle_count INTEGER NOT NULL DEFAULT 0 CHECK(cycle_count>=0),
    rotation_cursor INTEGER NOT NULL DEFAULT 0 CHECK(rotation_cursor>=0),
    rotation_next_due_at TEXT,
    last_backup_at TEXT,
    last_backup_attempt_at TEXT,
    backup_next_attempt_at TEXT,
    last_backup_status TEXT CHECK(last_backup_status IN ('success','failed') OR last_backup_status IS NULL),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_schedule (
    source_id INTEGER PRIMARY KEY REFERENCES sources(id) ON DELETE CASCADE,
    schedule_class TEXT NOT NULL CHECK(schedule_class IN ('hourly','daily','manual','disabled','dynamic')),
    cadence_seconds INTEGER CHECK(cadence_seconds IS NULL OR cadence_seconds>0),
    next_due_at TEXT,
    cooldown_until TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0 CHECK(consecutive_failures>=0),
    last_outcome TEXT CHECK(last_outcome IN ('success','blocked','error','skipped') OR last_outcome IS NULL),
    last_cycle_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS worker_cycles (
    id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL CHECK(status IN ('running','success','degraded','failed')),
    due_source_count INTEGER NOT NULL DEFAULT 0,
    attempted_source_count INTEGER NOT NULL DEFAULT 0,
    success_count INTEGER NOT NULL DEFAULT 0,
    blocked_count INTEGER NOT NULL DEFAULT 0,
    error_count INTEGER NOT NULL DEFAULT 0,
    rotated_profile_count INTEGER NOT NULL DEFAULT 0,
    backup_status TEXT CHECK(backup_status IN ('success','failed','not_due','disabled') OR backup_status IS NULL),
    summary_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS backfill_seeds (
    id INTEGER PRIMARY KEY,
    post_id TEXT NOT NULL UNIQUE,
    canonical_url TEXT NOT NULL UNIQUE,
    expected_source_uid TEXT NOT NULL,
    earliest_published_at TEXT NOT NULL,
    latest_published_at TEXT NOT NULL,
    minimum_text_chars INTEGER NOT NULL CHECK(minimum_text_chars>=200),
    required_content_type INTEGER NOT NULL,
    display_name TEXT NOT NULL,
    username TEXT NOT NULL,
    entity_slug TEXT NOT NULL,
    derived_title TEXT NOT NULL DEFAULT '',
    requested_window_start TEXT NOT NULL,
    requested_window_end TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','accepted','rejected','cooldown')),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count>=0),
    next_attempt_at TEXT,
    last_outcome TEXT,
    source_slug TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS crawler_policy_state (
    id INTEGER PRIMARY KEY CHECK(id=1),
    robots_url TEXT NOT NULL,
    user_agent TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('not_checked','enforced','refresh_failed','cooldown','not_configured')),
    reason_code TEXT NOT NULL,
    fetched_at TEXT,
    body_sha256 TEXT,
    body_text TEXT,
    last_attempt_at TEXT,
    last_success_at TEXT,
    next_refresh_at TEXT,
    error_class TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS discovery_state (
    id INTEGER PRIMARY KEY CHECK(id=1),
    status TEXT NOT NULL CHECK(status IN ('not_started','operational','not_due','policy_paused','error','disabled')),
    reason_code TEXT NOT NULL,
    last_started_at TEXT,
    last_completed_at TEXT,
    last_success_at TEXT,
    next_due_at TEXT,
    index_sha256 TEXT,
    child_sha256 TEXT,
    observed_url_count INTEGER NOT NULL DEFAULT 0 CHECK(observed_url_count>=0),
    canonical_candidate_count INTEGER NOT NULL DEFAULT 0 CHECK(canonical_candidate_count>=0),
    truncated INTEGER NOT NULL DEFAULT 0 CHECK(truncated IN (0,1)),
    error_class TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS discovery_candidates (
    id INTEGER PRIMARY KEY,
    canonical_url TEXT NOT NULL UNIQUE,
    queue_status TEXT NOT NULL CHECK(queue_status IN ('pending','retained','rejected')),
    original_published_at TEXT,
    author TEXT,
    quality_status TEXT NOT NULL DEFAULT 'unknown' CHECK(quality_status IN ('unknown','reviewed','rejected')),
    first_observed_at TEXT NOT NULL,
    last_observed_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS discovery_observations (
    id INTEGER PRIMARY KEY,
    candidate_id INTEGER NOT NULL REFERENCES discovery_candidates(id) ON DELETE CASCADE,
    source_url TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    lastmod TEXT,
    UNIQUE(candidate_id,source_sha256,lastmod)
);

CREATE TABLE IF NOT EXISTS content_archive_candidates (
    id INTEGER PRIMARY KEY,
    post_id TEXT NOT NULL UNIQUE,
    canonical_url TEXT NOT NULL UNIQUE,
    original_url TEXT NOT NULL,
    snapshot_timestamp TEXT NOT NULL,
    archive_url TEXT NOT NULL UNIQUE,
    cdx_digest TEXT,
    cdx_length INTEGER,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending','accepted','rejected','duplicate','cooldown')),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count>=0),
    next_attempt_at TEXT,
    last_outcome TEXT,
    response_sha256 TEXT,
    content_id INTEGER REFERENCES contents(id),
    body_chars INTEGER,
    published_at TEXT,
    discovered_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS content_volume_state (
    id INTEGER PRIMARY KEY CHECK(id=1),
    baseline_started_at TEXT NOT NULL,
    baseline_article_count INTEGER NOT NULL CHECK(baseline_article_count>=0),
    last_discovery_at TEXT,
    last_content_at TEXT,
    last_cdx_sha256 TEXT,
    last_cdx_row_count INTEGER NOT NULL DEFAULT 0 CHECK(last_cdx_row_count>=0),
    archive_cooldown_until TEXT,
    archive_last_blocked_status INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS content_discovery_partitions (
    id INTEGER PRIMARY KEY,
    partition_key TEXT NOT NULL UNIQUE,
    date_from TEXT NOT NULL,
    date_to TEXT NOT NULL,
    priority INTEGER NOT NULL CHECK(priority BETWEEN 0 AND 2),
    coverage_class TEXT NOT NULL CHECK(coverage_class IN ('recent_7d','recent_30d','historical')),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending','resumable','complete','cooldown')),
    resume_key TEXT,
    pages_fetched INTEGER NOT NULL DEFAULT 0 CHECK(pages_fetched>=0),
    rows_seen INTEGER NOT NULL DEFAULT 0 CHECK(rows_seen>=0),
    new_candidates INTEGER NOT NULL DEFAULT 0 CHECK(new_candidates>=0),
    last_attempt_at TEXT,
    next_attempt_at TEXT,
    last_outcome TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_evidence_entity_pillar ON evidence(entity_id, pillar);
CREATE INDEX IF NOT EXISTS idx_contents_entity ON contents(entity_id);
CREATE INDEX IF NOT EXISTS idx_claim_versions_version ON claim_versions(version_id, claim_id);
CREATE INDEX IF NOT EXISTS idx_versions_content ON content_versions(content_id, version_no DESC);
CREATE INDEX IF NOT EXISTS idx_fetch_source_time ON fetch_events(source_id, fetched_at DESC);
CREATE INDEX IF NOT EXISTS idx_worker_cycles_time ON worker_cycles(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_source_schedule_due ON source_schedule(next_due_at);
CREATE INDEX IF NOT EXISTS idx_backfill_seed_due ON backfill_seeds(status,next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_discovery_queue ON discovery_candidates(queue_status,last_observed_at);
CREATE INDEX IF NOT EXISTS idx_discovery_observation_candidate ON discovery_observations(candidate_id,observed_at);
CREATE INDEX IF NOT EXISTS idx_content_archive_queue ON content_archive_candidates(status,next_attempt_at,snapshot_timestamp);
CREATE INDEX IF NOT EXISTS idx_content_partition_due ON content_discovery_partitions(status,priority,next_attempt_at,last_attempt_at);
"""


def init_db(db_path: str | Path) -> None:
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)
        evidence_sql_row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='evidence'"
        ).fetchone()
        if evidence_sql_row and "operator_capture" not in evidence_sql_row["sql"]:
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.executescript(
                """
                BEGIN;
                CREATE TABLE evidence_v3 (
                    id INTEGER PRIMARY KEY,
                    source_id INTEGER NOT NULL REFERENCES sources(id),
                    entity_id INTEGER REFERENCES entities(id),
                    version_id INTEGER REFERENCES content_versions(id),
                    pillar TEXT NOT NULL CHECK(pillar IN ('engagement','performance','recognition')),
                    kind TEXT NOT NULL,
                    verification_level TEXT NOT NULL CHECK(verification_level IN ('platform','official','operator_capture','self_claim','secondary','unknown')),
                    observed_at TEXT NOT NULL,
                    period_label TEXT,
                    value_json TEXT NOT NULL DEFAULT '{}',
                    quote TEXT,
                    methodology TEXT NOT NULL,
                    limitations TEXT NOT NULL,
                    dependency_group TEXT NOT NULL,
                    public_shareable INTEGER NOT NULL DEFAULT 0 CHECK(public_shareable IN (0,1)),
                    UNIQUE(entity_id, pillar, kind, version_id, dependency_group, value_json)
                );
                INSERT INTO evidence_v3 SELECT * FROM evidence;
                DROP TABLE evidence;
                ALTER TABLE evidence_v3 RENAME TO evidence;
                CREATE INDEX idx_evidence_entity_pillar ON evidence(entity_id, pillar);
                COMMIT;
                """
            )
            conn.execute("PRAGMA foreign_keys = ON")
        entity_columns = {row["name"] for row in conn.execute("PRAGMA table_info(entities)")}
        if "source_uid" not in entity_columns:
            conn.execute("ALTER TABLE entities ADD COLUMN source_uid TEXT")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_entities_source_uid "
            "ON entities(source_uid) WHERE source_uid IS NOT NULL"
        )
        worker_columns = {row["name"] for row in conn.execute("PRAGMA table_info(worker_state)")}
        if worker_columns and "last_backup_attempt_at" not in worker_columns:
            conn.execute("ALTER TABLE worker_state ADD COLUMN last_backup_attempt_at TEXT")
        if worker_columns and "backup_next_attempt_at" not in worker_columns:
            conn.execute("ALTER TABLE worker_state ADD COLUMN backup_next_attempt_at TEXT")
        backfill_columns = {row["name"] for row in conn.execute("PRAGMA table_info(backfill_seeds)")}
        if backfill_columns and "requested_window_start" not in backfill_columns:
            conn.execute("ALTER TABLE backfill_seeds ADD COLUMN requested_window_start TEXT")
        if backfill_columns and "requested_window_end" not in backfill_columns:
            conn.execute("ALTER TABLE backfill_seeds ADD COLUMN requested_window_end TEXT")
        if backfill_columns:
            conn.execute(
                """UPDATE backfill_seeds SET
                       requested_window_start=COALESCE(requested_window_start,earliest_published_at),
                requested_window_end=COALESCE(requested_window_end,latest_published_at)"""
            )
        content_state_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(content_volume_state)")
        }
        if content_state_columns and "archive_cooldown_until" not in content_state_columns:
            conn.execute("ALTER TABLE content_volume_state ADD COLUMN archive_cooldown_until TEXT")
        if content_state_columns and "archive_last_blocked_status" not in content_state_columns:
            conn.execute("ALTER TABLE content_volume_state ADD COLUMN archive_last_blocked_status INTEGER")
        conn.execute(
            "INSERT INTO schema_meta(key,value) VALUES('schema_version',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )
        now = utc_now()
        conn.execute(
            """INSERT OR IGNORE INTO worker_state(
                   id,status,interval_seconds,created_at,updated_at
               ) VALUES(1,'not_started',3600,?,?)""",
            (now, now),
        )
        for row in conn.execute("SELECT id,normalized_text,excerpt FROM content_versions").fetchall():
            safe = public_excerpt(row["normalized_text"])
            if row["excerpt"] != safe:
                conn.execute("UPDATE content_versions SET excerpt=? WHERE id=?", (safe, row["id"]))
        capture_limit = (
            "Snapshot 30 ngày ở cấp portfolio; không chứng minh lợi nhuận đã thực hiện, toàn bộ dòng tiền, "
            "hiệu suất trọn đời hoặc danh tính trùng với awardee Square. "
            "operator-supplied, capture provenance not independently authenticated."
        )
        captured_rows = conn.execute(
            """SELECT ev.id,cv.metadata_json FROM evidence ev
               JOIN content_versions cv ON cv.id=ev.version_id
               WHERE ev.kind='copy_trading_30d_snapshot'"""
        ).fetchall()
        for row in captured_rows:
            metadata = json_load(row["metadata_json"], {})
            if metadata.get("capture_method") != "direct_public_html":
                conn.execute(
                    "UPDATE evidence SET verification_level='operator_capture',limitations=? WHERE id=?",
                    (capture_limit, row["id"]),
                )
        conn.commit()


def json_load(value: str | None, fallback: Any = None) -> Any:
    if value is None:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


def row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None

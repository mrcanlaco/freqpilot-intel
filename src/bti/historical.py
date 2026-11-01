from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .db import connect, init_db
from .ingest import FetchCoordinator, ingest_source_configs, minus_calendar_years, parse_utc_timestamp


def _utc(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("Historical queue time must be timezone-aware")
    return current.astimezone(timezone.utc)


def _iso(value: datetime | None = None) -> str:
    return _utc(value).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_historical_manifest(path: str | Path) -> tuple[list[dict[str, Any]], int]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    seeds = payload.get("seeds")
    maximum = int((payload.get("policy") or {}).get("max_per_cycle", 1))
    if not isinstance(seeds, list) or not 1 <= maximum <= 3:
        raise ValueError("Historical seed manifest must contain seeds and max_per_cycle 1..3")
    seen: set[str] = set()
    required = {
        "post_id", "canonical_url", "expected_source_uid", "earliest_published_at",
        "latest_published_at", "minimum_text_chars", "required_content_type",
        "display_name", "username", "entity_slug",
    }
    for seed in seeds:
        if not isinstance(seed, dict) or not required.issubset(seed):
            raise ValueError("Historical seed is missing a required field")
        post_id = str(seed["post_id"])
        if not post_id.isdigit() or post_id in seen:
            raise ValueError("Historical seed post IDs must be unique decimal identifiers")
        if seed["canonical_url"] != f"https://www.binance.com/en/square/post/{post_id}":
            raise ValueError("Historical seed must use its exact canonical Binance post URL")
        if int(seed["minimum_text_chars"]) < 200:
            raise ValueError("Historical seed substance threshold is too low")
        seen.add(post_id)
    return seeds, maximum


def _source_slug(post_id: str) -> str:
    return f"historical-post-{hashlib.sha256(post_id.encode('ascii')).hexdigest()[:16]}"


def seed_queue(db_path: str | Path, manifest_path: str | Path, *, now: datetime | None = None) -> int:
    seeds, _ = load_historical_manifest(manifest_path)
    init_db(db_path)
    current = _utc(now)
    timestamp = _iso(current)
    with connect(db_path) as conn:
        for seed in seeds:
            configured_start = parse_utc_timestamp(str(seed["earliest_published_at"]))
            configured_end = parse_utc_timestamp(str(seed["latest_published_at"]))
            requested_start = max(configured_start, minus_calendar_years(current, 2))
            requested_end = min(configured_end, current)
            if requested_start > requested_end:
                raise ValueError("Historical seed has no overlap with the rolling two-year window")
            conn.execute(
                """INSERT INTO backfill_seeds(
                       post_id,canonical_url,expected_source_uid,earliest_published_at,
                       latest_published_at,minimum_text_chars,required_content_type,display_name,
                       username,entity_slug,derived_title,requested_window_start,requested_window_end,
                       status,next_attempt_at,source_slug,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'pending',?,?,?,?)
                   ON CONFLICT(post_id) DO UPDATE SET
                       canonical_url=excluded.canonical_url,
                       expected_source_uid=excluded.expected_source_uid,
                       earliest_published_at=excluded.earliest_published_at,
                       latest_published_at=excluded.latest_published_at,
                       minimum_text_chars=excluded.minimum_text_chars,
                       required_content_type=excluded.required_content_type,
                       display_name=excluded.display_name,
                       username=excluded.username,
                       entity_slug=excluded.entity_slug,
                       derived_title=excluded.derived_title,
                       requested_window_start=excluded.requested_window_start,
                       requested_window_end=excluded.requested_window_end,
                       updated_at=excluded.updated_at""",
                (
                    str(seed["post_id"]), seed["canonical_url"], seed["expected_source_uid"],
                    seed["earliest_published_at"], seed["latest_published_at"],
                    int(seed["minimum_text_chars"]), int(seed["required_content_type"]),
                    seed["display_name"], seed["username"], seed["entity_slug"], seed.get("derived_title", ""),
                    _iso(requested_start), _iso(requested_end),
                    timestamp, _source_slug(str(seed["post_id"])),
                    timestamp, timestamp,
                ),
            )
        conn.commit()
    return len(seeds)


def _source(row: Any) -> dict[str, Any]:
    post_id = str(row["post_id"])
    return {
        "slug": row["source_slug"],
        "name": f"Verified historical creator article {post_id}",
        "url": row["canonical_url"],
        "fetch_url": f"https://www.binance.com/bapi/composite/v3/friendly/pgc/special/content/detail/{post_id}",
        "source_type": "binance_square_creator_posts",
        "adapter": "square_detail",
        "access_level": "public",
        "distribution": "public_shareable",
        "expected_pillar": "engagement",
        "historical_backfill": True,
        "expected_post_id": post_id,
        "expected_source_uid": row["expected_source_uid"],
        "earliest_published_at": row["requested_window_start"],
        "latest_published_at": row["requested_window_end"],
        "minimum_text_chars": int(row["minimum_text_chars"]),
        "required_content_type": int(row["required_content_type"]),
        "derived_title": row["derived_title"],
        "entity": {
            "slug": row["entity_slug"],
            "source_uid": row["expected_source_uid"],
            "display_name": row["display_name"],
            "username": row["username"],
            "profile_url": f"https://www.binance.com/en/square/profile/{row['username']}",
            "category": "official_trader_awardee",
            "identity_status": "official_award_payload_uid",
            "identity_evidence_url": "https://www.binance.com/en/square/blockchain-100-2025"
        },
        "delay_after_seconds": 1,
    }


def run_backfill_batch(
    db_path: str | Path,
    manifest_path: str | Path,
    *,
    now: datetime | None = None,
    fetch_coordinator: FetchCoordinator | None = None,
) -> dict[str, int]:
    current = _utc(now)
    seed_queue(db_path, manifest_path, now=current)
    _, maximum = load_historical_manifest(manifest_path)
    with connect(db_path) as conn:
        rows = conn.execute(
            """SELECT * FROM backfill_seeds
               WHERE status='pending' OR (status='cooldown' AND next_attempt_at<=?)
               ORDER BY id LIMIT ?""",
            (_iso(current), maximum),
        ).fetchall()
    counts = {"attempted": 0, "accepted": 0, "rejected": 0, "cooldown": 0, "policy_paused": 0}
    for row in rows:
        event = ingest_source_configs(
            db_path, [_source(row)], run_note="bounded_verified_historical_seed",
            fetch_coordinator=fetch_coordinator, current_time=current,
        )["events"][0]
        outcome = event.get("outcome")
        attempted_increment = 1
        if event.get("skip_reason") == "crawler_policy":
            status, next_attempt, counts_key = "cooldown", _iso(current + timedelta(hours=24)), "policy_paused"
            attempted_increment = 0
        elif outcome == "success":
            status, next_attempt, counts_key = "accepted", None, "accepted"
        elif outcome == "error" and event.get("error_class") == "ValueError":
            status, next_attempt, counts_key = "rejected", None, "rejected"
        else:
            hours = 6 if event.get("status_code") == 429 else 24 if outcome in {"blocked", "skipped"} else 1
            status, next_attempt, counts_key = "cooldown", _iso(current + timedelta(hours=hours)), "cooldown"
        counts["attempted"] += attempted_increment
        counts[counts_key] += 1
        with connect(db_path) as conn:
            conn.execute(
                """UPDATE backfill_seeds SET status=?,attempt_count=attempt_count+?,next_attempt_at=?,
                       last_outcome=?,updated_at=? WHERE id=?""",
                (status, attempted_increment, next_attempt, "policy_paused" if attempted_increment == 0 else outcome,
                 _iso(current), row["id"]),
            )
            conn.commit()
    return counts

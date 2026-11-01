from __future__ import annotations

import hashlib
import re
import sqlite3
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .db import connect, init_db
from .ingest import (
    AccessStopped,
    FetchCoordinator,
    FetchResult,
    PolicyPaused,
    USER_AGENT,
    _persisted_host_cooldown,
    _record_host_cooldown,
    fetch_public,
)

DEFAULT_ROBOTS_URL = "https://www.binance.com/robots.txt"
MAX_POLICY_BYTES = 128 * 1024
MAX_SITEMAP_BYTES = 512 * 1024
MAX_DISCOVERY_CANDIDATES = 2000
SAFE_REASON_CODES = {
    "robots_observed",
    "robots_disallow",
    "robots_default_allow",
    "robots_allow_specific",
    "policy_refresh_failed_fail_closed",
    "policy_cache_invalid_fail_closed",
    "policy_not_configured_fail_closed",
    "host_cooldown",
    "discovery_not_due",
    "discovery_operational",
    "discovery_disabled",
    "discovery_policy_paused",
    "discovery_fetch_failed",
}


def _utc(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("Crawler policy time must be timezone-aware")
    return current.astimezone(timezone.utc)


def _iso(value: datetime | None = None) -> str:
    return _utc(value).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class RobotsRule:
    allow: bool
    pattern: str

    @property
    def specificity(self) -> int:
        return len(self.pattern.replace("*", "").removesuffix("$"))

    def matches(self, path_and_query: str) -> bool:
        pattern = self.pattern
        anchored = pattern.endswith("$")
        if anchored:
            pattern = pattern[:-1]
        expression = "^" + re.escape(pattern).replace(r"\*", ".*")
        if anchored:
            expression += "$"
        return re.match(expression, path_and_query) is not None


@dataclass(frozen=True)
class RobotsPolicy:
    robots_url: str
    user_agent: str
    rules: tuple[RobotsRule, ...]
    fetched_at: str
    body_sha256: str

    @classmethod
    def parse(cls, body: bytes, *, robots_url: str, user_agent: str, fetched_at: str) -> "RobotsPolicy":
        text = body.decode("utf-8-sig", errors="strict")
        groups: list[tuple[list[str], list[RobotsRule]]] = []
        agents: list[str] = []
        rules: list[RobotsRule] = []
        for raw_line in text.splitlines():
            line = raw_line.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            key, value = (part.strip() for part in line.split(":", 1))
            key = key.lower()
            if key == "user-agent":
                if rules:
                    groups.append((agents, rules))
                    agents, rules = [], []
                agents.append(value.lower())
            elif key in {"allow", "disallow"} and agents:
                if value:
                    rules.append(RobotsRule(allow=key == "allow", pattern=value))
        if agents or rules:
            groups.append((agents, rules))
        product = user_agent.lower().split(None, 1)[0]
        product_name = product.split("/", 1)[0]

        def group_score(group_agents: list[str]) -> int | None:
            scores = []
            for agent in group_agents:
                normalized = agent.strip().lower()
                if normalized == "*":
                    scores.append(0)
                elif normalized and (
                    normalized == product
                    or normalized == product_name
                    or product.startswith(normalized + "/")
                ):
                    scores.append(len(normalized))
            return max(scores) if scores else None

        scored_groups = [
            (score, group_rules)
            for group_agents, group_rules in groups
            if (score := group_score(group_agents)) is not None
        ]
        if not scored_groups:
            raise ValueError("Robots policy had no applicable user-agent group")
        best_score = max(score for score, _rules in scored_groups)
        selected = tuple(
            rule for score, group_rules in scored_groups if score == best_score for rule in group_rules
        )
        return cls(
            robots_url=robots_url,
            user_agent=user_agent,
            rules=selected,
            fetched_at=fetched_at,
            body_sha256=hashlib.sha256(body).hexdigest(),
        )

    def check(self, url: str) -> tuple[bool, str]:
        policy_host = (urllib.parse.urlparse(self.robots_url).hostname or "").lower()
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "https" or (parsed.hostname or "").lower() != policy_host:
            return False, "robots_disallow"
        path_and_query = parsed.path or "/"
        if parsed.query:
            path_and_query += "?" + parsed.query
        matches = [rule for rule in self.rules if rule.matches(path_and_query)]
        if not matches:
            return True, "robots_default_allow"
        specificity = max(rule.specificity for rule in matches)
        winners = [rule for rule in matches if rule.specificity == specificity]
        allowed = any(rule.allow for rule in winners)
        return allowed, "robots_allow_specific" if allowed else "robots_disallow"


def _fail_closed(reason_code: str) -> Callable[[str], tuple[bool, str]]:
    return lambda _url: (False, reason_code)


def _robots_only(robots_url: str) -> Callable[[str], tuple[bool, str]]:
    def check(url: str) -> tuple[bool, str]:
        if url == robots_url:
            return True, "robots_default_allow"
        return False, "policy_cache_invalid_fail_closed"

    return check


def _validate_config(config: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(config, dict) or config.get("enabled") is not True:
        raise ValueError("Crawler policy is not enabled")
    robots_url = str(config.get("robots_url") or "")
    parsed = urllib.parse.urlparse(robots_url)
    if robots_url != DEFAULT_ROBOTS_URL or parsed.scheme != "https" or parsed.hostname != "www.binance.com":
        raise ValueError("Crawler policy must use the exact first-party robots URL")
    user_agent = str(config.get("user_agent") or "").strip()
    if user_agent != USER_AGENT:
        raise ValueError("Crawler policy user-agent must exactly match the HTTP client user-agent")
    refresh_seconds = int(config.get("refresh_seconds", 3600))
    if not 300 <= refresh_seconds <= 86400:
        raise ValueError("Crawler policy refresh_seconds must be between 300 and 86400")
    max_robots_bytes = int(config.get("max_robots_bytes", MAX_POLICY_BYTES))
    if not 1024 <= max_robots_bytes <= MAX_POLICY_BYTES:
        raise ValueError("Crawler policy robots byte limit is invalid")
    discovery = config.get("discovery")
    if not isinstance(discovery, dict) or discovery.get("enabled") is not True:
        raise ValueError("Crawler policy discovery lane is not enabled")
    index_url = str(discovery.get("index_url") or "")
    child_url = str(discovery.get("english_child_url") or "")
    expected_index = "https://www.binance.com/sitemap_output/domain=www.binance.com/sitemap_Square_index.xml"
    expected_child = "https://www.binance.com/sitemap_output/domain=www.binance.com/sitemap_Square_en_1.xml"
    if index_url != expected_index or child_url != expected_child:
        raise ValueError("Discovery must use the exact declared Square sitemap URLs")
    cadence_seconds = int(discovery.get("cadence_seconds", 86400))
    max_candidates = int(discovery.get("max_candidates", MAX_DISCOVERY_CANDIDATES))
    max_document_bytes = int(discovery.get("max_document_bytes", MAX_SITEMAP_BYTES))
    if not 3600 <= cadence_seconds <= 604800:
        raise ValueError("Discovery cadence must be between one hour and seven days")
    if not 1 <= max_candidates <= MAX_DISCOVERY_CANDIDATES:
        raise ValueError("Discovery candidate limit must be between 1 and 2000")
    if not 1024 <= max_document_bytes <= MAX_SITEMAP_BYTES:
        raise ValueError("Discovery document byte limit is invalid")
    return {
        "robots_url": robots_url,
        "user_agent": user_agent,
        "refresh_seconds": refresh_seconds,
        "max_robots_bytes": max_robots_bytes,
        "discovery": {
            "index_url": index_url,
            "english_child_url": child_url,
            "cadence_seconds": cadence_seconds,
            "max_candidates": max_candidates,
            "max_document_bytes": max_document_bytes,
        },
    }


def _upsert_policy_state(
    db_path: str | Path,
    *,
    robots_url: str,
    user_agent: str,
    status: str,
    reason_code: str,
    now: datetime,
    fetched: FetchResult | None = None,
    next_refresh_at: datetime | None = None,
    error_class: str | None = None,
) -> None:
    now_iso = _iso(now)
    with connect(db_path) as conn:
        conn.execute(
            """INSERT INTO crawler_policy_state(
                   id,robots_url,user_agent,status,reason_code,fetched_at,body_sha256,body_text,
                   last_attempt_at,last_success_at,next_refresh_at,error_class,created_at,updated_at
               ) VALUES(1,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                   robots_url=excluded.robots_url,user_agent=excluded.user_agent,status=excluded.status,
                   reason_code=excluded.reason_code,
                   fetched_at=COALESCE(excluded.fetched_at,crawler_policy_state.fetched_at),
                   body_sha256=COALESCE(excluded.body_sha256,crawler_policy_state.body_sha256),
                   body_text=COALESCE(excluded.body_text,crawler_policy_state.body_text),
                   last_attempt_at=excluded.last_attempt_at,
                   last_success_at=COALESCE(excluded.last_success_at,crawler_policy_state.last_success_at),
                   next_refresh_at=excluded.next_refresh_at,error_class=excluded.error_class,
                   updated_at=excluded.updated_at""",
            (
                robots_url,
                user_agent,
                status,
                reason_code,
                fetched.fetched_at if fetched else None,
                fetched.response_hash if fetched else None,
                fetched.body.decode("utf-8") if fetched else None,
                now_iso,
                fetched.fetched_at if fetched else None,
                _iso(next_refresh_at) if next_refresh_at else None,
                error_class,
                now_iso,
                now_iso,
            ),
        )
        conn.commit()


def _load_cached_policy(db_path: str | Path) -> tuple[RobotsPolicy | None, Any | None]:
    with connect(db_path) as conn:
        row = conn.execute("SELECT * FROM crawler_policy_state WHERE id=1").fetchone()
    if row is None or not row["body_text"] or not row["body_sha256"] or not row["fetched_at"]:
        return None, row
    body = row["body_text"].encode("utf-8")
    if hashlib.sha256(body).hexdigest() != row["body_sha256"]:
        return None, row
    try:
        policy = RobotsPolicy.parse(
            body,
            robots_url=row["robots_url"],
            user_agent=row["user_agent"],
            fetched_at=row["fetched_at"],
        )
    except (UnicodeError, ValueError):
        return None, row
    return policy, row


def _xml_locations(body: bytes) -> list[tuple[str, str | None]]:
    root = ET.fromstring(body)
    result: list[tuple[str, str | None]] = []
    for parent in root.iter():
        location = None
        lastmod = None
        for child in parent:
            kind = child.tag.rsplit("}", 1)[-1]
            if kind == "loc" and child.text:
                location = child.text.strip()
            elif kind == "lastmod" and child.text:
                lastmod = child.text.strip()
        if location:
            result.append((location, lastmod))
    return result


def _canonical_posts(entries: list[tuple[str, str | None]]) -> list[tuple[str, str | None]]:
    result: list[tuple[str, str | None]] = []
    seen: set[str] = set()
    for url, lastmod in entries:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "https" or parsed.hostname != "www.binance.com" or parsed.query or parsed.fragment:
            continue
        if not re.fullmatch(r"/en/square/post/[0-9]+", parsed.path):
            continue
        canonical = urllib.parse.urlunparse(("https", "www.binance.com", parsed.path, "", "", ""))
        if canonical not in seen:
            seen.add(canonical)
            result.append((canonical, lastmod))
    return result


def _set_discovery_state(
    db_path: str | Path,
    *,
    status: str,
    reason_code: str,
    now: datetime,
    started_at: str | None = None,
    completed_at: str | None = None,
    success_at: str | None = None,
    next_due_at: datetime | None = None,
    index_sha256: str | None = None,
    child_sha256: str | None = None,
    observed_url_count: int | None = None,
    candidate_count: int | None = None,
    truncated: bool | None = None,
    error_class: str | None = None,
) -> None:
    now_iso = _iso(now)
    with connect(db_path) as conn:
        conn.execute(
            """INSERT OR IGNORE INTO discovery_state(
                   id,status,reason_code,observed_url_count,canonical_candidate_count,truncated,created_at,updated_at
               ) VALUES(1,'not_started','discovery_not_due',0,0,0,?,?)""",
            (now_iso, now_iso),
        )
        conn.execute(
            """UPDATE discovery_state SET
                   status=?,reason_code=?,
                   last_started_at=COALESCE(?,last_started_at),
                   last_completed_at=COALESCE(?,last_completed_at),
                   last_success_at=COALESCE(?,last_success_at),
                   next_due_at=COALESCE(?,next_due_at),
                   index_sha256=COALESCE(?,index_sha256),
                   child_sha256=COALESCE(?,child_sha256),
                   observed_url_count=COALESCE(?,observed_url_count),
                   canonical_candidate_count=COALESCE(?,canonical_candidate_count),
                   truncated=COALESCE(?,truncated),error_class=?,updated_at=? WHERE id=1""",
            (
                status,
                reason_code,
                started_at,
                completed_at,
                success_at,
                _iso(next_due_at) if next_due_at else None,
                index_sha256,
                child_sha256,
                observed_url_count,
                candidate_count,
                int(truncated) if truncated is not None else None,
                error_class,
                now_iso,
            ),
        )
        conn.commit()


def _run_discovery(
    db_path: str | Path,
    config: dict[str, Any],
    coordinator: FetchCoordinator,
    *,
    now: datetime,
    fetcher: Callable[..., FetchResult],
) -> None:
    discovery = config["discovery"]
    with connect(db_path) as conn:
        state = conn.execute("SELECT * FROM discovery_state WHERE id=1").fetchone()
    next_due = _parse_time(state["next_due_at"]) if state else None
    if next_due is not None and next_due > now:
        _set_discovery_state(
            db_path, status="not_due", reason_code="discovery_not_due", now=now, next_due_at=next_due,
        )
        return

    started = _iso(now)
    _set_discovery_state(db_path, status="not_started", reason_code="discovery_not_due", now=now, started_at=started)
    try:
        index = fetcher(
            discovery["index_url"], retries=0, coordinator=coordinator,
            max_body_bytes=discovery["max_document_bytes"],
        )
        if index.status != 200 or index.url != discovery["index_url"]:
            raise ValueError("Sitemap index did not return the exact HTTP 200 resource")
        index_locations = _xml_locations(index.body)
        if discovery["english_child_url"] not in {url for url, _lastmod in index_locations}:
            raise ValueError("Declared English Square sitemap was absent from the index")
        child = fetcher(
            discovery["english_child_url"], retries=0, coordinator=coordinator,
            max_body_bytes=discovery["max_document_bytes"],
        )
        if child.status != 200 or child.url != discovery["english_child_url"]:
            raise ValueError("Sitemap child did not return the exact HTTP 200 resource")
        entries = _xml_locations(child.body)
        canonical = _canonical_posts(entries)
        truncated = len(canonical) > discovery["max_candidates"]
        selected = canonical[: discovery["max_candidates"]]
        observed_at = child.fetched_at
        with connect(db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            for canonical_url, lastmod in selected:
                conn.execute(
                    """INSERT INTO discovery_candidates(
                           canonical_url,queue_status,original_published_at,author,quality_status,
                           first_observed_at,last_observed_at,created_at,updated_at
                       ) VALUES(?,'pending',NULL,NULL,'unknown',?,?,?,?)
                       ON CONFLICT(canonical_url) DO UPDATE SET
                           last_observed_at=excluded.last_observed_at,updated_at=excluded.updated_at""",
                    (canonical_url, observed_at, observed_at, observed_at, observed_at),
                )
                candidate_id = conn.execute(
                    "SELECT id FROM discovery_candidates WHERE canonical_url=?", (canonical_url,)
                ).fetchone()[0]
                conn.execute(
                    """INSERT OR IGNORE INTO discovery_observations(
                           candidate_id,source_url,source_sha256,observed_at,lastmod
                       ) VALUES(?,?,?,?,?)""",
                    (candidate_id, discovery["english_child_url"], child.response_hash, observed_at, lastmod),
                )
            conn.commit()
        _set_discovery_state(
            db_path,
            status="operational",
            reason_code="discovery_operational",
            now=now,
            completed_at=_iso(now),
            success_at=observed_at,
            next_due_at=now + timedelta(seconds=discovery["cadence_seconds"]),
            index_sha256=index.response_hash,
            child_sha256=child.response_hash,
            observed_url_count=len(entries),
            candidate_count=len(selected),
            truncated=truncated,
        )
    except AccessStopped as exc:
        with connect(db_path) as conn:
            _record_host_cooldown(conn, "www.binance.com", exc.status_code, exc.retry_after_seconds, now)
            conn.commit()
        _set_discovery_state(
            db_path, status="error", reason_code="discovery_fetch_failed", now=now,
            completed_at=_iso(now), next_due_at=now + timedelta(hours=24), error_class=type(exc).__name__,
        )
    except Exception as exc:
        reason = "discovery_policy_paused" if isinstance(exc, PolicyPaused) else "discovery_fetch_failed"
        _set_discovery_state(
            db_path, status="policy_paused" if reason == "discovery_policy_paused" else "error",
            reason_code=reason, now=now, completed_at=_iso(now),
            next_due_at=now + timedelta(hours=24), error_class=type(exc).__name__,
        )


def prepare_crawler_cycle(
    db_path: str | Path,
    raw_config: dict[str, Any] | None,
    coordinator: FetchCoordinator,
    *,
    now: datetime | None = None,
    fetcher: Callable[..., FetchResult] = fetch_public,
) -> dict[str, Any]:
    """Refresh policy, install the request gate, then run allowed discovery only."""
    init_db(db_path)
    current = _utc(now)
    try:
        config = _validate_config(raw_config)
    except (TypeError, ValueError):
        coordinator.set_policy_checker(_fail_closed("policy_not_configured_fail_closed"))
        _upsert_policy_state(
            db_path,
            robots_url=DEFAULT_ROBOTS_URL,
            user_agent="*",
            status="not_configured",
            reason_code="policy_not_configured_fail_closed",
            now=current,
        )
        _set_discovery_state(
            db_path, status="disabled", reason_code="discovery_disabled", now=current,
        )
        return public_crawler_status(db_path)

    with connect(db_path) as conn:
        cooldown_until, _status = _persisted_host_cooldown(conn, "www.binance.com", current)
    if cooldown_until is not None:
        coordinator.set_policy_checker(_fail_closed("host_cooldown"))
        _upsert_policy_state(
            db_path,
            robots_url=config["robots_url"],
            user_agent=config["user_agent"],
            status="cooldown",
            reason_code="host_cooldown",
            now=current,
            next_refresh_at=cooldown_until,
        )
        _set_discovery_state(
            db_path, status="policy_paused", reason_code="host_cooldown", now=current,
            next_due_at=cooldown_until,
        )
        return public_crawler_status(db_path)

    cached_policy, cached_row = _load_cached_policy(db_path)
    cached_enforced = cached_row is not None and cached_row["status"] == "enforced"
    refresh_at = _parse_time(cached_row["next_refresh_at"]) if cached_row else None
    refresh_due = cached_policy is None or cached_row is None or refresh_at is None or refresh_at <= current
    policy = cached_policy if cached_enforced else None
    if not cached_enforced and refresh_at is not None and refresh_at > current:
        refresh_due = False
    if refresh_due:
        coordinator.set_policy_checker(_robots_only(config["robots_url"]))
        try:
            fetched = fetcher(
                config["robots_url"], retries=0, coordinator=coordinator,
                max_body_bytes=config["max_robots_bytes"],
            )
            if fetched.status != 200 or fetched.url != config["robots_url"]:
                raise ValueError("Robots refresh did not return the exact HTTP 200 resource")
            policy = RobotsPolicy.parse(
                fetched.body,
                robots_url=config["robots_url"],
                user_agent=config["user_agent"],
                fetched_at=fetched.fetched_at,
            )
            _upsert_policy_state(
                db_path,
                robots_url=config["robots_url"],
                user_agent=config["user_agent"],
                status="enforced",
                reason_code="robots_observed",
                now=current,
                fetched=fetched,
                next_refresh_at=current + timedelta(seconds=config["refresh_seconds"]),
            )
        except AccessStopped as exc:
            with connect(db_path) as conn:
                _record_host_cooldown(conn, "www.binance.com", exc.status_code, exc.retry_after_seconds, current)
                conn.commit()
            policy = None
            _upsert_policy_state(
                db_path,
                robots_url=config["robots_url"],
                user_agent=config["user_agent"],
                status="refresh_failed",
                reason_code="policy_refresh_failed_fail_closed",
                now=current,
                next_refresh_at=current + timedelta(hours=24),
                error_class=type(exc).__name__,
            )
        except Exception as exc:
            policy = None
            _upsert_policy_state(
                db_path,
                robots_url=config["robots_url"],
                user_agent=config["user_agent"],
                status="refresh_failed",
                reason_code="policy_refresh_failed_fail_closed",
                now=current,
                next_refresh_at=current + timedelta(hours=1),
                error_class=type(exc).__name__,
            )

    if policy is None:
        coordinator.set_policy_checker(_fail_closed("policy_refresh_failed_fail_closed"))
        _set_discovery_state(
            db_path, status="policy_paused", reason_code="discovery_policy_paused", now=current,
            next_due_at=current + timedelta(hours=1),
        )
        return public_crawler_status(db_path)

    coordinator.set_policy_checker(policy.check)
    _run_discovery(db_path, config, coordinator, now=current, fetcher=fetcher)
    return public_crawler_status(db_path)


def public_crawler_status(db_path: str | Path) -> dict[str, Any]:
    try:
        with connect(db_path) as conn:
            policy = conn.execute("SELECT * FROM crawler_policy_state WHERE id=1").fetchone()
            discovery = conn.execute("SELECT * FROM discovery_state WHERE id=1").fetchone()
            pending = int(conn.execute(
                "SELECT COUNT(*) FROM discovery_candidates WHERE queue_status='pending'"
            ).fetchone()[0])
            candidates = int(conn.execute("SELECT COUNT(*) FROM discovery_candidates").fetchone()[0])
    except sqlite3.OperationalError:
        policy = None
        discovery = None
        pending = 0
        candidates = 0
    policy_status = policy["status"] if policy else "not_checked"
    policy_reason = policy["reason_code"] if policy and policy["reason_code"] in SAFE_REASON_CODES else "policy_cache_invalid_fail_closed"
    discovery_status = discovery["status"] if discovery else "not_started"
    discovery_reason = discovery["reason_code"] if discovery and discovery["reason_code"] in SAFE_REASON_CODES else "discovery_not_due"
    raw_api_status = "not_checked"
    if policy_status == "enforced":
        cached_policy, _row = _load_cached_policy(db_path)
        if cached_policy is None:
            raw_api_status = "fail_closed"
        else:
            raw_allowed, _reason = cached_policy.check(
                "https://www.binance.com/bapi/composite/v3/friendly/pgc/content/example"
            )
            raw_api_status = "allowed" if raw_allowed else "policy_paused"
    elif policy_status != "not_checked":
        raw_api_status = "fail_closed"
    return {
        "policy": {
            "status": policy_status,
            "reason": policy_reason,
            "observed_at": policy["fetched_at"] if policy else None,
            "robots_sha256": policy["body_sha256"] if policy else None,
            "raw_api_collection": raw_api_status,
        },
        "discovery": {
            "status": discovery_status,
            "reason": discovery_reason,
            "last_success_at": discovery["last_success_at"] if discovery else None,
            "next_due_at": discovery["next_due_at"] if discovery else None,
            "observed_url_count": int(discovery["observed_url_count"]) if discovery else 0,
            "candidate_count": candidates,
            "pending_count": pending,
            "truncated": bool(discovery["truncated"]) if discovery else False,
            "definition": "URL sitemap là ứng viên khám phá, không phải bài đã xác minh; ngày gốc, tác giả và chất lượng vẫn unknown cho đến khi có bằng chứng được phép.",
        },
    }

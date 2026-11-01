from __future__ import annotations

import hashlib
import html
import json
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Iterable

from .db import connect, init_db, public_excerpt, utc_now
from .research import research_brief, store_source_claim

USER_AGENT = "BinanceTraderIntelligence/0.1 (+public evidence research; no trading)"
MAX_BODY_BYTES = 2 * 1024 * 1024
DEFAULT_TIMEOUT = 15
RETRYABLE_STATUS = {500, 502, 503, 504}
STOP_STATUS = {401, 403, 429}
ALLOWED_HOSTS = {"www.binance.com", "binance.com"}
ARCHIVE_HOST = "web.archive.org"
HOST_COOLDOWN_PREFIX = "host_cooldown:"


class BodyTooLarge(RuntimeError):
    pass


class AccessStopped(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None, retry_after_seconds: int | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds


class SourceSuppressed(RuntimeError):
    """A persisted permission revocation overrides a public manifest entry."""

    pass


class PolicyPaused(RuntimeError):
    """The configured first-party crawler policy forbids this request."""

    def __init__(self, reason_code: str):
        super().__init__(reason_code)
        self.reason_code = reason_code


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Return 3xx to the caller; policy must authorize each target before any request."""

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


def _retry_after_seconds(value: str | None) -> int | None:
    if not value:
        return None
    try:
        seconds = int(value)
        return max(0, min(seconds, 86400))
    except ValueError:
        try:
            moment = parsedate_to_datetime(value)
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
            return max(0, min(int((moment - datetime.now(timezone.utc)).total_seconds()), 86400))
        except (TypeError, ValueError, OverflowError):
            return None


class FetchCoordinator:
    """One per cycle: bounded lanes, one Binance-host start limiter, one stop circuit."""

    def __init__(
        self,
        concurrency: int = 2,
        minimum_spacing_seconds: float = 1.5,
        policy_checker: Callable[[str], tuple[bool, str]] | None = None,
    ):
        if not 1 <= int(concurrency) <= 3:
            raise ValueError("Fetch concurrency must be between 1 and 3")
        if float(minimum_spacing_seconds) < 1.0:
            raise ValueError("Per-host spacing must be at least one second")
        self.concurrency = int(concurrency)
        self.minimum_spacing_seconds = float(minimum_spacing_seconds)
        self._limiter_lock = threading.Lock()
        self._circuit_lock = threading.Lock()
        self._next_start: dict[str, float] = {}
        self._circuit_until = 0.0
        self._circuit_status: int | None = None

        self._policy_checker = policy_checker

    def set_policy_checker(self, checker: Callable[[str], tuple[bool, str]] | None) -> None:
        self._policy_checker = checker

    @property
    def policy_controlled(self) -> bool:
        return self._policy_checker is not None

    def before_request(self, host: str, url: str | None = None) -> None:
        if self._policy_checker is not None:
            if url is None:
                raise PolicyPaused("policy_url_missing_fail_closed")
            allowed, reason_code = self._policy_checker(url)
            if not allowed:
                raise PolicyPaused(reason_code)
        with self._limiter_lock:
            with self._circuit_lock:
                if time.monotonic() < self._circuit_until:
                    raise AccessStopped(
                        f"Shared fetch circuit is cooling down after HTTP {self._circuit_status}",
                        status_code=self._circuit_status,
                    )
            now = time.monotonic()
            wait_for = self._next_start.get(host, now) - now
            if wait_for > 0:
                time.sleep(wait_for)
            self._next_start[host] = time.monotonic() + self.minimum_spacing_seconds
            with self._circuit_lock:
                if time.monotonic() < self._circuit_until:
                    raise AccessStopped(
                        f"Shared fetch circuit is cooling down after HTTP {self._circuit_status}",
                        status_code=self._circuit_status,
                    )

    def trip(self, status_code: int, retry_after_seconds: int | None = None) -> None:
        default = 21600 if status_code == 429 else 86400
        delay = max(default, retry_after_seconds or 0)
        with self._circuit_lock:
            self._circuit_status = status_code
            self._circuit_until = max(self._circuit_until, time.monotonic() + min(delay, 86400))


class TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._ignored = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript", "svg"}:
            self._ignored += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "svg"} and self._ignored:
            self._ignored -= 1

    def handle_data(self, data: str) -> None:
        if not self._ignored:
            self.parts.append(data)


@dataclass
class FetchResult:
    url: str
    status: int
    content_type: str
    body: bytes
    latency_ms: int
    fetched_at: str

    @property
    def response_hash(self) -> str:
        return hashlib.sha256(self.body).hexdigest()


@dataclass
class ParsedItem:
    external_id: str
    canonical_url: str
    title: str
    text: str
    author: str | None
    username: str | None
    published_at: str | None
    language: str | None
    metadata: dict[str, Any]
    entity_category: str


def compact_text(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:80] or hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def iso_from_epoch(value: Any) -> str | None:
    if value is None:
        return None
    try:
        number = float(value)
        if number > 10_000_000_000:
            number /= 1000
        return datetime.fromtimestamp(number, tz=timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    except (TypeError, ValueError, OSError):
        return None


def parse_utc_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Timestamp must include a UTC offset")
    return parsed.astimezone(timezone.utc)


def minus_calendar_years(value: datetime, years: int) -> datetime:
    try:
        return value.replace(year=value.year - years)
    except ValueError:
        return value.replace(year=value.year - years, day=28)


def structured_text(value: Any) -> str:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            parser = TextExtractor()
            parser.feed(value)
            return compact_text(" ".join(parser.parts)) or compact_text(value)
    parts: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, item in node.items():
                if key in {"text", "value"} and isinstance(item, str):
                    parts.append(item)
                elif key not in {"attr", "style", "href", "src"}:
                    walk(item)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(value)
    return compact_text(" ".join(parts))


def load_manifest_document(path: str | Path) -> dict[str, Any]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("Manifest root must be an object")
    sources = raw.get("sources")
    if not isinstance(sources, list):
        raise ValueError("Manifest must contain a sources list")
    return raw


def load_manifest(path: str | Path) -> list[dict[str, Any]]:
    raw = load_manifest_document(path)
    sources = raw["sources"]
    return sources


def validate_source(source: dict[str, Any]) -> None:
    for field in ("slug", "name", "url", "fetch_url", "source_type", "adapter", "access_level", "distribution"):
        if not source.get(field):
            raise ValueError(f"Source missing {field}")
    for field in ("url", "fetch_url"):
        parsed = urllib.parse.urlparse(source[field])
        archive_source = source.get("source_type") == "binance_square_public_archive"
        allowed_host = parsed.hostname in ALLOWED_HOSTS or (
            archive_source and parsed.hostname == ARCHIVE_HOST
        )
        if parsed.scheme != "https" or not allowed_host:
            raise ValueError(f"Source URL is outside the HTTPS allowlist: {source[field]}")
        if archive_source and parsed.hostname == ARCHIVE_HOST and not (
            parsed.path == "/cdx/search/cdx" or re.match(
                r"^/web/[0-9]{14}id_/https://www\.binance\.com/en/square/(?:post|profile)/",
                parsed.path,
            )
        ):
            raise ValueError("Archive source URL is outside the bounded Square archive routes")


def seed_sources(db_path: str | Path, manifest_path: str | Path) -> int:
    sources = load_manifest(manifest_path)
    return seed_source_configs(db_path, sources)


def seed_source_configs(db_path: str | Path, sources: Iterable[dict[str, Any]]) -> int:
    init_db(db_path)
    source_list = list(sources)
    now = utc_now()
    with connect(db_path) as conn:
        for source in source_list:
            validate_source(source)
            conn.execute(
                """
                INSERT INTO sources(slug,name,url,fetch_url,source_type,adapter,access_level,
                    distribution,expected_pillar,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(slug) DO UPDATE SET
                    name=excluded.name,url=excluded.url,fetch_url=excluded.fetch_url,
                    source_type=excluded.source_type,adapter=excluded.adapter,
                    expected_pillar=excluded.expected_pillar,updated_at=excluded.updated_at
                """,
                (
                    source["slug"], source["name"], source["url"], source["fetch_url"],
                    source["source_type"], source["adapter"], source["access_level"],
                    source["distribution"], source.get("expected_pillar"), now, now,
                ),
            )
        conn.commit()
    return len(source_list)


def _as_utc(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("Host cooldown time must be timezone-aware")
    return current.astimezone(timezone.utc)


def _source_host(source: dict[str, Any]) -> str:
    return (urllib.parse.urlparse(str(source["fetch_url"])).hostname or "").lower()


def _persisted_host_cooldown(conn: Any, host: str, current: datetime) -> tuple[datetime | None, int | None]:
    until: datetime | None = None
    status: int | None = None
    row = conn.execute("SELECT value FROM schema_meta WHERE key=?", (HOST_COOLDOWN_PREFIX + host,)).fetchone()
    if row:
        try:
            payload = json.loads(row["value"])
            candidate = parse_utc_timestamp(str(payload.get("until") or ""))
            if candidate > current:
                until, status = candidate, int(payload["status_code"])
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    for schedule in conn.execute(
        """SELECT source.fetch_url,schedule.cooldown_until FROM source_schedule schedule
           JOIN sources source ON source.id=schedule.source_id
           WHERE schedule.last_outcome='blocked' AND schedule.cooldown_until IS NOT NULL"""
    ).fetchall():
        if (urllib.parse.urlparse(schedule["fetch_url"]).hostname or "").lower() != host:
            continue
        try:
            candidate = parse_utc_timestamp(schedule["cooldown_until"])
        except (TypeError, ValueError):
            continue
        if candidate > current and (until is None or candidate > until):
            until = candidate
    return until, status


def _record_host_cooldown(
    conn: Any,
    host: str,
    status_code: int | None,
    retry_after_seconds: int | None,
    current: datetime,
) -> None:
    if status_code not in STOP_STATUS:
        return
    default = 6 * 3600 if status_code == 429 else 24 * 3600
    delay = max(default, min(max(retry_after_seconds or 0, 0), 86400))
    candidate = current + timedelta(seconds=delay)
    existing, _ = _persisted_host_cooldown(conn, host, current)
    until = max(candidate, existing) if existing is not None else candidate
    payload = json.dumps(
        {
            "host": host,
            "status_code": status_code,
            "until": until.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    conn.execute(
        """INSERT INTO schema_meta(key,value) VALUES(?,?)
           ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
        (HOST_COOLDOWN_PREFIX + host, payload),
    )


def fetch_public(
    url: str,
    *,
    method: str = "GET",
    json_body: dict[str, Any] | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    retries: int = 1,
    coordinator: FetchCoordinator | None = None,
    max_body_bytes: int = MAX_BODY_BYTES,
) -> FetchResult:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS:
        raise ValueError("Fetch URL is outside the public Binance HTTPS allowlist")
    if not 1 <= int(max_body_bytes) <= MAX_BODY_BYTES:
        raise ValueError(f"Fetch body limit must be between 1 and {MAX_BODY_BYTES} bytes")
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        if coordinator is not None:
            coordinator.before_request(parsed.hostname or "binance.com", url)
        started = time.perf_counter()
        verb = method.upper()
        if verb not in {"GET", "POST"}:
            raise ValueError("Public fetch only supports GET or POST")
        body = json.dumps(json_body or {}, separators=(",", ":")).encode("utf-8") if verb == "POST" else None
        request = urllib.request.Request(
            url,
            data=body,
            method=verb,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json,text/html;q=0.8",
                "Referer": "https://www.binance.com/en/square/trending",
                **({"Content-Type": "application/json"} if verb == "POST" else {}),
            },
        )
        try:
            open_request = (
                urllib.request.build_opener(_NoRedirect()).open
                if coordinator is not None and coordinator.policy_controlled
                else urllib.request.urlopen
            )
            with open_request(request, timeout=timeout) as response:
                final = urllib.parse.urlparse(response.geturl())
                if final.scheme != "https" or final.hostname not in ALLOWED_HOSTS:
                    raise AccessStopped("Redirect left the Binance host allowlist")
                body = response.read(max_body_bytes + 1)
                if len(body) > max_body_bytes:
                    raise BodyTooLarge(f"Response exceeded {max_body_bytes} bytes")
                result = FetchResult(
                    url=response.geturl(),
                    status=int(response.status),
                    content_type=response.headers.get("Content-Type", ""),
                    body=body,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    fetched_at=utc_now(),
                )
                if result.status in STOP_STATUS:
                    if coordinator is not None:
                        coordinator.trip(result.status)
                    raise AccessStopped(f"Access stopped on HTTP {result.status}", status_code=result.status)
                if result.status in RETRYABLE_STATUS and attempt < retries:
                    time.sleep(1 + attempt)
                    continue
                return result
        except urllib.error.HTTPError as exc:
            if exc.code in STOP_STATUS:
                retry_after = _retry_after_seconds(exc.headers.get("Retry-After") if exc.headers else None)
                if coordinator is not None:
                    coordinator.trip(exc.code, retry_after)
                raise AccessStopped(
                    f"Access stopped on HTTP {exc.code}", status_code=exc.code,
                    retry_after_seconds=retry_after,
                ) from exc
            last_error = exc
            if exc.code not in RETRYABLE_STATUS or attempt >= retries:
                raise
        except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
            last_error = exc
            if attempt >= retries:
                raise
        if attempt < retries:
            time.sleep(1 + attempt)
    raise RuntimeError(str(last_error or "fetch failed"))


def _data_from_json(result: FetchResult) -> Any:
    payload = json.loads(result.body.decode("utf-8"))
    if payload.get("code") != "000000" or payload.get("data") is None:
        raise ValueError(f"Binance response code was {payload.get('code')!r}")
    return payload["data"]


def parse_square_feed(result: FetchResult, source: dict[str, Any]) -> list[ParsedItem]:
    data = _data_from_json(result)
    rows = data.get("vos", []) if isinstance(data, dict) else []
    items: list[ParsedItem] = []
    max_items = max(1, min(int(source.get("max_items", 8)), 12))
    for row in rows:
        if source.get("exclude_official", True) and (row.get("sourceType") == 4 or row.get("authorRole") == 7):
            continue
        title = compact_text(row.get("title"))
        subtitle = compact_text(row.get("subTitle"))
        content = compact_text(row.get("content"))
        ai_summary = compact_text(row.get("aiSummary"))
        text = compact_text(" ".join(part for part in (title, subtitle, content, ai_summary) if part))
        if len(text) < 40:
            continue
        author = compact_text(row.get("authorName")) or "Tác giả Square chưa rõ"
        username = compact_text(row.get("username")) or None
        external_id = str(row.get("id") or hashlib.sha256(text.encode("utf-8")).hexdigest()[:24])
        canonical_url = row.get("webLink") or f"https://www.binance.com/en/square/post/{external_id}"
        engagement = {
            "views": row.get("viewCount"),
            "likes": row.get("likeCount"),
            "comments": row.get("commentCount"),
            "shares": row.get("shareCount"),
            "replies": row.get("replyCount"),
            "followers": None,
            "denominator": "views" if row.get("viewCount") is not None else None,
        }
        metadata = {
            "engagement": engagement,
            "author_verified": bool(row.get("authorIsVerified")),
            "author_verification_type": row.get("authorVerificationType"),
            "source_type": row.get("sourceType"),
            "content_type": row.get("contentType"),
            "portfolio_performance": row.get("portfolioPerformance"),
            "discovery": "public_square_feed",
            "content_origin": "creator_post",
        }
        items.append(
            ParsedItem(
                external_id=external_id,
                canonical_url=canonical_url,
                title=title or text[:120],
                text=text,
                author=author,
                username=username,
                published_at=iso_from_epoch(row.get("date")),
                language=row.get("language") or row.get("detectedLanguage"),
                metadata=metadata,
                entity_category="square_creator_candidate",
            )
        )
        if len(items) >= max_items:
            break
    if not items:
        raise ValueError("Square feed returned no substantive non-official items")
    return items


def parse_square_detail(result: FetchResult, source: dict[str, Any]) -> list[ParsedItem]:
    row = _data_from_json(result)
    raw_title = compact_text(row.get("title"))
    derived_title = compact_text(source.get("derived_title"))
    body_text = compact_text(row.get("bodyTextOnly")) or structured_text(row.get("body"))
    text = compact_text(" ".join(filter(None, [raw_title or derived_title, body_text, compact_text(row.get("subTitle"))])))
    if not text:
        raise ValueError("Square detail had no readable text")
    external_id = str(row.get("id") or source["slug"])
    published_at = iso_from_epoch(row.get("firstReleaseTime") or row.get("createTime") or row.get("date"))
    if source.get("historical_backfill"):
        expected_post_id = str(source.get("expected_post_id") or "")
        expected_uid = str(source.get("expected_source_uid") or "")
        actual_uid = compact_text(row.get("squareUid"))
        minimum_text_chars = max(200, int(source.get("minimum_text_chars", 1000)))
        if external_id != expected_post_id:
            raise ValueError("Historical detail post ID did not match the configured canonical seed")
        if not expected_uid or actual_uid != expected_uid:
            raise ValueError("Historical detail author UID did not match the configured identity")
        if int(row.get("contentType") or 0) != int(source.get("required_content_type", 2)):
            raise ValueError("Historical detail was not a substantive article content type")
        if len(body_text) < minimum_text_chars or not (raw_title or derived_title):
            raise ValueError("Historical detail was below the substantive article threshold")
        if not published_at:
            raise ValueError("Historical detail had no original release timestamp")
        earliest = str(source.get("earliest_published_at") or "")
        latest = str(source.get("latest_published_at") or "")
        try:
            observed = parse_utc_timestamp(result.fetched_at)
            configured_start = parse_utc_timestamp(earliest)
            configured_end = parse_utc_timestamp(latest)
            released = parse_utc_timestamp(published_at)
        except (TypeError, ValueError) as exc:
            raise ValueError("Historical detail timestamps were not valid UTC timestamps") from exc
        effective_start = max(configured_start, minus_calendar_years(observed, 2))
        effective_end = min(configured_end, observed)
        if effective_start > effective_end or not (effective_start <= released <= effective_end):
            raise ValueError("Historical detail release timestamp was outside the effective rolling two-year window")
    return [ParsedItem(
        external_id=external_id,
        canonical_url=source["url"],
        title=raw_title or derived_title or source["name"],
        text=text,
        author=compact_text(row.get("authorName")) or source.get("entity", {}).get("display_name"),
        username=compact_text(row.get("username")) or source.get("entity", {}).get("username"),
        published_at=published_at,
        language=row.get("language") or row.get("detectedLanguage"),
        metadata={
            "engagement": {"views": row.get("viewCount"), "likes": row.get("likeCount"), "comments": row.get("commentCount"), "shares": row.get("shareCount"), "followers": None},
            "official_detail": source.get("source_type") == "binance_official_program",
            "content_origin": "creator_post" if source.get("historical_backfill") else "source_detail",
            "source_uid": compact_text(row.get("squareUid")) or None,
            "content_type": row.get("contentType"),
            "historical_seed": bool(source.get("historical_backfill")),
            "coverage_window_start": effective_start.replace(microsecond=0).isoformat().replace("+00:00", "Z") if source.get("historical_backfill") else None,
            "coverage_window_end": effective_end.replace(microsecond=0).isoformat().replace("+00:00", "Z") if source.get("historical_backfill") else None,
        },
        entity_category=source.get("entity", {}).get("category", "official_program"),
    )]


def parse_cms_detail(result: FetchResult, source: dict[str, Any]) -> list[ParsedItem]:
    row = _data_from_json(result)
    title = compact_text(row.get("title")) or source["name"]
    text = compact_text(f"{title} {structured_text(row.get('body'))}")
    if not text:
        raise ValueError("CMS detail had no readable text")
    return [ParsedItem(
        external_id=str(row.get("code") or row.get("id") or source["slug"]),
        canonical_url=source["url"],
        title=title,
        text=text,
        author="Binance",
        username=None,
        published_at=iso_from_epoch(row.get("releaseDate")),
        language=row.get("language") or "en",
        metadata={"official_detail": True, "catalog_id": row.get("catalogId")},
        entity_category=source.get("entity", {}).get("category", "official_program"),
    )]


def parse_html(result: FetchResult, source: dict[str, Any]) -> list[ParsedItem]:
    if not result.body:
        raise AccessStopped(f"HTML endpoint returned HTTP {result.status} with an empty body")
    decoded = result.body.decode("utf-8", "replace")
    if re.search(r"captcha|human verification|access denied", decoded, re.I):
        raise AccessStopped("HTML endpoint presented an access-control challenge")
    parser = TextExtractor()
    parser.feed(decoded)
    text = compact_text(" ".join(parser.parts))
    if len(text) < 80:
        raise ValueError("HTML endpoint had no substantive readable text")
    title_match = re.search(r"<title[^>]*>(.*?)</title>", decoded, re.I | re.S)
    title = compact_text(title_match.group(1)) if title_match else source["name"]
    return [ParsedItem(source["slug"], source["url"], title, text, None, None, None, None, {}, "source_page")]


def parse_award_winners(result: FetchResult, source: dict[str, Any]) -> list[ParsedItem]:
    """Project official Blockchain 100 winner/nominee payloads into auditable recognition records across categories."""
    data = _data_from_json(result)
    groups = data.get("groupList", []) if isinstance(data, dict) else []
    include_categories = source.get("include_categories")
    include_all = bool(source.get("include_all_categories")) or source.get("category_name") == "ALL"
    if isinstance(include_categories, list) and include_categories:
        allowed_types = {int(c) for c in include_categories if str(c).lstrip("-").isdigit()}
        allowed_names = {str(c).casefold() for c in include_categories if not str(c).lstrip("-").isdigit()}
        selected_groups = [
            g for g in groups
            if int(g.get("categoryType") or 0) in allowed_types
            or str(g.get("name") or "").casefold() in allowed_names
        ]
        if not selected_groups:
            raise ValueError(f"Official winner payload did not contain requested categories {include_categories!r}")
    elif include_all:
        selected_groups = [g for g in groups if isinstance(g.get("winners"), list) and g.get("winners")]
        if not selected_groups:
            raise ValueError("Official winner payload did not contain any non-empty categories")
    else:
        target_name = source.get("category_name", "Trader")
        target_type = int(source.get("category_type", 3))
        group = next(
            (candidate for candidate in groups if candidate.get("categoryType") == target_type and candidate.get("name") == target_name),
            None,
        )
        if not group:
            raise ValueError(f"Official winner payload did not contain {target_name!r} category")
        selected_groups = [group]

    total_winners = sum(len(g.get("winners") or []) for g in selected_groups)
    if total_winners == 0:
        raise ValueError("Official winner category was empty")
    expected = source.get("expected_count")
    if expected is not None and total_winners != int(expected):
        raise ValueError(f"Official winner count changed: expected {expected}, received {total_winners}")
    expected_cat_counts = source.get("expected_category_counts")
    if isinstance(expected_cat_counts, dict):
        for grp in selected_groups:
            gname = compact_text(grp.get("name"))
            if gname in expected_cat_counts and len(grp.get("winners") or []) != int(expected_cat_counts[gname]):
                raise ValueError(
                    f"Official winner count for {gname!r} changed: expected {expected_cat_counts[gname]}, received {len(grp.get('winners') or [])}"
                )

    program_name = str(source.get("program_name") or "Blockchain 100 2025")
    period_label = str(source.get("period_label") or ("2026" if "2026" in program_name else "2025"))
    award_result = str(source.get("award_result") or "Awardee")
    is_operator_capture = "public-browser-capture" in (result.content_type or "").casefold()
    is_nominee_voting = award_result.casefold() in {"nominee", "voting", "candidate"}
    items: list[ParsedItem] = []
    seen_uids: set[str] = set()
    for group in selected_groups:
        group_name = compact_text(group.get("name")) or str(source.get("category_name", "Trader"))
        group_type = int(group.get("categoryType") or source.get("category_type", 3))
        is_trader_category = (group_type == 3 and group_name == "Trader")
        category_slug = re.sub(r"[^a-z0-9]+", "-", group_name.casefold()).strip("-") or "creator"
        winners = group.get("winners") or []
        for winner in winners:
            source_uid = compact_text(winner.get("squareUid"))
            username = compact_text(winner.get("username"))
            display_name = compact_text(winner.get("displayName")) or username
            if not source_uid or not username or source_uid in seen_uids:
                raise ValueError("Official winner record had a missing or duplicate identity")
            seen_uids.add(source_uid)
            rank = winner.get("rank")
            vote_count = winner.get("voteCount")
            bio = compact_text(winner.get("biography")) or None
            if is_nominee_voting:
                title = f"{program_name} — {group_name} nominee snapshot: {display_name}"
                text = (
                    f"{program_name} voting snapshot ({result.fetched_at}) lists nominee {display_name} (@{username}) "
                    f"in the {group_name} category at rank {rank}, with {vote_count} community votes."
                )
                recognition = None
                ext_prefix = f"blockchain100-{period_label}-nominee-{category_slug}"
            else:
                title = f"{program_name} — {group_name} awardee: {display_name}"
                text = (
                    f"Official {program_name} record lists {display_name} (@{username}) "
                    f"in the {group_name} category at rank {rank}, with {vote_count} community award votes."
                )
                interpretation_limit = (
                    "Danh mục có tên Trader nhưng đây là công nhận content/influence do cộng đồng bình chọn; "
                    "không phải PnL được kiểm toán hay bằng chứng giao dịch có lãi."
                    if is_trader_category else
                    f"Hạng mục {group_name} thuộc chương trình {program_name} là công nhận nội dung/cộng đồng do bình chọn; "
                    "không phải PnL được kiểm toán hay bằng chứng giao dịch có lãi."
                )
                if is_operator_capture:
                    interpretation_limit += " operator-supplied, capture provenance not independently authenticated."
                recognition = {
                    "program_name": program_name,
                    "category_type": "content",
                    "category_name": group_name,
                    "result": award_result,
                    "criteria": "Community-voted recognition for content and influence in the Binance Square ecosystem.",
                    "period_label": period_label,
                    "recognized_at": None,
                    "quote": f"{group_name} awardee, rank {rank}",
                    "rank": rank,
                    "award_vote_count": vote_count,
                    "interpretation_limit": interpretation_limit,
                }
                ext_prefix = f"blockchain100-{period_label}-trader" if is_trader_category else f"blockchain100-{period_label}-{category_slug}"
            items.append(ParsedItem(
                external_id=f"{ext_prefix}:{source_uid}",
                canonical_url=source["url"],
                title=title,
                text=text,
                author=display_name,
                username=username,
                published_at=None,
                language="en",
                metadata={
                    "source_uid": source_uid,
                    "identity_status": "source_profile_only" if (is_operator_capture or is_nominee_voting) else "official_award_payload_uid",
                    "identity_evidence_url": source["url"],
                    "bio": bio,
                    "award_category_name": group_name,
                    "award_category_type": group_type,
                    "award_rank": rank,
                    "award_vote_count": vote_count,
                    "award_vote_semantics": "community_nominee_votes_snapshot" if is_nominee_voting else "community_award_votes",
                    "share_link": winner.get("shareLink"),
                    "recognition": recognition,
                    "recognition_verification_level": "operator_capture" if is_operator_capture else "official",
                    "nomination_vote_snapshot": {
                        "program_name": program_name,
                        "category_name": group_name,
                        "rank": rank,
                        "vote_count": vote_count,
                        "observed_at": result.fetched_at,
                    } if is_nominee_voting else None,
                    "content_origin": "nominee_vote_snapshot" if is_nominee_voting else "official_award_record",
                },
                entity_category=(
                    "official_trader_awardee"
                    if (is_trader_category and period_label == "2025" and not is_operator_capture and not is_nominee_voting)
                    else "square_creator_candidate"
                ),
            ))
    return items


def parse_square_user_posts(result: FetchResult, source: dict[str, Any]) -> list[ParsedItem]:
    """Read only the initial public profile batch; never follow the login-gated cursor."""
    data = _data_from_json(result)
    rows = data.get("contents", []) if isinstance(data, dict) else []
    configured_uid = (source.get("entity") or {}).get("source_uid")
    min_chars = max(80, int(source.get("minimum_text_chars", 200)))
    try:
        observed = parse_utc_timestamp(result.fetched_at)
    except (TypeError, ValueError) as exc:
        raise ValueError("Initial public creator batch had an invalid UTC fetch timestamp") from exc
    coverage_start = minus_calendar_years(observed, 2)
    candidates: list[tuple[int, int, dict[str, Any], str, str]] = []
    for position, row in enumerate(rows):
        source_uid = compact_text(row.get("squareUid"))
        if configured_uid and source_uid != configured_uid:
            continue
        published_at = iso_from_epoch(row.get("firstReleaseTime") or row.get("createTime"))
        if not published_at:
            continue
        try:
            published = parse_utc_timestamp(published_at)
        except ValueError:
            continue
        if not coverage_start <= published <= observed:
            continue
        text = compact_text(row.get("bodyTextOnly")) or structured_text(row.get("body"))
        if len(text) < min_chars:
            continue
        article_priority = 0 if row.get("contentType") == 2 else 1
        candidates.append((article_priority, position, row, text, published_at))
    candidates.sort(key=lambda item: (item[0], item[1]))
    max_items = max(1, min(int(source.get("max_items", 5)), 8))
    items: list[ParsedItem] = []
    for _, _, row, text, published_at in candidates[:max_items]:
        external_id = str(row.get("id") or hashlib.sha256(text.encode("utf-8")).hexdigest()[:24])
        source_uid = compact_text(row.get("squareUid")) or configured_uid
        username = compact_text(row.get("username")) or (source.get("entity") or {}).get("username")
        display_name = compact_text(row.get("displayName")) or (source.get("entity") or {}).get("display_name")
        title = compact_text(row.get("title")) or compact_text(text[:110])
        items.append(ParsedItem(
            external_id=external_id,
            canonical_url=row.get("webLink") or f"https://www.binance.com/en/square/post/{external_id}",
            title=title,
            text=text,
            author=display_name,
            username=username,
            published_at=published_at,
            language=row.get("lan") or row.get("detectedLang"),
            metadata={
                "source_uid": source_uid,
                "content_origin": "creator_post",
                "content_type": row.get("contentType"),
                "initial_public_batch": True,
                "login_gated_pagination_not_followed": True,
                "coverage_window_start": coverage_start.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                "coverage_window_end": observed.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                "engagement": {
                    "views": row.get("viewCount"),
                    "likes": row.get("likeCount"),
                    "comments": row.get("commentCount"),
                    "shares": row.get("shareCount"),
                    "replies": row.get("replyCount"),
                    "followers": None,
                    "denominator": "views" if row.get("viewCount") is not None else None,
                },
                "portfolio_performance": row.get("portfolioPerformance"),
            },
            entity_category="official_trader_awardee",
        ))
    if not items:
        raise ValueError("Initial public creator batch contained no substantive readable posts")
    return items


def _copy_trading_app_data(result: FetchResult) -> tuple[dict[str, Any], str | None, str | None, str]:
    decoded = result.body.decode("utf-8", "replace")
    observed_at: str | None = None
    capture_method: str | None = None
    if decoded.lstrip().startswith("{"):
        envelope = json.loads(decoded)
        app_data = envelope.get("appData")
        observed_at = envelope.get("observed_at")
        capture_method = envelope.get("capture_method")
        if envelope.get("url") != "https://www.binance.com/en/copy-trading":
            raise ValueError("Public browser capture URL did not match the configured copy-trading homepage")
        verification_level = "operator_capture"
    else:
        match = re.search(
            r'<script[^>]+id=["\']__APP_DATA["\'][^>]*>(.*?)</script>',
            decoded,
            flags=re.I | re.S,
        )
        if not match:
            raise AccessStopped(
                "Direct public copy-trading HTML did not expose __APP_DATA; explicit anonymous browser capture required"
            )
        app_data = json.loads(html.unescape(match.group(1)))
        verification_level = "platform"
    if not isinstance(app_data, dict):
        raise ValueError("Copy-trading source did not contain an appData object")
    return app_data, observed_at, capture_method, verification_level


def _copy_trading_rankings(app_data: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    routes = app_data.get("appState", {}).get("loader", {}).get("dataByRouteId", {})
    if not isinstance(routes, dict):
        raise ValueError("Copy-trading appData had no route data")
    for route in routes.values():
        if not isinstance(route, dict):
            continue
        queries = route.get("dehydratedState", {}).get("queries", [])
        for query in queries if isinstance(queries, list) else []:
            payload = query.get("state", {}).get("data", {}).get("data", {}) if isinstance(query, dict) else {}
            if isinstance(payload, dict) and "highestPnlLeads" in payload:
                return payload
    raise ValueError("Copy-trading ranking payload was not found in any discovered route")


def parse_copy_trading_home(result: FetchResult, source: dict[str, Any]) -> list[ParsedItem]:
    """Parse a live __APP_DATA payload or an explicit anonymous-browser capture."""
    app_data, captured_at, capture_method, verification_level = _copy_trading_app_data(result)
    rankings = _copy_trading_rankings(app_data)
    selected_ids = [str(value) for value in source.get("portfolio_ids", [])]
    if not selected_ids or len(selected_ids) > 3:
        raise ValueError("Copy-trading adapter requires one to three explicit portfolio IDs")
    records: dict[str, dict[str, Any]] = {}
    for list_name in ("highestPnlLeads", "highestRoiLeads", "highestCopyPnlLeads", "mostPopularLeads"):
        rows = rankings.get(list_name, [])
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            portfolio_id = row.get("leadPortfolioId")
            if not isinstance(portfolio_id, str):
                raise ValueError("Copy-trading portfolio ID was not an exact JSON string")
            if portfolio_id in selected_ids and portfolio_id not in records:
                records[portfolio_id] = row
    missing = [portfolio_id for portfolio_id in selected_ids if portfolio_id not in records]
    if missing:
        raise ValueError(f"Configured copy-trading portfolio IDs were absent: {', '.join(missing)}")

    required = ("nickname", "portfolioType", "roi", "pnl", "mdd", "winRate", "copierPnl", "startTime")
    items: list[ParsedItem] = []
    for portfolio_id in selected_ids:
        row = records[portfolio_id]
        absent = [field for field in required if field not in row]
        if absent:
            raise ValueError(f"Portfolio {portfolio_id} was missing fields: {', '.join(absent)}")
        if row["portfolioType"] != "PUBLIC":
            raise ValueError(f"Portfolio {portfolio_id} was not PUBLIC in the source snapshot")
        nickname = compact_text(row["nickname"]) or f"Portfolio {portfolio_id}"
        detail_url = f"https://www.binance.com/en/copy-trading/lead-details/{portfolio_id}?timeRange=30D"
        performance = {
            "portfolio_id": portfolio_id,
            "portfolio_type": row["portfolioType"],
            "period": "30 Days",
            "roi_percent": row["roi"],
            "leader_pnl": row["pnl"],
            "copier_pnl": row["copierPnl"],
            "mdd_percent": row["mdd"],
            "win_rate_percent": row["winRate"],
            "aum": row.get("aum"),
            "sharpe_ratio": row.get("sharpRatio"),
            "current_copy_count": row.get("currentCopyCount"),
            "chart_point_count": len(row.get("chartItems") or []),
            "unit_note": "ROI/MDD/win rate are source percentages; leader PnL, copier PnL and AUM use source-displayed portfolio values.",
        }
        text = (
            f"Binance Copy Trading public 30 Days snapshot for {nickname}, portfolio {portfolio_id}: "
            f"ROI {row['roi']}, leader PnL {row['pnl']}, copier PnL {row['copierPnl']}, "
            f"MDD {row['mdd']}, win rate {row['winRate']}."
        )
        items.append(ParsedItem(
            external_id=f"copy-trading-home-30d:{portfolio_id}",
            canonical_url=detail_url,
            title=f"30-day public portfolio snapshot — {nickname}",
            text=text,
            author=nickname,
            username=None,
            published_at=captured_at or result.fetched_at,
            language="en",
            metadata={
                "source_uid": f"copy-portfolio:{portfolio_id}",
                "entity_slug": f"copy-portfolio-{portfolio_id}",
                "identity_evidence_url": detail_url,
                "profile_url": detail_url,
                "content_origin": "platform_portfolio_snapshot",
                "platform_performance": performance,
                "capture_method": capture_method or "direct_public_html",
                "performance_verification_level": verification_level,
            },
            entity_category="copy_trading_portfolio",
        ))
    return items


PARSERS = {
    "square_feed": parse_square_feed,
    "square_detail": parse_square_detail,
    "cms_detail": parse_cms_detail,
    "html": parse_html,
    "award_winners": parse_award_winners,
    "square_user_posts": parse_square_user_posts,
    "copy_trading_home": parse_copy_trading_home,
}


def _upsert_entity(conn: Any, item: ParsedItem, source: dict[str, Any], now: str) -> int:
    static = source.get("entity") or {}
    display_name = static.get("display_name") or item.author or "Nguồn chưa rõ"
    username = static.get("username") or item.username
    source_uid = static.get("source_uid") or item.metadata.get("source_uid")
    profile_url = static.get("profile_url") or item.metadata.get("profile_url")
    identity_status = static.get("identity_status") or item.metadata.get("identity_status") or "source_profile_only"
    category = static.get("category") or item.entity_category
    configured_slug = static.get("slug") or item.metadata.get("entity_slug")
    if configured_slug:
        slug = configured_slug
    elif source_uid:
        slug = "square-uid-" + hashlib.sha256(str(source_uid).encode("utf-8")).hexdigest()[:16]
    elif username:
        slug = "square-handle-" + hashlib.sha256(str(username).casefold().encode("utf-8")).hexdigest()[:16]
    else:
        anonymous_key = f"{item.canonical_url}|{item.external_id}"
        slug = "square-unknown-" + hashlib.sha256(anonymous_key.encode("utf-8")).hexdigest()[:16]
    if not profile_url and username:
        profile_url = f"https://www.binance.com/en/square/profile/{urllib.parse.quote(username)}"
    public = 1 if source["access_level"] == "public" and source["distribution"] == "public_shareable" else 0
    existing = conn.execute("SELECT * FROM entities WHERE source_uid=?", (source_uid,)).fetchone() if source_uid else None
    if existing:
        incoming_is_official_authority = (
            identity_status == "official_award_payload_uid"
        )
        preserve_existing_identity = (
            existing["public_shareable"] != 1
            or (
                existing["category"] == "official_trader_awardee"
                and not (category == "official_trader_awardee" and incoming_is_official_authority)
            )
            or (
                existing["identity_status"] == "official_award_payload_uid"
                and not incoming_is_official_authority
            )
        )
        if preserve_existing_identity:
            incoming_bio = static.get("bio") or item.metadata.get("bio")
            if incoming_bio and existing["public_shareable"] == 1 and incoming_is_official_authority:
                conn.execute("UPDATE entities SET bio=COALESCE(?,bio),updated_at=? WHERE id=?", (incoming_bio, now, existing["id"]))
            else:
                conn.execute("UPDATE entities SET updated_at=? WHERE id=?", (now, existing["id"]))
            return int(existing["id"])
        conn.execute(
            """UPDATE entities SET display_name=?,category=?,username=?,profile_url=COALESCE(?,profile_url),
                   bio=COALESCE(?,bio),
                   identity_status=CASE WHEN public_shareable=1 THEN ? ELSE identity_status END,
                   identity_evidence_url=COALESCE(?,identity_evidence_url),updated_at=? WHERE id=?""",
            (display_name, category, username, profile_url,
             static.get("bio") or item.metadata.get("bio"),
             identity_status,
             static.get("identity_evidence_url") or item.metadata.get("identity_evidence_url"),
             now, existing["id"]),
        )
        return int(existing["id"])

    slug_owner = conn.execute("SELECT id,source_uid FROM entities WHERE slug=?", (slug,)).fetchone()
    if slug_owner is not None and slug_owner["source_uid"] != source_uid:
        # A display name, handle, or reused manifest slug is not identity proof.  Keep
        # different UIDs separate instead of merging through ON CONFLICT(slug).
        disambiguation_key = str(source_uid) if source_uid else f"{item.canonical_url}|{item.external_id}"
        prefix = "square-uid-" if source_uid else "square-unknown-"
        slug = prefix + hashlib.sha256(disambiguation_key.encode("utf-8")).hexdigest()[:16]
        second_owner = conn.execute("SELECT source_uid FROM entities WHERE slug=?", (slug,)).fetchone()
        if second_owner is not None and second_owner["source_uid"] != source_uid:
            raise ValueError("Entity slug collision did not have matching identity evidence")
    conn.execute(
        """
        INSERT INTO entities(slug,display_name,category,source_uid,username,profile_url,bio,identity_status,
            identity_evidence_url,public_shareable,created_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(slug) DO UPDATE SET display_name=excluded.display_name,username=excluded.username,
            source_uid=COALESCE(excluded.source_uid,entities.source_uid),
            profile_url=COALESCE(excluded.profile_url,entities.profile_url),
            bio=COALESCE(excluded.bio,entities.bio),updated_at=excluded.updated_at
        """,
        (slug, display_name, category, source_uid, username, profile_url,
         static.get("bio") or item.metadata.get("bio"), identity_status,
         static.get("identity_evidence_url") or item.metadata.get("identity_evidence_url") or item.canonical_url, public, now, now),
    )
    return int(conn.execute("SELECT id FROM entities WHERE slug=?", (slug,)).fetchone()[0])


def _short_quote(text: str, max_words: int = 20) -> str:
    return " ".join(text.split()[:max_words])


def _store_item(conn: Any, source_row: Any, source: dict[str, Any], item: ParsedItem, fetched_at: str) -> tuple[int, int, int]:
    entity_id = _upsert_entity(conn, item, source, fetched_at)
    owner = conn.execute("SELECT public_shareable FROM entities WHERE id=?", (entity_id,)).fetchone()
    public = 1 if (
        source_row["access_level"] == "public"
        and source_row["distribution"] == "public_shareable"
        and owner is not None and owner["public_shareable"] == 1
    ) else 0
    content_row = conn.execute(
        "SELECT * FROM contents WHERE source_id=? AND external_id=?",
        (source_row["id"], item.external_id),
    ).fetchone()
    new_content = 0
    if content_row is None:
        cursor = conn.execute(
            """
            INSERT INTO contents(source_id,entity_id,external_id,canonical_url,title,author,language,
                published_at,first_seen_at,public_shareable,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (source_row["id"], entity_id, item.external_id, item.canonical_url, item.title,
             item.author, item.language, item.published_at, fetched_at, public, fetched_at, fetched_at),
        )
        content_id = int(cursor.lastrowid)
        new_content = 1
    else:
        content_id = int(content_row["id"])
        conn.execute(
            "UPDATE contents SET entity_id=?,canonical_url=?,title=?,author=?,language=?,published_at=COALESCE(?,published_at),updated_at=? WHERE id=?",
            (entity_id, item.canonical_url, item.title, item.author, item.language, item.published_at, fetched_at, content_id),
        )
    normalized = compact_text(item.text)
    checksum = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    version = conn.execute("SELECT id FROM content_versions WHERE content_id=? AND checksum=?", (content_id, checksum)).fetchone()
    new_version = 0
    if version is None:
        number = int(conn.execute("SELECT COALESCE(MAX(version_no),0)+1 FROM content_versions WHERE content_id=?", (content_id,)).fetchone()[0])
        cursor = conn.execute(
            "INSERT INTO content_versions(content_id,version_no,fetched_at,checksum,normalized_text,excerpt,metadata_json) VALUES(?,?,?,?,?,?,?)",
            (content_id, number, fetched_at, checksum, normalized, public_excerpt(normalized), json.dumps(item.metadata, ensure_ascii=False, sort_keys=True)),
        )
        version_id = int(cursor.lastrowid)
        conn.execute("UPDATE contents SET latest_version_id=? WHERE id=?", (version_id, content_id))
        new_version = 1
    else:
        version_id = int(version["id"])

    engagement = item.metadata.get("engagement") if isinstance(item.metadata, dict) else None
    if isinstance(engagement, dict) and any(engagement.get(k) is not None for k in ("views", "likes", "comments", "shares", "replies")):
        value_json = json.dumps(engagement, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        engagement_level = item.metadata.get("engagement_verification_level", "platform")
        if engagement_level not in {"platform", "secondary"}:
            engagement_level = "unknown"
        engagement_observed_at = item.metadata.get("engagement_observed_at") or fetched_at
        if engagement_level == "secondary":
            engagement_methodology = "Các trường đếm nằm trong snapshot HTML tĩnh của một kho lưu trữ công khai."
            engagement_limitations = (
                "Archive snapshot; không phải phép đo live, có thể thiếu thay đổi sau thời điểm lưu và không chứng minh hiệu suất giao dịch."
            )
        else:
            engagement_methodology = "Các trường đếm được trả về trực tiếp bởi endpoint Binance Square công khai."
            engagement_limitations = (
                "Snapshot một bài; không có số follower/unique engager và không chứng minh hiệu suất giao dịch."
            )
        conn.execute(
            """
            INSERT OR IGNORE INTO evidence(source_id,entity_id,version_id,pillar,kind,verification_level,
                observed_at,period_label,value_json,quote,methodology,limitations,dependency_group,public_shareable)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (source_row["id"], entity_id, version_id, "engagement", "post_metrics", engagement_level,
             engagement_observed_at, "snapshot", value_json, None,
             engagement_methodology,
             engagement_limitations,
             f"square-post:{item.external_id}", public),
        )

    platform_performance = item.metadata.get("platform_performance") if isinstance(item.metadata, dict) else None
    portfolio = item.metadata.get("portfolio_performance") if isinstance(item.metadata, dict) else None
    if platform_performance:
        performance_level = item.metadata.get("performance_verification_level", "unknown")
        capture_limitation = (
            "Snapshot 30 ngày ở cấp portfolio; không chứng minh lợi nhuận đã thực hiện, toàn bộ dòng tiền, "
            "hiệu suất trọn đời hoặc danh tính trùng với awardee Square."
        )
        if performance_level == "operator_capture":
            capture_limitation += " operator-supplied, capture provenance not independently authenticated."
        conn.execute(
            """
            INSERT OR IGNORE INTO evidence(source_id,entity_id,version_id,pillar,kind,verification_level,
                observed_at,period_label,value_json,quote,methodology,limitations,dependency_group,public_shareable)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (source_row["id"], entity_id, version_id, "performance", "copy_trading_30d_snapshot", performance_level,
             fetched_at, platform_performance.get("period"),
             json.dumps(platform_performance, ensure_ascii=False, sort_keys=True, separators=(",", ":")), None,
             "Đọc nguyên trạng các trường portfolio trong __APP_DATA của trang Binance Copy Trading công khai.",
             capture_limitation,
             f"copy-portfolio:{platform_performance['portfolio_id']}:30d", public),
        )
    elif portfolio:
        conn.execute(
            """
            INSERT OR IGNORE INTO evidence(source_id,entity_id,version_id,pillar,kind,verification_level,
                observed_at,period_label,value_json,quote,methodology,limitations,dependency_group,public_shareable)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (source_row["id"], entity_id, version_id, "performance", "platform_portfolio_widget", "platform",
             fetched_at, "source-defined", json.dumps(portfolio, ensure_ascii=False, sort_keys=True), None,
             "Dữ liệu portfolioPerformance do endpoint Binance Square trả về.",
             "Chỉ phạm vi widget công khai; chưa xác nhận toàn bộ tài sản, cash flow hay tính bền vững.",
             f"square-post:{item.external_id}", public),
        )
    elif re.search(r"\b(?:roi|pnl|profit|return)\b", normalized, re.I):
        value_json = json.dumps({"reported_text": _short_quote(normalized)}, ensure_ascii=False, sort_keys=True)
        conn.execute(
            """
            INSERT OR IGNORE INTO evidence(source_id,entity_id,version_id,pillar,kind,verification_level,
                observed_at,period_label,value_json,quote,methodology,limitations,dependency_group,public_shareable)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (source_row["id"], entity_id, version_id, "performance", "performance_language", "self_claim",
             fetched_at, None, value_json, _short_quote(normalized),
             "Phát hiện từ ngôn ngữ ROI/PnL/profit/return trong chính nội dung tác giả.",
             "Tự công bố; không có hồ sơ platform, kỳ đo, cash flow hoặc lịch sử đủ để xác minh.",
             f"square-post:{item.external_id}", public),
        )

    recognition = item.metadata.get("recognition") or source.get("recognition")
    if recognition:
        value_json = json.dumps({
            "program": recognition["program_name"],
            "category_type": recognition["category_type"],
            "category_name": recognition.get("category_name"),
            "result": recognition.get("result"),
            "rank": recognition.get("rank"),
            "award_vote_count": recognition.get("award_vote_count"),
            "award_vote_semantics": "community_award_votes" if recognition.get("award_vote_count") is not None else None,
        }, ensure_ascii=False, sort_keys=True)
        recognition_dependency = f"official-program:{source['slug']}:{item.metadata.get('source_uid') or item.external_id}"
        recognition_level = str(item.metadata.get("recognition_verification_level") or "official")
        if recognition_level not in {"official", "operator_capture", "secondary"}:
            recognition_level = "unknown"
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO evidence(source_id,entity_id,version_id,pillar,kind,verification_level,
                observed_at,period_label,value_json,quote,methodology,limitations,dependency_group,public_shareable)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (source_row["id"], entity_id, version_id, "recognition", "official_program", recognition_level,
             fetched_at, recognition.get("period_label"), value_json, recognition.get("quote"),
             "Đối chiếu nội dung chương trình trên URL Binance chính thức.", recognition["interpretation_limit"],
             recognition_dependency, public),
        )
        evidence_row = conn.execute(
            "SELECT id FROM evidence WHERE source_id=? AND entity_id=? AND version_id=? AND pillar='recognition' AND dependency_group=?",
            (source_row["id"], entity_id, version_id, recognition_dependency),
        ).fetchone()
        if evidence_row:
            conn.execute(
                """
                INSERT OR IGNORE INTO recognitions(evidence_id,program_name,category_type,category_name,result,
                    criteria,period_label,recognized_at,known_at,interpretation_limit)
                VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (evidence_row["id"], recognition["program_name"], recognition["category_type"],
                 recognition.get("category_name"), recognition.get("result"), recognition.get("criteria"),
                 recognition.get("period_label"), recognition.get("recognized_at"), fetched_at,
                 recognition["interpretation_limit"]),
            )

    brief = research_brief(item.title, normalized)
    store_source_claim(
        conn, entity_id=entity_id, source_id=source_row["id"], version_id=version_id,
        brief=brief, claim_type="opinion" if item.metadata.get("content_origin") == "creator_post" else "fact",
        public=public, created_at=fetched_at,
    )
    return new_content, new_version, 0 if new_version else 1


def ingest_source_configs(
    db_path: str | Path,
    sources: Iterable[dict[str, Any]],
    *,
    run_note: str | None = None,
    concurrency: int = 2,
    fetch_coordinator: FetchCoordinator | None = None,
    current_time: datetime | None = None,
) -> dict[str, Any]:
    source_list = list(sources)
    seed_source_configs(db_path, source_list)
    started_at = utc_now()
    started_clock = time.perf_counter()
    coordinator = fetch_coordinator or FetchCoordinator(concurrency=concurrency)
    current = _as_utc(current_time)
    suppressed: dict[str, str | None] = {}
    with connect(db_path) as permission_conn:
        for source in source_list:
            source_row = permission_conn.execute("SELECT * FROM sources WHERE slug=?", (source["slug"],)).fetchone()
            hidden = source_row["access_level"] != "public" or source_row["distribution"] != "public_shareable"
            entity_config = source.get("entity") or {}
            owner = None
            if entity_config.get("source_uid"):
                owner = permission_conn.execute(
                    "SELECT public_shareable FROM entities WHERE source_uid=?", (entity_config["source_uid"],)
                ).fetchone()
            if owner is None and entity_config.get("slug"):
                owner = permission_conn.execute(
                    "SELECT public_shareable FROM entities WHERE slug=?", (entity_config["slug"],)
                ).fetchone()
            host_cooldown, _ = _persisted_host_cooldown(permission_conn, _source_host(source), current)
            if hidden or (owner is not None and owner["public_shareable"] != 1):
                suppressed[source["slug"]] = "permission"
            elif host_cooldown is not None:
                suppressed[source["slug"]] = "host_cooldown"
            else:
                suppressed[source["slug"]] = None

    def fetch_one(source: dict[str, Any]) -> FetchResult:
        return fetch_public(
            source["fetch_url"], method=source.get("method", "GET"),
            json_body=source.get("json_body"), coordinator=coordinator,
        )

    fetch_outcomes: dict[str, FetchResult | Exception] = {}
    active_sources = [source for source in source_list if suppressed[source["slug"]] is None]
    if active_sources:
        with ThreadPoolExecutor(max_workers=min(coordinator.concurrency, len(active_sources)), thread_name_prefix="bti-fetch") as pool:
            futures = {source["slug"]: pool.submit(fetch_one, source) for source in active_sources}
            for source in active_sources:
                try:
                    fetch_outcomes[source["slug"]] = futures[source["slug"]].result()
                except Exception as exc:
                    fetch_outcomes[source["slug"]] = exc
    totals = {"source_count": 0, "fetch_success": 0, "new_contents": 0, "new_versions": 0, "duplicates": 0, "errors": 0, "skipped": 0}
    events: list[dict[str, Any]] = []
    with connect(db_path) as conn:
        run_id = int(conn.execute(
            "INSERT INTO ingest_runs(started_at,status,notes) VALUES(?,?,?)",
            (started_at, "running", run_note),
        ).lastrowid)
        conn.commit()
        for source in source_list:
            slug = source["slug"]
            totals["source_count"] += 1
            source_row = conn.execute("SELECT * FROM sources WHERE slug=?", (slug,)).fetchone()
            event = {"source": slug, "url": source["url"], "fetch_url": source["fetch_url"], "fetched_at": utc_now()}
            clock = time.perf_counter()
            try:
                if source_row["access_level"] != "public" or source_row["distribution"] != "public_shareable":
                    raise SourceSuppressed("Persisted source permission is not public-shareable")
                entity_config = source.get("entity") or {}
                owner = None
                if entity_config.get("source_uid"):
                    owner = conn.execute(
                        "SELECT public_shareable FROM entities WHERE source_uid=?", (entity_config["source_uid"],)
                    ).fetchone()
                if owner is None and entity_config.get("slug"):
                    owner = conn.execute(
                        "SELECT public_shareable FROM entities WHERE slug=?", (entity_config["slug"],)
                    ).fetchone()
                if owner is not None and owner["public_shareable"] != 1:
                    raise SourceSuppressed("Persisted owner permission is revoked")
                if suppressed[slug] == "host_cooldown":
                    raise SourceSuppressed("Persisted Binance host cooldown is active")
                fetched = fetch_outcomes[slug]
                if isinstance(fetched, Exception):
                    raise fetched
                result = fetched
                conn.execute("BEGIN IMMEDIATE")
                source_row = conn.execute("SELECT * FROM sources WHERE slug=?", (slug,)).fetchone()
                if source_row["access_level"] != "public" or source_row["distribution"] != "public_shareable":
                    raise SourceSuppressed("Persisted source permission was revoked before storage")
                entity_config = source.get("entity") or {}
                owner = None
                if entity_config.get("source_uid"):
                    owner = conn.execute(
                        "SELECT public_shareable FROM entities WHERE source_uid=?", (entity_config["source_uid"],)
                    ).fetchone()
                if owner is None and entity_config.get("slug"):
                    owner = conn.execute(
                        "SELECT public_shareable FROM entities WHERE slug=?", (entity_config["slug"],)
                    ).fetchone()
                if owner is not None and owner["public_shareable"] != 1:
                    raise SourceSuppressed("Persisted owner permission was revoked before storage")
                event.update(status_code=result.status, latency_ms=result.latency_ms, byte_count=len(result.body), content_type=result.content_type, response_hash=result.response_hash)
                parser = PARSERS.get(source["adapter"])
                if parser is None:
                    raise ValueError(f"Unknown adapter {source['adapter']}")
                items = parser(result, source)
                for item in items:
                    nc, nv, dup = _store_item(conn, source_row, source, item, result.fetched_at)
                    totals["new_contents"] += nc
                    totals["new_versions"] += nv
                    totals["duplicates"] += dup
                totals["fetch_success"] += 1
                fields = sorted({key for item in items for key, value in item.metadata.items() if value is not None})
                capability = "available"
                detail = f"Ingested {len(items)} public item(s)"
                event.update(outcome="success", error_class=None, error_message=None, item_count=len(items))
                conn.execute("UPDATE sources SET capability_status=?,capability_detail=?,available_fields_json=?,last_attempt_at=?,last_success_at=?,updated_at=? WHERE id=?",
                             (capability, detail, json.dumps(fields), result.fetched_at, result.fetched_at, result.fetched_at, source_row["id"]))
            except SourceSuppressed:
                totals["skipped"] += 1
                event.update(status_code=None, latency_ms=int((time.perf_counter()-clock)*1000), byte_count=0,
                             content_type=None, response_hash=None, outcome="skipped",
                             error_class=None, error_message=None, item_count=0)
            except PolicyPaused as exc:
                totals["skipped"] += 1
                event.update(status_code=None, latency_ms=int((time.perf_counter()-clock)*1000), byte_count=0,
                             content_type=None, response_hash=None, outcome="skipped", skip_reason="crawler_policy",
                             policy_reason=exc.reason_code, error_class=None, error_message=None, item_count=0)
                conn.execute(
                    "UPDATE sources SET capability_status='policy_paused',capability_detail=?,updated_at=? WHERE id=?",
                    (exc.reason_code, event["fetched_at"], source_row["id"]),
                )
            except AccessStopped as exc:
                totals["errors"] += 1
                _record_host_cooldown(
                    conn, _source_host(source), exc.status_code, exc.retry_after_seconds, current,
                )
                event.update(status_code=exc.status_code or event.get("status_code"), retry_after_seconds=exc.retry_after_seconds,
                             latency_ms=int((time.perf_counter()-clock)*1000), byte_count=event.get("byte_count", 0),
                             content_type=event.get("content_type"), response_hash=event.get("response_hash"),
                             outcome="blocked", error_class=type(exc).__name__, error_message=str(exc))
                conn.execute("UPDATE sources SET capability_status='blocked',capability_detail=?,last_attempt_at=?,updated_at=? WHERE id=?", (str(exc), event["fetched_at"], event["fetched_at"], source_row["id"]))
            except Exception as exc:
                totals["errors"] += 1
                event.update(status_code=getattr(exc, "code", event.get("status_code")), latency_ms=int((time.perf_counter()-clock)*1000), byte_count=event.get("byte_count", 0), content_type=event.get("content_type"), response_hash=event.get("response_hash"), outcome="error", error_class=type(exc).__name__, error_message=str(exc)[:500])
                conn.execute("UPDATE sources SET capability_status='error',capability_detail=?,last_attempt_at=?,updated_at=? WHERE id=?", (str(exc)[:500], event["fetched_at"], event["fetched_at"], source_row["id"]))
            conn.execute(
                """
                INSERT INTO fetch_events(run_id,source_id,fetched_at,status_code,outcome,latency_ms,byte_count,
                    content_type,response_hash,error_class,error_message)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """,
                (run_id, source_row["id"], event["fetched_at"], event.get("status_code"), event["outcome"], event["latency_ms"],
                 event.get("byte_count", 0), event.get("content_type"), event.get("response_hash"), event.get("error_class"), event.get("error_message")),
            )
            conn.commit()
            events.append(event)
        finished_at = utc_now()
        duration_ms = int((time.perf_counter() - started_clock) * 1000)
        status = "success" if totals["errors"] == 0 else ("partial" if totals["fetch_success"] else "failed")
        conn.execute(
            """
            UPDATE ingest_runs SET finished_at=?,status=?,source_count=?,fetch_success=?,new_contents=?,
                new_versions=?,duplicates=?,errors=?,duration_ms=? WHERE id=?
            """,
            (finished_at, status, totals["source_count"], totals["fetch_success"], totals["new_contents"],
             totals["new_versions"], totals["duplicates"], totals["errors"], duration_ms, run_id),
        )
        conn.commit()
    return {"run_id": run_id, "started_at": started_at, "finished_at": finished_at, "status": status, **totals, "duration_ms": duration_ms, "events": events}


def ingest(
    db_path: str | Path,
    manifest_path: str | Path,
    *,
    only: Iterable[str] | None = None,
    concurrency: int = 2,
    crawler_fetcher_override: Callable[..., FetchResult] | None = None,
) -> dict[str, Any]:
    from .crawler_policy import prepare_crawler_cycle

    manifest = load_manifest_document(manifest_path)
    configs = manifest["sources"]
    selected = set(only or [source["slug"] for source in configs])
    sources = [source for source in configs if source["slug"] in selected]
    coordinator = FetchCoordinator(concurrency=concurrency)
    crawler_kwargs: dict[str, Any] = {}
    if crawler_fetcher_override is not None:
        crawler_kwargs["fetcher"] = crawler_fetcher_override
    crawler = prepare_crawler_cycle(
        db_path, manifest.get("crawler_policy"), coordinator, **crawler_kwargs,
    )
    result = ingest_source_configs(db_path, sources, fetch_coordinator=coordinator)
    return {**result, "crawler": crawler}


def ingest_public_capture(
    db_path: str | Path,
    manifest_path: str | Path,
    *,
    source_slug: str,
    capture_path: str | Path,
) -> dict[str, Any]:
    """Explicitly import a coordinator-captured anonymous public browser response."""
    project_root = Path(__file__).resolve().parents[2]
    resolved = Path(capture_path).resolve()
    if project_root.resolve() not in resolved.parents or not resolved.is_file():
        raise ValueError("Capture must be an existing file inside the project root")
    seed_sources(db_path, manifest_path)
    configs = {item["slug"]: item for item in load_manifest(manifest_path)}
    source = configs.get(source_slug)
    if source is None or source.get("adapter") not in {"copy_trading_home", "award_winners"}:
        raise ValueError("Capture source must name the configured copy-trading or award-winners adapter")
    raw = resolved.read_bytes()
    if len(raw) > MAX_BODY_BYTES:
        raise BodyTooLarge(f"Capture exceeded {MAX_BODY_BYTES} bytes")
    envelope = json.loads(raw.decode("utf-8"))
    observed_at = envelope.get("observed_at")
    if not isinstance(observed_at, str):
        raise ValueError("Capture must carry an explicit UTC observed_at")
    try:
        parsed_observed_at = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Capture observed_at was not a valid ISO-8601 timestamp") from exc
    if parsed_observed_at.tzinfo is None or parsed_observed_at.utcoffset() != timedelta(0):
        raise ValueError("Capture observed_at must be explicitly UTC")
    if parsed_observed_at > datetime.now(timezone.utc) + timedelta(minutes=5):
        raise ValueError("Capture observed_at is more than five minutes in the future")
    if not isinstance(envelope.get("capture_method"), str) or not envelope["capture_method"].strip():
        raise ValueError("Capture must preserve its operator-supplied capture_method label")
    result = FetchResult(
        url=source["url"], status=200, content_type="application/json; public-browser-capture",
        body=raw, latency_ms=0, fetched_at=observed_at.replace("+00:00", "Z"),
    )
    items = parse_award_winners(result, source) if source.get("adapter") == "award_winners" else parse_copy_trading_home(result, source)
    started = utc_now()
    with connect(db_path) as conn:
        run_id = int(conn.execute(
            "INSERT INTO ingest_runs(started_at,status,notes) VALUES(?,?,?)",
            (started, "running", "Explicit import of an anonymous public browser capture"),
        ).lastrowid)
        source_row = conn.execute("SELECT * FROM sources WHERE slug=?", (source_slug,)).fetchone()
        totals = {"new_contents": 0, "new_versions": 0, "duplicates": 0}
        for item in items:
            nc, nv, dup = _store_item(conn, source_row, source, item, result.fetched_at)
            totals["new_contents"] += nc; totals["new_versions"] += nv; totals["duplicates"] += dup
        finished = utc_now()
        conn.execute(
            """INSERT INTO fetch_events(run_id,source_id,fetched_at,status_code,outcome,latency_ms,byte_count,
                   content_type,response_hash,error_class,error_message) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (run_id, source_row["id"], result.fetched_at, 200, "success", 0, len(raw), result.content_type,
             result.response_hash, None, None),
        )
        fields = sorted({key for item in items for key, value in item.metadata.items() if value is not None})
        conn.execute(
            """UPDATE sources SET capability_status='available',capability_detail=?,available_fields_json=?,
                   last_attempt_at=?,last_success_at=?,updated_at=? WHERE id=?""",
            (f"Imported {len(items)} records from explicit anonymous public browser capture",
             json.dumps(fields), result.fetched_at, result.fetched_at, finished, source_row["id"]),
        )
        conn.execute(
            """UPDATE ingest_runs SET finished_at=?,status='success',source_count=1,fetch_success=1,
                   new_contents=?,new_versions=?,duplicates=?,errors=0,duration_ms=0 WHERE id=?""",
            (finished, totals["new_contents"], totals["new_versions"], totals["duplicates"], run_id),
        )
        conn.commit()
    return {"run_id": run_id, "started_at": started, "finished_at": finished, "status": "success",
            "source_count": 1, "fetch_success": 1, "errors": 0, "item_count": len(items), **totals,
            "capture_sha256": result.response_hash}

from __future__ import annotations

import gzip
import hashlib
import html
import io
import importlib.util
import json
import re
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .db import connect, init_db, utc_now
from .ingest import (
    AccessStopped,
    BodyTooLarge,
    FetchCoordinator,
    FetchResult,
    ParsedItem,
    USER_AGENT,
    _NoRedirect,
    _retry_after_seconds,
    _store_item,
    compact_text,
    iso_from_epoch,
    minus_calendar_years,
    parse_award_winners,
    parse_square_detail,
    parse_utc_timestamp,
    seed_source_configs,
    structured_text,
)

ARCHIVE_HOST = "web.archive.org"
ARCHIVE_MAX_BODY_BYTES = 1024 * 1024
ARCHIVE_TIMEOUT_SECONDS = 30
ARCHIVE_MINIMUM_SPACING_SECONDS = 1.5
ARCHIVE_HARD_MAX_FETCHES = 100
CDX_HARD_MAX_ROWS = 500
CDX_HARD_MAX_REQUESTS_PER_CYCLE = 3
ARCHIVE_HARD_MAX_REQUESTS_PER_CYCLE = 100
QUALITY_GATE_VERSION = 2
ARCHIVE_MAX_AUTOMATIC_RETRY_AFTER_SECONDS = 86400
ARCHIVE_MANUAL_HOLD_UNTIL = "9999-12-31T23:59:59Z"
POST_ID_RE = re.compile(r"(?:-|/)([0-9]{8,})$")
REPLAY_RE = re.compile(
    r"^/web/([0-9]{14})id_/https://www\.binance\.com/(?:en|vi)/square/(?:post|profile)/"
)

def _bounded_private_diagnostic(exc: BaseException, phase: str) -> dict[str, Any]:
    frames: list[dict[str, Any]] = []
    if exc.__traceback__ is not None:
        for frame in traceback.extract_tb(exc.__traceback__)[-5:]:
            frames.append({
                "file": Path(frame.filename).name,
                "line": int(frame.lineno or 0),
                "function": frame.name,
            })
    return {
        "phase": phase,
        "error_class": type(exc).__name__,
        "frames": frames,
    }


def _utc(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("Content-volume time must be timezone-aware")
    return current.astimezone(timezone.utc)


def _iso(value: datetime | None = None) -> str:
    return _utc(value).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_content_manifest(path: str | Path) -> dict[str, Any]:
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("Content-volume manifest root must be an object")
    if not isinstance(document.get("archive_source"), dict):
        raise ValueError("Content-volume manifest requires archive_source")
    if not isinstance(document.get("profile_snapshots"), list):
        raise ValueError("Content-volume manifest requires profile_snapshots")
    if not isinstance(document.get("cached_details"), list):
        raise ValueError("Content-volume manifest requires cached_details")
    cdx = document.get("cdx")
    if not isinstance(cdx, dict):
        raise ValueError("Content-volume manifest requires cdx configuration")
    if not 1 <= int(cdx.get("max_rows", 250)) <= CDX_HARD_MAX_ROWS:
        raise ValueError("CDX max_rows is outside the bounded range")
    if not 1 <= int(cdx.get("max_requests_per_cycle", 1)) <= CDX_HARD_MAX_REQUESTS_PER_CYCLE:
        raise ValueError("CDX request budget is outside the bounded range")
    throughput = document.get("throughput") or {}
    if not isinstance(throughput, dict):
        raise ValueError("Content-volume throughput configuration must be an object")
    base_fetches = int(throughput.get("base_archive_fetches_per_cycle", document.get("max_archive_fetches_per_cycle", 40)))
    max_fetches = int(throughput.get("max_archive_fetches_per_cycle", base_fetches))
    total_requests = int(throughput.get("max_total_archive_requests_per_cycle", ARCHIVE_HARD_MAX_REQUESTS_PER_CYCLE))
    if not 0 <= base_fetches <= max_fetches <= ARCHIVE_HARD_MAX_FETCHES:
        raise ValueError("Adaptive archive fetch bounds are invalid")
    if not 1 <= total_requests <= ARCHIVE_HARD_MAX_REQUESTS_PER_CYCLE:
        raise ValueError("Total archive request budget is outside the bounded range")
    baseline = document.get("baseline") or {}
    parse_utc_timestamp(str(baseline.get("started_at") or ""))
    if int(baseline.get("article_count", -1)) < 0:
        raise ValueError("Content-volume baseline count must be non-negative")
    return document


def _validate_archive_url(url: str, *, allow_cdx: bool = False) -> urllib.parse.ParseResult:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != ARCHIVE_HOST:
        raise ValueError("Archive request is outside the exact HTTPS host allowlist")
    if allow_cdx and parsed.path == "/cdx/search/cdx":
        return parsed
    if not REPLAY_RE.match(parsed.path):
        raise ValueError("Archive replay URL is outside the static Binance Square routes")
    if not (
        "id_/https://www.binance.com/en/square/" in parsed.path
        or "id_/https://www.binance.com/vi/square/" in parsed.path
    ):
        raise ValueError("Archive replay must use an id_ static Binance Square capture")
    return parsed


def _same_static_post_replay_redirect(requested_url: str, redirect_location: Any) -> bool:
    def embedded_original(url: str) -> str | None:
        if not isinstance(url, str) or not url or len(url) > 8192:
            return None
        if any(ord(character) < 32 for character in url):
            return None
        try:
            parsed = _validate_archive_url(url)
        except (TypeError, ValueError):
            return None
        if parsed.netloc != ARCHIVE_HOST or parsed.params or parsed.fragment:
            return None
        marker = "id_/"
        marker_at = parsed.path.find(marker)
        if marker_at < 0:
            return None
        original = parsed.path[marker_at + len(marker):]
        if parsed.query:
            original += "?" + parsed.query
        original_parsed = urllib.parse.urlparse(original)
        if (
            original_parsed.scheme != "https"
            or original_parsed.netloc != "www.binance.com"
            or original_parsed.params
            or original_parsed.fragment
            or not (
                (
                    (original_parsed.path.startswith("/en/square/post/") or original_parsed.path.startswith("/vi/square/post/"))
                    and POST_ID_RE.search(original_parsed.path) is not None
                )
                or re.match(r"^/(?:en|vi)/square/profile/[A-Za-z0-9_-]+/?$", original_parsed.path) is not None
            )
        ):
            return None
        return original

    requested_original = embedded_original(requested_url)
    redirected_original = embedded_original(redirect_location)
    return requested_original is not None and redirected_original == requested_original


def fetch_archive(
    url: str,
    *,
    coordinator: FetchCoordinator,
    allow_cdx: bool = False,
    max_body_bytes: int = ARCHIVE_MAX_BODY_BYTES,
    timeout: int = ARCHIVE_TIMEOUT_SECONDS,
) -> FetchResult:
    parsed = _validate_archive_url(url, allow_cdx=allow_cdx)
    if not 1 <= int(max_body_bytes) <= ARCHIVE_MAX_BODY_BYTES:
        raise ValueError("Archive body limit is outside the bounded range")
    coordinator.before_request(ARCHIVE_HOST, url)
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json,text/html;q=0.8",
            "Accept-Encoding": "identity",
        },
    )
    started = time.perf_counter()
    try:
        with urllib.request.build_opener(_NoRedirect()).open(request, timeout=timeout) as response:
            compressed = response.read(max_body_bytes + 1)
            if len(compressed) > max_body_bytes:
                raise BodyTooLarge(f"Archive response exceeded {max_body_bytes} bytes")
            if compressed[:2] == b"\x1f\x8b":
                with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as stream:
                    body = stream.read(max_body_bytes + 1)
            else:
                body = compressed
            if len(body) > max_body_bytes:
                raise BodyTooLarge(f"Expanded archive response exceeded {max_body_bytes} bytes")
            if response.status != 200:
                raise AccessStopped(f"Archive returned HTTP {response.status}", status_code=response.status)
            return FetchResult(
                url=url,
                status=response.status,
                content_type=response.headers.get_content_type(),
                body=body,
                latency_ms=int((time.perf_counter() - started) * 1000),
                fetched_at=utc_now(),
            )
    except urllib.error.HTTPError as exc:
        retry_after = _retry_after_seconds(
            exc.headers.get("Retry-After") if exc.headers is not None else None
        )
        if exc.code in {401, 403, 429}:
            coordinator.trip(exc.code, retry_after)
        stopped = AccessStopped(
            f"Archive stopped at HTTP {exc.code}; redirects and access gates are not followed",
            status_code=exc.code,
            retry_after_seconds=retry_after,
        )
        location = exc.headers.get("Location") if exc.headers is not None else None
        if location is not None:
            stopped.redirect_location = location
        raise stopped from exc


def _snapshot_time(archive_url: str) -> str:
    match = REPLAY_RE.match(urllib.parse.urlparse(archive_url).path)
    if not match:
        raise ValueError("Archive URL had no valid snapshot timestamp")
    parsed = datetime.strptime(match.group(1), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    return _iso(parsed)


def _app_data(body: bytes) -> dict[str, Any]:
    text = body.decode("utf-8", errors="strict")
    match = re.search(r'<script id="__APP_DATA"[^>]*>(.*?)</script>', text, re.DOTALL)
    if not match:
        raise ValueError("Static archive snapshot had no __APP_DATA payload")
    data = json.loads(html.unescape(match.group(1)))
    if not isinstance(data, dict):
        raise ValueError("Archive __APP_DATA was not an object")
    return data


def _post_records(data: Any) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            body = node.get("bodyTextOnly") or node.get("body")
            if node.get("id") is not None and isinstance(body, str):
                found.setdefault(str(node["id"]), node)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(data)
    return found


def _quality_rejection(body: str) -> tuple[str | None, dict[str, Any]]:
    words = re.findall(r"[A-Za-zÀ-ỹ0-9][A-Za-zÀ-ỹ0-9'’-]*", body.casefold())
    unique_words = len(set(words))
    sentence_marks = len(re.findall(r"[.!?;:]", body))
    links = re.findall(r"https?://|www\.", body.casefold())
    promotional_hits = sum(bool(re.search(pattern, body, re.IGNORECASE)) for pattern in (
        r"\b(referral|invite)\s+(code|link)\b",
        r"\bjoin\s+(my|our)\s+(telegram|whatsapp|discord|signal)\b",
        r"\b(dm|message)\s+me\s+(for|to)\b",
        r"\b(copy\s+my\s+trades?|guaranteed\s+profit)\b",
        r"\b(like|follow|share)\s+(and|to)\s+(win|earn)\b",
        r"\b(mã\s+giới\s+thiệu|link\s+đăng\s+ký|nhóm\s+tín\s+hiệu)\b",
        r"邀请码|永久返佣|填写邀请码",
    ))
    signals = {
        "word_count": len(words),
        "unique_word_count": unique_words,
        "sentence_mark_count": sentence_marks,
        "link_count": len(links),
        "promotional_pattern_count": promotional_hits,
    }
    if promotional_hits >= 1 or (len(links) >= 3 and re.search(r"\b(join|register|sign up|subscribe)\b", body, re.IGNORECASE)):
        return "promotional_or_referral", signals
    if len(words) < 30 or unique_words < 18:
        return "insufficient_information_density", signals
    if len(words) >= 40 and unique_words / len(words) < 0.22:
        return "repetitive_body", signals
    if sentence_marks < 2 and len(words) < 60:
        return "insufficient_structure", signals
    return None, signals


def _archive_item(
    row: dict[str, Any],
    *,
    archive_url: str,
    original_url: str,
    response_sha256: str,
    observed_at: str,
    current: datetime,
    minimum_text_chars: int,
    expected_uid: str | None = None,
) -> tuple[ParsedItem | None, str | None]:
    post_id = str(row.get("id") or "")
    if not re.fullmatch(r"[0-9]{8,}", post_id):
        return None, "invalid_post_id"
    source_uid = compact_text(row.get("squareUid")) or None
    if expected_uid and source_uid != expected_uid:
        return None, "unexpected_source_uid"
    web_link = compact_text(row.get("webLink"))
    link = urllib.parse.urlparse(web_link)
    link_id = POST_ID_RE.search(link.path)
    if link.scheme != "https" or link.hostname not in {"www.binance.com", "binance.com"} or not link_id or link_id.group(1) != post_id:
        return None, "missing_canonical_identity"
    body = compact_text(row.get("bodyTextOnly")) or structured_text(row.get("body"))
    if len(body) < minimum_text_chars:
        return None, "below_body_threshold"
    quality_reason, quality_signals = _quality_rejection(body)
    if quality_reason:
        return None, quality_reason
    published_at = iso_from_epoch(row.get("firstReleaseTime") or row.get("createTime"))
    if not published_at:
        return None, "missing_original_date"
    published = parse_utc_timestamp(published_at)
    if not (minus_calendar_years(current, 2) <= published <= current):
        return None, "outside_rolling_window"
    snapshot = parse_utc_timestamp(observed_at)
    if snapshot < published:
        return None, "snapshot_before_publication"
    username = compact_text(row.get("username")) or None
    display_name = compact_text(row.get("displayName")) or username or "Tác giả chưa rõ"
    title = compact_text(row.get("title")) or body[:120]
    if source_uid:
        entity_slug = None
        identity_status = "archive_embedded_source_uid"
    else:
        handle = username or display_name or post_id
        entity_slug = "archive-handle-" + hashlib.sha256(handle.casefold().encode("utf-8")).hexdigest()[:16]
        identity_status = "archive_handle_only"
    metadata = {
        "source_uid": source_uid,
        "entity_slug": entity_slug,
        "profile_url": f"https://www.binance.com/en/square/profile/{urllib.parse.quote(username)}" if username else None,
        "identity_evidence_url": archive_url,
        "identity_status": identity_status,
        "content_origin": "creator_post",
        "provenance_type": "archive_snapshot",
        "archive_snapshot_url": archive_url,
        "archive_original_url": original_url,
        "archive_observed_at": observed_at,
        "archive_response_sha256": response_sha256,
        "original_publication_time_evidence": {
            "field": "firstReleaseTime" if row.get("firstReleaseTime") is not None else "createTime",
            "raw_epoch_ms": row.get("firstReleaseTime") or row.get("createTime"),
            "normalized_utc": published_at,
        },
        "content_type": row.get("contentType"),
        "content_volume_goal": True,
        "qualifying_body": True,
        "quality_gate_version": QUALITY_GATE_VERSION,
        "quality_signals": quality_signals,
        "body_chars": len(body),
        "engagement": {
            "views": row.get("viewCount"),
            "likes": row.get("likeCount"),
            "comments": row.get("commentCount"),
            "shares": row.get("shareCount"),
            "followers": None,
        },
        "engagement_verification_level": "secondary",
        "engagement_observed_at": observed_at,
    }
    return ParsedItem(
        external_id=post_id,
        canonical_url=f"https://www.binance.com/en/square/post/{post_id}",
        title=title,
        text=body,
        author=display_name,
        username=username,
        published_at=published_at,
        language=row.get("language") or row.get("detectedLanguage") or row.get("detectedLang"),
        metadata=metadata,
        entity_category="square_creator_candidate",
    ), None


def parse_archive_profile(
    result: FetchResult,
    source: dict[str, Any],
    *,
    current: datetime,
) -> tuple[list[ParsedItem], dict[str, int]]:
    observed_at = _snapshot_time(result.url)
    records = _post_records(_app_data(result.body))
    items: list[ParsedItem] = []
    rejects: dict[str, int] = {}
    for post_id in sorted(records, key=lambda value: int(value)):
        item, reason = _archive_item(
            records[post_id],
            archive_url=result.url,
            original_url=source["original_url"],
            response_sha256=result.response_hash,
            observed_at=observed_at,
            current=current,
            minimum_text_chars=int(source.get("minimum_text_chars", 200)),
            expected_uid=str(source.get("expected_source_uid") or "") or None,
        )
        if item:
            items.append(item)
        else:
            rejects[reason or "invalid"] = rejects.get(reason or "invalid", 0) + 1
    return items, rejects


def parse_archive_post(
    result: FetchResult,
    candidate: dict[str, Any],
    *,
    current: datetime,
    minimum_text_chars: int,
) -> tuple[ParsedItem | None, str | None]:
    records = _post_records(_app_data(result.body))
    row = records.get(str(candidate["post_id"]))
    if row is None:
        return None, "target_post_missing"
    return _archive_item(
        row,
        archive_url=result.url,
        original_url=str(candidate["original_url"]),
        response_sha256=result.response_hash,
        observed_at=_snapshot_time(result.url),
        current=current,
        minimum_text_chars=minimum_text_chars,
    )


def _source_permission(conn: Any, source: dict[str, Any]) -> Any:
    row = conn.execute("SELECT * FROM sources WHERE slug=?", (source["slug"],)).fetchone()
    if row is None or row["access_level"] != "public" or row["distribution"] != "public_shareable":
        raise PermissionError("Persisted content source is not public-shareable")
    return row


def _store_result(
    db_path: str | Path,
    source: dict[str, Any],
    result: FetchResult,
    items: list[ParsedItem],
    *,
    note: str,
    baseline_post_ids: set[str] | None = None,
) -> dict[str, Any]:
    seed_source_configs(db_path, [source])
    started = utc_now()
    accepted: list[dict[str, Any]] = []
    duplicates = 0
    rejected_permission = 0
    with connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        run_id = int(conn.execute(
            "INSERT INTO ingest_runs(started_at,status,notes) VALUES(?,?,?)",
            (started, "running", note),
        ).lastrowid)
        source_row = _source_permission(conn, source)
        for item in items:
            if item.external_id in (baseline_post_ids or set()):
                duplicates += 1
                continue
            existing = conn.execute(
                "SELECT id FROM contents WHERE canonical_url=?", (item.canonical_url,)
            ).fetchone()
            normalized = compact_text(item.text)
            checksum = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
            same_body = conn.execute(
                "SELECT content_id FROM content_versions WHERE checksum=? LIMIT 1", (checksum,)
            ).fetchone()
            if existing is not None or same_body is not None:
                duplicates += 1
                continue
            source_uid = item.metadata.get("source_uid")
            if source_uid:
                owner = conn.execute(
                    "SELECT public_shareable FROM entities WHERE source_uid=?", (source_uid,)
                ).fetchone()
                if owner is not None and owner["public_shareable"] != 1:
                    rejected_permission += 1
                    continue
            nc, nv, _duplicate = _store_item(conn, source_row, source, item, result.fetched_at)
            content = conn.execute(
                "SELECT id FROM contents WHERE source_id=? AND external_id=?",
                (source_row["id"], item.external_id),
            ).fetchone()
            if nc and nv and content:
                accepted.append({
                    "post_id": item.external_id,
                    "canonical_url": item.canonical_url,
                    "title": item.title,
                    "author": item.author,
                    "source_uid": item.metadata.get("source_uid"),
                    "published_at": item.published_at,
                    "body_chars": len(normalized),
                    "content_id": int(content["id"]),
                    "provenance_type": item.metadata.get("provenance_type", "retained_public_response"),
                    "source_snapshot_url": item.metadata.get("archive_snapshot_url") or source["url"],
                    "source_observed_at": item.metadata.get("archive_observed_at") or item.metadata.get("retained_capture_observed_at") or result.fetched_at,
                    "original_publication_time_evidence": item.metadata.get("original_publication_time_evidence"),
                    "response_sha256": result.response_hash,
                })
        finished = utc_now()
        conn.execute(
            """INSERT INTO fetch_events(run_id,source_id,fetched_at,status_code,outcome,latency_ms,byte_count,
                   content_type,response_hash,error_class,error_message) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (run_id, source_row["id"], result.fetched_at, result.status, "success", result.latency_ms,
             len(result.body), result.content_type, result.response_hash, None, None),
        )
        fields = sorted({key for item in items for key, value in item.metadata.items() if value is not None})
        conn.execute(
            """UPDATE sources SET capability_status='available',capability_detail=?,available_fields_json=?,
                   last_attempt_at=?,last_success_at=?,updated_at=? WHERE id=?""",
            (f"Parsed {len(items)} qualifying bodies; accepted {len(accepted)} new canonical posts",
             json.dumps(fields), result.fetched_at, result.fetched_at, finished, source_row["id"]),
        )
        conn.execute(
            """UPDATE ingest_runs SET finished_at=?,status='success',source_count=1,fetch_success=1,
                   new_contents=?,new_versions=?,duplicates=?,errors=0,duration_ms=? WHERE id=?""",
            (finished, len(accepted), len(accepted), duplicates, result.latency_ms, run_id),
        )
        if accepted:
            conn.execute(
                "UPDATE content_volume_state SET last_content_at=?,updated_at=? WHERE id=1",
                (finished, finished),
            )
        conn.commit()
    return {
        "run_id": run_id,
        "accepted": accepted,
        "accepted_count": len(accepted),
        "duplicates": duplicates,
        "rejected_permission": rejected_permission,
    }


def _safe_artifact_dir(path: str | Path) -> Path:
    project_root = Path(__file__).resolve().parents[2]
    output = Path(path).resolve()
    if output != project_root and project_root not in output.parents:
        raise ValueError("Content-volume artifacts must stay inside the project root")
    output.mkdir(parents=True, exist_ok=True)
    return output


def _save_raw(root: Path, label: str, result: FetchResult) -> str:
    safe_label = re.sub(r"[^A-Za-z0-9._-]+", "-", label).strip("-")[:120]
    path = root / "raw" / f"{safe_label}-{result.response_hash[:16]}.body"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_bytes(result.body)
        temporary.replace(path)
    return str(path)


def _record_fetch_import(fetcher: Callable[..., FetchResult], result: FetchResult, count: int) -> None:
    recorder = getattr(fetcher, "record_imported", None)
    if callable(recorder):
        if not hasattr(result, "executor_metadata"):
            result.executor_metadata = {"node": "desktop"}
        recorder(result, int(count))

def _content_state(db_path: str | Path, manifest: dict[str, Any], now: datetime) -> None:
    baseline = manifest["baseline"]
    with connect(db_path) as conn:
        conn.execute(
            """INSERT INTO content_volume_state(
                   id,baseline_started_at,baseline_article_count,created_at,updated_at
               ) VALUES(1,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET updated_at=excluded.updated_at""",
            (baseline["started_at"], int(baseline["article_count"]), _iso(now), _iso(now)),
        )
        conn.commit()


def _archive_source(manifest: dict[str, Any], fetch_url: str | None = None, slug: str | None = None) -> dict[str, Any]:
    base = dict(manifest["archive_source"])
    if fetch_url:
        base["fetch_url"] = fetch_url
        base["url"] = fetch_url
    if slug:
        base["slug"] = slug
    return base


def _build_content_fetcher(
    db_path: str | Path,
    local_fetcher: Callable[..., FetchResult],
    *,
    config_path: str | Path | None = None,
    bridge_factory: Callable[..., Any] | None = None,
    clock: Callable[[], datetime] | None = None,
) -> Callable[..., FetchResult]:
    project_root = Path(__file__).resolve().parents[2]
    path = Path(config_path).resolve() if config_path else project_root / "runtime" / "msi-fetch-enabled.json"
    if not path.is_file():
        return local_fetcher
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or set(document) - {"enabled", "mode"}:
        raise ValueError("MSI fetch activation config has unsupported fields")
    if document.get("enabled") is not True:
        return local_fetcher
    mode = str(document.get("mode") or "hybrid")
    if mode != "hybrid":
        raise ValueError("MSI fetch mode must be hybrid")

    if bridge_factory is None:
        bridge_path = project_root / "ops" / "msi-fetch-worker" / "bridge.py"
        if not bridge_path.is_file():
            raise RuntimeError("Reviewed MSI bridge is unavailable while activation is enabled")
        spec = importlib.util.spec_from_file_location("bti_reviewed_msi_bridge", bridge_path)
        if spec is None or spec.loader is None:
            raise RuntimeError("Reviewed MSI bridge could not be loaded")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        bridge_factory = module.HybridArchiveFetcher

    current_time = clock or (lambda: datetime.now(timezone.utc))

    def authorization_check(url: str) -> bool:
        parsed = urllib.parse.urlparse(url)
        _validate_archive_url(url, allow_cdx=parsed.path == "/cdx/search/cdx")
        cooldown = _archive_cooldown(db_path, _utc(current_time()))
        if cooldown:
            raise AccessStopped(
                "Shared persisted archive cooldown denies MSI and desktop dispatch",
                status_code=cooldown.get("status_code"),
            )
        return True

    return bridge_factory(local_fetcher, authorization_check=authorization_check, mode=mode)



def _dispatch_archive_url(
    fetcher: Callable[..., FetchResult],
    url: str,
    *,
    coordinator: FetchCoordinator,
    allow_cdx: bool = False,
) -> FetchResult:
    """Route non-English Square URLs through desktop local_fetcher when hybrid MSI bridge is active."""
    local_fn = getattr(fetcher, "local_fetcher", None)
    auth_fn = getattr(fetcher, "authorization_check", None)
    if callable(local_fn) and ("/vi/square/" in url or "www.binance.com%2fvi%2fsquare" in url.lower()):
        if callable(auth_fn):
            auth_fn(url)
        result = local_fn(url, coordinator=coordinator, allow_cdx=allow_cdx)
        result.executor_metadata = {"node": "desktop"}
    elif allow_cdx:
        result = fetcher(url, coordinator=coordinator, allow_cdx=True)
    else:
        result = fetcher(url, coordinator=coordinator)
    # Network filters can return a block page with HTTP 200 on either executor.
    # Match the page title, never a phrase quoted inside an ordinary article.
    title = re.search(rb"<title\b[^>]*>(.*?)</title\s*>", result.body, re.I | re.S)
    if title and re.search(
        rb"website\s+blocked|access\s+denied|request\s+blocked|just\s+a\s+moment|captcha|human\s+verification",
        title.group(1), re.I,
    ):
        coordinator.trip(result.status)
        raise AccessStopped("Archive response presented an access-control page", status_code=result.status)
    return result

def _cached_source(entry: dict[str, Any]) -> dict[str, Any]:
    post_id = str(entry["post_id"])
    uid = str(entry["expected_source_uid"])
    return {
        "slug": f"retained-square-detail-{post_id}",
        "name": f"Retained public Square detail {post_id}",
        "url": f"https://www.binance.com/en/square/post/{post_id}",
        "fetch_url": str(entry["fetch_url"]),
        "source_type": "binance_square_creator_posts",
        "adapter": "square_detail",
        "access_level": "public",
        "distribution": "public_shareable",
        "expected_pillar": "engagement",
        "historical_backfill": True,
        "expected_post_id": post_id,
        "expected_source_uid": uid,
        "minimum_text_chars": int(entry.get("minimum_text_chars", 1000)),
        "required_content_type": int(entry.get("required_content_type", 2)),
        "earliest_published_at": str(entry["earliest_published_at"]),
        "latest_published_at": str(entry["latest_published_at"]),
        "entity": {
            "slug": "square-uid-" + hashlib.sha256(uid.encode("utf-8")).hexdigest()[:16],
            "source_uid": uid,
            "display_name": str(entry["display_name"]),
            "username": str(entry["username"]),
            "category": "square_creator_candidate",
            "identity_status": "source_profile_only",
            "identity_evidence_url": f"https://www.binance.com/en/square/post/{post_id}",
        },
    }


def _already_successful(db_path: str | Path, slug: str) -> bool:
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT capability_status,last_success_at FROM sources WHERE slug=?", (slug,)
        ).fetchone()
        return bool(row and row["capability_status"] == "available" and row["last_success_at"])


def import_cached_details(
    db_path: str | Path,
    manifest: dict[str, Any],
    *,
    artifact_root: Path,
) -> dict[str, Any]:
    project_root = Path(__file__).resolve().parents[2]
    accepted: list[dict[str, Any]] = []
    duplicates = errors = skipped = quality_rejected = 0
    baseline_ids = {str(value) for value in manifest.get("baseline", {}).get("qualifying_post_ids", [])}
    for entry in manifest["cached_details"]:
        source = _cached_source(entry)
        if _already_successful(db_path, source["slug"]):
            skipped += 1
            continue
        capture = (project_root / str(entry["path"])).resolve()
        if project_root not in capture.parents or not capture.is_file():
            errors += 1
            continue
        try:
            raw = capture.read_bytes()
            if hashlib.sha256(raw).hexdigest() != str(entry["sha256"]):
                raise ValueError("Retained capture SHA-256 did not match the verified manifest")
            result = FetchResult(
                url=source["fetch_url"], status=200, content_type="application/json; retained-public-response",
                body=raw, latency_ms=0, fetched_at=str(entry["observed_at"]),
            )
            items = parse_square_detail(result, source)
            payload = json.loads(raw.decode("utf-8"))
            payload_row = payload.get("data") if isinstance(payload, dict) else {}
            if isinstance(payload_row, dict) and isinstance(payload_row.get("data"), dict):
                payload_row = payload_row["data"]
            qualified_items: list[ParsedItem] = []
            for item in items:
                quality_reason, quality_signals = _quality_rejection(compact_text(item.text))
                if quality_reason:
                    quality_rejected += 1
                    continue
                item.metadata.update({
                    "provenance_type": "retained_public_response",
                    "retained_capture_sha256": result.response_hash,
                    "retained_capture_observed_at": result.fetched_at,
                    "content_volume_goal": True,
                    "qualifying_body": True,
                    "quality_gate_version": QUALITY_GATE_VERSION,
                    "quality_signals": quality_signals,
                    "body_chars": len(compact_text(item.text)),
                    "original_publication_time_evidence": {
                        "field": "firstReleaseTime" if payload_row.get("firstReleaseTime") is not None else "createTime",
                        "raw_epoch_ms": payload_row.get("firstReleaseTime") or payload_row.get("createTime"),
                        "normalized_utc": item.published_at,
                    },
                })
                qualified_items.append(item)
            _save_raw(artifact_root, f"cached-{entry['post_id']}", result)
            outcome = _store_result(
                db_path, source, result, qualified_items, note="Reprocess authentic retained public Square detail",
                baseline_post_ids=baseline_ids,
            )
            accepted.extend(outcome["accepted"])
            duplicates += outcome["duplicates"]
        except Exception:
            errors += 1
    return {
        "accepted": accepted, "accepted_count": len(accepted), "duplicates": duplicates,
        "errors": errors, "skipped": skipped, "quality_rejected": quality_rejected,
    }

def import_cached_award_rosters(
    db_path: str | Path,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    project_root = Path(__file__).resolve().parents[2]
    rosters = manifest.get("cached_award_rosters") or []
    new_contents = new_versions = duplicates = errors = skipped = 0
    for entry in rosters:
        slug = str(entry.get("slug") or "blockchain-100-2025-creator-rosters")
        if _already_successful(db_path, slug):
            skipped += 1
            continue
        capture = (project_root / str(entry.get("path") or "")).resolve()
        if project_root not in capture.parents or not capture.is_file():
            errors += 1
            continue
        try:
            raw = capture.read_bytes()
            if hashlib.sha256(raw).hexdigest() != str(entry["sha256"]):
                raise ValueError("Retained award roster SHA-256 did not match the verified manifest")
            canonical_award_url = str(entry.get("url") or "https://www.binance.com/en/square/blockchain-100-2025")
            source = {
                "slug": slug,
                "name": str(entry.get("name") or "Binance Blockchain 100 Multi-Category Official Roster"),
                "url": str(entry.get("source_url") or f"{canonical_award_url}#{slug}"),
                "fetch_url": str(entry.get("fetch_url") or "https://www.binance.com/bapi/composite/v1/public/pgc/activity/cact25/winners"),
                "source_type": "binance_official_award_winners",
                "adapter": "award_winners",
                "access_level": "public",
                "distribution": "public_shareable",
                "expected_pillar": "recognition",
                "program_name": str(entry.get("program_name") or "Blockchain 100 2025"),
                "period_label": str(entry.get("period_label") or "2025"),
                "include_categories": entry.get("include_categories", [1, 2, 4, 5, 6, 7]),
                "expected_count": entry.get("expected_count"),
            }
            result = FetchResult(
                url=source["fetch_url"], status=200, content_type="application/json; retained-public-response",
                body=raw, latency_ms=0, fetched_at=str(entry["observed_at"]),
            )
            items = parse_award_winners(result, source)
            for item in items:
                item.canonical_url = canonical_award_url
                item.metadata["identity_evidence_url"] = canonical_award_url
            payload = json.loads(raw.decode("utf-8"))
            groups = (payload.get("data") or {}).get("groupList", []) if isinstance(payload, dict) else []
            seed_source_configs(db_path, [source])
            started = utc_now()
            with connect(db_path) as conn:
                conn.execute("BEGIN IMMEDIATE")
                run_id = int(conn.execute(
                    "INSERT INTO ingest_runs(started_at,status,notes) VALUES(?,?,?)",
                    (started, "running", "Import retained official multi-category Blockchain 100 roster"),
                ).lastrowid)
                source_row = _source_permission(conn, source)
                nc_total = nv_total = dup_total = 0
                for item in items:
                    nc, nv, dup = _store_item(conn, source_row, source, item, result.fetched_at)
                    nc_total += nc
                    nv_total += nv
                    dup_total += dup
                for g in groups:
                    for w in g.get("winners") or []:
                        uid = compact_text(w.get("squareUid"))
                        bio = compact_text(w.get("biography"))
                        if uid and bio:
                            conn.execute(
                                "UPDATE entities SET bio=COALESCE(?,bio) WHERE source_uid=? AND public_shareable=1",
                                (bio, uid),
                            )
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
                    (f"Imported {len(items)} official multi-category Blockchain 100 awardees",
                     json.dumps(fields), result.fetched_at, result.fetched_at, finished, source_row["id"]),
                )
                conn.execute(
                    """UPDATE ingest_runs SET finished_at=?,status='success',source_count=1,fetch_success=1,
                           new_contents=?,new_versions=?,duplicates=?,errors=0,duration_ms=0 WHERE id=?""",
                    (finished, nc_total, nv_total, dup_total, run_id),
                )
                conn.commit()
            new_contents += nc_total
            new_versions += nv_total
            duplicates += dup_total
        except Exception:
            errors += 1
    return {
        "new_contents": new_contents,
        "new_versions": new_versions,
        "duplicates": duplicates,
        "errors": errors,
        "skipped": skipped,
    }


def _profile_snapshot_done(db_path: str | Path, slug: str, *, allow_terminal_skip: bool) -> bool:
    if _already_successful(db_path, slug):
        return True
    if not allow_terminal_skip:
        return False
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT capability_status,last_attempt_at FROM sources WHERE slug=?", (slug,)
        ).fetchone()
        return bool(row and row["capability_status"] == "unavailable" and row["last_attempt_at"])


def _mark_profile_source_unavailable(db_path: str | Path, source: dict[str, Any], detail: str, now_iso: str) -> None:
    seed_source_configs(db_path, [source])
    with connect(db_path) as conn:
        conn.execute(
            """UPDATE sources SET capability_status='unavailable',capability_detail=?,
                   last_attempt_at=?,updated_at=? WHERE slug=?""",
            (detail, now_iso, now_iso, source["slug"]),
        )
        conn.commit()


def import_profile_snapshots(
    db_path: str | Path,
    manifest: dict[str, Any],
    *,
    artifact_root: Path,
    coordinator: FetchCoordinator,
    current: datetime,
    fetcher: Callable[..., FetchResult] = fetch_archive,
) -> dict[str, Any]:
    accepted: list[dict[str, Any]] = []
    duplicates = errors = skipped = fetched = attempted = blocked = 0
    consecutive_errors = 0
    rejected: dict[str, int] = {}
    baseline_ids = {str(value) for value in manifest.get("baseline", {}).get("qualifying_post_ids", [])}
    throughput = manifest.get("throughput") or {}
    max_per_cycle = max(1, int(throughput.get("max_profile_snapshots_per_cycle", 8)))
    now_iso = _iso(current)
    for entry in manifest["profile_snapshots"]:
        archive_url = str(entry["archive_url"])
        slug = str(entry.get("slug") or ("wayback-square-profile-" + str(entry["snapshot_timestamp"])))
        expected_hash = str(entry.get("expected_sha256") or "")
        source = _archive_source(manifest, archive_url, slug)
        source.update({
            "name": f"Static Square profile archive — {entry.get('username') or entry['snapshot_timestamp']} ({entry['snapshot_timestamp']})",
            "original_url": str(entry["original_url"]),
            "expected_source_uid": str(entry["expected_source_uid"]),
            "minimum_text_chars": int(manifest.get("minimum_text_chars", 200)),
        })
        if _profile_snapshot_done(db_path, slug, allow_terminal_skip=not bool(expected_hash)):
            skipped += 1
            continue
        if attempted >= max_per_cycle:
            break
        try:
            attempted += 1
            result = _dispatch_archive_url(fetcher, archive_url, coordinator=coordinator)
            fetched += 1
            if expected_hash and result.response_hash != expected_hash:
                raise ValueError("Static profile snapshot hash changed from the verified capture")
            raw_path = _save_raw(artifact_root, f"profile-{entry['snapshot_timestamp']}", result)
            items, item_rejects = parse_archive_profile(result, source, current=current)
            for key, value in item_rejects.items():
                rejected[key] = rejected.get(key, 0) + value
            outcome = _store_result(
                db_path, source, result, items, note="Static public archive profile extraction",
                baseline_post_ids=baseline_ids,
            )
            _record_fetch_import(fetcher, result, len(outcome["accepted"]))
            for item in outcome["accepted"]:
                item["raw_artifact"] = raw_path
            accepted.extend(outcome["accepted"])
            duplicates += outcome["duplicates"]
            consecutive_errors = 0
        except AccessStopped as exc:
            if not expected_hash and (
                exc.status_code in {404, 410}
                or (exc.status_code in {301, 302, 303, 307, 308} and _same_static_post_replay_redirect(archive_url, exc.redirect_location))
                or (exc.status_code in {500, 502, 503, 504} and not exc.retry_after_seconds)
            ):
                reason = f"archive_profile_http_{exc.status_code}"
                rejected[reason] = rejected.get(reason, 0) + 1
                _mark_profile_source_unavailable(db_path, source, reason, now_iso)
                continue
            errors += 1
            blocked += 1
            _set_archive_cooldown(db_path, current, exc.status_code, exc.retry_after_seconds)
            break
        except (BodyTooLarge, ValueError) as exc:
            if not expected_hash:
                reason = type(exc).__name__
                rejected[reason] = rejected.get(reason, 0) + 1
                _mark_profile_source_unavailable(db_path, source, reason, now_iso)
                continue
            errors += 1
            consecutive_errors += 1
            if consecutive_errors >= 3:
                break
        except Exception:
            errors += 1
            consecutive_errors += 1
            if consecutive_errors >= 3:
                break
    return {
        "accepted": accepted, "accepted_count": len(accepted), "duplicates": duplicates,
        "errors": errors, "blocked": blocked, "skipped": skipped, "attempted": attempted,
        "fetched": fetched, "rejected": rejected,
    }


def _partition_specs(current: datetime) -> list[dict[str, Any]]:
    today = current.date()
    window_start = minus_calendar_years(current, 2).date()
    recent_start = max(window_start, today - timedelta(days=29))
    specs: list[dict[str, Any]] = []
    historical_end = recent_start - timedelta(days=1)
    cursor = window_start
    while cursor <= historical_end:
        next_month = date(cursor.year + (1 if cursor.month == 12 else 0), 1 if cursor.month == 12 else cursor.month + 1, 1)
        partition_end = min(historical_end, next_month - timedelta(days=1))
        specs.append({
            "partition_key": f"{cursor:%Y%m%d}-{partition_end:%Y%m%d}",
            "date_from": cursor.strftime("%Y%m%d"),
            "date_to": partition_end.strftime("%Y%m%d"),
            "priority": 2,
            "coverage_class": "historical",
        })
        cursor = partition_end + timedelta(days=1)
    cursor = recent_start
    while cursor <= today:
        age_days = (today - cursor).days
        specs.append({
            "partition_key": f"{cursor:%Y%m%d}-{cursor:%Y%m%d}",
            "date_from": cursor.strftime("%Y%m%d"),
            "date_to": cursor.strftime("%Y%m%d"),
            "priority": 0 if age_days <= 6 else 1,
            "coverage_class": "recent_7d" if age_days <= 6 else "recent_30d",
        })
        cursor += timedelta(days=1)
    return specs


def _seed_cdx_partitions(
    db_path: str | Path,
    current: datetime,
    *,
    include_vietnamese_locale: bool = False,
) -> int:
    now_iso = _iso(current)
    specs = _partition_specs(current)
    with connect(db_path) as conn:
        before = int(conn.execute("SELECT COUNT(*) FROM content_discovery_partitions").fetchone()[0])
        for spec in specs:
            conn.execute(
                """INSERT INTO content_discovery_partitions(
                       partition_key,date_from,date_to,priority,coverage_class,status,created_at,updated_at
                   ) VALUES(?,?,?,?,?,'pending',?,?)
                   ON CONFLICT(partition_key) DO UPDATE SET
                       priority=excluded.priority,coverage_class=excluded.coverage_class,
                       updated_at=excluded.updated_at""",
                (spec["partition_key"], spec["date_from"], spec["date_to"], spec["priority"],
                 spec["coverage_class"], now_iso, now_iso),
            )
        frontier_exists = conn.execute(
            "SELECT 1 FROM content_discovery_partitions WHERE partition_key LIKE 'frontier-full-%' LIMIT 1"
        ).fetchone()
        if frontier_exists is None:
            window_start = minus_calendar_years(current, 2).date().strftime("%Y%m%d")
            window_end = current.date().strftime("%Y%m%d")
            conn.execute(
                """INSERT INTO content_discovery_partitions(
                       partition_key,date_from,date_to,priority,coverage_class,status,created_at,updated_at
                   ) VALUES(?,?,?,?,?,'pending',?,?)""",
                (f"frontier-full-{window_start}-{window_end}", window_start, window_end, 2,
                 "historical", now_iso, now_iso),
            )
        if include_vietnamese_locale:
            window_start = minus_calendar_years(current, 2).date().strftime("%Y%m%d")
            window_end = current.date().strftime("%Y%m%d")
            recent_vi_start = max(window_start, "20250101")
            for prefix, part_key, p_start, p_prio in (
                ("vi-recent-%", f"vi-recent-{recent_vi_start}-{window_end}", recent_vi_start, 2),
                ("vi-frontier-full-%", f"vi-frontier-full-{window_start}-{window_end}", window_start, 2),
            ):
                exists = conn.execute(
                    "SELECT 1 FROM content_discovery_partitions WHERE partition_key LIKE ? LIMIT 1",
                    (prefix,),
                ).fetchone()
                if exists is None:
                    conn.execute(
                        """INSERT INTO content_discovery_partitions(
                               partition_key,date_from,date_to,priority,coverage_class,status,created_at,updated_at
                           ) VALUES(?,?,?,?,?,'pending',?,?)""",
                        (part_key, p_start, window_end, p_prio, "historical", now_iso, now_iso),
                    )
        after = int(conn.execute("SELECT COUNT(*) FROM content_discovery_partitions").fetchone()[0])
        conn.commit()
    return after - before

def _cdx_url(
    manifest: dict[str, Any],
    current: datetime,
    *,
    partition: dict[str, Any] | None = None,
    resume_key: str | None = None,
) -> str:
    cdx = manifest["cdx"]
    start = str(partition["date_from"]) if partition else minus_calendar_years(current, 2).date().strftime("%Y%m%d")
    end = str(partition["date_to"]) if partition else current.date().strftime("%Y%m%d")
    rows = min(int(cdx.get("max_rows", 250)), CDX_HARD_MAX_ROWS)
    is_vi_partition = bool(partition and str(partition.get("partition_key") or "").startswith("vi-"))
    target_url_pattern = (
        "www.binance.com/vi/square/post/*" if is_vi_partition else "www.binance.com/en/square/post/*"
    )
    params = [
        ("url", target_url_pattern),
        ("output", "json"),
        ("filter", "statuscode:200"),
        ("filter", "mimetype:text/html"),
        ("from", start),
        ("to", end),
        ("limit", str(rows)),
        ("collapse", "urlkey"),
        ("fl", "urlkey,timestamp,original,mimetype,statuscode,digest,length"),
        ("showResumeKey", "true"),
    ]
    if resume_key:
        if len(resume_key) > 2048 or any(ord(character) < 32 for character in resume_key):
            raise ValueError("CDX resume key is outside the bounded safe format")
        params.append(("resumeKey", resume_key))
    return "https://web.archive.org/cdx/search/cdx?" + urllib.parse.urlencode(params)


def _parse_cdx_payload(payload: Any) -> tuple[list[list[str]], str | None]:
    header = ["urlkey", "timestamp", "original", "mimetype", "statuscode", "digest", "length"]
    if payload == []:
        return [], None
    if not isinstance(payload, list) or not payload or payload[0] != header:
        raise ValueError("CDX response schema was not the bounded expected projection")
    rows: list[list[str]] = []
    resume_key: str | None = None
    index = 1
    while index < len(payload):
        raw_row = payload[index]
        if raw_row == []:
            if index + 1 >= len(payload) or not isinstance(payload[index + 1], list) or len(payload[index + 1]) != 1:
                raise ValueError("CDX response had a malformed resumption key trailer")
            candidate_key = payload[index + 1][0]
            if not isinstance(candidate_key, str) or not candidate_key or len(candidate_key) > 2048:
                raise ValueError("CDX response had an invalid resumption key")
            resume_key = candidate_key
            if index + 2 != len(payload):
                raise ValueError("CDX response had data after its resumption key")
            break
        if not isinstance(raw_row, list) or len(raw_row) != 7 or not all(isinstance(value, str) for value in raw_row):
            raise ValueError("CDX response contained a malformed result row")
        rows.append(raw_row)
        index += 1
    return rows, resume_key


def _archive_cooldown(db_path: str | Path, current: datetime) -> dict[str, Any] | None:
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT archive_cooldown_until,archive_last_blocked_status FROM content_volume_state WHERE id=1"
        ).fetchone()
    if row is None or not row["archive_cooldown_until"]:
        return None
    until = parse_utc_timestamp(row["archive_cooldown_until"])
    if until <= current:
        return None
    return {
        "until": _iso(until),
        "status_code": row["archive_last_blocked_status"],
        "manual_hold": row["archive_cooldown_until"] == ARCHIVE_MANUAL_HOLD_UNTIL,
    }


def _set_archive_cooldown(
    db_path: str | Path,
    current: datetime,
    status_code: int | None,
    retry_after_seconds: int | None = None,
) -> str:
    default_seconds = 21600 if status_code == 429 else 86400
    provider_seconds = max(retry_after_seconds or 0, 0)
    if provider_seconds > ARCHIVE_MAX_AUTOMATIC_RETRY_AFTER_SECONDS:
        until = ARCHIVE_MANUAL_HOLD_UNTIL
    else:
        until = _iso(current + timedelta(seconds=max(default_seconds, provider_seconds)))
    with connect(db_path) as conn:
        conn.execute(
            """UPDATE content_volume_state SET archive_cooldown_until=?,archive_last_blocked_status=?,
                   updated_at=? WHERE id=1""",
            (until, status_code, _iso(current)),
        )
        conn.commit()
    return until


def _partition_summary(db_path: str | Path) -> dict[str, Any]:
    with connect(db_path) as conn:
        has_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='content_discovery_partitions'"
        ).fetchone()
        if not has_table:
            return {"status": {}, "coverage": {}}
        status = {
            row["status"]: int(row["count"])
            for row in conn.execute(
                "SELECT status,COUNT(*) AS count FROM content_discovery_partitions GROUP BY status"
            ).fetchall()
        }
        coverage = {
            row["coverage_class"]: {
                "partitions": int(row["partitions"]),
                "complete": int(row["complete"]),
                "rows_seen": int(row["rows_seen"]),
                "new_candidates": int(row["new_candidates"]),
            }
            for row in conn.execute(
                """SELECT coverage_class,COUNT(*) AS partitions,
                          SUM(CASE WHEN status='complete' THEN 1 ELSE 0 END) AS complete,
                          SUM(rows_seen) AS rows_seen,SUM(new_candidates) AS new_candidates
                   FROM content_discovery_partitions GROUP BY coverage_class"""
            ).fetchall()
        }
    return {"status": status, "coverage": coverage}

def discover_cdx(
    db_path: str | Path,
    manifest: dict[str, Any],
    *,
    artifact_root: Path,
    coordinator: FetchCoordinator,
    current: datetime,
    fetcher: Callable[..., FetchResult] = fetch_archive,
) -> dict[str, Any]:
    seeded = _seed_cdx_partitions(
        db_path, current,
        include_vietnamese_locale=bool(manifest.get("cdx", {}).get("include_vietnamese_locale")),
    )
    cooldown = _archive_cooldown(db_path, current)
    if cooldown:
        return {
            "status": "host_cooldown", "requests": 0, "rows": 0, "new_candidates": 0,
            "cooldown": cooldown, "partitions_seeded": seeded, "partitions": _partition_summary(db_path),
        }
    cadence = max(600, int(manifest["cdx"].get("cadence_seconds", 600)))
    with connect(db_path) as conn:
        state = conn.execute("SELECT last_discovery_at FROM content_volume_state WHERE id=1").fetchone()
    if state and state["last_discovery_at"]:
        previous = parse_utc_timestamp(state["last_discovery_at"])
        if previous + timedelta(seconds=cadence) > current:
            return {
                "status": "not_due", "requests": 0, "rows": 0, "new_candidates": 0,
                "partitions_seeded": seeded, "partitions": _partition_summary(db_path),
            }
    request_budget = min(
        int(manifest["cdx"].get("max_requests_per_cycle", 1)),
        CDX_HARD_MAX_REQUESTS_PER_CYCLE,
    )
    now_iso = _iso(current)
    attempted_requests = successful_requests = rows_seen = new_candidates = unique_posts = 0
    response_hashes: list[str] = []
    raw_artifacts: list[str] = []
    terminal_status = "success"
    blocked_status: int | None = None
    for request_number in range(request_budget):
        with connect(db_path) as conn:
            partition_row = None
            if request_budget >= 2 and request_number == request_budget - 1:
                partition_row = conn.execute(
                    """SELECT * FROM content_discovery_partitions
                       WHERE partition_key LIKE 'frontier-full-%'
                         AND status IN ('pending','resumable','cooldown')
                         AND (next_attempt_at IS NULL OR next_attempt_at<=?)
                       ORDER BY id LIMIT 1""",
                    (now_iso,),
                ).fetchone()
            if partition_row is None:
                partition_row = conn.execute(
                    """SELECT * FROM content_discovery_partitions
                       WHERE partition_key NOT LIKE 'frontier-full-%'
                         AND status IN ('pending','resumable','cooldown')
                         AND (next_attempt_at IS NULL OR next_attempt_at<=?)
                       ORDER BY priority ASC,COALESCE(last_attempt_at,'') ASC,date_from DESC,partition_key
                       LIMIT 1""",
                    (now_iso,),
                ).fetchone()
        if partition_row is None:
            break
        partition = dict(partition_row)
        url = _cdx_url(manifest, current, partition=partition, resume_key=partition.get("resume_key"))
        source = _archive_source(manifest, url, "wayback-square-cdx")
        source["name"] = "Public archive index — partitioned Binance Square posts"
        try:
            attempted_requests += 1
            result = _dispatch_archive_url(fetcher, url, coordinator=coordinator, allow_cdx=True)
            successful_requests += 1
            raw_path = _save_raw(
                artifact_root,
                f"cdx-{partition['partition_key']}-page-{int(partition['pages_fetched']) + 1}",
                result,
            )
            raw_artifacts.append(raw_path)
            response_hashes.append(result.response_hash)
            payload = json.loads(result.body.decode("utf-8"))
            raw_rows, resume_key = _parse_cdx_payload(payload)
            selected: dict[str, dict[str, Any]] = {}
            for raw_row in raw_rows:
                _urlkey, timestamp, original, mimetype, statuscode, digest, length = raw_row
                parsed = urllib.parse.urlparse(original)
                match = POST_ID_RE.search(parsed.path)
                if parsed.scheme != "https" or parsed.hostname not in {"www.binance.com", "binance.com"} or not match:
                    continue
                if mimetype != "text/html" or statuscode != "200" or not re.fullmatch(r"[0-9]{14}", timestamp):
                    continue
                if not str(partition["date_from"]) <= timestamp[:8] <= str(partition["date_to"]):
                    continue
                post_id = match.group(1)
                candidate = {
                    "post_id": post_id,
                    "canonical_url": f"https://www.binance.com/en/square/post/{post_id}",
                    "original_url": original,
                    "snapshot_timestamp": timestamp,
                    "archive_url": f"https://web.archive.org/web/{timestamp}id_/{original}",
                    "cdx_digest": digest,
                    "cdx_length": int(length) if length.isdigit() else None,
                }
                previous_candidate = selected.get(post_id)
                candidate_score = (bool(parsed.query), -(candidate["cdx_length"] or 0), len(original), timestamp)
                if previous_candidate is None:
                    selected[post_id] = candidate
                else:
                    previous_parsed = urllib.parse.urlparse(previous_candidate["original_url"])
                    previous_score = (
                        bool(previous_parsed.query), -(previous_candidate["cdx_length"] or 0),
                        len(previous_candidate["original_url"]), previous_candidate["snapshot_timestamp"],
                    )
                    if candidate_score < previous_score:
                        selected[post_id] = candidate
            with connect(db_path) as conn:
                before = int(conn.execute("SELECT COUNT(*) FROM content_archive_candidates").fetchone()[0])
                for candidate in selected.values():
                    conn.execute(
                        """INSERT INTO content_archive_candidates(
                               post_id,canonical_url,original_url,snapshot_timestamp,archive_url,cdx_digest,cdx_length,
                               status,discovered_at,updated_at
                           ) VALUES(?,?,?,?,?,?,?,'pending',?,?)
                           ON CONFLICT(post_id) DO NOTHING""",
                        (candidate["post_id"], candidate["canonical_url"], candidate["original_url"],
                         candidate["snapshot_timestamp"], candidate["archive_url"], candidate["cdx_digest"],
                         candidate["cdx_length"], now_iso, now_iso),
                    )
                after = int(conn.execute("SELECT COUNT(*) FROM content_archive_candidates").fetchone()[0])
                added = after - before
                conn.execute(
                    """UPDATE content_discovery_partitions SET status=?,resume_key=?,pages_fetched=pages_fetched+1,
                           rows_seen=rows_seen+?,new_candidates=new_candidates+?,last_attempt_at=?,next_attempt_at=NULL,
                           last_outcome='success',updated_at=? WHERE id=?""",
                    ("resumable" if resume_key else "complete", resume_key, len(raw_rows), added,
                     now_iso, now_iso, partition["id"]),
                )
                conn.execute(
                    """UPDATE content_volume_state SET last_discovery_at=?,last_cdx_sha256=?,
                           last_cdx_row_count=?,updated_at=? WHERE id=1""",
                    (now_iso, result.response_hash, len(raw_rows), now_iso),
                )
                conn.commit()
            _store_result(db_path, source, result, [], note="Resumable partitioned public archive CDX discovery")
            _record_fetch_import(fetcher, result, 0)
            rows_seen += len(raw_rows)
            new_candidates += added
            unique_posts += len(selected)
        except AccessStopped as exc:
            blocked_status = exc.status_code
            terminal_status = "blocked"
            cooldown_until = _set_archive_cooldown(
                db_path, current, exc.status_code, exc.retry_after_seconds,
            )
            with connect(db_path) as conn:
                conn.execute(
                    """UPDATE content_discovery_partitions SET status='cooldown',last_attempt_at=?,next_attempt_at=?,
                           last_outcome='access_stopped',updated_at=? WHERE id=?""",
                    (now_iso, cooldown_until, now_iso, partition["id"]),
                )
                conn.commit()
            break
        except Exception as exc:
            terminal_status = "error"
            next_attempt = _iso(current + timedelta(hours=1))
            with connect(db_path) as conn:
                conn.execute(
                    """UPDATE content_discovery_partitions SET status='cooldown',last_attempt_at=?,next_attempt_at=?,
                           last_outcome=?,updated_at=? WHERE id=?""",
                    (now_iso, next_attempt, type(exc).__name__, now_iso, partition["id"]),
                )
                conn.commit()
            break
    return {
        "status": terminal_status,
        "status_code": blocked_status,
        "requests": successful_requests,
        "attempted_requests": attempted_requests,
        "rows": rows_seen,
        "new_candidates": new_candidates,
        "unique_posts_in_batch": unique_posts,
        "response_sha256": response_hashes[-1] if response_hashes else None,
        "response_hashes": response_hashes,
        "raw_artifacts": raw_artifacts,
        "partitions_seeded": seeded,
        "partitions": _partition_summary(db_path),
    }


def _adaptive_archive_budget(
    db_path: str | Path,
    manifest: dict[str, Any],
    *,
    explicit_max_fetch: int | None,
    requests_remaining: int,
    current: datetime,
) -> dict[str, Any]:
    throughput = manifest.get("throughput") or {}
    base = int(throughput.get("base_archive_fetches_per_cycle", manifest.get("max_archive_fetches_per_cycle", 40)))
    maximum = int(throughput.get("max_archive_fetches_per_cycle", base))
    scale_queue = int(throughput.get("scale_queue_threshold", max(base * 2, 80)))
    minimum_yield = float(throughput.get("scale_minimum_acceptance_rate", 0.25))
    now_iso = _iso(current)
    with connect(db_path) as conn:
        ready = int(conn.execute(
            """SELECT COUNT(*) FROM content_archive_candidates
               WHERE status IN ('pending','cooldown') AND (next_attempt_at IS NULL OR next_attempt_at<=?)""",
            (now_iso,),
        ).fetchone()[0])
        outcomes = {
            row["status"]: int(row["count"])
            for row in conn.execute(
                """SELECT status,COUNT(*) AS count FROM content_archive_candidates
                   WHERE status IN ('accepted','rejected','duplicate') GROUP BY status"""
            ).fetchall()
        }
    accepted = outcomes.get("accepted", 0)
    resolved = accepted + outcomes.get("rejected", 0) + outcomes.get("duplicate", 0)
    acceptance_rate = (accepted / resolved) if resolved else None
    if explicit_max_fetch is not None:
        desired = int(explicit_max_fetch)
        reason = "explicit_cli_bound"
    elif ready >= scale_queue and acceptance_rate is not None and acceptance_rate >= minimum_yield:
        desired = maximum
        reason = "scaled_for_productive_backlog"
    elif ready >= base and acceptance_rate is not None and acceptance_rate >= minimum_yield:
        desired = min(maximum, max(base, (base + maximum) // 2))
        reason = "moderate_productive_backlog"
    else:
        desired = base
        reason = "conservative_base"
    bounded = max(0, min(desired, maximum, ARCHIVE_HARD_MAX_FETCHES, requests_remaining))
    return {
        "budget": bounded,
        "desired": desired,
        "base": base,
        "maximum": maximum,
        "ready_candidates": ready,
        "historical_acceptance_rate": acceptance_rate,
        "reason": reason,
        "requests_remaining_before_consumer": requests_remaining,
    }


def consume_archive_candidates(
    db_path: str | Path,
    manifest: dict[str, Any],
    *,
    artifact_root: Path,
    coordinator: FetchCoordinator,
    current: datetime,
    max_fetch: int,
    fetcher: Callable[..., FetchResult] = fetch_archive,
) -> dict[str, Any]:
    started = time.perf_counter()
    bounded = max(0, min(int(max_fetch), ARCHIVE_HARD_MAX_FETCHES))
    cooldown = _archive_cooldown(db_path, current)
    if cooldown:
        return {
            "selected": 0, "attempted": 0, "accepted": [], "accepted_count": 0,
            "duplicates": 0, "rejected": {}, "errors": 0, "blocked": 0,
            "status": "host_cooldown", "cooldown": cooldown, "elapsed_seconds": 0.0,
            "qualifying_bodies_per_elapsed_hour": None,
        }
    now_iso = _iso(current)
    with connect(db_path) as conn:
        conn.execute(
            """UPDATE content_archive_candidates SET status='duplicate',last_outcome='existing_canonical',updated_at=?
               WHERE status IN ('pending','cooldown') AND EXISTS(
                   SELECT 1 FROM contents WHERE contents.canonical_url=content_archive_candidates.canonical_url
               )""",
            (now_iso,),
        )
        rows = conn.execute(
            """SELECT * FROM content_archive_candidates
               WHERE status IN ('pending','cooldown') AND (next_attempt_at IS NULL OR next_attempt_at<=?)
               ORDER BY CASE WHEN original_url LIKE '%/vi/square/%' AND snapshot_timestamp>='20250101000000' THEN 0 ELSE 1 END,
                        snapshot_timestamp DESC,post_id LIMIT ?""",
            (now_iso, bounded),
        ).fetchall()
        conn.commit()
    source = _archive_source(manifest, _cdx_url(manifest, current), "wayback-square-cdx")
    source["name"] = "Public archive snapshots — Binance Square posts"
    accepted: list[dict[str, Any]] = []
    baseline_ids = {str(value) for value in manifest.get("baseline", {}).get("qualifying_post_ids", [])}
    rejected: dict[str, int] = {}
    diagnostics: list[dict[str, Any]] = []
    duplicates = errors = blocked = attempted = consecutive_errors = 0
    max_inflight = min(2, coordinator.concurrency)
    stop_dispatch = False
    for offset in range(0, len(rows), max_inflight):
        if stop_dispatch:
            break
        batch = [dict(row) for row in rows[offset:offset + max_inflight]]
        attempted += len(batch)
        with ThreadPoolExecutor(max_workers=max_inflight, thread_name_prefix="bti-archive-fetch") as pool:
            futures = [
                pool.submit(_dispatch_archive_url, fetcher, candidate["archive_url"], coordinator=coordinator)
                for candidate in batch
            ]
            network_outcomes: list[tuple[FetchResult | None, Exception | None]] = []
            for future in futures:
                try:
                    network_outcomes.append((future.result(), None))
                except Exception as exc:
                    network_outcomes.append((None, exc))

        # SQLite/provenance work stays on this caller thread and follows the stable
        # candidate ordering, regardless of network completion order.
        for candidate, (result, error) in zip(batch, network_outcomes):
            if isinstance(error, AccessStopped):
                if error.status_code in {404, 410}:
                    reason = f"archive_snapshot_http_{error.status_code}"
                    rejected[reason] = rejected.get(reason, 0) + 1
                    with connect(db_path) as conn:
                        conn.execute(
                            """UPDATE content_archive_candidates SET status='rejected',attempt_count=attempt_count+1,
                                   next_attempt_at=NULL,last_outcome=?,updated_at=? WHERE id=?""",
                            (reason, now_iso, candidate["id"]),
                        )
                        conn.commit()
                    continue
                if (
                    error.status_code == 302
                    and not (error.retry_after_seconds is not None and error.retry_after_seconds > 0)
                    and _same_static_post_replay_redirect(
                        candidate["archive_url"], getattr(error, "redirect_location", None),
                    )
                ):
                    reason = "archive_snapshot_redirect_not_followed"
                    rejected[reason] = rejected.get(reason, 0) + 1
                    with connect(db_path) as conn:
                        conn.execute(
                            """UPDATE content_archive_candidates SET status='rejected',attempt_count=attempt_count+1,
                                   next_attempt_at=NULL,last_outcome=?,updated_at=? WHERE id=?""",
                            (reason, now_iso, candidate["id"]),
                        )
                        conn.commit()
                    continue
                if (
                    error.status_code in {500, 502, 503, 504}
                    and not (error.retry_after_seconds is not None and error.retry_after_seconds > 0)
                ):
                    reason = f"archive_snapshot_http_{error.status_code}"
                    prior_attempts = int(candidate.get("attempt_count") or 0)
                    if prior_attempts >= 1:
                        rejected[reason] = rejected.get(reason, 0) + 1
                        with connect(db_path) as conn:
                            conn.execute(
                                """UPDATE content_archive_candidates SET status='rejected',attempt_count=attempt_count+1,
                                       next_attempt_at=NULL,last_outcome=?,updated_at=? WHERE id=?""",
                                (reason, now_iso, candidate["id"]),
                            )
                            conn.commit()
                    else:
                        errors += 1
                        consecutive_errors += 1
                        with connect(db_path) as conn:
                            conn.execute(
                                """UPDATE content_archive_candidates SET status='cooldown',attempt_count=attempt_count+1,
                                       next_attempt_at=?,last_outcome=?,updated_at=? WHERE id=?""",
                                (_iso(current + timedelta(hours=6)), reason, now_iso, candidate["id"]),
                            )
                            conn.commit()
                        if consecutive_errors >= 3:
                            stop_dispatch = True
                    continue
                blocked += 1
                stop_dispatch = True
                retry_until = _set_archive_cooldown(
                    db_path, current, error.status_code, error.retry_after_seconds,
                )
                with connect(db_path) as conn:
                    conn.execute(
                        """UPDATE content_archive_candidates SET status='cooldown',attempt_count=attempt_count+1,
                               next_attempt_at=?,last_outcome='access_stopped',updated_at=? WHERE id=?""",
                        (retry_until, now_iso, candidate["id"]),
                    )
                    conn.commit()
                continue
            if error is not None:
                errors += 1
                consecutive_errors += 1
                with connect(db_path) as conn:
                    conn.execute(
                        """UPDATE content_archive_candidates SET status='cooldown',attempt_count=attempt_count+1,
                               next_attempt_at=?,last_outcome=?,updated_at=? WHERE id=?""",
                        (_iso(current + timedelta(hours=1)), type(error).__name__, now_iso, candidate["id"]),
                    )
                    conn.commit()
                if consecutive_errors >= 3:
                    stop_dispatch = True
                continue

            assert result is not None
            try:
                raw_path = _save_raw(
                    artifact_root, f"post-{candidate['post_id']}-{candidate['snapshot_timestamp']}", result,
                )
                item, reason = parse_archive_post(
                    result, candidate, current=current,
                    minimum_text_chars=int(manifest.get("minimum_text_chars", 200)),
                )
                if item is None:
                    rejected[reason or "invalid"] = rejected.get(reason or "invalid", 0) + 1
                    with connect(db_path) as conn:
                        conn.execute(
                            """UPDATE content_archive_candidates SET status='rejected',attempt_count=attempt_count+1,
                                   last_outcome=?,response_sha256=?,updated_at=? WHERE id=?""",
                            (reason, result.response_hash, now_iso, candidate["id"]),
                        )
                        conn.commit()
                    _record_fetch_import(fetcher, result, 0)
                    consecutive_errors = 0
                    continue
                outcome = _store_result(
                    db_path, source, result, [item], note="Static public archive post extraction",
                    baseline_post_ids=baseline_ids,
                )
                if outcome["accepted"]:
                    detail = outcome["accepted"][0]
                    detail["raw_artifact"] = raw_path
                    accepted.append(detail)
                    status = "accepted"
                    content_id = detail["content_id"]
                else:
                    duplicates += 1
                    status = "duplicate"
                    content_id = None
                with connect(db_path) as conn:
                    conn.execute(
                        """UPDATE content_archive_candidates SET status=?,attempt_count=attempt_count+1,
                               last_outcome=?,response_sha256=?,content_id=?,body_chars=?,published_at=?,updated_at=?
                           WHERE id=?""",
                        (status, "accepted" if status == "accepted" else "duplicate", result.response_hash,
                         content_id, len(compact_text(item.text)), item.published_at, now_iso, candidate["id"]),
                    )
                    conn.commit()
                _record_fetch_import(fetcher, result, 1 if status == "accepted" else 0)
                consecutive_errors = 0
            except Exception as exc:
                errors += 1
                consecutive_errors += 1
                diagnostics.append(_bounded_private_diagnostic(exc, "archive_consumer_item"))
                try:
                    with connect(db_path) as conn:
                        conn.execute(
                            """UPDATE content_archive_candidates SET status='cooldown',attempt_count=attempt_count+1,
                                   next_attempt_at=?,last_outcome=?,updated_at=? WHERE id=?""",
                            (_iso(current + timedelta(hours=1)), type(exc).__name__, now_iso, candidate["id"]),
                        )
                        conn.commit()
                except Exception:
                    pass
                if consecutive_errors >= 3:
                    stop_dispatch = True
    elapsed = max(0.0, time.perf_counter() - started)
    rate = (len(accepted) * 3600 / elapsed) if accepted and elapsed > 0 else 0.0 if attempted else None
    consumer_status = (
        "partial_error" if (accepted and errors)
        else "productive" if accepted
        else "blocked" if blocked
        else "error" if errors
        else "no_new_content"
    )
    return {
        "selected": len(rows), "attempted": attempted, "accepted": accepted, "accepted_count": len(accepted),
        "duplicates": duplicates, "rejected": rejected, "errors": errors, "blocked": blocked,
        "diagnostics": diagnostics,
        "status": consumer_status,
        "elapsed_seconds": round(elapsed, 3),
        "qualifying_bodies_per_elapsed_hour": round(rate, 3) if rate is not None else None,
        "network_max_inflight": max_inflight,
        "sqlite_writer": "caller_thread_serial",
    }


def content_volume_status(db_path: str | Path, *, now: datetime | None = None) -> dict[str, Any]:
    current = _utc(now)
    window_start = minus_calendar_years(current, 2)
    recent_7d_start = current - timedelta(days=7)
    recent_30d_start = current - timedelta(days=30)
    current_iso = _iso(current)
    window_start_iso = _iso(window_start)
    with connect(db_path) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "content_volume_state" not in tables:
            return {
                "status": "not_configured",
                "lifetime_status": "not_configured",
                "current_extraction_status": "not_configured",
                "new_qualifying_bodies_since_baseline": 0,
                "last_successful_content_extraction_at": None,
            }
        state = conn.execute("SELECT * FROM content_volume_state WHERE id=1").fetchone()
        if state is None:
            return {
                "status": "not_started",
                "lifetime_status": "not_started",
                "current_extraction_status": "not_started",
                "new_qualifying_bodies_since_baseline": 0,
                "last_successful_content_extraction_at": None,
            }
        accepted = int(conn.execute(
            """SELECT COUNT(*) FROM contents content
               JOIN content_versions version ON version.id=content.latest_version_id
               JOIN entities owner ON owner.id=content.entity_id
               JOIN sources source ON source.id=content.source_id
               WHERE content.public_shareable=1 AND owner.public_shareable=1
                  AND source.access_level='public' AND source.distribution='public_shareable'
                  AND content.published_at IS NOT NULL AND LENGTH(version.normalized_text)>=200
                  AND content.published_at>=? AND content.published_at<=?
                  AND json_extract(version.metadata_json,'$.content_volume_goal')=1
                  AND json_extract(version.metadata_json,'$.qualifying_body')=1""",
            (window_start_iso, current_iso),
        ).fetchone()[0])
        qualifying_where = """content.public_shareable=1 AND owner.public_shareable=1
                  AND source.access_level='public' AND source.distribution='public_shareable'
                  AND source.source_type IN ('binance_square_public_feed','binance_square_creator_posts','binance_square_public_archive')
                  AND content.canonical_url LIKE 'https://www.binance.com/%/square/post/%'
                  AND content.published_at IS NOT NULL AND content.published_at>=? AND content.published_at<=?
                  AND LENGTH(version.normalized_text)>=200"""
        base_query = f"""FROM contents content
                JOIN content_versions version ON version.id=content.latest_version_id
                JOIN entities owner ON owner.id=content.entity_id
                JOIN sources source ON source.id=content.source_id
                WHERE {qualifying_where}"""
        total_unique = int(conn.execute(
            f"SELECT COUNT(DISTINCT content.canonical_url) {base_query}",
            (window_start_iso, current_iso),
        ).fetchone()[0])
        quality_audited = int(conn.execute(
            f"""SELECT COUNT(DISTINCT content.canonical_url) {base_query}
                AND json_extract(version.metadata_json,'$.content_volume_goal')=1
                AND json_extract(version.metadata_json,'$.qualifying_body')=1
                AND COALESCE(json_extract(version.metadata_json,'$.quality_gate_version'),0)>=?""",
            (window_start_iso, current_iso, QUALITY_GATE_VERSION),
        ).fetchone()[0])
        recent_7d = int(conn.execute(
            f"SELECT COUNT(DISTINCT content.canonical_url) {base_query} AND content.published_at>=?",
            (window_start_iso, current_iso, _iso(recent_7d_start)),
        ).fetchone()[0])
        recent_30d = int(conn.execute(
            f"SELECT COUNT(DISTINCT content.canonical_url) {base_query} AND content.published_at>=?",
            (window_start_iso, current_iso, _iso(recent_30d_start)),
        ).fetchone()[0])
        author_diversity = int(conn.execute(
            f"SELECT COUNT(DISTINCT owner.id) {base_query}",
            (window_start_iso, current_iso),
        ).fetchone()[0])
        last_hour = int(conn.execute(
            """SELECT COUNT(DISTINCT content.canonical_url) FROM contents content
               JOIN content_versions version ON version.id=content.latest_version_id
               JOIN entities owner ON owner.id=content.entity_id
               JOIN sources source ON source.id=content.source_id
               WHERE content.public_shareable=1 AND owner.public_shareable=1
                 AND source.access_level='public' AND source.distribution='public_shareable'
                 AND json_extract(version.metadata_json,'$.content_volume_goal')=1
                 AND json_extract(version.metadata_json,'$.qualifying_body')=1
                 AND version.fetched_at>=? AND version.fetched_at<=?""",
            (_iso(current - timedelta(hours=1)), current_iso),
        ).fetchone()[0])
        queue = {
            row["status"]: int(row["count"])
            for row in conn.execute(
                "SELECT status,COUNT(*) AS count FROM content_archive_candidates GROUP BY status"
            ).fetchall()
        }
        state_dict = dict(state)
        cooldown_until_raw = state_dict.get("archive_cooldown_until")
        blocked_status_raw = state_dict.get("archive_last_blocked_status")
        cooldown_active = False
        if cooldown_until_raw:
            try:
                cooldown_active = parse_utc_timestamp(cooldown_until_raw) > current
            except ValueError:
                cooldown_active = False
        lifetime_status = "productive" if accepted else "no_new_content"
        if cooldown_active:
            current_extraction_status = "cooldown"
        elif last_hour > 0:
            current_extraction_status = "productive"
        elif accepted > 0:
            current_extraction_status = "stalled"
        else:
            current_extraction_status = "no_new_content"
        older_count = max(0, total_unique - recent_30d)
        return {
            "status": lifetime_status,
            "lifetime_status": lifetime_status,
            "current_extraction_status": current_extraction_status,
            "baseline_started_at": state_dict.get("baseline_started_at"),
            "baseline_article_count": int(state_dict.get("baseline_article_count", 0)),
            "new_qualifying_bodies_since_baseline": accepted,
            "total_unique_qualifying_bodies": total_unique,
            "quality_audited_bodies": quality_audited,
            "quality_gate_version": QUALITY_GATE_VERSION,
            "quality_audit_status": "partial",
            "target_total_unique_quality_bodies": 1000,
            "milestone_1000_quality_reached": quality_audited >= 1000,
            "milestone_10_qualifying_reached": accepted >= 10,
            "milestone_100_qualifying_reached": accepted >= 100,
            "publication_coverage": {
                "trailing_7d": recent_7d,
                "trailing_30d": recent_30d,
                "older_within_two_year_window": older_count,
                "window_start": window_start_iso,
                "window_end": current_iso,
                "basis": "original_publication_utc",
            },
            "goal_status": {
                "target_quality_bodies": 1000,
                "quality_audited_bodies": quality_audited,
                "total_unique_qualifying_bodies": total_unique,
                "remaining_quality_bodies": max(0, 1000 - quality_audited),
                "reached": quality_audited >= 1000,
                "recent_7d_bodies": recent_7d,
                "recent_30d_bodies": recent_30d,
                "historical_archive_bodies": older_count,
                "summary_vi": (
                    f"Mục tiêu 1.000 bài chất lượng: CHƯA ĐẠT ({quality_audited}/1.000 bài đã qua kiểm định v2; "
                    f"{total_unique} bài đạt chuẩn cấu trúc). 7 ngày qua: {recent_7d} bài, 30 ngày qua: {recent_30d} bài, "
                    f"kho lưu trữ cũ hơn: {older_count} bài (không tính lưu trữ cũ là bài mới trong tháng)."
                    if quality_audited < 1000
                    else f"Đã đạt mục tiêu 1.000+ bài kiểm định chất lượng ({quality_audited}/1.000)."
                ),
            },
            "public_author_count": author_diversity,
            "new_qualifying_bodies_last_hour": last_hour,
            "last_successful_content_extraction_at": state_dict.get("last_content_at"),
            "last_archive_discovery_at": state_dict.get("last_discovery_at"),
            "archive_host_cooldown": {
                "until": cooldown_until_raw,
                "status_code": blocked_status_raw,
                "active": cooldown_active,
            } if cooldown_until_raw else None,
            "archive_candidate_status": queue,
            "discovery_partitions": _partition_summary(db_path),
            "definition": "Qualifying means a public-chain persisted body with canonical post ID, >=200 characters and original date in the rolling two-calendar-year window; queued URLs are excluded. Quality-audited is the stricter current deterministic gate and remains a partial audit, not a claim that every qualifying body is useful.",
        }


def run_content_volume_batch(
    db_path: str | Path,
    manifest_path: str | Path,
    *,
    artifact_dir: str | Path,
    max_fetch: int | None = None,
    now: datetime | None = None,
    archive_fetcher: Callable[..., FetchResult] = fetch_archive,
    msi_config_path: str | Path | None = None,
) -> dict[str, Any]:
    current = _utc(now)
    manifest = load_content_manifest(manifest_path)
    init_db(db_path)
    _content_state(db_path, manifest, current)
    artifacts = _safe_artifact_dir(artifact_dir)
    before_status = content_volume_status(db_path, now=current)
    before_qualifying = int(before_status.get("new_qualifying_bodies_since_baseline", 0))
    coordinator = FetchCoordinator(concurrency=2, minimum_spacing_seconds=ARCHIVE_MINIMUM_SPACING_SECONDS)
    private_diagnostics: list[dict[str, Any]] = []
    effective_fetcher = archive_fetcher
    if archive_fetcher is fetch_archive:
        try:
            effective_fetcher = _build_content_fetcher(
                db_path, fetch_archive, config_path=msi_config_path,
            )
        except Exception as exc:
            private_diagnostics.append(_bounded_private_diagnostic(exc, "build_content_fetcher"))
            effective_fetcher = fetch_archive
    try:
        award_rosters = import_cached_award_rosters(db_path, manifest)
    except Exception as exc:
        private_diagnostics.append(_bounded_private_diagnostic(exc, "award_rosters"))
        award_rosters = {"new_contents": 0, "new_versions": 0, "duplicates": 0, "errors": 1, "skipped": 0}
    try:
        cached = import_cached_details(db_path, manifest, artifact_root=artifacts)
    except Exception as exc:
        private_diagnostics.append(_bounded_private_diagnostic(exc, "cached_details"))
        cached = {"accepted": [], "accepted_count": 0, "duplicates": 0, "errors": 1, "skipped": 0, "quality_rejected": 0}
    starting_cooldown = _archive_cooldown(db_path, current)
    if starting_cooldown:
        profiles = {
            "accepted": [], "accepted_count": 0, "duplicates": 0, "errors": 0,
            "blocked": 0, "skipped": len(manifest["profile_snapshots"]), "attempted": 0,
            "fetched": 0, "rejected": {}, "status": "host_cooldown", "cooldown": starting_cooldown,
        }
    else:
        try:
            profiles = import_profile_snapshots(
                db_path, manifest, artifact_root=artifacts, coordinator=coordinator,
                current=current, fetcher=effective_fetcher,
            )
        except Exception as exc:
            private_diagnostics.append(_bounded_private_diagnostic(exc, "profile_snapshots"))
            profiles = {
                "accepted": [], "accepted_count": 0, "duplicates": 0, "errors": 1,
                "blocked": 0, "skipped": 0, "attempted": 0, "fetched": 0, "rejected": {},
                "status": "error",
            }
    try:
        discovery = discover_cdx(
            db_path, manifest, artifact_root=artifacts, coordinator=coordinator,
            current=current, fetcher=effective_fetcher,
        )
    except AccessStopped as exc:
        discovery = {"status": "blocked", "status_code": exc.status_code, "rows": 0, "new_candidates": 0}
    except Exception as exc:
        private_diagnostics.append(_bounded_private_diagnostic(exc, "archive_discovery"))
        discovery = {"status": "error", "error_class": type(exc).__name__, "rows": 0, "new_candidates": 0}
    throughput_config = manifest.get("throughput") or {}
    total_request_budget = min(
        int(throughput_config.get("max_total_archive_requests_per_cycle", ARCHIVE_HARD_MAX_REQUESTS_PER_CYCLE)),
        ARCHIVE_HARD_MAX_REQUESTS_PER_CYCLE,
    )
    requests_used_before_consumer = int(profiles.get("attempted", 0)) + int(
        discovery.get("attempted_requests", discovery.get("requests", 0))
    )
    adaptive_budget = _adaptive_archive_budget(
        db_path,
        manifest,
        explicit_max_fetch=max_fetch,
        requests_remaining=max(0, total_request_budget - requests_used_before_consumer),
        current=current,
    )
    try:
        consumer = consume_archive_candidates(
            db_path, manifest, artifact_root=artifacts, coordinator=coordinator, current=current,
            max_fetch=adaptive_budget["budget"], fetcher=effective_fetcher,
        )
    except Exception as exc:
        private_diagnostics.append(_bounded_private_diagnostic(exc, "archive_consumer"))
        consumer = {
            "selected": 0, "attempted": 0, "accepted": [], "accepted_count": 0,
            "duplicates": 0, "rejected": {}, "errors": 1, "blocked": 0,
            "diagnostics": [_bounded_private_diagnostic(exc, "archive_consumer")],
            "status": "error", "elapsed_seconds": 0.0,
            "qualifying_bodies_per_elapsed_hour": None,
        }
    if consumer.get("diagnostics"):
        private_diagnostics.extend(consumer["diagnostics"])
    newly_accepted = cached["accepted"] + profiles["accepted"] + consumer["accepted"]
    status = content_volume_status(db_path, now=current)
    after_qualifying = int(status.get("new_qualifying_bodies_since_baseline", 0))
    persisted_delta = max(0, after_qualifying - before_qualifying)
    new_qualifying_count = max(len(newly_accepted), persisted_delta)
    total_requests_used = requests_used_before_consumer + int(consumer.get("attempted", 0))
    try:
        node_execution = (
            effective_fetcher.snapshot()
            if callable(getattr(effective_fetcher, "snapshot", None))
            else {
                "status": "local_only" if archive_fetcher is fetch_archive else "explicit_fetcher_override",
                "max_inflight_total": 2,
                "no_node_fallback": True,
            }
        )
    except Exception as exc:
        private_diagnostics.append(_bounded_private_diagnostic(exc, "node_snapshot"))
        node_execution = {"status": "snapshot_error", "max_inflight_total": 2, "no_node_fallback": True}
    batch_status = (
        "partial_error" if (private_diagnostics and new_qualifying_count > 0)
        else "error" if private_diagnostics
        else status.get("current_extraction_status", status.get("status", "ok"))
    )
    report = {
        "started_at": _iso(current),
        "completed_at": utc_now(),
        "batch_status": batch_status,
        "new_qualifying_bodies_this_batch": new_qualifying_count,
        "new_articles": newly_accepted,
        "award_rosters": award_rosters,
        "cached_details": {key: value for key, value in cached.items() if key != "accepted"},
        "profile_snapshots": {key: value for key, value in profiles.items() if key != "accepted"},
        "archive_discovery": discovery,
        "archive_consumer": {
            key: value for key, value in consumer.items() if key not in {"accepted", "diagnostics"}
        },
        "private_diagnostics": private_diagnostics,
        "throughput": {
            "adaptive_budget": adaptive_budget,
            "total_archive_request_budget": total_request_budget,
            "total_archive_requests_used": total_requests_used,
            "request_budget_respected": total_requests_used <= total_request_budget,
            "quality_gate_v2_bodies_this_batch": new_qualifying_count,
            "consumer_qualifying_bodies_per_elapsed_hour": consumer.get("qualifying_bodies_per_elapsed_hour"),
            "recent_and_older_counts": status.get("publication_coverage"),
            "node_execution": node_execution,
        },
        "status": status,
        "guardrails": {
            "live_binance_article_requests": 0,
            "redirects_followed": False,
            "scripts_executed": False,
            "authentication_used": False,
            "proxy_rotation_used": False,
            "sqlite_writer": "serial",
            "archive_host_spacing_seconds": ARCHIVE_MINIMUM_SPACING_SECONDS,
            "archive_request_hard_cap": ARCHIVE_HARD_MAX_REQUESTS_PER_CYCLE,
            "persistent_archive_host_cooldown": True,
        },
    }
    report_path = artifacts / "latest-report.json"
    temporary = report_path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(report_path)
    report["report_path"] = str(report_path)
    return report

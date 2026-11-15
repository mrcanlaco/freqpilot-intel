from __future__ import annotations

import hashlib
import json
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

MAX_IMAGES = 9
MAX_CAPTURE_BYTES = 8 * 1024 * 1024


def safe_image_url(value: Any) -> str | None:
    """Accept original raster URLs on the two captured Binance CDN routes only."""
    if not isinstance(value, str) or not value or len(value) > 2048:
        return None
    if any(char.isspace() or ord(char) < 32 or 127 <= ord(char) <= 159 for char in value) or "\\" in value:
        return None
    try:
        parsed = urlsplit(value)
        if not re.fullmatch(r"public\.bnbstatic\.com(?::443)?", parsed.netloc, re.IGNORECASE):
            return None
        if (parsed.scheme != "https" or parsed.hostname != "public.bnbstatic.com"
                or parsed.username is not None or parsed.password is not None
                or parsed.port not in (None, 443) or parsed.fragment or "#" in value):
            return None
    except ValueError:
        return None
    if not parsed.path.startswith(("/image/", "/static/content/square/images/")):
        return None
    if re.search(r"%(?:2f|5c)", parsed.path, re.IGNORECASE):
        return None
    if any(unquote(segment) in {".", ".."} for segment in parsed.path.split("/")):
        return None
    if not re.search(r"\.(?:jpg|jpeg|png|webp|gif)$", parsed.path, re.IGNORECASE):
        return None
    return value


def image_urls(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    result: list[str] = []
    for value in values[:512]:
        url = safe_image_url(value)
        if url and url not in result:
            result.append(url)
            if len(result) == MAX_IMAGES:
                break
    return result


class _BodyImages(HTMLParser):
    def __init__(self, add: Any) -> None:
        super().__init__(convert_charrefs=True)
        self.add = add

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "img":
            self.add(dict(attrs).get("src"))


def extract_post_images(row: dict[str, Any]) -> list[str]:
    """Cover, attached images, then body images; never scan arbitrary post metadata."""
    found: list[str] = []

    def add(value: Any) -> None:
        url = safe_image_url(value)
        if url and url not in found and len(found) < MAX_IMAGES:
            found.append(url)

    def attachment(value: Any) -> None:
        if isinstance(value, str):
            add(value)
        elif isinstance(value, dict):
            add(value.get("url"))
            add(value.get("src"))

    attachment(row.get("cover"))
    attachment(row.get("coverMeta"))
    for key in ("imageList", "imageMetaList"):
        values = row.get(key)
        if isinstance(values, list):
            for value in values[:512]:
                attachment(value)
    body = row.get("body")
    if isinstance(body, str):
        if len(body) > MAX_CAPTURE_BYTES:
            return found
        try:
            body = json.loads(body)
        except (ValueError, RecursionError):
            _BodyImages(add).feed(body)
            return found
    remaining = 10000

    def walk(node: Any, depth: int = 0) -> None:
        nonlocal remaining
        remaining -= 1
        if remaining < 0 or depth > 40 or len(found) >= MAX_IMAGES:
            return
        if isinstance(node, list):
            for child in node[:10000]:
                walk(child, depth + 1)
        elif isinstance(node, dict):
            kind = str(node.get("id") or node.get("type") or node.get("node") or "").lower()
            if kind in {"richtextimage", "image", "img"}:
                attachment(node)
                attachment(node.get("config"))
                attachment(node.get("attrs"))
                return
            # Do not descend into embedded/quoted posts, cards, user objects or banners.
            if kind and kind not in {"root", "doc", "paragraph", "richtextparagraph", "richtexttext", "text"}:
                return
            if isinstance(node.get("hash"), dict) and isinstance(node.get("layout"), dict):
                nodes, layout = node["hash"], node["layout"]
                visited: set[str] = set()

                def layout_walk(key: str, level: int) -> None:
                    if key in visited or level > 40 or len(visited) >= 10000:
                        return
                    visited.add(key)
                    if key in nodes:
                        walk(nodes[key], depth + level + 1)
                    children = layout.get(key)
                    if isinstance(children, list):
                        for child in children[:10000]:
                            if isinstance(child, str):
                                layout_walk(child, level + 1)

                layout_walk("root", 0)
                return
            for key in ("children", "content", "config"):
                child = node.get(key)
                if isinstance(child, (dict, list)):
                    walk(child, depth + 1)

    walk(body)
    return found


def content_image_checksum(normalized_text: str, urls: list[str]) -> str:
    payload = normalized_text + "\0image_urls:" + json.dumps(urls, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


_PERMITTED = """FROM contents c
    JOIN entities e ON e.id=c.entity_id
    JOIN sources s ON s.id=c.source_id
    JOIN content_versions v ON v.id=c.latest_version_id AND v.content_id=c.id
    WHERE c.public_shareable=1 AND e.public_shareable=1
      AND s.access_level='public' AND s.distribution='public_shareable'"""


INDEX_SCHEMA = "bti-source-images-v1"
MAX_INDEX_BYTES = 8 * 1024 * 1024


def _capture_sha(metadata: dict[str, Any]) -> str | None:
    value = metadata.get("archive_response_sha256") or metadata.get("retained_capture_sha256")
    return value if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) else None


def load_image_index(db_path: str | Path) -> dict[str, Any]:
    """Load a bounded derived index once per public projection; malformed files fail closed."""
    path = Path(db_path).with_suffix(".images.json")
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_INDEX_BYTES + 1)
        if len(raw) > MAX_INDEX_BYTES:
            return {}
        document = json.loads(raw)
        if not isinstance(document, dict) or document.get("schema") != INDEX_SCHEMA:
            return {}
        entries = document.get("entries")
        if not isinstance(entries, dict):
            return {}
        for key, entry in entries.items():
            if (not isinstance(entry, dict) or not key.isascii() or not key.isdigit()
                    or entry.get("version_id") != int(key) or int(key) < 1
                    or not all(isinstance(entry.get(field), str) and entry[field]
                               for field in ("post_id", "source_uid", "checksum", "capture_sha256"))
                    or not re.fullmatch(r"[0-9a-f]{64}", entry["checksum"])
                    or not re.fullmatch(r"[0-9a-f]{64}", entry["capture_sha256"])
                    or not isinstance(entry.get("urls"), list)
                    or image_urls(entry["urls"]) != entry["urls"] or not entry["urls"]):
                return {}
        return entries
    except (OSError, ValueError, TypeError, RecursionError):
        return {}


def projected_image_urls(metadata: dict[str, Any], *, index: dict[str, Any], version_id: int,
                         post_id: str, checksum: str, source_uid: str | None) -> list[str]:
    if not isinstance(metadata, dict):
        return []
    if "image_urls" in metadata:
        return image_urls(metadata["image_urls"])
    entry = index.get(str(version_id))
    uid = metadata.get("source_uid")
    if (not isinstance(entry, dict) or not uid or uid != source_uid
            or entry.get("version_id") != version_id or entry.get("post_id") != str(post_id)
            or entry.get("source_uid") != uid or entry.get("checksum") != checksum
            or entry.get("capture_sha256") != _capture_sha(metadata)):
        return []
    return image_urls(entry.get("urls"))


def _permitted_versions(db_path: str | Path) -> list[Any]:
    import sqlite3
    from contextlib import closing

    with closing(sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT c.id AS content_id,c.external_id,e.source_uid,v.id AS version_id,v.checksum,v.metadata_json "
            + _PERMITTED
        ).fetchall()


def enrich_images(db_path: str | Path, captures: str | Path, *, write_index: bool = False) -> dict[str, Any]:
    """Derive a hash/identity-bound URL sidecar without ever writing the canonical DB."""
    from .content_volume import _app_data, _post_records

    root = Path(captures)
    if not root.is_dir():
        raise ValueError("Captures must be an existing directory")
    if not Path(db_path).is_file():
        raise ValueError("Database must already exist")
    counters = dict(scanned=0, already_enriched=0, missing_hash=0, missing_capture=0,
                    invalid_capture=0, identity_mismatch=0, no_images=0, eligible=0,
                    indexed=0, changed_or_revoked=0, capture_files=0, unreadable_captures=0)
    versions = _permitted_versions(db_path)
    originals = {str(row["version_id"]): row for row in versions}
    pending: dict[str, list[tuple[Any, dict[str, Any]]]] = {}
    entries: dict[str, Any] = {}
    for version in versions:
        counters["scanned"] += 1
        try:
            metadata = json.loads(version["metadata_json"])
        except (ValueError, TypeError):
            counters["missing_hash"] += 1
            continue
        if not isinstance(metadata, dict):
            counters["missing_hash"] += 1
            continue
        if "image_urls" in metadata:
            counters["already_enriched"] += 1
            continue
        digest = _capture_sha(metadata)
        if not digest:
            counters["missing_hash"] += 1
            continue
        pending.setdefault(digest, []).append((version, metadata))
    matched: set[str] = set()
    for path in sorted(root.rglob("*.body")):
        counters["capture_files"] += 1
        try:
            if path.is_symlink() or path.stat().st_size > MAX_CAPTURE_BYTES:
                counters["unreadable_captures"] += 1
                continue
            with path.open("rb") as handle:
                body = handle.read(MAX_CAPTURE_BYTES + 1)
            if len(body) > MAX_CAPTURE_BYTES:
                counters["unreadable_captures"] += 1
                continue
        except OSError:
            counters["unreadable_captures"] += 1
            continue
        digest = hashlib.sha256(body).hexdigest()
        if digest not in pending or digest in matched:
            continue
        matched.add(digest)
        try:
            data = json.loads(body) if body.lstrip().startswith(b"{") else _app_data(body)
            records = _post_records(data)
        except (ValueError, TypeError, RecursionError):
            counters["invalid_capture"] += len(pending[digest])
            continue
        for version, metadata in pending[digest]:
            record = records.get(str(version["external_id"]))
            uid = metadata.get("source_uid")
            if (record is None or not uid or not version["source_uid"]
                    or str(record.get("squareUid") or "") != str(uid)
                    or str(version["source_uid"]) != str(uid)):
                counters["identity_mismatch"] += 1
                continue
            urls = extract_post_images(record)
            if not urls:
                counters["no_images"] += 1
                continue
            counters["eligible"] += 1
            entries[str(version["version_id"])] = {
                "version_id": version["version_id"], "post_id": str(version["external_id"]),
                "source_uid": uid, "checksum": version["checksum"], "capture_sha256": digest, "urls": urls,
            }
    counters["missing_capture"] = sum(len(rows) for digest, rows in pending.items() if digest not in matched)
    # Prune races before publishing. Public reads independently recheck binding and permissions.
    current = {str(row["version_id"]): row for row in _permitted_versions(db_path)}
    for key in list(entries):
        row = current.get(key)
        if row is None or any(row[field] != originals[key][field] for field in originals[key].keys()):
            del entries[key]
            counters["changed_or_revoked"] += 1
    counters["indexed"] = len(entries)
    written = False
    if write_index:
        import os
        import tempfile

        target = Path(db_path).with_suffix(".images.json")
        raw = (json.dumps({"schema": INDEX_SCHEMA, "entries": entries},
                          ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        if len(raw) > MAX_INDEX_BYTES:
            raise ValueError("Derived image index exceeds the bounded file size")
        try:
            with target.open("rb") as handle:
                unchanged = handle.read(MAX_INDEX_BYTES + 1) == raw
        except FileNotFoundError:
            unchanged = False
        if not unchanged:
            temporary: str | None = None
            try:
                with tempfile.NamedTemporaryFile(dir=target.parent, prefix=target.name + ".", suffix=".tmp",
                                                 delete=False) as handle:
                    temporary = handle.name
                    handle.write(raw)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, target)
                written = True
            finally:
                if temporary and os.path.exists(temporary):
                    os.unlink(temporary)
    return {"mode": "write_index" if write_index else "dry_run", "index_written": written, **counters}

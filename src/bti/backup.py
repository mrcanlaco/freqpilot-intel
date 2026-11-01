from __future__ import annotations

import os
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BACKUP_NAME = re.compile(r"^bti-backup-\d{8}T\d{6}Z\.sqlite3$")


def _as_utc(now: datetime | None) -> datetime:
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Backup timestamp must be timezone-aware")
    return value.astimezone(timezone.utc)


def _integrity(path: Path) -> str:
    uri = f"file:{path.as_posix()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=10)) as conn:
        return str(conn.execute("PRAGMA integrity_check").fetchone()[0])


def apply_retention(output_dir: str | Path, keep: int = 7) -> list[str]:
    if not 1 <= keep <= 365:
        raise ValueError("Backup retention must be between 1 and 365 copies")
    directory = Path(output_dir).resolve()
    if not directory.is_dir():
        return []
    candidates = sorted(
        (path for path in directory.iterdir() if path.is_file() and BACKUP_NAME.fullmatch(path.name)),
        key=lambda path: path.name,
        reverse=True,
    )
    removed: list[str] = []
    for path in candidates[keep:]:
        resolved = path.resolve()
        if resolved.parent != directory or not BACKUP_NAME.fullmatch(resolved.name):
            raise RuntimeError("Backup retention target escaped the configured directory")
        resolved.unlink()
        removed.append(resolved.name)
    return removed


def create_backup(
    db_path: str | Path,
    output_dir: str | Path,
    *,
    keep: int = 7,
    now: datetime | None = None,
) -> dict[str, Any]:
    source_path = Path(db_path).resolve()
    if not source_path.is_file():
        raise FileNotFoundError(f"SQLite database does not exist: {source_path}")
    destination_dir = Path(output_dir).resolve()
    if destination_dir == source_path.parent and destination_dir / source_path.name == source_path:
        raise ValueError("Backup destination cannot overwrite the live database")
    destination_dir.mkdir(parents=True, exist_ok=True)
    timestamp = _as_utc(now).strftime("%Y%m%dT%H%M%SZ")
    destination = destination_dir / f"bti-backup-{timestamp}.sqlite3"
    temporary = destination_dir / f".{destination.name}.{os.getpid()}.tmp"
    if temporary.exists():
        temporary.unlink()

    source_uri = f"file:{source_path.as_posix()}?mode=ro"
    try:
        with closing(sqlite3.connect(source_uri, uri=True, timeout=10)) as source:
            with closing(sqlite3.connect(temporary, timeout=10)) as target:
                source.backup(target)
                target.commit()
        integrity = _integrity(temporary)
        if integrity != "ok":
            raise RuntimeError(f"SQLite backup integrity check failed: {integrity}")
        os.replace(temporary, destination)
        try:
            destination.chmod(0o600)
        except OSError:
            pass
    finally:
        if temporary.exists():
            temporary.unlink()

    removed = apply_retention(destination_dir, keep=keep)
    return {
        "status": "success",
        "created_at": _as_utc(now).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "path": str(destination),
        "integrity_check": "ok",
        "retention_keep": keep,
        "removed": removed,
        "bytes": destination.stat().st_size,
    }

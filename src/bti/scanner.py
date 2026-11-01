"""Autonomous scanner orchestration, adaptive pacing, health doctor, and safe replication kit."""
from __future__ import annotations

from contextlib import closing
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import platform
import secrets
import sqlite3
from typing import Any

from .content_volume import content_volume_status, load_content_manifest
from .crawler_policy import public_crawler_status
from .db import SCHEMA_VERSION, connect, init_db, utc_now
from .ingest import seed_sources

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MIN_INTERVAL_SECONDS = 600
DEFAULT_IDLE_INTERVAL_SECONDS = 1800
DEFAULT_MAX_INTERVAL_SECONDS = 3600

REPLICATED_SOURCE_FILES = (
    "pyproject.toml",
    "README.md",
    "config/public_sources.json",
    "config/content_sources.json",
    "config/historical_seeds.json",
    "src/bti/__init__.py",
    "src/bti/__main__.py",
    "src/bti/backup.py",
    "src/bti/cli.py",
    "src/bti/content_volume.py",
    "src/bti/crawler_policy.py",
    "src/bti/db.py",
    "src/bti/domain.py",
    "src/bti/historical.py",
    "src/bti/ingest.py",
    "src/bti/public.py",
    "src/bti/scanner.py",
    "src/bti/server.py",
    "src/bti/verification.py",
    "src/bti/worker.py",
    "src/bti/static/index.html",
    "src/bti/static/style.css",
    "src/bti/static/app.js",
    "ops/windows_supervisor.py",
    "ops/msi-fetch-worker/executor.py",
    "ops/msi-fetch-worker/bridge.py",
    "ops/msi-fetch-worker/transport.py",
    "ops/msi-fetch-worker/CONTRACT.md",
)


def _utc(value: datetime | None = None) -> datetime:
    current = value or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("Scanner time must be timezone-aware")
    return current.astimezone(UTC)


def _iso(value: datetime | None = None) -> str:
    return _utc(value).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def compute_adaptive_interval(
    db_path: str | Path,
    *,
    base_interval_seconds: int = DEFAULT_MIN_INTERVAL_SECONDS,
    max_interval_seconds: int = DEFAULT_MAX_INTERVAL_SECONDS,
    now: datetime | None = None,
    volume_status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Calculate the recommended sleep interval based on backlog readiness and active cooldowns."""
    current = _utc(now)
    bounded_min = max(60, int(base_interval_seconds))
    bounded_max = max(bounded_min, min(int(max_interval_seconds), 86400))
    idle_interval = min(bounded_max, max(bounded_min, DEFAULT_IDLE_INTERVAL_SECONDS))

    vol_status = volume_status if volume_status is not None else content_volume_status(db_path, now=current)
    cooldown = vol_status.get("archive_host_cooldown") or {}
    queue = vol_status.get("archive_candidate_status") or {}
    pending_count = int(queue.get("pending", 0))
    with connect(db_path) as conn:
        has_candidates = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='content_archive_candidates'"
        ).fetchone()
        ready_now = 0
        ready_vi_now = 0
        if has_candidates:
            ready_now = int(conn.execute(
                """SELECT COUNT(*) FROM content_archive_candidates
                   WHERE status IN ('pending','cooldown')
                     AND (next_attempt_at IS NULL OR next_attempt_at<=?)""",
                (_iso(current),),
            ).fetchone()[0])
            ready_vi_now = int(conn.execute(
                """SELECT COUNT(*) FROM content_archive_candidates
                   WHERE status IN ('pending','cooldown')
                     AND (next_attempt_at IS NULL OR next_attempt_at<=?)
                     AND original_url LIKE '%/vi/square/%'""",
                (_iso(current),),
            ).fetchone()[0])

    if cooldown.get("active") and cooldown.get("until"):
        until_dt = _parse_utc(cooldown["until"])
        if until_dt is not None and until_dt > current:
            remaining = int((until_dt - current).total_seconds()) + 5
            chosen = max(bounded_min, min(bounded_max, remaining))
            return {
                "next_interval_seconds": chosen,
                "mode": "cooldown_aligned",
                "reason": "archive_host_cooldown_active",
                "ready_candidates": ready_now,
                "ready_vietnamese_candidates": ready_vi_now,
                "pending_candidates": pending_count,
                "cooldown_until": cooldown["until"],
            }

    if ready_now >= 20 or ready_vi_now > 0:
        return {
            "next_interval_seconds": bounded_min,
            "mode": "productive_fast",
            "reason": "ready_candidate_backlog",
            "ready_candidates": ready_now,
            "ready_vietnamese_candidates": ready_vi_now,
            "pending_candidates": pending_count,
            "cooldown_until": None,
        }
    if ready_now > 0:
        moderate = min(bounded_max, max(bounded_min, (bounded_min + idle_interval) // 2))
        return {
            "next_interval_seconds": moderate,
            "mode": "productive_steady",
            "reason": "moderate_candidate_backlog",
            "ready_candidates": ready_now,
            "ready_vietnamese_candidates": ready_vi_now,
            "pending_candidates": pending_count,
            "cooldown_until": None,
        }
    return {
        "next_interval_seconds": idle_interval,
        "mode": "discovery_patrol",
        "reason": "awaiting_new_discovery_partitions",
        "ready_candidates": ready_now,
        "ready_vietnamese_candidates": ready_vi_now,
        "pending_candidates": pending_count,
        "cooldown_until": None,
    }


def self_heal_database(db_path: str | Path, *, now: datetime | None = None) -> dict[str, Any]:
    """Perform read-only SQLite integrity and hold verification between cycles."""
    current = _utc(now)
    now_iso = _iso(current)
    init_db(db_path)
    with connect(db_path) as conn:
        quick_check = conn.execute("PRAGMA quick_check(1)").fetchone()[0]
        foreign_key_errors = len(conn.execute("PRAGMA foreign_key_check").fetchall())
        cooldown_row = conn.execute(
            "SELECT archive_cooldown_until FROM content_volume_state WHERE id=1"
        ).fetchone()
        host_hold_active = False
        if cooldown_row and cooldown_row["archive_cooldown_until"]:
            until_dt = _parse_utc(cooldown_row["archive_cooldown_until"])
            host_hold_active = bool(until_dt and until_dt > current)
    return {
        "checked_at": now_iso,
        "sqlite_quick_check": quick_check,
        "foreign_key_errors": foreign_key_errors,
        "host_hold_active": host_hold_active,
    }

def fleet_public_summary(project_root: str | Path = ROOT) -> dict[str, Any]:
    """Return a public-safe summary of the scanner's multi-node fleet capability."""
    root = Path(project_root).resolve()
    executor_path = root / "ops" / "msi-fetch-worker" / "executor.py"
    executor_sha256 = (
        hashlib.sha256(executor_path.read_bytes()).hexdigest()
        if executor_path.is_file()
        else None
    )
    msi_flag_path = root / "runtime" / "msi-fetch-enabled.json"
    hybrid_enabled = False
    mode = "local_only"
    if msi_flag_path.is_file():
        try:
            doc = json.loads(msi_flag_path.read_text(encoding="utf-8"))
            if isinstance(doc, dict) and doc.get("enabled") is True:
                hybrid_enabled = True
                mode = str(doc.get("mode") or "hybrid")
        except (OSError, json.JSONDecodeError):
            pass
    configured_nodes = 2 if hybrid_enabled else 1
    return {
        "execution_mode": mode,
        "hybrid_enabled": hybrid_enabled,
        "active_node_count": configured_nodes,
        "shared_host_spacing_seconds": 1.5,
        "sqlite_writer": "single_canonical_serial",
        "executor_sha256": executor_sha256,
        "node_resource_ceiling": {
            "memory_limit_mib": 256,
            "cpu_cores": 1,
            "nice": 10,
            "wall_deadline_seconds": 22,
            "idle_processes": 0,
        },
        "guardrails": {
            "shared_cooldown_across_all_nodes": True,
            "no_ip_or_node_fallback_on_403_429": True,
            "signed_hmac_task_leases": True,
            "replay_nonce_protection": True,
        },
    }


def _derived_worker_status(db_path: str | Path, current: datetime) -> dict[str, Any]:
    with connect(db_path) as conn:
        has_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='worker_state'"
        ).fetchone()
        if not has_table:
            return {"status": "not_configured", "heartbeat_at": None, "cycle_count": 0, "interval_seconds": None}
        row = conn.execute("SELECT * FROM worker_state WHERE id=1").fetchone()
        if row is None:
            return {"status": "not_started", "heartbeat_at": None, "cycle_count": 0, "interval_seconds": None}
        status = row["status"]
        heartbeat = _parse_utc(row["heartbeat_at"])
        stale_after = max(int(row["interval_seconds"]) * 2, 300)
        if status in {"running", "idle", "degraded", "stopping"} and (
            heartbeat is None or (current - heartbeat).total_seconds() > stale_after
        ):
            status = "stale"
        return {
            "status": status,
            "heartbeat_at": row["heartbeat_at"],
            "last_cycle_completed_at": row["last_cycle_completed_at"],
            "next_cycle_due_at": row["next_cycle_due_at"],
            "cycle_count": int(row["cycle_count"]),
            "interval_seconds": int(row["interval_seconds"]),
        }


def scanner_public_status(
    db_path: str | Path,
    *,
    project_root: str | Path = ROOT,
    now: datetime | None = None,
    volume_status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return truthful, public-safe status for the scanner and replication architecture."""
    current = _utc(now)
    root = Path(project_root).resolve()
    content_manifest_path = root / "config" / "content_sources.json"
    vi_discovery_enabled = False
    if content_manifest_path.is_file():
        try:
            manifest = load_content_manifest(content_manifest_path)
            vi_discovery_enabled = bool(manifest.get("cdx", {}).get("include_vietnamese_locale"))
        except (OSError, ValueError, KeyError):
            vi_discovery_enabled = False
    worker_flag_path = Path(db_path).resolve().parent / "worker-enabled.json"
    adaptive_enabled_in_config = False
    if worker_flag_path.is_file():
        try:
            wdoc = json.loads(worker_flag_path.read_text(encoding="utf-8"))
            adaptive_enabled_in_config = bool(isinstance(wdoc, dict) and wdoc.get("adaptive_interval") is True)
        except (OSError, json.JSONDecodeError):
            adaptive_enabled_in_config = False
    worker_info = _derived_worker_status(db_path, current)
    volume = volume_status if volume_status is not None else content_volume_status(db_path, now=current)
    adaptive = compute_adaptive_interval(db_path, now=current, volume_status=volume)
    adaptive["enabled_in_supervisor"] = adaptive_enabled_in_config
    crawler = public_crawler_status(db_path)
    fleet = fleet_public_summary(root)
    return {
        "status": worker_info["status"],
        "checked_at": _iso(current),
        "worker_state": worker_info,
        "autonomous_operation": {
            "worker_status": worker_info["status"],
            "startup_mode": "windows_task_scheduler_at_logon",
            "pre_login_reboot_guarantee": False,
            "startup_note_vi": (
                "Chạy tự động dưới sự giám sát của windows_supervisor.py khi đăng nhập người dùng hiện tại (AtLogOn); "
                "chưa có đảm bảo tự khởi chạy pre-login nếu máy khởi động lại mà chưa đăng nhập."
            ),
            "adaptive_pacing": adaptive,
            "min_interval_seconds": DEFAULT_MIN_INTERVAL_SECONDS,
            "idle_interval_seconds": DEFAULT_IDLE_INTERVAL_SECONDS,
            "max_interval_seconds": DEFAULT_MAX_INTERVAL_SECONDS,
            "self_healing_features": [
                "Tính toán nhịp quét khuyến nghị (600s khi có hàng đợi -> 1800s khi tuần tra -> căn theo mốc hết hạn cooldown; bật qua --adaptive-interval)",
                "Tự động thử lại ứng viên hết hạn backoff tạm thời trong consume_archive_candidates khi host không bị giữ lệnh cooldown",
                "Kiểm tra toàn vẹn SQLite read-only (quick_check & foreign_key_check) và sao lưu trực tuyến hàng ngày (giữ 7 bản)",
                "Bảo toàn partial commit và ghi nhận chẩn đoán riêng tư giới hạn khi xảy ra lỗi giữa chừng",
            ],
        },
        "discovery_engines": {
            "locales_supported": ["vi", "en"] if vi_discovery_enabled else ["en"],
            "vietnamese_cdx_partition_enabled": vi_discovery_enabled,
            "vietnamese_priority_queue": True,
            "cdx_partition_strategy": (
                "Ưu tiên ứng viên Tiếng Việt (/vi/square/post/*) trong hàng đợi (tự động định tuyến qua Desktop local_fetcher khi ở chế độ hybrid) + "
                "phân vùng 30 ngày gần nhất + biên độ lịch sử 2 năm có khóa tiếp tục (resumeKey)"
            ),
            "sitemap_discovery_status": crawler["discovery"]["status"],
            "sitemap_candidates_queued": crawler["discovery"]["candidate_count"],
            "archive_candidate_queue": volume.get("archive_candidate_status", {}),
            "discovery_partitions": volume.get("discovery_partitions", {}),
        },
        "fleet": fleet,
        "replication_architecture": {
            "supported_modes": ["worker_node", "cold_standby"],
            "worker_node_bundle_status": "install_kit_requires_coordinator_registration",
            "single_canonical_writer": True,
            "shared_cooldown_preserved_on_standby": True,
            "blueprint_steps_vi": [
                "1. Gói Cài đặt Node Thực thi (worker_node): Xuất gói executor.py gọn nhẹ (0 tiến trình chờ, trần RAM 256MiB, khóa HMAC-SHA256 32 byte riêng); việc đăng ký SSH host mới vào bridge thuộc quyền Coordinator.",
                "2. Bản sao Dự phòng Lạnh (cold_standby): Tạo bản sao online SQLite backup đầy đủ (giữ nguyên toàn bộ kho bài viết, hàng đợi ứng viên, phân vùng CDX và toàn bộ trạng thái cooldown/host-hold) kèm khóa bảo vệ single-writer (mặc định tắt worker).",
                "3. Kỷ luật Chính sách Chung: Mọi node đều tuân thủ bộ giới hạn tốc độ chung (1,5 giây/host) và dừng ngay khi gặp 401/403/429/202/WAF; tuyệt đối không xoay IP hay chuyển node để vượt chặn.",
            ],
        },
    }

def scanner_doctor(
    db_path: str | Path,
    *,
    manifest_path: str | Path | None = None,
    content_manifest_path: str | Path | None = None,
    project_root: str | Path = ROOT,
) -> dict[str, Any]:
    """Run an end-to-end readiness and integrity check on a scanner instance."""
    root = Path(project_root).resolve()
    db_file = Path(db_path).resolve()
    pub_manifest = Path(manifest_path).resolve() if manifest_path else root / "config" / "public_sources.json"
    cnt_manifest = Path(content_manifest_path).resolve() if content_manifest_path else root / "config" / "content_sources.json"
    checks: dict[str, Any] = {}

    init_db(db_file)
    with connect(db_file) as conn:
        schema_row = conn.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
        schema_ver = int(schema_row[0]) if schema_row and str(schema_row[0]).isdigit() else None
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        fk_errors = len(conn.execute("PRAGMA foreign_key_check").fetchall())
    checks["sqlite_schema_version"] = schema_ver == SCHEMA_VERSION
    checks["sqlite_integrity_ok"] = integrity == "ok" and fk_errors == 0
    checks["public_manifest_valid"] = pub_manifest.is_file()
    checks["content_manifest_valid"] = False
    if cnt_manifest.is_file():
        try:
            load_content_manifest(cnt_manifest)
            checks["content_manifest_valid"] = True
        except (OSError, ValueError, KeyError):
            checks["content_manifest_valid"] = False

    executor_file = root / "ops" / "msi-fetch-worker" / "executor.py"
    checks["executor_present"] = executor_file.is_file()
    standby_guard_file = db_file.parent / "standby-guard.json"
    standby_guarded = False
    if standby_guard_file.is_file():
        try:
            gdoc = json.loads(standby_guard_file.read_text(encoding="utf-8"))
            standby_guarded = gdoc.get("active_dispatch_allowed") is False
        except (OSError, json.JSONDecodeError):
            standby_guarded = True
    worker_enabled_file = db_file.parent / "worker-enabled.json"
    worker_enabled_flag = False
    if worker_enabled_file.is_file():
        try:
            wedoc = json.loads(worker_enabled_file.read_text(encoding="utf-8"))
            worker_enabled_flag = bool(isinstance(wedoc, dict) and wedoc.get("enabled") is True)
        except (OSError, json.JSONDecodeError):
            worker_enabled_flag = False
    checks["single_writer_guard_respected"] = not (standby_guarded and worker_enabled_flag)
    all_ready = all(bool(v) for v in checks.values())
    return {
        "ready": all_ready,
        "checked_at": utc_now(),
        "python_version": platform.python_version(),
        "schema_version": schema_ver,
        "expected_schema_version": SCHEMA_VERSION,
        "standby_guarded": standby_guarded,
        "checks": checks,
        "fleet": fleet_public_summary(root),
    }


def check_standby_guard(db_path: str | Path) -> None:
    """Refuse active worker cycles on a cold-standby replica unless explicitly promoted."""
    guard_path = Path(db_path).resolve().parent / "standby-guard.json"
    if not guard_path.is_file():
        return
    try:
        doc = json.loads(guard_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("Invalid standby-guard.json; refusing active worker cycle") from exc
    if doc.get("active_dispatch_allowed") is not True:
        raise RuntimeError(
            "Cold-standby replica has active_dispatch_allowed=false; "
            "Desktop remains sole active scheduler/writer to preserve shared cooldowns."
        )


def _snapshot_standby_db(source_db: Path, target_db: Path, manifest_path: Path) -> dict[str, Any]:
    """Create a full online SQLite backup for cold_standby so corpus, queue, partitions, and cooldowns are preserved."""
    summary = {
        "full_online_backup": False,
        "archive_cooldown_until": None,
        "host_cooldown_keys": 0,
        "source_schedule_cooldowns": 0,
        "retained_contents": 0,
        "retained_archive_candidates": 0,
    }
    if source_db.is_file():
        target_db.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(f"file:{source_db.as_posix()}?mode=ro", uri=True)) as src_conn:
            with closing(sqlite3.connect(target_db)) as dst_conn:
                src_conn.backup(dst_conn)
        summary["full_online_backup"] = True
    init_db(target_db)
    seed_sources(target_db, manifest_path)
    with connect(target_db) as conn:
        cv_row = conn.execute("SELECT archive_cooldown_until FROM content_volume_state WHERE id=1").fetchone()
        if cv_row:
            summary["archive_cooldown_until"] = cv_row["archive_cooldown_until"]
        summary["host_cooldown_keys"] = int(conn.execute(
            "SELECT COUNT(*) FROM schema_meta WHERE key LIKE 'host_cooldown:%'"
        ).fetchone()[0])
        summary["source_schedule_cooldowns"] = int(conn.execute(
            "SELECT COUNT(*) FROM source_schedule WHERE cooldown_until IS NOT NULL"
        ).fetchone()[0])
        summary["retained_contents"] = int(conn.execute("SELECT COUNT(*) FROM contents").fetchone()[0])
        summary["retained_archive_candidates"] = int(conn.execute(
            "SELECT COUNT(*) FROM content_archive_candidates"
        ).fetchone()[0])
    return summary

def replicate_scanner_instance(
    target_dir: str | Path,
    *,
    mode: str = "worker_node",
    node_name: str = "bti-node",
    interval_seconds: int = DEFAULT_MIN_INTERVAL_SECONDS,
    source_root: str | Path = ROOT,
    source_db_path: str | Path | None = None,
) -> dict[str, Any]:
    """Create a lease-bound remote worker node bundle or a cooldown-preserving cold-standby DR replica."""
    if mode not in {"worker_node", "cold_standby"}:
        raise ValueError("Replication mode must be 'worker_node' or 'cold_standby'")
    if not 60 <= int(interval_seconds) <= 86400:
        raise ValueError("interval_seconds must be between 60 and 86400")

    src_root = Path(source_root).resolve()
    dst_root = Path(target_dir).resolve()
    if dst_root == src_root:
        raise ValueError("Replication target_dir must be distinct from source_root")
    dst_root.mkdir(parents=True, exist_ok=True)

    copied_files: dict[str, str] = {}
    if mode == "worker_node":
        worker_files = (
            "ops/msi-fetch-worker/executor.py",
            "ops/msi-fetch-worker/test_executor.py",
            "ops/msi-fetch-worker/install_remote.py",
            "ops/msi-fetch-worker/CONTRACT.md",
            "ops/msi-fetch-worker/README.md",
        )
        for rel_path in worker_files:
            src_file = src_root / rel_path
            if not src_file.is_file():
                continue
            dst_file = dst_root / Path(rel_path).name
            dst_file.parent.mkdir(parents=True, exist_ok=True)
            body = src_file.read_bytes()
            dst_file.write_bytes(body)
            copied_files[dst_file.name] = hashlib.sha256(body).hexdigest()

        key_dir = dst_root / "secrets"
        key_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        lease_key_path = key_dir / f"{node_name}-lease.key"
        if not lease_key_path.exists():
            lease_key_path.write_bytes(secrets.token_bytes(32))
            try:
                os.chmod(lease_key_path, 0o600)
            except OSError:
                pass
        node_descriptor = {
            "node_name": node_name,
            "mode": "worker_node",
            "created_at": utc_now(),
            "executor_sha256": copied_files.get("executor.py"),
            "lease_policy": {"issuer": "desktop", "policy": "archive-static-v2", "quota": 1},
            "resource_limits": {
                "memory_limit_bytes": 256 * 1024 * 1024,
                "cpu_cores": 1,
                "nice": 10,
                "wall_deadline_seconds": 22,
            },
            "files": copied_files,
        }
        manifest_file = dst_root / "replication-manifest.json"
        manifest_file.write_text(json.dumps(node_descriptor, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return {
            "status": "replicated",
            "mode": "worker_node",
            "node_name": node_name,
            "target_dir": str(dst_root),
            "file_count": len(copied_files),
            "executor_sha256": copied_files.get("executor.py"),
            "manifest_path": str(manifest_file),
        }

    # Cold-standby DR replica: disabled by default, copies active cooldowns/holds, guarded against dual-writer
    for rel_path in REPLICATED_SOURCE_FILES:
        src_file = src_root / rel_path
        if not src_file.is_file():
            continue
        dst_file = dst_root / rel_path
        dst_file.parent.mkdir(parents=True, exist_ok=True)
        body = src_file.read_bytes()
        dst_file.write_bytes(body)
        copied_files[rel_path] = hashlib.sha256(body).hexdigest()

    runtime_dir = dst_root / "runtime"
    (runtime_dir / "backups").mkdir(parents=True, exist_ok=True)
    now_iso = utc_now()
    (runtime_dir / "worker-enabled.json").write_text(
        json.dumps(
            {
                "enabled": False,
                "role": "cold_standby",
                "interval_seconds": max(300, int(interval_seconds)),
            },
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )
    (runtime_dir / "standby-guard.json").write_text(
        json.dumps(
            {
                "role": "cold_standby",
                "primary_writer": "desktop",
                "active_dispatch_allowed": False,
                "created_at": now_iso,
            },
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )

    replica_db = runtime_dir / "bti.sqlite3"
    src_db = Path(source_db_path).resolve() if source_db_path else (src_root / "runtime" / "bti.sqlite3")
    copied_cooldowns = _snapshot_standby_db(src_db, replica_db, dst_root / "config" / "public_sources.json")
    with connect(replica_db) as conn:
        seeded_count = int(conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0])
    doctor = scanner_doctor(
        replica_db,
        manifest_path=dst_root / "config" / "public_sources.json",
        content_manifest_path=dst_root / "config" / "content_sources.json",
        project_root=dst_root,
    )
    descriptor = {
        "node_name": node_name,
        "mode": "cold_standby",
        "created_at": now_iso,
        "worker_enabled": False,
        "active_dispatch_allowed": False,
        "interval_seconds": max(300, int(interval_seconds)),
        "seeded_sources": seeded_count,
        "preserved_cooldowns": copied_cooldowns,
        "doctor_ready": doctor["ready"],
        "files": copied_files,
    }
    manifest_file = dst_root / "replication-manifest.json"
    manifest_file.write_text(json.dumps(descriptor, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {
        "status": "replicated",
        "mode": "cold_standby",
        "node_name": node_name,
        "worker_enabled": False,
        "active_dispatch_allowed": False,
        "preserved_cooldowns": copied_cooldowns,
        "target_dir": str(dst_root),
        "file_count": len(copied_files),
        "seeded_sources": seeded_count,
        "doctor": doctor,
        "manifest_path": str(manifest_file),
    }

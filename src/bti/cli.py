from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .backup import create_backup
from .content_volume import run_content_volume_batch
from .db import init_db
from .domain import build_drafts
from .ingest import ingest, ingest_public_capture, seed_sources
from .public import health, public_export, worker_public_status
from .scanner import replicate_scanner_instance, scanner_doctor, scanner_public_status
from .server import serve
from .worker import ProcessLock, default_backup_dir, default_lock_path, run_cycle, run_worker

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = ROOT / "runtime" / "bti.sqlite3"
DEFAULT_MANIFEST = ROOT / "config" / "public_sources.json"
DEFAULT_CONTENT_MANIFEST = ROOT / "config" / "content_sources.json"


def _json_print(payload: object) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="bti", description="Future Intelligence (BTI) MVP")
    result.add_argument("--db", default=os.environ.get("BTI_DB_PATH", str(DEFAULT_DB)))
    result.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    sub = result.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="Initialize SQLite and source registry")
    ingest_parser = sub.add_parser("ingest", help="Run bounded public-source ingestion")
    ingest_parser.add_argument("--source", action="append", default=[], help="Limit to source slug; repeatable")
    ingest_parser.add_argument("--fetch-concurrency", type=int, choices=(1, 2, 3), default=2)
    capture_parser = sub.add_parser("ingest-capture", help="Import an explicit anonymous public browser capture")
    capture_parser.add_argument("--source", required=True, help="Configured source slug")
    capture_parser.add_argument("--input", required=True, help="Project-local capture JSON")
    volume_parser = sub.add_parser("content-volume", help="Run bounded retained/archive article-body extraction")
    volume_parser.add_argument("--content-manifest", default=str(DEFAULT_CONTENT_MANIFEST))
    volume_parser.add_argument("--artifact-dir", help="Ignored runtime output directory")
    volume_parser.add_argument(
        "--max-fetch", type=int, choices=range(0, 101), default=None,
        help="Override the manifest's bounded adaptive archive batch for this invocation",
    )
    sub.add_parser("build-drafts", help="Build three citation-bearing draft types")
    sub.add_parser("health", help="Print operational and business-gate health")
    sub.add_parser("worker-status", help="Print the public-safe worker status")
    sub.add_parser("scanner-status", help="Print the public-safe 24/7 scanner and fleet status")
    sub.add_parser("scanner-doctor", help="Verify SQLite schema, manifests, executor, and single-writer guard")
    rep_parser = sub.add_parser(
        "scanner-replicate",
        help="Create a lease-bound worker_node bundle or a cooldown-preserving cold_standby replica",
    )
    rep_parser.add_argument("--target", required=True, help="Target directory for the replicated bundle")
    rep_parser.add_argument("--mode", choices=("worker_node", "cold_standby"), default="worker_node")
    rep_parser.add_argument("--node-name", default="bti-node")
    rep_parser.add_argument("--interval-seconds", type=int, default=600)
    cycle_parser = sub.add_parser("cycle", help="Run one bounded unattended ingestion cycle")
    cycle_parser.add_argument("--interval-seconds", type=int, default=3600)
    cycle_parser.add_argument("--allow-short-interval", action="store_true")
    cycle_parser.add_argument("--no-backup", action="store_true")
    cycle_parser.add_argument("--backup-dir")
    cycle_parser.add_argument("--backup-keep", type=int, default=7)
    cycle_parser.add_argument("--fetch-concurrency", type=int, choices=(1, 2, 3), default=2)
    worker_parser = sub.add_parser("worker", help="Run deterministic unattended cycles")
    worker_parser.add_argument("--interval-seconds", type=int, default=3600)
    worker_parser.add_argument("--allow-short-interval", action="store_true")
    worker_parser.add_argument("--backup-dir")
    worker_parser.add_argument("--backup-keep", type=int, default=7)
    worker_parser.add_argument("--max-cycles", type=int, help="Acceptance only; omit for unattended operation")
    worker_parser.add_argument("--fetch-concurrency", type=int, choices=(1, 2, 3), default=2)
    worker_parser.add_argument("--adaptive-interval", action="store_true", help="Adapt sleep interval to backlog and cooldowns")
    backup_parser = sub.add_parser("backup", help="Create an integrity-checked online SQLite backup")
    backup_parser.add_argument("--output-dir")
    backup_parser.add_argument("--keep", type=int, default=7)
    serve_parser = sub.add_parser("serve", help="Run the read-only localhost dashboard")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8768)
    export_parser = sub.add_parser("export", help="Write a public-safe JSON export")
    export_parser.add_argument("--output", required=True)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    db_path = Path(args.db)
    manifest_path = Path(args.manifest)
    if args.command == "init":
        with ProcessLock(default_lock_path(db_path)):
            init_db(db_path)
            _json_print({"db": str(db_path), "sources": seed_sources(db_path, manifest_path)})
    elif args.command == "ingest":
        with ProcessLock(default_lock_path(db_path)):
            _json_print(ingest(
                db_path, manifest_path, only=args.source or None, concurrency=args.fetch_concurrency,
            ))
    elif args.command == "ingest-capture":
        with ProcessLock(default_lock_path(db_path)):
            _json_print(ingest_public_capture(
                db_path, manifest_path, source_slug=args.source, capture_path=args.input,
            ))
    elif args.command == "content-volume":
        artifact_dir = Path(args.artifact_dir) if args.artifact_dir else db_path.resolve().parent / "content-volume"
        with ProcessLock(default_lock_path(db_path)):
            _json_print(run_content_volume_batch(
                db_path,
                args.content_manifest,
                artifact_dir=artifact_dir,
                max_fetch=args.max_fetch,
            ))
    elif args.command == "build-drafts":
        with ProcessLock(default_lock_path(db_path)):
            _json_print(build_drafts(db_path))
    elif args.command == "health":
        _json_print(health(db_path))
    elif args.command == "worker-status":
        _json_print(worker_public_status(db_path))
    elif args.command == "scanner-status":
        _json_print(scanner_public_status(db_path))
    elif args.command == "scanner-doctor":
        doc = scanner_doctor(db_path, manifest_path=manifest_path)
        _json_print(doc)
        return 0 if doc["ready"] else 2
    elif args.command == "scanner-replicate":
        _json_print(replicate_scanner_instance(
            args.target,
            mode=args.mode,
            node_name=args.node_name,
            interval_seconds=args.interval_seconds,
            source_db_path=db_path,
        ))
    elif args.command == "cycle":
        backup_dir = None if args.no_backup else Path(args.backup_dir) if args.backup_dir else default_backup_dir(db_path)
        _json_print(run_cycle(
            db_path, manifest_path, interval_seconds=args.interval_seconds,
            allow_short_interval=args.allow_short_interval, backup_dir=backup_dir,
            backup_keep=args.backup_keep,
            fetch_concurrency=args.fetch_concurrency,
        ))
    elif args.command == "worker":
        backup_dir = Path(args.backup_dir) if args.backup_dir else default_backup_dir(db_path)
        result = run_worker(
            db_path, manifest_path, interval_seconds=args.interval_seconds,
            allow_short_interval=args.allow_short_interval, backup_dir=backup_dir,
            backup_keep=args.backup_keep, max_cycles=args.max_cycles,
            fetch_concurrency=args.fetch_concurrency,
            adaptive_interval=args.adaptive_interval,
        )
        _json_print({"status": "stopped", **result})
    elif args.command == "backup":
        output_dir = Path(args.output_dir) if args.output_dir else default_backup_dir(db_path)
        _json_print(create_backup(db_path, output_dir, keep=args.keep))
    elif args.command == "serve":
        serve(db_path, args.host, args.port)
    elif args.command == "export":
        output = Path(args.output).resolve()
        if ROOT.resolve() not in output.parents:
            raise SystemExit("Export path must stay inside the project root")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(public_export(db_path), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        _json_print({"output": str(output)})
    return 0

from __future__ import annotations

import argparse
import http.client
import json
import os
import platform
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from .db import connect, utc_now
from .public import drafts, health
from .server import create_server

ROOT = Path(__file__).resolve().parents[2]


def run_command(command: list[str]) -> dict[str, Any]:
    started = time.perf_counter()
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    result = subprocess.run(command, cwd=ROOT, env=env, text=True, capture_output=True, encoding="utf-8", errors="replace")
    combined = "\n".join(part.strip() for part in (result.stdout, result.stderr) if part.strip())
    return {
        "command": subprocess.list2cmdline(command),
        "exit_code": result.returncode,
        "duration_ms": int((time.perf_counter() - started) * 1000),
        "output_tail": combined[-4000:],
    }


def http_smoke(db_path: Path) -> dict[str, Any]:
    server = create_server(db_path, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_port
    results: dict[str, Any] = {"port": port, "endpoints": {}}
    try:
        for path in (
            "/", "/api/health", "/api/overview", "/api/worker", "/api/dossiers",
            "/api/articles", "/api/evidence", "/api/drafts", "/api/export.json",
        ):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("GET", path)
            response = conn.getresponse(); body = response.read(); headers = {k.lower(): v for k, v in response.getheaders()}; conn.close()
            results["endpoints"][path] = {
                "status": response.status,
                "bytes": len(body),
                "content_type": headers.get("content-type"),
                "csp": bool(headers.get("content-security-policy")),
                "nosniff": headers.get("x-content-type-options") == "nosniff",
            }
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("POST", "/api/dossiers", body=b"{}")
        response = conn.getresponse(); response.read(); conn.close()
        results["mutation_status"] = response.status
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=3)
    results["passed"] = (
        all(item["status"] == 200 and item["csp"] and item["nosniff"] for item in results["endpoints"].values())
        and results["mutation_status"] == 405
    )
    return results


def source_evidence(db_path: Path) -> list[dict[str, Any]]:
    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT s.slug,s.name,s.url,s.source_type,s.capability_status,s.last_attempt_at,s.last_success_at,
                   f.status_code,f.outcome,f.byte_count,f.content_type,f.response_hash,f.error_class
            FROM sources s
            LEFT JOIN fetch_events f ON f.id=(SELECT id FROM fetch_events latest WHERE latest.source_id=s.id ORDER BY latest.id DESC LIMIT 1)
            WHERE s.access_level='public'
            ORDER BY s.id
            """
        ).fetchall()
        return [dict(row) for row in rows]


def source_attempt_history(db_path: Path) -> list[dict[str, Any]]:
    with connect(db_path) as conn:
        rows = conn.execute(
            """SELECT s.slug,f.fetched_at,f.status_code,f.outcome,f.byte_count,f.content_type,
                      f.response_hash,f.error_class
               FROM fetch_events f JOIN sources s ON s.id=f.source_id
               WHERE s.slug IN ('blockchain-100-2025-trader-winners',
                                'cryptomechanic-initial-public-posts',
                                'copy-trading-home-30d-public')
               ORDER BY f.id"""
        ).fetchall()
        return [dict(row) for row in rows]


def build_report(db_path: Path) -> tuple[dict[str, Any], int]:
    compile_result = run_command([sys.executable, "-m", "compileall", "-q", "src"])
    node = shutil.which("node")
    static_results = [
        run_command([node, "--check", path]) if node else {
            "command": f"node --check {path}", "exit_code": 127, "duration_ms": 0,
            "output_tail": "Node.js not available",
        }
        for path in ("src/bti/static/app.js", "src/bti/static/workspace.js")
    ]
    workspace_tests = run_command([node, "--test", "tests/test_workspace.js"]) if node else {
        "command": "node --test tests/test_workspace.js", "exit_code": 127, "duration_ms": 0,
        "output_tail": "Node.js not available",
    }
    test_result = run_command([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"])
    public_health = health(db_path)
    smoke = http_smoke(db_path)
    sources = source_evidence(db_path)
    attempts = source_attempt_history(db_path)
    public_drafts = drafts(db_path)
    draft_types = sorted({item["artifact_type"] for item in public_drafts})
    successful_real_sources = sum(1 for item in sources if item["outcome"] == "success" and item["response_hash"])
    phase2_report_path = ROOT / "runtime" / "coordinator-phase2-live" / "report.json"
    phase2_acceptance: dict[str, Any] = {"available": False, "method": "coordinator isolated copy; never production"}
    if phase2_report_path.is_file():
        observed = json.loads(phase2_report_path.read_text(encoding="utf-8"))
        phase2_acceptance = {
            "available": True,
            "method": "coordinator isolated copy; never production",
            "checked_at": observed.get("checked_at"),
            "cycle_count": len(observed.get("cycles") or []),
            "cycle_statuses": [cycle.get("status") for cycle in observed.get("cycles") or []],
            "cycle_attempted_sources": [
                (cycle.get("source_counts") or {}).get("attempted_source_count")
                for cycle in observed.get("cycles") or []
            ],
            "historical_accepted": [
                ((cycle.get("source_counts") or {}).get("historical_backfill") or {}).get("accepted")
                for cycle in observed.get("cycles") or []
            ],
            "backup_statuses": [cycle.get("backup_status") for cycle in observed.get("cycles") or []],
            "schema_version": (observed.get("health") or {}).get("schema_version"),
            "integrity": observed.get("integrity"),
            "foreign_key_error_count": len(observed.get("fk_errors") or []),
            "awardee_substantive_contents": (
                ((observed.get("health") or {}).get("trader_quality") or {}).get("awardee_substantive_contents")
            ),
            "trader_quality_status": (
                ((observed.get("health") or {}).get("trader_quality") or {}).get("status")
            ),
        }
    checks = {
        "compile": compile_result["exit_code"] == 0,
        "static_javascript_syntax": all(item["exit_code"] == 0 for item in static_results),
        "personal_workspace_privacy_tests": workspace_tests["exit_code"] == 0,
        "unit_integration_security_tests": test_result["exit_code"] == 0,
        "phase2_worker_history_concurrency_privacy_regressions": test_result["exit_code"] == 0,
        "http_smoke": smoke["passed"],
        "real_public_fetches": successful_real_sources >= 5,
        "pipeline_smoke": public_health["pipeline_smoke"]["status"] == "passed",
        "official_trader_awardees": public_health["trader_quality"]["official_trader_awardees"] == 20,
        "awardee_substantive_articles": public_health["trader_quality"]["awardee_substantive_contents"] >= 2,
        "unlinked_operator_captured_performance": (
            public_health["trader_quality"]["unlinked_captured_performance_portfolios"] >= 3
            and public_health["trader_quality"]["unlinked_platform_performance_portfolios"] == 0
            and public_health["trader_quality"]["awardees_with_platform_performance"] == 0
        ),
        "grounded_research_outputs": bool(public_drafts) and all(
            item["body"].get("generator") == "grounded_research_v1"
            and item["body"].get("citations")
            and (
                item["artifact_type"] == "content"
                or item["body"]["research_brief"].get(
                    "strategy_eligible" if item["artifact_type"] == "strategy_hypothesis" else "market_eligible"
                ) is True
            )
            for item in public_drafts
        ),
        "private_runtime_excluded": "runtime/" in (ROOT / ".gitignore").read_text(encoding="utf-8"),
    }
    passed = all(checks.values())
    report = {
        "schema": "bti-verification-v4",
        "generated_at": utc_now(),
        "overall_status": "passed" if passed else "failed",
        "environment": {
            "python": platform.python_version(),
            "sqlite": sqlite3.sqlite_version,
            "platform": platform.system(),
        },
        "checks": checks,
        "commands": [compile_result, *static_results, workspace_tests, test_result],
        "public_health": public_health,
        "http_smoke": smoke,
        "phase2_isolated_acceptance": phase2_acceptance,
        "real_source_evidence": sources,
        "source_attempt_history": attempts,
        "draft_types": draft_types,
        "research_capabilities": {
            kind: "available_source_conditions" if kind in draft_types else "insufficient_source_conditions"
            for kind in ("strategy_hypothesis", "market_scenario")
        },
        "known_gaps": [
            "Direct Square profile HTML probe did not yield substantive readable content; the UID-scoped initial public JSON batch did.",
            "Crypto Mechanic pagination stops at the subsequent public login gate; no bypass or deep pagination was attempted.",
            "No official Trader awardee is linked to a Copy Trading portfolio by first-party evidence; awardee performance remains unknown.",
            "Direct Copy Trading HTML returned HTTP 202 without __APP_DATA; the three performance records are operator_capture/unverified because the explicit browser capture provenance is not independently authenticated.",
            "Rounded profile follower DOM was not imported as an exact count; follower count remains unknown.",
            "The public Square friendly JSON endpoint is not documented as a stable read API; adapter remains manual and bounded.",
            "The requested trailing two-calendar-year window is enforced, but four canonical seeds and bounded exact-UID rotation do not establish complete historical coverage.",
            "The 12-request concurrency benchmark showed no wall-time throughput gain at the shared 1.5-second host pace; no speedup or no-block guarantee is claimed.",
            "Desktop startup remains AtLogOn for the current interactive user, not a pre-login service or verified 24/7 reboot recovery.",
        ],
        "guardrails": {
            "trading": False,
            "automatic_publishing": False,
            "authentication_used": False,
            "external_infrastructure_changed": False,
        },
    }
    return report, 0 if passed else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(ROOT / "runtime" / "bti.sqlite3"))
    parser.add_argument("--output", default=str(ROOT / "artifacts" / "verification.json"))
    args = parser.parse_args(argv)
    output = Path(args.output).resolve()
    if ROOT.resolve() not in output.parents:
        raise SystemExit("Verification output must stay inside the project root")
    report, status = build_report(Path(args.db))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(output), "overall_status": report["overall_status"], "checks": report["checks"]}, ensure_ascii=False, indent=2))
    return status


if __name__ == "__main__":
    raise SystemExit(main())

"""Project-scoped Windows supervisor; no trading, ingest, or credential output."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime"
PORT = 8768
RESTART_WINDOW_SECONDS = 600
MAX_RESTARTS_PER_WINDOW = 6
RESTART_COOLDOWN_SECONDS = 300

def utcnow():
    return datetime.now(timezone.utc).isoformat()

def port_busy():
    with socket.socket() as sock:
        sock.settimeout(1)
        return sock.connect_ex(("127.0.0.1", PORT)) == 0

def worker_enabled(config_path=None):
    path = Path(config_path) if config_path else RUNTIME / "worker-enabled.json"
    if not path.is_file():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(payload, dict) and payload.get("enabled") is True

def worker_interval(config_path=None):
    path = Path(config_path) if config_path else RUNTIME / "worker-enabled.json"
    if not path.is_file():
        return 3600
    payload = json.loads(path.read_text(encoding="utf-8"))
    value = payload.get("interval_seconds", 3600) if isinstance(payload, dict) else 3600
    if type(value) is not int or not 300 <= value <= 86400:
        raise ValueError("Worker interval must be an integer between300 and86400 seconds")
    return value

def build_commands(python, db, manifest, cloudflared, config, *, app_only=False, enable_worker=False, worker_interval_seconds=3600):
    if type(worker_interval_seconds) is not int or not 300 <= worker_interval_seconds <= 86400:
        raise ValueError("Worker interval is outside the supported bounds")
    python = str(Path(python).resolve())
    db = str(Path(db).resolve())
    manifest = str(Path(manifest).resolve())
    commands = {
        "app": [python, "-m", "bti", "--db", db, "--manifest", manifest,
                "serve", "--host", "127.0.0.1", "--port", str(PORT)]
    }
    if not app_only:
        commands["tunnel"] = [str(Path(cloudflared)), "tunnel", "--config", str(Path(config).resolve()),
                              "run"]
    if enable_worker:
        commands["worker"] = [python, "-m", "bti", "--db", db, "--manifest", manifest,
                              "worker", "--interval-seconds", str(worker_interval_seconds),
                              "--backup-dir", str((Path(db).parent / "backups").resolve()),
                              "--backup-keep", "7"]
    return commands

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--app-only", action="store_true")
    args = parser.parse_args()
    config = RUNTIME / "cloudflared.yml"
    db = RUNTIME / "bti.sqlite3"
    manifest = ROOT / "config" / "public_sources.json"
    enable_worker = worker_enabled()
    interval_seconds = worker_interval()
    cloudflared = Path("C:/Program Files (x86)/cloudflared/cloudflared.exe")
    requirements = [ROOT / "src/bti/__main__.py", db, manifest]
    if not args.app_only:
        requirements += [config, cloudflared]
    missing = [str(p) for p in requirements if not p.is_file()]
    if missing:
        print(json.dumps({"ready": False, "missing": missing}))
        return 2
    if args.check:
        print(json.dumps({"ready": True, "root": str(ROOT), "python": sys.executable, "bind": "127.0.0.1:8768", "tunnel": not args.app_only, "worker_enabled": enable_worker, "worker_interval_seconds": interval_seconds}))
        return 0
    RUNTIME.mkdir(parents=True, exist_ok=True)
    lock = (RUNTIME / "supervisor.lock").open("a+b")
    if os.name == "nt":
        import msvcrt
        if lock.seek(0, 2) == 0:
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            print("Another project supervisor owns the runtime; no duplicate started.")
            return 0
    if port_busy():
        print("Port 8768 is occupied; refusing to adopt or terminate an unknown listener.")
        return 3
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    commands = build_commands(sys.executable, db, manifest, cloudflared, config,
                              app_only=args.app_only, enable_worker=enable_worker, worker_interval_seconds=interval_seconds)
    children = {}
    logs = {}
    restarts = {name: 0 for name in commands}
    restart_history = {name: [] for name in commands}
    blocked_until = {name: 0.0 for name in commands}
    stopping = False
    def stop(signum, frame):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        while not stopping:
            for name, command in commands.items():
                process = children.get(name)
                if process is None or process.poll() is not None:
                    current = time.monotonic()
                    if current < blocked_until[name]:
                        continue
                    if process is not None:
                        restarts[name] += 1
                        logs[name].close()
                        restart_history[name] = [stamp for stamp in restart_history[name]
                                                 if current - stamp < RESTART_WINDOW_SECONDS]
                        restart_history[name].append(current)
                        if len(restart_history[name]) >= MAX_RESTARTS_PER_WINDOW:
                            blocked_until[name] = current + RESTART_COOLDOWN_SECONDS
                            restart_history[name].clear()
                            continue
                        time.sleep(min(30, 2 * restarts[name]))
                    logfile = RUNTIME / (name + ".log")
                    if logfile.exists() and logfile.stat().st_size > 10_000_000:
                        logfile.replace(RUNTIME / (name + ".previous.log"))
                    logs[name] = logfile.open("ab", buffering=0)
                    children[name] = subprocess.Popen(command, cwd=ROOT, env=env, stdin=subprocess.DEVNULL, stdout=logs[name], stderr=subprocess.STDOUT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            state = {"updated_at": utcnow(), "supervisor_pid": os.getpid(), "root": str(ROOT), "worker_enabled": enable_worker, "processes": {name: {"pid": p.pid, "exit_code": p.poll(), "restarts": restarts[name]} for name, p in children.items()}}
            temporary = RUNTIME / "supervisor-state.tmp"
            temporary.write_text(json.dumps(state, indent=2), encoding="utf-8")
            temporary.replace(RUNTIME / "supervisor-state.json")
            for _ in range(10):
                if stopping:
                    break
                time.sleep(1)
    finally:
        for process in children.values():
            if process.poll() is None:
                process.terminate()
        for process in children.values():
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        for logfile in logs.values():
            logfile.close()
        lock.close()
    return 0

if __name__ == "__main__":
    raise SystemExit(main())

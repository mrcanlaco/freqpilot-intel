from __future__ import annotations

import json
import socket
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from . import public
from .campaigns import load_campaign_assets

STATIC_DIR = Path(__file__).with_name("static")
CAMPAIGN_DIR = Path(__file__).resolve().parents[2] / "artifacts"
STATIC_TYPES = {
    "index.html": "text/html; charset=utf-8",
    "style.css": "text/css; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
    "workspace.js": "text/javascript; charset=utf-8",
}
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; img-src 'self' https: data:; style-src 'self'; script-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
    "Cache-Control": "no-store",
}
REJECTED_BODY_DRAIN_LIMIT = 64 * 1024
REJECTED_BODY_DRAIN_TIMEOUT_SECONDS = 0.25


class AppServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], handler: type[BaseHTTPRequestHandler], db_path: str | Path):
        # Pin a coherent UI release for this process; API permissions remain live.
        self.static_assets = {name: (STATIC_DIR / name).read_bytes() for name in STATIC_TYPES}
        self.campaign_assets = load_campaign_assets(CAMPAIGN_DIR)
        super().__init__(address, handler)
        self.db_path = Path(db_path)


class Handler(BaseHTTPRequestHandler):
    server_version = "BTI/0.1"
    sys_version = ""

    @property
    def app(self) -> AppServer:
        return self.server  # type: ignore[return-value]

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.log_date_time_string()} {self.client_address[0]} {fmt % args}")

    def _headers(self, status: int, content_type: str, length: int, csp: str | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        for name, value in SECURITY_HEADERS.items():
            self.send_header(name, csp if name == "Content-Security-Policy" and csp is not None else value)
        self.end_headers()

    def _send(self, body: bytes, content_type: str, status: int = 200, *, head: bool = False, csp: str | None = None) -> None:
        self._headers(status, content_type, len(body), csp)
        if not head:
            self.wfile.write(body)

    def _json(self, payload: Any, status: int = 200, *, head: bool = False) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._send(body, "application/json; charset=utf-8", status, head=head)

    def _error(self, status: int, message: str, *, head: bool = False) -> None:
        self._json({"error": HTTPStatus(status).phrase, "message": message}, status, head=head)

    def _route(self, *, head: bool = False) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=False)
        one = lambda name: query.get(name, [None])[0]
        try:
            if path == "/":
                self._static("index.html", head=head)
            elif path == "/assets/style.css":
                self._static("style.css", head=head)
            elif path == "/assets/app.js":
                self._static("app.js", head=head)
            elif path == "/assets/workspace.js":
                self._static("workspace.js", head=head)
            elif path in self.app.campaign_assets:
                body, content_type, csp = self.app.campaign_assets[path]
                self._send(body, content_type, head=head, csp=csp)
            elif path == "/api/health":
                self._json(public.health(self.app.db_path), head=head)
            elif path == "/api/overview":
                self._json(public.overview(self.app.db_path), head=head)
            elif path == "/api/worker":
                self._json(public.worker_public_status(self.app.db_path), head=head)
            elif path == "/api/scanner":
                self._json(public.scanner_public_status(self.app.db_path), head=head)
            elif path == "/api/discovery":
                self._json(public.public_crawler_status(self.app.db_path), head=head)
            elif path == "/api/sources":
                self._json({"items": public.sources(self.app.db_path)}, head=head)
            elif path == "/api/dossiers":
                self._json(public.dossier_listing(
                    self.app.db_path, program=one("program"), year=one("year"),
                    category=one("category"), author=one("author"),
                    window=one("window") or "30d", date_from=one("date_from"),
                    date_to=one("date_to"), sort=one("sort") or "official_rank",
                    limit=int(one("limit") or 12), offset=int(one("offset") or 0),
                ), head=head)
            elif path == "/api/articles":
                self._json(public.articles(
                    self.app.db_path, q=one("q"), author=one("author"),
                    date_from=one("date_from"), date_to=one("date_to"),
                    sort=one("sort") or "latest",
                    lang=one("lang"), asset=one("asset"), topic=one("topic"),
                    limit=int(one("limit") or 12), offset=int(one("offset") or 0),
                    entity=one("entity"), post_ids=one("post_ids"), seen_after=one("seen_after"),
                    quality_only=one("quality_only") == "1",
                    basis=one("basis") or "all", cohort=one("cohort") or "all",
                ), head=head)
            elif path.startswith("/api/articles/"):
                post_key = urllib.parse.unquote(path.removeprefix("/api/articles/"))
                if not post_key or "/" in post_key or "\\" in post_key or ".." in post_key:
                    self._error(400, "Invalid article key", head=head)
                    return
                item = public.article_detail(self.app.db_path, post_key)
                if item is None:
                    self._error(404, "Article not found", head=head)
                else:
                    self._json(item, head=head)
            elif path.startswith("/api/dossiers/"):
                slug = urllib.parse.unquote(path.removeprefix("/api/dossiers/"))
                if not slug or "/" in slug or "\\" in slug or ".." in slug:
                    self._error(400, "Invalid dossier slug", head=head)
                    return
                item = public.dossier(
                    self.app.db_path, slug, window=one("window") or "30d",
                    date_from=one("date_from"), date_to=one("date_to"),
                )
                if item is None:
                    self._error(404, "Dossier not found", head=head)
                else:
                    self._json(item, head=head)
            elif path == "/api/evidence":
                self._json(public.evidence_listing(
                    self.app.db_path, q=one("q"), pillar=one("pillar"), level=one("level"),
                    limit=int(one("limit") or 20), offset=int(one("offset") or 0),
                ), head=head)
            elif path == "/api/drafts":
                self._json({"items": public.drafts(self.app.db_path)}, head=head)
            elif path == "/api/export.json":
                self._json(public.public_export(self.app.db_path), head=head)
            else:
                self._error(404, "Route not found", head=head)
        except ValueError as exc:
            self._error(400, str(exc), head=head)
        except Exception as exc:
            self._error(500, f"Public read failed: {type(exc).__name__}", head=head)

    def _static(self, filename: str, *, head: bool = False) -> None:
        body = self.app.static_assets.get(filename)
        if body is None:
            self._error(404, "Asset not found", head=head)
            return
        self._send(body, STATIC_TYPES[filename], head=head)

    def do_GET(self) -> None:  # noqa: N802
        self._route()

    def do_HEAD(self) -> None:  # noqa: N802
        self._route(head=True)

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self.send_header("Allow", "GET, HEAD, OPTIONS")
        for name, value in SECURITY_HEADERS.items():
            self.send_header(name, value)
        self.end_headers()

    def _drain_rejected_body(self) -> bool:
        """Boundedly consume a declared body before closing a denied mutation.

        Windows can turn a close with unread request bytes into an abortive TCP
        reset, which may discard the already-written 405 at the client.  This
        reads only a small, declared body and never parses or dispatches it.
        """
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            return True
        try:
            declared_length = int(raw_length, 10)
        except (TypeError, ValueError):
            return False
        if declared_length < 0:
            return False
        if declared_length == 0:
            return True

        target = min(declared_length, REJECTED_BODY_DRAIN_LIMIT)
        drained = 0
        deadline = time.monotonic() + REJECTED_BODY_DRAIN_TIMEOUT_SECONDS
        previous_timeout = self.connection.gettimeout()
        try:
            while drained < target:
                remaining_time = deadline - time.monotonic()
                if remaining_time <= 0:
                    break
                self.connection.settimeout(remaining_time)
                chunk = self.rfile.read1(min(8192, target - drained))
                if not chunk:
                    break
                drained += len(chunk)
        except (TimeoutError, OSError):
            pass
        finally:
            try:
                self.connection.settimeout(previous_timeout)
            except OSError:
                pass
        return declared_length <= REJECTED_BODY_DRAIN_LIMIT and drained == declared_length

    def _method_not_allowed(self) -> None:
        body_drained = self._drain_rejected_body()
        body = json.dumps({"error": "Method Not Allowed", "message": "Public HTTP surface is read-only"}).encode()
        self.close_connection = True
        self.send_response(405)
        self.send_header("Allow", "GET, HEAD, OPTIONS")
        self.send_header("Connection", "close")
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for name, value in SECURITY_HEADERS.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()
        if not body_drained:
            try:
                self.connection.shutdown(socket.SHUT_WR)
            except OSError:
                pass

    do_POST = _method_not_allowed  # type: ignore[assignment]
    do_PUT = _method_not_allowed  # type: ignore[assignment]
    do_PATCH = _method_not_allowed  # type: ignore[assignment]
    do_DELETE = _method_not_allowed  # type: ignore[assignment]


def create_server(db_path: str | Path, host: str = "127.0.0.1", port: int = 8768) -> AppServer:
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("The MVP server only binds to a loopback host")
    return AppServer((host, port), Handler, db_path)


def serve(db_path: str | Path, host: str = "127.0.0.1", port: int = 8768) -> None:
    server = create_server(db_path, host, port)
    print(f"BTI dashboard: http://{host}:{server.server_port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

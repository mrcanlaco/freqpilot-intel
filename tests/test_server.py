from __future__ import annotations

import http.client
import json
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from bti.db import init_db
from bti.server import REJECTED_BODY_DRAIN_LIMIT, create_server

TEST_TMP = Path(__file__).resolve().parents[1] / "runtime" / "test-tmp"


class ServerSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        TEST_TMP.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=TEST_TMP)
        self.db = Path(self.temp.name) / "server.sqlite3"
        init_db(self.db)
        self.server = create_server(self.db, "127.0.0.1", 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_port

    def tearDown(self) -> None:
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=3)
        self.temp.cleanup()

    def request(self, method: str, path: str) -> tuple[int, dict[str, str], bytes]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request(method, path, body=b"{}" if method == "POST" else None)
        response = conn.getresponse(); body = response.read(); headers = {k.lower(): v for k, v in response.getheaders()}; conn.close()
        return response.status, headers, body

    def delayed_post(self, delay_seconds: float = 0.03) -> tuple[int, dict[str, str], bytes]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.putrequest("POST", "/api/dossiers")
        conn.putheader("Content-Length", "2")
        conn.endheaders()
        time.sleep(delay_seconds)
        conn.send(b"{}")
        response = conn.getresponse()
        body = response.read()
        headers = {key.lower(): value for key, value in response.getheaders()}
        status = response.status
        conn.close()
        return status, headers, body

    def raw_post(self, declared_length: int, body: bytes) -> bytes:
        request_head = (
            "POST /api/dossiers HTTP/1.0\r\n"
            "Host: 127.0.0.1\r\n"
            f"Content-Length: {declared_length}\r\n"
            "\r\n"
        ).encode("ascii")
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as client:
            client.settimeout(5)
            client.sendall(request_head + body)
            chunks: list[bytes] = []
            while True:
                chunk = client.recv(8192)
                if not chunk:
                    break
                chunks.append(chunk)
        return b"".join(chunks)

    def test_health_is_json_and_has_security_headers(self) -> None:
        status, headers, body = self.request("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["status"], "ok")
        self.assertEqual(headers["x-content-type-options"], "nosniff")
        self.assertEqual(headers["x-frame-options"], "DENY")
        self.assertIn("default-src 'self'", headers["content-security-policy"])
        self.assertIn("form-action 'none'", headers["content-security-policy"])

        status, _, body = self.request("GET", "/api/discovery")
        self.assertEqual(status, 200)
        discovery = json.loads(body)
        self.assertEqual(discovery["policy"]["status"], "not_checked")
        self.assertIn("không phải bài đã xác minh", discovery["discovery"]["definition"])

    def test_all_mutations_are_denied(self) -> None:
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            with self.subTest(method=method):
                status, headers, body = self.request(method, "/api/dossiers")
                self.assertEqual(status, 405)
                self.assertEqual(headers["allow"], "GET, HEAD, OPTIONS")
                self.assertEqual(headers["connection"], "close")
                self.assertIn(b"read-only", body)

    def test_delayed_small_post_body_reliably_receives_denial(self) -> None:
        for attempt in range(20):
            with self.subTest(attempt=attempt):
                status, headers, body = self.delayed_post()
                self.assertEqual(status, 405)
                self.assertEqual(headers["connection"], "close")
                self.assertIn(b"read-only", body)

    def test_oversized_rejected_body_is_bounded_and_denied(self) -> None:
        body = b"x" * (REJECTED_BODY_DRAIN_LIMIT + 1)
        response = self.raw_post(len(body), body)
        self.assertIn(b" 405 ", response.split(b"\r\n", 1)[0])
        self.assertIn(b"Connection: close\r\n", response)
        self.assertIn(b"read-only", response)

    def test_incomplete_rejected_body_times_out_and_is_denied(self) -> None:
        started = time.monotonic()
        response = self.raw_post(32, b"{}")
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 2.0)
        self.assertIn(b" 405 ", response.split(b"\r\n", 1)[0])
        self.assertIn(b"Connection: close\r\n", response)
        self.assertIn(b"read-only", response)

    def test_traversal_and_admin_routes_are_not_exposed(self) -> None:
        for path in ("/assets/../db.py", "/admin", "/api/admin", "/api/dossiers/..%2Fsecret"):
            with self.subTest(path=path):
                status, _, _ = self.request("GET", path)
                self.assertIn(status, (400, 404))

    def test_non_loopback_bind_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            create_server(self.db, "0.0.0.0", 0)

    def test_static_assets_and_public_detail_routes(self) -> None:
        for path in ("/", "/assets/app.js", "/assets/style.css", "/assets/workspace.js"):
            with self.subTest(path=path):
                status, headers, body = self.request("GET", path)
                self.assertEqual(status, 200)
                self.assertTrue(body)
                self.assertEqual(headers["x-content-type-options"], "nosniff")
        status, _, _ = self.request("GET", "/api/articles/..%2Fsecret")
        self.assertEqual(status, 400)
        status, _, _ = self.request("GET", "/api/articles/nonexistent-post-999")
        self.assertEqual(status, 404)
        status, _, scanner_body = self.request("GET", "/api/scanner")
        self.assertEqual(status, 200)
        scanner_payload = json.loads(scanner_body)
        self.assertEqual(scanner_payload["status"], "not_started")
        self.assertFalse(scanner_payload["autonomous_operation"]["pre_login_reboot_guarantee"])
        self.assertEqual(scanner_payload["replication_architecture"]["supported_modes"], ["worker_node", "cold_standby"])

    def test_static_release_stays_coherent_until_restart(self) -> None:
        assets = Path(self.temp.name) / "assets"
        assets.mkdir()
        for name in ("index.html", "style.css", "app.js", "workspace.js"):
            (assets / name).write_bytes(f"release-one:{name}".encode())
        with patch("bti.server.STATIC_DIR", assets):
            server = create_server(self.db, "127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for name, route in (("index.html", "/"), ("app.js", "/assets/app.js")):
                (assets / name).write_bytes(f"release-two:{name}".encode())
                client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
                client.request("GET", route)
                response = client.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(response.read(), f"release-one:{name}".encode())
                client.close()
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=3)

    def test_listing_defaults_and_invalid_page_bounds(self) -> None:
        for route, default in (("articles", 12), ("dossiers", 12), ("evidence", 20)):
            status, _, body = self.request("GET", f"/api/{route}")
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)["pagination"], {
                "limit": default, "offset": 0, "total": 0, "has_more": False, "next_offset": None,
            })
            for query in ("limit=0", "limit=101", "limit=no", "offset=-1", "offset=1.5"):
                with self.subTest(route=route, query=query):
                    status, _, _ = self.request("GET", f"/api/{route}?{query}")
                    self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()

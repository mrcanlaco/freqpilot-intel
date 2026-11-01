"""Synthetic fixtures only: published snapshots must not expose other files."""
from __future__ import annotations

import base64
import hashlib
import http.client
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from bti.campaigns import CAMPAIGN_FILES, METHOD_FILES, RESEARCH_FILES
from bti.db import init_db
from bti.server import create_server

TEST_TMP = Path(__file__).resolve().parents[1] / "runtime" / "test-tmp"


class CampaignPublicationTests(unittest.TestCase):
    def setUp(self) -> None:
        TEST_TMP.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=TEST_TMP)
        self.directory = Path(self.temp.name)
        self.db = self.directory / "test.sqlite3"
        init_db(self.db)
        self.script = "\n/* Synthetic publication fixture: mẫu */\nwindow.fixture = 'original';\n"
        self.style = "\n/* Synthetic style */\nbody { color: black; }\n"
        self.html = (
            '<!doctype html><html><head><style>' + self.style + '</style></head><body>'
            '<script type="application/json">{"synthetic":true,"public_dossier_url":"https://legacy.example.invalid/?dossier=test"}</script>'
            '<script>' + self.script + '</script></body></html>'
        )
        for html_name, json_name, csv_name in (*CAMPAIGN_FILES.values(), RESEARCH_FILES, METHOD_FILES):
            (self.directory / html_name).write_bytes(self.html.replace('\n', '\r\n').encode('utf-8'))
            (self.directory / json_name).write_text('{"synthetic": true}', encoding='utf-8')
            (self.directory / csv_name).write_bytes(b'synthetic,value\r\nfixture,1\r\n')
        (self.directory / "private-record.json").write_text('DO-NOT-PUBLISH', encoding='utf-8')
        self.start_server()

    def start_server(self) -> None:
        with patch('bti.server.CAMPAIGN_DIR', self.directory):
            self.server = create_server(self.db, '127.0.0.1', 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop_server(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def tearDown(self) -> None:
        self.stop_server()
        self.temp.cleanup()

    def request(self, path: str, method: str = 'GET') -> tuple[int, dict[str, str], bytes]:
        client = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        client.request(method, path)
        response = client.getresponse()
        result = response.status, {k.lower(): v for k, v in response.getheaders()}, response.read()
        client.close()
        return result

    @staticmethod
    def source_hash(source: str) -> str:
        return "'sha256-" + base64.b64encode(hashlib.sha256(source.encode('utf-8')).digest()).decode('ascii') + "'"

    def test_snapshot_code_has_exact_csp_without_weakening_app(self) -> None:
        for route in [*(f"/blockchain100/{year}" for year in CAMPAIGN_FILES), "/research/portfolios", "/research/methods"]:
            with self.subTest(route=route):
                status, headers, body = self.request(route)
                self.assertEqual(status, 200)
                policy = headers['content-security-policy']
                self.assertIn(self.source_hash(self.script), policy)
                self.assertIn(self.source_hash(self.style), policy)
                self.assertNotIn("'unsafe-inline'", policy)
                self.assertNotIn("'unsafe-eval'", policy)
                self.assertIn("connect-src 'none'", policy)
                self.assertEqual(headers['x-content-type-options'], 'nosniff')
                self.assertEqual(headers['x-frame-options'], 'DENY')
                self.assertIn(b'"public_dossier_url":"/?dossier=test"', body)
                self.assertNotIn(b'https://legacy.example.invalid/', body)
                status, head_headers, head_body = self.request(route, 'HEAD')
                self.assertEqual(status, 200)
                self.assertEqual(head_body, b'')
                self.assertEqual(int(head_headers['content-length']), len(body))
                self.assertEqual(head_headers['content-security-policy'], policy)
        _, headers, _ = self.request('/')
        self.assertIn("script-src 'self'", headers['content-security-policy'])
        self.assertNotIn("'unsafe-inline'", headers['content-security-policy'])

    def test_only_complete_fixed_bundles_are_readable_and_never_writable(self) -> None:
        for route in [*(f"/blockchain100/{year}" for year in CAMPAIGN_FILES), "/research/portfolios", "/research/methods"]:
            for extension, mime in (('.json', 'application/json'), ('.csv', 'text/csv')):
                status, headers, _ = self.request(route + extension)
                self.assertEqual(status, 200)
                self.assertTrue(headers['content-type'].startswith(mime))
            for method in ('POST', 'PUT', 'PATCH', 'DELETE'):
                self.assertEqual(self.request(route, method)[0], 405)
        for path in (
            '/blockchain100/private-record.json', '/artifacts/private-record.json',
            '/blockchain100/../private-record.json', '/blockchain100/%2e%2e/private-record.json',
            '/blockchain100/2025/../../private-record.json', '/runtime/bti.sqlite3',
            '/research/portfolios/../private-record.json', '/research/private-record.json',
            '/research/methods/../private-record.json', '/research/methods/../../runtime/bti.sqlite3',
        ):
            with self.subTest(path=path):
                status, _, body = self.request(path)
                self.assertEqual(status, 404)
                self.assertNotIn(b'DO-NOT-PUBLISH', body)
        for files, missing_route in ((CAMPAIGN_FILES[2026], "/blockchain100/2026"), (RESEARCH_FILES, "/research/portfolios"), (METHOD_FILES, "/research/methods")):
            self.stop_server()
            (self.directory / files[2]).unlink()
            self.start_server()
            for extension in ('', '.json', '.csv'):
                self.assertEqual(self.request(missing_route + extension)[0], 404)
            self.assertEqual(self.request('/blockchain100/2025')[0], 200)

    def test_html_and_its_policy_remain_coherent_after_source_edit(self) -> None:
        before = self.request('/blockchain100/2025')
        changed = self.html.replace("'original'", "'new-release'")
        (self.directory / CAMPAIGN_FILES[2025][0]).write_text(changed, encoding='utf-8')
        after = self.request('/blockchain100/2025')
        self.assertEqual(after[0], 200)
        self.assertEqual(after[2], before[2])
        self.assertEqual(after[1]['content-security-policy'], before[1]['content-security-policy'])
        self.assertNotIn(b'new-release', after[2])
        self.stop_server()
        self.start_server()
        _, headers, body = self.request('/blockchain100/2025')
        self.assertIn(b'new-release', body)
        self.assertNotEqual(headers['content-security-policy'], before[1]['content-security-policy'])


if __name__ == '__main__':
    unittest.main()

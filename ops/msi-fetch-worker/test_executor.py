"""Isolated protocol tests: synthetic bodies are fixtures, never article data."""
import base64
import datetime as dt
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.parse

PATH = Path(__file__).with_name('executor.py')
spec = importlib.util.spec_from_file_location('bti_msi_executor', PATH)
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)
KEY = b'unit-test-only-not-a-production-key'
URL = 'https://web.archive.org/web/20250101000000id_/https://www.binance.com/en/square/post/12345678'


def task(url=URL, before=None, expiry=None, task_id='fixture_task_001'):
    now = dt.datetime.now(dt.timezone.utc)
    return w.sign_task({'task_id': task_id, 'not_before': (before or now - dt.timedelta(seconds=1)).isoformat(),
                        'expires_at': (expiry or now + dt.timedelta(seconds=90)).isoformat(),
                        'lease': dict(w.LEASE), 'url': url}, KEY)


class Response(io.BytesIO):
    code = 200
    headers = {'Content-Type': 'text/html', 'Set-Cookie': 'unit-fixture-not-a-cookie', 'Retry-After': '30'}


class Opener:
    def __init__(self, data=b'unit fixture only', error=None):
        self.data, self.error, self.calls = data, error, 0

    def open(self, request, timeout):
        self.calls += 1
        if self.error:
            raise self.error
        return Response(self.data)


class ProtocolTests(unittest.TestCase):
    def test_signed_task(self):
        self.assertEqual(w.validate(task(), KEY), 'archive_snapshot')

    def test_tampered_url(self):
        t = task(); t['url'] += '9'
        with self.assertRaisesRegex(ValueError, 'signature'):
            w.validate(t, KEY)

    def test_wrong_key(self):
        with self.assertRaisesRegex(ValueError, 'signature'):
            w.validate(task(), b'other-test-key')

    def test_unsigned(self):
        t = task(); del t['signature']
        with self.assertRaises(ValueError):
            w.validate(t, KEY)

    def test_expired(self):
        now = dt.datetime.now(dt.timezone.utc)
        with self.assertRaisesRegex(ValueError, 'expired'):
            w.validate(task(before=now-dt.timedelta(seconds=90), expiry=now-dt.timedelta(seconds=1)), KEY)

    def test_not_yet_valid(self):
        with self.assertRaisesRegex(ValueError, 'not_yet_valid'):
            w.validate(task(before=dt.datetime.now(dt.timezone.utc)+dt.timedelta(seconds=10)), KEY)

    def test_long_ttl(self):
        with self.assertRaisesRegex(ValueError, 'lease_ttl'):
            w.validate(task(expiry=dt.datetime.now(dt.timezone.utc)+dt.timedelta(seconds=200)), KEY)

    def test_timezone_required(self):
        t = task(); t['not_before'] = '2025-01-01T00:00:00'; t = w.sign_task(t, KEY)
        with self.assertRaisesRegex(ValueError, 'timezone'):
            w.validate(t, KEY)

    def test_boolean_quota_rejected(self):
        t = task(); t['lease']['quota'] = True; t = w.sign_task(t, KEY)
        with self.assertRaisesRegex(ValueError, 'lease'):
            w.validate(t, KEY)

    def test_extra_fields_rejected(self):
        t = task(); t['headers'] = {'Cookie': 'fixture'}
        with self.assertRaises(ValueError):
            w.validate(t, KEY)

    def test_arbitrary_live_bapi_and_nested_urls_rejected(self):
        for u in ('http://web.archive.org/', 'https://example.com/', 'https://www.binance.com/bapi/a',
                  'https://www.binance.com/en/square/post/12345678',
                  'https://user@web.archive.org/web/x', URL+'?a=1', URL+'#fragment',
                  URL.replace('https://www.binance.com', 'https://evil.invalid'),
                  URL.replace('/post/12345678', '/post/../profile/a'),
                  URL.replace('web.archive.org', 'web.arch\tive.org')):
            with self.subTest(url=u), self.assertRaises(ValueError):
                w.validate(task(u), KEY)

    def test_profile_replay_and_robots(self):
        w.validate(task(URL.replace('/post/12345678', '/profile/cryptomechanic')), KEY)
        self.assertEqual(w.validate(task('https://www.binance.com/robots.txt'), KEY), 'connectivity_probe')

    def test_real_queue_slug_form_preserved_not_repaired(self):
        url = 'https://web.archive.org/web/20250102144547id_/https://www.binance.com/en/square/post/01-02-2025-grayscale-transfers-700-btc-to-unmarked-address-18399556331985'
        t = task(url)
        self.assertEqual(w.validate(t, KEY), 'archive_snapshot')
        self.assertEqual(t['url'], url)
        for prefix in ('a%2F..%2Fbapi', 'a%252Fbapi', 'a%zz'):
            with self.subTest(prefix=prefix), self.assertRaises(ValueError):
                w.validate(task(URL.replace('12345678', prefix+'-12345678')), KEY)

    def test_static_capture_tracking_query_is_preserved_but_redirect_query_rejected(self):
        url = URL+'?utm_source=BinanceNewsRSS&ref=527648310&v=1737041956598'
        t = task(url); self.assertEqual(w.validate(t, KEY), 'archive_snapshot')
        self.assertEqual(t['url'], url)
        with self.assertRaises(ValueError):
            w.validate(task(URL+'?redirect=https%3A%2F%2Fevil.invalid'), KEY)

    def test_cdx_date_partition_resume_and_repeated_filters(self):
        q = urllib.parse.urlencode([('url', 'www.binance.com/en/square/post/*'), ('output', 'json'),
                                   ('filter', 'statuscode:200'), ('filter', 'mimetype:text/html'),
                                   ('from', '20260918'), ('to', '20261018'), ('limit', '500'),
                                   ('showResumeKey', 'true'), ('resumeKey', 'unit fixture opaque key')])
        self.assertEqual(w.validate(task('https://web.archive.org/cdx/search/cdx?'+q), KEY), 'archive_discovery')

    def test_cdx_reject_arbitrary_duplicate_limit_and_bad_dates(self):
        prefix = 'https://web.archive.org/cdx/search/cdx?output=json&url=www.binance.com%2Fen%2Fsquare%2F*'
        for suffix in ('&cmd=x', '&limit=501', '&limit=-1', '&limit=1&limit=2', '&from=20261301',
                       '&url=example.com', '&fl=cookies', '&filter=arbitrary:expression'):
            with self.subTest(query=suffix), self.assertRaises(ValueError):
                w.validate(task(prefix+suffix), KEY)

    def test_oversized_or_unterminated_input(self):
        for raw in (b'x'*(w.MAX_INPUT+1)+b'\n', b'{}'):
            with self.assertRaises(ValueError):
                w.decode_input(raw)

    def test_duplicate_json_keys(self):
        with self.assertRaisesRegex(ValueError, 'duplicate_json_key'):
            w.decode_input(b'{"task_id":"a","task_id":"b"}\n')

    def test_no_redirect(self):
        self.assertIsNone(w.NoRedirect().redirect_request(None, None, 302, 'Found', {}, 'https://example.com'))

    def test_redirect_location_is_returned_for_classification_never_followed(self):
        location = URL.replace('20250101000000', '20250101000001')
        error = urllib.error.HTTPError(URL, 302, 'fixture nearest capture', {'Location': location}, io.BytesIO(b''))
        opener = Opener(error=error)
        with tempfile.TemporaryDirectory() as directory:
            result = w.run(task(), key=KEY, nonce_dir=directory, opener=opener)
        self.assertEqual(result['status'], 302)
        self.assertEqual(result['headers']['location'], location)
        self.assertEqual(opener.calls, 1)

    def test_body_hash_and_header_filter(self):
        with tempfile.TemporaryDirectory() as directory:
            result = w.run(task(), key=KEY, nonce_dir=directory, opener=Opener())
        body = base64.b64decode(result['body_base64'], validate=True)
        self.assertEqual(result['sha256'], hashlib.sha256(body).hexdigest())
        self.assertEqual(result['body_bytes'], len(body))
        self.assertNotIn('set-cookie', result['headers'])
        self.assertEqual(result['headers']['retry-after'], '30')

    def test_oversized_body_rejected(self):
        with tempfile.TemporaryDirectory() as directory, self.assertRaisesRegex(ValueError, 'response_body_limit'):
            w.run(task(), key=KEY, nonce_dir=directory, opener=Opener(b'x'*(w.MAX_BODY+1)))

    def test_replay_never_fetches_twice(self):
        opener = Opener()
        with tempfile.TemporaryDirectory() as directory:
            t = task(); w.run(t, key=KEY, nonce_dir=directory, opener=opener)
            with self.assertRaisesRegex(ValueError, 'task_replayed'):
                w.run(t, key=KEY, nonce_dir=directory, opener=opener)
        self.assertEqual(opener.calls, 1)

    def test_http_429_and_retry_after_returned_without_retry(self):
        error = urllib.error.HTTPError(URL, 429, 'fixture rate limit', {'Retry-After': '90'}, io.BytesIO(b'fixture denial'))
        opener = Opener(error=error)
        with tempfile.TemporaryDirectory() as directory:
            result = w.run(task(), key=KEY, nonce_dir=directory, opener=opener)
        self.assertEqual(result['status'], 429)
        self.assertEqual(result['headers']['retry-after'], '90')
        self.assertEqual(opener.calls, 1)

    @unittest.skipUnless(w.resource is not None and hasattr(os, 'getuid') and os.getuid() != 0, 'requires unprivileged Linux')
    def test_actual_cli_resource_bounds(self):
        result = subprocess.run([sys.executable, str(PATH), '--self-check'], capture_output=True, timeout=8)
        self.assertEqual(result.returncode, 0, result.stderr)
        facts = json.loads(result.stdout)['runtime']
        self.assertNotEqual(facts['uid'], 0)
        self.assertEqual(facts['memory_limit_bytes'], 256*1024*1024)
        self.assertEqual(len(facts['cpu_affinity']), 1)
        self.assertGreaterEqual(facts['nice'], 10)

    @unittest.skipUnless(w.resource is not None and hasattr(os, 'getuid') and os.getuid() != 0, 'requires unprivileged Linux')
    def test_cli_stdin_total_wall_deadline(self):
        started = time.monotonic()
        process = subprocess.Popen([sys.executable, str(PATH)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            process.wait(timeout=w.MAX_WALL_SECONDS+6)
            elapsed = time.monotonic()-started
            self.assertEqual(process.returncode, 2)
            self.assertEqual(json.loads(process.stdout.read())['error'], 'wall_deadline')
            self.assertGreaterEqual(elapsed, w.MAX_WALL_SECONDS-1)
            self.assertLess(elapsed, w.MAX_WALL_SECONDS+6)
        finally:
            if process.poll() is None:
                process.kill(); process.wait()
            process.stdin.close(); process.stdout.close(); process.stderr.close()

    @unittest.skipUnless(w.resource is not None and hasattr(os, 'getuid') and os.getuid() != 0, 'requires unprivileged Linux')
    def test_cli_bounded_oversized_input_without_network(self):
        result = subprocess.run([sys.executable, str(PATH)], input=b'x'*(w.MAX_INPUT+2)+b'\n', capture_output=True, timeout=8)
        self.assertEqual(result.returncode, 2)
        self.assertIn('input_limit', json.loads(result.stdout)['error'])


if __name__ == '__main__':
    unittest.main(verbosity=2)

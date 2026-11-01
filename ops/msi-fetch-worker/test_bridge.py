"""Offline hybrid adapter tests; fixtures never count as collected articles."""
import base64
from concurrent.futures import ThreadPoolExecutor
import gzip
import hashlib
import importlib.util
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from bti.ingest import AccessStopped, FetchCoordinator, FetchResult, PolicyPaused, BodyTooLarge

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('bti_bridge_tested', HERE/'bridge.py')
b = importlib.util.module_from_spec(spec); spec.loader.exec_module(b)
protocol = b._load('executor')
URL = 'https://web.archive.org/web/20250101000000id_/https://www.binance.com/en/square/post/12345678'
KEY = b'0'*32  # Test fixture only.


class FixtureTransport:
    def __init__(self):
        self.calls = 0; self.verifications = 0; self.status = 200; self.body = b'fixture body'
        self.error = None; self.delay = 0; self.counter = None; self.headers = {}

    def verify_installation(self, expected):
        self.verifications += 1
        return {'sha256': expected}

    def execute_signed_task(self, task):
        protocol.validate(task, KEY)
        self.calls += 1
        if self.error:
            raise self.error
        if self.counter:
            self.counter.enter()
        try:
            time.sleep(self.delay)
            return {'status': self.status, 'headers': {'content-type': 'text/html', 'retry-after': '90000'} | self.headers,
                    'body_base64': base64.b64encode(self.body).decode(), 'body_bytes': len(self.body),
                    'sha256': hashlib.sha256(self.body).hexdigest(), 'fetched_at': '2025-01-01T00:00:00Z',
                    'provenance': 'archive_snapshot', 'latency_ms': 1, 'runtime': {'uid': 997}}
        finally:
            if self.counter:
                self.counter.exit()


class Counter:
    def __init__(self):
        self.lock = threading.Lock(); self.active = 0; self.peak = 0; self.starts = []
    def enter(self):
        with self.lock:
            self.active += 1; self.peak = max(self.peak, self.active); self.starts.append(time.monotonic())
    def exit(self):
        with self.lock:
            self.active -= 1


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); (self.root/'runtime').mkdir()
        self.ops = self.root/'ops'; (self.ops/'runtime').mkdir(parents=True)
        (self.ops/'executor.py').write_bytes((HERE/'executor.py').read_bytes())
        (self.ops/'runtime/msi-fetch-lease.key').write_bytes(KEY)
        self.transport = FixtureTransport(); self.local_calls = 0
        self.local_delay = 0; self.counter = None
        for name, value in [('ROOT', self.root), ('HERE', self.ops)]:
            p = patch.object(b, name, value); p.start(); self.addCleanup(p.stop)
        p = patch.object(b, '_load', side_effect=lambda n: protocol if n=='executor' else self.transport)
        p.start(); self.addCleanup(p.stop)

    def local(self, url, *, coordinator, **kwargs):
        coordinator.before_request('web.archive.org', url)
        self.local_calls += 1
        if self.counter:
            self.counter.enter()
        try:
            time.sleep(self.local_delay)
            return FetchResult(url, 200, 'text/html', b'local fixture', 1, '2025-01-01T00:00:00Z')
        finally:
            if self.counter:
                self.counter.exit()

    def make(self, **kwargs):
        return b.HybridArchiveFetcher(self.local, authorization_check=kwargs.pop('authorization_check', lambda u: True), **kwargs)

    def test_msi_real_transport_contract_and_import_counter(self):
        fetcher = self.make(mode='msi'); result = fetcher(URL, coordinator=FetchCoordinator())
        self.assertEqual(result.body, self.transport.body)
        self.assertEqual(result.executor_metadata['node'], 'msi')
        fetcher.record_imported(result, 1)
        self.assertEqual(fetcher.snapshot()['nodes']['msi']['accepted_new_bodies'], 1)
        self.assertEqual(self.transport.calls, 1)
        self.assertEqual(self.local_calls, 0)

    def test_authorization_denied_no_ssh_or_source_call(self):
        fetcher = self.make(authorization_check=lambda u: False)
        with self.assertRaises(PolicyPaused):
            fetcher(URL, coordinator=FetchCoordinator())
        self.assertEqual(self.transport.verifications, 0)
        self.assertEqual(self.transport.calls, 0)
        self.assertEqual(self.local_calls, 0)

    def test_403_trips_shared_coordinator_no_local_fallback(self):
        self.transport.status = 403
        fetcher = self.make(); coordinator = FetchCoordinator()
        with self.assertRaises(AccessStopped) as raised:
            fetcher(URL, coordinator=coordinator)
        self.assertEqual(raised.exception.status_code, 403)
        self.assertEqual(raised.exception.retry_after_seconds, 90000)
        with self.assertRaises(AccessStopped):
            fetcher(URL.replace('12345678', '12345679'), coordinator=coordinator)
        self.assertEqual(self.local_calls, 0)
        self.assertEqual(self.transport.calls, 1)

    def test_redirect_metadata_forwarded_without_follow_or_fallback(self):
        location = URL.replace('20250101000000', '20250101000001')
        self.transport.status = 302; self.transport.headers = {'location': location}
        fetcher = self.make(mode='msi')
        with self.assertRaises(AccessStopped) as raised:
            fetcher(URL, coordinator=FetchCoordinator())
        self.assertEqual(raised.exception.status_code, 302)
        self.assertEqual(raised.exception.redirect_location, location)
        self.assertEqual(self.transport.calls, 1)
        self.assertEqual(self.local_calls, 0)

    def test_unknown_remote_execution_stops_both_nodes(self):
        self.transport.error = RuntimeError('fixture uncertain SSH result')
        fetcher = self.make(); coordinator = FetchCoordinator()
        with self.assertRaises(AccessStopped) as raised:
            fetcher(URL, coordinator=coordinator)
        self.assertTrue(raised.exception.uncertain_execution)
        self.assertIsNone(raised.exception.status_code)
        with self.assertRaises(AccessStopped):
            fetcher(URL.replace('12345678', '12345679'), coordinator=coordinator)
        self.assertEqual(self.local_calls, 0)

    def test_completed_transient_is_backed_off_not_global_banned_and_stays_same_node(self):
        error = RuntimeError('fixture completed SSL EOF')
        error.remote_process_ended = True; error.remote_error_class = 'SSLEOFError'
        error.observed_http_status = 200
        self.transport.error = error; fetcher = self.make(); coordinator = FetchCoordinator()
        with self.assertRaises(RuntimeError) as raised:
            fetcher(URL, coordinator=coordinator)
        self.assertNotIsInstance(raised.exception, AccessStopped)
        self.transport.error = None
        result = fetcher(URL.replace('12345678','12345679'), coordinator=coordinator)
        self.assertEqual(result.executor_metadata['node'], 'desktop')
        later = self.make()
        later(URL.replace('12345678','12345680'), coordinator=FetchCoordinator())
        result = later(URL, coordinator=FetchCoordinator())
        self.assertEqual(result.executor_metadata['node'], 'msi')

    def test_incomplete_403_body_remains_a_real_global_access_hold(self):
        error = RuntimeError('fixture failed reading denied response')
        error.remote_process_ended = True; error.remote_error_class = 'IncompleteRead'
        error.observed_http_status = 403; error.response_retry_after = '90000'
        self.transport.error = error; fetcher = self.make(); coordinator = FetchCoordinator()
        with self.assertRaises(AccessStopped) as raised:
            fetcher(URL, coordinator=coordinator)
        self.assertEqual(raised.exception.status_code,403)
        with self.assertRaises(AccessStopped):
            fetcher(URL.replace('12345678','12345679'),coordinator=coordinator)
        self.assertEqual(self.local_calls,0)

    def test_no_duplicate_dispatch_or_changed_node(self):
        fetcher = self.make(); fetcher(URL, coordinator=FetchCoordinator())
        with self.assertRaisesRegex(ValueError, 'duplicate_dispatch'):
            fetcher(URL, coordinator=FetchCoordinator())
        self.assertEqual(self.transport.calls, 1); self.assertEqual(self.local_calls, 0)

    def test_gzip_expansion_bound(self):
        self.transport.body = gzip.compress(b'x'*(1024*1024+1))
        with self.assertRaises(BodyTooLarge):
            self.make(mode='msi')(URL, coordinator=FetchCoordinator())

    def test_gzip_body_is_expanded_but_wire_hash_preserved(self):
        self.transport.body = gzip.compress(b'fixture compressed body')
        result = self.make(mode='msi')(URL, coordinator=FetchCoordinator())
        self.assertEqual(result.body, b'fixture compressed body')
        self.assertEqual(result.executor_metadata['transport_sha256'], hashlib.sha256(self.transport.body).hexdigest())

    def test_live_route_and_probe_cannot_enter_content_adapter(self):
        for url in ('https://www.binance.com/bapi/a', 'https://www.binance.com/robots.txt'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.make(mode='msi')(url, coordinator=FetchCoordinator())
        self.assertEqual(self.transport.calls, 0)

    def test_two_nodes_overlap_but_keep_one_global_start_spacing(self):
        self.counter = Counter(); self.transport.counter = self.counter
        self.local_delay = self.transport.delay = 1.8
        fetcher = self.make(); coordinator = FetchCoordinator(concurrency=2, minimum_spacing_seconds=1.5)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(fetcher, URL.replace('12345678', str(12345678+i)), coordinator=coordinator) for i in range(2)]
            results = [f.result(timeout=8) for f in futures]
        self.assertEqual({r.executor_metadata['node'] for r in results}, {'msi', 'desktop'})
        self.assertEqual(self.counter.peak, 2)
        self.assertGreaterEqual(sorted(self.counter.starts)[1]-sorted(self.counter.starts)[0], 1.45)
        self.assertEqual(self.transport.verifications, 1)

    def test_invalid_import_count_rejected(self):
        fetcher = self.make(); result = fetcher(URL, coordinator=FetchCoordinator())
        with self.assertRaises(ValueError):
            fetcher.record_imported(result, True)


if __name__ == '__main__':
    unittest.main(verbosity=2)

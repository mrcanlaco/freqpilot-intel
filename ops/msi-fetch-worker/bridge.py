"""Hybrid I/O adapter. Desktop caller retains queue, quotas, cooldowns and DB writes."""
import base64
import copy
import datetime as dt
from email.utils import parsedate_to_datetime
import gzip
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import threading
import time
import uuid

from bti.ingest import AccessStopped, BodyTooLarge, FetchResult, PolicyPaused

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]

def _load(name):
    spec = importlib.util.spec_from_file_location('bti_msi_'+name, HERE/(name+'.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def retry_after_seconds(value):
    if not value:
        return None
    if str(value).isdigit():
        return int(value)
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return max(0, int((parsed-dt.datetime.now(dt.timezone.utc)).total_seconds()))
    except (ValueError, TypeError, OverflowError):
        return None


class HybridArchiveFetcher:
    """One adapter per central batch; bounded concurrent callers, serial imports.

    authorization_check(url) MUST explicitly return True only after checking the
    canonical shared source-policy/cooldown state. Caller supplies one shared
    FetchCoordinator and owns the total request/inflight budgets. This adapter
    never writes SQLite, independently creates jobs, retries or changes nodes on
    failure. The importer calls record_imported(result, genuinely_new_count).
    """
    def __init__(self, local_fetcher, *, authorization_check, mode='hybrid'):
        if mode not in {'hybrid', 'msi', 'desktop'} or not callable(authorization_check):
            raise ValueError('explicit_shared_authorization_required')
        self.local_fetcher, self.authorization_check, self.mode = local_fetcher, authorization_check, mode
        self.protocol, self.transport = _load('executor'), _load('transport')
        self.expected_hash = hashlib.sha256((HERE/'executor.py').read_bytes()).hexdigest()
        self._lock = threading.Lock()
        self._verify_lock = threading.Lock()
        self._log_lock = threading.Lock()
        self._verified = False
        self._started = time.monotonic()
        self._seen = set()
        self._sequence = 0
        self._affinity_path = ROOT/'runtime'/'msi-retry-affinity.json'
        self._affinity = json.loads(self._affinity_path.read_text(encoding='utf-8')) if self._affinity_path.exists() else {}
        if not isinstance(self._affinity, dict) or any(v!='msi' for v in self._affinity.values()):
            raise ValueError('invalid_retry_affinity')
        self._stats = {node: {'dispatch_attempts': 0, 'successful_fetches': 0, 'failed_fetches': 0,
                              'accepted_new_bodies': 0, 'returned_body_bytes': 0} for node in ('desktop', 'msi')}

    def _update(self, node, **values):
        with self._lock:
            for name, number in values.items():
                self._stats[node][name] += number

    def _journal(self, event):
        # No body, signature, secret, auth header or cookie is logged.
        path = ROOT/'runtime'/'msi-fetch-jobs.jsonl'
        with self._log_lock:
            with path.open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(event, separators=(',', ':'))+'\n')
                stream.flush()

    def _verify(self):
        with self._verify_lock:
            if not self._verified:
                self.transport.verify_installation(self.expected_hash)
                self._verified = True

    def _pin_retry(self, url, enabled):
        with self._lock:
            key = hashlib.sha256(url.encode()).hexdigest()
            if enabled:
                self._affinity[key] = 'msi'
            else:
                self._affinity.pop(key, None)
            temporary = self._affinity_path.with_suffix('.json.tmp')
            temporary.write_text(json.dumps(self._affinity), encoding='utf-8')
            temporary.replace(self._affinity_path)

    def __call__(self, url, *, coordinator, allow_cdx=False, max_body_bytes=1024*1024, timeout=30):
        if self.authorization_check(url) is not True:
            raise PolicyPaused('shared_archive_authorization_denied')
        if not 1 <= int(max_body_bytes) <= 1024*1024:
            raise ValueError('app_archive_body_limit')
        with self._lock:
            if url in self._seen:
                raise ValueError('duplicate_dispatch_requires_reconciliation')
            self._seen.add(url)
            node = self.mode if self.mode != 'hybrid' else ('msi' if self._sequence % 2 == 0 else 'desktop')
            if self._affinity.get(hashlib.sha256(url.encode()).hexdigest()) == 'msi':
                node = 'msi'
            self._sequence += 1
        self._update(node, dispatch_attempts=1)
        task_id = 'archive_'+uuid.uuid4().hex
        started = time.monotonic()
        transport_started = False
        try:
            if node == 'desktop':
                result = self.local_fetcher(url, coordinator=coordinator, allow_cdx=allow_cdx,
                                            max_body_bytes=max_body_bytes, timeout=timeout)
                metadata = {'node': node, 'task_id': task_id}
            else:
                # Independently enforce exact static URL scope before network.
                key = (HERE/'runtime'/'msi-fetch-lease.key').read_bytes()
                if len(key) != 32:
                    raise ValueError('invalid_service_key_size')
                now = dt.datetime.now(dt.timezone.utc)
                unsigned = {'task_id': task_id, 'not_before': (now-dt.timedelta(seconds=2)).isoformat(),
                            'expires_at': (now+dt.timedelta(seconds=90)).isoformat(),
                            'lease': dict(self.protocol.LEASE), 'url': url}
                precheck = self.protocol.sign_task(unsigned, key)
                kind = self.protocol.validate(precheck, key)
                if kind == 'connectivity_probe' or (kind == 'archive_discovery' and not allow_cdx):
                    raise ValueError('not_an_authorized_archive_content_task')
                self._verify()
                coordinator.before_request('web.archive.org', url)
                # Recheck after any pacing wait; never sign over an active hold.
                if self.authorization_check(url) is not True:
                    raise PolicyPaused('shared_archive_authorization_denied')
                now = dt.datetime.now(dt.timezone.utc)
                unsigned.update(not_before=(now-dt.timedelta(seconds=2)).isoformat(),
                                expires_at=(now+dt.timedelta(seconds=90)).isoformat())
                signed = self.protocol.sign_task(unsigned, key)
                self._journal({'at': self.protocol.utc(), 'state': 'dispatched', 'node': node,
                               'task_id': task_id, 'url': url, 'executor_sha256': self.expected_hash})
                transport_started = True
                packet = self.transport.execute_signed_task(signed)
                status = packet['status']
                if status != 200:
                    wait = retry_after_seconds(packet['headers'].get('retry-after'))
                    if status in {401, 403, 429}:
                        coordinator.trip(status, wait)
                    error = AccessStopped('MSI archive HTTP '+str(status)+'; no retry or node fallback', status_code=status)
                    error.retry_after_seconds = wait
                    error.redirect_location = packet['headers'].get('location')
                    raise error
                body = base64.b64decode(packet['body_base64'], validate=True)
                if len(body) > max_body_bytes:
                    raise BodyTooLarge('MSI archive body exceeds app bound')
                if body[:2] == b'\x1f\x8b':
                    with gzip.GzipFile(fileobj=io.BytesIO(body)) as stream:
                        body = stream.read(max_body_bytes+1)
                if len(body) > max_body_bytes:
                    raise BodyTooLarge('MSI expanded archive body exceeds app bound')
                result = FetchResult(url=url, status=status,
                    content_type=packet['headers'].get('content-type', 'application/octet-stream').split(';',1)[0],
                    body=body, latency_ms=round((time.monotonic()-started)*1000), fetched_at=packet['fetched_at'])
                metadata = {'node': node, 'task_id': task_id, 'transport_sha256': packet['sha256'],
                            'executor_sha256': self.expected_hash, 'provenance': packet['provenance'],
                            'source_fetch_latency_ms': packet['latency_ms'], 'runtime': packet['runtime']}
            result.executor_metadata = metadata
            if node == 'msi':
                self._pin_retry(url, False)
            self._update(node, successful_fetches=1, returned_body_bytes=len(result.body))
            self._journal({'at': self.protocol.utc(), 'state': 'fetched', 'node': node, 'task_id': task_id,
                           'url': url, 'status': result.status, 'body_sha256': result.response_hash,
                           'body_bytes': len(result.body), 'elapsed_ms': round((time.monotonic()-started)*1000)})
            return result
        except Exception as error:
            self._update(node, failed_fetches=1)
            self._journal({'at': self.protocol.utc(), 'state': 'failed', 'node': node, 'task_id': task_id,
                           'url': url, 'error_class': type(error).__name__, 'error_detail': str(error)[:1200],
                           'status': getattr(error, 'status_code', None)})
            if node == 'msi' and transport_started and getattr(error, 'remote_process_ended', False):
                observed = getattr(error, 'observed_http_status', None)
                if observed is not None and observed != 200:
                    wait = retry_after_seconds(getattr(error, 'response_retry_after', None))
                    coordinator.trip(observed, wait)
                    stopped = AccessStopped('MSI failed while reading actual HTTP '+str(observed), status_code=observed)
                    stopped.retry_after_seconds = wait
                    raise stopped from error
                transient = {'URLError','TimeoutError','SSLError','SSLEOFError','IncompleteRead',
                             'RemoteDisconnected','ConnectionResetError','ConnectionAbortedError'}
                if getattr(error, 'remote_error_class', None) in transient or getattr(error, 'remote_reason_class', None) in transient:
                    # The signed executor returned a completed failure, not an
                    # uncertain SSH execution or observed access denial. No
                    # immediate retry: Core retains its per-candidate backoff;
                    # pin the exact URL to MSI across subsequent batches.
                    self._pin_retry(url, True)
                    raise
            if node == 'msi' and transport_started and not isinstance(error, (AccessStopped, BodyTooLarge)):
                # Unknown execution is not an HTTP status or a reason to switch IP.
                coordinator.trip(None)
                stopped = AccessStopped('MSI transport execution uncertain; reconcile before retry', status_code=None)
                stopped.uncertain_execution = True
                raise stopped from error
            raise

    def record_imported(self, result, count):
        if type(count) is not int or count < 0:
            raise ValueError('new_body_count_must_be_nonnegative_int')
        self._update(result.executor_metadata['node'], accepted_new_bodies=count)

    def snapshot(self):
        with self._lock:
            nodes = copy.deepcopy(self._stats)
        elapsed = max(0.000001, time.monotonic()-self._started)
        return {'mode': self.mode, 'nodes': nodes, 'elapsed_seconds': round(elapsed, 3),
                'accepted_new_bodies_per_elapsed_hour': sum(x['accepted_new_bodies'] for x in nodes.values())*3600/elapsed,
                'automatic_retry': False, 'node_fallback': False, 'sqlite_writer': 'desktop_serial'}

#!/usr/bin/env python3
"""Authenticated one-shot fetch executor; no scheduler, daemon or database."""
import base64
import datetime as dt
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import signal
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
try:
    import resource
except ImportError:  # Allow protocol tests on desktop Windows, not execution.
    resource = None

MAX_INPUT = 16384
MAX_BODY = 4 * 1024 * 1024
MAX_TTL = 120
MAX_WALL_SECONDS = 22
KEY = Path('/var/lib/bti-fetch-worker/lease.key')
NONCES = Path('/var/lib/bti-fetch-worker/nonces')
USER_AGENT = 'BinanceTraderIntelligence/0.1 (+public evidence research; no trading)'
LEASE = {'issuer': 'desktop', 'policy': 'archive-static-v2', 'quota': 1}
FIELDS = ('task_id', 'not_before', 'expires_at', 'lease', 'url')
REPLAY = re.compile(r'^/web/[0-9]{14}id_/https://www\.binance\.com/en/square/(?:post/(?:[A-Za-z0-9_%.-]{1,1024}-)?[0-9]{8,24}|profile/[A-Za-z0-9_-]{1,100})/?$')
CDX_TARGET = re.compile(r'^www\.binance\.com/en/square/(?:\*|post/(?:\*|[0-9]{1,24})|profile/(?:\*|[A-Za-z0-9_-]{1,100}))$')
SAFE_HEADERS = {'content-type', 'content-length', 'last-modified', 'etag', 'date', 'retry-after', 'content-encoding', 'location'}

def utc():
    return dt.datetime.now(dt.timezone.utc).isoformat().replace('+00:00', 'Z')

def canonical(task):
    return json.dumps({k: task[k] for k in FIELDS}, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('utf-8')

def sign_task(task, key):
    result = dict(task)
    result['signature'] = hmac.new(key, canonical(task), hashlib.sha256).hexdigest()
    return result

def parse_date(value):
    if not isinstance(value, str):
        raise ValueError('timestamp_type')
    parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('timestamp_timezone_required')
    return parsed.astimezone(dt.timezone.utc)

def check_cdx(query):
    if not query or len(query) > 4096:
        raise ValueError('unsafe_cdx_query')
    pairs = urllib.parse.parse_qsl(query, keep_blank_values=True, strict_parsing=True, max_num_fields=20)
    allowed = {'url', 'output', 'filter', 'fl', 'from', 'to', 'limit', 'collapse', 'matchType', 'showResumeKey', 'resumeKey'}
    values = {}
    for name, value in pairs:
        if name not in allowed or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError('unsafe_cdx_parameter')
        if name in values and name != 'filter':
            raise ValueError('duplicate_cdx_parameter')
        values.setdefault(name, []).append(value)
    if not CDX_TARGET.fullmatch(values.get('url', [''])[0]) or values.get('output') != ['json']:
        raise ValueError('unsafe_cdx_target')
    if values.get('filter', []) not in ([], ['statuscode:200'], ['mimetype:text/html'], ['statuscode:200', 'mimetype:text/html'], ['mimetype:text/html', 'statuscode:200']):
        raise ValueError('unsafe_cdx_filter')
    for name in ('from', 'to'):
        for value in values.get(name, []):
            formats = {4: '%Y', 6: '%Y%m', 8: '%Y%m%d', 14: '%Y%m%d%H%M%S'}
            if len(value) not in formats or not value.isascii() or not value.isdigit() or not value.startswith('20'):
                raise ValueError('unsafe_cdx_date')
            dt.datetime.strptime(value, formats[len(value)])
    limit = values.get('limit', ['100'])[0]
    if not limit.isascii() or not limit.isdigit() or not 1 <= int(limit) <= 500:
        raise ValueError('unsafe_cdx_limit')
    if 'fl' in values:
        fields = values['fl'][0].split(',')
        if not fields or len(fields) != len(set(fields)) or set(fields) - {'timestamp', 'original', 'statuscode', 'mimetype', 'digest', 'length', 'urlkey'}:
            raise ValueError('unsafe_cdx_fields')
    for name, choices in {'collapse': {'urlkey', 'timestamp:4', 'timestamp:6', 'timestamp:8'}, 'matchType': {'prefix', 'exact'}, 'showResumeKey': {'true', 'false'}}.items():
        if name in values and values[name][0] not in choices:
            raise ValueError('unsafe_cdx_option')
    if len(values.get('resumeKey', [''])[0]) > 2048:
        raise ValueError('unsafe_cdx_resume_key')

def validate(task, key, now=None):
    if not isinstance(task, dict) or set(task) != set(FIELDS) | {'signature'}:
        raise ValueError('invalid_task_fields')
    if not isinstance(task['task_id'], str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,80}', task['task_id']):
        raise ValueError('invalid_task_id')
    if not isinstance(task['lease'], dict) or task['lease'] != LEASE or type(task['lease'].get('quota')) is not int:
        raise ValueError('invalid_lease')
    signature = task['signature']
    if not isinstance(signature, str) or not re.fullmatch(r'[0-9a-f]{64}', signature) or not hmac.compare_digest(signature, hmac.new(key, canonical(task), hashlib.sha256).hexdigest()):
        raise ValueError('invalid_signature')
    current = now or dt.datetime.now(dt.timezone.utc)
    before, expiry = parse_date(task['not_before']), parse_date(task['expires_at'])
    if before > current:
        raise ValueError('not_yet_valid')
    if expiry <= current:
        raise ValueError('expired')
    if not 0 < (expiry - before).total_seconds() <= MAX_TTL:
        raise ValueError('lease_ttl')
    if not isinstance(task['url'], str) or len(task['url']) > 8192 or any(ord(c) <= 32 or ord(c) == 127 for c in task['url']):
        raise ValueError('invalid_url')
    u = urllib.parse.urlsplit(task['url'])
    if u.scheme != 'https' or u.username or u.password or u.port not in (None, 443) or u.fragment:
        raise ValueError('unsafe_url')
    if u.hostname == 'www.binance.com' and u.path == '/robots.txt' and not u.query:
        return 'connectivity_probe'
    if u.hostname == 'web.archive.org' and REPLAY.fullmatch(u.path):
        if u.query:
            pairs = urllib.parse.parse_qsl(u.query, keep_blank_values=True, strict_parsing=True, max_num_fields=16)
            tracking = {'utm_source','utm_medium','utm_campaign','utm_content','utm_term','utm_id','ref','v','_ul'}
            if len(u.query)>4096 or any(k not in tracking or len(v)>2048 or any(ord(c)<32 for c in v) for k,v in pairs):
                raise ValueError('unsafe_archive_original_query')
        token = u.path.rstrip('/').rsplit('/', 1)[-1]
        if re.search(r'%(?![A-Fa-f0-9]{2})', token):
            raise ValueError('invalid_url_escape')
        decoded = urllib.parse.unquote(token, errors='strict')
        if any(c in decoded for c in ('/', '\\', '?', '#', '%')) or '..' in decoded or any(ord(c) <= 32 for c in decoded):
            raise ValueError('unsafe_encoded_replay_path')
        return 'archive_snapshot'
    if u.hostname == 'web.archive.org' and u.path == '/cdx/search/cdx':
        check_cdx(u.query)
        return 'archive_discovery'
    raise ValueError('url_outside_allowlist')

def decode_input(raw):
    if len(raw) > MAX_INPUT or not raw.endswith(b'\n'):
        raise ValueError('input_limit_or_missing_newline')
    def unique(pairs):
        result = {}
        for k, v in pairs:
            if k in result:
                raise ValueError('duplicate_json_key')
            result[k] = v
        return result
    return json.loads(raw, object_pairs_hook=unique)

def load_key():
    stat = KEY.stat()
    if stat.st_uid != os.getuid() or stat.st_mode & 0o077:
        raise ValueError('insecure_key_permissions')
    key = KEY.read_bytes()
    if len(key) != 32:
        raise ValueError('invalid_key_size')
    return key

def claim_task(task, directory):
    # Never issue a second HTTP request for a replayed task ID.
    directory = Path(directory)
    marker = directory / task['task_id']
    try:
        fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise ValueError('task_replayed') from None
    with os.fdopen(fd, 'w') as stream:
        stream.write(task['expires_at'])
    cutoff = time.time() - 600
    for count, path in enumerate(directory.iterdir()):
        if count >= 512:
            break
        if path.is_file() and path.stat().st_mtime < cutoff:
            path.unlink()

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None

def runtime_facts():
    return {'uid': os.getuid() if hasattr(os, 'getuid') else None,
            'memory_limit_bytes': resource.getrlimit(resource.RLIMIT_AS)[0] if resource else None,
            'cpu_affinity': sorted(os.sched_getaffinity(0)) if hasattr(os, 'sched_getaffinity') else None,
            'nice': os.nice(0) if hasattr(os, 'nice') else None,
            'rss_max_kib': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss if resource else None}

def configure():
    if resource is None or not hasattr(os, 'sched_setaffinity') or os.getuid() == 0:
        raise ValueError('linux_unprivileged_runtime_required')
    resource.setrlimit(resource.RLIMIT_AS, (256 * 1024 * 1024,) * 2)
    resource.setrlimit(resource.RLIMIT_CPU, (8, 8))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    os.sched_setaffinity(0, {min(os.sched_getaffinity(0))})
    os.nice(10)
    def deadline(_signal, _frame):
        raise TimeoutError('wall_deadline')
    signal.signal(signal.SIGALRM, deadline)
    signal.setitimer(signal.ITIMER_REAL, MAX_WALL_SECONDS)

def run(task, key=None, nonce_dir=NONCES, opener=None):
    kind = validate(task, key if key is not None else load_key())
    claim_task(task, nonce_dir)
    start, started = time.monotonic(), utc()
    req = urllib.request.Request(task['url'], headers={'User-Agent': USER_AGENT, 'Accept-Encoding': 'identity'})
    opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        response = opener.open(req, timeout=12)
    except urllib.error.HTTPError as error:
        response = error  # Includes redirects,403,429; no follow or retry.
    with response:
        status = response.code
        headers = {k.lower(): str(v)[:512] for k, v in response.headers.items() if k.lower() in SAFE_HEADERS}
        try:
            data = response.read(MAX_BODY + 1)
            if len(data) > MAX_BODY:
                raise ValueError('response_body_limit')
        except Exception as error:
            error.observed_http_status = status
            error.response_retry_after = headers.get('retry-after')
            raise
    return {'task_id': task['task_id'], 'requested_url': task['url'], 'request_started_at': started,
            'fetched_at': utc(), 'provenance': kind, 'tls_verified': True, 'status': status,
            'headers': headers, 'body_base64': base64.b64encode(data).decode('ascii'),
            'body_bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest(),
            'latency_ms': round((time.monotonic() - start) * 1000), 'runtime': runtime_facts()}

def main():
    try:
        configure()
        if sys.argv[1:] == ['--self-check']:
            result = {'status': 'self_check_only', 'runtime': runtime_facts()}
        elif not sys.argv[1:]:
            result = run(decode_input(sys.stdin.buffer.readline(MAX_INPUT + 1)))
        else:
            raise ValueError('unsupported_arguments')
        print(json.dumps(result, separators=(',', ':')))
        return 0
    except Exception as error:
        print(json.dumps({'error': str(error), 'error_class': type(error).__name__,
                          'reason_class': type(getattr(error,'reason',error)).__name__,
                          'observed_http_status': getattr(error,'observed_http_status',None),
                          'retry_after': getattr(error,'response_retry_after',None),
                          'remote_process_ended': True, 'fetched_at': utc()}, separators=(',', ':')))
        return 2
    finally:
        if hasattr(signal, 'setitimer'):
            signal.setitimer(signal.ITIMER_REAL, 0)

if __name__ == '__main__':
    raise SystemExit(main())

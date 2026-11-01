"""Root-side scoped installer template; receives files and NEW service key in SSH stdin."""
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import pwd
import subprocess

PAYLOAD = None  # desktop substitutes this line in memory; never stores a key-bearing script
assert PAYLOAD is not None and os.getuid() == 0
code = Path('/opt/bti-fetch-worker')
state = Path('/var/lib/bti-fetch-worker')
for path in (code, state):
    if path.is_symlink():
        raise RuntimeError('refusing_symlink_install_target')
try:
    user = pwd.getpwnam('bti-fetch')
    if user.pw_dir != str(state) or user.pw_shell not in ('/usr/sbin/nologin', '/sbin/nologin'):
        raise RuntimeError('existing_identity_not_owned_by_this_component')
except KeyError:
    subprocess.run(['useradd', '--system', '--user-group', '--home-dir', str(state), '--no-create-home', '--shell', '/usr/sbin/nologin', 'bti-fetch'], check=True, capture_output=True)
    user = pwd.getpwnam('bti-fetch')
code.mkdir(mode=0o755, parents=True, exist_ok=True)
os.chown(code, 0, 0); os.chmod(code, 0o755)
for path in (state, state/'nonces', state/'tmp'):
    if path.is_symlink():
        raise RuntimeError('refusing_symlink_state_directory')
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chown(path, user.pw_uid, user.pw_gid); os.chmod(path, 0o700)
hashes = {}
for name, item in PAYLOAD['files'].items():
    if name not in {'executor.py', 'test_executor.py'}:
        raise RuntimeError('unexpected_install_file')
    body = base64.b64decode(item['body'], validate=True)
    if hashlib.sha256(body).hexdigest() != item['sha256']:
        raise RuntimeError('input_file_hash_mismatch')
    path = code/name
    if path.is_symlink():
        raise RuntimeError('refusing_symlink_file')
    path.write_bytes(body); os.chown(path, 0, 0); os.chmod(path, 0o644)
    hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
key = base64.b64decode(PAYLOAD['new_purpose_key'], validate=True)
assert len(key) == 32
keypath = state/'lease.key'
if keypath.is_symlink():
    raise RuntimeError('refusing_symlink_key')
if keypath.exists() and not hmac.compare_digest(keypath.read_bytes(), key):
    raise RuntimeError('existing_key_mismatch_no_rotation_performed')
keypath.write_bytes(key); os.chown(keypath, user.pw_uid, user.pw_gid); os.chmod(keypath, 0o600)
env = {'PATH': '/usr/sbin:/usr/bin:/bin', 'LANG': 'C.UTF-8', 'HOME': str(state), 'TMPDIR': str(state/'tmp'), 'PYTHONDONTWRITEBYTECODE': '1'}
base = ['runuser', '-u', 'bti-fetch', '--', 'timeout', '--signal=TERM', '--kill-after=3s', '30s', 'python3']
tests = subprocess.run(base+['-B', str(code/'test_executor.py')], capture_output=True, timeout=38, env=env, cwd=state)
check = subprocess.run(base+['-I', str(code/'executor.py'), '--self-check'], capture_output=True, timeout=12, env=env, cwd=state)
report = {'status': 'installed_tests_passed' if tests.returncode == check.returncode == 0 else 'installed_verification_failed',
          'code_path': str(code), 'state_path': str(state), 'uid': user.pw_uid, 'file_hashes': hashes,
          'key_mode': oct(keypath.stat().st_mode & 0o777), 'key_owner_uid': keypath.stat().st_uid,
          'tests_exit': tests.returncode, 'tests_output': (tests.stdout+tests.stderr).decode('utf-8', 'replace')[-16000:],
          'self_check_exit': check.returncode, 'self_check': json.loads(check.stdout),
          'network_requests_made': 0, 'production_integration': 'not_modified_by_installer'}
reportfile = state/'install-verification.json'; reportfile.write_text(json.dumps(report, indent=2), encoding='utf-8')
os.chown(reportfile, user.pw_uid, user.pw_gid); os.chmod(reportfile, 0o600)
print(json.dumps(report))
raise SystemExit(0 if report['status'] == 'installed_tests_passed' else 1)

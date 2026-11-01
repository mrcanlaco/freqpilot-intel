"""Deploy only the MSI fetch component through existing authenticated SSH."""
import base64
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
from transport import ssh_commands

folder = Path(__file__).resolve().parent
root = folder.parent.parent
keypath = folder/'runtime'/'msi-fetch-lease.key'
keypath.parent.mkdir(parents=True, exist_ok=True)
if not keypath.exists():
    fd = os.open(keypath, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(secrets.token_bytes(32))
if os.name == 'nt':
    who = subprocess.run(['whoami', '/user', '/fo', 'csv', '/nh'], capture_output=True, check=True, text=True)
    sid = list(csv.reader(io.StringIO(who.stdout)))[0][1]
    acl = subprocess.run(['icacls', str(keypath), '/inheritance:r', '/grant:r', '*'+sid+':(F)'], capture_output=True, check=True)
    quoted = str(keypath).replace("'", "''")
    ps = "$a=Get-Acl -LiteralPath '"+quoted+"';[pscustomobject]@{protected=$a.AreAccessRulesProtected;allow_sids=@($a.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier]) | Where-Object {$_.AccessControlType -eq 'Allow'} | ForEach-Object {$_.IdentityReference.Value})}|ConvertTo-Json -Compress"
    check = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', ps], capture_output=True, check=True, text=True)
    readback = json.loads(check.stdout)
    assert readback['protected'] and set(readback['allow_sids']) == {sid}, 'local_key_acl_readback_failed'
else:
    keypath.chmod(0o600)
key = keypath.read_bytes()
assert len(key) == 32, 'invalid_service_key_size'
files = {}
for name in ('executor.py', 'test_executor.py'):
    body = (folder/name).read_bytes()
    files[name] = {'body': base64.b64encode(body).decode(), 'sha256': hashlib.sha256(body).hexdigest()}
payload = base64.b64encode(json.dumps({'files': files, 'new_purpose_key': base64.b64encode(key).decode()}).encode()).decode()
remote = (folder/'install_remote.py').read_text(encoding='utf-8').replace('PAYLOAD = None  # desktop substitutes this line in memory; never stores a key-bearing script', 'PAYLOAD = json.loads(base64.b64decode('+repr(payload)+'))')
ssh, _, inspect = ssh_commands()
result = subprocess.run(ssh+[inspect], input=remote.encode(), capture_output=True, timeout=90)
if result.returncode:
    print(result.stdout.decode('utf-8', 'replace')); print(result.stderr.decode('utf-8', 'replace')[-4000:], file=sys.stderr)
    raise SystemExit(result.returncode)
report = json.loads(result.stdout)
assert report['file_hashes'] == {n: f['sha256'] for n, f in files.items()}
report['local_key_acl_verified'] = True
out = root/'runtime'/'msi-fetch-install-report.json'
out.write_text(json.dumps(report, ensure_ascii=True, indent=2), encoding='utf-8')
print(json.dumps(report, ensure_ascii=True))

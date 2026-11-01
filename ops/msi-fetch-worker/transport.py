"""Fixed SSH transport for already-authorized, signed tasks; never schedules/mints jobs."""
import base64
import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT / 'runtime' / 'remote-executor.json'


def ssh_commands(config_path=None):
    """Load operator routing from ignored local config; never discover/fallback."""
    import re
    path = Path(config_path) if config_path else CONFIG_PATH
    if not path.is_file():
        raise RuntimeError('remote_executor_not_configured')
    config = json.loads(path.read_text(encoding='utf-8'))
    host = config.get('ssh_host')
    distro = config.get('wsl_distribution')
    if not isinstance(host, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.@-]{0,200}', host):
        raise ValueError('invalid_ssh_host')
    if not isinstance(distro, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,80}', distro):
        raise ValueError('invalid_wsl_distribution')
    ssh = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', host]
    prefix = 'wsl.exe -d '+distro+' -u root --cd / --exec '
    execute = prefix+('/usr/sbin/runuser -u bti-fetch -- '
                      '/usr/bin/timeout --signal=TERM --kill-after=3s 30s '
                      '/usr/bin/python3 -I /opt/bti-fetch-worker/executor.py')
    return ssh, execute, prefix+'python3 -'

MAX_STDOUT = 6 * 1024 * 1024

class RemoteTaskError(RuntimeError):
    def __init__(self, packet, artifact):
        super().__init__('remote_task_failed: '+str(packet.get('error'))+'; '+artifact)
        self.remote_process_ended = packet.get('remote_process_ended') is True
        self.remote_error_class = packet.get('error_class')
        self.remote_reason_class = packet.get('reason_class')
        self.observed_http_status = packet.get('observed_http_status')
        self.response_retry_after = packet.get('retry_after')

def _retain_failure(task, result):
    # Private ignored diagnostic, never the HMAC key or signed input packet.
    directory = Path(__file__).resolve().parents[2]/'runtime'/'msi-transport-failures'
    directory.mkdir(exist_ok=True)
    path = directory/(task['task_id']+'.json')
    path.write_text(json.dumps({'task_id':task['task_id'],'url':task['url'],
        'exit':result.returncode,'stdout_base64':base64.b64encode(result.stdout).decode(),
        'stderr':result.stderr.decode('utf-8','replace')[:6000]},indent=2),encoding='utf-8')
    return str(path)


def verify_installation(expected_hash):
    script = """import hashlib,json,pwd,stat\nfrom pathlib import Path\np=Path('/opt/bti-fetch-worker/executor.py');k=Path('/var/lib/bti-fetch-worker/lease.key');u=pwd.getpwnam('bti-fetch')\nprint(json.dumps({'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'code_uid':p.stat().st_uid,'code_mode':oct(stat.S_IMODE(p.stat().st_mode)),'key_uid':k.stat().st_uid,'key_mode':oct(stat.S_IMODE(k.stat().st_mode)),'worker_uid':u.pw_uid}))\n"""
    ssh, _, inspect = ssh_commands()
    r = subprocess.run(ssh+[inspect], input=script.encode(), capture_output=True, timeout=20)
    if r.returncode:
        raise RuntimeError('remote_readback_failed: '+r.stderr.decode('utf-8', 'replace')[:500])
    data = json.loads(r.stdout)
    if data['sha256'] != expected_hash or data['code_uid'] != 0 or data['code_mode'] != '0o644' or data['key_mode'] != '0o600' or data['key_uid'] != data['worker_uid'] or data['worker_uid'] == 0:
        raise RuntimeError('remote_installation_verification_failed')
    return data


def execute_signed_task(task):
    """Caller MUST grant shared policy/quota and reserve ONE node before signing.

    There is deliberately no retry/fallback to another node here. HTTP403/429/
    Retry-After must trip the caller's shared persisted host cooldown. A transport
    error is uncertain execution, not permission to resubmit elsewhere.
    """
    payload = json.dumps(task, separators=(',', ':')).encode()+b'\n'
    if len(payload) > 16384:
        raise ValueError('task_input_limit')
    ssh, command, _ = ssh_commands()
    result = subprocess.run(ssh+[command], input=payload, capture_output=True, timeout=40)
    if len(result.stdout) > MAX_STDOUT:
        raise ValueError('remote_output_limit')
    try:
        packet = json.loads(result.stdout)
    except ValueError:
        artifact = _retain_failure(task, result)
        raise RuntimeError('remote_protocol_error_exit_'+str(result.returncode)+'; '+artifact) from None
    if result.returncode or 'error' in packet:
        artifact = _retain_failure(task, result)
        if result.returncode == 2 and packet.get('remote_process_ended') is True:
            raise RemoteTaskError(packet, artifact)
        raise RuntimeError('remote_task_failed: '+str(packet.get('error', result.returncode))+'; '+artifact)
    if packet.get('task_id') != task['task_id'] or packet.get('requested_url') != task['url'] or packet.get('tls_verified') is not True:
        raise ValueError('remote_response_binding')
    if type(packet.get('status')) is not int or not 100 <= packet['status'] <= 599:
        raise ValueError('remote_http_status')
    body = base64.b64decode(packet['body_base64'], validate=True)
    if len(body) > 4*1024*1024 or len(body) != packet['body_bytes'] or hashlib.sha256(body).hexdigest() != packet['sha256']:
        raise ValueError('remote_body_integrity')
    facts = packet['runtime']
    if not facts['uid'] or facts['memory_limit_bytes'] != 256*1024*1024 or len(facts['cpu_affinity']) != 1 or facts['nice'] < 10:
        raise ValueError('remote_runtime_bounds')
    return packet

"""Offline disposable isolation proof, NOT a coder launcher or Stage C enablement."""
import argparse
import json
import re
import subprocess
import tempfile
import uuid
from pathlib import Path

REQUIRED = ("non_root", "no_capabilities", "no_new_privileges", "workspace_write",
            "root_readonly", "no_control_credentials", "no_sensitive_mounts", "no_egress")


def validate_image(image):
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", image):
        raise ValueError("Use an audited local immutable image ID; no pull or mutable tag")


def verify(report):
    if (not isinstance(report, dict) or set(report) != set(REQUIRED)
            or any(report[key] is not True for key in REQUIRED)):
        raise ValueError("Sandbox proof incomplete or failed")


def verify_profile(container, workspace):
    host = container.get('HostConfig', {})
    expected = {'NetworkMode': 'none', 'ReadonlyRootfs': True, 'Privileged': False,
                'CapDrop': ['ALL'], 'CapAdd': None, 'SecurityOpt': ['no-new-privileges:true'],
                'Memory': 134217728, 'NanoCpus': 250000000, 'PidsLimit': 32,
                'PidMode': '', 'IpcMode': 'private'}
    mounts = container.get('Mounts', [])
    if (container.get('Config', {}).get('User') != '10001:10001'
            or any(type(host.get(k)) is not type(v) or host.get(k) != v for k, v in expected.items())
            or len(mounts) != 1
            or any(mounts[0].get(k) != v for k, v in {
                'Type': 'bind', 'Source': workspace, 'Destination': '/workspace', 'RW': True}.items())):
        raise ValueError('Docker runtime profile differs from the bounded offline fixture')


PROBE = r'''
import json, os, socket
from pathlib import Path
status = dict(line.split(':', 1) for line in Path('/proc/self/status').read_text().splitlines() if ':' in line)
def denied(path):
    try:
        Path(path).write_text('disposable probe')
    except OSError:
        return True
    return False
path = Path('/workspace/proof.txt')
path.write_text('workspace only')
forbidden_env = {'DATABASE_URL','GITHUB_TOKEN','GH_TOKEN','CLOUDFLARE_API_TOKEN',
                 'TELEGRAM_BOT_TOKEN','OPENAI_API_KEY','ANTHROPIC_API_KEY','CODEX_API_KEY',
                 'CLAUDE_CODE_OAUTH_TOKEN'}
sock = socket.socket(); sock.settimeout(1)
try:
    sock.connect(('192.0.2.1', 443))
    no_egress = False
except OSError:
    no_egress = True
finally:
    sock.close()
print(json.dumps({
    'non_root': os.getuid() == 10001,
    'no_capabilities': int(status['CapEff'].strip(),16) == 0,
    'no_new_privileges': status['NoNewPrivs'].strip() == '1',
    'workspace_write': path.read_text() == 'workspace only',
    'root_readonly': bool(os.statvfs('/').f_flag & os.ST_RDONLY) and denied('/sandbox-root-probe') and denied('/app/sandbox-probe'),
    'no_control_credentials': not forbidden_env.intersection(os.environ),
    'no_sensitive_mounts': not any(os.access(p, os.R_OK) for p in (
        '/var/run/docker.sock','/run/docker.sock','/root/.ssh','/opt/company-os',
        '/workspace/.git','/workspace/.env','/workspace/host-canary')),
    'no_egress': no_egress,
}))
'''


def run(image, *, timeout_probe=False):
    validate_image(image)
    # Image must already be local. Never download or inherit a Compose environment.
    inspected = subprocess.run(['docker', 'image', 'inspect', image], check=True,
                               capture_output=True, text=True, timeout=10)
    config = json.loads(inspected.stdout)[0]['Config']
    # An image-declared mount could silently widen the writable filesystem.
    if config.get('Volumes'):
        raise ValueError('Probe image must not declare additional volumes')
    name = 'company-coder-proof-' + uuid.uuid4().hex
    with tempfile.TemporaryDirectory(prefix='company-coder-proof-') as fixture:
        root = Path(fixture)
        (root / 'host-canary').write_text('synthetic host fixture, not a secret')
        workspace = root / 'workspace'
        workspace.mkdir(mode=0o777)
        workspace.chmod(0o777)  # only this empty disposable fixture, never a repo
        command = ['docker', 'run', '--name', name, '--pull=never', '--network=none',
                   '--read-only', '--user=10001:10001', '--cap-drop=ALL',
                   '--security-opt=no-new-privileges:true', '--memory=128m',
                   '--cpus=.25', '--pids-limit=32', '--workdir=/workspace',
                   '--mount', f'type=bind,source={workspace},target=/workspace',
                   '--entrypoint=python', image, '-c',
                   'import time; time.sleep(60)' if timeout_probe else PROBE]
        try:
            result = subprocess.run(command, capture_output=True, text=True,
                                    timeout=2 if timeout_probe else 20)
            if timeout_probe:
                raise ValueError('Timeout probe unexpectedly completed')
            if result.returncode:
                raise ValueError('Container proof failed; raw container output suppressed')
            report = json.loads(result.stdout)
            verify(report)
        except subprocess.TimeoutExpired:
            if not timeout_probe:
                raise
            report = {'controller_timeout_enforced': True}
        finally:
            try:
                inspected = subprocess.run(['docker', 'container', 'inspect', name], check=True,
                                           capture_output=True, text=True, timeout=10)
                container = json.loads(inspected.stdout)[0]
                verify_profile(container, str(workspace))
                if timeout_probe:
                    if container.get('State', {}).get('Running') is not True:
                        raise ValueError('Timeout fixture was not running; timeout evidence is incomplete')
                    report['fixture_running_at_timeout'] = True
            finally:
                # UUID-named fixture only; never a running production container.
                subprocess.run(['docker', 'rm', '-f', name], check=True,
                               capture_output=True, timeout=10)
        removed = subprocess.run(['docker', 'container', 'ls', '-aq', '--filter', f'name=^{name}$'],
                                 check=True, capture_output=True, text=True, timeout=10)
        if removed.stdout.strip():
            raise ValueError('Probe cleanup failed')
        report['fixture_removed'] = True
        report['runtime_profile_verified'] = True
        return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True)
    args = parser.parse_args()
    print(json.dumps({'isolation': run(args.image),
                      'timeout': run(args.image, timeout_probe=True),
                      'stage_c_enabled': False}, sort_keys=True))

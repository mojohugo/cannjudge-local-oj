"""Reconnect using an EVS-pinned signing identity, never blind host-key trust."""
import base64
import hashlib
import json
import os
from pathlib import Path
import secrets
import shlex
import subprocess
import tempfile
import threading
import time

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


class SSHUnavailable(RuntimeError):
    pass


class SSHIdentityError(RuntimeError):
    pass


def state_paths(settings):
    tag = hashlib.sha256(settings.ssh_host.encode()).hexdigest()[:20]
    root = settings.state_dir/'ssh-hosts'/tag
    return root, root/'identity.json', root/'known_hosts'


def verify_proof(proof, pin, nonce, observed_key, remote_root):
    try:
        payload = proof['payload']
        raw = json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()
        public = base64.b64decode(pin['public_key'], validate=True)
        Ed25519PublicKey.from_public_bytes(public).verify(base64.b64decode(proof['signature'], validate=True), raw)
        if (payload['version'] != 1 or payload['nonce'] != nonce or payload['root'] != remote_root
                or pin['root'] != remote_root or list(observed_key) not in payload['host_keys']
                or payload['server_id'] != hashlib.sha256(public).hexdigest()
                or not isinstance(payload['boot_id'], str) or not payload['boot_id']):
            raise ValueError('Challenge, endpoint key or persistent worker root does not match')
        return payload
    except (InvalidSignature, ValueError, KeyError, TypeError) as error:
        raise SSHIdentityError('服务器身份验证失败；未更新 SSH 指纹，也未发送评测源码') from error


def known_hosts_options(path):
    path = str(path).replace('\\', '/')
    return ['-o', f'UserKnownHostsFile="{path}"', '-o', f'GlobalKnownHostsFile="{os.devnull}"',
            '-o', 'CheckHostIP=no']


class SSHConnector:
    def __init__(self, settings):
        self.settings = settings
        self.root, self.pin_path, self.known_hosts = state_paths(settings)
        self.status = dict(state='idle', message='提交时自动连接并验证服务器身份')
        self.probe_lock = threading.Lock()

    def command(self, remote_command):
        args = ['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
                '-o', 'ConnectTimeout=10', '-o', 'ServerAliveInterval=10', '-o', 'ServerAliveCountMax=2']
        if self.pin_path.exists():
            args += known_hosts_options(self.known_hosts)
        return args + [self.settings.ssh_host, remote_command]

    def probe(self):
        if not self.pin_path.exists():
            # Legacy backends keep normal OpenSSH verification; no automatic rekey.
            result = subprocess.run(self.command('echo cannjudge-ready'), capture_output=True, timeout=15)
            if result.returncode:
                error = result.stderr.decode('utf-8', errors='replace')[-1500:]
                if 'HOST IDENTIFICATION HAS CHANGED' in error or 'Host key verification failed' in error:
                    raise SSHIdentityError('此后端未登记持久身份，SSH 指纹变化需要先核实并登记')
                raise SSHUnavailable(error)
            return dict(verified_identity=False, boot_id=None, rekeyed=False)
        self.root.mkdir(parents=True, exist_ok=True)
        pin = json.loads(self.pin_path.read_text(encoding='utf-8'))
        nonce = secrets.token_hex(32)
        command = 'python3 ' + shlex.quote(self.settings.remote_root+'/identity.py') + ' ' + nonce
        # The quarantine connection sends only a nonce, never source or secrets.
        # A fresh key is promoted only after a signature binds it to our EVS pin.
        with tempfile.TemporaryDirectory(prefix='probe-', dir=self.root) as temporary:
            candidate = Path(temporary)/'known_hosts'
            args = ['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=accept-new',
                    '-o', 'HashKnownHosts=no', '-o', 'ConnectTimeout=10']
            args += known_hosts_options(candidate) + [self.settings.ssh_host, command]
            try:
                result = subprocess.run(args, capture_output=True, timeout=20)
            except subprocess.TimeoutExpired as error:
                raise SSHUnavailable('SSH 连接或身份检查超时') from error
            if result.returncode:
                raise SSHUnavailable(result.stderr.decode('utf-8', errors='replace')[-1500:])
            if len(result.stdout) > 32768 or not candidate.exists():
                raise SSHIdentityError('身份响应异常，拒绝更新 SSH 指纹')
            lines = [line for line in candidate.read_text().splitlines() if line and not line.startswith('#')]
            if len(lines) != 1 or len(lines[0].split()) < 3:
                raise SSHIdentityError('SSH 临时指纹记录异常')
            _, kind, value, *_ = lines[0].split()
            try:
                proof = json.loads(result.stdout)
            except (ValueError, UnicodeError) as error:
                raise SSHIdentityError('服务器没有返回有效身份签名') from error
            payload = verify_proof(proof, pin, nonce, (kind, value), self.settings.remote_root)
            new = candidate.read_bytes()
            old = self.known_hosts.read_bytes() if self.known_hosts.exists() else b''
            if old != new:
                if old:
                    (self.root/'known_hosts.previous').write_bytes(old)
                pending = self.known_hosts.with_suffix('.tmp')
                pending.write_bytes(new)
                pending.replace(self.known_hosts)
            fingerprint = 'SHA256:' + base64.b64encode(hashlib.sha256(base64.b64decode(value)).digest()).decode().rstrip('=')
            return dict(verified_identity=True, boot_id=payload['boot_id'], server_id=payload['server_id'],
                        host_fingerprint=fingerprint, rekeyed=bool(old and old != new))

    def ensure_ready(self, update, stop_event):
        started = time.monotonic()
        timeout = self.settings.ssh_reconnect_seconds
        attempts = 0
        while not stop_event.is_set():
            attempts += 1
            try:
                # Parallel preparations share the pinned known_hosts file.
                # Serialize only identity checks/promotions, never whole jobs.
                with self.probe_lock:
                    result = self.probe()
                self.status = dict(state='connected', attempts=attempts, connected_at=time.time(), **result)
                update('SSH 已连接，服务器身份验证通过' if result['verified_identity'] else 'SSH 已连接', self.status)
                return self.status.copy()
            except (SSHUnavailable, subprocess.TimeoutExpired) as error:
                remaining = timeout - (time.monotonic()-started)
                if remaining <= 0:
                    self.status = dict(state='unavailable', attempts=attempts, message=str(error)[:1000])
                    raise SSHUnavailable(f'等待服务器恢复超过 {timeout} 秒：{str(error)[:1000]}') from error
                delay = min(2**min(attempts, 5), 30, remaining)
                self.status = dict(state='reconnecting', attempts=attempts, retry_in_seconds=delay,
                                   message=str(error)[:1000])
                update(f'服务器暂不可达，{int(delay)} 秒后自动重连（第 {attempts} 次）', self.status)
                if stop_event.wait(delay):
                    break
            except SSHIdentityError as error:
                self.status = dict(state='identity_error', message=str(error))
                raise
        raise SSHUnavailable('服务正在停止，已结束重连等待')

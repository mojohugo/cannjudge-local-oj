from dataclasses import dataclass, field
from pathlib import Path
import hashlib
import json
import os
import secrets

ROOT = Path(__file__).resolve().parent.parent


def password_hash(password, salt):
    return hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), 310000).hex()


@dataclass
class Settings:
    state_dir: Path = ROOT / 'var'
    ssh_host: str = 'npu-worker'
    remote_root: str = '/opt/cannjudge-local-oj'
    allowed_hosts: list = field(default_factory=lambda: ['127.0.0.1', 'localhost', '[::1]'])
    secure_cookies: bool = False
    public_origin: str | None = None
    local_origin: str = 'http://127.0.0.1:8765'
    queue_limit: int = 10
    max_source_bytes: int = 2 * 1024 * 1024
    session_seconds: int = 43200
    # Preserve legacy configs that omitted these switches. A fresh install
    # explicitly writes False below, so it never launches external work.
    worker_enabled: bool = True
    official_submit_enabled: bool = False
    official_auto_sync_enabled: bool = True
    official_auto_sync_seconds: int = 60
    evaluation_workers: int = 2
    ssh_reconnect_seconds: int = 300
    ssh_job_attempts: int = 3
    password_salt: str = ''
    password_digest: str = ''
    api_token_digest: str = ''


def load_settings():
    directory = Path(os.environ.get('OJ_STATE_DIR', ROOT / 'var')).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    config = directory / 'config.json'
    if not config.exists():
        password = secrets.token_urlsafe(24)
        api_token = secrets.token_urlsafe(36)
        salt = secrets.token_hex(16)
        values = dict(ssh_host=Settings.ssh_host, remote_root=Settings.remote_root,
                      allowed_hosts=['127.0.0.1', 'localhost', '[::1]'], secure_cookies=False,
                      worker_enabled=False, official_submit_enabled=False, official_auto_sync_enabled=False,
                      password_salt=salt, password_digest=password_hash(password, salt),
                      api_token_digest=hashlib.sha256(api_token.encode()).hexdigest())
        config.write_text(json.dumps(values, indent=2) + '\n', encoding='utf-8')
        (directory / 'admin-access.txt').write_text(
            'CannJudge Local OJ — private credentials; do not share this file.\n'
            'URL: http://127.0.0.1:8765\n'
            'Login password: ' + password + '\nAPI bearer token: ' + api_token + '\n', encoding='utf-8')
        for path in (config, directory / 'admin-access.txt'):
            path.chmod(0o600)
    values = json.loads(config.read_text(encoding='utf-8'))
    if os.environ.get('OJ_LOCAL_ORIGIN'):
        values['local_origin'] = os.environ['OJ_LOCAL_ORIGIN']
    return Settings(state_dir=directory, **values)

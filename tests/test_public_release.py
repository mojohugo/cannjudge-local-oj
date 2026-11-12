import hashlib
import json
from pathlib import Path
import re

from oj.config import load_settings, password_hash
from scripts.audit_public import issues_for

ROOT = Path(__file__).resolve().parents[1]


def test_fresh_install_generates_credentials_without_starting_workers(tmp_path, monkeypatch):
    monkeypatch.setenv('OJ_STATE_DIR', str(tmp_path))
    settings = load_settings()
    assert not settings.worker_enabled
    assert not settings.official_submit_enabled
    assert not settings.official_auto_sync_enabled
    assert settings.ssh_host == 'npu-worker'
    lines = (tmp_path / 'admin-access.txt').read_text(encoding='utf-8').splitlines()
    password = next(line.split(': ', 1)[1] for line in lines if line.startswith('Login password:'))
    token = next(line.split(': ', 1)[1] for line in lines if line.startswith('API bearer token:'))
    assert password_hash(password, settings.password_salt) == settings.password_digest
    assert hashlib.sha256(token.encode()).hexdigest() == settings.api_token_digest
    assert load_settings().password_digest == settings.password_digest
    values = json.loads((tmp_path / 'config.json').read_text())
    values.update(worker_enabled=True, official_auto_sync_enabled=True, ssh_host='existing-worker')
    (tmp_path / 'config.json').write_text(json.dumps(values))
    existing = load_settings()
    assert existing.worker_enabled and existing.official_auto_sync_enabled
    assert existing.ssh_host == 'existing-worker'


def test_active_html_assets_exist_in_a_clean_tree():
    for name in ('index.html', 'official.html', 'consent.html'):
        for asset in re.findall(r'(?:src|href)="/static/([^"?]+)', (ROOT/'web'/name).read_text(encoding='utf-8')):
            assert (ROOT/'web'/asset).is_file(), asset
            assert '/releases/' not in '/'+asset


def test_release_audit_flags_private_data_without_echoing_values():
    secret = b'synthetic-private-credential'
    assert issues_for('var/config.json', b'{}')
    assert issues_for('config/backends.json', b'{}')
    assert issues_for('kernel.pem', b'placeholder')
    assert issues_for('problems/test/data/input.bin', b'1234')
    assert issues_for('skills/private/SKILL.md', b'private notes')
    assert issues_for('README.md', secret, [secret]) == ['value from private deployment state']
    assert issues_for('config/backends.example.json', b'{"ssh_host":"npu-worker"}') == []
    assert issues_for('deploy/env.sh.example', b'export ASCEND_RT_VISIBLE_DEVICES=0') == []

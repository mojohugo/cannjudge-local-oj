import hashlib
from pathlib import Path

from fastapi.testclient import TestClient

from oj.app import create_app
from oj.config import Settings
from scripts.audit_public import issues_for


def test_empty_install_can_start_and_list_without_private_packs(tmp_path):
    key = 'synthetic-test-key'
    settings = Settings(state_dir=tmp_path, worker_enabled=False,
                        official_submit_enabled=False, official_auto_sync_enabled=False,
                        api_token_digest=hashlib.sha256(key.encode()).hexdigest(),
                        allowed_hosts=['testserver'])
    with TestClient(create_app(settings)) as client:
        assert client.get('/api/health').status_code == 200
        result = client.get('/api/problems', headers={'Authorization': 'Bearer ' + key})
        assert result.status_code == 200
        assert result.json()['problems'] == []
        assert result.json()['optimization_skill']['available'] is False


def test_no_private_data_is_distributed():
    root = Path(__file__).resolve().parents[1]
    for name in ('problems', 'knowledge', 'skills', 'official_problems', 'official_templates'):
        assert not (root/name).exists()
        assert issues_for(name + '/example.json', b'{}')
    assert issues_for('config/official_cases.json', b'{"problems":{"private":{}}}')
    assert issues_for('config/official_cases.json', b'{"problems":{}}') == []
    assert issues_for('config/contest_scoring.json', b'{"problems":{"private":{}}}')

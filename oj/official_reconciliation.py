"""Receipt-bound, archive-only recovery. Never enqueue, fetch, or submit anything."""
from collections import Counter
from datetime import datetime
import hashlib
import json
import re
import time
from urllib.parse import urlsplit

from .official_accounts import account_filter

_TERMINAL = re.compile(r'^(pass|accepted|wrong\s*answer|compile\s*error|compilation\s*error|runtime\s*error|time\s*limit\s*exceeded|memory\s*limit\s*exceeded|output\s*limit\s*exceeded|presentation\s*error|system\s*error|internal\s*error|illegal|invalid|rejected|fail|failed)$', re.I)


def _timestamp(value):
    stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if stamp.tzinfo is None:
        raise ValueError('Timestamp must have timezone')
    return stamp.timestamp()


def _evidence(judge, row, db):
    """Fail closed on missing/ambiguous evidence; source hashes alone never link jobs."""
    current = json.loads(row['result'])
    owner = current.get('account_id', 'default')
    directory = judge.directory / row['id']
    try:
        with (directory / 'accepted.json').open('rb') as stream:
            raw_receipt = stream.read(8193)
        if len(raw_receipt) > 8192:
            return 'evidence_unreadable', None
    except FileNotFoundError:
        return 'receipt_missing', None
    receipt = json.loads(raw_receipt)
    url = receipt['url']
    base = current['problem_url']
    parsed = urlsplit(base)
    if (parsed.scheme != 'https' or parsed.netloc != 'cannjudge.cn' or parsed.query or parsed.fragment
            or not isinstance(url, str) or not url.startswith(base + '/submission/')):
        return 'receipt_target_mismatch', None
    identity = url[len(base + '/submission/'):]
    if (not re.fullmatch(r'[a-f0-9]{24}', identity) or
            receipt.get('official_id', identity) != identity or
            current.get('official_id') not in (None, identity)):
        return 'receipt_identity_mismatch', None
    accepted = _timestamp(receipt['acceptedAt'])
    if not row['created'] <= accepted <= row['updated']:
        return 'receipt_time_mismatch', None
    # An official acceptance may not be assigned to two local requests, even
    # when their sources are identical or a receipt was copied accidentally.
    if db.execute("SELECT 1 FROM official_submissions WHERE id<>? AND json_extract(result,'$.official_id')=?",
                  (row['id'], identity)).fetchone():
        return 'official_id_already_linked', None
    for other in db.execute("SELECT id FROM official_submissions WHERE status='outcome_unknown' AND id<>?", (row['id'],)):
        try:
            with (judge.directory / other['id'] / 'accepted.json').open('rb') as stream:
                other_receipt = json.loads(stream.read(8193))
            if isinstance(other_receipt, dict) and other_receipt.get('url') == url:
                return 'ambiguous_receipt', None
        except (OSError, ValueError):
            pass
    stored = db.execute('SELECT record FROM official_history WHERE official_id=?', (identity,)).fetchone()
    if stored is None:
        return 'archive_missing', None
    record = json.loads(stored['record'])
    if (record.get('account_id', 'default') != owner or record.get('problem_id') != row['problem_id'] or
            record.get('data_origin') != 'official_cannjudge' or record.get('official_id') != identity or
            record.get('official_url') != url):
        return 'archive_identity_mismatch', None
    if not (record.get('evaluation_complete') is True and
            _TERMINAL.fullmatch(str(record.get('official_status', '')))):
        return 'archive_not_terminal', None
    if record.get('source_available') is not True or record.get('source_sha256') != row['source_sha256']:
        return 'archive_source_mismatch', None
    for key in ('score', 'cases', 'official_server_score', 'official_formula_score', 'official_score_snapshot'):
        if current.get(key) is not None and current[key] != record.get(key):
            return 'existing_result_conflict', None
    # Verify bytes from both independent archives, not just stored hash claims.
    archive = judge.settings.state_dir / 'official-history'
    if owner != 'default':
        archive = archive / 'accounts' / owner
    files = json.loads((archive / identity / 'files.json').read_text(encoding='utf-8'))
    if not isinstance(files, list) or len({f['path'] for f in files}) != len(files):
        return 'archive_files_invalid', None
    contents = {f['path']: f['content'] for f in files}
    if current.get('submission_kind', 'kernel') == 'project':
        from .official_targets import normalize_project
        local = json.loads((directory / 'project.json').read_text(encoding='utf-8'))
        required = current['editable_files']
        official = [dict(path=p, content=contents[p]) for p in required]
        local_hash = normalize_project(row['problem_id'], local, judge.settings.max_source_bytes)[1]
        official_hash = normalize_project(row['problem_id'], official, judge.settings.max_source_bytes)[1]
    else:
        local_hash = hashlib.sha256((directory / 'kernel.asc').read_bytes()).hexdigest()
        official_hash = hashlib.sha256(contents['kernel.asc'].encode('utf-8')).hexdigest()
    if local_hash != row['source_sha256'] or official_hash != row['source_sha256']:
        return 'source_bytes_mismatch', None
    return 'ready', dict(record=record, receipt_sha256=hashlib.sha256(raw_receipt).hexdigest(),
                         archive_sha256=hashlib.sha256(stored['record'].encode()).hexdigest(),
                         accepted_at=receipt['acceptedAt'])


def _checked_evidence(judge, row, db):
    try:
        return _evidence(judge, row, db)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
        return 'evidence_unreadable', None


def reconcile_unknown(judge, account_id, *, apply=True):
    """Only unknown -> completed, atomically rechecked; all other records untouched.

    Dry runs and unresolved checks acquire no writer lock. No history writes,
    score computation, browser calls, worker jobs or network retries occur here.
    """
    owner = judge.accounts.get(account_id)['id']
    with judge.store.db() as db:
        rows = db.execute("SELECT * FROM official_submissions WHERE status='outcome_unknown' AND " +
                          account_filter('result') + ' ORDER BY created,id', (owner,)).fetchall()
    outcomes = []
    for row in rows:
        with judge.store.db() as db:
            reason, evidence = _checked_evidence(judge, row, db)
        if reason == 'ready' and apply:
            with judge.store.db() as db:
                db.execute('BEGIN IMMEDIATE')
                fresh = db.execute('SELECT * FROM official_submissions WHERE id=?', (row['id'],)).fetchone()
                if fresh is None or dict(fresh) != dict(row):
                    reason = 'job_changed'
                else:
                    reason, evidence = _checked_evidence(judge, fresh, db)
                    if reason == 'ready':
                        record = evidence['record']
                        # Keep request metadata and the exact historical score evidence.
                        # Never feed this saved record back through archive.ingest().
                        fields = {key: record[key] for key in (
                            'official_id', 'official_url', 'official_display_id', 'official_status',
                            'passed', 'valid', 'evaluation_complete', 'passed_cases', 'total_cases',
                            'cases', 'score', 'score_source', 'score_note', 'score_formula',
                            'official_server_score', 'official_formula_score', 'official_score_snapshot',
                            'fetched_at', 'reference_fetched_at', 'reference_snapshot_id', 'ranking_status',
                            'files', 'source_available') if key in record}
                        fields.update(record=record, source_verified=True, source_status='verified',
                                      archived_source_sha256=record['source_sha256'], archive_error=None,
                                      message='已通过原接收凭据和同账户源码归档核实官方结果；未重新提交',
                                      reconciliation=dict(kind='saved_acceptance_and_archive_v1', at=time.time(),
                                          previous_status=row['status'], previous_message=json.loads(row['result']).get('message'),
                                          **{k: v for k, v in evidence.items() if k != 'record'}))
                        judge.update(row['id'], 'completed', db=db, **fields)
                        reason = 'reconciled'
        outcomes.append(dict(id=row['id'], reason=reason))
    return dict(account_id=owner, checked=len(rows), counts=dict(Counter(x['reason'] for x in outcomes)), jobs=outcomes)

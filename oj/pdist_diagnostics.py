"""Read-only auxiliary Pdist view. No score inputs, network, or NPU work."""
import copy
import hashlib
import json
import re

from remote import pdist_sync
from remote.pdist_host import BOUNDARIES, PROTOCOL, SIDECAR, digest, finite, summarize_samples, unavailable


def official_same_source(store, state_dir, source_sha256, include_build_evidence=False):
    """Join exact archived source bytes across accounts; never infer hidden times."""
    with store.db() as db:
        rows = db.execute("SELECT record FROM official_history WHERE problem_id='pdist' "
                          "AND json_extract(record,'$.source_sha256')=? ORDER BY create_time,official_id",
                          (source_sha256,)).fetchall()
    matches = []
    for row in rows:
        try:
            record = json.loads(row['record'])
            identity, account = record['official_id'], record.get('account_id', 'default')
            if (not record.get('source_available') or not re.fullmatch('[0-9a-f]{24}', identity)
                    or not re.fullmatch(r'[a-zA-Z0-9_-]+', account)):
                continue
            directory = state_dir / 'official-history'
            if account != 'default':
                directory = directory / 'accounts' / account
            from .pdist_build_evidence import read_archive
            files = json.loads(read_archive(directory / identity, 'files.json', 16 * 1024 * 1024))
            sources = [f['content'] for f in files if f['path'] == 'kernel.asc']
            if len(sources) != 1 or hashlib.sha256(sources[0].encode()).hexdigest() != source_sha256:
                continue
            cases = []
            for c in record.get('cases', []):
                hidden = c.get('status') == 'Hidden' or c.get('hidden') is True
                value = c.get('time_us')
                available = not hidden and c.get('status') == 'Pass' and finite(value) and value > 0
                cases.append(dict(index=c['index'], testcase_id=c['testcase_id'], status=c.get('status'),
                                  time_us=value if available else None, hidden=hidden))
            build = {}
            if include_build_evidence:
                from .pdist_build_evidence import official_evidence
                build['build_evidence'] = official_evidence(record, files)
            matches.append(dict(official_id=identity, account_id=account, source_sha256=source_sha256,
                                source_verified=True, fetched_at=record.get('fetched_at'),
                                official_url=record.get('official_url'), cases=cases, **build))
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            continue
    return matches


def sync_evidence(row, state_dir):
    """Identity-bound optional sidecar; reads cannot initiate sampling."""
    try:
        path = state_dir / 'submissions' / row['id'] / pdist_sync.SIDECAR
        if not path.exists():
            return {}, 'independent_sync_diagnostic_not_sampled', None
        if path.stat().st_size > 2 * 1024 * 1024:
            raise ValueError('oversized_sidecar')
        value = json.loads(path.read_text(encoding='utf-8'))
        if (value.get('schema_version') != 1 or value.get('diagnostic_kind') != pdist_sync.KIND
                or value.get('submission_id') != row['id'] or value.get('source_sha256') != row['sha256']
                or row['result'].get('source_sha256') != row['sha256']
                or value.get('result_sha256') != digest(row['result']) or value.get('protocol') != PROTOCOL
                or value.get('independent_diagnostic') is not True or value.get('scoring_eligible') is not False
                or not isinstance(value.get('cases'), dict)):
            raise ValueError('diagnostic_identity_or_protocol_mismatch')
        return value['cases'], 'case_not_sampled', value.get('sampled_at')
    except (OSError, ValueError, TypeError, AttributeError):
        return {}, 'invalid_sync_diagnostic_sidecar', None


def with_pdist_diagnostics(row, state_dir, store, binary_layout=False):
    if row.get('problem_id') != 'pdist':
        return row
    row = copy.deepcopy(row)
    result = row['result']
    reason, sidecar = 'independent_diagnostic_not_sampled', {}
    try:
        path = state_dir / 'submissions' / row['id'] / SIDECAR
        if path.exists():
            if path.stat().st_size > 2 * 1024 * 1024:
                raise ValueError('oversized_sidecar')
            sidecar = json.loads(path.read_text(encoding='utf-8'))
            if (sidecar.get('schema_version') != 1 or sidecar.get('submission_id') != row['id']
                    or sidecar.get('source_sha256') != row['sha256'] or result.get('source_sha256') != row['sha256']
                    or sidecar.get('result_sha256') != digest(result) or sidecar.get('protocol') != PROTOCOL
                    or sidecar.get('independent_diagnostic') is not True or sidecar.get('scoring_eligible') is not False
                    or not isinstance(sidecar.get('cases'), dict)):
                raise ValueError('diagnostic_identity_or_protocol_mismatch')
            reason = 'case_not_sampled'
    except (OSError, ValueError, TypeError, AttributeError) as error:
        sidecar, reason = {}, 'invalid_diagnostic_sidecar: '+str(error)
    sync_cases, sync_reason, sync_sampled_at = sync_evidence(row, state_dir)
    cases = []
    metric = result.get('metrics', {}).get('PipeUtilization', {})
    device = {c['index']: c for c in metric.get('cases', [])} if metric.get('status') == 'available' else {}
    dataset = {c['index']: c for c in result.get('dataset', {}).get('cases', [])}
    for case in result.get('cases', []):
        index = case['index']
        detail = sidecar.get('cases', {}).get(str(index))
        host = unavailable(reason)
        if row['status'] != 'accepted' or case['status'] != 'accepted':
            host = unavailable('original_evaluation_not_accepted')
        elif detail is not None:
            try:
                host = (summarize_samples(detail['samples']) if detail['status'] == 'available'
                        else unavailable(detail['unavailable_reason']))
            except (KeyError, TypeError, ValueError, AttributeError):
                host = unavailable('invalid_diagnostic_samples')
        sync = pdist_sync.unavailable(sync_reason)
        sync_detail = sync_cases.get(str(index))
        if row['status'] != 'accepted' or case['status'] != 'accepted':
            sync = pdist_sync.unavailable('original_evaluation_not_accepted')
        elif sync_detail is not None:
            try:
                sync = (pdist_sync.summarize(sync_detail['invocations']) if sync_detail['available'] is True
                        else pdist_sync.unavailable('diagnostic_collection_unavailable'))
            except (KeyError, TypeError, ValueError, AttributeError):
                sync = pdist_sync.unavailable('invalid_sync_diagnostic_samples')
        event_us, device_us = case.get('time_us'), device.get(index, {}).get('steady_state_mean_us')
        passed = case['status'] == 'accepted'
        cases.append(dict(index=index, official_case_index=dataset.get(index, {}).get('official_case_index'),
                          event_us=event_us if passed and finite(event_us) else None,
                          device_us=device_us if passed and finite(device_us) else None, host_calls=host, prevalidation_sync=sync))
    official = official_same_source(store, state_dir, row['sha256'], binary_layout)
    result['pdist_diagnostics'] = dict(schema_version=1, scoring_eligible=False, boundaries=BOUNDARIES,
        protocol=PROTOCOL, prevalidation_sync_sampled_at=sync_sampled_at, prevalidation_sync_boundaries=pdist_sync.BOUNDARIES, sampled_at=sidecar.get('sampled_at'), cases=cases,
        official_same_source=[{k: v for k, v in r.items() if k != 'build_evidence'} for r in official],
        comparison_note='Compare candidate ordering separately for event_us, device_us and archived official public times. '
                        'Different runs, inputs and instrumentation; no inferred official boundary, hidden timing or score.')
    if binary_layout:
        from .pdist_build_evidence import build_evidence
        result['pdist_diagnostics']['binary_layout'] = build_evidence(row, state_dir, official)
    return row

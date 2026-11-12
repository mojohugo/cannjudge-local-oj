"""Read-only presentation of independently replayed historical diagnostics."""
import copy
import json

from remote.launch_metrics import metric_digest, unavailable, COUNT_MISMATCH, PARTIAL_SCHEMA


SIDECAR = 'launch-metrics-v1.json'


def with_launch_metrics(row, state_dir):
    row = copy.deepcopy(row)
    path = state_dir / 'submissions' / row['id'] / SIDECAR
    sidecar, reason = {}, 'historical_launch_trace_not_replayed'
    try:
        if path.exists():
            if path.stat().st_size > 2 * 1024 * 1024:
                raise ValueError('oversized sidecar')
            sidecar = json.loads(path.read_text(encoding='utf-8'))
            if (sidecar.get('schema_version') != 1 or sidecar.get('submission_id') != row['id']
                    or sidecar.get('source_sha256') != row['sha256']
                    or not isinstance(sidecar.get('groups'), dict)):
                raise ValueError('sidecar identity mismatch')
    except (OSError, ValueError, AttributeError):
        sidecar, reason = {}, 'historical_launch_sidecar_invalid'
    for group, metric in row['result'].get('metrics', {}).items():
        group_reason = reason
        saved = sidecar.get('groups', {}).get(group, {})
        if not isinstance(saved, dict) or not isinstance(saved.get('cases', {}), dict):
            saved, group_reason = {}, 'historical_launch_sidecar_invalid'
        matches = saved.get('metric_sha256') == metric_digest(metric)
        if saved:
            group_reason = 'historical_launch_case_missing' if matches else 'historical_launch_metric_mismatch'
        if (row.get('problem_id') == 'pdist' and metric.get('status') == 'unavailable'
                and metric.get('reason') == COUNT_MISMATCH and matches
                and saved.get('dataset_sha256') == metric_digest(row['result'].get('dataset', {}))
                and valid_partial(saved.get('partial_observations'))):
            metric.setdefault('partial_observations', copy.deepcopy(saved['partial_observations']))
        for case in metric.get('cases', []):
            phase = case.setdefault('phase_metrics', {})
            existing = phase.get('launch_breakdown')
            retry_subset = (isinstance(existing, dict) and 'original_index' in case
                            and existing.get('status') == 'unavailable'
                            and existing.get('unavailable_reason') == 'trace_does_not_match_saved_metric')
            if 'launch_breakdown' in phase and not retry_subset:
                continue
            detail = saved.get('cases', {}).get(str(case['index'])) if matches else None
            case_reason = group_reason
            if detail is not None and (not isinstance(detail, dict)
                    or detail.get('status') not in ('matched', 'unavailable')):
                detail, case_reason = None, 'historical_launch_sidecar_invalid'
            if retry_subset:
                # Only a successful replay bound to this complete saved metric
                # may supersede the old metadata-only rejection. Keep the stored
                # refusal if the sidecar is absent, stale, invalid or unavailable.
                if detail is not None and detail['status'] == 'matched':
                    phase['launch_breakdown'] = detail
            else:
                phase['launch_breakdown'] = detail or unavailable(case_reason)
    # The auxiliary Pdist view is computed before this read-only sidecar overlay.
    # Expose per-case observations there without changing any scored metric field.
    partial = row['result'].get('metrics', {}).get('PipeUtilization', {}).get('partial_observations')
    if row.get('problem_id') == 'pdist' and row.get('status') == 'accepted' and valid_partial(partial):
        observations = {c['index']: c for c in partial['cases']}
        passed = {c['index'] for c in row['result'].get('cases', []) if c.get('status') == 'accepted'}
        for case in row['result'].get('pdist_diagnostics', {}).get('cases', []):
            detail = observations.get(case['index'])
            if detail is not None and case['index'] in passed:
                case['device_observation'] = copy.deepcopy(detail)
                case['device_us'] = detail['device_us'] if detail['available'] else None
    return row


def valid_partial(value):
    """Fail closed for malformed sidecars; partial values never become score inputs."""
    import math
    if (not isinstance(value, dict) or value.get('schema_version') != PARTIAL_SCHEMA
            or value.get('scoring_eligible') is not False or value.get('status') not in ('partial', 'unavailable')
            or not isinstance(value.get('cases'), list)):
        return False
    indices = []
    for case in value['cases']:
        if (not isinstance(case, dict) or type(case.get('index')) is not int
                or case['index'] < 1 or type(case.get('available')) is not bool):
            return False
        indices.append(case['index'])
        duration = case.get('device_us')
        if case['available']:
            if (value.get('status') != 'partial' or value.get('mapping') != 'complete_host_task_identity_and_order'
                    or case.get('status') != 'matched' or type(duration) not in (int, float)
                    or not math.isfinite(duration) or duration <= 0):
                return False
        elif duration is not None:
            return False
    return len(set(indices)) == len(indices)

"""Read-only LOCAL case projection; optional bounded sample-sidecar reads, no scoring."""
import math

from .store import TERMINAL


def _dict(value):
    return value if isinstance(value, dict) else {}


def _cases(value):
    return [c for c in value if isinstance(c, dict)] if isinstance(value, list) else []


def _index(value):
    return value if type(value) is int and value > 0 else None


def _number(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def _unique(cases, key, value):
    matches = [c for c in cases if value is not None and c.get(key) == value]
    return matches[0] if len(matches) == 1 else {}


def _phase(metric, name, phase, field):
    detail = _dict(_dict(metric.get('phase_metrics')).get(name))
    if detail.get('available') is True and detail.get('phase') == phase:
        return _number(detail.get(field))
    return None



def _device_unavailable(group, index, original, metadata, correctness_only):
    """Project explicit archived per-case evidence; never explain device causes."""
    value = dict(reason_code='unknown', expected_tasks=None, observed_tasks=None)
    if correctness_only:
        value['reason_code'] = 'correctness_only'
        return value
    partial = _dict(group.get('partial_observations'))
    if (original is None or group.get('status') != 'unavailable'
            or partial.get('schema_version') != 'host-task-partial-v1'
            or partial.get('scoring_eligible') is not False
            or partial.get('mapping') != 'complete_host_task_identity_and_order'):
        return value
    cases = _cases(partial.get('cases'))
    # The archived partial ledger uses task-local indices. Check its label too;
    # neither an array offset nor group totals identify a particular case.
    detail = _unique(cases, 'index', index)
    if (not detail or type(detail.get('index')) is not int
            or not isinstance(metadata.get('name'), str)
            or detail.get('case_id') != metadata['name']
            or ('original_index' in detail and (type(detail['original_index']) is not int
                                               or detail['original_index'] != original))):
        return value
    for source, target in [('expected_launches', 'expected_tasks'),
                           ('observed_launches', 'observed_tasks')]:
        count = detail.get(source)
        if type(count) is int and count >= 0:
            value[target] = count
    # Whitelist a recorded absence-of-profiler-rows code, not arbitrary runtime
    # messages. Counts alone never manufacture a reason, fallback or device fault.
    if (detail.get('available') is False and detail.get('status') == 'unavailable'
            and detail.get('unavailable_reason') == 'missing_op_summary_tasks'):
        value['reason_code'] = 'missing_op_summary_tasks'
    return value

def timing_view(row, *, include_device_samples=False, state_dir=None):
    """Use explicit indices, never list offsets or official/calibrated times.

    Dataset metadata is authoritative for task-local indices. Before it exists,
    the archived problem/regression specification supplies the selected inventory.
    Missing/ambiguous phase or identity evidence stays null, including old metrics
    that only contain op_time_us (which can be a median rather than a mean).
    """
    result = _dict(row.get('result'))
    selection = result.get('local_case_indices')
    subset = result.get('suite') == 'local_subset'
    selected = (sorted({_index(i) for i in selection if _index(i) is not None})
                if isinstance(selection, list) else [])
    originals = dict(enumerate(selected, 1)) if subset else {}
    observed = _cases(result.get('cases'))
    inventory = _cases(_dict(result.get('dataset')).get('cases'))
    if not inventory:
        if result.get('suite') == 'structured_regression':
            inventory = _cases(_dict(result.get('regression_spec')).get('cases'))
        elif result.get('suite') not in ('custom', 'local_ab'):
            inventory = _cases(_dict(result.get('problem')).get('cases'))
            if subset:
                inventory = [dict(_unique(inventory, 'index', original), index=index,
                                  original_index=original)
                             for index, original in originals.items()]
    indices = sorted({_index(c.get('index')) for c in inventory + observed
                      if _index(c.get('index')) is not None})
    group = _dict(_dict(result.get('metrics')).get('PipeUtilization'))
    metrics = _cases(group.get('cases')) if group.get('status') == 'available' else []
    correctness_only = (result.get('correctness_only') is True
                        or result.get('suite') == 'structured_regression'
                        or _dict(result.get('regression_spec')).get('correctness_only') is True)
    samples = {}
    if include_device_samples and not correctness_only:
        from .timing_samples import load_samples
        samples = load_samples(row, state_dir)
    rows = []
    for index in indices:
        case = _unique(observed, 'index', index)
        metadata = _unique(inventory, 'index', index)
        candidates = {_index(c.get('original_index')) for c in (case, metadata)
                      if c.get('original_index') is not None}
        if index in originals:
            candidates.add(originals[index])
        original = (next(iter(candidates)) if len(candidates) == 1 else
                    index if not candidates and not subset else None)
        # A conflicting original index cannot be repaired by matching its offset.
        metric = {}
        if original is not None:
            if any('original_index' in m for m in metrics):
                metric = _unique(metrics, 'original_index', original)
            else:
                metric = _unique(metrics, 'index', index)
        rows.append(dict(
            index=index, original_index=original,
            shape=case.get('shape', metadata.get('shape')),
            dtype=case.get('dtype', metadata.get('dtype')),
            status=case.get('status'), error_rate=_number(case.get('error_rate')),
            event_us=None if correctness_only else _number(case.get('time_us')),
            first_invocation_device_us=None if correctness_only else
                _phase(metric, 'first_invocation', 'prevalidation', 'value_us'),
            steady_state_device_mean_us=None if correctness_only else
                _phase(metric, 'steady_state', 'measured', 'mean_us')))
        if (rows[-1]['first_invocation_device_us'] is None
                or rows[-1]['steady_state_device_mean_us'] is None):
            rows[-1]['device_timing_unavailable'] = _device_unavailable(
                group, index, original, metadata, correctness_only)
        if include_device_samples:
            rows[-1]['device_samples'] = samples.get(metric.get('index')) if metric else None
    view = dict(id=row['id'], sha256=row.get('sha256'), problem_id=row.get('problem_id'),
                status=row['status'], terminal=row['status'] in TERMINAL,
                queue_stage=row.get('queue_stage'), ahead_count=row.get('ahead_count'),
                suite=result.get('suite'), profile_mode=result.get('profile_mode'),
                correctness_only=correctness_only, timing_scope='local', timing_unit='us',
                timing_semantics={
                    'event_us': 'Archived cases.time_us: ACL event timing (Pdist: median of measured calls).',
                    'first_invocation_device_us': 'PipeUtilization prevalidation: sum of device tasks in the first invocation; not verified cold-cache latency.',
                    'steady_state_device_mean_us': 'PipeUtilization measured phase: arithmetic mean of per-invocation device-task sums; profiler-instrumented, not pure kernel latency.',
                    'missing': 'null means unavailable, unrecorded or ambiguous; pending case status is null. No official or estimated timings.'},
                cases=rows)

    if include_device_samples:
        view['timing_semantics']['device_samples'] = ('Verified archived PipeUtilization per-invocation device-task sums in us; measured, warmup and prevalidation are separate. sample_count counts invocations, spread_us=max_us-min_us. Each op_summary task counted once; no task_time/AIC/AIV counters. Null when missing or unverified. Local observations only, not official variability or a pass condition.')
    return view

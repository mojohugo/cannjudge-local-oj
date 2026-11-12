"""Add diagnostic launch ordinals without changing the profiler timing ledger.

Only parsers with checked per-invocation counts and exact task ordering qualify.
Replaying their existing collect() validates the trace and binds it to the result.
Neither task_time nor AIC/AIV counters are added to op_summary durations.
"""
import csv
import hashlib
import importlib.util
import json
import math
import statistics


def metric_digest(metric):
    return hashlib.sha256(json.dumps(metric, sort_keys=True, separators=(',', ':'),
                                    ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def unavailable(reason):
    return dict(status='unavailable', available=False, phase='measured',
                unavailable_reason=reason, launches=[])


def summarize_launches(calls, logical_indices, order_verified):
    """calls contains the same ordered op_summary rows used by the total."""
    if not order_verified:
        return unavailable('task_order_unverified')
    if not calls or any(not call for call in calls):
        return unavailable('missing_measured_invocation')
    counts = [len(call) for call in calls]
    if len(set(counts)) != 1:
        return {**unavailable('unequal_launch_counts'), 'observed_launch_counts': counts}
    if len(logical_indices) != len(calls) or len(set(logical_indices)) != len(calls):
        return unavailable('invocation_mapping_incomplete')
    launches = []
    for ordinal, rows in enumerate(zip(*calls), 1):
        names = {row.get('Op Name') for row in rows}
        if len(names) != 1 or not next(iter(names)):
            return unavailable('launch_name_mismatch_at_ordinal_' + str(ordinal))
        values = [float(row['Task Duration(us)']) for row in rows]
        if any(not math.isfinite(value) or value <= 0 for value in values):
            return unavailable('invalid_task_duration')
        launches.append(dict(launch_ordinal=ordinal, kernel_name=rows[0]['Op Name'],
                             status='matched', observations=len(values),
                             mean_us=statistics.fmean(values), median_us=statistics.median(values)))
    return dict(status='matched', available=True, phase='measured', unavailable_reason=None,
                ordinal_base=1, invocations=len(calls), launches_per_invocation=counts[0],
                kernel_count=sum(counts), launches=launches,
                sum_launch_mean_us=math.fsum(row['mean_us'] for row in launches),
                duration_source='op_summary.Task Duration(us)',
                accounting='same_invocation_task_duration_sum_as_steady_state; each op_summary row once',
                grouping=dict(source='recorded_device_launch_counts', verified=True,
                              task_order='single_stream_decimal_start_time',
                              logical_launch_indices=logical_indices),
                parent_correlation=dict(available=False, unavailable_reason='not_recorded_by_this_parser'))


def collect_launch_metrics(metric, jsonl, directory, parser_path, *, include_device_samples=False):
    """Return per-case diagnostics; invalid/unsupported traces never affect scores."""
    def failed(reason):
        return {case['index']: unavailable(reason) for case in metric.get('cases', [])}

    try:
        spec = importlib.util.spec_from_file_location('launch_diagnostic_parser', parser_path)
        parser = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(parser)
        if not all(hasattr(parser, name) for name in ('order_operations', 'measurement_protocol', 'harness')):
            return failed('parser_has_no_verified_invocation_mapping')
        protocol = metric.get('measurement_protocol', {})
        if protocol.get('invocation_mapping') != 'recorded_device_launch_counts':
            return failed('invocation_mapping_not_recorded')
        if protocol.get('task_order') != 'single_stream_decimal_start_time':
            return failed('task_order_unverified')
        aggregation = 'mean' if protocol.get('primary_metric') == 'steady_state_mean_us' else 'median'
        replay = parser.collect(jsonl, directory, metric['group'], aggregation=aggregation)
        # local_cases.publish adds dataset identity before Execution.emit runs.
        # The trace and parser still use dense local indices, not original_index.
        # Exclude only presentation metadata; compare every measurement field.
        original = json.loads(json.dumps(metric))
        for case in original.get('cases', []):
            case.pop('original_index', None)
            case.get('phase_metrics', {}).pop('launch_breakdown', None)
        if replay != original:
            return failed('trace_does_not_match_saved_metric')
        paths = list(directory.rglob('op_summary*.csv'))
        with paths[0].open(encoding='utf-8-sig', newline='') as handle:
            ops, verified = parser.order_operations(list(csv.DictReader(handle)), protocol['run_config']['device'])
        cursor, calls, indices, samples = 0, {}, {}, {}
        for phase in parser.harness.read_jsonl(jsonl):
            if phase['type'] != 'phase':
                continue
            for ordinal, count in enumerate(phase['device_launch_counts']):
                call = ops[cursor:cursor + count]
                cursor += count
                if include_device_samples and verified and phase['phase'] in ('prevalidation', 'warmup', 'measured'):
                    # Use the very same checked ledger, never launch-name averages.
                    entry = samples.setdefault(phase['case_index'] + 1, {}).setdefault(
                        phase['phase'], dict(task_sums_us=[], logical_launch_indices=[], task_counts=[]))
                    entry['task_sums_us'].append(math.fsum(float(r['Task Duration(us)']) for r in call) if call else None)
                    entry['logical_launch_indices'].append(phase['logical_launch_begin'] + ordinal)
                    entry['task_counts'].append(count)
                if phase['phase'] == 'measured':
                    index = phase['case_index'] + 1
                    calls.setdefault(index, []).append(call)
                    indices.setdefault(index, []).append(phase['logical_launch_begin'] + ordinal)
        output = {}
        for case in metric['cases']:
            index = case['index']
            detail = summarize_launches(calls.get(index, []), indices.get(index, []), verified)
            if detail['available'] and not math.isclose(detail['sum_launch_mean_us'],
                    case['steady_state_mean_us'], rel_tol=1e-12, abs_tol=1e-6):
                detail = unavailable('launch_mean_sum_does_not_match_steady_state_mean')
            if include_device_samples:
                # Unequal launch counts can prevent ordinal comparison while each
                # invocation total remains exact. No scoring field is changed.
                detail['device_samples'] = samples.get(index) if verified else None
            output[index] = detail
        return output
    except (OSError, ValueError, KeyError, TypeError, IndexError, AttributeError, ImportError) as error:
        # Do not return filenames or raw CSV/source content to callers.
        if isinstance(error, OSError):
            return failed('trace_or_parser_files_unavailable')
        for message, reason in (
                ('launch count mismatch', 'device_launch_count_mismatch'),
                ('counts do not cover logical invocations', 'invocation_mapping_incomplete'),
                ('without device kernel', 'missing_measured_invocation'),
                ('Duplicate profiler task', 'duplicate_device_task'),
                ('Incomplete profiler task timestamps', 'incomplete_task_timestamps'),
                ('Overlapping profiler tasks', 'overlapping_device_tasks'),
                ('Ambiguous profiler streams', 'ambiguous_device_streams')):
            if message in str(error):
                return failed(reason)
        return failed('trace_validation_failed:' + type(error).__name__)


def attach_launch_metrics(metric, details):
    for case in metric.get('cases', []):
        case.setdefault('phase_metrics', {})['launch_breakdown'] = details[case['index']]


COUNT_MISMATCH = 'Profiler / observed device launch count mismatch'
PARTIAL_SCHEMA = 'host-task-partial-v1'


def collect_partial_profile(jsonl, directory, parser_path):
    """Diagnostic-only recovery using a complete, independently recorded host ledger.

    Counts alone cannot resynchronize a damaged CSV. HostTask must cover every
    expected kernel, in one thread/stream, with unique identities and timestamps.
    Missing CSV identities then leave holes rather than shifting later calls.
    Neither numeric Task ID gaps nor kernel names establish invocation boundaries.
    """
    import sqlite3

    result = dict(schema_version=PARTIAL_SCHEMA, status='unavailable', scoring_eligible=False,
                  expected_launches=None, observed_launches=None, cases=[], first_mismatch=None,
                  mapping='unverified', unavailable_reason=None, cause='unknown',
                  fallback='unknown', sampling_interruption='unknown',
                  expected_source='harness.device_launch_counts', observed_source='op_summary',
                  host_launches=None)
    try:
        spec = importlib.util.spec_from_file_location('partial_launch_parser', parser_path)
        parser = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(parser)
        report = parser.harness.summarize_file(jsonl)
        if report['status'] != 'PASS' or report['run_config']['mode'] != 'profile':
            raise ValueError('incomplete_profiler_validation')
        phases = [r for r in parser.harness.read_jsonl(jsonl) if r['type'] == 'phase']
        if any(type(p.get('device_launch_counts')) is not list or
               len(p['device_launch_counts']) != p['logical_launch_count'] or
               any(type(n) is not int or n < 0 for n in p['device_launch_counts']) for p in phases):
            raise ValueError('invocation_mapping_incomplete')
        expected = sum(sum(p['device_launch_counts']) for p in phases)
        result['expected_launches'] = expected
        paths = list(directory.rglob('op_summary*.csv'))
        if len(paths) != 1:
            raise ValueError('missing_or_ambiguous_op_summary')
        with paths[0].open(encoding='utf-8-sig', newline='') as handle:
            ops = list(csv.DictReader(handle))
        result['observed_launches'] = len(ops)
        if len(ops) == expected:
            raise ValueError('not_a_launch_count_mismatch')
        ops, ordered = parser.order_operations(ops, report['run_config']['device'])
        if not ordered:
            raise ValueError('task_order_unverified')
        paths = list(directory.glob('PROF_*/host/sqlite/runtime.db'))
        if len(paths) != 1:
            raise ValueError('complete_host_task_ledger_unavailable')
        # Read only: never run SDK analysis or mutate the archived database.
        with sqlite3.connect(paths[0].resolve().as_uri() + '?mode=ro', uri=True) as db:
            db.row_factory = sqlite3.Row
            host = [dict(r) for r in db.execute('SELECT * FROM HostTask')]
        kernel_types = {'KERNEL_AICORE', 'KERNEL_AIVEC', 'KERNEL_MIX_AIC', 'KERNEL_MIX_AIV'}
        if any(r['task_type'] not in kernel_types | {'PROFILING_ENABLE', 'PROFILING_DISABLE'} for r in host):
            raise ValueError('unsupported_host_task_type')
        host = [r for r in host if r['task_type'] in kernel_types]
        result['host_launches'] = len(host)
        if len(host) != expected:
            raise ValueError('host_task_count_mismatch')
        if (not host or any(r['device_id'] != report['run_config']['device'] for r in host) or
                len({(r['device_id'], r['stream_id'], r['thread_id'], r['connection_id'],
                      r['model_id'], r['batch_id']) for r in host}) != 1):
            raise ValueError('ambiguous_host_task_scope')
        host.sort(key=lambda r: parser.decimal_value(r['timestamp'], 'host timestamp', positive=True))
        if len({parser.decimal_value(r['timestamp'], 'host timestamp') for r in host}) != expected:
            raise ValueError('ambiguous_host_task_order')
        def host_key(r):
            return (str(r['device_id']), str(r['stream_id']), str(r['task_id']))
        def op_key(r):
            return (r['Device_id'], r['Stream ID'], r['Task ID'])
        keys = [host_key(r) for r in host]
        if len(set(keys)) != expected:
            raise ValueError('ambiguous_host_task_identity')
        positions = {key: i for i, key in enumerate(keys)}
        # Preserve the validated dense case index used by the existing ordinal path.
        locations = []
        for phase in phases:
            for ordinal, count in enumerate(phase['device_launch_counts']):
                location = dict(index=phase['case_index'] + 1, case_id=phase['label'],
                                phase=phase['phase'], logical_launch_index=phase['logical_launch_begin'] + ordinal,
                                expected_launches=count)
                locations.extend([location] * count)
        known = [r for r in ops if op_key(r) in positions]
        seq = [positions[op_key(r)] for r in known]
        if seq != sorted(seq):
            raise ValueError('host_device_order_mismatch')
        for row in known:
            h = host[positions[op_key(row)]]
            if (row['Op Name'] != h['kernel_name'] or
                    ('Model ID' in row and row['Model ID'] != str(h['model_id']))):
                raise ValueError('host_device_identity_mismatch')
        extras = [i for i, r in enumerate(ops) if op_key(r) not in positions]
        if extras:
            # An extra device task has no trusted host boundary. Report a bounded
            # location only when both neighbouring known tasks are in ONE call.
            i = extras[0]
            left = next((op_key(r) for r in reversed(ops[:i]) if op_key(r) in positions), None)
            right = next((op_key(r) for r in ops[i+1:] if op_key(r) in positions), None)
            if left and right and locations[positions[left]] == locations[positions[right]]:
                loc = locations[positions[left]]
                by_left = next(r for r in known if op_key(r) == left)
                by_right = next(r for r in known if op_key(r) == right)
                observed = sum(locations[positions[op_key(r)]] == loc for r in known)
                extra_count = sum(op_key(r) not in positions for r in ops[ops.index(by_left):ops.index(by_right)])
                result['first_mismatch'] = dict(loc, observed_launches=observed + extra_count,
                    unassigned_extra_launches=len(extras), observed_count_scope='bracketed_device_tasks',
                    reason='unattributed_device_task_between_known_tasks')
            raise ValueError('device_task_absent_from_host_ledger')
        result['mapping'] = 'complete_host_task_identity_and_order'
        by_key = {op_key(r): r for r in ops}
        cases, cursor = {}, 0
        for phase in phases:
            index = phase['case_index'] + 1
            case = cases.setdefault(index, dict(index=index, case_id=phase['label'], status='matched',
                available=True, device_us=None, expected_launches=0, observed_launches=0,
                invocations=[], phase_metrics={}, cause='unknown', fallback='unknown', sampling_interruption='unknown'))
            for ordinal, count in enumerate(phase['device_launch_counts']):
                call_keys = keys[cursor:cursor+count]
                call = [by_key[k] for k in call_keys if k in by_key]
                missing = [k for k in call_keys if k not in by_key]
                cursor += count
                detail = dict(phase=phase['phase'], logical_launch_index=phase['logical_launch_begin'] + ordinal,
                    expected_launches=count, observed_launches=len(call), status='matched' if not missing and count else 'unavailable',
                    missing_launch_ordinals=[i for i, k in enumerate(call_keys, 1) if k not in by_key],
                    missing_task_ids=[dict(device_id=k[0], stream_id=k[1], task_id=k[2]) for k in missing])
                case['expected_launches'] += count
                case['observed_launches'] += len(call)
                case['invocations'].append(detail)
                if missing or not count:
                    case.update(status='unavailable', available=False,
                                unavailable_reason='missing_op_summary_tasks' if missing else 'invocation_without_device_kernel')
                    if result['first_mismatch'] is None:
                        result['first_mismatch'] = dict(index=index, case_id=phase['label'], **detail)
                # Private accumulator removed before serialization; never sum holes.
                case.setdefault('_calls', []).append((detail, call))
        for case in cases.values():
            calls = case.pop('_calls')
            if not case['available']:
                case['phase_metrics']['launch_breakdown'] = unavailable(case['unavailable_reason'])
                continue
            measured = [(d, c) for d, c in calls if d['phase'] == 'measured']
            values = [sum(float(r['Task Duration(us)']) for r in c) for _, c in measured]
            if not values or any(not math.isfinite(v) or v <= 0 for v in values):
                raise ValueError('invalid_measured_duration')
            case['device_us'] = statistics.fmean(values)
            case['phase_metrics']['steady_state'] = dict(available=True, value_us=case['device_us'],
                aggregation='arithmetic_mean_of_invocation_task_duration_sums', phase='measured')
            breakdown = summarize_launches([c for _, c in measured],
                                          [d['logical_launch_index'] for d, _ in measured], True)
            if breakdown['available']:
                breakdown['grouping']['source'] = 'complete_host_task_identity_and_recorded_device_launch_counts'
            case['phase_metrics']['launch_breakdown'] = breakdown
        result.update(status='partial', cases=list(cases.values()), unavailable_reason=None,
                      available_cases=sum(c['available'] for c in cases.values()), total_cases=len(cases))
    except (OSError, ValueError, KeyError, TypeError, IndexError, AttributeError, ImportError, sqlite3.Error) as error:
        # No exception text containing paths, SQL or source is returned.
        safe = {'incomplete_profiler_validation', 'invocation_mapping_incomplete',
                'missing_or_ambiguous_op_summary', 'not_a_launch_count_mismatch', 'task_order_unverified',
                'complete_host_task_ledger_unavailable', 'unsupported_host_task_type', 'host_task_count_mismatch',
                'ambiguous_host_task_scope', 'ambiguous_host_task_order', 'ambiguous_host_task_identity',
                'host_device_order_mismatch', 'host_device_identity_mismatch', 'device_task_absent_from_host_ledger',
                'invalid_measured_duration'}
        result.update(status='unavailable', cases=[], unavailable_reason=str(error) if str(error) in safe
                      else 'partial_trace_validation_failed:' + type(error).__name__)
    return result

"""Bounded prevalidation-only ACL status evidence; never a score or root cause."""
import importlib.util
import json

from .pdist_host import PROTOCOL

SIDECAR = 'pdist-sync-status-v1.json'
KIND = 'prevalidation-sync-status-v1'
BOUNDARIES = dict(
    scope='independent replay; first two prevalidation run_kernel invocations only',
    ordering='api_sequence counts candidate alloc/sync/free calls; launch_calls use existing OJLaunchCount snapshots',
    launch_semantics='host launch API attempts, not successful API returns or completed device tasks; no kernel identity',
    excluded='harness APIs, warmup, measured calls, other APIs, indirect external calls and threads',
    limits='first non-success per invocation only; at most 64 subsequent allocations; overflow rejects evidence',
    interpretation='non-success is an observed return code, not a device root cause or proof of fallback; no historical attribution',
    perturbation='separate diagnostic shim and process; no clocks; original executable, flags, data, timing and scores unchanged',
)


def unavailable(reason):
    return dict(status='unavailable', available=False, unavailable_reason=str(reason)[:1000],
                first_non_success=None, invocations=[], cause='unknown', fallback='unknown', scoring_eligible=False)


def natural(value):
    return type(value) is int and 0 <= value <= 2**63 - 1


def summarize(samples):
    if not isinstance(samples, list) or len(samples) != 2:
        raise ValueError('missing_prevalidation_sync_samples')
    normalized = []
    for i, s in enumerate(samples):
        if (not isinstance(s, dict) or type(s.get('invocation')) is not int or s['invocation'] != i
                or s.get('overflow') is not False
                or any(not natural(s.get(k)) for k in ('api_calls', 'sync_calls', 'allocation_calls', 'launch_calls'))
                or s['sync_calls'] + s['allocation_calls'] > s['api_calls']):
            raise ValueError('invalid_prevalidation_sync_sample')
        first, allocs = s.get('first_non_success'), s.get('subsequent_allocations')
        if not isinstance(allocs, list) or len(allocs) > 64:
            raise ValueError('invalid_subsequent_allocations')
        if first is None:
            if allocs:
                raise ValueError('allocations_without_sync_failure')
        else:
            if (not isinstance(first, dict) or first.get('api') not in
                    ('aclrtSynchronizeStream', 'aclrtSynchronizeStreamWithTimeout')
                    or type(first.get('code')) is not int or not -(2**31) <= first['code'] < 2**31 or first['code'] == 0
                    or any(not natural(first.get(k)) for k in
                           ('api_sequence', 'sync_ordinal', 'allocation_calls_before', 'launch_calls_before'))
                    or not 1 <= first['api_sequence'] <= s['api_calls']
                    or not 1 <= first['sync_ordinal'] <= s['sync_calls']
                    or first['allocation_calls_before'] > s['allocation_calls']
                    or first['allocation_calls_before'] + first['sync_ordinal'] > first['api_sequence']
                    or first['launch_calls_before'] > s['launch_calls']
                    or len(allocs) != s['allocation_calls'] - first['allocation_calls_before']):
                raise ValueError('invalid_first_sync_failure')
            sequence, launches = first['api_sequence'], first['launch_calls_before']
            for j, a in enumerate(allocs, first['allocation_calls_before'] + 1):
                if (not isinstance(a, dict) or any(not natural(a.get(k)) for k in
                        ('api_sequence', 'allocation_ordinal', 'launch_calls_before'))
                        or not sequence < a['api_sequence'] <= s['api_calls']
                        or a['allocation_ordinal'] != j or type(a.get('success')) is not bool
                        or not launches <= a['launch_calls_before'] <= s['launch_calls']):
                    raise ValueError('invalid_subsequent_allocation_order')
                sequence, launches = a['api_sequence'], a['launch_calls_before']
            first = {k: first[k] for k in ('api', 'code', 'api_sequence', 'sync_ordinal',
                                          'allocation_calls_before', 'launch_calls_before')}
        # Whitelist fields: runtime messages, addresses and other payloads are never surfaced.
        normalized.append(dict(invocation=i, phase='prevalidation',
            **{k: s[k] for k in ('api_calls', 'sync_calls', 'allocation_calls', 'launch_calls')},
            overflow=False, first_non_success=first,
            subsequent_allocations=[{k: a[k] for k in ('api_sequence', 'allocation_ordinal', 'launch_calls_before', 'success')} for a in allocs],
            subsequent_launch_calls=None if first is None else s['launch_calls']-first['launch_calls_before']))
    first = next((dict(invocation=s['invocation'], phase='prevalidation', **s['first_non_success'])
                  for s in normalized if s['first_non_success'] is not None), None)
    return dict(status='observed_non_success' if first else 'no_non_success_observed', available=True,
                unavailable_reason=None, first_non_success=first, invocations=normalized,
                cause='unknown', fallback='unknown', scoring_eligible=False)


def parse_run(jsonl, stderr, parser_path, expected, code=0, reason=None):
    try:
        if code or reason or 'OJ_RULE ' in stderr or 'OJ_HOST_ERROR ' in stderr:
            raise ValueError('diagnostic_execution_failed')
        spec = importlib.util.spec_from_file_location('pdist_sync_checker', parser_path)
        parser = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(parser)
        report = parser.summarize_file(jsonl)
        if report['status'] != 'PASS' or len(report['cases']) != 1:
            raise ValueError('diagnostic_correctness_failed')
        if any(report['cases'][0].get(k) != v for k, v in {**PROTOCOL, **expected}.items()):
            raise ValueError('diagnostic_case_or_protocol_mismatch')
        rows = parser.read_jsonl(jsonl)
        pre = [r for r in rows if r.get('phase') == 'prevalidation']
        if len(pre) != 1 or pre[0]['logical_launch_begin'] != 0 or pre[0]['logical_launch_count'] != 2:
            raise ValueError('diagnostic_prevalidation_mapping_mismatch')
        samples = [json.loads(line[len('OJ_SYNC '):]) for line in stderr.splitlines() if line.startswith('OJ_SYNC ')]
        value = summarize(samples)
        if [s['launch_calls'] for s in samples] != pre[0]['device_launch_counts']:
            raise ValueError('diagnostic_launch_count_mismatch')
        return value
    except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError) as error:
        return unavailable(error)

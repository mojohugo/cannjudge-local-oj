"""Direct host API observations from a separate, correctness-checked Pdist run."""
import hashlib
import importlib.util
import json
import math
import re
import statistics

SIDECAR = 'pdist-host-diagnostics-v1.json'
OPERATIONS = ('workspace_alloc', 'stream_sync', 'workspace_free')
PROTOCOL = dict(mode='profile', buffers=1, warmups=1, repeats=3, rounds=1)
BOUNDARIES = dict(
    host_api='steady_clock immediately around candidate aclrtMalloc, aclrtFree, '
             'aclrtSynchronizeStream / WithTimeout, while run_kernel is active; attempted calls including failures',
    excluded='harness input/output allocation, copies, validation and harness stream sync; other APIs and threads are not observed',
    host_call='run_kernel entry to return, including instrumentation; async return is not device completion',
    local_event='existing timing-process ACL-event batch span / repeats, median across rounds; includes stream submission gaps',
    local_device='existing independent profiler mean of complete invocation device-task duration sums',
    official='archived official public-point timings; private timer boundary is not verified',
    non_additive='sync wall time can include device execution; API times cannot be added to device time or inferred from event minus device',
    perturbation='separate process and rebuilt shim; clock reads, counters, wrapper calls and changed code layout perturb timing; '
                 'raw ACL allocations replace the harness memory-accounting wrapper in this diagnostic only; '
                 'logs emitted after validation; no overhead subtraction or correction of scored timings',
)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def unavailable(reason):
    return dict(status='unavailable', available=False, unavailable_reason=str(reason)[:1000], samples=[], operations=None)


def finite(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def summarize_samples(samples):
    if not isinstance(samples, list) or len(samples) != PROTOCOL['repeats']:
        raise ValueError('missing_diagnostic_samples')
    for sample in samples:
        if type(sample.get('invocation')) is not int or not finite(sample.get('host_call_us')):
            raise ValueError('invalid_diagnostic_sample')
        for op in OPERATIONS:
            c = sample[op]
            if (type(c.get('calls')) is not int or c['calls'] < 0 or type(c.get('errors')) is not int
                    or c['errors'] != 0 or not finite(c.get('total_us'))
                    or (c['calls'] == 0 and c['total_us'] != 0)):
                raise ValueError('failed_or_invalid_host_api_observation')
        if sum(sample[op]['total_us'] for op in OPERATIONS) > sample['host_call_us'] + 1e-6:
            raise ValueError('host_api_intervals_exceed_invocation')
    if [s['invocation'] for s in samples] != list(range(3, 6)):
        raise ValueError('diagnostic_measured_invocation_mismatch')
    operations = {}
    for op in OPERATIONS:
        times = [s[op]['total_us'] for s in samples]
        counts = [s[op]['calls'] for s in samples]
        operations[op] = dict(mean_us_per_invocation=statistics.mean(times), median_us_per_invocation=statistics.median(times),
                              total_calls=sum(counts), calls_per_invocation=counts)
    return dict(status='available', available=True, unavailable_reason=None, samples=samples, operations=operations)


def parse_run(jsonl, stderr, parser_path, expected, code=0, reason=None):
    """Validate the full original checker ledger before exposing ANY sample."""
    try:
        if code or reason or 'OJ_RULE ' in stderr or 'OJ_HOST_ERROR ' in stderr:
            raise ValueError('diagnostic_execution_failed: '+str(reason or code))
        spec = importlib.util.spec_from_file_location('pdist_host_checker', parser_path)
        parser = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(parser)
        report = parser.summarize_file(jsonl)
        if report['status'] != 'PASS' or len(report['cases']) != 1:
            raise ValueError('diagnostic_correctness_failed')
        case = report['cases'][0]
        if any(case.get(k) != v for k, v in {**PROTOCOL, **expected}.items()):
            raise ValueError('diagnostic_case_or_protocol_mismatch')
        samples = [json.loads(line[len('OJ_HOST '):]) for line in stderr.splitlines() if line.startswith('OJ_HOST ')]
        total = case['launch_counts']['total_launches']
        if [s.get('invocation') for s in samples] != list(range(total)):
            raise ValueError('diagnostic_invocation_ledger_mismatch')
        # Check failures even in prevalidation/warmup; never treat a failed API as zero cost.
        if any(s[op]['errors'] != 0 for s in samples for op in OPERATIONS):
            raise ValueError('diagnostic_host_api_failed')
        rows = parser.read_jsonl(jsonl)
        measured = [i for r in rows if r.get('phase') == 'measured'
                    for i in range(r['logical_launch_begin'], r['logical_launch_begin'] + r['logical_launch_count'])]
        return summarize_samples([samples[i] for i in measured])
    except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError) as error:
        return unavailable(error)


def candidate_shim(source, header, sync_status_only=False):
    # Candidate-only macros are removed before the harness resumes. The harness's
    # external synchronization can therefore never enter stream_sync counters.
    text = source.read_text(encoding='utf-8')
    clean = re.sub(r'//[^\n]*|/\*[\s\S]*?\*/', '', text)
    if re.search(r'\b(?:rtMalloc\w*|rtFree\w*|rtStreamSynchronize\w*|aclrtMalloc\w+|aclrtFree\w+|'
                 r'aclrtSynchronizeDevice\w*|dlsym|dlopen|pthread_create|thread)\b|'
                 r'^\s*#\s*(?:undef|define)\s+(?:aclrt\w+|run_kernel)\b', clean, re.M):
        raise ValueError('unsupported_host_api_or_macro_or_thread')
    return ('#define OJ_PDIST_SYNC_STATUS_ONLY 1\n' if sync_status_only else '') + '''#undef aclrtMalloc
#undef aclrtFree
#include HEADER
#define aclrtMalloc oj_pdist_alloc
#define aclrtFree oj_pdist_free
#define aclrtSynchronizeStream oj_pdist_sync
#define aclrtSynchronizeStreamWithTimeout oj_pdist_sync_timeout
#define run_kernel oj_pdist_original_run_kernel
#include SOURCE
#undef run_kernel
#undef aclrtMalloc
#undef aclrtFree
#undef aclrtSynchronizeStream
#undef aclrtSynchronizeStreamWithTimeout
extern "C" void run_kernel(GM_ADDR x, const TensorGroupInfo& ix, GM_ADDR y,
                          const TensorGroupInfo& iy, int64_t cores, aclrtStream stream) {
    oj_pdist_host::Begin();
    oj_pdist_original_run_kernel(x, ix, y, iy, cores, stream);
    oj_pdist_host::End();
}
'''.replace('HEADER', json.dumps(str(header.resolve()))).replace('SOURCE', json.dumps(str(source.resolve())))

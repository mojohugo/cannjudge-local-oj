"""Bounded ABBA diagnostics; ordinary adapter, data, checks and timing stay intact."""
import copy
import csv
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import signal
import statistics
import time
import uuid

if __package__:
    from .compilation import Execution
    from .local_cases import evaluate_local_cases, validate_local_case_indices
    from .locking import DeviceLease
else:
    from compilation import Execution
    from local_cases import evaluate_local_cases, validate_local_case_indices
    from locking import DeviceLease


def case_indices(request):
    return request.get('custom_case_indices') or request.get('local_case_indices')


def validate_request(pack, request):
    if not isinstance(request, dict):
        raise ValueError('Invalid A/B request')
    custom = request.get('custom_case_indices') is not None
    if custom:
        if pack['id'] != 'pdist' or request.get('local_case_indices') is not None:
            raise ValueError('Choose exactly one of canonical or custom case indices')
        if __package__:
            from .custom_ab import indices as validate_custom_indices
        else:
            from custom_ab import indices as validate_custom_indices
        indices = validate_custom_indices(request['custom_case_indices'])
    else:
        indices = validate_local_case_indices(pack, request.get('local_case_indices'))
        if indices is None or len(indices) > 3:
            raise ValueError('A/B requires an explicit canonical subset of 1 to 3 cases')
    cycles, seconds = request.get('cycles'), request.get('budget_seconds')
    if type(cycles) is not int or not 1 <= cycles <= 3:
        raise ValueError('cycles must be an explicit integer from 1 to 3')
    if type(seconds) is not int or not 60 <= seconds <= 1200:
        raise ValueError('budget_seconds must be an explicit integer from 60 to 1200')
    return dict(**({'custom_case_indices':indices} if custom else {'local_case_indices':indices}), cycles=cycles, budget_seconds=seconds,
                max_evaluations=4*cycles, max_case_evaluations=4*cycles*len(indices))


def raw_device_samples(metric, work, parser_path):
    """Replay the trusted parser before extracting its exact measured task sums."""
    if metric.get('status') != 'available':
        raise ValueError('PipeUtilization device times unavailable')
    spec = importlib.util.spec_from_file_location('ab_profile_parser', parser_path)
    parser = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(parser)
    trace, directory = work/'profile-PipeUtilization.jsonl', work/'profile-PipeUtilization'
    replay = parser.collect(trace, directory, 'PipeUtilization', aggregation='mean')
    original = copy.deepcopy(metric)
    for case in original['cases']:
        case.pop('original_index', None)
        case.get('phase_metrics', {}).pop('launch_breakdown', None)
    if replay != original or not replay['measurement_protocol']['exact']:
        raise ValueError('Device trace identity/order mismatch')
    paths = list(directory.rglob('op_summary*.csv'))
    with paths[0].open(encoding='utf-8-sig', newline='') as stream:
        ops, verified = parser.order_operations(list(csv.DictReader(stream)),
            replay['measurement_protocol']['run_config']['device'])
    if not verified:
        raise ValueError('Device task order unverified')
    cursor, samples = 0, {}
    for phase in parser.harness.read_jsonl(trace):
        if phase['type'] != 'phase':
            continue
        for ordinal, count in enumerate(phase['device_launch_counts']):
            call = ops[cursor:cursor+count]
            cursor += count
            if phase['phase'] == 'measured':
                durations = [float(r['Task Duration(us)']) for r in call]
                samples.setdefault(phase['case_index']+1, []).append(dict(
                    logical_launch_index=phase['logical_launch_begin']+ordinal,
                    device_us=math.fsum(durations), task_durations_us=durations))
    for case in replay['cases']:
        values = [s['device_us'] for s in samples.get(case['index'], [])]
        if (len(values) != 3 or any(not math.isfinite(v) or v <= 0 for v in values)
                or not math.isclose(statistics.fmean(values), case['steady_state_mean_us'],
                                    rel_tol=1e-12, abs_tol=1e-6)):
            raise ValueError('Incomplete or inconsistent measured device samples')
    return samples


def summarize(report):
    """Pair adjacent run means, B-A in both AB and BA halves; no pseudo-replicates."""
    output = []
    runs = report['runs']
    for index, original in enumerate(case_indices(report['request']), 1):
        samples = {label: [s['device_us'] for r in runs if r.get('available') and r['label'] == label
                          for s in r['device_samples'][str(index)]] for label in ('A', 'B')}
        pairs, blocks = [], []
        for offset in range(0, len(runs), 4):
            block = runs[offset:offset+4]
            if len(block) != 4 or not all(r.get('available') for r in block):
                continue
            means = [statistics.fmean(s['device_us'] for s in r['device_samples'][str(index)]) for r in block]
            differences = [means[1]-means[0], means[2]-means[3]]
            for positions, delta in zip(((0,1),(3,2)), differences):
                pairs.append(dict(a_run=offset+positions[0]+1, b_run=offset+positions[1]+1,
                                  b_minus_a_us=delta))
            blocks.append(statistics.fmean(differences))
        def stats(values):
            return (dict(count=len(values), mean_us=statistics.fmean(values),
                         min_us=min(values), max_us=max(values), range_us=max(values)-min(values)) if values else None)
        output.append(dict(index=index, original_index=original, raw_device_us=samples,
                           sample_statistics={k:stats(v) for k,v in samples.items()}, pairs=pairs,
                           paired_difference=stats([p['b_minus_a_us'] for p in pairs]),
                           block_b_minus_a_us=blocks, paired=bool(pairs)))
    return output


def validate_report(report, request, dataset):
    """Fail closed on truncated, reordered or misidentified transport evidence."""
    if (request is None or report.get('schema_version') != 1 or report.get('request') != request
            or report.get('dataset') != dataset
            or report.get('source_sha256') != {k:v['source_sha256'] for k,v in request['sources'].items()}
            or report.get('order') != ['A','B','B','A']*request['cycles']):
        raise ValueError('A/B report identity mismatch')
    if request.get('custom_case_indices') is not None and dataset is not None:
        if __package__:
            from .custom_ab import dataset_error
        else:
            from custom_ab import dataset_error
        if dataset_error(dataset, request):
            raise ValueError(dataset_error(dataset, request))
    runs = report.get('runs', [])
    if len(runs) > 4*request['cycles']:
        raise ValueError('A/B run count exceeds budget')
    protocol = None
    for ordinal, run in enumerate(runs, 1):
        label = report['order'][ordinal-1]
        if (run.get('run') != ordinal or run.get('label') != label
                or run.get('source_sha256') != report['source_sha256'][label]):
            raise ValueError('A/B run order/source mismatch')
        if not run.get('available'):
            continue
        cases = run.get('cases', [])
        indices = case_indices(request)
        if (run.get('status') != 'accepted' or len(cases) != len(indices)
                or any(c.get('status') != 'accepted' for c in cases)
                or [c.get('original_index') for c in cases] != indices
                or set(run['device_samples']) != {str(i) for i in range(1,len(indices)+1)}):
            raise ValueError('A/B run is not a complete pass')
        metric = run['metric']
        if not metric.get('protocol_id') or not metric.get('measurement_protocol', {}).get('exact'):
            raise ValueError('A/B measurement protocol unverified')
        current = (metric['protocol_id'], metric['measurement_protocol'])
        if protocol is not None and current != protocol:
            raise ValueError('A/B measurement protocol changed')
        protocol = current
        for i in range(1,len(indices)+1):
            samples = run['device_samples'][str(i)]
            if len(samples) != 3 or len({s['logical_launch_index'] for s in samples}) != 3:
                raise ValueError('A/B measured sample count mismatch')
            for sample in samples:
                values = sample['task_durations_us']
                if (not values or any(type(v) not in (int,float) or not math.isfinite(v) or v <= 0 for v in values)
                        or not math.isclose(math.fsum(values), sample['device_us'], rel_tol=1e-12, abs_tol=1e-6)):
                    raise ValueError('A/B device sample mismatch')
            observed = metric['cases'][i-1]
            if observed.get('original_index') != indices[i-1] or not math.isclose(
                    statistics.fmean(s['device_us'] for s in samples), observed['steady_state_mean_us'],
                    rel_tol=1e-12, abs_tol=1e-6):
                raise ValueError('A/B device mean mismatch')
    complete = len(runs) == 4*request['cycles'] and all(r.get('available') for r in runs)
    if (report.get('status') == 'available') != complete or report.get('paired') != complete:
        raise ValueError('A/B completeness mismatch')
    if report.get('cases') != summarize(report):
        raise ValueError('A/B summary differs from samples')


class BudgetExpired(BaseException):
    """Bypass adapter exception-to-metric conversion, including cache lock waits."""


class RetainedLease:
    """Inner evaluations may release; only the outer diagnostic owns release."""
    def __init__(self, lease):
        self.lease = lease

    def __getattr__(self, name):
        return getattr(self.lease, name)

    def close(self):
        pass


def evaluate_ab(worker, adapter, pack, job, sources, request, emit):
    budget = validate_request(pack, request)
    selected = case_indices(budget)
    hashes = {k:hashlib.sha256(v).hexdigest() for k,v in sources.items()}
    if hashes != {k:v['source_sha256'] for k,v in request['sources'].items()}:
        raise ValueError('A/B source SHA mismatch')
    report = dict(schema_version=1, status='unavailable', request=request, source_sha256=hashes,
                  same_source=hashes['A'] == hashes['B'], order=['A','B','B','A']*budget['cycles'],
                  paired=False, runs=[], cases=[], dataset=None,
                  timing='msprof measured invocation device task-duration sum; 3 samples per run',
                  difference='B - A; negative means B faster; adjacent run means, not individual invocation pairs',
                  limits='Local diagnostic only; cache/thermal state uncontrolled; no official timing inference')
    started = time.monotonic()
    def expire(*args):
        raise BudgetExpired()
    previous = signal.signal(signal.SIGALRM, expire)
    signal.setitimer(signal.ITIMER_REAL, budget['budget_seconds'])
    status = 'infrastructure_error'
    lease = DeviceLease(worker.LOCK, emit)
    try:
        with lease:
            frozen = None
            if request.get('custom_case_indices') is not None:
                if __package__:
                    from .custom_ab import freeze, evaluate_view
                else:
                    from custom_ab import freeze, evaluate_view
                frozen = freeze(worker, pack, job, request)
            for ordinal, label in enumerate(report['order'], 1):
                identity = uuid.uuid4().hex
                slot = worker.JOBS/identity
                slot.mkdir(mode=0o755)
                source = sources[label]
                (slot/'kernel.asc').write_bytes(source)
                work = slot/'work'
                work.mkdir(mode=0o700); worker.own(work)
                (work/'tmp').mkdir(mode=0o700); worker.own(work/'tmp')
                env = worker.environment(work)
                env['OJ_JOB_ROOT'] = str(slot)
                run = dict(run=ordinal, label=label, remote_job=str(slot), available=False,
                           source_sha256=hashes[label], cases=[])
                report['runs'].append(run)
                def publish(kind, **fields):
                    if kind == 'status':
                        emit(kind, **dict(fields, message=f'A/B {ordinal}/{len(report["order"])} {label}: '+fields.get('message','')))
                    elif kind == 'dataset':
                        if report['dataset'] is None:
                            report['dataset'] = fields['dataset']
                        elif fields['dataset'] != report['dataset']:
                            raise ValueError('Dataset changed between A/B runs')
                    elif kind == 'case':
                        run['cases'].append(fields['case'])
                    elif kind == 'metrics' and fields['metrics']['group'] == 'PipeUtilization':
                        run['metric'] = fields['metrics']
                    elif kind == 'finished':
                        run['status'] = fields['status']
                        run['evaluation'] = fields['evaluation']
                groups = worker.CONFIG.get('profiling_plans', {}).get(pack['pack_sha256'])
                execution = Execution(worker.ROOT, slot, work, pack, source, env, worker.run, worker.own,
                                      'timing', RetainedLease(lease), groups)
                relay = lambda kind, **fields:execution.emit(publish, kind, **fields)
                if frozen is not None:
                    evaluate_view(adapter, frozen, slot, work, env, execution.run, relay, identity, started, hashes[label])
                else:
                    evaluate_local_cases(adapter, pack, selected, slot, work, None, env,
                        execution.run, relay, identity, started, hashes[label])
                if (run.get('status') != 'accepted' or len(run['cases']) != len(selected)
                        or any(c['status'] != 'accepted' for c in run['cases'])):
                    raise ValueError('A/B rerun correctness failed; diagnostic stopped')
                samples = raw_device_samples(run.get('metric', {}), work,
                                              worker.ROOT/'problems'/pack['id']/'profile_metrics.py')
                if sorted(samples) != list(range(1, len(selected)+1)):
                    raise ValueError('Device sample case inventory mismatch')
                if ordinal > 1 and (run['metric']['protocol_id'] != report['runs'][0]['metric']['protocol_id']
                        or run['metric']['measurement_protocol'] != report['runs'][0]['metric']['measurement_protocol']):
                    raise ValueError('Measurement protocol changed between A/B runs')
                run['device_samples'] = {str(k):v for k,v in samples.items()}
                run['available'] = True
                run['elapsed_seconds'] = time.monotonic()-started
                (job/'local-ab-partial.json').write_text(json.dumps(report, allow_nan=False))
            report.update(status='available', paired=True)
            status = 'accepted'
    except BudgetExpired:
        report['reason'] = 'Explicit wall-clock budget exhausted; no automatic retry'
        status = 'time_limit'
    except Exception as error:
        report['reason'] = str(error)[:1000]
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
        lease.close()
    report['elapsed_seconds'] = time.monotonic()-started
    report['scheduling'] = lease.snapshot()
    report['cases'] = summarize(report)
    (job/'local-ab.json').write_text(json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2))
    if report['dataset'] is not None:
        emit('dataset', dataset=report['dataset'])
    emit('local_ab', report=report)
    if status == 'accepted':
        for case in report['cases']:
            emit('case', case=dict(index=case['index'], original_index=case['original_index'],
                                  status='accepted', time_us=None, message='Local A/B diagnostic; see local_ab'))
    emit('finished', status=status, elapsed_seconds=report['elapsed_seconds'],
         message='本地 A/B 诊断完成；不计排名' if status == 'accepted' else report.get('reason', 'Diagnostic unavailable'))

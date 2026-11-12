"""Per-problem local performance calibration. Never invent official scores."""
import json
import math
import threading
import time
from .problem import problem, DEFAULT_PROBLEM
from .calibration import estimate, protocol, PROTOCOL_KEYS, positive
from .validation import dataset_validation, protocol_environment, reference_passes

_BASELINE_LOCK = threading.Lock()


def baseline_path(directory, identity):
    # Registry validates ID before it becomes part of a filesystem path.
    problem(identity)
    return directory / 'baselines' / identity / 'current.json'


def maybe_save_baseline(directory, identity, result):
    with _BASELINE_LOCK:
        _save_baseline(directory, identity, result)


def _save_baseline(directory, identity, result):
    spec = result.get('problem') or problem(result.get('problem_id', DEFAULT_PROBLEM))
    current_spec = problem(spec['id'])
    dataset = dataset_validation(result, current_spec)
    if dataset is not None and not dataset['current']:
        return
    environment = protocol_environment(result, current_spec)
    calibration = spec.get('calibration') or {}
    expected_count = len(spec['cases'])
    if (result.get('status') != 'accepted'
            or (calibration.get('source_sha256') and result.get('source_sha256') != calibration['source_sha256'])
            or result.get('suite') != 'official_shapes'
            or valid_times(result.get('cases'), expected_count, 'time_us') is None
            or any(c.get('status') != 'accepted' for c in result['cases'])
            or not reference_passes(result['cases'],spec)
            or environment.get('problem_sha256') != current_spec['pack_sha256']
            or not result.get('dataset', {}).get('sha256')
            or any(environment.get(k) is None for k in PROTOCOL_KEYS)):
        return
    path = baseline_path(directory, spec['id'])
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        old = json.loads(path.read_text(encoding='utf-8'))
        old_dataset = dataset_validation(old['result'], current_spec)
        old_current = dataset is None or (old_dataset is not None and old_dataset['current'])
        # Explicit owner selection stays fixed, even if another calibration run finishes.
        if old.get('selection') == 'manual':
            if dataset is None:
                return
            if old_current and protocol(old['result'], current_spec) == protocol(result, current_spec):
                return
        # Keep a fixed baseline for each protocol, but permit a complete device
        # profile to replace a run whose profiler failed during provisioning.
        if old_current and protocol(old['result'], current_spec) == protocol(result, current_spec):
            improves_profile = (profile_times(old['result'], expected_count) is None
                                and profile_times(result, expected_count) is not None)
            if not improves_profile:
                return
    payload = dict(submission_id=identity, problem_id=spec['id'], selection='automatic', result=result, **calibration)
    _replace_baseline(path, payload)


def _replace_baseline(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        previous = path.read_bytes()
        old = json.loads(previous)
        archived = path.parent / (old['submission_id'] + '.json')
        pending = archived.with_suffix('.tmp')
        pending.write_bytes(previous)
        pending.replace(archived)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def valid_times(cases, count, field):
    if (not count or not isinstance(cases, list) or len(cases) != count
            or any(not isinstance(c, dict) for c in cases)):
        return None
    if [c.get('index') for c in cases] != list(range(1, count + 1)):
        return None
    values = [c.get(field) for c in cases]
    return values if all(positive(v) for v in values) else None


def select_baseline(directory, row):
    """Select an already evaluated default-suite run; never run or change a job."""
    spec = problem(row['problem_id'])
    result, count = row['result'], len(spec['cases'])
    dataset = dataset_validation(result, spec)
    if dataset is not None and not dataset['current']:
        raise ValueError(dataset['note'])
    environment = protocol_environment(result, spec)
    if row['status'] != 'accepted' or result.get('status') != 'accepted':
        raise ValueError('只能选择全部通过的提交作为基线')
    if result.get('suite') != 'official_shapes':
        raise ValueError('自定义数据不能作为默认测试集的基线')
    if (result.get('problem_id') != spec['id']
            or environment.get('problem_sha256') != spec['pack_sha256']):
        raise ValueError('提交的题目版本与当前版本不同，请选择当前版本的提交')
    cases = result.get('cases', [])
    if not reference_passes(cases,spec):
        raise ValueError('提交尚未通过当前参考输出兼容性检查，不能作为基线')
    if (valid_times(cases, count, 'time_us') is None
            or any(c.get('status') != 'accepted' for c in cases)):
        raise ValueError('基线必须具备全部测试点的通过结果和有效计时')
    if (not result.get('dataset', {}).get('sha256')
            or any(environment.get(k) is None for k in PROTOCOL_KEYS)):
        raise ValueError('提交缺少完整的数据集或评测环境标识')
    payload = dict(submission_id=row['id'], problem_id=spec['id'], label=row['label'],
                   selection='manual', selected_at=time.time(), result=result)
    # Official timings belong to a particular source, not any newly chosen baseline.
    calibration = spec.get('calibration') or {}
    if result.get('source_sha256') == calibration.get('source_sha256'):
        payload.update(calibration)
    with _BASELINE_LOCK:
        _replace_baseline(baseline_path(directory, spec['id']), payload)
    return dict(problem_id=spec['id'], baseline_id=row['id'], label=row['label'],
                selection='manual', comparison_only=True,
                method='device' if profile_times(result, count) is not None else 'event')


def profile_times(result, count):
    metric = result.get('metrics', {}).get('PipeUtilization', {})
    if metric.get('status') != 'available':
        return None
    return valid_times(metric.get('cases'), count, 'op_time_us')


def comparison(directory, result):
    identity = result.get('problem_id', DEFAULT_PROBLEM)
    spec = problem(identity) if identity == 'indexreduce' else None
    dataset = dataset_validation(result, spec)
    if dataset is not None and not dataset['current']:
        return dict(available=False, requires_reevaluation=True, reason=dataset['note'])
    try:
        path = baseline_path(directory, identity)
    except ValueError:
        return None
    if result.get('status') != 'accepted' or result.get('suite') != 'official_shapes' or not path.exists():
        return None
    baseline = json.loads(path.read_text(encoding='utf-8'))
    baseline_dataset = dataset_validation(baseline['result'], spec)
    if baseline_dataset is not None and not baseline_dataset['current']:
        return dict(available=False, reason='当前本地数据版本尚未建立匹配的比较基线')
    if protocol(baseline['result'], spec) != protocol(result, spec):
        candidates = list(path.parent.glob('*.json')) + list((path.parent/'history').glob('*.json'))
        for archived in candidates:
            old = json.loads(archived.read_text(encoding='utf-8'))
            old_dataset = dataset_validation(old['result'], spec)
            if old_dataset is not None and not old_dataset['current']:
                continue
            if protocol(old['result'], spec) == protocol(result, spec):
                baseline = old
                break
    control = baseline['result']
    if baseline['problem_id'] != identity:
        return {'available': False, 'reason': '题目与基线不同，不能比较'}
    candidate_environment = protocol_environment(result, spec)
    control_environment = protocol_environment(control, spec)
    for key in ('harness_sha256', 'cores', 'environment', 'problem_sha256', 'device'):
        if candidate_environment.get(key) != control_environment.get(key):
            return {'available': False, 'reason': '题目版本、运行环境或 harness 与固定基线不同，不能比较'}
    if result.get('dataset', {}).get('sha256') != control.get('dataset', {}).get('sha256'):
        return {'available': False, 'reason': '数据集与固定基线不同，不能比较'}
    count = len(control['cases'])
    before, after = profile_times(control, count), profile_times(result, count)
    method = 'profiler op_summary，中位数；包含 profiler 扰动'
    if before is None or after is None:
        before = [c['time_us'] for c in control['cases']]
        after = [c['time_us'] for c in result['cases']]
        method = 'ACL event 批量跨度；包含提交间隙，短 kernel 波动较大'
    else:
        mean_aggregation = 'arithmetic_mean_of_measured_invocation_task_duration_sums'
        means = [r.get('metrics', {}).get('PipeUtilization', {}).get('measurement_protocol', {}).get(
            'primary_aggregation') == mean_aggregation for r in (control, result)]
        if means[0] != means[1]:
            return {'available': False, 'reason': '设备耗时聚合方式与固定基线不同，不能比较算术均值和中位数'}
        if means[1]:
            method = 'profiler op_summary，完整调用设备任务耗时之和的算术均值；包含 profiler 扰动'
    if not count or len(before) != count or len(after) != count or any(not isinstance(t, (float,int)) or not math.isfinite(t) or t <= 0 for t in before + after):
        return {'available': False, 'reason': '缺少完整有效计时'}
    ratios = [a/b for a,b in zip(before,after)]
    online = baseline.get('online_us') or []
    calibration = estimate(directory, result, count)
    calibrated = {c['index']: c for c in calibration.get('per_case', [])}
    return dict(available=True, problem_id=identity, index=100*math.exp(sum(math.log(r) for r in ratios)/count),
                baseline_id=baseline['submission_id'], method=method,
                per_case=[dict(speedup=r, baseline_us=before[i-1], candidate_us=after[i-1],
                               legacy_estimated_online_us=online[i-1]/r if len(online)==count else None,
                               **calibrated.get(i, dict(index=i, estimated_online_us=None))) for i,r in enumerate(ratios,1)],
                calibration={k:v for k,v in calibration.items() if k!='per_case'},
                online_url=baseline.get('online_url'),
                limitation='指数 = 100 × 本题各点加速比的几何平均；不是官方分数。线上实测与估时分开显示，估时不参与此指数。')

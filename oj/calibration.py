"""Source-deduplicated, protocol-bound online timing observations and estimates."""
import json
import math
import statistics

FEATURES = ('aiv_vec_ratio', 'aiv_scalar_ratio', 'aiv_mte2_ratio', 'aiv_mte3_ratio')
PROTOCOL_KEYS = ('harness_sha256', 'cores', 'environment', 'problem_sha256', 'device')


def protocol(result, spec=None):
    from .validation import protocol_environment, VERSIONED_DATASET_PROBLEMS, TIMING_COMPATIBLE_PROBLEMS
    if result.get('problem_id') in VERSIONED_DATASET_PROBLEMS | TIMING_COMPATIBLE_PROBLEMS and spec is None:
        from .problem import problem
        spec = problem(result['problem_id'])
    environment = protocol_environment(result, spec)
    value = dict(problem_id=result.get('problem_id', 'cum_sum'),
                 environment={k: environment.get(k) for k in PROTOCOL_KEYS},
                 dataset_sha256=result.get('dataset', {}).get('sha256'))
    if (result.get('problem_id') in VERSIONED_DATASET_PROBLEMS and spec
            and isinstance(spec.get('dataset_revision'), str) and spec['dataset_revision']):
        value.update(dataset_version=result.get('dataset', {}).get('version'),
                     dataset_profile_sha256=result.get('dataset', {}).get('profile_sha256'))
    return value


def positive(value):
    return type(value) in (float, int) and math.isfinite(value) and value > 0


def measured_cases(result, count):
    metric = result.get('metrics', {}).get('PipeUtilization', {})
    cases = metric.get('cases', [])
    if (metric.get('status') != 'available' or len(cases) != count
            or [c.get('index') for c in cases] != list(range(1, count + 1))):
        return None
    output = []
    for case in cases:
        values = [case.get('counters', {}).get(key, {}).get('median') for key in FEATURES]
        if (not positive(case.get('op_time_us'))
                or any(type(v) not in (float, int) or not math.isfinite(v) or not 0 <= v <= 1 for v in values)):
            return None
        output.append(dict(local_us=case['op_time_us'], features=values))
    return output


def predict_case(anchors, case_index, measured, exclude_sha=None, method='profile_neighbors'):
    available = [a for a in anchors if a['source_sha256'] != exclude_sha]
    if len(available) < 3:
        return None
    # Fixed rule, not fitted to individual test points: nearby pipeline mix and
    # timing scale identify similar implementations; no kernel-name matching.
    candidates = []
    for anchor in available:
        sample = anchor['cases'][case_index]
        distance = math.sqrt(sum((x-y)**2 for x,y in zip(measured['features'], sample['features'])))
        distance += .20 * abs(math.log(measured['local_us'] / sample['local_us']))
        candidates.append((distance, anchor['source_sha256'], sample))
    candidates.sort(key=lambda x: (x[0], x[1]))
    if method == 'median_ratio':
        selected = candidates
    else:
        selected = [x for x in candidates[:5] if x[0] <= candidates[0][0] + .20]
    factors = [sample['online_us'] / sample['local_us'] for _,_,sample in selected]
    center = measured['local_us'] * statistics.median(factors)
    return dict(estimate_us=center, neighbor_sources=[sha for _,sha,_ in selected],
                neighbors=len(selected), nearest_distance=candidates[0][0],
                factor_min=min(factors), factor_max=max(factors))


def validation(anchors, count, method='profile_neighbors'):
    """Each fold excludes every local/online repeat of the held-out source."""
    cases = []
    for i in range(count):
        folds = []
        for anchor in anchors:
            measured = anchor['cases'][i]
            predicted = predict_case(anchors, i, measured, anchor['source_sha256'], method)
            if predicted is not None:
                actual = measured['online_us']
                folds.append(dict(source_sha256=anchor['source_sha256'], actual_us=actual,
                                  predicted_us=predicted['estimate_us'],
                                  relative_error=predicted['estimate_us']/actual-1))
        absolute = sorted(abs(f['relative_error']) for f in folds)
        cases.append(dict(index=i+1, folds=folds, samples=len(folds),
                          median_absolute_relative_error=statistics.median(absolute) if absolute else None,
                          max_absolute_relative_error=max(absolute) if absolute else None))
    errors = [abs(f['relative_error']) for c in cases for f in c['folds']]
    return dict(method=method, split='leave_one_source_out', cases=cases,
                mean_absolute_relative_error=statistics.mean(errors) if errors else None,
                max_absolute_relative_error=max(errors) if errors else None)


def estimate(directory, result, count):
    if result.get('suite') in {'local_subset','structured_regression'}:
        return dict(available=False, reason='本地子集或结构化正确性回归不参与校准或线上估时')
    identity = result.get('problem_id', 'cum_sum')
    from .validation import dataset_validation
    dataset = dataset_validation(result)
    if dataset is not None and not dataset['current']:
        return dict(available=False, requires_reevaluation=True, reason=dataset['note'])
    # Called only after registry validation by scoring.comparison.
    path = directory / 'calibrations' / identity / 'current.json'
    if not path.exists():
        return dict(available=False, reason='缺少多版本校准样本；已停止显示单基线线上估时')
    try:
        model = json.loads(path.read_text(encoding='utf-8'))
        if model.get('protocol') != protocol(result):
            for archived in (path.parent / 'history').glob('*.json'):
                previous = json.loads(archived.read_text(encoding='utf-8'))
                if previous.get('protocol') == protocol(result):
                    model = previous
                    break
    except (OSError, ValueError):
        return dict(available=False, reason='校准档案暂不可用')
    if model.get('schema_version') != 1 or model.get('protocol') != protocol(result) or model.get('case_count') != count:
        return dict(available=False, reason='校准样本与当前题目、数据或测量协议不一致')
    measured = measured_cases(result, count)
    if measured is None:
        return dict(available=False, reason='缺少完整设备计时或流水线指标，不能套用校准模型')
    source = result.get('source_sha256')
    observations = [o for o in model.get('observations', []) if o['source_sha256'] == source]
    anchors = model['anchors']
    per_case = []
    for i in range(count):
        predicted = predict_case(anchors, i, measured[i], source) if len(anchors) >= 4 else None
        reported = [o['online_us'][i] for o in observations]
        row = dict(index=i+1, observed_online_us=statistics.median(reported) if reported else None,
                   observed_online_records=[dict(official_id=o.get('official_id'), url=o.get('url'),
                       provenance=o['provenance']) for o in observations])
        if predicted:
            # Spread + worst held-out log residual. Empirical envelope only,
            # deliberately not labelled a probabilistic confidence interval.
            folds = model['validation']['cases'][i]['folds']
            logs = [abs(math.log(f['actual_us']/f['predicted_us'])) for f in folds]
            margin = max([math.log(1.10)] + logs)
            extrapolated = predicted['nearest_distance'] > .50
            if extrapolated:
                margin = max(margin, math.log(1.50))
            low = min(predicted['estimate_us'], measured[i]['local_us']*predicted['factor_min']) / math.exp(margin)
            high = max(predicted['estimate_us'], measured[i]['local_us']*predicted['factor_max']) * math.exp(margin)
            row.update(estimated_online_us=predicted['estimate_us'], estimated_online_range_us=[low, high],
                       calibration_neighbors=predicted['neighbors'], extrapolated=extrapolated,
                       validation_samples=len(folds), validation_max_error=model['validation']['cases'][i]['max_absolute_relative_error'])
        else:
            row.update(estimated_online_us=None, estimated_online_range_us=None, calibration_neighbors=0)
        per_case.append(row)
    return dict(available=True, method='profile_neighbors', unique_sources=len(anchors),
                generated_at=model['generated_at'], validation=model['validation_summary'], per_case=per_case,
                limitation='估时排除当前源码，用相近流水线指标的历史版本校准；区间为经验误差范围，不保证覆盖。历史版本高度相关，新算法仍可能超出范围。')

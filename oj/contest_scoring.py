"""Official time-ratio formula, with explicit provenance for local simulations."""
import json
import math

from .calibration import positive
from .config import ROOT
from .problem import problem

RULES_PATH = ROOT/'config/contest_scoring.json'


def scoring_rules(identity):
    spec = problem(identity)
    config = json.loads(RULES_PATH.read_text(encoding='utf-8'))
    local = config.get('local_suites', {}).get(identity)
    if local and synthetic_suite(spec):
        groups = [dict(id=key, label=label, case_indices=[c['index'] for c in spec['cases']
                  if c.get('specification_origin') == origin]) for key, label, origin in (
            ('known', '公开规格', 'confirmed_official_shape'),
            ('diagnostic', '诊断规格', 'diagnostic_shape'),
            ('synthetic', '推测压力', 'synthetic_guess'))]
        groups = [g for g in groups if g['case_indices']]
        if sum(len(g['case_indices']) for g in groups) != len(spec['cases']):
            return dict(available=False, problem_id=identity, reason='本地测试点来源分组不完整')
        return dict(available=True, problem_id=identity, formula_id=config['formula_id'],
            formula=config['formula'], point_formula='100 / (1 + log(time / TBest) / log(1.5))',
            aggregation='arithmetic_mean', max_score=100, partial_credit=local['partial_credit'],
            failed_case_score=0, reference_kind='local_tbest', score_scope='synthetic_local_suite',
            groups=groups, official_tbest_snapshot=None,
            local_reference='同版本默认数据、同评测协议与计时方式的历史通过点逐点最优；错误点不更新 TBest。',
            baseline_role='全通过基线仅用于性能对比；没有基线时可用完整测完的部分通过记录建立排名协议。',
            sources=[config['formula_source'], local['problem_url']],
            verification=dict(formula_source=config['formula_source'], score_mode=local['score_mode'],
                              official_score_validated=False),
            limitation='本地合成数据模拟分，不是官方成绩或隐藏点估分。诊断规格的隐藏点顺序未知，输入值是合成的。'
                       '总分为全部点等权均分，失败点为 0；新 TBest 会更新历史分数。')
    setting = config['problems'].get(identity)
    if setting is None or len(setting['case_ids']) != len(spec['cases']):
        return dict(available=False, problem_id=identity, reason='本题尚未配置已核实的官方计分规则')
    return dict(available=True, problem_id=identity, formula_id=config['formula_id'],
                formula=config['formula'], point_formula='100 / (1 + log(time / TBest) / log(1.5))',
                aggregation='arithmetic_mean', max_score=100, partial_credit=setting['partial_credit'],
                failed_case_score=0, reference_kind='local_tbest',
                local_reference='同题、同默认数据、同评测协议、同计时方式的历史通过点，逐点取最优耗时；不是某一份提交。',
                baseline_role='手动基线仅用于性能对比；相对基线指数不用于本榜排名。',
                official_tbest_snapshot=dict(times_us=setting['official_tbest_us'],
                    captured_at=setting.get('verified_at',config['verified_at']),source=setting['ranking_url']),
                sources=[config['formula_source'],setting['ranking_url']],
                verification=dict(checked_at=setting.get('verified_at',config['verified_at']),
                    matching_official_rows=len(setting['validation']),precision='2 decimal places'),
                limitation='本地模拟分使用官方公式和本地 TBest；不把本地 NPU 用时直接套入线上 TBest，不等于线上实际分数。新最优耗时会使历史模拟分变化。')


def synthetic_suite(spec):
    return spec.get('data_evidence', {}).get('status') in {'confirmed_shapes_plus_guessed_stress_cases', 'diagnostic_shapes_synthetic_values'}


def score_breakdown(scored, rules):
    groups = rules.get('groups')
    if not groups:
        return {}
    result = {}
    for group in [dict(id='all', label='全部本地点', case_indices=[c['index'] for c in scored['per_case']]), *groups]:
        cases = [c for c in scored['per_case'] if c['index'] in group['case_indices']]
        count = len(cases)
        total = sum(c['score'] for c in cases)
        result[group['id']] = dict(label=group['label'], case_indices=group['case_indices'],
            passed=sum(c['passed'] for c in cases), total=count, score_sum=total, max_score_sum=100*count,
            mean_score=total/count if count else None, max_mean_score=100,
            contribution=sum(c['contribution'] for c in cases), weight=count/scored['total'],
            reference_coverage=sum(positive(c['tbest_us']) for c in cases))
    return dict(score_breakdown=result)


def point_score(duration, best):
    if not positive(duration) or not positive(best):
        raise ValueError('计分需要有效的正数耗时')
    # A new fastest run updates TBest; its score is 100, never >100.
    ratio = max(1.0, duration / best)
    return 100.0 / (1.0 + math.log(ratio) / math.log(1.5))


def formula_score(times, best, partial_credit=True):
    if not times or len(times) != len(best):
        raise ValueError('计分测试点数量不匹配')
    per_case = []
    all_passed = all(positive(t) for t in times)
    for index, (duration, reference) in enumerate(zip(times,best),1):
        passed = positive(duration)
        score = point_score(duration,reference) if passed and (partial_credit or all_passed) else 0.0
        per_case.append(dict(index=index,passed=passed,time_us=duration,tbest_us=reference,
                             score=score,contribution=score/len(times)))
    return dict(score=sum(c['contribution'] for c in per_case),max_score=100,
                passed=sum(c['passed'] for c in per_case),total=len(times),per_case=per_case)


def measured_times(cases, metric, count, method):
    if (not isinstance(cases,list) or len(cases)!=count or any(not isinstance(c,dict) for c in cases)
            or [c.get('index') for c in cases]!=list(range(1,count+1))):
        return None
    terminal = {'accepted','wrong_answer','runtime_error','time_limit','illegal'}
    if any(c.get('status') not in terminal for c in cases):
        return None
    profiles = metric.get('cases') or []
    if method=='device':
        if metric.get('status')!='available' or len(profiles)!=count:
            return None
        if any(not isinstance(c,dict) for c in profiles) or [c.get('index') for c in profiles]!=list(range(1,count+1)):
            return None
    values = []
    for index, case in enumerate(cases):
        value = (profiles[index].get('op_time_us') if method=='device' else case.get('time_us')) if case['status']=='accepted' else None
        if case['status']=='accepted' and not positive(value):
            return None
        values.append(value)
    return values

"""Versioned operator guidance, independent of executable problem-pack hashes."""
import hashlib
import json
from pathlib import Path
import re

from .config import ROOT
from .problem import registry
from .contest_scoring import scoring_rules
from .optimization_skill import skill_hint

DIRECTORY = ROOT / 'knowledge'
SECTIONS = {'all', 'semantics', 'pitfalls', 'optimization', 'workflow'}


def _read(identity):
    if not isinstance(identity, str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,63}', identity):
        raise ValueError('题目 ID 无效')
    path = DIRECTORY / (identity + '.json')
    if path.is_symlink() or path.resolve().parent != DIRECTORY.resolve() or not path.is_file():
        raise ValueError('尚无此题的知识指南：' + identity)
    raw = path.read_bytes()
    value = json.loads(raw)
    if value.get('id') != identity or value.get('schema_version') != 1:
        raise ValueError('知识指南格式错误')
    return value, hashlib.sha256(raw).hexdigest()


def catalog():
    installed = {p['id'] for p in registry.listing()}
    result = []
    for path in sorted(DIRECTORY.glob('*.json')):
        if path.stem == 'common':
            continue
        value, digest = _read(path.stem)
        result.append(dict(problem_id=value['id'], name=value['name'], updated=value['updated'],
                           installed=value['id'] in installed, guide_sha256=digest,
                           tool='get_problem_guide'))
    return result


def guide(problem_id='cum_sum', section='all'):
    if section not in SECTIONS:
        raise ValueError('未知指南章节')
    value, digest = _read(problem_id)
    common, common_digest = _read('common')
    try:
        spec = registry.get(problem_id)
    except ValueError:
        spec = None
    result = dict(problem_id=problem_id, name=value['name'], updated=value['updated'],
                  official_url=value['official_url'], section=section,
                  installed=spec is not None, guide_sha256=digest, common_sha256=common_digest,
                  evidence_levels=common['evidence_levels'])
    result['optimization_skill'] = skill_hint(problem_id)
    if problem_id == 'pdist' and section in {'all', 'workflow'}:
        from .pdist_regression import access_info
        result['pdist_regression'] = access_info()
        if spec is not None:
            from .pdist_subset import access_info as subset_access_info
            result['pdist_subset'] = subset_access_info(spec)
    if problem_id == 'topk' and spec is not None and section in {'all', 'workflow'}:
        result['local_subset'] = dict(
            parameter='local_case_indices', example=[1, 3], index_base=1,
            case_count=len(spec['cases']), order='ascending original index',
            dataset_version=spec['dataset_revision'], profile_sha256=spec['dataset_profile_sha256'],
            legacy_selector='cann-oj-subset:1,3',
            checks='Unchanged stable TopK values and exact int32 indices; installed x/y/index bytes verified.',
            provenance='result.local_case_indices; dataset.cases[].original_index/generation/sha256; '
                       'execution_target and environment; source_sha256; cases[].time_us; '
                       'metrics.PipeUtilization.cases[].original_index and phase_metrics.',
            paired=dict(tool='submit_local_ab', sources='Two accepted LOCAL TopK IDs, same current pack, '
                        'official_shapes or local_subset covering the selection; no custom IDs.',
                        local_case_indices=[1, 3], cycles=1, budget_seconds=300,
                        report='result.local_ab: A-B-B-A runs, both source SHAs, raw measured device samples and B-A pairs'),
            eligibility='All selections, including all 15 indices, are excluded from scores, leaderboard, '
                        'case bests, baseline, calibration and official validation. Local synthetic cases, not official inputs.')
    # Evaluation always uses the registered pack; pending recovery notes cannot
    # silently replace a dataset or invalidate jobs already in the queue.
    result['evaluation'] = (dict(available=True, problem_version=spec['version'],
                                problem_sha256=spec['pack_sha256'], cases=spec['cases'],
                                dataset=spec.get('dataset'), protocol=spec.get('protocol'))
                            if spec else dict(available=False, reason='本题尚未启用本地评测；可以读取官方题面、模板并使用官方工程提交接口。'))
    for name in SECTIONS - {'all'}:
        if section in {'all', name}:
            result[name] = dict(common=common.get(name, []), problem=value.get(name, []))
    if section == 'all':
        result['scoring'] = scoring_rules(problem_id) if spec else dict(available=False)
        result['sources'] = value.get('sources', [])
        result['data_evidence'] = value.get('data_evidence', {})
    return result

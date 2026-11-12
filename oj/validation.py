"""Explain historical and current validation without rewriting measured results."""
from .problem import problem

VERSIONED_DATASET_PROBLEMS = {'indexreduce', 'copysign', 'erfinv', 'pdist'}
TIMING_COMPATIBLE_PROBLEMS = {'erf'}


def protocol_environment(result, spec=None):
    environment = result.get('environment') or {}
    if result.get('problem_id') in TIMING_COMPATIBLE_PROBLEMS:
        spec = spec or problem(result['problem_id'])
        previous = environment.get('problem_sha256')
        dataset = result.get('dataset') or {}
        # Administrator-verified CPU-only releases may share rankings. Preserve
        # every original hash in stored results; normalize only comparison keys.
        if (result.get('suite') == 'official_shapes'
                and previous in spec.get('timing_compatible_pack_sha256', [])
                and result.get('problem_sha256') == previous
                and environment.get('harness_sha256') == previous
                and dataset.get('version') == spec.get('dataset_revision')
                and dataset.get('profile_sha256') == spec.get('dataset_profile_sha256')
                and dataset.get('sha256')):
            environment = dict(environment, problem_sha256=spec['pack_sha256'],
                               harness_sha256=spec['pack_sha256'])
    if result.get('problem_id') not in VERSIONED_DATASET_PROBLEMS:
        return environment
    spec = spec or problem(result['problem_id'])
    revision = spec.get('dataset_revision')
    if isinstance(revision, str) and revision and not environment.get('problem_sha256'):
        return dict(environment, problem_sha256=result.get('problem_sha256'))
    return environment


def dataset_validation(result, spec=None, *, require_dataset=True):
    """Opt-in local dataset gate; historical measurements stay intact."""
    if result.get('problem_id') not in VERSIONED_DATASET_PROBLEMS:
        return None
    spec = spec or problem(result['problem_id'])
    if result.get('suite') == 'structured_regression':
        from remote.pdist_structured import SUITE, dataset_error, suite_info
        mismatches = []
        if result.get('regression_suite') != SUITE or result.get('regression_spec') != suite_info():
            mismatches.append('regression_recipe')
        if protocol_environment(result, spec).get('problem_sha256') != spec['pack_sha256']:
            mismatches.append('problem_sha256')
        if require_dataset and dataset_error(result.get('dataset')):
            mismatches.append('structured_dataset')
        return dict(current=not mismatches, requires_reevaluation=bool(mismatches), mismatches=mismatches,
                    note='独立结构化正确性回归；不计性能排名、基线、校准或官方上传门禁。'
                         + ('回归配方或数据不匹配，需重新评测。' if mismatches else ''))
    if result.get('suite') == 'local_ab' and result.get('custom_case_indices') is not None:
        from remote.custom_ab import dataset_error
        mismatches = []
        if protocol_environment(result, spec).get('problem_sha256') != spec['pack_sha256']:
            mismatches.append('problem_sha256')
        if require_dataset and dataset_error(result.get('dataset'), result.get('local_ab_request', {})):
            mismatches.append('custom_dataset')
        return dict(current=not mismatches, requires_reevaluation=bool(mismatches), mismatches=mismatches,
                    note='同数据 custom A/B 诊断；不计 canonical 排名、基线、校准或官方上传门禁。')
    revision = spec.get('dataset_revision')
    if spec.get('id') != result['problem_id'] or not isinstance(revision, str) or not revision:
        return None
    dataset = result.get('dataset') or {}
    observed_pack = protocol_environment(result, spec).get('problem_sha256')
    expected = dict(problem_sha256=spec['pack_sha256'], dataset_version=revision,
                    profile_sha256=spec.get('dataset_profile_sha256'))
    observed = dict(problem_sha256=observed_pack, dataset_version=dataset.get('version'),
                    profile_sha256=dataset.get('profile_sha256'))
    mismatches = []
    if observed_pack != expected['problem_sha256']:
        mismatches.append('problem_sha256')
    if require_dataset and result.get('suite') != 'custom':
        if observed['dataset_version'] != revision:
            mismatches.append('dataset_version')
        if expected['profile_sha256'] and observed['profile_sha256'] != expected['profile_sha256']:
            mismatches.append('profile_sha256')
    current = not mismatches
    return dict(current=current, requires_reevaluation=not current, expected=expected,
                observed=observed, mismatches=mismatches,
                note='当前本地数据与测量协议匹配。' if current else
                     '本地数据或测量协议已更新；此记录保留作历史参考，需重新评测，不计当前分数、比较或线上估时。')


def reference_passes(cases, spec):
    required=spec.get('validation', {}).get('reference_compatibility', {})
    if not required.get('required'):
        return True
    return len(cases)==len(spec['cases']) and all(c.get('mathematical_status')=='accepted'
        and c.get('reference_compatibility', {}).get('protocol')==required['protocol']
        and c.get('reference_compatibility', {}).get('status')=='matched'
        and c.get('reference_compatibility', {}).get('reference_source_sha256')==required['reference_source_sha256'] for c in cases)


def local_selection_summary(result):
    if result.get('suite') == 'local_ab' and result.get('custom_case_indices') is not None:
        return dict(suite='local_ab', custom_case_indices=result['custom_case_indices'],
                    full_suite_eligible=False, dataset_version='custom',
                    source_dataset_sha256=result.get('local_ab_request', {}).get('custom_dataset', {}).get('sha256'))
    if result.get('regression_suite') is not None:
        return dict(suite=result.get('suite'),regression_suite=result['regression_suite'],
                    full_suite_eligible=False,correctness_only=True,
                    dataset_version=result['regression_suite'])
    indices = result.get('local_case_indices')
    if indices is None:
        return {}
    dataset = result.get('dataset') or {}
    return dict(suite=result.get('suite', 'local_subset'), local_case_indices=indices, full_suite_eligible=False,
                dataset_version=dataset.get('version') or result.get('problem', {}).get('dataset_revision'),
                canonical_dataset_sha256=dataset.get('canonical_sha256'))


def validation_summary(row, spec=None):
    spec = spec or problem(row['problem_id'])
    from .store import TERMINAL
    result = row['result']
    dataset = dataset_validation(result, spec, require_dataset=row['status'] in TERMINAL or bool(result.get('dataset')))
    if dataset is not None:
        legacy = dataset['requires_reevaluation']
        return dict(status='legacy_unverified' if legacy and row['status'] in TERMINAL else row['status'],
                    original_status=row['status'], accepted=row['status']=='accepted' and not legacy,
                    requires_reevaluation=legacy, protocol=spec['id']+'_dataset_revision',
                    total=len(result.get('custom_case_indices') or result.get('local_case_indices') or result.get('regression_spec',spec)['cases']), dataset=dataset,
                    note=('仅本地子集；不计全套排名、校准或官方上传门禁。' + dataset['note'])
                         if result.get('suite') in {'local_subset', 'local_ab'} else dataset['note'],
                    **local_selection_summary(result))
    required = spec.get('validation', {}).get('reference_compatibility', {})
    if not required.get('required'):
        return None
    cases = result.get('cases', [])
    count = len(spec['cases'])
    complete = len(cases) == count and all(c.get('reference_compatibility', {}).get('protocol') == required['protocol']
                 and c.get('reference_compatibility', {}).get('status') in {'matched','mismatch'} for c in cases)
    legacy = row['status'] == 'accepted' and (not reference_passes(cases,spec) or
        result.get('environment', {}).get('problem_sha256') != spec['pack_sha256'])
    return dict(status='legacy_unverified' if legacy else row['status'],
                accepted=row['status']=='accepted' and not legacy,
                requires_reevaluation=legacy, protocol=required['protocol'],
                mathematical_passed=sum(c.get('mathematical_status', c.get('status'))=='accepted' for c in cases),
                compatibility_passed=sum(c.get('reference_compatibility', {}).get('status')=='matched' for c in cases),
                compatibility_checked=complete, total=count,
                note=('评测协议已更新，请复测后参与当前排行榜；原有数学和参考兼容性结果保留。' if reference_passes(cases,spec)
                      else '旧版只检查数学性质，未完成当前参考兼容性校验，不参加当前排行榜；需要复测。') if legacy else required['note'])

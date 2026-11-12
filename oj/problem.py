from .config import ROOT
from remote.registry import Registry as PackRegistry


class Registry(PackRegistry):
    def get(self, identity):
        spec = super().get(identity)
        # Submission routing is service metadata, not part of the NPU pack.
        from .official_targets import configuration, problem_url
        config = configuration()
        available = identity in config['problems']
        spec['local_submission_only'] = not available
        spec['official_submission'] = dict(available=available)
        if available:
            spec['official_submission'].update(
                url=problem_url(config, identity),
                tool='submit_official_project' if identity in config.get('project_templates', {}) else 'submit_official_kernel')
        return spec

registry = Registry(ROOT / 'problems')
DEFAULT_PROBLEM = 'cum_sum'  # Backward-compatible default; every registered ID is selectable.


def problem(problem_id=DEFAULT_PROBLEM):
    return registry.get(problem_id)


def generator_example(problem_id=DEFAULT_PROBLEM):
    spec = problem(problem_id)
    if spec.get('custom_generator_supported') is False:
        raise ValueError('此题暂未开放自定义数据生成器；请使用默认测试集')
    root = registry.path(problem_id)
    return dict(problem_id=problem_id, source=(root / 'examples/generate.cpp').read_text(encoding='utf-8'),
                header=(root / 'generator/oj_generator.hpp').read_text(encoding='utf-8'),
                language='C++17', limits=spec.get('generator_limits', 'See problem specification'))


def check_source(source, problem_id=DEFAULT_PROBLEM):
    from remote.rules import inspect_source
    spec = problem(problem_id)
    result = inspect_source(source, spec['contract'])
    result.update(problem_id=problem_id, problem_version=spec['version'], problem_sha256=spec['pack_sha256'])
    return result

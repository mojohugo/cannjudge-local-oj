"""MCP access contract; independent of the immutable worker recipe hash."""
from remote.pdist_structured import SUITE, suite_info, validate_suite

ACCESS_VERSION = 'pdist-regression-access-v1'
SELECTOR_PREFIX = 'cann-oj-suite:'
SELECTOR = SELECTOR_PREFIX + SUITE


def resolve_selection(spec, generator, suite, indices):
    # A standalone reserved token cannot be a valid C++ generator. Never scan
    # comments, literals or kernel source, and never interpret labels as routing.
    if generator is not None and generator.lstrip().startswith(SELECTOR_PREFIX):
        if generator != SELECTOR:
            raise ValueError('Unknown or malformed CANN OJ suite selector; expected ' + SELECTOR)
        if suite is not None:
            raise ValueError('Use either generator_cpp suite selector or regression_suite, not both')
        generator, suite = None, SUITE
    validate_suite(spec, suite, generator, indices)
    return generator, suite


def access_info():
    return dict(
        access_version=ACCESS_VERSION,
        suite=suite_info(),
        read=dict(tool='get_problem_guide', arguments=dict(problem_id='pdist', section='workflow'),
                  result_field='pdist_regression'),
        submit=dict(tool='submit_kernel', arguments=dict(
            problem_id='pdist', source='<complete kernel.asc source>',
            generator_cpp=SELECTOR, profile_mode='timing', label='Pdist wide correctness')),
        named_tools=dict(read='get_pdist_regression_suite', submit='submit_pdist_regression'),
        refreshed_schema=dict(tool='submit_kernel', parameter='regression_suite', value=SUITE),
        poll=dict(tool='get_submission', id_field='id', argument='submission_id', interval_seconds=[5, 10]),
        selector_rules='Exact standalone token only; unknown/malformed tokens and mixing with '
                       'regression_suite, a real generator or local_case_indices are rejected. '
                       'The selector is never stored or compiled as C++. Labels do not select suites.',
        profile_mode='Structured regression always uses correctness-only execution without timings or profiler.',
        discovery_scope='Server response only; does not prove named tools or new parameters exist in the client snapshot.',
        limitation='Fixed structured synthetic family only; not arbitrary random inputs or official hidden cases.')

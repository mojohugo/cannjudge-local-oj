"""Old MCP connector access to the existing canonical Pdist subset path."""
import re

from remote.local_cases import validate_local_case_indices

ACCESS_VERSION = 'pdist-subset-access-v1'
SELECTOR_PREFIX = 'cann-oj-subset:'
EXAMPLE = SELECTOR_PREFIX + '7,13'


def resolve_selection(spec, generator, indices, suite):
    # Only a standalone reserved prefix is routing syntax. C++ comments,
    # literals, source and labels must never select a dataset.
    if generator is None or not generator.lstrip().startswith(SELECTOR_PREFIX):
        return generator, indices
    if indices is not None or suite is not None:
        raise ValueError('Use either generator_cpp subset selector, local_case_indices or regression_suite, not multiple selections')
    # Bound parsing before int conversion; the existing validator owns the
    # problem, range, uniqueness and ascending-order semantics.
    count = len(spec['cases'])
    maximum_length = len(SELECTOR_PREFIX) + count * (len(str(count)) + 1) - 1
    if (len(generator) > maximum_length
            or re.fullmatch(r'cann-oj-subset:[1-9][0-9]*(?:,[1-9][0-9]*)*', generator) is None):
        raise ValueError('Malformed CANN OJ subset selector; expected an exact standalone token such as ' + EXAMPLE)
    selected = [int(value) for value in generator[len(SELECTOR_PREFIX):].split(',')]
    return None, validate_local_case_indices(spec, selected)


def access_info(spec):
    return dict(
        access_version=ACCESS_VERSION,
        read=dict(tool='get_problem_guide', arguments=dict(problem_id='pdist', section='workflow'),
                  result_field='pdist_subset'),
        submit=dict(tool='submit_kernel', arguments=dict(
            problem_id='pdist', source='<complete kernel.asc source>', generator_cpp=EXAMPLE,
            profile_mode='timing', label='Pdist canonical subset 7,13')),
        selection=dict(index_base=1, case_count=len(spec['cases']), order='ascending original index',
                       example_indices=[7, 13]),
        dataset=dict(version=spec['dataset_revision'], profile_sha256=spec['dataset_profile_sha256'],
                     source='installed canonical input and reference bytes; no regeneration'),
        refreshed_schema=dict(tool='submit_kernel', parameter='local_case_indices', value=[7, 13]),
        poll=dict(tool='get_submission', id_field='id', argument='submission_id', interval_seconds=[5, 10]),
        selector_rules='Exact standalone token: cann-oj-subset:<unique one-based decimal indices separated by commas>. '
                       'No whitespace, leading zeros, comments, C++ suffixes, duplicates or out-of-range indices. '
                       'Cannot mix with explicit local_case_indices (even the same list) or regression_suite. '
                       'The token is never stored or compiled as C++; C++ comments/literals and labels do not route.',
        result=dict(suite='local_subset', original_index=True, dataset_hashes=True,
                    full_suite_eligible=False, official_validation_eligible=False),
        profile_mode='Uses the same timing/full settings and validation as local_case_indices.',
        limitation='Even all 15 indices remain a local subset, excluded from rankings, case bests, baselines, '
                   'calibration and official pre-upload validation. Local indices are not official hidden case numbers.',
        discovery_scope='Works with the five existing submit_kernel fields; does not prove client schema refresh.')

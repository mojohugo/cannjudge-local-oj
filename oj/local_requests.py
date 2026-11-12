"""Durable local submission keys; no official account or credential identities."""
import hashlib
import json
import re


# This service has ONE authenticated local owner (API key, password and approved
# OAuth clients). Neither OAuth client IDs nor rotating tokens identify a user.
# Separate owners use separate state directories. Never accept this from RPC.
LOCAL_OWNER = 'local-owner'


def validate_request_id(request_id):
    if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9._-]{8,100}', request_id):
        raise ValueError('request_id must contain 8..100 ASCII letters, digits, dot, underscore or hyphen')
    return request_id


class RequestConflict(ValueError):
    def __init__(self, request_id, identity):
        super().__init__(json.dumps(dict(error='request_id_conflict', request_id=request_id,
            submission_id=identity), separators=(',', ':')))


def request_config(spec, source, generator, profile_mode, indices, regression):
    """Hash the effective evaluation, after selectors/defaults are resolved.

    pack_sha256 covers contract, cases, fixed-input hashes and generator/harness
    sources. Include explicit dataset identity and the separate regression recipe.
    Labels and response/precondition options do not change an evaluation.
    """
    config = dict(version='local-submit-request-v1', source_sha256=hashlib.sha256(source).hexdigest(),
        source_bytes=len(source), problem_id=spec['id'], problem_version=spec['version'],
        problem_sha256=spec['pack_sha256'], dataset_revision=spec.get('dataset_revision'),
        dataset_profile_sha256=spec.get('dataset_profile_sha256'), cases=spec['cases'],
        generator_sha256=hashlib.sha256(generator.encode('utf-8')).hexdigest() if generator is not None else None,
        profile_mode=profile_mode, local_case_indices=indices, regression_spec=regression)
    encoded = json.dumps(config, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    return hashlib.sha256(encoded.encode('utf-8')).hexdigest(), encoded


def compact_receipt(row):
    result = row['result']
    indices = result.get('local_case_indices')
    cases = result.get('dataset', {}).get('cases')
    if cases is not None:
        total_cases = len(cases)
    elif indices is not None:
        total_cases = len(indices)
    elif result['suite'] == 'custom':
        total_cases = None
    elif result['suite'] == 'structured_regression':
        total_cases = len(result['regression_spec']['cases'])
    else:
        total_cases = len(result['problem']['cases'])
    return dict(id=row['id'], sha256=row['sha256'], status=row['status'], problem_id=row['problem_id'],
        suite=result['suite'], profile_mode=result.get('profile_mode', 'full'),
        local_case_indices=indices, total_cases=total_cases,
        queue_stage=row['queue_stage'], ahead_count=row['ahead_count'])

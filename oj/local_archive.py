"""Hash-pinned local source replay through the ordinary keyed enqueue path."""
import hashlib
import json
import re
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, StrictStr

from .local_requests import compact_receipt, validate_request_id
from .problem import problem, check_source


class ArchivedKernelRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, hide_input_in_errors=True)

    submission_id: StrictStr
    expected_source_sha256: StrictStr
    request_id: StrictStr
    generator_mode: Literal['default', 'custom']
    generator_cpp: StrictStr | None = None
    label: StrictStr = 'Archived kernel rerun'
    profile_mode: Literal['full', 'timing'] = 'full'


def submit_archived(settings, store, request: ArchivedKernelRequest):
    """Read once; validate before enqueue, including on idempotent retries.

    Only source and problem identity come from the archive. The caller chooses
    the new generator and profiling mode. No historical results/data are reused.
    """
    validate_request_id(request.request_id)
    if not re.fullmatch(r'[0-9a-f]{32}', request.submission_id):
        raise ValueError('Invalid local submission_id')
    if not re.fullmatch(r'[0-9a-fA-F]{64}', request.expected_source_sha256):
        raise ValueError('expected_source_sha256 must contain exactly 64 hexadecimal characters')
    if len(request.label) > 120:
        raise ValueError('Label must contain at most 120 characters')
    generator = request.generator_cpp
    if request.generator_mode == 'default':
        if generator is not None:
            raise ValueError('generator_mode=default requires generator_cpp omitted or null')
    elif (generator is None or not generator.strip() or '\x00' in generator
          or len(generator.encode('utf-8')) > 128 * 1024
          or generator.strip().startswith('cann-oj-')):
        raise ValueError('generator_mode=custom requires new C++ generator_cpp (1..128 KiB), not a suite selector')

    base = store.get(request.submission_id)
    if base is None:
        raise ValueError('Unknown local submission_id')
    spec = problem(base['problem_id'])
    if (base['result'].get('submission_kind', 'kernel') != 'kernel'
            or spec['contract'].get('submission_kind') == 'project'):
        raise ValueError('Archived reruns require a local single-file kernel submission')
    directory = settings.state_dir / 'submissions' / request.submission_id
    path = directory / 'kernel.asc'
    if directory.is_symlink() or path.is_symlink() or not path.is_file():
        raise ValueError('Local kernel archive is unavailable')
    try:
        with path.open('rb') as archive:
            source = archive.read(settings.max_source_bytes + 1)
    except OSError:
        raise ValueError('Local kernel archive is unavailable') from None
    if len(source) > settings.max_source_bytes:
        raise ValueError('Archived source too large')
    digest = hashlib.sha256(source).hexdigest()
    expected = request.expected_source_sha256.lower()
    if digest != expected:
        raise ValueError(json.dumps(dict(error='source_sha256_mismatch', actual_source_sha256=digest,
            expected_source_sha256=expected, source_bytes=len(source)), separators=(',', ':')))
    if digest != base['sha256']:
        raise ValueError('Local archive SHA does not match its stored record')
    try:
        text = source.decode('utf-8')
    except UnicodeError:
        raise ValueError('Archived source must be valid UTF-8') from None
    if not text.strip() or '\x00' in text:
        raise ValueError('Invalid archived kernel source')
    check = check_source(text, base['problem_id'])
    if not check['passed']:
        rules = sorted({issue['rule_id'] for issue in check['issues']})
        raise ValueError('Archived source failed static review: ' + ', '.join(rules))
    identity = store.enqueue(uuid.uuid4().hex, request.label, digest, source, settings.queue_limit,
        generator, base['problem_id'], request.profile_mode, request_id=request.request_id)
    return compact_receipt(store.get(identity, queue_info=True))

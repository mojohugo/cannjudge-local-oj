"""Explicit, bounded local diagnostics using immutable accepted source snapshots."""
import hashlib
import json
import re
import uuid
from pathlib import Path

from remote.custom_ab import admitted_dataset

from .problem import problem, check_source
from .worker import validate_dataset_identity
from remote.local_ab import validate_request


def enqueue_ab(settings, store, submission_a, submission_b, local_case_indices, cycles, budget_seconds, *, custom_case_indices=None):
    custom = custom_case_indices is not None
    sources, origins = {}, {}
    # The same transaction snapshots both records and creates the one queue job.
    with store.enqueue_transaction() as db:
        rows = []
        for label, identity in [('A', submission_a), ('B', submission_b)]:
            if not isinstance(identity, str) or not re.fullmatch('[0-9a-f]{32}', identity):
                raise ValueError('Expected a local submission ID')
            row = store.row(db.execute('SELECT * FROM submissions WHERE id=?', (identity,)).fetchone())
            if row is None or row['problem_id'] not in {'pdist', 'topk'} or row['status'] != 'accepted':
                raise ValueError('A/B requires two accepted LOCAL Pdist or TopK submissions')
            rows.append((label, identity, row))
        if rows[0][2]['problem_id'] != rows[1][2]['problem_id']:
            raise ValueError('A/B requires sources from the same problem')
        spec = problem(rows[0][2]['problem_id'])
        request = validate_request(spec, dict(local_case_indices=local_case_indices,
                                            cycles=cycles, budget_seconds=budget_seconds,
                                            custom_case_indices=custom_case_indices))
        for label, identity, row in rows:
            result = row['result']
            if result.get('problem_id') != spec['id']:
                raise ValueError('A/B source problem identity mismatch')
            if (result.get('suite') not in ({'custom'} if custom else {'official_shapes', 'local_subset'})
                    or row['problem_sha256'] != spec['pack_sha256']
                    or result.get('problem_sha256') != spec['pack_sha256']
                    or result.get('environment', {}).get('harness_sha256') != spec['pack_sha256']):
                raise ValueError('A/B requires current-pack passes for the selected data mode')
            selection = result.get('local_case_indices')
            if spec['id'] == 'topk':
                from remote.local_cases import validate_local_case_indices
                if validate_local_case_indices(spec, selection) != selection:
                    raise ValueError('Invalid source subset order')
                if (result.get('suite') == 'local_subset') != (selection is not None):
                    raise ValueError('Source suite/selection mismatch')
                if selection is None:
                    dataset = result.get('dataset', {})
                    cases = dataset.get('cases', [])
                    if (dataset.get('mode') != 'synthetic_mixed' or len(cases) != len(spec['cases'])
                            or any(c.get('index') != e['index'] or c.get('shape') != e['shape']
                                   or c.get('dtype') != e['dtype']
                                   or c.get('params') != {k:e[k] for k in ('k', 'largest')}
                                   or any(c.get(k) != e[k] for k in ('k', 'largest'))
                                   for c, e in zip(cases, spec['cases']))):
                        raise ValueError('A/B requires the installed TopK synthetic dataset')
            if custom:
                data = admitted_dataset(result, request['custom_case_indices'])
                if 'custom_dataset' in request and request['custom_dataset'] != data:
                    raise ValueError('Custom A/B input/reference dataset digests must match exactly')
                request['custom_dataset'] = data
                target = dict(ssh_host=settings.ssh_host, remote_root=settings.remote_root)
                if result.get('execution_target') != target:
                    raise ValueError('Custom A/B requires archives on the current worker')
                request['execution_target'] = target
                archived = json.loads((settings.state_dir/'submissions'/identity/'result.json').read_text())
                if archived != result:
                    raise ValueError('Archived custom result differs from database')
            validate_dataset_identity(spec, result.get('dataset'), custom=custom, local_case_indices=selection)
            expected = list(range(1, len(data['cases'])+1)) if custom else selection if selection is not None else list(range(1, len(spec['cases'])+1))
            cases = result.get('cases', [])
            if (len(cases) != len(expected) or any(c.get('status') != 'accepted' for c in cases)
                    or [c.get('index') for c in cases] != list(range(1, len(expected)+1))
                    or (selection is not None and [c.get('original_index') for c in cases] != expected)
                    or not set(request['custom_case_indices'] if custom else request['local_case_indices']).issubset(expected)):
                raise ValueError('Selected cases must have passed in both source records')
            source = (settings.state_dir/'submissions'/identity/'kernel.asc').read_bytes()
            digest = hashlib.sha256(source).hexdigest()
            if digest != row['sha256'] or digest != result.get('source_sha256'):
                raise ValueError('Archived source SHA mismatch')
            if len(source) > settings.max_source_bytes or not check_source(source.decode(), spec['id'])['passed']:
                raise ValueError('Archived source does not pass current source checks')
            sources[label] = source
            origins[label] = dict(submission_id=identity, source_sha256=digest)
            if custom:
                remote = result.get('environment', {}).get('remote_job')
                if not isinstance(remote, str) or not re.fullmatch('[0-9a-f]{32}', Path(remote).name):
                    raise ValueError('Invalid custom archive remote identity')
                origins[label].update(remote_job=remote, remote_job_id=Path(remote).name)
        request['sources'] = origins
        identity = uuid.uuid4().hex
        store.enqueue(identity, 'Local A/B diagnostic', origins['A']['source_sha256'], sources['A'],
                      settings.queue_limit, problem_id=spec['id'], profile_mode='timing', connection=db,
                      local_case_indices=request.get('local_case_indices'))
        directory = settings.state_dir/'submissions'/identity
        (directory/'kernel-b.asc').write_bytes(sources['B'])
        row = store.row(db.execute('SELECT * FROM submissions WHERE id=?', (identity,)).fetchone())
        result = dict(row['result'], suite='local_ab', local_ab_request=request)
        if custom:
            result['custom_case_indices'] = request['custom_case_indices']
        db.execute('UPDATE submissions SET result=? WHERE id=?', (json.dumps(result), identity))
    return store.get(identity)

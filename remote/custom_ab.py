"""Frozen custom-data views for local ABBA; no generator or reference rebuild."""
import hashlib
import json
import math
from pathlib import Path
import re
import shutil

if __package__:
    from .local_cases import file_sha256
else:
    from local_cases import file_sha256


def indices(value, count=30):
    if (not isinstance(value, list) or not 1 <= len(value) <= 3
            or any(type(i) is not int or not 1 <= i <= count for i in value)
            or len(set(value)) != len(value)):
        raise ValueError('custom_case_indices must select 1..3 unique one-based custom cases')
    return sorted(value)


def digest_ok(value):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None


def positive(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def admitted_dataset(result, selection):
    """Only complete custom passes with actual event AND measured device times."""
    data = result.get('dataset') or {}
    cases, metadata = result.get('cases', []), data.get('cases', [])
    metric = result.get('metrics', {}).get('PipeUtilization', {})
    if (result.get('suite') != 'custom' or result.get('status') != 'accepted'
            or result.get('regression_suite') is not None or result.get('correctness_only')
            or result.get('profile_mode') not in {'timing', 'full'}
            or data.get('mode') != 'custom' or data.get('version') != 'custom'
            or data.get('profile_sha256') is not None or not digest_ok(data.get('sha256'))
            or not 1 <= len(metadata) <= 30 or len(cases) != len(metadata)
            or metric.get('status') != 'available' or not metric.get('protocol_id')
            or metric.get('measurement_protocol', {}).get('exact') is not True
            or len(metric.get('cases', [])) != len(metadata)):
        raise ValueError('Custom A/B requires a complete accepted timed custom evaluation')
    indices(selection, len(metadata))
    for i, (case, meta, timing) in enumerate(zip(cases, metadata, metric['cases']), 1):
        if (case.get('index') != i or meta.get('index') != i or timing.get('index') != i
                or case.get('status') != 'accepted' or not positive(case.get('time_us'))
                or not positive(timing.get('steady_state_mean_us'))
                or timing.get('protocol_id') != metric['protocol_id']
                or case.get('shape') != meta.get('shape') or case.get('dtype') != meta.get('dtype')
                or 'original_index' in case or 'original_index' in meta):
            raise ValueError('Incomplete custom case identity/correctness/timing')
    return data


def dataset_error(dataset, request):
    """Coordinator-side identity gate; never accept a canonical-data substitute."""
    expected = request.get('custom_dataset') or {}
    selected = request.get('custom_case_indices', [])
    if (not isinstance(dataset, dict) or dataset.get('mode') != 'custom_subset'
            or dataset.get('version') != 'custom' or dataset.get('profile_sha256') is not None
            or dataset.get('source_dataset_sha256') != expected.get('sha256')
            or not digest_ok(dataset.get('source_dataset_sha256')) or not digest_ok(dataset.get('sha256'))
            or dataset.get('custom_case_indices') != selected or 'local_case_indices' in dataset
            or 'canonical_sha256' in dataset or dataset.get('original_case_count') != len(expected.get('cases', []))
            or len(dataset.get('cases', [])) != len(selected)):
        return 'Custom A/B dataset identity mismatch'
    for ordinal, (original, case) in enumerate(zip(selected, dataset['cases']), 1):
        original_case = expected['cases'][original-1]
        if (case.get('index') != ordinal or case.get('original_index') != original
                or any(case.get(k) != original_case.get(k) for k in
                       ('shape','dtype','name','params','specification_origin','official_case_index'))
                or set(case.get('sha256', {})) != {'x','y'}
                or not all(digest_ok(v) for v in case['sha256'].values())):
            return 'Custom A/B selected case identity mismatch'
    return None


def regular(path, parent, size=None):
    if (path.is_symlink() or not path.is_file() or path.resolve().parent != parent.resolve()
            or (size is not None and path.stat().st_size != size)):
        raise ValueError('Custom archive file boundary/size mismatch or data no longer retained')


def freeze(worker, pack, job, request):
    """Validate both full archived manifests/tensors, copy selected pairs once.

    Pin selected bytes in this diagnostic before any candidate execution. Old
    scratch cleanup cannot change the four runs after this snapshot is made.
    """
    selected = indices(request['custom_case_indices'])
    expected = request['custom_dataset']
    # The ordinary archived-job cleaner already reclaims eval-data binaries
    # while retaining manifests/TSVs; keep the frozen view in that lifecycle.
    view = job/'eval-data'
    view.mkdir(mode=0o755)
    first = None
    for label in ('A','B'):
        origin = request['sources'][label]
        remote_id = origin['remote_job_id']
        if not isinstance(remote_id, str) or not re.fullmatch('[0-9a-f]{32}', remote_id):
            raise ValueError('Invalid archived remote job ID')
        archive = worker.JOBS/remote_id
        if (archive.is_symlink() or archive.resolve().parent != worker.JOBS.resolve()
                or str(archive) != origin['remote_job']):
            raise ValueError('Custom archive belongs to a different worker')
        source, result_file, data = archive/'kernel.asc', archive/'result.json', archive/'eval-data'
        regular(source, archive); regular(result_file, archive)
        if file_sha256(source) != origin['source_sha256']:
            raise ValueError('Archived remote source SHA mismatch')
        old = json.loads(result_file.read_text())
        if (old.get('status') != 'accepted' or old.get('source_sha256') != origin['source_sha256']
                or len(old.get('cases', [])) != len(expected['cases'])
                or any(c.get('status') != 'accepted' or not positive(c.get('time_us')) for c in old['cases'])):
            raise ValueError('Archived remote result is not a complete timed pass')
        if data.is_symlink() or not data.is_dir() or data.resolve().parent != archive.resolve():
            raise ValueError('Custom data must be an archived regular directory')
        manifest_path = data/'manifest.json'
        regular(manifest_path, data)
        if manifest_path.stat().st_size > 1024*1024 or file_sha256(manifest_path) != expected['sha256']:
            raise ValueError('Custom input/reference manifest SHA mismatch')
        manifest = json.loads(manifest_path.read_text())
        records = manifest.get('records', [])
        if (manifest.get('schema_version') != 1 or manifest.get('problem_id') != 'pdist'
                or manifest.get('dataset_version') != 'custom' or manifest.get('profile_sha256') is not None
                or not 1 <= len(records) <= 30 or len(records) != len(expected['cases'])
                or (first is not None and manifest != first)):
            raise ValueError('Custom datasets differ or are not trusted custom manifests')
        first = manifest
        indices(selected, len(records))
        total = 0
        for number, (record, meta) in enumerate(zip(records, expected['cases']), 1):
            shape, dtype = record.get('shape'), record.get('dtype')
            if (not isinstance(shape, list) or len(shape) != 2
                    or any(type(v) is not int or v < 1 for v in shape) or type(dtype) is not int or dtype not in (0,1,2)):
                raise ValueError('Invalid custom tensor geometry')
            rows, cols = shape; width = 4 if dtype == 0 else 2
            sizes = dict(x=rows*cols*width, y=rows*(rows-1)//2*width)
            if max(sizes.values()) > 256*1024**2 or rows*(rows-1)//2*cols > 3000000000:
                raise ValueError('Custom tensor exceeds existing geometry budget')
            total += sum(sizes.values())
            dtype_name = ('float32','float16','bfloat16')[dtype]
            name = f'custom_{number:02d}'
            tsv = '\t'.join([name,str(dtype),'0',','.join(map(str,shape)),
                str(pack['checker']['rtol'][dtype_name]),str(pack['checker']['atol'][dtype_name]),'0','1','1'])
            if (record.get('index') != number or record.get('label') != name or record.get('tsv') != tsv
                    or record.get('bytes') != sizes or record.get('shape') != meta.get('shape')
                    or dtype_name != meta.get('dtype') or record.get('name') != meta.get('name')
                    or record.get('params') != meta.get('params') or record.get('specification_origin') != 'user_custom'
                    or record.get('official_case_index') is not None or set(record.get('sha256', {})) != {'x','y'}
                    or total > 2*1024**3):
                raise ValueError('Custom manifest case identity/checker mismatch')
            case_tsv = data/f'case-{number:02d}.tsv'; regular(case_tsv, data)
            if case_tsv.read_bytes() != (tsv+'\n').encode():
                raise ValueError('Custom TSV differs from archived manifest')
            for suffix in ('x','y'):
                binary = data/(name+'_'+suffix+'.bin'); regular(binary, data, sizes[suffix])
                if not digest_ok(record['sha256'][suffix]) or file_sha256(binary) != record['sha256'][suffix]:
                    raise ValueError('Custom input/reference bytes differ from archived SHA')
                if label == 'A' and number in selected:
                    target = view/binary.name
                    shutil.copyfile(binary, target); target.chmod(0o644)
                    if file_sha256(target) != record['sha256'][suffix]:
                        raise ValueError('Custom data changed during snapshot')
        all_tsv = data/'all.tsv'; regular(all_tsv, data)
        if all_tsv.read_bytes() != ''.join(r['tsv']+'\n' for r in records).encode():
            raise ValueError('Custom full TSV differs from manifest')
    records = [first['records'][i-1] for i in selected]
    for ordinal, record in enumerate(records, 1):
        (view/f'case-{ordinal:02d}.tsv').write_text(record['tsv']+'\n')
    (view/'all.tsv').write_text(''.join(r['tsv']+'\n' for r in records))
    selection = dict(custom_case_indices=selected, original_case_count=len(first['records']),
                     source_dataset_sha256=expected['sha256'])
    (view/'manifest.json').write_text(json.dumps(dict(first, records=records, **selection),
                                                ensure_ascii=False, indent=2)+'\n')
    return view, selection, records


def evaluate_view(adapter, frozen, job, work, env, run, emit, identity, started, source_sha256):
    view, selection, records = frozen
    def publish(kind, **fields):
        if kind == 'dataset':
            dataset = fields['dataset']; dataset.update(mode='custom_subset', **selection)
            for case, original, record in zip(dataset['cases'], selection['custom_case_indices'], records):
                case.update(original_index=original, sha256=record['sha256'],
                            generation=record.get('generation'), values_origin=record.get('values_origin'))
        elif kind == 'case':
            fields['case']['original_index'] = selection['custom_case_indices'][fields['case']['index']-1]
        elif kind == 'metrics':
            for case in fields['metrics'].get('cases', []):
                case['original_index'] = selection['custom_case_indices'][case['index']-1]
        emit(kind, **fields)
    factory = adapter.fixed_data_dir
    try:
        adapter.fixed_data_dir = lambda:view
        return adapter.evaluate(job, work, None, env, run, publish, identity, started, source_sha256)
    finally:
        adapter.fixed_data_dir = factory

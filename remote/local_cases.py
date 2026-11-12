"""Local Pdist/TopK selection over installed data; never regenerate a pack."""
import hashlib
import json
import re


def validate_local_case_indices(spec, indices, generator=None):
    if indices is None:
        return None
    if spec['id'] not in {'pdist', 'topk'} or generator is not None:
        raise ValueError('local_case_indices only supports Pdist/TopK installed default data, without a custom generator')
    count = len(spec['cases'])
    if (not isinstance(indices, list) or not 1 <= len(indices) <= count
            or any(type(i) is not int or not 1 <= i <= count for i in indices)
            or len(set(indices)) != len(indices)):
        raise ValueError(f'local_case_indices must be a nonempty list of unique one-based integers in 1..{count}')
    return sorted(indices)


def file_sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def selected_data_view(adapter, spec, indices, job):
    """A coordinator-owned view: original TSV bytes and binaries, compact ordinals.

    The unchanged adapter and profile.sh both consume eval-data. Its records keep
    their original identity; only case-NN.tsv filenames enumerate the selection.
    No files in the installed dataset are written, copied, or regenerated.
    """
    indices = validate_local_case_indices(spec, indices)
    canonical = adapter.fixed_data_dir()  # Existing version/profile/path checks.
    manifest_path = canonical / 'manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    if (manifest.get('problem_id') != spec['id']
            or manifest.get('dataset_version') != spec['dataset_revision']
            or manifest.get('profile_sha256') != spec['dataset_profile_sha256']
            or len(manifest['records']) != len(spec['cases'])):
        raise ValueError('Installed dataset identity/count mismatch')
    records = [manifest['records'][i - 1] for i in indices]
    view = job / 'selected-data'
    view.mkdir(mode=0o755)
    lines = []
    for ordinal, (original, record) in enumerate(zip(indices, records), 1):
        expected = spec['cases'][original - 1]
        if (record.get('index') != original or record['shape'] != expected['shape']
                or record['dtype'] != expected['dtype_id']
                or not re.fullmatch(r'fixed_[0-9]{2}', record['label'])
                or record['sha256']['x'] != expected['expected_input_sha256']):
            raise ValueError('Installed case identity mismatch')
        suffixes = ('x', 'y', 'index') if spec['id'] == 'topk' else ('x', 'y')
        if spec['id'] == 'topk':
            # TopK has TWO outputs. Preserve the exact stable int32 reference,
            # parameters, tolerances and seed policy as well as the value bytes.
            width = {'float32': 4, 'float16': 2}[expected['dtype']]
            sizes = dict(x=expected['elements'] * width,
                         y=expected['output_elements'] * width,
                         index=expected['output_elements'] * 4)
            params = dict(k=expected['k'], largest=expected['largest'])
            trusted_tsv = '\t'.join([
                f'fixed_{original:02d}', str(expected['dtype_id']), '0',
                ','.join(map(str, expected['shape'])),
                str(spec['checker']['rtol'][expected['dtype']]),
                str(spec['checker']['atol'][expected['dtype']]), '0',
                str(expected['k']), str(int(expected['largest']))])
            if (record['label'] != f'fixed_{original:02d}' or record.get('params') != params
                    or record.get('generation') != expected['generation']
                    or record.get('output_shape') != expected['output_shape']
                    or record.get('bytes') != sizes or record.get('tsv') != trusted_tsv
                    or set(record['sha256']) != set(suffixes)):
                raise ValueError('Installed TopK parameters/reference layout mismatch')
        tsv = canonical / f'case-{original:02d}.tsv'
        line = tsv.read_bytes()
        if line != (record['tsv'] + '\n').encode('utf-8'):
            raise ValueError('Installed case TSV mismatch')
        for suffix in suffixes:
            binary = canonical / (record['label'] + '_' + suffix + '.bin')
            if (binary.is_symlink() or not binary.is_file()
                    or (spec['id'] == 'topk' and binary.stat().st_size != sizes[suffix])
                    or file_sha256(binary) != record['sha256'][suffix]):
                raise ValueError('Installed input/reference checksum mismatch')
            (view / binary.name).symlink_to(binary)
        (view / f'case-{ordinal:02d}.tsv').symlink_to(tsv)
        lines.append(line)
    (view / 'all.tsv').write_bytes(b''.join(lines))
    selection = dict(local_case_indices=indices, original_case_count=len(spec['cases']),
                     canonical_sha256=file_sha256(manifest_path))
    (view / 'manifest.json').write_text(json.dumps(dict(manifest, records=records, **selection),
                                                  ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return view, selection, records


def evaluate_local_cases(adapter, spec, indices, job, work, generator, env, run, emit,
                         identity, started, source_sha256):
    indices = validate_local_case_indices(spec, indices, generator)
    if indices is None:
        return adapter.evaluate(job, work, generator, env, run, emit, identity, started, source_sha256)
    view, selection, records = selected_data_view(adapter, spec, indices, job)

    def publish(kind, **fields):
        if kind == 'dataset':
            dataset = fields['dataset']
            dataset.update(mode='local_subset', **selection)
            for case, original, record in zip(dataset['cases'], indices, records):
                case.update(original_index=original, generation=record.get('generation'),
                            values_origin=record.get('values_origin'), sha256=record['sha256'])
        elif kind == 'case':
            fields['case']['original_index'] = indices[fields['case']['index'] - 1]
        elif kind == 'metrics':
            for case in fields['metrics'].get('cases', []):
                case['original_index'] = indices[case['index'] - 1]
        elif kind == 'finished':
            fields['message'] = fields.get('message', '') + ' · 本地子集 ' + str(indices) + '；不计全套排名、校准或官方上传门禁'
        emit(kind, **fields)

    # Registry loads a fresh adapter per worker process/job. Temporarily bind its
    # existing data-directory hook; leave pack bytes/hash and default execution
    # unchanged. Restore even on errors so embedded/test callers cannot leak it.
    original_factory = adapter.fixed_data_dir
    try:
        adapter.fixed_data_dir = lambda: view
        return adapter.evaluate(job, work, None, env, run, publish, identity, started, source_sha256)
    finally:
        adapter.fixed_data_dir = original_factory

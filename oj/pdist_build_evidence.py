"""Bounded, read-only evidence from archived Pdist results; never a judge rule."""
import hashlib
import re
import stat
import struct


NOTE = ('Evidence only: common mixed-kernel warnings and source differences do not establish '
        'an official rule violation. Cache reuse is not an independent build. Public samples '
        'are not actual official build artifacts. No compatibility verdict or score adjustment.')


def unknown(reason):
    return dict(status='unknown', reason=reason)


def mapping(value):
    return value if isinstance(value, dict) else {}


def sha(value):
    return value if isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) else None


def read_archive(root, relative, limit):
    """Fixed archive-relative paths only; do not follow symlinks or special files."""
    path = root / relative
    if root.is_symlink() or any(p.is_symlink() for p in path.parents) or path.is_symlink():
        raise ValueError('unsafe_archive_path')
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
        raise ValueError('unsupported_or_oversized_archive')
    with path.open('rb') as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError('oversized_archive')
    return raw


def compile_evidence(directory, identity):
    try:
        raw = read_archive(directory, 'compile.log', 2 * 1024 * 1024)
        lines = raw.decode('utf-8', errors='replace').splitlines()
        candidates = [line.split(': ', 1)[1] for line in lines if line.startswith('-- Independent candidate: ')]
        if not candidates or any(not p.endswith('/'+identity+'/kernel.asc') for p in candidates):
            return unknown('compile_log_source_identity_unverified')
        selected = []
        entries, artifacts = set(), set()
        for index, line in enumerate(lines, 1):
            obj = re.search(r'Building (?:ASC|CXX) object CMakeFiles/([^/]+)\.dir/([^\s]+)\.o$', line)
            link = re.search(r'Linking (?:ASC|CXX) executable ([\w.-]+)$', line)
            if obj:
                artifacts.add(obj[1]); entries.add(obj[2])
            if link:
                artifacts.add(link[1])
            if obj or link or line.startswith(('-- CMAKE_ASC_COMPILER:', '-- CMAKE_ASC_LLD_LINKER:',
                                              '-- The CXX compiler identification', 'WARNING: kernel type')):
                if len(selected) < 80:
                    selected.append(dict(line=index, text=line[:1000]))
        return dict(status='available', provenance='compile.log', sha256=hashlib.sha256(raw).hexdigest(),
                    entries=sorted(entries), artifacts=sorted(artifacts), excerpts=selected,
                    full_arguments=unknown('complete_compile_and_link_commands_not_archived'))
    except FileNotFoundError:
        return unknown('compile_log_not_archived')
    except (OSError, ValueError):
        return unknown('compile_log_unreadable_or_unsafe')


def elf_metadata(raw):
    """Public ELF64 little-endian tables only. No execution or device-image inference."""
    if len(raw) < 64:
        raise ValueError('truncated_elf')
    if raw[:4] != b'\x7fELF' or raw[4:7] != b'\x02\x01\x01':
        raise ValueError('unsupported_elf_format')
    kind, machine, version, entry, phoff, shoff, flags, ehsize, phsize, phnum, shsize, shnum, shstr = struct.unpack_from('<HHIQQQIHHHHHH', raw, 16)
    if version != 1 or ehsize != 64 or phnum == 0xffff or shstr == 0xffff or (shoff and not shnum):
        raise ValueError('unsupported_elf_header_or_extended_numbering')
    if phnum > 256 or shnum > 4096:
        raise ValueError('oversized_elf_tables')
    if ((phnum and (phoff < 64 or phsize != 56 or phoff+phnum*phsize > len(raw)))
            or (shnum and (shoff < 64 or shsize != 64 or shoff+shnum*shsize > len(raw)))):
        raise ValueError('truncated_or_unsupported_elf_tables')
    programs = []
    for i in range(phnum):
        t, f, off, va, pa, size, mem, align = struct.unpack_from('<IIQQQQQQ', raw, phoff+i*phsize)
        if size and off+size > len(raw):
            raise ValueError('truncated_elf_segment')
        programs.append(dict(type=t, flags=f, offset=off, file_size=size, memory_size=mem, alignment=align))
    headers = [struct.unpack_from('<IIQQQQIIQQ', raw, shoff+i*shsize) for i in range(shnum)]
    names = b''
    if shstr:
        if shstr >= shnum or headers[shstr][1] != 3:
            raise ValueError('invalid_elf_section_names')
        h = headers[shstr]
        if h[4]+h[5] > len(raw):
            raise ValueError('truncated_elf_section_names')
        names = raw[h[4]:h[4]+h[5]]
    sections = []
    for h in headers:
        name, t, f, addr, off, size, link, info, align, entsize = h
        if t not in (0, 8) and size and off+size > len(raw):
            raise ValueError('truncated_elf_section')
        label = None
        if name:
            end = names.find(b'\x00', name)
            if end == -1 or end-name > 512:
                raise ValueError('invalid_elf_section_name')
            label = names[name:end].decode('utf-8', errors='replace')
        sections.append(dict(name=label, type=t, flags=f, offset=off, size=size, alignment=align))
    return dict(status='available', scope='public_elf64_header_and_tables_only',
                header=dict(elf_class=64, endianness='little', type=kind, machine=machine,
                            flags=flags, osabi=raw[7], abi_version=raw[8]),
                program_headers=programs, sections=sections,
                symbols=unknown('not_decoded'), device_kernel_layout=unknown('not_decoded'))


def local_elf(directory, binary_sha256):
    for relative in ('pdist_bench', 'build/pdist_bench', 'work/build/pdist_bench'):
        try:
            raw = read_archive(directory, relative, 64 * 1024 * 1024)
        except FileNotFoundError:
            continue
        except (OSError, ValueError):
            return unknown('local_elf_unreadable_or_unsafe')
        if not binary_sha256:
            return unknown('local_elf_binary_hash_not_recorded')
        actual = hashlib.sha256(raw).hexdigest()
        if actual != binary_sha256:
            return unknown('local_elf_binary_hash_mismatch')
        try:
            return dict(elf_metadata(raw), provenance=relative, binary_sha256=actual,
                        artifact_role='original_local_evaluation')
        except (ValueError, struct.error) as error:
            return unknown(str(error))
    return unknown('local_elf_not_archived_on_service; remote_paths_not_fetched')


def official_evidence(record, files):
    """Called only after exact source bytes have been verified in the account archive."""
    public = [dict(index=c.get('index'), status=c.get('status'), message=str(c.get('message') or '')[:4000])
              for c in record.get('cases', []) if c.get('status') != 'Hidden' and c.get('hidden') is not True]
    entries = sorted({name for c in public for name in re.findall(r'\bjudge_entry\.asc\b', c['message'])})
    samples = []
    for f in files:
        if f.get('path') in ('CMakeLists.txt', 'main.asc', 'run.sh') and isinstance(f.get('content'), str):
            samples.append(dict(path=f['path'], sha256=hashlib.sha256(f['content'].encode()).hexdigest(),
                                artifact_role='public_sample_not_official_build'))
    return dict(official_status=record.get('official_status'), public_cases=public, public_samples=samples,
                compilation_entries=dict(status='available', entries=entries, provenance='public_case_messages') if entries
                    else unknown('same_source_public_messages_do_not_identify_entry'),
                actual_elf=unknown('actual_official_elf_not_archived'),
                full_arguments=unknown('actual_official_compile_and_link_arguments_not_archived'),
                private_layout_rules=unknown('not_public'))


def build_evidence(row, state_dir, official):
    result = row['result']
    view = dict(schema_version=1, scoring_eligible=False, status='unavailable',
                compatibility=unknown('official_binary_and_full_build_parameters_unavailable'), note=NOTE,
                source_sha256=row.get('sha256'), local=None,
                official=dict(status='available' if official else 'unavailable', records=official,
                              reason=None if official else 'no_verified_same_source_archive'))
    if not re.fullmatch('[0-9a-f]{32}', str(row.get('id', ''))):
        return dict(view, reason='invalid_submission_identity')
    directory = state_dir / 'submissions' / row['id']
    source_sha = sha(row.get('sha256'))
    if not source_sha or result.get('source_sha256') != source_sha:
        return dict(view, reason='result_source_identity_unverified')
    try:
        raw = read_archive(directory, 'kernel.asc', 4 * 1024 * 1024)
        if hashlib.sha256(raw).hexdigest() != source_sha:
            return dict(view, reason='archived_source_hash_mismatch')
    except (OSError, ValueError):
        return dict(view, reason='archived_source_unavailable_or_unsafe')
    cache = mapping(mapping(result.get('evaluation')).get('compile_cache'))
    binary_sha = sha(cache.get('binary_sha256'))
    cache_status = cache.get('status')
    compile_info = compile_evidence(directory, row['id'])
    kernels = []
    metric = mapping(mapping(result.get('metrics')).get('PipeUtilization'))
    if metric.get('status') == 'available':
        for case in metric.get('cases') or []:
            names = mapping(case).get('kernel_names', [])
            if isinstance(names, list) and names:
                kernels.append(dict(index=case.get('index'), kernel_names=[n[:1000] for n in names[:64] if isinstance(n, str)]))
    local = dict(artifact_role='original_local_evaluation', submission_id=row['id'],
                 source_sha256=source_sha, binary_sha256=binary_sha,
                 harness_sha256=sha(mapping(result.get('environment')).get('harness_sha256')),
                 problem_sha256=sha(result.get('problem_sha256')),
                 build_fingerprint=unknown('not_archived; cache_key_is_not_a_build_manifest_hash'),
                 compile_cache=dict(status=cache_status or 'unknown', key=sha(cache.get('key')),
                     compiled_from_job=cache.get('compiled_from_job') if cache_status == 'hit' and re.fullmatch('[0-9a-f]{32}', str(cache.get('compiled_from_job'))) else None,
                     independent_build=False if cache_status == 'hit' else None,
                     provenance='result.evaluation.compile_cache'),
                 compilation=compile_info, elf=local_elf(directory, binary_sha),
                 observed_kernels=dict(status='available' if kernels else 'unavailable', cases=kernels,
                     provenance='result.metrics.PipeUtilization.cases[].kernel_names',
                     reason=None if kernels else 'profiler_kernel_names_not_archived'))
    return dict(view, status='partial' if binary_sha or kernels or compile_info['status'] == 'available' or official else 'unavailable',
                reason='official_build_evidence_incomplete', local=local)

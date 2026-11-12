"""Execution optimizations that preserve problem packs and measurement settings."""
import hashlib
import json
from pathlib import Path
import re
import shlex
import shutil
import time

if __package__:
    from .launch_metrics import attach_launch_metrics, collect_launch_metrics, collect_partial_profile, COUNT_MISMATCH
    from .locking import FileLock
    from .profiling import ProfilerPipeline
else:
    from launch_metrics import attach_launch_metrics, collect_launch_metrics, collect_partial_profile, COUNT_MISMATCH
    from locking import FileLock
    from profiling import ProfilerPipeline


def sha256(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024*1024), b''):
            value.update(chunk)
    return value.hexdigest()


class Execution:
    def __init__(self, root, job, work, pack, source, env, runner, own, profile_mode='full', device_lease=None,
                 profiling_groups=None):
        self.root, self.job, self.work = Path(root), Path(job), Path(work)
        self.pack, self.env, self.runner, self.own = pack, env, runner, own
        self.profile_mode = profile_mode
        self.stages = {}
        self.timeline = []
        self.device_lease = device_lease
        self.cache = {'status':'disabled','reason':'No supported build artifact'}
        self.entry = None
        self.cached = False
        # A build reusing these macros could intentionally depend on its job path/time.
        self.cacheable = not re.search(rb'\b(?:__FILE__|__BASE_FILE__|__DATE__|__TIME__|__TIMESTAMP__|__builtin_FILE|CUMSUM_KERNEL_SOURCE|COSINE_SIMILARITY_KERNEL_SOURCE|LOG_SIGMOID_KERNEL_SOURCE|MISH_KERNEL_SOURCE)\b', source)
        self.source_sha256 = hashlib.sha256(source).hexdigest()
        self.profiler = ProfilerPipeline(self, profiling_groups[:1] if profile_mode == 'timing' else profiling_groups) if profiling_groups else None

    def _cache_entry(self, source_dir, argv):
        compiler = shutil.which('bisheng', path=self.env.get('PATH'))
        if not compiler or not self.cacheable:
            return None
        compiler = str(Path(compiler).resolve())
        # Toolchain changes invalidate the cache. Dependencies are also checked
        # byte-for-byte on each hit, including the CANN headers in compiler .d files.
        settings = {k:v for k,v in self.env.items() if k in {
            'ASCEND_HOME_PATH','CMAKE_PREFIX_PATH','CC','CXX','CFLAGS','CXXFLAGS',
            'ASCFLAGS','LDFLAGS','CPATH','CPLUS_INCLUDE_PATH','LIBRARY_PATH','LD_LIBRARY_PATH'}}
        fingerprint = dict(schema=1, source_sha256=self.source_sha256,
            pack_sha256=self.pack['pack_sha256'],compiler=compiler,compiler_sha256=sha256(compiler),
            source_dir=str(source_dir.resolve()),settings=settings,
            arguments=[str(arg).replace(str(self.job),'$JOB') for arg in argv])
        key = hashlib.sha256(json.dumps(fingerprint,sort_keys=True).encode()).hexdigest()
        self.cache = {'status':'miss','key':key}
        self.entry = self.root/'build-cache'/key
        return self.entry

    def _restore(self, source_dir, argv):
        entry = self._cache_entry(source_dir,argv)
        if entry is None or not (entry/'metadata.json').is_file():
            return False
        metadata = json.loads((entry/'metadata.json').read_text())
        if metadata['source_sha256'] != self.source_sha256 or sha256(entry/'program') != metadata['binary_sha256']:
            return False
        for name,digest in metadata['dependencies'].items():
            if sha256(name) != digest:
                self.cache['reason'] = 'Build dependency changed'
                return False
        target = self.work/'build'
        target.mkdir(mode=0o700,exist_ok=True)
        self.own(target)
        shutil.copyfile(entry/'program',target/metadata['artifact_name'])
        (target/metadata['artifact_name']).chmod(0o700)
        self.own(target/metadata['artifact_name'])
        self.cache.update(status='hit',binary_sha256=metadata['binary_sha256'],
                          compiled_from_job=metadata['compiled_from_job'])
        (entry/'metadata.json').touch()
        return True

    def _save(self):
        # Only known regular ELF executables at the build root are cacheable.
        if self.entry is None:
            return
        build = self.work/'build'
        programs = []
        for path in build.iterdir():
            if path.is_file() and not path.is_symlink() and path.suffix == '' and path.name != 'Makefile':
                with path.open('rb') as stream:
                    if stream.read(4) == b'\x7fELF':
                        programs.append(path)
        if len(programs) != 1:
            self.cache['reason'] = 'Build does not produce exactly one executable'
            return
        dependencies = {}
        allowed = [self.root/'problems', Path(self.env['ASCEND_HOME_PATH']), Path('/usr/include'),
                   Path('/usr/lib'), Path('/usr/lib64'), Path('/lib'), Path('/lib64')]
        depfiles = list(build.rglob('*.o.d'))
        if not depfiles:
            self.cache['reason'] = 'Compiler dependency list unavailable'
            return
        for depfile in depfiles:
            data = depfile.read_text().replace('\\\n',' ')
            for name in shlex.split(data.split(':',1)[1]):
                path = Path(name).resolve()
                if path == (self.job/'kernel.asc').resolve():
                    continue
                if not any(path.is_relative_to(p.resolve()) for p in allowed):
                    self.cache['reason'] = 'Dependency outside installed compiler/problem paths'
                    return
                dependencies[str(path)] = sha256(path)
        if not dependencies:
            return
        program = programs[0]
        self.entry.parent.mkdir(mode=0o700,exist_ok=True)
        temporary = self.entry.with_name(self.entry.name+'.'+self.job.name+'.tmp')
        temporary.mkdir(mode=0o700)
        shutil.copyfile(program,temporary/'program')
        metadata = dict(source_sha256=self.source_sha256,binary_sha256=sha256(program),
                        artifact_name=program.name,compiled_from_job=self.job.name,dependencies=dependencies)
        (temporary/'metadata.json').write_text(json.dumps(metadata,sort_keys=True))
        if self.entry.exists():
            shutil.rmtree(self.entry)
        temporary.rename(self.entry)
        self.cache.update(status='stored',binary_sha256=metadata['binary_sha256'])
        self._prune()

    def _prune(self):
        entries = [p for p in self.entry.parent.iterdir() if p.is_dir() and not p.is_symlink()
                   and re.fullmatch('[0-9a-f]{64}',p.name) and (p/'metadata.json').is_file()]
        entries.sort(key=lambda p:(p/'metadata.json').stat().st_mtime,reverse=True)
        total = 0
        for index,path in enumerate(entries):
            total += sum(p.stat().st_size for p in path.iterdir() if p.is_file())
            if path != self.entry and (index >= 64 or total > 256*1024**2):
                shutil.rmtree(path)

    def run(self, argv, cwd, timeout, env):
        category = 'other'
        configure = argv[0] == 'cmake' and '-S' in argv and '-B' in argv
        build = argv[:2] == ['cmake','--build']
        if configure:
            category = 'configure'
        elif build:
            category = 'compile'
        elif argv[0] == 'msprof':
            group = next((p.split('=',1)[1] for p in argv if p.startswith('--aic-metrics=')),None)
            category = 'profiler_'+str(group)
            if self.profile_mode == 'timing' and group in {'L2Cache','MemoryUB'}:
                return 125, {'stdout':'','stderr':'Skipped by timing profile mode'}, 'profile_skipped'
            if self.profiler is not None and (self.profiler.results or self.profiler.matches(argv)):
                if not self.profiler.results and self.device_lease is not None:
                    self.device_lease.acquire()
                    self.stages['device_wait'] = self.device_lease.wait_seconds
                return self.profiler.run(argv, cwd, timeout, env)
        elif Path(argv[0]).parent == self.work/'build':
            category = 'correctness_and_event'
        elif argv[0] in {'g++','python3'} or Path(argv[0]).name == 'generate':
            category = 'generator'
        preparation = category in {'configure', 'compile', 'generator'}
        # Unrecognized adapter commands conservatively require the device lock.
        if self.device_lease is not None and not preparation:
            self.device_lease.acquire()
            self.stages['device_wait'] = self.device_lease.wait_seconds
        started = time.monotonic()
        started_at = time.time()
        run_env = dict(env)
        run_env['OJ_CPU_PREPARATION'] = '1' if preparation else '0'
        try:
            if configure:
                try:
                    with FileLock(self.root/'build-cache.lock'):
                        self.cached = self._restore(Path(argv[argv.index('-S')+1]),argv)
                except (OSError,ValueError,KeyError) as error:
                    self.cache.update(status='miss',reason='Cache unavailable: '+str(error)[:200])
            if self.cached and (configure or build):
                return 0, {'stdout':'Verified build cache hit: '+self.cache['key']+'\n','stderr':''}, None
            result = self.runner(argv,cwd,timeout,run_env)
            if build and not result[0] and not result[2]:
                try:
                    with FileLock(self.root/'build-cache.lock'):
                        self._save()
                except (OSError,ValueError,KeyError) as error:
                    self.cache.update(status='miss',reason='Cache store unavailable: '+str(error)[:200])
            return result
        finally:
            self.stages[category] = round(self.stages.get(category,0)+time.monotonic()-started,4)
            self.timeline.append(dict(stage=category, started_at=started_at, finished_at=time.time()))

    def emit(self, emit, kind, **fields):
        if kind == 'metrics' and self.profile_mode == 'timing' and fields['metrics']['group'] in {'L2Cache','MemoryUB'}:
            fields['metrics'] = dict(group=fields['metrics']['group'],status='skipped',
                                    reason='快速计时模式：完整评测可采集此组指标')
        if kind == 'metrics' and fields['metrics'].get('status') == 'available':
            metric = fields['metrics']
            group = metric['group']
            if group in ('PipeUtilization', 'L2Cache', 'MemoryUB'):
                attach_launch_metrics(metric, collect_launch_metrics(
                    metric, self.work / ('profile-' + group + '.jsonl'),
                    self.work / ('profile-' + group),
                    self.root / 'problems' / self.pack['id'] / 'profile_metrics.py'))
        if (kind == 'metrics' and self.pack.get('id') == 'pdist'
                and fields['metrics'].get('status') == 'unavailable'
                and fields['metrics'].get('reason') == COUNT_MISMATCH):
            metric = fields['metrics']
            group = metric['group']
            metric['partial_observations'] = collect_partial_profile(
                self.work / ('profile-' + group + '.jsonl'), self.work / ('profile-' + group),
                self.root / 'problems' / self.pack['id'] / 'profile_metrics.py')
        if kind == 'finished':
            if self.device_lease is not None:
                self.device_lease.close()
            fields['evaluation'] = dict(profile_mode=self.profile_mode,stages_seconds=self.stages,
                                        compile_cache=self.cache,stage_timeline=self.timeline)
            if self.device_lease is not None:
                fields['evaluation']['scheduling'] = self.device_lease.snapshot()
            if self.profiler is not None:
                fields['evaluation']['profiler_pipeline'] = self.profiler.report
        emit(kind,**fields)

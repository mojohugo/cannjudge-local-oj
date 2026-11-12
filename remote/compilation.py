"""Bounded Pdist CPU build coordination, separate from device execution policy."""
from pathlib import Path
import time

if __package__:
    from .execution import Execution as BaseExecution
    from .locking import FileLock
else:
    from execution import Execution as BaseExecution
    from locking import FileLock


class Execution(BaseExecution):
    def __init__(self, root, job, work, pack, source, *args, **kwargs):
        super().__init__(root, job, work, pack, source, *args, **kwargs)
        self.configure_request = None
        self.status_emit = None
        self.failure = None
        self.compile_policy = None
        # A local CPU policy, not a problem-pack or device/runtime limit.
        if pack.get('id') == 'pdist':
            self.compile_policy = dict(single_flight=True, source_bytes=len(source),
                heavy_source_threshold_bytes=384*1024,
                compile_timeout_seconds=360 if len(source) >= 384*1024 else 240,
                cache_wait_timeout_seconds=600)

    def _compile_status(self, message):
        if self.status_emit is not None:
            self.status_emit('status', status='compiling', message=message)

    def run(self, argv, cwd, timeout, env):
        if self.compile_policy is None:
            return super().run(argv, cwd, timeout, env)
        configure = argv[0] == 'cmake' and '-S' in argv and '-B' in argv
        build = argv[:2] == ['cmake', '--build']
        if configure:
            self.configure_request = (Path(argv[argv.index('-S')+1]), list(argv))
        flight = None
        try:
            if build:
                timeout = self.compile_policy['compile_timeout_seconds']
                if not self.cached and self.entry is not None and self.configure_request is not None:
                    wait_limit = self.compile_policy['cache_wait_timeout_seconds']
                    self._compile_status(f'等待同缓存键编译，最多 {wait_limit} 秒；等待不占编译预算或 NPU')
                    # Stable lock files live outside the pruned cache: unlinking
                    # a lock inode could let two builders acquire different locks.
                    flight = FileLock(self.root/'build-flight-locks'/(self.entry.name+'.lock'), wait_limit)
                    started, started_at = time.monotonic(), time.time()
                    try:
                        flight.acquire()
                    except OSError as error:
                        self.failure = dict(stage='compile_cache_wait', resource='cpu',
                            reason='timeout' if isinstance(error, TimeoutError) else 'lock_error',
                            timeout_seconds=wait_limit)
                        return -1, dict(stdout='', stderr='Compile cache wait failed: '+str(error)), 'compile_cache_wait_failed'
                    finally:
                        self.stages['compile_cache_wait'] = round(
                            self.stages.get('compile_cache_wait', 0)+time.monotonic()-started, 4)
                        self.timeline.append(dict(stage='compile_cache_wait',
                            started_at=started_at, finished_at=time.time()))
                    # A preceding builder may have published while we waited.
                    # Recheck the executable and all dependencies; never share a
                    # failed build or a stale/corrupted artifact with a waiter.
                    try:
                        with FileLock(self.root/'build-cache.lock'):
                            self.cached = self._restore(*self.configure_request)
                    except (OSError, ValueError, KeyError) as error:
                        self.cache.update(status='miss', reason='Cache unavailable: '+str(error)[:200])
                    self.cache.update(single_flight=True, wait_seconds=self.stages['compile_cache_wait'])
                self._compile_status('编译缓存命中' if self.cached else f'CPU 编译中，墙钟硬上限 {timeout} 秒；未占用 NPU')
            # The existing executor owns cache publication, CPU/NPU classification,
            # resource isolation, device lease, timing and profiler behavior.
            result = super().run(argv, cwd, timeout, env)
            if result[2] == 'timeout':
                if configure or build:
                    stage, resource = ('configure' if configure else 'compile'), 'cpu'
                elif argv[0] in {'g++', 'python3'} or Path(argv[0]).name == 'generate':
                    stage, resource = 'generator', 'cpu'
                elif Path(argv[0]).parent == self.work/'build':
                    stage, resource = 'correctness_and_event', 'npu'
                else:
                    stage, resource = ('profiler' if argv[0] == 'msprof' else 'other'), 'npu'
                self.failure = dict(stage=stage, resource=resource, reason='timeout', timeout_seconds=timeout)
            if build:
                result = (result[0], dict(result[1], stdout=f'CPU compile wall limit: {timeout}s; cache wait is separate.\n'+result[1]['stdout']), result[2])
            return result
        finally:
            if flight is not None:
                flight.close()

    def emit(self, emit, kind, **fields):
        self.status_emit = emit
        if kind == 'status' and fields.get('status') == 'compiling' and self.compile_policy is not None:
            fields['message'] = fields.get('message', '') + f"；CPU 编译硬上限 {self.compile_policy['compile_timeout_seconds']} 秒，同键等待上限 {self.compile_policy['cache_wait_timeout_seconds']} 秒"

        def publish(kind, **fields):
            if kind == 'finished':
                if self.compile_policy is not None:
                    fields['evaluation']['compile_policy'] = self.compile_policy
                if self.failure is not None:
                    fields['evaluation']['failure'] = self.failure
                    if self.failure['stage'] == 'compile_cache_wait':
                        fields.update(status='infrastructure_error', message='等待同键编译失败（CPU 缓存锁），未执行 NPU；请查看编译日志')
                    elif self.failure['stage'] in {'configure', 'compile'}:
                        fields['message'] = f"{'编译' if self.failure['stage'] == 'compile' else '构建配置'}超时（CPU 墙钟上限 {self.failure['timeout_seconds']} 秒），未执行 NPU"
            emit(kind, **fields)

        super().emit(publish, kind, **fields)

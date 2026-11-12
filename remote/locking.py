"""Process-scoped locks for shared cache metadata and an exclusive NPU lease."""
import errno
import os
from pathlib import Path
import time


class FileLock:
    def __init__(self, path, timeout=30):
        self.path, self.timeout = Path(path), timeout
        self.stream = None

    def acquire(self):
        if self.stream is not None:
            return self
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open('a+b')
        deadline = time.monotonic() + self.timeout
        try:
            while True:
                try:
                    if os.name == 'nt':
                        import msvcrt
                        stream.seek(0)
                        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self.stream = stream
                    return self
                except OSError as error:
                    if error.errno not in (errno.EACCES, errno.EAGAIN):
                        raise
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f'Lock wait exceeded {self.timeout} seconds: {self.path.name}') from error
                    time.sleep(0.05)
        except BaseException:
            stream.close()
            raise

    def close(self):
        if self.stream is not None:
            # Closing releases the OS lock, including after failed evaluations.
            self.stream.close()
            self.stream = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *args):
        self.close()


class DeviceLease:
    """Acquire only at the first device command; retain through profiling."""
    def __init__(self, path, emit, timeout=900):
        self.lock = FileLock(path, timeout)
        self.emit = emit
        self.ready_at = self.acquired_at = self.released_at = None
        self.wait_seconds = 0.0
        self.acquired_monotonic = None
        self.held_seconds = 0.0

    def acquire(self):
        if self.acquired_at is not None:
            if self.released_at is not None:
                raise RuntimeError('Device execution attempted after final result')
            return
        self.ready_at = time.time()
        started = time.monotonic()
        self.emit('status', status='waiting', message='编译和数据已准备好，等待独占 NPU')
        self.lock.acquire()
        self.acquired_monotonic = time.monotonic()
        self.wait_seconds = round(self.acquired_monotonic - started, 4)
        self.acquired_at = time.time()
        self.emit('status', status='running', message='已取得 NPU，独占执行正确性检查、计时和性能采集')

    def close(self):
        if self.acquired_at is not None and self.released_at is None:
            self.held_seconds = round(time.monotonic() - self.acquired_monotonic, 4)
            self.released_at = time.time()
        self.lock.close()

    def snapshot(self):
        return dict(npu_exclusive=True, device_ready_at=self.ready_at,
                    device_acquired_at=self.acquired_at, device_released_at=self.released_at,
                    device_wait_seconds=self.wait_seconds, device_held_seconds=self.held_seconds)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

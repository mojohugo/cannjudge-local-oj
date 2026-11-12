"""Reclaim archived job scratch space; never remove source or result records."""
import collections
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import time

JOB_ID = re.compile(r'[0-9a-f]{32}')


def active_jobs(root):
    active = set()
    pattern = re.compile(re.escape(str(root)) + r'/([0-9a-f]{32})(?:/|\x00|$)')
    for proc in Path('/proc').iterdir():
        if not proc.name.isdecimal():
            continue
        try:
            command = (proc/'cmdline').read_bytes().decode(errors='replace')
            active.update(pattern.findall(command))
            args = command.split('\0')
            for i, arg in enumerate(args[:-1]):
                if arg.endswith('/worker.py') and JOB_ID.fullmatch(args[i+1]):
                    active.add(args[i+1])
            links = [proc/'cwd', *list((proc/'fd').iterdir())]
            for link in links:
                try:
                    active.update(pattern.findall(os.readlink(link)))
                except OSError:
                    pass
        except OSError:
            continue
    return active


def safe_tree(path, job):
    if path.is_symlink() or path.resolve().parent != job or not path.is_dir():
        return False
    device = job.stat().st_dev
    for parent, dirs, _ in os.walk(path, followlinks=False):
        for name in dirs:
            child = Path(parent)/name
            if not child.is_symlink() and (child.stat().st_dev != device or os.path.ismount(child)):
                return False
    return True


def cleanup(root, records, *, apply=False, now=None, max_jobs=200,
            retain_hours=24, pressure_hours=1, high_water=.8, target_water=.6,
            active=active_jobs, usage=shutil.disk_usage):
    root = Path(root)
    if root.is_symlink() or not root.is_dir() or len(root.resolve().parts) < 3:
        raise ValueError('Invalid jobs root')
    root = root.resolve()
    now = time.time() if now is None else now
    before = usage(root)
    pressure = before.used / before.total >= high_water
    skipped = collections.Counter()
    removed = []
    protected = active(root)
    for row in sorted(records, key=lambda r: r.get('completed', 0)):
        if len(removed) >= max_jobs:
            break
        identity = row.get('id', '')
        completed = row.get('completed')
        if (not JOB_ID.fullmatch(identity) or not re.fullmatch('[0-9a-f]{64}', row.get('source_sha256', ''))
                or not isinstance(completed, (int, float)) or not math.isfinite(completed)):
            skipped['invalid_record'] += 1
            continue
        age = now - completed
        if age < retain_hours*3600:
            if not pressure or age < pressure_hours*3600 or usage(root).used/usage(root).total <= target_water:
                skipped['retained_recent'] += 1
                continue
        job = root/identity
        if not job.is_dir() or job.is_symlink() or job.resolve().parent != root:
            skipped['missing_or_unsafe'] += 1
            continue
        work, data = job/'work', job/'eval-data'
        bins = [] if data.is_symlink() or not data.is_dir() else [
            p for p in data.glob('*.bin') if not p.is_symlink() and stat.S_ISREG(p.stat().st_mode)]
        if not work.exists() and not bins:
            continue
        if identity in protected or identity in active(root):
            skipped['active'] += 1
            continue
        source = job/'kernel.asc'
        if source.is_symlink() or not source.is_file() or hashlib.sha256(source.read_bytes()).hexdigest() != row['source_sha256']:
            skipped['source_mismatch'] += 1
            continue
        if work.exists() and not safe_tree(work, job):
            skipped['unsafe_work_tree'] += 1
            continue
        if apply:
            # Reclaim regenerable arrays first so diagnostic retention still
            # succeeds when the work filesystem has no free blocks left.
            for p in bins:
                if p.resolve().parent == data.resolve() and not p.is_symlink():
                    p.unlink()
            # Keep exported text diagnostics in addition to the archived OJ result.
            if work.exists():
                for p in work.glob('profile-*.jsonl'):
                    if p.is_file() and not p.is_symlink() and p.stat().st_size <= 4*1024**2:
                        # Same filesystem: retain the existing inode without
                        # allocating a second copy on a full work disk.
                        os.replace(p, job/('retained-'+p.name))
                shutil.rmtree(work)
        removed.append(identity)
    after = usage(root)
    return dict(applied=apply, jobs_cleaned=len(removed), job_ids=removed, skipped=dict(skipped),
                free_before=before.free, free_after=after.free, observed_reclaimed_bytes=max(0, after.free-before.free),
                pressure=pressure, retain_hours=retain_hours, pressure_hours=pressure_hours,
                preserved='kernel.asc, generate.cpp, problem.json, result.json, top-level logs and dataset manifests/TSVs')


def main():
    import fcntl
    from host import JOBS
    os.nice(10)
    request = json.loads(sys.stdin.buffer.read(16*1024**2))
    records = request['records']
    if not isinstance(records, list) or len(records) > 20000:
        raise ValueError('Invalid archive manifest')
    lock_id = hashlib.sha256(str(JOBS.resolve()).encode()).hexdigest()[:16]
    # /dev/shm remains writable when the job filesystem is full.
    with open('/dev/shm/cannjudge-cleanup-'+lock_id+'.lock', 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({'busy': True}))
            return
        report = cleanup(JOBS, records, apply=request.get('apply') is True,
                         max_jobs=min(10000, max(1, int(request.get('max_jobs', 200)))))
        print(json.dumps(report))


if __name__ == '__main__':
    main()

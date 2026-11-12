#!/usr/bin/env python3
"""Trusted SSH coordinator. Uploaded sources compile/run as a non-root account.

This is a private benchmarking worker for trusted authors, not a hostile-code sandbox.
Only this coordinator emits the result protocol; candidate stdout is captured as data.
"""
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import resource
import selectors
import signal
import subprocess
import sys
import time
import ctypes

from host import ROOT, JOBS, LOCK, CONFIG, ISOLATION, account, own
from registry import Registry
from rules import inspect_source

CURRENT_PROCESS = None
START = time.monotonic()


def emit(kind, **fields):
    print(json.dumps(dict(type=kind, **fields), ensure_ascii=False, allow_nan=False), flush=True)


def kill_group(process):
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def terminate(signum, frame):
    if CURRENT_PROCESS is not None:
        kill_group(CURRENT_PROCESS)
    raise SystemExit(128 + signum)


for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
    signal.signal(sig, terminate)


def child_setup(cpu_preparation=False):
    selected = account()
    if cpu_preparation:
        os.nice(5)  # Give host-side NPU timing threads priority over compilation.
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_FSIZE, (512 * 1024**2, 512 * 1024**2))
    resource.setrlimit(resource.RLIMIT_NOFILE, (1024, 1024))
    # RLIMIT_NPROC counts the shared host UID, including tasks outside this
    # notebook's PID namespace. A low UID-wide limit prevents even /bin/true
    # from spawning on ModelArts. Bound this job's process group below instead.
    if ISOLATION != 'trusted_user':
        resource.setrlimit(resource.RLIMIT_NPROC, (1024, 1024))
    resource.setrlimit(resource.RLIMIT_AS, (32 * 1024**3, 32 * 1024**3))
    resource.setrlimit(resource.RLIMIT_CPU, (300, 300))
    if os.geteuid() == 0:
        os.setgroups([])
        os.setgid(selected.pw_gid)
        os.setuid(selected.pw_uid)
    os.umask(0o077)
    if ctypes.CDLL(None).prctl(38, 1, 0, 0, 0) != 0:  # PR_SET_NO_NEW_PRIVS
        raise RuntimeError('Cannot enable no_new_privs')


def process_group_tasks(group):
    total = 0
    for entry in Path('/proc').iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            fields = (entry / 'stat').read_text().rsplit(')', 1)[1].split()
            if int(fields[2]) == group:
                total += int(fields[17])
        except (OSError, ValueError, IndexError):
            continue  # A process may have exited during the snapshot.
    return total


def run(argv, cwd, timeout, env):
    global CURRENT_PROCESS
    # Persist compiler progress even if the notebook is restarted before the
    # coordinator can return its final log over SSH.
    compile_trace = Path(env.get('OJ_JOB_ROOT', cwd)) / 'compile-progress.log' if argv[0] == 'cmake' else None
    if compile_trace:
        with compile_trace.open('a', encoding='utf-8') as trace:
            trace.write('\n' + json.dumps(dict(argv=argv, started=time.time())) + '\n')
    process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               start_new_session=True,
                               preexec_fn=lambda: child_setup(env.get('OJ_CPU_PREPARATION') == '1'))
    CURRENT_PROCESS = process
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, 'stdout')
    selector.register(process.stderr, selectors.EVENT_READ, 'stderr')
    outputs = {'stdout': bytearray(), 'stderr': bytearray()}
    deadline, limit, reason = time.monotonic() + timeout, 1024 * 1024, None
    next_process_check = time.monotonic()
    try:
        while selector.get_map():
            if time.monotonic() > deadline:
                reason = 'timeout'
                break
            if ISOLATION == 'trusted_user' and time.monotonic() >= next_process_check:
                if process_group_tasks(process.pid) > 1024:
                    reason = 'process_limit'
                    break
                next_process_check = time.monotonic() + 1
            for key, _ in selector.select(timeout=0.2):
                data = os.read(key.fileobj.fileno(), 65536)
                if not data:
                    selector.unregister(key.fileobj)
                    continue
                dest = outputs[key.data]
                kept = data[:max(0, limit - len(dest))]
                dest.extend(kept)
                if compile_trace:
                    with compile_trace.open('ab') as trace:
                        trace.write(kept)
                if len(dest) >= limit:
                    reason = 'output_limit'
                    break
            if reason:
                break
        if reason:
            kill_group(process)
        code = process.wait(timeout=5)
        return code, {k: bytes(v).decode('utf-8', errors='replace') for k, v in outputs.items()}, reason
    finally:
        kill_group(process)  # Also clean descendants left after a normal exit.
        process.wait(timeout=5)
        selector.close()
        process.stdout.close()
        process.stderr.close()
        CURRENT_PROCESS = None


def environment(work):
    # Administrator-owned SDK setup. No user-controlled shell text.
    raw = subprocess.check_output(['bash', '-c', 'source "$1" >/dev/null && env -0', 'oj-env', str(ROOT / 'env.sh')], timeout=15,
                                  env={'PATH': '/usr/local/bin:/usr/bin:/bin', 'HOME': str(work), 'LANG': 'C.UTF-8'})
    env = dict(item.decode().split('=', 1) for item in raw.split(b'\0') if item)
    env.update(HOME=str(work), TMPDIR=str(work / 'tmp'), USER=account().pw_name, LOGNAME=account().pw_name,
               OJ_JOBS_ROOT=str(JOBS), OJ_CORES=str(CONFIG.get('cores',40)), OJ_DEVICE=str(CONFIG.get('device',0)))
    # Do not pass SSH connection metadata or root-related configuration into submissions.
    for key in list(env):
        if key.startswith('SSH_') or key in {'SUDO_USER', 'SUDO_COMMAND', 'PYTHONPATH'}:
            env.pop(key, None)
    return env


def main():
    account()  # Validate configured execution identity; never silently relax it.
    identity = sys.argv[1] if len(sys.argv) == 2 else ''
    if not re.fullmatch('[0-9a-f]{32}', identity):
        raise ValueError('Invalid submission identity')
    payload = sys.stdin.buffer.read(14 * 1024**2 + 1)
    if len(payload) > 14 * 1024**2:
        raise ValueError('Request exceeds 14 MiB')
    payload = json.loads(payload)
    registry = Registry(ROOT / 'problems')
    pack = registry.get(payload.get('problem_id', 'cum_sum'))
    if payload.get('problem_sha256') != pack['pack_sha256']:
        raise ValueError('题目包与本地不一致，请更新 worker 后重新提交')
    adapter = registry.adapter(pack['id'])
    source = payload['source'].encode('utf-8')
    generator = payload.get('generator')
    from local_cases import evaluate_local_cases, validate_local_case_indices
    local_case_indices = validate_local_case_indices(pack, payload.get('local_case_indices'), generator)
    from pdist_structured import validate_suite, suite_info, evaluate as evaluate_regression
    regression_suite = validate_suite(pack, payload.get('regression_suite'), generator, local_case_indices)
    if regression_suite is not None and payload.get('regression_spec') != suite_info():
        raise ValueError('Structured recipe differs between web and worker')
    profile_mode = payload.get('profile_mode','full')
    if profile_mode not in {'full','timing'}:
        raise ValueError('Invalid profiling mode')
    if not source or len(source) > (12 if pack['contract'].get('submission_kind') == 'project' else 2) * 1024**2:
        raise ValueError('Invalid source size')
    source.decode('utf-8')
    check = inspect_source(source.decode('utf-8'), pack['contract'])
    emit('checks', checks=check)
    if not check['passed']:
        emit('finished', status='illegal', message='源码检查未通过', elapsed_seconds=time.monotonic() - START)
        return
    if generator is not None and (not isinstance(generator, str) or len(generator.encode('utf-8')) > 128 * 1024):
        raise ValueError('Invalid generator')
    from project import source_digest
    digest = source_digest(source, pack['contract'])
    job = JOBS / identity
    job.mkdir(mode=0o755)
    (job / 'kernel.asc').write_bytes(source)
    work = job / 'work'
    work.mkdir(mode=0o700)
    own(work)
    (work / 'tmp').mkdir(mode=0o700)
    own(work / 'tmp')
    (job / 'problem.json').write_text(json.dumps(pack, ensure_ascii=False, indent=2), encoding='utf-8')
    emit('identity', source_sha256=digest, remote_job=str(job), worker_version='2',
         problem_id=pack['id'], problem_version=pack['version'], problem_sha256=pack['pack_sha256'],
         environment=CONFIG.get('environment',pack['environment']), device=CONFIG.get('device',0),
         cores=CONFIG.get('cores',pack['protocol'].get('cores')),
         physical_cores=CONFIG.get('physical_cores',pack['protocol'].get('physical_cores')),
         isolation=ISOLATION, harness_sha256=pack['pack_sha256'])
    if payload.get('local_ab') is not None:
        from local_ab import evaluate_ab, validate_request
        request = payload['local_ab']
        validate_request(pack, request)
        if regression_suite is not None or generator is not None or local_case_indices != request.get('local_case_indices') or profile_mode != 'timing':
            raise ValueError('Invalid A/B evaluation envelope')
        source_b = payload['source_b'].encode('utf-8')
        if not source_b or len(source_b) > 2 * 1024**2 or not inspect_source(source_b.decode(), pack['contract'])['passed']:
            raise ValueError('Invalid B source')
        evaluate_ab(sys.modules[__name__], adapter, pack, job, {'A':source, 'B':source_b}, request, emit)
        return
    env = environment(work)
    env['OJ_JOB_ROOT'] = str(job)
    from compilation import Execution
    from locking import DeviceLease
    with DeviceLease(LOCK, emit) as lease:
        # Only administrator-verified adapter versions declare a complete plan.
        # New/changed problem packs keep the ordinary sequential path by default.
        groups = CONFIG.get('profiling_plans', {}).get(pack['pack_sha256'])
        execution = Execution(ROOT, job, work, pack, source, env, run, own, profile_mode, lease, groups)
        evaluate = evaluate_regression if regression_suite is not None else evaluate_local_cases
        selection = regression_suite if regression_suite is not None else local_case_indices
        evaluate(adapter, pack, selection, job, work, generator, env, execution.run,
                         lambda kind,**fields:execution.emit(emit,kind,**fields), identity, START, digest)



if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        emit('finished', status='infrastructure_error', message=str(error)[:2000], elapsed_seconds=time.monotonic() - START)

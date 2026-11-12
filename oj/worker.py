import hashlib
import json
import logging
import queue
import re
import shlex
import subprocess
import threading
import time
import uuid

from .store import TERMINAL, UpdatePersistenceError, is_rsi_deployment_gate
from .ssh_connection import SSHConnector, SSHUnavailable

logger = logging.getLogger(__name__)


class SSHJobDisconnected(SSHUnavailable):
    pass


class DatasetIdentityError(ValueError):
    pass


def validate_dataset_identity(spec, dataset, *, custom=False, local_case_indices=None, regression_suite=None):
    from remote.pdist_structured import validate_suite, dataset_error
    if regression_suite is not None:
        validate_suite(spec, regression_suite, 'custom' if custom else None, local_case_indices)
        error = dataset_error(dataset)
        if error:
            raise DatasetIdentityError(error)
        return
    if isinstance(dataset, dict) and dataset.get('mode') == 'structured_regression':
        raise DatasetIdentityError('Unexpected structured regression for ordinary request')
    if custom or 'dataset_revision' not in spec:
        return
    expected_version = spec['dataset_revision']
    expected_profile = spec.get('dataset_profile_sha256')
    if not isinstance(expected_version, str) or not expected_version or not isinstance(expected_profile, str) or not expected_profile:
        raise DatasetIdentityError('默认数据集身份配置不完整：题目必须声明 dataset_revision 和 dataset_profile_sha256')
    if not isinstance(dataset, dict):
        raise DatasetIdentityError('默认数据集身份缺失：worker 未返回有效的 dataset 元数据')
    mismatches = []
    for field, expected in [('version', expected_version), ('profile_sha256', expected_profile)]:
        actual = dataset.get(field)
        if actual != expected:
            mismatches.append(f'{field}: expected={expected!r}, received={actual!r}')
    if local_case_indices is not None:
        cases = dataset.get('cases', [])
        if (dataset.get('mode') != 'local_subset'
                or dataset.get('local_case_indices') != local_case_indices
                or dataset.get('original_case_count') != len(spec['cases'])
                or not re.fullmatch(r'[0-9a-f]{64}', str(dataset.get('canonical_sha256', '')))
                or [c.get('index') for c in cases] != list(range(1, len(local_case_indices)+1))
                or [c.get('original_index') for c in cases] != local_case_indices
                or any(c.get('shape') != spec['cases'][i-1]['shape'] or c.get('dtype') != spec['cases'][i-1]['dtype']
                       for c, i in zip(cases, local_case_indices))):
            mismatches.append('local_case_indices: worker selection differs from request')
        if spec['id'] == 'topk' and any(
                c.get('params') != {k: spec['cases'][i-1][k] for k in ('k', 'largest')}
                or any(c.get(k) != spec['cases'][i-1][k] for k in ('k', 'largest'))
                or not isinstance(c.get('sha256'), dict)
                or c.get('sha256', {}).get('x') != spec['cases'][i-1]['expected_input_sha256']
                or any(not re.fullmatch('[0-9a-f]{64}', str(c.get('sha256', {}).get(k, '')))
                       for k in ('y', 'index'))
                or c.get('generation') != spec['cases'][i-1]['generation']
                for c, i in zip(cases, local_case_indices)):
            mismatches.append('TopK subset parameters/input/value/index provenance differs from request')
    elif dataset.get('mode') == 'local_subset' or dataset.get('local_case_indices') is not None:
        mismatches.append('unexpected local subset for full-suite request')
    if mismatches:
        raise DatasetIdentityError('默认数据集身份不一致：' + '; '.join(mismatches))


class QueueWorker:
    def __init__(self, settings, store):
        self.settings = settings
        self.store = store
        self.stop_event = threading.Event()
        self.threads = []
        self.worker_count = max(1, min(4, int(settings.evaluation_workers)))
        self.connector = SSHConnector(settings)

    def start(self):
        self.store.recover()
        self.threads = [threading.Thread(target=self.loop, name=f'ssh-judge-{i+1}', daemon=True)
                        for i in range(self.worker_count)]
        for thread in self.threads:
            thread.start()

    def stop(self):
        self.stop_event.set()
        deadline = time.monotonic() + 2
        for thread in self.threads:
            thread.join(timeout=max(0, deadline - time.monotonic()))

    def loop(self):
        while not self.stop_event.is_set():
            try:
                identity = self.store.claim()
            except Exception as error:
                if is_rsi_deployment_gate(error):
                    logger.info('Evaluation claims paused by RSI deployment gate; waiting for idle window')
                    self.stop_event.wait(5)
                    continue
                # A failed claim rolls back its transaction. In particular,
                # SQLite writer contention must not permanently lose a worker.
                logger.exception('Could not claim evaluation job; retrying')
                self.stop_event.wait(1)
                continue
            if not identity:
                self.stop_event.wait(0.5)
                continue
            try:
                self.evaluate(identity)
            except UpdatePersistenceError as error:
                # The exception owns the exact snapshot whose write failed. Never
                # replace a complete result with an older row or rerun the NPU job.
                status, result = error.status, error.result
                if status not in TERMINAL:
                    status = 'interrupted' if self.stop_event.is_set() else 'infrastructure_error'
                    result = result if result is not None else {}
                    result.update(status=status, message=str(error)[:2000])
                if not self.persist_pending_result(identity, status, result):
                    return
            except Exception as error:
                # Retry saving the failure, not executing the submitted code.
                while True:
                    try:
                        previous = self.store.get(identity)['result']
                        previous['message'] = str(error)[:2000]
                        self.store.update(identity, 'interrupted' if self.stop_event.is_set() else 'infrastructure_error', previous)
                        break
                    except Exception:
                        logger.exception('Could not save failure for evaluation job %s; retrying', identity)
                        if self.stop_event.wait(1):
                            return

    def persist_pending_result(self, identity, status, result):
        while not self.stop_event.is_set():
            try:
                self.store.update(identity, status, result)
                return True
            except UpdatePersistenceError:
                logger.exception('Database still locked; retaining result for evaluation job %s', identity)
                if self.stop_event.wait(1):
                    break
            except Exception:
                logger.exception('Unrecoverable result persistence error for evaluation job %s; result files retained', identity)
                return False
        return False

    def evaluate(self, identity):
        history = []
        maximum = 1 if self.store.get(identity)['result'].get('local_ab_request') else max(1, min(5, int(self.settings.ssh_job_attempts)))
        for attempt in range(1, maximum + 1):
            remote_id = identity if attempt == 1 else uuid.uuid4().hex
            try:
                return self.evaluate_once(identity, remote_id, attempt, history)
            except (SSHJobDisconnected, BrokenPipeError) as error:
                previous = self.store.get(identity)['result']
                history.append(dict(attempt=attempt, remote_id=remote_id, error=str(error)[:2000],
                                    completed_cases=len(previous.get('cases', []))))
                previous['attempt_history'] = history.copy()
                previous['message'] = 'SSH 连接中断，保留本次记录并准备恢复连接'
                self.store.update(identity, 'connecting', previous)
                self.archive_attempt(identity, attempt, previous)
                if attempt == maximum or self.stop_event.is_set():
                    raise SSHUnavailable(f'SSH 评测连续中断，已达到 {attempt} 次尝试上限。{error}') from error

    def archive_attempt(self, identity, attempt, previous):
        directory = self.settings.state_dir/'submissions'/identity
        if not directory.resolve().is_relative_to((self.settings.state_dir/'submissions').resolve()):
            raise ValueError('Invalid attempt archive path')
        archive = directory/'attempts'/str(attempt)
        archive.mkdir(parents=True, exist_ok=True)
        (archive/'partial-result.json').write_text(json.dumps(previous,ensure_ascii=False,indent=2),encoding='utf-8')
        for path in list(directory.glob('*.log')) + list(directory.glob('*.jsonl')):
            if path.is_file() and path.name != 'connection.log':
                path.rename(archive/path.name)

    def evaluate_once(self, identity, remote_id, attempt, history):
        settings = self.settings
        if not re.fullmatch(r'[0-9a-f]{32}', identity):
            raise ValueError('Invalid submission ID')
        directory = settings.state_dir / 'submissions' / identity
        source = (directory / 'kernel.asc').read_bytes()
        from .problem import problem, check_source
        row = self.store.get(identity)
        profile_mode = row['result'].get('profile_mode','full')
        spec = problem(row['problem_id'])
        from remote.project import source_digest
        digest = source_digest(source, spec['contract'])
        if row['problem_sha256'] and spec['pack_sha256'] != row['problem_sha256']:
            raise ValueError('排队后题目包已更新，请重新提交以采用新规则')
        generator_path = directory / 'generate.cpp'
        generator = generator_path.read_text(encoding='utf-8') if generator_path.exists() else None
        from remote.local_cases import validate_local_case_indices
        local_case_indices = validate_local_case_indices(spec, row['result'].get('local_case_indices'), generator)
        from remote.pdist_structured import validate_suite, suite_info
        regression_suite = validate_suite(spec, row['result'].get('regression_suite'), generator, local_case_indices)
        if regression_suite is not None and row['result'].get('regression_spec') != suite_info():
            raise ValueError('Structured recipe changed after enqueue; resubmit')
        context = dict(suite='structured_regression' if regression_suite is not None else 'local_subset' if local_case_indices is not None else 'custom' if generator is not None else 'official_shapes', problem_id=spec['id'], problem_version=spec['version'], problem_sha256=spec['pack_sha256'], problem=spec,
                       profile_mode=profile_mode,
                       execution_target=dict(ssh_host=settings.ssh_host, remote_root=settings.remote_root),
                       submission_kind=spec['contract'].get('submission_kind', 'kernel'))
        if local_case_indices is not None:
            context['local_case_indices'] = local_case_indices
        if regression_suite is not None:
            context.update(regression_suite=regression_suite, regression_spec=suite_info())
        ab_request = row['result'].get('local_ab_request')
        if regression_suite is not None and ab_request is not None:
            raise ValueError('Cannot combine structured regression and A/B')
        ab_payload = {}
        if ab_request is not None:
            from remote.local_ab import validate_request
            validate_request(spec, ab_request)
            source_b = (directory/'kernel-b.asc').read_bytes()
            if (hashlib.sha256(source).hexdigest() != ab_request['sources']['A']['source_sha256']
                    or hashlib.sha256(source_b).hexdigest() != ab_request['sources']['B']['source_sha256']
                    or not check_source(source_b.decode(), spec['id'])['passed']):
                raise ValueError('A/B source snapshot mismatch')
            ab_payload = dict(local_ab=ab_request, source_b=source_b.decode())
            context.update(suite='local_ab', local_ab_request=ab_request)
            if ab_request.get('custom_case_indices') is not None:
                if ab_request.get('execution_target') != context['execution_target']:
                    raise ValueError('Custom A/B archived data belongs to another worker')
                context['custom_case_indices'] = ab_request['custom_case_indices']
        check = check_source(source.decode('utf-8'), spec['id'])
        (directory / 'checks.json').write_text(json.dumps(check, ensure_ascii=False, indent=2), encoding='utf-8')
        if not check['passed']:
            result = dict(**context, status='illegal', cases=[], source_sha256=digest, checks=check,
                          message='源码检查未通过：' + '; '.join(i['message'] for i in check['issues'] if i['severity']=='error'))
            (directory / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
            self.store.update(identity, 'illegal', result)
            return
        def connection_update(message, connection):
            value = self.store.get(identity)['result']
            value.update(message=message, ssh=connection, attempt=attempt, attempt_history=history.copy())
            self.store.update(identity, 'connecting', value)
            with (directory/'connection.log').open('a',encoding='utf-8') as stream:
                stream.write(json.dumps({**connection,'time':time.time(),'message':message},ensure_ascii=False)+'\n')
        connection = self.connector.ensure_ready(connection_update, self.stop_event)
        remote_command = 'python3 ' + shlex.quote(settings.remote_root + '/worker.py') + ' ' + remote_id
        command = self.connector.command(remote_command)
        events = queue.Queue()
        with (directory / 'ssh.stderr.log').open('wb') as stderr:
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr)
            def read_output():
                try:
                    for line in iter(process.stdout.readline, b''):
                        if len(line) > 3 * 1024 * 1024:
                            events.put(RuntimeError('Worker response exceeded limit'))
                            break
                        events.put(line)
                finally:
                    events.put(None)
            reader = threading.Thread(target=read_output, daemon=True)
            reader.start()
            try:
                process.stdin.write(json.dumps({'source': source.decode('utf-8'), 'generator': generator,
                                               'problem_id': spec['id'], 'problem_sha256': spec['pack_sha256'],
                                               'profile_mode':profile_mode, **ab_payload,
                                               **({'regression_suite':regression_suite, 'regression_spec':suite_info()} if regression_suite is not None else {}),
                                               **({'local_case_indices':local_case_indices} if local_case_indices is not None else {})}, ensure_ascii=False).encode('utf-8'))
                process.stdin.close()
            except OSError as error:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=10)
                try:
                    process.stdin.close()
                except OSError:
                    pass
                raise SSHJobDisconnected('SSH 在上传源码时中断') from error
            deadline = time.monotonic() + 3600
            result = {**context, 'cases': [], 'source_sha256': digest, 'checks': check,
                      'ssh':connection, 'attempt':attempt, 'attempt_history':history.copy()}
            completed = False
            try:
                with (directory / 'events.jsonl').open('wb') as archive:
                    while True:
                        if self.stop_event.is_set():
                            self.store.update(identity, 'interrupted', result)
                            return
                        if time.monotonic() > deadline:
                            raise TimeoutError('SSH 评测超过 60 分钟；远端仍由独立超时与设备锁保护，不会自动重试')
                        try:
                            line = events.get(timeout=0.5)
                        except queue.Empty:
                            continue
                        if line is None:
                            break
                        if isinstance(line, Exception):
                            raise line
                        archive.write(line)
                        archive.flush()
                        message = json.loads(line)
                        kind = message.get('type')
                        if kind == 'status':
                            if message['status'] not in {'waiting','generating','compiling','running'}:
                                raise ValueError('Unexpected worker status')
                            result['message'] = message.get('message', '')
                            self.store.update(identity, message['status'], result)
                        elif kind == 'identity':
                            if message['source_sha256'] != digest:
                                raise ValueError('Remote source digest does not match upload')
                            if message.get('problem_id') != spec['id'] or message.get('problem_sha256') != spec['pack_sha256']:
                                raise ValueError('Remote problem pack differs from submission snapshot')
                            result['environment'] = message
                        elif kind == 'compile_log':
                            (directory / 'compile.log').write_text(message['text'], encoding='utf-8')
                        elif kind == 'generator_log':
                            (directory / 'generator.log').write_text(message['text'], encoding='utf-8')
                        elif kind == 'dataset':
                            dataset = message.get('dataset')
                            if ab_request is not None and ab_request.get('custom_case_indices') is not None:
                                from remote.custom_ab import dataset_error
                                error = dataset_error(dataset, ab_request)
                                if error:
                                    raise DatasetIdentityError(error)
                            else:
                                validate_dataset_identity(spec, dataset, custom=generator is not None, local_case_indices=local_case_indices, regression_suite=regression_suite)
                            result['dataset'] = dataset
                            self.store.update(identity, 'generating' if generator is not None else 'waiting', result)
                        elif kind == 'case':
                            case = message['case']
                            index = case['index']
                            if type(index) is not int or index != len(result['cases']) + 1 or not 1 <= index <= len(result['dataset']['cases']):
                                raise ValueError('Worker case ordering is invalid')
                            selected = ab_request.get('custom_case_indices') if ab_request and ab_request.get('custom_case_indices') is not None else local_case_indices
                            if selected is not None and case.get('original_index') != selected[index-1]:
                                raise DatasetIdentityError('Worker original case index differs from selection')
                            (directory / f'case-{index:02d}.jsonl').write_text(message.get('raw', ''), encoding='utf-8')
                            (directory / f'case-{index:02d}.stderr.log').write_text(message.get('stderr', ''), encoding='utf-8')
                            result['cases'].append(case)
                            self.store.update(identity, 'running', result)
                        elif kind == 'local_ab':
                            from remote.local_ab import validate_report
                            validate_report(message['report'], ab_request, result.get('dataset'))
                            result['local_ab'] = message['report']
                        elif kind == 'metrics':
                            metric = message['metrics']
                            result.setdefault('metrics', {})[metric['group']] = metric
                            self.store.update(identity, 'running', result)
                        elif kind == 'checks':
                            result['checks'] = message['checks']
                        elif kind == 'finished':
                            status = message['status']
                            if status not in TERMINAL:
                                raise ValueError('Unexpected final status')
                            if status in {'accepted', 'wrong_answer', 'compatibility_mismatch'} and len(result['cases']) != len(result['dataset']['cases']):
                                raise ValueError('Incomplete problem test-suite result')
                            if ab_request is not None and status == 'accepted' and result.get('local_ab', {}).get('status') != 'available':
                                raise ValueError('Missing complete A/B diagnostic')
                            if status == 'accepted' and any(c['status'] != 'accepted' for c in result['cases']):
                                raise ValueError('Accepted result contains failed cases')
                            result['passed'] = sum(c['status'] == 'accepted' for c in result['cases'])
                            result['message'] = message.get('message', '')
                            result['elapsed_seconds'] = message.get('elapsed_seconds')
                            if message.get('evaluation'):
                                result['evaluation'] = message['evaluation']
                            result['status'] = status
                            completed = True
                        else:
                            raise ValueError('Unknown worker message')
                returncode = process.wait(timeout=10)
                if not completed or returncode != 0:
                    error = (directory / 'ssh.stderr.log').read_text(encoding='utf-8', errors='replace')[-3000:]
                    raise SSHJobDisconnected(f'SSH / worker exited without a complete result (exit {returncode}). {error}')
                (directory / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
                self.store.update(identity, result['status'], result)
                from .scoring import maybe_save_baseline
                try:
                    maybe_save_baseline(settings.state_dir, identity, result)
                except Exception:
                    logger.exception('Could not save comparison baseline for %s; completed result retained', identity)
            except DatasetIdentityError as error:
                result.update(status='data_error', passed=0, message=str(error)[:2000])
                (directory / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
                self.store.update(identity, 'data_error', result)
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=10)

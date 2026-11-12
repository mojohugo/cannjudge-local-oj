import hashlib
import json
import logging
import shutil
import sqlite3
import time
from contextlib import contextmanager, nullcontext

from .local_requests import LOCAL_OWNER, RequestConflict, request_config, validate_request_id

TERMINAL = {'accepted', 'wrong_answer', 'compile_error', 'runtime_error', 'time_limit',
            'infrastructure_error', 'data_error', 'illegal', 'interrupted', 'cancelled', 'compatibility_mismatch'}


class QueueFull(ValueError):
    pass


UPDATE_RETRY_SECONDS = 60.0
UPDATE_BUSY_SECONDS = 1.0
ENQUEUE_BUSY_SECONDS = 2.0


class SubmissionBusy(ValueError):
    """Admission failed BEFORE a transaction or archive was created."""
    def __init__(self, request_id=None):
        self.detail = dict(error='local_submission_busy', retryable=True, enqueued=False,
                           retry_after_seconds=1, wait_limit_seconds=ENQUEUE_BUSY_SECONDS)
        if request_id is not None:
            self.detail['request_id'] = request_id
        self.detail['message'] = ('This call created no evaluation. Retry after a short delay; '
                                  'reuse the same request_id/config when supplied. '
                                  'Existing tasks are unaffected.')
        super().__init__(json.dumps(self.detail, separators=(',', ':')))


def is_sqlite_busy(error):
    code = getattr(error, 'sqlite_errorcode', None)
    if code is not None:
        return (code & 0xff) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
    message = str(error).lower()
    return (message in ('database is locked', 'database table is locked', 'database schema is locked')
            or message.startswith(('database table is locked: ', 'database schema is locked: ')))


def is_rsi_deployment_gate(error):
    """Return true for the controller's intentional claim-pause rejection.

    RSI installs short-lived SQLite triggers while an activation waits for an
    idle window.  A claim rejected by those triggers is expected backpressure,
    not a failed evaluation or an upload whose state needs recovery.
    """
    return (isinstance(error, sqlite3.IntegrityError)
            and str(error).startswith('RSI deployment: waiting for '))


class UpdatePersistenceError(RuntimeError):
    def __init__(self, identity, status, encoded, elapsed):
        super().__init__(f'Database write remained locked after {elapsed:.1f}s; pending status={status}')
        self.identity, self.status = identity, status
        self.result = json.loads(encoded) if encoded is not None else None


class Store:
    def __init__(self, directory):
        self.directory = directory
        self.path = directory / 'oj.sqlite3'
        directory.mkdir(parents=True, exist_ok=True)
        (directory / 'submissions').mkdir(exist_ok=True)
        with self.db() as db:
            db.executescript('''
              PRAGMA journal_mode=WAL;
              CREATE TABLE IF NOT EXISTS submissions (
                id TEXT PRIMARY KEY, created REAL NOT NULL, updated REAL NOT NULL,
                status TEXT NOT NULL, label TEXT NOT NULL, sha256 TEXT NOT NULL,
                bytes INTEGER NOT NULL, result TEXT NOT NULL DEFAULT '{}');
              CREATE TABLE IF NOT EXISTS local_submission_requests (
                owner TEXT NOT NULL, request_id TEXT NOT NULL,
                config_sha256 TEXT NOT NULL, config_json TEXT NOT NULL,
                submission_id TEXT NOT NULL REFERENCES submissions(id), created REAL NOT NULL,
                PRIMARY KEY(owner, request_id));
              CREATE TABLE IF NOT EXISTS sessions (
                digest TEXT PRIMARY KEY, csrf TEXT NOT NULL, expires REAL NOT NULL);
            ''')
            columns = {r['name'] for r in db.execute('PRAGMA table_info(submissions)')}
            if 'problem_id' not in columns:
                db.execute("ALTER TABLE submissions ADD COLUMN problem_id TEXT NOT NULL DEFAULT 'cum_sum'")
            if 'problem_version' not in columns:
                db.execute("ALTER TABLE submissions ADD COLUMN problem_version TEXT NOT NULL DEFAULT 'legacy'")
            if 'problem_sha256' not in columns:
                db.execute("ALTER TABLE submissions ADD COLUMN problem_sha256 TEXT NOT NULL DEFAULT ''")
            db.execute('CREATE INDEX IF NOT EXISTS submissions_status_created ON submissions(status,created,id)')
            db.execute('CREATE INDEX IF NOT EXISTS submissions_created ON submissions(created DESC)')
            db.execute('CREATE INDEX IF NOT EXISTS submissions_problem_created ON submissions(problem_id,created DESC)')
            db.execute('CREATE INDEX IF NOT EXISTS submissions_problem_status_signature ON submissions(problem_id,status,id,updated)')

    @contextmanager
    def db(self, timeout=15):
        db = sqlite3.connect(self.path, timeout=timeout)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @contextmanager
    def enqueue_transaction(self, request_id=None):
        """Bound admission lock waiting, never retry an uncertain transaction."""
        with self.db(timeout=ENQUEUE_BUSY_SECONDS) as db:
            started = time.monotonic()
            try:
                db.execute('BEGIN IMMEDIATE')
            except sqlite3.OperationalError as error:
                if not is_sqlite_busy(error):
                    raise
                logging.getLogger(__name__).warning(
                    'local_submission_busy at=%.3f waited_seconds=%.3f enqueued=false',
                    time.time(), time.monotonic() - started)
                raise SubmissionBusy(request_id) from error
            # Errors after admission (especially commit/close) must not be
            # reported as enqueued=false, even if they mention SQLite BUSY.
            yield db

    @staticmethod
    def row(row):
        if row is None:
            return None
        result = dict(row)
        result['result'] = json.loads(result['result'])
        return result

    def get(self, identity, *, queue_info=False):
        with self.db() as db:
            if queue_info:
                db.execute('BEGIN')  # Row and queue counts share one read snapshot.
            row = self.row(db.execute('SELECT * FROM submissions WHERE id=?', (identity,)).fetchone())
            if queue_info and row is not None:
                self._queue_info(db, [row])
            return row

    def get_by_request_id(self, request_id, *, owner=LOCAL_OWNER, queue_info=True):
        validate_request_id(request_id)
        with self.db() as db:
            db.execute('BEGIN')
            row = self.row(db.execute('SELECT s.* FROM local_submission_requests r '
                'JOIN submissions s ON s.id=r.submission_id WHERE r.owner=? AND r.request_id=?',
                (owner, request_id)).fetchone())
            if row is not None and queue_info:
                self._queue_info(db, [row])
            return row

    def listing(self, limit=100, problem_id=None, *, queue_info=False):
        with self.db() as db:
            if queue_info:
                db.execute('BEGIN')
            if problem_id is not None:
                rows = [self.row(r) for r in db.execute('SELECT * FROM submissions WHERE problem_id=? ORDER BY created DESC LIMIT ?', (problem_id, limit))]
            else:
                rows = [self.row(r) for r in db.execute('SELECT * FROM submissions ORDER BY created DESC LIMIT ?', (limit,))]
            if queue_info:
                self._queue_info(db, rows)
            return rows

    @staticmethod
    def _queue_info(db, rows):
        from .queue_status import queue_fields
        active = 0
        if any(row['status'] == 'queued' for row in rows):
            active = db.execute('SELECT count(*) FROM submissions WHERE status NOT IN ('
                                + ','.join('?' * (len(TERMINAL)+1)) + ')',
                                (*TERMINAL, 'queued')).fetchone()[0]
        for row in rows:
            ahead = 'unknown'
            if row['status'] == 'queued':
                # Match claim()'s global (created, id) order, independent of the
                # result list's problem filter/limit. Active workers occupy slots.
                ahead = active + db.execute("SELECT count(*) FROM submissions WHERE status='queued' "
                    'AND (created,id) < (?,?)', (row['created'], row['id'])).fetchone()[0]
            row.update(queue_fields(row, ahead))

    def enqueue(self, identity, label, digest, source, capacity, generator=None, problem_id='cum_sum', profile_mode='full', *, connection=None, local_case_indices=None, regression_suite=None, request_id=None, request_owner=LOCAL_OWNER):
        from .problem import problem
        if profile_mode not in {'full','timing'}:
            raise ValueError('profile_mode 必须是 full 或 timing')
        spec = problem(problem_id)
        from remote.local_cases import validate_local_case_indices
        local_case_indices = validate_local_case_indices(spec, local_case_indices, generator)
        from remote.pdist_structured import validate_suite, suite_info
        regression_suite = validate_suite(spec, regression_suite, generator, local_case_indices)
        if generator is not None and spec.get('custom_generator_supported') is False:
            raise ValueError('此题暂未开放自定义数据生成器')
        if spec['contract'].get('submission_kind') == 'project':
            from remote.project import normalize_files
            _, actual, source = normalize_files(json.loads(source), spec['contract']['editable_files'])
            if digest != actual:
                raise ValueError('工程源码哈希不一致')
        regression = suite_info() if regression_suite is not None else None
        if request_id is not None:
            validate_request_id(request_id)
            if not isinstance(request_owner, str) or not request_owner:
                raise ValueError('Local request owner is required')
            if connection is not None:
                raise ValueError('Local request keys require a standalone enqueue transaction')
            if spec['contract'].get('submission_kind') == 'project':
                raise ValueError('Local request keys currently support submit_kernel only')
            # Regression disables profiling; both public spellings resolve here.
            if regression is not None:
                profile_mode = 'timing'
            fingerprint, config_json = request_config(spec, source, generator, profile_mode,
                                                       local_case_indices, regression)
            if digest != hashlib.sha256(source).hexdigest():
                raise ValueError('Source SHA does not match archived bytes')
        # An official precheck links both rows in the same transaction. Other
        # callers retain the original standalone enqueue behavior.
        created_path = None
        try:
            with self.enqueue_transaction(request_id) if connection is None else nullcontext(connection) as db:
                if request_id is not None:
                    previous = db.execute('SELECT config_sha256,submission_id FROM local_submission_requests '
                        'WHERE owner=? AND request_id=?', (request_owner, request_id)).fetchone()
                    if previous is not None:
                        if previous['config_sha256'] != fingerprint:
                            raise RequestConflict(request_id, previous['submission_id'])
                        return previous['submission_id']
                pending = db.execute("SELECT count(*) FROM submissions WHERE status NOT IN (" + ','.join('?' * len(TERMINAL)) + ')', tuple(TERMINAL)).fetchone()[0]
                if pending >= capacity:
                    raise QueueFull('评测队列已满，请稍后再提交')
                path = self.directory / 'submissions' / identity
                path.mkdir()
                created_path = path
                (path / 'kernel.asc').write_bytes(source)
                if spec['contract'].get('submission_kind') == 'project':
                    (path / 'project.json').write_bytes(source)
                (path / 'problem.json').write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding='utf-8')
                if generator is not None:
                    (path / 'generate.cpp').write_text(generator, encoding='utf-8')
                now = time.time()
                result = {'suite': 'structured_regression' if regression_suite is not None else 'local_subset' if local_case_indices is not None else 'custom' if generator is not None else 'official_shapes', 'profile_mode':profile_mode,
                          'problem_id': problem_id, 'problem_version': spec['version'], 'problem_sha256': spec['pack_sha256'],
                          'problem': spec, 'submission_kind': spec['contract'].get('submission_kind', 'kernel')}
                if local_case_indices is not None:
                    result['local_case_indices'] = local_case_indices
                if regression_suite is not None:
                    result.update(regression_suite=regression_suite, regression_spec=regression)
                db.execute('INSERT INTO submissions(id,created,updated,status,label,sha256,bytes,result,problem_id,problem_version,problem_sha256) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                           (identity, now, now, 'queued', label, digest, len(source), json.dumps(result),
                            problem_id, spec['version'], spec['pack_sha256']))
                if request_id is not None:
                    db.execute('INSERT INTO local_submission_requests VALUES(?,?,?,?,?,?)',
                        (request_owner, request_id, fingerprint, config_json, identity, now))
            return identity
        except BaseException:
            # No key is consumed by capacity, archive or transaction failures.
            # A transport/close/cancellation error may arrive AFTER commit.
            # Never remove a committed archive, or guess when DB state is unknown.
            if request_id is not None and created_path is not None:
                try:
                    with self.db() as check:
                        persisted = check.execute('SELECT 1 FROM submissions WHERE id=?', (identity,)).fetchone()
                except (sqlite3.Error, OSError):
                    pass
                else:
                    if not persisted:
                        shutil.rmtree(created_path, ignore_errors=True)
            raise

    def claim(self):
        with self.db() as db:
            if not db.execute("SELECT 1 FROM submissions WHERE status='queued' LIMIT 1").fetchone():
                return None
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT id FROM submissions WHERE status='queued' ORDER BY created,id LIMIT 1").fetchone()
            if not row:
                return None
            db.execute("UPDATE submissions SET status='connecting',updated=? WHERE id=?", (time.time(), row['id']))
            return row['id']

    def update(self, identity, status, result=None):
        encoded = json.dumps(result, ensure_ascii=False, allow_nan=False) if result is not None else None
        started = time.monotonic()
        deadline, delay = started + UPDATE_RETRY_SECONDS, 0.05
        # This bounds lock retries, not a blocking filesystem fsync inside SQLite.
        while True:
            try:
                with self.db(timeout=min(UPDATE_BUSY_SECONDS, max(0, deadline-time.monotonic()))) as db:
                    if encoded is None:
                        db.execute('UPDATE submissions SET status=?,updated=? WHERE id=?', (status, time.time(), identity))
                    else:
                        db.execute('UPDATE submissions SET status=?,updated=?,result=? WHERE id=?',
                                   (status, time.time(), encoded, identity))
                return
            except sqlite3.OperationalError as error:
                if not is_sqlite_busy(error):
                    raise
                remaining = deadline-time.monotonic()
                if remaining <= 0:
                    raise UpdatePersistenceError(identity, status, encoded, time.monotonic()-started) from error
                time.sleep(min(delay, remaining))
                delay = min(1.0, delay*2)

    def recover(self):
        with self.db() as db:
            db.execute("UPDATE submissions SET status='interrupted', updated=? WHERE status IN ('connecting','waiting','generating','compiling','running')", (time.time(),))

    def cancel_queued(self, identity):
        with self.db() as db:
            return db.execute("UPDATE submissions SET status='cancelled',updated=? WHERE id=? AND status='queued'", (time.time(), identity)).rowcount == 1

"""Durable official queue with pinned accounts and optional local validation."""
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
import uuid

from .config import ROOT
from .official_archive import OfficialArchive
from .official_accounts import OfficialAccounts, account_filter
from .official_quota import policy as quota_policy
from .problem import problem
from .store import TERMINAL as LOCAL_TERMINAL, QueueFull, is_rsi_deployment_gate
from .official_targets import configuration, problem_url, project_template, normalize_project

ACTIVE = ('validating', 'submitting', 'submitted', 'judging')
TERMINAL = {'completed', 'failed', 'login_required', 'rate_limited', 'outcome_unknown', 'local_failed'}
logger = logging.getLogger(__name__)
class BrowserRunner:
    """The user's existing one-click login is used only by the browser helper."""
    def __init__(self, settings):
        self.settings = settings

    def run(self, payload, emit):
        config = configuration()
        node = shutil.which('node')
        if not node:
            raise RuntimeError('Node.js 不可用，尚未启动官方提交')
        helper = (ROOT/config['helper_script']).resolve()
        if not helper.is_file():
            raise RuntimeError('一键提交辅助脚本不存在，尚未启动官方提交')
        env = os.environ.copy()
        if payload.get('browser_state_dir'):
            env['CANNJUDGE_BROWSER_STATE_DIR'] = payload['browser_state_dir']
        payload = dict(payload, helper_script=str(helper))
        command = [node, str(ROOT/'scripts/official-browser.mjs')]
        with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True, encoding='utf-8', env=env,
                              creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)) as process:
            # Drain diagnostics, but never return browser internals or authentication data.
            def drain():
                for _ in process.stderr:
                    pass
            stderr = threading.Thread(target=drain, daemon=True)
            stderr.start()
            process.stdin.write(json.dumps(payload, ensure_ascii=False)+'\n')
            process.stdin.close()
            timer = threading.Timer(60 if payload['action'] in ('check','quota') else 1800 if payload['action']=='history' else 1020, process.kill)
            timer.daemon = True
            timer.start()
            done = False
            try:
                for line in process.stdout:
                    if not line.startswith('OJ_OFFICIAL '):
                        continue
                    event = json.loads(line[len('OJ_OFFICIAL '):])
                    done |= event.get('event') == 'done'
                    emit(event)
                process.wait(timeout=10)
                if not done:
                    raise RuntimeError('官方提交连接中断；请查询原请求，不要自动重发源码')
            finally:
                timer.cancel()


class OfficialJudge:
    def __init__(self, settings, store, runner=None):
        self.settings, self.store = settings, store
        self.directory = settings.state_dir/'official-submissions'
        self.runner = runner or BrowserRunner(settings)
        self.stop_event = threading.Event()
        self.thread = None
        self.account_threads = {}
        self.workers_lock = threading.RLock()
        self.archive_lock = threading.RLock()
        self.probe_locks = {}
        self.login_check = {}
        self.accounts = OfficialAccounts(settings,store)
        self._archives = {}
        self.archive_for('default')
        with store.db() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS official_submissions (
                id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE, fingerprint TEXT NOT NULL,
                problem_id TEXT NOT NULL, local_submission_id TEXT, label TEXT NOT NULL,
                source_sha256 TEXT NOT NULL, source_bytes INTEGER NOT NULL,
                created REAL NOT NULL, updated REAL NOT NULL, status TEXT NOT NULL,
                result TEXT NOT NULL DEFAULT '{}')''')
            db.execute('CREATE INDEX IF NOT EXISTS official_submissions_queue ON official_submissions(status,created,id)')

    @property
    def archive(self):
        return self.archive_for()

    def archive_for(self, account_id=None):
        identity=self.accounts.get(account_id)['id']
        with self.archive_lock:
            if identity not in self._archives:
                self._archives[identity]=OfficialArchive(self.settings,self.store,identity,self.accounts)
            return self._archives[identity]

    def read_archive(self, account_id=None):
        if account_id is not None:return self.archive_for(account_id)
        identity=self.accounts.current_id()
        key=('merged',identity)
        with self.archive_lock:
            if key not in self._archives:
                self._archives[key]=OfficialArchive(self.settings,self.store,identity,self.accounts,merge_accounts=True)
            return self._archives[key]

    def run_browser(self, account_id, payload, emit):
        def capture(event):
            if event.get('event')=='account':
                self.accounts.bind(account_id,event['user_id'])
            else:emit(event)
        self.runner.run(dict(payload,**self.accounts.browser_options(account_id)),capture)

    def decode(self, row):
        if row is None:
            return None
        item = dict(row)
        item['result'] = json.loads(item['result'])
        item['account_id']=item['result'].get('account_id','default')
        item['account']=self.accounts.public(self.accounts.get(item['account_id']))
        item.pop('fingerprint', None)
        item['target'] = 'official_cannjudge'
        item['terminal'] = item['status'] in TERMINAL
        item['poll_after_seconds'] = None if item['terminal'] else 5
        if item['result'].get('enrichment_sync_id'):
            item['result']['enrichment']=self.archive_for(item['account_id']).enrichment_status(item['result']['enrichment_sync_id'])
        validation = item['result'].get('local_validation')
        if validation and validation.get('submission_id'):
            local = self.store.get(validation['submission_id'])
            if local:
                result = local['result']
                validation.update(status=local['status'],
                                  completed_cases=len(result.get('cases', [])),
                                  passed_cases=sum(c.get('status') == 'accepted' for c in result.get('cases', [])))
                if not item['terminal'] and not validation.get('passed'):
                    validation['message'] = result.get('message', validation.get('message', ''))
        return item

    def get(self, identity, refresh=False):
        if not isinstance(identity, str) or not re.fullmatch(r'[0-9a-f]{32}', identity):
            raise ValueError('Invalid official job ID; use the id returned by submit_official_kernel')
        with self.store.db() as db:
            row = db.execute('SELECT * FROM official_submissions WHERE id=?', (identity,)).fetchone()
            if row is None:
                raise ValueError('Official submission job not found')
            if refresh and row['status'] == 'awaiting_result' and json.loads(row['result']).get('official_id'):
                db.execute("UPDATE official_submissions SET status='queued',updated=? WHERE id=? AND status='awaiting_result'", (time.time(), identity))
                row = db.execute('SELECT * FROM official_submissions WHERE id=?', (identity,)).fetchone()
            return self.decode(row)

    def listing(self, problem_id=None, limit=20, account_id=None):
        condition="COALESCE(json_extract(result, '$.account_id'),'default') IN (SELECT id FROM official_accounts)"
        args=()
        if account_id is not None:
            condition=account_filter('result');args=(self.accounts.get(account_id)['id'],)
        if problem_id is not None:
            condition+=' AND problem_id=?';args+=(problem_id,)
        with self.store.db() as db:
            rows = db.execute('SELECT * FROM official_submissions WHERE '+condition+' ORDER BY created DESC LIMIT ?',args+(max(1,min(100,limit)),))
            return [self.decode(row) for row in rows]

    def submit_project(self, problem_id, request_id, files, label='project', account_id=None, expected_local_validation=None, purpose=None):
        return self.submit(problem_id,request_id,label=label,account_id=account_id,
                           expected_local_validation=expected_local_validation,files=files,purpose=purpose)

    def submit(self, problem_id, request_id, source=None, local_submission_id=None, label='kernel.asc', account_id=None, expected_local_validation=None, files=None, purpose=None):
        if not self.settings.official_submit_enabled:
            raise ValueError('官方提交尚未启用；本地评测仍可使用 submit_kernel')
        config = configuration()
        if problem_id not in config['problems']:
            raise ValueError('该题尚未配置官方提交入口')
        if not isinstance(request_id,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{7,95}',request_id):
            raise ValueError('request_id 需要 8–96 位字母、数字、点、下划线或连字符；重试时使用同一个值')
        is_project=problem_id in config.get('project_templates',{})
        if is_project and (files is None or source is not None or local_submission_id is not None):
            raise ValueError('该题需要完整多文件工程；请用 submit_official_project(files=...)')
        if not is_project and files is not None:
            raise ValueError('该题只接受 kernel.asc')
        if not is_project and (source is None) == (local_submission_id is None):
            raise ValueError('source 和 local_submission_id 必须且只能提供一个')
        if not isinstance(label,str) or not label.strip() or len(label)>120:
            raise ValueError('版本备注需要 1–120 个字符（Unicode 码点），不能全为空白；首尾空白计入长度且原样保留。'
                             '本次请求未创建新任务、未上传代码、未消耗官方提交配额；已有任务不受影响。')
        if local_submission_id is not None:
            if not re.fullmatch(r'[0-9a-f]{32}',local_submission_id):
                raise ValueError('Invalid local submission ID')
            local = self.store.get(local_submission_id)
            if local is None or local['problem_id'] != problem_id:
                raise ValueError('本地提交不存在或不属于所选题目')
            raw = (self.settings.state_dir/'submissions'/local_submission_id/'kernel.asc').read_bytes()
            if hashlib.sha256(raw).hexdigest() != local['sha256']:
                raise ValueError('本地归档源码哈希不一致，停止官方提交')
            source = raw.decode('utf-8')
        if is_project:
            files,digest,template=normalize_project(problem_id,files,self.settings.max_source_bytes)
            encoded=json.dumps(files,ensure_ascii=False,separators=(',',':')).encode('utf-8')
        else:
            if not isinstance(source,str) or not source.strip() or '\x00' in source or 'run_kernel' not in source:
                raise ValueError('需要包含 run_kernel 的 kernel.asc 源码')
            source = source.removeprefix('\ufeff').replace('\r\n','\n').replace('\r','\n')
            encoded = source.encode('utf-8')
            if len(encoded)>self.settings.max_source_bytes:
                raise ValueError('Source too large')
            digest = hashlib.sha256(encoded).hexdigest()
        fingerprint = hashlib.sha256(json.dumps([problem_id,digest,local_submission_id,label],ensure_ascii=False).encode()).hexdigest()
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            account,purpose=self.accounts.submission_account(problem_id,account_id,purpose,db)
            if not account['profile']:raise ValueError('请先完成此官方账户的登录')
            previous = db.execute('SELECT * FROM official_submissions WHERE request_id=?',(request_id,)).fetchone()
            if previous:
                if json.loads(previous['result']).get('account_id','default')!=account['id']:
                    raise ValueError('此 request_id 已属于另一官方账户；请查询原任务，不会转投其他账户')
                if previous['fingerprint'] != fingerprint:
                    raise ValueError('同一个 request_id 已用于不同内容；不会覆盖或重复提交')
                return self.decode(previous)
            required=self.accounts.validation_required(db)
            if expected_local_validation is not None and (not isinstance(expected_local_validation,bool) or expected_local_validation!=required):
                raise ValueError('本地检查开关已变化，请刷新确认后重新提交')
            if required and not self.settings.worker_enabled:
                raise ValueError('本地评测未启用；请启用本地评测或在设置中关闭提交前检查')
            if required and is_project:
                try:
                    local_spec = problem(problem_id)
                    if local_spec['contract'].get('submission_kind') != 'project':
                        raise ValueError('Wrong adapter contract')
                except ValueError:
                    raise ValueError('此多文件题目尚无本地 NPU 评测适配器，无法执行强制本地检查；未创建或上传任务')
            if required and problem_id in config.get('kernel_templates',{}):
                try:
                    problem(problem_id)
                except ValueError:
                    raise ValueError('本题仅接入官方评测，尚无本地评测适配器；请在设置中关闭提交前本地检查。未创建或上传任务')
            pending = db.execute("SELECT count(*) FROM official_submissions WHERE status IN ('queued','validating','submitting','submitted','judging') AND "+account_filter('result'),(account['id'],)).fetchone()[0]
            if pending >= self.settings.queue_limit:
                raise ValueError('此账号的官方提交队列已满，请稍后再试')
            identity = uuid.uuid4().hex
            directory = self.directory/identity
            directory.mkdir(parents=True)
            (directory/('project.json' if is_project else 'kernel.asc')).write_bytes(encoded)
            now = time.time()
            initial = dict(message='等待本地完整测试；全部通过后才会上传官方' if required else '本地正确性检查已关闭，等待直接提交官方',
                           account_id=account['id'],submission_purpose=purpose,
                           contest_url=problem_url(config,problem_id).rsplit('/',1)[0],problem_url=problem_url(config,problem_id),
                           official_id=None,official_url=None,source_verified=False,
                           local_validation=self.validation_policy(required),
                           submission_kind='project' if is_project else 'kernel',
                           normalization='UTF-8 BOM removed; CRLF/CR normalized to LF; '+('all editable template files are submitted; bundle SHA-256 hashes sorted [path,file SHA-256] pairs' if is_project else 'only kernel.asc is submitted'))
            if is_project:
                initial.update(editable_files=template['editable_files'],official_problem_id=template['official_problem_id'])
            db.execute('INSERT INTO official_submissions VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                       (identity,request_id,fingerprint,problem_id,local_submission_id,label,digest,len(encoded),now,now,'queued',json.dumps(initial,ensure_ascii=False)))
            return self.decode(db.execute('SELECT * FROM official_submissions WHERE id=?',(identity,)).fetchone())

    def update(self, identity, status, *, db=None, **fields):
        if db is None:
            with self.store.db() as connection:
                connection.execute('BEGIN IMMEDIATE')
                return self.update(identity, status, db=connection, **fields)
        old = db.execute('SELECT result FROM official_submissions WHERE id=?',(identity,)).fetchone()
        result = json.loads(old['result'])
        result.update(fields)
        db.execute('UPDATE official_submissions SET status=?,updated=?,result=? WHERE id=?',
                   (status,time.time(),json.dumps(result,ensure_ascii=False,allow_nan=False),identity))

    def recover(self, account_id=None):
        condition='1=1';args=()
        if account_id is not None:
            condition=account_filter('result');args=(self.accounts.get(account_id)['id'],)
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("UPDATE official_sync SET status='queued' WHERE status='running' AND "+condition,args)
            # No official POST can have happened in this phase. Reuse the
            # atomically linked local job instead of launching another test.
            db.execute("UPDATE official_submissions SET status='queued',updated=? WHERE status='validating' AND "+condition, (time.time(),)+args)
            rows = db.execute("SELECT id,result FROM official_submissions WHERE status IN ('submitting','submitted','judging') AND "+condition,args).fetchall()
            for row in rows:
                result = json.loads(row['result'])
                known = bool(result.get('official_id'))
                status = 'queued' if known else 'outcome_unknown'
                result['message'] = '服务恢复后仅查询已知官方记录，不重复提交' if known else '服务在官方提交期间中断，结果不确定；请先检查官方记录，不自动重发'
                db.execute('UPDATE official_submissions SET status=?,updated=?,result=? WHERE id=?', (status,time.time(),json.dumps(result,ensure_ascii=False),row['id']))

    def start(self):
        if not self.settings.official_submit_enabled:
            return
        with self.workers_lock:
            if self.thread and self.thread.is_alive():return
            if any(t.is_alive() for t in self.account_threads.values()):
                raise RuntimeError('Official account workers have not stopped')
            self.stop_event.clear()
            self.recover()
            self.ensure_account_workers()
            self.thread = threading.Thread(target=self.supervise, name='official-accounts', daemon=True)
            self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=2)
        with self.workers_lock:
            threads=list(self.account_threads.values())
        deadline=time.monotonic()+2
        for thread in threads:
            thread.join(timeout=max(0,deadline-time.monotonic()))

    def ready_account_ids(self):
        with self.store.db() as db:
            return [r['id'] for r in db.execute("SELECT id FROM official_accounts WHERE profile<>'' ORDER BY created,id")]

    def ensure_account_workers(self):
        for account_id in self.ready_account_ids():
            with self.workers_lock:
                if self.stop_event.is_set():return
                previous=self.account_threads.get(account_id)
                if previous and previous.is_alive():continue
                # Only a stopped account worker is recovered; other accounts may
                # be uploading right now and must never have their state reset.
                self.recover(account_id)
                thread=threading.Thread(target=self.loop,args=(account_id,),name='official-'+account_id,daemon=True)
                self.account_threads[account_id]=thread
                thread.start()

    def supervise(self):
        while not self.stop_event.is_set():
            try:self.ensure_account_workers()
            except Exception:logger.exception('Official account supervisor will retry')
            self.stop_event.wait(1)

    def claim(self, account_id=None):
        condition='';args=()
        if account_id is not None:
            condition=' AND '+account_filter('q.result');args=(self.accounts.get(account_id)['id'],)
        query = """SELECT q.* FROM official_submissions q WHERE q.status='queued'"""+condition+"""
                AND NOT EXISTS (SELECT 1 FROM official_submissions a
                    WHERE a.status IN ('validating','submitting','submitted','judging')
                    AND COALESCE(json_extract(a.result,'$.account_id'),'default')=COALESCE(json_extract(q.result,'$.account_id'),'default'))
                AND NOT EXISTS (SELECT 1 FROM official_sync s WHERE s.status='running'
                    AND COALESCE(json_extract(s.result,'$.account_id'),'default')=COALESCE(json_extract(q.result,'$.account_id'),'default'))
                ORDER BY q.created,q.id LIMIT 1"""
        with self.store.db() as db:
            # Idle workers must not contend with result writers. This read is
            # only a hint; eligibility is checked again under the write lock.
            if db.execute(query,args).fetchone() is None:
                return None
            db.execute('BEGIN IMMEDIATE')
            row = db.execute(query,args).fetchone()
            if row is None:
                return None
            # Only the verified gate transitions a new upload to 'submitting'.
            # A known official ID always resumes reads, without another test.
            result=json.loads(row['result'])
            state = 'judging' if result.get('official_id') else 'validating' if result.get('local_validation',{}).get('required',True) else 'submitting'
            db.execute("UPDATE official_submissions SET status=?,updated=? WHERE id=?", (state,time.time(),row['id']))
            return row['id']

    def validation_policy(self, required=None):
        required=self.accounts.validation_required() if required is None else required
        return dict(required=required, data_origin='local_npu_validation',
                    ssh_host=self.settings.ssh_host, remote_root=self.settings.remote_root,
                    suite='official_shapes', profile_mode='timing',
                    status='pending' if required else 'skipped', submission_id=None, passed=False,
                    reuse_previous_results=False,
                    note='本地默认测试全部通过才上传；本地耗时不计入官方成绩' if required else '创建任务时已关闭本地正确性检查；直接上传官网，不代表本地已通过')

    def enqueue_validation(self, identity):
        """Create a fresh default-suite job and its official link atomically."""
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM official_submissions WHERE id=?', (identity,)).fetchone()
            result = json.loads(row['result'])
            validation = result.get('local_validation', {})
            if validation.get('submission_id'):
                return validation['submission_id']
            filename = 'project.json' if result.get('submission_kind') == 'project' else 'kernel.asc'
            raw = (self.directory/identity/filename).read_bytes()
            from remote.project import source_digest
            if source_digest(raw, problem(row['problem_id'])['contract']) != row['source_sha256']:
                raise ValueError('官方待提交源码哈希不一致')
            local_id = uuid.uuid4().hex
            self.store.enqueue(local_id, ('官方提交前检查 · '+row['label'])[:120],
                               row['source_sha256'], raw, self.settings.queue_limit,
                               problem_id=row['problem_id'], profile_mode='timing', connection=db)
            validation = dict(self.validation_policy(True), submission_id=local_id,
                              status='queued', source_sha256=row['source_sha256'],
                              total_cases=len(problem(row['problem_id'])['cases']))
            result.update(local_validation=validation, message='本地测试已排队；通过前不会上传官方')
            db.execute('UPDATE official_submissions SET status=?,updated=?,result=? WHERE id=?',
                       ('validating',time.time(),json.dumps(result,ensure_ascii=False),identity))
            return local_id

    def validation_result(self, row, local):
        """Check exact bytes, current problem/target, and the complete default suite."""
        report = dict(row['result']['local_validation'])
        if local is None:
            raise ValueError('关联的本地测试记录不存在')
        result = local['result']
        spec = problem(row['problem_id'])
        cases = result.get('cases', [])
        total = len(spec['cases'])
        report.update(status=local['status'], total_cases=total,
                      completed_cases=len(cases), passed_cases=sum(c.get('status') == 'accepted' for c in cases),
                      failed_cases=[{k:c.get(k) for k in ('index','status','error_rate','message')}
                                    for c in cases if c.get('status') != 'accepted'],
                      message=result.get('message', ''), passed=False)
        if local['status'] not in LOCAL_TERMINAL:
            return report
        if local['status'] != 'accepted':
            report['message'] = result.get('message') or '本地测试未通过：'+local['status']
            report['checks'] = result.get('checks', {})
            log = self.settings.state_dir/'submissions'/local['id']/'compile.log'
            if log.is_file():
                report['compile_log_tail'] = log.read_text(encoding='utf-8',errors='replace')[-12000:]
            return report
        target = dict(ssh_host=self.settings.ssh_host, remote_root=self.settings.remote_root)
        checks = {
            '题目': local['problem_id'] == row['problem_id'] and result.get('problem_id') == row['problem_id'],
            '默认测试集': result.get('suite') == 'official_shapes' and not (self.settings.state_dir/'submissions'/local['id']/'generate.cpp').exists(),
            '题目版本': local['problem_sha256'] == result.get('problem_sha256') == spec['pack_sha256'],
            '评测服务器': result.get('execution_target') == target and all(report.get(k) == v for k,v in target.items()),
            '完整用例': len(cases) == total and [c.get('index') for c in cases] == list(range(1,total+1)),
            '全部通过': all(c.get('status') == 'accepted' for c in cases) and result.get('passed') == total,
            '静态检查': result.get('checks', {}).get('passed') is True,
            '源码身份': local['sha256'] == result.get('source_sha256') == result.get('environment', {}).get('source_sha256') == row['source_sha256'],
            '远端题目': result.get('environment', {}).get('problem_sha256') == spec['pack_sha256'],
        }
        filename = 'project.json' if row['result'].get('submission_kind') == 'project' else 'kernel.asc'
        from remote.project import source_digest
        for name,path in [('官方源码',self.directory/row['id']/filename),
                          ('本地源码',self.settings.state_dir/'submissions'/local['id']/filename)]:
            try:
                checks[name] = path.is_file() and source_digest(path.read_bytes(),spec['contract']) == row['source_sha256']
            except (ValueError,TypeError):
                checks[name] = False
        failures = [name for name,passed in checks.items() if not passed]
        report.update(passed=not failures, integrity_checks=checks,
                      message='本地完整测试全部通过，源码一致' if not failures else '本地检查凭据不匹配：'+'、'.join(failures))
        return report

    def validate_before_submit(self, identity):
        """Wait only in the background worker; HTTP/MCP return the durable job ID."""
        row=self.get(identity)
        if row['result'].get('local_validation',{}).get('required',True) is False:
            self.update(identity,'submitting',message='此任务创建时已关闭本地正确性检查，直接提交官方')
            return True
        try:
            while not self.stop_event.is_set():
                try:
                    local_id = self.enqueue_validation(identity)
                except QueueFull:
                    self.update(identity,'validating',message='等待本地评测队列空位；尚未上传官方')
                    self.stop_event.wait(1)
                    continue
                row = self.get(identity)
                local = self.store.get(local_id)
                report = self.validation_result(row,local)
                if local['status'] not in LOCAL_TERMINAL:
                    self.stop_event.wait(1)
                    continue
                if not report['passed']:
                    self.update(identity,'local_failed',local_validation=report,
                                message='本地检查未通过，未提交官方。'+report['message'])
                    return False
                # Persist BEFORE any possible official POST. Crash recovery must
                # now treat an unknown outcome conservatively, never resend it.
                self.update(identity,'submitting',local_validation=report,
                            message=f"本地 {report['passed_cases']}/{report['total_cases']} 通过，开始官方提交")
                return True
            return False
        except Exception as error:
            validation = self.get(identity)['result'].get('local_validation', self.validation_policy())
            validation.update(passed=False, message=str(error)[:1500])
            self.update(identity,'local_failed',local_validation=validation,
                        message='本地检查未完成，未提交官方。'+str(error)[:1500])
            return False

    def loop(self, account_id):
        account_id=self.accounts.get(account_id)['id']
        own_archive=self.archive_for(account_id)
        next_reconciliation = 0.0
        previous_reconciliation = None
        while not self.stop_event.is_set():
            identity = sync_id = archive = None
            try:
                identity = self.claim(account_id)
                if identity is None:
                    if time.monotonic() >= next_reconciliation:
                        from .official_reconciliation import reconcile_unknown
                        report = reconcile_unknown(self, account_id)
                        next_reconciliation = time.monotonic() + 60
                        signature = report['jobs']
                        if signature != previous_reconciliation and report['checked']:
                            logger.warning('Official unknown-outcome reconciliation account=%s counts=%s; unresolved requests are never resubmitted', account_id, report['counts'])
                        previous_reconciliation = signature
                    # Preserve the existing automatic-crawl scope. Explicit
                    # history requests and quotas run in their owner's queue.
                    if self.accounts.current_id()==account_id:
                        own_archive.schedule_auto_sync(configuration()['problems'])
                    archive=own_archive
                    sync_id=archive.sync_claim()
                    if sync_id:
                        self.sync_execute(sync_id)
                        continue
                    quota_account=self.accounts.quota.due_account([self.accounts.public(self.accounts.get(account_id))])
                    if quota_account:
                        self.refresh_quota(quota_account)
                        continue
                    self.stop_event.wait(1)
                    continue
                self.execute(identity)
            except Exception as error:
                if is_rsi_deployment_gate(error):
                    logger.info('Official claims paused by RSI deployment gate; waiting for idle window')
                    self.stop_event.wait(5)
                    continue
                # A failed claim rolls back, but a claimed upload may already
                # have reached the judge. Retry persistence, never that upload.
                logger.exception('Official queue iteration failed; recovering state before continuing')
                self.recover_iteration(identity, archive, sync_id)
                self.stop_event.wait(1)

    def recover_iteration(self, identity, archive=None, sync_id=None):
        while not self.stop_event.is_set():
            try:
                if identity:
                    current = self.get(identity)
                    if current is None or current['terminal'] or current['status'] in ('queued','awaiting_result'):
                        return
                    if current['result'].get('official_id'):
                        self.update(identity,'awaiting_result',message='调度异常，保留已知官方记录；后续只查询结果，不重复提交')
                    elif current['status']=='validating':
                        self.update(identity,'queued',message='本地检查调度异常，等待恢复；尚未上传官方，复用已关联的本地任务')
                    else:
                        self.update(identity,'outcome_unknown',message='官方提交期间调度异常，结果不确定；保留原请求，请先核实官方历史，不自动重发')
                elif sync_id:
                    current = archive.sync_get(sync_id)
                    if current and not current['terminal']:
                        archive.sync_update(sync_id,'queued',message='官方历史读取调度异常，等待安全重试；不提交源码')
                return
            except Exception:
                logger.exception('Could not persist official queue recovery; retrying without uploading')
                if self.stop_event.wait(1):
                    return

    def worker_status(self):
        expected=self.ready_account_ids() if self.settings.official_submit_enabled else []
        with self.workers_lock:
            accounts={key:bool(self.account_threads.get(key) and self.account_threads[key].is_alive()) for key in expected}
            supervisor=bool(self.thread and self.thread.is_alive())
        return dict(alive=sum(accounts.values()),expected=len(expected),mode='per_account',
                    supervisor_alive=supervisor,accounts=accounts)

    def queue_status(self, account_id=None):
        selected=self.accounts.get(account_id)['id'] if account_id is not None else None
        workers=self.worker_status()
        with self.store.db() as db:
            accounts=db.execute('SELECT id,label,profile FROM official_accounts ORDER BY created,id').fetchall()
            jobs=db.execute("SELECT id,status,result FROM official_submissions WHERE status IN ('queued','validating','submitting','submitted','judging') ORDER BY created,id").fetchall()
            unresolved=db.execute("SELECT id,created,result FROM official_submissions WHERE status='outcome_unknown' ORDER BY created,id").fetchall()
        result=[]
        for account in accounts:
            identity=account['id']
            if selected is not None and identity!=selected:continue
            owned=[r for r in jobs if json.loads(r['result']).get('account_id','default')==identity]
            unknown=[r for r in unresolved if json.loads(r['result']).get('account_id','default')==identity]
            result.append(dict(account_id=identity,account_label=account['label'],ready=bool(account['profile']),
                outcome_unknown=len(unknown),oldest_outcome_unknown_created=unknown[0]['created'] if unknown else None,
                outcome_unknown_sample_ids=[r['id'] for r in unknown[:10]],
                worker_alive=workers['accounts'].get(identity,False),queued=sum(r['status']=='queued' for r in owned),
                active_jobs=[dict(id=r['id'],status=r['status']) for r in owned if r['status']!='queued'],
                capacity=self.settings.queue_limit))
        return result

    def publish_result(self, identity, record, account_id):
        """Commit result, readable archive and GET-only follow-up in one transaction."""
        archive=self.archive_for(account_id)
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM official_submissions WHERE id=?',(identity,)).fetchone()
            current=json.loads(row['result'])
            if (record['official_id']!=current.get('official_id') or record['problem_id']!=row['problem_id']
                    or current.get('account_id','default')!=archive.account_id):
                raise ValueError('Official result does not match the accepted job')
            sync=archive.request_sync({row['problem_id']:configuration()['problems'][row['problem_id']]},
                                      incremental=True,trigger='result_enrichment',db=db,
                                      official_ids=[record['official_id']])
            record=dict(record,enrichment_sync_id=sync['id'])
            archive.ingest(record,db=db)
            self.update(identity,'completed',db=db,**record,
                        message='官方终态已发布：'+record['official_status']+'；源码归档和榜单刷新另行同步')

    def ingest_record(self, record, account_id=None):
        archive=self.archive_for(account_id)
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            rows=db.execute("SELECT id,status,result,source_sha256 FROM official_submissions WHERE "+
                            account_filter('result')+" AND json_extract(result,'$.official_id')=?",
                            (archive.account_id,record['official_id'])).fetchall()
            record=dict(record)
            for row in rows:
                current=json.loads(row['result'])
                verified=bool(record.get('source_available') and record.get('source_sha256')==row['source_sha256'])
                record.update(source_verified=verified,source_status='verified' if verified else
                              'mismatch' if record.get('source_available') else 'unavailable')
                for key in ('result_published_at','enrichment_sync_id'):
                    if key in current:record[key]=current[key]
                if ((row['status']=='awaiting_result' and record.get('evaluation_complete')) or
                        (row['status']=='completed' and current.get('result_published_at') and
                         (record.get('evaluation_complete') or record.get('official_status')==current.get('official_status')))):
                    self.update(row['id'],'completed',db=db,**record,record=record,
                                archived_source_sha256=record.get('source_sha256'),
                                archive_error=None if verified else '官方归档源码哈希未确认',
                                message='官方结果已归档'+('，源码哈希一致' if verified else '；源码哈希未确认'))
            archive.ingest(record,db=db)

    def execute(self, identity):
        row = self.get(identity)
        if row['terminal']:
            return
        if not row['result'].get('official_id'):
            if not self.validate_before_submit(identity):
                return
            row = self.get(identity)
        account_id=row['account_id']
        archive=self.archive_for(account_id)
        config = configuration()
        directory = self.directory/identity
        payload = dict(action='result' if row['result'].get('official_id') else 'submit',
                       official_id=row['result'].get('official_id'),
                       problem_id=row['problem_id'],history_dir=str(archive.directory.resolve()),
                       problem_url=problem_url(config,row['problem_id']),
                       submission_kind=row['result'].get('submission_kind','kernel'),
                       source_path=str((directory/('project.json' if row['result'].get('submission_kind')=='project' else 'kernel.asc')).resolve()),
                       source_sha256=row['source_sha256'],archive_dir=str(directory.resolve()))
        if payload['submission_kind']=='project':
            template=project_template(row['problem_id'])
            payload.update(official_problem_id=template['official_problem_id'],template_files=template['files'])
        def emit(event):
            kind = event.pop('event', '')
            if kind == 'record':
                self.ingest_record(event['record'],account_id)
            elif kind == 'submitted':
                self.update(identity,'submitted',**event)
                self.accounts.quota.request_refresh(account_id,invalidate=True)
            elif kind == 'progress':
                current = self.get(identity)
                self.update(identity,'judging' if current['result'].get('official_id') else 'submitting',**event)
            elif kind == 'done':
                status = event.pop('status')
                if status=='completed' and 'result_record' in event:
                    self.publish_result(identity,event['result_record'],account_id)
                    return
                if status not in TERMINAL | {'awaiting_result','profile_busy'}:
                    raise ValueError('Invalid official worker status')
                if status == 'profile_busy':
                    # This account's browser lock was not acquired; no upload occurred.
                    self.update(identity,'queued',**event)
                    self.stop_event.wait(5)
                else:
                    self.update(identity,status,**event)
        try:
            self.run_browser(account_id,payload,emit)
        except Exception:
            current = self.get(identity)
            if current['terminal']:
                return
            known = bool(current['result'].get('official_id'))
            self.update(identity,'awaiting_result' if known else 'outcome_unknown',
                        message='官方连接中断；已保留源码和原请求。'+('可刷新已知官方记录，绝不会重新提交。' if known else '提交结果不确定，请核实官方记录；不会自动重发。'))

    def sync(self, problem_id=None, account_id=None):
        if not self.settings.official_submit_enabled:
            raise ValueError('官方连接尚未启用')
        problems=configuration()['problems']
        if problem_id is not None:
            if problem_id not in problems:raise ValueError('未配置该官方题目')
            problems={problem_id:problems[problem_id]}
        account=self.accounts.get(account_id)
        if not account['profile']:raise ValueError('请先登录此官方账户')
        return self.archive_for(account['id']).request_sync(problems)

    def sync_archive(self, identity):
        with self.store.db() as db:
            row=db.execute('SELECT result FROM official_sync WHERE id=?',(identity,)).fetchone()
        if row is None:raise ValueError('同步任务不存在')
        return self.archive_for(json.loads(row['result']).get('account_id','default'))

    def sync_execute(self, identity):
        archive=self.sync_archive(identity)
        job=archive.sync_get(identity)
        def emit(event):
            kind=event.pop('event','')
            if kind=='record':self.ingest_record(event['record'],archive.account_id)
            elif kind=='problem':archive.save_problem(event['problem'])
            elif kind=='progress':archive.sync_update(identity,'running',**event)
            elif kind=='done':
                status=event.pop('status')
                archive.sync_update(identity,'queued' if status=='profile_busy' else status,**event)
                if status=='profile_busy':self.stop_event.wait(5)
        config=configuration()
        try:
            self.run_browser(archive.account_id,dict(action='history',problems=job['problems'],
                incremental=job['result'].get('mode')=='incremental',
                official_ids=job['result'].get('official_ids'),
                problem_urls={p:problem_url(config,p) for p in job['problems']},
                problem_url=problem_url(config,next(iter(job['problems']))),
                archive_dir=str(archive.directory.resolve())),emit)
        except Exception:
            archive.sync_update(identity,'failed',message='官方历史读取中断；已导入记录保留，可安全重试同步')
        current=archive.sync_get(identity)
        if current['terminal']:
            archive.finish_auto_sync(identity,current['status'])

    def refresh_quota(self, account_id):
        result={'status':'unavailable'}
        def capture(event):
            if event.get('event')=='done':result.update(event)
        config=configuration()
        try:
            self.run_browser(account_id,dict(action='quota',quota_policy=quota_policy(),
                problem_url=problem_url(config,next(iter(config['problems'])))),capture)
        except Exception:
            result={'status':'unavailable'}
        try:
            self.accounts.quota.save(account_id,result)
        except (ValueError,KeyError,TypeError):
            self.accounts.quota.save(account_id,{'status':'unavailable'})

    def account_settings(self, refresh_quotas=False):
        settings=self.accounts.listing()
        if refresh_quotas:
            for account in settings['accounts']:
                if account['ready']:self.accounts.quota.request_refresh(account['id'])
            settings=self.accounts.listing()
        return settings

    def connection(self, check_login=False, account_id=None):
        account=self.accounts.get(account_id);account_id=account['id']
        archive=self.archive_for(account_id)
        config = configuration()
        with self.store.db() as db:
            active = db.execute("SELECT id FROM official_submissions WHERE status IN ('validating','submitting','submitted','judging') AND "+account_filter('result')+" LIMIT 1",(account_id,)).fetchone()
            pending = db.execute("SELECT count(*) FROM official_submissions WHERE status='queued' AND "+account_filter('result'),(account_id,)).fetchone()[0]
        result = dict(enabled=self.settings.official_submit_enabled,contest_url=config['contest_url'],
                      official_worker=self.worker_status(),
                      queues=self.queue_status(),
                      data_origin='official_cannjudge',account_id=account_id,account=self.accounts.public(account),settings=self.accounts.listing(),history=archive.catalog(),
                      auto_sync=archive.auto_sync_status(),
                      problems=config['problems'],active_job_id=active['id'] if active else None,
                      problem_urls={p:problem_url(config,p) for p in config['problems']},
                      submission_kinds={p:'project' if p in config.get('project_templates',{}) else 'kernel' for p in config['problems']},
                      queued=pending,login_check=self.login_check.get(account_id),
                      authentication='服务专用 CANNJudge 浏览器会话；凭据不经过 MCP',
                      local_validation=self.validation_policy(),
                      submission_policy='只在用户明确要求时提交。提交前本地检查按创建任务时的设置执行；关闭时直接上传官网。网络结果不确定时不自动重发。')
        if check_login and self.settings.official_submit_enabled and not active:
            with self.workers_lock:
                probe_lock=self.probe_locks.setdefault(account_id,threading.Lock())
            if not probe_lock.acquire(blocking=False):
                result['message'] = '此账号已有登录检查在执行'
                return result
            try:
                def capture(event):
                    if event.get('event') == 'done':
                        self.login_check[account_id] = {k:v for k,v in event.items() if k!='event'}
                        self.login_check[account_id]['checked_at'] = time.time()
                try:
                    self.run_browser(account_id,dict(action='check',problem_url=config['contest_url']+'/'+next(iter(config['problems'].values()))),capture)
                except Exception:
                    self.login_check[account_id] = dict(status='unavailable',message='登录检查失败；未提交源码',checked_at=time.time())
                result['login_check'] = self.login_check.get(account_id)
            finally:
                probe_lock.release()
        return result

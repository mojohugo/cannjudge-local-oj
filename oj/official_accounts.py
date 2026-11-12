"""Named official login profiles and global defaults; no credentials in the API."""
import json
import os
from pathlib import Path
import re
import time
import uuid
from .official_quota import OfficialQuota

DEFAULT_ACCOUNT = 'default'


def account_filter(column):
    # Column names are constants supplied by this application, never request input.
    return f"COALESCE(json_extract({column}, '$.account_id'),'default')=?"


class OfficialAccounts:
    def __init__(self, settings, store):
        self.settings, self.store = settings, store
        self.quota = OfficialQuota(store)
        with store.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS official_accounts (
                    id TEXT PRIMARY KEY, label TEXT NOT NULL, user_id TEXT UNIQUE,
                    profile TEXT NOT NULL, created REAL NOT NULL, verified REAL,
                    auto_sync TEXT NOT NULL DEFAULT '{}');
                CREATE TABLE IF NOT EXISTS official_preferences (
                    id INTEGER PRIMARY KEY CHECK(id=1), active_account TEXT NOT NULL,
                    local_validation INTEGER NOT NULL CHECK(local_validation IN (0,1)));
                INSERT OR IGNORE INTO official_preferences VALUES(1,'default',1);
                CREATE TABLE IF NOT EXISTS official_submission_routes (
                    problem_id TEXT PRIMARY KEY, optimization_account TEXT NOT NULL, test_account TEXT NOT NULL);
            ''')
            db.execute("INSERT OR IGNORE INTO official_accounts(id,label,profile,created) VALUES('default','原有账户','legacy',?)", (time.time(),))
        self._bootstrap_legacy()

    def _bootstrap_legacy(self):
        # Pin the existing profile to the owner in its already archived official
        # response. No browser work, passwords, history rewrite or account switch.
        with self.store.db() as db:
            if self.get('default',db)['user_id']:return
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='official_history'").fetchone():return
            row=db.execute('SELECT official_id FROM official_history WHERE '+account_filter('record')+' ORDER BY create_time LIMIT 1',('default',)).fetchone()
        if not row or not re.fullmatch(r'[a-f0-9]{24}',row[0]):return
        try:
            detail=json.loads((self.settings.state_dir/'official-history'/row[0]/'raw-detail.json').read_text(encoding='utf-8'))
            user=detail.get('user_id')
            if detail.get('_id')==row[0] and isinstance(user,str) and re.fullmatch(r'[a-f0-9]{24}',user):
                with self.store.db() as db:
                    db.execute("UPDATE official_accounts SET user_id=? WHERE id='default' AND user_id IS NULL AND NOT EXISTS(SELECT 1 FROM official_accounts WHERE user_id=?)",(user,user))
        except (OSError,ValueError):pass

    def current_id(self, db=None):
        if db is None:
            with self.store.db() as connection:
                return self.current_id(connection)
        return db.execute('SELECT active_account FROM official_preferences WHERE id=1').fetchone()[0]

    def get(self, identity=None, db=None):
        if db is None:
            with self.store.db() as connection:
                return self.get(identity, connection)
        identity = self.current_id(db) if identity is None else identity
        if not isinstance(identity, str) or not re.fullmatch(r'default|[a-f0-9]{32}', identity):
            raise ValueError('无效的官方账户 ID')
        row = db.execute('SELECT * FROM official_accounts WHERE id=?', (identity,)).fetchone()
        if row is None:
            raise ValueError('官方账户不存在')
        return dict(row)

    @staticmethod
    def public(row):
        return dict(id=row['id'], label=row['label'], official_user_id=row['user_id'],
                    created=row['created'], verified_at=row['verified'],
                    ready=bool(row['profile']), needs_login=not bool(row['profile']))

    def listing(self):
        with self.store.db() as db:
            prefs = db.execute('SELECT * FROM official_preferences WHERE id=1').fetchone()
            rows = db.execute('SELECT * FROM official_accounts ORDER BY created,id').fetchall()
        return dict(active_account_id=prefs['active_account'], accounts=[dict(self.public(dict(r)),daily_quota=self.quota.snapshot(r['id'],bool(r['profile']))) for r in rows],
                    local_validation_required=bool(prefs['local_validation']),
                    submission_routes={}, submission_account_policy='selected_account',
                    history_scope='all_saved_official_accounts',
                    queue_policy=dict(mode='per_account',parallel_accounts=True,per_account_concurrency=1,
                        pending_limit_per_account=self.settings.queue_limit,local_validation_queue='shared_npu'),
                    scope='shared_service', note='官方记录与榜单默认合并所有已保存账户；显式 account_id 可筛选。提交使用当前选定账户，显式 account_id 可固定账户；purpose 仅作备注，不再分流。已排队任务固定原账户与检查设置。')

    def set_routes(self, problem_ids, optimization_account, test_account):
        from .official_targets import configuration
        configured=configuration()['problems']
        if not problem_ids or any(p not in configured for p in problem_ids):
            raise ValueError('用途路由必须指定已配置题目')
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            a,b=self.get(optimization_account,db),self.get(test_account,db)
            if a['id']==b['id'] or not a['profile'] or not b['profile']:
                raise ValueError('优化和测试必须使用两个已登录的不同账户')
            for p in problem_ids:
                db.execute('INSERT OR REPLACE INTO official_submission_routes VALUES(?,?,?)',(p,a['id'],b['id']))
        return self.listing()

    def submission_account(self, problem_id, account_id=None, purpose=None, db=None):
        if purpose not in (None,'optimization','test'):
            raise ValueError('purpose 只能是 optimization 或 test')
        if db is None:
            with self.store.db() as connection:
                return self.submission_account(problem_id,account_id,purpose,connection)
        # Legacy routes remain stored for audit, but no longer choose an account.
        return self.get(account_id,db),purpose

    def create(self, label):
        if not isinstance(label, str) or not label.strip() or len(label.strip()) > 60:
            raise ValueError('账户备注需要 1–60 个字符')
        identity = uuid.uuid4().hex
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT count(*) FROM official_accounts').fetchone()[0] >= 20:
                raise ValueError('最多保存 20 个官方账户')
            db.execute('INSERT INTO official_accounts(id,label,profile,created) VALUES(?,?,?,?)',
                       (identity,label.strip(),'',time.time()))
        return self.public(self.get(identity))

    def switch(self, identity):
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = self.get(identity, db)
            if not row['profile']:
                raise ValueError('请先在账户管理中完成此账户的官方登录')
            db.execute('UPDATE official_preferences SET active_account=? WHERE id=1', (identity,))
        return self.listing()

    def set_validation(self, required):
        if not isinstance(required, bool):
            raise ValueError('required 必须为布尔值')
        with self.store.db() as db:
            db.execute('UPDATE official_preferences SET local_validation=? WHERE id=1', (int(required),))
        return self.listing()

    def validation_required(self, db=None):
        if db is None:
            with self.store.db() as connection:
                return self.validation_required(connection)
        return bool(db.execute('SELECT local_validation FROM official_preferences WHERE id=1').fetchone()[0])

    def bind(self, identity, user_id, profile=None):
        if not isinstance(user_id, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,100}',user_id):
            raise ValueError('官网未返回有效账户身份，登录尚未保存')
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = self.get(identity, db)
            if row['user_id'] and row['user_id'] != user_id:
                raise ValueError('登录的是另一官方账户；请新增账户，不能覆盖原账户身份')
            duplicate = db.execute('SELECT label FROM official_accounts WHERE user_id=? AND id<>?', (user_id,identity)).fetchone()
            if duplicate:
                raise ValueError('该官方账户已保存为“'+duplicate['label']+'”，请选择已有账户')
            if profile is not None and not re.fullmatch(r'[a-f0-9]{32}',profile):
                raise ValueError('无效的登录会话')
            db.execute('UPDATE official_accounts SET user_id=?,verified=?,profile=? WHERE id=?',
                       (user_id,time.time(),row['profile'] if profile is None else profile,identity))
        return self.public(self.get(identity))

    def state_path(self, identity=None, profile=None):
        row = self.get(identity)
        slot = row['profile'] if profile is None else profile
        if slot == 'legacy' and row['id'] == DEFAULT_ACCOUNT:
            base = os.environ.get('CANNJUDGE_BROWSER_STATE_DIR')
            if base:
                return Path(base)
            return Path(os.environ.get('LOCALAPPDATA', str(Path.home()/'.local/share')))/'CANNJudgeSubmit'
        if not re.fullmatch(r'[a-f0-9]{32}',slot or ''):
            raise ValueError('请先登录此官方账户')
        return self.settings.state_dir/'official-accounts'/row['id']/'profiles'/slot

    def browser_options(self, identity=None):
        row = self.get(identity)
        return dict(account_id=row['id'],browser_state_dir=str(self.state_path(row['id']).resolve()),
                    expected_user_id=row['user_id'],enforce_identity=True)

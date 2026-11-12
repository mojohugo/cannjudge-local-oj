"""Short-lived, authenticated, user-operated browser sessions for official login."""
import json
import os
import queue
import shutil
import subprocess
import threading
import time
import uuid
from .config import ROOT


class LoginSession:
    def __init__(self, accounts, account_id, node=None):
        self.accounts=accounts
        self.account_id=accounts.get(account_id)['id']
        self.id=uuid.uuid4().hex
        self.directory=accounts.state_path(account_id,self.id).resolve()
        self.directory.mkdir(parents=True,mode=0o700)
        self.expires=time.time()+600
        self.lock=threading.RLock()
        self.responses=queue.Queue()
        self.saved=False
        self.closed=False
        self.process=subprocess.Popen([node or shutil.which('node') or 'node',str(ROOT/'scripts/official-login.mjs')],
            cwd=ROOT,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
            text=True,encoding='utf-8',env=os.environ.copy(),creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        def consume():
            try:
                for line in self.process.stdout:
                    try:self.responses.put(json.loads(line))
                    except ValueError:pass
            finally:self.responses.put(None)
        threading.Thread(target=consume,daemon=True).start()
        self.timer=threading.Timer(600,self.close);self.timer.daemon=True;self.timer.start()

    def rpc(self, action, **fields):
        with self.lock:
            if self.closed or time.time()>self.expires:raise ValueError('登录窗口已过期，请重新打开')
            if self.process.poll() is not None:raise ValueError('登录窗口已关闭，请重新打开')
            try:
                self.process.stdin.write(json.dumps(dict(action=action,**fields),ensure_ascii=False)+'\n');self.process.stdin.flush()
                response=self.responses.get(timeout=35)
            except (OSError,queue.Empty):
                self.close();raise ValueError('登录窗口连接超时，请重新打开') from None
            if response is None:raise ValueError('登录窗口已结束，请重新打开')
            if not response.get('ok'):raise ValueError(response.get('message','登录操作失败'))
            return dict(response,session_id=self.id,account_id=self.account_id,expires_at=self.expires)

    def finish(self):
        with self.lock:
            result=self.rpc('verify')
            try:
                old=self.accounts.get(self.account_id)
                if old['profile']:
                    cooldown=self.accounts.state_path(self.account_id)/'last-accepted.json'
                    if cooldown.is_file():shutil.copy2(cooldown,self.directory/'last-accepted.json')
                account=self.accounts.bind(self.account_id,result['user_id'],self.id)
                self.saved=True
                return dict(account=account,saved=True,switched=False,message='登录已保存，请选择此账户并点击切换')
            finally:self.close()

    def close(self):
        with self.lock:
            if self.closed:return
            self.closed=True;self.timer.cancel()
            if self.process.poll() is None:
                self.process.terminate()
                try:self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:self.process.kill();self.process.wait(timeout=5)
            for stream in (self.process.stdin,self.process.stdout):
                if stream:stream.close()
            # Only this uncommitted, UUID-named session directory is disposable.
            root=(self.accounts.settings.state_dir/'official-accounts').resolve()
            if not self.saved and self.directory.is_relative_to(root) and self.directory.name==self.id:
                shutil.rmtree(self.directory,ignore_errors=True)


class OfficialLogin:
    def __init__(self, accounts):
        self.accounts=accounts;self.sessions={};self.lock=threading.RLock()

    def start(self, account_id):
        with self.lock:
            self.sessions={key:value for key,value in self.sessions.items() if not value.closed}
            if len(self.sessions)>=2:raise ValueError('已有两个登录窗口，请先关闭不用的窗口')
            if any(s.account_id==account_id for s in self.sessions.values()):raise ValueError('此账户已有登录窗口，请先关闭它')
            session=LoginSession(self.accounts,account_id)
            self.sessions[session.id]=session
        try:return session.rpc('start',state_dir=str(session.directory))
        except Exception:
            session.close();raise

    def get(self, identity):
        with self.lock:session=self.sessions.get(identity)
        if not session or session.closed:raise ValueError('登录窗口不存在或已过期')
        return session

    def close(self):
        for session in list(self.sessions.values()):session.close()

"""Out-of-process maintenance-controller activation, with recovery and rollback."""
import fcntl
import hashlib
import json
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.request

LIVE=Path('/opt/cannjudge-local-oj')
STATE=Path('/var/lib/cann-oj-rsi')
RUNTIME=Path('/opt/cann-oj-rsi')


def check():
    key=Path('/etc/cann-oj-rsi/token').read_text().strip()
    request=urllib.request.Request('http://127.0.0.1:8776/rpc',data=b'{"op":"status"}',
        headers={'Authorization':'Bearer '+key,'Content-Type':'application/json'})
    with urllib.request.urlopen(request,timeout=5) as response:return json.load(response)


def main():
    identity=sys.argv[1]
    if not re.fullmatch('[0-9a-f]{32}',identity):raise ValueError('Invalid task')
    folder=STATE/'jobs'/identity;backup=folder/('runtime-backup-'+str(time.time_ns()))
    staged=folder/'runtime-staged';staged.mkdir(exist_ok=True)
    source=LIVE/'rsi'
    for path in source.glob('*.py'):
        if path.is_symlink():raise ValueError('Symlinked runtime')
        compile(path.read_text(),str(path),'exec')
        shutil.copy2(path,staged/path.name)
    if not (staged/'service.py').exists():raise ValueError('Controller missing')
    with (STATE/'runtime.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        shutil.copytree(RUNTIME,backup,ignore=shutil.ignore_patterns('__pycache__'))
        subprocess.run(['systemctl','stop','cann-oj-rsi'],check=True,timeout=45)
        try:
            for path in staged.glob('*.py'):
                target=RUNTIME/path.name;temporary=target.with_suffix('.new')
                shutil.copy2(path,temporary);temporary.chmod(0o644);temporary.replace(target)
            subprocess.run(['systemctl','start','cann-oj-rsi'],check=True,timeout=30)
            healthy=False
            for _ in range(15):
                try:
                    status=check();healthy=bool(status.get('version'))
                    if healthy:break
                except Exception:pass
                time.sleep(2)
            if not healthy:raise RuntimeError('New controller health failed')
            outcome='deployed';error=None
        except Exception as e:
            subprocess.run(['systemctl','stop','cann-oj-rsi'],timeout=45)
            for path in RUNTIME.glob('*.py'):
                if not (backup/path.name).exists():path.unlink()
            for path in backup.glob('*.py'):shutil.copy2(path,RUNTIME/path.name)
            subprocess.run(['systemctl','start','cann-oj-rsi'],check=True,timeout=30)
            outcome='review_required';error=str(e)
        with sqlite3.connect(STATE/'jobs.sqlite3',timeout=60) as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT result FROM jobs WHERE id=?',(identity,)).fetchone()
            result=json.loads(row[0]);result.update(runtime_activation=dict(at=time.time(),backup=str(backup),error=error),stage='observe' if not error else 'runtime_review')
            if not error:result['health_check']=True
            db.execute('UPDATE jobs SET status=?,updated=?,result=? WHERE id=?',(outcome,time.time(),json.dumps(result),identity))
            for key in ('runtime_handoff','runtime_activation_scheduled'):
                db.execute('INSERT OR REPLACE INTO control VALUES (?,?)',(key,'false'))
        (folder/'runtime-activation.json').write_text(json.dumps({'status':outcome,'backup':str(backup),'error':error},indent=2))
        if error:raise RuntimeError(error)


if __name__=='__main__':main()

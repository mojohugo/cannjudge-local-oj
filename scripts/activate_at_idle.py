"""Warm a replacement web service; switch only with no active evaluation jobs."""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))


def code_hashes():
    paths = [*sorted((ROOT/'oj').glob('*.py')),ROOT/'config/contest_scoring.json',
             ROOT/'config/official_submit.json',ROOT/'config/official_cases.json',ROOT/'scripts/official-browser.mjs',ROOT/'scripts/official-data.mjs']
    return {str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


def serve(ready):
    import uvicorn
    from oj.app import create_app
    app = create_app()  # Import and construct now; evaluation workers start only at activation.
    ready.write_text(str(__import__('os').getpid()))
    if sys.stdin.readline().strip()!='ACTIVATE':
        return
    uvicorn.run(app,host='127.0.0.1',port=8765,workers=1,proxy_headers=False,access_log=False)


def activate(args):
    stage=args.stage.resolve()
    for name in ('before/index.html','web/index.html','web/app.js','web/polling.js'):
        if not (stage/name).is_file():
            raise RuntimeError('Incomplete frontend stage; original service remains running: '+name)
    if (ROOT/'web/index.html').read_bytes() not in ((stage/'before/index.html').read_bytes(),(stage/'web/index.html').read_bytes()):
        raise RuntimeError('Live frontend changed after staging; original service remains running')
    release_dir = ROOT/'var/development'/('activation-'+uuid.uuid4().hex)
    release_dir.mkdir()
    ready = release_dir/'ready'
    report_path = ROOT/'var/leaderboard-activation.json'
    expected = code_hashes()
    log = (release_dir/'server.log').open('w',encoding='utf-8')
    server = subprocess.Popen([sys.executable,'-X','utf8',str(Path(__file__).resolve()),
                               '--serve-warm',str(ready)],cwd=ROOT,stdin=subprocess.PIPE,
                              stdout=log,stderr=subprocess.STDOUT,text=True,
                              creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    activated = False
    try:
        for _ in range(200):
            if ready.exists():break
            if server.poll() is not None:raise RuntimeError('Replacement preflight failed; original service remains running')
            time.sleep(.1)
        else:raise RuntimeError('Replacement preflight timed out; original service remains running')
        started=time.monotonic();previous=None
        while time.monotonic()-started<args.timeout:
            with sqlite3.connect(ROOT/'var/oj.sqlite3',timeout=10) as db:
                db.execute('BEGIN IMMEDIATE')
                active=db.execute("SELECT id,status FROM submissions WHERE status IN ('connecting','waiting','generating','compiling','running') ORDER BY id").fetchall()
                tables={row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if 'official_submissions' in tables:
                    active+=db.execute("SELECT id,status FROM official_submissions WHERE status IN ('validating','submitting','submitted','judging')").fetchall()
                if 'official_sync' in tables:
                    active+=db.execute("SELECT id,status FROM official_sync WHERE status='running'").fetchall()
                queued=db.execute("SELECT id FROM submissions WHERE status='queued' ORDER BY created").fetchall()
                if active:
                    db.rollback()
                    state=(active,len(queued))
                    if state!=previous:
                        print(json.dumps(dict(waiting_for_idle=active,queued=len(queued),jobs_interrupted=0)),flush=True)
                        previous=state
                else:
                    if code_hashes()!=expected:raise RuntimeError('Source changed after warm-up; refusing stale deployment')
                    old_pid=int((ROOT/'var/server.pid').read_text().strip())
                    command=subprocess.check_output(['powershell','-NoProfile','-Command',
                        f"(Get-CimInstance Win32_Process -Filter 'ProcessId = {old_pid}').CommandLine"],text=True)
                    if not ('-m uvicorn oj.app:create_app' in command or 'activate_at_idle.py' in command and '--serve-warm' in command):
                        raise RuntimeError('Original server PID identity mismatch')
                    # Allow terminal-result file/baseline finalization to finish; claims remain blocked.
                    time.sleep(.25)
                    cutover=time.monotonic()
                    subprocess.run(['powershell','-NoProfile','-Command',f'Stop-Process -Id {old_pid}'],check=True,timeout=5)
                    db.commit()
                    server.stdin.write('ACTIVATE\n');server.stdin.flush();server.stdin.close()
                    activated=True
                    (ROOT/'var/server.pid').write_text(str(server.pid))
                    public_path=ROOT/'var/public-mcp.json'
                    if public_path.exists():
                        public=json.loads(public_path.read_text(encoding='utf-8-sig'));public['server_pid']=server.pid
                        public_path.write_text(json.dumps(public,indent=2)+'\n',encoding='utf-8')
                    break
            time.sleep(2)
        if not activated:raise RuntimeError('No idle boundary within timeout; original service was not stopped')
        for _ in range(100):
            if server.poll() is not None:raise RuntimeError('Replacement exited; inspect '+str(release_dir/'server.log'))
            try:
                with urllib.request.urlopen('http://127.0.0.1:8765/api/health',timeout=1) as response:
                    if json.load(response).get('status')=='ok':break
            except OSError:pass
            time.sleep(.1)
        else:raise RuntimeError('Replacement health check timed out')
        report=dict(activated=True,server_pid=server.pid,previous_server_pid=old_pid,
                    active_jobs_interrupted=0,preserved_queued_jobs=[r[0] for r in queued],
                    cutover_seconds=round(time.monotonic()-cutover,3),code_sha256=expected,
                    server_log=str(release_dir/'server.log'),public_tunnel_restarted=False)
        report_path.write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
        subprocess.run([sys.executable,str(ROOT/'scripts/publish_frontend.py'),str(args.stage),
                        '--release',args.release],cwd=ROOT,check=True)
        print(json.dumps(report),flush=True)
        return server.wait()
    finally:
        if not activated and server.poll() is None:
            server.terminate();server.wait(timeout=5)
        log.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--serve-warm',type=Path)
    parser.add_argument('--stage',type=Path)
    parser.add_argument('--release')
    parser.add_argument('--timeout',type=int,default=1800)
    args=parser.parse_args()
    if args.serve_warm:serve(args.serve_warm)
    else:
        if not args.stage or not args.release:parser.error('--stage and --release are required')
        raise SystemExit(activate(args))

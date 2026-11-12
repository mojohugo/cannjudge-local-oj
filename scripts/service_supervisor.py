"""Run the Linux web service and adopt tunnel URL changes only when jobs are idle."""
from datetime import datetime
from pathlib import Path
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import time
from urllib.parse import urlparse

ROOT=Path(__file__).resolve().parent.parent


def current_tunnel_start():
    boot=int(next(line.split()[1] for line in Path('/proc/stat').read_text().splitlines() if line.startswith('btime ')))
    starts=[]
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():continue
        try:
            argv=(entry/'cmdline').read_bytes().split(b'\0')
            if argv[:2]!=[str(ROOT/'runtime/cloudflared').encode(),b'tunnel']:continue
            fields=(entry/'stat').read_text().rsplit(')',1)[1].split()
            starts.append(boot+int(fields[19])/os.sysconf('SC_CLK_TCK'))
        except (OSError,ValueError,IndexError):continue
    return max(starts,default=None)


def origin_from_log(text,started):
    if started is None:return None
    origin=None
    connected=False
    for line in text.splitlines():
        try:stamp=datetime.fromisoformat(line[:20].replace('Z','+00:00')).timestamp()
        except ValueError:continue
        if stamp<started-1:continue
        found=re.search(r'https://[a-z0-9-]+\.trycloudflare\.com\b',line)
        if found:origin=found.group()
        if 'Registered tunnel connection' in line:connected=True
    return origin if connected else None


def tunnel_origin():
    try:return origin_from_log((ROOT/'logs/cloudflared.log').read_text(),current_tunnel_start())
    except OSError:return None


def active_jobs(db):
    active=db.execute("SELECT count(*) FROM submissions WHERE status IN ('connecting','waiting','generating','compiling','running')").fetchone()[0]
    tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'official_submissions' in tables:
        active+=db.execute("SELECT count(*) FROM official_submissions WHERE status IN ('validating','submitting','submitted','judging')").fetchone()[0]
    if 'official_sync' in tables:
        active+=db.execute("SELECT count(*) FROM official_sync WHERE status='running'").fetchone()[0]
    return active


def set_origin(origin):
    path=ROOT/'var/config.json'
    value=json.loads(path.read_text())
    old=urlparse(value.get('public_origin') or '').netloc
    hosts=[h for h in value['allowed_hosts'] if h!=old or h in ('127.0.0.1','localhost','[::1]')]
    value.update(public_origin=origin,allowed_hosts=list(dict.fromkeys(hosts+[urlparse(origin).netloc])))
    pending=path.with_suffix('.tmp');pending.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n');pending.chmod(0o600);pending.replace(path)
    (ROOT/'var/public-mcp.json').write_text(json.dumps(dict(origin=origin,mcp_url=origin+'/mcp',backend=value.get('ssh_host', 'npu-worker'),temporary=True),indent=2)+'\n')
    (ROOT/'var/public-mcp-url.txt').write_text(origin+'/mcp\n')
    print(json.dumps(dict(mcp_url=origin+'/mcp',worker_enabled=value['worker_enabled'])),flush=True)


def launch():
    process=subprocess.Popen([sys.executable,'-m','uvicorn','oj.app:create_app','--factory','--host','127.0.0.1','--port','8765','--workers','1','--no-proxy-headers','--no-access-log'],cwd=ROOT)
    (ROOT/'var/server.pid').write_text(str(process.pid))
    return process


def main():
    process=None
    stopping=False
    def stop(_signal,_frame):
        nonlocal stopping
        stopping=True
        if process and process.poll() is None:process.terminate()
    signal.signal(signal.SIGTERM,stop)
    signal.signal(signal.SIGINT,stop)
    while not stopping:
        origin=tunnel_origin()
        if origin:break
        time.sleep(1)
    if stopping:return
    set_origin(origin)
    process=launch()
    try:
        while not stopping:
            if process.poll() is not None:raise RuntimeError('Web process exited; supervisor will restart it')
            candidate=tunnel_origin()
            if candidate and candidate!=origin:
                with sqlite3.connect(ROOT/'var/oj.sqlite3',timeout=15) as db:
                    db.execute('BEGIN IMMEDIATE')
                    if not active_jobs(db):
                        # Block new claims until the old process has stopped. Finished
                        # jobs get a moment to finish writing logs and baseline files.
                        time.sleep(.3)
                        process.terminate()
                        try:process.wait(timeout=12)
                        except subprocess.TimeoutExpired:
                            process.kill();process.wait(timeout=5)
                        db.commit()
                        set_origin(candidate)
                        origin=candidate
                        process=launch()
            time.sleep(2)
    finally:
        if process and process.poll() is None:
            process.terminate()
            try:process.wait(timeout=15)
            except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)


if __name__=='__main__':main()

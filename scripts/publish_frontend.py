"""Publish versioned static assets, then atomically switch HTML without restarting OJ."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import time
import urllib.request
import uuid

ROOT=Path(__file__).resolve().parent.parent


def sha(data):
    return hashlib.sha256(data).hexdigest()


def active_jobs():
    with sqlite3.connect(ROOT/'var/oj.sqlite3',timeout=5) as db:
        return db.execute("SELECT id,problem_id,status FROM submissions WHERE status IN ('queued','connecting','waiting','generating','compiling','running') ORDER BY created").fetchall()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage',type=Path)
    parser.add_argument('--release',required=True)
    parser.add_argument('--url',default='http://127.0.0.1:8765')
    args=parser.parse_args()
    if not re.fullmatch(r'[a-z][a-z0-9-]{0,63}',args.release):
        raise ValueError('Invalid static release name')
    stage=args.stage.resolve()
    index=ROOT/'web/index.html'
    previous=(stage/'before/index.html').read_bytes()
    candidate=(stage/'web/index.html').read_bytes()
    prefix='/static/releases/'+args.release+'/'
    files=('polling.js','app.js')+tuple(p.name for p in sorted((stage/'web').glob('*.css'))
                                       if (prefix+p.name).encode() in candidate)
    if any((prefix+name).encode() not in candidate for name in files):
        raise ValueError('Staged HTML does not reference the exact versioned assets')
    for name in files:
        if name.endswith('.js'):
            subprocess.run(['node','--check',str(stage/'web'/name)],check=True)
    live=index.read_bytes()
    if live not in (previous,candidate):
        raise RuntimeError('Live HTML changed since staging; no files were overwritten')
    hashes={name:sha((stage/'web'/name).read_bytes()) for name in files}
    parent=(ROOT/'web/releases').resolve()
    target=(parent/args.release).resolve()
    if target.parent!=parent or not parent.is_relative_to((ROOT/'web').resolve()):
        raise RuntimeError('Release path outside the static workspace')
    parent.mkdir(exist_ok=True)
    if target.exists():
        if any(sha((target/name).read_bytes())!=hashes[name] for name in files):
            raise RuntimeError('An existing release is immutable; choose a new name')
    else:
        pending=(parent/(args.release+'.'+uuid.uuid4().hex+'.next')).resolve()
        if pending.parent!=parent:
            raise RuntimeError('Invalid staging path')
        pending.mkdir()
        for name in files:shutil.copy2(stage/'web'/name,pending/name)
        pending.replace(target)
    # Verify files through the existing HTTP server before any page references them.
    for name in files:
        with urllib.request.urlopen(args.url.rstrip('/')+prefix+name,timeout=15) as response:
            assert sha(response.read())==hashes[name], 'Served static asset hash mismatch'
    before_pid=(ROOT/'var/server.pid').read_text().strip()
    before_jobs=active_jobs()
    pending=index.with_name('index.'+uuid.uuid4().hex+'.next.html')
    pending.write_bytes(candidate)
    for attempt in range(20):
        try:
            # A reader gets either complete old HTML or complete new HTML.
            pending.replace(index)
            break
        except PermissionError:
            if attempt==19:raise
            time.sleep(0.05)
    with urllib.request.urlopen(args.url.rstrip('/')+'/',timeout=15) as response:
        assert sha(response.read())==sha(candidate), 'Live HTML differs from validated release'
    after_pid=(ROOT/'var/server.pid').read_text().strip()
    assert before_pid==after_pid, 'Server was independently restarted during publication'
    report=dict(release=args.release,published_at=time.time(),server_pid=int(after_pid),
                server_restarted=False,previous_html_sha256=sha(previous),html_sha256=sha(candidate),
                assets=hashes,active_jobs_before=before_jobs,active_jobs_after=active_jobs(),
                rollback_html=str(stage/'before/index.html'))
    (ROOT/'var/frontend-release.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False))


if __name__=='__main__':
    main()

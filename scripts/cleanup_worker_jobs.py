"""Send verified archived-job identities to the active NPU worker's cleanup tool."""
import argparse
import hashlib
import json
from pathlib import Path
import shlex
import sqlite3
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from oj.config import load_settings
from oj.ssh_connection import SSHConnector


def archived_jobs(settings):
    records = {}
    with sqlite3.connect(settings.state_dir/'oj.sqlite3', timeout=15) as db:
        rows = db.execute("SELECT id,updated,result FROM submissions WHERE status IN ('accepted','wrong_answer','compatibility_mismatch','compile_error','runtime_error','time_limit','data_error','illegal')").fetchall()
    for identity, completed, raw in rows:
        result = json.loads(raw)
        if result.get('execution_target', {}).get('ssh_host') != settings.ssh_host:
            continue
        directory = settings.state_dir/'submissions'/identity
        if not (directory/'result.json').is_file() or not (directory/'kernel.asc').is_file():
            continue
        archive = json.loads((directory/'result.json').read_text(encoding='utf-8'))
        remote = result.get('environment', {}).get('remote_job')
        if not remote or archive.get('source_sha256') != result.get('source_sha256'):
            continue
        if archive.get('environment', {}).get('remote_job') != remote:
            continue
        remote_id = Path(remote).name
        records[remote_id] = dict(id=remote_id, completed=completed,
            source_sha256=hashlib.sha256((directory/'kernel.asc').read_bytes()).hexdigest())
        # ABBA slots are sibling worker jobs. Reclaim their scratch space only
        # after the complete diagnostic evidence and both sources are archived.
        report = result.get('local_ab')
        if result.get('suite') != 'local_ab' or not report or archive.get('local_ab') != report:
            continue
        try:
            from remote.local_ab import validate_report
            validate_report(report, result['local_ab_request'], result.get('dataset'))
            hashes = {label:hashlib.sha256((directory/name).read_bytes()).hexdigest()
                      for label,name in [('A','kernel.asc'),('B','kernel-b.asc')]}
            if hashes != report['source_sha256']:
                continue
            for run in report['runs']:
                child = Path(run['remote_job'])
                if child.parent != Path(remote).parent:
                    continue
                records[child.name] = dict(id=child.name, completed=completed,
                    source_sha256=hashes[run['label']])
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return list(records.values())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--max-jobs', type=int, default=200)
    args = parser.parse_args()
    settings = load_settings()
    records = archived_jobs(settings)
    connector = SSHConnector(settings)
    connector.probe()
    command = connector.command('python3 '+shlex.quote(settings.remote_root+'/job_cleanup.py'))
    run = subprocess.run(command, input=json.dumps(dict(records=records, apply=args.apply, max_jobs=args.max_jobs)),
                         text=True, capture_output=True, timeout=540)
    if run.returncode:
        raise RuntimeError(run.stderr[-2000:])
    report = json.loads(run.stdout)
    report.update(checked_at=time.time(), backend=settings.ssh_host, verified_archives=len(records))
    target = settings.state_dir/'worker-cleanup-latest.json'
    temporary = target.with_suffix('.tmp')
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    temporary.replace(target)
    print(json.dumps({k:v for k,v in report.items() if k!='job_ids'}, ensure_ascii=False))


if __name__ == '__main__':
    main()

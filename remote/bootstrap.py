#!/usr/bin/env python3
"""Install owned worker resources; retain submissions and the existing project."""
import fcntl
import json
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import re
import sys


def main():
    if os.geteuid() != 0:
        raise SystemExit('Install the trusted coordinator as administrator')
    staging = Path(__file__).resolve().parent
    config = json.loads((staging / 'worker-config.json').read_text())
    root, jobs, lock_path = (Path(config[key]) for key in ('root', 'jobs', 'lock'))
    if any(not path.is_absolute() or path == Path('/') for path in (root, jobs, lock_path)):
        raise SystemExit('root, jobs and lock must be absolute non-root paths')
    user = config['execution_user']
    if config['isolation'] != 'separate_uid' or not re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}', user) or user == 'root':
        raise SystemExit('Configure separate_uid with a dedicated non-root execution user')
    if not (staging / 'env.sh').is_file():
        raise SystemExit('Stage the configured CANN env.sh before installation')
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            account = pwd.getpwnam(user)
        except KeyError:
            subprocess.run(['useradd', '--system', '--no-create-home', '--home-dir', '/nonexistent', '--shell', '/usr/sbin/nologin', user], check=True)
            account = pwd.getpwnam(user)
        if account.pw_uid == 0:
            raise SystemExit('Execution account must not have UID 0')
        root.mkdir(mode=0o755, parents=True, exist_ok=True)
        for source in staging.rglob('*'):
            if not source.is_file() or '__pycache__' in source.parts:
                continue
            dest = root / source.relative_to(staging)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, dest)
            dest.chmod(0o644)
        jobs.mkdir(parents=True, exist_ok=True, mode=0o755)
        # The Ascend logger consults passwd's home directory, not only $HOME.
        home = jobs.parent / 'home'
        home.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chown(home, account.pw_uid, account.pw_gid)
        if account.pw_dir != str(home):
            subprocess.run(['usermod', '--home', str(home), user], check=True)
        sys.path.insert(0, str(root))
        from registry import Registry
        registry = Registry(root / 'problems')
        for problem in registry.listing():
            registry.adapter(problem['id']).install()
        print(json.dumps(dict(status='installed', root=str(root), problems=[p['id'] for p in registry.listing()], execution_user=user)))



if __name__ == '__main__':
    main()

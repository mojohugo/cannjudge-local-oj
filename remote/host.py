"""Administrator/owner supplied host configuration, separate from problem packs."""
import json
import os
from pathlib import Path
import pwd

ROOT = Path(__file__).resolve().parent
CONFIG = json.loads((ROOT / 'worker-config.json').read_text()) if (ROOT / 'worker-config.json').exists() else {}
JOBS = Path(CONFIG.get('jobs', '/var/lib/cannjudge-local-oj/jobs'))
LOCK = Path(CONFIG.get('lock', '/var/lock/cannjudge-oj-device0.lock'))
EXECUTION_USER = CONFIG.get('execution_user', 'cannjudge_oj')
ISOLATION = CONFIG.get('isolation', 'separate_uid')


def account():
    selected = pwd.getpwnam(EXECUTION_USER)
    if ISOLATION == 'separate_uid':
        if os.geteuid() != 0 or selected.pw_uid == 0:
            raise RuntimeError('separate_uid mode requires privileged coordinator and non-root execution account')
    elif ISOLATION == 'trusted_user':
        if os.geteuid() == 0 or selected.pw_uid != os.geteuid():
            raise RuntimeError('trusted_user mode must use the current non-root account')
    else:
        raise RuntimeError('Unknown worker isolation mode')
    return selected


def own(path):
    selected = account()
    if os.geteuid() == 0:
        os.chown(path, selected.pw_uid, selected.pw_gid)
    elif path.stat().st_uid != selected.pw_uid:
        raise RuntimeError('Worker path is not owned by the configured account')

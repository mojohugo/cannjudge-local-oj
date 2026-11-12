#!/usr/bin/env python3
"""Forced-command gateway for the web service's loopback-only worker key."""
import os
from pathlib import Path
import re
import shlex
import sys


def allowed(command, root):
    try:
        parts=shlex.split(command)
    except ValueError:
        return None
    if parts==['echo','cannjudge-ready']:
        return ['ready']
    if (len(parts)==3 and parts[:2]==['python3',str(root/'worker.py')]
            and re.fullmatch(r'[0-9a-f]{32}',parts[2])):
        return ['worker',parts[2]]
    return None


if __name__=='__main__':
    root=Path(__file__).resolve().parent
    action=allowed(os.environ.get('SSH_ORIGINAL_COMMAND',''),root)
    if action==['ready']:
        print('cannjudge-ready')
    elif action and action[0]=='worker':
        os.execv(sys.executable,[sys.executable,str(root/'worker.py'),action[1]])
    else:
        raise SystemExit('Only the configured OJ worker command is permitted')

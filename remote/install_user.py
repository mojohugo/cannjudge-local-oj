"""Install a private trusted-user worker without administrator privileges."""
import fcntl
import json
import os
from pathlib import Path
import pwd
import shutil
import sys

staging=Path(__file__).resolve().parent
config=json.loads((staging/'worker-config.json').read_text())
root=Path(config['root'])
selected=pwd.getpwnam(config['execution_user'])
if os.geteuid()==0 or selected.pw_uid!=os.geteuid() or config['isolation']!='trusted_user':
    raise SystemExit('This installer requires the configured non-root owner')
root.mkdir(parents=True,exist_ok=True)
Path(config['lock']).parent.mkdir(parents=True,exist_ok=True)
with Path(config['lock']).open('a') as lock:
    fcntl.flock(lock,fcntl.LOCK_EX)
    for source in staging.rglob('*'):
        if not source.is_file() or '__pycache__' in source.parts:
            continue
        destination=root/source.relative_to(staging)
        destination.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(source,destination)
        destination.chmod(0o600)
    Path(config['jobs']).mkdir(mode=0o700,parents=True,exist_ok=True)
    sys.path.insert(0,str(root))
    from registry import Registry
    registry=Registry(root/'problems')
    for problem in registry.listing():
        registry.adapter(problem['id']).install()
    print(json.dumps(dict(status='installed',root=str(root),execution_user=selected.pw_name,
                         isolation=config['isolation'],problems=[p['id'] for p in registry.listing()])))

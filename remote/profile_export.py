"""Export existing profiler databases on CPU; never launches a device workload."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def export_one(item, script):
    started = time.monotonic()
    try:
        process = subprocess.run([sys.executable, str(script), 'export', 'summary',
                                  '-dir', item['directory']],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, timeout=120)
        return dict(group=item['group'], code=process.returncode, reason=None,
                    output=process.stdout.decode('utf-8', 'replace')[-240000:],
                    seconds=round(time.monotonic()-started, 4))
    except subprocess.TimeoutExpired as error:
        return dict(group=item['group'], code=124, reason='timeout',
                    output=(error.stdout or b'').decode('utf-8', 'replace')[-240000:],
                    seconds=round(time.monotonic()-started, 4))


def main():
    plan = json.loads(Path(sys.argv[1]).read_text())
    work = Path(plan['work']).resolve(strict=True)
    script = Path(os.environ['ASCEND_HOME_PATH'])/'tools/profiler/profiler_tool/analysis/msprof/msprof.py'
    if not script.is_file() or not 1 <= len(plan['exports']) <= 3:
        raise ValueError('Invalid profiler export plan or SDK')
    groups = set()
    for item in plan['exports']:
        directory = Path(item['directory'])
        resolved = directory.resolve(strict=True)
        if (item['group'] not in {'PipeUtilization', 'L2Cache', 'MemoryUB'}
                or item['group'] in groups or directory.is_symlink()
                or not resolved.is_relative_to(work) or not resolved.name.startswith('PROF_')
                or not (resolved/'host').is_dir()):
            raise ValueError('Unexpected profiler export directory/group')
        groups.add(item['group'])
    # The coordinator supervises this whole process group, including SDK children.
    # No preexec_fn, fork hooks, device programs or device locks in these threads.
    with ThreadPoolExecutor(max_workers=len(groups)) as pool:
        result = list(pool.map(lambda item: export_one(item, script), plan['exports']))
    print(json.dumps(dict(exports=result)), flush=True)


if __name__ == '__main__':
    main()

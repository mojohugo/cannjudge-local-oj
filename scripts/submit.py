"""Submit a kernel via the private HTTP API; no browser automation needed."""
import argparse
import json
from pathlib import Path
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parent.parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('--url', default='http://127.0.0.1:8765')
    parser.add_argument('--label', default='')
    parser.add_argument('--problem', default='cum_sum', help='Installed problem ID; list via GET /api/problems')
    parser.add_argument('--wait', action='store_true')
    parser.add_argument('--generator', type=Path, help='Custom C++17 input generator')
    parser.add_argument('--profile-mode', choices=['full','timing'], default='full', help='timing retains correctness and device timing; full also collects cache/UB counters')
    args = parser.parse_args()
    credentials = (ROOT / 'var/admin-access.txt').read_text(encoding='utf-8')
    token = next(line.removeprefix('API bearer token: ') for line in credentials.splitlines() if line.startswith('API bearer token: '))
    def request(path, data=None):
        encoded = json.dumps(data).encode() if data is not None else None
        req = urllib.request.Request(args.url.rstrip('/') + path, data=encoded,
                                     headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=20) as response:
            return json.load(response)
    job = request('/api/submissions', dict(problem_id=args.problem, source=args.source.read_text(encoding='utf-8-sig'), label=args.label or args.source.name,
                                          generator=args.generator.read_text(encoding='utf-8-sig') if args.generator else None,profile_mode=args.profile_mode))
    print('Submission: ' + job['id'])
    last = None
    while args.wait:
        job = request('/api/submissions/' + job['id'])
        state = (job['status'], job['result'].get('message'))
        if state != last:
            print(state, flush=True)
            last = state
        if job['status'] not in {'queued','connecting','waiting','generating','compiling','running'}:
            for case in job['result'].get('cases', []):
                print(case['index'], case['status'], case.get('time_us'), 'us')
            return 0 if job['status'] == 'accepted' else 1
        time.sleep(3)
    return 0


if __name__ == '__main__':
    sys.exit(main())

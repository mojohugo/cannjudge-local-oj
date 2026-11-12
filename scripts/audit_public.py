"""Check staged Git blobs before publication, without printing secret values.

This is a release guard, not a substitute for reviewing the staged diff.
Run after git add; unstaged/ignored working files are deliberately not published.
"""
import argparse
import json
from pathlib import Path, PurePosixPath
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
PRIVATE_DIRS = {'var', 'tools', 'runtime', 'node_modules', '.venv', '.ssh', '.tools',
                'exports', 'benchmarks', 'logs', '__pycache__', '.pytest_cache',
                'skills', 'backups', 'staging', 'home', 'problems', 'knowledge',
                'official_problems', 'official_templates', 'fixtures', 'datasets', 'data', 'evidence'}
PRIVATE_FILES = {'config/backends.json', 'config.local.json', 'worker-config.json',
                 'env.sh', 'admin-access.txt', 'RSI-ANSWER.txt'}
PRIVATE_SUFFIXES = {'.pem', '.key', '.p12', '.pfx', '.sqlite3', '.sqlite', '.db', '.bin',
                    '.exe', '.dll', '.so', '.zip', '.tgz', '.log', '.pyc'}
PATTERNS = {
    'private key': rb'-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----',
    'GitHub token': rb'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b',
    'personal Windows path': rb'(?i)[CD]:[\\/]+Users[\\/]+(?!Public\b|<|example\b)[^\s"<>]+',
    'personal workspace path': rb'(?i)[A-Z]:[\\/]+(?:develop|projects)[\\/]+[^\s"<>]+',
}


def issues_for(name, data, known_values=()):
    path = PurePosixPath(name)
    issues = []
    if (set(path.parts) & PRIVATE_DIRS or name in PRIVATE_FILES
            or path.suffix.lower() in PRIVATE_SUFFIXES
            or path.name == '.env' or path.name.startswith('.env.') and path.name != '.env.example'
            or name.startswith('problems/') and 'data' in path.parts
            or path.name in {'reference', 'generate_fixed'}):
        issues.append('private or generated file')
    if len(data) > 1024 * 1024:
        issues.append('file exceeds 1 MiB; review before inclusion')
    empty_config_fields = {
        'config/official_cases.json': ('problems',),
        'config/contest_scoring.json': ('problems', 'local_suites'),
        'config/official_submit.json': ('problems', 'contest_overrides', 'project_templates', 'kernel_templates'),
    }
    if name in empty_config_fields:
        try:
            value = json.loads(data)
            if any(value.get(key) != {} for key in empty_config_fields[name]):
                issues.append('private problem or score configuration')
        except (ValueError, AttributeError):
            issues.append('invalid public configuration')
    for label, pattern in PATTERNS.items():
        if re.search(pattern, data):
            issues.append(label)
    for value in known_values:
        if value and value in data:
            issues.append('value from private deployment state')
            break
    if not name.startswith('tests/'):
        if re.search(rb'https://[a-z0-9-]+\.trycloudflare\.com', data):
            issues.append('temporary deployment URL')
        if re.search(rb'/submission/[a-f0-9]{24}\b', data):
            issues.append('personal official submission URL')
    return issues


def private_values():
    """Read only known local credentials/configs, never include them in output."""
    values = []
    config = ROOT / 'var/config.json'
    if config.is_file():
        data = json.loads(config.read_text(encoding='utf-8'))
        for key in ('password_salt', 'password_digest', 'api_token_digest', 'public_origin'):
            if data.get(key):
                values.append(data[key].encode())
    credentials = ROOT / 'var/admin-access.txt'
    if credentials.is_file():
        for line in credentials.read_text(encoding='utf-8').splitlines():
            if line.startswith(('Login password: ', 'API bearer token: ')):
                values.append(line.split(': ', 1)[1].encode())
    return values


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--no-local-secrets', action='store_true', help='Only use portable checks (e.g. CI)')
    args = parser.parse_args()
    entries = subprocess.check_output(['git', 'ls-files', '--stage', '-z'], cwd=ROOT).split(b'\0')
    known = [] if args.no_local_secrets else private_values()
    problems, count, total = [], 0, 0
    for entry in entries:
        if not entry:
            continue
        header, raw_name = entry.split(b'\t', 1)
        mode, oid, stage = header.split()
        name = raw_name.decode('utf-8')
        if stage != b'0' or mode not in {b'100644', b'100755'}:
            problems.append((name, ['unmerged file, symlink or submodule']))
            continue
        data = subprocess.check_output(['git', 'cat-file', 'blob', oid.decode()], cwd=ROOT)
        findings = issues_for(name, data, known)
        if findings:
            problems.append((name, findings))
        count += 1
        total += len(data)
    if not count:
        raise SystemExit('No staged/tracked files. Stage the intended public files first.')
    for name, findings in problems:
        print(name + ': ' + '; '.join(findings))
    print(json.dumps(dict(files=count, bytes=total, findings=len(problems))))
    if problems:
        raise SystemExit(1)


if __name__ == '__main__':
    main()

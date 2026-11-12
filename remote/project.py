"""Canonical source bundles for administrator-installed multi-file problems."""
import hashlib
import json
import re


def normalize_files(files, required, max_bytes=2 * 1024 * 1024):
    required = set(required)
    if not isinstance(files, list) or len(files) != len(required):
        raise ValueError('必须提供完整可编辑文件集合：' + ', '.join(sorted(required)))
    values = {}
    for item in files:
        if not isinstance(item, dict) or set(item) != {'path', 'content'}:
            raise ValueError('工程文件需要 path 和 content')
        path, text = item['path'], item['content']
        if not isinstance(path, str) or path not in required or path in values:
            raise ValueError('文件重复、只读或不在允许列表中')
        if not isinstance(text, str) or not text.strip() or '\0' in text:
            raise ValueError('工程源码不能为空或包含 NUL')
        values[path] = text.removeprefix('\ufeff').replace('\r\n', '\n').replace('\r', '\n')
    if sum(len(x.encode('utf-8')) for x in values.values()) > max_bytes:
        raise ValueError('Source too large')
    files = [{'path': path, 'content': text} for path, text in sorted(values.items())]
    hashes = [[f['path'], hashlib.sha256(f['content'].encode()).hexdigest()] for f in files]
    digest = hashlib.sha256(json.dumps(hashes, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
    encoded = json.dumps(files, separators=(',', ':'), ensure_ascii=False).encode()
    return files, digest, encoded


def source_digest(source, contract):
    if contract.get('submission_kind') == 'project':
        return normalize_files(json.loads(source), contract['editable_files'])[1]
    return hashlib.sha256(source).hexdigest()


def inspect_project(source, contract, inspector):
    try:
        files, digest, _ = normalize_files(json.loads(source), contract['editable_files'])
    except (ValueError, TypeError, UnicodeError) as error:
        return {'passed': False, 'issues': [{'rule_id': 'PROJECT_FILES', 'severity': 'error',
                    'origin': 'template_contract', 'stage': 'static', 'message': str(error),
                    'line': 1, 'column': 1}], 'coverage': ['editable file inventory'], 'limitations': []}
    issues = []
    for file in files:
        rules = dict(contract, submission_kind='kernel',
                     require_entrypoint=file['path'] == contract['kernel_file'])
        checked = inspector(file['content'], rules)
        issues.extend(dict(issue, file=file['path']) for issue in checked['issues'])
        # Full-solver restrictions follow the administrator's problem contract.
        code = re.sub(r'//[^\n]*|/\*[\s\S]*?\*/', '', file['content'])
        pattern = contract.get('forbidden_solver_pattern', r'\b(?:LAPACKE_\w+|[sd]syevd?_?|SelfAdjointEigenSolver|JacobiSVD|aclnnLinalgEigh|aclnnLinalgEighGetWorkspaceSize)\b')
        for match in re.finditer(pattern, code):
            issues.append(dict(rule_id='EXTERNAL_SOLVER' if contract.get('forbidden_solver_pattern') else 'EXTERNAL_EIGENSOLVER', severity='error', origin='official_statement',
                stage='static', file=file['path'], line=code.count('\n', 0, match.start())+1, column=1,
                message=contract.get('forbidden_solver_message','题面禁止调用完整外部特征分解实现；请在 NPU 实现算法。')))
    return dict(passed=not any(i['severity'] == 'error' for i in issues), issues=issues,
                source_sha256=digest, coverage=['all editable files', 'kernel entrypoint', 'host side effects', 'external solver names'],
                limitations=['静态规则不能完整证明 CPU 回退或恶意宏行为；服务只面向可信开发者。',
                             '本地检查实现公开题面的数值契约，不声称复制官方私有检查器。'])

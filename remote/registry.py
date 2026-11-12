"""Administrator-installed problem packs; never load code or paths from an upload."""
import hashlib
import importlib.util
import json
from pathlib import Path
import re


class Registry:
    def __init__(self, directory):
        self.directory = Path(directory).resolve()

    def path(self, identity):
        if not isinstance(identity, str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,63}', identity):
            raise ValueError('题目 ID 无效')
        path = self.directory / identity
        if path.is_symlink() or path.resolve().parent != self.directory or not (path / 'problem.json').is_file():
            raise ValueError('题目不存在：' + identity)
        return path

    def get(self, identity):
        path = self.path(identity)
        spec = json.loads((path / 'problem.json').read_text(encoding='utf-8'))
        if spec.get('id') != identity or spec.get('schema_version') != 1 or not spec.get('enabled'):
            raise ValueError('题目包无效或已停用：' + identity)
        if not isinstance(spec.get('version'), str) or not spec.get('cases') or not spec.get('contract'):
            raise ValueError('题目包缺少版本、测试集或接口契约')
        if not (path / 'adapter.py').is_file():
            raise ValueError('题目缺少服务端 adapter.py')
        hashes = {str(p.relative_to(path)).replace('\\','/'): hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in sorted(path.rglob('*'))
                  if p.is_file() and not any(part in {'__pycache__','data','build'} for part in p.relative_to(path).parts)
                  and p.suffix in {'.json','.py','.cpp','.asc','.hpp','.h','.txt','.sh','.md'}}
        spec['pack_sha256'] = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
        return spec

    def listing(self):
        specs = []
        for entry in sorted(self.directory.glob('*/problem.json')):
            metadata = json.loads(entry.read_text(encoding='utf-8'))
            if metadata.get('enabled'):
                specs.append(self.get(entry.parent.name))
        return specs

    def adapter(self, identity):
        path = self.path(identity) / 'adapter.py'
        spec = importlib.util.spec_from_file_location('oj_problem_' + identity, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for method in ('install', 'evaluate'):
            if not callable(getattr(module, method, None)):
                raise ValueError('题目 adapter 缺少 ' + method)
        return module

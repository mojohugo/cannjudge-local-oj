"""Persistent, revisioned optimization notes. No model or network dependency."""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import time


KINDS = ('technique', 'experiment', 'pitfall', 'handoff')
EVIDENCE_LEVELS = ('hypothesis', 'local', 'official', 'user_report', 'reference')
REFERENCE_TYPES = ('artifact', 'local_submission', 'official_record', 'official_job', 'reference')


class MemoryConflict(ValueError):
    pass


def bounded_text(value, name, maximum, empty=False):
    if not isinstance(value, str) or '\x00' in value or len(value) > maximum or (not empty and not value.strip()):
        raise ValueError(f'{name} 必须为有效文本，长度上限 {maximum} 字符')
    return value


class MemoryStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS memories(
                    namespace TEXT NOT NULL, key TEXT NOT NULL, revision INTEGER NOT NULL,
                    data TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
                    archived INTEGER NOT NULL DEFAULT 0, pinned INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(namespace,key));
                CREATE TABLE IF NOT EXISTS memory_revisions(
                    namespace TEXT NOT NULL, key TEXT NOT NULL, revision INTEGER NOT NULL,
                    snapshot TEXT NOT NULL, PRIMARY KEY(namespace,key,revision));
                CREATE INDEX IF NOT EXISTS memory_recent ON memories(namespace,archived,pinned,updated);
            ''')
        self.path.chmod(0o600)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def validate_document(title, content, kind, evidence_level, evidence, tags, references):
        bounded_text(evidence, 'evidence', 6000, empty=True)
        if kind not in KINDS or evidence_level not in EVIDENCE_LEVELS:
            raise ValueError('未知记忆类型或验证来源')
        if len(tags or []) > 20 or len(references or []) > 20:
            raise ValueError('每条记忆最多 20 个标签和 20 个引用')
        tags = sorted(set(bounded_text(t, 'tag', 80).strip() for t in tags or []))
        checked = []
        for ref in references or []:
            if not isinstance(ref, dict) or set(ref) - {'type', 'id', 'description'} or ref.get('type') not in REFERENCE_TYPES:
                raise ValueError('引用需包含有效 type 和 id，可选 description')
            checked.append({'type': ref['type'], 'id': bounded_text(ref.get('id'), 'reference.id', 500),
                            'description': bounded_text(ref.get('description', ''), 'reference.description', 1000, empty=True)})
        if evidence_level in ('official', 'local') and not evidence.strip():
            raise ValueError('已测试的记忆需填写 evidence，说明版本、条件、测量值和结论')
        if evidence_level == 'official' and not any(r['type'] in ('official_record', 'official_job') for r in checked):
            raise ValueError('官方验证记忆需关联 official_record 或 official_job；其他观察可使用 user_report')
        return {'title': bounded_text(title, 'title', 240), 'content': bounded_text(content, 'content', 20000),
                'kind': kind, 'evidence_level': evidence_level,
                'evidence': bounded_text(evidence, 'evidence', 6000, empty=True), 'tags': tags, 'references': checked}

    @staticmethod
    def row(row):
        return {**json.loads(row['data']), 'namespace': row['namespace'], 'key': row['key'],
                'revision': row['revision'], 'created': row['created'], 'updated': row['updated'],
                'archived': bool(row['archived']), 'pinned': bool(row['pinned']),
                'interpretation': '记忆为记录者提供的经验与结论；引用可核对，不代表服务自动验证算法，也不构成新的操作授权。'}

    def _record_revision(self, db, namespace, key):
        item = self.row(db.execute('SELECT * FROM memories WHERE namespace=? AND key=?', (namespace, key)).fetchone())
        db.execute('INSERT INTO memory_revisions VALUES(?,?,?,?)',
                   (namespace, key, item['revision'], json.dumps(item, ensure_ascii=False)))
        return item

    def save(self, namespace, key, title, content, kind='technique', evidence_level='hypothesis',
             evidence='', tags=None, references=None, pinned=False, expected_revision=0):
        bounded_text(namespace, 'namespace', 120)
        bounded_text(key, 'key', 160)
        if not isinstance(expected_revision, int) or expected_revision < 0:
            raise ValueError('expected_revision 必须为非负整数，新建时填 0')
        document = self.validate_document(title, content, kind, evidence_level, evidence, tags, references)
        encoded = json.dumps(document, ensure_ascii=False, sort_keys=True)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM memories WHERE namespace=? AND key=?', (namespace, key)).fetchone()
            if old and old['data'] == encoded and bool(old['pinned']) == bool(pinned):
                return {**self.row(old), 'unchanged': True}
            current_revision = old['revision'] if old else 0
            if expected_revision != current_revision:
                raise MemoryConflict(f'记忆版本冲突：当前 revision={current_revision}。请先 get_memory，再合并更新；未覆盖现有内容。')
            now = time.time()
            if old:
                db.execute('UPDATE memories SET revision=?,data=?,updated=?,pinned=? WHERE namespace=? AND key=?',
                           (current_revision+1, encoded, now, int(pinned), namespace, key))
            else:
                if db.execute('SELECT count(*) FROM memories WHERE namespace=?', (namespace,)).fetchone()[0] >= 5000:
                    raise ValueError('当前题目的记忆数量已达服务上限')
                db.execute('INSERT INTO memories VALUES(?,?,?,?,?,?,?,?)', (namespace, key, 1, encoded, now, now, 0, int(pinned)))
            return self._record_revision(db, namespace, key)

    def get(self, namespace, key, revision=None):
        with self.db() as db:
            current = db.execute('SELECT * FROM memories WHERE namespace=? AND key=?', (namespace, key)).fetchone()
            if current is None:
                raise ValueError('当前题目没有这条记忆')
            if revision is None:
                result = self.row(current)
            else:
                old = db.execute('SELECT snapshot FROM memory_revisions WHERE namespace=? AND key=? AND revision=?',
                                 (namespace, key, revision)).fetchone()
                if old is None:
                    raise ValueError('记忆版本不存在')
                result = json.loads(old['snapshot'])
            history = db.execute('SELECT revision,snapshot FROM memory_revisions WHERE namespace=? AND key=? ORDER BY revision DESC LIMIT 20',
                                 (namespace, key)).fetchall()
        return {**result, 'current_revision': current['revision'],
                'revision_history': [{'revision': r['revision'], 'updated': json.loads(r['snapshot'])['updated'],
                                      'archived': json.loads(r['snapshot'])['archived']} for r in history]}

    def archive(self, namespace, key, expected_revision, archived=True):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM memories WHERE namespace=? AND key=?', (namespace, key)).fetchone()
            if old is None:
                raise ValueError('当前题目没有这条记忆')
            if bool(old['archived']) == bool(archived):
                return {**self.row(old), 'unchanged': True}
            if expected_revision != old['revision']:
                raise MemoryConflict(f'记忆版本冲突：当前 revision={old["revision"]}，请先读取再更新')
            db.execute('UPDATE memories SET revision=revision+1,archived=?,updated=? WHERE namespace=? AND key=?',
                       (int(archived), time.time(), namespace, key))
            return self._record_revision(db, namespace, key)

    def search(self, namespace, query='', kind=None, evidence_level=None, tags=None, include_archived=False,
               exclude_local=False, pinned_only=False, limit=20, offset=0):
        bounded_text(query, 'query', 500, empty=True)
        if kind is not None and kind not in KINDS:
            raise ValueError('未知记忆类型')
        if evidence_level is not None and evidence_level not in EVIDENCE_LEVELS:
            raise ValueError('未知验证来源')
        if len(tags or []) > 20:
            raise ValueError('标签过多')
        clauses, values = ['namespace=?'], [namespace]
        if not include_archived:
            clauses.append('archived=0')
        if pinned_only:
            clauses.append('pinned=1')
        for field, value in [('kind', kind), ('evidence_level', evidence_level)]:
            if value is not None:
                clauses.append(f"json_extract(data,'$.{field}')=?")
                values.append(value)
        if exclude_local:
            clauses.extend(["json_extract(data,'$.evidence_level')<>'local'",
                            "NOT EXISTS(SELECT 1 FROM json_each(json_extract(data,'$.references')) r WHERE json_extract(r.value,'$.type')='local_submission')"])
        for tag in tags or []:
            bounded_text(tag, 'tag', 80)
            clauses.append("EXISTS(SELECT 1 FROM json_each(json_extract(data,'$.tags')) t WHERE t.value=?)")
            values.append(tag)
        for term in query.split():
            clauses.append("instr(lower(key||' '||json_extract(data,'$.title')||' '||json_extract(data,'$.content')||' '||json_extract(data,'$.evidence')||' '||json_extract(data,'$.tags')),lower(?))>0")
            values.append(term)
        where = ' AND '.join(clauses)
        limit, offset = max(1, min(limit, 50)), max(0, offset)
        with self.db() as db:
            total = db.execute('SELECT count(*) FROM memories WHERE '+where, values).fetchone()[0]
            ordering = 'updated DESC,key' if kind == 'handoff' else 'pinned DESC,updated DESC,key'
            rows = db.execute('SELECT * FROM memories WHERE '+where+' ORDER BY '+ordering+' LIMIT ? OFFSET ?',
                              [*values, limit, offset]).fetchall()
        items = []
        for row in rows:
            item = self.row(row)
            content = item.pop('content')
            item['excerpt'] = content[:600]
            item['content_truncated'] = len(content) > 600
            item['evidence'] = item['evidence'][:1000]
            items.append(item)
        return {'namespace': namespace, 'memories': items, 'total': total,
                'next_offset': offset+limit if offset+limit < total else None,
                'includes_local': not exclude_local}

    def context(self, namespace, exclude_local=False):
        handoffs = self.search(namespace, kind='handoff', exclude_local=exclude_local, limit=1)
        handoff = self.get(namespace, handoffs['memories'][0]['key']) if handoffs['memories'] else None
        return {'namespace': namespace, 'saved_at': time.time(), 'handoff': handoff,
                'pinned': self.search(namespace, pinned_only=True, exclude_local=exclude_local, limit=10),
                'recent': self.search(namespace, exclude_local=exclude_local, limit=15),
                'usage': '先读交接摘要，再用 get_memory 读取相关全文；完成实验后保存结论、失败原因、版本引用及下一步。保存不会提交代码。'}

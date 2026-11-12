"""Versioned owner edits to allowlisted skill documents, separate from judge data."""
import hashlib
import json
import re
import sqlite3
import time
from pathlib import Path

from . import optimization_skill as packaged


CONNECT_TIMEOUT_SECONDS = 3.0


def _enable_wal(db):
    """Finish only the WAL bootstrap within the original connection wait budget.

    Concurrent first writers can get an immediate BUSY while converting the
    journal, even with a busy timeout. No revision transaction has begun here.
    Never retry a schema statement, revision write or uncertain commit.
    """
    from .store import is_sqlite_busy
    deadline = time.monotonic() + CONNECT_TIMEOUT_SECONDS
    delay = .005
    while True:
        remaining = max(0, deadline - time.monotonic())
        db.execute(f'PRAGMA busy_timeout={int(remaining * 1000)}')
        try:
            db.execute('PRAGMA journal_mode=WAL').fetchone()
            break
        except sqlite3.OperationalError as error:
            remaining = deadline - time.monotonic()
            if not is_sqlite_busy(error) or remaining <= 0:
                raise
            time.sleep(min(delay, remaining))
            delay = min(.05, delay * 2)
    db.execute(f'PRAGMA busy_timeout={int(CONNECT_TIMEOUT_SECONDS * 1000)}')


def sha(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def document(reference):
    packaged._identifier(reference, 'document ID')
    manifest, documents, _ = packaged._manifest()
    if reference not in documents:
        raise ValueError('Unknown optimization skill reference; read section=catalog first')
    doc = documents[reference]
    try:
        content = packaged._bytes(doc['path']).decode('utf-8')
    except UnicodeError:
        raise ValueError('Skill document is not UTF-8') from None
    return manifest, doc, content


def revision_number(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError('revision must be a nonnegative integer')


def hash_value(value):
    if not isinstance(value, str) or not re.fullmatch('[a-f0-9]{64}', value):
        raise ValueError('Expected a lowercase SHA-256 from get_optimization_skill')


class SkillEdits:
    def __init__(self, directory):
        self.path = Path(directory) / 'optimization-skill.sqlite3'

    def _connect(self, write=False):
        if self.path.is_symlink() or (self.path.exists() and not self.path.is_file()):
            raise ValueError('Invalid skill revision store')
        if not write and not self.path.exists():
            return None
        if write:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=CONNECT_TIMEOUT_SECONDS)
        db.row_factory = sqlite3.Row
        try:
            if not write and not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='skill_revisions'").fetchone():
                # A first writer may have created the file but not its schema,
                # or failed during WAL admission. No revision exists yet.
                db.close()
                return None
            if write:
                self.path.chmod(0o600)
                _enable_wal(db)
                db.execute('PRAGMA synchronous=FULL')
                db.execute('''CREATE TABLE IF NOT EXISTS skill_revisions (
                    document TEXT NOT NULL, revision INTEGER NOT NULL, content TEXT NOT NULL,
                    content_sha256 TEXT NOT NULL, package_sha256 TEXT NOT NULL,
                    created REAL, summary TEXT NOT NULL, operation_key TEXT,
                    PRIMARY KEY(document, revision))''')
            return db
        except BaseException:
            db.close()
            raise

    @staticmethod
    def _base(reference, content):
        return dict(document=reference, revision=0, content=content, content_sha256=sha(content),
                    package_sha256=sha(content), created=None, summary='Packaged document', operation_key=None)

    def read(self, reference, revision=None, package_content=None):
        if revision is not None:
            revision_number(revision)
        if package_content is None:
            _, _, package_content = document(reference)
        head = row = None
        db = self._connect()
        if db is not None:
            try:
                db.execute('BEGIN')
                head = db.execute('SELECT * FROM skill_revisions WHERE document=? ORDER BY revision DESC LIMIT 1', (reference,)).fetchone()
                row = head if revision is None else db.execute('SELECT * FROM skill_revisions WHERE document=? AND revision=?', (reference, revision)).fetchone()
            finally:
                db.close()
        if row is None:
            if revision not in (None, 0) or head is not None:
                raise ValueError('Unknown skill revision')
            row = self._base(reference, package_content)
        row = dict(row)
        row.pop('operation_key', None)
        row.update(current_revision=head['revision'] if head else 0,
                   package_content_sha256=sha(package_content),
                   package_changed_since_edit=row['package_sha256'] != sha(package_content),
                   content_origin='shared_owner_edit' if row['revision'] else 'packaged_reference',
                   historical=revision is not None and head is not None and revision != head['revision'])
        return row

    def history(self, reference, offset=0, limit=20):
        _, doc, content = document(reference)
        revision_number(offset)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError('limit must be between 1 and 100')
        db = self._connect()
        if db is None:
            rows, total = [self._base(reference, content)][offset:offset+limit], 1
        else:
            try:
                db.execute('BEGIN')
                total = db.execute('SELECT count(*) FROM skill_revisions WHERE document=?', (reference,)).fetchone()[0]
                rows = [dict(r) for r in db.execute('''SELECT revision,content_sha256,package_sha256,created,summary
                    FROM skill_revisions WHERE document=? ORDER BY revision DESC LIMIT ? OFFSET ?''', (reference, limit, offset))]
                if not total:
                    rows, total = [self._base(reference, content)][offset:offset+limit], 1
            finally:
                db.close()
        return dict(reference=reference, document=packaged._document_info(doc),
                    revisions=[{k:v for k,v in r.items() if k not in {'content','operation_key','document'}} for r in rows],
                    total=total, offset=offset, next_offset=offset+len(rows) if offset+len(rows)<total else None,
                    content_tool='get_optimization_skill', content_arguments={'section':'references','reference':reference},
                    note='Revision 0 is the original packaged snapshot captured before the first edit. Restore creates a new revision.')

    def update(self, reference, content, expected_revision, expected_sha256,
               expected_package_sha256, summary):
        revision_number(expected_revision)
        hash_value(expected_sha256)
        hash_value(expected_package_sha256)
        if not isinstance(content, str) or not content.strip() or '\x00' in content:
            raise ValueError('content must be complete nonempty Markdown without NUL')
        try:
            size = len(content.encode('utf-8'))
        except UnicodeError:
            raise ValueError('content must be valid UTF-8') from None
        if size > packaged.MAX_DOCUMENT_BYTES:
            raise ValueError('Skill document exceeds 1 MiB')
        if not isinstance(summary, str) or not 1 <= len(summary.strip()) <= 500:
            raise ValueError('summary must contain 1..500 characters')
        manifest, doc, base_content = document(reference)
        if doc['path'] == 'SKILL.md':
            import yaml
            lines = content.splitlines()
            if not lines or lines[0] != '---' or '---' not in lines[1:]:
                raise ValueError('SKILL.md must preserve YAML name and description frontmatter')
            try:
                metadata = yaml.safe_load('\n'.join(lines[1:lines.index('---',1)]))
            except yaml.YAMLError:
                raise ValueError('Invalid SKILL.md frontmatter') from None
            if (not isinstance(metadata, dict) or metadata.get('name') != manifest['id'] or
                    not isinstance(metadata.get('description'), str) or not metadata['description'].strip()):
                raise ValueError('Keep the skill name and a nonempty description')
        package_sha = sha(base_content)
        if expected_package_sha256 != package_sha:
            raise ValueError('Skill package changed; read the current document and reconcile before saving')
        operation = sha(json.dumps([reference, sha(content), expected_revision, expected_sha256,
                                   expected_package_sha256, summary], ensure_ascii=False))
        db = self._connect(write=True)
        try:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM skill_revisions WHERE document=? ORDER BY revision DESC LIMIT 1', (reference,)).fetchone()
            current = dict(row) if row else self._base(reference, base_content)
            if current.get('operation_key') == operation and current['revision'] == expected_revision+1:
                return self._receipt(current, changed=False, repeated=True)
            if current['revision'] != expected_revision or current['content_sha256'] != expected_sha256:
                raise ValueError('Skill edit conflict; re-read current revision and SHA, then merge changes')
            if current['content'] == content:
                return self._receipt(current, changed=False)
            if not row:
                db.execute('INSERT INTO skill_revisions VALUES(?,?,?,?,?,?,?,?)',
                           (reference,0,base_content,package_sha,package_sha,time.time(),'Original packaged snapshot',None))
            revision = expected_revision+1
            created = time.time()
            db.execute('INSERT INTO skill_revisions VALUES(?,?,?,?,?,?,?,?)',
                       (reference,revision,content,sha(content),package_sha,created,summary.strip(),operation))
            # A concurrent packaged release must not be silently accepted as our baseline.
            if sha(document(reference)[2]) != package_sha:
                raise ValueError('Skill package changed during save; read and retry after reconciliation')
            db.commit()
            return self._receipt(dict(document=reference,revision=revision,content_sha256=sha(content),
                                      package_sha256=package_sha,created=created,summary=summary.strip()), changed=True)
        finally:
            db.close()

    @staticmethod
    def _receipt(row, changed, repeated=False):
        return {k:row[k] for k in ('document','revision','content_sha256','created','summary')} | dict(
            package_content_sha256=row['package_sha256'],changed=changed,repeated=repeated,
            effective_immediately=True,shared=True,evaluation_created=False,
            note='Shared skill reference updated. This does not validate the advice or authorize evaluations/submissions.')

    def restore(self, reference, revision, expected_revision, expected_sha256,
                expected_package_sha256, summary):
        revision_number(revision)
        target = self.read(reference, revision)
        return self.update(reference,target['content'],expected_revision,expected_sha256,
                           expected_package_sha256,summary)

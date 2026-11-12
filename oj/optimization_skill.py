"""Read the packaged optimization skill through an explicit, bounded allowlist.

This module never reads evaluation databases or submission source. Curated notes
can discuss multiple evidence types, which must not become official measurements.
"""
import hashlib
import json
from pathlib import Path, PurePosixPath
import re

from .config import ROOT


DIRECTORY = ROOT / 'skills' / 'ascend-operator-optimization'
SECTIONS = {'catalog', 'overview', 'workflow', 'profiling', 'operators', 'references'}
MAX_DOCUMENT_BYTES = 1024 * 1024
MAX_PAGE_CHARS = 24000
EVIDENCE_POLICY = (
    'Curated reference material, not a current evaluation or an authorization. '
    'Official measurements, local measurements, CPU/static checks and hypotheses '
    'retain their individual evidence labels. Local timings and synthetic data '
    'are not official results; a component test is not a merged-source test. '
    'Use official-named tools for current official records and quotas.'
)


def _identifier(value, label):
    if not isinstance(value, str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', value):
        raise ValueError('Invalid ' + label)
    return value


def _path(relative):
    if not isinstance(relative, str) or not relative or '\\' in relative or ':' in relative:
        raise ValueError('Invalid skill document path')
    parts = PurePosixPath(relative)
    if parts.is_absolute() or any(part in {'', '.', '..'} for part in relative.split('/')):
        raise ValueError('Invalid skill document path')
    root = DIRECTORY.resolve()
    candidate = DIRECTORY
    if DIRECTORY.is_symlink():
        raise ValueError('Linked skill directory is not supported')
    for part in parts.parts:
        candidate = candidate / part
        if candidate.is_symlink():
            raise ValueError('Linked skill document is not supported')
    try:
        candidate.resolve().relative_to(root)
    except (OSError, ValueError):
        raise ValueError('Skill document is outside its package') from None
    return candidate


def _bytes(relative):
    path = _path(relative)
    try:
        with path.open('rb') as handle:
            raw = handle.read(MAX_DOCUMENT_BYTES + 1)
    except OSError:
        raise ValueError('Skill document is not available') from None
    if len(raw) > MAX_DOCUMENT_BYTES:
        raise ValueError('Skill document is too large')
    return raw


def _manifest():
    raw = _bytes('manifest.json')
    try:
        manifest = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError):
        raise ValueError('Invalid optimization skill manifest') from None
    if not isinstance(manifest, dict) or manifest.get('schema_version') != 1:
        raise ValueError('Unsupported optimization skill manifest')
    _identifier(manifest.get('id'), 'skill ID')
    if not isinstance(manifest.get('version'), str) or not manifest['version']:
        raise ValueError('Missing optimization skill version')
    documents = manifest.get('documents')
    problems = manifest.get('problems')
    evidence = manifest.get('evidence_levels')
    if not isinstance(documents, list) or not documents or not isinstance(problems, dict):
        raise ValueError('Invalid optimization skill index')
    if not isinstance(evidence, (dict, list)) or not evidence:
        raise ValueError('Missing optimization skill evidence labels')
    indexed = {}
    for doc in documents:
        if not isinstance(doc, dict):
            raise ValueError('Invalid skill document entry')
        identity = _identifier(doc.get('id'), 'document ID')
        if identity in indexed or doc.get('section') not in SECTIONS - {'catalog', 'references'}:
            raise ValueError('Duplicate document ID or invalid section')
        if not isinstance(doc.get('title'), str) or not doc['title']:
            raise ValueError('Missing skill document title')
        path = _path(doc.get('path'))
        if path.suffix != '.md':
            raise ValueError('Only Markdown skill documents are readable')
        ids = doc.get('problem_ids', [])
        if not isinstance(ids, list):
            raise ValueError('Invalid skill problem list')
        for problem_id in ids:
            _identifier(problem_id, 'problem ID')
        levels = doc.get('evidence_levels', [])
        if not isinstance(levels, list) or any(level not in evidence for level in levels):
            raise ValueError('Unknown skill evidence label')
        indexed[identity] = doc
    for identity, entry in problems.items():
        _identifier(identity, 'problem ID')
        if not isinstance(entry, dict) or entry.get('reference') not in indexed:
            raise ValueError('Invalid skill problem reference')
        if identity not in indexed[entry['reference']].get('problem_ids', []):
            raise ValueError('Skill reference does not cover its problem')
    return manifest, indexed, hashlib.sha256(raw).hexdigest()


def _metadata(manifest, digest):
    return dict(skill_id=manifest['id'], version=manifest['version'],
                updated=manifest.get('updated'), manifest_sha256=digest,
                data_origin='curated_skill_reference', evidence_levels=manifest['evidence_levels'],
                evidence_policy=EVIDENCE_POLICY, evaluation_records_included=False,
                contains_local_evidence=True, historical_summary=True,
                tool='get_optimization_skill')


def _document_info(doc):
    return {name: doc.get(name, [] if name.endswith('s') else None)
            for name in ('id', 'title', 'section', 'path', 'problem_ids', 'evidence_levels')}


def read_skill(section='overview', problem_id=None, reference=None, offset=0, limit=12000,
               *, state_dir=None, revision=None, view='current'):
    """Read a catalog or one document; offsets count Unicode characters."""
    if section not in SECTIONS:
        raise ValueError('Unknown optimization skill section')
    if view not in {'current','packaged'} or view == 'packaged' and revision is not None:
        raise ValueError('view=packaged reads the current package, without a revision selector')
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError('offset must be a nonnegative integer')
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_PAGE_CHARS:
        raise ValueError('limit must be between 1 and ' + str(MAX_PAGE_CHARS))
    if problem_id is not None:
        _identifier(problem_id, 'problem ID')
    if reference is not None:
        _identifier(reference, 'document ID')
    manifest, documents, digest = _manifest()
    result = _metadata(manifest, digest)
    result['view'] = view
    from .skill_edits import SkillEdits, revision_number
    if revision is not None:
        revision_number(revision)
        if section == 'catalog' or section == 'operators' and problem_id is None and reference is None:
            raise ValueError('Select a document before selecting a revision')
    edits = SkillEdits(state_dir) if state_dir is not None else None
    result['editing'] = dict(available=edits is not None, shared=True,
        update_tool='update_optimization_skill', history_tool='get_optimization_skill_history',
        restore_tool='restore_optimization_skill',
        note='Read all pages before replacing complete Markdown. Edits persist across sessions; package version and edit revision are separate.')
    if problem_id is not None and problem_id not in manifest['problems']:
        raise ValueError('No optimization skill coverage for this problem')
    selected = manifest['problems'].get(problem_id)
    if selected is not None:
        result.update(problem_id=problem_id, problem_coverage=selected)
    if section == 'catalog' or section == 'operators' and problem_id is None and reference is None:
        if reference is not None:
            raise ValueError('catalog does not select a document')
        result.update(section=section, documents=[_document_info(doc) for doc in documents.values()
            if (section != 'operators' or doc['section'] == 'operators') and
               (problem_id is None or not doc.get('problem_ids') or problem_id in doc['problem_ids'])],
            problems=manifest['problems'] if problem_id is None else {problem_id: selected})
        if edits and view == 'current':
            for entry in result['documents']:
                saved = edits.read(entry['id'])
                entry.update({k:saved[k] for k in ('revision','content_sha256','package_content_sha256','package_changed_since_edit','content_origin')})
        return result
    if reference is not None:
        if section != 'references':
            raise ValueError('Use section=references when selecting a reference ID')
        doc = documents.get(reference)
        if doc is None:
            raise ValueError('Unknown optimization skill reference')
        if problem_id is not None and doc.get('problem_ids') and problem_id not in doc['problem_ids']:
            raise ValueError('Skill reference does not cover the requested problem')
    elif problem_id is not None and section in {'overview', 'operators', 'references'}:
        doc = documents[selected['reference']]
    else:
        matches = [doc for doc in documents.values() if doc['section'] == section]
        if len(matches) != 1:
            raise ValueError('Select a document ID from section=catalog')
        doc = matches[0]
    raw = _bytes(doc['path'])
    try:
        content = raw.decode('utf-8')
    except UnicodeError:
        raise ValueError('Skill document is not UTF-8') from None
    if edits and view == 'current':
        saved = edits.read(doc['id'], revision, package_content=content)
        content = saved['content']
        raw = content.encode('utf-8')
        result.update({k:saved[k] for k in ('revision','current_revision','package_content_sha256',
                      'package_changed_since_edit','content_origin','created','summary','historical')})
    else:
        if revision not in (None,0):
            raise ValueError('Skill revision storage is unavailable')
        result.update(revision=0, current_revision=0, package_content_sha256=hashlib.sha256(raw).hexdigest(),
                      package_changed_since_edit=False,content_origin='packaged_reference',historical=False)
        if view == 'packaged':
            result.update(revision=None,
                          current_revision=edits.read(doc['id'])['current_revision'] if edits else 0)
    if offset > len(content):
        raise ValueError('offset is past the end of the skill document')
    end = min(offset + limit, len(content))
    result.update(section=doc['section'], document=_document_info(doc),
                  content_sha256=hashlib.sha256(raw).hexdigest(), content=content[offset:end],
                  offset=offset, limit=limit, total_chars=len(content),
                  next_offset=end if end < len(content) else None)
    return result


def skill_hint(problem_id=None):
    """Keep old guide/catalog calls usable when the optional package is absent."""
    try:
        manifest, _, digest = _manifest()
    except ValueError:
        return dict(available=False, tool='get_optimization_skill')
    if problem_id is not None and problem_id not in manifest['problems']:
        return dict(available=False, tool='get_optimization_skill',
                    reason='No operator-specific skill coverage; the general overview remains available')
    result = dict(available=True, tool='get_optimization_skill', skill_id=manifest['id'],
                  version=manifest['version'], manifest_sha256=digest,
                  catalog_arguments={'section': 'catalog'},
                  edit_tool='update_optimization_skill', history_tool='get_optimization_skill_history',
                  restore_tool='restore_optimization_skill')
    if problem_id in manifest['problems']:
        result.update(problem_coverage=manifest['problems'][problem_id],
                      arguments={'problem_id': problem_id})
    else:
        result['arguments'] = {'section': 'overview'}
    return result

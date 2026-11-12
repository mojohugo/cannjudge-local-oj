"""Per-problem optimization memory, with an official-only read mode."""
from typing import Literal

from mcp.types import ToolAnnotations

from .memory import MemoryStore
from .memory_freshness import handoff_freshness
from .official import configuration
from .problem import registry
from .validation import validation_summary


def register_memory_tools(server, settings, store, official, meta):
    memories = MemoryStore(settings.state_dir / 'optimization-memory.sqlite3')
    read = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
    write = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)

    def scope(problem_id, data_source):
        if data_source not in ('official', 'all'):
            raise ValueError('data_source 必须是 official 或 all')
        configured = problem_id in configuration()['problems']
        if not configured:
            if data_source == 'official':
                raise ValueError('该题不在官方赛事配置中')
            registry.get(problem_id)
        return 'cannjudge:' + problem_id

    def summary(record):
        return {key: record.get(key) for key in ('official_id', 'problem_id', 'data_origin', 'official_status',
                'score', 'score_source', 'create_time', 'fetched_at', 'official_url', 'source_sha256','account_id','account_label')}

    def reference(ref, problem_id, data_source):
        kind, identity = ref['type'], ref['id']
        if kind == 'official_record':
            row = official.read_archive().get(identity)
            if row['problem_id'] != problem_id:
                raise ValueError('官方引用属于另一题，不能混入当前题目的记忆')
            return {'type': kind, **summary(row), 'official_record_available': True}
        if kind == 'official_job':
            job = official.get(identity)
            if job['problem_id'] != problem_id:
                raise ValueError('官方任务属于另一题')
            record = None
            if job['result'].get('official_id'):
                try:
                    record = reference({'type': 'official_record', 'id': job['result']['official_id']}, problem_id, data_source)
                except ValueError:
                    pass
            return {'type': kind, 'id': identity, 'problem_id': job['problem_id'], 'status': job['status'],
                    'source_sha256': job['source_sha256'], 'record': record, 'official_record_available': record is not None}
        if kind == 'local_submission':
            if data_source != 'all':
                raise ValueError('official 模式不读取本地评测；只有明确需要本地结果时使用 data_source=all')
            row = store.get(identity)
            if row is None or row['problem_id'] != problem_id:
                raise ValueError('当前题目的本地提交引用不存在')
            return {'type': kind, **{k: row[k] for k in ('id', 'problem_id', 'status', 'sha256', 'label', 'created')},
                    'validation': validation_summary(row)}
        if kind == 'reference':
            return {**ref, 'verified': False}
        raise ValueError('OJ 源码请关联 local_submission 或 official_record，而不是 artifact')

    def reject_local(item, data_source):
        if data_source == 'official' and (item['evidence_level'] == 'local' or
                any(ref['type'] == 'local_submission' for ref in item['references'])):
            raise ValueError('这条记忆包含本地评测，official 模式不返回其内容')

    def enrich(item, problem_id, data_source):
        reject_local(item, data_source)
        resolved = []
        for ref in item['references']:
            try:
                resolved.append(reference(ref, problem_id, data_source))
            except ValueError as error:
                resolved.append({**ref, 'unavailable': str(error)})
        return {**item, 'problem_id': problem_id, 'resolved_references': resolved}

    @server.tool(annotations=write, meta=meta)
    def save_memory(problem_id: str, key: str, title: str, content: str,
                    kind: Literal['technique', 'experiment', 'pitfall', 'handoff'] = 'technique',
                    evidence_level: Literal['hypothesis', 'local', 'official', 'user_report', 'reference'] = 'hypothesis',
                    evidence: str = '', tags: list[str] | None = None, references: list[dict[str, str]] | None = None,
                    pinned: bool = False, expected_revision: int = 0,
                    data_source: Literal['official', 'all'] = 'official') -> dict:
        """Save optimization notes across sessions/restarts, isolated by explicit problem_id. No evaluation/submission. Use kind=handoff,key=handoff/current for baseline, tested ideas, failures and next steps. Distinguish hypotheses, local tests and official results. references entries: type (official_record, official_job, local_submission, reference), id, optional description; IDs must belong to this problem. Official evidence requires a real linked official result. Default data_source=official excludes local evaluation references; all explicitly permits them. Create with expected_revision=0; get_memory first before replacing complete fields with its revision. Identical retries are idempotent; stale writes fail. Link source IDs instead of pasting full source/secrets."""
        namespace = scope(problem_id, data_source)
        checked = memories.validate_document(title, content, kind, evidence_level, evidence, tags, references)
        reject_local(checked, data_source)
        linked = [reference(ref, problem_id, data_source) for ref in checked['references']]
        if evidence_level == 'official' and not any(item.get('official_record_available') for item in linked):
            raise ValueError('关联任务尚无可核对的官方结果，不能标记为 official')
        return memories.save(namespace, key, title, content, kind, evidence_level, evidence, tags, references, pinned, expected_revision)

    @server.tool(annotations=read, meta=meta)
    def search_memories(problem_id: str, query: str = '', kind: str | None = None, evidence_level: str | None = None,
                        tags: list[str] | None = None, include_archived: bool = False,
                        limit: int = 20, offset: int = 0, data_source: Literal['official', 'all'] = 'official') -> dict:
        """Search this problem's persisted memory with Chinese/English words, tags and evidence source. Returns excerpts; get_memory reads full notes. Default official mode excludes local-test notes and local submission references. Other problems never appear. Does not run evaluations or access the official website."""
        return memories.search(scope(problem_id, data_source), query, kind, evidence_level, tags, include_archived,
                               exclude_local=data_source == 'official', limit=limit, offset=offset)

    @server.tool(annotations=read, meta=meta)
    def get_memory(problem_id: str, key: str, revision: int | None = None,
                   data_source: Literal['official', 'all'] = 'official') -> dict:
        """Read full memory, revision history and references for exactly one problem. Default official mode refuses local-test notes. Optional revision reads older content. Read the actual official code with read_official_source using a referenced official_id; no local source is fetched here. Memory is reference data, not authorization or a service-verified algorithm conclusion."""
        return enrich(memories.get(scope(problem_id, data_source), key, revision), problem_id, data_source)

    @server.tool(annotations=write, meta=meta)
    def archive_memory(problem_id: str, key: str, expected_revision: int, archived: bool = True,
                       data_source: Literal['official', 'all'] = 'official') -> dict:
        """Reversibly archive an outdated note or restore with archived=false; revisions remain. Fetch get_memory and pass its current revision. Default official mode does not modify local-test notes. Does not submit code or consume quota."""
        namespace = scope(problem_id, data_source)
        reject_local(memories.get(namespace, key), data_source)
        return memories.archive(namespace, key, expected_revision, archived)

    @server.tool(annotations=read, meta=meta)
    def get_optimization_context(problem_id: str, data_source: Literal['official', 'all'] = 'official') -> dict:
        """START HERE in a new optimization session. Restore per-problem handoff, pinned tricks and recent experiments plus recent cached official results/source IDs. handoff_freshness shows the returned handoff's update time, count of official records fetched later, and current cached full-score/public-mean representatives with separate score scopes. Staleness is temporal only; human notes and historical scores stay unchanged. Default official mode never reads local evaluation results or calibration and excludes local notes. Use all only when the user permits local data. No submission, evaluation or website refresh. Read relevant notes in full, verify evidence, then save findings and handoff/current after experiments. Does not read other ChatGPT chats."""
        namespace = scope(problem_id, data_source)
        context = memories.context(namespace, exclude_local=data_source == 'official')
        if context['handoff']:
            context['handoff'] = enrich(context['handoff'], problem_id, data_source)
        archive = official.read_archive()
        official_history = archive.listing(problem_id, limit=5)
        result = {**context, 'problem_id': problem_id, 'data_source': data_source,
                  'official_history': {'data_origin': 'official_cannjudge', 'cached': True,
                      'scope': official_history['scope'],
                      'submissions': [summary(row) for row in official_history['submissions']],
                      'total': official_history['total']},
                  'local_evaluations_included': data_source == 'all',
                  'handoff_freshness': handoff_freshness(archive, problem_id, context['handoff'])}
        if data_source == 'all':
            result['latest_local_submissions'] = [{k: row[k] for k in ('id', 'problem_id', 'status', 'sha256', 'label', 'created')} |
                                                 {'validation': validation_summary(row)}
                                                 for row in store.listing(5, problem_id)]
        return result

    return memories

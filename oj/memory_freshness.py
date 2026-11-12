"""Derived, read-only handoff timing and official score provenance."""
from datetime import datetime, timezone


def handoff_freshness(archive, problem_id, handoff):
    updated = handoff['updated'] if handoff else None
    cache = archive.cache_updates_since(problem_id, updated)
    board = archive.bests(problem_id)
    newer = cache['records_updated_after_handoff']
    status = ('no_handoff' if handoff is None else 'newer_official_cache' if newer else
              'unknown' if cache['records_with_unknown_timestamp'] else 'no_newer_official_cache')

    def identity(row):
        return {k: row.get(k) for k in ('official_id', 'official_display_id', 'account_id',
                'account_label', 'source_sha256', 'official_status')}

    full = board['best_official_submission']
    if full:
        total = full['current_official_total']
        full = dict(**identity(full), score=total['score'], score_scope='official_full_total',
                    score_source=total['source'], snapshot_id=total['snapshot_id'],
                    verified_at=total['verified_at'], tbest_comparison=total['tbest_comparison'],
                    note='最新已缓存共同榜单响应中核实的完整分，含隐藏点；不是原归档分。')
    public = board['best_public_submission']
    if public:
        group = public['score_breakdown']['visible']
        public = dict(**identity(public), score=group['mean_score'], score_scope='public_case_mean',
                      score_source='official_formula_from_official_timings',
                      total_cases=group['total_cases'], reference=board['public_reference'],
                      note='本份代码全部公开点按统一最新缓存官方 TBest 复算的均分；不含隐藏点，不是整题官方分，也不是逐点拼接投影。')
    return dict(data_origin='official_cannjudge', cached=True, scope=board['scope'],
                handoff_key=handoff['key'] if handoff else None,
                handoff_revision=handoff['revision'] if handoff else None,
                handoff_updated_at=datetime.fromtimestamp(updated, timezone.utc).isoformat() if updated is not None else None,
                status=status, stale_temporally=None if status in ('no_handoff', 'unknown') else bool(newer),
                official_cache=cache, best_official_submission=full, best_public_submission=public,
                detail_tool='get_official_case_bests',
                note='过期仅针对时间：缓存抓取晚于所返回交接，不判断人工结论错误或历史成绩失效。'
                     '无更新也不证明结论正确；缺少可比证据时代表为 null。当前仅指最新缓存，读取不刷新官网，不改写任何记忆、记录或分数。')

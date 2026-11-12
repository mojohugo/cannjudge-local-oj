"""Read-only score provenance. Age and equal visible TBest do not prove comparability."""
import copy
from datetime import datetime

from .official_scoring import breakdown, snapshot_matches, valid_score


def _references(cases):
    return {str(c.get('_id', c.get('id'))):
            (bool(c.get('hidden')), None if c.get('hidden') else c.get('tbest')) for c in cases}


def comparison_page(info):
    """Only the first response shares the problem's public TBest observation.

    Other pages are not assumed simultaneous, even if visible TBest is equal:
    hidden references may have changed between requests. Legacy archives have
    no response identity and deliberately remain unverified.
    """
    snapshot = info.get('ranking_snapshot')
    if not isinstance(snapshot, dict) or snapshot.get('schema_version') != 1:
        return None
    if (snapshot.get('official_problem_id') != info.get('official_id') or
            any(snapshot.get(k) != info.get(k) for k in
                ('score_mode', 'use_baseline', 'code_template', 'ranking_submission_mode'))):
        return None
    pages = snapshot.get('pages')
    if not isinstance(pages, list) or not pages or not isinstance(pages[0], dict):
        return None
    page = pages[0]
    if (page.get('page') != 1 or page.get('source') != 'official_ranking_api' or
            not page.get('snapshot_id') or page['snapshot_id'] != info.get('reference_snapshot_id') or
            page.get('fetched_at') != info.get('reference_fetched_at')):
        return None
    try:
        if datetime.fromisoformat(page['fetched_at'].replace('Z', '+00:00')).tzinfo is None:
            return None
        references = _references(page['testcases'])
        rows = page['rows']
        if (not references or len(references) != len(page['testcases']) or
                references != _references(info.get('testcases', [])) or
                len(references) != len(info.get('testcases', [])) or
                not isinstance(rows, list) or len({r['official_id'] for r in rows}) != len(rows)):
            return None
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
    return page


def public_reference(info):
    page = comparison_page(info)
    return dict(snapshot_id=page['snapshot_id'] if page else None,
                fetched_at=page['fetched_at'] if page else info.get('fetched_at'),
                source='official_ranking_api',
                note='最新已同步公开 TBest；读取不会触发官网刷新，抓取时间不是本次读取时间。')


def reference_relation(total, reference):
    identity = total.get('snapshot_id')
    common = bool(total.get('available') and identity and identity == reference.get('snapshot_id'))
    return dict(status='unavailable' if not total.get('available') else
                'same_snapshot' if common else 'different_snapshot' if identity and reference.get('snapshot_id') else 'unverified',
                same_snapshot=common, total_snapshot_id=identity,
                reference_snapshot_id=reference.get('snapshot_id'), reference_fetched_at=reference.get('fetched_at'),
                note='完整分与公开 TBest 来自同一 ranking 响应。' if common else
                     '完整分与公开 TBest 不共时或缺少共同快照证据，不能混算；公开 TBest 相等也不能证明隐藏点引用相同。')


def annotate_totals(record, info, known_ids):
    """Keep the archived truth; expose current observations separately.

    No network, credential check, database write, wall-clock expiry or inferred
    verification timestamp is allowed on this read path.
    """
    saved = record.get('official_score_snapshot')
    fallback = record.get('official_server_score') is None and snapshot_matches(record, saved)
    score = saved['score'] if fallback else record.get('official_server_score')
    available = valid_score(score)
    total = dict(available=available, score=score if available else None,
                 source='official_ranking_snapshot' if fallback else record.get('score_source'),
                 fetched_at=saved.get('fetched_at') if fallback else record.get('fetched_at'),
                 snapshot_id=saved.get('snapshot_id') if fallback else record.get('score_snapshot_id'),
                 historical=available, comparable=False, verified_at=None,
                 reason_code=None if available else 'official_total_not_returned')
    total['original_snapshot_id'] = total['snapshot_id']
    current = dict(available=False, score=None, source='official_ranking_api', historical=False,
                   comparable=False, snapshot_id=None, fetched_at=None, verified_at=None,
                   reason_code='ranking_snapshot_unavailable')
    page = comparison_page(info)
    split = None
    if page:
        current.update(snapshot_id=page['snapshot_id'], fetched_at=page['fetched_at'])
        row = next((r for r in page['rows'] if r['official_id'] == record.get('official_id')), None)
        current['reason_code'] = 'not_in_comparison_snapshot'
        if row is not None:
            current['observed_score'] = row.get('score')
            candidate = dict(official_id=row['official_id'], official_problem_id=info['official_id'],
                             source='official_ranking_api', score=row.get('score'),
                             official_status=row.get('official_status'), cases=row.get('cases', []))
            current['reason_code'] = 'ranking_record_mismatch'
            try:
                matches = (record.get('official_problem_id') == info['official_id'] and snapshot_matches(record, candidate)
                           and {c['testcase_id'] for c in candidate['cases']} == set(_references(page['testcases'])))
            except (KeyError, TypeError, ValueError):
                matches = False
            if matches:
                current.update(available=True, comparable=True, score=row['score'],
                               verified_at=page['fetched_at'], reason_code=None,
                               note='此完整分在最新已同步的同一 ranking 响应中核实；原归档分与抓取时间另行保留。')
                if available and score == row['score']:
                    total.update(historical=False, comparable=True, verified_at=page['fetched_at'],
                                 snapshot_id=page['snapshot_id'])
                refs = _references(page['testcases'])
                cases = [dict(c, tbest_us=refs[c['testcase_id']][1]) for c in candidate['cases']]
                split = breakdown(info, cases, known_ids, official_total=row['score'], official_status=row['official_status'])
                split.update(reference_kind='verified_ranking_snapshot', snapshot_id=page['snapshot_id'],
                             fetched_at=page['fetched_at'])
    total['comparability_reason'] = ('verified_in_common_snapshot' if total['comparable'] else
        'score_changed' if current['available'] and available else current['reason_code'] or 'official_total_not_returned')
    total['note'] = ('原归档完整分已在共同榜单快照重新核实；保留原 fetched_at，verified_at 为真实 API 抓取时间。'
                     if total['comparable'] else
                     '历史或未重新验证的代表分；保留原值、来源和抓取时间，不参与当前完整分排名。'
                     if available else '本条未取得官方完整总分；不能由公开点补出隐藏分。')
    if current['available'] and available and score != current['score']:
        total['note'] += ' 本次共同快照返回的完整分为 '+str(current['score'])+'，见 current_official_total。'
    reference = public_reference(info)
    for value in (total, current):
        value['tbest_comparison'] = reference_relation(value, reference)
    record.update(official_total=total, current_official_total=current,
                  current_official_score_breakdown=copy.deepcopy(split))
    return record

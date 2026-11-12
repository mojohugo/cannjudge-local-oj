"""Official timing scores, partitioned by confirmed input specifications."""
import math


def supported(info):
    return (info.get('code_template') in ('npu_kernel_dev', 'custom_template')
            and not info.get('use_baseline') and info.get('score_mode') in (0, 1))


def positive(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0


def valid_score(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and 0 <= value <= 100


def snapshot_matches(record, snapshot):
    def results(cases):
        return {c['testcase_id']: (c.get('status'), c.get('time_us')) for c in cases}
    return (isinstance(snapshot, dict) and snapshot.get('official_id') == record.get('official_id')
            and snapshot.get('official_problem_id') == record.get('official_problem_id')
            and snapshot.get('source') == 'official_ranking_api' and valid_score(snapshot.get('score'))
            and snapshot.get('official_status') == record.get('official_status')
            and record.get('valid') is not False and record.get('evaluation_complete') is True
            and bool(snapshot.get('cases'))
            and len(results(snapshot['cases'])) == len(snapshot['cases']) == len(record.get('cases', []))
            and results(snapshot['cases']) == results(record['cases']))


def official_snapshot_breakdown(info, record, known_ids):
    snapshot = record.get('official_score_snapshot')
    if not snapshot_matches(record, snapshot):
        return None
    if (any(snapshot.get(k) != info.get(k) for k in ('score_mode', 'use_baseline', 'code_template'))
            or {c['testcase_id'] for c in snapshot['cases']} != {str(c.get('_id', c.get('id'))) for c in info.get('testcases', [])}):
        return None
    result = breakdown(info, snapshot['cases'], known_ids, official_total=snapshot['score'],
                       official_status=snapshot['official_status'])
    result.update(reference_kind='official_ranking_snapshot', fetched_at=snapshot['fetched_at'])
    return result


def breakdown(info, cases, known_ids, *, references=None, valid=True, complete=True,
              official_total=None, official_status=None):
    inventory = info.get('testcases', [])
    ids = [str(c.get('_id', c.get('id'))) for c in inventory]
    by_id = {c['testcase_id']: c for c in cases}
    hidden = {str(c.get('_id', c.get('id'))) for c in inventory if c.get('hidden')}
    usable = (bool(ids) and len(set(ids)) == len(ids) and len(by_id) == len(cases)
              and set(by_id).issubset(ids) and valid is not False and complete is not False and supported(info))
    pending = {'hidden', 'pending', 'running', 'judging', 'notrun', 'queued', 'compiling', 'unknown', ''}
    points = []
    for index, key in enumerate(ids, 1):
        case = by_id.get(key, {})
        status = str(case.get('status', '')).lower()
        invisible = key in hidden or status == 'hidden'
        score = None
        passed = status in ('pass', 'accepted')
        if usable and not invisible and case and status not in pending:
            if not passed:
                score = 0.
            else:
                timing = case.get('time_us')
                reference = references.get(key) if references is not None else case.get('tbest_us')
                if positive(timing) and positive(reference):
                    score = 100 / (1 + max(0., math.log(timing) - math.log(reference)) / math.log(1.5))
        points.append(dict(index=index, known=key in known_ids, hidden=invisible, score=score, passed=passed))

    def group(items, label):
        measured = [c for c in items if c['score'] is not None]
        available = bool(items) and len(measured) == len(items)
        total = math.fsum(c['score'] for c in measured) if available else None
        return dict(label=label, available=available, total_cases=len(items), scored_cases=len(measured),
                    hidden_cases=sum(c['hidden'] for c in items), score_sum=total, sum_max=100 * len(items),
                    mean_score=total / len(items) if available else None, max_score=100,
                    contribution=total / len(ids) if available and info.get('score_mode') == 0 else None,
                    contribution_max=100 * len(items) / len(ids) if ids and info.get('score_mode') == 0 else None,
                    unavailable_case_indices=[c['index'] for c in items if c['score'] is None],
                    reason=None if available else ('无该类测试点' if not items else
                        '含 Hidden、缺失结果/耗时/TBest，或记录无效、未完成、规则未确认；不按零分补齐'))

    known = group([c for c in points if c['known']], '已知点')
    unknown = group([c for c in points if not c['known']], '未知点')
    hidden_group = group([c for c in points if c['hidden']], '隐藏点')
    visible_group = group([c for c in points if not c['hidden']], '公开点')
    all_points = group(points, '全部测试点')
    total = dict(available=all_points['available'], score=all_points['mean_score'], max_score=100,
                 total_cases=len(ids), scored_cases=all_points['scored_cases'],
                 passed_cases=sum(c['passed'] and c['score'] is not None for c in points),
                 reason=all_points['reason'])
    if total['available'] and info.get('score_mode') == 1 and total['passed_cases'] != len(ids):
        total['score'] = 0.
    if usable and valid_score(official_total):
        total.update(available=True, score=official_total, source='official_ranking_api', official=True, reason=None)
        # All-pass scoring is invertible only when the overall verdict confirms Pass.
        additive = info.get('score_mode') == 0 or str(official_status).lower() in ('pass', 'accepted')
        if additive:
            for missing, measured in ((unknown, known), (known, unknown), (hidden_group, visible_group)):
                if missing['available'] or not missing['total_cases'] or not measured['available']:
                    continue
                value = official_total * len(ids) - measured['score_sum']
                rounding = .005 * len(ids)
                if value < -rounding or value > missing['sum_max'] + rounding:
                    missing['reason'] = '总分与公开点不一致，无法可靠反算；保留官方总分'
                    continue
                value = min(missing['sum_max'], max(0., value))
                missing.update(available=True, aggregate_only=True, approximate=True,
                               source='official_total_minus_visible_sum', reason=None,
                               score_sum=value, mean_score=value / missing['total_cases'],
                               contribution=value / len(ids), contribution_max=missing['sum_max'] / len(ids),
                               rounding_sum_error=rounding, rounding_mean_error=rounding / missing['total_cases'])
                measured.update(contribution=measured['score_sum'] / len(ids),
                                contribution_max=measured['sum_max'] / len(ids))
    return dict(classification='confirmed_input_specifications',
                reference_kind='latest_official_tbest' if references is not None else 'archived_official_tbest',
                formula='100/(1+max(0,log(time/TBest))/log(1.5))', score_mode=info.get('score_mode'),
                known=known, unknown=unknown, hidden=hidden_group, visible=visible_group, total=total,
                note='已知/未知按已确认输入规格分组，不等同于通过/失败。小计为组内单点分之和，均分为小计/组内点数；'
                     '部分计分模式下整题贡献=小计/全部点数。Hidden 或缺失结果不计零分；'
                     '未知规格但公开耗时的点仍可计分。同一官方榜单快照下，可用总分×总点数−公开组小计反算另一组的合计与均分；'
                     '反算值为近似值，总分保留两位小数，公开耗时也可能经过舍入。不能据此还原隐藏单点分数或耗时。')

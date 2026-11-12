"""Whole-history top three and per-case best code using local TBest."""
import copy
import json
import threading

from .calibration import protocol, positive, PROTOCOL_KEYS
from .problem import problem
from .scoring import profile_times
from .contest_scoring import scoring_rules, measured_times, formula_score, score_breakdown, synthetic_suite, RULES_PATH
from .validation import dataset_validation, protocol_environment, reference_passes


def current_dataset(result, spec):
    if not synthetic_suite(spec):
        return True
    environment, data = protocol_environment(result, spec), result.get('dataset') or {}
    return (all(environment.get(k) is not None for k in PROTOCOL_KEYS)
            and environment.get('problem_sha256') == spec['pack_sha256']
            and bool(data.get('sha256'))
            and data.get('version') == spec.get('dataset_revision')
            and data.get('profile_sha256') == spec.get('dataset_profile_sha256'))


def ranking_protocol(result, spec):
    value = protocol(result, spec)
    if synthetic_suite(spec):
        value.update(dataset_version=result.get('dataset', {}).get('version'),
                     dataset_profile_sha256=result.get('dataset', {}).get('profile_sha256'))
    return value


class Leaderboards:
    def __init__(self, directory, store):
        self.directory, self.store = directory, store
        self.cache = {}
        self.lock = threading.Lock()

    def completed_reference(self, identity, spec):
        with self.store.db() as db:
            rows = db.execute("""SELECT id,label,result FROM submissions WHERE problem_id=?
                AND status IN ('accepted','wrong_answer') AND json_extract(result,'$.status')=status
                ORDER BY created,id""", (identity,))
            for row in rows:
                result = json.loads(row['result'])
                if result.get('suite') != 'official_shapes' or not current_dataset(result, spec):
                    continue
                cases = result.get('cases')
                times = measured_times(cases, {}, len(spec['cases']), 'event')
                if times is None or not any(positive(t) for t in times):
                    continue
                if result['status'] == 'accepted' and not all(c['status']=='accepted' for c in cases):
                    continue
                if reference_passes(cases, spec):
                    return dict(submission_id=row['id'], label=row['label'], result=result)

    def get(self, identity):
        spec = problem(identity)
        kind = spec['contract'].get('submission_kind', 'kernel')
        source_info = dict(submission_kind=kind, source_tool='read_project_file' if kind == 'project' else 'read_kernel',
                           source_files=spec['contract'].get('editable_files', ['kernel.asc']),
                           download_filename='project.json' if kind == 'project' else 'kernel.asc')
        path = self.directory/'baselines'/identity/'current.json'
        base = dict(problem_id=identity, entries=[], case_bests=[], available=False, case_count=len(spec['cases']), validation=spec.get('validation',{}), **source_info)
        raw = path.read_bytes() if path.exists() else b''
        baseline = json.loads(raw) if raw else None
        reference = baseline
        if synthetic_suite(spec) and (not reference or not current_dataset(reference['result'], spec)):
            baseline = None
            reference = self.completed_reference(identity, spec)
            raw = json.dumps(reference, sort_keys=True).encode() if reference else b''
        if not reference:
            return dict(base, reason='尚无完整测完且包含通过点的当前版本记录' if synthetic_suite(spec)
                        else '尚未建立本题默认评测协议，完成基线评测后显示排名')
        control = reference['result']
        dataset = dataset_validation(control, spec)
        if dataset is not None and not dataset['current']:
            return dict(base, reason='当前本地数据版本尚未建立匹配的评测协议')
        if ((baseline and baseline.get('problem_id') != identity)
                or protocol_environment(control, spec).get('problem_sha256') != spec['pack_sha256']):
            return dict(base, reason='当前题目版本尚未建立匹配的评测协议')
        count = len(spec['cases'])
        method = 'device' if profile_times(control,count) is not None else 'event'
        if method=='event' and measured_times(control.get('cases'),{},count,'event') is None:
            return dict(base, reason='缺少完整有效的参考计时')
        with self.lock, self.store.db() as db:
            db.execute('BEGIN')
            signature = tuple(tuple(r) for r in db.execute("""SELECT id,updated FROM submissions
                WHERE problem_id=? AND status IN ('accepted','wrong_answer') ORDER BY id""", (identity,)))
            token = (spec['pack_sha256'], spec.get('dataset_revision'), spec.get('dataset_profile_sha256'),
                     raw, signature, RULES_PATH.stat().st_mtime_ns)
            cached = self.cache.get(identity)
            if cached and cached[0] == token:
                return copy.deepcopy(cached[1])
            rules = scoring_rules(identity)
            if not rules['available']:
                return dict(base,reason=rules['reason'])
            rows = db.execute('''SELECT id,label,created,sha256,status,
              json_extract(result,'$.suite') AS suite,
              json_extract(result,'$.environment') AS environment,
              json_extract(result,'$.problem_sha256') AS result_pack_sha256,
              json_extract(result,'$.dataset.sha256') AS data_sha256,
              json_extract(result,'$.dataset.version') AS data_version,
              json_extract(result,'$.dataset.profile_sha256') AS data_profile_sha256,
              json_extract(result,'$.cases') AS cases,
              json_extract(result,'$.metrics.PipeUtilization.status') AS profile_status,
              (SELECT json_group_array(json_object('index',json_extract(value,'$.index'),
                 'op_time_us',CASE WHEN json_type(value,'$.op_time_us') IN ('real','integer')
                   THEN json_extract(value,'$.op_time_us') ELSE NULL END))
               FROM json_each(submissions.result,'$.metrics.PipeUtilization.cases')) AS device_cases
              FROM submissions WHERE problem_id=? AND status IN ('accepted','wrong_answer')
                AND json_extract(result,'$.status')=status ORDER BY created,id''', (identity,))
            candidates = []
            expected = ranking_protocol(control, spec)
            best, origins = [None]*count, [None]*count
            best_submissions = [[] for _ in range(count)]
            for row in rows:
                observed = dict(problem_id=identity, environment=json.loads(row['environment'] or '{}'),
                                problem_sha256=row['result_pack_sha256'], suite=row['suite'],
                                dataset={'sha256':row['data_sha256'], 'version':row['data_version'],
                                         'profile_sha256':row['data_profile_sha256']})
                dataset = dataset_validation(observed, spec)
                if dataset is not None and not dataset['current']:
                    continue
                if not current_dataset(observed, spec):
                    continue
                if row['suite']!='official_shapes' or ranking_protocol(observed, spec)!=expected:
                    continue
                cases = json.loads(row['cases'] or '[]')
                if not reference_passes(cases,spec):
                    continue
                if row['status']=='accepted' and any(not isinstance(c,dict) or c.get('status')!='accepted' for c in cases):
                    continue
                metric = dict(status=row['profile_status'],cases=json.loads(row['device_cases']))
                times = measured_times(cases,metric,count,method)
                if times is None or (not rules['partial_credit'] and any(t is None for t in times)):
                    continue
                source = dict(id=row['id'], label=row['label'], created=row['created'],
                              source_sha256=row['sha256'], status=row['status'],
                              passed=sum(positive(t) for t in times), total=count,
                              source_url='/api/submissions/'+row['id']+'/source', **source_info)
                for i,value in enumerate(times):
                    if positive(value) and (best[i] is None or value<best[i]):
                        best[i],origins[i] = value,row['id']
                        best_submissions[i] = [source]
                    elif positive(value) and value==best[i]:
                        best_submissions[i].append(source)
                candidates.append(dict(**source,times=times))
            entries = []
            for row in candidates:
                scored = formula_score(row.pop('times'),best,rules['partial_credit'])
                entries.append(row | scored | score_breakdown(scored, rules))
            entries.sort(key=lambda r: (-r['score'],-r['passed'],r['created'],r['id']))
            totals = {row['id']:row['score'] for row in entries}
            groups = {row['id']:row.get('score_breakdown') for row in entries}
            for winners in best_submissions:
                for source in winners:
                    source['total_score'] = totals[source['id']]
                    if groups[source['id']]:
                        source['score_breakdown'] = groups[source['id']]
            reference_row = db.execute('SELECT label FROM submissions WHERE id=?', (reference['submission_id'],)).fetchone()
            reference_label = reference_row['label'] if reference_row else reference.get('label', reference['submission_id'])
            result = dict(base, available=True, baseline_id=baseline['submission_id'] if baseline else None,
                          baseline_label=reference_label if baseline else None,
                          baseline_selection=baseline.get('selection','automatic') if baseline else None,
                          reference_submission_id=reference['submission_id'], reference_label=reference_label,
                          reference_selection='baseline' if baseline else 'completed_run',
                          method=method,total_eligible=len(entries),rules=rules,reference_protocol=expected,
                          tbest_us=best,tbest_submission_ids=origins,reference_kind='local_tbest',
                          case_bests=[dict(index=case['index'],specification=case,
                              available=positive(best[i]),time_us=best[i],
                              score=rules['max_score'] if positive(best[i]) else None,
                              submissions=best_submissions[i],tie_count=len(best_submissions[i]))
                              for i,case in enumerate(spec['cases'])],
                          case_bests_note='逐点选择当前榜单可比提交中的通过点；'
                              + ('不要求整份提交全部通过。' if rules['partial_credit'] else '要求整份提交全部通过。')
                              + '精确耗时相同的提交全部列出，按提交时间、ID 排序。'
                              '单点最高分为 100，不代表该提交总分为 100；源码按提交 ID 读取。',
                          limitation=rules['limitation'],
                          entries=[dict(rank=i,**entry) for i,entry in enumerate(entries[:3],1)])
            self.cache[identity] = (token,result)
            return copy.deepcopy(result)

    def score(self, result, board=None):
        spec = problem(result.get('problem_id','cum_sum'))
        if result.get('status') in {'accepted','wrong_answer'} and not current_dataset(result, spec):
            return dict(available=False, requires_reevaluation=True, reason='本地数据或评测协议版本不匹配；旧记录保留但不参与当前计分')
        dataset = dataset_validation(result)
        if dataset is not None and not dataset['current']:
            return dict(available=False, requires_reevaluation=True, reason=dataset['note'])
        if result.get('status') == 'compatibility_mismatch':
            return dict(available=False,reason='参考输出兼容性未通过，不参与本地排名；数学检查结果单独保留')
        if result.get('status') not in {'accepted','wrong_answer'} or result.get('suite')!='official_shapes':
            return dict(available=False,reason='等待默认测试集评测完成后计分；自定义数据不参加排名')
        board = board if board is not None else self.get(result.get('problem_id','cum_sum'))
        if not board['available']:
            return dict(available=False,reason=board['reason'])
        if ranking_protocol(result, spec)!=board['reference_protocol']:
            return dict(available=False,reason='与当前排行榜的题目版本、数据或评测协议不同')
        if not reference_passes(result.get('cases',[]),dict(validation=board.get('validation',{}),cases=[None]*board['case_count'])):
            return dict(available=False,reason='缺少或未通过当前参考兼容性检查，不参与本地排名')
        metric = result.get('metrics',{}).get('PipeUtilization',{})
        values = measured_times(result.get('cases'),metric,board['case_count'],board['method'])
        if values is None:
            return dict(available=False,reason='通过点缺少与排行榜一致的有效计时，不能混用 Event 和设备耗时')
        if any(positive(value) and not positive(best) for value,best in zip(values,board['tbest_us'])):
            return dict(available=False,reason='本次评测刚完成，等待排行榜刷新对应的 TBest')
        scored = formula_score(values,board['tbest_us'],board['rules']['partial_credit'])
        return dict(available=True,method=board['method'],reference_kind='local_tbest',
                    formula=board['rules']['point_formula'],limitation=board['limitation'],
                    **scored, **score_breakdown(scored, board['rules']))

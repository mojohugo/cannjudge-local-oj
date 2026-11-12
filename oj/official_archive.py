"""Official-only history. No reads from local evaluation results or calibration."""
import hashlib
import copy
import json
import logging
import math
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from .config import ROOT
from .official_accounts import OfficialAccounts, account_filter
from .official_scoring import breakdown as score_breakdown
from .official_scoring import official_snapshot_breakdown, snapshot_matches
from .official_comparison import annotate_totals, public_reference, reference_relation
from .official_targets import configuration, project_template, official_template, normalize_project, public_problem, local_evaluation

logger = logging.getLogger(__name__)


def compact_record(record):
    """Project an already annotated read; never recompute scores or freshness."""
    result = {key: record.get(key) for key in (
        'data_origin', 'problem_id', 'account_id', 'official_display_id',
        'official_id', 'official_url', 'source_sha256', 'source_verified')}
    total = record.get('current_official_total') or {}
    result['current_official_total'] = {key: total.get(key) for key in (
        'available', 'score', 'snapshot_id', 'verified_at', 'historical')}
    result['cases'] = []
    for case in record.get('cases', []):
        hidden = case.get('status') == 'Hidden'
        item = {key: case.get(key) for key in ('index', 'status')}
        item.update({key: None if hidden else case.get(key) for key in (
            'time_us', 'tbest_us', 'error_percent', 'score')})
        spec = case.get('data_spec')
        item['data_spec'] = ({key: copy.deepcopy(spec.get(key)) for key in ('dtype', 'shape')}
                             if not hidden and isinstance(spec, dict) else None)
        result['cases'].append(item)
    return result


def validate_case_metadata(value):
    if (not isinstance(value, dict) or type(value.get('schema_version')) is not int or value['schema_version'] != 1 or
            not isinstance(value.get('note'), str) or not isinstance(value.get('problems'), dict)):
        raise ValueError('Invalid official case metadata document')
    json.dumps(value, allow_nan=False)
    identity = lambda x: isinstance(x, str) and re.fullmatch(r'[0-9a-f]{24}', x)
    for key, problem in value['problems'].items():
        if (not re.fullmatch(r'[a-z][a-z0-9_]{0,63}', key) or not isinstance(problem, dict) or
                not identity(problem.get('official_problem_id')) or not isinstance(problem.get('evidence'), dict) or
                not isinstance(problem.get('cases'), list)):
            raise ValueError('Invalid official case metadata problem: '+key)
        seen_ids, seen_indices = set(), set()
        for case in problem['cases']:
            if (not isinstance(case, dict) or not identity(case.get('testcase_id')) or
                    type(case.get('index')) is not int or case['index'] < 1 or
                    case['testcase_id'] in seen_ids or case['index'] in seen_indices):
                raise ValueError('Invalid or duplicate official case identity: '+key)
            seen_ids.add(case['testcase_id'])
            seen_indices.add(case['index'])
        diagnostic = problem.get('diagnostic_submission_ids', [])
        if not isinstance(diagnostic, list) or any(not identity(x) for x in diagnostic) or len(set(diagnostic)) != len(diagnostic):
            raise ValueError('Invalid diagnostic submission identities: '+key)
        unmapped = problem.get('unmapped_specifications', [])
        if not isinstance(unmapped, list):
            raise ValueError('Invalid unmapped specifications: '+key)
        seen_specs = set()
        for spec in unmapped:
            if (not isinstance(spec, dict) or 'testcase_id' in spec or 'index' in spec or
                    spec.get('official_case_index') is not None or
                    spec.get('original_values_available') is not False or
                    not isinstance(spec.get('shape'), list) or not spec['shape'] or
                    any(type(n) is not int or n < 1 for n in spec['shape']) or
                    not isinstance(spec.get('dtype'), str) or not re.fullmatch(r'[a-zA-Z0-9_]{1,32}', spec['dtype']) or
                    not isinstance(spec.get('parameters'), dict) or not isinstance(spec.get('evidence'), dict)):
                raise ValueError('Invalid unmapped specification: '+key)
            evidence = spec['evidence']
            if (not identity(evidence.get('official_id')) or
                    not isinstance(evidence.get('source_sha256'), str) or
                    not re.fullmatch(r'[0-9a-f]{64}', evidence['source_sha256']) or
                    not isinstance(evidence.get('kind'), str) or not evidence['kind']):
                raise ValueError('Unmapped specification lacks official evidence: '+key)
            signature = json.dumps([spec['shape'], spec['dtype'], spec['parameters']], sort_keys=True)
            if signature in seen_specs:
                raise ValueError('Duplicate unmapped specification: '+key)
            seen_specs.add(signature)
    return value


class OfficialArchive:
    def __init__(self, settings, store, account_id='default', accounts=None, merge_accounts=False):
        self.settings, self.store = settings, store
        self.accounts = accounts or OfficialAccounts(settings, store)
        self.account_id = self.accounts.get(account_id)['id']
        self.merge_accounts = merge_accounts
        self.directory = settings.state_dir/'official-history'
        if self.account_id != 'default':self.directory = self.directory/'accounts'/self.account_id
        self._case_metadata = None
        self._case_metadata_seen = None
        self._case_metadata_lock = threading.Lock()
        self._case_metadata_path = ROOT/'config/official_cases.json'
        self._case_snapshot()
        with store.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS official_history (
                    official_id TEXT PRIMARY KEY, problem_id TEXT NOT NULL,
                    create_time TEXT NOT NULL, fetched_at TEXT NOT NULL,
                    score REAL, status TEXT NOT NULL, record TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS official_history_problem ON official_history(problem_id,create_time);
                CREATE TABLE IF NOT EXISTS official_problems (problem_id TEXT PRIMARY KEY, record TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS official_sync (
                    id TEXT PRIMARY KEY, created REAL NOT NULL, updated REAL NOT NULL,
                    status TEXT NOT NULL, problems TEXT NOT NULL, result TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS official_sync_queue ON official_sync(status,created);
                CREATE TABLE IF NOT EXISTS official_auto_sync (
                    id INTEGER PRIMARY KEY CHECK(id=1), next_due REAL NOT NULL,
                    last_success REAL, failures INTEGER NOT NULL DEFAULT 0,
                    last_sync_id TEXT, last_status TEXT);
                INSERT OR IGNORE INTO official_auto_sync(id,next_due) VALUES(1,0);
            ''')

    def _case_snapshot(self):
        """Readers keep one validated snapshot while atomic metadata updates arrive."""
        def version(stat):
            return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns
        with self._case_metadata_lock:
            stamp = None
            try:
                stamp = version(self._case_metadata_path.stat())
                if stamp == self._case_metadata_seen and self._case_metadata is not None:
                    return self._case_metadata
                raw = self._case_metadata_path.read_bytes()
                if version(self._case_metadata_path.stat()) != stamp:
                    if self._case_metadata is not None:
                        return self._case_metadata
                    raise ValueError('Official case metadata changed while being read')
                candidate = validate_case_metadata(json.loads(raw.decode('utf-8')))
            except (OSError, ValueError, TypeError) as error:
                if self._case_metadata is None:
                    raise
                if stamp != self._case_metadata_seen:
                    logger.warning('Keeping last valid official case metadata: %s', error)
                self._case_metadata_seen = stamp
                return self._case_metadata
            self._case_metadata = candidate
            self._case_metadata_seen = stamp
            return candidate

    @property
    def case_metadata(self):
        return copy.deepcopy(self._case_snapshot())

    @staticmethod
    def identity(value):
        if not isinstance(value,str) or not re.fullmatch(r'[0-9a-f]{24}',value):
            raise ValueError('需要 24 位官方 submission ID，不是本地 OJ 的 32 位 ID')
        return value

    def ingest(self, record, db=None):
        if db is None:
            with self.store.db() as connection:
                connection.execute('BEGIN IMMEDIATE')
                return self.ingest(record, connection)
        if self.merge_accounts:raise ValueError('合并档案只用于读取；写入必须指定来源账户')
        identity = self.identity(record['official_id'])
        if record.get('data_origin') != 'official_cannjudge':
            raise ValueError('Only official records may be imported')
        if record.get('account_id',self.account_id)!=self.account_id:raise ValueError('官方记录账户不匹配')
        record = dict(record,account_id=self.account_id)
        old=db.execute('SELECT record FROM official_history WHERE official_id=?',(identity,)).fetchone()
        if old and json.loads(old['record']).get('account_id','default')!=self.account_id:
            raise ValueError('此官方记录属于另一账户，不能覆盖')
        if old and not record.get('official_score_snapshot') and record.get('official_server_score') is None:
            snapshot=json.loads(old['record']).get('official_score_snapshot')
            if snapshot_matches(record,snapshot):
                record['official_score_snapshot']=snapshot
                if record.get('score') is None:
                    record.update(score=snapshot['score'],score_source='official_ranking_snapshot',
                                  score_note='保留此前官方榜单总分，取自 '+snapshot['fetched_at']+'；不代表最新 TBest 下的分数。')
        value = json.dumps(record,ensure_ascii=False,allow_nan=False)
        db.execute('INSERT OR REPLACE INTO official_history VALUES(?,?,?,?,?,?,?)',
            (identity,record['problem_id'],record.get('create_time') or '',record['fetched_at'],
             record.get('score'),record['official_status'],value))

    def save_problem(self, info):
        if self.merge_accounts:raise ValueError('合并档案只用于读取；写入必须指定来源账户')
        if info.get('data_origin') != 'official_cannjudge':
            raise ValueError('Invalid official problem origin')
        with self.store.db() as db:
            db.execute('INSERT OR REPLACE INTO official_problems VALUES(?,?)',
                       (self.problem_key(info['problem_id']),json.dumps(info,ensure_ascii=False,allow_nan=False)))

    def problem_key(self, problem_id):
        return problem_id if self.account_id=='default' else self.account_id+'/'+problem_id

    def _read_filter(self):
        if self.merge_accounts:
            return "COALESCE(json_extract(record, '$.account_id'),'default') IN (SELECT id FROM official_accounts)", ()
        return account_filter('record'), (self.account_id,)

    def _problem_infos(self):
        with self.store.db() as db:
            rows=db.execute('SELECT problem_id,record FROM official_problems ORDER BY problem_id').fetchall()
            accounts={r[0] for r in db.execute('SELECT id FROM official_accounts')}
        infos={}
        for row in rows:
            owner, _, key=row['problem_id'].rpartition('/')
            owner=owner or 'default'
            if owner not in accounts or (not self.merge_accounts and owner!=self.account_id):continue
            info=json.loads(row['record'])
            if info.get('problem_id')!=key:continue
            old=infos.get(key)
            if old is None or (info.get('fetched_at') or '')>(old.get('fetched_at') or ''):
                infos[key]=info
        return infos

    def _account_labels(self):
        with self.store.db() as db:
            return {r['id']:r['label'] for r in db.execute('SELECT id,label FROM official_accounts')}

    def _read_scope(self):
        return dict(scope='all_saved_official_accounts' if self.merge_accounts else 'selected_official_account',
                    account_id=None if self.merge_accounts else self.account_id)

    def catalog(self):
        config=configuration()
        with self.store.db() as db:
            infos=self._problem_infos()
            for identity in config['problems']:
                if identity not in infos:
                    value=public_problem(identity)
                    if value:infos[identity]=value
            result=[]
            for info in sorted(infos.values(),key=lambda r:r['problem_id']):
                condition,args=self._read_filter()
                count=db.execute('SELECT count(*) FROM official_history WHERE problem_id=? AND '+condition,(info['problem_id'],)+args).fetchone()[0]
                is_project = info['problem_id'] in config.get('project_templates', {})
                local = local_evaluation(info['problem_id'])
                contest = config.get('contest_overrides', {}).get(info['problem_id'], config['contest_url'])
                result.append({k:info.get(k) for k in ('problem_id','official_id','title','official_url','fetched_at','total_submissions','code_template')} | {
                    'imported_submissions':count, 'submission_kind':'project' if is_project else 'kernel',
                    'submission_tool':'submit_official_project' if is_project else 'submit_official_kernel',
                    'local_evaluation':local,'official_only':not local['available'],'case_count':len(info.get('testcases',[])),
                    'contest_title':config.get('contest_titles',{}).get(contest)})
            return dict(data_origin='official_cannjudge',**self._read_scope(),problems=result,
                        latest_sync=self.sync_get(),auto_sync=self.auto_sync_status(),local_evaluations_included=False)

    def problem(self, problem_id):
        metadata=self._case_snapshot()
        info=self._score_info(problem_id)
        if not info:
            raise ValueError('官方题目信息尚未同步，请先 sync_official_history')
        info.update(self._read_scope())
        info.pop('data_specifications',None)
        known=metadata['problems'].get(problem_id,{})
        if known.get('official_problem_id')==info['official_id']:
            visible={str(c.get('_id',c.get('id'))) for c in info.get('testcases',[]) if not c.get('hidden')}
            cases=[c for c in known['cases'] if 'testcases' not in info or c['testcase_id'] in visible]
            info['data_specifications']=copy.deepcopy(dict(cases=cases,evidence=known['evidence'],note=metadata['note']))
            if known.get('unmapped_specifications'):
                info['data_specifications']['unmapped_specifications']=copy.deepcopy(known['unmapped_specifications'])
                info['data_specifications']['unmapped_specifications_note']='这些规格已由官方诊断确认存在，但官方点号、顺序和重复次数未知，原始输入未恢复；不附到任意隐藏测试点，也不增加逐点已知规格的计分覆盖率。'
        config=configuration()
        is_project=problem_id in config.get('project_templates',{})
        info.update(submission_kind='project' if is_project else 'kernel',
                    submission_tool='submit_official_project' if is_project else 'submit_official_kernel',
                    local_evaluation=local_evaluation(problem_id))
        info['official_only']=not info['local_evaluation']['available']
        contest=config.get('contest_overrides',{}).get(problem_id,config['contest_url'])
        info['contest_title']=config.get('contest_titles',{}).get(contest)
        if problem_id in config.get('kernel_templates',{}):
            info.setdefault('data_specifications',dict(cases=[],evidence=dict(kind='not_acquired'),
                            note='尚无有证据支持的测试规格；公开模板样例不代表隐藏测试规格。'))
        if is_project or problem_id in config.get('kernel_templates',{}):
            template=official_template(problem_id)
            info.update(editable_files=template['editable_files'],
                        readonly_files=[f['path'] for f in template['files'] if not f['editable']],
                        environment=template['environment'],template_tool='get_official_template')
        # The internal evidence includes public rows for other contestants. The
        # archive API exposes only saved submissions and the reference identity.
        info.pop('ranking_snapshot',None)
        return info

    def _score_info(self, problem_id):
        return self._problem_infos().get(problem_id) or public_problem(problem_id) or {}

    def annotate(self, record, metadata=None, info=None, account_labels=None):
        metadata=self._case_snapshot() if metadata is None else metadata
        record.setdefault('account_id','default')
        record['account_label']=(self._account_labels() if account_labels is None else account_labels).get(record['account_id'],record['account_id'])
        known=metadata['problems'].get(record['problem_id'],{})
        matches=known.get('official_problem_id')==record.get('official_problem_id')
        specs={s['testcase_id']:s for s in known.get('cases',[])} if matches else {}
        for case in record.get('cases',[]):
            spec=specs.get(case['testcase_id'])
            case['data_spec']=copy.deepcopy(dict(spec,evidence=known['evidence'])) if spec and case['status']!='Hidden' else None
        record['data_spec_note']=metadata['note']
        if record.get('enrichment_sync_id'):
            record['enrichment']=self.enrichment_status(record['enrichment_sync_id'],record['account_id'])
        record.setdefault('source_verified',None)
        record.setdefault('source_status','archived_unverified' if record.get('source_available') else 'unavailable')
        record.setdefault('ranking_status','fetched' if record.get('reference_snapshot_id') else 'unavailable')
        record['diagnostic_submission']=bool(matches and record['official_id'] in known.get('diagnostic_submission_ids',[]))
        if record['diagnostic_submission']:
            record['diagnostic_note']='已知的数据规格诊断提交；保留官方原始成绩，但不作为优化性能最佳候选。'
        else:
            record.pop('diagnostic_note',None)
        info=self._score_info(record['problem_id']) if info is None else info
        record['score_breakdown']=score_breakdown(
            info,record.get('cases',[]),set(specs)-{str(c.get('_id',c.get('id'))) for c in info.get('testcases',[]) if c.get('hidden')},
            valid=record.get('valid') is not False and info.get('official_id')==record.get('official_problem_id'),
            complete=record.get('evaluation_complete'))
        record['official_score_breakdown']=official_snapshot_breakdown(info,record,set(specs)-{
            str(c.get('_id',c.get('id'))) for c in info.get('testcases',[]) if c.get('hidden')})
        annotate_totals(record,info,set(specs)-{
            str(c.get('_id',c.get('id'))) for c in info.get('testcases',[]) if c.get('hidden')})
        snapshot=record.get('official_score_snapshot')
        split=record['official_score_breakdown']
        if split and record['official_total']['comparable'] and all(c.get('tbest_us')==next(
                (s.get('tbest_us') for s in snapshot['cases'] if s['testcase_id']==c['testcase_id']),None)
                for c in record.get('cases',[])):
            record['score_breakdown']=split
        return record

    def listing(self, problem_id=None, limit=30, offset=0, order='newest'):
        metadata=self._case_snapshot()
        if order not in ('newest','score'):
            raise ValueError('order 必须是 newest 或 score')
        limit=max(1,min(200,limit));offset=max(0,offset)
        condition,args=self._read_filter()
        where=' WHERE '+condition+(' AND problem_id=?' if problem_id else '')
        if problem_id:args+=(problem_id,)
        ordering='score DESC,create_time DESC,official_id DESC' if order=='score' else 'create_time DESC,official_id DESC'
        with self.store.db() as db:
            total=db.execute('SELECT count(*) FROM official_history'+where,args).fetchone()[0]
            rows=db.execute('SELECT record FROM official_history'+where+' ORDER BY '+ordering+' LIMIT ? OFFSET ?',args+(limit,offset)).fetchall()
        records=[json.loads(row['record']) for row in rows]
        infos={p:self._score_info(p) for p in {r['problem_id'] for r in records}}
        labels=self._account_labels()
        records=[self.annotate(row,metadata,infos[row['problem_id']],labels) for row in records]
        for row in records:
            row.pop('files',None)
            for case in row.get('cases',[]):case.pop('message',None)
        return dict(data_origin='official_cannjudge',**self._read_scope(),submissions=records,total=total,offset=offset,limit=limit,
                    order=order,order_note='历史浏览排序，保留原归档分；跨快照分数不构成当前性能排名。当前可比榜见 get_official_case_bests。',
                    next_offset=offset+len(records) if offset+len(records)<total else None,local_evaluations_included=False)

    def get(self, identity, summary_only=False):
        self.identity(identity)
        condition,args=self._read_filter()
        with self.store.db() as db:
            row=db.execute('SELECT record FROM official_history WHERE official_id=? AND '+condition,(identity,)+args).fetchone()
        if not row:
            raise ValueError('官方记录尚未导入，请先同步官方历史')
        record = self.annotate(json.loads(row['record']))
        return compact_record(record) if summary_only else record

    def cache_updates_since(self, problem_id, updated):
        """Count cached records, not refresh events or newly created submissions."""
        condition, args = self._read_filter()
        with self.store.db() as db:
            rows = db.execute('SELECT fetched_at FROM official_history WHERE problem_id=? AND '+condition,
                              (problem_id,)+args).fetchall()
        times = []
        for row in rows:
            try:
                stamp = datetime.fromisoformat(row['fetched_at'].replace('Z', '+00:00'))
                if stamp.tzinfo is not None:
                    times.append(stamp.astimezone(timezone.utc))
            except (ValueError, TypeError, AttributeError, OverflowError):
                pass
        cutoff = datetime.fromtimestamp(updated, timezone.utc) if updated is not None else None
        return dict(total_records=len(rows), records_updated_after_handoff=
                    sum(stamp > cutoff for stamp in times) if cutoff is not None else None,
                    latest_record_fetched_at=max(times).isoformat() if times else None,
                    records_with_unknown_timestamp=len(rows)-len(times),
                    timestamp_basis='official_history.fetched_at',
                    note='按本题全部已缓存记录的最后抓取时间计数，包含旧提交的重新抓取；不是新增提交数或刷新次数。')

    def source_content(self, identity, file_path='kernel.asc'):
        record=self.get(identity)
        inventory={item['path']:item for item in record['files']}
        if file_path not in inventory:
            raise ValueError('官方未提供该文件或没有读取权限；可用文件见 get_official_record.files')
        # Select a logical archive entry, never turn an untrusted file path into a disk path.
        owner=self.accounts.get(record['account_id'])['id']
        directory=self.settings.state_dir/'official-history'
        if owner!='default':directory=directory/'accounts'/owner
        files=json.loads((directory/identity/'files.json').read_text(encoding='utf-8'))
        item=next((f for f in files if f['path']==file_path),None)
        if item is None or hashlib.sha256(item['content'].encode()).hexdigest()!=inventory[file_path]['sha256']:
            raise ValueError('官方源码归档校验失败，请重新同步')
        return record,item['content'],inventory[file_path]['sha256']

    def source(self, identity, file_path='kernel.asc', start_line=1, max_lines=300):
        record,content,sha=self.source_content(identity,file_path)
        lines=content.splitlines(keepends=True)
        start=max(1,start_line);count=max(1,min(2000,max_lines))
        end=min(start-1+count,len(lines))
        return dict(data_origin='official_cannjudge',official_id=identity,problem_id=record['problem_id'],
                    account_id=record['account_id'],account_label=record['account_label'],
                    file_path=file_path,sha256=sha,source=''.join(lines[start-1:end]),
                    start_line=start,total_lines=len(lines),next_line=end+1 if end<len(lines) else None,
                    official_url=record['official_url'])

    def project_source(self, identity):
        record=self.get(identity)
        template=project_template(record['problem_id'])
        files=[dict(path=p,content=self.source_content(identity,p)[1]) for p in template['editable_files']]
        files,digest,_=normalize_project(record['problem_id'],files,self.settings.max_source_bytes)
        if digest!=record.get('source_sha256'):
            raise ValueError('官方工程归档集合哈希不一致，请重新同步')
        return dict(problem_id=record['problem_id'],files=files,source_sha256=digest,official_id=identity,
                    account_id=record['account_id'],account_label=record['account_label'])

    def bests(self, problem_id, case_index=None):
        metadata=self._case_snapshot()
        condition,args=self._read_filter()
        with self.store.db() as db:
            rows=[json.loads(r['record']) for r in db.execute('SELECT record FROM official_history WHERE problem_id=? AND '+condition,(problem_id,)+args)]
        info=self._score_info(problem_id)
        labels=self._account_labels()
        formula_supported=not info or (info.get('code_template') in ('npu_kernel_dev','custom_template') and
                                       not info.get('use_baseline') and info.get('score_mode') in (0,1))
        # Compare every submission against the same, latest OFFICIAL reference.
        # Local timing, baseline and calibration tables are never read here.
        references={str(c.get('_id',c.get('id'))):(info.get('fetched_at',''),c.get('tbest'))
                    for c in info.get('testcases',[]) if not c.get('hidden')}
        public_ref=public_reference(info)
        reference_ids={key:public_ref['snapshot_id'] for key in references}
        groups={}
        positive=lambda x:isinstance(x,(int,float)) and not isinstance(x,bool) and math.isfinite(x) and x>0
        expected=[str(c.get('_id',c.get('id'))) for c in info.get('testcases',[])]
        indices={key:index for index,key in enumerate(expected,1)}
        hidden={str(c.get('_id',c.get('id'))) for c in info.get('testcases',[]) if c.get('hidden')}
        known=metadata['problems'].get(problem_id,{})
        specifications={s['testcase_id']:copy.deepcopy(dict(s,evidence=known['evidence'])) for s in known.get('cases',[])} if known.get('official_problem_id')==info.get('official_id') else {}
        for index,key in enumerate(expected,1):
            groups[(index,key)]=dict(index=index,testcase_id=key,data_spec=None if key in hidden else specifications.get(key),candidates=[])
        for row in rows:
            if info and row.get('official_problem_id')!=info['official_id']:continue
            self.annotate(row,metadata,info,labels)
            for case in row['cases']:
                if expected and case['testcase_id'] not in expected:continue
                key=(indices.get(case['testcase_id'],case['index']),case['testcase_id'])
                groups.setdefault(key,dict(index=case['index'],testcase_id=case['testcase_id'],data_spec=case['data_spec'],candidates=[]))
                if case['data_spec'] and case['testcase_id'] not in hidden:groups[key]['data_spec']=case['data_spec']
                old=references.get(case['testcase_id'],('',None))
                if (not (row.get('result_published_at') and row.get('ranking_status') in ('pending','unavailable'))
                        and case['status']!='Hidden' and positive(case.get('tbest_us')) and row['fetched_at']>old[0]):
                    references[case['testcase_id']]=(row['fetched_at'],case['tbest_us'])
                    reference_ids[case['testcase_id']]=row.get('reference_snapshot_id')
        info_references={str(c.get('_id',c.get('id'))):c.get('tbest') for c in info.get('testcases',[]) if not c.get('hidden')}
        if (not references or any(value!=public_ref['snapshot_id'] for value in reference_ids.values()) or
                any(value[1]!=info_references.get(key) for key,value in references.items())):
            public_ref.update(snapshot_id=None,fetched_at=max((v[0] for v in references.values()),default=None),
                             note='公开 TBest 由不同或缺少身份的归档引用组成；未证明与完整分来自同一快照。')
        for row in rows:
            if 'official_total' not in row:continue
            for name in ('official_total','current_official_total'):
                row[name]['tbest_comparison']=reference_relation(row[name],public_ref)
        def total_score(row):
            result=dict(available=False,score=None,max_score=100,total_cases=len(expected),
                        source='official_formula_from_official_timings',official=False,
                        note='按本份代码的官方耗时和统一最新官方 TBest 复算，不是新增官方实测成绩。')
            if not formula_supported or not expected or hidden:
                return {**result,'reason':'计分规则未确认、测试点不完整或含隐藏点'}
            cases={c['testcase_id']:c for c in row['cases']}
            if (len(cases)!=len(row['cases']) or set(cases)!=set(expected)
                    or row.get('valid') is False or row.get('evaluation_complete') is False):
                return {**result,'reason':'该记录无效或缺少完整官方结果'}
            scores=[];passed=0
            for key in expected:
                case=cases[key];status=case['status'].lower()
                if status in ('hidden','pending','running','judging','notrun','queued',''):
                    return {**result,'reason':'仍有隐藏、未运行或未完成的测试点'}
                if status not in ('pass','accepted'):
                    scores.append(0.);continue
                timing=case.get('time_us');reference=references.get(key,('',None))[1]
                if not positive(timing) or not positive(reference):
                    return {**result,'reason':'通过点缺少有效官方耗时或 TBest'}
                scores.append(100/(1+math.log(max(1,timing/reference))/math.log(1.5)));passed+=1
            score=math.fsum(scores)/len(expected) if info.get('score_mode')==0 or passed==len(expected) else 0.
            return {**result,'available':True,'score':score,'passed_cases':passed}
        totals={row['official_id']:total_score(row) for row in rows
                if not info or row.get('official_problem_id')==info['official_id']}
        breakdowns={row['official_id']:score_breakdown(info,row['cases'],set(specifications)-hidden,
                    references={key:value[1] for key,value in references.items()},
                    valid=row.get('valid'),complete=row.get('evaluation_complete'))
                    for row in rows if row['official_id'] in totals}
        def source_ref(row):
            return dict(official_id=row['official_id'],official_url=row['official_url'],source_sha256=row['source_sha256'],
                        account_id=row['account_id'],account_label=row['account_label'],
                        submission_kind=row.get('submission_kind','kernel'),
                        source_files=[f['path'] for f in row.get('files',[])],
                        official_display_id=row.get('official_display_id'),source_available=row['source_available'],
                        source_verified=row.get('source_verified'),source_status=row.get('source_status'),
                        ranking_status=row.get('ranking_status'),reference_fetched_at=row.get('reference_fetched_at'),
                        passed=row['passed'],passed_cases=row.get('passed_cases'),total_cases=row.get('total_cases'),
                        official_status=row['official_status'],score=row['score'],score_source=row['score_source'],
                        official_total=row['official_total'],official_score_breakdown=row['official_score_breakdown'],
                        current_official_total=row['current_official_total'],
                        current_official_score_breakdown=row['current_official_score_breakdown'],
                        estimated_total_score=totals[row['official_id']],
                        score_breakdown=breakdowns[row['official_id']],
                        create_time=row.get('create_time'),fetched_at=row['fetched_at'])
        for row in rows:
            if info and row.get('official_problem_id')!=info['official_id']:continue
            if row.get('valid') is False or row['diagnostic_submission'] or row.get('evaluation_complete') is False:continue
            for case in row['cases']:
                if (expected and case['testcase_id'] not in expected) or case['testcase_id'] in hidden:continue
                timing=case.get('time_us')
                if case['status'].lower() not in ('pass','accepted') or not positive(timing):continue
                key=(indices.get(case['testcase_id'],case['index']),case['testcase_id'])
                _,reference=references.get(case['testcase_id'],('',None))
                score=100/(1+math.log(max(1,timing/reference))/math.log(1.5)) if formula_supported and positive(reference) else None
                ref=dict(**source_ref(row),time_us=timing,case_score=score,error_percent=case.get('error_percent'))
                groups[key]['candidates'].append(ref)
        best=[]
        for key,item in sorted(groups.items()):
            candidates=item.pop('candidates')
            candidates.sort(key=lambda c:(c['time_us'],c['official_id']))
            winner=candidates[0] if candidates else None
            score=winner['case_score'] if winner else None
            winners=[c for c in candidates if (math.isclose(c['case_score'],score,rel_tol=0,abs_tol=1e-10)
                     if score is not None else c['time_us']==winner['time_us'])] if winner else []
            fetched_at,tbest=references.get(key[1],(None,None))
            item.update(score=score,score_source='official_formula_from_official_timings' if score is not None else 'not_available',
                        time_us=winner['time_us'] if winner else None,tbest_us=tbest,
                        reference_fetched_at=fetched_at,reference_snapshot_id=reference_ids.get(key[1]),
                        submissions=winners,tie_count=len(winners),
                        eligible_submissions=len(candidates),available=bool(winner))
            best.append(item)
        if case_index is not None and case_index not in {c['index'] for c in best}:
            raise ValueError('该官方测试点尚未同步或不存在')
        # The denominator is the official problem's complete case inventory, never
        # the subset requested by case_index or just the passing/visible cases.
        covered=[c for c in best if c['testcase_id'] in expected and c['score'] is not None]
        complete=bool(expected) and not hidden and len(covered)==len(expected) and formula_supported
        best_single=sorted((dict(official_id=r['official_id'],official_display_id=r.get('official_display_id'),
                                account_id=r['account_id'],account_label=r['account_label'],
                                score=totals[r['official_id']]['score']) for r in rows
                            if r['official_id'] in totals and totals[r['official_id']]['available']
                            and totals[r['official_id']]['passed_cases']==len(expected)
                            and not r.get('diagnostic_submission')),key=lambda r:(-r['score'],r['official_id']))
        score=math.fsum(c['score'] for c in covered)/len(expected) if complete else None
        ranked=sorted((source_ref(r) for r in rows if r['official_id'] in totals
                       and totals[r['official_id']]['available'] and not r.get('diagnostic_submission')),
                      key=lambda r:(-r['estimated_total_score']['score'],
                                    -r['estimated_total_score']['passed_cases'],r['official_id']))
        leaderboard=dict(available=bool(ranked),total_eligible=len(ranked),
                         entries=[dict(rank=i,**r) for i,r in enumerate(ranked[:3],1)],
                         sort_by='estimated_total_score',reference_kind='latest_official_tbest',
                         note='当前账户的完整有效官方记录，按统一最新官方 TBest 复算总分排序；保留原始记录分数及来源。排除无效、未完成和已知诊断提交。')
        leaderboard['ranking_scope']='all'
        for entry in leaderboard['entries']:
            entry['ranking_score']=entry['estimated_total_score']['score']
        if not ranked:
            known_ranked=sorted((source_ref(r) for r in rows if r['official_id'] in breakdowns
                                and breakdowns[r['official_id']]['known']['available']
                                and not r.get('diagnostic_submission')),
                               key=lambda r:(-r['score_breakdown']['known']['mean_score'],r['official_id']))
            leaderboard.update(available=bool(known_ranked),total_eligible=len(known_ranked),ranking_scope='known',
                sort_by='score_breakdown.known.mean_score',
                entries=[dict(rank=i,ranking_score=r['score_breakdown']['known']['mean_score'],**r)
                         for i,r in enumerate(known_ranked[:3],1)],
                note='完整复算总分不可用；此榜仅按所有已知点的均分排序（统一最新官方 TBest），不是完整官方总分榜。未知点单列，排除诊断、无效与未完成记录。')
        known_leaderboard=copy.deepcopy(leaderboard) if leaderboard['ranking_scope']=='known' else None
        # Visible results and confirmed input specifications are different groups.
        # Select from all eligible records, not the top three known-point entries.
        public_ranked=sorted((r for r in rows if r['official_id'] in breakdowns
                              and breakdowns[r['official_id']]['visible']['available']
                              and not r.get('diagnostic_submission')),
                             key=lambda r:(-breakdowns[r['official_id']]['visible']['mean_score'],r['official_id']))
        best_public=source_ref(public_ranked[0]) if public_ranked else None
        official_ranked=sorted((source_ref(r) for r in rows if r['official_id'] in totals
            and r['current_official_total']['available'] and r['current_official_total']['comparable']
            and r.get('valid') is not False and r.get('evaluation_complete') is not False
            and not r.get('diagnostic_submission')),key=lambda r:(-r['current_official_total']['score'],r['official_id']))
        historical=sorted((source_ref(r) for r in rows if r['official_id'] in totals
            and r['official_total']['available'] and not r['official_total']['comparable']),
            key=lambda r:(r['official_total']['fetched_at'] or '',r['official_id']),reverse=True)
        historical_totals=dict(available=bool(historical),comparable=False,total_records=len(historical),
            sort_by='official_total.fetched_at DESC',entries=historical[:20],
            note='历史或未重新验证的代表分，按抓取时间展示，不按分数混排；保留原值与来源，不按天数删除。更多记录见 list_official_history。')
        if not ranked and official_ranked:
            leaderboard=dict(available=True,total_eligible=len(official_ranked),ranking_scope='official_total',
                sort_by='current_official_total.score',reference_kind='official_ranking_api',
                snapshot_id=official_ranked[0]['current_official_total']['snapshot_id'],
                verified_at=official_ranked[0]['current_official_total']['verified_at'],
                entries=[dict(rank=i,ranking_score=r['current_official_total']['score'],**r) for i,r in enumerate(official_ranked[:3],1)],
                note='仅按同一 ranking 响应中重新核实的完整分排序，包含隐藏点。原归档值保留于 official_total；历史或未验证代表分单列。当前指最新已同步快照，读取不会自动刷新官网。')
        hidden_ranked=[];historical_hidden=[]
        for row in rows:
            if (row['official_id'] not in totals or row.get('diagnostic_submission')
                    or row.get('valid') is False or row.get('evaluation_complete') is not True):
                continue
            for field,target in (('current_official_score_breakdown',hidden_ranked),('official_score_breakdown',historical_hidden)):
                split=row.get(field)
                group=split.get('hidden') if split else None
                if not group or not group['available'] or not group['total_cases']:continue
                if field=='official_score_breakdown' and row['official_total']['comparable']:continue
                target.append(dict(**source_ref(row),hidden_score=copy.deepcopy(group),score_snapshot_at=split['fetched_at']))
        def hidden_top(items):
            items.sort(key=lambda r:(-r['hidden_score']['score_sum'],r['official_id']))
            return [dict(rank=i,ranking_score=r['hidden_score']['score_sum'],**r) for i,r in enumerate(items[:3],1)]
        hidden_leaderboard=dict(available=bool(hidden_ranked),ranking_scope='hidden_aggregate',
            sort_by='hidden_score.score_sum',total_eligible=len(hidden_ranked),entries=hidden_top(hidden_ranked),
            historical_total_eligible=len(historical_hidden),historical_entries=[dict(r,rank=None,ranking_score=None) for r in
                sorted(historical_hidden,key=lambda r:(r['score_snapshot_at'],r['official_id']),reverse=True)[:20]],
            note='隐藏点合计仅在同一已核验 ranking 响应内排序；不是隐藏单点成绩。历史合计按快照时间单列，不作跨快照性能排名。')
        projection=dict(available=complete,score=score,max_score=100,total_cases=len(expected),covered_cases=len(covered),
                        source='official_case_best_projection',official=False,measured=False,
                        source_available_cases=sum(any(s['source_available'] for s in c['submissions']) for c in covered),
                        source_count=len({c['submissions'][0]['official_id'] for c in covered}),
                        best_single_submission=best_single[0] if best_single else None,
                        gain_over_best_single=score-best_single[0]['score'] if complete and best_single else None,
                        contributions=[dict(index=c['index'],testcase_id=c['testcase_id'],
                                            score=c['score'],contribution=c['score']/len(expected),
                                            account_id=c['submissions'][0]['account_id'],
                                            official_id=c['submissions'][0]['official_id']) for c in covered] if expected else [],
                        reason=None if complete else '缺少完整的官方通过点、有效 TBest 或已确认计分规则，暂不估算总分。',
                        note='逐点选择已同步官方历史的最高分，再按完整测试集取平均；仅表示组合目标，未合并代码或重新实测，不保证合并后保持各点性能。')
        projection['score_breakdown']=score_breakdown(info,
            [dict(testcase_id=c['testcase_id'],status='Hidden' if c['testcase_id'] in hidden else
                  ('Pass' if c['score'] is not None else 'NotRun'),time_us=c['time_us']) for c in best],
            set(specifications)-hidden,references={key:value[1] for key,value in references.items()})
        if self.merge_accounts:
            leaderboard['note']=leaderboard['note'].replace('当前账户的','所有已保存账户的')
        return dict(data_origin='official_cannjudge',**self._read_scope(),problem_id=problem_id,
                    case_bests=[c for c in best if case_index is None or c['index']==case_index],
                    projection=projection,leaderboard=leaderboard,known_leaderboard=known_leaderboard,
                    public_reference=public_ref,historical_official_totals=historical_totals,
                    best_official_submission=official_ranked[0] if official_ranked else None,
                    best_public_submission=best_public,
                    hidden_leaderboard=hidden_leaderboard,
                    best_hidden_submission=hidden_leaderboard['entries'][0] if hidden_ranked else None,
                    source_tool='read_official_source',record_tool='get_official_record',
                    note=('所有已保存账号' if self.merge_accounts else '当前账号')+'已同步官方记录的单点最高分，按统一的最新官方 TBest 复算；不是全站榜单。并列最高分全部保留，按耗时排序。仅通过点参选，排除无效及已知诊断提交；整份代码可能仍有未通过点。',
                    auto_sync=self.auto_sync_status(),local_evaluations_included=False)

    def request_sync(self, problems, incremental=False, trigger='manual', db=None, official_ids=None):
        if db is None:
            with self.store.db() as connection:
                connection.execute('BEGIN IMMEDIATE')
                return self.request_sync(problems,incremental,trigger,connection,official_ids)
        active=db.execute("SELECT id,problems,result,status FROM official_sync WHERE status IN ('queued','running') AND "+account_filter('result')+" ORDER BY created",(self.account_id,)).fetchall()
        for row in active:
            existing=json.loads(row['problems'])
            saved=json.loads(row['result'])
            mode=saved.get('mode','full')
            targets=saved.get('official_ids')
            if targets and (not official_ids or not set(official_ids)<=set(targets)):continue
            if official_ids and (not targets or row['status']!='queued'):continue
            if all(existing.get(key)==value for key,value in problems.items()) and (incremental or mode=='full'):
                return self.sync_get(row['id'])
        identity=uuid.uuid4().hex;now=time.time()
        result=dict(message='等待同步官方历史，不提交源码',mode='incremental' if incremental else 'full',trigger=trigger)
        if self.account_id!='default':result['account_id']=self.account_id
        if official_ids is not None:result['official_ids']=[self.identity(i) for i in official_ids]
        db.execute('INSERT INTO official_sync VALUES(?,?,?,?,?,?)',
                   (identity,now,now,'queued',json.dumps(problems),json.dumps(result,ensure_ascii=False)))
        return dict(id=identity,account_id=self.account_id,created=now,updated=now,status='queued',problems=problems,result=result,terminal=False)

    def _auto_get(self, db):
        if self.account_id=='default':
            row=dict(db.execute('SELECT * FROM official_auto_sync WHERE id=1').fetchone());row.pop('id');return row
        saved=json.loads(db.execute('SELECT auto_sync FROM official_accounts WHERE id=?',(self.account_id,)).fetchone()[0])
        return dict(next_due=0,last_success=None,failures=0,last_sync_id=None,last_status=None) | saved

    def _auto_set(self, db, **fields):
        if self.account_id=='default':
            db.execute('UPDATE official_auto_sync SET '+','.join(key+'=?' for key in fields)+' WHERE id=1',tuple(fields.values()))
        else:
            value=self._auto_get(db);value.update(fields)
            db.execute('UPDATE official_accounts SET auto_sync=? WHERE id=?',(json.dumps(value),self.account_id))

    def auto_sync_status(self):
        with self.store.db() as db:
            row=self._auto_get(db)
        row.update(account_id=self.account_id,enabled=self.settings.official_submit_enabled and self.settings.official_auto_sync_enabled and bool(self.accounts.get(self.account_id)['profile']),
                   interval_seconds=max(30,int(self.settings.official_auto_sync_seconds)),
                   mode='incremental',scope='all_configured_problems_current_account',
                   note='服务运行时在官方提交队列空闲后检查官网新记录；每轮结束后等待指定间隔。连接失败自动延后重试，源码不会重新提交。')
        return row

    def schedule_auto_sync(self, problems, now=None):
        status=self.auto_sync_status()
        now=time.time() if now is None else now
        if not status['enabled'] or now<status['next_due']:return None
        with self.store.db() as db:
            if db.execute("SELECT 1 FROM official_sync WHERE status IN ('queued','running') AND "+account_filter('result')+" LIMIT 1",(self.account_id,)).fetchone():return None
            db.execute('BEGIN IMMEDIATE')
            if self._auto_get(db)['next_due']>now:return None
            if db.execute("SELECT 1 FROM official_sync WHERE status IN ('queued','running') AND "+account_filter('result')+" LIMIT 1",(self.account_id,)).fetchone():return None
            job=self.request_sync(problems,incremental=True,trigger='automatic',db=db)
            self._auto_set(db,next_due=now+status['interval_seconds'],last_sync_id=job['id'],last_status='queued')
            return job

    def finish_auto_sync(self, identity, status, now=None):
        now=time.time() if now is None else now
        interval=max(30,int(self.settings.official_auto_sync_seconds))
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=self._auto_get(db)
            if row['last_sync_id']!=identity:return
            failures=0 if status=='completed' else row['failures']+1
            delay=min(900,interval*2**min(failures,5))
            self._auto_set(db,next_due=now+delay,last_success=now if not failures else row['last_success'],failures=failures,last_status=status)

    def sync_get(self, identity=None):
        with self.store.db() as db:
            row=db.execute('SELECT * FROM official_sync WHERE id=? AND '+account_filter('result'),(identity,self.account_id)).fetchone() if identity else db.execute('SELECT * FROM official_sync WHERE '+account_filter('result')+' ORDER BY created DESC LIMIT 1',(self.account_id,)).fetchone()
        if not row:return None
        result=dict(row);result['problems']=json.loads(result['problems']);result['result']=json.loads(result['result'])
        result['terminal']=result['status'] not in ('queued','running')
        result['account_id']=self.account_id
        return result

    def enrichment_status(self, identity, account_id=None):
        owner=self.account_id if account_id is None else account_id
        with self.store.db() as db:
            row=db.execute('SELECT id,status,updated,result FROM official_sync WHERE id=? AND '+account_filter('result'),
                           (identity,owner)).fetchone()
        if row is None:return None
        result=json.loads(row['result'])
        return dict(id=row['id'],status=row['status'],updated=row['updated'],
                    message=result.get('message'),errors=result.get('errors',[]))

    def sync_update(self, identity, status, **fields):
        with self.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT result FROM official_sync WHERE id=? AND '+account_filter('result'),(identity,self.account_id)).fetchone()
            if row is None:raise ValueError('同步任务不属于此账户')
            result=json.loads(row['result']);result.update(fields)
            db.execute('UPDATE official_sync SET updated=?,status=?,result=? WHERE id=?',
                       (time.time(),status,json.dumps(result,ensure_ascii=False,allow_nan=False),identity))

    def _sync_candidate(self, db):
        row=db.execute("SELECT id FROM official_sync WHERE status='queued' AND "+account_filter('result')+" ORDER BY created LIMIT 1",(self.account_id,)).fetchone()
        if not row:return None
        if db.execute("SELECT 1 FROM official_sync WHERE status='running' AND "+account_filter('result')+" LIMIT 1",(self.account_id,)).fetchone():return None
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='official_submissions'").fetchone():
            if db.execute("SELECT 1 FROM official_submissions WHERE status IN ('queued','validating','submitting','submitted','judging') AND "+account_filter('result')+" LIMIT 1",(self.account_id,)).fetchone():return None
        return row

    def sync_claim(self):
        with self.store.db() as db:
            # The unlocked read avoids a write transaction when idle or blocked.
            # Re-read every exclusion under the lock before claiming a job.
            if self._sync_candidate(db) is None:return None
            db.execute('BEGIN IMMEDIATE')
            row=self._sync_candidate(db)
            if not row:return None
            db.execute("UPDATE official_sync SET status='running',updated=? WHERE id=?",(time.time(),row['id']))
            if self._auto_get(db)['last_sync_id']==row['id']:self._auto_set(db,last_status='running')
            return row['id']

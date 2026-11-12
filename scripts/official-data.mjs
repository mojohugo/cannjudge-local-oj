// Authenticated, read-only official records; authentication stays in Edge.
import { promises as fs } from 'node:fs';
import path from 'node:path';
import { createHash, randomUUID } from 'node:crypto';
import {bundleHash,validProblemUrl,editableProjectFiles} from './official-project-format.mjs';

const digest = text => createHash('sha256').update(text).digest('hex');
const pause = ms => new Promise(resolve=>setTimeout(resolve,ms));
const numeric = value => value!==null && value!==undefined && value!=='' && Number.isFinite(Number(value)) ? Number(value) : null;
const positive = value => numeric(value)>0 ? Number(value) : null;
const pass = status => String(status).toLowerCase()==='pass';
export const pending = status => /^(pending|queued|waiting|judging|running|compiling|processing|submitted|等待|排队中|评测中)$/i.test(String(status));
export const finished = status => /^(pass|accepted|wrong\s*answer|compile\s*error|compilation\s*error|runtime\s*error|time\s*limit\s*exceeded|memory\s*limit\s*exceeded|output\s*limit\s*exceeded|presentation\s*error|system\s*error|internal\s*error|illegal|invalid|rejected|fail|failed)$/i.test(String(status));
const stable = value => JSON.stringify(value,(_key,v)=>v&&typeof v==='object'&&!Array.isArray(v)?Object.fromEntries(Object.keys(v).sort().map(k=>[k,v[k]])):v);

async function atomicJson(file,value) {
  const temporary=file+'.'+randomUUID()+'.tmp';
  try {await fs.writeFile(temporary,JSON.stringify(value,null,2));await fs.rename(temporary,file);}
  finally {await fs.unlink(temporary).catch(()=>{});}
}

export async function fetchOfficial(context, endpoint) {
  if (!/^\/api\/(?:submissions\/[0-9a-f]{24}|problems\/[a-z0-9_]+(?:\/ranking)?)$/.test(endpoint.split('?')[0]))
    throw new Error('Unsupported official read endpoint');
  const response = await context.request.get('https://cannjudge.cn'+endpoint, {timeout:25000});
  if (!response.ok()) throw new Error('Official read HTTP '+response.status());
  return response.json();
}

export async function readProblem(page, problemUrl) {
  const listPromise = page.waitForResponse(response => /\/api\/submissions\/user\/[^/]+\/problem\/[^/?]+(?:\?|$)/.test(response.url()), {timeout:25000});
  const detailPromise = page.waitForResponse(response => /\/api\/problems\/[^/?]+(?:\?|$)/.test(response.url()), {timeout:25000});
  // Attach rejection handlers immediately while navigation is in flight.
  const responses = Promise.all([listPromise, detailPromise]);
  responses.catch(()=>{});
  await page.goto(problemUrl, {waitUntil:'domcontentloaded',timeout:25000});
  const [listResponse,detailResponse] = await responses;
  if (!listResponse.ok() || !detailResponse.ok()) throw new Error('Official login or problem access required');
  const [records,problem] = await Promise.all([listResponse.json(),detailResponse.json()]);
  if (!Array.isArray(records)) throw new Error('Unknown official history pagination format');
  const userId=new URL(listResponse.url()).pathname.match(/^\/api\/submissions\/user\/([^/]+)\/problem\//)?.[1];
  return {records,problem,userId};
}

export async function readRanking(context, problem, wantedIds) {
  const rows=[], wanted=new Set(wantedIds);
  const observations=[];
  let testcases=[], pages=1;
  for(let page=1;page<=pages;page++) {
    const data=await fetchOfficial(context,`/api/problems/${problem._id}/ranking?page=${page}&size=100`);
    const fetchedAt=new Date().toISOString();
    const batch=data.rows??data.list;
    if(!Array.isArray(batch))throw new Error('Unknown official ranking format');
    if(page===1)testcases=data.testcases??[];
    pages=Number(data.pages??Math.ceil((data.total??batch.length)/(data.size??100)))||1;
    if(pages>500)throw new Error('Official ranking exceeds import bound');
    // A page is one real response, not an assumed atomic multi-page ranking.
    // Retain public score evidence across accounts, without users or source code.
    const hiddenIds=new Set((data.testcases??[]).filter(c=>c.hidden===true).map(c=>String(c._id??c.id)));
    const observation={page,source:'official_ranking_api',fetched_at:fetchedAt,
      testcases:(data.testcases??[]).map(c=>({_id:String(c._id??c.id),hidden:c.hidden===true,
        tbest:c.hidden===true?null:positive(c.tbest)})),
      rows:batch.map(r=>({official_id:r.submission_id??r.submission?._id??r._id,
        score:numeric(r.score),official_status:r.status,
        cases:(r.result??[]).map(c=>({testcase_id:String(c.testcase_id),
          status:hiddenIds.has(String(c.testcase_id))?'Hidden':c.testcase_status,
          time_us:c.testcase_status==='Hidden'||hiddenIds.has(String(c.testcase_id))?null:positive(c.time)}))}))};
    observation.snapshot_id='ranking-'+digest(stable(observation));
    observations.push(observation);
    for(const row of batch)if(wanted.has(row.submission_id??row.submission?._id??row._id))rows.push({...row,score_testcases:data.testcases??[]});
    if(page<pages)await pause(180);
  }
  return {rows,testcases,comparison_snapshot:{schema_version:1,official_problem_id:problem._id,
    score_mode:problem.score_mode,use_baseline:problem.use_baseline,code_template:problem.code_template,
    ranking_submission_mode:problem.ranking_submission_mode,pages:observations}};
}

export function normalizeRecord(detail, problem, ranking, problemId, problemUrl, fetchedAt) {
  const id=String(detail._id||'');
  if(!/^[0-9a-f]{24}$/.test(id) || String(detail.problem_id)!==String(problem._id))throw new Error('Official record/problem mismatch');
  const specs=(problem.testcases??[]).map(t=>t.testcase_id&&typeof t.testcase_id==='object'?t.testcase_id:t);
  const result=Array.isArray(detail.result)?detail.result:[];
  const byId=new Map(result.map(r=>[String(r.testcase_id),r]));
  const bests=new Map((ranking.testcases??[]).map(r=>[String(r._id??r.id),r]));
  const ordered=specs.length?specs:result.map(r=>({_id:r.testcase_id}));
  const cases=ordered.map((spec,index)=>{
    const testcaseId=String(spec._id??spec.id), row=byId.get(testcaseId)||{};
    const best=bests.get(testcaseId);
    const hidden=row.testcase_status==='Hidden'||best?.hidden===true;
    const status=hidden?'Hidden':row.testcase_status||(pass(detail.status)?'NotReported':'NotRun');
    const timeUs=hidden?null:positive(row.time);
    const tbest=hidden?null:positive(best?.tbest??row.best_time??row.bestTime);
    const ratio=numeric(row.precision_ratio);
    let score=null;
    if(!hidden && status!=='NotReported' && !pass(status))score=0;
    else if(timeUs&&tbest)score=100/(1+Math.log(Math.max(1,timeUs/tbest))/Math.log(1.5));
    return {index:index+1,testcase_id:testcaseId,official_case_id:spec.ID??null,status,
      time_us:timeUs,tbest_us:tbest,detail_tbest_us:hidden?null:positive(row.best_time??row.bestTime),
      error_percent:hidden||ratio===null?null:Math.min(1,Math.max(0,1-ratio))*100,
      score,score_source:'official_formula_from_official_timings',message:hidden?'':String(row.msg??'').slice(0,12000)};
  });
  const rankRow=(ranking.rows??[]).find(r=>(r.submission_id??r.submission?._id??r._id)===id);
  const referencePage=ranking.comparison_snapshot?.pages?.[0];
  const scorePage=ranking.comparison_snapshot?.pages?.find(p=>p.rows.some(r=>r.official_id===id));
  const serverScore=numeric(rankRow?.score??detail.score);
  const serverSource=numeric(rankRow?.score)!==null?'official_ranking_api':'official_detail_api';
  const supported=['npu_kernel_dev','custom_template'].includes(problem.code_template)&&!problem.use_baseline&&[0,1].includes(problem.score_mode);
  let calculated=null;
  const complete=finished(detail.status)&&(!pass(detail.status)||(cases.length>0&&
    result.length===cases.length&&byId.size===result.length&&
    (detail.total_testcase_count==null||detail.total_testcase_count===cases.length)&&
    cases.every(c=>pass(c.status)||c.status==='Hidden')));
  if(supported&&cases.length&&cases.every(c=>c.score!==null)&&complete) {
    calculated=cases.reduce((sum,c)=>sum+c.score,0)/cases.length;
    if(detail.valid===false || (problem.score_mode===1&&cases.some(c=>!pass(c.status))))calculated=0;
    calculated=Math.round((calculated+Number.EPSILON)*100)/100;
  }
  const files=detail.can_view_code===false?[]:(Array.isArray(detail.files)?detail.files:[]).filter(f=>typeof f.path==='string'&&typeof f.content==='string');
  const kernel=files.find(f=>f.path==='kernel.asc');
  const project=problem.code_template==='custom_template';
  const expectedFiles=editableProjectFiles(problemId,problem._id);
  const editable=project?files.filter(f=>expectedFiles.includes(f.path)):[];
  const projectComplete=expectedFiles.length>0&&editable.length===expectedFiles.length&&new Set(editable.map(f=>f.path)).size===expectedFiles.length;
  const record={data_origin:'official_cannjudge',official_id:id,official_display_id:detail.ID??null,problem_id:problemId,
    official_problem_id:problem._id,official_url:problemUrl+'/submission/'+id,create_time:detail.create_time,
    official_status:String(detail.status||'Unknown'),valid:detail.valid??null,passed:pass(detail.status),
    evaluation_complete:complete,
    passed_cases:cases.filter(c=>pass(c.status)).length,total_cases:cases.length,cases,
    score:serverScore??calculated,official_server_score:serverScore,official_formula_score:calculated,
    score_source:serverScore!==null?serverSource:calculated!==null?'official_formula_from_official_timings':'not_available',
    score_note:'官方榜单只返回代表提交总分；其余历史按本次抓取的官方耗时与官方 TBest 复算，未使用本地 OJ 数据。不是当时的历史榜单快照。',
    score_formula:'mean(100/(1+log(time/TBest)/log(1.5)))',fetched_at:fetchedAt,
    reference_snapshot_id:referencePage?.snapshot_id??null,reference_fetched_at:referencePage?.fetched_at??null,
    score_snapshot_id:serverSource==='official_ranking_api'?scorePage?.snapshot_id??null:null,
    ranking_status:referencePage?'fetched':'unavailable',
    submission_kind:project?'project':'kernel',
    source_available:project?projectComplete:Boolean(kernel),source_sha256:project?(projectComplete?bundleHash(editable):null):kernel?digest(kernel.content):null,
    source_hash_kind:project?'sorted editable [path, SHA256(LF content)] JSON bundle':'single_file_sha256',
    files:files.map(f=>({path:f.path,bytes:Buffer.byteLength(f.content),sha256:digest(f.content)})),
    provenance:{detail_api:'https://cannjudge.cn/api/submissions/'+id,
      ranking_api:`https://cannjudge.cn/api/problems/${problem._id}/ranking`,
      formula_url:'https://cannjudge.cn/js/pages/scoring-rules-content.js',time_unit:'microseconds'}};
  // Bind the published total to the result and TBest from its own ranking page.
  const rankCases=new Map((rankRow?.result??[]).map(c=>[String(c.testcase_id),c]));
  const rankBests=new Map((rankRow?.score_testcases??ranking.testcases??[]).map(c=>[String(c._id??c.id),c]));
  if(serverSource==='official_ranking_api'&&serverScore!==null&&supported&&complete&&detail.valid!==false
      &&(!rankRow.status||rankRow.status===record.official_status)
      &&rankCases.size===cases.length&&(rankRow.result??[]).length===cases.length
      &&cases.every(c=>{
        const r=rankCases.get(c.testcase_id);
        return r&&r.testcase_status===c.status&&(c.status==='Hidden'||positive(r.time)===c.time_us);
      })) {
    record.official_score_snapshot={official_id:id,official_problem_id:problem._id,
      score:serverScore,source:'official_ranking_api',fetched_at:fetchedAt,
      snapshot_id:scorePage?.snapshot_id??null,
      score_mode:problem.score_mode,use_baseline:problem.use_baseline,code_template:problem.code_template,
      official_status:record.official_status,rounding_unit:0.01,
      cases:cases.map(c=>({testcase_id:c.testcase_id,status:c.status,time_us:c.time_us,
        tbest_us:c.status==='Hidden'?null:positive(rankBests.get(c.testcase_id)?.tbest)}))};
  }
  return {record,files};
}

// Terminal detail is authoritative even when some case data is not yet public.
// Do not wait for the DOM's visible-case counter, source copies or ranking pages.
export async function pollOfficialResult(context, payload, problem, emit, options={}) {
  const deadline=Date.now()+(options.timeoutMs??12*60*1000);
  let previous;
  while(Date.now()<deadline) {
    const detail=await fetchOfficial(context,'/api/submissions/'+payload.official_id);
    if(detail._id!==payload.official_id)throw new Error('Official record identity mismatch');
    const fetchedAt=new Date().toISOString();
    // Strip source before normalization: hashing/archival belongs to the sync job.
    const {record}=normalizeRecord({...detail,files:[]},problem,{},payload.problem_id,payload.problem_url,fetchedAt);
    if(record.official_status!==previous) {
      emit({event:'progress',official_status:record.official_status,message:'官方评测：'+record.official_status});
      previous=record.official_status;
    }
    if(finished(record.official_status))return {...record,detail_fetched_at:fetchedAt,
      result_published_at:fetchedAt,source_verified:false,source_status:'pending',ranking_status:'pending'};
    await (options.pause??pause)(5000);
  }
  return null;
}

export async function cachedDetail(payload,item,previousItem) {
  if(!payload.incremental || !finished(item.status) || !/^[0-9a-f]{24}$/.test(item._id))return null;
  const directory=path.join(payload.archive_dir,item._id);
  try {
    const [record,detail,files]=await Promise.all(['record.json','raw-detail.json','files.json'].map(async name=>JSON.parse(await fs.readFile(path.join(directory,name),'utf8'))));
    const signature=digest(stable(item));
    if((record.history_signature??(previousItem?digest(stable(previousItem)):null))!==signature)return null;
    const fetched=record.detail_fetched_at??record.fetched_at;
    // Periodically re-read even unchanged terminal rows, to detect rejudges/permissions.
    const age=Date.now()-Date.parse(fetched);
    if(!Number.isFinite(age)||age<0||age>3600*1000)return null;
    if(detail._id!==item._id||record.official_id!==item._id||detail.status!==item.status||record.official_status!==item.status||record.evaluation_complete===false)return null;
    const sourceFiles=detail.can_view_code===false?[]:detail.files??[];
    if(files.length!==record.files.length||sourceFiles.length!==files.length)return null;
    for(const file of files) {
      if(typeof file.content!=='string'||record.files.find(f=>f.path===file.path)?.sha256!==digest(file.content)||
         sourceFiles.find(f=>f.path===file.path)?.content!==file.content)return null;
    }
    return {detail,detail_fetched_at:fetched};
  }catch{return null;}
}

export async function archiveRecord(context, payload, catalog, ranking, identity, problemId, problemUrl, emit, cached=null, historyItem=null) {
  const detail=cached?.detail??await fetchOfficial(context,'/api/submissions/'+identity);
  const fetchedAt=new Date().toISOString();
  const {record,files}=normalizeRecord(detail,catalog.problem,ranking,problemId,problemUrl,fetchedAt);
  record.detail_fetched_at=cached?.detail_fetched_at??fetchedAt;
  record.detail_cache_reused=Boolean(cached);
  record.source_status=record.source_available?'archived':'unavailable';
  if(historyItem)record.history_signature=digest(stable(historyItem));
  const directory=path.join(payload.archive_dir,identity);
  await fs.mkdir(directory,{recursive:true});
  // Original server response is retained privately; MCP only returns the sanitized record.
  if(!cached) {
    await atomicJson(path.join(directory,'raw-detail.json'),detail);
    await atomicJson(path.join(directory,'files.json'),files);
    const kernel=files.find(f=>f.path==='kernel.asc');
    if(kernel)await fs.writeFile(path.join(directory,'kernel.asc'),kernel.content);
  }
  await atomicJson(path.join(directory,'record.json'),record);
  emit({event:'record',record});
  return record;
}

export async function syncHistory(context,page,payload,emit,readers={readProblem,readRanking}) {
  const summaries=[],errors=[];
  for(const [problemId,slug] of Object.entries(payload.problems)) {
    const problemUrl=payload.problem_urls?.[problemId]||'https://cannjudge.cn/ccpc/ladder_2026_changchun/'+slug;
    emit({event:'progress',message:'读取官方历史：'+problemId});
    try {
      if(!validProblemUrl(problemUrl))throw new Error('Invalid official history target');
      const catalog=await readers.readProblem(page,problemUrl);
      const targeted=Array.isArray(payload.official_ids);
      const records=targeted?payload.official_ids.map(_id=>({_id})):[...new Map(catalog.records.map(r=>[r._id,r])).values()];
      let ranking={};
      try {ranking=await readers.readRanking(context,catalog.problem,records.map(r=>r._id));}
      catch(error) {
        if(!targeted)throw error;
        errors.push({problem_id:problemId,phase:'ranking',message:'官方榜单刷新失败；结果和源码仍单独归档'});
      }
      const p=catalog.problem;
      const info={data_origin:'official_cannjudge',problem_id:problemId,official_id:p._id,title:p.title,
        description:p.desc,official_url:problemUrl,code_template:p.code_template,cann_version:p.cann_version,
        kernel_pattern:p.kernel_pattern,iterations:p.iterations,score_mode:p.score_mode,use_baseline:p.use_baseline,
        ranking_submission_mode:p.ranking_submission_mode,testcases:ranking.testcases,
        total_submissions:catalog.records.length,fetched_at:new Date().toISOString(),
        ranking_snapshot:ranking.comparison_snapshot??null,
        reference_snapshot_id:ranking.comparison_snapshot?.pages?.[0]?.snapshot_id??null,
        reference_fetched_at:ranking.comparison_snapshot?.pages?.[0]?.fetched_at??null};
      if(!targeted||ranking.comparison_snapshot)emit({event:'problem',problem:info});
      const dir=path.join(payload.archive_dir,'problems',problemId);
      await fs.mkdir(dir,{recursive:true});
      let previous;
      try {previous=JSON.parse(await fs.readFile(path.join(dir,'snapshot.json'),'utf8'));}catch{}
      const previousRecords=new Map((previous?.history??[]).map(r=>[r._id,r]));
      let imported=0,fetched=0,reused=0;
      for(const item of records) {
        if(!/^[0-9a-f]{24}$/.test(item._id))throw new Error('Invalid official history ID');
        let cached;
        try {
          cached=await cachedDetail(payload,item,previousRecords.get(item._id));
          await archiveRecord(context,payload,catalog,ranking,item._id,problemId,problemUrl,emit,cached,item);
          imported++;if(cached)reused++;else fetched++;
        }
        catch(error){errors.push({problem_id:problemId,official_id:item._id,message:String(error.message).slice(0,160)});}
        if(!cached||imported===records.length)emit({event:'progress',message:`${problemId}：${imported} / ${records.length}，读取 ${fetched}，复用 ${reused}`,imported,fetched,reused,expected:records.length,problem_id:problemId});
        if(!cached)await pause(180);
      }
      if(!targeted)await atomicJson(path.join(dir,'snapshot.json'),{problem:catalog.problem,history:records,ranking});
      summaries.push({problem_id:problemId,expected:records.length,imported,fetched,reused});
    }catch(error){errors.push({problem_id:problemId,message:String(error.message).replace(/https?:\/\/\S+/g,'[URL]').slice(0,160)});}
  }
  emit({event:'done',status:errors.length?'partial':'completed',summaries,errors,
    message:errors.length?'部分官方历史未导入，可重试同步；不会提交源码':'当前账号的官方历史已全部同步'});
}

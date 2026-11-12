const $=id=>document.getElementById(id);
const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const readCache=new Map();
let csrf='',problem='',offset=0,total=0,selected=null,catalog=[],pendingRequest=false,bestsState='',submitEnabled=false;
let refreshController=null,refreshWork=null,refreshKey='',refreshVersion=0,detailController=null,detailVersion=0,selectedId=null;
let sourceController=null,sourceVersion=0,sourceRecord=null,toastTimer=null,jobState='';
let submissionKinds={},problemUrls={},editorProblem='',editorPath='',editorVersion=0,editorReady=false;
const editorDrafts=new Map();
const currentProblem=()=>catalog.find(p=>p.problem_id===problem);
const officialOnly=()=>currentProblem()?.official_only===true;
function renderProblemCatalog(){
  const selector=$('problem');
  for(const option of selector.options){const info=catalog.find(p=>p.problem_id===option.value);if(info)option.textContent=info.title+(info.contest_title?' · '+info.contest_title:'');}
  const info=currentProblem();
  $('problem-title').textContent=info?.title||problem;
  $('problem-context').textContent=[info?.contest_title,officialOnly()?'仅官方评测':null,Number.isInteger(info?.case_count)?info.case_count+' 个官方测试点':null].filter(Boolean).join(' · ');
  $('problem-context').classList.toggle('hidden',!$('problem-context').textContent);
  if(accountSettings)renderAccountControls(accountSettings);
}
const PAGE=20;
const prompt=`请优化【题目名】，全程只使用 CANNJudge 官方数据。
允许的 MCP 工具：list_official_accounts、get_official_settings、list_official_problems、get_official_problem、list_official_history、get_official_record、read_official_source、get_official_case_bests、get_official_connection、sync_official_history、get_official_sync、get_official_template、submit_official_project、submit_official_kernel、get_official_job、list_official_jobs。
禁止调用任何本地评测工具，包括 list_problems、get_problem、get_problem_guide、get_leaderboard、get_case_bests、get_submission、read_kernel、submit_kernel、get_scoring_rules；也不要读取本地 OJ 的数据、代码、估时、模拟分或基线。
先用 list_official_accounts 和 get_official_settings 确认账户及本地检查开关，查询历史与榜单时省略 account_id，合并所有已保存账号；提交时用 account_id 固定当前选定账号；没有我的明确要求不要切换账户或更改检查开关。再同步官方历史并等待完成，分页读取本题全部记录，查看分数最高与逐点最快的官方源码；读取源码时跟进 next_line，直到完整。
只用官方逐点耗时和正确性评价优化。区分 official_ranking_api 与 official_formula_from_official_timings；后者是按抓取时官方 TBest 复算，不是官方直接返回的历史总分。缺失字段如实说明，不能用本地数据补全。读取 score_breakdown 区分已知规格点和未知规格点的小计、均分及整题贡献；official_total 是官网完整总分；official_score_breakdown 使用同一榜单快照反算隐藏组合计与均分，aggregate_only=true 表示近似反算值。隐藏单点分数与耗时仍为 null；不同时间的总分与 TBest 不能混算。current_official_total 是共同 ranking 响应核实的当前分；official_total 保留原归档值，historical_official_totals 单列历史或未验证分，不作跨快照排名。前三 ranking_scope=known 时只比较已知点，不能说是整题总分榜。
代码必须泛化，不能硬编码测试答案或具体测试尺寸。实际提交遵守我的授权与次数限制，单文件题目使用 submit_official_kernel(source=...)，不传 local_submission_id；eigh 先 get_official_template，再以全部 4 个可编辑文件调用 submit_official_project。只读文件保持原样。本地检查默认开启，开启时必须完整通过才上传，失败应修复；只有我明确要求关闭时才能调用 set_official_local_validation(false)。关闭检查的任务直接上传，不得声称本地通过。本地检查耗时不能当作官方成绩。已排队任务保留创建时的账户和检查设置；同一请求重试复用 request_id；outcome_unknown 时先核查历史，禁止自动重发。
每次比较写清官方提交 ID、源码 SHA-256、总分来源、逐点耗时变化和抓取时间。`;
$('agent-prompt').value=prompt;
const date=value=>{const d=new Date(typeof value==='number'?value*1000:value);return value&&!Number.isNaN(d.valueOf())?d.toLocaleString('zh-CN',{hour12:false}):'—';};
const finite=value=>typeof value==='number'&&Number.isFinite(value);
const num=value=>finite(value)?value.toFixed(2):'—';
const duration=value=>!finite(value)||value<=0?'—':value>=1000?(value/1000).toFixed(3)+' ms':value.toFixed(2)+' μs';
const origin=value=>['official_ranking_api','official_detail_api'].includes(value)?'官方返回':value==='official_ranking_snapshot'?'官方历史快照':value==='official_formula_from_official_timings'?'官方数据复算':'未提供';
const recordSplit=row=>row.official_score_breakdown||(row.official_total?.available?null:row.score_breakdown);
const recordValue=row=>row.official_total?.available?row.official_total.score:row.score;
const recordOrigin=row=>row.official_total?.available&&row.official_total.historical?'历史 / 未重新验证 · '+origin(row.official_total.source):origin(row.official_total?.source||row.score_source);
const comparisonTotal=row=>row.current_official_total?.available?row.current_official_total:row.official_total;
const comparisonSplit=row=>row.current_official_total?.available?row.current_official_score_breakdown:recordSplit(row);
const scoreEvidence=total=>total?.available?`${total.comparable?'同一榜单已核验':'历史 / 未重新验证'} · 抓取 ${date(total.fetched_at)}${total.verified_at?' · 核验 '+date(total.verified_at):''} · ${total.tbest_comparison?.note||'缺少与公开 TBest 的共同快照证据。'}`:'';
const scoreGroups=value=>[value?.visible??value?.known,value?.hidden??value?.unknown];
const scorePoint=(time,reference)=>finite(time)&&time>0&&finite(reference)&&reference>0?100/(1+Math.max(0,Math.log(time)-Math.log(reference))/Math.log(1.5)):null;
function scoreTableCases(row){
  const snapshot=row.official_score_snapshot;
  if(row.official_score_breakdown?.reference_kind!=='official_ranking_snapshot'||!snapshot?.cases)return row.cases;
  const cases=new Map(snapshot.cases.map(c=>[c.testcase_id,c]));
  if(cases.size!==row.cases.length||!row.cases.every(c=>cases.has(c.testcase_id)))return row.cases;
  return row.cases.map(c=>{const saved=cases.get(c.testcase_id),status=String(saved.status||'').toLowerCase();return {...c,...saved,score:['pass','accepted'].includes(status)?scorePoint(saved.time_us,saved.tbest_us):status==='hidden'?null:0};});
}
const recordScoreContext=row=>{
  const hasTotal=row.official_total?.available||(['official_ranking_api','official_detail_api','official_ranking_snapshot'].includes(row.score_source)&&finite(row.score));
  return hasTotal?{unavailableLabel:'缺少同批评分数据',unavailableReason:'本条已有官方总分，但缺少匹配的公开点成绩或 TBest，暂不能反算隐藏合计。',unmatchedTotal:!row.official_score_breakdown}:
    {unavailableLabel:'缺少整题总分',unavailableReason:row.official_total?.note||'官网榜单通常只返回代表提交总分；本条尚未取得总分，不能由公开点推算隐藏合计。'};
};
function groupScore(group,compact=false,context={}){
  if(!group)return '—';
  const state=group.total_cases===0?'无此类点':group.available?'非加权计分':group.hidden_cases?(context.unavailableLabel||'缺少计算依据'):'待完善';
  const inferred=group.aggregate_only===true,prefix=inferred?'≈ ':'';
  const usable=group.available&&finite(group.contribution);
  const coverage=inferred?'由同一快照的总分反算':`${group.scored_cases} / ${group.total_cases} 点可计分`;
  const reason=!group.available&&group.total_cases?(context.unavailableReason||group.reason||''):'';
  if(compact)return `<span title="${esc(reason)}">${usable?prefix+num(group.contribution):group.total_cases===0?'—':esc(state)}</span><small>${group.total_cases} 点 · 均分 ${prefix}${num(group.mean_score)} / 100${inferred?' · 反算':''}</small>`;
  return `<div class="score-group ${usable?'':'unavailable'}"><span>${esc(group.label)}贡献 <small>/ ${num(group.contribution_max)}</small></span><strong>${usable?prefix+num(group.contribution):esc(state)}</strong><small>${group.total_cases} 点 · 组内均分 ${prefix}${num(group.mean_score)} / 100</small><small>${coverage}</small>${reason?'<small>'+esc(reason)+'</small>':''}</div>`;
}
function splitScores(value,context={}){
  if(!value)return '';
  const [visible,hidden]=scoreGroups(value),total=value.total,totalCases=total.total_cases;
  const groups=[{...visible,label:'公开点'},{...hidden,label:'隐藏点'}];
  const available=g=>!g.total_cases||(g.available&&finite(g.contribution));
  const contribution=g=>g.total_cases?g.contribution:0;
  const additive=!context.unmatchedTotal&&groups.every(available)&&total.available&&finite(total.score)&&Math.abs(groups.reduce((s,g)=>s+contribution(g),0)-total.score)<.015;
  const equation=additive?`<div class="score-equation"><span>公开点 ${num(contribution(groups[0]))}</span><b>+</b><span>隐藏点 ${groups[1].aggregate_only?'≈ ':''}${num(contribution(groups[1]))}</span><b>≈</b><strong>总分 ${num(total.score)} <small>/ 100</small></strong></div>`:'';
  const basis=value.fetched_at?'总分与拆分使用同一官方快照：'+esc(date(value.fetched_at))+'。':value.reference_kind==='latest_official_tbest'?'按最新同步的 TBest 复算；不能直接与历史官方总分相加对照。':'按该记录归档的 TBest 复算。';
  const note=context.unmatchedTotal?'尚无与上方官方总分匹配的拆分，以下仅为单独复算。':basis;
  return `${equation}<div class="score-groups">${groups.map(g=>groupScore(g,false,context)).join('')}</div><p class="score-split-note">${note} 每组贡献 = 组内均分 × 组内点数 ÷ 全部 ${totalCases} 点；两组贡献相加才是总分。${groups.some(g=>g.aggregate_only)?'“≈”表示隐藏合计由同一快照反算，不能还原隐藏单点。':''}未取得的分数显示缺失，不按零分计算；显示舍入可能相差 0.01。${value.score_mode===1?'本题要求全部通过后才计总分；未全通过时不能按贡献相加。':''}</p>`;
}
function dataLabel(spec){
  const known=value=>value!==undefined&&value!==null;
  const tensorNames=['x','index','value','y'];
  const tensors=tensorNames.filter(name=>Array.isArray(spec?.[name+'_shape'])||known(spec?.[name+'_dtype']));
  if(tensors.length){
    const parts=tensors.map(name=>{
      const shape=spec[name+'_shape'],dtype=spec[name+'_dtype']??(name==='index'?null:spec.dtype);
      return `${name} ${dtype||'dtype 未确认'} ${Array.isArray(shape)?'['+shape.join(', ')+']':'shape 未确认'}`;
    });
    if(known(spec.eps))parts.push('eps '+spec.eps);
    if(known(spec.reduce))parts.push('reduce '+spec.reduce);
    const dim=spec.dim_raw??spec.raw_dim??spec.dim;
    if(known(dim))parts.push((tensors.includes('index')||tensors.includes('value')||known(spec.dim_raw)||known(spec.raw_dim)?'dim（原始） ':'dim ')+dim);
    if(known(spec.include_self))parts.push('include_self '+spec.include_self);
    return parts.join(' · ');
  }
  return Array.isArray(spec?.a_shape)&&Array.isArray(spec?.b_shape)?`${spec.dtype||'dtype 未确认'} · A [${spec.a_shape.join(', ')}] · B [${spec.b_shape.join(', ')}]`:Array.isArray(spec?.tree_points_shape)&&Array.isArray(spec?.line_segments_shape)?`${spec.dtype||'dtype 未确认'} · 点 [${spec.tree_points_shape.join(', ')}] · 线段 [${spec.line_segments_shape.join(', ')}]`:Array.isArray(spec?.shape)?`${spec.dtype||'dtype 未确认'} [${spec.shape.join(', ')}]${known(spec.dim)?` · dim ${spec.dim}`:''}${known(spec.eps)?` · eps ${spec.eps}`:''}${known(spec.k)?` · k ${spec.k}`:''}${known(spec.largest)?` · largest ${spec.largest}`:''}${spec.UPLO?' · UPLO '+spec.UPLO:''}`:spec?.n?`n = ${spec.n} · 矩阵数 ${spec.matrix_count??'待确认'} · 完整 shape 待确认`:'规格未确认';
}
function caseSpecsChanged(record,cases){
  if(!Array.isArray(cases))return false;
  const specs=new Map(cases.map(c=>[c.testcase_id,JSON.stringify(c.data_spec??null)]));
  return record.cases?.some(c=>specs.has(c.testcase_id)&&specs.get(c.testcase_id)!==JSON.stringify(c.data_spec??null))||false;
}
const jobStates={queued:'等待提交前检查',validating:'本地检查中',submitting:'提交官方中',submitted:'官方已接收',judging:'官方评测中',completed:'已完成',local_failed:'本地未通过 · 未上传官方',failed:'提交失败',login_required:'官方需登录',rate_limited:'官方额度或冷却限制',outcome_unknown:'官方结果待核实',awaiting_result:'等待官方结果'};
const localStates={queued:'排队中',connecting:'连接中',waiting:'等待设备',generating:'准备数据',compiling:'编译中',running:'运行中',accepted:'全部通过',compatibility_mismatch:'参考兼容未通过',wrong_answer:'答案错误',compile_error:'编译失败',runtime_error:'运行失败',illegal:'合规检查未通过',infrastructure_error:'环境异常'};
function specCell(spec){
  let outputs=Array.isArray(spec?.solution_shape)&&Array.isArray(spec?.residuals_shape)?`<small>${esc('solution ['+spec.solution_shape.join(', ')+'] · residuals '+(spec.residuals_shape.includes(0)?'空张量':'['+spec.residuals_shape.join(', ')+']'))}</small>`:'';
  if(!outputs&&Array.isArray(spec?.output_shape))outputs=`<small>${esc('输出 ['+spec.output_shape.join(', ')+']')}</small>`;
  else if(!outputs&&finite(spec?.output_elements)&&!Array.isArray(spec?.tree_points_shape))outputs=`<small>输出元素数 ${Number(spec.output_elements).toLocaleString()}</small>`;
  return `${esc(dataLabel(spec))}${outputs}<small>${spec?(Array.isArray(spec.tree_points_shape)&&finite(spec.output_elements)?'输出点数 = '+Number(spec.output_elements).toLocaleString()+' · ':finite(spec.elements)?'N = '+Number(spec.elements).toLocaleString()+' · ':'')+esc(spec.evidence?.label||'规格记录'):'官方详情未返回尺寸'}</small>`;
}
function validationInfo(v){
  if(!v)return '';
  if(v.required===false)return '<div class="local-validation skipped"><strong>本地检查已关闭</strong><p class="small">此任务直接提交官方，未运行本地正确性检查。</p></div>';
  const percent=v.total_cases>0?Math.max(0,Math.min(100,100*(v.completed_cases||0)/v.total_cases)):0;
  return `<div class="local-validation"><strong>本地检查 · ${esc(v.ssh_host||'npu-worker')}</strong><p class="small">${esc(localStates[v.status]||v.status)} · ${v.passed_cases??0} / ${v.total_cases??'—'} 通过</p><div class="progress-track"><i style="width:${percent}%"></i></div>${v.message?`<p class="small">${esc(v.message)}</p>`:''}${v.failed_cases?.map(c=>`<p class="small">点 ${esc(c.index)} · ${esc(localStates[c.status]||c.status)} · ${esc(c.message||'')}</p>`).join('')||''}${v.submission_id?`<a href="/api/submissions/${esc(v.submission_id)}" target="_blank" rel="noopener">完整检查报告（JSON） ↗</a>`:''}<small>本地正确性检查，非官方成绩</small></div>`;
}
function toast(text){clearTimeout(toastTimer);$('toast').textContent=text;$('toast').style.display='block';toastTimer=setTimeout(()=>$('toast').style.display='none',6000);}
function pageError(text=''){$('page-error-text').textContent=text;$('page-error').classList.toggle('hidden',!text);}
async function api(url,options={}) {
  const read=!options.method||options.method==='GET';
  if(read)url=accountUrl(url);
  const cacheMs=/\/api\/official\/(accounts|problems)(?:[/?]|$)/.test(url)?45000:/\/case-bests[?]/.test(url)?30000:0;
  const cached=readCache.get(url);if(read&&cacheMs&&cached&&Date.now()-cached.at<cacheMs)return cached.value;
  const signal=options.signal?AbortSignal.any([options.signal,AbortSignal.timeout(30000)]):AbortSignal.timeout(30000);
  const response=await fetch(url,{...options,signal,headers:{'Content-Type':'application/json','X-CSRF-Token':csrf,...options.headers}});
  if(response.status===401){$('app').classList.add('hidden');$('boot-status').classList.add('hidden');$('logout').classList.add('hidden');$('login').classList.remove('hidden');throw new Error('请先登录');}
  const body=await response.json().catch(()=>null);if(!response.ok)throw new Error(typeof body?.detail==='string'?body.detail:`请求失败（HTTP ${response.status}），请重试`);if(!body)throw new Error('服务器返回了无效数据，请重试');if(read&&cacheMs)readCache.set(url,{at:Date.now(),value:body});if(!read)readCache.clear();return body;
}
function run(action){return async event=>{event?.preventDefault();try{await action(event);}catch(error){if(error.name!=='AbortError')toast(error.message);}};}
function refresh({force=false}={}){
  if($('app').classList.contains('hidden')||!problem)return Promise.resolve();
  if(force)readCache.clear();
  const key=activeAccount+':'+problem+':'+offset+':'+$('order').value;
  if(refreshWork&&refreshKey===key&&!force)return refreshWork;
  refreshController?.abort();const controller=new AbortController();refreshController=controller;
  refreshKey=key;const version=++refreshVersion;const options={signal:controller.signal};
  $('refresh').textContent='刷新中…';
  refreshWork=(async()=>{try {
    const results=await Promise.allSettled([
      api('/api/official/problems',options),api(`/api/official/history?problem_id=${encodeURIComponent(problem)}&offset=${offset}&limit=${PAGE}&order=${$('order').value}&summary_only=true`,options).then(value=>{if(version===refreshVersion){renderHistoryPage(value);if(!selectedId&&value.submissions.length)select(value.submissions[0].official_id).catch(e=>pageError(e.message));}return value;}),
      api(`/api/official/jobs?problem_id=${encodeURIComponent(problem)}`,options),
      api(`/api/official/case-bests?problem_id=${encodeURIComponent(problem)}&summary_only=true`,options),api('/api/official/accounts',options),
      ['erfinv','pdist','topk'].includes(problem)?api(`/api/official/problems/${encodeURIComponent(problem)}`,options):Promise.resolve(null)]);
    if(version!==refreshVersion)return;
    if(results[4].status==='fulfilled'){
      const changed=renderAccountControls(results[4].value);
      if(changed){await refresh({force:true});return;}
    }
    const errors=results.filter(r=>r.status==='rejected'&&r.reason.name!=='AbortError').map(r=>r.reason.message);
    pageError(errors.length?'部分数据读取失败：'+[...new Set(errors)].join('；'):'');
    const [info,history,jobs,bests]=results.map(r=>r.status==='fulfilled'?r.value:null);
    if(info){
    catalog=info.problems;
    renderProblemCatalog();
    $('archive-count').textContent=catalog.map(p=>`${p.title} ${p.imported_submissions} 条`).join(' · ')||'尚未同步';
    const sync=info.latest_sync;
    $('sync-status').textContent=sync?`${sync.terminal?'上次同步':'同步中'}：${sync.result.message} · ${date(sync.updated)}`:'可同步当前账号的完整官方历史';
    $('sync').disabled=!!sync&&!sync.terminal;
    const auto=info.auto_sync;
    $('auto-sync-status').textContent=auto?.enabled?
      `自动同步已开启 · 每轮结束 ${auto.interval_seconds} 秒后检查官网新记录 · ${auto.last_status==='running'?'本轮同步中':auto.last_status==='queued'?'等待当前账号队列空闲':'下次检查 '+date(auto.next_due)}${auto.last_success?' · 最近成功 '+date(auto.last_success):''}${auto.failures?' · 连接失败，正在延后重试':''}`:'自动同步未开启';
    }
    if(bests)renderBests(bests);
    if(results[5].status==='fulfilled')renderInputSpecifications(results[5].value);
    if(history)renderHistoryPage(history);
    if(jobs&&JSON.stringify(jobs.jobs)!==jobState){jobState=JSON.stringify(jobs.jobs);
      $('jobs').innerHTML=jobs.jobs.map(j=>`<div class="history-item"><strong>${esc(j.label)}</strong><span class="job-state ${j.status==='completed'?'completed':['queued','validating','submitting','submitted','judging'].includes(j.status)?'active':'failed'}">${esc(jobStates[j.status]||j.status)}</span><p class="small muted">${esc(j.account?.label||j.account_id||'')} · ${esc(date(j.created))}</p><p class="muted small">${esc(j.result.message)}</p>${validationInfo(j.result.local_validation)}${j.result.official_id?`<button class="plain" data-job-record="${esc(j.result.official_id)}">查看官方记录 ↗</button>`:''}${j.status==='awaiting_result'?`<button class="secondary" data-refresh="${esc(j.id)}">继续查询结果</button>`:''}<small>${esc(j.id)}</small></div>`).join('')||'<p class="empty">暂无官方提交任务</p>';
    }
    if(jobs?.queues){
      const text=jobs.queues.map(q=>`<p class="small"><strong>${esc(q.account_label)}</strong> · ${q.ready?(q.worker_alive?'独立队列在线':'队列恢复中'):'需登录'} · 排队 ${q.queued} · 执行中 ${q.active_jobs.length}</p>`).join('');
      if($('official-queues').innerHTML!==text)$('official-queues').innerHTML=text;
    }
    if(!selectedId&&history?.submissions.length)await select(history.submissions[0].official_id);
    else if(!selectedId&&history&&!history.submissions.length)$('result-title').textContent='该题暂无官方记录，请先同步';
    // Refresh only the same selected record; never let a background read undo a click.
    else if(selected&&history&&!$('source-dialog').open){const entry=history.submissions.find(r=>r.official_id===selectedId);if((entry&&(entry.fetched_at!==selected.fetched_at||entry.score!==selected.score||entry.data_spec_note!==selected.data_spec_note))||caseSpecsChanged(selected,entry?.cases||bests?.case_bests))await select(selectedId,{background:true});}
  }catch(error){if(error.name!=='AbortError'&&version===refreshVersion)pageError(error.message);}
  finally{if(version===refreshVersion){refreshWork=null;$('refresh').textContent='刷新';}}})();
  return refreshWork;
}
function renderHistoryPage(history){
    total=history.total;
    const historyHtml=history.submissions.map(r=>`<tr data-history-record="${esc(r.official_id)}" class="${r.official_id===selectedId?'selected':''}"><td>#${esc(r.official_display_id||r.official_id.slice(-8))}<small>${esc(r.account_label||r.account_id||'')}</small></td><td>${esc(date(r.create_time))}</td><td>${esc(r.official_status)}</td><td class="numeric">${num(recordValue(r))}</td><td class="numeric">${groupScore(scoreGroups(recordSplit(r))[0],true,recordScoreContext(r))}</td><td class="numeric">${groupScore(scoreGroups(recordSplit(r))[1],true,recordScoreContext(r))}</td><td class="score-origin">${recordOrigin(r)}<small>${esc(date(r.official_total?.fetched_at||r.fetched_at))}</small></td><td class="numeric">${r.passed_cases} / ${r.total_cases}</td><td><button class="plain" data-record="${esc(r.official_id)}">查看</button></td></tr>`).join('')||'<tr><td colspan="9" class="empty">暂无官方记录，可点击上方同步</td></tr>';
    if($('history').innerHTML!==historyHtml)$('history').innerHTML=historyHtml;
    $('page-info').textContent=`${total?offset+1:0}–${Math.min(offset+PAGE,total)} / ${total}`;
    $('prev').disabled=offset===0;$('next').disabled=offset+PAGE>=total;
}
function renderInputSpecifications(info){
  const card=$('input-specifications');
  if(!card)return;
  card.classList.toggle('hidden',!info);
  if(!info)return;
  const data=info.data_specifications||{},mapped=data.cases||[],unmapped=data.unmapped_specifications||[];
  const evidence=data.evidence||{},profile=evidence.input_profile||{};
  $('input-spec-count').textContent=`${mapped.length} 个公开点 · ${unmapped.length} 种未映射规格`;
  $('input-spec-note').textContent=evidence.scope||data.note||'仅列出已确认的规格。';
  const rows=(specs,known)=>specs.map((s,i)=>{
    const flat={...s,...s.parameters},shape=s.shape||[],elements=s.elements??(shape.length?shape.reduce((a,b)=>a*b,1):null);
    const bytes=elements&&({'float32':4,'float16':2,'bfloat16':2}[s.dtype])?elements*({'float32':4,'float16':2,'bfloat16':2}[s.dtype]):null;
    const output=s.output_elements??(info.problem_id==='erfinv'?elements:info.problem_id==='pdist'&&shape.length===2?shape[0]*(shape[0]-1)/2:info.problem_id==='topk'&&shape.length===2&&flat.k?shape[0]*flat.k:null);
    const id=s.evidence?.official_id,link=id&&/^[0-9a-f]{24}$/.test(id)?`<a class="plain" target="_blank" rel="noopener" href="https://cannjudge.cn/ccpc/ladder_2026_xiamen/${encodeURIComponent(info.problem_id)}/submission/${id}">诊断证据 ↗</a>`:'';
    return `<tr><td>${known?'公开点 '+String(s.index).padStart(2,'0'):'规格 '+(i+1)+'<small>官方点号未知</small>'}</td><td>${esc(dataLabel(flat))}</td><td class="numeric">${elements==null?'—':Number(elements).toLocaleString()}<small>${bytes==null?'':(bytes/1048576).toFixed(3)+' MiB 输入'}</small></td><td class="numeric">${output==null?'—':Number(output).toLocaleString()}</td><td>${link||'已确认规格'}</td></tr>`;
  }).join('');
  $('input-known-rows').innerHTML=rows(mapped,true)||'<tr><td colspan="5" class="empty">暂无公开点规格</td></tr>';
  $('input-unmapped-rows').innerHTML=rows(unmapped,false)||'<tr><td colspan="5" class="empty">暂无已确认的未映射规格</td></tr>';
  $('input-unmapped-note').textContent=data.unmapped_specifications_note||'这些规格尚未对应到官方隐藏点号；下表顺序不是官方测试顺序。';
  $('input-features').innerHTML=(profile.features||[]).map(f=>`<div class="input-feature"><strong>${esc(f.title)}</strong><p>${esc(f.detail)}</p>${/^[0-9a-f]{24}$/.test(f.official_id||'')?`<a class="plain" target="_blank" rel="noopener" href="https://cannjudge.cn/ccpc/ladder_2026_xiamen/${encodeURIComponent(info.problem_id)}/submission/${f.official_id}">官方证据 ↗</a>`:''}</div>`).join('');
  $('input-unknown').textContent=profile.unknown||'原始输入值和随机种子未知。';
  $('input-local-model').textContent=profile.local_model||'暂无合成模型说明。';
}
function bestCode(row){
  const estimate=row.estimated_total_score;
  const project=row.submission_kind==='project';
  const split=comparisonSplit(row),total=comparisonTotal(row);
  const [visible,hidden]=scoreGroups(split);
  const parts=split?`<small>公开点贡献 ${num(visible?.contribution)} · 隐藏点贡献 ${hidden?.aggregate_only?'≈ ':''}${num(hidden?.contribution)}</small><small>${split.fetched_at?'拆分与总分同一快照 · '+esc(date(split.fetched_at)):'公开点单独复算；隐藏点未取得'}</small>`:total?.available?`<small>缺少匹配快照，无法拆分此总分</small><small>总分抓取于 ${esc(date(total.fetched_at))}</small>`:'';
  const scoreLabel=total?.available?(total.comparable?'同批官方完整分':'历史 / 未验证总分'):'整份预计分';
  const score=total?.available?total.score:estimate?.available?estimate.score:null;
  return `<div class="best-code"><strong>#${esc(row.official_display_id??row.official_id.slice(-8))}</strong><small>${esc(row.account_label||row.account_id||'')}</small><span class="best-estimate" title="${esc(total?.available?total.note:estimate?.reason||estimate?.note||'按最新 TBest 复算')}">${scoreLabel} <b>${num(score)}</b></span><small>${esc(scoreEvidence(total))}</small>${total?.snapshot_id?`<small title="${esc(total.snapshot_id)}">快照 ${esc(total.snapshot_id.slice(0,20))}…</small>`:''}${parts}<small>可见 ${row.passed_cases??'—'} 点通过 / 全部 ${row.total_cases??'—'} 点 · ${esc(row.official_status)}</small><small title="${esc(row.source_sha256)}">SHA ${esc(row.source_sha256?.slice(0,12)||'源码不可用')}${row.source_sha256?'…':''}</small><div class="best-actions"><button class="plain" data-best-record="${esc(row.official_id)}">记录 ↗</button>${row.source_available?`<button class="plain" data-best-source="${esc(row.official_id)}">源码</button><a class="plain" href="${esc(accountUrl(`/api/official/history/${row.official_id}/${project?'project':'source'}`,row.account_id))}" download="${project?problem+'-project.json':'kernel.asc'}">下载</a>`:'<span class="muted small">官方未提供源码</span>'}</div></div>`;
}
function renderBests(board){
  const state=JSON.stringify([board.problem_id,board.case_bests,board.note,board.projection,board.leaderboard,board.best_official_submission,board.hidden_leaderboard,board.historical_official_totals,board.public_reference]);
  if(state===bestsState)return;
  bestsState=state;
  const p=board.projection;
  $('projection-score-breakdown').innerHTML=splitScores(p?.score_breakdown,{unavailableLabel:'无法逐点组合',unavailableReason:'各隐藏点对应的最佳代码未知，不能计算逐点组合分。单份代码的隐藏合计请看上方隐藏点榜单或记录详情。'});
  $('projected-score').textContent=p?.available?num(p.score):'—';
  $('projected-coverage').textContent=p?`${p.covered_cases} / ${p.total_cases} 点有有效成绩 · ${p.source_count??0} 份最佳来源`:'等待更新预计分数据';
  $('projection-note').textContent=p?.available?p.note:p?.reason||'尚无完整官方数据，暂不估算。';
  const bestOfficial=board.best_official_submission;
  const bestSingle=p?.best_single_submission||bestOfficial;
  $('best-single-label').textContent=p?.best_single_submission?'整份最佳 · 与组合分同一口径':bestOfficial?'同一榜单完整分最高 · '+date(comparisonTotal(bestOfficial)?.verified_at):'当前整份最佳 · 最新复算';
  $('best-single-score').textContent=num(p?.best_single_submission?.score??(bestOfficial?comparisonTotal(bestOfficial)?.score:null));
  $('projected-gain').textContent=finite(p?.gain_over_best_single)?(p.gain_over_best_single>=0?'+':'')+num(p.gain_over_best_single):'—';
  $('best-single-record').classList.toggle('hidden',!bestSingle);
  $('best-single-record').dataset.record=bestSingle?.official_id||'';
  $('bests-note').textContent=board.note+(board.public_reference?' 公开 TBest 抓取于 '+date(board.public_reference.fetched_at)+'；'+board.public_reference.note:'');
  const ranked=board.leaderboard;
  $('official-top-title').textContent=ranked?.ranking_scope==='known'?'官方记录 · 已知点前三':ranked?.ranking_scope==='official_total'?'同一榜单完整分 · 前三':'官方记录 · 前三';
  $('ranking-score-label').textContent=ranked?.ranking_scope==='known'?'已知点均分 / 100':ranked?.ranking_scope==='official_total'?'同批完整分 / 100':'最新复算分 / 100';
  $('official-top-note').textContent=(ranked?.note||'等待同步完整官方记录')+(ranked?.verified_at?' 核验于 '+date(ranked.verified_at)+' · 快照 '+ranked.snapshot_id:'');
  $('official-top-count').textContent=ranked?ranked.total_eligible+(ranked.ranking_scope==='official_total'?' 份同批核验记录':' 份可比记录'):'—';
  $('official-top-rows').innerHTML=ranked?.entries?.length?ranked.entries.map(r=>`<tr><td>${String(r.rank).padStart(2,'0')}</td><td>${bestCode(r)}</td><td class="numeric"><strong>${num(r.ranking_score??r.estimated_total_score?.score)}</strong></td><td class="numeric">${num(recordValue(r))}<small>${esc(recordOrigin(r))}</small></td><td>${r.passed_cases??'—'} / ${r.total_cases??'—'}<small>${esc(date(r.create_time))}</small></td></tr>`).join(''):'<tr><td colspan="5" class="empty">暂无可比的完整总分或已知点分数，等待同步</td></tr>';
  const history=board.historical_official_totals;
  $('official-total-history').classList.toggle('hidden',!history?.entries?.length);
  $('official-total-history-note').textContent=history?.note||'';
  $('official-total-history-rows').innerHTML=(history?.entries||[]).map(r=>`<tr><td>${bestCode({...r,current_official_total:null})}</td><td class="numeric">${num(r.official_total.score)}<small>${esc(r.official_total.source)}</small></td><td>${esc(date(r.official_total.fetched_at))}<small>${esc(r.official_total.note)}</small></td></tr>`).join('');
  const hidden=board.hidden_leaderboard;
  $('hidden-top-note').textContent='显示同一已核验榜单中公开点与隐藏点对完整分的贡献。'+(hidden?.note||'缺少完整总分或匹配快照时不反算。');
  $('hidden-top-count').textContent=hidden?(hidden.total_eligible+' 份可拆分记录'):'—';
  const hiddenRow=r=>{const historical=r.rank==null;if(historical)r={...r,current_official_total:null};const visible=scoreGroups(comparisonSplit(r))[0];return `<tr><td>${r.rank==null?'—':String(r.rank).padStart(2,'0')}</td><td>${bestCode(r)}<small>快照 ${esc(date(r.score_snapshot_at))}</small></td><td class="numeric">${num(visible?.contribution)}<small>${visible?.total_cases??'—'} 点 · 均分 ${num(visible?.mean_score)}</small></td><td class="numeric"><strong>≈ ${num(r.hidden_score.contribution)}</strong><small>${r.hidden_score.total_cases} 点 · 均分 ≈ ${num(r.hidden_score.mean_score)}</small></td><td class="numeric">${num(comparisonTotal(r)?.score)}</td></tr>`;};
  $('hidden-top-rows').innerHTML=hidden?.entries?.length?hidden.entries.map(hiddenRow).join(''):'<tr><td colspan="5" class="empty">暂无可反算的隐藏点合计；没有完整总分的提交不参与排名</td></tr>';
  $('hidden-history').classList.toggle('hidden',!hidden?.historical_entries?.length);
  $('hidden-history-rows').innerHTML=hidden?.historical_entries?.map(hiddenRow).join('')||'';
  const expanded=new Set([...$('case-bests').querySelectorAll('details[open]')].map(d=>d.dataset.case));
  $('case-bests').innerHTML=board.case_bests.map(c=>`<tr><td>${String(c.index).padStart(2,'0')}</td><td>${specCell(c.data_spec)}</td><td class="numeric"><strong>${num(c.score)}</strong></td><td class="numeric">${duration(c.time_us)}<small>TBest ${duration(c.tbest_us)}</small></td><td>${c.submissions.length?bestCode(c.submissions[0])+(c.submissions.length>1?`<details data-case="${esc(c.testcase_id)}" ${expanded.has(c.testcase_id)?'open':''}><summary>另有 ${c.submissions.length-1} 份并列最高分</summary>${c.submissions.slice(1).map(bestCode).join('')}</details>`:''):'暂无有效通过记录'}</td></tr>`).join('')||'<tr><td colspan="5" class="empty">尚无官方测试点记录，等待同步</td></tr>';
}
function highlightSelection(){document.querySelectorAll('[data-history-record]').forEach(row=>row.classList.toggle('selected',row.dataset.historyRecord===selectedId));}
function resetDetail(message='请选择一条官方记录'){
  detailController?.abort();detailVersion++;selected=null;selectedId=null;
  $('result-title').textContent=message;$('detail-status').textContent='';$('record-content').classList.add('hidden');
  $('official-link').classList.add('hidden');$('show-source').disabled=true;$('download').classList.add('hidden');
}
async function select(id,{background=false}={}){
  const requestedProblem=problem;const version=++detailVersion;
  detailController?.abort();const controller=new AbortController();detailController=controller;
  if(!background){selected=null;selectedId=id;highlightSelection();$('result-title').textContent='正在读取官方记录…';$('record-content').classList.add('hidden');$('official-link').classList.add('hidden');$('show-source').disabled=true;$('detail-status').textContent='加载中…';}
  try{
  const row=await api('/api/official/history/'+id+'?summary_only=true',{signal:controller.signal});
  if(version!==detailVersion||selectedId!==id||requestedProblem!==problem||row.problem_id!==problem)return null;
  selected=row;
  $('record-content').classList.remove('hidden');$('detail-status').textContent='';
  $('result-title').textContent=`${row.problem_id} · #${row.official_display_id??row.official_id}`;
  $('score').textContent=finite(recordValue(row))?num(recordValue(row)):'未取得';$('passed').textContent=`${row.passed_cases} / ${row.total_cases}`;
  $('record-score-breakdown').innerHTML=splitScores(recordSplit(row),recordScoreContext(row))||(row.official_total?.available?'<p class="score-split-note">此官方总分缺少匹配的公开点与 TBest 快照，暂时无法拆分。下方逐点复算不用于解释这个历史总分。</p>':'');
  $('submitted-at').textContent=date(row.create_time)+(row.account_label?' · '+row.account_label:'');
  $('score-note').textContent=`${recordOrigin(row)} · ${row.official_total&&(row.official_total.available||!finite(row.score))?(row.official_total.note+' · '+scoreEvidence(row.official_total)):row.score_note||''}${row.diagnostic_submission?' '+row.diagnostic_note:''}`;
  $('sha').textContent='SHA-256：'+(row.source_sha256||'官方未提供源码');
  $('fetched-at').textContent='抓取时间：'+date(row.fetched_at)+'。'+(row.data_spec_note||'');
  $('official-link').href=row.official_url;$('official-link').classList.remove('hidden');
  const shownCases=scoreTableCases(row);
  if($('cases-score-basis'))$('cases-score-basis').textContent=shownCases!==row.cases?'逐点分数与上方总分拆分使用同一官方快照：'+date(row.official_score_breakdown.fetched_at)+'。':'逐点分数使用该记录归档的耗时与 TBest；没有匹配快照时，不用于拆分历史官方总分。';
  $('cases').innerHTML=shownCases.map(c=>`<tr><td>${String(c.index).padStart(2,'0')}</td><td>${specCell(c.data_spec)}</td><td>${esc(c.status)}</td><td class="numeric">${finite(c.error_percent)?num(c.error_percent)+'%':'—'}</td><td class="numeric">${duration(c.time_us)}</td><td class="numeric">${duration(c.tbest_us)}</td><td class="numeric">${num(c.score)}</td></tr>`).join('');
  $('diagnostics').innerHTML=row.cases.filter(c=>c.message).map(c=>`<details><summary>测试点 ${c.index} · ${esc(c.status)} 诊断</summary><pre>${esc(c.message)}</pre></details>`).join('');
  $('show-source').disabled=!row.source_available;
  const project=row.submission_kind==='project';
  $('download').classList.toggle('hidden',!row.source_available);$('download').href=accountUrl('/api/official/history/'+id+(project?'/project':'/source'),row.account_id);
  $('download').download=project?row.problem_id+'-project.json':'kernel.asc';$('download').textContent=project?'下载完整工程 JSON':'下载 kernel.asc';
  return row;
  }catch(error){if(error.name==='AbortError')return null;if(version===detailVersion){$('detail-status').textContent=error.message;$('result-title').textContent='记录加载失败，请重新选择';}throw error;}
}
async function source(){
  if(!sourceRecord)return;
  sourceController?.abort();const controller=new AbortController();sourceController=controller;
  const version=++sourceVersion;const id=sourceRecord.official_id;
  $('source-status').textContent='正在读取归档源码…';$('source-code').textContent='';
  try{
    const response=await fetch(accountUrl(`/api/official/history/${id}/source?file_path=${encodeURIComponent($('source-file').value)}`,sourceRecord.account_id),{signal:controller.signal});
    if(!response.ok)throw new Error('无法读取归档源码，请重新登录或同步');
    const text=await response.text();if(version!==sourceVersion||id!==sourceRecord?.official_id)return;
    $('source-code').textContent=text;$('source-status').textContent='官方归档源码 · SHA-256 已由服务端校验';
  }catch(error){if(error.name==='AbortError')return;if(version===sourceVersion)$('source-status').textContent=error.message;throw error;}
}
async function showSource(row){
  if(!row?.source_available)return;
  sourceRecord=row;
  $('source-title').textContent=`官方源码 · #${row.official_display_id??row.official_id.slice(-8)}`;
  $('source-file').innerHTML=(row.files||[]).map(f=>`<option value="${esc(f.path)}">${esc(f.path)}</option>`).join('');
  if(row.files?.some(f=>f.path==='kernel.asc'))$('source-file').value='kernel.asc';
  if(!$('source-dialog').open)$('source-dialog').showModal();
  await source();
}
function updateSubmitButton(){
  const unavailable=officialOnly()&&localValidationRequired;
  $('submit').disabled=pendingRequest||!submitEnabled||!editorReady||unavailable;
  $('submit').textContent=pendingRequest?'正在保存提交请求…':unavailable?'请先关闭本地检查':localValidationRequired?'本地检查后提交官方 →':'直接提交官方 →';
  if(officialOnly()){
    $('policy-title').textContent='仅官方评测';
    $('policy-description').textContent='本题直接使用 CANNJudge 官方提交、分数和逐点最佳记录，未配置本地评测。'+(unavailable?'当前本地检查开关开启，请关闭后再提交。':'新任务将直接上传官方，消耗官网提交额度。');
    $('validation-setting-note').textContent='本题没有本地测试集。此开关仍是网页与 MCP 共用设置；开启时本题不会上传。';
    $('submit-note').textContent=unavailable?'关闭上方“提交前本地正确性检查”后可提交本题；其他题目的设置也会随之改变。':(submissionKinds[problem]==='project'?'仅提交可编辑工程文件。':'仅提交 kernel.asc。')+'官方成绩与逐点最优会随评测结果同步；未公开的数据规格保持未知。';
  }
}
async function start(){
  const session=await api('/api/session');csrf=session.csrf||'';
  const status=await api('/api/official/connection');
  submissionKinds=status.submission_kinds||{};problemUrls=status.problem_urls||{};
  renderAccountControls(status.settings||await api('/api/official/accounts'));
  $('problem').innerHTML=Object.keys(status.problems).map(p=>`<option value="${esc(p)}">${esc(p)}</option>`).join('');
  const requested=new URL(location.href).searchParams.get('problem');if(requested&&Object.hasOwn(status.problems,requested))$('problem').value=requested;
  problem=$('problem').value;
  $('app').classList.remove('hidden');$('boot-status').classList.add('hidden');$('login').classList.add('hidden');$('logout').classList.remove('hidden');
  submitEnabled=!!status.enabled;updateSubmitButton();
  if(!submitEnabled)$('submit-note').textContent='官方提交当前未启用，仍可查看历史与最佳代码。';
  await Promise.all([loadEditor(),refresh()]);
}
function saveEditor(){
  const draft=editorDrafts.get(editorProblem);
  if(!draft||!editorReady)return;
  if(draft.kind==='project'&&editorPath&&!draft.files.find(f=>f.path===editorPath)?.readonly)draft.files.find(f=>f.path===editorPath).content=$('source').value;
  else if(draft.kind==='kernel')draft.source=$('source').value;
  draft.label=$('label').value;
}
function renderProjectFile(){
  const draft=editorDrafts.get(editorProblem),f=draft.files.find(f=>f.path===$('project-file').value);
  editorPath=f.path;$('source').value=f.content;$('source').readOnly=f.readonly;
  $('source').required=!f.readonly;$('file').disabled=f.readonly;
  $('file-label').textContent=f.readonly?'该文件由官方保留，不可修改':'替换当前文件，或导入完整工程 JSON';
}
async function loadEditor(){
  const current=problem,version=++editorVersion;editorReady=false;updateSubmitButton();
  renderProblemCatalog();
  const project=submissionKinds[current]==='project';
  $('source').value='';$('source').disabled=true;$('file').disabled=true;
  $('project-controls').classList.toggle('hidden',!project);$('submission-kind').textContent=project?'4 文件工程':'kernel.asc';
  $('contest-link').href=problemUrls[current]||'https://cannjudge.cn/ccpc/ladder_2026_changchun/'+current;
  $('file').value='';$('file').accept=project?'.cpp,.h,.json,.txt':'.asc,.txt';
  $('source').placeholder=project?'选择工程文件后编辑完整内容':'粘贴包含 run_kernel 的完整源码';
  $('file-note').textContent=project?'按目录选择当前文件，再载入 .cpp / .h；JSON 必须包含全部 4 个可编辑文件。':'支持 .asc / .txt，也可以直接粘贴源码。';
  $('project-note').textContent='正在读取官方模板…';
  try{
    let draft=editorDrafts.get(current);
    if(!draft){
      if(project){const template=await api('/api/official/problems/'+current+'/template');draft={kind:'project',label:'',localEvaluation:template.local_evaluation,files:template.files.map(f=>({path:f.path,content:f.content,readonly:!f.editable}))};}
      else draft={kind:'kernel',source:'',label:''};
      editorDrafts.set(current,draft);
    }
    if(version!==editorVersion||current!==problem)return;
    editorProblem=current;editorReady=true;$('source').disabled=false;$('source').readOnly=false;$('source').required=true;$('file').disabled=false;$('label').value=draft.label;
    if(project){
      $('project-file').innerHTML=draft.files.map(f=>`<option value="${esc(f.path)}">${f.readonly?'只读 · ':''}${esc(f.path)}</option>`).join('');
      $('project-file').value=draft.files.find(f=>!f.readonly).path;
      $('project-note').textContent='仅提交 4 个 op_host/op_kernel 文件。3 个 CMakeLists.txt 保持官方原文。'+(draft.localEvaluation?.available?'本题支持本地 NPU 评测；本地数据与官方原始输入不同。':'本题暂无本地默认测试集；强制本地检查开启时不会上传。');renderProjectFile();
    }else{$('source').value=draft.source;$('file-label').textContent='上传源码文件';editorPath='';}
  }finally{if(version===editorVersion)updateSubmitButton();}
}
$('source').addEventListener('input',saveEditor);$('label').addEventListener('input',saveEditor);
$('project-file').onchange=()=>{saveEditor();renderProjectFile();};
$('export-project').onclick=run(()=>{
  saveEditor();const d=editorDrafts.get(editorProblem);if(d?.kind!=='project')return;
  const data=JSON.stringify({problem_id:editorProblem,files:d.files.filter(f=>!f.readonly).map(({path,content})=>({path,content}))},null,2);
  const url=URL.createObjectURL(new Blob([data],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download=editorProblem+'-project.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
});
$('login-form').addEventListener('submit',run(async()=>{
  try{const result=await api('/api/login',{method:'POST',body:JSON.stringify({password:$('password').value})});csrf=result.csrf;$('password').value='';await start();}catch(error){$('login-error').textContent=error.message;}
}));
$('logout').onclick=run(async()=>{await api('/api/logout',{method:'POST'});location.reload();});
$('file').onchange=run(async()=>{const file=$('file').files[0];if(!file)return;if(file.size>2*1024*1024)throw new Error('源码最大 2 MiB');const current=editorProblem,activePath=editorPath,version=editorVersion,text=await file.text();if(version!==editorVersion||current!==problem||activePath!==editorPath)return;
  const draft=editorDrafts.get(current);
  if(draft?.kind==='project'&&file.name.endsWith('.json')){
    const data=JSON.parse(text),rows=Array.isArray(data)?data:data.files,required=draft.files.filter(f=>!f.readonly).map(f=>f.path).sort();
    if(data.problem_id&&data.problem_id!==current)throw new Error('工程 JSON 属于另一题目');
    if(!Array.isArray(rows)||rows.length!==required.length||new Set(rows.map(f=>f.path)).size!==required.length||JSON.stringify(rows.map(f=>f.path).sort())!==JSON.stringify(required)||rows.some(f=>typeof f.content!=='string'||!f.content.trim()||Object.keys(f).sort().join(',')!=='content,path'))throw new Error('必须恰好提供 4 个可编辑文件；不能修改 CMakeLists.txt 或新增文件');
    for(const f of rows)draft.files.find(x=>x.path===f.path).content=f.content;renderProjectFile();
  }else{$('source').value=text;saveEditor();}
  $('file-note').textContent=`已载入 ${file.name} · ${(file.size/1024).toFixed(1)} KiB`;
});
$('submit-form').addEventListener('submit',run(async()=>{
  if(pendingRequest||!submitEnabled)return;
  pendingRequest=true;updateSubmitButton();
  try {
    saveEditor();const draft=editorDrafts.get(problem);
    if(!editorReady||editorProblem!==problem||!draft)throw new Error('题目模板尚未加载完成');
    const body={problem_id:problem,label:$('label').value.trim()||(draft.kind==='project'?'project':'kernel.asc'),account_id:activeAccount,expected_local_validation:localValidationRequired};
    if(draft.kind==='project')body.files=draft.files.filter(f=>!f.readonly).map(({path,content})=>({path,content}));else body.source=draft.source;
    const hash=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(JSON.stringify(body))))).map(v=>v.toString(16).padStart(2,'0')).join('');
    const key='official-request-'+hash;
    body.request_id=sessionStorage.getItem(key)||crypto.randomUUID();sessionStorage.setItem(key,body.request_id);
    const job=await api('/api/official/jobs',{method:'POST',body:JSON.stringify(body)});
    toast('请求已保存 · '+(job.account?.label||'当前账户')+' · '+(job.result?.local_validation?.required===false?'直接提交官方':'先完成本地检查')+'：'+job.id);await refresh({force:true});
  }finally{pendingRequest=false;updateSubmitButton();}
}));
$('sync').onclick=run(async()=>{const button=$('sync');button.disabled=true;try{await api('/api/official/sync',{method:'POST',body:JSON.stringify({account_id:activeAccount})});toast('同步请求已加入队列');await refresh({force:true});}catch(error){button.disabled=false;throw error;}});
$('connection').onclick=run(async()=>{const button=$('connection');button.disabled=true;try{const result=await api('/api/official/connection?check_login=true');toast(result.login_check?.message||result.message||'官方提交正在进行');}finally{button.disabled=false;}});
function resetOfficialView(){
  renderInputSpecifications(null);
  refreshController?.abort();refreshVersion++;refreshWork=null;
  offset=0;bestsState='';jobState='';$('official-total-history').classList.add('hidden');$('official-total-history-rows').innerHTML='';resetDetail('正在读取 '+problem+'…');
  if($('source-dialog').open)$('source-dialog').close();sourceController?.abort();sourceRecord=null;
  $('case-bests').innerHTML='<tr><td colspan="5" class="empty">正在读取该题最佳代码…</td></tr>';
  $('official-top-rows').innerHTML='<tr><td colspan="5" class="empty">正在读取该题前三…</td></tr>';$('official-top-note').textContent='';$('official-top-count').textContent='—';
  $('hidden-top-rows').innerHTML='<tr><td colspan="5" class="empty">正在读取该题隐藏点合计…</td></tr>';
  $('hidden-history-rows').innerHTML='';$('hidden-history').classList.add('hidden');$('hidden-history').open=false;
  $('hidden-top-note').textContent='';$('hidden-top-count').textContent='—';
  $('history').innerHTML='<tr><td colspan="9" class="empty">正在读取该题官方历史…</td></tr>';
  $('projection-score-breakdown').innerHTML='';$('record-score-breakdown').innerHTML='';
  $('official-top-title').textContent='官方记录 · 前三';$('ranking-score-label').textContent='最新复算分 / 100';
  $('jobs').innerHTML='<p class="empty">正在读取该题队列…</p>';
  for(const id of ['projected-score','best-single-score','projected-gain'])$(id).textContent='—';
  $('projected-coverage').textContent='正在读取官方测试点…';$('projection-note').textContent='';$('bests-note').textContent='';$('best-single-record').classList.add('hidden');
  $('prev').disabled=true;$('next').disabled=true;$('page-info').textContent='加载中…';
}
$('problem').onchange=run(async()=>{saveEditor();problem=$('problem').value;const url=new URL(location.href);url.searchParams.set('problem',problem);history.replaceState(null,'',url);resetOfficialView();await Promise.all([loadEditor(),refresh({force:true})]);});
$('order').onchange=run(async()=>{offset=0;await refresh({force:true});});
function loadingHistory(){$('prev').disabled=true;$('next').disabled=true;$('history').innerHTML='<tr><td colspan="9" class="empty">正在读取这一页…</td></tr>';}
$('prev').onclick=run(async()=>{offset=Math.max(0,offset-PAGE);loadingHistory();await refresh({force:true});});
$('next').onclick=run(async()=>{offset+=PAGE;loadingHistory();await refresh({force:true});});
$('refresh').onclick=run(()=>refresh({force:true}));
$('retry').onclick=run(()=>refresh({force:true}));
async function selectAndReveal(id){const row=await select(id);if(row)$('result-card').scrollIntoView({behavior:matchMedia('(prefers-reduced-motion: reduce)').matches?'auto':'smooth',block:'start'});return row;}
$('history').onclick=run(async e=>{const id=e.target.closest('[data-history-record]')?.dataset.historyRecord;if(id)await selectAndReveal(id);});
$('best-single-record').onclick=run(async()=>{const id=$('best-single-record').dataset.record;if(id)await selectAndReveal(id);});
$('official-total-history-rows').onclick=$('hidden-top-rows').onclick=$('hidden-history-rows').onclick=$('official-top-rows').onclick=$('case-bests').onclick=async e=>{
  const record=e.target.closest('[data-best-record]')?.dataset.bestRecord;
  const code=e.target.closest('[data-best-source]')?.dataset.bestSource;
  if(!record&&!code)return;
  e.preventDefault();
  try {if(code){const row=await select(code);if(row)await showSource(row);}else await selectAndReveal(record);}
  catch(error){toast(error.message);}
};
$('jobs').onclick=run(async e=>{const record=e.target.closest('[data-job-record]')?.dataset.jobRecord;if(record)await selectAndReveal(record);const id=e.target.closest('[data-refresh]')?.dataset.refresh;if(id){await api('/api/official/jobs/'+id+'/refresh',{method:'POST'});await refresh({force:true});}});
$('show-source').onclick=run(async()=>showSource(selected));
$('source-file').onchange=run(source);
$('close-source').onclick=()=>$('source-dialog').close();
$('source-dialog').addEventListener('close',()=>{sourceController?.abort();sourceVersion++;sourceRecord=null;});
$('copy-prompt').onclick=run(async()=>{await navigator.clipboard.writeText(prompt);toast('已复制，只需替换题目名');});
setInterval(()=>{if(!document.hidden)refresh();},7000);
setupAccountControls();
start().catch(error=>{if($('login').classList.contains('hidden')){$('boot-status').textContent='连接失败：'+error.message+'。请刷新页面重试。';}});

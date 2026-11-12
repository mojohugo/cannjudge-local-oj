const $ = (id) => document.getElementById(id);
let csrf = '', selected = null, rows = [], specification = null, timer = null, catalog = [], currentProblem = '', lastPreflight = null, drafts = {};
let renderedJob=null, renderedPreflight=null, renderedSpec=null;
let pinnedReport=null, reportRequest=0, settingBaseline=false;
let leaderboard=null, leaderboardReadAt=-Infinity, leaderboardPending=null, leaderboardEpoch=0;
let kernelRequest=0,kernelFiles=[];
let projectSources={},projectPath='';
let activePanel='report',historySignature='',detailLoading=null,detailAbort=null,submitAttempt=null;
function isProject(){return specification?.contract?.submission_kind==='project';}
function saveProjectEditor(){if(isProject()&&projectPath)projectSources[projectPath]=$('source').value;}
function projectProgress(){const paths=specification?.contract?.editable_files||[];$('project-progress').textContent=paths.filter(p=>(projectSources[p]||'').trim()).length+' / '+paths.length+' 个文件已填写';}
function submissionSource(){if(!isProject())return {source:$('source').value};saveProjectEditor();return {files:specification.contract.editable_files.map(path=>({path,content:projectSources[path]||''}))};}
function configureProjectEditor(draft){
  projectSources={...(draft.projectSources||{})};projectPath='';$('project-editor').classList.toggle('hidden',!isProject());
  $('project-path').replaceChildren();
  if(isProject()){
    for(const path of specification.contract.editable_files){const option=node('option',path);option.value=path;$('project-path').append(option);}
    projectPath=specification.contract.editable_files.includes(draft.projectPath)?draft.projectPath:specification.contract.editable_files[0];
    $('project-path').value=projectPath;$('source').value=projectSources[projectPath]||'';$('source-label').textContent=projectPath;
    $('source').placeholder='粘贴当前文件源码；四个文件齐全后提交…';$('file').accept='.cpp,.h,.json';$('file-label').textContent='上传当前文件或 project.json';
    $('source-kind').textContent='四个文件 · 合计最大 2 MiB';$('profile-mode').value=draft.profile_mode||'timing';$('profile-mode').disabled=specification.profiler_supported===false;
  }else{$('source-label').textContent='kernel.asc';$('source').placeholder='粘贴包含 run_kernel 的 Ascend C 源码…';$('file').accept='.asc,.txt';$('source-kind').textContent='单文件 · 最大 2 MiB';$('profile-mode').disabled=false;}
  const custom=specification.custom_generator_supported!==false;$('suite').options[1].disabled=!custom;if(!custom)$('suite').value='official_shapes';$('suite').onchange();projectProgress();
}
const caseBestSelections=new Map();
const poller=new OJPolling.SubmissionPoller({compact:true,historyInterval:15000,request:(path,options)=>api(path,options),onRows:updated=>{
  const finished=updated.some(row=>!active.has(row.status)&&rows.some(old=>old.id===row.id&&active.has(old.status)));
  rows=updated;if(!selected&&rows.length)selected=rows[0].id;
  poller.setSelected(selected);render();
  $('sync-indicator').textContent='更新于 '+new Date().toLocaleTimeString('zh-CN');
  if(selectedJob()?._summary)openSubmission(selected,{reveal:false});
  if(finished){leaderboardReadAt=-Infinity;if(activePanel==='rankings')refreshLeaderboard(true);}
}});
const names = {queued:'排队中',connecting:'连接服务器',waiting:'等待设备',generating:'生成数据',compiling:'编译中',running:'评测中',accepted:'通过',compatibility_mismatch:'参考兼容未通过',legacy_unverified:'旧校验 · 待复测',wrong_answer:'答案错误',compile_error:'编译失败',runtime_error:'运行错误',time_limit:'超时',infrastructure_error:'服务异常',data_error:'数据错误',illegal:'违规',interrupted:'已中断',cancelled:'已取消'};
const active = new Set(['queued','connecting','waiting','generating','compiling','running']);
function toast(text){$('toast').textContent=text;$('toast').style.display='block';setTimeout(()=>{$('toast').style.display='none';},5000);}
async function api(path, options={}){
  const headers = {'Content-Type':'application/json',...options.headers};
  if(csrf) headers['X-CSRF-Token']=csrf;
  const signal=options.signal?AbortSignal.any([options.signal,AbortSignal.timeout(30000)]):AbortSignal.timeout(30000);
  const response=await fetch(path,{...options,signal,headers,credentials:'same-origin'});
  const value=await response.json();
  if(!response.ok){if(response.status===401 && path!='/api/login') showLogin();throw new Error(typeof value.detail==='string'?value.detail:value.detail?.message||'请求失败');}
  return value;
}
function showLogin(){$('boot-local')?.classList.add('hidden');$('login-view').classList.remove('hidden');$('app-view').classList.add('hidden');$('logout').classList.add('hidden');clearInterval(timer);timer=null;poller.setProblem('');rows=[];pinnedReport=null;resetLeaderboard();}
function node(tag,text,className){const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(className)n.className=className;return n;}
function badge(status){return node('span',names[status]||'—','badge '+(status==='accepted'?'accepted':active.has(status)?'active':status?'failed':''));}
function time(us){return us==null?'—':us>=1000?(us/1000).toFixed(3)+' ms':us.toFixed(2)+' μs';}
function caseDescription(spec){const label=spec.specification_origin==='confirmed_official_shape'?'已知规格':spec.specification_origin==='diagnostic_shape'?'诊断规格':spec.specification_origin==='synthetic_guess'?'推测压力点':'';return spec.description||[label,spec.shape?'['+spec.shape.join(', ')+']':spec.name||spec.label||'—',spec.k!==undefined?'k='+spec.k:null,typeof spec.largest==='boolean'?(spec.largest?'最大':'最小'):null].filter(Boolean).join(' · ');}
function groupSummary(groups){return ['known','diagnostic','synthetic'].map(key=>groups?.[key]).filter(Boolean).map(g=>g.label+' '+(g.mean_score==null?'—':g.mean_score.toFixed(2))).join(' · ');}
function renderScoreGroups(score){
  const root=$('score-groups');root.replaceChildren();root.classList.toggle('hidden',!score?.available||!score.score_breakdown);
  if(!score?.available||!score.score_breakdown)return;
  const metric=score.method==='device'?'设备耗时':'Event 耗时';
  for(const key of ['known','diagnostic','synthetic']){
    const g=score.score_breakdown[key];if(!g)continue;
    const card=node('div',undefined,'score-group');
    card.append(node('span',g.label+' · '+g.total+' 点'),node('strong',(g.mean_score==null?'—':g.mean_score.toFixed(2))+' / 100'),
      node('small','通过 '+g.passed+' / '+g.total+' · '+metric),
      node('small','小计 '+g.score_sum.toFixed(2)+' / '+g.max_score_sum+' · 总分贡献 '+g.contribution.toFixed(2)),
      node('small','占总分 '+(100*g.weight).toFixed(2)+'% · 已有通过参照 '+g.reference_coverage+' 点'));
    root.append(card);
  }
}
function deviceAggregation(info,item){
  const kind=info?.measurement_protocol?.primary_aggregation??item?.phase_metrics?.steady_state?.aggregation;
  if(kind==='arithmetic_mean_of_measured_invocation_task_duration_sums'||kind==='arithmetic_mean_of_invocation_task_duration_sums')return '算术均值';
  if(kind==='pooled_median_of_measured_invocation_task_duration_sums'||kind==='pooled_median_of_invocation_task_duration_sums')return '中位数';
  return null;
}
function deviceTimeCell(job,index){
  const metric=job?.result?.metrics?.PipeUtilization;
  const partial=metric?.partial_observations;
  const observation=partial?.scoring_eligible===false?partial.cases?.find(c=>c.index===index):null;
  const entry=metric?.status==='available'?metric.cases?.find(c=>c.index===index):observation?.available?{...observation,op_time_us:observation.device_us}:null;
  if(entry?.op_time_us!=null){
    const cell=node('td',time(entry.op_time_us));
    if(metric?.status!=='available')cell.append(node('small','局部观测 · 不计分'));
    const aggregation=deviceAggregation(metric,entry);
    cell.title=aggregation?'主值为预热后完整调用的设备任务总时长'+aggregation+'。':'聚合方式未记录，保留此提交的原始设备主耗时。';
    if(Number.isFinite(entry.first_invocation_us)){
      cell.append(node('small','首次 '+time(entry.first_invocation_us)));
      cell.title+='首次值为输入上传后的第一轮校验调用，仅用于对照，不参与主分数。';
    }
    cell.title+='这些是本地 profiler 设备任务耗时，不代表官方耗时。';
    return cell;
  }
  const cell=node('td',!job?'—':active.has(job.status)?'待采集':metric?.status==='unavailable'?'不可用':'未采集','muted');
  const mismatch=observation?.invocations?.find(c=>c.status==='unavailable');
  cell.title=mismatch?`${observation.case_id} · ${mismatch.phase} · observed ${mismatch.observed_launches} / expected ${mismatch.expected_launches} · cause unknown`:metric?.reason||(!job?'尚无提交':active.has(job.status)?'正确性通过后采集真实 profiler 设备耗时':'这份历史记录没有 profiler 设备测量值；重新评测可采集。Event 时间不会代替设备耗时。');
  return cell;
}
function selectedJob(){return rows.find(r=>r.id===selected)||(pinnedReport?.id===selected?pinnedReport:null);}
function resetLeaderboard(){leaderboard=null;leaderboardReadAt=-Infinity;leaderboardPending=null;leaderboardEpoch++;caseBestSelections.clear();closeKernel();renderLeaderboard();}
function closeKernel(){kernelRequest++;kernelFiles=[];if($('kernel-dialog').open)$('kernel-dialog').close();$('kernel-code').textContent='';$('kernel-file').replaceChildren();}
async function openKernel(submission,index){
  const request=++kernelRequest,generation=poller.generation;
  $('kernel-title').textContent=(index?'Case '+String(index).padStart(2,'0')+' · ':'')+submission.label;
  $('kernel-meta').textContent='提交 '+submission.id+' · 整份通过 '+submission.passed+' / '+submission.total+' · SHA-256 '+submission.source_sha256;
  $('kernel-download').href=submission.source_url;
  const project=submission.submission_kind==='project';
  $('kernel-download').download=project?'project.json':'kernel.asc';
  $('kernel-download').textContent=project?'下载完整工程 JSON':'下载源码';
  kernelFiles=[];$('kernel-file').replaceChildren();$('kernel-file').classList.add('hidden');
  $('kernel-code').textContent='正在读取源码…';
  if(!$('kernel-dialog').open)$('kernel-dialog').showModal();
  try{
    const response=await fetch(submission.source_url,{credentials:'same-origin'});
    if(!response.ok){if(response.status===401)showLogin();throw new Error('源码读取失败（'+response.status+'）');}
    const source=await response.text();
    if(request===kernelRequest&&generation===poller.generation){
      if(project){
        kernelFiles=JSON.parse(source);
        for(const file of kernelFiles){const option=node('option',file.path);option.value=file.path;$('kernel-file').append(option);}
        if(kernelFiles.some(f=>f.path===specification?.contract?.kernel_file))$('kernel-file').value=specification.contract.kernel_file;
        $('kernel-file').classList.remove('hidden');$('kernel-file').onchange=()=>{$('kernel-code').textContent=kernelFiles.find(f=>f.path===$('kernel-file').value)?.content||'';};$('kernel-file').onchange();
      }else $('kernel-code').textContent=source;
    }
  }catch(error){if(request===kernelRequest&&generation===poller.generation)$('kernel-code').textContent=error.message;}
}
function renderCaseBests(){
  $('case-best-rows').replaceChildren();
  $('case-best-method').textContent=leaderboard?.available?(leaderboard.method==='device'?'设备耗时':'Event 耗时')+' · 全部可比历史':'全部可比历史';
  if(!leaderboard?.available){$('case-best-note').textContent=leaderboard?.reason||'正在读取逐点最优…';return;}
  for(const best of leaderboard.case_bests||[]){
    const spec=best.specification,tr=node('tr'),shape=node('td',caseDescription(spec));
    shape.append(node('small',[spec.dtype,spec.dim!==undefined?'dim '+spec.dim:null,spec.eps!==undefined?'eps '+spec.eps:null,spec.uplo?'UPLO '+spec.uplo:null].filter(Boolean).join(' · ')));
    const duration=node('td',time(best.time_us));duration.title=best.time_us==null?'':String(best.time_us)+' μs';
    tr.append(node('td',String(best.index).padStart(2,'0')),shape,node('td',best.score==null?'—':best.score.toFixed(2)),duration);
    if(!best.available||!best.submissions.length){tr.append(node('td','暂无可比通过代码'),node('td','—'),node('td','—'));$('case-best-rows').append(tr);continue;}
    const versions=node('td',undefined,'case-best-version'),passed=node('td'),actions=node('td'),buttons=node('div',undefined,'actions');
    const select=node('select');select.setAttribute('aria-label','选择 Case '+best.index+' 的并列最优代码');
    for(const entry of best.submissions){const option=node('option',entry.label+' · '+entry.id.slice(0,8));option.value=entry.id;select.append(option);}
    const saved=caseBestSelections.get(best.index);if(best.submissions.some(entry=>entry.id===saved))select.value=saved;
    const label=node('strong'),detail=node('small'),source=node('button','查看源码','secondary'),download=node('a','下载','secondary'),report=node('button','报告','plain');
    source.type=report.type='button';download.download='kernel.asc';
    const choose=()=>{
      const entry=best.submissions.find(item=>item.id===select.value)||best.submissions[0];caseBestSelections.set(best.index,entry.id);
      label.textContent=entry.label;detail.textContent=entry.source_sha256.slice(0,12)+' · '+new Date(entry.created*1000).toLocaleString('zh-CN');detail.title='提交 '+entry.id+' · SHA-256 '+entry.source_sha256;
      passed.replaceChildren(node('span',entry.passed+' / '+entry.total),node('small',names[entry.status]||entry.status),node('small',entry.total_score==null?'':'本地总分 '+entry.total_score.toFixed(2)));
      if(entry.score_breakdown)passed.append(node('small',groupSummary(entry.score_breakdown)));
      source.setAttribute('aria-label','查看 Case '+best.index+' 最优源码：'+entry.label);source.onclick=()=>openKernel(entry,best.index);
      download.href=entry.source_url;download.download=entry.download_filename||'kernel.asc';download.textContent=entry.submission_kind==='project'?'下载工程':'下载';download.setAttribute('aria-label','下载 Case '+best.index+' 最优源码');report.onclick=()=>openSubmission(entry.id);
    };
    if(best.tie_count>1){versions.append(node('small',best.tie_count+' 份提交并列最优'),select);}else versions.append(label);
    versions.append(detail);select.onchange=choose;choose();buttons.append(source,download,report);actions.append(buttons);tr.append(versions,passed,actions);$('case-best-rows').append(tr);
  }
  $('case-best-note').textContent=leaderboard.case_bests_note||'逐点最优代码正在更新。';
}
function renderBaselineButton(){
  const job=selectedJob(), isCurrent=Boolean(job&&job.id===leaderboard?.baseline_id);
  const eligible=job?.status==='accepted'&&!job?.validation?.requires_reevaluation&&job.result?.suite==='official_shapes'&&job.result?.environment?.problem_sha256===specification?.pack_sha256;
  $('set-baseline').disabled=settingBaseline||!eligible||isCurrent;
  $('set-baseline').textContent=settingBaseline?'正在切换基线…':isCurrent?'当前对比基线':'设为对比基线';
  $('baseline-note').textContent=isCurrent?'当前提交为本题对比基线；排行榜的 TBest 仍取逐点历史最优。':eligible?'将此提交设为本题共用对比基线，查看逐点加速比；无需重新评测。':'选择全部通过的当前版本默认测试集提交，可设为本题对比基线。';
}
function renderLeaderboard(){
  $('leaderboard-rows').replaceChildren();$('leaderboard-baseline').replaceChildren();
  $('leaderboard-count').textContent=leaderboard?.available?leaderboard.total_eligible+' 份可比提交':'全部历史提交';
  if(leaderboard?.available){
    const referenceId=leaderboard.baseline_id||leaderboard.reference_submission_id;
    const button=node('button',leaderboard.baseline_label||leaderboard.reference_label,'plain');button.type='button';button.onclick=()=>openSubmission(referenceId);
    $('leaderboard-baseline').append(node('span',leaderboard.baseline_id?'对比基线 · ':'排名协议参考 · '),button,node('span',leaderboard.baseline_id?(leaderboard.baseline_selection==='manual'?' · 手动选择':' · 自动建立'):' · 完整测完的记录，尚无全通过基线'));
    for(const entry of leaderboard.entries){
      const tr=node('tr'), label=node('td',entry.label), score=node('td');
      label.append(node('small',new Date(entry.created*1000).toLocaleString('zh-CN')+' · '+entry.source_sha256.slice(0,8)));
      score.append(node('strong',entry.score.toFixed(2)));
      if(entry.score_breakdown)score.append(node('small',groupSummary(entry.score_breakdown)));
      const action=node('td'), button=node('button','查看','secondary');button.type='button';button.setAttribute('aria-label','查看第 '+entry.rank+' 名：'+entry.label);button.onclick=()=>openSubmission(entry.id);
      action.append(button);
      if(entry.source_url){const source=node('button','源码','plain');source.type='button';source.onclick=()=>openKernel(entry);action.append(source);}
      tr.append(node('td',String(entry.rank).padStart(2,'0')),label,score,node('td',entry.passed+' / '+entry.total),action);$('leaderboard-rows').append(tr);
    }
    $('leaderboard-note').textContent=(leaderboard.entries.length?'':'暂无符合条件的提交。')+'每点 100 / (1 + log₁.₅(用时 / TBest))，全部测试点取平均；使用本地'+(leaderboard.method==='device'?'设备耗时':'Event 耗时')+'和逐点历史最优，失败点 0 分。不是线上实得分。';
    if(leaderboard.rules?.score_scope==='synthetic_local_suite')$('leaderboard-note').textContent+=' 公开规格、诊断规格和推测压力按来源分别计分；总分按点数加权，未恢复的官方隐藏点序号不会强行对应。';
  }else $('leaderboard-note').textContent=leaderboard?.reason||'正在读取排行榜…';
  renderCaseBests();renderBaselineButton();
}
async function refreshLeaderboard(force=false){
  if(!poller.problem||(!force&&Date.now()-leaderboardReadAt<30000))return;
  if(leaderboardPending)return leaderboardPending;
  const generation=poller.generation,epoch=leaderboardEpoch,problem=currentProblem;
  const pending=Promise.resolve().then(async()=>{
    try{
      const value=await api('/api/leaderboard?problem_id='+encodeURIComponent(problem));
      if(generation!==poller.generation||epoch!==leaderboardEpoch)return;
      const changed=leaderboard&&JSON.stringify([value.baseline_id,value.reference_submission_id,value.tbest_us,value.method])!==JSON.stringify([leaderboard.baseline_id,leaderboard.reference_submission_id,leaderboard.tbest_us,leaderboard.method]);
      leaderboard=value;leaderboardReadAt=Date.now();renderLeaderboard();
      if(changed&&selected){
        const identity=selected,updated=await api('/api/submissions/'+identity);
        if(generation===poller.generation&&epoch===leaderboardEpoch&&selected===identity){
          pinnedReport=updated;rows=rows.map(row=>row.id===identity?updated:row);poller.rows=rows;render();
        }
      }
    }catch(error){
      if(generation!==poller.generation||epoch!==leaderboardEpoch)return;
      leaderboardReadAt=Date.now();$('leaderboard-note').textContent='排行榜暂未刷新：'+error.message;
    }
  }).finally(()=>{if(leaderboardPending===pending)leaderboardPending=null;});
  leaderboardPending=pending;return pending;
}
async function openSubmission(identity,{reveal=true}={}){
  if(detailLoading===identity)return;
  const generation=poller.generation,request=++reportRequest;
  try{
    const cached=rows.find(row=>row.id===identity);
    if(detailLoading===identity)return;
    detailAbort?.abort();detailAbort=new AbortController();detailLoading=identity;
    selected=identity;poller.setSelected(identity);render();
    const job=cached&&!cached._summary?cached:await api('/api/submissions/'+identity,{signal:detailAbort.signal});
    if(generation!==poller.generation||request!==reportRequest)return;
    if(job.problem_id!==currentProblem)throw new Error('提交与当前题目不匹配');
    pinnedReport=job;selected=identity;rows=rows.map(row=>row.id===identity?job:row);poller.rows=rows;poller.setSelected(identity);lastPreflight=null;render();$('logs-card').classList.add('hidden');
    if(reveal){showPanel('report');$('result-title').scrollIntoView({behavior:matchMedia('(prefers-reduced-motion: reduce)').matches?'auto':'smooth',block:'center'});}
  }catch(error){if(error.name!=='AbortError'&&generation===poller.generation&&request===reportRequest)toast(error.message);}
  finally{if(detailLoading===identity)detailLoading=null;}
}
function onlineCells(estimate){
  const observed=node('td',time(estimate?.observed_online_us));
  const records=estimate?.observed_online_records||[];
  if(records.length){
    const label=records.some(r=>r.provenance==='user_reported')?'用户提供 · 同源码':'历史记录 · 同源码';
    const record=records.find(r=>r.url?.startsWith('https://cannjudge.cn/'));
    const detail=node(record?'a':'small',label);
    if(record){detail.href=record.url;detail.target='_blank';detail.rel='noopener noreferrer';}
    observed.append(detail);
  }
  const predicted=node('td',time(estimate?.estimated_online_us));
  if(estimate?.estimated_online_range_us){
    predicted.append(node('small',estimate.estimated_online_range_us.map(time).join(' – ')));
    predicted.append(node('small',estimate.calibration_neighbors+' 个邻近版本'+(estimate.extrapolated?' · 外推，低可信':'')));
    if(estimate.validation_max_error!=null)predicted.title='本点逐源码留出最大误差 '+(estimate.validation_max_error*100).toFixed(1)+'%，共 '+estimate.validation_samples+' 个版本。经验范围不保证覆盖新版本。';
  }
  return [observed,predicted];
}
function bytes(n){if(n==null)return '未采集';if(n===0)return '0 B';const units=['B','KiB','MiB','GiB'];const i=Math.min(3,Math.floor(Math.log(n)/Math.log(1024)));return (n/1024**i).toFixed(i?2:0)+' '+units[i];}
function renderChecks(check,violations=[]){const root=$('checks');root.replaceChildren();const issues=[...(check?.issues||[]),...violations];if(!check&&!issues.length){root.append(node('p','等待检查','muted small'));return;}root.append(node('p',issues.some(x=>x.severity==='error')?'检查未通过':issues.length?'有待复核项':'当前已执行检查通过','muted small'));for(const issue of issues){const box=node('div',undefined,'rule-issue');const origins={confirmed_online_rule:'线上已确认规则',template_contract:'模板契约',local_service_policy:'本服务策略'};box.append(node('strong',issue.rule_id+' · '+(origins[issue.origin]||issue.origin)),node('p',issue.message),node('p',issue.stage+(issue.line?' · 第 '+issue.line+' 行，第 '+issue.column+' 列':''),'muted small'));if(issue.evidence)box.append(node('pre',issue.evidence));if(issue.observed!==undefined)box.append(node('p','期望 '+issue.expected+'，实测 '+issue.observed+'，调用序号（从 0 开始）'+issue.invocation));box.append(node('p',issue.suggestion,'muted small'));root.append(box);}root.append(node('p',(check?.limitations||[]).join(' '),'footnote'));}
function renderReferenceModels(root,c){
  if(!c.reference_models)return;
  const titles={mathematical_fp64:'公开公式参考（本地计分）',fp16_staged:'FP16 中间舍入对照（仅诊断）'};
  for(const [name,v] of Object.entries(c.reference_models)){
    root.append(node('p',(titles[name]||name)+'：'+(v.status==='matched'?'通过':'不匹配')+' · 差异 '+v.mismatches+' / '+v.elements+'（'+(v.error_rate*100).toFixed(5)+'%） · 最大有限绝对差 '+v.max_abs_error.toExponential(3),'muted small'));
  }
  root.append(node('p','官方中间舍入规则未知；两种参考仅作用于本地合成数据，不能据此证明官方通过。NaN 输出 '+c.nan_outputs+'，保护区 '+(c.guards_intact?'完整':'被改写'),'footnote'));
}
function renderMetrics(job){
  const container=$('metrics');const opened=new Set([...container.querySelectorAll('details[open]')].map(d=>d.dataset.case));container.replaceChildren();
  if(!job||job._summary){container.append(node('p','正确性通过后采集 PipeUtilization / L2Cache / MemoryUB','empty'));return;}
  const groups=job.result.metrics||{};if(!(job.result.cases||[]).length){container.append(node('p','尚无运行结果；编译、数据或规则失败时不采集性能指标。','empty'));return;}
  for(const c of job.result.cases||[]){const d=node('details',undefined,'metric-case');d.dataset.case=String(c.index);d.open=opened.has(String(c.index));d.append(node('summary','Case '+(c.original_index??c.index)+' · 工作区 '+bytes(c.memory?.workspace_peak_requested_bytes??c.memory?.workspace_bytes)));
    const m=c.memory||{};renderReferenceModels(d,c);d.append(node('p','最大绝对误差 '+(c.max_abs_error??'—')+' / 失败检查 '+(c.failed_checks??'—')+' / 输入改写 '+(c.input_mutations??'—')+' / 未写输出 '+(c.unwritten_outputs??'—'),'muted small'));if(c.orthogonality_frobenius!=null)d.append(node('p','正交误差 '+c.orthogonality_frobenius.toExponential(3)+' / 相对重构误差 '+c.relative_reconstruction_frobenius.toExponential(3)+' / 保护区 '+(c.guards_intact?'完整':'被改写'),'muted small'));if(c.index_mismatches!=null){d.append(node('p','距离错误 '+c.distance_mismatches+' / '+c.output_elements+' · 索引错误 '+c.index_mismatches+' / '+c.output_elements+' · 索引越界 '+c.index_out_of_range+' · 保护区 '+(c.guards_intact?'完整':'被改写'),'muted small'));}if(c.solution_mismatches!=null){d.append(node('p','解错误 '+c.solution_mismatches+' / '+c.solution_elements+' · 残差错误 '+c.residual_mismatches+' / '+c.residual_elements+' · 保护区 '+(c.guards_intact?'完整':'被改写'),'muted small'));}if(c.reference_compatibility){const v=c.reference_compatibility;d.append(node('p','数学检查 '+(c.mathematical_status==='accepted'?'通过':'未通过')+' / 参考输出兼容 '+(v.status==='matched'?'通过':v.status==='mismatch'?'未通过':'未检查')+' / Q 差异 '+(v.error_rate*100).toFixed(4)+'%（'+v.mismatches+' / '+v.elements+'） / Q 最大绝对差 '+v.max_abs_error.toExponential(3),'muted small'));}if(c.message)d.append(node('p',c.message,'muted small'));d.append(node('p','输入 '+bytes(m.input_bytes)+' / 输出 '+bytes(m.output_bytes)+' / HBM 空闲 '+bytes(m.hbm_free_bytes)+' / 总量 '+bytes(m.hbm_total_bytes),'muted small'));
    d.append(node('p','分配调用 '+(m.allocation_calls??'未采集')+' 次 / 累计申请 '+bytes(m.allocated_requested_bytes)+' / 结束时存活申请 '+bytes(m.workspace_live_requested_bytes),'muted small'));d.append(node('p',m.workspace_peak_requested_bytes!=null?'工作区为候选通过 aclrtMalloc 申请的峰值字节数；不含框架输入输出、分配器额外占用和静态 UB。':m.workspace_bytes!=null?'工作区为 aclnn GetWorkspaceSize 返回的容量，包含 CANN 框架所需部分；不含输入输出、分配器额外占用和静态 UB。':'工作区容量未采集；不以零代替未知值。','muted small'));
    renderCaseDiagnostics(d,job,c);
    for(const group of ['PipeUtilization','L2Cache','MemoryUB']){const info=groups[group];if(!info){d.append(node('p',group+'：待采集','muted small'));continue;}if(info.status!=='available'&&info.status!=='partial'){d.append(node('p',group+(info.status==='skipped'?'：未采集 · ':'：不可用 · ')+info.reason,'muted small'));continue;}
      const item=info.cases.find(x=>x.index===c.index);if(!item)continue;d.append(node('h3',group));d.append(node('p','op_summary '+time(item.op_time_us)+' / task_time '+time(item.task_time_us)+' · '+item.kernel_count+' 个设备任务','muted small'));
      if(Object.hasOwn(item,'first_invocation_us')||info.measurement_protocol){
        const aggregation=deviceAggregation(info,item),parts=[];
        if(Object.hasOwn(item,'first_invocation_us'))parts.push('首次调用 '+time(item.first_invocation_us));
        parts.push('预热后 '+time(item.op_time_us)+'（'+(aggregation||'聚合方式未记录')+'）');
        if(aggregation==='算术均值'&&Number.isFinite(item.steady_state_median_us))parts.push('中位数 '+time(item.steady_state_median_us)+'（辅助）');
        d.append(node('p',parts.join(' · '),'small'));
        d.append(node('p',aggregation?'预热后值先将每次完整调用的所有设备任务时长求和，再对各次调用取'+aggregation+'。':'此记录未保存可识别的聚合方式，主值保持原始记录，不按当前配置重解释。','muted small'));
        if(Object.hasOwn(item,'first_invocation_us'))d.append(node('p','首次值取输入上传后的首个校验调用，仅用于对照，不替代主耗时或官方估时；历史未采集值保持为空。','muted small'));
      }
      if(info.measurement_protocol?.run_config){const p=info.measurement_protocol.run_config;d.append(node('p','本次采集：预热 '+p.warmups+' 次 · 每轮 '+p.repeats+' 次 · '+p.rounds+' 轮 · '+p.buffers+' 组缓冲区 · '+p.cores+' 核','muted small'));}
      if(info.protocol_id){const p=node('p','测量协议 '+info.protocol_id,'muted small');p.style.overflowWrap='anywhere';d.append(p);}
      const grid=node('dl',undefined,'counter-grid');for(const [name,value] of Object.entries(item.counters||{})){grid.append(node('dt',name),node('dd',value.median==null?'不可用':Number(value.median).toLocaleString('en-US',{maximumFractionDigits:5})));}d.append(grid);
    }container.append(d);
  }
}
function renderProtocol(job){
  const root=$('protocol');root.replaceChildren();
  const actual=job&&!job._summary?job.result?.problem:specification;
  $('timing-note').textContent=actual?.timing?actual.timing+' 设备耗时取 profiler op_summary，有采集扰动；线上估时仅供参考。':job?'此历史记录未保存计时协议；当前配置不能代表当时配置。':'设备耗时取 profiler op_summary，有采集扰动；线上估时仅供参考。';
  if(!actual){root.append(node('p','此历史记录未保存评测协议；当前配置不能代表当时配置。','muted small'));return;}
  root.append(node('p',(job?'此提交使用：':'新提交使用：')+actual.name+' · v'+actual.version,'small'));
  for(const field of ['environment','dataset','timing','tolerance','reference','limitation'])if(actual[field])root.append(node('p',actual[field]));
}
function render(){
  const job=selectedJob();
  renderBaselineButton();
  const historyScroll=$('history').scrollTop;
  const filter=($('history-search')?.value||'').toLowerCase(),visibleRows=rows.filter(r=>(r.label+' '+r.sha256).toLowerCase().includes(filter));
  $('history-count').textContent=visibleRows.length+' / '+rows.length+' 份';
  const signature=JSON.stringify([selected,filter,rows.map(r=>[r.id,r.updated,r.status])]);
  if(signature!==historySignature){historySignature=signature;$('history').replaceChildren();
  if(!rows.length)$('history').append(node('p','尚无提交','empty'));
  for(const row of visibleRows){const b=node('button',undefined,'history-item'+(row.id===selected?' selected':''));b.type='button';
    const top=node('div');top.append(node('strong',row.label),badge(row.validation?.status||row.status));b.append(top,node('small',new Date(row.created*1000).toLocaleString('zh-CN')+' · '+row.sha256.slice(0,8)));b.onclick=()=>openSubmission(row.id);$('history').append(b);}
  $('history').scrollTop=historyScroll;}
  renderQueue(job);
  $('submission-identity').textContent=job?'提交 ID · '+job.id:'';$('copy-submission-id').disabled=!job;
  if(job?._summary){$('result-title').textContent=job.label;$('result-message').textContent='正在读取完整报告…';$('passed').textContent='—';$('score').textContent='—';$('sha').textContent=job.sha256.slice(0,16)+'…';$('cases').replaceChildren();$('cost-grid').replaceChildren();$('score-groups').classList.add('hidden');$('score-note').textContent='等待本次报告';$('evaluation-cost').textContent='';$('result-status').replaceWith(Object.assign(badge(job.status),{id:'result-status'}));$('show-logs').disabled=true;$('set-baseline').disabled=true;$('download').classList.add('disabled');$('cancel').classList.add('hidden');return;}
  if(renderedJob===job&&renderedPreflight===lastPreflight&&renderedSpec===specification)return;
  renderedJob=job;renderedPreflight=lastPreflight;renderedSpec=specification;
  const cases=job?.result?.cases||[], specs=job?.result?.dataset?.cases||job?.result?.regression_spec?.cases||(job?.result?.local_case_indices?job.result.local_case_indices.map((original,i)=>({...job.result.problem.cases[original-1],index:i+1,original_index:original})):(job?.result?.suite==='custom'?[]:(job?.result?.problem?.cases||specification.cases))), count=specs.length;
  $('result-title').textContent=job?job.label:'等待第一份提交';$('result-status').replaceWith(Object.assign(badge(job?.validation?.status||job?.status),{id:'result-status'}));
  $('passed').textContent=job?(job.validation?.requires_reevaluation?'—':String(cases.filter(c=>c.status==='accepted').length)):'—';$('passed').append(node('small',' / '+(count||'待生成')));
  const comp=job?.comparison;
  const scored=job?.score;
  $('score').textContent=scored?.available?scored.score.toFixed(2):'—';
  renderScoreGroups(scored);
  $('score-note').textContent=scored?.available?'每点 100 / (1 + log₁.₅(用时 / TBest))，全部点取平均。'+scored.limitation:(scored?.reason||'默认测试集评测完成后，按官方公式计算本地模拟分。');
  const calibration=comp?.calibration;
  $('calibration-note').textContent=calibration?.available?
    calibration.unique_sources+' 个不同源码配对校准；逐源码留出平均误差 '+(calibration.validation?.mean_absolute_relative_error==null?'尚无足够样本':(calibration.validation.mean_absolute_relative_error*100).toFixed(1)+'%')+'。'+calibration.limitation:
    (calibration?.reason||'线上实测取同源码历史记录；估时需要题目、数据与测量协议一致的多版本样本。');
  $('sha').textContent=job?job.sha256.slice(0,16)+'…':'—';$('sha').title=job?.sha256||'';
  $('result-message').textContent=(job?.result?.message||'上传源码后，这里会逐点显示评测结果。')+(job?.validation?' '+job.validation.note:'')+(job?.result?.suite==='local_subset'?' 原始索引 '+job.result.local_case_indices.join(', ')+' · 数据版本 '+(job.result.dataset?.version||job.result.problem?.dataset_revision):'');
  const evaluation=job?.result?.evaluation; const stageNames={compile_cache_wait:'等待同键编译',configure:'构建准备',compile:'编译',device_wait:'等待 NPU',correctness_and_event:'正确性与 Event',profiler_PipeUtilization:'设备耗时与流水线',profiler_L2Cache:'L2 缓存',profiler_MemoryUB:'UB',profiler_collect_PipeUtilization:'设备耗时与流水线采集',profiler_collect_L2Cache:'L2 缓存采集',profiler_collect_MemoryUB:'UB 采集',profiler_summary_export:'CPU 报表并行导出',generator:'生成数据',other:'其他'};
  $('evaluation-cost').textContent=evaluation?[(evaluation.profile_mode==='timing'?'快速迭代':'完整评测'),...(evaluation.compile_policy?['CPU 编译上限 '+evaluation.compile_policy.compile_timeout_seconds+' 秒，同键等待上限 '+evaluation.compile_policy.cache_wait_timeout_seconds+' 秒']:[]),evaluation.compile_cache?.status==='hit'?'编译缓存命中':'本次编译',...Object.entries(evaluation.stages_seconds||{}).map(([k,v])=>(stageNames[k]||k)+' '+v.toFixed(1)+' 秒')].join(' · '):'';
  $('cases').replaceChildren();
  for(const spec of specs){const c=cases.find(x=>x.index===spec.index),tr=node('tr');tr.append(node('td',String(spec.original_index??spec.index).padStart(2,'0')));const shape=node('td',caseDescription(spec));shape.append(node('small',[spec.dtype,spec.dim!==undefined?'dim '+spec.dim:null,spec.eps!==undefined?'eps '+spec.eps:null,spec.uplo?'UPLO '+spec.uplo:null].filter(Boolean).join(' · ')));tr.append(shape);const status=node('td');status.append(c?badge(job.validation?.requires_reevaluation&&c.status==='accepted'?'legacy_unverified':c.status):node('span','—','muted'));const profile=job?.result?.metrics?.PipeUtilization?.cases?.find(x=>x.index===spec.index);const estimate=comp?.per_case?.find(x=>x.index===spec.index);const point=scored?.per_case?.find(x=>x.index===spec.index);tr.append(status,node('td',point?point.score.toFixed(2):'—'),node('td',time(point?.tbest_us)),node('td',c?.error_rate==null?'—':(c.error_rate*100).toFixed(4)+'%'),node('td',time(c?.time_us)),deviceTimeCell(job,spec.index),node('td',estimate?.speedup==null?'—':estimate.speedup.toFixed(3)+'×'),...onlineCells(estimate));$('cases').append(tr);}
  renderProtocol(job);renderCosts(job);if(activePanel==='diagnostics')renderMetrics(job);
  renderChecks(lastPreflight||job?.result?.checks,lastPreflight?[]:(job?.result?.cases||[]).flatMap(c=>(c.violations||[]).map(v=>({...v,message:'Case '+(c.original_index??c.index)+' · '+v.message}))));
  $('show-logs').disabled=!job;$('download').classList.toggle('disabled',!job);$('download').href=job?'/api/submissions/'+job.id+'/source':'#';$('cancel').classList.toggle('hidden',job?.status!=='queued');
}
async function refresh(forceHistory=false){if(document.hidden&&!forceHistory)return;try{await poller.refresh({forceHistory});if(activePanel==='rankings')refreshLeaderboard(forceHistory);}catch(error){if(error.name!=='AbortError')toast(error.message);}}
async function selectProblem(id){
  saveProjectEditor();
  if(currentProblem)drafts[currentProblem]={projectSources:{...projectSources},projectPath,source:$('source').value,generator:$('generator').value,label:$('label').value,suite:$('suite').value,profile_mode:$('profile-mode').value};
  detailAbort?.abort();detailLoading=null;historySignature='';
  currentProblem=id;specification=catalog.find(p=>p.id===id);selected=null;rows=[];lastPreflight=null;pinnedReport=null;reportRequest++;
  poller.setProblem(id);
  resetLeaderboard();
  const draft=drafts[id]||{};for(const field of ['source','generator','label'])$(field).value=draft[field]||'';
  $('profile-mode').value=draft.profile_mode||'timing';
  $('suite').value=draft.suite||'official_shapes';$('suite').onchange();$('file').value='';$('generator-file').value='';$('file-label').textContent='选择或拖入 kernel.asc';
  configureProjectEditor(draft);
  $('problem-title').textContent=specification.name;$('problem-version').textContent=specification.id+' · v'+specification.version+' · '+specification.cases.length+' 个默认测试点';
  $('machine-info').textContent=specification.environment+' · 评测使用 '+(specification.protocol?.cores??'默认')+' 核';$('dataset-note').textContent=specification.dataset+(isProject()?' 本地通过只代表公开数学契约检查通过，官方判定尚未完全对齐。':'');
  $('generator-limits').textContent=specification.generator_limits;$('suite').options[0].textContent=specification.data_evidence?.diagnostic_shape_count?'默认 5 公开规格 + '+specification.data_evidence.diagnostic_shape_count+' 诊断规格':specification.data_evidence?.guessed_shape_count?'默认 '+specification.data_evidence.known_shape_count+' 已知规格 + '+specification.data_evidence.guessed_shape_count+' 推测压力点':'默认 '+specification.cases.length+' 点 · 本题规格';
  configureCasePicker(draft);$('ab-card').classList.toggle('hidden',id!=='pdist'||activePanel!=='diagnostics');$('build-evidence').disabled=id!=='pdist';
  $('logs-card').classList.add('hidden');render();refresh(true);
}
async function enter(){const session=await api('/api/session');csrf=session.csrf;const data=await api('/api/problems');$('worker-host').textContent='SSH · '+(data.backend?.ssh_host||'NPU');$('machine-name').textContent=data.backend?.ssh_host||'NPU';catalog=data.problems;$('problem-select').replaceChildren();for(const p of catalog){const option=node('option',p.name+' · '+p.id);option.value=p.id;$('problem-select').append(option);}if(!catalog.length)throw new Error('暂无已启用的题目');const id=catalog.some(p=>p.id===currentProblem)?currentProblem:(catalog.find(p=>p.id===data.default_problem)||catalog[0]).id;$('problem-select').value=id;$('boot-local').classList.add('hidden');$('login-view').classList.add('hidden');$('app-view').classList.remove('hidden');$('logout').classList.remove('hidden');await selectProblem(id);showPanel(activePanel);clearInterval(timer);timer=setInterval(refresh,5000);}
$('problem-select').onchange=()=>selectProblem($('problem-select').value).catch(error=>toast(error.message));
$('login-form').onsubmit=async(event)=>{event.preventDefault();try{await api('/api/login',{method:'POST',body:JSON.stringify({password:$('password').value})});$('password').value='';await enter();}catch(error){toast(error.message);}};
$('logout').onclick=async()=>{try{await api('/api/logout',{method:'POST'});csrf='';showLogin();}catch(error){toast(error.message);}};
async function loadFile(file){if(!file)return;if(file.size>2*1024*1024){toast('文件最大 2 MiB');return;}try{const text=await file.text();if(isProject()&&file.name.endsWith('.json')){const parsed=JSON.parse(text);const files=Array.isArray(parsed)?parsed:parsed.files;if(!Array.isArray(files))throw new Error('工程 JSON 应包含 files 数组');for(const f of files)if(specification.contract.editable_files.includes(f.path)&&typeof f.content==='string')projectSources[f.path]=f.content;$('source').value=projectSources[projectPath]||'';projectProgress();}else{$('source').value=text;saveProjectEditor();projectProgress();}$('file-label').textContent=file.name;if(!$('label').value)$('label').value=file.name;}catch(error){toast(error.message);}}
$('source').addEventListener('input',()=>{saveProjectEditor();projectProgress();});
$('project-path').onchange=()=>{saveProjectEditor();projectPath=$('project-path').value;$('source').value=projectSources[projectPath]||'';$('source-label').textContent=projectPath;projectProgress();};
$('project-folder').onchange=async()=>{const problem=currentProblem;try{const values={};let bytes=0;for(const file of $('project-folder').files){const path=specification.contract.editable_files.find(p=>file.webkitRelativePath.endsWith('/'+p));if(!path)continue;if(path in values)throw new Error('目录含多个同名工程：'+path);bytes+=file.size;if(bytes>2*1024*1024)throw new Error('源码合计最大 2 MiB');values[path]=await file.text();}if(problem!==currentProblem)return;if(Object.keys(values).length!==4)throw new Error('目录必须包含完整四个源码文件');projectSources=values;$('source').value=values[projectPath];projectProgress();toast('已导入四个工程文件');}catch(error){toast(error.message);}};
$('file').onchange=()=>loadFile($('file').files[0]);
for(const event of ['dragenter','dragover'])$('dropzone').addEventListener(event,e=>{e.preventDefault();$('dropzone').classList.add('drag');});
for(const event of ['dragleave','drop'])$('dropzone').addEventListener(event,e=>{e.preventDefault();$('dropzone').classList.remove('drag');});
$('dropzone').addEventListener('drop',e=>loadFile(e.dataTransfer.files[0]));
$('submit-form').onsubmit=async(event)=>{event.preventDefault();$('submit').disabled=true;const problem=currentProblem;try{
  const suite=$('suite').value,indices=suite==='subset'?pickedCases():null;
  if(suite==='subset'&&!indices.length)throw new Error('请至少选择一个测试点');
  const payload={problem_id:problem,profile_mode:$('profile-mode').value,...submissionSource(),label:$('label').value||(isProject()?currentProblem+' project':'kernel.asc'),generator:suite==='custom'?$('generator').value:null,local_case_indices:indices,regression_suite:suite==='structured'?'pdist-wide-structured-v1':null,summary_only:true};
  const fingerprint=JSON.stringify(payload);if(!submitAttempt||submitAttempt.fingerprint!==fingerprint)submitAttempt={fingerprint,key:'web-'+crypto.randomUUID()};payload.request_id=submitAttempt.key;
  const value=await api('/api/submissions',{method:'POST',body:JSON.stringify(payload)});submitAttempt=null;
  if(problem===currentProblem){selected=value.id;poller.setSelected(selected);lastPreflight=null;showPanel('report');await refresh(true);}toast('已加入评测队列');
}catch(error){toast(error.message+'；网络错误时保留同一请求号，可安全重试。');}finally{$('submit').disabled=false;}};
$('suite').onchange=()=>{const value=$('suite').value;$('custom-data').classList.toggle('hidden',value!=='custom');$('subset-data').classList.toggle('hidden',value!=='subset');$('structured-note').classList.toggle('hidden',value!=='structured');$('generator').required=value==='custom';};
$('load-example').onclick=async()=>{const generation=poller.generation,problem=currentProblem;try{const example=await api('/api/generator-example?problem_id='+encodeURIComponent(problem));if(generation===poller.generation)$('generator').value=example.source;}catch(error){toast(error.message);}};
$('example-link').onclick=e=>{e.preventDefault();$('load-example').click();};
$('generator-file').onchange=async()=>{const file=$('generator-file').files[0];if(file){if(file.size>128*1024){toast('脚本最大 128 KiB');return;}$('generator').value=await file.text();}};
$('preflight').onclick=async()=>{const generation=poller.generation,source=$('source').value;try{const report=await api('/api/check',{method:'POST',body:JSON.stringify({...submissionSource(),problem_id:currentProblem})});if(generation!==poller.generation||source!==$('source').value)return;lastPreflight=report;renderChecks(report);showPanel('diagnostics');$('checks').scrollIntoView({behavior:'smooth'});toast(report.passed?'静态检查通过；运行时检查将在评测时执行':'静态检查发现问题');}catch(error){toast(error.message);}};
$('show-logs').onclick=async()=>{if(!selected)return;const identity=selected,generation=poller.generation;try{const data=await api('/api/submissions/'+identity+'/logs');if(generation!==poller.generation||identity!==selected)return;$('logs').textContent=Object.entries(data.logs).map(([name,text])=>'── '+name+' ──\n'+text).join('\n\n')||'暂无日志';$('logs-card').classList.remove('hidden');}catch(error){toast(error.message);}};
$('hide-logs').onclick=()=>$('logs-card').classList.add('hidden');
$('kernel-close').onclick=closeKernel;
$('kernel-dialog').addEventListener('close',()=>{kernelRequest++;$('kernel-code').textContent='';});
$('cancel').onclick=async()=>{try{await api('/api/submissions/'+selected+'/cancel',{method:'POST'});await refresh(true);}catch(error){toast(error.message);}};
$('set-baseline').onclick=async()=>{
  if(!selected||settingBaseline)return;
  const identity=selected,problem=currentProblem,generation=poller.generation;settingBaseline=true;renderBaselineButton();
  try{
    await api('/api/submissions/'+identity+'/baseline',{method:'POST'});
    if(generation===poller.generation){
      // Invalidate in-flight reads too; otherwise an old score response could arrive last.
      poller.setProblem(problem);poller.setSelected(identity);rows=[];pinnedReport=null;resetLeaderboard();
      const updatedGeneration=poller.generation;
      await refresh(true);
      if(updatedGeneration===poller.generation)await openSubmission(identity);
    }
    toast('已设置 '+problem+' 的对比基线，逐点加速比已更新');
  }catch(error){toast(error.message);}finally{settingBaseline=false;renderBaselineButton();}
};
$('refresh-history').onclick=async()=>{$('refresh-history').disabled=true;try{await refresh(true);}finally{$('refresh-history').disabled=false;}};
document.addEventListener('visibilitychange',()=>{if(!document.hidden&&timer)refresh();});

function showPanel(name){
 activePanel=name;document.querySelectorAll('[data-workspace-panel]').forEach(el=>el.classList.toggle('hidden',el.dataset.workspacePanel!==name||(el.id==='ab-card'&&currentProblem!=='pdist')));
 document.querySelectorAll('[data-workspace-tab]').forEach(el=>el.setAttribute('aria-selected',String(el.dataset.workspaceTab===name)));
 if(name==='rankings')refreshLeaderboard();if(name==='diagnostics')renderMetrics(selectedJob());
}
document.querySelectorAll('[data-workspace-tab]').forEach(el=>el.onclick=()=>showPanel(el.dataset.workspaceTab));
$('history-search').oninput=()=>render();
function configureCasePicker(draft){
 $('case-picker').replaceChildren();for(const spec of specification.cases){const label=node('label'),input=node('input');input.type='checkbox';input.value=spec.index;label.title=caseDescription(spec);label.append(input,node('span',String(spec.index).padStart(2,'0')));$('case-picker').append(label);}
 $('suite').querySelector('[value="structured"]').disabled=currentProblem!=='pdist';$('suite').querySelector('[value="subset"]').disabled=currentProblem!=='pdist';
 if(['structured','subset'].includes($('suite').value)&&currentProblem!=='pdist')$('suite').value='official_shapes';$('suite').onchange();
}
function pickedCases(){return [...$('case-picker').querySelectorAll('input:checked')].map(x=>Number(x.value));}
function renderQueue(job){
 const shown=job&&active.has(job.status);$('queue-strip').classList.toggle('hidden',!shown);if(!shown)return;
 const stages={worker_slot:'等待执行名额',compile_cache_single_flight:'等待相同源码编译',npu:'等待 NPU',compiling:'正在编译',connecting:'连接评测服务器',generating:'生成测试数据',running:'正在评测'};
 $('queue-label').textContent=stages[job.queue_stage]||names[job.status]||'等待状态更新';$('queue-position').textContent=Number.isInteger(job.ahead_count)?'前方 '+job.ahead_count+' 个任务':'服务器未提供准确队列位置';
}
function renderCosts(job){
 const root=$('cost-grid');root.replaceChildren();const ev=job?.result?.evaluation;if(!ev)return;
 const stages=ev.stages_seconds||{},items=[['编译',stages.compile,ev.compile_cache?.status==='hit'?'缓存命中':''],['等待设备',stages.device_wait,''],['正确性与 Event',stages.correctness_and_event,''],['设备采集',stages.profiler_PipeUtilization??stages.profiler_collect_PipeUtilization,'']];
 for(const [label,value,note] of items){if(!Number.isFinite(value)&&!note)continue;const item=node('div',undefined,'cost-item');item.append(node('span',label),node('strong',Number.isFinite(value)?value.toFixed(2)+' s':note));if(note&&Number.isFinite(value))item.append(node('small',note));root.append(item);}
 const ab=job?.result?.local_ab;$('ab-result').replaceChildren();if(ab){const details=node('details');details.append(node('summary','A/B 原始样本与配对差'),node('pre',JSON.stringify(ab,null,2)));$('ab-result').append(details);}
}
function diagnosticTable(root,headers,values){
 const wrap=node('div',undefined,'table-wrap'),table=node('table'),head=node('thead'),tr=node('tr');headers.forEach(h=>tr.append(node('th',h)));head.append(tr);table.append(head);const body=node('tbody');for(const row of values){const r=node('tr');row.forEach(v=>r.append(node('td',String(v??'—'))));body.append(r);}table.append(body);wrap.append(table);root.append(wrap);
}
function renderCaseDiagnostics(root,job,c){
 const item=job.result.metrics?.PipeUtilization?.cases?.find(x=>x.index===c.index),launch=item?.phase_metrics?.launch_breakdown;
 if(launch){root.append(node('h3','按启动顺序拆分'));const samples=launch.launches||launch.ordinals||launch.entries||[];
  if(Array.isArray(samples)&&samples.length)diagnosticTable(root,['序号','Kernel','均值','中位数','样本数'],samples.map((x,i)=>[x.ordinal??x.launch_ordinal??i+1,x.name??x.kernel_name,time(x.mean_us??x.mean),time(x.median_us??x.median),x.observations??x.count??x.samples]));
  else root.append(node('p',launch.unavailable_reason||launch.reason||launch.status||'未提供可用的启动序列','muted small'));
 }
 const diag=job.result.pdist_diagnostics?.cases?.find(x=>x.index===c.index);
 if(diag){const h=diag.host_calls;root.append(node('h3','Host 调用与同步诊断'));
  if(h?.status==='available'){const details=node('details');details.append(node('summary','分配 / 同步 / 释放 · 独立采样'),node('pre',JSON.stringify(h,null,2)));root.append(details);}else root.append(node('p','Host 耗时：'+(h?.reason||h?.unavailable_reason||'尚未独立采样'),'muted small'));
  const sync=diag.prevalidation_sync;if(sync?.available){const details=node('details');details.append(node('summary','查看校验阶段同步返回值'),node('pre',JSON.stringify(sync,null,2)));root.append(details);}
  root.append(node('p','独立诊断可能引入采集扰动，不计入分数。缺失数据保留未知。','footnote'));
 }
}
$('build-evidence').onclick=async()=>{if(!selected||currentProblem!=='pdist')return;const id=selected;$('build-evidence').disabled=true;try{const job=await api('/api/submissions/'+id+'?binary_layout=true');if(selected!==id)return;const root=$('build-evidence-content');root.replaceChildren();const details=node('details');details.open=true;details.append(node('summary','构建兼容性证据 · 不等于官方判定'),node('pre',JSON.stringify(job.result?.pdist_diagnostics?.binary_layout||{reason:'未采集'},null,2)));root.append(details);}catch(e){toast(e.message);}finally{$('build-evidence').disabled=currentProblem!=='pdist';}};
$('ab-form').onsubmit=async e=>{e.preventDefault();$('ab-submit').disabled=true;try{const indices=$('ab-indices').value.split(/[,，\s]+/).filter(Boolean).map(Number);if(indices.length<1||indices.length>3||indices.some(n=>!Number.isInteger(n)||n<1))throw new Error('填写 1–3 个测试点编号');const row=await api('/api/diagnostics/local-ab',{method:'POST',body:JSON.stringify({submission_a:$('ab-a').value.trim(),submission_b:$('ab-b').value.trim(),local_case_indices:indices,cycles:1,budget_seconds:Number($('ab-budget').value)})});selected=row.id;poller.setSelected(selected);await refresh(true);toast('A/B 诊断已入队');}catch(error){toast(error.message);}finally{$('ab-submit').disabled=false;}};

showPanel('report');
enter().catch(()=>showLogin());

$('copy-submission-id').onclick=async()=>{if(!selected)return;try{await navigator.clipboard.writeText(selected);toast('提交 ID 已复制');}catch{toast('无法访问剪贴板，请复制报告下方的提交 ID');}};

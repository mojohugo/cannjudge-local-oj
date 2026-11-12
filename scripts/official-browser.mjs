// Account-bound browser worker: each profile has its own lock and cooldown.
// Protocol output contains no browser credentials or account storage.
import { promises as fs } from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import { pathToFileURL } from 'node:url';
import { createHash } from 'node:crypto';
import { syncHistory, readProblem, pollOfficialResult } from './official-data.mjs';
import {validProblemUrl} from './official-project-format.mjs';
import {projectAction} from './official-project.mjs';
import {readDailyQuota} from './official-quota.mjs';

const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const digest = source => createHash('sha256').update(source).digest('hex');
const emit = event => process.stdout.write('OJ_OFFICIAL '+JSON.stringify(event)+'\n');

export function validateInventory(rows) {
  if (!rows.length || new Set(rows.map(row=>row.path)).size !== rows.length)
    throw new Error('无法确认编辑器文件清单，未提交');
  const editable=rows.filter(row=>!row.readonly);
  if(editable.length!==1 || editable[0].path!=='kernel.asc' || editable[0].userCreated)
    throw new Error('编辑器存在额外可编辑文件或旧草稿，请先在官方页面清理；未提交');
}

export async function selectKernelFile(page) {
  const row=page.locator('.open-editor-explorer-body [data-action="switch-file"][data-file="kernel.asc"]');
  if(await row.count()!==1)throw new Error('无法唯一确认 kernel.asc 文件，未提交');
  const active=async()=>(await row.getAttribute('class')||'').split(/\s+/).includes('active');
  // The official editor can collapse its file explorer while keeping kernel.asc
  // active. Clicking that hidden row times out even though the editor is ready.
  if(await active())return;
  await row.click();
  if(!await active())throw new Error('kernel.asc 未切换为当前文件，未提交');
}

export function officialId(url, problemUrl) {
  const prefix=problemUrl+'/submission/';
  if(!url.startsWith(prefix))return null;
  const id=url.slice(prefix.length).split(/[?#]/)[0];
  return /^[0-9a-f]{24}$/.test(id)?id:null;
}

export function rejection(message) {
  const rate=/(?:今日|当天|每日|频繁|冷却|间隔|quota|cooldown|too many|rate limit)/i.test(message);
  const match=message.match(/(\d+)\s*(秒|seconds?|分钟|minutes?)/i);
  const refused=/(?:格式错误|参数无效|不允许|禁止提交|超过.*限制|权限不足|invalid parameter|rejected|forbidden)/i.test(message);
  return {status:rate?'rate_limited':refused?'failed':'outcome_unknown',message,
          retry_after_seconds:match?Number(match[1])*(/分钟|minute/i.test(match[2])?60:1):null};
}

async function diagnostic(page) {
  const alerts=await page.getByRole('alert').allTextContents().catch(()=>[]);
  const dialogs=await page.locator('[role="dialog"]:visible,[aria-modal="true"]:visible,dialog[open]:visible,.modal-mask:visible,.modal:visible').allTextContents().catch(()=>[]);
  const body=await page.locator('body').innerText().catch(()=>'');
  const custom=body.match(/(?:^|\r?\n)错误信息\s*\r?\n([\s\S]*?)\r?\n确定(?:\r?\n|$)/)?.[1]?.trim();
  return [...new Set([...alerts,...dialogs,custom||''].map(s=>s.trim()).filter(Boolean))].join('\n').slice(0,6000);
}

export async function main(payload) {
  if(!['submit','result','check','history','prepare','quota'].includes(payload.action) ||
     !validProblemUrl(payload.problem_url))
    throw new Error('Invalid official target or action');
  if(payload.action==='result' && !/^[0-9a-f]{24}$/.test(payload.official_id||''))throw new Error('Invalid official ID');
  const helper=await import(pathToFileURL(payload.helper_script).href);
  const state=process.env.CANNJUDGE_BROWSER_STATE_DIR||path.join(process.env.LOCALAPPDATA||path.join(os.homedir(),'.local/share'),'CANNJudgeSubmit');
  await fs.mkdir(state,{recursive:true});
  let release,context,page,catalog;
  let attempted=false,id=payload.official_id||null;
  const finish=data=>emit({event:'done',...data});
  try {
    try{release=await helper.acquireLock(path.join(state,'submit.lock'));}
    catch{finish({status:'profile_busy',message:'一键提交工具正在使用登录会话，等待释放；未提交'});return;}
    if(payload.action==='submit'){
      let previous;
      try{previous=JSON.parse(await fs.readFile(path.join(state,'last-accepted.json'),'utf8'));}catch{}
      const remaining=previous?Date.parse(previous.acceptedAt)+182000-Date.now():0;
      if(remaining>0){
        emit({event:'progress',message:'等待此账号的官方提交间隔',retry_after_seconds:Math.ceil(remaining/1000)});
        await sleep(Math.min(remaining,182000));
      }
    }
    const {chromium}=helper.loadPlaywright();
    context=await chromium.launchPersistentContext(path.join(state,'browser-profile'),{
      channel:process.env.CANNJUDGE_BROWSER_CHANNEL||(process.platform==='win32'?'msedge':undefined),
      headless:true,chromiumSandbox:process.env.CANNJUDGE_BROWSER_SANDBOX!=='0',
      viewport:{width:1440,height:1000},timeout:25000
    });
    if(['prepare','quota'].includes(payload.action)) {
      // Operator-only verification: block writes as an extra guard. This action
      // is not exposed as a submission tool and never clicks the submit button.
      await context.route('https://cannjudge.cn/**',route=>
        ['GET','HEAD','OPTIONS'].includes(route.request().method())?route.continue():route.abort());
    }
    page=context.pages()[0]||await context.newPage();page.setDefaultTimeout(12000);
    if(payload.enforce_identity){
      let identity;
      try{identity=await readProblem(page,payload.problem_url);catalog=identity;}
      catch{finish({status:'login_required',message:'官方账户登录已失效或无法确认身份，请在账户管理中重新登录；未提交源码'});return;}
      if(!identity.userId || (payload.expected_user_id && identity.userId!==payload.expected_user_id)){
        finish({status:'login_required',message:'浏览器登录账户与任务绑定账户不一致，未提交；请重新登录原账户'});return;
      }
      emit({event:'account',user_id:identity.userId});
      if(payload.action==='submit'&&!payload.expected_user_id){
        finish({status:'login_required',message:'首次账户身份已读取；请先检查官方登录后再创建新提交，本次未上传源码'});return;
      }
    }
    if(payload.action==='quota') {
      const quota=await readDailyQuota(context,payload.expected_user_id,payload.quota_policy);
      finish({status:'ready',quota});return;
    }
    if(payload.action==='history') {
      if(!payload.problems||Object.entries(payload.problems).some(([key,value])=>!(/^[a-z0-9_]+$/.test(key)&&/^[a-z0-9_]+$/.test(value))))throw new Error('Invalid problem mapping');
      if(payload.problem_urls&&Object.values(payload.problem_urls).some(url=>!validProblemUrl(url)))throw new Error('Invalid history target');
      await syncHistory(context,page,payload,emit);return;
    }
    if(payload.submission_kind==='project') {await projectAction(context,page,payload,state,emit);return;}
    const target=payload.action==='result'?payload.problem_url+'/submission/'+id:payload.problem_url+'/submit';
    await page.goto(target,{waitUntil:'domcontentloaded',timeout:25000});
    if(payload.action!=='result'){
      const editor=page.getByRole('textbox',{name:'Ascend C 代码编辑器',exact:true});
      try{await editor.waitFor({state:'visible',timeout:15000});}
      catch{
        const loginVisible=await page.getByRole('link',{name:'前往登录',exact:true}).isVisible().catch(()=>false);
        const loginRoute=/\/auth\/login(?:[/?#]|$)/.test(page.url());
        finish({status:loginVisible||loginRoute?'login_required':'failed',message:loginVisible||loginRoute?
          '服务专用 CANNJudge 登录会话已失效，请联系服务管理员重新登录；未提交源码。':
          '官方代码编辑器尚未就绪，未发送提交请求',login_url:payload.problem_url+'/submit'});return;
      }
      if(payload.action==='check'){
        const rows=await page.locator('.open-editor-explorer-body .open-editor-tree-row.is-file[data-action="switch-file"]').evaluateAll(nodes=>nodes.map(node=>({path:node.getAttribute('data-file'),readonly:node.classList.contains('is-readonly'),userCreated:node.classList.contains('is-user-created')})));
        validateInventory(rows);
        finish({status:'ready',message:'独立登录会话有效，官方编辑器及文件清单检查通过；未提交源码',editable_files:['kernel.asc'],readonly_files:rows.filter(r=>r.readonly).map(r=>r.path)});return;
      }
      const source=await fs.readFile(payload.source_path,'utf8');
      if(digest(source)!==payload.source_sha256)throw new Error('待提交源码哈希不一致，未提交');
      const tree=page.locator('.open-editor-explorer-body .open-editor-tree-row.is-file[data-action="switch-file"]');
      const inventory=()=>tree.evaluateAll(nodes=>nodes.map(node=>({path:node.getAttribute('data-file'),readonly:node.classList.contains('is-readonly'),userCreated:node.classList.contains('is-user-created')})));
      validateInventory(await inventory());
      await selectKernelFile(page);
      await helper.fillAndVerify(page,context,source);
      validateInventory(await inventory());
      if(payload.action==='prepare') {
        finish({status:'ready',message:'登录、文件选择和源码回读校验通过；未提交官方评测',source_editor_verified:true,submitted:false});return;
      }
      emit({event:'progress',message:'源码与编辑器已核对，正在提交官方评测',source_editor_verified:true});
      // Exactly one click. A timeout never causes an automatic second POST.
      attempted=true;
      await page.getByRole('button',{name:'提交代码',exact:true}).click();
      await page.waitForURL(url=>Boolean(officialId(String(url),payload.problem_url)),{timeout:20000}).catch(()=>{});
      id=officialId(page.url(),payload.problem_url);
      if(!id){
        const message=await diagnostic(page);
        if(message){finish({...rejection(message),acceptance_confirmed:false});return;}
        finish({status:'outcome_unknown',message:'未确认官方提交记录；请求可能已到达官方，请先核实，禁止自动重发',acceptance_confirmed:false});return;
      }
      const accepted={url:payload.problem_url+'/submission/'+id,acceptedAt:new Date().toISOString()};
      emit({event:'submitted',official_id:id,official_url:accepted.url,message:'官方已接收提交，等待评测',acceptance_confirmed:true});
      await fs.writeFile(path.join(payload.archive_dir,'accepted.json'),JSON.stringify(accepted,null,2));
      await fs.writeFile(path.join(state,'last-accepted.json'),JSON.stringify(accepted,null,2));
    }
    catalog??=await readProblem(page,payload.problem_url);
    const record=await pollOfficialResult(context,{...payload,official_id:id},catalog.problem,emit);
    if(!record){finish({status:'awaiting_result',message:'官方评测仍未结束，请用 refresh=true 继续查询原记录'});return;}
    finish({status:'completed',result_record:record});
  }catch(error){
    const known=Boolean(id);
    finish({status:known?'awaiting_result':attempted?'outcome_unknown':'failed',
      message:known?'读取官方记录失败，请继续查询原官方 ID；不重新提交':attempted?
      '发送后的结果不确定，请核实官方记录；不会自动重发':'准备官方提交失败，尚未发送提交请求',
      error_phase:known?'result':attempted?'submission':'preparation',
      diagnostic:(!attempted&&!known?String(error.message).replace(/https?:\/\/\S+/g,'[URL]').slice(0,400):undefined)});
  }finally{
    if(context)await context.close().catch(()=>{});
    if(release)await release();
  }
}

if(process.argv[1]&&import.meta.url===pathToFileURL(path.resolve(process.argv[1])).href){
  let input='';for await(const chunk of process.stdin)input+=chunk;
  try{await main(JSON.parse(input));}catch{emit({event:'done',status:'failed',message:'官方提交辅助进程配置无效，未发送请求'});process.exitCode=1;}
}

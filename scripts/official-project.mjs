// Multi-file custom_template submissions. At most one POST per saved job.
import {promises as fs} from 'node:fs';
import path from 'node:path';
import {readProblem,pollOfficialResult} from './official-data.mjs';
import {bundleHash,validateProject,sha256,normalizeContent} from './official-project-format.mjs';

export async function projectAction(context,page,payload,state,emit) {
  let id=payload.official_id||null,attempted=false;
  const done=result=>emit({event:'done',...result});
  try {
    const catalog=await readProblem(page,payload.problem_url);
    if(!catalog.userId||catalog.userId!==payload.expected_user_id||catalog.problem._id!==payload.official_problem_id||catalog.problem.code_template!=='custom_template')throw new Error('官方账户、题目或工程类型不匹配');
    const response=await context.request.get('https://cannjudge.cn/api/problems/'+catalog.problem._id+'/template',{timeout:25000});
    if(!response.ok())throw new Error('读取官方模板失败');
    const template=(await response.json())?.data?.files;
    if(!Array.isArray(template)||!Array.isArray(payload.template_files))throw new Error('模板缺失');
    const stamp=rows=>JSON.stringify(rows.map(f=>[f.path,f.editable!==false,sha256(normalizeContent(f.content))]).sort((a,b)=>a[0]<b[0]?-1:1));
    if(stamp(template)!==stamp(payload.template_files))throw new Error('官方模板已更新，请刷新服务端模板后再提交');
    if(payload.action==='check'){done({status:'ready',message:'官方账户和多文件模板验证通过；未提交源码',editable_files:template.filter(f=>f.editable!==false).map(f=>f.path)});return;}
    const files=validateProject(JSON.parse(await fs.readFile(payload.source_path,'utf8')),template);
    if(bundleHash(files)!==payload.source_sha256)throw new Error('工程源码集合哈希不一致');
    if(payload.action==='prepare'){done({status:'ready',message:'账户、模板、四文件及集合哈希验证通过；未提交源码',submitted:false,source_editor_verified:true});return;}
    if(!id){
      // A crash/timeout after this marker must be reconciled by history, never by another POST.
      try {await fs.writeFile(path.join(payload.archive_dir,'project-attempt.json'),JSON.stringify({at:new Date().toISOString(),sha256:payload.source_sha256}),{flag:'wx',mode:0o600});}
      catch(error){if(error.code==='EEXIST'){done({status:'outcome_unknown',message:'此请求已有上传标记，请先核实官方历史；禁止重复上传'});return;}throw error;}
      emit({event:'progress',message:'工程文件和官方模板已核对，正在上传官方',source_editor_verified:true});
      attempted=true;
      const sent=await context.request.post('https://cannjudge.cn/api/submissions/submit',{
        headers:{Origin:'https://cannjudge.cn',Referer:payload.problem_url+'/submit'},
        data:{problemId:catalog.problem._id,userId:catalog.userId,files},timeout:120000});
      const result=await sent.json();
      id=result?.data?.submissionId;
      if(!sent.ok()||!/^[0-9a-f]{24}$/.test(id||'')){
        id=null;
        const message=String(result.msg||result.message||'官网未确认接收，请核实历史后处理').slice(0,2000);
        const rate=/(今日|当天|每日|频繁|冷却|间隔|quota|cooldown|too many)/i.test(message);
        done({status:rate?'rate_limited':sent.status()===401?'login_required':sent.status()>=400&&sent.status()<500?'failed':'outcome_unknown',message,acceptance_confirmed:false});return;
      }
      const accepted={official_id:id,url:payload.problem_url+'/submission/'+id,acceptedAt:new Date().toISOString()};
      emit({event:'submitted',official_id:id,official_url:accepted.url,message:'官方已接收工程，等待评测',acceptance_confirmed:true});
      await fs.writeFile(path.join(payload.archive_dir,'accepted.json'),JSON.stringify(accepted),{mode:0o600});
      await fs.writeFile(path.join(state,'last-accepted.json'),JSON.stringify(accepted),{mode:0o600});
    }
    const record=await pollOfficialResult(context,{...payload,official_id:id},catalog.problem,emit);
    if(!record){done({status:'awaiting_result',official_id:id,message:'官方评测仍未结束；刷新仅继续读取原记录'});return;}
    done({status:'completed',result_record:record});
  }catch(error){
    done({status:id?'awaiting_result':attempted?'outcome_unknown':'failed',official_id:id,
      message:id?'读取原官方记录失败，可继续刷新；不会重新上传':attempted?'上传结果不确定，请核实官方历史；不会自动重发':String(error.message).slice(0,400),
      error_phase:id?'result':attempted?'submission':'preparation'});
  }
}

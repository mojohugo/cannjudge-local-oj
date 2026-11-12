// User-operated login window. No source upload, submission, or credential export.
import readline from 'node:readline';
import { promises as fs } from 'node:fs';
import path from 'node:path';
import { loadPlaywright } from '../vendor/cannjudge-submit.mjs';
import { readProblem } from './official-data.mjs';

const send=value=>process.stdout.write(JSON.stringify(value)+'\n');
let context,page;
const viewport={width:1100,height:760};
const allowedHost=host=>['cannjudge.cn','gitcode.com','gitcode.net','csdn.net','huawei.com','huaweicloud.com'].some(base=>host===base||host.endsWith('.'+base));
const allowedWrite=url=>url.hostname!=='cannjudge.cn'||/^\/api\/users\/(?:login|captcha|oauth\/)/.test(url.pathname);
const keys=new Set(['Enter','Tab','Shift+Tab','Backspace','Delete','ArrowLeft','ArrowRight','ArrowUp','ArrowDown','Home','End','Control+a','Escape']);
async function close(){if(context)await context.close().catch(()=>{});context=null;}
process.on('SIGTERM',()=>{close().finally(()=>process.exit(0));});
async function frame(){
  if(page?.isClosed())page=context.pages().find(p=>!p.isClosed());
  if(!page)throw new Error('登录页面已关闭，请重新打开');
  const url=new URL(page.url());
  return {image:(await page.screenshot({type:'jpeg',quality:75})).toString('base64'),
          width:viewport.width,height:viewport.height,url:url.origin+url.pathname};
}
for await(const line of readline.createInterface({input:process.stdin,crlfDelay:Infinity})){
  let command;
  try{
    command=JSON.parse(line);
    if(command.action==='start'){
      if(context)throw new Error('登录窗口已经打开');
      await fs.mkdir(command.state_dir,{recursive:true,mode:0o700});
      const {chromium}=loadPlaywright();
      context=await chromium.launchPersistentContext(path.join(command.state_dir,'browser-profile'),{
        headless:true,viewport,channel:process.env.CANNJUDGE_BROWSER_CHANNEL||(process.platform==='win32'?'msedge':undefined),
        chromiumSandbox:process.env.CANNJUDGE_BROWSER_SANDBOX!=='0',timeout:25000});
      await context.route('**/*',route=>{
        const request=route.request(),url=new URL(request.url());
        // This login window cannot submit kernels, change OJ state or access private hosts.
        if(url.protocol!=='https:'||!allowedHost(url.hostname)||(!['GET','HEAD','OPTIONS'].includes(request.method())&&!allowedWrite(url)))return route.abort();
        return route.continue();
      });
      context.on('page',newPage=>{page=newPage;page.setDefaultTimeout(10000);});
      page=context.pages()[0]||await context.newPage();page.setDefaultTimeout(10000);
      await page.goto('https://cannjudge.cn/auth/login',{waitUntil:'domcontentloaded',timeout:25000});
    }else if(command.action==='frame'){
      if(!context)throw new Error('登录窗口已过期');
    }else if(command.action==='click'){
      if(!Number.isFinite(command.x)||!Number.isFinite(command.y)||command.x<0||command.y<0||command.x>viewport.width||command.y>viewport.height)throw new Error('无效坐标');
      await page.mouse.click(command.x,command.y);
    }else if(command.action==='text'){
      if(typeof command.text!=='string'||command.text.length>4096)throw new Error('输入过长');
      await page.keyboard.insertText(command.text);
    }else if(command.action==='key'){
      if(!keys.has(command.key))throw new Error('不支持该按键');
      await page.keyboard.press(command.key);
    }else if(command.action==='scroll'){
      if(!Number.isFinite(command.delta)||Math.abs(command.delta)>1500)throw new Error('无效滚动量');
      await page.mouse.wheel(0,command.delta);
    }else if(command.action==='verify'){
      const result=await readProblem(page,'https://cannjudge.cn/ccpc/ladder_2026_changchun/cum_sum');
      if(!result.userId)throw new Error('未检测到官方登录，请先完成登录');
      await close();send({ok:true,user_id:result.userId});break;
    }else if(command.action==='close'){
      await close();send({ok:true});break;
    }else throw new Error('不支持的登录操作');
    send({ok:true,...await frame()});
  }catch{
    // Browser exceptions may contain entered values/URLs. Return no raw diagnostics.
    send({ok:false,message:command?.action==='verify'?'尚未确认官方登录。请完成登录及验证码，再点击保存。':'登录页面暂不可用，请刷新画面或重新打开登录窗口。'});
  }
}
await close();

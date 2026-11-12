/* Account defaults and a user-operated official login screen. No credential export. */
let activeAccount='default',localValidationRequired=true,accountSettings=null;
let accountLoginId=null,accountLoginBusy=false,accountLoginTimer=null,accountLoginEpoch=0;
let accountLoginActions=Promise.resolve();
function accountUrl(url,identity){
  if(!/^\/api\/official\/(?:connection|problems|history|case-bests|sync|jobs)(?:[/?]|$)/.test(url))return url;
  const parsed=new URL(url,location.origin);
  if(identity===undefined&&/^\/api\/official\/(?:connection|sync)(?:[/?]|$)/.test(url))identity=activeAccount;
  if(identity&&!parsed.searchParams.has('account_id'))parsed.searchParams.set('account_id',identity);
  return parsed.pathname+parsed.search;
}
function quotaView(account){
  const q=account.daily_quota||{},known=Number.isInteger(q.remaining),fresh=q.status==='ready';
  const states={pending:'等待刷新',stale:'等待更新',profile_busy:'账户会话使用中，稍后更新',login_required:'请重新登录以更新额度',unavailable:'官网读取失败，稍后重试'};
  const detail=known?`今日已用 ${q.used} / ${q.limit} 次`:(q.limit?`每日上限 ${q.limit} 次`:'等待读取官网记录');
  return {q,known,fresh,remaining:known?String(q.remaining):'—',
    label:fresh?'今日剩余':known?'上次统计剩余':'今日剩余',
    state:fresh?'按官网记录统计':states[q.status]||'等待刷新',detail};
}
function renderAccountQuotas(){
  const cards=(accountSettings?.accounts||[]).map(a=>{
    const v=quotaView(a),q=v.q;
    return `<article class="quota-card ${a.id===activeAccount?'is-current ':''}${v.known&&q.remaining===0?'is-empty':''}" data-quota-account="${esc(a.id)}"><div class="quota-card-heading"><strong>${esc(a.label)}</strong>${a.id===activeAccount?'<span class="pill">当前</span>':''}</div><div class="quota-number"><span>${v.label}</span><b>${v.remaining}</b><span>次</span></div><p class="quota-detail">${esc(v.detail)}</p><small class="quota-status ${v.fresh?'':'is-stale'}">${esc(v.state)}</small><small class="muted">${q.checked_at?'更新于 '+esc(date(q.checked_at)):'尚未读取'}${q.reset_at?' · '+esc(q.date)+'（北京时间）':''}</small></article>`;
  }).join('');
  // Avoid repeatedly replacing an aria-live region during background polling.
  const grid=$('account-quotas');if(grid.innerHTML!==cards)grid.innerHTML=cards;
}
function renderAccountControls(settings){
  if(!settings)return false;
  const changed=activeAccount!==settings.active_account_id;
  activeAccount=settings.active_account_id;localValidationRequired=settings.local_validation_required;
  accountSettings=settings;
  const selector=$('official-account');const pending=selector.value;
  const role=()=>'';
  selector.innerHTML=settings.accounts.map(a=>{const v=quotaView(a);return `<option value="${esc(a.id)}" ${a.ready?'':'disabled'}>${esc(a.label)}${role(a.id)}${a.id===activeAccount?' · 当前':''}${a.ready?'':' · 未登录'}${v.known?` · ${v.fresh?'今日':'上次统计'}剩余 ${v.remaining} 次`:''}</option>`;}).join('');
  selector.value=!changed&&settings.accounts.some(a=>a.id===pending&&a.ready)?pending:activeAccount;
  const account=settings.accounts.find(a=>a.id===activeAccount);
  $('account-status').textContent=account?`${account.label}${account.official_user_id?' · 官方 UID '+account.official_user_id:''}${account.verified_at?' · 最近验证 '+date(account.verified_at):' · 可点击“检查官方登录”确认身份'}`:'';
  $('local-validation-toggle').checked=localValidationRequired;
  $('validation-setting-note').textContent=localValidationRequired?'开启：同份源码在当前 NPU 后端完整通过后才上传。':'关闭：新任务直接上传官网，未经过本地正确性检查。';
  $('policy-title').textContent=localValidationRequired?'先本地验证，再提交官方':'直接提交官方';
  $('policy-description').textContent=`提交账户：${account?.label||'—'}${role(activeAccount)}。${localValidationRequired?'本地检查失败不会上传官方。':'本地正确性检查已关闭，官方提交仍会消耗官网额度。'}成绩与预计分数均依据官方耗时和官方 TBest。`;
  $('submit-note').textContent=localValidationRequired?'本地检查失败时不会上传官方。重复点击同一份请求会自动去重。':'此设置只影响新任务；关闭检查不代表本地通过。重复点击同一份请求会自动去重。';
  updateSubmitButton();
  renderAccountQuotas();
  if($('accounts-dialog').open)renderAccountList();
  return changed;
}
function renderAccountList(){
  $('account-list').innerHTML=(accountSettings?.accounts||[]).map(a=>{const v=quotaView(a);return `<div class="account-row"><div><strong>${esc(a.label)}${a.id===activeAccount?' <span class="pill">当前</span>':''}</strong><small>${a.official_user_id?'官方 UID '+esc(a.official_user_id):a.ready?'原有登录会话':'尚未登录'}${a.verified_at?' · '+esc(date(a.verified_at)):''}</small><small>${v.label} ${v.remaining} 次 · ${esc(v.detail)} · ${esc(v.state)}</small></div><div class="account-row-actions">${a.ready&&a.id!==activeAccount?`<button class="secondary" data-account-activate="${esc(a.id)}">切换到此账户</button>`:''}<button class="plain" data-account-login="${esc(a.id)}">${a.ready?'重新登录':'登录'}</button></div></div>`;}).join('');
}
async function activateOfficialAccount(id){
  const settings=await api(`/api/official/accounts/${encodeURIComponent(id)}/activate`,{method:'POST',body:'{}'});
  renderAccountControls(settings);await refresh({force:true});
  toast('已切换提交账户；历史与榜单保持合并，已排队任务继续使用原账户');
}
function showLoginFrame(result){
  $('official-login-image').src='data:image/jpeg;base64,'+result.image;
  $('official-login-image').dataset.width=result.width;$('official-login-image').dataset.height=result.height;
  $('official-login-url').textContent=result.url;
}
async function openOfficialLogin(id){
  const epoch=++accountLoginEpoch;
  const label=accountSettings?.accounts.find(a=>a.id===id)?.label||'新账户';
  $('official-login-title').textContent='登录 CANNJudge · '+label;
  $('official-login-image').removeAttribute('src');$('official-login-url').textContent='https://cannjudge.cn/auth/login';
  $('official-login-status').textContent='正在打开官网登录窗口…';$('save-official-login').disabled=true;
  if(!$('official-login-dialog').open)$('official-login-dialog').showModal();
  try{
    const result=await api(`/api/official/accounts/${encodeURIComponent(id)}/login`,{method:'POST',body:'{}'});
    if(epoch!==accountLoginEpoch||!$('official-login-dialog').open){await api(`/api/official/login/${result.session_id}/close`,{method:'POST',body:'{}'});return;}
    accountLoginId=result.session_id;showLoginFrame(result);$('save-official-login').disabled=false;
    $('official-login-status').textContent='点击画面中的输入框填写信息。验证码需手动填写，完成后点击“保存登录”。';
    accountLoginTimer=setInterval(()=>{if(accountLoginId&&!accountLoginBusy&&!document.hidden)refreshLoginFrame().catch(()=>{});},2000);
  }catch(error){if(epoch===accountLoginEpoch)$('official-login-status').textContent=error.message;}
}
async function refreshLoginFrame(){
  if(!accountLoginId||accountLoginBusy)return;
  const id=accountLoginId;accountLoginBusy=true;
  try{const result=await api(`/api/official/login/${id}`);if(accountLoginId===id)showLoginFrame(result);}
  catch(error){if(accountLoginId===id)$('official-login-status').textContent=error.message;throw error;}
  finally{accountLoginBusy=false;}
}
function loginAction(action,fields={}){
  const id=accountLoginId;if(!id)return Promise.resolve();
  // Preserve ordering for clicks, typed text and key presses even on a slow link.
  accountLoginActions=accountLoginActions.catch(()=>{}).then(async()=>{
    if(accountLoginId!==id)return;
    accountLoginBusy=true;
    try{const result=await api(`/api/official/login/${id}/action`,{method:'POST',body:JSON.stringify({action,...fields})});if(accountLoginId===id)showLoginFrame(result);}
    catch(error){if(accountLoginId===id)$('official-login-status').textContent=error.message;}
    finally{accountLoginBusy=false;}
  });return accountLoginActions;
}
function setupAccountControls(){
  $('refresh-quotas').onclick=run(async()=>{
    const button=$('refresh-quotas');button.disabled=true;button.textContent='请求中…';
    try{renderAccountControls(await api('/api/official/accounts?refresh_quotas=true'));toast('已请求更新所有账户额度；官方队列空闲时自动读取');}
    finally{button.disabled=false;button.textContent='刷新额度';}
  });
  $('switch-account').onclick=run(async()=>{const b=$('switch-account');b.disabled=true;try{await activateOfficialAccount($('official-account').value);}finally{b.disabled=false;}});
  $('manage-accounts').onclick=run(async()=>{renderAccountControls(await api('/api/official/accounts'));renderAccountList();$('accounts-dialog').showModal();});
  $('close-accounts').onclick=()=>$('accounts-dialog').close();
  $('local-validation-toggle').onchange=run(async()=>{
    const toggle=$('local-validation-toggle'),required=toggle.checked;toggle.disabled=true;
    try{renderAccountControls(await api('/api/official/settings',{method:'POST',body:JSON.stringify({local_validation_required:required})}));toast(required?'新提交将先检查本地正确性':'新提交将直接上传官方，已排队任务不变');}
    finally{toggle.checked=localValidationRequired;toggle.disabled=false;}
  });
  $('account-list').onclick=run(async event=>{
    const activate=event.target.closest('[data-account-activate]')?.dataset.accountActivate;
    const login=event.target.closest('[data-account-login]')?.dataset.accountLogin;
    if(activate)await activateOfficialAccount(activate);
    else if(login)await openOfficialLogin(login);
  });
  $('add-account-form').onsubmit=run(async()=>{
    const button=$('add-account-form').querySelector('button');button.disabled=true;
    try{const account=await api('/api/official/accounts',{method:'POST',body:JSON.stringify({label:$('account-label').value})});$('account-label').value='';renderAccountControls(await api('/api/official/accounts'));await openOfficialLogin(account.id);}
    finally{button.disabled=false;}
  });
  $('close-official-login').onclick=()=>$('official-login-dialog').close();
  $('official-login-dialog').addEventListener('close',()=>{
    accountLoginEpoch++;clearInterval(accountLoginTimer);const id=accountLoginId;accountLoginId=null;
    $('official-login-image').removeAttribute('src');$('official-login-text').value='';
    if(id)api(`/api/official/login/${id}/close`,{method:'POST',body:'{}'}).catch(()=>{});
  });
  $('official-login-image').onclick=event=>{
    const img=$('official-login-image'),rect=img.getBoundingClientRect();img.focus();
    loginAction('click',{x:(event.clientX-rect.left)*Number(img.dataset.width)/rect.width,y:(event.clientY-rect.top)*Number(img.dataset.height)/rect.height});
  };
  $('official-login-image').onkeydown=event=>{
    if(event.isComposing)return;
    const keys=['Enter','Tab','Backspace','Delete','ArrowLeft','ArrowRight','ArrowUp','ArrowDown','Home','End'];
    if((event.ctrlKey||event.metaKey)&&event.key.toLowerCase()==='a'){event.preventDefault();loginAction('key',{key:'Control+a'});}
    else if(keys.includes(event.key)){event.preventDefault();loginAction('key',{key:event.key==='Tab'&&event.shiftKey?'Shift+Tab':event.key});}
    else if(event.key.length===1&&!event.ctrlKey&&!event.metaKey&&!event.altKey){event.preventDefault();loginAction('text',{text:event.key});}
  };
  $('official-login-image').onpaste=event=>{event.preventDefault();loginAction('text',{text:event.clipboardData.getData('text/plain')});};
  $('official-login-image').addEventListener('wheel',event=>{event.preventDefault();loginAction('scroll',{delta:Math.max(-1500,Math.min(1500,event.deltaY))});},{passive:false});
  $('official-login-type').onclick=()=>{const text=$('official-login-text').value;$('official-login-text').value='';loginAction('text',{text});};
  $('official-login-text').onkeydown=event=>{if(event.key==='Enter'){event.preventDefault();$('official-login-type').click();}};
  $('official-login-tab').onclick=()=>loginAction('key',{key:'Tab'});
  $('official-login-enter').onclick=()=>loginAction('key',{key:'Enter'});
  $('official-login-refresh').onclick=run(refreshLoginFrame);
  $('save-official-login').onclick=run(async()=>{
    const id=accountLoginId;if(!id)return;const button=$('save-official-login');button.disabled=true;clearInterval(accountLoginTimer);
    await accountLoginActions;accountLoginBusy=true;$('official-login-status').textContent='正在验证官网登录身份并保存…';
    try{const result=await api(`/api/official/login/${id}/finish`,{method:'POST',body:'{}'});accountLoginId=null;$('official-login-dialog').close();renderAccountControls(await api('/api/official/accounts'));$('account-manager-message').textContent=result.message;toast(result.message);}
    catch(error){$('official-login-status').textContent=error.message;throw error;}
    finally{accountLoginBusy=false;button.disabled=false;if(accountLoginId)accountLoginTimer=setInterval(()=>{if(!accountLoginBusy&&!document.hidden)refreshLoginFrame().catch(()=>{});},2000);}
  });
}

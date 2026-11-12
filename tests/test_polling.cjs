const test=require('node:test');
const assert=require('node:assert/strict');
const path=require('node:path');
const {SubmissionPoller}=require(process.env.OJ_POLLING_FILE||path.join(__dirname,'../web/polling.js'));

function row(id,status='accepted',updated=1,problem='cum_sum'){
  return {id,status,updated,problem_id:problem,result:{cases:[],message:''}};
}
function deferred(){let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {promise,resolve,reject};}

test('only active records refresh between bounded history reads; unchanged reports do not render',async()=>{
  let clock=0,version=1,status='running',renders=0;
  const calls=[];
  const poller=new SubmissionPoller({now:()=>clock,onRows:()=>renders++,request:async p=>{
    calls.push(p);
    return p.includes('?')?{submissions:[row('live',status,version),...Array.from({length:99},(_,i)=>row(String(i)))]}:row('live',status,version);
  }});
  poller.setProblem('cum_sum');await poller.refresh();
  for(clock=3000;clock<30000;clock+=3000)await poller.refresh();
  assert.equal(calls.filter(p=>p.includes('?')).length,1);
  assert.equal(calls.filter(p=>!p.includes('?')).length,9);
  assert.equal(renders,1);
  await poller.refresh();assert.equal(calls.filter(p=>p.includes('?')).length,2);
  clock=33000;version=2;status='accepted';await poller.refresh();
  assert.equal(poller.rows[0].status,'accepted');assert.equal(renders,3);
  const previous=calls.length;clock=36000;await poller.refresh();assert.equal(calls.length,previous);
});

test('slow refreshes are single-flight; a concurrent manual refresh is preserved',async()=>{
  const first=deferred();let calls=0;
  const poller=new SubmissionPoller({onRows:()=>{},request:async()=>{calls++;return calls===1?first.promise:{submissions:[]};}});
  poller.setProblem('cum_sum');
  const a=poller.refresh();await Promise.resolve();
  assert.equal(poller.refresh(),a);
  assert.equal(poller.refresh({forceHistory:true}),a);
  assert.equal(calls,1);
  first.resolve({submissions:[]});await a;
  assert.equal(calls,2);
});

test('late results cannot cross a problem switch or an A-B-A switch',async()=>{
  const old=deferred();let calls=0;const renders=[];let oldSignal;
  const poller=new SubmissionPoller({onRows:r=>renders.push(r.map(x=>x.id)),request:async(p,o)=>{
    calls++;if(calls===1){oldSignal=o.signal;return old.promise;}return {submissions:[row('new')]};
  }});
  poller.setProblem('cum_sum');const first=poller.refresh();await Promise.resolve();
  poller.setProblem('log_sigmoid');poller.setProblem('cum_sum');
  await poller.refresh();old.resolve({submissions:[row('stale')]});await first;
  assert.equal(oldSignal.aborted,true);assert.deepEqual(renders,[['new']]);
  assert.equal(poller.rows[0].id,'new');
});

test('failed reads retain data and back off; manual refresh can retry immediately',async()=>{
  let clock=0,calls=0,fail=false;
  const poller=new SubmissionPoller({now:()=>clock,onRows:()=>{},request:async p=>{
    calls++;if(fail)throw new Error('offline');return {submissions:[row('kept')]};
  }});
  poller.setProblem('cum_sum');await poller.refresh();fail=true;clock=30000;
  await assert.rejects(poller.refresh(),/offline/);assert.equal(poller.rows[0].id,'kept');
  const attempts=calls;await poller.refresh();assert.equal(calls,attempts);
  fail=false;await poller.refresh({forceHistory:true});assert.equal(calls,attempts+1);
});

test('active detail reads have concurrency two and prioritize the selected record',async()=>{
  let count=0,maximum=0;const order=[];
  const poller=new SubmissionPoller({onRows:()=>{},request:async p=>{
    if(p.includes('?'))return {submissions:Array.from({length:7},(_,i)=>row(String(i),'queued'))};
    const id=p.split('/').at(-1);order.push(id);count++;maximum=Math.max(maximum,count);
    await new Promise(r=>setTimeout(r,5));count--;return row(id,'running',2);
  }});
  poller.setProblem('cum_sum');await poller.refresh();poller.setSelected('5');await poller.refresh();
  assert.equal(maximum,2);assert.equal(order[0],'5');
  assert.ok(poller.rows.every(r=>r.status==='running'));
});

test('mismatched detail responses never replace another problem or submission',async()=>{
  const poller=new SubmissionPoller({onRows:()=>{},request:async p=>p.includes('?')?
    {submissions:[row('original','running')]}:row('original','accepted',2,'log_sigmoid')});
  poller.setProblem('cum_sum');await poller.refresh();
  await assert.rejects(poller.refresh(),/不匹配/);assert.equal(poller.rows[0].status,'running');
});

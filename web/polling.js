/* Read-only progress polling; uses the existing API without restarting workers. */
(function(root,factory){
  if(typeof module==='object'&&module.exports)module.exports=factory();
  else root.OJPolling=factory();
})(typeof globalThis!=='undefined'?globalThis:this,function(){
  'use strict';
  const active=new Set(['queued','connecting','waiting','generating','compiling','running']);
  class SubmissionPoller {
    constructor({request,onRows,now=Date.now,historyInterval=30000,compact=false}){
      this.request=request;this.onRows=onRows;this.now=now;this.historyInterval=historyInterval;
      this.compact=compact;
      this.generation=0;this.problem='';this.rows=[];this.lastHistory=-Infinity;
      this.inflight=null;this.controller=null;this.forceHistory=false;
      this.failures=0;this.nextAttempt=0;this.selected=null;
    }
    setProblem(problem){
      if(this.controller)this.controller.abort();
      this.generation++;this.problem=problem;this.rows=[];this.lastHistory=-Infinity;
      this.inflight=null;this.controller=null;this.forceHistory=false;
      this.failures=0;this.nextAttempt=0;this.selected=null;
    }
    setSelected(identity){this.selected=identity;}
    refresh({forceHistory=false}={}){
      if(!this.problem)return Promise.resolve();
      this.forceHistory=this.forceHistory||forceHistory;
      if(this.inflight)return this.inflight;
      if(!this.forceHistory&&this.now()<this.nextAttempt)return Promise.resolve();
      const generation=this.generation, problem=this.problem;
      const controller=new AbortController();this.controller=controller;
      const options={signal:controller.signal};
      const current=()=>generation===this.generation;
      // Defer work one microtask so inflight is set even for an empty refresh.
      const pending=Promise.resolve().then(async()=>{
        do {
          const history=this.forceHistory||this.now()-this.lastHistory>=this.historyInterval;
          this.forceHistory=false;
          if(history){
            const result=await this.request('/api/submissions?problem_id='+encodeURIComponent(problem)+(this.compact?'&summary_only=true':''),options);
            if(!current())return;
            const old=new Map(this.rows.map(row=>[row.id,row]));
            this.rows=result.submissions.map(row=>{
              const previous=old.get(row.id);
              return this.compact&&previous&&!previous._summary&&previous.updated===row.updated&&previous.status===row.status?
                {...previous,validation:row.validation??previous.validation,queue_stage:row.queue_stage,ahead_count:row.ahead_count}:row;
            });
            this.lastHistory=this.now();
            this.onRows(this.rows);
          }else{
            const targets=this.rows.filter(row=>active.has(row.status)&&(!this.compact||row.id===this.selected));
            targets.sort((a,b)=>Number(b.id===this.selected)-Number(a.id===this.selected));
            const updates=new Map();let failure=null;
            // A page opens at most two concurrent read requests, even for a full queue.
            for(let start=0;start<targets.length;start+=2){
              const batch=targets.slice(start,start+2);
              const results=await Promise.allSettled(batch.map(row=>this.request('/api/submissions/'+row.id,options)));
              if(!current())return;
              for(let index=0;index<results.length;index++){
                const result=results[index];
                if(result.status==='rejected'){failure=failure||result.reason;continue;}
                const before=batch[index], after=result.value;
                if(after.id!==before.id||after.problem_id!==problem){
                  failure=failure||new Error('评测记录与当前题目不匹配');continue;
                }
                if(after.updated!==before.updated||after.status!==before.status)updates.set(after.id,after);
              }
            }
            if(updates.size){
              this.rows=this.rows.map(row=>updates.get(row.id)||row);
              this.onRows(this.rows);
            }
            if(failure)throw failure;
          }
          this.failures=0;this.nextAttempt=0;
        }while(current()&&this.forceHistory);
      }).catch(error=>{
        if(!current()||error.name==='AbortError')return;
        this.failures++;
        this.nextAttempt=this.now()+Math.min(30000,3000*2**Math.min(this.failures-1,4));
        throw error;
      }).finally(()=>{
        if(this.inflight===pending){this.inflight=null;this.controller=null;}
      });
      this.inflight=pending;
      return pending;
    }
  }
  return {SubmissionPoller};
});

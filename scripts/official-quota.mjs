// Read only the logged-in account's official history. No submission request.
export function beijingDay(now) {
  const date=new Date(now+8*3600000).toISOString().slice(0,10);
  const start=Date.parse(date+'T00:00:00+08:00');
  return {date,start,end:start+86400000};
}

export async function readDailyQuota(context,userId,policy) {
  if(!/^[a-f0-9]{24}$/.test(userId)||!Number.isSafeInteger(policy.daily_limit)||policy.daily_limit<1)
    throw Error('Invalid quota identity or policy');
  const ids=new Set();let day,checkedAt,total=null,previous=Infinity,used=0;
  for(let skip=0;skip<10000;) {
    const response=await context.request.get(`https://cannjudge.cn/api/submissions/user/${userId}?withCount=1&limit=100&skip=${skip}&order=desc`,{timeout:20000});
    if(!response.ok())throw Error('Official history unavailable');
    const data=await response.json(), rows=data.list;
    if(!Array.isArray(rows)||!Number.isSafeInteger(data.total)||data.total<0||skip+rows.length>data.total)throw Error('Unknown history format');
    if(!day){
      const serverTime=Date.parse(response.headers().date||'');
      checkedAt=Number.isFinite(serverTime)?serverTime:Date.now();day=beijingDay(checkedAt);total=data.total;
    }else if(data.total!==total)throw Error('History changed while counting; retry later');
    let reachedOlder=false;
    for(const row of rows) {
      const at=Date.parse(row.create_time);
      if(row.user_id!==userId||!/^[a-f0-9]{24}$/.test(row._id)||!Number.isFinite(at)||at>previous||ids.has(row._id))
        throw Error('Incomplete or inconsistent account history');
      previous=at;ids.add(row._id);
      if(at<day.start)reachedOlder=true;
      else if(at<day.end)used++;
    }
    skip+=rows.length;
    if(reachedOlder||skip>=total) return {date:day.date,used,limit:policy.daily_limit,
      remaining:Math.max(0,policy.daily_limit-used),checked_at:checkedAt/1000,reset_at:day.end/1000,
      timezone:'Asia/Shanghai',scope:'account',estimated:true,source:'official_history',limit_source:policy.limit_source};
    if(!rows.length)throw Error('Incomplete official history');
  }
  throw Error('Official history count exceeds read bound');
}

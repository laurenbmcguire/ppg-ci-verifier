const test = require('node:test'), assert = require('node:assert/strict'), vm = require('node:vm'), fs = require('node:fs');
const src = fs.readFileSync(process.env.PPG_UI_SOURCE || 'web/app.js', 'utf8');
const start = src.indexOf('  function fdAfterAI('), end = src.indexOf('  function fdSummaryWhenReady(', start);
async function run(items, whole=true) {
  const state = {report:{file_hash:'h'}, fixes:[], fdAIWhole:whole}, calls=[], messages=[], writes=[];
  const ctx=vm.createContext({state, store:{get:(_,d)=>d,set:(...a)=>writes.push(a)},
    fdItems:()=>items, versionReadOnly:()=>false, drawSide:()=>{}, fdActiveBusy:()=>false,
    findingBinding:()=>({}), fdSendDecision:async x=>{calls.push(x); for(const it of items)if(x.ids.includes(it.key))it.state='kept';},
    setTimeout:fn=>{fn();return 1;}, document:{getElementById:()=>null}, toast:x=>messages.push(x),
    plural:(n,s)=>`${n} ${s}${n===1?'':'s'}`, decisionsKey:()=>'', fdAIDoneDialog:()=>{throw Error('Unexpected dialog');}});
  vm.runInContext(src.slice(start,end),ctx);
  ctx.fdAfterAI({hash:'h',fixed:0,unfixed:items.map(x=>({key:x.key}))},state.report);
  for(let n=0;n<10;n++) await Promise.resolve();
  return {state,calls,messages,writes};
}
test('whole-report AI cannot dismiss serious or SharePoint findings without evidence',async()=>{
  const items=[{key:'high',fp:'1',state:'open',tier:'blocker',issue:{severity:'high',code:'missing_evidence'}},
    {key:'source',fp:'2',state:'open',source:'SharePoint',issue:{source:'SharePoint',code:'quantity_mismatch'}}];
  const h=await run(items);assert.equal(h.calls.length,0);assert.ok(items.every(x=>x.state==='open'));
  assert.match(h.messages[0],/2 errors still need you/);assert.doesNotMatch(h.messages[0],/No possible errors left/);
});
test('selected-scope AI cannot dismiss serious findings either',async()=>{
 const items=[{key:'high',fp:'1',state:'open',issue:{severity:'high'}}];const h=await run(items,false);
 assert.equal(h.calls.length,0);assert.equal(items[0].state,'open');
});
test('unapplied guessed proposals and selected findings remain unresolved',async()=>{
 const items=[{key:'guess',state:'open',fix:{id:'p',reason:'guess'}},{key:'selected',fp:'1',state:'selected',issue:{severity:'high'}}];
 const h=await run(items);assert.equal(h.writes.length,0);assert.equal(h.calls.length,0);assert.equal(items[0].state,'open');assert.equal(items[1].state,'selected');
});
test('nothing is set aside automatically, not even a format-only hint',async()=>{
 const items=[{key:'format',fp:'1',state:'open',issue:{code:'identifier_format_differs',severity:'low'}},
 {key:'part',fp:'3',state:'open',issue:{code:'part_format_differs',severity:'low'}},
 {key:'source',fp:'2',state:'open',source:'SharePoint',issue:{source:'SharePoint',code:'quantity_mismatch'}}];
 const h=await run(items);assert.equal(h.calls.length,0);assert.ok(items.every(x=>x.state==='open'));
 assert.equal(h.state.fdAIRun.leftAsWritten,0);assert.equal(h.state.fdAIRun.unsolved.length,3);
 assert.match(h.messages[0],/3 errors still need you/);
});

test('the reported case: 2 cells fixed, 57 not corrected, and 57 still need a person',async()=>{
 const items=Array.from({length:57},(_,n)=>({key:'k'+n,fp:'f'+n,state:'open',issue:{code:'missing_defect_reason',severity:'medium',sheet:'1st Shift',row:16+n},text:'No defect reason'}));
 const state={report:{file_hash:'h'},fixes:[],fdAIWhole:true},calls=[],messages=[];
 const ctx=vm.createContext({state,store:{get:(_,d)=>d,set:()=>{}},fdItems:()=>items,versionReadOnly:()=>false,drawSide:()=>{},fdActiveBusy:()=>false,
  findingBinding:()=>({}),fdSendDecision:async x=>{calls.push(x);for(const it of items)if(x.ids.includes(it.key))it.state='kept';},
  setTimeout:fn=>{fn();return 1;},document:{getElementById:()=>null},toast:x=>messages.push(x),plural:(n,s)=>`${n} ${s}${n===1?'':'s'}`,decisionsKey:()=>'',fdAIDoneDialog:()=>{throw Error('Unexpected dialog');}});
 vm.runInContext(src.slice(start,end),ctx);
 ctx.fdAfterAI({hash:'h',fixed:2,unfixed:items.map(x=>({key:x.key}))},state.report);
 for(let n=0;n<10;n++) await Promise.resolve();
 assert.equal(calls.length,0);assert.ok(items.every(x=>x.state==='open'));
 assert.equal(state.fdAIRun.fixed,2);assert.equal(state.fdAIRun.unsolved.length,57);assert.equal(state.fdAIRun.leftAsWritten,0);
 assert.match(messages[0],/Dex fixed 2 cells/);assert.match(messages[0],/57 errors still need you/);assert.doesNotMatch(messages[0],/No possible errors left/);
 // the summary box: even handed a stale zero it cannot show fewer than the uncorrected findings that are still open
 const a=src.indexOf('  function fdRevisionSummary('),b=src.indexOf('  function fdAIRunBanner(',a);
 const text=n=>n==null||n===false?'':Array.isArray(n)?n.map(text).join(' '):typeof n==='object'?text(n.kids):String(n);
 const sctx=vm.createContext({state:{report:{file_hash:'h',filename:'r.xlsx'}},fdItems:()=>items,h:(tag,attrs,...kids)=>({tag,attrs,kids}),plural:(n,s)=>`${n} ${s}${n===1?'':'s'}`,
  fdManualStart:()=>{},fdReviewDialog:()=>{},drawReport:()=>{}});
 vm.runInContext(src.slice(a,b)+'\nthis.summary=fdRevisionSummary;',sctx);
 const shown=text(sctx.summary(state.fdAIRun,0,null)).replace(/\s+/g,' ');
 assert.match(shown,/2 cells fixed by Dex/);assert.match(shown,/57 errors Dex could not correct: still need review/);
 assert.match(shown,/57 possible errors still need you/);assert.doesNotMatch(shown,/(^| )0 possible errors still need you/);
 assert.doesNotMatch(shown,/left as written/);assert.doesNotMatch(shown,/Nothing is left to fix/);
});

test('medium missing values are not silently treated as formatting',async()=>{
 const items=[{key:'part',fp:'1',state:'open',issue:{code:'missing_part_number',severity:'medium'}},{key:'reject',fp:'2',state:'open',issue:{code:'missing_defect_reason',severity:'medium'}}];
 const h=await run(items);assert.equal(h.calls.length,0);assert.ok(items.every(x=>x.state==='open'));
});

test('Done cannot override unresolved counts or findings even if a stale summary says clear',()=>{
 const a=src.indexOf('  const queueZero ='),b=src.indexOf('  // The same report',a);
 const ctx=vm.createContext({});vm.runInContext(src.slice(a,b)+'\nthis.done=queueDone;',ctx);
 for(const severity of ['high','medium','low']){
  const r={review:{status:'clear',counts:{[severity]:1},coverage:{},sharepoint:{}}};
  assert.equal(ctx.done(r),false);r.review.counts={};r.review.issues=[{severity}];assert.equal(ctx.done(r),false);
 }
});

test('a report with recovered findings is not Done, whatever the source comparison says',()=>{
 const a=src.indexOf('  const queueZero ='),b=src.indexOf('  // The same report',a);
 const ctx=vm.createContext({});vm.runInContext(src.slice(a,b)+'\nthis.done=queueDone;',ctx);
 for(const sp of [{status:'day_level'},{status:'match',source_verified:true},{status:'differences'},{}]){
  for(const status of ['hold','check','clear']){
   const r={review:{status,counts:{high:31,medium:26,low:0,info:3},coverage:{},sharepoint:sp,reconcile:{open:57,ai_fallback_unresolved:57}}};
   assert.equal(ctx.done(r),false,JSON.stringify([sp,status]));
  }
 }
});

test('a card says plainly when a saved decision needs a person to look, and never credits the fallback to a reviewer',()=>{
 const a=src.indexOf('      // A record left by the automatic Fix with AI fallback'),b=src.indexOf('\n',src.indexOf("' Review needed: '",a));
 assert.ok(a>0&&b>a,'the card shows both notes');
 const flat=x=>Array.isArray(x)?x.map(flat).join(''):x==null?'':typeof x==='object'?flat(x.children):String(x);
 const lines=it=>Array.from(vm.runInNewContext('['+src.slice(a,b)+']',{it,h:(tag,props,...children)=>({tag,props,children}),icon:()=>''})).filter(Boolean).map(flat);
 const fallback=lines({previous:{note:'Left by Fix with AI, not checked by a person.',action:'reject',by:'Example Reviewer',reason:'Dex had no safe change to make, so this was left as written by Fix with AI.'},issue:{ai_fallback_not_review:true}});
 assert.equal(fallback.length,1);assert.match(fallback[0],/Saved record: reject, under the name Example Reviewer/);assert.doesNotMatch(fallback[0],/by Example Reviewer|by a reviewer|Earlier:/);
 const human=lines({previous:{note:'This finding came back.',action:'reject',by:'Example Reviewer',reason:'Checked item 4'},issue:{}});
 assert.match(human[0],/Earlier: reject by Example Reviewer/);
 const unclear=lines({issue:{fallback_wording_review_needed:true,fallback_wording_note:'Who made this decision cannot be told from the record.'}});
 assert.deepEqual(unclear,[' Review needed: Who made this decision cannot be told from the record.']);
 assert.deepEqual(lines({issue:{}}),[]);
});

// Protected snapshots retain historical counts; presentation must not advertise a green zero.
test('protected snapshot has conspicuous integrity warning without changing historical queue membership',()=>{
 const integrity={label:'Approved snapshot · integrity review required',unresolved_count:57,message:'History preserved; independent review required.'};
 const r={file_hash:'a',review:{status:'clear',counts:{},sharepoint:{status:'day_level'},historical_integrity:integrity,coverage:{}}};
 const state={report:r,fixes:[],fdItems:[]};const h=(tag,props,...children)=>({tag,props,children});
 const ctx=vm.createContext({state,h,fdCounts:()=>({}),fdItems:()=>[],icon:()=>'',plural:(n,s)=>`${n} ${s}`});
 for(const name of ['queueCheck','fdStatus','fdShown','fdCheckedCopy','fdClean']){
  const a=src.indexOf('  function '+name+'('),b=src.indexOf('\n  function ',a+10);vm.runInContext(src.slice(a,b),ctx);
 }
 const check=ctx.queueCheck(r);assert.equal(check[1],'check');assert.match(check[0],/integrity review required/);
 const status=ctx.fdStatus(r);assert.equal(status.tone,'amber');assert.equal(ctx.fdShown(status),status);
 assert.equal(ctx.fdClean(r,{}).props.role,'alert');
 const copy=ctx.fdCheckedCopy(r);assert.equal(copy.props.role,'alert');assert.match(JSON.stringify(copy),/57 unresolved findings/);assert.doesNotMatch(JSON.stringify(copy),/0 remaining|Completed/);
 assert.match(src,/integrity \? `\$\{integrity.unresolved_count\} findings need independent integrity review`/);
});

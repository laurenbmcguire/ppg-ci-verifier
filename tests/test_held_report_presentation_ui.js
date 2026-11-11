const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync('web/app.js','utf8');
const section=(a,b)=>source.slice(source.indexOf(a),source.indexOf(b,source.indexOf(a)));
const h=(tag,attrs,...children)=>({tag,attrs,children:children.flat().filter(x=>x!=null)});
const text=n=>typeof n==='string'?n:n?.children?.map(text).join(' ')||'';
const state={workingCopies:true,list:[]};
const ctx=vm.createContext({state,SETTLED:['manual','verified'],h,Date,visibleList:()=>state.list,
  sp:{},day:'2026-11-10',prevDay:'2026-11-09',data:{sharepoint_on:false},fmt:String,
  fdCounts:()=>({}),outdated:r=>r.internal_check_status==='outdated_checks_require_recheck',plural:(n,s)=>`${n} ${s}`,
  fdCheckedState:r=>r.checked_copy});
vm.runInContext(section('  const queueDay =','  function drawQueue()')+'\nthis.queueNeedsAttention=queueNeedsAttention;\n'+section('      const repCell = (','      // This legacy dashboard')+'\nthis.repCell=repCell;\n'+section('  function todaysWins()','  async function autopilotTick()')+section('  function fdStatus(', '  // ---------- corrections:'),ctx);
const clean=()=>({file_hash:'synthetic',filename:'Synthetic.xlsx',internal_check_status:'no_findings_in_checks_performed',holds:[],
  assignment:{status:'manual'},review:{status:'clear',job:'SYNTHETIC',day:'2026-11-10',coverage:{truncated:false,not_listed:0},counts:{high:0,medium:0,low:0},
  sharepoint:{status:'match',source_verified:true}},
  verification:{source_verified:true,source_status:'match',checks_current:true,holds:[]}});
const daily=r=>({...r.review,file_hash:r.file_hash,verification:r.verification});
test('current verified zero-findings version may be final, without inventing approval or sending',()=>{
 const r=clean(),before=JSON.stringify(r);assert.equal(ctx.queueFinal(r,[r]),true);assert.equal(ctx.queueNeedsAttention(r),false);
 assert.deepEqual(Array.from(ctx.queueCheck(r)),['Completed','clear']);const cell=text(ctx.repCell(daily(r),0,'today','SYNTHETIC'));
 assert.match(cell,/Completed/);assert.match(cell,/Not sent/);assert.doesNotMatch(cell,/Approved|Sent$/);assert.equal(JSON.stringify(r),before);
});
test('zero possible errors is Completed and Done, while the SharePoint note and next step stay visible',()=>{
 const r=clean();r.review.rejected=null;r.review.quantities={rejected:{value:null,partial:false,complete:false}};
 r.review.sharepoint={status:'unverified',source_verified:false};r.holds=[{hard:true,code:'source_evidence_incomplete',text:'Rejection evidence missing'}];
 r.verification={source_verified:false,source_status:'unverified',checks_current:true,holds:r.holds};const before=JSON.stringify(r);
 state.list=[r];assert.equal(ctx.queueFinal(r,[r]),true);assert.equal(ctx.queueNeedsAttention(r),false);
 assert.deepEqual(Array.from(ctx.queueCheck(r)),['Completed','clear']);assert.equal(ctx.queueQuantity(r,'rejected'),'Unknown');
 assert.equal(ctx.queueSource(r),'Source not verified');
 assert.match(text(ctx.repCell(daily(r),0,'today','SYNTHETIC')),/Completed.*Not sent/);
 assert.equal(ctx.fdStatus(r).next,'reconcile');assert.equal(JSON.stringify(r),before);
});
test('incomplete, unavailable or out-of-date checks never become Completed',()=>{
 for(const change of [r=>r.review.coverage.truncated=true,
  r=>r.internal_check_status='could_not_load_latest_check',r=>r.internal_check_status='outdated_checks_require_recheck',r=>r.review.status='unknown']){
  const r=clean();delete r.holds;delete r.review.sharepoint.source_verified;change(r);
  assert.equal(ctx.queueFinal(r,[r]),false);assert.notEqual(ctx.queueCheck(r)[1],'clear');assert.equal(ctx.queueNeedsAttention(r),true);
 }
 const r=clean();r.verification.source_verified=false;r.verification.source_status='error';assert.equal(ctx.queueCheck(r)[0],'Completed');
 assert.equal(ctx.queueSource(r),'Source not verified');
});
test('holds stay on the report screen; the list shows a report with no possible errors as Completed',()=>{
 for(const hard of [false,true])for(const current of [false,true]){
  const r=clean();r.approval={by:'Synthetic reviewer'};r.review.approved='Synthetic reviewer';r.review.approval_current=current;
  r.holds=[{hard,code:'awaiting_source_owner',text:'An answer is required'}];r.verification.holds=r.holds;r.checked_copy={ready:true};
  assert.equal(ctx.queueFinal(r,[r]),true);assert.equal(ctx.queueCheck(r)[1],'clear');assert.equal(ctx.fdStatus(r).text,'Verification held');
  const output=text(ctx.repCell(daily(r),0,'today','SYNTHETIC'));assert.match(output,/Completed/);
  assert.match(output,current?/Internally approved, not sent/:/Approval out of date, not sent/);
 }
});
test('verified approval and sent history remain separate from current check presentation',()=>{
 const r=clean();r.approval={by:'Synthetic'};r.review.approved='Synthetic';r.review.approval_current=true;
 assert.equal(ctx.fdStatus(r).text,'Internally approved');r.review.approval_current=false;assert.equal(ctx.fdStatus(r).text,'Approval out of date');
 r.review.sent_at='2026-11-10T00:00:00Z';r.verification.holds=[{hard:true}];
 assert.equal(ctx.queueNeedsAttention(r),false);assert.equal(ctx.queueFinal(r,[r]),true);
 assert.match(text(ctx.repCell(daily(r),0,'today','SYNTHETIC')),/Sent/);
});
test('wins makes only a loaded saved-report claim, with failures and capped lists disclosed',()=>{
 state.list=[];assert.match(text(ctx.todaysWins()),/No saved reports waiting in this list/);
 for(const key of ['listError','listPartial']){state[key]=true;assert.match(text(ctx.todaysWins()),/Waiting count unavailable/);state[key]=false;}
 state.list=[{file_hash:'missing-summary'}];assert.match(text(ctx.todaysWins()),/1\s+saved report needs attention/);
});
test('presentation helpers do not change AI, manual, Ignore or saved decision state',()=>{
 const r=clean();Object.assign(r,{finding_decisions:{x:{action:'reject',reason:'Keep as recorded'}},review_decisions:{},ai_review:{status:'done'}});
 r.verification.holds=[{code:'source_evidence_incomplete',hard:true}];const before=JSON.stringify(r);
 ctx.queueFinal(r,[r]);ctx.queueCheck(r);ctx.queueSource(r);ctx.queueNeedsAttention(r);ctx.repCell(daily(r),0,'today','SYNTHETIC');ctx.fdStatus(r);
 assert.equal(JSON.stringify(r),before);
});

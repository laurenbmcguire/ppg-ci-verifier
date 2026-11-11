const test = require('node:test'), assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm');
const src = fs.readFileSync('web/app.js', 'utf8');
const start = src.indexOf('  function fdCheckedState('), end = src.indexOf('  function fdCheckedCopy(', start);
const receiptFor = report => ({protocol:'ppg-checked-copy-v1',ready:true,file_hash:report.file_hash,check_hash:report.check_hash,expires_at:report.checked_copy.expires_at});
const headersFor = (report, type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet') => ({get:name=>name==='X-PPG-Checked-Copy'?JSON.stringify(receiptFor(report)):name==='Content-Type'?type:null});
function setup() {
  const report = {file_hash:'a'.repeat(64), check_hash:'b'.repeat(64), filename:'Synthetic.xlsx', checked_copy:{ready:true, file_hash:'a'.repeat(64), check_hash:'b'.repeat(64), expires_at:new Date(Date.now()+60000).toISOString()}};
  const log = [], ctx = vm.createContext({Date, Number, encodeURIComponent, state:{report}, readyChanges:()=>({total:0}), toast:x=>log.push(x), drawSide:()=>log.push('draw'), fdRefreshFoot:()=>{}, fetch:async()=>{throw Error('not mocked');}, URL:{createObjectURL:()=>{log.push('blob');return 'blob:x';},revokeObjectURL:()=>{}}, h:(tag,props)=>({click:()=>log.push(props),remove:()=>{}}), document:{body:{append:()=>{}}}, setTimeout:()=>1});
  vm.runInContext(src.slice(start,end),ctx); return {ctx, report, log};
}
test('only matching saved current proof with unexpired evidence and no pending changes qualifies',()=>{
 const {ctx,report}=setup();assert.equal(ctx.fdCheckedState(report).ready,true);
 for(const field of ['busy','rechecking','reviewing','verifying','decisionPending','gridDraft']){ctx.state[field]=true;assert.equal(ctx.fdCheckedState(report).ready,false);delete ctx.state[field];}
 ctx.readyChanges=()=>({total:1});assert.equal(ctx.fdCheckedState(report).ready,false);ctx.readyChanges=()=>({total:0});
 report.checked_copy.check_hash='wrong';assert.equal(ctx.fdCheckedState(report).ready,false);report.checked_copy.check_hash=report.check_hash;
 assert.equal(ctx.fdCheckedState(report,Date.now()+60001).ready,false);
});
test('409 is readable and never saved as XLSX',async()=>{
 const {ctx,report,log}=setup();ctx.fetch=async()=>({ok:false,json:async()=>({error:'Source evidence expired. Reload.'})});
 await ctx.fdDownloadChecked(report);assert.equal(report.checked_copy.ready,false);assert.ok(log.includes('Source evidence expired. Reload.'));assert.ok(!log.includes('blob'));
});
test('successful response is exact requested hash, never proof-provided alternate URL',async()=>{
 const {ctx,report,log}=setup();report.checked_copy.download='/api/spreadsheets/download?id=SIBLING';
 let url;ctx.fetch=async p=>(url=p,{ok:true,headers:headersFor(report),blob:async()=>({})});
 await ctx.fdDownloadChecked(report);assert.equal(url,`/api/spreadsheets/download?id=${report.file_hash}&checked=1&check=${report.check_hash}`);assert.ok(log.includes('blob'));assert.equal(log.find(x=>x.download)?.download,'Synthetic.xlsx');
});
test('wrong content type, scope change and local expiry after response never download',async()=>{
 for(const mode of ['type','scope','expiry']){const {ctx,report,log}=setup();ctx.fetch=async()=>({ok:true,headers:headersFor(report,mode==='type'?'application/json':'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'),blob:async()=>{if(mode==='scope')ctx.state.report={};if(mode==='expiry')report.checked_copy.expires_at='2000-01-01';return {};}});await ctx.fdDownloadChecked(report);assert.ok(!log.includes('blob'),mode);}
});

test('saved-version, empty-list and AI-result surfaces never promise finality for ignored or expired evidence',()=>{
 const part=(a,b)=>src.slice(src.indexOf(a),src.indexOf(b,src.indexOf(a)));
 const text=n=>n==null?'':typeof n==='object'?(n.children||[]).map(text).join(' '):String(n);
 const h=(tag,props,...children)=>({tag,props,children:children.flat(Infinity),append(...nodes){this.children.push(...nodes);}});
 for(const mode of ['ignored','ai-set-aside','expired','incomplete','source-hold']){
  const report={file_hash:'a',check_hash:'b',filename:'Synthetic.xlsx',review:{status:'clear',coverage:{},decisions:{}},checked_copy:{ready:mode==='expired',file_hash:'a',check_hash:'b',expires_at:'2000-01-01',coverage_complete:mode!=='incomplete',remaining_detected_errors:mode==='ignored'||mode==='ai-set-aside'?1:0,set_aside_unresolved:1,reasons:[mode==='source-hold'?'Source verification holds remain.':'Verification is not complete.']}};
  const items=[{state:mode==='ai-set-aside'?'claude':'kept',key:'synthetic'}];
  const ctx=vm.createContext({Date,Number,state:{report,fixes:[],fdAIRun:{hash:'a',fixed:1,from:1,version:2}},h,readyChanges:()=>({total:0}),revisionOf:()=>2,plural:(n,s)=>`${n} ${s}`,icon:()=>null,clearTimeout:()=>{},setTimeout:()=>1,fdStatus:()=>({}),fdCounts:()=>({applied:1,kept:1}),fdItems:()=>items,fdGroups:()=>[],fdNextStep:()=>null,fdAutoSummary:()=>null,fdManualList:()=>null});
  vm.runInContext(part('  function fdSheetBar(', '  function drawReport(')+part('  function fdCheckedState(', '  async function fdDownloadChecked(')+part('  function fdCheckedCopy(', '  // ---------- right-hand panel')+part('  function fdCorrections(', '  // One selection, shared')+part('  function fdResolutionText(', '  async function fdAIFixNow(')+part('  function fdAIRunBanner(', '  // ---------- the app fixes'),ctx);
  const bar=text(ctx.fdSheetBar(report));assert.match(bar,/Saved working copy: version 2/);assert.doesNotMatch(bar,/FINAL/);
  const panel=h('div',{});ctx.fdCorrections(panel);const rendered=text(panel);
  assert.match(rendered,mode==='incomplete'?/Verification not complete/:/Completed/);assert.match(rendered,/Nothing left to fix/);assert.doesNotMatch(rendered,/No errors left to fix|0 remaining detected errors|Download checked copy/);
  if(mode==='expired')assert.match(rendered,/Source evidence expired/);
  if(mode==='ignored'||mode==='ai-set-aside')assert.match(rendered,/1 ignored, left as written/);
  if(mode==='source-hold')assert.match(rendered,/Source verification holds remain/);
 }
});


test('checked download rejects ordinary old-server 200 and malformed, mismatched or expired receipts without retry',async()=>{
 for(const mode of ['missing','malformed','protocol','not-ready','file','check','expiry','no-expiry','invalid-expiry','expires-during-body']){
  const {ctx,report,log}=setup();let calls=0;const receipt=receiptFor(report);
  if(mode==='protocol')receipt.protocol='unknown';if(mode==='not-ready')receipt.ready=false;
  if(mode==='file')receipt.file_hash='c'.repeat(64);if(mode==='check')receipt.check_hash='d'.repeat(64);
  if(mode==='expiry')receipt.expires_at='2000-01-01';if(mode==='no-expiry')delete receipt.expires_at;if(mode==='invalid-expiry')receipt.expires_at='bad';
  if(mode==='expires-during-body')receipt.expires_at=new Date(Date.now()+30000).toISOString();
  ctx.fetch=async()=>{calls++;return {ok:true,headers:{get:name=>name==='Content-Type'?'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet':name==='X-PPG-Checked-Copy'?(mode==='missing'?null:mode==='malformed'?'{bad':JSON.stringify(receipt)):null},blob:async()=>{if(mode==='expires-during-body')ctx.Date={...Date,parse:Date.parse,now:()=>Date.now()+60000};return {};}};};
  await ctx.fdDownloadChecked(report);assert.equal(calls,1,mode);assert.ok(!log.includes('blob'),mode);assert.equal(report.checked_copy.ready,false,mode);assert.ok(log.some(x=>typeof x==='string'&&/No file was downloaded/.test(x)),mode);
 }
});

// Fabricated actual-function lifecycle. No service, browser, database or workbook.
const assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm');
const source = fs.readFileSync('web/app.js','utf8');
const part = (a,b) => source.slice(source.indexOf(a),source.indexOf(b,source.indexOf(a)));
const code = part('  function versionReadOnly(', '  function versionLinks(') + part('  function reviewBinding(', '  function findingBinding(') + part('  function fdSourceRefreshReason(', '  // ---------- history and sources');
const report = () => ({file_hash:'a'.repeat(64), check_hash:'check', assignment:{site_id:'site',list_id:'list'}, version_view:{status:'current'}, review:{sharepoint:{status:'incomplete'},decisions:{rev:0}},source_reconciliation:{snapshot_id:'old'},history:['original'],bytes:'immutable'});
function setup() {
 const r=report(), calls=[], messages=[], controls=[{disabled:false,isConnected:true}], state={report:r,selected:r.file_hash,health:{sharepoint_configured:true}}, c={state,toast:m=>messages.push(m),document:{querySelectorAll:s=>s.includes('input:not')?[]:controls},gridDirty:()=>false,drawSide(){},drawReport(){},loadFixes(){},loadList(){},fillPages:async x=>x,api:async (path,body)=>{calls.push({path,body});if(path.includes('working-copy'))return {requested_hash:r.file_hash,status:'current'};if(body)return {item:r};return {...r,source_reconciliation:{snapshot_id:'new'}};}};
 vm.createContext(c);vm.runInContext(code,c);
 const run=()=>c.fdReconcile(r,JSON.stringify(c.reviewBinding(r)),JSON.stringify(r.assignment));return {r,c,state,calls,messages,controls,run};
}
(async()=>{
 for(const field of ['busy','rechecking','decisionPending','aiProposalOperation','fdReconciling','fdAutoSPWaiting']) {const t=setup();t.state[field]=true;await t.run();assert.equal(t.calls.length,0,field);}
 for(const mutate of [t=>t.state.gridDraft=true,t=>t.r.version_view.historical=true,t=>t.r.assignment.needs_person=true,t=>delete t.r.assignment.site_id,t=>t.state.health.sharepoint_configured=false,t=>t.state.selected='elsewhere']) {const t=setup();t.c.gridDirty=()=>true;mutate(t);await t.run();assert.equal(t.calls.length,0);}
 {const t=setup(),binding=JSON.stringify(t.c.reviewBinding(t.r));t.r.check_hash='new';await t.c.fdReconcile(t.r,binding,JSON.stringify(t.r.assignment));assert.equal(t.calls.length,0);}
 {const t=setup();t.c.document.querySelectorAll=s=>s.includes('input:not')?[{value:'draft',defaultValue:''}]:t.controls;await t.run();assert.equal(t.calls.length,0);}
 {const t=setup(),before=JSON.stringify(t.r);await Promise.all([t.run(),t.run()]);assert.equal(t.calls.filter(x=>x.body).length,1);assert.equal(JSON.stringify(t.calls.find(x=>x.body).body),JSON.stringify({file_hash:t.r.file_hash}));assert.equal(JSON.stringify(t.r),before);assert.equal(t.state.report.source_reconciliation.snapshot_id,'new');assert.equal(t.controls[0].disabled,false);assert.equal(t.state.rechecking,false);}
 for(const phase of ['preflight','post','receipt']) {
  const t=setup(),api=t.c.api;let release;const pending=new Promise(r=>release=r);t.c.api=async(p,b)=>{const answer=await api(p,b);if(phase==='preflight'&&p.includes('working-copy')||phase==='post'&&b||phase==='receipt'&&p.startsWith('/api/spreadsheet?'))await pending;return answer;};
  const running=t.run();for(let i=0;i<10;i++)await Promise.resolve();const next={...report(),file_hash:'b'.repeat(64)};t.state.report=next;t.state.selected=next.file_hash;release();await running;assert.equal(t.state.report,next);assert.equal(t.calls.filter(x=>x.body).length,phase==='preflight'?0:1);assert.equal(t.state.fdReconciling,null);
 }
 for(const mode of ['transport','pending','unavailable','receipt_failure']) {
  const t=setup(),api=t.c.api;t.c.api=async(p,b)=>{if(b){t.calls.push({path:p,body:b});if(mode==='transport')throw Error('connection lost');if(mode==='pending')return {pending:true};return {item:t.r};}if(p.startsWith('/api/spreadsheet?')){if(mode==='receipt_failure')throw Error('read failed');if(mode==='unavailable')return {...t.r,review:{sharepoint:{status:'error',message:'source unavailable'}}};}return api(p,b);};
  await t.run();assert.equal(t.calls.filter(x=>x.body).length,1);assert.equal(t.state.rechecking,false);assert.equal(t.state.fdReconciling,null);assert.equal(t.controls[0].disabled,false);if(mode!=='unavailable')assert.match(t.messages.join(' '),/unconfirmed/);assert.doesNotMatch(t.messages.join(' '),/Matches SharePoint|success|verified/i);
 }
 assert.match(part('  function fdComparison(', '  function fdSources('),/fdSourceRefresh\(r\)/);assert.match(part('  function fdSources(', '  function drawSide('),/fdComparison\(r\)/);
 console.log('PASS source refresh: exact single POST, guards, stale binding, navigation at three awaits, transport/pending/read failure, unavailable source, cleanup, immutable original/history');
})().catch(e=>{console.error(e);process.exitCode=1;});

// Isolated real request builder. No server, network, provider, or persistent browser state.
const test=require('node:test'),assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');
const source=fs.readFileSync('web/app.js','utf8');
const between=(a,b)=>source.slice(source.indexOf(a),source.indexOf(b,source.indexOf(a)));
function harness(mode='ok'){
 const writes=[],events=[],selections=[],report={file_hash:'original',filename:'synthetic.xlsx',revision:1};
 const item={file_hash:'saved',filename:'synthetic-fixed.xlsx',fixed_from:{file_hash:'original',changes:[]}};
 const state={report};const ctx=vm.createContext({state,revisionOf:r=>r.revision||1,drawFixes(){},updateDownloads(){},$(){return null;},plural:(n,s)=>`${n} ${s}s`,
 toast:(...a)=>events.push(a),store:{get:()=>({}),set(){}},select:h=>{if(mode==='select-fail')throw Error('Synthetic screen failure');selections.push(h);},
 loadList:async()=>{if(mode==='refresh-fail')throw Error('Synthetic list failure');},
 api:async(path,body)=>{writes.push({path,body});if(mode==='committed-denied')throw Object.assign(Error('The corrected version was saved, but access changed before it could be opened. Ask an admin to review access before trying again.'),{httpStatus:403,saveCommitted:true});if(mode==='rejected')throw Object.assign(Error('Exact source changed. Nothing was applied.'),{httpStatus:409});if(mode==='lost')throw Error('Network response lost');if(mode==='server')throw Object.assign(Error('Unavailable'),{httpStatus:503});if(mode==='malformed')return {};return {item};}});
 vm.runInContext(source.slice(source.indexOf('  function versionReadOnly('),source.indexOf('  function versionLinks(')) + between('  const correctionSnapshots =','  function readyChanges()')+between('  async function applyFixes(','  // Take out extra copies'),ctx);
 const binding={file_hash:'original',expected_check_hash:'checked',expected_run:{id:1},expected_rev:2};
 const p={id:'p',sheet:'Sheet',cell:'A1',before:'2',after:'3',kind:'number',source:'SharePoint',snapshot_id:'snap',item:'item',field:'quantity',change_set:'whole',change_set_size:1};
 const capture=ctx.captureCorrections([p],{p:{state:'accepted',after:'3'}},{},binding,report);
 return {ctx,state,writes,events,selections,item,capture,run:opts=>ctx.applyFixes(capture.accepted,{}, {},{download:false,throwOnError:true,...opts})};
}
test('exact immutable source and atomic metadata survive request construction',async()=>{const h=harness();await h.run();const c=h.writes[0].body.changes[0];assert.equal(c.change_set,'whole');assert.equal(c.change_set_size,1);assert.equal(c.snapshot_id,'snap');assert.equal(c.item,'item');assert.equal(c.field,'quantity');assert.equal(h.writes[0].body.expected_check_hash,'checked');assert.equal(h.writes.length,1);});
for(const mode of ['ok','refresh-fail','select-fail'])test(`committed save remains saved after ${mode}`,async()=>{const h=harness(mode);assert.equal(await h.run(), 'saved');assert.equal(h.writes.length,1);assert.equal(h.state.busy,false);});
test('receipt remains available for truthful AI confirmation without a successful reload',async()=>{const h=harness('refresh-fail');assert.equal(await h.run({returnReceipt:true}),h.item);assert.deepEqual(h.selections,['saved']);});
test('server refusal preserves the exact actionable error',async()=>{const h=harness('rejected');await assert.rejects(h.run(),/Exact source changed/);assert.equal(h.writes.length,1);});
for(const mode of ['lost','server','malformed'])test(`${mode} never claims no change and never automatically retries`,async()=>{const h=harness(mode);await assert.rejects(h.run(),/save could not be confirmed/);assert.equal(h.writes.length,1);assert.ok(h.events.every(e=>!e[0].includes('Nothing was changed')));});
for(const flag of ['busy','decisionPending','rechecking','aiProposalOperation'])test(`${flag} reports no save started`,async()=>{const h=harness();h.state[flag]={};await assert.rejects(h.run(),/No save was started/);assert.equal(h.writes.length,0);});

test('identical repeated cell proposals deduplicate but conflicting values or provenance never choose first',()=>{
 const ctx=vm.createContext({});vm.runInContext(source.slice(source.indexOf('  function versionReadOnly('),source.indexOf('  function versionLinks(')) + between('  function fdUniqueAIProposals(', '  async function fdAIFixNow('),ctx);
 const p={sheet:'Sheet',cell:'A1',finding_id:'first',before:'2',after:'3',kind:'number',source:'SharePoint',snapshot_id:'snap',item:'row',field:'quantity',change_set:'atomic',change_set_size:2};
 assert.equal(ctx.fdUniqueAIProposals([p,{...p,finding_id:'second'}]).length,1);
 for(const field of ['before','after','kind','source','snapshot_id','item','field','change_set','change_set_size'])assert.throws(()=>ctx.fdUniqueAIProposals([p,{...p,[field]:'different'}]),/Conflicting suggestions.*No save was started/);
 const second={...p,cell:'A2'};assert.equal(ctx.fdUniqueAIProposals([p,second]).length,2);
});

test('committed access loss stays explicit and never retries or claims no write',async()=>{const h=harness('committed-denied');await assert.rejects(h.run(),e=>e.saveCommitted===true&&/was saved/.test(e.message));assert.equal(h.writes.length,1);assert.ok(h.events.every(e=>!e[0].includes('Nothing was changed')));});

const test=require('node:test'),assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');
const source=fs.readFileSync('web/app.js','utf8');
const between=(a,b)=>source.slice(source.indexOf(a),source.indexOf(b,source.indexOf(a)));
function context(){const state={report:{file_hash:'synthetic',check_hash:'check',review:{page:{complete:true}}}};const ctx=vm.createContext({state});vm.runInContext(between('  function fdRecommended(', '  function fdPreviewRecommended('),ctx);return {state,ctx};}
function item(id='p',extra={}){return {key:'finding-'+id,sheet:'S',cell:'A2',state:'open',proposed:'0',selectable:true,fix:{id,after:'0',recommendation_eligible:true,recommendation_check_hash:'check'},...extra};}
test('recommended cells counted once without losing separate evidence occurrences',()=>{const {ctx}=context();const items=[item(),item()];assert.equal(ctx.fdRecommended(items).length,1);assert.equal(items.length,2);});
test('bulk excludes unbound, held, altered, own and previous decisions without inventing zeros',()=>{const {ctx,state}=context();for(const x of [item('p',{state:'deferred'}),item('p',{own:{after:'1'}}),item('p',{previous:{action:'reject'}}),item('p',{proposed:''}),item('p',{fix:{after:'0',recommendation_eligible:true,recommendation_check_hash:'old'}})])assert.equal(ctx.fdRecommended([x]).length,0);assert.equal(ctx.fdRecommended([item()])[0].proposed,'0');state.report.review.page.complete=false;assert.equal(ctx.fdRecommended([item()]).length,0);});
test('preview-all is snapshot-only and stale binding opens no preview',()=>{const {ctx}=context();let shown=null;ctx.fdRecommended=()=>[item()];ctx.toast=()=>{};ctx.captureCorrections=(accepted,dec,edits,binding)=>({accepted,dec,edits,binding,total:1});ctx.fdPreview=x=>shown=x;vm.runInContext(between('  function fdPreviewRecommended(', '  function fdModeSelector('),ctx);ctx.fdPreviewRecommended({file_hash:'synthetic',expected_check_hash:'old'});assert.equal(shown,null);ctx.fdPreviewRecommended({file_hash:'synthetic',expected_check_hash:'check'});assert.equal(shown.recommendedOnly,true);assert.equal(Object.keys(shown.edits).length,0);});
test('leave unchanged preserves drafts and apply-all uses an explicit guarded request',()=>{assert.ok(!between('  function fdChooseMode(', '  // Only the server').includes('store.set(decisionsKey()'));assert.ok(source.includes('recommended_only: true'));assert.ok(source.includes('if (hash) d.close()'));assert.ok(source.includes("class: 'fd-hold-details'"));assert.ok(!source.includes('hardHolds.slice(0, 2)'));});
test('legacy AI mode cannot execute a provider action or alter mode state',()=>{let writes=0,calls=0;const ctx=vm.createContext({state:{report:{file_hash:'h',check_hash:'c'}},store:{set:()=>writes++},toast(){},drawReport(){},runAiFixes:()=>calls++});vm.runInContext(between('  function fdChooseMode(', '  // Only the server'),ctx);ctx.fdChooseMode('ai',{file_hash:'h',expected_check_hash:'c'});assert.equal(writes,0);assert.equal(calls,0);});
test('preview provenance matches recorded typed chat edits and existing versions are explicit',()=>{assert.ok(between('  function fdPreview(', '  async function fdApprove(').includes("src: e.source === 'Claude' ? 'Claude (chat)' : 'Your edit'"));assert.ok(source.includes('No new version was created.'));assert.ok(source.includes('answer.item.fixed_from?.changes'));const api=fs.readFileSync('api/index.py','utf8');assert.ok(api.includes("already_applied = fixed.get('already_applied') is True"));assert.ok(api.includes("'already_applied': already_applied"));});

test('group selection labels count distinct eligible cells separately from held occurrences',()=>{
 const ctx=vm.createContext({});vm.runInContext(between('  function fdGroupSelection(', '  function fdGroups('),ctx);
 const eligible=Array.from({length:59},(_,i)=>({key:'k'+i,sheet:'S',cell:'A'+(i+1),state:'selected'}));
 const held={key:'held',sheet:'S',cell:'A60',state:'deferred'};
 const items=[...eligible,{...eligible[0],key:'second-evidence'},held];
 assert.deepEqual(JSON.parse(JSON.stringify(ctx.fdGroupSelection(items,eligible))),{eligible:59,selected:59,individualReview:1});
 eligible[0].state='open';assert.equal(ctx.fdGroupSelection(items,eligible).selected,58);
 assert.ok(source.includes('All eligible changes selected'));assert.ok(!source.includes("? 'Partly selected'"));
});

test('none/leave unchanged records only the mode and preserves manual drafts and selection',()=>{
 const memory={dec:{p:{state:'accepted'}},edits:{'S!B2':{after:'draft'}}};const before=JSON.stringify(memory);
 const ctx=vm.createContext({state:{report:{file_hash:'h',check_hash:'c'}},store:{set:(k,v)=>memory[k]=v},toast(){},drawReport(){}});
 vm.runInContext(between('  function fdChooseMode(', '  // Only the server'),ctx);ctx.fdChooseMode('unchanged',{file_hash:'h',expected_check_hash:'c'});
 assert.equal(memory['rd-mode:h'],'unchanged');delete memory['rd-mode:h'];assert.equal(JSON.stringify(memory),before);
});

test('selection focus uses stable finding identity when accessible labels repeat',()=>{
 let focused=null;const input=key=>({getAttribute:name=>name==='data-selection-key'?key:'same visible label',focus(){focused=key;}});
 const ctx=vm.createContext({document:{activeElement:input('finding:second'),querySelectorAll:()=>[input('finding:first'),input('finding:second')]},state:{fixes:[]},store:{get:()=>({}),set(){}},decisionsKey:()=>'',editsKey:()=>'',toast(){},fdRefresh(){},plural(){}});
 vm.runInContext(between('  function fdSelect(', '  // what the reviewer'),ctx);ctx.fdSelect([],false);assert.equal(focused,'finding:second');
});

test('held reports retain only the relevant recovery action without repeated warning text',()=>{
 let rechecked=0,assigned=null;const ctx=vm.createContext({h:(tag,attrs,...kids)=>({tag,...attrs,kids}),fdRecheck:()=>rechecked++,assignForm:(report,button)=>assigned={report,button}});
 vm.runInContext(between('  function fdHoldAction(', '  function fdNextStep('),ctx);
 const report={holds:[{hard:true,text:'A long source warning'}]};
 const recheck=ctx.fdHoldAction(report,{next:'recheck',why:'A long source warning'});assert.equal(recheck.kids[0],'Re-check this report');recheck.onclick();assert.equal(rechecked,1);
 const client=ctx.fdHoldAction(report,{next:'client',why:'A long source warning'});assert.equal(client.kids[0],'Choose client and job');client.onclick({currentTarget:client});assert.equal(assigned.report,report);
 assert.ok(!JSON.stringify(recheck).includes('A long source warning'));assert.equal(ctx.fdHoldAction(report,{next:'blockers'}),null);
 assert.ok(between('  function fdCorrections(', '  // ---------- the app fixes').includes('const action = fdHoldAction(r, st)'));
});

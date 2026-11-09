const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync('web/app.js','utf8');
const between=(a,b)=>source.slice(source.indexOf(a),source.indexOf(b,source.indexOf(a)));
const text=n=>typeof n==='string'?n:n?.kids?.flat(Infinity).map(text).join(' ')||'';
const nodes=n=>n?.kids?.flat(Infinity).flatMap(x=>x&&typeof x==='object'?[x,...nodes(x)]:[])||[];
function harness(holds){const state={fixes:[],report:{file_hash:'h',check_hash:'c',fixed_from:{changes:[]},holds,review:{issues:[]}}};const h=(tag,attrs,...kids)=>({tag,...attrs,kids});const ctx=vm.createContext({state,h,plural:(n,s)=>`${n} ${s}${n===1?'':'s'}`,fdMode:()=> 'manual',reviewBinding:()=>({}),allIssues:r=>r.issues,fdRecommended:()=>[],revisionOf:()=>2,fdChooseMode(){},fdPreviewRecommended(){},fdStatus:()=>({next:'blockers'}),fdCounts:()=>({}),fdItems:()=>[],fdGroups:()=>[],icon:()=>null,fdHoldAction:()=>null,fdNextStep:()=>null,fdAutoSummary:()=>null,fdClean:()=>null});vm.runInContext(between('  function fdHoldSummary(', '  function fdOccurrenceList(')+between('  function fdCorrections(', '  // ---------- the app fixes'),ctx);return {ctx,state};}
test('saved summary and disclosure share blocking/advisory counts, and no hold disappears',()=>{
 for(const [holds,expected] of [[[],'0 blocking holds · 0 advisory holds'],[[{hard:true,text:'Blocked'}],'1 blocking hold · 0 advisory holds'],[[{hard:false,text:'Advisory'}],'0 blocking holds · 1 advisory hold'],[[{hard:true,text:'Blocked'},{hard:false,text:'Advisory'}, {text:'Unclassified advisory'}],'1 blocking hold · 2 advisory holds']]){
  const {ctx,state}=harness(holds);assert.equal(ctx.fdHoldSummary(state.report),expected);const mode=ctx.fdModeSelector(state.report);assert.equal(text(mode),'Review potential errors');
  const body={kids:[],append(...kids){this.kids.push(...kids)}};ctx.fdCorrections(body);const details=nodes(body).find(n=>n.class==='fd-hold-details');
  if(!holds.length){assert.equal(details,undefined);continue;}
  assert.ok(text(nodes(details).find(n=>n.tag==='summary')).includes(expected));const listed=nodes(details).filter(n=>n.tag==='li');assert.equal(listed.length,holds.length);
  holds.forEach((hold,i)=>assert.equal(text(listed[i]),`${hold.hard?'Blocking':'Advisory'}: ${hold.text}`));
 }
});
test('individual accessible names match visible label, identify duplicate findings and explain shared selection',()=>{
 const {ctx}=harness([]);const all=[{key:'one',sheet:'North',cell:'A2',fix:{id:'p'}},{key:'two',sheet:'North',cell:'A2',fix:{id:'p'}},{key:'three',sheet:'South',cell:'A2',fix:{id:'q'}}];const labels=all.map(it=>ctx.fdOccurrenceLabel(it,all));
 assert.equal(new Set(labels).size,3);labels.forEach(label=>assert.ok(label.startsWith('Include change,')));
 assert.match(labels[0],/North, cell A2, finding 1 of 3/);assert.match(labels[1],/finding 2 of 3/);assert.match(labels[0],/2 findings share this cell change; their selections stay together/);assert.match(labels[2],/South, cell A2/);assert.ok(!labels[2].includes('share'));
});

test('coverage dominates status ledger even when stored categories add up',()=>{
 const {ctx,state}=harness([]);ctx.fmt=x=>String(x??'Unknown');
 for(const coverage of [{truncated:true,findings_total:80,findings_listed:50,not_listed:30},{truncated:false,findings_total:50,findings_listed:50,not_listed:0}]){
  state.report.review.coverage=coverage;state.report.review.reconcile={total:70,adds_up:true};
  const body={kids:[],append(...kids){this.kids.push(...kids)}};ctx.fdCorrections(body);
  const ledger=text(nodes(body).find(n=>n.class==='fd-recon'));assert.match(ledger,/Listed findings/);assert.doesNotMatch(ledger,/All .*accounted for|✓/);
  assert.equal(text(body).includes('Some potential errors are unavailable'),coverage.truncated);
  const mode=ctx.fdModeSelector(state.report);assert.equal(text(mode),'Review potential errors');
 }
 state.report.review.page={complete:false};state.report.review.coverage={};state.report.review.reconcile.adds_up=false;
 const body={kids:[],append(...kids){this.kids.push(...kids)}};ctx.fdCorrections(body);assert.match(text(body),/Reopen the report to load the rest/);
 assert.equal(text(ctx.fdModeSelector(state.report)),'Review potential errors');
});

test('recheck waits for pending decisions and never reloads a different report after navigation',async()=>{
 let resolve,calls=0,reloads=0;const state={report:{file_hash:'one'},selected:'one',decisionPending:true};
 const ctx=vm.createContext({state,myName:()=> 'Synthetic',toast:()=>{},document:{querySelectorAll:()=>[],querySelector:()=>null},api:()=>{calls++;return new Promise(r=>resolve=r)},openReport:()=>{reloads++},loadList:()=>{}});
 vm.runInContext(between('  async function fdRecheck(', '  async function fdReconcile('),ctx);
 await ctx.fdRecheck();assert.equal(calls,0);state.decisionPending=false;
 const running=ctx.fdRecheck();await ctx.fdRecheck();assert.equal(calls,1);state.report={file_hash:'two'};state.selected='two';resolve({});await running;
 assert.equal(reloads,0);assert.equal(state.rechecking,false);
});

test('successful explicit recheck refreshes recovery controls after releasing its lock',async()=>{
 const state={report:{file_hash:'one'},selected:'one'};let refreshed=0;
 const ctx=vm.createContext({state,myName:()=> 'Synthetic',toast:()=>{},api:async()=>({}),openReport:async()=>{state.report={file_hash:'one'}},loadList:()=>{},fdModeSelector:()=>{assert.equal(state.rechecking,false);return {}},document:{querySelectorAll:()=>[],querySelector:()=>({replaceWith:()=>refreshed++})}});
 vm.runInContext(between('  async function fdRecheck(', '  async function fdReconcile('),ctx);await ctx.fdRecheck();assert.equal(refreshed,1);assert.equal(state.rechecking,false);
});

test('queue summary never calls known incomplete finding coverage clear',()=>{
 const ctx=vm.createContext({});vm.runInContext(source.split('\n').find(line=>line.includes('const statusNote ='))+'\nthis.note=statusNote;',ctx);
 assert.equal(ctx.note({status:'clear',counts:{},coverage:{truncated:true}}),'Finding coverage incomplete');
 assert.equal(ctx.note({status:'clear',counts:{},page:{complete:false}}),'Finding coverage incomplete');
 assert.equal(ctx.note({status:'hold',counts:{high:1}}),'1 high-severity finding');
});

test('recheck-first blocks later apply and decision requests and restores controls after navigation',async()=>{
 let resolve,calls=0;const button={disabled:false,isConnected:true,removeAttribute(){}};const state={report:{file_hash:'one'},selected:'one'};
 const snapshot={binding:{expected_check_hash:'c'},accepted:[],dec:{},edits:{}};
 const ctx=vm.createContext({state,myName:()=> 'Synthetic',toast:()=>{},api:()=>{calls++;return new Promise(r=>resolve=r)},document:{querySelectorAll:()=>[button],querySelector:()=>null},correctionSnapshots:{get:()=>snapshot},fdModeSelector:()=>null,drawFixes:()=>{throw Error('Apply started')},updateDownloads:()=>{},drawSide:()=>{throw Error('Decision started')}});
 vm.runInContext(between('  async function fdRecheck(', '  async function fdReconcile(')+between('  async function applyFixes(', '  // Take out extra copies')+between('  async function fdSendDecision(', '  function fdRefreshFoot('),ctx);
 const run=ctx.fdRecheck();assert.equal(button.disabled,true);await ctx.applyFixes([],{},{});await ctx.fdSendDecision({});assert.equal(calls,1);state.report={file_hash:'two'};state.selected='two';resolve({});await run;assert.equal(button.disabled,false);assert.equal(state.rechecking,false);
});

test('recheck releases newly rendered report controls without replacing typed decision forms',async()=>{
 let resolve,refresh=0;const old={disabled:false,isConnected:true},current={disabled:true,removeAttribute(){this.released=true}},typed={value:'Keep this typed reason'};
 const state={report:{file_hash:'one'},selected:'one'};
 const ctx=vm.createContext({state,myName:()=> 'Synthetic',toast:()=>{},api:()=>new Promise(r=>resolve=r),fdModeSelector:r=>{assert.equal(r.file_hash,'two');return {}},document:{querySelectorAll:s=>s==='[data-recheck-disabled]'?[current]:[old],querySelector:()=>({replaceWith:()=>refresh++})}});
 vm.runInContext(between('  async function fdRecheck(', '  async function fdReconcile('),ctx);const run=ctx.fdRecheck();old.isConnected=false;state.report={file_hash:'two'};state.selected='two';resolve({});await run;
 assert.equal(current.disabled,false);assert.equal(current.released,true);assert.equal(refresh,1);assert.equal(typed.value,'Keep this typed reason');
});

test('failed old report refresh never paints over a newer selected report',async()=>{
 let reject,paints=0;const state={selected:'one'};const ctx=vm.createContext({state,$:()=>({replaceChildren:()=>paints++}),api:()=>new Promise((r,j)=>reject=j),fillPages:x=>x,h:()=>({})});
 vm.runInContext(between('  async function openReport(', '  // Reports uploaded before'),ctx);const pending=ctx.openReport('one',true);state.selected='two';reject(Error('Old refresh failed'));await pending;assert.equal(paints,0);
});

test('dirty unsubmitted reviewer text stops recheck before any request',async()=>{
 let calls=0;const state={report:{file_hash:'one'},selected:'one'};const ctx=vm.createContext({state,toast:()=>{},document:{querySelectorAll:()=>[{value:'unsaved',defaultValue:''}]},api:()=>calls++});
 vm.runInContext(between('  async function fdRecheck(', '  async function fdReconcile('),ctx);await ctx.fdRecheck();assert.equal(calls,0);assert.equal(state.rechecking,undefined);
});

test('pending recheck freezes late form inputs and restores their prior disabled states',async()=>{
 let reject;const original={value:'',defaultValue:'',disabled:false,isConnected:true},unavailable={value:'',defaultValue:'',disabled:true,isConnected:true},late={value:'',defaultValue:'',disabled:false,isConnected:true};let inputs=[original,unavailable];
 const state={report:{file_hash:'one'},selected:'one'};const ctx=vm.createContext({state,myName:()=> 'Synthetic',toast:()=>{},api:()=>new Promise((r,j)=>reject=j),document:{querySelectorAll:s=>s==='[data-recheck-disabled]'?[]:inputs,querySelector:()=>null},fdModeSelector:()=>null});
 vm.runInContext(between('  async function fdRecheck(', '  async function fdReconcile('),ctx);const run=ctx.fdRecheck();assert.equal(original.disabled,true);inputs=[late];state.rechecking.lock();assert.equal(late.disabled,true);reject(Error('Synthetic failure'));await run;assert.equal(original.disabled,false);assert.equal(unavailable.disabled,true);assert.equal(late.disabled,false);
});

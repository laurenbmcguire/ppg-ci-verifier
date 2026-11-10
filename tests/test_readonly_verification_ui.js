// Saved evidence only: no database, providers or real report data.
const test=require('node:test'),assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');
const source=fs.readFileSync('web/app.js','utf8'),part=(a,b)=>source.slice(source.indexOf(a),source.indexOf(b,source.indexOf(a)));
const node=(tag,attrs,...children)=>({tag,attrs,children:children.flat(Infinity).filter(x=>x!=null&&x!==false),replaceChildren(...items){this.children=items.flat(Infinity).filter(x=>x!=null&&x!==false);},focus(){}});
const walk=n=>[n,...(n.children||[]).flatMap(c=>typeof c==='object'?walk(c):[])];
function context(report){const calls=[],state={report,me:{user:{role:'admin'}}},ctx=vm.createContext({state,h:node,icon:()=>node('svg'),showWs:String,plural:(n,s)=>`${n} ${s}`,when2:x=>x||'',revisionOf:r=>r.revision||1,api:async(...x)=>calls.push(x),openReport:async()=>{},issueType:()=> 'Finding'});
 vm.runInContext(part('  function versionReadOnly(', '  function versionLinks(')+part('  function fdReadOnlyReason(', '  function fdReportRail(')+part('  function kindNames(', '  function drawCleanOffer(')+part('  function reviewBinding(', '  function findingBinding(')+part('  function fdHistoryGroups(', '  function fdHoldAction(')+part('  function fdHistory(', '  // One difference between'),ctx);return {ctx,state,calls};}
const base=()=>({file_hash:'a'.repeat(64),check_hash:'check',filename:'SYNTHETIC-HMMA.csv',review:{issues:[],dismissed:[]},version_view:{status:'unavailable',unavailable_reason:'history_inconsistent'},holds:[{code:'source_incomplete',hard:true,text:'Incomplete source capture'}]});
test('1294 saved findings group without dropping identities or mixing severity/source/status/sheet',()=>{
 const r=base();r.review.issues=Array.from({length:1290},(_,i)=>({key:'k'+i,sheet:'Inspection',cell:'I'+(i+2),severity:'medium',code:'blank_cell',text:'Missing cell'}));
 r.review.issues.push({...r.review.issues[0],key:'other',sheet:'Other'});
 r.review.dismissed=[{...r.review.issues[0],key:'ai',dismissed_by:'claude_verify'},{...r.review.issues[0],key:'kept',decision:{action:'reject',by:'Reviewer'}},{...r.review.issues[0],key:'info',severity:'info'}];
 const before=JSON.stringify(r),{ctx}=context(r),groups=ctx.fdRecordedFindingGroups(r);
 assert.equal(groups.length,5);assert.equal(groups.reduce((n,g)=>n+g.entries.length,0),1294);assert.equal(new Set(groups.flatMap(g=>g.entries.map(e=>e.id))).size,1294);
 assert.equal(groups[0].entries.length,1290);assert.equal(groups[0].cells,1290);assert.equal(groups[0].rows,1290);
 assert.ok(groups.some(g=>g.status==='AI recommendation, not a human decision'));assert.ok(groups.some(g=>g.status==='Kept as recorded'));assert.equal(JSON.stringify(r),before);
});
test('all historical, approved, sent, sending and inconsistent views are locked with a truthful reason',()=>{
 for(const change of [{version_view:{status:'current',historical:true}},{approval:{by:'Reviewer'}},{sent:[{sent:true}]},{sending:true},{}]){
  const r={...base(),version_view:{status:'current'},...change};if(!Object.keys(change).length)r.version_view=base().version_view;
  const {ctx}=context(r);assert.equal(ctx.versionReadOnly(r),true);assert.match(ctx.fdReadOnlyReason(r),/Historical|approved|sent|sending|inconsistent/);
 }
});
test('self-cycle history is inspectable without treating its parent and child claims as verified ancestry',()=>{
 const r=base();r.fixed_from={file_hash:r.file_hash,filename:r.filename,changes:[]};r.fixes_applied=[{fixed_file_hash:r.file_hash,changes:[]}];r.finding_decision_log=[{action:'reject',decision_id:'d',ids:['k'],cells:{k:'I2'}}];
 const {ctx,calls}=context(r),tree=ctx.fdHistory(r),text=JSON.stringify(tree);
 assert.match(text,/Saved history could not be verified/);assert.match(text,/Unverified recorded parent reference/);assert.match(text,/recorded in unverified history/);
 assert.equal(walk(tree).filter(n=>n.tag==='button').length,0);assert.equal(calls.length,0);
 assert.ok(walk(tree).filter(n=>n.tag==='a').every(n=>n.attrs.href.endsWith('&history=1')));
});
test('source mapping controls disappear in read-only views and stale retained controls cannot post',async()=>{
 const r=base();r.source_reconciliation={snapshot_id:'s',site_id:'site',list_id:'list',column_mapping:{inspected:{status:'needs_confirmation',column:'Qty'}}};
 const {ctx,calls}=context(r);assert.equal(walk(ctx.fdSourceRecovery(r)).filter(n=>n.tag==='button').length,0);
 r.version_view={status:'current'};const tree=ctx.fdSourceRecovery(r),buttons=walk(tree).filter(n=>n.tag==='button');assert.equal(buttons.length,2);
 r.version_view.historical=true;for(const button of buttons)await button.attrs.onclick();assert.equal(calls.length,0);
 r.version_view={status:'current'};ctx.state.report={...r,approval:{by:'Reviewer'}};for(const button of buttons)await button.attrs.onclick();assert.equal(calls.length,0);
});

test('inconsistent ancestry labels automatic and corrected claims as unverified without rewriting recorded statuses',()=>{
 const r=base();r.auto_fixed={changed:[{sheet:'Inspection',cell:'I2',before:'',after:'N/A',reason:'Recorded cleanup'}],removed:[{sheet:'Inspection',row:3,reason:'Recorded duplicate'}],moved:[{sheet:'Inspection',row:4,to_sheet:'Other',reason:'Recorded move'}]};
 r.review.dismissed=[{key:'corrected',sheet:'Inspection',cell:'I2',severity:'medium',code:'blank_cell',text:'Missing cell',dismissed_by:'corrected'}];
 const original=JSON.stringify(r),{ctx}=context(r),groups=ctx.fdRecordedFindingGroups(r),tree=ctx.fdAutoSummary(r);
 const text=walk(tree).flatMap(n=>(n.children||[]).filter(c=>typeof c==='string')).join(' ');
 assert.match(groups[0].action,/Recorded correction claim, history unverified/);assert.equal(groups[0].status,'Fixed in a later version');assert.equal(groups[0].entries[0].entry.dismissed_by,'corrected');
 assert.match(text,/Recorded cell changes \(history unverified\)/);assert.match(text,/Recorded row removals \(history unverified\)/);assert.match(text,/Recorded row moves \(history unverified\)/);assert.match(text,/Unverified record: Blank cells filled/);
 assert.doesNotMatch(text,/Applied cell changes|Applied row removals|Applied row moves|recorded applied change/);
 assert.deepEqual(Array.from(ctx.fdHistoryGroups(r.auto_fixed),g=>g.status),['applied','removed','moved']);assert.equal(JSON.stringify(r),original);
});

test('stale or unbound read-only source evidence never queues auto reconciliation, even with loaded fixes',()=>{
 for(const view of [{status:'unavailable',unavailable_reason:'history_inconsistent'},{status:'current',historical:true},...['approved','sent','sending'].map(read_only_reason=>({status:'current',read_only_reason}))])for(const rec of [{stale:true},{bound_to:null}]){
  const r={...base(),version_view:view,source_reconciliation:rec},timers=[],{ctx,state,calls}=context(r);state.fixes=[];ctx.setTimeout=fn=>timers.push(fn);
  vm.runInContext(part('  function fdAutoReconcile(', '  function fdSourceRefreshReason('),ctx);
  assert.equal(ctx.fdAutoReconcile(r),false);assert.equal(timers.length,0);assert.equal(state.fdAutoSPWaiting,undefined);assert.equal(calls.length,0);
 }
});
test('a queued automatic source comparison stops when the same report becomes read-only',async()=>{
 const r={...base(),version_view:{status:'current'}},timers=[],{ctx,state,calls}=context(r);state.fixes=[];ctx.setTimeout=fn=>timers.push(fn);ctx.drawSide=()=>{throw Error('Must not draw an editable panel');};
 vm.runInContext(part('  function fdAutoReconcile(', '  function fdSourceRefreshReason('),ctx);
 assert.equal(ctx.fdAutoReconcile(r),true);assert.equal(timers.length,1);r.version_view.historical=true;
 await timers.shift()();assert.equal(state.fdAutoSPWaiting,false);assert.equal(calls.length,0);assert.equal(timers.length,0);
});

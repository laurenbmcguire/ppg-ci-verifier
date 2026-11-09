const test=require('node:test'),assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');
const source=fs.readFileSync('web/app.js','utf8');
const between=(a,b)=>source.slice(source.indexOf(a),source.indexOf(b,source.indexOf(a)));
function history(){const ctx=vm.createContext({});vm.runInContext(between('  function fdHistoryGroups(', '  function fdAutoSummary('),ctx);return ctx.fdHistoryGroups;}
test('300 blank changes collapse losslessly, preserving source/risk/status boundaries',()=>{
 const changed=Array.from({length:300},(_,i)=>({id:`cell-${i}`,sheet:'Synthetic',cell:`I${i+1}`,before:'',after:'N/A',reason:'Blank defect labels normalized',source:'Mechanical',by:'app'}));
 const extra=[{...changed[0],source:'SharePoint'}, {...changed[0],severity:'high',held:true}, {...changed[0],sheet:'Other'}, {...changed[0],reverted:true}, {...changed[0],after:''}];
 const summary={changed:[...changed,...extra],fine:[{text:'Blank remains unknown',reason:'Source owner required',sheet:'Synthetic',source:'AI',by:'Claude'}],removed:[{sheet:'Synthetic',row:5,reason:'Duplicate'}],moved:[{sheet:'Synthetic',row:5,to_sheet:'Other',reason:'Duplicate'}]};
 const original=JSON.stringify(summary),groups=history()(summary);
 assert.equal(groups.length,9);assert.equal(groups[0].entries.length,300);assert.equal(groups[0].cells,300);assert.equal(groups[0].rows,300);
 assert.equal(groups.reduce((n,g)=>n+g.entries.length,0),308);
 assert.equal(new Set(groups.flatMap(g=>g.entries.map(x=>x.id))).size,308);
 assert.equal(groups[0].entries[211].entry,changed[211],'original occurrence object retained');
 assert.equal(JSON.stringify(summary),original,'grouping does not mutate audit records');
 assert.ok(groups.some(g=>g.status==='recommendation'));assert.ok(groups.some(g=>g.status==='reverted'));
 assert.equal(groups[0].action,'Blank cells filled');
});
test('same cell repeated remains two occurrences but one distinct cell/row',()=>{
 const item={sheet:'Synthetic',cell:'I7',before:'',after:'N/A',reason:'Blank label'};
 const groups=history()({changed:[item,{...item}]});
 assert.equal(groups.length,1);assert.equal(groups[0].entries.length,2);assert.equal(groups[0].cells,1);assert.equal(groups[0].rows,1);
 assert.notEqual(groups[0].entries[0].id,groups[0].entries[1].id);
});
test('correction grouping includes sheet, risk, evidence, and decision state without changing finding binding',()=>{
 const issues=Array.from({length:5},(_,i)=>({key:`k${i}`,fp:`fp${i}`,code:'blank',text:'Blank cell',sheet:i===1?'Other':'Synthetic',cell:`I${i+1}`,severity:i===2?'high':'medium',decision:i===3?{action:'defer',decision_id:'held'}:null,source_value:i===4?'source':null}));
 const ctx=vm.createContext({state:{report:{file_hash:'hash',check_hash:'check',review:{issues,run:{version:1},decisions:{rev:9}}},fixes:[]},
  store:{get:(k,d)=>d},decisionsKey:()=>'',editsKey:()=>'',allIssues:r=>r.issues,splitCell:cell=>['I',Number(cell.slice(1))],sameKey:()=> 'blank',fixCovers:()=>false,editId:(s,c)=>s+c,ASIDE:{}});
 vm.runInContext(between('  function reviewBinding(', '  function fdGroups('),ctx);
 const items=ctx.fdItems();assert.equal(new Set(items.map(x=>x.group)).size,5);
 for(let i=0;i<items.length;i++){assert.equal(items[i].key,issues[i].key);assert.equal(items[i].fp,issues[i].fp);assert.equal(items[i].binding.expected_check_hash,'check');assert.equal(items[i].binding.expected_rev,9);}
});
test('paged findings all contribute and incomplete pages remain explicitly incomplete',async()=>{
 const calls=[],ctx=vm.createContext({api:async(path,p)=>{calls.push(p);return {items:Array.from({length:Math.min(50,301-p.offset)},(_,i)=>({key:`k${p.offset+i}`}))}}});
 vm.runInContext(between('  async function fillPages(', '  function showEmpty('),ctx);
 const data={file_hash:'synthetic',review:{issues:[{key:'k0'}],dismissed:[],page:{issues_total:301,dismissed_total:0,size:50}}};
 await ctx.fillPages(data);assert.equal(data.review.issues.length,301);assert.equal(new Set(data.review.issues.map(x=>x.key)).size,301);assert.equal(data.review.page.complete,true);assert.equal(calls.length,6);
 ctx.api=async()=>({items:[]});const partial={file_hash:'synthetic',review:{issues:[],dismissed:[],page:{issues_total:301,dismissed_total:0,size:50}}};
 await ctx.fillPages(partial);assert.equal(partial.review.page.complete,false);
 assert.ok(source.includes('Counts below cover loaded occurrences only'));
});
test('Facts tab removed; source evidence and underlying audit records remain accessible',()=>{
 assert.ok(!source.includes("btn('facts', 'Facts')"));assert.ok(!source.includes("else if (tab === 'facts')"));
 for(const label of ['Source evidence and coverage','Snapshot','Fields not checked','Why not verified','Already reported on another day'])assert.ok(source.includes(label),label);
 const summary=between('  function fdAutoSummary(', '  function fdNextStep(');
 assert.ok(!summary.includes("api("));assert.ok(!summary.includes('Revert all'));
 assert.ok(summary.includes('AI recommendations, not human decisions'));
});
class Element {constructor(tag){this.tag=tag;this.nodeType=1;this.children=[];}append(...x){this.children.push(...x.flat());}setAttribute(k,v){this[k]=v;}replaceChildren(...x){this.children=x.flat();}focus(){Element.focused=this;}}
const textOf=node=>typeof node==='string'?node:(node.children||[]).map(textOf).join(' ');
function comparison(report){const ctx=vm.createContext({state:{report},icon:()=>new Element('svg'),document:{createElement:t=>new Element(t)}});vm.runInContext(between('  const h = (','  const store =')+between('  function reviewBinding(', '  function findingBinding(')+between('  function fdSourceVerified(', '  function drawReport(')+between('  function fdSourceRecovery(', '  function fdSources('),ctx);return textOf(ctx.fdComparison(report));}
test('exact-source comparison keeps unknown distinct from explicit zero and decimal precision',()=>{
 const pair=(report,source,status)=>({report:{total:report,known_rows:1,unknown_rows:report==null?1:0},source:{total:source,known_rows:1,unknown_rows:0},status});
 const report={review:{sharepoint:{status:'match',snapshot_id:'snapshot-1',source:'Exact list'}},source_reconciliation:{verified:false,coverage:{complete:false},fields_checked:['inspected','rejected'],fields_not_checked:['photos'],not_verified_because:['One source page missing'],count_reconciliation:{status:'held',record_counts:{report_rows:2,source_rows:1,matched_unique_items:1,duplicate_report_rows:0,missing_source_items:1,unmatched_report_rows:0},quantities:{inspected:pair(null,'9007199254740993.25','unavailable'),rejected:pair('0','0','matched')},rejected_records:pair('0','0','matched'),duplicates:[{sheet:'Synthetic',row:7,item:'17',evidence:'Both rows retained'}],exclusions:[{item:'19',day:'2026-01-02',day_basis:'business_date',reason:'Outside exact day'}]}}};
 const text=comparison(report);assert.match(text,/Report: Unknown/);assert.match(text,/9007199254740993\.25/);assert.match(text,/Report: 0/);assert.match(text,/Not checked against SharePoint yet/);assert.match(text,/Inspection records/);assert.match(text,/Records with rejected quantity/);assert.match(text,/Source item 17/);assert.match(text,/Source item 19/);assert.match(text,/One source page missing/);assert.match(text,/Rows read are not a complete source total/);
 report.source_reconciliation.verified=true;report.source_reconciliation.count_reconciliation.status='verified';report.review.sharepoint.status='stale';assert.match(comparison(report),/Not checked against SharePoint yet/,'old snapshot never looks current');
});
test('missing count contract and completion do not imply client readiness',()=>{
 assert.match(comparison({review:{sharepoint:{}}}),/comparison is unavailable/);
 const finish=between('  function offerFinish(', '  // ---------------- recommendations:');
 assert.ok(!source.includes("'All clear!'"));assert.ok(finish.includes('status.text'));assert.ok(finish.includes("status.next === 'approve' && !p.ready"));
 assert.ok(source.includes("['open', 'selected', 'deferred', 'confirmed'].includes(x.state)"));
});
test('cross-day evidence survives removal of Facts without missing context',()=>{
 const ctx=vm.createContext({document:{createElement:t=>new Element(t)},shortDay:x=>x,when2:x=>x,readBlock:()=>new Element('div'),fdComparison:()=>new Element('details')});
 vm.runInContext(between('  const h = (','  const store =')+between('  function fdSourceVerified(', '  function drawReport(')+between('  function fdSources(', '  // switching sheets'),ctx);
 const text=textOf(ctx.fdSources({review:{job:'Synthetic'},other_days:[{vin:'SYNTHETIC-VIN',file_hash:'old',filename:'Synthetic old',day:'2026-01-01'}]}));
 assert.match(text,/Identifiers also found on another day/);assert.match(text,/SYNTHETIC-VIN/);assert.match(text,/Synthetic old/);
});

test('record totals do not double-count the same row across correction groups',()=>{
 const ctx=vm.createContext({});vm.runInContext(between('  function fdRecordCount(', '  function fdGroups('),ctx);
 assert.equal(ctx.fdRecordCount([{key:'quantity',sheet:'A',row:7},{key:'source',sheet:'A',row:7},{key:'other',sheet:'B',row:7}]),2);
});
test('current backend source verdict and reassignment holds override a stored matching receipt',()=>{
 const report={file_hash:'file',checker_version:'old',review:{sharepoint:{status:'match',source_verified:true}},source_reconciliation:{snapshot_id:'snapshot',site_id:'site',list_id:'list',bound_to:{file_hash:'file',checker_version:'old'},coverage:{complete:true},verified:true,count_reconciliation:{status:'verified'}}};
 assert.match(comparison(report),/Matches SharePoint/);
 for(const verdict of [false,undefined]){report.review.sharepoint.source_verified=verdict;assert.match(comparison(report),/Not checked against SharePoint yet/);}
 report.review.sharepoint.source_verified=true;report.holds=[{code:'source_assignment_changed',text:'Resolved source assignment has changed'}];
 assert.match(comparison(report),/Not checked against SharePoint yet/);assert.match(comparison(report),/Resolved source assignment has changed/);
});
test('Nexus clipboard refuses incomplete/null captures and preserves explicit zero',async()=>{
 const writes=[],ctx=vm.createContext({document:{createElement:t=>new Element(t)},navigator:{clipboard:{writeText:async text=>writes.push(text)}},toast(){},hm12:x=>x,fmt:x=>x==null?'—':String(x)});
 vm.runInContext(between('  const h = (','  const store =')+between('  function nexusCopyAllowed(', '  const hm12 ='),ctx);
 const complete={status:'compared',coverage:{complete:true,source_records:1,placed_records:1},unplaced:0,shifts:[{name:'First',start:'06:00',end:'14:00',scans:1,inspected:0,rejected:0,certified:0}],total:{scans:1,inspected:0,rejected:0,certified:0}};
 const buttons=node=>node.children.flatMap(x=>typeof x==='object'?[...(x.tag==='button'?[x]:[]),...buttons(x)]:[]);
 const good=ctx.nexusTable(complete),copy=buttons(good)[0];assert.ok(!Object.hasOwn(copy,'disabled'));await copy.onclick();assert.match(writes[0],/First\t0\t0\t0/);
 for(const bad of [{...complete,status:'held'},{...complete,coverage:{complete:false,source_records:2,placed_records:1}}, {...complete,total:{...complete.total,inspected:null}}, {...complete,shifts:[{...complete.shifts[0],rejected:null}]}]){
  const block=ctx.nexusTable(bad),button=buttons(block)[0];assert.ok(Object.hasOwn(button,'disabled'));button.onclick();assert.match(textOf(block),/Nexus totals held/);
 }
 assert.equal(writes.length,1);
});
test('Today and approval labels never infer shift completion or client-ready status',()=>{
 const today=between('  async function showToday(', '  function timelineBlock(');
 assert.ok(!today.includes('Ready to build'));assert.ok(!today.includes('ready about'));assert.ok(!today.includes('45 minutes after'));
 assert.ok(today.includes('Build captured report'));assert.ok(today.includes('A gap in scans does not confirm shift completion'));
 assert.ok(!source.includes("'Approve as final'"));assert.ok(source.includes("'Record internal approval'"));
});
test('column confirmation captures exact source site/list and mapping at render time',()=>{
 const section=between('  function reconcileLine(', '  function assignForm(');
 assert.ok(section.includes('site_id: x.site_id'));assert.ok(section.includes('Object.freeze(Object.fromEntries(confirm.map'));
 assert.ok(section.includes("api('/api/sharepoint/confirm-columns', mappingRequest)"));
 assert.ok(section.includes('if (!mappingRequest.site_id)'));assert.ok(section.includes('exact source site identity is missing'));
});
test('Today capture state renders explicit provisional build action and never treats missing capture as zero',()=>{
 const ctx=vm.createContext({document:{createElement:t=>new Element(t)},sp:{},day:'2026-01-01',prevDay:'2025-12-31',data:{sharepoint_on:true},STATUS:{},fmt:String,
 buildBtn:(job,day,label)=>{const e=new Element('button');e.append(label);return e;}});
 vm.runInContext(between('  const h = (','  const store =')+between('      const repCell = (','      const addBox =')+'\nglobalThis.rep=repCell;',ctx);
 assert.match(textOf(ctx.rep(null,null,'today','job')),/Source capture not loaded/);
 ctx.sp.job={today:0,readiness:{state:'no_scans'}};assert.match(textOf(ctx.rep(null,null,'today','job')),/No scans in this capture/);
 ctx.sp.job={today:3,readiness:{state:'provisional',ready_at:null,shift_end_confirmed:false,note:'Through capture only; shift end not confirmed'}};
 const text=textOf(ctx.rep(null,3,'today','job'));assert.match(text,/Through capture only/);assert.match(text,/Build captured report/);assert.ok(!text.includes('Ready to build'));assert.ok(!text.includes('Invalid Date'));
});
test('comparison displays exact source IDs when backend list metadata is an object',()=>{
 const text=comparison({review:{sharepoint:{}},source_reconciliation:{list:{id:'list-exact',name:'Synthetic source'},site_id:'site-exact'}});
 assert.match(text,/Synthetic source/);assert.match(text,/Site ID: site-exact/);assert.match(text,/List ID: list-exact/);assert.ok(!text.includes('[object Object]'));
});

test('history pages preserve every stable ID, scope and collapse focus without mutations',()=>{
 const entries=Array.from({length:61},(_,i)=>({id:`changed:${i}`,entry:{sheet:'Synthetic',cell:`I${i+1}`,before:'',after:'N/A',reason:'Blank label'}}));
 const original=JSON.stringify(entries),ctx=vm.createContext({document:{createElement:t=>new Element(t)},plural:(n,s)=>`${n} ${s}${n===1?'':'s'}`,showWs:String});
 vm.runInContext(between('  const h = (','  const store =')+between('  function fdHistoryGroup(', '  function fdAutoSummary('),ctx);
 const group=ctx.fdHistoryGroup({key:'stable-group',entries,category:'applied',status:'applied',action:'Blank cells filled',sheet:'Synthetic',cells:61,rows:61,rule:'Blank label'});
 const walk=node=>node.children.flatMap(x=>typeof x==='object'?[x,...walk(x)]:[]),nodes=walk(group);
 const next=nodes.find(x=>x.tag==='button'&&x.children.includes('Next')),previous=nodes.find(x=>x.tag==='button'&&x.children.includes('Previous'));
 const collapse=nodes.find(x=>x.tag==='button'&&x.children.includes('Collapse')),range=nodes.find(x=>x.className==='fd-history-range');
 const seen=[];assert.equal(previous.disabled,true);
 while(true){const ids=walk(group).filter(x=>x['data-history-id']).map(x=>x['data-history-id']);assert.ok(ids.length<=25);seen.push(...ids);if(next.disabled)break;next.onclick();assert.equal(Element.focused,range);}
 assert.equal(seen.length,61);assert.equal(new Set(seen).size,61);assert.equal(range.textContent,'Occurrences 51–61 of 61 · 25 per page');
 previous.onclick();assert.equal(range.textContent,'Occurrences 26–50 of 61 · 25 per page');
 group.open=true;collapse.onclick();assert.equal(group.open,false);assert.equal(Element.focused,group.children[0]);
 group.open=true;assert.equal(range.textContent,'Occurrences 26–50 of 61 · 25 per page');
 assert.equal(walk(group).filter(x=>x.className==='fd-hint fd-history-notice').length,1);assert.equal(JSON.stringify(entries),original);
});
test('reachable source confirmation freezes exact scope, rejects stale clicks and separates fresh compare',async()=>{
 const requests=[],opened=[],report={file_hash:'synthetic',check_hash:'shown',review:{run:{version:1}},source_reconciliation:{snapshot_id:'snapshot',site_id:'exact-site',list_id:'exact-list',column_mapping:{inspected:{status:'needs_confirmation',column:'InspectedCount'}}}};
 const ctx=vm.createContext({state:{report,me:{user:{role:'admin'}}},document:{createElement:t=>new Element(t)},api:async(path,payload)=>requests.push({path,payload}),openReport:async(...args)=>opened.push(args)});
 vm.runInContext(between('  const h = (','  const store =')+between('  function reviewBinding(', '  function findingBinding(')+between('  function fdSourceRecovery(', '  function fdComparison('),ctx);
 const walk=node=>node.children.flatMap(x=>typeof x==='object'?[x,...walk(x)]:[]);
 const ui=ctx.fdSourceRecovery(report),nodes=walk(ui),confirm=nodes.find(x=>x.tag==='button'&&x.children.includes('Confirm source columns')),compare=nodes.find(x=>x.tag==='button'&&x.children.includes('Compare again'));
 assert.match(textOf(ui),/inspected → InspectedCount/);assert.match(textOf(ui),/exact-site/);
 report.check_hash='new';await confirm.onclick();assert.equal(requests.length,0);
 report.check_hash='shown';report.source_reconciliation.mapping_binding={revision:'changed'};await confirm.onclick();assert.equal(requests.length,0,'changed mapping revision cannot submit old receipt');delete report.source_reconciliation.mapping_binding;report.source_reconciliation.column_mapping.inspected.column='LaterColumn';await confirm.onclick();
 assert.equal(requests.length,1);assert.equal(requests[0].path,'/api/sharepoint/confirm-columns');assert.equal(requests[0].payload.mapping.inspected,'InspectedCount');assert.equal(requests[0].payload.site_id,'exact-site');assert.equal(requests[0].payload.file_hash,'synthetic');assert.equal(requests[0].payload.expected_check_hash,'shown');assert.equal(requests[0].payload.expected_snapshot_id,'snapshot');assert.equal(requests[0].payload.expected_mapping_revision,null);assert.equal(requests[0].payload.list_id,'exact-list');assert.ok(Object.isFrozen(requests[0].payload));assert.ok(Object.isFrozen(requests[0].payload.mapping));
 assert.equal(compare.hidden,false);assert.equal(opened.length,0,'saving does not silently compare or refresh away holds');
 await compare.onclick();assert.equal(requests[1].path,'/api/spreadsheets/source-reconcile');assert.equal(opened.length,1);
 ctx.state.me.user.role='member';const restricted=ctx.fdSourceRecovery(report);assert.match(textOf(restricted),/Ask an admin/);const restrictedButton=walk(restricted).find(x=>x.tag==='button'&&x.children.includes('Confirm source columns'));assert.ok(Object.hasOwn(restrictedButton,'disabled'));await restrictedButton.onclick();assert.equal(requests.length,2);ctx.state.me.user.role='admin';
 report.source_reconciliation.site_id=null;const missing=ctx.fdSourceRecovery(report);assert.ok(Object.hasOwn(walk(missing).find(x=>x.tag==='button'&&x.children.includes('Confirm source columns')),'disabled'));
 report.source_reconciliation.column_mapping={inspected:{status:'single',column:'InspectedCount'}};assert.equal(ctx.fdSourceRecovery(report),null,'only explicit confirmation receipts expose mapping action');
 report.holds=[{code:'source_mapping_changed'}];assert.match(textOf(ctx.fdSourceRecovery(report)),/Compare again/);
});

test('header reports receipt matches, never parsed report rows as matched source evidence',()=>{
 const ctx=vm.createContext({});vm.runInContext(between('  function fdSourceVerified(', '  function drawReport('),ctx);
 const r={file_hash:'file',checker_version:'checker',review:{scans:13,sharepoint:{status:'incomplete',source_verified:false,matched:13}},source_reconciliation:{snapshot_id:'snapshot',site_id:'site',list_id:'list',bound_to:{file_hash:'file',checker_version:'checker'},coverage:{complete:false},verified:false,count_reconciliation:{status:'held',record_counts:{report_rows:13,source_rows:0,matched_unique_items:0}}}};
 assert.equal(ctx.fdSourceSummary(r),'Source comparison incomplete · 0 unique source items matched / 13 report rows in the recorded capture');
 assert.ok(!ctx.fdSourceSummary(r).includes('13 of 13'));assert.ok(!ctx.fdSourceSummary(r).includes('Source verified'));
 Object.assign(r.source_reconciliation,{snapshot_id:'snapshot',site_id:'site',list_id:'list'});r.source_reconciliation.coverage.complete=true;r.source_reconciliation.verified=true;r.source_reconciliation.count_reconciliation={status:'verified',record_counts:{report_rows:13,source_rows:13,matched_unique_items:13}};r.review.sharepoint={status:'match',source_verified:true};
 assert.equal(ctx.fdSourceSummary(r),'Source verified · 13 unique source items matched / 13 report rows');
 r.holds=[{code:'source_assignment_changed'}];assert.match(ctx.fdSourceSummary(r),/^Source not verified/);
 delete r.source_reconciliation.count_reconciliation;assert.match(ctx.fdSourceSummary(r),/matched count unavailable/);
});
test('next action and unresolved tabs precede optional collapsed recorded history',()=>{
 const ui=between('  function fdCorrections(', '  // ---------- the app fixes');
 assert.ok(ui.indexOf('const nextStep')<ui.indexOf('const auto ='));
 assert.ok(ui.includes("class: 'fd-history-overview'"));assert.ok(ui.includes('History · ${plural(historyApplied'));assert.ok(!ui.includes("${fdHoldSummary(r)} · review details"));
 assert.ok(ui.indexOf("class: 'fd-tiers'")<ui.lastIndexOf('appendHistory();'));
});

test('Sources and header use the same current match-count evidence',()=>{
 const ctx=vm.createContext({document:{createElement:t=>new Element(t)},shortDay:x=>x,when2:x=>x,readBlock:()=>new Element('div'),fdComparison:()=>new Element('details')});
 vm.runInContext(between('  const h = (','  const store =')+between('  function fdSourceVerified(', '  function drawReport(')+between('  function fdSources(', '  // switching sheets'),ctx);
 const r={review:{scans:13,sharepoint:{status:'incomplete',matched:13,source_verified:false}},source_reconciliation:{coverage:{complete:false},count_reconciliation:{record_counts:{report_rows:13,source_rows:0,matched_unique_items:0}}}};
 assert.ok(textOf(ctx.fdSources(r)).includes(ctx.fdSourceSummary(r)));assert.match(textOf(ctx.fdSources(r)),/0 unique source items matched/);assert.ok(!textOf(ctx.fdSources(r)).includes('13 of 13'));
});
test('Sources article title cannot reuse a stale match label over current holds',()=>{
 const ctx=vm.createContext({document:{createElement:t=>new Element(t)},shortDay:x=>x,when2:x=>x,readBlock:()=>new Element('div'),fdComparison:()=>new Element('details')});
 vm.runInContext(between('  const h = (','  const store =')+between('  function fdSourceVerified(', '  function drawReport(')+between('  function fdSources(', '  // switching sheets'),ctx);
 const report={file_hash:'synthetic',checker_version:'current',review:{sharepoint:{status:'match',source_verified:true}},source_reconciliation:{snapshot_id:'snapshot',site_id:'site',list_id:'list',bound_to:{file_hash:'synthetic',checker_version:'current'},verified:true,coverage:{complete:true},count_reconciliation:{status:'verified'}}};
 const title=()=>textOf(ctx.fdSources(report).children.find(x=>x.tag==='article').children[0]);
 assert.equal(title(),'SharePoint: matches item by item');
 report.holds=[{code:'source_assignment_changed',hard:true}];assert.equal(title(),'SharePoint: not verified');
 report.holds=[];report.review.sharepoint.source_verified=false;assert.equal(title(),'SharePoint: not verified');
 report.review.sharepoint.source_verified=true;report.source_reconciliation.stale=true;assert.equal(title(),'SharePoint: not verified');
});
test('blocker and approval-hold counts live beside sheet, not in report title',()=>{
 const ctx=vm.createContext({state:{fdItems:[{tier:'blocker',state:'open'},{tier:'blocker',state:'kept'}]},plural:(n,s)=>`${n} ${s}${n===1?'':'s'}`});
 vm.runInContext(between('  function fdGlobalStatus(', '  function fdSourceVerified('),ctx);
 const st={next:'blockers',text:'4 blockers'};
 assert.equal(ctx.fdGlobalStatus(st),'Review blocked');
 assert.equal(ctx.fdPanelStatus({holds:[{hard:true},{hard:true},{hard:true}]},st),'1 blocker · 3 approval holds');
 assert.ok(between('  function drawReport(', '  function fdMetrics(').includes('fd-global-status'));
 assert.ok(!between('  function fdCorrections(', '  // ---------- the app fixes').includes('fd-hold-details'));
 assert.ok(!source.includes("btn('facts', 'Facts')"));assert.ok(source.includes("btn('claude', 'Ask Dex'"));
});

test('SharePoint verdict distinguishes identity joins from values and requires complete current evidence',()=>{
 const r={file_hash:'h',checker_version:'v',review:{sharepoint:{status:'differences',source_verified:false}},source_reconciliation:{snapshot_id:'snapshot',site_id:'site',list_id:'list',bound_to:{file_hash:'h',checker_version:'v'},snapshot_id:'snap',site_id:'site',list_id:'list',coverage:{complete:true},rows:[{sheet:'Detail',row:3,item:'2',status:'value_differs',field:'rejected',report:'2',sharepoint:'3'}],count_reconciliation:{record_counts:{report_rows:2,source_rows:2,matched_unique_items:2}}}};
 let text=comparison(r);assert.match(text,/Does not match SharePoint/);assert.match(text,/Detail row 3: rejected is 2 in the report but 3 in SharePoint \(SharePoint item 2\)/);assert.doesNotMatch(text,/100%/);
 for(const patch of [{stale:true},{rows_truncated:true},{unconfirmed_rows:1},{coverage:{complete:false}},{coverage:{complete:true,timezone_unconfirmed:true}},{column_mapping:{rejected:{status:'needs_confirmation'}}},{snapshot_id:null}]){const x=structuredClone(r);Object.assign(x.source_reconciliation,patch);assert.match(comparison(x),/Not checked against SharePoint yet/);}
 r.source_reconciliation.rows[0].report='';r.source_reconciliation.rows[0].sharepoint=0;assert.match(comparison(r),/rejected is blank in the report but 0 in SharePoint/);
 r.source_reconciliation.rows[0].report=null;assert.match(comparison(r),/rejected is unknown in the report but 0 in SharePoint/);
 r.holds=[{code:'source_assignment_changed'}];assert.match(comparison(r),/Not checked against SharePoint yet/);
});


test('Yes and No share complete-source guards and invalid differences are explicitly earlier evidence',()=>{
 const r={file_hash:'h',checker_version:'v',review:{sharepoint:{status:'match',source_verified:true}},source_reconciliation:{snapshot_id:'snap',site_id:'site',list_id:'list',bound_to:{file_hash:'h',checker_version:'v'},coverage:{complete:true},verified:true,count_reconciliation:{status:'verified'},rows:[]}};
 assert.match(comparison(r),/Matches SharePoint/);
 for(const patch of [{snapshot_id:null},{site_id:null},{list_id:null},{rows_truncated:true},{stale:true},{coverage:{complete:true,denied:1}},{coverage:{complete:true,failed_pages:[2]}},{column_mapping:{rejected:{status:'conflict'}}}]){
  const x=structuredClone(r);Object.assign(x.source_reconciliation,patch);x.source_reconciliation.rows=[{status:'value_differs',field:'rejected',report:0,sharepoint:1}];assert.match(comparison(x),/Not checked against SharePoint yet/);assert.match(comparison(x),/Earlier comparison, not verified/);
 }
});

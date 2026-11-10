// Actual shipped functions with fabricated pages; no server/provider/browser state.
const test=require('node:test'),assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');
const source=fs.readFileSync('web/app.js','utf8'),between=(a,b)=>source.slice(source.indexOf(a),source.indexOf(b,source.indexOf(a)));
function pages(){
 const binding={file_hash:'file',check_hash:'check',run:{version:1},rev:1,issues_total:2,dismissed_total:1};
 const data={file_hash:'file',check_hash:'check',review:{run:{version:1},decisions:{rev:1},issues:[{key:'k0'}],dismissed:[{key:'d0'}],page:{size:1,issues_total:2,dismissed_total:1,issues_returned:1,dismissed_returned:1,binding}}};
 const next={list:'issues',offset:1,total:2,items:[{key:'k1'}],rev:1,binding:JSON.parse(JSON.stringify(binding))};
 const calls=[],ctx=vm.createContext({api:async(path,p)=>{calls.push(p);return next}});
 vm.runInContext(between('  async function fillPages(', '  function showEmpty('),ctx);return {data,next,ctx,calls};
}
test('complete finding inventory includes open and dismissed lists',async()=>{const {ctx,data,calls}=pages();await ctx.fillPages(data);assert.equal(data.review.page.complete,true);assert.equal(data.review.issues.length,2);assert.equal(data.review.dismissed.length,1);assert.deepEqual(calls[0].expected_binding,data.review.page.binding);});
const corruptions={
 'changed revision and count':h=>{h.next.rev=2;h.next.total=3;},
 'same-count new check':h=>h.next.binding.check_hash='later',
 'changed run':h=>h.next.binding.run.version=2,
 'wrong file':h=>h.next.binding.file_hash='other',
 'wrong list':h=>h.next.list='dismissed',
 'wrong offset':h=>h.next.offset=0,
 'missing page':h=>h.next.items=[],
 'oversized page':h=>h.next.items.push({key:'k2'}),
 'duplicate stable key':h=>h.next.items[0].key='k0',
 'duplicate dismissed key':h=>h.next.items[0].key='d0',
 'missing stable key':h=>h.next.items=[{}],
 'missing binding':h=>delete h.next.binding,
 'invalid initial total':h=>h.data.review.page.issues_total=-1,
 'missing initial finding':h=>h.data.review.issues=[],
};
for(const [name,corrupt] of Object.entries(corruptions))test(`${name} cannot expose a mixed or complete selection scope`,async()=>{const h=pages();corrupt(h);const initial=JSON.stringify(h.data.review.issues);await assert.rejects(h.ctx.fillPages(h.data),/findings changed or a page is missing/);assert.equal(h.data.review.page.complete,false);assert.equal(JSON.stringify(h.data.review.issues),initial);});
const node=(tag,attrs,...children)=>({tag,attrs,children:children.flat(Infinity).filter(Boolean)});
const walk=n=>[n,...(n.children||[]).flatMap(c=>typeof c==='object'?walk(c):[])];
test('every one of 1301 problems is individually reachable and exclusions survive pages and collapse',()=>{
 const report={file_hash:'file',check_hash:'check'},state={report},items=Array.from({length:1301},(_,i)=>({key:'k'+i,sheet:'Synthetic',cell:'A'+(i+1),text:'Synthetic item '+i}));
 let tree;const ctx=vm.createContext({state,h:node,reviewBinding:r=>({file_hash:r.file_hash,check_hash:r.check_hash}),drawSide:()=>{tree=ctx.fdProblemList(items);},document:{activeElement:null,querySelectorAll:()=>[]}});
 vm.runInContext(between('  function fdProblemList(', '  function fdOccurrenceList(')+between('  function fdPicked(', '  // AI fix:'),ctx);
 ctx.fdTick(items,true);assert.equal(ctx.fdPicked(items).length,1301);
 walk(tree).find(n=>n.tag==='button'&&n.children.includes('Show all 1301')).attrs.onclick();
 const seen=[];
 while(true){
  const checks=walk(tree).filter(n=>n.tag==='input');assert.ok(checks.length<=25);seen.push(...checks.map(n=>n.attrs['data-tick']));
  if(checks.some(n=>n.attrs['data-tick']==='k1000'))checks.find(n=>n.attrs['data-tick']==='k1000').attrs.onchange({target:{checked:false}});
  const next=walk(tree).find(n=>n.tag==='button'&&n.children.includes('Next errors'));if(next.attrs.disabled)break;
  next.attrs.onclick({currentTarget:{closest:()=>null}});
 }
 assert.equal(new Set(seen).size,1301);assert.equal(seen.length,1301);assert.equal(ctx.fdPicked(items).length,1300);assert.ok(!ctx.fdPicked(items).some(it=>it.key==='k1000'));
 walk(tree).find(n=>n.tag==='button'&&n.children.includes('Show fewer')).attrs.onclick();
 walk(tree).find(n=>n.tag==='button'&&n.children.includes('Show all 1301')).attrs.onclick();
 assert.equal(ctx.fdPicked(items).length,1300);ctx.fdTick(items,false);assert.equal(ctx.fdPicked(items).length,0);
 state.report={...report,check_hash:'later'};assert.equal(ctx.fdPicked(items).length,0);
});
test('existing receipt confirms requested transformation separately from historical parent changes',async()=>{
 const report={file_hash:'a',check_hash:'check',revision:1},binding={file_hash:'a',expected_check_hash:'check'},proposal={id:'p',sheet:'S',cell:'A1',before:'old',after:'new',kind:'text'};
 const item={key:'finding',fix:proposal,proposed:'new',state:'open',text:'Test'},receipt={file_hash:'c',revision:3,fixed_from:{file_hash:'b',changes:[{...proposal,before:'middle'}]},save_outcome:{type:'existing_version',requested_from:'a',expected_check_hash:'check',changes:[proposal]}};
 let done;const state={report,aiOn:true},ctx=vm.createContext({state,reviewBinding:()=>binding,fdCanAI:()=>true,fdReadyFix:it=>it.fix,document:{querySelectorAll:()=>[],body:{classList:{add(){}}}},drawSide(){},captureCorrections:(a,d,e,b)=>({accepted:a,dec:d,edits:e,binding:b}),applyFixes:async()=>receipt,revisionOf:r=>r.revision||1,fdAIDoneDialog:r=>done=r,toast(){}});
 vm.runInContext(between('  function fdUniqueAIProposals(', '  // Fix manually:'),ctx);await ctx.fdAIFixNow([item]);
 assert.equal(done.reused,true);assert.equal(done.version,3);assert.equal(done.confirmed,1);assert.equal(done.originalHash,'a');assert.equal(receipt.fixed_from.file_hash,'b');
 vm.runInContext(between('  function fdAISummaryText(', '  function fdAIDoneDialog('),ctx);const summary=ctx.fdAISummaryText(done,receipt);assert.match(summary,/Opened existing version 3/);assert.match(summary,/No new version was created/);assert.doesNotMatch(summary,/Saved as version 2|FIXED /);
});
test('redraw preserves genuinely typed manual values without staging an untouched suggestion',()=>{
 const report={file_hash:'file'},binding={file_hash:'file'},state={report,fdManual:{binding:JSON.stringify(binding)}};
 let edits={};const input={value:'2',getAttribute:k=>({'data-cell':'A1','data-before':'1','data-initial-value':'2'}[k])};
 const ctx=vm.createContext({state,it:{sheet:'S'},reviewBinding:()=>binding,store:{get:()=>edits,set:(k,v)=>edits=v},editsKey:()=>'key',editId:(s,c)=>s+c,document:{querySelectorAll:()=>[input]}});
 vm.runInContext(between('    const manualBinding =', '    const save = async () =>')+'\nglobalThis.explicitKeep=keep;',ctx);
 state.fdManual.keepDraft();assert.deepEqual(edits,{});
 input.value='3';state.fdManual.keepDraft();assert.equal(edits.SA1.after,'3');
 input.value='1';state.fdManual.keepDraft();assert.deepEqual(edits,{});
 input.value='2';ctx.explicitKeep();assert.equal(edits.SA1.after,'2','explicit Save/Next keeps existing review semantics');
 state.fdManual.binding='different';input.value='4';state.fdManual.keepDraft();assert.equal(edits.SA1.after,'2','stale card never writes into another binding');
});

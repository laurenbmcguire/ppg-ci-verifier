const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync('web/app.js','utf8');
const ctx=vm.createContext({state:{},SETTLED:['manual','verified','matched','source']});
vm.runInContext(source.slice(source.indexOf('  const queueDay ='),source.indexOf('  function drawQueue()'))+'\nObject.assign(globalThis,{queueDay,queueFamily,queueNeedsAttention,queueVersion,queueSearch});',ctx);
const row={file_hash:'a',filename:'Daily.xlsx',received_at:'2026-11-09T10:00:00Z',assignment:{status:'ambiguous',client:'Wrong assumption'},review:{job:'1234',day:null,quantities:{inspected:{value:0,unit:'parts',partial:false},rejected:{value:2,unit:'rows',partial:true}}}};
test('missing report day is never replaced by upload day, ambiguous client remains explicit',()=>{assert.equal(ctx.queueDay(row),'');assert.equal(ctx.queueClient(row),'Client not confirmed');assert.equal(ctx.queueSearch(row).includes('wrong assumption'),false);});
test('known zero, partial, unknown and units stay distinct',()=>{assert.equal(ctx.queueQuantity(row,'inspected'),'0 parts');assert.equal(ctx.queueQuantity(row,'rejected'),'2 rows (partial)');assert.equal(ctx.queueQuantity(row,'certified'),'Unknown');for(const value of ['',null,true,'NaN',-1])assert.equal(ctx.queueQuantity({review:{quantities:{inspected:{value}}}},'inspected'),'Unknown');});
test('same name and same client job day do not form a version family',()=>{assert.notEqual(ctx.queueFamily(row),ctx.queueFamily({...row,file_hash:'b'}));assert.equal(ctx.queueFamily(row),ctx.queueFamily({...row,file_hash:'c',root_hash:'a'}));});
test('missing review and incomplete or stale checks cannot be clear or omitted from To do',()=>{for(const r of [{},{review:{status:'clear',coverage:{truncated:true}}},{review:{status:'clear'},internal_check_status:'outdated_checks_require_recheck'}]){assert.ok(ctx.queueNeedsAttention(r));assert.notEqual(ctx.queueCheck(r)[0],'No open findings');}assert.equal(ctx.queueNeedsAttention({review:{status:'hold',sent_at:'at'}}),false);});
test('version labels reject zero and malformed versions',()=>{for(const value of [null,0,-1,'oops','2',true])assert.equal(ctx.queueVersion(value),'unknown');assert.equal(ctx.queueVersion(2),'2');});
test('search includes confirmed client, source, date, version and exact identity',()=>{const r={...row,version:2,review:{...row.review,day:'2026-11-06',sharepoint:{source:'Synthetic Source'}},assignment:{client:'Synthetic Client',status:'manual'}};for(const q of ['synthetic client','2026-11-06','synthetic source','v2'])assert.ok(ctx.queueSearch(r).includes(q));});
test('separate review control and inventory coverage disclosure are present',()=>{assert.ok(source.includes("state.listPartial = data.has_more === true"));assert.ok(source.includes("class: 'queue-entry'"));assert.ok(source.includes("'aria-pressed'"));assert.ok(!source.includes("dayOf = r => r.review?.day || (r.received_at"));});

test('client tab and rail attention badges use exactly the To do predicate and visible inventory',()=>{
 const clear={status:'clear',job:'SYNTHETIC-1234',day:'2026-11-06',sharepoint:{status:'match',source_verified:true}};
 const make=(id,key,review={})=>({holds:[],internal_check_status:'no_findings_in_checks_performed',file_hash:id,filename:'Synthetic '+id+'.xlsx',assignment:{status:'manual',client_key:key,client:key},review:{...clear,...review}});
 const list=[make('source-incomplete','alpha',{sharepoint:{status:'incomplete'}}),make('source-stale','beta',{sharepoint:{status:'stale'}}),
  {...make('client-ambiguous','alpha'),assignment:{status:'ambiguous'}},{...make('review-missing','alpha'),review:null},
  make('already-sent','beta',{status:'hold',sharepoint:{status:'error'},sent_at:'2026-11-07'}),make('clear','alpha'),
  make('hidden-original','alpha',{status:'hold'}),make('cleaned-copy','alpha',{auto_hidden:['hidden-original']}),
  {...make('client-unknown','alpha'),assignment:null},make('coverage-incomplete','beta',{coverage:{truncated:true}})];
 const bar={replaceChildren(...children){this.children=children;}},rail={replaceChildren(...children){this.children=children;}};
 const state={list,client:'',clients:[{key:'alpha',client:'Alpha',sites:[],jobs:[]},{key:'beta',client:'Beta',sites:[],jobs:[]}]};
 const context=vm.createContext({state,location:{hash:''},$:id=>id==='clienttabs'?bar:rail,h:(tag,attrs,...children)=>({tag,attrs,children:children.flat()})});
 const section=(start,end)=>source.slice(source.indexOf(start),source.indexOf(end,source.indexOf(start)));
 vm.runInContext(section('  const SETTLED =','  const inClientR')+section('  const visibleList =','  function queueClient(')+section('  function drawClientTabs()','  // ---------------------------------------------------------------- queue')+'\nObject.assign(globalThis,{visibleList,queueNeedsAttention});drawClientTabs();',context);
 const badge=node=>Number(node.children.find(child=>child?.tag==='i')?.children[0]||0);
 const tabs=Object.fromEntries(bar.children.map(node=>[node.children[0],badge(node)]));
 const rails=Object.fromEntries(rail.children.filter(node=>node.tag==='a').map(node=>[node.children.find(child=>child?.tag==='span').children[0],badge(node)]));
 assert.deepEqual(tabs,{'All clients':2,Alpha:1,Beta:1,'Needs client':0});
 assert.deepEqual(rails,{'All clients':2,Alpha:1,Beta:1,'Needs client':0});
 assert.equal(tabs['All clients'],context.visibleList().filter(context.queueNeedsAttention).length);
 // A badge must clear when its only review needs are resolved, without treating sent/hidden rows as work.
 list[0].review.sharepoint={status:'match',source_verified:true};list[3].review={...clear};context.drawClientTabs();
 assert.equal(badge(bar.children.find(node=>node.children[0]==='Alpha')),0);
 assert.equal(badge(rail.children.find(node=>node.tag==='a'&&node.children[0].children[0]==='Alpha')),0);
});

test('filtered historical attention retains newer same-family context without declaring a current branch',()=>{
 const original={holds:[],internal_check_status:'no_findings_in_checks_performed',file_hash:'original',filename:'Same.xlsx',received_at:'2026-11-09T01:00:00Z',assignment:{status:'manual',client_key:'alpha'},review:{status:'clear',job:'1234',day:'2026-11-06',sharepoint:{status:'differences'}}};
 const corrected={...original,file_hash:'corrected',root_hash:'original',revision:2,received_at:'2026-11-09T02:00:00Z',review:{...original.review,sharepoint:{status:'match',source_verified:true}}};
 const parallel={...corrected,file_hash:'parallel',received_at:'2026-11-09T03:00:00Z'};
 const unrelated={...corrected,file_hash:'unrelated',root_hash:null};
 const inventory=[original,corrected,parallel,unrelated];
 assert.ok(ctx.queueNeedsAttention(original));assert.equal(ctx.queueNeedsAttention(corrected),false);
 assert.deepEqual(Array.from(ctx.queueNewerCopies(original,inventory),r=>r.file_hash),['parallel','corrected']);
 assert.deepEqual(Array.from(ctx.queueNewerCopies(corrected,inventory),r=>r.file_hash),['parallel']);
 assert.equal(ctx.queueNewerCopies(unrelated,inventory).length,0);
 assert.equal(ctx.queueNewerCopies({...original,received_at:null},inventory).length,0);
 corrected.review={...corrected.review,status:'check'};assert.ok(ctx.queueNeedsAttention(corrected));
 assert.deepEqual(Array.from(ctx.queueNewerCopies(original,inventory),r=>r.file_hash),['parallel','corrected']);
 assert.ok(source.includes('queueNewerCopies(r, shown)'));assert.ok(source.includes('Earlier saved copy'));
 assert.ok(source.includes('copies may be parallel branches'));assert.ok(source.includes('saved versions needing attention'));
});

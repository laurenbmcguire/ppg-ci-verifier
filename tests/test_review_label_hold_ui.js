const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync('web/app.js','utf8');
const between=(a,b)=>source.slice(source.indexOf(a),source.indexOf(b,source.indexOf(a)));
const text=n=>typeof n==='string'?n:n?.kids?.flat(Infinity).map(text).join(' ')||'';
const nodes=n=>n?.kids?.flat(Infinity).flatMap(x=>x&&typeof x==='object'?[x,...nodes(x)]:[])||[];
function harness(holds){const state={fixes:[],report:{file_hash:'h',check_hash:'c',fixed_from:{changes:[]},holds,review:{issues:[]}}};const h=(tag,attrs,...kids)=>({tag,...attrs,kids});const ctx=vm.createContext({state,h,plural:(n,s)=>`${n} ${s}${n===1?'':'s'}`,fdMode:()=> 'manual',reviewBinding:()=>({}),allIssues:r=>r.issues,fdRecommended:()=>[],revisionOf:()=>2,fdChooseMode(){},fdPreviewRecommended(){},fdStatus:()=>({next:'blockers'}),fdCounts:()=>({}),fdItems:()=>[],fdGroups:()=>[],icon:()=>null,fdHoldAction:()=>null,fdNextStep:()=>null,fdAutoSummary:()=>null,fdClean:()=>null});vm.runInContext(between('  function fdHoldSummary(', '  function fdOccurrenceList(')+between('  function fdCorrections(', '  // ---------- the app fixes'),ctx);return {ctx,state};}
test('saved summary and disclosure share blocking/advisory counts, and no hold disappears',()=>{
 for(const [holds,expected] of [[[],'0 blocking holds · 0 advisory holds'],[[{hard:true,text:'Blocked'}],'1 blocking hold · 0 advisory holds'],[[{hard:false,text:'Advisory'}],'0 blocking holds · 1 advisory hold'],[[{hard:true,text:'Blocked'},{hard:false,text:'Advisory'}, {text:'Unclassified advisory'}],'1 blocking hold · 2 advisory holds']]){
  const {ctx,state}=harness(holds);assert.equal(ctx.fdHoldSummary(state.report),expected);const mode=ctx.fdModeSelector(state.report);assert.ok(text(nodes(mode).find(n=>n.class==='fd-saved-summary')).includes(expected));
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

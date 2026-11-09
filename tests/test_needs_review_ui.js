const test=require('node:test'),assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');
const source=fs.readFileSync('web/app.js','utf8');const between=(a,b)=>source.slice(source.indexOf(a),source.indexOf(b,source.indexOf(a)));
test('selected-scope advances never enter unselected findings and external revisions stop',()=>{
 const calls=[],state={report:{id:1},fdItems:[{key:'outside',state:'open'},{key:'picked',state:'open'},{key:'done',state:'selected'}],fdReviewScope:{binding:'{"id":1}',keys:['done','picked']}};
 const ctx=vm.createContext({state,reviewBinding:r=>r,toast:x=>calls.push(x),refreshHeadFd:()=>{},fdOpen:it=>calls.push(it.key),drawSide:()=>{},drawGrid:()=>{}});
 vm.runInContext(between('  function fdAdvanceSelected(', '  function fdAdvance(done)'),ctx);
 assert.equal(ctx.fdAdvanceSelected({key:'done'},'Saved.'),true);assert.equal(calls.at(-1),'picked');
 state.fdItems[1].state='kept';ctx.fdAdvanceSelected({key:'done'},'Saved.');assert.match(calls.at(-1),/No further open findings/);
 state.report.id=2;ctx.fdAdvanceSelected({key:'done'},'Saved.');assert.equal(state.fdReviewScope,null);assert.match(calls.at(-1),/review changed/);
});
test('chooser never calls direct AI apply or mutates existing correction basket',()=>{
 const popup=between('  function fdReviewDialog(', '  function fdOccurrenceList(');
 assert.ok(popup.includes('findingBinding(scope)'));assert.ok(popup.includes('captureCorrections(accepted, dec, {}, binding, report)'));
 assert.ok(!popup.includes('store.set'));assert.ok(!popup.includes('aiFix('));assert.ok(!popup.includes('/ai-fix'));
 assert.ok(popup.includes('scope.length <= 1000'));assert.ok(popup.includes('byType'));assert.ok(popup.includes("['open', 'selected', 'deferred', 'confirmed', 'claude']"));
});
class Element{constructor(t){this.tag=t;this.nodeType=1;this.children=[];}setAttribute(k,v){this[k]=v;}append(...x){this.children.push(...x.flat());}}
const textOf=n=>typeof n==='string'?n:(n.children||[]).map(textOf).join(' ');
test('detail quantities label partial and legacy values and suppress unsupported rates',()=>{
 const ctx=vm.createContext({document:{createElement:t=>new Element(t)},fmt:x=>String(x)});vm.runInContext(between('  const h = (','  const store =')+between('  function fdMetrics(', '  // resizable / collapsible'),ctx);
 let text=textOf(ctx.fdMetrics({review:{scans:2,inspected:10,rejected:1,certified:9}}));assert.match(text,/Partial or completeness unknown/);assert.ok(!text.includes('10.0%'));
 text=textOf(ctx.fdMetrics({review:{scans:2,quantities:{inspected:{value:10,partial:false,complete:true,unit:'parts'},certified:{value:9,partial:false,complete:true,unit:'parts'},rejected:{value:1,partial:false,complete:true,unit:'parts'}}}}));assert.match(text,/10.0%/);
 text=textOf(ctx.fdMetrics({review:{scans:2,quantities:{inspected:{value:10,partial:true},certified:{value:9,partial:false,complete:true,unit:'parts'},rejected:{value:1,partial:false,complete:true,unit:'parts'}}}}));assert.ok(!text.includes('10.0%'));
});

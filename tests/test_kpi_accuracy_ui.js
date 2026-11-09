const test=require('node:test'),assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');
const source=fs.readFileSync('web/app.js','utf8'),between=(a,b)=>source.slice(source.indexOf(a),source.indexOf(b,source.indexOf(a)));
class Element{constructor(tag){this.tag=tag;this.nodeType=1;this.children=[];this.style={};}append(...x){this.children.push(...x.flat());}replaceChildren(...x){this.children=x.flat().filter(v=>v!=null);}setAttribute(k,v){this[k]=v;}}
const walk=n=>n.children.flatMap(x=>typeof x==='object'?[x,...walk(x)]:[]),textOf=n=>typeof n==='string'?n:[n.textContent||'',...n.children.map(textOf)].join(' ');
function setup(hash,data){const root=new Element('main');const ctx=vm.createContext({document:{createElement:t=>new Element(t),createElementNS:(ns,t)=>new Element(t)},$:()=>root,location:{hash},state:{kpi:{days:90,client:'synthetic',job:''}},fmt:v=>v==null?'—':String(v),api:async()=>data,recsBlock:()=>null,changeRecords:()=>null});vm.runInContext(between('  const h = (','  const store =')+between('  const statisticNumber =','  function builtBanner('),ctx);return{ctx,root};}
const totals={reports:4,jobs:2,clients:1,caught:17,caught_complete:true,current_issues:6,current_issues_complete:true,must_fix:3,corrected:2,changes:11,sent:0,unsent:4,uploads:8,on_time:0,on_time_unknown:0,on_time_pct:null,turnaround:null,turnaround_reports:0,hold:1,open:1,inspected:null,inspected_known:12.5,inspected_complete_reports:1,inspected_missing_reports:3,inspected_incompatible_reports:0,rejected:null,rejected_known:1.25,rejected_complete_reports:1,rejected_missing_reports:3,rejected_incompatible_reports:0,certified:null,certified_known:11.25,certified_complete_reports:1,certified_missing_reports:3,certified_incompatible_reports:0,rate:null,stopped:null,reached:null};
const fixture={totals,label:'Last 90 days',days:90,end:'2026-11-08',categories:[],clients:[],jobs:[{...totals,job:'Synthetic',days:4}],client_name:'Synthetic',trend:[{...totals,day:'2026-11-01'}],statistics:{definitions:{caught:'Current review of first original.'},coverage:{excluded_assignment:2,duplicate_versions:4}}};
test('KPI and Overview use same first-original findings and show unknown rates/coverage',async()=>{
 for(const [hash,fn] of [['#kpis','showKpis'],['#overview','showOverview']]){
  const {ctx,root}=setup(hash,fixture);await ctx[fn]();const text=textOf(root);
  assert.match(text,/17/);assert.match(text,/Findings on first uploads/);assert.match(text,/current review/i);assert.match(text,/Unknown/);assert.match(text,/Known subtotal: 12.5/);assert.match(text,/3 missing\/partial/);assert.match(text,/excluded assignment: 2/);
  assert.ok(!/fixed before|stopped before|reached a client|Every report.*clean/.test(text));assert.match(text,/Reject rate:? Unknown/);
 }
});
test('daily canonical series preserves unknowns and coverage for long periods',()=>{
 const {ctx}=setup('#kpis',fixture);const trend=[{day:'2026-01-01',reports:1,inspected:10,rejected:0,rate:0},{day:'2026-01-02',reports:1,inspected:null,inspected_known:3,rejected:null,rate:null}];
 const series=ctx.kpiSeries(trend,365);assert.equal(series.length,2);assert.equal(series[1].inspected,null);assert.equal(series[1].rate,null);assert.equal(series[1].inspected_known,3);assert.equal(series[0].rate,0);
});
test('charts mark unknown explicitly instead of plotting a fabricated zero',()=>{
 const {ctx}=setup('#kpis',fixture);const chart=ctx.kpiChart([{v:null,label:'Unknown day',short:'Jan1'},{v:0,label:'Known zero',short:'Jan2'},{v:5,label:'Known five',short:'Jan3'}],{label:'Quantity',fmt:String});
 const nodes=walk(chart);assert.equal(nodes.filter(n=>n.class==='unknown').length,1);assert.equal(nodes.filter(n=>n.tag==='path').length,1);assert.match(textOf(chart),/Unknown day: Unknown/);assert.match(textOf(chart),/Known zero: 0/);assert.match(chart['aria-label'],/1 unknown/);
});
test('uncertain original or ordering basis is not labeled as a lower bound',()=>{
 const {ctx}=setup('#kpis',{...fixture,totals:{...totals,caught_complete:false}});assert.match(vm.runInContext('statisticFindings({caught:12,caught_complete:false})',ctx),/12 \(basis incomplete or uncertain\)/);
});

test('incomplete finding lists do not become complete chart bars',async()=>{
 const {ctx,root}=setup('#kpis',{...fixture,totals:{...totals,caught_complete:false},trend:[{...totals,day:'2026-11-01',caught_complete:false}]});
 await ctx.showKpis();const chart=walk(root).find(n=>n.tag==='svg'&&n['aria-label'].startsWith('Findings on first uploads'));
 assert.equal(walk(chart).filter(n=>n.class==='unknown').length,1);assert.equal(walk(chart).filter(n=>n.tag==='path').length,0);
});

test('quantity totals and chart tooltips preserve supplied decimal precision',async()=>{
 const {ctx,root}=setup('#kpis',{...fixture,totals:{...totals,inspected:13.125},trend:[{...totals,day:'2026-11-01',inspected:13.125}]});
 await ctx.showKpis();const chart=walk(root).find(n=>n.tag==='svg'&&n['aria-label'].startsWith('Inspected quantity'));
 assert.match(textOf(chart),/13\.125/);assert.equal(vm.runInContext('statisticNumber(13.125)',ctx),'13.125');assert.equal(vm.runInContext('statisticNumber(0)',ctx),'0');assert.equal(vm.runInContext('statisticNumber(null)',ctx),'Unknown');
});

test('source-specific rows disclose scope and job-wide navigation',async()=>{
 const jobs=['synthetic-site|list-a|','synthetic-site|list-b|'].map(source=>({...totals,job:'Same synthetic job',source,source_label:source.endsWith('list-a|')?'Synthetic north':'Synthetic south',days:4}));
 for(const [hash,fn] of [['#kpis','showKpis'],['#overview','showOverview']]){
  const {ctx,root}=setup(hash,{...fixture,jobs});await ctx[fn]();const text=textOf(root);
  assert.match(text,/Source scope: Synthetic north/);assert.match(text,/Source scope: Synthetic south/);assert.match(text,/synthetic-site\|list-a/);assert.match(text,/Exact source scope/);
  assert.match(text,/not source-verified totals/);assert.match(text,/navigation combines source groups/);
  if(hash==='#overview') assert.match(text,/View job reports/);
 }
});
test('empty selections describe eligibility rather than absence of reports',async()=>{
 for(const [hash,fn] of [['#kpis','showKpis'],['#overview','showOverview']]){
  const {ctx,root}=setup(hash,{...fixture,totals:{...totals,reports:0},jobs:[]});await ctx[fn]();assert.match(textOf(root),/No eligible daily reports/);
 }
});

test('unsupported aggregate range explains unknown even with no missing reports',()=>{
 const {ctx}=setup('#kpis',fixture);
 const text=vm.runInContext("statisticQuantity({inspected:null,inspected_known:null,inspected_range_exceeded:true,inspected_complete_reports:4,inspected_missing_reports:0,inspected_incompatible_reports:0},'inspected')",ctx);
 assert.match(text,/total exceeds the supported numeric range/);assert.match(text,/0 missing/);
});

test('coverage exclusions disclose their global rather than selected scope',async()=>{
 const coverage_scope='All authorized saved reports, before client/job filters; not the selected scope denominator.';
 for(const [hash,fn] of [['#kpis','showKpis'],['#overview','showOverview']]){
  const {ctx,root}=setup(hash,{...fixture,statistics:{...fixture.statistics,coverage_scope}});await ctx[fn]();assert.ok(textOf(root).includes(coverage_scope));
 }
});

test('missing original or uncertain ordering discloses uncertain basis in both views',async()=>{
 for(const [hash,fn] of [['#kpis','showKpis'],['#overview','showOverview']]){
  const {ctx,root}=setup(hash,{...fixture,totals:{...totals,caught_complete:false},statistics:{...fixture.statistics,coverage:{missing_original_groups:1,uncertain_order_groups:1}}});await ctx[fn]();const text=textOf(root);
  assert.match(text,/17 \(basis incomplete or uncertain\)/);assert.match(text,/not a lower-bound count/);assert.ok(!text.includes('known (incomplete list)'));
 }
});

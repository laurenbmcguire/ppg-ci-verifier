// Actual shipped functions; made-up state only, no server, browser, workbook or provider.
// A report is never shown with 0 errors while Fix with AI left some errors not corrected.
const test=require('node:test'),assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');
const source=fs.readFileSync('web/app.js','utf8'),between=(a,b)=>source.slice(source.indexOf(a),source.indexOf(b,source.indexOf(a)));
const WHY='Dex had no safe change to make and no correct value to put in, so this was left exactly as written by Fix with AI. Not corrected.';
const text=n=>n==null||n===false?'':Array.isArray(n)?n.map(text).join(''):typeof n==='object'?n.kids.map(text).join(''):String(n);
const h=(tag,attrs,...kids)=>({tag,attrs:attrs||{},kids});
const plural=(n,s)=>n+' '+s+(n===1?'':'s');
function harness({items=[],dec={}}={}){
 const state={report:{file_hash:'a',filename:'r.xlsx'},fixes:[],fdItems:items};
 const ctx=vm.createContext({state,h,plural,icon:()=>'',store:{get:()=>dec},decisionsKey:()=>'k',fdItems:()=>items,drawReport(){},fdManualStart(){},fdReviewDialog(){},sharePointUrl:()=>null});
 vm.runInContext(between('  function fdNotCorrected(','  function fdRecordCount(')+between('  function fdShown(','  function fdPanelStatus(')+between('  function fdRevisionSummary(','  function fdAIRunBanner('),ctx);
 return ctx;
}
const unsolved=n=>Array.from({length:n},(_,i)=>({key:'u'+i,where:'1st Shift row '+(i+16),text:'Duplicate identifier'}));
const notCorrected=n=>Array.from({length:n},(_,i)=>({key:'u'+i,fp:'f'+i,state:'kept',note:WHY}));

test('errors Fix with AI could not correct are counted; ordinary ignored ones are not',()=>{
 const items=[...notCorrected(19),{key:'i1',fp:'x',state:'kept',note:'Checked with the plant: a real second inspection.'},{key:'i2',fp:'y',state:'rejected',note:'Dex had no safe change to make, so this was left as written by Fix with AI.'},
  {key:'g1',fix:{id:'g1'},state:'rejected',note:''},{key:'g2',fix:{id:'g2'},state:'rejected',note:''},{key:'o',fp:'z',state:'open',note:WHY}];
 const ctx=harness({items,dec:{g1:{state:'rejected',notCorrected:true},g2:{state:'rejected'}}});
 assert.equal(ctx.fdNotCorrected().length,20);            // 19 by their recorded reason + 1 guessed correction set aside; the open one is counted as open, not here
});

test('the status line says how many were not corrected instead of Completed',()=>{
 const st={tone:'red',icon:'stop',text:'Blocked',next:'blockers'};
 const shown=harness({items:notCorrected(19)}).fdShown(st);
 assert.equal(shown.text,'19 errors not corrected');assert.notEqual(shown.tone,'green');assert.match(shown.why,/still wrong in the report/);
 assert.equal(harness({items:notCorrected(1)}).fdShown(st).text,'1 error not corrected');
 assert.equal(harness({items:[{key:'i',fp:'x',state:'kept',note:'A real second inspection.'}]}).fdShown(st).text,'Completed');
 assert.match(harness({items:[...notCorrected(2),{key:'o',fp:'z',state:'open',note:''}]}).fdShown(st).text,/^1 possible error to fix or ignore/);
});

test('the revision summary gives one number: never 19 not corrected beside 0 left',()=>{
 const ctx=harness({items:notCorrected(19)});
 const box=ctx.fdRevisionSummary({fixed:0,leftAsWritten:10,unsolved:unsolved(19)},0,null),t=text(box);
 assert.match(t,/19 errors Dex could not correct: left exactly as written, not checked by anyone/);
 assert.doesNotMatch(t,/0 possible errors still need you/);assert.doesNotMatch(t,/Nothing is left to fix/);
 assert.match(t,/The report still has 19 errors that were not corrected\. The count at the top shows the same number\./);
 assert.doesNotMatch(box.attrs.class,/good/);
 const one=text(ctx.fdRevisionSummary({fixed:0,leftAsWritten:0,unsolved:unsolved(1)},0,null));
 assert.match(one,/The report still has 1 error that was not corrected/);
});

test('with nothing left uncorrected the summary still says 0 and is marked good; open ones are still counted',()=>{
 const ctx=harness(),clean=ctx.fdRevisionSummary({fixed:3,leftAsWritten:2,unsolved:[]},0,null);
 assert.match(text(clean),/0 possible errors still need you/);assert.match(text(clean),/Nothing is left to fix/);assert.match(clean.attrs.class,/good/);
 const mixed=ctx.fdRevisionSummary({fixed:1,leftAsWritten:0,unsolved:unsolved(4)},2,null);
 assert.match(text(mixed),/4 errors Dex could not correct/);assert.match(text(mixed),/2 possible errors still need you/);assert.doesNotMatch(mixed.attrs.class,/good/);
});

test('the count at the top of the report uses the same number and is not shown as done',()=>{
 const head=between("h('p', {class: 'fd-errcount'","h('button', {type: 'button', class: 'btn', id: 'fd-ai-any'");
 assert.match(head,/open === 0 && !notCorrected \? ' done' : ''/);
 assert.match(head,/open === 0 && notCorrected \? `\$\{plural\(notCorrected, 'error'\)\} not corrected`/);
 assert.match(head,/plural\(open, 'possible error'\) \+ \(notCorrected \? ` · \$\{notCorrected\} not corrected` : ''\)/);
 assert.match(source,/state: 'rejected', notCorrected: true\}/);
 assert.match(source,/unsolved\.length \? '' : 'No possible errors left\.'/);
});

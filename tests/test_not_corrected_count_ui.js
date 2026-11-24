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
 const ctx=vm.createContext({state,h,plural,icon:()=>'',store:{get:()=>dec},decisionsKey:()=>'k',fdItems:()=>items,fdGroups:its=>[{items:its,title:'Duplicate identifier'}],drawReport(){},fdManualStart(){},fdReviewDialog(){},sharePointUrl:()=>null});
 vm.runInContext(between('  function fdNotCorrected(','  function fdRecordCount(')+between('  function fdPlainSummary(','  function fdSummaryContext(')+between('  function fdShown(','  function fdPanelStatus(')+between('  function fdRevisionSummary(','  function fdAIRunBanner('),ctx);
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
 assert.match(harness({items:[...notCorrected(2),{key:'o',fp:'z',issue:{key:'o'},state:'open',note:''}]}).fdShown(st).text,/^1 possible error to fix or ignore/);
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

test('what Dex left as written is asked about: review or ignore',()=>{
 const find=(n,id)=>n&&typeof n==='object'?(Array.isArray(n)?n.map(x=>find(x,id)).find(Boolean):n.attrs?.id===id?n:find(n.kids,id)):null;
 const ctx=harness();let reviewed=null,drawn=0;ctx.fdReviewLeft=r=>{reviewed=r;};ctx.drawSide=()=>{drawn++;};
 const run={fixed:2,leftAsWritten:3,unsolved:[],leftKeys:['a','b','c']},box=ctx.fdRevisionSummary(run,0,null),t=text(box);
 assert.match(t,/3 left as written/);assert.match(t,/Dex left 3 errors as written\. Would you like to review them or ignore them\?/);
 assert.equal(text(find(box,'fd-left-review')),'Review them');assert.equal(text(find(box,'fd-left-ignore')),'Ignore them');
 find(box,'fd-left-review').attrs.onclick();assert.equal(reviewed,run);
 find(box,'fd-left-ignore').attrs.onclick();assert.equal(ctx.state.fdAIRun.leftAnswered,'ignored');assert.equal(drawn,1);
 const after=ctx.fdRevisionSummary(ctx.state.fdAIRun,0,null);                                    // answered: not asked twice
 assert.equal(find(after,'fd-revision-ask'),undefined);assert.match(text(after),/Ignored: 3 errors stay as written\. Open Ignored to bring them back/);
 const both=text(ctx.fdRevisionSummary({fixed:0,leftAsWritten:1,unsolved:unsolved(4)},0,null));    // both kinds are counted in the question
 assert.match(both,/Dex left 5 errors as written/);
 assert.match(text(ctx.fdRevisionSummary({fixed:0,leftAsWritten:1,unsolved:[]},0,null)),/Dex left 1 error as written\. Would you like to review it or ignore it\?/);
 for(const quiet of [{fixed:3,leftAsWritten:0,unsolved:[]},{fixed:1,manual:true,leftAsWritten:0,unsolved:[]},{tidy:true,fixed:0,leftAsWritten:0,items:[]}])
  assert.equal(find(ctx.fdRevisionSummary(quiet,0,null),'fd-revision-ask'),undefined);            // nothing left as written: no question
});

test('Review them puts exactly those errors back on the list and opens them for fixing by hand',async()=>{
 const items=[{key:'a',fp:'1',state:'kept'},{key:'b',fp:'2',state:'rejected'},{key:'c',fp:'3',state:'open'},{key:'other',fp:'4',state:'kept'}];
 const state={report:{file_hash:'h'},fixes:[],fdAIRun:{summary:true}},sent=[],started=[],toasts=[];
 const ctx=vm.createContext({state,setTimeout,fdItems:()=>items,findingBinding:()=>({file_hash:'h'}),fdActiveBusy:()=>false,drawSide(){},toast:(m,bad)=>toasts.push([m,bad]),
  fdManualStart:list=>started.push(list.map(x=>x.key)),fdSendDecision:async d=>{sent.push(d);for(const it of items)if(d.ids.includes(it.key))it.state='open';}});
 vm.runInContext(between('  async function fdReviewLeft(','  function fdAIRunBanner('),ctx);
 await ctx.fdReviewLeft({leftKeys:['a','b','c']});
 assert.equal(sent.length,1);assert.equal(sent[0].action,'reopen');assert.deepEqual([...sent[0].ids],['a','b']);assert.match(sent[0].reason,/chose to review what Dex left as written/);
 assert.deepEqual(started,[['a','b','c']]);assert.equal(state.fdAIRun,null);assert.equal(items[3].state,'kept');assert.equal(toasts.length,0);
 // A refused save changes nothing and says so; the summary stays so the question can be answered again.
 const items2=[{key:'a',fp:'1',state:'kept'}],state2={report:{file_hash:'h'},fixes:[],fdAIRun:{summary:true}},t2=[],s2=[];
 const ctx2=vm.createContext({state:state2,setTimeout,fdItems:()=>items2,findingBinding:()=>({}),fdActiveBusy:()=>false,drawSide(){},toast:(m,bad)=>t2.push([m,bad]),fdManualStart:l=>s2.push(l),fdSendDecision:async()=>{throw new Error('offline');}});
 vm.runInContext(between('  async function fdReviewLeft(','  function fdAIRunBanner('),ctx2);
 await ctx2.fdReviewLeft({leftKeys:['a']});
 assert.equal(s2.length,0);assert.deepEqual(state2.fdAIRun,{summary:true});assert.match(t2[0][0],/could not be put back on the list/);assert.equal(t2[0][1],true);
});

test('the count at the top of the report uses the same number and is not shown as done',()=>{
 const head=between("h('p', {class: 'fd-errcount'","h('button', {type: 'button', class: 'btn', id: 'fd-ai-any'");
 assert.match(head,/open === 0 && !notCorrected \? ' done' : ''/);
 assert.match(head,/open === 0 && notCorrected \? `\$\{plural\(notCorrected, 'error'\)\} not corrected`/);
 assert.match(head,/fdOpenWords\(openCounts\) \+ \(notCorrected \? ` · \$\{notCorrected\} not corrected` : ''\)/);
 assert.match(source,/state: 'rejected', notCorrected: true\}/);
 assert.match(source,/unsolved\.length \? '' : 'No possible errors left\.'/);
});

test('the report page counts the same errors as the report list; suggested changes are named beside the number',()=>{
 const err=(k,state='open')=>({key:k,fp:k,issue:{key:k},state,note:''}),sug=(k,state='open')=>({key:'fix:'+k,fix:{id:k},state,note:''});
 // The Nov 21 case: the list showed 9, the page showed 11 because two blank-header suggestions were added in.
 const items=[...Array.from({length:9},(_,i)=>err('e'+i)),sug('d5'),sug('d6'),err('done','kept')],ctx=harness({items});
 const c=ctx.fdOpenCounts();assert.deepEqual({...c},{open:11,errors:9,suggested:2});
 assert.equal(ctx.fdOpenWords(c),'9 possible errors · 2 suggested changes');
 const st={tone:'red',icon:'stop',text:'Blocked',next:'blockers'};
 assert.equal(ctx.fdShown(st).text,'9 possible errors to fix or ignore · 2 suggested changes');
 assert.equal(ctx.fdShown({tone:'amber',icon:'i',text:'x',next:'review'}).text,'9 possible errors to fix or ignore · 2 suggested changes');
 const only=harness({items:[sug('d5')]});
 assert.equal(only.fdOpenWords(only.fdOpenCounts()),'1 suggested change');assert.equal(only.fdShown(st).text,'1 suggested change to accept or ignore');
 const plain=harness({items:[err('a'),err('b','deferred')]});
 assert.equal(plain.fdOpenWords(plain.fdOpenCounts()),'2 possible errors');assert.equal(plain.fdShown(st).text,'2 possible errors to fix or ignore');
 assert.equal(harness().fdOpenWords({open:0,errors:0,suggested:0}),'0 possible errors');
 assert.match(source,/state\.fdLastCount = \{hash: r\.file_hash, n: openCounts\.errors\}/);
});

test('the quick summary and the fix-everything question use the same count',()=>{
 const err=k=>({key:k,fp:k,issue:{key:k},state:'open',note:''}),sug=k=>({key:'fix:'+k,fix:{id:k},state:'open',note:''});
 const items=[...Array.from({length:9},(_,i)=>err('e'+i)),sug('d5'),sug('d6')],ctx=harness({items});
 assert.equal(ctx.fdCountWords(items),'9 possible errors and 2 suggested changes');
 assert.match(ctx.fdPlainSummary(),/^The loaded findings contain 9 possible errors and 2 suggested changes in 1 group\./);
 assert.equal(ctx.fdCountWords([err('a')]),'1 possible error');assert.equal(ctx.fdCountWords([sug('a')]),'1 suggested change');
 assert.equal(ctx.fdCountWords([{key:'plain',state:'open'}]),'1 possible error');     // an item with no proposed change is an error, never a suggestion
 assert.match(source,/What would you like to do with \$\{all\.length === 1 \? \(fdCountsOf\(all\)\.suggested \? 'this suggested change' : 'this possible error'\) : `these \$\{fdCountWords\(all\)\}`\}\?/);
 assert.doesNotMatch(source,/these \$\{all\.length\} possible errors/);
});

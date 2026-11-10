// Actual zero-proposal AI flow: current server evidence, no workbook writes.
const test=require('node:test'),assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');
const src=fs.readFileSync('web/app.js','utf8'),part=(a,b)=>src.slice(src.indexOf(a),src.indexOf(b,src.indexOf(a)));
async function run(mode){
 const binding={file_hash:'a',expected_check_hash:'old'},report={file_hash:'a',check_hash:'old',checker_version:'v'},scope=[{key:'shape',state:'open',sheet:'Inspection Detail',row:15,text:'Part 00356870-V-LH differs',binding,issue:{source:'Checks',code:'part_format_differs'}}];
 const hint={key:'shape',sheet:'Inspection Detail',row:15,source:'Checks',code:'part_format_differs',severity:'info',source_support:{kind:'source_confirmed_pattern',snapshot_id:'snap',captured_at:new Date().toISOString()}};
 const fresh={file_hash:'a',check_hash:'new',checker_version:'v',internal_check_status:'findings',source_reconciliation:{snapshot_id:'snap'},checked_copy:{file_hash:'a',check_hash:'new',coverage_complete:true},review:{issues:[hint],dismissed:[],page:{complete:true,issues_total:1,dismissed_total:0},coverage:{truncated:false,not_listed:0,findings_total:1,findings_listed:1,all_findings:1}}};
 const working={status:'current',requested_hash:'a',working_hash:'a'};
 if(mode==='expired')hint.source_support.captured_at='2000-01-01T00:00:00Z';
 if(mode==='incomplete')fresh.review.page.complete=false;
 if(mode==='other-file')fresh.file_hash='b';
 if(mode==='mismatch')hint.severity='high';
 if(mode==='superseded')working.status='superseded';
 let done,draws=0,loads=0,reads=0;const state={report,aiOn:true};
 const ctx=vm.createContext({state,location:{hash:'#r=a'},reviewBinding:()=>binding,fdCanAI:()=>true,fdReadyFix:()=>null,fdSourceGap:()=>false,document:{querySelectorAll:()=>[],body:{classList:{add(){}}}},drawSide(){},drawReport(){draws++},loadFixes(){loads++},plural:(n,s)=>`${n} ${s}${n===1?'':'s'}`,revisionOf:()=>1,toast(){},
 fdPrepareAI:async(s,b,v)=>v.success({proposals:[],skipped:[{finding_id:'shape',reason:'No editable cell'}]}),
 fillPages:async x=>x,api:async path=>{reads++;if(mode==='read-failed')throw Error('read failed');if(mode==='navigation')state.report={file_hash:'other'};return path.includes('working-copy')?working:fresh;},
 fdAIDoneDialog:r=>done=r,applyFixes:()=>assert.fail('No save allowed')});
 vm.runInContext(part('  function fdUniqueAIProposals(', '  // Fix manually:'),ctx);
 await ctx.fdAIFixNow(scope);return {done,state,draws,loads,reads,text:ctx.fdResolutionText(state.fdAIRun)};
}
test('zero proposals refreshes and removes newly source-confirmed pattern from Needs a person without claiming a fix',async()=>{
 const r=await run('ok');assert.equal(r.done.fixed,0);assert.equal(r.done.resolved,0);assert.equal(r.done.informational,1);assert.equal(r.done.unfixed.length,0);assert.equal(r.done.skipped.length,0);assert.equal(r.reads,2);assert.equal(r.draws,1);assert.equal(r.loads,1);assert.equal(r.state.report.check_hash,'new');assert.match(r.text,/identifiers unchanged/);assert.match(r.text,/No selected findings were resolved by a saved correction/);
});
for(const mode of ['expired','incomplete','other-file','mismatch','superseded','read-failed'])test(`zero proposals keeps unresolved shape when ${mode}`,async()=>{const r=await run(mode);assert.equal(r.done.fixed,0);assert.equal(r.done.informational,0);assert.equal(r.done.unfixed.length,1);assert.equal(r.draws,0);});
test('zero-proposal fresh read cannot redirect a newer navigation or open a stale dialog',async()=>{const r=await run('navigation');assert.equal(r.done,undefined);assert.equal(r.state.report.file_hash,'other');assert.equal(r.draws,0);});

test('no-save informational banner does not ask a person to fix unchanged source-confirmed identifiers',()=>{
 const state={fdAIRun:{hash:'a',done:true,fixed:0,tried:1,informational:1,unfixed:[],skipped:[]},report:{file_hash:'a'},fdItems:[]};
 const ctx=vm.createContext({state,plural:(n,s)=>`${n} ${s}`,h:(tag,attrs,...children)=>({tag,children:children.flat(Infinity)}),drawSide(){},icon:()=>'',fdResolutionText:()=> '1 source-confirmed pattern hint informational; identifiers unchanged.'});
 vm.runInContext(part('  function fdAIRunBanner(', '  // ---------- the app fixes'),ctx);
 const text=x=>typeof x==='string'?x:(x?.children||[]).map(text).join(' ');
 const result=text(ctx.fdAIRunBanner(state.fdAIRun,0));assert.match(result,/identifiers unchanged/);assert.doesNotMatch(result,/Fix .*manually|Dex could not fix|not fixed:/);
});

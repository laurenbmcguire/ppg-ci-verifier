// Actual shipped functions; synthetic state and transports, no server or provider calls.
const test=require('node:test'),assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');
const source=fs.readFileSync('web/app.js','utf8'),between=(a,b)=>source.slice(source.indexOf(a),source.indexOf(b,source.indexOf(a)));
const helper=between('  function fdAIRequestFailure(', '  // Own the read-only proposal');
function harness({ready=true,error=null,empty=false,chunks=false}={}){
 let done,saves=0,calls=0;const report={file_hash:'a',check_hash:'c'},binding={file_hash:'a',expected_check_hash:'c'},p={id:'p',sheet:'S',cell:'A1',before:'old',after:'new',kind:'text'};
 const items=[...(ready?[{key:'ready',fix:p,state:'open'}]:[]),...Array.from({length:chunks?121:1},(_,i)=>({key:'ask'+i,sheet:'S',cell:'B'+(i+1),state:'open'}))];
 const state={report,aiOn:true},ctx=vm.createContext({state,reviewBinding:()=>binding,fdCanAI:()=>true,fdReadyFix:i=>i.fix,fdSourceGap:()=>false,document:{querySelectorAll:()=>[],body:{classList:{add(){}}}},drawSide(){},fdPrepareAI:async(s,b,v)=>{calls++;if(chunks&&calls===1)return v.success({proposals:[{...p,finding_id:'ask0'}],skipped:[]});if(!empty)v.failure(error)},captureCorrections:(a,d,e,b)=>({accepted:a,dec:d,edits:e,binding:b}),applyFixes:async()=>{saves++;return {file_hash:'b',revision:2,fixed_from:{file_hash:'a',changes:[p]}}},revisionOf:r=>r.revision||1,fdAIDoneDialog:r=>done=r,toast(){}});
 vm.runInContext(helper+between('  function fdUniqueAIProposals(', '  // Fix manually:'),ctx);
 return {run:async()=>{await ctx.fdAIFixNow(items);return {done,state,saves,calls}}};
}
for(const status of [401,403,409,503])for(const ready of [true,false])test(`HTTP ${status}, ready=${ready} preserves truthful category and save receipt`,async()=>{
 const e=Object.assign(new Error('The review changed. Reload before preparing suggestions.'),{httpStatus:status}),r=await harness({ready,error:e}).run();
 assert.equal(r.saves,ready?1:0);const failure=ready?r.done.skipped[0]:r.state.fdAIRun;
 assert.equal(failure.http_status,status);assert.doesNotMatch(failure.reason||failure.error,/Dex could not be reached|Try AI fix again/);
 if(ready){assert.equal(r.done.confirmed,1);assert.equal(r.done.hash,'b')}else assert.ok(r.state.fdAIRun.error);
});
for(const ready of [true,false])test(`unconfirmed empty response, ready=${ready}`,async()=>{const r=await harness({ready,empty:true}).run();const f=ready?r.done.skipped[0]:r.state.fdAIRun;assert.equal(f.category,'unconfirmed');assert.equal(r.saves,ready?1:0)});
test('successful earlier chunk survives later failure without pre-existing ready fix',async()=>{const r=await harness({ready:false,chunks:true,error:Object.assign(new Error('The review changed.'),{httpStatus:409})}).run();assert.equal(r.calls,2);assert.equal(r.saves,1);assert.equal(r.done.confirmed,1);assert.equal(r.done.skipped[0].http_status,409)});
for(const answer of [null,{}, {status:'done',file_hash:'a',check_hash:'c',ids:['ask'],proposals:[{finding_id:'wrong'}]}])test('actual prepare rejects malformed response as unverified, not outage',async()=>{
 let failure;const report={file_hash:'a'},binding={file_hash:'a',expected_check_hash:'c'},state={report,selected:'a',aiOn:true};
 const ctx=vm.createContext({state,reviewBinding:()=>binding,document:{querySelectorAll:()=>[]},api:async()=>({ai_proposals:answer})});
 vm.runInContext(helper+between('  async function fdPrepareAI(', '  // AI provides a read-only explanation'),ctx);
 await ctx.fdPrepareAI([{key:'ask',fp:'fp',state:'open'}],binding,{owner:{},active:()=>true,pending(){},failure:e=>failure=e,success(){throw Error('unexpected success')}});
 assert.equal(failure.aiCategory,'response');assert.equal(ctx.fdAIRequestFailure(failure).category,'response');
});
test('classification does not reflect arbitrary backend/provider content',()=>{const ctx=vm.createContext({});vm.runInContext(helper,ctx);const f=ctx.fdAIRequestFailure({httpStatus:503,message:'secret raw provider text'});assert.doesNotMatch(f.reason,/secret/);assert.equal(f.category,'server')});
test('unknown source evidence is not model editable; verified source fix remains eligible',()=>{const ctx=vm.createContext({state:{aiOn:true}});vm.runInContext(between('  const fdReadyFix =', '  function fdUniqueAIProposals('),ctx);assert.equal(vm.runInContext("fdCanAI({fp:'f',state:'open',issue:{source:'SharePoint'}})",ctx),false);assert.equal(vm.runInContext("fdCanAI({fp:'f',state:'open',issue:{code:'source_timezone_unconfirmed'}})",ctx),false);assert.equal(vm.runInContext("fdCanAI({fp:'f',state:'open',fix:{},selectable:true,evidence:'source',issue:{source:'SharePoint'}})",ctx),true)});

test('approved/sent/superseded and source reasons remain visible without claiming provider failure',()=>{const ctx=vm.createContext({});vm.runInContext(helper,ctx);for(const message of ['This version is approved, sent, or superseded. Suggestions cannot change it.','The source column mapping changed during capture. Compare again using the current mapping.']){const f=ctx.fdAIRequestFailure({httpStatus:409,message});assert.ok(f.reason.includes(message));assert.equal(f.category,message.startsWith('The source')?'source':'stale')}});
test('actual prepare reports AI-off as not started',async()=>{let failure;const ctx=vm.createContext({state:{aiOn:false}});vm.runInContext(between('  async function fdPrepareAI(', '  // AI provides a read-only explanation'),ctx);await ctx.fdPrepareAI([],{}, {failure:e=>failure=e});assert.equal(failure.aiCategory,'not_started')});

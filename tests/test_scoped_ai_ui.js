const test=require('node:test'),assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');const src=fs.readFileSync('web/app.js','utf8');
const between=(a,b)=>src.slice(src.indexOf(a),src.indexOf(b,src.indexOf(a)));
function harness(){const binding={file_hash:'file',expected_check_hash:'check',expected_rev:1,expected_run:{id:1}},report={binding},state={report,aiOn:true},controls=[{disabled:false,isConnected:true,dataset:{}},{disabled:true,isConnected:true,dataset:{}}],drafts=[],events=[],requests=[];let resolve,reject;
const ctx=vm.createContext({state,document:{querySelectorAll:q=>q.includes('default-never')?[]:q.includes('#side input:not')?drafts:controls},reviewBinding:r=>r.binding,toast:x=>events.push(x),api:(path,body)=>{requests.push({path,body});return new Promise((ok,bad)=>{resolve=ok;reject=bad;});}});
vm.runInContext(between('  async function fdPrepareAI(', '  // A review scope'),ctx);
const scope=[{key:'picked',fp:'fp',state:'open'}],view={owner:{},active:()=>true,pending:()=>events.push('pending'),success:r=>events.push(r),failure:e=>events.push(e.message)};
return{ctx,state,report,binding,controls,drafts,events,requests,scope,view,go:()=>ctx.fdPrepareAI(scope,binding,view),resolve:x=>resolve(x),reject:x=>reject(x),answer:()=>({ai_proposals:{status:'done',file_hash:'file',check_hash:'check',ids:['picked'],proposals:[],skipped:[]}})};}
test('AI sends exact selected fingerprints/run, locks controls and preserves prior disabled state',async()=>{const h=harness(),p=h.go();assert.equal(h.requests.length,1);assert.deepEqual(JSON.parse(JSON.stringify(h.requests[0].body)),{file_hash:'file',expected_check_hash:'check',expected_rev:1,run:{id:1},ids:['picked'],seen:{picked:'fp'}});assert.ok(h.controls.every(c=>c.disabled));const fresh={disabled:false,isConnected:true,dataset:{}};h.controls.push(fresh);h.state.aiProposalOperation.lock();assert.equal(fresh.disabled,true);h.resolve(h.answer());await p;assert.equal(h.state.aiProposalOperation,null);assert.equal(h.controls[0].disabled,false);assert.equal(h.controls[1].disabled,true);assert.equal(fresh.disabled,false);});
test('save/recheck/decision first blocks AI; typed unsent draft is not changed',async()=>{for(const key of ['busy','rechecking','decisionPending','aiProposalOperation']){const h=harness();h.state[key]={};await h.go();assert.equal(h.requests.length,0);}const h=harness();h.drafts.push({value:'unsent reason',defaultValue:''});await h.go();assert.equal(h.requests.length,0);assert.equal(h.drafts[0].value,'unsent reason');assert.match(h.events[0],/text is unchanged/);});
test('AI first blocks apply/recheck/decision without side effects',async()=>{const h=harness(),p=h.go();const accepted=[];h.ctx.correctionSnapshots=new WeakMap([[accepted,{binding:{expected_check_hash:'check'},accepted,dec:{},edits:{}}]]);vm.runInContext(between('  async function fdRecheck(', '  async function fdReconcile(')+between('  async function fdSendDecision(', '  function fdAdvanceAfter(')+between('  async function applyFixes(', '  // Take out extra copies'),h.ctx);await h.ctx.fdRecheck();await h.ctx.fdSendDecision({});await h.ctx.applyFixes(accepted,{},{});assert.equal(h.requests.length,1);assert.equal(h.state.busy,undefined);assert.equal(h.state.decisionPending,undefined);h.resolve(h.answer());await p;});
test('cancel/new operation cannot be unlocked by stale success or failure',async()=>{for(const error of [false,true]){const h=harness(),p=h.go(),old=h.state.aiProposalOperation;old.cancel();const fresh={lock:()=>{},cancel:()=>{throw Error('must not cancel successor');}};h.state.aiProposalOperation=fresh;if(error)h.reject(Error('old error'));else h.resolve(h.answer());await p;assert.equal(h.state.aiProposalOperation,fresh);assert.deepEqual(h.events,['pending']);}});
test('navigation or changed binding drops success/error and releases owned controls',async()=>{for(const error of [false,true]){const h=harness(),p=h.go();h.state.report={binding:{...h.binding,file_hash:'new'}};h.state.aiProposalOperation.lock();assert.equal(h.state.aiProposalOperation,null);if(error)h.reject(Error('old failure'));else h.resolve(h.answer());await p;assert.deepEqual(h.events,['pending']);assert.equal(h.controls[0].disabled,false);}const h=harness(),p=h.go();h.report.binding={...h.binding,expected_rev:2};h.resolve(h.answer());await p;assert.deepEqual(h.events,['pending']);});
test('malformed/out-of-scope response is a visible failure without proposals',async()=>{const h=harness(),p=h.go();const a=h.answer();a.ai_proposals.ids=['unselected'];h.resolve(a);await p;assert.match(h.events.at(-1),/selected scope/);assert.equal(h.state.aiProposalOperation,null);});

test('already changed navigation cannot start an old-report request',async()=>{const h=harness();h.state.selected='new-report';await h.go();assert.equal(h.requests.length,0);});

test('legacy AI entrypoints open the exact-selection chooser without provider requests',async()=>{
  const state={aiOn:true,report:{file_hash:'report',check_hash:'check'}},calls=[];
  const ctx=vm.createContext({state,fdReviewDialog:()=>calls.push('chooser'),toast:x=>calls.push(x),api:()=>{throw Error('Must not request provider');}});
  vm.runInContext(between('  async function runReview(', '  const restoredKey =')+between('  async function rerun(', '  // ---------------------------------------------------------------- upload'),ctx);
  await ctx.runReview('report');await ctx.runVerify('report');await ctx.runAiFixes(false,{file_hash:'report',expected_check_hash:'check'});
  for(const kind of ['ai-review','ai-verify','ai-fixes'])await ctx.rerun(kind,'');
  assert.deepEqual(calls,Array(6).fill('chooser'));
  await ctx.runReview('old');await ctx.runVerify('old');await ctx.runAiFixes(false,{file_hash:'old',expected_check_hash:'check'});
  assert.equal(calls.filter(x=>x==='chooser').length,6);
  assert.doesNotMatch(src,/api\(['"]\/api\/spreadsheets\/ai-(?:fix(?:es)?|review|verify)['"]/);
});
test('retained issue panel never accepts fixes or broadens selected scope',()=>{
  const state={report:{file_hash:'report'}},calls=[],sel=new Set(['picked','missing']);
  const ctx=vm.createContext({state,sel,issues:[{key:'picked'},{key:'other'},{key:'missing'}],reviewBinding:r=>r,fdItems:()=>[{key:'picked'},{key:'other'}],fdReviewDialog:()=>calls.push('chooser')});
  vm.runInContext(between('    const fixSelected = () => {', '    const draw = () => {')+'fixSelected();',ctx);
  assert.deepEqual(JSON.parse(JSON.stringify(state.fdReviewSelection)),{binding:'{"file_hash":"report"}',keys:['picked']});
  assert.deepEqual(calls,['chooser']);assert.deepEqual([...sel],['picked','missing']);
});

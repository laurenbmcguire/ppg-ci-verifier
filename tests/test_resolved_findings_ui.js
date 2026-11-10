const test=require('node:test'),assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');
const source=fs.readFileSync('web/app.js','utf8'),between=(a,b)=>source.slice(source.indexOf(a),source.indexOf(b,source.indexOf(a)));
const ctx=vm.createContext({plural:(n,s)=>`${n} ${s}${n===1?'':'s'}`});
vm.runInContext(between('  function fdSavedFindingResolution(', '  async function fdAIFixNow('),ctx);
function fixture(){
 const binding={file_hash:'parent',expected_check_hash:'parent-check',expected_run:{checker:'current'},expected_rev:0};
 const scope=[{key:'old-key',fp:'old-fp',binding,sheet:'Sheet',cell:'G17',row:17,state:'open',issue:{source:'Checks',code:'negative_count_or_quantity'}}];
 const accepted=[{sheet:'Sheet',cell:'G17',before:'-1',after:'-1'}];
 const savedItem={file_hash:'child',check_hash:'child-check',checker_version:'current',fixed_from:{file_hash:'parent',changes:structuredClone(accepted)}};
 const fresh={file_hash:'child',check_hash:'child-check',checker_version:'current',internal_check_status:'findings',checked_copy:{file_hash:'child',check_hash:'child-check',coverage_complete:true},
  review:{issues:[],dismissed:[],page:{complete:true,issues_total:0,dismissed_total:0},coverage:{truncated:false,not_listed:0,findings_total:0,findings_listed:0,all_findings:0}}};
 const working={status:'current',requested_hash:'child',working_hash:'child'};
 const f={scope,accepted,binding,savedItem,fresh,working};f.run=()=>ctx.fdSavedFindingResolution(f.scope,f.accepted,f.binding,f.savedItem,f.fresh,f.working);return f;
}
function addIssue(f,issue,list='issues'){f.fresh.review[list].push(issue);f.fresh.review.page[list+'_total']++;f.fresh.review.coverage.all_findings++;f.fresh.review.coverage.findings_total++;f.fresh.review.coverage.findings_listed++;}
test('exact saved correction and full current saved check resolve the selected occurrence',()=>{const f=fixture(),before=JSON.stringify(f),r=f.run();assert.equal(r.confirmed,1);assert.deepEqual(Array.from(r.resolved),['old-key']);assert.equal(r.remaining.length,0);assert.equal(JSON.stringify(f),before);});
for(const list of ['issues','dismissed'])test(`same rule and cell still ${list}, even when keys, fingerprints and observed values changed`,()=>{const f=fixture();addIssue(f,{key:'changed-key',fp:'changed-fp',source:'Checks',code:'negative_count_or_quantity',sheet:'Sheet',cell:'G17',dismissed_by:list==='dismissed'?'claude_verify':null,decision:list==='dismissed'?{action:'reject'}:null},list);const r=f.run();assert.equal(r.resolved.length,0);assert.equal(r.remaining[0].currentKey,'changed-key');assert.match(r.remaining[0].reason,/still detected/);});
for(const [name,change] of Object.entries({
 missing_refresh:f=>f.fresh=null,
 wrong_child:f=>f.fresh.file_hash='other',
 stale_check:f=>f.fresh.check_hash='later',
 old_checker:f=>f.fresh.checker_version='old',
 outdated:f=>f.fresh.internal_check_status='outdated_checks_require_recheck',
 incomplete_pages:f=>f.fresh.review.page.complete=false,
 truncated:f=>f.fresh.review.coverage.truncated=true,
 omitted:f=>f.fresh.review.coverage.not_listed=1,
 inventory_mismatch:f=>f.fresh.review.coverage.all_findings=4,
 missing_inventory:f=>f.fresh.review.coverage.findings_total=undefined,
 both_inventory_counts_missing:f=>{delete f.fresh.review.coverage.findings_total;delete f.fresh.review.coverage.findings_listed;},
 malformed_inventory:f=>f.fresh.review.issues={},
 missing_page_count:f=>delete f.fresh.review.page.issues_total,
 proof_incomplete:f=>f.fresh.checked_copy.coverage_complete=false,
 proof_other_check:f=>f.fresh.checked_copy.check_hash='stale',
 missing_working:f=>f.working=null,
 superseded:f=>f.working.status='superseded',
 working_other:f=>f.working.working_hash='other',
 locked:f=>f.working.read_only_reason='approved',
 stale_selection:f=>f.scope[0].binding={...f.binding,expected_check_hash:'old'},
}))test(`${name} retains pending findings, never rolls back the committed save`,()=>{const f=fixture();change(f);const r=f.run();assert.equal(r.confirmed,1);assert.equal(r.resolved.length,0);assert.match(r.remaining[0].reason,/resolution could not be verified/);});
for(const field of ['sheet','cell','before','after'])test(`mismatched saved ${field} never confirms or removes a finding`,()=>{const f=fixture();f.savedItem.fixed_from.changes[0][field]='different';const r=f.run();assert.equal(r.confirmed,0);assert.equal(r.resolved.length,0);});
test('unrelated saved parent and merely selected/drafted proposal never resolve',()=>{const f=fixture();f.savedItem.fixed_from.file_hash='other';f.scope[0].state='selected';assert.equal(f.run().resolved.length,0);f.savedItem.fixed_from.changes=[];assert.equal(f.run().confirmed,0);});
test('reused child is checked against the exact requested transformation, not its inherited history',()=>{const f=fixture();f.savedItem.fixed_from={file_hash:'unrelated',changes:[]};f.savedItem.save_outcome={type:'existing_version',requested_from:'parent',expected_check_hash:'parent-check',changes:f.accepted};assert.equal(f.run().resolved.length,1);f.savedItem.save_outcome.expected_check_hash='old';assert.equal(f.run().resolved.length,0);});
for(const source of ['SharePoint','AI review','Usual day'])test(`${source} absence without a current original-source check is not resolution`,()=>{const f=fixture();f.scope[0].issue.source=source;assert.equal(f.run().resolved.length,0);assert.match(f.run().remaining[0].reason,/original source/);});
test('current exact SharePoint comparison can establish source-finding resolution, but a stale comparison cannot',()=>{const f=fixture();f.scope[0].issue.source='SharePoint';f.fresh.review.sharepoint={source_verified:true};f.fresh.checked_copy.expires_at='2999-01-01T00:00:00Z';assert.equal(f.run().resolved.length,1);f.fresh.review.sharepoint.source_verified=false;assert.equal(f.run().resolved.length,0);});
test('row/report findings and unrelated proposals remain unresolved rather than guessing from a nearby change',()=>{const f=fixture();for(const cell of [null,'H17']){f.scope[0].cell=cell;assert.equal(f.run().resolved.length,0);}f.scope[0].cell='G17';f.scope[0].issue=null;assert.equal(f.run().resolved.length,0);assert.equal(f.run().appliedProposals.length,1);assert.equal(f.run().remaining.length,0);});
test('mixed selected scope resolves only the corrected rule and keeps other cells open',()=>{const f=fixture();f.scope.push({...f.scope[0],key:'unfixed',cell:'H17'});const r=f.run();assert.equal(r.resolved.length,1);assert.equal(r.remaining.length,1);assert.equal(r.remaining[0].item.key,'unfixed');});
test('dismissed and AI-set-aside original items are never counted as saved resolutions',()=>{const f=fixture();for(const state of ['kept','rejected','claude','kept_earlier']){f.scope[0].state=state;assert.equal(f.run().resolved.length,0);}});
test('download summary distinguishes saved changes from resolved findings and retains exact audit values',()=>{vm.runInContext(between('  function fdAISummaryText(', '  function fdAIDoneDialog('),ctx);const text=ctx.fdAISummaryText({fixed:2,confirmed:2,resolved:1,from:1,version:2,items:[{where:'Sheet G17',before:'-1',after:'-1'}],unfixed:[{where:'Sheet G17',text:'Negative quantity',reason:'Still detected'}]},{filename:'Synthetic.xlsx'});assert.match(text,/Saved cell changes: 2/);assert.match(text,/1 selected finding cleared/);assert.match(text,/SAVED  Sheet G17/);assert.match(text,/Still needs review: 1/);assert.doesNotMatch(text,/Fixed by Dex|FIXED  Sheet G17/);});
test('open panel omits saved-cell occurrence list; exact saved details remain in the completion/history views',()=>{const banner=between('  function fdAIRunBanner(', '  function fdAutoEligible(');assert.doesNotMatch(banner,/fd-fixed-list|h\('s'/);assert.match(banner,/View saved changes in History/);assert.ok(source.includes("plural(c.applied, 'saved cell change')"));assert.match(between('  function fdAIDoneDialog(', '  function fdAIRunBanner('),/fd-saved-changes/);});

test('standalone saved suggestion is applied without being miscounted as a resolved detected finding',()=>{const f=fixture();f.scope[0].issue=null;addIssue(f,{key:'other-rule',code:'negative_count_or_quantity',source:'Checks',sheet:'Sheet',cell:'G17'});const r=f.run();assert.equal(r.appliedProposals.length,1);assert.equal(r.resolved.length,0);assert.equal(r.remaining.length,0);assert.equal(f.fresh.review.issues.length,1);});

test('expired source evidence or source hold never clears a source finding',()=>{const f=fixture();f.scope[0].issue.source='SharePoint';f.fresh.review.sharepoint={source_verified:true};f.fresh.checked_copy.expires_at='2000-01-01T00:00:00Z';assert.equal(f.run().resolved.length,0);f.fresh.checked_copy.expires_at='2999-01-01T00:00:00Z';f.fresh.holds=[{hard:true,code:'source_coverage_incomplete'}];assert.equal(f.run().resolved.length,0);});

test('no-save summary cannot imply historical saved changes exist',()=>{assert.match(ctx.fdResolutionText({fixed:0}),/No selected findings were resolved/);assert.doesNotMatch(ctx.fdResolutionText({fixed:0}),/Saved changes are in History/);});

test('malformed committed receipt changes never throw or confirm a correction',()=>{const f=fixture();for(const changes of [{bad:true},[null]]){f.savedItem.fixed_from.changes=changes;assert.equal(f.run().confirmed,0);assert.equal(f.run().resolved.length,0);}});

for(const field of ['file_hash','check_hash','checker_version'])for(const value of [undefined,null,'', '  ',42,{}])test(`missing/malformed ${field} ${JSON.stringify(value)} cannot establish resolution`,()=>{const f=fixture();f.fresh[field]=value;f.savedItem[field]=value;assert.equal(f.run().resolved.length,0);assert.equal(f.run().complete,false);});
test('only completed scoped editors are synchronously removed before saved-copy navigation',()=>{assert.ok(source.includes('finished = true; d.close(); d.remove(); c.onApplied?.();'));assert.equal((source.match(/onApplied: \(\) => \{ dlg.close\(\); dlg.remove\(\); \}/g)||[]).length,2);assert.match(between('  function navigationDrafts(', '  function versionReadOnly('),/unsavedForm.*window.confirm/);});

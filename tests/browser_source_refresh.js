// Synthetic HTTP-only actual comparison/Sources and refresh handlers. Every request intercepted.
const assert=require('node:assert/strict'),fs=require('node:fs');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright-core');
const source=fs.readFileSync('web/app.js','utf8'),part=(a,b)=>source.slice(source.indexOf(a),source.indexOf(b,source.indexOf(a)));
const report={file_hash:'a'.repeat(64),check_hash:'check',checker_version:'synthetic',assignment:{status:'verified',site_id:'site',list_id:'list',label:'Fabricated source'},version_view:{status:'current'},review:{sharepoint:{status:'incomplete'},issues:[]},source_reconciliation:{snapshot_id:'old',bound_to:{file_hash:'a'.repeat(64),checker_version:'synthetic'},coverage:{complete:false},rows:[{status:'source_day_unconfirmed',item:'123',sheet:'Inspection Detail',row:4}]},original_bytes:'FABRICATED_IMMUTABLE',version_history:['original','working']};
const code=part('  const h =','  const store =')+part('  function versionReadOnly(', '  function versionLinks(')+part('  function reviewBinding(', '  function findingBinding(')+part('  function fdSourceRefreshReason(', '  // ---------- history and sources')+part('  function fdSourceVerified(', '  function drawReport(')+part('  function fdSourceRecovery(', '  function drawSide(');
const coverageStart=source.indexOf('    const cov = r.review.coverage || {}, dc = r.review.decisions || {};',source.indexOf('  function fdCorrections('));
const coverageEnd=source.indexOf("    body.append(h('p', {class: 'fd-countline'",coverageStart);
const coverageCode=source.slice(coverageStart,coverageEnd);
const boot=`${code}
function exerciseCoverage(r) { const body={append(){}}; ${coverageCode} }
const fdRecheck=()=>{throw Error('Source-only action launched workbook recheck');};
const state={report:${JSON.stringify(report)},selected:'${report.file_hash}',health:{sharepoint_configured:true},fdView:'review'};
const $=id=>document.getElementById(id), icon=()=>h('span',{},'•'),when2=x=>x||'',allIssues=r=>r.issues||[],readBlock=()=>null,gridDirty=()=>!!state.dirty;
const toast=t=>$('toast').textContent=t, fillPages=async r=>r,loadFixes=()=>{},loadList=()=>{};
const api=async(path,body)=>{const response=await fetch(path,body?{method:'POST',body:JSON.stringify(body)}:{});const data=await response.json();if(!response.ok)throw Error(data.error||'Synthetic transport error');return data;};
const drawSide=()=>{exerciseCoverage(state.report);$('side').replaceChildren(state.fdView==='sources'?fdSources(state.report):fdComparison(state.report));state.rechecking?.lock?.();},drawReport=drawSide;
window.test={state,drawSide,exerciseCoverage,reset:()=>{state.report=${JSON.stringify(report)};state.selected=state.report.file_hash;state.fdAutoSPError=null;state.fdView='review';drawSide();}};drawSide();`;
(async()=>{
const browser=await chromium.launch({headless:true,executablePath:process.env.CHROMIUM_EXECUTABLE});
try{for(const width of [1200,390]){
const context=await browser.newContext({viewport:{width,height:1000}}),page=await context.newPage(),requests=[],errors=[],pending=[];let mode='normal',working='current';
page.on('pageerror',e=>errors.push(String(e)));
await context.route('**/*',route=>{
 const request=route.request(),url=new URL(request.url());assert.equal(url.origin,'https://synthetic.invalid');
 if(url.pathname==='/')return route.fulfill({contentType:'text/html',body:'<!doctype html><meta name="viewport" content="width=device-width"><style>'+fs.readFileSync('web/app.css','utf8')+'</style><main style="max-width:100%;padding:16px"><div id="side"></div><p id="toast"></p></main>'});
 if(url.pathname.startsWith('/assets/fonts/'))return route.fulfill({contentType:'font/woff2',body:fs.readFileSync('web'+url.pathname)});
 requests.push({path:url.pathname,method:request.method(),body:request.postDataJSON()});
 if(url.pathname==='/api/spreadsheets/working-copy')return route.fulfill({contentType:'application/json',body:JSON.stringify({requested_hash:report.file_hash,status:working})});
 if(url.pathname==='/api/spreadsheets/sharepoint'){pending.push(route);return;}
 if(url.pathname==='/api/spreadsheet')return route.fulfill({contentType:'application/json',body:JSON.stringify({...report,source_reconciliation:{...report.source_reconciliation,snapshot_id:'fresh'},review:{...report.review,coverage:{truncated:true,not_listed:20},sharepoint:{status:mode==='unavailable'?'error':'incomplete',message:mode==='unavailable'?'Synthetic source unavailable':'Business day remains unconfirmed'}}})});
 throw Error('Unexpected endpoint: '+url.pathname);
});
await page.clock.install();await page.goto('https://synthetic.invalid/');await page.addScriptTag({content:boot});
const posts=()=>requests.filter(x=>x.method==='POST'), until=async f=>{for(let i=0;i<150&&!f();i++)await new Promise(r=>setTimeout(r,10));assert.ok(f());};
const button=()=>page.locator('.fd-source-refresh-button').first(), expand=async()=>page.locator('.fd-comparison').first().evaluate(n=>n.open=true);
const finish=async kind=>{await until(()=>pending.length);const r=pending.shift();if(kind==='transport')await r.abort();else await r.fulfill({contentType:'application/json',body:JSON.stringify(kind==='pending'?{pending:true}:{item:report})});await page.waitForFunction(()=>!test.state.fdReconciling);};
await expand();assert.equal(await button().isVisible(),true);assert.match(await page.locator('#side').innerText(),/Refresh SharePoint only/);assert.equal(posts().length,0);
for(const flag of ['busy','rechecking','decisionPending','aiProposalOperation']){await page.evaluate(flag=>{test.state[flag]=true;document.querySelector('.fd-source-refresh-button').onclick();test.state[flag]=false;},flag);assert.equal(posts().length,0);}
await page.evaluate(()=>{test.state.gridDraft={};test.state.dirty=true;document.querySelector('.fd-source-refresh-button').onclick();test.state.gridDraft=null;test.state.dirty=false;});assert.equal(posts().length,0);
await page.evaluate(()=>{const input=document.createElement('textarea');input.id='draft';input.value='keep my draft';document.querySelector('#side').append(input);});await button().click();assert.equal(posts().length,0);assert.equal(await page.locator('#draft').inputValue(),'keep my draft');await page.locator('#draft').evaluate(n=>n.remove());
await page.evaluate(()=>{const old=document.querySelector('.fd-source-refresh-button').onclick;test.state.report.check_hash='changed';old();test.state.report.check_hash='check';});assert.equal(posts().length,0);
await page.evaluate(()=>{test.state.report.review.coverage={truncated:true,not_listed:20};test.exerciseCoverage(test.state.report);document.querySelector('.fd-source-refresh-button').onclick();});await until(()=>posts().length===1);await page.evaluate(()=>{document.querySelector('.fd-source-refresh-button').onclick();test.drawSide();document.querySelector('.fd-source-refresh-button').onclick();});assert.equal(posts().length,1);assert.equal(await button().isDisabled(),true);await finish();await page.clock.fastForward(61000);await page.evaluate(()=>test.drawSide());await page.clock.fastForward(61000);
assert.deepEqual(posts()[0].body,{file_hash:report.file_hash});assert.equal(await page.evaluate(()=>test.state.report.original_bytes),report.original_bytes);assert.deepEqual(await page.evaluate(()=>test.state.report.version_history),report.version_history);assert.match(await page.locator('#toast').innerText(),/receipt loaded/);
await expand();await page.screenshot({path:`../source-refresh-comparison-${width}.png`});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
// Sources uses exactly the same source-only action, including expired/incomplete receipts.
await page.evaluate(()=>{test.state.fdView='sources';test.state.report.source_reconciliation.stale=true;test.drawSide();});await expand();assert.equal(await button().isVisible(),true);await button().click();await until(()=>posts().length===2);await finish('pending');assert.match(await page.locator('#toast').innerText(),/unconfirmed.*no retry/i);assert.equal(posts().length,2);
await page.evaluate(()=>test.reset());await expand();await button().click();await until(()=>posts().length===3);await finish('transport');assert.match(await page.locator('#toast').innerText(),/unconfirmed/);assert.equal(posts().length,3);
await page.evaluate(()=>test.reset());await expand();mode='unavailable';await button().click();await until(()=>posts().length===4);await finish();assert.equal(await page.evaluate(()=>test.state.report.review.sharepoint.status),'error');assert.doesNotMatch(await page.locator('#toast').innerText(),/Matches SharePoint|success|verified/i);
// Navigation while the source POST is outstanding never opens or overwrites another report.
await page.evaluate(()=>test.reset());await expand();await button().click();await until(()=>posts().length===5);await page.evaluate(()=>{test.state.report={...test.state.report,file_hash:'b'.repeat(64)};test.state.selected=test.state.report.file_hash;test.drawSide();});const reads=requests.filter(x=>x.path==='/api/spreadsheet').length;await finish();assert.equal(await page.evaluate(()=>test.state.report.file_hash),'b'.repeat(64));assert.equal(requests.filter(x=>x.path==='/api/spreadsheet').length,reads);
await page.evaluate(()=>test.reset());working='superseded';await expand();await button().click();await page.waitForFunction(()=>!test.state.fdReconciling);assert.equal(posts().length,5);assert.match(await page.locator('#toast').innerText(),/no longer the current/);working='current';
for(const scenario of ['readonly','assignment','configuration']){await page.evaluate(s=>{test.reset();if(s==='readonly')test.state.report.version_view.historical=true;if(s==='assignment')delete test.state.report.assignment.list_id;if(s==='configuration')test.state.health.sharepoint_configured=false;test.drawSide();},scenario);assert.equal(await button().isDisabled(),true);await button().evaluate(n=>n.onclick());assert.equal(posts().length,5);}
await expand();await page.screenshot({path:`../source-refresh-unavailable-${width}.png`});assert.deepEqual(errors,[]);assert.equal(posts().filter(x=>x.path!='/api/spreadsheets/sharepoint').length,0);
console.log(JSON.stringify({passed:true,width,sourcePosts:5,postsPerAction:1,AI:0,workbookRecheck:0,apply:0,originalBytesPreserved:true,historyPreserved:true,guards:true,staleBinding:true,pending:true,transport:true,navigation:true,superseded:true,unavailable:true}));await context.close();
}}finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});

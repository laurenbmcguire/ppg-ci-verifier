// Isolated actual-handler DOM regression: fabricated reports, every request intercepted.
// No server, user browser, workbook, credentials, database or provider is used.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright-core');
const source = fs.readFileSync('web/app.js', 'utf8');
const between = (a,b) => source.slice(source.indexOf(a),source.indexOf(b,source.indexOf(a)));
const handlers = between('  const h =', '  const store =') + between('  function fdReportSummary(', '  // A rejected application') + between('  function fdSummaryInventory(', '  function fdManualCellHeld(');
const health = between("  api('/api/health')", '  // ---------------------------------------------------------------- client tabs');
const boot = `
${handlers}
const report = {file_hash:'synthetic',check_hash:'check',revision:2,review:{issues:[{key:'a',severity:'medium'}],dismissed:[],page:{complete:true}},holds:[]};
const item = {key:'a',state:'open',title:'Missing identifier',text:'Missing identifier'};
const state = {report,selected:'synthetic',aiOn:false,fixes:[],fdItems:[item]};
const reviewBinding = r => ({file_hash:r.file_hash,expected_check_hash:r.check_hash,expected_rev:r.revision,expected_run:{checker:'synthetic'}});
const fdItems = () => state.fdItems, fdRecommended = () => [], fdCanAI = () => false;
const fdGroups = items => [{items,title:'Missing identifier'}];
const plural = (n,s) => n+' '+s+(n===1?'':'s');
const allIssues = r => r.issues;
const toast = text => document.querySelector('#toast').textContent = text;
const $ = id => document.getElementById(id), drawChat = () => {};
const api = async (path,body) => { const response = await fetch(path,body ? {method:'POST',body:JSON.stringify(body)} : {}); const data = await response.json(); if(!response.ok)throw Error(data.error); return data; };
const redraw = () => { $('host').replaceChildren(fdReportSummary(state.report)); state.aiProposalOperation?.lock?.(); };
window.test = {state,report,redraw,reviewBinding, reset:()=>{state.aiProposalOperation?.cancel(); state.report=report; state.selected=report.file_hash;state.fdSummaryCache=null;redraw();}};
redraw();
${health}
`;
(async () => {
 const browser = await chromium.launch({headless:true,executablePath:process.env.CHROMIUM_EXECUTABLE});
 try { for (const width of [1200,390]) {
  const context = await browser.newContext({viewport:{width,height:900}}), page = await (async()=>{await context.addInitScript(()=>{try{localStorage.setItem('rd-auto-tidy','false')}catch(e){}});return context.newPage()})();
  const requests=[],pending=[],errors=[]; let healthRoute;
  page.on('pageerror',e=>errors.push(String(e)));
  await page.clock.install();
  await context.route('**/*',route=>{
   const request=route.request(),url=new URL(request.url());
   if(url.origin!=='https://synthetic.invalid') throw Error('Unexpected external request: '+url.origin);
   if(url.pathname==='/')return route.fulfill({contentType:'text/html',body:'<!doctype html><meta name="viewport" content="width=device-width"><style>'+fs.readFileSync('web/app.css','utf8')+'</style><div id="connections"></div><div id="side"><div id="host"></div></div><div id="toast"></div>'});
   requests.push({path:url.pathname,body:request.postDataJSON()});
   if(url.pathname==='/api/health'){healthRoute=route;return;}
   if(url.pathname==='/api/spreadsheets/review-summary'){pending.push(route);return;}
   throw Error('Unexpected request: '+url.pathname);
  });
  await page.goto('https://synthetic.invalid/'); await page.addScriptTag({content:boot});
  const panel=page.locator('.fd-report-summary'),button=panel.locator('.fd-summary-request'), count=()=>requests.filter(r=>r.path.endsWith('/review-summary')).length;
  const until=async f=>{for(let i=0;i<100&&!f();i++)await new Promise(r=>setTimeout(r,10));assert.ok(f());};
  const answer = () => ({counts:{available:1,active:1,dismissed:0},groups:[{id:'g',ids:['a'],count:1,code:'missing_identifier',source:'Check',severity:'medium',state:'open'}],read_only:true,file_hash:'synthetic',check_hash:'check',expected_rev:2,run:{checker:'synthetic'},reply:'Requested synthetic AI explanation.',notice:'No changes made.',coverage:{label:'All available findings included.'},narrative_notice:'Synthetic narrative.',narrative_status:'done'});
  const finish=async (data=answer(),status=200)=>{await until(()=>pending.length>0);await pending.shift().fulfill({status,contentType:'application/json',body:JSON.stringify(data)});await page.waitForFunction(()=>!test.state.aiProposalOperation);};
  assert.match(await panel.innerText(),/loaded findings contain 1 possible error/);
  assert.doesNotMatch(await panel.innerText(),/Dex is writing/);assert.equal(await button.isDisabled(),true);
  await until(()=>healthRoute);await healthRoute.fulfill({contentType:'application/json',body:JSON.stringify({ai_review_configured:true})});
  await page.waitForFunction(()=>!document.querySelector('.fd-summary-request').disabled);
  await page.clock.fastForward(21000);assert.equal(count(),0);
  await panel.locator('summary').first().click();await panel.locator('summary').first().click();
  await page.evaluate(()=>test.redraw());await page.clock.fastForward(21000);assert.equal(count(),0);
  assert.equal(await button.isVisible(),true);assert.match(await panel.innerText(),/Only when you choose Generate AI summary.*Anthropic.*bounded aggregate finding-group metadata.*not workbook contents/);
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
  await page.screenshot({path:`../explicit-summary-idle-${width}.png`});
  // Typed drafts and operation guards are checked by the actual click handler.
  await page.evaluate(()=>{const input=document.createElement('textarea');input.id='draft';input.value='unsaved';document.querySelector('#side').append(input);});
  await button.click();assert.equal(count(),0);assert.equal(await page.locator('#draft').inputValue(),'unsaved');await page.locator('#draft').evaluate(n=>n.remove());
  for(const flag of ['busy','rechecking','decisionPending']){await page.evaluate(flag=>{test.state[flag]=true;document.querySelector('.fd-summary-request').click();test.state[flag]=false;},flag);assert.equal(count(),0);}
  await button.click();await until(()=>count()===1);assert.equal(await button.isDisabled(),true);
  await button.evaluate(n=>{n.onclick();n.onclick();});assert.equal(count(),1);
  assert.deepEqual(requests.find(r=>r.path.endsWith('/review-summary')).body,{scope:'available_findings',file_hash:'synthetic',expected_check_hash:'check',expected_rev:2,run:{checker:'synthetic'}});
  await panel.locator('summary').first().click();await panel.locator('summary').first().click();await page.evaluate(()=>test.redraw());assert.equal(await button.isDisabled(),true);assert.match(await panel.innerText(),/Summarizing the available findings/);await button.evaluate(n=>n.onclick());assert.equal(count(),1);
  await finish();assert.match(await panel.innerText(),/Requested synthetic AI explanation/);assert.equal(await button.isDisabled(),false);
  await page.evaluate(()=>test.redraw());assert.match(await panel.innerText(),/Requested synthetic AI explanation/);assert.equal(count(),1);
  // Explicit regeneration is still allowed; redraw failures remain visible and unlocked.
  await button.click();await until(()=>count()===2);await page.evaluate(()=>test.redraw());await finish({error:'Synthetic summary unavailable'},503);
  assert.match(await panel.innerText(),/Summary unavailable: Synthetic summary unavailable/);assert.equal(await button.isDisabled(),false);
  // Source-context changes invalidate cache and responses without another request.
  await page.evaluate(()=>{test.report.source_reconciliation={snapshot_id:'changed'};test.redraw();});
  assert.doesNotMatch(await panel.innerText(),/Requested synthetic AI explanation/);assert.equal(count(),2);
  await button.click();await until(()=>count()===3);await page.evaluate(()=>{test.report.source_reconciliation={snapshot_id:'newer'};test.redraw();});await finish();
  assert.doesNotMatch(await panel.innerText(),/Requested synthetic AI explanation/);
  // New report object/navigation cannot acquire an old response or auto-start.
  await button.click();await until(()=>count()===4);await page.evaluate(()=>{test.state.report={...test.report,file_hash:'other'};test.redraw();});await finish();
  assert.doesNotMatch(await panel.innerText(),/Requested synthetic AI explanation/);assert.equal(count(),4);
  await page.evaluate(()=>{test.reset();test.state.fdItems=[];test.report.review.page.complete=false;test.redraw();});
  assert.match(await panel.innerText(),/Coverage incomplete/);assert.match(await panel.innerText(),/does not establish source completeness/);assert.doesNotMatch(await panel.innerText(),/No errors left/);
  await page.clock.fastForward(21000);assert.equal(count(),4);
  await page.screenshot({path:`../explicit-summary-incomplete-${width}.png`});
  assert.deepEqual(errors,[]);assert.ok(requests.every(r=>['/api/health','/api/spreadsheets/review-summary'].includes(r.path)));
  console.log(JSON.stringify({passed:true,width,passive_requests:0,explicit_requests:4,late_health:true,drafts:true,double_click:true,pending_redraw:true,cache:true,redraw_failure:true,source_stale:true,navigation_stale:true,incomplete:true,external_requests:0}));
  await context.close();
 }} finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});

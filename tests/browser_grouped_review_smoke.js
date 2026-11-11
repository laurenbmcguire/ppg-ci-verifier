// Standalone synthetic render of production grouping code, strict CSP, loopback only.
const fs=require('node:fs'),http=require('node:http'),assert=require('node:assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright-core');
const source=fs.readFileSync('web/app.js','utf8'),css=fs.readFileSync('web/app.css');
const between=(a,b)=>source.slice(source.indexOf(a),source.indexOf(b,source.indexOf(a)));
const fixture=JSON.parse(fs.readFileSync('tests/fixtures/grouped_review.json','utf8'));
const js=between('  const h = (','  const store =')+`
const fmt=v=>v==null?'—':String(v);const hm12=x=>x;const toast=()=>{};
const plural=(n,s)=>n+' '+s+(n===1?'':'s');const icon=()=>document.createElement('span');const showWs=v=>v==null||v===''?'(blank)':String(v);
`+between('  function fdHistoryGroups(', '  function fdNextStep(')+between('  function fdSourceVerified(', '  function drawReport(')+between('  function reviewBinding(', '  function findingBinding(')+between('  function versionReadOnly(', '  function versionLinks(')+between('  function fdSourceRefreshReason(', '  async function fdReconcile(')+between('  function fdSourceRecovery(', '  function fdSources(')+between('  function nexusCopyAllowed(', '  const hm12 =')+`
const fixture=${JSON.stringify(fixture)};const state={report:fixture};document.querySelector('main').append(fdAutoSummary(fixture),fdComparison(fixture));
const nexus={status:'held',coverage:{complete:false,source_records:2,placed_records:1},unplaced:1,shifts:[{name:'First',start:'06:00',end:'14:00',scans:1,inspected:null,rejected:0,certified:0}],total:{scans:2,inspected:null,rejected:0,certified:0}};
const nexusSection=document.createElement('section');nexusSection.id='test-nexus';nexusSection.append(nexusTable(nexus));document.querySelector('main').append(nexusSection);`;
(async()=>{
 const requests=[],server=http.createServer((req,res)=>{
  requests.push(req.url);res.setHeader('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; font-src 'none'; connect-src 'none'");
  const files={'/':['text/html','<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/app.css"></head><body><main></main><script src="/fixture.js"></script></body></html>'],'/app.css':['text/css',css],'/fixture.js':['text/javascript',js]};
  const file=files[req.url];if(!file){res.writeHead(404);return res.end();}res.setHeader('Content-Type',file[0]+'; charset=utf-8');res.end(file[1]);
 });await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
 const base=`http://127.0.0.1:${server.address().port}`,browser=await chromium.launch({executablePath:process.env.CHROMIUM_EXECUTABLE||undefined,headless:true});
 const results=[];
 try{for(const width of [1200,390]){
  const context=await browser.newContext({viewport:{width,height:850}}),page=await (async()=>{await context.addInitScript(()=>{try{localStorage.setItem('rd-auto-tidy','false')}catch(e){}});return context.newPage()})(),errors=[],external=[];
  await context.route('**/*',route=>{if(route.request().url().startsWith(base+'/'))return route.continue();external.push(route.request().url());return route.abort();});
  page.on('pageerror',e=>errors.push(String(e)));await page.goto(base,{waitUntil:'networkidle'});
  assert.equal(await page.locator('.fd-history-group').count(),3);
  assert.equal(await page.locator('[data-history-id]').count(),27,'only current pages render, all totals remain visible');
  assert.equal(await page.locator('[data-history-id]:visible').count(),0,'all groups collapsed initially');
  const seen=new Set();
  for(const group of await page.locator('.fd-history-group').all()){
   const summary=group.locator('summary');await summary.focus();await page.keyboard.press('Enter');
   assert.equal(await group.locator('.fd-history-notice').count(),1,'common note appears once');
   assert.equal(await group.getByRole('button',{name:'Previous',exact:true}).isDisabled(),true);
   let pageIndex=0;
   while(true){
    const ids=await group.locator('[data-history-id]').evaluateAll(nodes=>nodes.map(n=>n.dataset.historyId));
    assert.ok(ids.length<=25);ids.forEach(id=>seen.add(id));
    const range=await group.locator('.fd-history-range').innerText();
    assert.ok(range.startsWith(`Occurrences ${pageIndex*25+1}–`));
    assert.ok(await group.locator('.fd-history-page').evaluate(el=>el.getBoundingClientRect().height<=innerHeight*.55+1));
    if(await group.getByRole('button',{name:'Next',exact:true}).isDisabled())break;
    await group.getByRole('button',{name:'Next',exact:true}).click();pageIndex++;
    assert.equal(await group.locator('.fd-history-range').evaluate(el=>el===document.activeElement),true,'page change focuses range');
   }
   if(pageIndex){
    await group.locator('[data-history-id="changed:299"]').scrollIntoViewIfNeeded();
    assert.match(await group.locator('[data-history-id="changed:299"]').innerText(),/I300/);
    await group.getByRole('button',{name:'Previous',exact:true}).click();
    assert.match(await group.locator('.fd-history-range').innerText(),/Occurrences 251–275 of 300/);
    await group.getByRole('button',{name:'Next',exact:true}).click();
   }
   const lastRange=await group.locator('.fd-history-range').innerText();
   await group.getByRole('button',{name:'Collapse',exact:true}).click();
   assert.equal(await group.evaluate(el=>el.open),false);assert.equal(await summary.evaluate(el=>el===document.activeElement),true);
   await page.keyboard.press('Enter');assert.equal(await group.locator('.fd-history-range').innerText(),lastRange,'reopen retains page');
   await group.getByRole('button',{name:'Collapse',exact:true}).click();
  }
  assert.equal(seen.size,302,'every original identity reachable exactly across pages');
  const ai=page.locator('[data-history-status="recommendation"]');assert.match(await ai.innerText(),/not human decisions/);
  await ai.locator('summary').click();assert.match(await ai.innerText(),/does not resolve a source hold/);
  await page.locator('.fd-comparison > summary').click();
  await page.locator('.fd-comparison').getByText('Technical details',{exact:true}).click();
  const comparisonText=await page.locator('.fd-comparison').innerText();
  assert.match(comparisonText,/Report: Unknown/);assert.match(comparisonText,/Report: 0/);assert.match(comparisonText,/9007199254740993.25/);
  assert.match(comparisonText,/verification is unavailable/);assert.match(comparisonText,/Synthetic missing source page/);
  await page.getByText('Duplicate report references · 1',{exact:true}).click();
  await page.getByText('Source records outside the confirmed business date · 1',{exact:true}).click();
  assert.equal(await page.locator('[data-comparison-id]:visible').count(),2);
  assert.equal(await page.getByRole('button',{name:'Copy captured Nexus totals',exact:true}).isDisabled(),true);
  assert.match(await page.locator('#test-nexus').innerText(),/Nexus totals held/);
  assert.match(await page.locator('#test-nexus').innerText(),/Placed records: 1 of 2/);
  assert.ok((await page.locator('#test-nexus').innerText()).includes('—'));
  assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'no page-level overflow');
  assert.deepEqual(errors,[]);assert.deepEqual(external,[]);results.push({width,groups:3,occurrences:302,keyboard_expand_collapse:true,last_occurrence_visible:true});await context.close();
 }}finally{await browser.close();await new Promise(resolve=>server.close(resolve));}
 console.log(JSON.stringify({passed:true,results,requests}));
})().catch(e=>{console.error(e);process.exitCode=1;});

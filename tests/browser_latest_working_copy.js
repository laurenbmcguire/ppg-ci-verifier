const assert=require('node:assert/strict'),crypto=require('node:crypto');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright-core');
(async()=>{
 const [base,raw,w]=process.argv.slice(2),c=JSON.parse(raw),width=Number(w);
 const browser=await chromium.launch({headless:true,executablePath:process.env.CHROMIUM_EXECUTABLE});
 const context=await browser.newContext({viewport:{width,height:900}}),page=await (async()=>{await context.addInitScript(()=>{try{localStorage.setItem('rd-auto-tidy','false')}catch(e){}});return context.newPage()})(),errors=[],external=[],writes=[];
 await context.route('**/*',r=>{if(!r.request().url().startsWith(base+'/')){external.push(r.request().url());return r.abort();}return r.continue();});
 page.on('pageerror',e=>errors.push(String(e)));page.on('request',r=>{if(/apply-fixes|ai-proposals|decide|recheck|source-reconcile/.test(r.url()))writes.push(r.url());});
 const open=async hash=>{await page.goto(base+'/app#r='+hash,{waitUntil:'networkidle'});};
 const readonly=async()=>{await page.locator('.version-readonly').waitFor();assert.equal(await page.locator('#fd-open-review,#fd-fix-everything,#fd-preview-btn,#grid-edit-cell,.fd-mode-selector').count(),0);const download=page.getByRole('link',{name:'Download this original version',exact:true});await download.scrollIntoViewIfNeeded();assert.equal(await download.isVisible(),true);assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'Readonly page must fit viewport');};
 try{
  await open(c.a);await page.locator('.fd-head').waitFor();assert.equal(new URL(page.url()).hash,'#r='+c.c);
  assert.match(await page.locator('#sheet-block').innerText(),/third/);
  for(const hash of [c.a,c.b]){
   await open(hash+'&history=1');await readonly();await page.locator('.readonly-sheet table').waitFor();
   assert.equal(await page.getByRole('link',{name:'Download this original version',exact:true}).getAttribute('href'),'/api/spreadsheets/download?id='+hash);
   const response=await context.request.get(base+'/api/spreadsheets/download?id='+hash);assert.equal(response.status(),200);assert.equal(crypto.createHash('sha256').update(await response.body()).digest('hex'),c.sha[hash]);
   await page.getByRole('link',{name:'Return to working copy',exact:true}).click();await page.waitForURL('**#r='+c.c);await page.locator('.fd-head').waitFor();
   await page.goBack();await readonly();assert.equal(new URL(page.url()).hash,'#r='+hash+'&history=1');
   await page.goForward();await page.locator('.fd-head').waitFor();assert.equal(new URL(page.url()).hash,'#r='+c.c);
  }
  await open(c.root);await page.locator('.version-choice').waitFor();assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'Branch choice must fit viewport');assert.equal(await page.locator('.version-choice .btn').count(),2);assert.equal(new URL(page.url()).hash,'#r='+c.root);
  await page.locator('.version-choice a[href="#r='+c.left+'"]').click();await page.locator('.fd-head').waitFor();assert.equal(new URL(page.url()).hash,'#r='+c.left);
  for(const [reason,hash] of Object.entries(c.locked)){await open(hash);await readonly();assert.match(await page.locator('.version-readonly').innerText(),new RegExp(reason));}
  await open(c.broken);await readonly();assert.match(await page.locator('.version-readonly').innerText(),/inconsistent saved version history/i);
  await open('f'.repeat(64));await page.locator('#report [role="alert"]').waitFor();assert.equal(await page.locator('.fd-head,.version-readonly,.version-choice').count(),0);
  // Resolver failure must not fall back to an editable old report.
  await page.route('**/api/spreadsheets/working-copy?id='+c.a,r=>r.fulfill({status:503,contentType:'application/json',body:'{"error":"Synthetic resolver unavailable"}'}));
  await open(c.a);await page.getByText('Synthetic resolver unavailable',{exact:true}).waitFor();assert.equal(await page.locator('.fd-head,.version-readonly').count(),0);await page.unroute('**/api/spreadsheets/working-copy?id='+c.a);
  await page.getByRole('button',{name:'Retry opening report',exact:true}).click();await page.locator('.fd-head').waitFor();assert.equal(new URL(page.url()).hash,'#r='+c.c);
  // Late completion from an old route never replaces the newly selected branch.
  let release;const held=new Promise(resolve=>release=resolve);
  await page.route('**/api/spreadsheets/working-copy?id='+c.a,async r=>{await held;await r.continue();});
  await page.evaluate(hash=>{location.hash='r='+hash},c.a);await page.waitForTimeout(100);
  await page.evaluate(hash=>{location.hash='r='+hash},c.right);await page.locator('.fd-head').waitFor();release();await page.waitForTimeout(200);
  assert.equal(new URL(page.url()).hash,'#r='+c.right);assert.match(await page.locator('#sheet-block').innerText(),/right branch/);
  await page.unroute('**/api/spreadsheets/working-copy?id='+c.a);
  // Dirty cell draft: cancellation preserves text, explicit discard navigates without writes.
  await open(c.dirty);await page.locator('#sheet-block td[data-cell="I2"]').click();await page.locator('#grid-edit-cell').click();await page.locator('#grid-cell-input').fill('Unsaved synthetic value');
  page.once('dialog',d=>d.dismiss());await page.evaluate(hash=>{location.hash='r='+hash+'&history=1'},c.a);await page.waitForTimeout(100);
  assert.equal(await page.locator('#grid-cell-input').inputValue(),'Unsaved synthetic value');assert.equal(new URL(page.url()).hash,'#r='+c.dirty);
  page.once('dialog',d=>d.accept());await page.evaluate(hash=>{location.hash='r='+hash+'&history=1'},c.a);await readonly();assert.equal(await page.locator('#grid-cell-input').count(),0);
  assert.deepEqual(errors,[]);assert.deepEqual(external,[]);assert.deepEqual(writes,[]);
  console.log(JSON.stringify({passed:true,width,unique_latest:true,branch_choice:true,exact_history_downloads:true,locked_readonly:true,stale_resolver_guard:true,draft_cancel_and_discard:true,back_forward:true}));
 }finally{await context.close();await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1});

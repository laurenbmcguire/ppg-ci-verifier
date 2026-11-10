// Actual handler and synthetic-only inputs. Browser refuses every external request.
const assert=require('node:assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright-core');
const path=require('node:path');
(async()=>{
 const [base,config,viewport]=process.argv.slice(2),cases=JSON.parse(config),width=Number(viewport);
 const browser=await chromium.launch({headless:true,executablePath:process.env.CHROMIUM_EXECUTABLE});
 const context=await browser.newContext({viewport:{width,height:900}}),page=await context.newPage(),errors=[],external=[],writes=[],requests=[];
 await context.route('**/*',r=>r.request().url().startsWith(base+'/')?r.continue():(external.push(r.request().url()),r.abort()));
 page.on('pageerror',e=>errors.push(String(e)));
 page.on('request',r=>{if(r.url().endsWith('/apply-fixes'))writes.push(r.postDataJSON());if(r.url().endsWith('/ai-proposals'))requests.push(r.postDataJSON());});
 try {
  for(const c of cases){
   await page.goto(base+'/app?synthetic_case='+c.name+'#r='+c.hash,{waitUntil:'networkidle'});
   await page.locator('.fd-head .fd-actions > a[download][href="/api/spreadsheets/download?id='+c.hash+'"]').waitFor();
   if(width<900&&!await page.locator('body').evaluate(el=>el.classList.contains('fd-panel-open')))await page.locator('.fd-mobile-panel').click();
   await page.locator('#fd-open-review:enabled').click();
   const dialog=page.locator('.fd-review-dialog');
   for(const id of c.ids)await dialog.locator('input[data-review-key='+JSON.stringify(id)+']').check();
   const beforeWrites=writes.length,beforeRequests=requests.length;
   const prepared=page.waitForResponse(r=>r.url().endsWith('/ai-proposals')&&r.request().method()==='POST');
   const saved=c.name==='unsupported'?null:page.waitForResponse(r=>r.url().endsWith('/apply-fixes')&&r.request().method()==='POST');
   const refreshFailed=c.name==='refresh_failure'?page.waitForResponse(r=>r.url().includes('/api/spreadsheet?id=')&&r.status()===503):null;
   if(refreshFailed)refreshFailed.catch(()=>{});
   if(saved)saved.catch(()=>{});
   await dialog.getByRole('button',{name:'AI fix selected',exact:true}).click();
   const response=await prepared;assert.equal(response.status(),200);const payload=(await response.json()).ai_proposals;
   assert.equal(requests.length,beforeRequests+1);assert.deepEqual(requests.at(-1).ids.slice().sort(),c.ids.slice().sort());assert.deepEqual(requests.at(-1).seen,c.fps);
   if(c.name==='unsupported'){
    assert.equal(payload.proposals.length,0);assert.equal(payload.skipped.length,1);
    assert.match(payload.skipped[0].reason,/row or report, not one editable cell/);
    await page.getByText(payload.skipped[0].reason,{exact:false}).first().waitFor();assert.equal(writes.length,beforeWrites);
   }else{
    assert.equal(payload.proposals.length,2);
    const mechanical=payload.proposals.filter(p=>p.selected_finding_id);
    assert.equal(mechanical.length,c.name==='mixed'?1:2);
    for(const p of mechanical){assert.equal(p.selected_finding_fp,c.fps[p.selected_finding_id]);assert.equal(p.sure,true);assert.equal(p.recommendation_eligible,false);}
    if(c.name==='mixed')assert.equal(payload.proposals.filter(p=>p.source==='AI fix (asked)').length,1);
    const receipt=await saved;assert.equal(receipt.status(),200);const body=await receipt.json();assert.equal(body.item.fixed_from.changes.length,2);
    await page.locator('#fd-aidone').waitFor();assert.match(await page.locator('#fd-aidone').innerText(),/All 2 changes are applied and saved as version 2/);
    if(c.name!=='refresh_failure'){await page.locator('.fd-airun.good').waitFor();assert.match(await page.locator('.fd-airun.good').innerText(),/Dex fixed 2 cells/);}else{
     await refreshFailed;
     assert.match(await page.locator('#fd-aidone').innerText(),/Version 1 is kept/);
    }
    assert.equal(writes.length,beforeWrites+1);assert.equal(writes.at(-1).expected_check_hash,requests.at(-1).expected_check_hash);
   }
   assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
   await page.screenshot({path:path.join(process.env.EVIDENCE_DIR||'/tmp',`supported-${c.name}-${width}.png`),fullPage:true});
  }
  assert.equal(writes.length,3);assert.equal(requests.length,4);assert.deepEqual(errors,[]);assert.deepEqual(external,[]);
  console.log(JSON.stringify({passed:true,width,exact_selection_fingerprints:true,mechanical_multi_item:true,mixed_one_save:true,unsupported_precise_reason:true,no_retries:true,post_save_refresh_failure_receipt_preserved:true,screenshots:cases.map(c=>`supported-${c.name}-${width}.png`)}));
 }finally{await context.close();await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1});

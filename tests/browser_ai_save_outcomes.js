// Actual application + HTTP handler; fabricated workbooks/provider only.
const assert=require('node:assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright-core');
(async()=>{
 const [base,hash,viewport,mode]=process.argv.slice(2),width=Number(viewport);
 const browser=await chromium.launch({headless:true,executablePath:process.env.CHROMIUM_EXECUTABLE});
 const context=await browser.newContext({viewport:{width,height:900}}),page=await context.newPage();
 const errors=[],external=[],writes=[],proposals=[];let saved=false,receipt=null;
 await context.route('**/*',async route=>{
  const req=route.request(),url=req.url();
  if(!url.startsWith(base+'/')){external.push(url);return route.abort();}
  if(url.endsWith('/api/spreadsheets')&&saved&&mode==='refresh-fail')return route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({error:'Synthetic list refresh unavailable'})});
  if(url.endsWith('/apply-fixes')){
   writes.push(req.postDataJSON());
   if(mode==='backend-reject')return route.fulfill({status:409,contentType:'application/json',body:JSON.stringify({error:'Synthetic source snapshot changed. Review the source again. Nothing was applied.'})});
   const response=await route.fetch();receipt=await response.json();assert.equal(response.status(),200,JSON.stringify(receipt));saved=true;
   if(mode==='lost-response')return route.abort('failed');
   return route.fulfill({response});
  }
  if(url.endsWith('/ai-proposals'))proposals.push(req.postDataJSON());
  return route.continue();
 });
 page.on('pageerror',e=>errors.push(String(e)));
 try{
  await page.goto(base+'/app#r='+hash,{waitUntil:'networkidle'});
  if(width<900){await page.locator('.fd-mobile-panel').waitFor();await page.locator('.fd-mobile-panel').click();}
  const button=page.locator('#fd-fix-everything:enabled');await button.waitFor({timeout:15000});
  assert.equal(writes.length,0);
  if(mode==='preview'){
   const prepare=async()=>{await page.locator('#fd-open-review:enabled').click();const dialog=page.locator('.fd-review-dialog');await dialog.getByRole('button',{name:/^Select all \(/}).click();await dialog.getByRole('button',{name:'Preview 2 supported changes',exact:true}).click();await page.locator('#fd-preview-dialog').waitFor();};
   await prepare();assert.equal(await page.locator('#fd-preview-dialog tbody tr').count(),2);assert.equal(writes.length,0);
   await page.locator('#fd-preview-dialog').getByRole('button',{name:'Back to review',exact:true}).click();assert.equal(writes.length,0);
   await prepare();await page.locator('#fd-apply').click();await page.waitForURL(url=>saved&&url.hash==='#r='+receipt.item.file_hash);
   assert.equal(writes.length,1);assert.equal(proposals.length,0);assert.equal(writes[0].recommended_only,true);assert.equal(writes[0].changes.length,2);assert.ok(writes[0].changes.every(c=>c.source==='Clean-up'));
   assert.deepEqual(errors,[]);assert.deepEqual(external,[]);console.log(JSON.stringify({passed:true,mode,width,preview_and_cancel_no_write:true,exact_two_changes:true,one_apply_request:true}));return;
  }
  await button.click();
  if(mode==='provider-failure'){
   await page.locator('#fd-aidone').waitFor({timeout:20000});
   const text=await page.locator('#fd-aidone').innerText();
   assert.match(text,/provider rejected the request.*HTTP 503/);
   assert.doesNotMatch(text,/Synthetic secret|Dex could not be reached|All 2 changes are applied|whole row or an earlier decision/);
   assert.equal(writes.length,0);assert.equal(proposals.length,1);
   assert.deepEqual(errors,[]);assert.deepEqual(external,[]);
   console.log(JSON.stringify({passed:true,mode,width,truthful_provider_http:true,no_save:true}));return;
  }
  if(mode==='backend-reject'||mode==='lost-response'){
   await page.locator('.fd-airun.bad').waitFor();const text=await page.locator('.fd-airun.bad').innerText();
   if(mode==='backend-reject')assert.match(text,/Synthetic source snapshot changed/);
   else{assert.match(text,/could not be confirmed|not confirmed/i);assert.doesNotMatch(text,/Nothing was changed/i);}
  }else{
   await page.locator('#fd-aidone').waitFor({timeout:20000});
   const text=await page.locator('#fd-aidone').innerText();
   if(mode==='reuse'){
    assert.match(text,/Opened the existing matching version/);assert.match(text,/All 2 requested changes match existing version 3/);assert.match(text,/No new version was created/);assert.doesNotMatch(text,/saved as version 2|What was fixed/);
    assert.equal(receipt.item.fixed_from.changes.length,3);assert.equal(receipt.item.save_outcome.changes.length,2);
    await page.waitForURL(url=>url.hash==='#r='+receipt.item.file_hash);
    if(process.env.QA_SCREENSHOT_DIR)await page.screenshot({path:process.env.QA_SCREENSHOT_DIR+'/reuse-'+width+'.png',fullPage:true});
    await page.locator('#fd-aidone').getByRole('link',{name:'Open starting version 2',exact:true}).click();
    await page.waitForURL(url=>url.hash==='#r='+hash);await page.waitForLoadState('networkidle');
    assert.equal(writes.length,1);assert.deepEqual(errors,[]);assert.deepEqual(external,[]);
    console.log(JSON.stringify({passed:true,mode,width,existing_version_three_opened:true,starting_version_reopened:true,no_new_write:true,requested_changes_separate_from_history:true}));return;
   }
   assert.match(text,/All 2 changes are applied and saved/);
   assert.equal(await page.locator('#fd-aidone a[download]').getAttribute('href'),'/api/spreadsheets/download?id='+receipt.item.file_hash);
  }
  assert.equal(writes.length,1);assert.equal(proposals.length,1);
  assert.equal(writes[0].expected_check_hash,proposals[0].expected_check_hash);
  assert.equal(writes[0].changes.length,2);assert.ok(writes[0].changes.every(c=>c.source==='Clean-up'&&c.selected_finding_id&&c.selected_finding_fp));
  assert.deepEqual(errors,[]);assert.deepEqual(external,[]);
  console.log(JSON.stringify({passed:true,mode,width,one_apply_request:true,exact_binding:true,provider_calls_are_synthetic:true}));
 }catch(e){console.error('Diagnostic:',JSON.stringify({mode,writes:writes.length,saved,receipt_hash:receipt?.item?.file_hash,visible_toast:await page.locator('#toast').textContent(),ai_status:await page.locator('.fd-airun').allTextContents(),errors}));throw e;}
 finally{await context.close();await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1});

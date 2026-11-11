const assert=require('node:assert/strict'),path=require('node:path');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright-core');
(async()=>{
 const [base,config,w,mode]=process.argv.slice(2),width=Number(w),cases=JSON.parse(config);
 const browser=await chromium.launch({headless:true,executablePath:process.env.CHROMIUM_EXECUTABLE});
 const context=await browser.newContext({viewport:{width,height:900}}),page=await (async()=>{await context.addInitScript(()=>{try{localStorage.setItem('rd-auto-tidy','false')}catch(e){}});return context.newPage()})(),errors=[],external=[],writes=[],dialogs=[];
 let current,saved=false,receipt,release,arrived;
 await context.route('**/*',async route=>{
  const req=route.request(),url=req.url();if(!url.startsWith(base+'/')){external.push(url);return route.abort();}
  if(saved&&current.name==='refresh_failure'&&url.includes('/api/spreadsheet?id='))return route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({error:'Synthetic saved report read unavailable'})});
  if(url.endsWith('/apply-fixes')){
   writes.push(req.postDataJSON());const response=await route.fetch();receipt=await response.json();assert.equal(response.status(),200,JSON.stringify(receipt));saved=true;
   if(current.name==='navigation'){arrived();await new Promise(r=>release=r);}
   return route.fulfill({response});
  }
  return route.continue();
 });
 page.on('pageerror',e=>errors.push(String(e)));page.on('dialog',async d=>{dialogs.push(d.message());await d.dismiss();});
 try{
  for(const c of cases){
   current=c;saved=false;receipt=null;console.log('Checking '+c.name+' at '+width);
   await page.goto(base+'/app#r='+c.hash,{waitUntil:'networkidle'});
   if(width<900&&!await page.locator('body').evaluate(el=>el.classList.contains('fd-panel-open')))await page.locator('.fd-mobile-panel').click();
   await page.locator('#fd-open-review:enabled').waitFor();
   await page.locator('#fd-open-review').click();const dialog=page.locator('.fd-review-dialog');
   for(const id of c.ids)await dialog.locator('input[data-review-key='+JSON.stringify(id)+']').check();
   if(c.name==='manual'){
    const prior=writes.length;
    await dialog.getByRole('button',{name:'Fix selected manually',exact:true}).click();
    const editor=dialog.getByRole('region',{name:'Fix manually in this review'}),input=editor.getByRole('textbox',{name:'New value for 1st Shift!G17',exact:true});
    await input.fill('34');await editor.getByRole('button',{name:'Preview 1 manual changes',exact:true}).click();
    await page.locator('#fd-preview-dialog').waitFor();assert.equal(await page.locator('#fd-preview-dialog tbody tr').count(),1);assert.match(await page.locator('#fd-preview-dialog').innerText(),/Your edit/);assert.equal(writes.length,prior);
    await page.locator('#fd-preview-dialog').getByRole('button',{name:'Back to review',exact:true}).click();assert.equal(writes.length,prior);assert.equal(await input.inputValue(),'34');
    await editor.getByRole('button',{name:'Preview 1 manual changes',exact:true}).click();
    await page.locator('#fd-preview-dialog').getByRole('button',{name:'Apply 1 change and re-check',exact:true}).click();
    await dialog.waitFor({state:'detached'});try{await page.locator('.fd-saved-summary').waitFor();}catch(e){console.error(JSON.stringify({manual_url:page.url(),dialogs,receipt,body:(await page.locator('body').innerText()).slice(-7000)}));throw e;}
    assert.equal(new URL(page.url()).hash,'#r='+receipt.item.file_hash);assert.equal(writes.length,prior+1);
    if(width<900&&!await page.locator('body').evaluate(el=>el.classList.contains('fd-panel-open')))await page.locator('.fd-mobile-panel').click();
    await page.locator('#fd-open-review').click();const currentDialog=page.locator('.fd-review-dialog');
    assert.equal(await currentDialog.locator('input[data-review-key='+JSON.stringify(c.ids[0])+']').count(),0);
    assert.equal(await currentDialog.locator('input[data-review-key='+JSON.stringify(c.unselected_id)+']').count(),1);
    await currentDialog.getByRole('button',{name:'Close review',exact:true}).click();
    assert.match(await page.locator('#fd-countline').innerText(),/1 saved cell change/);
    await page.getByRole('button',{name:'History',exact:true}).click();await page.locator('.fd-saved-history > summary').click();
    await page.locator('.fd-saved-history .fd-history-group > summary').click();assert.match(await page.locator('.fd-saved-history').innerText(),/G17/);assert.match(await page.locator('.fd-saved-history').innerText(),/-1 → 34/);
    await page.screenshot({path:path.join(process.env.EVIDENCE_DIR||'/tmp',`finding-history-manual-${width}.png`),fullPage:true});
    await page.getByRole('button',{name:'Review',exact:true}).click();
    await page.screenshot({path:path.join(process.env.EVIDENCE_DIR||'/tmp',`finding-open-list-manual-${width}.png`),fullPage:true});continue;
   }
   const prior=writes.length,hold=new Promise(r=>arrived=r);
   if(c.name==='standalone'){await dialog.getByRole('button',{name:'Close review',exact:true}).click();await page.locator('#side input[data-tick*="fmt:extra spaces"]').first().check();await page.locator('#fd-fix-everything').click();}
   else await dialog.getByRole('button',{name:'AI fix selected',exact:true}).click();
   if(c.name==='navigation'){
    await hold;await page.evaluate(()=>location.hash='');await page.waitForFunction(h=>location.hash==='#r='+h,c.hash);
    assert.match(await page.locator('#toast').innerText(),/Wait for the current operation/);release();
    await page.locator('#fd-aidone').waitFor();await page.locator('#fd-aidone').getByRole('button',{name:'Close',exact:true}).last().click();
    await page.evaluate(()=>location.hash='');await page.waitForURL(url=>!url.hash);await page.waitForTimeout(250);
    assert.equal(new URL(page.url()).hash,'');assert.equal(await page.locator('#fd-aidone[open]').count(),0);assert.equal(writes.length,prior+1);continue;
   }
   await page.locator('#fd-aidone').waitFor();const text=await page.locator('#fd-aidone').innerText();
   if(mode==='baseline'){
    assert.equal(c.name,'partial');assert.match(text,/Dex fixed everything you selected/);
    await page.locator('#fd-aidone').getByRole('button',{name:'Close',exact:true}).last().click();
    await page.locator('.fd-airun.good').waitFor(); console.log(JSON.stringify({baseline_dialog:text,side:await page.locator('#side').innerText(),keys:await page.locator('#side [data-tick]').evaluateAll(es=>es.map(e=>e.getAttribute('data-tick')))}));
    assert.ok(await page.locator('#side .fd-airun .fd-fixed-list').count());
    console.log(JSON.stringify({reproduced:true,bug:'Claims every selected finding fixed while negative quantity is still open; saved cells repeated in open panel',width}));break;
   }
   assert.doesNotMatch(text,/Dex fixed everything you selected/);
   assert.match(text,/All 2 changes are applied and saved as version 2/);
   if(c.name==='refresh_failure'){assert.match(text,/resolution could not be verified/i);assert.doesNotMatch(text,/2 selected findings cleared/);}
   else if(c.name==='standalone'){assert.match(text,/2 selected suggestions applied/);assert.match(text,/0 selected findings still need review/);}
   else{assert.match(text,new RegExp((c.name==='partial'?1:2)+' selected finding'+(c.name==='partial'?'':'s')+' cleared'));
    if(c.name==='partial')assert.match(text,/still detected in the saved check/);
   }
   await page.screenshot({path:path.join(process.env.EVIDENCE_DIR||'/tmp',`finding-lifecycle-${c.name}-${width}.png`),fullPage:true});
   await page.locator('#fd-aidone').getByRole('button',{name:'Close',exact:true}).last().click();
   if(c.name!=='refresh_failure'){
    await page.locator('.fd-airun.good').waitFor();assert.equal(await page.locator('#side .fd-airun .fd-fixed-list').count(),0);
    await page.locator('#fd-open-review').click();
    assert.equal(await page.locator('.fd-review-dialog input[data-review-key='+JSON.stringify(c.ids[1])+']').count(),0);
    if(c.name==='partial')assert.ok(await page.locator('.fd-review-dialog input[data-review-key='+JSON.stringify(c.ids[0])+']').count());
    await page.locator('.fd-review-dialog').getByRole('button',{name:'Close review',exact:true}).click();
    await page.getByRole('button',{name:'View saved changes in History',exact:true}).click();
    await page.locator('.fd-saved-history > summary').click();
    for(const summary of await page.locator('.fd-saved-history .fd-history-group > summary').all())await summary.click();
    assert.equal(await page.locator('.fd-saved-history .fd-history-page li').count(),2);
    assert.match(await page.locator('.fd-saved-history').innerText(),c.name==='standalone'?/I17/:/G17/);
    await page.screenshot({path:path.join(process.env.EVIDENCE_DIR||'/tmp',`finding-history-${c.name}-${width}.png`),fullPage:true});
    await page.getByRole('button',{name:'Review',exact:true}).click();
    await page.screenshot({path:path.join(process.env.EVIDENCE_DIR||'/tmp',`finding-open-list-${c.name}-${width}.png`),fullPage:true});
    const initial=await page.locator('#fd-countline').innerText();await page.reload({waitUntil:'networkidle'});if(width<900&&!await page.locator('body').evaluate(el=>el.classList.contains('fd-panel-open')))await page.locator('.fd-mobile-panel').click();await page.locator('#fd-countline').waitFor();assert.equal(await page.locator('#fd-countline').innerText(),initial);
   }
   assert.equal(writes.length,prior+1);assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
  }
  assert.deepEqual(errors,[]);assert.deepEqual(external,[]);assert.deepEqual(dialogs,[]);console.log(JSON.stringify({passed:true,width,mode,saves:writes.length}));
 }finally{await context.close();await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1});

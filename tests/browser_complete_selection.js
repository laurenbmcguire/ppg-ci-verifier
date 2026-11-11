const assert=require('node:assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright-core');
(async()=>{
 const [base,hash,viewport]=process.argv.slice(2),width=Number(viewport),browser=await chromium.launch({headless:true,executablePath:process.env.CHROMIUM_EXECUTABLE});
 const context=await browser.newContext({viewport:{width,height:900}}),page=await (async()=>{await context.addInitScript(()=>{try{localStorage.setItem('rd-auto-tidy','false')}catch(e){}});return context.newPage()})(),errors=[],external=[],proposals=[],pages=[];
 await context.route('**/*',async route=>{const req=route.request();if(!req.url().startsWith(base+'/')){external.push(req.url());return route.abort();}if(req.url().endsWith('/ai-proposals'))proposals.push(req.postDataJSON());if(req.url().endsWith('/findings'))pages.push(req.postDataJSON());return route.continue();});
 page.on('pageerror',e=>errors.push(String(e)));
 try{
  await page.goto(base+'/app#r='+hash,{waitUntil:'networkidle'});
  if(width<900)await page.locator('.fd-mobile-panel').click();
  const all=page.locator('[data-tick="all"]');await all.waitFor();await all.check();await page.waitForFunction(()=>document.querySelector('.fd-tickcount')?.textContent==='1301 selected');
  await all.uncheck();assert.match(await page.locator('.fd-tickcount').innerText(),/Tick the errors/);await all.check();
  await page.getByRole('button',{name:'Show all 1301',exact:true}).click();
  const seen=[];
  while(true){
   const checks=page.locator('.fd-problems input[data-tick]');seen.push(...await checks.evaluateAll(nodes=>nodes.map(n=>n.dataset.tick)));
   const next=page.getByRole('button',{name:'Next errors',exact:true});if(await next.isDisabled())break;await next.click();
  }
  assert.equal(seen.length,1301);assert.equal(new Set(seen).size,1301);
  const excluded=await page.locator('.fd-problems input[data-tick]').last().getAttribute('data-tick');
  await page.locator('.fd-problems input[data-tick]').last().uncheck();assert.equal(await page.locator('.fd-tickcount').innerText(),'1300 selected');
  await page.getByRole('button',{name:'Show fewer',exact:true}).click();await page.getByRole('button',{name:'Show all 1301',exact:true}).click();
  assert.equal(await page.locator('.fd-problems input[data-tick]').last().isChecked(),false);
  if(process.env.QA_SCREENSHOT_DIR)await page.screenshot({path:process.env.QA_SCREENSHOT_DIR+'/selection-last-page-'+width+'.png',fullPage:true});
  await page.locator('#fd-fix-everything').click();await page.locator('#fd-aidone').waitFor({timeout:60000});
  const keys=proposals.flatMap(p=>p.ids);assert.equal(keys.length,1300);assert.equal(new Set(keys).size,1300);assert.ok(!keys.includes(excluded));assert.equal(proposals.length,11);assert.ok(proposals.every(p=>p.ids.length<=120));
  assert.ok(pages.length>=4&&pages.every(p=>p.expected_binding.file_hash===hash));assert.deepEqual(errors,[]);assert.deepEqual(external,[]);
  await page.locator('#fd-aidone').getByRole('button',{name:'Close',exact:true}).last().click();
  await page.locator('#fd-fix-all-manual').click();
  const draft=page.locator('.fd-manual input[data-cell]');await draft.waitFor();await draft.fill('Synthetic draft kept');
  await page.getByRole('button',{name:'Previous errors',exact:true}).click();assert.equal(await draft.inputValue(),'Synthetic draft kept');
  await page.getByRole('button',{name:'Show fewer',exact:true}).click();assert.equal(await draft.inputValue(),'Synthetic draft kept');
  await page.getByRole('button',{name:'Show all 1301',exact:true}).click();
  await page.locator('.fd-problems input[data-tick]').first().uncheck();assert.equal(await draft.inputValue(),'Synthetic draft kept');
  assert.deepEqual(errors,[]);
  console.log(JSON.stringify({passed:true,width,manual_draft_preserved:true,requested_keys:keys,excluded}));
 }catch(e){console.error('Diagnostic:',JSON.stringify({errors,proposals:proposals.length,toast:await page.locator('#toast').textContent(),text:await page.locator('#side').innerText()}));throw e;}
 finally{await context.close();await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1});

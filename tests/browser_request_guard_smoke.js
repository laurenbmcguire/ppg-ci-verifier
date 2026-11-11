// Existing installed runtime only; no package installation or external traffic.
const assert = require('node:assert/strict');
const groupedFixture=JSON.parse(require('node:fs').readFileSync('tests/fixtures/grouped_review.json','utf8'));
// Set PLAYWRIGHT_MODULE to an existing module path/name when it is not on Node's module path.
// Set CHROMIUM_EXECUTABLE to an existing browser binary, or use Playwright's installed default.
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright-core');
const executablePath = process.env.CHROMIUM_EXECUTABLE || undefined;
(async () => {
 const base = process.argv[2], hash = process.argv[3];
 const browser = await chromium.launch({executablePath, headless: true});
 const results = [];
 try {
  for (const width of [1440, 390]) {
   const context = await browser.newContext({viewport: {width, height: 900}});
   const page = await (async()=>{await context.addInitScript(()=>{try{localStorage.setItem('rd-auto-tidy','false')}catch(e){}});return context.newPage()})(), errors = [], external = [];
   await context.route('**/*', route => { if (route.request().url().startsWith(base + '/')) return route.continue(); external.push(route.request().url()); return route.abort(); });
   page.on('pageerror', e => errors.push(String(e)));
   await page.goto(base + '/app#r=' + hash, {waitUntil: 'networkidle'});
   await page.locator('.fd-title').waitFor({timeout: 15000});
   assert.equal(await page.getByRole('tab',{name:'Facts',exact:true}).count(),0);
   await page.getByRole('button', {name: 'Review manually', exact: true}).click();
   if (await page.locator('.fd-mobile-panel').isVisible()) await page.locator('.fd-mobile-panel').click();
   await page.locator('.fd-grouphead:visible').first().click();
   await page.locator('#fd-card').waitFor();
   await page.getByRole('tab', {name: /Formatting/}).click();
   await page.locator('.fd-grouphead:visible').first().click();
   await page.getByRole('button', {name: 'Include in preview', exact: true}).click();
   await page.locator('#fd-preview-btn').click();
   assert.equal(await page.locator('dialog.fd-modal[open]').count(),1);
   const layout=await page.locator('#fd-preview-dialog').evaluate(dialog=>{
    const sheet=dialog.querySelector('.fd-preview-sheet'),cell=dialog.querySelector('.fd-preview-cell'),remove=dialog.querySelector('.fd-preview-remove');
    return {sheetDisplay:getComputedStyle(sheet).display,sheet:sheet.textContent,cellWhite:getComputedStyle(cell).whiteSpace,
      removeWhite:getComputedStyle(remove).whiteSpace,removeMin:getComputedStyle(remove).minWidth,
      cellBelow:cell.getBoundingClientRect().top>=sheet.getBoundingClientRect().bottom,
      locationMin:getComputedStyle(dialog.querySelector('.fd-preview-location')).minWidth,
      columnWidths:[...dialog.querySelector('tbody tr').cells].map(td=>td.getBoundingClientRect().width),
      rowHeight:dialog.querySelector('tbody tr').getBoundingClientRect().height,
      scrollWidth:dialog.querySelector('.fd-prevlist').scrollWidth,clientWidth:dialog.querySelector('.fd-prevlist').clientWidth};
   });
   assert.equal(layout.sheetDisplay,'block');assert.ok(layout.sheet.length);assert.equal(layout.cellWhite,'nowrap');
   assert.equal(layout.removeWhite,'nowrap');assert.equal(layout.removeMin,'max-content');assert.ok(layout.cellBelow);
   assert.ok(parseFloat(layout.locationMin)>0,'external CSS applies under CSP');
   assert.ok(layout.columnWidths[1]>=130 && layout.columnWidths[2]>=130,'Before and After remain readable');
   assert.ok(layout.columnWidths[3]>=200,'Why retains readable line lengths');
   assert.ok(layout.rowHeight<350,'normal synthetic correction must not become a vertical letter stack');
   if(width<600){
    assert.ok(layout.scrollWidth>layout.clientWidth,'phone preview scrolls instead of shrinking columns');
    const scroller=page.locator('#fd-preview-dialog .fd-prevlist');
    await scroller.focus();await page.keyboard.press('ArrowRight');await page.waitForTimeout(250);
    assert.ok(await scroller.evaluate(el=>el.scrollLeft)>0,'horizontal scroll reachable by keyboard');
   }
   await page.locator('#fd-preview-btn').dispatchEvent('click');
   assert.equal(await page.locator('dialog.fd-modal[open]').count(),1,'repeated event stays singleton');
   await page.getByRole('button', {name:'Back to review',exact:true}).click();
   assert.equal(await page.locator('dialog.fd-modal[open]').count(),0);
   await page.locator('#fd-preview-btn').click();
   await page.getByRole('button', {name:/Remove .* from preview/}).first().click();
   assert.equal(await page.locator('dialog.fd-modal[open]').count(),0);
   assert.equal(await page.locator('#fd-selcount').innerText(),'No changes selected');
   await page.locator('.fd-grouphead:visible').first().click();
   await page.locator('#fd-card summary').filter({hasText:'Edit value'}).click();
   await page.locator('#fd-card .fd-own input').fill('SYNTHETIC EDIT');
   await page.getByRole('button',{name:'Use this value',exact:true}).click();
   await page.getByRole('button',{name:'Remove from preview',exact:true}).click();
   assert.equal(await page.locator('#fd-selcount').innerText(),'No changes selected','manual representation removed too');

   if (await page.locator('#fd-card').isVisible() && width < 600) await page.getByRole('button', {name: 'Close this correction', exact: true}).click();
   if (width < 600 && await page.locator('.fd-mobile-panel').isVisible()) await page.locator('.fd-mobile-panel').click();
   await page.getByRole('button', {name: 'History', exact: true}).click();
   await page.locator('.fd-hist').first().waitFor();
   await page.getByRole('button', {name: 'Review', exact: true}).click();
   if (await page.locator('.fd-mobile-panel').isVisible()) await page.locator('.fd-mobile-panel').click();
   await page.locator('.fd-grouphead:visible').first().waitFor();
   assert.deepEqual(errors, []); assert.deepEqual(external, []);
   await page.getByRole('tab',{name:'Ask Dex',exact:true}).click();
   assert.match(await page.locator('#chat').innerText(),/AI preparation is not configured/);
   assert.equal(await page.locator('#chat textarea').isDisabled(),true);
   assert.equal(await page.locator('#chat').getByRole('button',{name:'Send',exact:true}).isDisabled(),true);
   assert.equal(await page.locator('#chat').getByRole('button',{name:/Do all|Go through them|I'll pick/}).count(),0);
   await page.getByRole('button',{name:'Go to review modes',exact:true}).click();
   await page.getByRole('button',{name:'Leave file unchanged for now',exact:true}).click();
   assert.equal(await page.locator('#fd-selcount').innerText(),'No changes selected');
   assert.equal(await page.locator('#fd-preview-btn').isDisabled(),true);
   assert.equal(await page.getByRole('button',{name:'AI prepares suggestions',exact:true}).count(),0,'choice flow does not launch AI');
   assert.match(await page.locator('.fd-mode-selector').innerText(),/Review found/);
   await page.getByRole('button',{name:'Source evidence and coverage',exact:true}).click();
   await page.getByRole('heading',{name:'Sources',exact:true}).waitFor();
   assert.match(await page.locator('.fd-utility').innerText(),/partial or conflicting read is never called verified/);
   assert.equal(await page.locator('.fd-comparison').count(),1);
   await page.getByRole('button',{name:'Review',exact:true}).click();
   // Mock only these synthetic recovery responses; no settings/source mutation reaches the server.
   const recoveryCalls=[];let confirmed=false,compared=false,capturedCheckHash;
   await page.route('**/api/spreadsheet?*',async route=>{
    const response=await route.fetch(),data=await response.json();capturedCheckHash=data.check_hash;
    data.source_reconciliation={snapshot_id:'synthetic-recovery-snapshot',site_id:'synthetic-site',list_id:'synthetic-list',list:{id:'synthetic-list',name:'Synthetic exact list'},
      bound_to:{file_hash:hash,checker_version:data.checker_version},coverage:{complete:false},verified:false,count_reconciliation:{status:'held',record_counts:{report_rows:13,source_rows:0,matched_unique_items:0,duplicate_report_rows:0,missing_source_items:0,unmatched_report_rows:13}},
      column_mapping:{inspected:{status:compared?'confirmed':'needs_confirmation',column:'SyntheticQuantity'}}};
    data.holds=compared?[]:[{code:'source_mapping_changed',hard:true,text:'Synthetic source map requires fresh comparison'}];
    data.auto_fixed=structuredClone(groupedFixture.auto_fixed);
    data.review.sharepoint={status:'incomplete',matched:13,source_verified:false,message:compared?'Synthetic fresh comparison complete; quantities still unknown.':'Synthetic source column confirmation required.'};
    await route.fulfill({response,json:data});
   });
   await page.route('**/api/sharepoint/confirm-columns',async route=>{
    recoveryCalls.push({path:'confirm',body:route.request().postDataJSON()});confirmed=true;
    await route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({saved:true})});
   });
   await page.route('**/api/spreadsheets/source-reconcile',async route=>{
    assert.ok(confirmed,'comparison follows explicit confirmation');compared=true;recoveryCalls.push({path:'compare',body:route.request().postDataJSON()});
    await route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({ok:true})});
   });
   await page.reload({waitUntil:'networkidle'});await page.locator('.fd-title').waitFor();
   if(await page.locator('.fd-mobile-panel').isVisible())await page.locator('.fd-mobile-panel').click();
   await page.locator('.fd-comparison > summary').click();
   assert.match(await page.locator('.fd-meta').innerText(),/Source comparison incomplete · 0 unique source items matched \/ 13 report rows/);
   assert.ok(!(await page.locator('.fd-meta').innerText()).includes('13 of 13'));
   const history=page.locator('.fd-history-overview');
   assert.equal(await history.evaluate(el=>el.open),false);
   assert.match(await history.locator(':scope > summary').innerText(),/301 applied changes · 1 AI recommendation/);
   assert.equal(await page.locator('.fd-autohead:visible').count(),0,'history success icon does not outrank current holds');
   assert.ok(await page.locator('.fd-tiers').evaluate(el=>!!(el.compareDocumentPosition(document.querySelector('.fd-history-overview'))&Node.DOCUMENT_POSITION_FOLLOWING)));
   const recovery=page.locator('.fd-source-recovery');
   assert.match(await recovery.innerText(),/synthetic-site/);assert.match(await recovery.innerText(),/synthetic-list/);
   assert.match(await recovery.innerText(),/inspected → SyntheticQuantity/);
   await page.getByRole('button',{name:'Sources',exact:true}).click();
   assert.match(await page.locator('.fd-utility').innerText(),/0 unique source items matched \/ 13 report rows/);
   assert.ok(!(await page.locator('.fd-utility').innerText()).includes('13 of 13'));
   await page.getByRole('button',{name:'Review',exact:true}).click();
   if(!(await page.locator('.fd-comparison > summary').isVisible()) && await page.locator('.fd-mobile-panel').isVisible())await page.locator('.fd-mobile-panel').click();
   await page.locator('.fd-comparison > summary').click();
   await recovery.getByRole('button',{name:'Confirm source columns',exact:true}).click();
   await page.getByText('Source columns saved. The workbook is unchanged and existing holds still apply. Compare again to capture fresh evidence.',{exact:true}).waitFor();
   assert.equal(recoveryCalls.length,1,'confirmation never automatically compares');
   assert.deepEqual(recoveryCalls[0].body,{file_hash:hash,expected_check_hash:capturedCheckHash,expected_snapshot_id:'synthetic-recovery-snapshot',expected_mapping_revision:null,site_id:'synthetic-site',list_id:'synthetic-list',mapping:{inspected:'SyntheticQuantity'}});
   await Promise.all([page.waitForResponse(response=>response.url().includes('/api/spreadsheet?id=')),recovery.getByRole('button',{name:'Compare again',exact:true}).click()]);
   assert.equal(recoveryCalls.length,2);assert.deepEqual(recoveryCalls[1].body,{file_hash:hash});
   compared=false;
   await page.route('**/api/me',async route=>{const response=await route.fetch(),data=await response.json();data.user={...data.user,role:'member'};await route.fulfill({response,json:data});});
   await page.reload({waitUntil:'networkidle'});await page.locator('.fd-title').waitFor();
   if(await page.locator('.fd-mobile-panel').isVisible())await page.locator('.fd-mobile-panel').click();
   await page.locator('.fd-comparison > summary').click();
   assert.match(await page.locator('.fd-source-recovery').innerText(),/Ask an admin/);
   assert.equal(await page.getByRole('button',{name:'Confirm source columns',exact:true}).isDisabled(),true);
   assert.equal(recoveryCalls.length,2,'member cannot trigger a source mapping save');
   assert.deepEqual(errors,[]);assert.deepEqual(external,[]);
   results.push({width, opened:true, finding_selected:true, history_and_back:true, preview_singleton:true, preview_cancel:true, manual_cancel:true, leave_unchanged:true, page_errors:errors});
   await context.close();
  }
 } finally { await browser.close(); }
 console.log(JSON.stringify({passed: true, results}));
})().catch(e => { console.error(e); process.exitCode = 1; });

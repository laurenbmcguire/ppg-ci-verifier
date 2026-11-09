// Production app and handlers; only the inventory response is fabricated for edge cases.
const assert=require('node:assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright-core');
(async()=>{
 const [base,hash]=process.argv.slice(2),browser=await chromium.launch({headless:true,executablePath:process.env.CHROMIUM_EXECUTABLE});
 try {for(const width of [1200,390]){
  const context=await browser.newContext({viewport:{width,height:900}}),page=await context.newPage(),errors=[],external=[];
  await context.route('**/*',route=>{if(route.request().url().startsWith(base+'/'))return route.continue();external.push(route.request().url());return route.abort();});
  page.on('pageerror',e=>errors.push(String(e)));
  await page.route('**/api/spreadsheets',async route=>{
   const response=await route.fetch(),data=await response.json(),r=data.spreadsheets[0];
   r.review={...r.review,day:'2026-10-07',job:'SYNTHETIC-2230',counts:{high:0,medium:3,low:1},status:'check',auto_hidden:[],quantities:{inspected:{value:120,partial:true,unit:'parts'},certified:{value:100,partial:false,unit:'parts'},rejected:{value:0,partial:false,unit:'rows'}},sharepoint:{status:'incomplete',source:'Synthetic exact source list'}};
   r.assignment={status:'manual',client:'Synthetic Alpha',client_key:'alpha'};r.received_at='2026-11-09T03:00:00Z';r.version=1;
   const newer=structuredClone(r);Object.assign(newer,{file_hash:'b'.repeat(64),root_hash:hash,revision:2,received_at:'2026-11-09T04:00:00Z'});newer.review.fixed_from=r.filename;
   const separate=structuredClone(r);Object.assign(separate,{file_hash:'c'.repeat(64),version:2,received_at:'2026-11-09T02:00:00Z'});separate.review.coverage={truncated:true,not_listed:30};
   const missing=structuredClone(r);Object.assign(missing,{file_hash:'d'.repeat(64),version:null,assignment:{status:'ambiguous',client:'DO NOT INFER'},review:{status:'clear',day:null,job:null,counts:{high:0,medium:0,low:0},quantities:{},sharepoint:{status:'error'}}});
   const sent=structuredClone(r);Object.assign(sent,{file_hash:'e'.repeat(64)});sent.review.sent_at='2026-11-09T05:00:00Z';sent.review.day='2026-10-08';sent.assignment={status:'manual',client:'Synthetic Beta',client_key:'beta'};
   await route.fulfill({response,json:{...data,spreadsheets:[newer,r,separate,missing,sent],count:130,has_more:true}});
  });
  await page.goto(base+'/app',{waitUntil:'networkidle'});
  await page.locator('.queue-name').first().waitFor();
  assert.match(await page.locator('.queue-notice').innerText(),/not the full report history/);
  await page.getByRole('button',{name:'All',exact:true}).click();
  assert.equal(await page.locator('.item').count(),4,'only proven corrected family is collapsed');
  assert.equal(await page.locator('.item button').count(),0,'report button contains no nested review button');
  assert.match(await page.locator('.queue-entry').first().innerText(),/Report values/);
  const missing=page.locator('.item[data-report-hash="'+'d'.repeat(64)+'"]');
  assert.match(await missing.innerText(),/Report date not found/);assert.match(await missing.innerText(),/Uploaded 2026-11-09 03:00:00 UTC/);assert.match(await missing.innerText(),/Inspected: Unknown/);assert.match(await missing.innerText(),/Client not confirmed/);assert.match(await missing.innerText(),/Version unknown/);
  const independent=page.locator('.item[data-report-hash="'+'c'.repeat(64)+'"]');
  assert.match(await independent.innerText(),/Finding coverage incomplete/);assert.match(await independent.innerText(),/120 parts \(partial\)/);assert.match(await independent.innerText(),/Rejected: 0 rows/);
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
  assert.equal(await page.locator('.queue-name').first().evaluate(el=>getComputedStyle(el).whiteSpace),'normal');
  await page.screenshot({path:'../report-list-context-'+width+'.png',fullPage:true});
  const toggle=page.locator('.older-toggle');await toggle.click();assert.equal(await page.locator('.item').count(),5);assert.equal(await toggle.getAttribute('aria-expanded'),'true');assert.equal(await toggle.evaluate(el=>document.activeElement===el),true);
  const search=page.getByRole('searchbox',{name:'Search reports'});await search.fill('Synthetic Alpha');assert.equal(await page.locator('.item').count(),3);
  const original=page.locator('.item[data-report-hash="'+hash+'"]');await original.focus();await page.keyboard.press('Enter');await page.locator('.fd-title').waitFor();
  assert.equal(new URL(page.url()).hash,'#r='+hash);
  await page.getByRole('button',{name:'Back to all reports',exact:true}).click();await original.waitFor({state:'visible'});await page.waitForFunction(hash=>document.activeElement?.getAttribute('data-report-hash')===hash,hash);
  assert.equal(await search.inputValue(),'Synthetic Alpha');assert.equal(await page.getByRole('button',{name:'All',exact:true}).getAttribute('aria-pressed'),'true');assert.equal(await original.evaluate(el=>document.activeElement===el),true);
  assert.match(await original.innerText(),/Last opened/);
  await search.fill('No matching synthetic record');assert.match(await page.locator('.queue-empty').innerText(),/No reports match/);
  await search.fill('');await page.getByRole('button',{name:'Review requested',exact:true}).click();assert.match(await page.locator('.queue-empty').innerText(),/Use To do/);
  await page.getByRole('button',{name:/^To do/}).click();assert.equal(await page.locator('.item[data-report-hash="'+'e'.repeat(64)+'"]').count(),0,'sent excluded consistently from count and rows');
  assert.deepEqual(errors,[]);assert.deepEqual(external,[]);
  console.log(JSON.stringify({width,passed:true,full_app:true,unknown_date_labeled:true,partial_and_zero_distinct:true,lineage_only:true,complete_names_wrap:true,no_horizontal_overflow:true,keyboard_focus_restored:true,search_filter_preserved:true,no_mutations:true}));await context.close();
 }} finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});

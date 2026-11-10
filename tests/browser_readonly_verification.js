// Real Chromium, whole production app, loopback synthetic API. No live services/storage.
const fs=require('node:fs'),http=require('node:http'),assert=require('node:assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright-core');
const A='a'.repeat(64),B='b'.repeat(64),C='c'.repeat(64),D='d'.repeat(64),E='e'.repeat(64);
const make=(hash,extra={})=>({file_hash:hash,filename:'SYNTHETIC-HMMA.csv',revision:1,check_hash:'check-'+hash,checker_version:'synthetic',received_at:'2026-11-06T12:00:00Z',
 assignment:{status:'manual',client:'Synthetic Hyundai',client_key:'synthetic',site_id:'site',list_id:'list'},
 sheets:[{name:'Inspection',tables:[]},{name:'Other',tables:[]},{name:'Slow',tables:[]}],dataset_selection:{authoritative:['Inspection']},
 review:{job:'Synthetic job',day:'2026-11-06',scans:1294,status:'hold',counts:{high:0,medium:1294,low:0},quantities:{inspected:{value:1294,unit:'parts',complete:true},certified:{value:1294,unit:'parts',complete:true},rejected:{value:null,unit:'parts',complete:false}},
  sharepoint:{status:'incomplete',message:'Synthetic missing source page',source:'Synthetic list',snapshot_id:'snapshot'},run:{version:1},issues:Array.from({length:1290},(_,i)=>({key:'k'+i,sheet:'Inspection',cell:'I'+(i+2),code:'blank_cell',text:'Missing cell '+(i+2),severity:'medium'})),
  dismissed:[{key:'kept',sheet:'Inspection',cell:'I2',code:'blank_cell',text:'Saved person decision',severity:'medium',decision:{action:'reject',by:'Reviewer',reason:'Recorded only'}},{key:'ai',sheet:'Inspection',cell:'I3',code:'blank_cell',text:'AI recommendation',severity:'medium',dismissed_by:'claude_verify'},{key:'deferred',sheet:'Other',cell:'I2',code:'blank_cell',text:'Owner needed',severity:'medium',decision:{action:'defer'}},{key:'info',text:'Informational context',severity:'info'}]},
 source_reconciliation:{snapshot_id:'snapshot',site_id:'site',list_id:'list',bound_to:{file_hash:hash,checker_version:'synthetic'},coverage:{complete:false,failed_pages:['synthetic']},column_mapping:{inspected:{status:'needs_confirmation',column:'Qty'}}},
 holds:[{code:'source_incomplete',hard:true,text:'Source capture incomplete. Keep quantities unknown.'},{code:'awaiting_source_owner',text:'Waiting for source owner'}],
 finding_decision_log:[{action:'reject',decision_id:'d',ids:['kept'],cells:{kept:'Inspection!I2'},by:'Reviewer'}],...extra});
const reports={
 [A]:make(A,{fixed_from:{file_hash:A,filename:'SYNTHETIC-HMMA.csv',changes:[]},fixes_applied:[{fixed_file_hash:A,changes:[]}],auto_fixed:{from_hash:A,changed:[{sheet:'Inspection',cell:'I2',before:'',after:'N/A',reason:'Recorded cleanup'}]}}),
 [B]:make(B),[C]:make(C,{approval:{by:'Reviewer'},review: {...make(C).review,approval_current:true}}),[D]:make(D,{sent:[{sent:true,to:['synthetic@example.invalid']}]}),[E]:make(E,{sending:true})};
reports[A].source_reconciliation.stale=true;delete reports[B].source_reconciliation.bound_to;
const resolver=id=>({requested_hash:id,working_hash:id,status:id===A?'unavailable':id===B?'superseded':'current',unavailable_reason:id===A?'history_inconsistent':null,
 message:id===A?'Saved correction history is inconsistent. No working copy can be verified.':null,read_only_reason:{[C]:'approved',[D]:'sent',[E]:'sending'}[id]||null,history:[],choices:[]});
(async()=>{
 const requests=[],unexpected=[],snapshot=JSON.stringify(reports),server=http.createServer(async(req,res)=>{
  const url=new URL(req.url,'http://127.0.0.1');let raw='';for await(const chunk of req)raw+=chunk;const body=raw?JSON.parse(raw):null;requests.push({path:url.pathname,method:req.method,body,query:url.search});
  res.setHeader('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; font-src 'self'; img-src 'self' data:");
  const files={'/app':['text/html','web/app.html'],'/app.js':['text/javascript','web/app.js'],'/app.css':['text/css','web/app.css']};
  if(files[url.pathname]){const [mime,path]=files[url.pathname];res.setHeader('Content-Type',mime);return res.end(fs.readFileSync(path));}
  if(url.pathname.startsWith('/assets/')&&!url.pathname.includes('..')){const p='web'+url.pathname;if(fs.existsSync(p))return res.end(fs.readFileSync(p));res.writeHead(404);return res.end();}
  const send=data=>{res.setHeader('Content-Type','application/json');res.end(JSON.stringify(data));};
  if(url.pathname==='/api/health')return send({ai_review_configured:true,sharepoint_configured:true});
  if(url.pathname==='/api/me')return send({user:{name:'Synthetic reviewer',role:'admin',virtual:true}});
  if(url.pathname==='/api/clients')return send({clients:[]});
  if(url.pathname==='/api/spreadsheets')return send({spreadsheets:[],working_copies:true});
  if(url.pathname==='/api/spreadsheets/working-copy')return send(resolver(url.searchParams.get('id')));
  if(url.pathname==='/api/spreadsheet')return send(reports[url.searchParams.get('id')]);
  if(url.pathname==='/api/spreadsheets/sheet'){
   if(body.sheet==='Slow')await new Promise(r=>setTimeout(r,250));
   return send({sheet:body.sheet,width:9,height:1292,rows:Array.from({length:1292},(_,i)=>[body.sheet+' row '+(i+1),'','','','','','','','saved '+(i+1)])});
  }
  if(url.pathname==='/api/spreadsheets/download'){res.setHeader('Content-Disposition','attachment; filename="SYNTHETIC-HMMA.csv"');return res.end('SYNTHETIC exact bytes '+url.searchParams.get('id'));}
  if(url.pathname==='/favicon.ico'){res.writeHead(204);return res.end();}
  unexpected.push(url.pathname);res.writeHead(409);send({error:'Unexpected API call in read-only test'});
 });await new Promise(r=>server.listen(0,'127.0.0.1',r));
 const base='http://127.0.0.1:'+server.address().port,browser=await chromium.launch({headless:true,executablePath:process.env.CHROMIUM_EXECUTABLE||undefined}).catch(async error=>{await new Promise(r=>server.close(r));throw error;}),results=[];
 try{for(const width of [1200,390]){
  const context=await browser.newContext({viewport:{width,height:900}}),page=await context.newPage(),errors=[],external=[];
  await context.route('**/*',route=>{if(route.request().url().startsWith(base+'/'))return route.continue();external.push(route.request().url());return route.abort();});page.on('pageerror',e=>errors.push(String(e)));
  const open=async(hash,history=false)=>{await page.goto(base+'/app#r='+hash+(history?'&history=1':''),{waitUntil:'networkidle'});await page.locator('.version-readonly').waitFor();};
  await open(A);console.log(JSON.stringify({stage:'opened',width}));
  assert.equal(await page.getByRole('heading',{name:'Verification App',exact:true}).count(),1);assert.equal(await page.getByLabel('Key totals').count(),1);
  assert.match(await page.locator('.fd-readonly-notice').innerText(),/inconsistent/);assert.match(await page.locator('.fd-readonly-holds').innerText(),/Source capture incomplete/);
  assert.match(await page.getByLabel('Key totals').innerText(),/Unknown/);assert.equal(await page.getByText('blank is not zero',{exact:true}).isVisible(),true);assert.equal(await page.locator('.fd-finding-group').count(),5);assert.ok(await page.locator('[data-finding-id]').count()<100);
  const seen=new Set();
  for(const group of await page.locator('.fd-finding-group').all()){
   await group.locator('summary').focus();await page.keyboard.press('Enter');let pageIndex=0;
   while(true){const ids=await group.locator('[data-finding-id]').evaluateAll(nodes=>nodes.map(n=>n.dataset.findingId));assert.ok(ids.length<=25);ids.forEach(id=>seen.add(id));
    if(await group.getByRole('button',{name:'Next',exact:true}).isDisabled())break;
    await group.getByRole('button',{name:'Next',exact:true}).click();pageIndex++;assert.equal(await group.locator('.fd-history-range').evaluate(el=>el===document.activeElement),true);
   }
   const range=await group.locator('.fd-history-range').innerText();await group.getByRole('button',{name:'Collapse',exact:true}).click();assert.equal(await group.evaluate(el=>el.open),false);await page.keyboard.press('Enter');assert.equal(await group.locator('.fd-history-range').innerText(),range);
   if(pageIndex){await group.locator('[data-finding-id="k1289"]').getByRole('button',{name:'View saved location'}).click();await page.waitForFunction(()=>document.querySelector('.readonly-sheet td.focus')?.getAttribute('data-cell')==='I1291');}
   await group.getByRole('button',{name:'Collapse',exact:true}).click();
  }
  assert.equal(seen.size,1294);console.log(JSON.stringify({stage:'all_findings_reachable',width}));
  await page.getByLabel('Saved sheet').selectOption('Slow');await page.getByLabel('Saved sheet').selectOption('Other');await page.waitForTimeout(400);
  assert.equal(await page.getByLabel('Saved sheet').inputValue(),'Other');assert.match(await page.locator('.readonly-sheet').innerText(),/^.*Other row 1/s);assert.doesNotMatch(await page.locator('.readonly-sheet').innerText(),/Slow row/);
  await page.getByLabel('Saved sheet').selectOption('Slow');await page.getByRole('button',{name:'Sources',exact:true}).click();await page.waitForTimeout(400);
  assert.equal(await page.getByRole('button',{name:'Confirm source columns',exact:true}).count(),0);assert.equal(await page.getByRole('button',{name:'Compare again',exact:true}).count(),0);
  if(!await page.locator('.fd-comparison').evaluate(el=>el.open))await page.locator('.fd-comparison > summary').click();
  assert.equal(await page.getByRole('button',{name:'Refresh SharePoint only',exact:true}).isDisabled(),true);assert.match(await page.locator('.fd-utility').innerText(),/Source mapping is read-only/);assert.doesNotMatch(await page.locator('.fd-utility').innerText(),/Checking SharePoint|Waiting for SharePoint|Refreshing SharePoint/);
  await page.getByRole('button',{name:'History',exact:true}).click();assert.match(await page.locator('.fd-utility').innerText(),/Saved history could not be verified/);assert.match(await page.locator('.fd-auto').innerText(),/Recorded cell changes \(history unverified\)/);assert.doesNotMatch(await page.locator('.fd-auto').innerText(),/Applied cell changes|recorded applied change/);assert.equal(await page.getByRole('link',{name:'Open recorded source version (history unverified)',exact:true}).getAttribute('href'),'#r='+A+'&history=1');assert.equal(await page.getByRole('button',{name:'Undo',exact:true}).count(),0);
  await page.getByRole('button',{name:'Review',exact:true}).click();await page.getByLabel('Saved sheet').selectOption('Other');await page.waitForTimeout(100);
  await page.getByText('Exact saved version and check',{exact:true}).click();assert.match(await page.locator('.fd-exact-version').innerText(),new RegExp(A));
  const [download]=await Promise.all([page.waitForEvent('download'),page.getByRole('link',{name:'Download this original version',exact:true}).click()]);assert.equal(fs.readFileSync(await download.path(),'utf8'),'SYNTHETIC exact bytes '+A);
  assert.equal(await page.locator('#grid-edit-cell,#fd-open-review,#fd-preview-btn,.fd-mode-selector,#chat').count(),0);
  await page.locator('.readonly-sheet td[data-cell="I2"]').click();await page.keyboard.press('j');assert.equal(await page.locator('dialog[open]').count(),0);
  assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'no horizontal page overflow');
  if(process.env.READONLY_SCREENSHOT_DIR)await page.screenshot({path:process.env.READONLY_SCREENSHOT_DIR+'/readonly-'+width+'.png',fullPage:true});
  for(const [id,reason] of [[B,'Historical'],[C,'approved'],[D,'sent'],[E,'sending']]){
   await open(id,id===B);assert.match(await page.locator('.fd-readonly-notice').innerText(),new RegExp(reason));
   assert.equal(await page.getByRole('link',{name:'Download this original version',exact:true}).getAttribute('href'),'/api/spreadsheets/download?id='+id);
   await page.getByRole('button',{name:'History',exact:true}).click();assert.equal(await page.getByRole('button',{name:'Undo',exact:true}).count(),0);
   await page.getByRole('button',{name:'Sources',exact:true}).click();assert.equal(await page.getByRole('button',{name:'Confirm source columns',exact:true}).count(),0);assert.doesNotMatch(await page.locator('.fd-utility').innerText(),/Checking SharePoint|Waiting for SharePoint|Refreshing SharePoint/);
  }
  // A failed sheet is recoverable; late responses cannot cross report navigation.
  await open(A);let failOnce=true;
  await page.route('**/api/spreadsheets/sheet',route=>{if(failOnce&&route.request().postDataJSON().sheet==='Other'){failOnce=false;return route.fulfill({status:503,contentType:'application/json',body:'{"error":"Synthetic sheet unavailable"}'});}return route.continue();});
  await page.getByLabel('Saved sheet').selectOption('Other');await page.getByRole('button',{name:'Retry saved sheet',exact:true}).waitFor();
  assert.match(await page.locator('.readonly-sheet [role=alert]').innerText(),/Synthetic sheet unavailable/);await page.getByRole('button',{name:'Retry saved sheet',exact:true}).click();await page.getByLabel('Saved spreadsheet: Other (read-only)',{exact:true}).waitFor();
  await page.getByLabel('Saved sheet').selectOption('Slow');await page.evaluate(hash=>{location.hash='r='+hash+'&history=1'},B);await page.waitForTimeout(500);
  assert.equal(await page.getByRole('link',{name:'Download this original version',exact:true}).getAttribute('href'),'/api/spreadsheets/download?id='+B);assert.doesNotMatch(await page.locator('.readonly-sheet').innerText(),/Slow row/);
  await page.goBack();await page.waitForTimeout(500);assert.equal(await page.getByRole('link',{name:'Download this original version',exact:true}).getAttribute('href'),'/api/spreadsheets/download?id='+A);
  assert.deepEqual(errors,[]);assert.deepEqual(external,[]);results.push({width,findings_reachable:seen.size,exact_download:true,readonly_views:5,stale_sheet_ignored:true});await context.close();
 }}finally{await browser.close();await new Promise(r=>server.close(r));}
 assert.deepEqual(unexpected,[]);assert.equal(JSON.stringify(reports),snapshot);assert.ok(requests.filter(x=>x.method==='POST').every(x=>['/api/clients','/api/spreadsheets/sheet'].includes(x.path)));
 console.log(JSON.stringify({passed:true,results,api_calls:requests.filter(x=>x.path.startsWith('/api/')).length,mutations:0,ai_requests:0}));
})().catch(e=>{console.error(e);process.exitCode=1;});

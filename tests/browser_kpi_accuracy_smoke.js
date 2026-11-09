// Production KPI/Overview render functions, synthetic data, strict CSP, headless loopback only.
// Use installed PLAYWRIGHT_MODULE and CHROMIUM_EXECUTABLE; no installation required.
const fs=require('node:fs'),http=require('node:http'),assert=require('node:assert/strict');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright-core');
const source=fs.readFileSync('web/app.js','utf8'),unit=fs.readFileSync('tests/test_kpi_accuracy_ui.js','utf8');
const between=(s,a,b)=>s.slice(s.indexOf(a),s.indexOf(b,s.indexOf(a)));
const js=between(source,'  const h = (','  const store =')+`
const $=id=>document.getElementById(id),fmt=v=>v==null?'—':String(v),state={kpi:{days:90,client:'synthetic',job:''}};
const recsBlock=()=>null,changeRecords=()=>null;
`+between(unit,'const totals=','test(')+`
fixture.categories=[{kind:'Synthetic A',reason:'Synthetic A',count:8},{kind:'Synthetic B',reason:'Synthetic B',count:2}];
fixture.jobs=['synthetic-site|list-a|','synthetic-site|list-b|'].map(source=>({...totals,job:'Synthetic same job',source,source_label:source.endsWith('list-a|')?'Synthetic north':'Synthetic south',days:4}));
fixture.trend=[{...totals,day:'2026-11-01',inspected:null},{...totals,day:'2026-11-02',inspected:0},{...totals,day:'2026-11-03',inspected:13.125}];
const api=async()=>fixture;
`+between(source,'  const statisticNumber =','  function builtBanner(')+`
(location.hash==='#overview'?showOverview():showKpis()).then(()=>document.body.dataset.ready='yes');`;
(async()=>{
const requests=[],server=http.createServer((req,res)=>{
 requests.push(req.url);res.setHeader('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; font-src 'none'; connect-src 'none'");
 const files={'/':['text/html','<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><link rel="stylesheet" href="/app.css"><main id="report"></main><script src="/fixture.js"></script>'],'/app.css':['text/css',fs.readFileSync('web/app.css')],'/fixture.js':['text/javascript',js]};
 const f=files[req.url];if(req.method!=='GET'||!f){res.writeHead(403);return res.end();}res.setHeader('Content-Type',f[0]);res.end(f[1]);
});await new Promise(r=>server.listen(0,'127.0.0.1',r));
const base=`http://127.0.0.1:${server.address().port}`,browser=await chromium.launch({headless:true,executablePath:process.env.CHROMIUM_EXECUTABLE||undefined}),results=[];
try{for(const width of [1200,390])for(const mode of ['kpis','overview']){
 const context=await browser.newContext({viewport:{width,height:850}}),page=await context.newPage(),errors=[],external=[];
 await context.route('**/*',r=>{if(r.request().url().startsWith(base+'/'))return r.continue();external.push(r.request().url());return r.abort();});
 page.on('pageerror',e=>errors.push(String(e)));await page.goto(base+'/#'+mode);await page.waitForSelector('body[data-ready=yes]');
 const body=await page.locator('body').innerText();assert.match(body,/Findings on first uploads/);assert.match(body,/Known subtotal: 12.5/);assert.match(body,/Unknown/);assert.match(body,/Synthetic north/);assert.match(body,/Synthetic south/);assert.ok(!body.includes('synthetic-site|'));
 const exact=page.getByText('Exact source scope',{exact:true}).first();await exact.focus();await page.keyboard.press('Enter');assert.match(await page.locator('body').innerText(),/synthetic-site\|list-a/);
 const summary=page.getByText('Definitions and data coverage',{exact:true});await summary.focus();await page.keyboard.press('Enter');assert.equal(await summary.evaluate(e=>e.parentElement.open),true);assert.match(await page.locator('body').innerText(),/not source-verified totals/);
 const bars=await page.locator('.ovbar i').evaluateAll(es=>es.map(e=>({width:e.getBoundingClientRect().width,height:e.getBoundingClientRect().height,color:getComputedStyle(e).backgroundColor})));
 assert.equal(bars.length,2);assert.ok(bars[0].width>0&&bars[0].height>=10);assert.ok(Math.abs(bars[0].width/bars[1].width-4)<0.1,JSON.stringify(bars));
 if(mode==='kpis'){
  const chart=page.locator('svg[aria-label^="Inspected quantity"]');assert.equal(await chart.locator('.unknown').count(),1);assert.equal(await chart.locator('path').count(),1);assert.match(await chart.textContent(),/13\.125/);assert.match(await chart.textContent(),/Nov 2: 0/);
  await page.locator('tr.go').first().focus();assert.equal(await page.locator('tr.go').first().evaluate(e=>e===document.activeElement),true);
 }
 const layout=await page.evaluate(()=>({viewport:innerWidth,document:document.documentElement.scrollWidth,tables:[...document.querySelectorAll('.tablewrap')].map(e=>({width:e.clientWidth,scroll:e.scrollWidth}))}));
 assert.ok(layout.document<=width+1,JSON.stringify(layout));assert.equal(errors.length,0,errors.join('\n'));assert.equal(external.length,0);results.push({width,mode,bars,layout});await context.close();
}console.log(JSON.stringify({passed:true,results,requests},null,2));}finally{await browser.close();await new Promise(r=>server.close(r));}
})().catch(e=>{console.error(e);process.exitCode=1;});

const test = require('node:test'), assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm');
const src = fs.readFileSync('web/app.js', 'utf8');
const start = src.indexOf('  function fdCheckedState('), end = src.indexOf('  function fdCheckedCopy(', start);
function setup() {
  const report = {file_hash:'a'.repeat(64), check_hash:'b'.repeat(64), filename:'Synthetic.xlsx', checked_copy:{ready:true, file_hash:'a'.repeat(64), check_hash:'b'.repeat(64), expires_at:new Date(Date.now()+60000).toISOString()}};
  const log = [], ctx = vm.createContext({Date, Number, encodeURIComponent, state:{report}, readyChanges:()=>({total:0}), toast:x=>log.push(x), drawSide:()=>log.push('draw'), fdRefreshFoot:()=>{}, fetch:async()=>{throw Error('not mocked');}, URL:{createObjectURL:()=>{log.push('blob');return 'blob:x';},revokeObjectURL:()=>{}}, h:(tag,props)=>({click:()=>log.push(props),remove:()=>{}}), document:{body:{append:()=>{}}}, setTimeout:()=>1});
  vm.runInContext(src.slice(start,end),ctx); return {ctx, report, log};
}
test('only matching saved current proof with unexpired evidence and no pending changes qualifies',()=>{
 const {ctx,report}=setup();assert.equal(ctx.fdCheckedState(report).ready,true);
 for(const field of ['busy','rechecking','reviewing','verifying','decisionPending','gridDraft']){ctx.state[field]=true;assert.equal(ctx.fdCheckedState(report).ready,false);delete ctx.state[field];}
 ctx.readyChanges=()=>({total:1});assert.equal(ctx.fdCheckedState(report).ready,false);ctx.readyChanges=()=>({total:0});
 report.checked_copy.check_hash='wrong';assert.equal(ctx.fdCheckedState(report).ready,false);report.checked_copy.check_hash=report.check_hash;
 assert.equal(ctx.fdCheckedState(report,Date.now()+60001).ready,false);
});
test('409 is readable and never saved as XLSX',async()=>{
 const {ctx,report,log}=setup();ctx.fetch=async()=>({ok:false,json:async()=>({error:'Source evidence expired. Reload.'})});
 await ctx.fdDownloadChecked(report);assert.equal(report.checked_copy.ready,false);assert.ok(log.includes('Source evidence expired. Reload.'));assert.ok(!log.includes('blob'));
});
test('successful response is exact requested hash, never proof-provided alternate URL',async()=>{
 const {ctx,report,log}=setup();report.checked_copy.download='/api/spreadsheets/download?id=SIBLING';
 let url;ctx.fetch=async p=>(url=p,{ok:true,headers:{get:()=> 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'},blob:async()=>({})});
 await ctx.fdDownloadChecked(report);assert.equal(url,`/api/spreadsheets/download?id=${report.file_hash}&checked=1&check=${report.check_hash}`);assert.ok(log.includes('blob'));assert.equal(log.find(x=>x.download)?.download,'Synthetic.xlsx');
});
test('wrong content type, scope change and local expiry after response never download',async()=>{
 for(const mode of ['type','scope','expiry']){const {ctx,report,log}=setup();ctx.fetch=async()=>({ok:true,headers:{get:()=>mode==='type'?'application/json':'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'},blob:async()=>{if(mode==='scope')ctx.state.report={};if(mode==='expiry')report.checked_copy.expires_at='2000-01-01';return {};}});await ctx.fdDownloadChecked(report);assert.ok(!log.includes('blob'),mode);}
});

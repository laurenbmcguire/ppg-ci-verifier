const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync('web/app.js','utf8');
const start=source.indexOf('  function fdPresentationRow('),end=source.indexOf('  function fdRowValues(',start);
const ctx=vm.createContext({});vm.runInContext(source.slice(start,end),ctx);
test('unknown source day says item exists without declaring missing or another day',()=>{
 const text=ctx.fdDifferenceText({status:'source_day_unconfirmed',sheet:'Inspection Detail',row:4,item:'123'});
 assert.match(text,/exists in SharePoint/);assert.match(text,/business day could not be confirmed/);
 assert.doesNotMatch(text,/not in SharePoint|another day|dated nothing/);
});

test('confirmed wrong day and quantity mismatches retain specific error text',()=>{
 assert.match(ctx.fdDifferenceText({status:'not_on_sharepoint_for_day',item:'123'}),/not in SharePoint for this day/);
 assert.match(ctx.fdDifferenceText({status:'value_differs',item:'123',field:'inspected',report:80,sharepoint:72}),/80 in the report but 72 in SharePoint/);
});


const rowStart=source.indexOf('  function fdRowValues('),rowEnd=source.indexOf('  function fdRowLine(',rowStart);
test('row context uses actual table headers instead of preceding data',()=>{
 const context=vm.createContext({state:{report:{sheets:[{name:'Inspection Detail',tables:[{header_row:3}]}]},grid:{sheet:'Inspection Detail',rows:[['Title'],[],['Date','Part','Inspector','Inspected'],['2026-11-06','PartA','Alice',10],['2026-11-06','PartB','Bob',20]]}}});
 vm.runInContext(source.slice(rowStart,rowEnd),context);
 const text=context.fdRowValues('Inspection Detail',5);
 assert.match(text,/Date: 2026-11-06/);assert.match(text,/Part: PartB/);assert.match(text,/Inspector: Bob/);
 assert.doesNotMatch(text,/PartA:|Alice:|2026-11-06: 2026/);
 context.state.report.sheets=[];
 assert.match(context.fdRowValues('Inspection Detail',5),/Column 1: 2026/);
});

test('legacy exact unknown-date note is presentation-only; genuine wrong day remains a mismatch',()=>{
 const legacy={status:'not_on_sharepoint_for_day',item:'123',note:'On SharePoint the item is dated nothing (timezone unconfirmed).'};
 assert.match(ctx.fdDifferenceText(legacy),/business day could not be confirmed/);
 assert.equal(legacy.status,'not_on_sharepoint_for_day');
 assert.match(ctx.fdDifferenceText({...legacy,note:'On SharePoint the item is dated 2026-11-05 (business date).'}),/not in SharePoint for this day/);
});

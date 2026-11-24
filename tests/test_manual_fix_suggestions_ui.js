// Actual shipped fdItems; made-up state only. After a person fixes a report by hand, no suggested change is offered on it.
const test=require('node:test'),assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');
const source=fs.readFileSync('web/app.js','utf8'),between=(a,b)=>source.slice(source.indexOf(a),source.indexOf(b,source.indexOf(a)));
const fixes=[{id:'d5',sheet:'Summary',cell:'D5',before:'',after:'SL Alabama',source:'Known detail',reason:'"Customer" is blank.'},
             {id:'c31',sheet:'1st Shift',cell:'C31',before:'',after:'Main',source:'Suggested',reason:'Area is blank.'}];
const issue={key:'e1',fp:'f1',severity:'medium',code:'missing_value',sheet:'1st Shift',cell:'C31',text:'Area is blank here.'};
function items({changes=null,dec={}}={}){
 const report={file_hash:'a',review:{issues:[issue],dismissed:[]},review_decisions:{},fixed_from:changes?{changes}:null};
 const ctx=vm.createContext({state:{report,fixes},store:{get:(k,d)=>k==='dec'?dec:d},decisionsKey:()=>'dec',editsKey:()=>'edits',allIssues:rv=>rv.issues,
  fixCovers:(f,i)=>f.sheet===i.sheet&&String(f.cell).toUpperCase()===String(i.cell||'').toUpperCase(),fmtKind:()=>null,ASIDE:{},splitCell:c=>{const m=/^([A-Z]+)(\d+)$/.exec(String(c));return m?[m[1],Number(m[2])]:null},
  sameKey:i=>i.key,editId:(s,c)=>s+'!'+c,isSureFix:()=>false,reviewBinding:()=>({})});
 vm.runInContext(between('  function fdItems() {','  // Errors Fix with AI set aside')+between('  function fdOpenCounts(','  function fdRecordCount('),ctx);
 const all=ctx.fdItems();return {all,counts:{...ctx.fdOpenCounts(all)},words:ctx.fdOpenWords?null:null,state:k=>all.find(x=>x.key===k).state};
}
const plural=(n,s)=>n+' '+s+(n===1?'':'s');
test('on a report nobody has fixed by hand, a suggested change is still offered',()=>{
 const r=items();assert.equal(r.state('fix:d5'),'open');assert.deepEqual(r.counts,{open:2,errors:1,suggested:1});
 const ai=items({changes:[{sheet:'1st Shift',cell:'G18',source:'AI fix'}]});assert.equal(ai.state('fix:d5'),'open');   // a version Dex made keeps its suggestions
 const tidy=items({changes:[{sheet:'1st Shift',cell:'A1',source:'automatic cleanup'}]});assert.equal(tidy.state('fix:d5'),'open');
});
test('after a manual fix there are no suggested changes; listed errors are untouched',()=>{
 const r=items({changes:[{sheet:'1st Shift',cell:'G18',source:'Your edit'}]});
 assert.equal(r.state('fix:d5'),'rejected');                     // set aside, still listed under Ignored
 assert.equal(r.state('e1'),'open');                             // a listed error is never settled by this
 assert.deepEqual(r.counts,{open:1,errors:1,suggested:0});
 for (const src of ['Known detail','SharePoint','Shift times','Reverted (person)',undefined]) assert.equal(items({changes:[{sheet:'S',cell:'A1',source:src}]}).state('fix:d5'),'open',String(src));   // only a person's own edit counts
});
test('a suggestion the person chose to apply on that version is still theirs',()=>{
 const r=items({changes:[{sheet:'1st Shift',cell:'G18',source:'Your edit'}],dec:{d5:{state:'accepted'}}});
 assert.equal(r.state('fix:d5'),'selected');
});

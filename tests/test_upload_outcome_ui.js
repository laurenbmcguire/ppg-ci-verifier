// Production upload functions, synthetic bytes and network, no server or file upload.
const test=require('node:test'),assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');
const src=fs.readFileSync(process.env.PPG_UI_SOURCE||'web/app.js','utf8');
const HASH='a'.repeat(64), OTHER='b'.repeat(64);
function harness(options={}) {
 const nodes=[], calls=[], messages=[];
 const h=(tag,props={},...kids)=>{const n={tag,props,kids,style:{},textContent:'',classList:{add(){}},replaceChildren(...xs){this.kids=xs;},prepend(x){this.kids.unshift(x);}};nodes.push(n);return n;};
 const uploads=h('div');const item={file_hash:HASH,filename:'synthetic.xlsx'};
 const ctx=vm.createContext({h,$:()=>uploads,CHUNK:4,sha256:async()=>HASH,toBase64:async()=> 'synthetic',crypto:{randomUUID:()=> 'synthetic-id'},
  toast:x=>{messages.push(x);if(options.toastThrows)throw Error('UI unavailable');},
  api:async(path,body)=>{calls.push({path,body});if(path.endsWith('/part')){if(options.partFailure)throw Error('part unavailable');return {};}
   if(path.endsWith('/finish')){if(options.finishFailure)throw Error('answer lost');return {item:options.finishItem===undefined?item:options.finishItem,upload_receipt:options.receipt};}
   if(options.readFailure)throw Error('read unavailable');return options.readItem===undefined?item:options.readItem;
  }});
 const a=src.indexOf('  function uploadReceipt('),b=src.indexOf("  $('file-input').addEventListener",a);assert.ok(a>0&&b>a);vm.runInContext(src.slice(a,b),ctx);
 if(options.renderFailure)ctx.uploadReceipt=()=>{throw Error('renderer unavailable');};
 const text=n=>[n.textContent||'',...(n.kids||[]).map(k=>typeof k==='object'?text(k):String(k))].join(' ');
 return {ctx,calls,nodes,messages,uploads,text:()=>text(uploads),run:()=>ctx.uploadOne({name:options.filename||'synthetic.xlsx',size:7,slice:()=>({})})};
}
test('successful finish returns exact receipt and renders working saved-report link',async()=>{
 const e=harness(),r=await e.run();assert.equal(r.file_hash,HASH);assert.match(e.text(),/saved/);
 assert.equal(e.nodes.find(n=>n.tag==='a').props.href,'#r='+HASH);assert.equal(e.calls.filter(c=>c.path.endsWith('/finish')).length,1);
});
test('renderer failure after save never becomes nothing saved or a reupload',async()=>{
 const e=harness({renderFailure:true}),r=await e.run();assert.equal(r.file_hash,HASH);assert.match(e.text(),/Saved copy confirmed/);assert.doesNotMatch(e.text(),/Nothing was saved|upload it again/i);
});
test('toast failure after an idempotent saved receipt still returns the confirmed item',async()=>{
 const e=harness({toastThrows:true,finishItem:{file_hash:HASH,idempotent:true}});assert.equal((await e.run()).file_hash,HASH);assert.match(e.text(),/Saved copy confirmed/);
});
test('lost finish response reads exact bytes once and finds existing copy without retrying finish',async()=>{
 const e=harness({finishFailure:true});assert.equal((await e.run()).file_hash,HASH);assert.match(e.text(),/saved copy found/);
 assert.deepEqual(e.calls.filter(c=>c.path.startsWith('/api/spreadsheet?')).map(c=>c.path),['/api/spreadsheet?id='+HASH]);assert.equal(e.calls.filter(c=>c.path.endsWith('/finish')).length,1);
});
for(const options of [{finishFailure:true,readFailure:true},{finishFailure:true,readItem:null},{finishFailure:true,readItem:{file_hash:OTHER}},
 {finishItem:{file_hash:OTHER},readItem:{file_hash:OTHER}},{finishItem:null,readFailure:true}]){
 test('unconfirmed finish or mismatched receipt never claims failure/success or recommends retry '+JSON.stringify(options),async()=>{
  const e=harness(options);assert.equal(await e.run(),null);assert.match(e.text(),/outcome could not be confirmed/);assert.doesNotMatch(e.text(),/Nothing was saved|upload it again|Saved copy confirmed/);
  assert.equal(e.calls.filter(c=>c.path.endsWith('/finish')).length,1);
 });
}
test('wrong-hash finish receipt is not accepted; exact-hash readback may establish existing copy',async()=>{
 const e=harness({finishItem:{file_hash:OTHER}});assert.equal((await e.run()).file_hash,HASH);assert.match(e.text(),/saved copy found/);
});
test('part failure never sends finish or claims no earlier saved copy exists',async()=>{
 const e=harness({partFailure:true});assert.equal(await e.run(),null);assert.match(e.text(),/before the final save request/);assert.equal(e.calls.length,1);assert.doesNotMatch(e.text(),/Nothing was saved|upload it again/);
});

for(const filename of ['synthetic.pdf','synthetic.docx','synthetic.xlsx']) {
 const receipt={protocol:'ppg-upload-v1',input_sha256:HASH,stored_file_hash:OTHER,file_hash:OTHER};
 test('server-computed input-to-result binding confirms converted or formatted '+filename,async()=>{
  const e=harness({filename,finishItem:{file_hash:OTHER},receipt});assert.equal((await e.run()).file_hash,OTHER);assert.equal(e.calls.filter(x=>x.path.includes('?')).length,0);
 });
 test('wrong original digest never confirms transformed '+filename,async()=>{
  const e=harness({filename,finishItem:{file_hash:OTHER},receipt:{...receipt,input_sha256:OTHER},readItem:null});assert.equal(await e.run(),null);assert.match(e.text(),/could not be confirmed/);
 });
}
for(const filename of ['synthetic.pdf','synthetic.docx']) test('converted lost response stays unknown without pretending original hash is workbook id '+filename,async()=>{
 const e=harness({filename,finishFailure:true});assert.equal(await e.run(),null);assert.equal(e.calls.filter(x=>x.path.includes('?')).length,0);assert.match(e.text(),/could not be confirmed/);
});
test('receipt must bind exact returned converted artifact, not just original input',async()=>{
 const e=harness({filename:'synthetic.pdf',finishItem:{file_hash:OTHER},receipt:{protocol:'ppg-upload-v1',input_sha256:HASH,stored_file_hash:OTHER,file_hash:HASH}});assert.equal(await e.run(),null);
});

for(const filename of ['synthetic.pdf','synthetic.docx','synthetic.xlsx']) test('confirmed transformed render failure identifies the stored report, not uploaded bytes '+filename,async()=>{
 const e=harness({filename,renderFailure:true,finishItem:{file_hash:OTHER},receipt:{protocol:'ppg-upload-v1',input_sha256:HASH,stored_file_hash:OTHER,file_hash:OTHER}});
 assert.equal((await e.run()).file_hash,OTHER);assert.match(e.text(),new RegExp('Report ID: '+OTHER));assert.doesNotMatch(e.text(),new RegExp('Report ID: '+HASH));
});
test('unknown converted upload labels input hash as fingerprint, not report identity',async()=>{
 const e=harness({filename:'synthetic.pdf',finishFailure:true});assert.equal(await e.run(),null);assert.match(e.text(),new RegExp('Uploaded-file fingerprint: '+HASH));assert.doesNotMatch(e.text(),/File ID:|Report ID:/);
});

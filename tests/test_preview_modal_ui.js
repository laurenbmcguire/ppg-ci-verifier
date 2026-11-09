const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const source = fs.readFileSync('web/app.js', 'utf8');
const between = (a,b) => source.slice(source.indexOf(a),source.indexOf(b,source.indexOf(a)));
class Element {
 constructor(tag){this.tag=tag;this.children=[];this.listeners={};this.nodeType=1;this.open=false;}
 append(...x){this.children.push(...x.flat());}
 setAttribute(k,v){this[k]=v;}
 addEventListener(k,v){(this.listeners[k] ||= []).push(v);}
 fire(k){for(const fn of this.listeners[k]||[])fn({currentTarget:this});this['on'+k]?.({currentTarget:this});}
 focus(){}
 showModal(){this.open=true;}
 close(){this.open=false;this.fire('close');}
 remove(){this.removed=true;}
 querySelector(selector){return this.walk().find(x=>selector==='#'+x.id);}
 querySelectorAll(selector){return this.walk().filter(x=>x.tag===selector);}
 walk(){return this.children.flatMap(x=>x instanceof Element?[x,...x.walk()]:[]);}
}
function harness(){
 const body=new Element('body');
 const context=vm.createContext({document:{body,createElement:tag=>new Element(tag)},
   $:id=>body.walk().find(x=>x.id===id&&!x.removed),state:{busy:false},
   plural:(n,s)=>`${n} ${s}`,revisionOf:r=>r.revision||1,showWs:String,applyFixes(){throw Error('No apply expected')},toast(){}});
 vm.runInContext(between('  const h = (','  const store =')+between('  function fdPreview(c) {','  async function fdApprove('),context);
 return {context,body,dialogs:()=>body.walk().filter(x=>x.tag==='dialog'&&x.open&&!x.removed)};
}
test('refreshed click handler replaces original listener instead of stacking',()=>{
 const {context}=harness();
 vm.runInContext("var hits=0;var button=h('button',{onclick:()=>hits++});button.onclick=()=>hits+=10;button.fire('click');",context);
 assert.equal(context.hits,10);
});
test('preview is singleton, back closes once, repeated clicks and reopen stay single',()=>{
 const {context,dialogs}=harness();
 context.c={binding:{file_hash:'synthetic',expected_check_hash:'shown'},report:{revision:1},total:1,accepted:[{id:'f',sheet:'Synthetic',cell:'I18',before:'old',after:'shown'}],dec:{},edits:{}};
 for(let attempt=0;attempt<3;attempt++){
  vm.runInContext('fdPreview(c);fdPreview(c);',context);
  assert.equal(dialogs().length,1);
  const back=dialogs()[0].walk().find(x=>x.tag==='button'&&x.children.includes('Back to review'));
  back.fire('click');assert.equal(dialogs().length,0);
 }
 vm.runInContext('fdPreview(c)',context);dialogs()[0].close();assert.equal(dialogs().length,0,'native close/Escape needs no second dismissal');
});

test('AI preparation submits displayed binding and never selects or applies suggestions',async()=>{
 const calls=[],writes=[];
 const old={file_hash:'file',check_hash:'old'},fresh={file_hash:'file',check_hash:'fresh'};
 const ctx=vm.createContext({state:{report:old,selected:'file',aiOn:true},
  api:async(path,payload)=>{calls.push({path,payload});return {report:fresh,ai_fixes:{fixes:[{id:'guess'}]}}},
  fillPages:async r=>r,loadFixes:async()=>{},drawReport(){},toast(){},store:{set:(...args)=>writes.push(args)}});
 vm.runInContext(between('  async function runAiFixes(', '  const restoredKey'),ctx);
 await vm.runInContext("runAiFixes(false,{file_hash:'file',expected_check_hash:'old'})",ctx);
 assert.equal(calls.length,1);assert.equal(calls[0].path,'/api/spreadsheets/ai-fixes');
 assert.equal(calls[0].payload.expected_check_hash,'old');assert.equal(ctx.state.report.check_hash,'fresh');
 assert.deepEqual(writes,[],'no accepted/selected edits written');
 await vm.runInContext("runAiFixes(false,{file_hash:'file',expected_check_hash:'old'})",ctx);
 assert.equal(calls.length,1,'stale rendered preparation is not sent');
 ctx.state.aiOn=false;
 await vm.runInContext("runAiFixes(false,{file_hash:'file',expected_check_hash:'fresh'})",ctx);
 assert.equal(calls.length,1,'unconfigured preparation is unavailable');
});

test('preview visibly identifies sheet and cell and keeps removal control unbroken',()=>{
 const {context,dialogs}=harness();
 context.c={binding:{file_hash:'synthetic',expected_check_hash:'shown'},report:{revision:1},total:1,accepted:[{id:'f',sheet:'Synthetic sheet',cell:'I18',before:'old',after:'shown'}],dec:{},edits:{}};
 vm.runInContext('fdPreview(c)',context);
 const nodes=dialogs()[0].walk();
 assert.ok(nodes.some(x=>x.tag==='span'&&x.children.includes('Synthetic sheet')),'sheet name is visible, not only a tooltip');
 assert.ok(nodes.some(x=>x.tag==='b'&&x.children.includes('I18')&&x.className==='fd-preview-cell'));
 const remove=nodes.find(x=>x.tag==='button'&&x.children.includes('Remove'));
 assert.ok(remove.className.includes('fd-preview-remove'));
 const css=fs.readFileSync('web/app.css','utf8');
 assert.match(css,/\.fd-preview-cell\{[^}]*white-space:nowrap/);
 assert.match(css,/\.fd-prevlist \.fd-preview-remove\{[^}]*white-space:nowrap[^}]*min-width:max-content/);
});

test('assistant display is Dex while provider setup and provenance remain accurate',()=>{
 assert.ok(source.includes("btn('claude', 'Ask Dex'"));
 assert.ok(source.includes("'aria-label': 'Ask Dex'"));
 assert.ok(source.includes("'Message to Dex'"));
 assert.ok(source.includes("'Ask Dex about this report'"));
 assert.ok(source.includes("'Claude and ChatGPT connector'"));
 assert.ok(source.includes("'In Claude'"));
 assert.ok(source.includes("e.source === 'Claude' ? 'Claude (chat)'"));
 const sheets=fs.readFileSync('web/spreadsheet-ui.js','utf8');
 assert.ok(sheets.includes('Dex is reading the report'));
 assert.ok(!sheets.includes('Claude'));
});


test('Dex pane uses only the review workflow and cannot send or bulk-select',()=>{
 for (const configured of [false,true]) {
  const panel=new Element('section');panel.replaceChildren=(...children)=>{panel.children=children;};
  const writes=[],requests=[];
  const ctx=vm.createContext({document:{createElement:tag=>new Element(tag)},$:()=>panel,
   state:{report:{file_hash:'synthetic'},aiOn:configured},showSide(){},
   store:{get:()=>[{kind:'brief',content:'Want me to do all the fixes?',offer:['old']}],set:(...args)=>writes.push(args)},
   api:(...args)=>requests.push(args)});
  vm.runInContext(between('  const h = (','  const store =')+between('  function drawChat() {','  async function showCell('),ctx);
  vm.runInContext('drawChat()',ctx);
  const nodes=panel.walk(),text=nodes.flatMap(x=>x.children.filter(y=>typeof y==='string')).join(' ');
  assert.match(text,configured ? /Chat sending is unavailable/ : /AI preparation is not configured/);
  assert.ok(!text.includes('Want me to do all'));
  assert.equal(nodes.filter(x=>x.tag==='button'&&!Object.hasOwn(x,'disabled')).length,1);
  assert.ok(Object.hasOwn(nodes.find(x=>x.tag==='textarea'),'disabled'));
  assert.ok(Object.hasOwn(nodes.find(x=>x.tag==='button'&&x.children.includes('Send')),'disabled'));
  assert.deepEqual(writes,[]);assert.deepEqual(requests,[]);
 }
 for (const legacy of ['function applyChat(', 'function undoChat(', 'function doFixes(', 'function undoFixes(', "api('/api/spreadsheets/chat'", 'Go through them with me', 'Want me to do all the fixes?']) assert.ok(!source.includes(legacy),legacy);
});

test('completion and recommendation actions distinguish internal review from delivery',()=>{
 assert.ok(!source.includes('Next: approve it and send it to the client'));
 assert.ok(!source.includes("approve_send: {label: 'Send'"));
 assert.ok(source.includes("approve_send: {label: 'Internal approval details'"));
 assert.ok(source.includes('Request internal review (no notification sent)'));
 assert.ok(source.includes('Internal review requested. No notification was sent.'));
});
test('failed apply retains preview, busy click makes no request, and Escape is blocked only while saving',async()=>{
 const {context,dialogs}=harness();let calls=0;context.applyFixes=async()=>{calls++;return null;};
 const c={binding:{file_hash:'h',expected_check_hash:'c'},report:{file_hash:'h',revision:1},accepted:[{id:'p',sheet:'Synthetic',cell:'A2',before:'1',after:'2'}],dec:{},edits:{},total:1};
 context.fdPreview(c);const d=dialogs()[0],button=d.querySelector('#fd-apply');
 context.state.busy=true;await button.onclick({currentTarget:button});assert.equal(calls,0);assert.equal(dialogs().length,1);
 let prevented=false;d.listeners.cancel[0]({preventDefault(){prevented=true;}});assert.equal(prevented,true);
 context.state.busy=false;prevented=false;d.listeners.cancel[0]({preventDefault(){prevented=true;}});assert.equal(prevented,false);
 await button.onclick({currentTarget:button});assert.equal(calls,1);assert.equal(dialogs().length,1);assert.equal(button.disabled,false);assert.match(d.querySelector('#fd-preview-error').textContent,/Not confirmed saved/);
});

test('pending save disables Back and Remove; failed response restores controls and visible error even when event.currentTarget clears',async()=>{
 const {context,dialogs}=harness();let finish,calls=0,removed=0;context.fdRemovePreview=()=>removed++;
 context.applyFixes=()=>{calls++;return new Promise(resolve=>finish=resolve);};
 const c={binding:{file_hash:'h',expected_check_hash:'c'},report:{file_hash:'h',revision:1},accepted:[{id:'p',sheet:'Synthetic',cell:'A2',before:'1',after:'2'}],dec:{},edits:{},total:1};
 context.fdPreview(c);const d=dialogs()[0],apply=d.querySelector('#fd-apply'),back=d.walk().find(x=>x.tag==='button'&&x.children.includes('Back to review')),remove=d.walk().find(x=>x.tag==='button'&&x.children.includes('Remove'));
 const event={currentTarget:apply};const pending=apply.onclick(event);event.currentTarget=null;
 assert.equal(back.disabled,true);assert.equal(remove.disabled,true);assert.equal(apply.disabled,true);
 back.onclick();remove.onclick();await apply.onclick({currentTarget:apply});assert.equal(calls,1);assert.equal(removed,0);assert.equal(dialogs().length,1);
 let canceled=false;d.listeners.cancel[0]({preventDefault(){canceled=true;}});assert.equal(canceled,true,'local pending guard works before/without global busy');
 finish(null);await pending;
 assert.equal(dialogs().length,1);assert.equal(back.disabled,false);assert.equal(remove.disabled,false);assert.equal(apply.disabled,false);assert.match(d.querySelector('#fd-preview-error').textContent,/Not confirmed saved/);
 back.onclick();assert.equal(dialogs().length,0);
});

test('preview Remove accessible names start with visible label and distinguish identical cells on different sheets',()=>{
 const {context,dialogs}=harness();context.fdPreview({binding:{file_hash:'h',expected_check_hash:'c'},report:{file_hash:'h',revision:1},accepted:[{id:'a',sheet:'Synthetic North',cell:'A2',before:'1',after:'2'},{id:'b',sheet:'Synthetic South',cell:'A2',before:'1',after:'2'}],dec:{},edits:{},total:2});
 const buttons=dialogs()[0].walk().filter(x=>x.tag==='button'&&x.children.includes('Remove'));
 assert.deepEqual(buttons.map(x=>x['aria-label']),['Remove Synthetic North, cell A2 from preview','Remove Synthetic South, cell A2 from preview']);
});

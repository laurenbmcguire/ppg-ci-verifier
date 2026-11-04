'use strict';
let state, token, busy=false;
const mode=new URLSearchParams(location.search).get('mode')||'synthetic';
const query='?mode='+encodeURIComponent(mode);
const $=id=>document.getElementById(id);
const el=(tag,text,cls)=>{let e=document.createElement(tag);e.textContent=text;if(cls)e.className=cls;return e};
function message(text,error=false){$('message').textContent=text;$('message').className=error?'error':'';}
async function load(){const r=await fetch('/api/state'+query);const d=await r.json();if(!r.ok)throw Error(d.error);state=d.state;token=d.token;if(d.storage==='local-sqlite'){$('hosting-notice').classList.remove('banner-ok');$('hosting-notice').textContent='LOCAL SQLITE COPY. Saved work stays in this copy’s SQLite database on the configured local disk. It survives restarts only while that disk is retained. Existing database connections are not used. This computer only; no remote or phone access, user accounts, business approval or messages. Use synthetic test files only.'}render();}
function render(){
 const historical=state.mode==='historical',meta=state.metadata;document.body.dataset.mode=mode;
 $('dataset-banner').replaceChildren(el('strong',state.label||'SYNTHETIC FIXTURE — NOT A REAL CLIENT REPORT'),el('p',historical?'Verified September 29 source · Briggs & Stratton worksite · Schaefer supplier. Review extraction only; original XLSX retained unchanged. Simulated approval, not business signoff.':'Invented fixture only. Do not enter real client data in this shared demo workspace.'));
 $('cell-hint').textContent=historical?'Source cells: '+meta.sheet+' · rows 16–18. Duplicate IDs 14, 15, 15 are retained. This editable review extraction exports CSV; source workbook formulas and layout are not edited.':'Cell coordinates refer to SYNTHETIC_original.csv. Hover or focus to inspect original values.';
 document.querySelector('.identity').textContent=state.approval?state.approval.reviewer:'Unassigned · Simulated reviewer';
 $('version').textContent=`v${state.revision} · ${state.hash.slice(0,8)}`;$('status').textContent=state.status.replace('_',' ');$('count').textContent=state.checks.length;
 $('validation').textContent=state.validated===state.hash?'This exact version was revalidated.':'Revalidate this version before approval.';
 $('approve').disabled=state.validated!==state.hash||state.checks.some(x=>x.kind==='blocking')||!!state.approval;
 $('generate').disabled=!state.approval;
 const grid=$('grid');grid.replaceChildren();const head=document.createElement('tr');['Source row',...(state.headers||['Client','Period','Topic','Prior volume','Volume'])].forEach(x=>head.append(el('th',x)));grid.append(head);
 state.rows.forEach((row,i)=>{const tr=document.createElement('tr');tr.append(el('td',String(historical?i+16:i+2)));row.forEach((value,j)=>{const cell=state.coordinates?.[i]?.[j]||`${String.fromCharCode(65+j)}${i+2}`,td=document.createElement('td'),input=document.createElement(historical&&[2,4,5].includes(j)?'textarea':'input'),flag=state.checks.find(x=>x.cell===cell);if(flag)td.className=flag.kind==='blocking'?'blocked':'flag';input.value=value;input.setAttribute('aria-label',cell);input.title=`Source: ${historical?meta.sheet:'SYNTHETIC_original.csv'}!${cell}\nOriginal: ${state.original[i][j]}\nWorking: ${value}`;const save=()=>{if(input.value!==value)act('edit',{row:i,col:j,value:input.value})};input.addEventListener('change',save);input.addEventListener('blur',save);input.addEventListener('input',()=>{$('approve').disabled=true;$('generate').disabled=true;$('reviewed').checked=false;$('status').textContent='Unsaved edit — tab out to save';});td.append(input);tr.append(td)});grid.append(tr)});
 renderFinding();
 $('provenance').textContent=JSON.stringify({source:state.source_path||'original/SYNTHETIC_original.csv',metadata:state.metadata,working_snapshot:state.working_path,source_sha256:state.source_hash,working_sha256:state.hash,revision:state.revision,approved:state.approval},null,2);
 $('simulation').textContent=JSON.stringify({request:state.request,approved_artifact:state.approval?.path||null,simulated_report:state.simulated_report,published:false},null,2);
 $('audit').replaceChildren(...state.audit.slice().reverse().map(a=>{let d=el('div','', 'audit-row');d.append(el('span',a.at),el('strong',a.event),el('pre',JSON.stringify(a.detail)));return d}));
}
async function act(action,extra={}){
 if(busy)return;busy=true;let fatal=false;
 document.querySelectorAll('#grid input, #grid textarea, [data-action], #reviewer, #reviewed').forEach(e=>e.disabled=true);
 const revision=state.revision;
 try{
  const r=await fetch(`/api/${action}${query}`,{method:'POST',headers:{'Content-Type':'application/json','X-Review-Token':token},body:JSON.stringify({revision,role:'reviewer',reviewer:$('reviewer').value,reviewed:$('reviewed').checked,...extra})});
  if(!r.ok){const d=await r.json();throw Error(d.error)}
  if(action==='download'){const blob=await r.blob(),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download=`UNAPPROVED_${mode}_private-copy.csv`;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000)}
  if(action==='edit'||action==='leave')$('reviewed').checked=false;
  await load();message(action==='download'?'Private UNAPPROVED copy downloaded. Nothing was published.':`Completed: ${action}. Saved to the private demo workspace. All actions are simulated.`);
 }catch(e){message(e.message,true);try{await load()}catch(err){fatal=true;document.querySelectorAll('button').forEach(b=>b.disabled=true);message(err.message,true)}}finally{busy=false;if(!fatal){document.querySelectorAll('[data-action], #reviewer, #reviewed').forEach(e=>e.disabled=false);render()}}
}

let findingIndex=0, findingVersion='', findingMarks={};
function renderFinding(){
 const binding=JSON.stringify([mode,state.source_hash,state.revision,state.hash]);
 if(binding!==findingVersion){findingVersion=binding;findingIndex=0;findingMarks={};try{findingMarks=JSON.parse(sessionStorage.getItem('ci-findings:'+binding)||'{}')}catch{}}
 const count=state.checks.length;findingIndex=Math.min(findingIndex,Math.max(0,count-1));
 const f=state.checks[findingIndex],key=JSON.stringify(f);
 $('finding-position').textContent=count?`Finding ${findingIndex+1} of ${count}`:'No findings';
 $('findings').replaceChildren();
 if(f){const d=el('div','', 'finding');d.append(el('b',`${f.cell} · ${f.kind}`),el('span',f.message));$('findings').append(d)}else $('findings').textContent='Human review is still required.';
 $('finding-status').textContent=f?(findingMarks[key]==='accepted'?'✓ Done — accepted for review; value unchanged':findingMarks[key]==='ignored'?'Ignored — value unchanged':'Not reviewed'):'';
 ['finding-ignore','finding-accept'].forEach(id=>$(id).disabled=!f);
 $('finding-next').disabled=count<2;
}
function markFinding(status){
 if(busy||!state.checks[findingIndex])return;
 findingMarks[JSON.stringify(state.checks[findingIndex])]=status;
 try{sessionStorage.setItem('ci-findings:'+findingVersion,JSON.stringify(findingMarks))}catch{}
 renderFinding();
}
$('finding-ignore').addEventListener('click',()=>markFinding('ignored'));
$('finding-accept').addEventListener('click',()=>markFinding('accepted'));
$('finding-next').addEventListener('click',()=>{findingIndex=(findingIndex+1)%state.checks.length;renderFinding()});

document.querySelectorAll('[data-action]').forEach(b=>b.addEventListener('click',()=>act(b.dataset.action)));
load().catch(e=>{message(e.message,true);document.querySelectorAll('button').forEach(b=>b.disabled=true)});

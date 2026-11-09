'use strict';
(() => {
 const node=id=>document.getElementById(id);
 const period=scope=>scope.report_granularity==='daily'?'Daily report · all member shifts':`Shift ${scope.shift_id}`;
 const make=(tag,text,cls)=>{const e=document.createElement(tag);e.textContent=text;if(cls)e.className=cls;return e};
 let selected=null,pending=false,generation=0,nextCursor=null,developmentDemo=false;
 const message=(text,error=false)=>{node('workflow-message').textContent=text;node('workflow-message').className=error?'intake-error':''};
 function lock(value){pending=value;['workflow-file','workflow-sample','workflow-refresh','workflow-more'].forEach(id=>node(id).disabled=value);node('workflow-list').querySelectorAll('button').forEach(e=>e.disabled=value);node('workflow-detail').querySelectorAll('button,input').forEach(e=>e.disabled=value||e.dataset.locked==='true')}
 async function api(path,payload){const r=await fetch(path,payload===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json','X-Review-Token':'same-origin'},body:typeof payload==='string'?payload:JSON.stringify(payload)});const d=await r.json();if(!r.ok)throw Error(d.error||'The action could not be completed.');return d}
 function draw(result){
  const target=node('workflow-detail');target.replaceChildren();
  const demo=developmentDemo&&result.development_demo===true&&result.demo_eligible===true;
  if(demo)target.append(make('p','DEVELOPMENT SIMULATION ONLY · Accept / Reject / Hold records demo decisions, not authenticated human approval. Release remains held.','intake-error'));
  const binding={manifest_hash:result.manifest_hash,report_hash:result.report_hash,source_hash:result.source_hash};
  target.append(make('h3',`${result.report_id} · version ${result.version}`),make('div',result.finding_count?'Blocked · evidence or corrections needed':'Matches supplied snapshot · approval still required','result-badge '+(result.finding_count?'unable_to_verify':'matched_supplied_snapshot')));
  target.append(make('p',`${result.scope.job_id} · ${result.scope.report_date} · ${period(result.scope)}`));
  if(!result.current_version||!result.current_source)target.append(make('p','A newer report or source snapshot is present. This historical version cannot be corrected or approved.','intake-error'));
  target.append(make('p',`Supplied source is not independently authenticated. ${result.approval_effective?'Authenticated private-draft approval recorded.':'Human approval remains required.'} Customer release is held.`,'hint'));
  const scope=document.createElement('details');scope.append(make('summary','Exact source scope and hashes'),make('pre',JSON.stringify({scope:result.scope,manifest_hash:result.manifest_hash,report_hash:result.report_hash,source_hash:result.source_hash,recipient_mapping_version:result.recipient_mapping_version,approval:result.approval},null,2)));target.append(scope);
  if(result.scope.report_granularity==='daily'){
   target.append(make('h3','Shift breakdown'),make('p',result.shift_coverage_complete?'Shift coverage is asserted complete by supplied evidence.':'Shift coverage is unconfirmed. This daily draft may contain unfinished or missing shifts.','hint'));
   Object.entries(result.report_shift_breakdown||{}).forEach(([shift,item])=>{const box=make('div','','report-finding');box.append(make('strong',`Shift ${shift} · ${item.record_count} records`));Object.entries(item.totals).forEach(([name,value])=>box.append(make('p',`${name}: ${value===null?'Unknown':value}`)));const member=(result.member_shifts||[]).find(m=>m.scope.shift_id===shift);if(member)box.append(make('small',`Completion: ${member.shift_complete===true?'confirmed':member.shift_complete===false?'unfinished':'unknown'} · Source captured ${member.captured_at}`));target.append(box)});
  }
  target.append(make('h3',`Findings (${result.finding_count})`));
  result.findings.forEach(f=>{const box=make('div','','report-finding');box.append(make('strong',f.code),make('p',f.message),make('small',[f.side,f.record_id,f.field].filter(Boolean).join(' · ')));target.append(box)});
  if(result.findings_truncated)target.append(make('p','Showing the first 200 findings. Aggregate counts remain in the evidence details.','hint'));
  target.append(make('h3',`Correction proposals (${result.proposal_count})`));
  if(!result.proposals.length)target.append(make('p','No actionable proposals. Missing or uncertain source evidence must be resolved first; no values are filled in automatically.'));
  const versionLabel=make('label','New private draft version');const version=document.createElement('input');version.type='text';version.maxLength=200;version.value=result.version+'-review';version.setAttribute('aria-label','New private draft version');versionLabel.append(version);target.append(versionLabel);
  const selections=[];const blocked=!result.current_version||!result.current_source||(developmentDemo&&!demo);
  if(developmentDemo&&!demo)target.append(make('p','Development decisions require an explicitly synthetic sample manifest.','intake-error'));
  async function apply(ids){if(pending||selected!==result.manifest_hash)return;if(!version.value.trim()){message('Enter a new draft version.',true);return}await operation(async()=>{const response=await api(demo?'/api/workflow/demo/apply':'/api/workflow/apply',demo?{...binding,proposal_ids:ids,new_version:version.value.trim()}:{manifest_hash:result.manifest_hash,proposal_ids:ids,new_version:version.value.trim()});selected=response.item.manifest_hash;draw(response.item);await refresh();message(demo?'DEMO ONLY: selected demo-approved proposals applied to a new private draft. Originals unchanged. Human approval still required; release held.':'Selected proposals applied to a new private draft. Original report and source are unchanged. Approval did not carry forward.')})}
  async function decideSelected(ids,decision){if(!ids.length){message('Select at least one proposal.',true);return}if(pending||selected!==result.manifest_hash)return;await operation(async()=>{const response=await api('/api/workflow/demo/decisions',{...binding,proposal_ids:ids,decision});draw(response.item);message('DEMO ONLY: selected proposal decisions recorded. Selections cleared. Human approval still required; release held.')})}
  async function decide(proposal,decision){if(pending||selected!==result.manifest_hash)return;await operation(async()=>{const response=await api('/api/workflow/demo/decision',{...binding,proposal_id:proposal.id,decision});draw(response.item);message(`DEMO ONLY: ${decision} recorded for this exact proposal. No human identity or release approval claimed.`)})}
  result.proposals.forEach(p=>{
   const decision=(result.demo_decisions||[]).find(d=>d.proposal_id===p.id);
   const approved=decision&&decision.decision==='approve';const cannotApply=blocked||(demo&&!approved);
   const box=make('div','','report-finding');const label=make('label',`${p.operation} · ${p.operation.startsWith('set_shift_')?'shift '+p.record_id:p.record_id||'totals'} · ${p.field||'record'}`);
   const checkbox=document.createElement('input');checkbox.type='checkbox';checkbox.disabled=blocked;checkbox.dataset.locked=String(blocked);checkbox.setAttribute('aria-label','Select proposal '+p.operation+' '+(p.record_id||'totals')+' '+(p.field||''));label.prepend(checkbox);selections.push({checkbox,id:p.id,approved});
   box.append(label,make('pre',JSON.stringify({before:p.before,after:p.after},null,2)));
   if(demo){box.append(make('p','Demo decision: '+(decision?decision.decision:'not reviewed')+(blocked?' (stale)':''),'hint'));['approve','deny','hold'].forEach(choice=>{const b=make('button','Demo '+({approve:'Accept',deny:'Reject',hold:'Hold'}[choice]));b.disabled=blocked;b.dataset.locked=String(blocked);b.addEventListener('click',()=>decide(p,choice));box.append(b)})}
   const button=make('button',demo?'Apply demo-approved proposal to new draft':'Apply this proposal to new draft');button.disabled=cannotApply;button.dataset.locked=String(cannotApply);button.addEventListener('click',()=>apply([p.id]));box.append(button);target.append(box)
  });
  if(demo&&result.proposals.length){[['approve','Demo Accept selected'],['deny','Demo Reject selected'],['hold','Demo Hold selected']].forEach(([choice,label])=>{const b=make('button',label);b.disabled=blocked;b.dataset.locked=String(blocked);b.addEventListener('click',()=>decideSelected(selections.filter(s=>s.checkbox.checked&&!s.checkbox.disabled).map(s=>s.id),choice));target.append(b)})}
  if(result.proposals.length){const bulk=make('button',demo?'Apply selected demo-approved proposals to new draft':'Apply selected proposals to new draft');bulk.disabled=blocked;bulk.dataset.locked=String(blocked);bulk.addEventListener('click',()=>{const ids=selections.filter(s=>s.checkbox.checked&&!s.checkbox.disabled).map(s=>s.id);if(!ids.length){message('Select at least one eligible proposal.',true);return}if(demo&&selections.some(s=>s.checkbox.checked&&!s.approved)){message('Every selected proposal must be demo-accepted before applying. Reject, hold, or unreviewed selections cannot apply.',true);return}apply(ids)});target.append(bulk)}
  if(result.proposals_truncated)target.append(make('p','Showing the first 500 proposals. Apply an explicitly selected batch, then review the new version for remaining proposals.','hint'));
  const approve=make('button','Human approval unavailable until reviewer identity is connected');approve.disabled=true;approve.dataset.locked='true';target.append(approve);
  const evidence=document.createElement('details');evidence.append(make('summary','Validation counts and review history'),make('pre',JSON.stringify({finding_counts:result.finding_counts,report_totals:result.report_totals,source_totals:result.source_totals,report_shift_breakdown:result.report_shift_breakdown,source_shift_breakdown:result.source_shift_breakdown,current_member_sources:result.current_member_sources,events:result.events},null,2)));target.append(evidence);
 }
 // Serialize navigation, file reads and mutations. No older response can replace newer evidence.
 async function operation(work){if(pending)return;const request=++generation;lock(true);try{await work(request)}catch(e){if(request===generation)message(e.message,true)}finally{if(request===generation)lock(false)}}
 async function select(hash){return operation(async()=>{const result=await api('/api/workflow/version?hash='+encodeURIComponent(hash));selected=hash;draw(result)})}
 async function refresh(append=false){const d=await api('/api/workflow'+(append&&nextCursor?'?before='+nextCursor:''));const list=node('workflow-list');if(!append)list.replaceChildren();if(!d.reports.length&&!append)list.append(make('p','No generated report manifests imported yet.','empty-state'));d.reports.forEach(r=>{const b=make('button','','report-card');b.disabled=pending;b.append(make('strong',`${r.report_id} · v${r.version}`),make('span',`${r.scope.job_id} · ${period(r.scope)} · ${r.scope.report_date}`),make('small',`${r.finding_count} findings · ${r.current_version&&r.current_source?'current evidence':'historical evidence'}`));b.addEventListener('click',()=>select(r.manifest_hash));list.append(b)});nextCursor=d.next_cursor;node('workflow-more').hidden=!nextCursor}
 async function ingest(manifest){const d=await api('/api/workflow/import',manifest);selected=d.item.manifest_hash;draw(d.item);await refresh();message(d.item.idempotent?'This exact report version was already imported.':'Report version stored. Findings and proposals are ready; nothing was sent or released.')}
 const isSpreadsheet=async file=>{if(/\.(csv|xlsx|xlsm|xls|tsv)$/i.test(file.name))return true;const head=new Uint8Array(await file.slice(0,8).arrayBuffer());return head[0]===0x50&&head[1]===0x4b};
 node('workflow-file').addEventListener('change',async e=>{const file=e.target.files[0];node('workflow-file').value='';if(!file||pending)return;
  // Lock before the first asynchronous byte read, including format detection and handoff.
  return operation(async()=>{
   if(await isSpreadsheet(file)&&window.ciUploadSpreadsheet){message(`"${file.name}" is a spreadsheet, so it was sent to "Check a spreadsheet" above.`);await window.ciUploadSpreadsheet(file);return}
   if(file.size>2*1024*1024)throw Error('Use a manifest no larger than 2 MiB.');await ingest(await file.text())
  })});
 node('workflow-sample').addEventListener('click',()=>operation(async()=>ingest(await api('/api/workflow/sample'))));
 node('workflow-refresh').addEventListener('click',()=>operation(async()=>{await refresh();if(selected)draw(await api('/api/workflow/version?hash='+encodeURIComponent(selected)));message('Report versions and selected evidence refreshed.')}));
 node('workflow-more').addEventListener('click',()=>operation(()=>refresh(true)));
 operation(async()=>{const health=await api('/api/health');developmentDemo=health.development_demo===true;await refresh()});
})();

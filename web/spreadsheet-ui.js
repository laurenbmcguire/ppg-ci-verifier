'use strict';
(() => {
  const node = id => document.getElementById(id);
  const make = (tag, text, cls) => { const e = document.createElement(tag); e.textContent = text; if (cls) e.className = cls; return e; };
  const explanations = {
    formula_not_evaluated: 'This cell contains a formula. It was retained but never executed or recalculated.',
    csv_formula_injection_risk: 'Formula-like CSV text may execute when opened in a spreadsheet app. Review it before opening or distributing this file.',
    blank_header: 'This column has no header, so its meaning is unclear.',
    header_whitespace: 'This header has leading or trailing whitespace.',
    duplicate_header: 'This header appears more than once. No automatic column mapping was made.',
    inconsistent_row_width: 'This row has a different number of fields from the header.',
    missing_cell: 'This cell is blank or missing. Confirm whether that is intentional.',
    invalid_numeric_value: 'A conventional count or quantity column contains a value that is not a finite number.',
    negative_count_or_quantity: 'A count or quantity is negative. Confirm whether that is allowed.',
    fractional_count: 'A count is fractional rather than a whole number.',
    total_mismatch: 'The stated total differs from the sum of earlier rows in this column.',
    inspected_count_mismatch: 'Inspected does not equal accepted plus rejected in this row.',
    rows_after_total_not_reconciled: 'Data appears after a Total row. The totals layout needs manual review.',
    empty_sheet: 'This sheet is empty.',
    no_data_rows: 'This sheet has headers but no data rows.',
    no_quantity_mapping: 'No supported quantity columns were mapped. This sheet is unverified, not passed.',
    known_template_layout_differs: 'Recognized as a known report type, but the layout differs from the exact template, so columns were detected automatically. Check the detected columns.',
    no_quantity_columns_detected: 'No Inspected / Certified / Rejected style headers were found on this sheet, so it was not quantity-checked.',
    quantity_formulas_not_evaluated: 'Some quantity cells contain formulas. They were not evaluated, so those values are unknown rather than zero.',
    number_stored_as_text: 'A number is stored as text. It was read as a number, but Excel may not include it in sums.',
    rejected_exceeds_inspected: 'More parts were rejected than inspected in this row.',
    formula_results_used: 'Some quantities are formulas; the result Excel last saved was checked. Re-save the file in Excel if it was edited elsewhere.',
    duplicate_row: 'This row repeats an earlier row exactly.',
    duplicate_identifier: 'The same identifier (scan / lot) is listed more than once.',
    possible_identifier_typo: 'This identifier differs from another only by a look-alike character or prefix — likely a typo.',
    identifier_format_differs: 'This identifier is formatted differently from the others (e.g. a letter where digits are expected).',
    part_format_differs: 'This part number is formatted differently from the others.',
    possible_part_number_typo: 'This part number is one character off a part number used throughout the report — likely a typo.',
    possible_defect_typo: 'This defect / reject reason looks like a misspelling of a reason used elsewhere.',
    possible_condition_typo: 'This condition value looks like a misspelling of one used elsewhere.',
    possible_auditor_typo: 'This auditor name looks like a misspelling of one used elsewhere.',
    extra_spaces: 'A value has extra spaces.',
    missing_part_number: 'A row with quantities has no part number.',
    missing_identifier: 'A row with quantities has no identifier.',
    missing_defect_reason: 'Parts were rejected but no defect / reject reason is given.',
    condition_conflicts_with_quantity: 'The OK/NOK condition does not agree with the rejected quantity.',
    invalid_date: 'The date is not a valid date.',
    date_differs_from_report: 'The row date differs from the report date.',
    future_date: 'The date is in the future.',
    invalid_shift: 'The shift value is not a recognizable shift.',
    shift_differs_from_sheet: 'The row shift does not match the shift sheet it is on.',
    totals_block_mismatch: 'A totals box does not equal the sum of the rows on its sheet.',
    summary_mismatch: 'A Summary figure does not equal the rows on the sheet it summarizes.',
    reject_log_mismatch: 'The reject log disagrees with the shift rows for this identifier.',
    reject_log_row_not_in_detail: 'The reject log lists an identifier that is not in any shift rows.',
    reject_log_total_mismatch: 'The reject log total disagrees with the shift rows.',
    external_reference_formula: 'A formula refers to another workbook or a web/network location. The stored value was checked; the link was never opened, so that value may be stale.',
    web_function_formula: 'A formula uses a web function (WEBSERVICE, IMAGE or FILTERXML). It was never run; check that the stored result is current.',
    hyperlink_formula: 'A HYPERLINK formula is present. Links are recorded only and never opened.',
    dde_or_macro_call_formula: 'A formula uses DDE or a macro-call function (e.g. cmd|, CALL, RUN, EXEC). It was never run. Treat this workbook with care before opening it in Excel.',
    external_hyperlink: 'The sheet contains web or file hyperlinks. Links are recorded only and never opened.',
    external_workbook_link: 'The workbook links to another workbook. Values from that link were not refreshed or checked against the linked file.',
    external_data_connection: 'The workbook has a data connection or query (e.g. a database or web query). It was never refreshed; checks use the stored values.',
    dde_link: 'The workbook contains a DDE link to another program. It was never run. Treat this workbook with care before opening it in Excel.'
  };
  let pending = false, selected = null, selectionVersion = 0, detailVersion = 0, developmentDemo = false;
  function setBusy(value){pending=value;if(value)++selectionVersion;node('spreadsheet-file').disabled=value;node('spreadsheet-refresh').disabled=value;node('spreadsheet-list').querySelectorAll('button').forEach(button=>button.disabled=value);node('spreadsheet-detail').querySelectorAll('input').forEach(input=>input.disabled=value);node('spreadsheet-detail').querySelectorAll('button').forEach(button=>button.disabled=value);node('development-examples')?.querySelectorAll('button').forEach(button=>button.disabled=value)}
  const message = (text, error = false) => { node('spreadsheet-message').textContent = text; node('spreadsheet-message').className = error ? 'intake-error' : ''; };
  function detail(result) {
    const detailTicket=++detailVersion;
    const target = node('spreadsheet-detail'); target.replaceChildren();
    target.append(make('h3', `${result.filename} · version ${result.version}`), make('div', result.sharepoint&&result.sharepoint.status==='match'?'Matches SharePoint · release held':result.sharepoint&&result.sharepoint.status==='differences'?'Differs from SharePoint · release held':result.source_reconciliation_status==='mismatch'?'Source mismatch · release held':result.mapping_mode==='adaptive'?'Columns detected automatically · release held':result.template_id?(result.template_mapping_status==='incomplete'?'Template mapping incomplete · release held':'Mapped template checks · release held'):'Internal checks only · source not verified', 'result-badge unable_to_verify'), make('p', 'Original retained unchanged. These findings are not independent verification, approval, or permission to release a report.', 'hint'));
    { const n = possibleErrors(result).filter(i => i.severity !== 'info').length;
      const open = make('button', n ? `Review possible errors (${n})` : 'Review possible errors', n ? 'primary errors-open' : 'errors-open');
      open.type = 'button'; open.addEventListener('click', () => showErrorsPopup(result)); target.append(open); }
    target.append(baselinePanel(result));
    target.append(aiPanel(result));
    if(!result.template_id)target.append(make('p',`${result.numeric_columns_checked||0} supported quantity columns checked. ${result.internal_check_status==='unverified'?'No complete supported mapping: this is unverified.':'Only the documented internal checks were performed.'}`));
    if(result.mapping_mode==='adaptive'){
      if(result.recognized_template)target.append(make('p','This looks like a known PPG/Pratt report with a different layout, so the columns were detected automatically.','hint'));
      const used=result.formula_results_used||0,unknown=Math.max(0,(result.formula_count||0)-used);
      target.append(make('p',`${result.record_count} data rows found · ${result.numeric_cells_checked} numbers checked${used?` (${used} from formulas, using the result Excel saved)`:''}${unknown?` · ${unknown} formula cell(s) had no saved result (unknown, not zero)`:''}.`));
      const columns=make('div','','report-finding');columns.append(make('strong','Detected columns — please confirm'));
      const names={inspected:'Inspected',certified:'Certified / accepted',rejected:'Rejected'};
      (result.sheets||[]).forEach(sheet=>{
        if(!(sheet.tables||[]).length){columns.append(make('p',`${sheet.name}: no quantity columns found (not checked).`,'hint'));return}
        sheet.tables.forEach(table=>columns.append(make('p',`${sheet.name}, header row ${table.header_row}: `+Object.entries(table.columns).map(([field,c])=>`${names[field]||field} = column ${c.column} “${c.header}”`).join(' · ')+` — ${table.data_row_count} row${table.data_row_count===1?'':'s'}`)));
      });
      target.append(columns);
      target.append(sharepointPanel(result));
      const t=result.totals||{};target.append(make('p',['inspected','certified','rejected'].filter(f=>t[f]).map(f=>`${names[f]}: ${t[f].total!==null?t[f].total:`at least ${t[f].known_sum} (some values unknown)`}`).join(' · ')));
    }
    else if(result.template_id){
      target.append(make('p',`${result.record_count} ${result.template_id.startsWith('pratt-kn-')?'mapped audit-detail rows':'detail records'} · ${result.numeric_cells_checked} numeric cells checked · ${result.formula_count} formulas preserved without execution.`));
      if(result.source_sha256)target.append(make('p',`${result.numeric_values_compared} known ${result.template_id.startsWith('pratt-kn-')?'numeric/audit':'quantity'} values compared to ${result.source_provenance_kind==='independently_retrieved'?'a separately retrieved source snapshot':'retained historical source evidence'}. Source authentication and human approval remain unconfirmed.`,'hint'));
      (result.source_differences||[]).forEach(d=>{const box=make('div','','report-finding');box.append(make('strong',`${d.code} · ${d.sheet||'source'} ${d.row?'row '+d.row:''}`),make('p','Review only. No client workbook or source value was changed.'),make('pre',JSON.stringify({workbook_value:d.before,supplied_source_value:d.source_value},null,2)));target.append(box)});
      const label=make('label','Compare separate source evidence (JSON)');const input=document.createElement('input');input.type='file';input.disabled=pending;input.accept='.json,.csv,.xlsx,application/json';label.append(input);target.append(label,make('p','Supply the documented source-evidence schema from a separate retrieval. The workbook itself is never used as source truth. Comparison preserves the original, formulas and prior review revisions.','hint'));
      input.addEventListener('change',async()=>{const file=input.files[0];if(!file||pending)return;if(file.size>2*1024*1024){message('Source evidence must be no larger than 2 MiB.',true);return}setBusy(true);try{if(/\.(csv|xlsx)$/i.test(file.name)){const answer=await post('/api/spreadsheets/source-upload',{file_hash:result.file_hash,filename:file.name,content_base64:await toBase64(file)});++selectionVersion;selected=answer.item.file_hash;detail(answer.item);await refresh();message(answer.item.sharepoint.status==='match'?'Report matches the uploaded SharePoint export.':'Differences found — see the SharePoint check.');return}const source=JSON.parse(await file.text());const response=await fetch('/api/spreadsheets/reconcile',{method:'POST',headers:{'Content-Type':'application/json','X-Review-Token':'same-origin'},body:JSON.stringify({file_hash:result.file_hash,source})});const value=await response.json();if(!response.ok)throw Error(value.error||'Source comparison failed.');++selectionVersion;selected=result.file_hash;detail(value.item);await refresh();message(value.item.idempotent?'This exact source comparison is already the current review revision. Release remains held.':'Source comparison saved as a new review revision. Client workbook and source remain unchanged; release is held.')}catch(error){message(error.message,true)}finally{setBusy(false)}});
    }
    const findings = result.findings || [];
    if (!findings.length) target.append(make('p', 'No supported internal issues found. This does not establish that the spreadsheet is accurate or complete.'));
    findings.forEach(f => {
      const box = make('div', '', 'report-finding');
      box.append(make('strong', `${f.severity || 'Review'} · ${f.code || 'Finding'}`), make('p', f.message || explanations[f.code] || 'Review this finding in the original spreadsheet.'));
      if (f.sheet || f.cell || f.row) box.append(make('small', [f.sheet, f.cell || (f.row ? `Row ${f.row}` : ''), f.column ? `Column ${f.column}` : ''].filter(Boolean).join(' · ')));
      target.append(box);
    });
    if (result.findings_truncated) target.append(make('p', `Showing a bounded selection of ${result.finding_count || 'all'} findings. Review the original spreadsheet for the remainder.`, 'hint'));
    showDemoReview(result,target,detailTicket);
    const summary = document.createElement('details'); summary.append(make('summary', 'File, sheet, and provenance details'), make('pre', JSON.stringify(result, null, 2))); target.append(summary);
  }

  // ---- Pop-up: every possible error for this upload, in one list to work through ----
  const INFO_CODES = new Set(['hyperlink_formula', 'external_hyperlink', 'known_template_layout_differs', 'formula_results_used']);
  const LOW_CODES = new Set(['labeled_total_unit_differs', 'date_stored_as_text', 'zero_inspected', 'hidden_columns_with_data', 'extra_spaces', 'number_stored_as_text']);
  const HIGH_CODES = /duplicate|total|differs|missing_from_report|not_on_sharepoint|typo|mismatch|exceeds|future|invalid|excel_error|wrong_column|scientific/;
  const sevRank = {high: 0, medium: 1, low: 2, info: 3};
  function possibleErrors(result) {
    const items = [];
    (result.findings || []).forEach((f, i) => items.push({
      key: 'check:' + (f.code || '') + ':' + (f.sheet || '') + ':' + (f.cell || f.row || i),
      source: 'Automatic check', severity: INFO_CODES.has(f.code) ? 'info' : LOW_CODES.has(f.code) ? 'low' : HIGH_CODES.test(f.code || '') ? 'high' : 'medium',
      where: [f.sheet, f.cell || (f.row ? `row ${f.row}` : ''), f.column ? `column ${f.column}` : ''].filter(Boolean).join(' · '),
      text: f.message || explanations[f.code] || f.code || 'Review this finding.'}));
    const sp = result.sharepoint || {};
    (sp.findings || []).forEach((f, i) => items.push({
      key: 'sp:' + (f.code || '') + ':' + (f.sheet || '') + ':' + (f.row || i) + ':' + (f.field || ''),
      source: 'SharePoint', severity: 'high', where: [f.sheet && f.sheet !== 'SharePoint comparison' ? f.sheet : '', f.row ? `row ${f.row}` : ''].filter(Boolean).join(' · '),
      text: f.message || f.code}));
    ((result.baseline || {}).findings || []).forEach((f, i) => items.push({
      key: 'usual:' + f.code + ':' + i, source: 'Usual day', severity: f.severity || 'medium', where: 'Whole report', text: f.message}));
    const ai = result.ai_review || {};
    (ai.status === 'done' ? ai.issues : []).forEach((f, i) => items.push({
      key: 'ai:' + i + ':' + (f.sheet || '') + ':' + (f.cell || f.row || '') + ':' + (f.issue || '').slice(0, 40),
      source: 'AI suggestion', severity: f.severity || 'medium',
      where: [f.sheet, f.cell || (f.row ? `row ${f.row}` : '')].filter(Boolean).join(' · '),
      text: f.issue, why: f.why, check: f.check}));
    return items.sort((a, b) => sevRank[a.severity] - sevRank[b.severity]);
  }
  const reviewedKey = hash => 'ci-reviewed:' + hash;
  const loadReviewed = hash => { try { return new Set(JSON.parse(localStorage.getItem(reviewedKey(hash)) || '[]')); } catch (e) { return new Set(); } };
  const saveReviewed = (hash, set) => { try { localStorage.setItem(reviewedKey(hash), JSON.stringify([...set])); } catch (e) { /* per-browser convenience only */ } };
  function showErrorsPopup(result) {
    document.getElementById('errors-dialog')?.remove();
    const items = possibleErrors(result), reviewed = loadReviewed(result.file_hash);
    const dialog = document.createElement('dialog'); dialog.id = 'errors-dialog'; dialog.className = 'errors-dialog';
    dialog.setAttribute('aria-labelledby', 'errors-title');
    const head = make('div', '', 'errors-head');
    const title = make('h2', items.filter(i => i.severity !== 'info').length ? 'Possible errors to review' : 'No possible errors found'); title.id = 'errors-title';
    const close = make('button', '✕', 'errors-close'); close.setAttribute('aria-label', 'Close'); close.addEventListener('click', () => dialog.close());
    head.append(title, close);
    const counts = {high: 0, medium: 0, low: 0, info: 0}; items.forEach(i => counts[i.severity]++);
    const sp = result.sharepoint || {}, ai = result.ai_review || {};
    const status = make('div', '', 'errors-status');
    status.append(make('p', `${result.filename} · version ${result.version}`, 'errors-file'));
    const chips = make('div', '', 'errors-chips');
    [['high', 'High'], ['medium', 'Medium'], ['low', 'Low'], ['info', 'Info']].forEach(([k, label]) => { if (counts[k]) chips.append(make('span', `${counts[k]} ${label}`, 'chip chip-' + k)); });
    status.append(chips);
    status.append(make('p', 'SharePoint: ' + (sp.status === 'match' ? `matches (${sp.matched_rows} rows, totals agree)` : sp.status === 'differences' ? `${(sp.findings || []).length} difference(s)` : sp.message || 'not compared yet'), 'hint'));
    const bl = result.baseline || {};
    status.append(make('p', 'Usual day: ' + (bl.message || 'not compared yet'), 'hint'));
    status.append(make('p', 'AI review: ' + (ai.status === 'done' ? (ai.summary || `${ai.issues.length} suggestion(s)`) : ai.message || 'not run yet'), 'hint'));
    const tabs = make('div', '', 'errors-tabs'); const list = make('div', '', 'errors-list');
    let filter = 'All';
    const progress = make('span', '', 'errors-progress');
    const updateProgress = () => { const n = items.filter(i => reviewed.has(i.key)).length; progress.textContent = items.length ? `${n} of ${items.length} reviewed` : ''; };
    function draw() {
      list.replaceChildren();
      const shown = items.filter(i => filter === 'All' || i.source === filter);
      if (!shown.length) list.append(make('p', items.length ? 'Nothing in this group.' : 'The automatic checks, SharePoint comparison and AI review found nothing to flag. This does not prove the report is correct.', 'hint'));
      shown.forEach(item => {
        const row = make('label', '', 'errors-item sev-' + item.severity + (reviewed.has(item.key) ? ' is-reviewed' : ''));
        const box = document.createElement('input'); box.type = 'checkbox'; box.checked = reviewed.has(item.key);
        box.addEventListener('change', () => { box.checked ? reviewed.add(item.key) : reviewed.delete(item.key); row.classList.toggle('is-reviewed', box.checked); saveReviewed(result.file_hash, reviewed); updateProgress(); });
        const body = make('div', '', 'errors-body');
        const meta = make('div', '', 'errors-meta');
        meta.append(make('span', item.severity.toUpperCase(), 'chip chip-' + item.severity), make('span', item.source, 'errors-source'));
        if (item.where) meta.append(make('span', item.where, 'errors-where'));
        body.append(meta, make('p', item.text, 'errors-text'));
        if (item.why) body.append(make('p', 'Why: ' + item.why, 'hint'));
        if (item.check) body.append(make('p', 'Check: ' + item.check, 'hint'));
        row.append(box, body); list.append(row);
      });
      tabs.querySelectorAll('button').forEach(b => b.classList.toggle('active-tab', b.dataset.filter === filter));
    }
    ['All', 'Automatic check', 'SharePoint', 'Usual day', 'AI suggestion'].forEach(name => {
      const n = name === 'All' ? items.length : items.filter(i => i.source === name).length;
      if (name !== 'All' && !n) return;
      const b = make('button', `${name === 'Automatic check' ? 'Automatic checks' : name === 'AI suggestion' ? 'AI suggestions' : name} (${n})`); b.dataset.filter = name; b.type = 'button';
      b.addEventListener('click', () => { filter = name; draw(); }); tabs.append(b);
    });
    const foot = make('div', '', 'errors-foot');
    const all = make('button', 'Mark all reviewed'); all.type = 'button';
    all.addEventListener('click', () => { items.forEach(i => reviewed.add(i.key)); saveReviewed(result.file_hash, reviewed); draw(); updateProgress(); });
    const done = make('button', 'Done', 'primary'); done.type = 'button'; done.addEventListener('click', () => dialog.close());
    if (!items.length) all.hidden = true;
    foot.append(progress, all, done);
    dialog.append(head, status, tabs, list, foot, make('p', 'Ticks are saved in this browser only. Reviewing here does not approve or release the report.', 'hint errors-note'));
    dialog.addEventListener('close', () => dialog.remove());
    dialog.addEventListener('click', e => { if (e.target === dialog) dialog.close(); });
    document.body.append(dialog); draw(); updateProgress(); dialog.showModal(); done.focus();
  }
  function baselinePanel(result) {
    const bl = result.baseline || {};
    const box = make('section', '', 'report-finding baseline-panel');
    box.append(make('strong', 'Compared with the usual day'));
    if (bl.status === 'normal' || bl.status === 'unusual') {
      box.append(make('p', bl.message, bl.status === 'unusual' ? 'intake-error' : 'hint'));
      const table = document.createElement('table'); table.className = 'baseline-table';
      const head = table.insertRow(); ['', 'This report', 'Usual (median)', 'Range'].forEach(h => { const th = document.createElement('th'); th.textContent = h; head.append(th); });
      [['scans', 'Scans / rows'], ['inspected', 'Parts inspected'], ['rejected', 'Rejects'], ['reject_rate', 'Reject rate']].forEach(([k, label]) => {
        const u = (bl.usual || {})[k]; if (!u) return;
        const row = table.insertRow(); const flagged = (bl.findings || []).some(f => f.field === k || (k === 'reject_rate' && f.code === 'unusual_reject_rate'));
        if (flagged) row.className = 'baseline-flag';
        [label, (bl.today || {})[k] ?? '—', u.median, `${u.low} – ${u.high}`].forEach(v => { row.insertCell().textContent = v; });
      });
      box.append(table);
      const sources = [...new Set((bl.history || []).map(h => h.source === 'saved report' ? 'reports saved here' : 'earlier SharePoint exports'))].join(' and ');
      box.append(make('small', `Based on ${bl.days} earlier day(s) of ${bl.job} from ${sources}. A different day is not always wrong — it's a prompt to check.`));
    } else {
      box.append(make('p', bl.message || 'Not compared yet.', 'hint'));
    }
    const run = make('button', bl.status ? 'Compare again' : 'Compare with usual day'); run.type = 'button'; run.disabled = pending;
    run.addEventListener('click', async () => {
      if (pending) return; setBusy(true);
      try {
        message('Looking up earlier days for this job…');
        const answer = await post('/api/spreadsheets/baseline', {file_hash: result.file_hash});
        ++selectionVersion; selected = answer.item.file_hash; detail(answer.item); await refresh();
        message((answer.item.baseline || {}).message || 'Compared.');
      } catch (error) { message(error.message, true); } finally { setBusy(false); }
    });
    box.append(run);
    return box;
  }
  function aiPanel(result) {
    const ai = result.ai_review || {};
    const box = make('section', '', 'report-finding ai-panel');
    box.append(make('strong', 'AI review — possible issues'));
    if (ai.status === 'done') {
      const c = ai.counts || {};
      box.append(make('p', ai.summary || ''), make('p', ai.issues.length ? `${ai.issues.length} possible issue(s): ${c.high || 0} high · ${c.medium || 0} medium · ${c.low || 0} low.` : 'Claude did not spot anything beyond the automatic checks.', 'hint'));
      ai.issues.forEach(issue => {
        const item = make('div', '', 'ai-issue ai-' + issue.severity);
        const where = [issue.sheet, issue.cell || (issue.row ? `row ${issue.row}` : '')].filter(Boolean).join(' · ');
        item.append(make('strong', `${issue.severity.toUpperCase()} · ${issue.category.replace('_', ' ')}${where ? ' · ' + where : ''}`), make('p', issue.issue));
        if (issue.why) item.append(make('p', 'Why: ' + issue.why, 'hint'));
        if (issue.check) item.append(make('p', 'Check: ' + issue.check, 'hint'));
        box.append(item);
      });
      if (ai.sheet_text_truncated) box.append(make('p', 'This workbook is very large, so Claude read only the first part of it.', 'hint'));
      box.append(make('small', `${ai.notice || 'AI suggestions to double-check.'} Reviewed ${new Date(ai.reviewed_at).toLocaleString()} · ${ai.model}.`));
    } else if (ai.status) {
      box.append(make('p', ai.message || 'AI review did not run.', ai.status === 'error' ? 'intake-error' : 'hint'));
    } else {
      box.append(make('p', 'Not reviewed by AI yet.', 'hint'));
    }
    const run = make('button', ai.status === 'done' ? 'Run AI review again' : 'Run AI review');
    run.disabled = pending;
    run.addEventListener('click', async () => {
      if (pending) return; setBusy(true);
      try {
        message('Claude is reading the report… this can take up to a minute.');
        const answer = await post('/api/spreadsheets/ai-review', {file_hash: result.file_hash});
        ++selectionVersion; selected = answer.item.file_hash; detail(answer.item); await refresh();
        const r = answer.item.ai_review || {};
        message(r.status === 'done' ? `AI review finished: ${r.issues.length} possible issue(s).` : r.message || 'AI review finished.', r.status !== 'done');
      } catch (error) { message(error.message, true); } finally { setBusy(false); }
    });
    box.append(run);
    return box;
  }
  function sharepointPanel(result) {
    const sp = result.sharepoint || {};
    const box = make('section', '', 'report-finding sharepoint-panel');
    box.append(make('strong', 'SharePoint check'));
    if (sp.status === 'match' || sp.status === 'differences') {
      const where = sp.site ? ` (${sp.site} · ${sp.path})` : sp.source_filename ? ` (${sp.source_filename})` : '';
      box.append(make('p', sp.status === 'match'
        ? `Matches the ${sp.source_label || 'SharePoint export'}${where}: all ${sp.matched_rows} rows and totals agree.`
        : `Compared with the ${sp.source_label || 'SharePoint export'}${where}: ${sp.matched_rows} of ${sp.report_rows} report rows matched; ${sp.value_differences} value difference(s), ${sp.missing_from_report} row(s) on SharePoint but not in the report, ${sp.not_on_sharepoint} report row(s) not on SharePoint.`));
      if (sp.web_url && /^https:\/\/[a-z0-9.-]+\.sharepoint\.com\//i.test(sp.web_url)) { const link = make('a', 'Open the SharePoint export'); link.href = sp.web_url; link.target = '_blank'; link.rel = 'noopener'; box.append(link); }
      const t = sp.totals || {};
      box.append(make('p', ['inspected', 'certified', 'rejected'].filter(f => t[f]).map(f => `${f[0].toUpperCase() + f.slice(1)}: report ${t[f].report ?? '?'} / SharePoint ${t[f].sharepoint ?? '?'}`).join(' · '), 'hint'));
      (sp.findings || []).forEach(f => { const item = make('p', f.message || f.code); if (f.sheet && f.row) item.append(make('small', ` — ${f.sheet} row ${f.row}`)); box.append(item); });
    } else if (sp.status) {
      box.append(make('p', sp.message || 'SharePoint was not checked.', sp.status === 'error' ? 'intake-error' : 'hint'));
    } else {
      box.append(make('p', 'Not compared with SharePoint yet.', 'hint'));
    }
    const run = make('button', sp.status === 'match' || sp.status === 'differences' ? 'Re-check against SharePoint' : 'Check against SharePoint');
    run.disabled = pending;
    run.addEventListener('click', async () => {
      if (pending) return; setBusy(true);
      try {
        message('Looking up this job on SharePoint…');
        const answer = await post('/api/spreadsheets/sharepoint', {file_hash: result.file_hash});
        ++selectionVersion; selected = answer.item.file_hash; detail(answer.item); await refresh();
        const st = (answer.item.sharepoint || {}).status;
        message(st === 'match' ? 'Report matches SharePoint.' : st === 'differences' ? 'Differences from SharePoint found — see the SharePoint check.' : (answer.item.sharepoint || {}).message || 'SharePoint check finished.', st !== 'match' && st !== 'differences');
      } catch (error) { message(error.message, true); } finally { setBusy(false); }
    });
    const label = make('label', 'Or upload the SharePoint export (CSV or XLSX)');
    const input = document.createElement('input'); input.type = 'file'; input.accept = '.csv,.xlsx'; input.disabled = pending; label.append(input);
    input.addEventListener('change', async () => {
      const file = input.files[0]; if (!file || pending) return; setBusy(true);
      try {
        message('Comparing with the uploaded SharePoint export…');
        const answer = await post('/api/spreadsheets/source-upload', {file_hash: result.file_hash, filename: file.name, content_base64: await toBase64(file)});
        ++selectionVersion; selected = answer.item.file_hash; detail(answer.item); await refresh();
        message(answer.item.sharepoint.status === 'match' ? 'Report matches the uploaded SharePoint export.' : 'Differences found — see the SharePoint check.');
      } catch (error) { message(error.message, true); } finally { setBusy(false); input.value = ''; }
    });
    box.append(run, label);
    return box;
  }
  async function showDemoReview(result, target, detailTicket) {
    if (!developmentDemo || !result.template_id || !result.source_sha256) return;
    const panel = make('section', '', 'spreadsheet-demo-review');
    target.append(panel);
    const current = () => detailTicket === detailVersion && selected === result.file_hash;
    const bindingOf = review => ({file_hash: review.file_hash, check_hash: review.check_hash, source_sha256: review.source_sha256});
    async function call(path, payload) {
      const response = await fetch(path, payload === undefined ? {} : {method:'POST',headers:{'Content-Type':'application/json','X-Review-Token':'same-origin'},body:JSON.stringify(payload)});
      const value = await response.json();
      if (!response.ok) throw Error(value.error || 'The sample review action was not saved.');
      return value;
    }
    function draw(review) {
      if (!current()) return;
      panel.replaceChildren();
      panel.append(make('h3', 'Sample spreadsheet change review'), make('p', 'DEVELOPMENT SIMULATION ONLY. These choices are practice decisions, not Chris’s authenticated approval. The original and source are unchanged; release stays held.', 'intake-error'));
      if (review.notice) panel.append(make('p', review.notice, 'hint'));
      if (review.download_url && /^\/api\/spreadsheets\/demo\/download\?id=[0-9a-f]{64}$/.test(review.download_url)) {
        const link = make('a', 'Download unapproved synthetic Excel draft'); link.href = review.download_url; link.download = ''; panel.append(link);
      }
      if (!review.eligible) return;
      if (review.current_version === false) {
        panel.append(make('p','A corrected draft supersedes this version. Its old decisions remain audit history only.','intake-error'));
        if (/^[0-9a-f]{64}$/.test(review.superseded_by || '')) {
          const open=make('button','Open latest corrected draft');open.disabled=pending;
          open.addEventListener('click',()=>select(review.superseded_by).catch(error=>message(error.message,true)));panel.append(open);
        }
        const history=document.createElement('details');history.append(make('summary','Historical spreadsheet review'),make('pre',JSON.stringify({binding:bindingOf(review),decisions:review.decisions,events:review.events},null,2)));panel.append(history);
        return;
      }
      const selectedProposals = [];
      const binding = bindingOf(review);
      async function act(ids, decision, apply = false) {
        if (pending || !current()) return;
        if (!ids.length) { message('Select at least one visible proposal.', true); return; }
        if (apply && ids.some(id => !(review.decisions || []).some(d => d.proposal_id === id && d.decision === 'approve'))) {
          message('Every selected spreadsheet proposal must be demo-accepted first. Rejected, held and unreviewed proposals cannot apply.', true); return;
        }
        setBusy(true);
        try {
          const answer = await call('/api/spreadsheets/demo/' + (apply ? 'apply' : 'decisions'), {...binding, proposal_ids:ids, ...(apply ? {} : {decision})});
          if (!current()) return;
          if (apply) {
            ++selectionVersion; selected=answer.item.file_hash; detail(answer.item); await refresh();
            message('New unapproved synthetic Excel draft created and checked. The original workbook and source are unchanged; review decisions do not carry forward.');
          } else {
            draw(answer.review);
            message('Demo spreadsheet decisions saved for the exact file, source and comparison. Selection cleared; release held.');
          }
        } catch(error) {
          message(error.message, true);
          // An interrupted response can still have committed. Reload instead of retrying the action.
          try { const latest=await call('/api/spreadsheet?id='+encodeURIComponent(result.file_hash)); if(current()){detail(latest);await refresh()} } catch(_) {}
        } finally { setBusy(false); }
      }
      for (const proposal of review.proposals || []) {
        const box=make('div','','report-finding');
        const decision=(review.decisions||[]).find(d=>d.proposal_id===proposal.id);
        const label=make('label',`${proposal.sheet} · ${proposal.cell} · ${proposal.field}`);
        const check=document.createElement('input'); check.type='checkbox'; check.disabled=pending;
        check.setAttribute('aria-label','Select spreadsheet proposal '+proposal.sheet+' '+proposal.cell);
        label.append(check); selectedProposals.push({check,id:proposal.id});
        box.append(label,make('pre',JSON.stringify({before:proposal.before,after:proposal.after},null,2)),make('p','Demo decision: '+(decision?decision.decision:'not reviewed'),'hint'));
        for (const [choice,labelText] of [['approve','Demo Accept change'],['deny','Demo Reject change'],['hold','Demo Hold change']]) {
          const button=make('button',labelText); button.disabled=pending; button.addEventListener('click',()=>act([proposal.id],choice)); box.append(button);
        }
        panel.append(box);
      }
      if (!(review.proposals||[]).length) panel.append(make('p','No supported numeric correction proposals for this version. Missing values and unsupported fields are never filled automatically.'));
      else {
        const selectedIds=()=>selectedProposals.filter(p=>p.check.checked&&!p.check.disabled).map(p=>p.id);
        for (const [choice,labelText] of [['approve','Demo Accept selected changes'],['deny','Demo Reject selected changes'],['hold','Demo Hold selected changes']]) {
          const button=make('button',labelText); button.disabled=pending; button.addEventListener('click',()=>act(selectedIds(),choice)); panel.append(button);
        }
        const apply=make('button','Create Excel draft from selected accepted changes'); apply.disabled=pending;
        apply.addEventListener('click',()=>act(selectedIds(),null,true));panel.append(apply);
      }
      const audit=document.createElement('details');audit.append(make('summary','Spreadsheet decisions and exact version bindings'),make('pre',JSON.stringify({binding,decisions:review.decisions,events:review.events},null,2)));panel.append(audit);
    }
    try { const review=await call('/api/spreadsheets/demo/review?id='+encodeURIComponent(result.file_hash));draw(review); }
    catch(error) { if(current())panel.append(make('p','Sample decision review unavailable: '+error.message,'hint')); }
  }

  async function select(fileHash) {
    if(pending)return;
    const requestVersion = ++selectionVersion;
    const response = await fetch('/api/spreadsheet?id=' + encodeURIComponent(fileHash));
    const result = await response.json(); if (!response.ok) throw Error(result.error || 'Could not load this version.');
    if (requestVersion !== selectionVersion) return;
    selected = fileHash; detail(result);
    node('spreadsheet-list').querySelectorAll('button').forEach(button => button.setAttribute('aria-pressed', String(button.dataset.id === selected)));
  }
  async function refresh() {
    const response = await fetch('/api/spreadsheets'); const result = await response.json();
    if (!response.ok) throw Error(result.error || 'Could not load spreadsheet uploads.');
    const list = node('spreadsheet-list'); list.replaceChildren();
    if (!result.spreadsheets.length) list.append(make('p', 'No spreadsheets uploaded yet.', 'empty-state'));
    result.spreadsheets.forEach(item => {
      const button = make('button', '', 'report-card'); button.dataset.id = item.file_hash;button.disabled=pending;
      button.setAttribute('aria-pressed', String(item.file_hash === selected));
      button.append(make('strong', `${item.filename} · v${item.version}`), make('small', 'Internal checks only · source not verified'));
      button.addEventListener('click', () => select(item.file_hash).catch(error => message(error.message, true))); list.append(button);
    });
    if (result.has_more) list.append(make('p', `Showing the latest ${result.limit} of ${result.count} uploaded versions.`, 'hint'));
  }
  const CHUNK = 3 * 1024 * 1024;
  const toBase64 = blob => new Promise((resolve, reject) => { const reader = new FileReader(); reader.onload = () => resolve(String(reader.result).split(',')[1]); reader.onerror = () => reject(Error('Could not read this file.')); reader.readAsDataURL(blob); });
  const post = async (path, payload) => {
    const response = await fetch(path, {method:'POST', headers:{'Content-Type':'application/json','X-Review-Token':'same-origin'}, body:JSON.stringify(payload)});
    const result = await response.json(); if (!response.ok) throw Error(result.error || 'Spreadsheet was not saved.');
    return result;
  };
  const sha256Hex = async file => { const digest = await crypto.subtle.digest('SHA-256', await file.arrayBuffer()); return Array.from(new Uint8Array(digest), b => b.toString(16).padStart(2, '0')).join(''); };
  const mib = n => (n / 1048576).toFixed(n < 10 * 1048576 ? 2 : 1) + ' MiB';
  node('spreadsheet-file').addEventListener('change', async event => { await uploadSpreadsheet(event.target.files[0]); event.target.value = ''; });
  // Other upload boxes (report manifest, legacy JSON inbox) hand spreadsheets here instead of rejecting them.
  window.ciUploadSpreadsheet = file => { node('spreadsheet-upload').scrollIntoView({behavior: 'smooth', block: 'start'}); return uploadSpreadsheet(file); };
  async function uploadSpreadsheet(file) {
    if (!file || pending) return;
    const problem = /\.(xls|xlsm|xlsb)$/i.test(file.name) ? `"${file.name}" is a legacy or macro-enabled Excel file. In Excel use File → Save As → "Excel Workbook (.xlsx)", then upload that copy.`
      : !/\.(csv|xlsx)$/i.test(file.name) ? `"${file.name}" is not a .csv or .xlsx file. Only UTF-8 CSV and XLSX workbooks are supported.`
      : file.size < 1 ? `"${file.name}" is empty (0 bytes).`
      : null;
    if (problem) { message(problem, true); return; }
    setBusy(true);
    try {
      // No file-size limit: the file is hashed in the browser, sent in 3 MiB parts, then assembled, verified and checked on the server.
      message(`Preparing ${file.name} (${mib(file.size)})…`);
      const sha256 = await sha256Hex(file);
      const uploadId = crypto.randomUUID().replace(/-/g, '');
      const partCount = Math.max(1, Math.ceil(file.size / CHUNK));
      for (let index = 0; index < partCount; index++) {
        message(partCount > 1 ? `Uploading part ${index + 1} of ${partCount} (${mib(Math.min(file.size, (index + 1) * CHUNK))} of ${mib(file.size)})…` : 'Uploading…');
        await post('/api/spreadsheets/upload/part', {upload_id: uploadId, index, content_base64: await toBase64(file.slice(index * CHUNK, (index + 1) * CHUNK))});
      }
      message('Checking the spreadsheet (and SharePoint / AI review when set up)… this can take a minute.');
      const result = await post('/api/spreadsheets/upload/finish', {upload_id: uploadId, filename: file.name, part_count: partCount, sha256});
      ++selectionVersion; selected = result.item.file_hash; detail(result.item); await refresh();
      showErrorsPopup(result.item);
      message(result.item.idempotent ? 'This exact file was already checked. Its original version was preserved.' : 'Internal checks saved and original preserved. Independent source verification and report release remain held.');
    } catch (error) { message(error.message, true); }
    finally { setBusy(false); }
  }
  node('spreadsheet-refresh').addEventListener('click', () => refresh().then(() => message('Uploads refreshed from the private database.')).catch(error => message(error.message, true)));
  async function loadDevelopmentExample(example){
    if(pending)return;
    setBusy(true);
    const status=node('development-message');status.className='';status.textContent='Loading synthetic report and checking its separate example evidence…';
    let saved=null;
    try{
      const response=await fetch('/api/spreadsheets/import',{method:'POST',headers:{'Content-Type':'application/json','X-Review-Token':'same-origin'},body:JSON.stringify({filename:example.filename,content_base64:example.content_base64})});
      const value=await response.json();if(!response.ok)throw Error(value.error||'Synthetic sample import failed.');
      let result=value.item;saved=result;
      if(example.source){
        const compared=await fetch('/api/spreadsheets/reconcile',{method:'POST',headers:{'Content-Type':'application/json','X-Review-Token':'same-origin'},body:JSON.stringify({file_hash:result.file_hash,source:example.source})});
        const evidence=await compared.json();if(!compared.ok)throw Error(evidence.error||'Synthetic source comparison failed.');result=evidence.item;
      }
      ++selectionVersion;selected=result.file_hash;detail(result);await refresh();
      status.textContent='Synthetic example ready. See its findings below. No actual client data, business approval or release.';
      message('Development example checked. Original retained; release held.');
      node('spreadsheet-upload')?.scrollIntoView({behavior:'smooth',block:'start'});
    }catch(error){if(saved){++selectionVersion;selected=saved.file_hash;detail(saved);try{await refresh()}catch(_){} }const explanation=(saved?'Original sample was saved, but its comparison did not finish. ':'')+error.message;status.className='intake-error';status.textContent=explanation;message(explanation,true)}
    finally{setBusy(false)}
  }
  if(node('development-lab')&&Array.isArray(globalThis.CI_DEVELOPMENT_EXAMPLES)){
    fetch('/api/health').then(response=>response.json()).then(health=>{
      if(health.development_demo!==true)return;
      developmentDemo=true;document.body?.classList?.add('development-mode');
      node('development-lab').hidden=false;
      const target=node('development-examples');
      globalThis.CI_DEVELOPMENT_EXAMPLES.forEach(example=>{
        const button=make('button','','development-example');button.dataset.example=example.id;button.disabled=pending;
        button.append(make('strong',example.title),make('small',example.description));
        button.addEventListener('click',()=>loadDevelopmentExample(example));target.append(button);
      });
    }).catch(error=>message('Development examples unavailable: '+error.message,true));
  }
  refresh().catch(error => message(error.message, true));
})();

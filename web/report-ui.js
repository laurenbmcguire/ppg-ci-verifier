'use strict';
(() => {
  let inbox = { reports: [], outbox: [] }, selected = null, pending = false;
  const node = id => document.getElementById(id);
  const make = (tag, text, cls) => { const n = document.createElement(tag); n.textContent = text; if (cls) n.className = cls; return n; };
  const key = r => JSON.stringify([r.report_id, r.version]);
  const labels = { matched_supplied_snapshot: 'Matches supplied snapshot', mismatch: 'Discrepancies found', unable_to_verify: 'Unable to verify' };
  const note = (text, error = false) => { node('intake-message').textContent = text; node('intake-message').className = error ? 'intake-error' : ''; };
  function draw() {
    const reports = inbox.reports || [];
    node('report-stats').replaceChildren(...[
      [reports.length, 'Report versions'],
      [reports.filter(r => r.status === 'mismatch').length, 'With discrepancies'],
      [reports.filter(r => r.status === 'unable_to_verify').length, 'Unable to verify'],
      [(inbox.outbox || []).length, 'Text previews · not sent']
    ].map(([n, label]) => { const d = make('div', ''); d.append(make('strong', String(n)), make('span', label)); return d; }));
    const list = node('report-list'); list.replaceChildren();
    if (!reports.length) list.append(make('p', 'No reports imported yet. Load an example to inspect the checks.', 'empty-state'));
    reports.forEach(r => {
      const b = make('button', '', 'report-card' + (selected === key(r) ? ' selected' : ''));
      b.append(make('strong', `${r.report_id} · v${r.version}`), make('span', labels[r.status] || r.status), make('small', `${r.job_id} · ${r.period?.start || 'Unknown period'}`));
      b.setAttribute('aria-pressed', String(selected === key(r))); b.addEventListener('click', () => { selected = key(r); draw(); }); list.append(b);
    });
    const detail = node('report-detail'); detail.replaceChildren();
    const r = reports.find(x => key(x) === selected) || reports[0];
    if (!r) { detail.append(make('h3', 'Evidence before approval'), make('p', 'Each version gets its own findings, hashes, source scope and text preview. Missing evidence holds verification; no report is released.')); return; }
    selected = key(r);
    detail.append(make('h3', `${r.report_id} · version ${r.version}`), make('div', labels[r.status] || r.status, 'result-badge ' + r.status), make('p', 'Compared only with the supplied evidence. Source authenticity is not independently established. Customer release is held.', 'hint'));
    const findings = r.findings || [];
    if (!findings.length) detail.append(make('p', 'No discrepancies found in the supplied records and totals. This does not approve the report.'));
    findings.forEach(f => { const box = make('div', '', 'report-finding'); box.append(make('strong', `${f.severity || 'Review'} · ${f.code || 'Finding'}`), make('p', f.message || 'Review required'));
      if (f.record_id) box.append(make('small', `Record: ${f.record_id}`));
      if (f.expected !== undefined || f.observed !== undefined) box.append(make('pre', JSON.stringify({ expected: f.expected, observed: f.observed }, null, 2))); detail.append(box); });
    const outbox = (inbox.outbox || []).filter(o => o.report_id === r.report_id && o.version === r.version);
    detail.append(make('h3', 'Text preview'));
    if (!outbox.length) detail.append(make('p', 'No text preview available. No message has been sent.'));
    outbox.forEach(o => { detail.append(make('div', o.message || o.text || o.preview || 'Preview only', 'sms-preview')); });
    detail.append(make('p', 'Preview only. No recipients or SMS provider configured. Text delivery is never report approval.', 'hint'));
    const prov = document.createElement('details'); prov.append(make('summary', 'Evidence and version details'), make('pre', JSON.stringify(r, null, 2))); detail.append(prov);
  }
  async function fetchInbox() {
    const response = await fetch('/api/reports'); const data = await response.json();
    if (!response.ok) throw Error(data.error || 'Could not load the report inbox.');
    inbox = data; if (!selected && inbox.reports.length) selected = key(inbox.reports[0]); draw();
  }
  async function importReport(manifest) {
    if (pending) return; pending = true;
    const controls = ['report-file', 'sample-match', 'sample-errors', 'refresh-reports']; controls.forEach(id => node(id).disabled = true);
    try {
      const s = await fetch('/api/state?mode=synthetic'); const auth = await s.json(); if (!s.ok) throw Error(auth.error || 'Private workspace is unavailable.');
      const response = await fetch('/api/reports/import', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Review-Token': auth.token }, body: typeof manifest === 'string' ? manifest : JSON.stringify(manifest) });
      const data = await response.json(); if (!response.ok) throw Error(data.error || 'Import failed.');
      selected = key(data.item); inbox = data.state; draw();
      note(data.item.idempotent ? 'This exact version was already imported. No duplicate report or text preview was created.' : 'Report compared and findings saved to the private workspace. Text preview created; nothing sent or released.');
    } catch (e) { note(e.message, true); } finally { pending = false; controls.forEach(id => node(id).disabled = false); node('report-file').value = ''; }
  }
  node('report-file').addEventListener('change', async e => {
    const file = e.target.files[0]; if (!file) return;
    if (/\.(csv|xlsx|xlsm|xls|tsv)$/i.test(file.name) && window.ciUploadSpreadsheet) { e.target.value = ''; note(`"${file.name}" is a spreadsheet, so it was sent to "Check a spreadsheet".`); await window.ciUploadSpreadsheet(file); return; }
    if (file.size > 1024 * 1024) { note('Use a JSON file no larger than 1 MiB.', true); e.target.value = ''; return; }
    try { await importReport(await file.text()); } catch (err) { note('Invalid JSON: ' + err.message, true); e.target.value = ''; }
  });
  async function sample(name) { try { const r = await fetch('/api/report-sample?name=' + name); const data = await r.json(); if (!r.ok) throw Error(data.error || 'Example not available.'); await importReport(data); } catch (e) { note(e.message, true); } }
  node('sample-match').addEventListener('click', () => sample('match'));
  node('sample-errors').addEventListener('click', () => sample('errors'));
  node('refresh-reports').addEventListener('click', () => fetchInbox().then(() => note('Inbox refreshed from the private database.')).catch(e => note(e.message, true)));
  fetchInbox().catch(e => note(e.message, true));
})();

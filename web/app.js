'use strict';
(() => {
  // ---------------------------------------------------------------- helpers
  const $ = id => document.getElementById(id);
  const h = (tag, attrs, ...kids) => {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v == null || v === false) continue;
      if (k === 'class') el.className = v;
      else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
      else if (v === true) el.setAttribute(k, '');
      else el.setAttribute(k, v);
    }
    for (const kid of kids.flat()) if (kid != null && kid !== false) el.append(kid.nodeType ? kid : String(kid));
    return el;
  };
  const store = {
    get(key, fallback) { try { return JSON.parse(localStorage.getItem(key)) ?? fallback; } catch (e) { return fallback; } },
    set(key, value) { try { localStorage.setItem(key, JSON.stringify(value)); } catch (e) { /* per-browser convenience only */ } },
  };
  async function api(path, body) {
    const response = await fetch(path, body === undefined ? {headers: {Accept: 'application/json'}} : {
      method: 'POST', headers: {'Content-Type': 'application/json', 'X-Review-Token': 'same-origin'}, body: JSON.stringify(body)});
    let data = {};
    try { data = await response.json(); } catch (e) { /* non-JSON error page */ }
    if (!response.ok) throw new Error(data.error || `The server answered ${response.status}. Try again in a moment.`);
    return data;
  }
  let toastTimer;
  function toast(text, isError) {
    const t = $('toast'); t.textContent = text; t.className = 'toast' + (isError ? ' err' : ''); t.hidden = false;
    clearTimeout(toastTimer); toastTimer = setTimeout(() => { t.hidden = true; }, isError ? 9000 : 5000);
  }
  const fmt = n => n == null || Number.isNaN(n) ? '—' : Number(n).toLocaleString('en-US', {maximumFractionDigits: 1});
  const dayLabel = d => { if (!d) return 'No report date'; const x = new Date(d + 'T12:00:00'); return isNaN(x) ? d : x.toLocaleDateString('en-US', {weekday: 'long', month: 'long', day: 'numeric', year: 'numeric'}); };
  const shortDay = d => { if (!d) return ''; const x = new Date(d + 'T12:00:00'); return isNaN(x) ? d : x.toLocaleDateString('en-US', {weekday: 'short', month: 'short', day: 'numeric'}); };
  const ago = iso => { const s = (Date.now() - new Date(iso)) / 1000; if (!(s >= 0)) return ''; if (s < 90) return 'just now'; if (s < 3600) return `${Math.round(s / 60)} min ago`; if (s < 86400) return `${Math.round(s / 3600)} h ago`; return new Date(iso).toLocaleDateString('en-US', {month: 'short', day: 'numeric'}); };
  const STATUS = {hold: 'Hold', check: 'Check', clear: 'Clear'};
  const statusNote = r => { const c = r.counts || {}; if (r.status === 'hold') return `${c.high} must be fixed`; if (r.status === 'check') return `${(c.medium || 0) + (c.low || 0)} to look at`; return 'Nothing flagged'; };

  // ---------------------------------------------------------------- state
  const state = {list: [], filter: store.get('rd-filter', 'attention'), search: '', selected: null, report: null, fixes: null, issueSource: 'All', showAll: false, busy: false};

  // ---------------------------------------------------------------- connections
  api('/api/health').then(hh => {
    const box = $('connections'); box.replaceChildren(
      h('span', {class: 'conn' + (hh.storage === 'vercel-postgres' ? ' on' : '')}, h('i'), 'Saved in the cloud'),
      h('span', {class: 'conn' + (hh.sharepoint_configured ? ' on' : '')}, h('i'), hh.sharepoint_configured ? 'SharePoint connected' : 'SharePoint off'),
      h('span', {class: 'conn' + (hh.ai_review_configured ? ' on' : '')}, h('i'), hh.ai_review_configured ? 'AI review on' : 'AI review off'));
  }).catch(() => {});

  // ---------------------------------------------------------------- queue
  async function loadList() {
    const data = await api('/api/spreadsheets');
    state.list = data.spreadsheets || [];
    drawQueue();
  }
  function drawQueue() {
    const q = state.search.trim().toLowerCase();
    let rows = state.list.filter(r => !q || [r.filename, r.review?.job, r.review?.day].some(v => (v || '').toLowerCase().includes(q)));
    if (state.filter === 'attention') rows = rows.filter(r => !r.review || r.review.status !== 'clear');
    document.querySelectorAll('.segmented button').forEach(b => b.classList.toggle('on', b.dataset.filter === state.filter));
    const list = $('queue-list'); list.replaceChildren();
    if (!rows.length) {
      list.append(h('p', {class: 'queue-empty'}, state.list.length
        ? (state.filter === 'attention' && !q ? 'Every report is clear. Switch to All to see them.' : 'No reports match that search.')
        : 'No reports yet. Upload an end-of-shift report to start.'));
      return;
    }
    let lastDay = null;
    for (const r of rows) {
      const day = r.review?.day || (r.received_at || '').slice(0, 10);
      if (day !== lastDay) { list.append(h('div', {class: 'day'}, dayLabel(day))); lastDay = day; }
      const rv = r.review || {status: 'check', counts: {}};
      const c = rv.counts || {};
      const count = rv.status === 'clear' ? 'Clear' : rv.status === 'hold' ? `${c.high} to fix` : `${(c.medium || 0) + (c.low || 0)} to look at`;
      list.append(h('button', {type: 'button', class: 'item' + (r.file_hash === state.selected ? ' sel' : ''), onclick: () => select(r.file_hash),
        'aria-label': `${rv.job || r.filename}, ${STATUS[rv.status] || 'Check'}, ${count}`},
        h('span', {class: 'tag tag-' + rv.status}, STATUS[rv.status] || 'Check'),
        h('span', {class: 'job'}, (rv.job || 'Unknown job') + (rv.fixed_from ? ' (fixed)' : '')),
        h('span', {class: 'count'}, count),
        h('span', {class: 'file'}, `${r.filename} · v${r.version} · ${ago(r.received_at)}`)));
    }
  }
  $('search').addEventListener('input', e => { state.search = e.target.value; drawQueue(); });
  document.querySelectorAll('.segmented button').forEach(b => b.addEventListener('click', () => { state.filter = b.dataset.filter; store.set('rd-filter', state.filter); drawQueue(); }));

  // ---------------------------------------------------------------- routing
  function select(hash) { if (location.hash !== '#r=' + hash) location.hash = 'r=' + hash; else route(); }
  window.addEventListener('hashchange', route);
  async function route() {
    const m = location.hash.match(/^#r=([0-9a-f]{64})$/);
    document.body.classList.toggle('viewing', !!m);
    if (!m) { state.selected = null; drawQueue(); showEmpty(); return; }
    state.selected = m[1]; drawQueue();
    await openReport(m[1]);
  }
  const emptyState = $('empty');
  function showEmpty() { $('report').replaceChildren(emptyState); }

  // ---------------------------------------------------------------- report detail
  async function openReport(hash, keepScroll) {
    const report = $('report');
    if (!keepScroll) report.replaceChildren(h('div', {class: 'sheetwrap'}, h('p', {class: 'note'}, h('span', {class: 'spin'}), ' Opening report…')));
    try {
      const data = await api('/api/spreadsheet?id=' + hash);
      if (state.selected !== hash) return;
      state.report = data; state.fixes = null; state.showAll = false;
      const top = report.scrollTop;
      drawReport();
      if (keepScroll) report.scrollTop = top;
      loadFixes(hash);
    } catch (e) {
      report.replaceChildren(h('div', {class: 'sheetwrap'}, h('p', {class: 'note'}, e.message)));
    }
  }

  function drawReport() {
    const r = state.report, rv = r.review;
    const wrap = h('div', {class: 'sheetwrap'});
    wrap.append(h('button', {type: 'button', class: 'back', onclick: () => { location.hash = ''; }}, '‹ All reports'));
    // header with the inspection tag
    wrap.append(h('div', {class: 'head'},
      h('div', {class: `tag tag-big tag-${rv.status}`}, STATUS[rv.status], h('small', {}, statusNote(rv))),
      h('div', {}, h('h1', {}, rv.job || r.filename),
        h('div', {class: 'meta'}, `${dayLabel(rv.day)}. ${r.filename}, version ${r.version}, uploaded ${ago(r.received_at)}.`))));
    wrap.append(h('div', {class: 'actions'},
      h('a', {class: 'btn', href: '/api/spreadsheets/download?id=' + r.file_hash, download: r.filename}, 'Download this version'),
      h('button', {type: 'button', class: 'btn', onclick: () => rerun('sharepoint', 'Checking SharePoint…')}, 'Check SharePoint again'),
      h('button', {type: 'button', class: 'btn', onclick: () => rerun('baseline', 'Comparing with earlier days…')}, 'Compare with usual day'),
      h('button', {type: 'button', class: 'btn', onclick: () => rerun('ai-review', 'Claude is reading the report…')}, rv.ai.status === 'done' ? 'Run AI review again' : 'Run AI review')));
    wrap.append(numbers(r));
    wrap.append(h('section', {class: 'block', id: 'fixes-block'}, h('header', {}, h('h2', {}, 'Fixes'), h('p', {}, 'Loading suggested fixes…'))));
    wrap.append(issuesBlock(rv));
    wrap.append(sourcesBlock(r));
    wrap.append(readBlock(r));
    if (rv.fixed_from || rv.fixes_applied) wrap.append(historyBlock(r));
    $('report').replaceChildren(wrap);
    if (state.fixes) drawFixes();
  }

  function numbers(r) {
    const rv = r.review, bl = r.baseline || {}, usual = bl.usual || {}, flagged = new Set((bl.findings || []).map(f => f.field));
    const rate = rv.inspected ? (rv.rejected || 0) / rv.inspected * 100 : null;
    const cell = (value, label, u, key) => h('div', {}, h('b', {}, value), h('span', {}, label),
      u ? h('em', {class: flagged.has(key) ? 'off' : ''}, `Usual ${u.median}`) : null);
    return h('div', {class: 'numbers'},
      cell(fmt(rv.scans), 'Scans / rows', usual.scans, 'scans'),
      cell(fmt(rv.inspected), 'Inspected', usual.inspected, 'inspected'),
      cell(fmt(rv.rejected), 'Rejected', usual.rejected, 'rejected'),
      cell(rate == null ? '—' : rate.toFixed(1) + '%', 'Reject rate', usual.reject_rate, 'reject_rate'));
  }

  // ---------------- fixes
  async function loadFixes(hash) {
    try {
      const data = await api('/api/spreadsheets/fixes', {file_hash: hash});
      if (state.selected !== hash) return;
      state.fixes = data.proposals || [];
    } catch (e) { state.fixes = []; }
    drawFixes();
  }
  const decisionsKey = () => 'rd-fixes:' + state.report.file_hash;
  const editsKey = () => 'rd-edits:' + state.report.file_hash;
  const editId = (sheet, cell) => `${sheet}!${cell}`;

  // Row editor: shows one row's cells with their headers; changed cells become "Your edit" fixes.
  async function rowEditor(sheet, row, note, flagCell) {
    const box = h('div', {class: 'editor'}, h('p', {class: 'note'}, h('span', {class: 'spin'}), ' Loading the row…'));
    try {
      const data = await api('/api/spreadsheets/cells', {file_hash: state.report.file_hash, sheet, row});
      const edits = store.get(editsKey(), {});
      const inputs = [];
      const grid = h('div', {class: 'cells'});
      for (const c of data.cells) {
        const saved = edits[editId(data.sheet, c.cell)];
        const input = h('input', {value: saved ? saved.after : c.value, disabled: c.formula, 'aria-label': `${c.header || c.cell} (${c.cell})`});
        if (!c.formula) inputs.push({c, input});
        grid.append(h('label', {class: 'cellbox' + (c.formula ? ' formula' : '') + (saved ? ' edited' : '') + (flagCell && c.cell === String(flagCell).toUpperCase() ? ' flag' : '')},
          h('span', {}, c.header || 'Column', h('small', {}, c.cell + (c.formula ? ' · formula' : ''))), input));
      }
      const save = () => {
        const all = store.get(editsKey(), {});
        let n = 0;
        for (const {c, input} of inputs) {
          const id = editId(data.sheet, c.cell);
          if (input.value !== c.value) { all[id] = {sheet: data.sheet, cell: c.cell, before: c.before, after: input.value, header: c.header}; n++; }
          else delete all[id];
        }
        store.set(editsKey(), all);
        toast(n ? `${n} edit${n === 1 ? '' : 's'} added to Fixes. Download the fixed version when you're ready.` : 'No changes on this row.');
        box.remove(); drawFixes();
      };
      box.replaceChildren(
        h('p', {class: 'note'}, `${data.sheet}, row ${data.row}` + (data.header_row ? ` (headers on row ${data.header_row})` : '') + (note ? ` · ${note}` : '') + '. Change any value, then save.'),
        grid,
        h('div', {class: 'editbar'},
          h('button', {type: 'button', class: 'btn primary small', onclick: save}, 'Save edits'),
          h('button', {type: 'button', class: 'btn small', onclick: () => { inputs.forEach(({input}) => { input.value = ''; }); }}, 'Clear the whole row'),
          h('button', {type: 'button', class: 'btn ghost small', onclick: () => box.remove()}, 'Cancel')));
      (inputs.find(x => flagCell && x.c.cell === String(flagCell).toUpperCase()) || inputs[0])?.input.focus();
    } catch (e) { box.replaceChildren(h('p', {class: 'note'}, e.message)); }
    return box;
  }

  function drawFixes() {
    const block = $('fixes-block'); if (!block || !state.fixes) return;
    const dec = store.get(decisionsKey(), {}), edits = store.get(editsKey(), {});
    const editList = Object.entries(edits);
    const fixes = state.fixes.filter(f => !edits[editId(f.sheet, f.cell)]);
    const accepted = fixes.filter(f => dec[f.id]?.state === 'accepted');
    const total = accepted.length + editList.length;
    block.replaceChildren();
    const save = d => { store.set(decisionsKey(), d); drawFixes(); };
    block.append(h('header', {}, h('h2', {}, 'Fixes'),
      h('p', {}, fixes.length ? `${fixes.length} suggested. Accept the ones you want, or edit any cell yourself, then download a corrected copy.`
        : 'No automatic fixes for this report. Use "Fix this" on an issue below, or edit any cell here, then download a corrected copy.'),
      fixes.length ? h('div', {class: 'right'}, h('button', {type: 'button', class: 'btn small', onclick: () => {
        const d = store.get(decisionsKey(), {}); fixes.filter(f => f.confidence === 'high').forEach(f => { d[f.id] = {state: 'accepted', after: d[f.id]?.after ?? f.after}; }); save(d);
      }}, 'Accept all safe fixes')) : null));
    const panel = h('div', {class: 'panel'});
    for (const f of fixes) {
      const d = dec[f.id] || {};
      const input = h('input', {value: d.after ?? f.after, 'aria-label': `New value for ${f.sheet} ${f.cell}`, onchange: e => {
        const all = store.get(decisionsKey(), {}); all[f.id] = {state: all[f.id]?.state || 'accepted', after: e.target.value}; save(all); }});
      const set = st => { const all = store.get(decisionsKey(), {}); all[f.id] = {state: all[f.id]?.state === st ? undefined : st, after: input.value}; save(all); };
      const before = f.before === '' ? '(blank)' : f.source === 'Clean-up' ? f.before.replace(/ /g, '·').replace(/ /g, '⍽') : f.before;
      panel.append(h('div', {class: 'fix' + (d.state ? ' ' + d.state : '')},
        h('div', {class: 'where'}, `${f.source} · ${f.sheet}, cell ${f.cell}` + (f.confidence !== 'high' ? ' · check carefully' : '')),
        h('div', {class: 'choose'},
          h('button', {type: 'button', class: 'btn small' + (d.state === 'accepted' ? ' on-accept' : ''), 'aria-pressed': String(d.state === 'accepted'), onclick: () => set('accepted')}, d.state === 'accepted' ? '✓ Accepted' : 'Accept'),
          h('button', {type: 'button', class: 'btn small' + (d.state === 'rejected' ? ' on-reject' : ''), 'aria-pressed': String(d.state === 'rejected'), onclick: () => set('rejected')}, d.state === 'rejected' ? 'Rejected' : 'Reject')),
        h('div', {class: 'change'}, h('span', {class: 'before'}, before), h('span', {'aria-hidden': 'true'}, '→'), input),
        h('p', {class: 'why'}, f.reason)));
    }
    // your own edits
    for (const [id, e] of editList) {
      const input = h('input', {value: e.after, 'aria-label': `New value for ${e.sheet} ${e.cell}`, onchange: ev => {
        const all = store.get(editsKey(), {}); if (all[id]) { all[id].after = ev.target.value; store.set(editsKey(), all); drawFixes(); } }});
      panel.append(h('div', {class: 'fix accepted mine'},
        h('div', {class: 'where'}, `Your edit · ${e.sheet}, cell ${e.cell}` + (e.header ? ` (${e.header})` : '')),
        h('div', {class: 'choose'}, h('button', {type: 'button', class: 'btn small', onclick: () => {
          const all = store.get(editsKey(), {}); delete all[id]; store.set(editsKey(), all); drawFixes(); }}, 'Remove')),
        h('div', {class: 'change'}, h('span', {class: 'before'}, e.before === '' ? '(blank)' : e.before), h('span', {'aria-hidden': 'true'}, '→'),
          e.after === '' ? h('span', {class: 'blank'}, '(blank)') : null, input)));
    }
    // edit any cell
    const sheets = (state.report.sheets || []).map(x => x.name).filter(Boolean);
    const sheetPick = h('select', {'aria-label': 'Sheet'}, sheets.map(n => h('option', {value: n}, n)));
    const rowPick = h('input', {type: 'number', min: '1', placeholder: 'Row', 'aria-label': 'Row number', class: 'rownum'});
    const slot = h('div', {});
    const openRow = async () => { if (!rowPick.value) { rowPick.focus(); return; } slot.replaceChildren(await rowEditor(sheetPick.value, Number(rowPick.value))); };
    rowPick.addEventListener('keydown', e => { if (e.key === 'Enter') openRow(); });
    panel.append(h('div', {class: 'anycell'}, h('span', {}, 'Edit any cell:'), sheets.length > 1 ? sheetPick : null, rowPick,
      h('button', {type: 'button', class: 'btn small', onclick: openRow}, 'Open row')), slot);
    panel.append(h('div', {class: 'fixbar'},
      h('span', {}, total ? `${total} change${total === 1 ? '' : 's'} ready` + (editList.length ? ` (${editList.length} yours)` : '') : 'No changes yet'),
      total ? h('button', {type: 'button', class: 'btn ghost small', onclick: () => { store.set(decisionsKey(), {}); store.set(editsKey(), {}); drawFixes(); }}, 'Clear all') : null,
      h('button', {type: 'button', class: 'btn', disabled: !total || state.busy, onclick: () => applyFixes(accepted, dec, edits)},
        total ? `Download fixed version (${total})` : 'Download fixed version')));
    block.append(panel);
  }
  async function applyFixes(accepted, dec, edits) {
    if (state.busy) return; state.busy = true; drawFixes();
    toast('Building the fixed version…');
    try {
      const changes = accepted.map(f => {
        const after = dec[f.id]?.after ?? f.after;
        return {id: f.id, sheet: f.sheet, cell: f.cell, before: f.before, after, reason: f.reason, source: f.source,
          kind: after === f.after ? f.kind : (f.kind === 'number' && after.trim() !== '' && !isNaN(Number(after)) ? 'number' : 'text')};
      });
      for (const e of Object.values(edits || {})) {
        const numeric = e.after.trim() !== '' && !isNaN(Number(e.after.replace(/,/g, ''))) && (e.before === '' || !isNaN(Number(e.before)));
        changes.push({sheet: e.sheet, cell: e.cell, before: e.before, after: e.after, reason: 'Edited by reviewer', source: 'Your edit', kind: numeric ? 'number' : 'text'});
      }
      const answer = await api('/api/spreadsheets/apply-fixes', {file_hash: state.report.file_hash, changes});
      store.set(decisionsKey(), {}); store.set(editsKey(), {});
      const link = h('a', {href: '/api/spreadsheets/download?id=' + answer.item.file_hash, download: answer.item.filename});
      document.body.append(link); link.click(); link.remove();
      toast(`Saved and downloaded "${answer.item.filename}" with ${changes.length} change${changes.length === 1 ? '' : 's'}. It has been checked again. The original is unchanged.`);
      await loadList(); select(answer.item.file_hash);
    } catch (e) { toast(e.message, true); }
    finally { state.busy = false; if ($('fixes-block')) drawFixes(); }
  }

  // ---------------- issues
  function issuesBlock(rv) {
    const reviewedKey = 'rd-reviewed:' + state.report.file_hash;
    const done = new Set(store.get(reviewedKey, []));
    const issues = (rv.issues || []).filter(i => i.severity !== 'info');
    const info = (rv.issues || []).filter(i => i.severity === 'info');
    const sources = ['All', ...new Set(issues.map(i => i.source))];
    const block = h('section', {class: 'block', id: 'issues'});
    const draw = () => {
      const shown = issues.filter(i => state.issueSource === 'All' || i.source === state.issueSource);
      const left = issues.filter(i => !done.has(i.key)).length;
      block.replaceChildren(h('header', {}, h('h2', {}, 'Issues'),
        h('p', {}, issues.length ? `${left} of ${issues.length} not yet reviewed` : 'Nothing flagged by the checks, SharePoint, the usual-day comparison or AI.')));
      if (!issues.length) { if (info.length) block.append(infoNote(info)); return; }
      if (sources.length > 2) block.append(h('div', {class: 'filters', role: 'group', 'aria-label': 'Filter issues'}, sources.map(s =>
        h('button', {type: 'button', class: 'chip' + (state.issueSource === s ? ' on' : ''), onclick: () => { state.issueSource = s; state.showAll = false; draw(); }},
          s === 'All' ? `All ${issues.length}` : `${s} ${issues.filter(i => i.source === s).length}`))));
      const panel = h('div', {class: 'panel'});
      const limit = state.showAll ? shown.length : 25;
      for (const i of shown.slice(0, limit)) {
        const box = h('input', {type: 'checkbox', checked: done.has(i.key), onchange: e => {
          e.target.checked ? done.add(i.key) : done.delete(i.key); store.set(reviewedKey, [...done]); draw(); }});
        const rowNo = Number(i.row) || Number(String(i.cell || '').replace(/^[A-Z]+/i, '')) || null;
        const body = h('div', {}, h('p', {class: 'src'}, [i.source, i.where].filter(Boolean).join(' · ')), h('p', {class: 'text'}, i.text),
          i.why ? h('p', {class: 'hint'}, 'Why: ' + i.why) : null, i.check ? h('p', {class: 'hint'}, 'Check: ' + i.check) : null);
        const fixBtn = rowNo ? h('button', {type: 'button', class: 'btn small', onclick: async () => {
          const open = body.querySelector('.editor'); if (open) { open.remove(); return; }
          body.append(await rowEditor(i.sheet || (state.report.sheets || [])[0]?.name, rowNo, i.cell ? `flagged cell ${i.cell} is outlined` : null, i.cell)); }}, 'Fix this') : null;
        panel.append(h('div', {class: 'issue' + (done.has(i.key) ? ' done' : '')},
          h('span', {class: 'sev ' + i.severity, title: i.severity}), body,
          h('div', {class: 'side'}, fixBtn, h('label', {}, box, 'Reviewed'))));
      }
      if (shown.length > limit) panel.append(h('button', {type: 'button', class: 'more', onclick: () => { state.showAll = true; draw(); }}, `Show all ${shown.length}`));
      block.append(panel);
      if (info.length) block.append(infoNote(info));
    };
    draw();
    return block;
  }
  const infoNote = info => { const texts = [...new Set(info.map(i => i.text))]; return h('p', {class: 'note'}, 'For information: ' + texts.slice(0, 3).join(' ') + (texts.length > 3 ? ` (and ${texts.length - 3} more)` : '')); };

  // ---------------- sources
  function sourcesBlock(r) {
    const rv = r.review, sp = r.sharepoint || {}, bl = r.baseline || {}, ai = r.ai_review || {};
    const spState = {match: ['Matches SharePoint', 'good'], differences: ['Differs from SharePoint', 'bad'], not_found: ['No SharePoint data found', ''],
      not_configured: ['SharePoint not connected', ''], error: ['SharePoint check failed', 'bad']}[sp.status] || ['Not checked yet', ''];
    const spText = sp.status === 'match' ? `All ${sp.matched_rows} rows and the totals agree with ${sp.source_kind === 'list' ? 'the job\'s SharePoint list' : 'the daily export'}.`
      : sp.status === 'differences' ? `${(sp.findings || []).length} difference(s), listed under Issues. ${sp.matched_rows || 0} of ${sp.report_rows || 0} rows matched.`
      : sp.message || 'The report has not been compared with SharePoint yet.';
    const blState = {normal: ['In line with usual', 'good'], unusual: ['Different from usual', 'bad'], not_enough_history: ['Not enough history', '']}[bl.status] || ['Not compared yet', ''];
    const aiState = {done: [ai.issues?.length ? `${ai.issues.length} suggestion(s)` : 'Nothing spotted', ai.issues?.length ? 'bad' : 'good'], error: ['AI review failed', 'bad'], not_configured: ['AI review off', '']}[ai.status] || ['Not run yet', ''];
    const source = (title, [label, tone], text, link, button) => h('div', {class: 'source'}, h('h3', {}, title),
      h('p', {class: 'state ' + tone}, label), h('p', {}, text), link, button);
    return h('section', {class: 'block'}, h('header', {}, h('h2', {}, 'Checked against')),
      h('div', {class: 'panel sources'},
        source('SharePoint', spState, spText, sp.web_url ? h('a', {href: sp.web_url, target: '_blank', rel: 'noopener'}, 'Open the SharePoint data') : null,
          h('button', {type: 'button', class: 'btn small', onclick: () => rerun('sharepoint', 'Checking SharePoint…')}, 'Check again')),
        source('Usual day', blState, bl.message || 'Compares scans, inspected and reject rate with this job\'s earlier days.', null,
          h('button', {type: 'button', class: 'btn small', onclick: () => rerun('baseline', 'Comparing with earlier days…')}, 'Compare again')),
        source('AI review', aiState, ai.summary || ai.message || 'Claude reads every sheet and points out anything that looks wrong.', null,
          h('button', {type: 'button', class: 'btn small', onclick: () => rerun('ai-review', 'Claude is reading the report…')}, ai.status === 'done' ? 'Run again' : 'Run AI review'))));
  }

  // ---------------- what was read
  function readBlock(r) {
    const rows = [];
    for (const s of r.sheets || []) for (const t of s.tables || []) {
      if (!t.columns || !Object.keys(t.columns).length) continue;
      const cols = Object.entries(t.columns).map(([k, v]) => `${({inspected: 'Inspected', rejected: 'Rejected', certified: 'OK / certified'})[k] || k}: ${v.header}`);
      const labels = Object.entries(t.labels || {}).map(([k, v]) => `${k}: ${v.header || v}`);
      rows.push(h('tr', {}, h('td', {}, s.name), h('td', {}, `Row ${t.header_row}`), h('td', {}, t.kind === 'detail' ? `${t.data_row_count} data rows` : t.kind), h('td', {}, [...cols, ...labels].join('; '))));
    }
    return h('details', {class: 'read'}, h('summary', {}, 'What the desk read from this file'),
      h('div', {class: 'cols panel'}, rows.length ? h('table', {}, h('thead', {}, h('tr', {}, h('th', {}, 'Sheet'), h('th', {}, 'Headers'), h('th', {}, 'Rows'), h('th', {}, 'Columns found'))), h('tbody', {}, rows))
        : h('p', {class: 'note'}, 'No inspection columns were recognised.')),
      h('p', {class: 'note'}, 'Formula cells are checked using the results Excel saved. The original file is stored unchanged.'));
  }
  function historyBlock(r) {
    const items = [];
    if (r.fixed_from) items.push(h('p', {class: 'note'}, `This is a fixed copy of "${r.fixed_from.filename}" (version ${r.fixed_from.version}) with ${r.fixed_from.changes.length} approved change(s): `
      + r.fixed_from.changes.map(c => `${c.sheet} ${c.cell} ${c.before || '(blank)'} → ${c.after}`).join('; ') + '.'));
    for (const f of r.fixes_applied || []) items.push(h('p', {class: 'note'}, `${new Date(f.applied_at).toLocaleString()}: fixed version "${f.fixed_filename}" made with ${f.changes.length} change(s). `,
      h('a', {href: '#r=' + f.fixed_file_hash}, 'Open it')));
    return h('section', {class: 'block'}, h('header', {}, h('h2', {}, 'History')), ...items);
  }

  async function rerun(kind, message) {
    if (state.busy || !state.report) return;
    state.busy = true; toast(message);
    try {
      await api('/api/spreadsheets/' + kind, {file_hash: state.report.file_hash});
      await openReport(state.report.file_hash, true); await loadList();
      const rv = state.report.review;
      toast(kind === 'sharepoint' ? (rv.sharepoint.status === 'match' ? 'Matches SharePoint.' : rv.sharepoint.message || 'SharePoint check finished.')
        : kind === 'baseline' ? rv.usual.message || 'Compared.' : rv.ai.status === 'done' ? 'AI review finished.' : rv.ai.message || 'AI review finished.', rv.ai.status === 'error' && kind === 'ai-review');
    } catch (e) { toast(e.message, true); }
    finally { state.busy = false; }
  }

  // ---------------------------------------------------------------- upload
  const CHUNK = 3 * 1024 * 1024;
  const toBase64 = blob => new Promise((resolve, reject) => { const fr = new FileReader(); fr.onload = () => resolve(String(fr.result).split(',')[1]); fr.onerror = () => reject(new Error('This file could not be read.')); fr.readAsDataURL(blob); });
  const sha256 = async file => Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', await file.arrayBuffer())), b => b.toString(16).padStart(2, '0')).join('');
  async function upload(files) {
    let last = null;
    for (const file of files) last = (await uploadOne(file)) || last;
    if (last) { await loadList(); select(last.file_hash); showResult(last.file_hash); }
  }
  async function uploadOne(file) {
    const meter = h('b'), text = h('span', {}, `Preparing ${file.name}…`);
    const row = h('div', {class: 'uploading'}, text, h('div', {class: 'meter'}, meter));
    $('uploads').prepend(row);
    const problem = /\.(xls|xlsm|xlsb)$/i.test(file.name) ? `${file.name} is an older or macro Excel file. In Excel choose File, Save As, Excel Workbook (.xlsx), then upload that copy.`
      : !/\.(csv|xlsx)$/i.test(file.name) ? `${file.name} is not an Excel (.xlsx) or CSV file.` : file.size < 1 ? `${file.name} is empty.` : null;
    if (problem) { row.classList.add('failed'); text.textContent = problem; return null; }
    try {
      const hash = await sha256(file);
      const id = crypto.randomUUID().replace(/-/g, ''), parts = Math.max(1, Math.ceil(file.size / CHUNK));
      for (let i = 0; i < parts; i++) {
        text.textContent = `Uploading ${file.name}` + (parts > 1 ? ` (part ${i + 1} of ${parts})` : '');
        meter.style.width = `${Math.round((i / parts) * 60)}%`;
        await api('/api/spreadsheets/upload/part', {upload_id: id, index: i, content_base64: await toBase64(file.slice(i * CHUNK, (i + 1) * CHUNK))});
      }
      text.textContent = `Checking ${file.name}: rows, SharePoint, usual day and AI. This can take a minute.`;
      meter.style.width = '75%';
      const result = await api('/api/spreadsheets/upload/finish', {upload_id: id, filename: file.name, part_count: parts, sha256: hash});
      meter.style.width = '100%';
      setTimeout(() => row.remove(), 800);
      if (result.item.idempotent) toast(`${file.name} was already uploaded, so the saved copy was opened.`);
      return result.item;
    } catch (e) { row.classList.add('failed'); text.textContent = `${file.name}: ${e.message}`; return null; }
  }
  $('file-input').addEventListener('change', e => { const files = [...e.target.files]; e.target.value = ''; if (files.length) upload(files); });
  let dragDepth = 0;
  window.addEventListener('dragenter', e => { if ([...(e.dataTransfer?.types || [])].includes('Files')) { dragDepth++; $('drop').hidden = false; } });
  window.addEventListener('dragleave', () => { dragDepth = Math.max(0, dragDepth - 1); if (!dragDepth) $('drop').hidden = true; });
  window.addEventListener('dragover', e => e.preventDefault());
  window.addEventListener('drop', e => { e.preventDefault(); dragDepth = 0; $('drop').hidden = true; const files = [...(e.dataTransfer?.files || [])]; if (files.length) upload(files); });

  // ---------------- after-upload summary
  async function showResult(hash) {
    let data;
    try { data = state.report?.file_hash === hash ? state.report : await api('/api/spreadsheet?id=' + hash); } catch (e) { return; }
    const rv = data.review, dialog = $('result-dialog');
    const top = (rv.issues || []).filter(i => i.severity === 'high' || i.severity === 'medium').slice(0, 5);
    const title = rv.status === 'hold' ? `${rv.counts.high} thing${rv.counts.high === 1 ? '' : 's'} must be fixed before this goes out`
      : rv.status === 'check' ? 'A few things to look at' : 'Nothing flagged';
    dialog.replaceChildren(h('div', {class: 'inner'},
      h('div', {class: `tag tag-big tag-${rv.status}`}, STATUS[rv.status], h('small', {}, statusNote(rv))),
      h('h2', {id: 'result-title'}, title),
      h('p', {class: 'note'}, `${rv.job || data.filename}, ${shortDay(rv.day)}. ${fmt(rv.scans)} rows, ${fmt(rv.inspected)} inspected, ${fmt(rv.rejected)} rejected.`),
      top.length ? h('ul', {}, top.map(i => h('li', {}, h('span', {class: 'sev ' + i.severity}), h('span', {}, i.text)))) : null),
      h('div', {class: 'row'}, h('button', {type: 'button', class: 'btn', onclick: () => dialog.close()}, 'Close'),
        h('button', {type: 'button', class: 'btn primary', onclick: () => { dialog.close(); $('fixes-block')?.scrollIntoView({behavior: 'smooth'}); }},
          rv.status === 'clear' ? 'Open report' : 'Review and fix')));
    dialog.showModal();
  }
  $('result-dialog').addEventListener('click', e => { if (e.target === $('result-dialog')) $('result-dialog').close(); });

  // ---------------------------------------------------------------- start
  loadList().then(route).catch(e => { $('queue-list').replaceChildren(h('p', {class: 'queue-empty'}, e.message)); });
})();

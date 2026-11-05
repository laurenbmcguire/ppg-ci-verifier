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
    state.aiOn = !!hh.ai_review_configured;
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
      h('button', {type: 'button', class: 'btn primary dl-changes', onclick: downloadWithChanges}, 'Download report'),
      h('button', {type: 'button', class: 'btn chat-btn', onclick: openChat}, 'Chat with Claude'),
      h('a', {class: 'btn', href: '/api/spreadsheets/download?id=' + r.file_hash, download: r.filename}, 'Download original'),
      h('button', {type: 'button', class: 'btn', onclick: () => rerun('sharepoint', 'Checking SharePoint…')}, 'Check SharePoint again'),
      h('button', {type: 'button', class: 'btn', onclick: () => rerun('baseline', 'Comparing with earlier days…')}, 'Compare with usual day'),
      h('button', {type: 'button', class: 'btn', onclick: () => rerun('ai-review', 'Claude is reading the report…')}, rv.ai.status === 'done' ? 'Run AI review again' : 'Run AI review')));
    wrap.append(numbers(r));
    wrap.append(h('section', {class: 'block', id: 'sheet-block'}, h('header', {}, h('h2', {}, 'Spreadsheet'), h('p', {}, 'Loading the file…'))));
    wrap.append(h('section', {class: 'block', id: 'fixes-block'}, h('header', {}, h('h2', {}, 'Fixes'), h('p', {}, 'Loading suggested fixes…'))));
    wrap.append(issuesBlock(rv));
    wrap.append(sourcesBlock(r));
    wrap.append(readBlock(r));
    if (rv.fixed_from || rv.fixes_applied) wrap.append(historyBlock(r));
    wrap.append(h('div', {class: 'dlbar', id: 'dlbar', hidden: true}, h('span', {}), h('button', {type: 'button', class: 'btn primary dl-changes', onclick: downloadWithChanges}, 'Download report')));
    $('report').replaceChildren(wrap);
    updateDownloads();
    drawChat();
    if (state.fixes) drawFixes();
    state.grid = null; state.gridSheet = null; state.gridCell = null;
    loadGrid();
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
      state.aiFix = data.ai || {status: 'not_run'};
    } catch (e) { state.fixes = []; state.aiFix = {status: 'not_run'}; }
    drawFixes();
    // first time a report with issues is opened, let Claude draft the fixes
    const open = (state.report?.review?.issues || []).filter(i => i.severity !== 'info').length;
    if (state.aiOn && open && state.aiFix.status === 'not_run' && !state.aiFixTried?.has(hash)) runAiFixes(true);
  }
  async function runAiFixes(auto) {
    const hash = state.report.file_hash;
    (state.aiFixTried ||= new Set()).add(hash);
    if (state.aiFixing) return;
    state.aiFixing = true; drawFixes();
    if (!auto) toast('Claude is working out the fixes…');
    try {
      const out = (await api('/api/spreadsheets/ai-fixes', {file_hash: hash})).ai_fixes || {};
      if (state.selected !== hash) return;
      const data = await api('/api/spreadsheets/fixes', {file_hash: hash});
      state.fixes = data.proposals || []; state.aiFix = data.ai || {};
      // AI fixes start accepted; reject any you disagree with before downloading
      const dec = store.get(decisionsKey(), {});
      for (const f of state.fixes) if ((f.source === 'AI fix' || f.confidence === 'high') && !dec[f.id]) dec[f.id] = {state: 'accepted', after: f.after};
      store.set(decisionsKey(), dec);
      const n = (out.fixes || []).length, k = (out.skipped || []).length;
      const ready = state.fixes.filter(f => dec[f.id]?.state === 'accepted').length;
      toast(n || ready ? `Claude fixed ${n} cell${n === 1 ? '' : 's'}${k ? ` and left ${k} issue${k === 1 ? '' : 's'} for a person` : ''}. ${ready} fix${ready === 1 ? ' is' : 'es are'} accepted; review them, then download.`
        : `Claude found nothing it could fix safely${k ? ` (${k} issue${k === 1 ? '' : 's'} need a person)` : ''}.`);
    } catch (e) { if (!auto || state.selected === hash) toast(e.message, true); }
    finally { state.aiFixing = false; if (state.selected === hash) drawFixes(); }
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
      h('p', {}, fixes.length ? `${fixes.length} suggested${fixes.some(f => f.source === 'AI fix') ? ', including Claude\'s' : ''}. Accept the ones you want, or edit any cell yourself, then download a corrected copy.`
        : state.aiFixing ? 'Claude is reading the report and writing fixes for the issues below…' : 'No fixes yet. Let AI fix the issues, use "Fix this" on an issue below, or edit any cell here.'),
      h('div', {class: 'right'},
        state.aiOn ? h('button', {type: 'button', class: 'btn small' + (state.aiFix?.status === 'done' ? '' : ' primary'), disabled: state.aiFixing, onclick: () => runAiFixes(false)},
          state.aiFixing ? [h('span', {class: 'spin'}), ' Claude is fixing…'] : state.aiFix?.status === 'done' ? 'Ask AI to fix again' : 'Let AI fix the issues') : null,
        fixes.length ? h('button', {type: 'button', class: 'btn small', onclick: () => {
          const d = store.get(decisionsKey(), {}); fixes.filter(f => f.confidence === 'high' || f.source === 'AI fix').forEach(f => { d[f.id] = {state: 'accepted', after: d[f.id]?.after ?? f.after}; }); save(d);
        }}, 'Accept all') : null)));
    const ai = state.aiFix || {};
    if (ai.status === 'done' && (ai.skipped || []).length) block.append(h('details', {class: 'skipped'},
      h('summary', {}, `Claude left ${ai.skipped.length} issue${ai.skipped.length === 1 ? '' : 's'} for a person to fix`),
      h('ul', {}, ai.skipped.map(x => h('li', {}, h('b', {}, x.issue), ' ', x.reason)))));
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
        h('div', {class: 'where'}, `${e.source === 'Claude' ? 'Claude (chat)' : 'Your edit'} · ${e.sheet}, cell ${e.cell}` + (e.header ? ` (${e.header})` : '')),
        e.reason ? h('p', {class: 'why'}, e.reason) : null,
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
    drawGrid();
    updateDownloads();
    if (!state.chatBusy) drawChat();
  }
  function readyChanges() {
    if (!state.report) return {accepted: [], dec: {}, edits: {}, total: 0};
    const dec = store.get(decisionsKey(), {}), edits = store.get(editsKey(), {});
    const accepted = (state.fixes || []).filter(f => !edits[editId(f.sheet, f.cell)] && dec[f.id]?.state === 'accepted');
    return {accepted, dec, edits, total: accepted.length + Object.keys(edits).length};
  }
  function downloadWithChanges() {
    const c = readyChanges();
    if (!c.total) { const a = h('a', {href: '/api/spreadsheets/download?id=' + state.report.file_hash, download: state.report.filename}); document.body.append(a); a.click(); a.remove(); return; }
    $('walk-dialog')?.open && $('walk-dialog').close();
    applyFixes(c.accepted, c.dec, c.edits);
  }
  function updateDownloads() {
    const c = readyChanges(), label = c.total ? `Download with ${c.total} change${c.total === 1 ? '' : 's'}` : 'Download report';
    for (const b of document.querySelectorAll('.dl-changes')) { b.textContent = state.busy ? 'Building the fixed copy…' : label; b.disabled = !!state.busy; }
    const bar = $('dlbar');
    if (bar) { bar.hidden = !c.total; const t = bar.querySelector('span'); if (t) t.textContent = `${c.total} change${c.total === 1 ? '' : 's'} ready. The download is a corrected copy; your original stays as it is.`; }
  }
  async function applyFixes(accepted, dec, edits) {
    if (state.busy) return; state.busy = true; drawFixes(); updateDownloads();
    toast('Building the fixed copy with your changes…');
    try {
      const changes = accepted.map(f => {
        const after = dec[f.id]?.after ?? f.after;
        return {id: f.id, sheet: f.sheet, cell: f.cell, before: f.before, after, reason: f.reason, source: f.source,
          kind: f.kind === 'auto' ? 'auto' : after === f.after ? f.kind : (f.kind === 'number' && after.trim() !== '' && !isNaN(Number(after)) ? 'number' : 'text')};
      });
      for (const e of Object.values(edits || {})) {
        const numeric = e.after.trim() !== '' && !isNaN(Number(e.after.replace(/,/g, ''))) && (e.before === '' || !isNaN(Number(e.before)));
        changes.push({sheet: e.sheet, cell: e.cell, before: e.before, after: e.after, reason: e.reason || 'Edited by reviewer', source: e.source === 'Claude' ? 'Claude (chat)' : 'Your edit', kind: numeric ? 'number' : 'text'});
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

  // ---------------- spreadsheet view
  const colNum = l => [...l].reduce((n, ch) => n * 26 + ch.charCodeAt(0) - 64, 0) - 1;
  const colName = i => { let s = '', n = i + 1; while (n) { const m = (n - 1) % 26; s = String.fromCharCode(65 + m) + s; n = Math.floor((n - 1) / 26); } return s; };
  const splitCell = a => { const m = /^([A-Z]{1,3})(\d+)$/.exec(String(a || '').toUpperCase()); return m ? [colNum(m[1]), Number(m[2])] : null; };
  function marks() {
    const rv = state.report.review, dec = store.get(decisionsKey(), {}), edits = store.get(editsKey(), {});
    const cells = {}, rows = {}, perSheet = {};
    const add = (sheet, key, kind, item) => { (cells[`${sheet}!${key}`] ||= []).push({kind, item}); perSheet[sheet] = (perSheet[sheet] || 0) + 1; };
    for (const i of (rv.issues || []).filter(x => x.severity !== 'info' && x.sheet)) {
      if (i.cell && splitCell(i.cell)) add(i.sheet, i.cell.toUpperCase(), 'issue', i);
      else if (i.row) { (rows[`${i.sheet}!${i.row}`] ||= []).push(i); perSheet[i.sheet] = (perSheet[i.sheet] || 0) + 1; }
    }
    for (const f of state.fixes || []) {
      if (edits[editId(f.sheet, f.cell)] || dec[f.id]?.state === 'rejected') continue;
      add(f.sheet, f.cell, dec[f.id]?.state === 'accepted' ? 'ready' : 'fix', {...f, after: dec[f.id]?.after ?? f.after});
    }
    for (const e of Object.values(edits)) add(e.sheet, e.cell, 'ready', {...e, source: 'Your edit'});
    return {cells, rows, perSheet};
  }
  async function loadGrid(sheet) {
    const block = $('sheet-block'); if (!block) return;
    const hash = state.report.file_hash;
    if (!sheet) { // open the sheet with the most to look at
      const m = marks(), names = (state.report.sheets || []).map(x => x.name);
      sheet = Object.entries(m.perSheet).sort((a, b) => b[1] - a[1])[0]?.[0] || names.find(n => (state.report.sheets.find(x => x.name === n).tables || []).length) || names[0];
    }
    try {
      const g = await api('/api/spreadsheets/sheet', {file_hash: hash, sheet});
      if (state.selected !== hash) return;
      state.grid = g; state.gridSheet = g.sheet; drawGrid(true);
    } catch (e) { block.replaceChildren(h('header', {}, h('h2', {}, 'Spreadsheet')), h('p', {class: 'note'}, e.message)); }
  }
  // One sheet as an HTML table. opts: onlyRows (Set of row numbers), selected (cell), focus {cell, row}
  function buildGrid(g, m, opts = {}) {
    const sheet = g.sheet, formulas = new Set(g.formulas || []), onlyRows = opts.onlyRows;
    const span = {}, covered = new Set();
    for (const ref of g.merges || []) {
      const [a, b] = ref.split(':').map(splitCell); if (!a || !b) continue;
      span[`${a[0]},${a[1]}`] = [b[0] - a[0] + 1, b[1] - a[1] + 1];
      for (let r = a[1]; r <= b[1]; r++) for (let c = a[0]; c <= b[0]; c++) if (r !== a[1] || c !== a[0]) covered.add(`${c},${r}`);
    }
    const hiddenRows = new Set(g.hidden_rows || []);
    let total = 44;
    const colgroup = h('colgroup', {}, h('col', {class: 'rn'}), Array.from({length: g.width}, (_, c) => {
      const w = g.widths?.[c], px = w === 0 ? 0 : w ? Math.max(24, Math.round(w * 7 + 10)) : 104;
      const col = h('col', {}); col.style.width = px + 'px'; total += px; return col; }));
    const thead = h('thead', {}, h('tr', {}, h('th', {class: 'corner'}), Array.from({length: g.width}, (_, c) => h('th', {}, colName(c)))));
    const tbody = h('tbody', {});
    let first = null, focusEl = null;
    const focusCell = opts.focus?.cell ? String(opts.focus.cell).toUpperCase() : null, focusRow = opts.focus?.row || (focusCell ? splitCell(focusCell)?.[1] : null);
    for (let r = 1; r <= g.height; r++) {
      if (onlyRows && !onlyRows.has(r)) continue;
      const rowIssues = m.rows[`${sheet}!${r}`];
      const isFocusRow = !focusCell && focusRow === r;
      const tr = h('tr', {class: [rowIssues ? 'rowflag ' + rowIssues[0].severity : '', hiddenRows.has(r) ? 'hiddenrow' : '', isFocusRow ? 'focusrow' : ''].filter(Boolean).join(' ') || null},
        h('th', {title: rowIssues ? rowIssues.map(i => i.text).join('\n') : null}, String(r)));
      if (isFocusRow) focusEl = tr;
      for (let c = 0; c < g.width; c++) {
        if (!onlyRows && covered.has(`${c},${r}`)) continue;
        const addr = colName(c) + r, list = m.cells[`${sheet}!${addr}`] || [];
        const value = g.rows[r - 1]?.[c] ?? '';
        const ready = list.find(x => x.kind === 'ready'), fix = list.find(x => x.kind === 'fix'), issue = list.find(x => x.kind === 'issue');
        const cls = [formulas.has(addr) ? 'fx' : '', issue ? 'iss ' + issue.item.severity : '', fix ? 'fixme' : '', ready ? 'ready' : '',
          opts.selected === addr ? 'sel' : '', focusCell === addr ? 'focus' : '', /^-?[\d,.]+%?$/.test(value) ? 'num' : ''].filter(Boolean).join(' ');
        const change = ready || fix;
        const td = h('td', {class: cls || null, 'data-cell': addr, title: [addr, ...list.map(x => x.kind === 'issue' ? x.item.text : `${x.item.source}: ${x.item.before || '(blank)'} → ${x.item.after || '(blank)'}`)].join('\n')},
          change ? [h('s', {}, change.item.before || ''), ' ', h('b', {}, change.item.after === '' ? '(blank)' : change.item.after)] : value);
        const sp = !onlyRows && span[`${c},${r}`]; if (sp) { if (sp[0] > 1) td.colSpan = sp[0]; if (sp[1] > 1) td.rowSpan = sp[1]; }
        if (list.length && !first) first = td;
        if (focusCell === addr) focusEl = td;
        tr.append(td);
      }
      tbody.append(tr);
    }
    const table = h('table', {class: 'grid'}, colgroup, thead, tbody);
    table.style.width = total + 'px';
    return {table, first, focusEl};
  }
  // Scroll a grid's box so an element is in view (centered when it has to move).
  function reveal(scroller, el, center) {
    scroller.scrollTop = 0; scroller.scrollLeft = 0;
    if (!el) return;
    const box = scroller.getBoundingClientRect(), r = el.getBoundingClientRect();
    if (center || r.bottom > box.bottom - 10) scroller.scrollTop = Math.max(0, r.top - box.top - (center ? box.height / 2 - r.height / 2 : 80));
    if (r.right > box.right - 10 || (center && r.left - box.left > box.width / 2)) scroller.scrollLeft = Math.max(0, r.left - box.left - (center ? box.width / 2 - Math.min(r.width, box.width / 2) / 2 : 120));
  }
  function drawGrid(scroll) {
    const block = $('sheet-block'), g = state.grid; if (!block || !g) return;
    const m = marks(), sheet = g.sheet;
    const onlyChanged = !!state.gridOnlyMarked;
    const markedRows = new Set();
    for (const k of Object.keys(m.cells)) if (k.startsWith(sheet + '!')) markedRows.add(splitCell(k.slice(sheet.length + 1))[1]);
    for (const k of Object.keys(m.rows)) if (k.startsWith(sheet + '!')) markedRows.add(Number(k.slice(sheet.length + 1)));
    const {table, first} = buildGrid(g, m, {onlyRows: onlyChanged ? markedRows : null, selected: state.gridCell});
    const counts = {flag: 0, fix: 0, ready: 0};
    for (const [k, list] of Object.entries(m.cells)) if (k.startsWith(sheet + '!')) for (const x of list) counts[x.kind === 'issue' ? 'flag' : x.kind]++;
    const tabs = h('div', {class: 'tabs', role: 'tablist'}, (g.sheets || []).map(n => h('button', {type: 'button', role: 'tab', class: n === sheet ? 'on' : '', 'aria-selected': String(n === sheet),
      onclick: () => { state.gridCell = null; loadGrid(n); }}, n, m.perSheet[n] ? h('i', {}, String(m.perSheet[n])) : null)));
    table.addEventListener('click', e => { const td = e.target.closest('td[data-cell]'); if (td) { state.gridCell = td.dataset.cell; drawGrid(); } });
    const scroller = h('div', {class: 'gridwrap'}, table);
    const issueCount = (state.report.review.issues || []).filter(i => i.severity !== 'info').length;
    block.replaceChildren(
      h('header', {}, h('h2', {}, 'Spreadsheet'),
        h('p', {}, 'The file as it is now. Click any cell to see what is wrong with it or to change it.'),
        h('div', {class: 'right'},
          issueCount ? h('button', {type: 'button', class: 'btn primary small', onclick: () => openWalk(0)}, `Go through the ${issueCount} issue${issueCount === 1 ? '' : 's'}`) : null,
          h('label', {class: 'only'}, h('input', {type: 'checkbox', checked: onlyChanged, onchange: e => { state.gridOnlyMarked = e.target.checked; drawGrid(true); }}), 'Only rows with something to fix'))),
      h('div', {class: 'legend'},
        h('span', {class: 'k iss high'}, `Flagged (${counts.flag + Object.keys(m.rows).filter(k => k.startsWith(sheet + '!')).length})`),
        h('span', {class: 'k fixme'}, `Suggested fix (${counts.fix})`),
        h('span', {class: 'k ready'}, `Will change in the download (${counts.ready})`),
        h('span', {class: 'k fx'}, 'Formula')),
      ...[tabs, scroller, cellPanel(m), g.truncated ? h('p', {class: 'note'}, 'Large sheet: only the first rows and columns are shown.') : null].filter(Boolean));
    if (scroll) requestAnimationFrame(() => reveal(scroller, first, false));
  }

  // ---------------- chat with Claude: ask questions, have it make the changes
  const chatKey = () => 'rd-chat:' + state.report.file_hash;
  function queuedList() {
    const c = readyChanges();
    return [...c.accepted.map(f => ({sheet: f.sheet, cell: f.cell, before: f.before, after: c.dec[f.id]?.after ?? f.after, source: f.source})),
      ...Object.values(c.edits).map(e => ({sheet: e.sheet, cell: e.cell, before: e.before, after: e.after, source: e.source || 'Your edit'}))];
  }
  function applyChat(msg) {
    const edits = store.get(editsKey(), {}), dec = store.get(decisionsKey(), {});
    msg.prev = {};
    for (const u of msg.undo || []) {
      const id = editId(u.sheet, u.cell);
      if (edits[id]) { msg.prev[id] = edits[id]; delete edits[id]; }
      for (const f of state.fixes || []) if (f.sheet === u.sheet && f.cell === u.cell && dec[f.id]?.state === 'accepted') { dec[f.id] = {state: 'rejected', after: dec[f.id].after}; (msg.rejected ||= []).push(f.id); }
    }
    for (const ch of msg.changes || []) {
      const id = editId(ch.sheet, ch.cell);
      if (!(id in msg.prev)) msg.prev[id] = edits[id] || null;
      edits[id] = {sheet: ch.sheet, cell: ch.cell, before: ch.before, after: ch.after, header: '', source: 'Claude', reason: ch.reason};
    }
    msg.applied = true;
    store.set(editsKey(), edits); store.set(decisionsKey(), dec);
  }
  function undoChat(msg) {
    const edits = store.get(editsKey(), {}), dec = store.get(decisionsKey(), {});
    for (const [id, prev] of Object.entries(msg.prev || {})) { if (prev) edits[id] = prev; else delete edits[id]; }
    for (const fid of msg.rejected || []) if (dec[fid]) dec[fid].state = 'accepted';
    msg.applied = false;
    store.set(editsKey(), edits); store.set(decisionsKey(), dec);
  }
  function openChat() {
    let panel = $('chat');
    if (!panel) {
      panel = h('aside', {id: 'chat', class: 'chat', 'aria-label': 'Chat with Claude'});
      document.body.append(panel);
    }
    panel.hidden = false; document.body.classList.add('chatting');
    drawChat(); panel.querySelector('textarea')?.focus();
  }
  function closeChat() { const p = $('chat'); if (p) p.hidden = true; document.body.classList.remove('chatting'); }
  function drawChat() {
    const panel = $('chat'); if (!panel || panel.hidden || !state.report) return;
    const log = store.get(chatKey(), []);
    const save = () => store.set(chatKey(), log);
    const list = h('div', {class: 'chat-log', 'aria-live': 'polite'});
    if (!log.length) list.append(h('div', {class: 'chat-hello'},
      h('p', {}, 'Tell me what to change in this report, or ask about anything in it. I make the changes in a corrected copy; nothing is final until you download it.'),
      h('div', {class: 'chat-ideas'}, ['Why is this report on hold?', 'Fix everything you safely can', 'Make the defect reason spellings match', 'Clear any duplicate rows']
        .map(t => h('button', {type: 'button', class: 'chip', onclick: () => send(t)}, t)))));
    for (const m of log) {
      if (m.role === 'user') { list.append(h('div', {class: 'msg me'}, m.content)); continue; }
      const n = (m.changes || []).length, u = (m.undo || []).length;
      list.append(h('div', {class: 'msg ai' + (m.error ? ' err' : '')}, h('div', {class: 'txt'}, m.content),
        n || u ? h('div', {class: 'chg'},
          h('div', {class: 'chg-head'}, h('b', {}, m.applied ? `✓ ${n ? `Changed ${n} cell${n === 1 ? '' : 's'}` : ''}${n && u ? ', ' : ''}${u ? `undid ${u}` : ''}` : 'Changes undone'),
            h('button', {type: 'button', class: 'btn small', onclick: () => { m.applied ? undoChat(m) : applyChat(m); save(); drawFixes(); drawChat(); }}, m.applied ? 'Undo' : 'Redo')),
          h('ul', {}, (m.changes || []).slice(0, 30).map(ch => h('li', {}, h('button', {type: 'button', class: 'cellref', onclick: () => showCell(ch.sheet, ch.cell)}, `${ch.sheet} ${ch.cell}`), ' ',
            h('s', {}, ch.before || '(blank)'), ' → ', h('b', {}, ch.after || '(blank)'))), (m.changes || []).length > 30 ? h('li', {}, `…and ${(m.changes || []).length - 30} more`) : null),
          (m.dropped || []).length ? h('p', {class: 'skipnote'}, `Skipped ${m.dropped.length}: ` + m.dropped.slice(0, 4).map(d => `${d.cell} (${d.why})`).join(', ')) : null) : null));
    }
    if (state.chatBusy) list.append(h('div', {class: 'msg ai'}, h('span', {class: 'spin'}), ' Claude is reading the report…'));
    const box = h('textarea', {rows: 2, placeholder: 'e.g. Change the auditor on every row to Robert Swanke', 'aria-label': 'Message to Claude'});
    box.addEventListener('keydown', e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(box.value); } });
    const c = readyChanges();
    panel.replaceChildren(
      h('div', {class: 'chat-top'}, h('b', {}, 'Chat with Claude'), h('span', {class: 'hint'}, state.report.review?.job || ''),
        log.length ? h('button', {type: 'button', class: 'btn ghost small', onclick: () => { store.set(chatKey(), []); drawChat(); }}, 'New chat') : null,
        h('button', {type: 'button', class: 'btn ghost small', onclick: closeChat}, 'Close')),
      list,
      h('form', {class: 'chat-send', onsubmit: e => { e.preventDefault(); send(box.value); }}, box,
        h('button', {type: 'submit', class: 'btn primary', disabled: !!state.chatBusy}, 'Send')),
      c.total ? h('div', {class: 'chat-dl'}, h('button', {type: 'button', class: 'btn primary dl-changes', onclick: downloadWithChanges}, `Download with ${c.total} change${c.total === 1 ? '' : 's'}`)) : null);
    requestAnimationFrame(() => { list.scrollTop = list.scrollHeight; });
    async function send(text) {
      text = (text || '').trim(); if (!text || state.chatBusy) return;
      const hash = state.report.file_hash;
      log.push({role: 'user', content: text}); save();
      state.chatBusy = true; drawChat();
      try {
        const out = await api('/api/spreadsheets/chat', {file_hash: hash, messages: log.filter(m => !m.error).map(m => ({role: m.role, content: m.content})), queued: queuedList()});
        if (state.report?.file_hash !== hash) return;
        const msg = {role: 'assistant', content: out.reply || 'Done.', changes: out.changes || [], undo: out.undo || [], dropped: out.dropped || []};
        if (msg.changes.length || msg.undo.length) applyChat(msg);
        log.push(msg); save();
        drawFixes();
        if (msg.changes[0]) showCell(msg.changes[0].sheet, msg.changes[0].cell, true);
      } catch (e) {
        log.push({role: 'assistant', content: e.message, error: true}); save();
      } finally { state.chatBusy = false; drawChat(); }
    }
  }
  async function showCell(sheet, cell, quiet) {
    if (!state.grid || state.grid.sheet !== sheet) await loadGrid(sheet);
    state.gridCell = cell; drawGrid();
    const td = document.querySelector(`#sheet-block td[data-cell="${cell}"]`), wrap = document.querySelector('#sheet-block .gridwrap');
    if (!td || !wrap) return;
    if (!quiet) $('sheet-block').scrollIntoView({behavior: 'smooth', block: 'start'});
    reveal(wrap, td, true);
  }

  // ---------------- guided review: one issue at a time, shown in the sheet
  const gridCache = {};
  async function sheetFor(name) {
    const hash = state.report.file_hash, key = hash + '|' + (name || '');
    if (gridCache[key]) return gridCache[key];
    if (state.grid && state.grid.sheet === name) return (gridCache[key] = state.grid);
    return (gridCache[key] = await api('/api/spreadsheets/sheet', {file_hash: hash, sheet: name}));
  }
  const walkIssues = () => (state.report.review.issues || []).filter(i => i.severity !== 'info');
  function fixesFor(issue) {
    const dec = store.get(decisionsKey(), {});
    const rowNo = Number(issue.row) || splitCell(issue.cell)?.[1];
    return (state.fixes || []).filter(f => f.sheet === (issue.sheet || f.sheet) && (issue.cell ? f.cell === String(issue.cell).toUpperCase() : rowNo && splitCell(f.cell)?.[1] === rowNo))
      .map(f => ({...f, after: dec[f.id]?.after ?? f.after, state: dec[f.id]?.state}));
  }
  async function openWalk(index) {
    const issues = walkIssues(); if (!issues.length) return;
    let dlg = $('walk-dialog');
    if (!dlg) { dlg = h('dialog', {id: 'walk-dialog', class: 'walk', 'aria-labelledby': 'walk-title'}); document.body.append(dlg);
      dlg.addEventListener('close', () => { drawFixes(); const old = $('issues'); if (old && state.report) old.replaceWith(issuesBlock(state.report.review)); }); }
    state.walk = {index: Math.max(0, Math.min(index, issues.length - 1)), target: null};
    if (!dlg.open) dlg.showModal();
    await drawWalk();
  }
  async function drawWalk() {
    const dlg = $('walk-dialog'); if (!dlg) return;
    const issues = walkIssues(), w = state.walk, issue = issues[w.index];
    const reviewedKey = 'rd-reviewed:' + state.report.file_hash, done = new Set(store.get(reviewedKey, []));
    const markDone = () => { done.add(issue.key); store.set(reviewedKey, [...done]); };
    const next = () => { markDone(); if (w.index < issues.length - 1) { w.index++; w.target = null; drawWalk(); } else finish(); };
    const finish = () => {
      const ready = (state.fixes || []).filter(f => store.get(decisionsKey(), {})[f.id]?.state === 'accepted').length + Object.keys(store.get(editsKey(), {})).length;
      dlg.replaceChildren(h('div', {class: 'walk-end'},
        h('h2', {id: 'walk-title'}, 'All issues gone through'),
        h('p', {}, ready ? `${ready} change${ready === 1 ? '' : 's'} ready. Download the fixed version to get a corrected copy; it is checked again automatically.` : 'No changes were chosen.'),
        h('div', {class: 'row'}, h('button', {type: 'button', class: 'btn', onclick: () => { w.index = 0; drawWalk(); }}, 'Start over'),
          h('button', {type: 'button', class: 'btn', onclick: () => dlg.close()}, 'Close'),
          ready ? h('button', {type: 'button', class: 'btn primary', onclick: downloadWithChanges}, `Download with ${ready} change${ready === 1 ? '' : 's'}`) : null)));
    };
    const sheetName = issue.sheet || state.grid?.sheet || (state.report.sheets || [])[0]?.name;
    const head = h('div', {class: 'walk-top'},
      h('b', {id: 'walk-title'}, `Issue ${w.index + 1} of ${issues.length}`),
      h('div', {class: 'walk-bar'}, h('i', {})),
      h('button', {type: 'button', class: 'btn primary small dl-changes', onclick: downloadWithChanges}, 'Download report'),
      h('button', {type: 'button', class: 'btn ghost small', onclick: () => dlg.close()}, 'Close'));
    head.querySelector('.walk-bar i').style.width = Math.round((w.index + 1) / issues.length * 100) + '%';
    requestAnimationFrame(updateDownloads);
    const gridBox = h('div', {class: 'walk-grid gridwrap'}, h('p', {class: 'note'}, h('span', {class: 'spin'}), ' Opening the sheet…'));
    const side = h('div', {class: 'walk-side'});
    dlg.replaceChildren(head, h('div', {class: 'walk-body'}, h('div', {class: 'walk-left'}, h('div', {class: 'walk-where'}, `${sheetName || 'Sheet'}${issue.cell ? ' · cell ' + issue.cell : issue.row ? ' · row ' + issue.row : ' · whole report'}`), gridBox), side));
    // issue card
    const fixes = fixesFor(issue);
    const target = w.target || (issue.cell ? String(issue.cell).toUpperCase() : fixes[0]?.cell || null);
    side.append(h('div', {class: 'walk-issue ' + issue.severity},
      h('span', {class: 'chip-sev'}, {high: 'Must fix', medium: 'Check', low: 'Minor'}[issue.severity] || 'Check'), h('span', {class: 'src'}, issue.source),
      h('p', {class: 'text'}, issue.text), issue.why ? h('p', {class: 'hint'}, 'Why: ' + issue.why) : null, issue.check ? h('p', {class: 'hint'}, 'Check: ' + issue.check) : null,
      done.has(issue.key) ? h('p', {class: 'hint done'}, '✓ Reviewed') : null));
    // suggested fixes
    const applyFix = f => { const d = store.get(decisionsKey(), {}); d[f.id] = {state: 'accepted', after: f.after}; store.set(decisionsKey(), d); toast(`${f.sheet} ${f.cell} will change to "${f.after || '(blank)'}".`); next(); };
    if (fixes.length) side.append(h('div', {class: 'walk-fix'}, h('h3', {}, fixes.length > 1 ? 'Suggested fixes' : 'Suggested fix'),
      fixes.map(f => h('div', {class: 'wf' + (f.state === 'accepted' ? ' on' : '')},
        h('div', {}, h('small', {}, `${f.source} · ${f.cell}`), h('div', {class: 'change'}, h('s', {}, f.before || '(blank)'), ' → ', h('b', {}, f.after || '(blank)')), f.reason ? h('small', {}, f.reason) : null),
        f.state === 'accepted' ? h('span', {class: 'tagok'}, '✓ Will apply') : h('button', {type: 'button', class: 'btn primary small', onclick: () => applyFix(f)}, 'Apply this fix')))));
    else side.append(h('p', {class: 'note walk-none'}, state.aiFixing ? 'Claude is still working out fixes…' : issue.source === 'Usual day' ? 'This compares the whole day with usual days, so there is no single cell to change. Check the scans, then mark it reviewed.' : 'No automatic fix for this one. Enter the right value yourself below, or mark it reviewed if nothing needs to change.'));
    // own value
    const own = h('div', {class: 'walk-own'});
    side.append(own);
    // nav
    side.append(h('div', {class: 'walk-nav'},
      h('button', {type: 'button', class: 'btn small', disabled: w.index === 0, onclick: () => { w.index--; w.target = null; drawWalk(); }}, '‹ Back'),
      h('button', {type: 'button', class: 'btn small', onclick: next}, done.has(issue.key) ? 'Next ›' : 'No change needed ›'),
      h('button', {type: 'button', class: 'btn ghost small', onclick: () => { if (w.index < issues.length - 1) { w.index++; w.target = null; drawWalk(); } else finish(); }}, 'Skip for now')));
    // sheet
    let g;
    try { g = await sheetFor(sheetName); } catch (e) { gridBox.replaceChildren(h('p', {class: 'note'}, e.message)); }
    if (state.walk !== w || !g) return;
    const m = marks();
    const focus = target ? {cell: target} : issue.row ? {row: Number(issue.row)} : null;
    const {table, focusEl} = buildGrid(g, m, {focus, selected: w.target});
    table.addEventListener('click', e => { const td = e.target.closest('td[data-cell]'); if (td) { w.target = td.dataset.cell; drawWalk(); } });
    gridBox.replaceChildren(table);
    requestAnimationFrame(() => reveal(gridBox, focusEl, true));
    // own value box for the target cell
    if (target) {
      const [c, r] = splitCell(target), shown = g.rows[r - 1]?.[c] ?? '', formula = (g.formulas || []).includes(target);
      const edits = store.get(editsKey(), {}), mine = edits[editId(g.sheet, target)];
      if (formula) own.append(h('h3', {}, `Your own fix · ${target}`), h('p', {class: 'note'}, `${target} is a formula (shows ${shown || 'blank'}). Click one of the cells it uses in the sheet to change that instead.`));
      else {
        const input = h('input', {value: mine ? mine.after : shown, 'aria-label': `Your value for ${target}`});
        const apply = async () => {
          let before = shown;
          try { const d = await api('/api/spreadsheets/cells', {file_hash: state.report.file_hash, sheet: g.sheet, row: r}); before = (d.cells.find(x => x.cell === target) || {}).before ?? shown; } catch (e) { /* use shown */ }
          if (input.value === before) { toast('That is the value already in the file.'); return; }
          const all = store.get(editsKey(), {}); all[editId(g.sheet, target)] = {sheet: g.sheet, cell: target, before, after: input.value, header: ''}; store.set(editsKey(), all);
          // your value replaces any suggested fix for the same cell
          toast(`${target} will change to "${input.value || '(blank)'}".`); next();
        };
        input.addEventListener('keydown', e => { if (e.key === 'Enter') apply(); });
        own.append(h('h3', {}, fixes.length ? 'Or apply it yourself' : 'Apply it yourself'),
          h('p', {class: 'hint'}, `Cell ${target}` + (w.target ? '' : ' · click another cell in the sheet to change that one instead')),
          h('div', {class: 'own-row'}, input, h('button', {type: 'button', class: 'btn small', onclick: apply}, mine ? 'Update my value' : 'Apply my value')));
        if (!fixes.length) input.focus();
      }
    } else own.append(h('h3', {}, 'Apply it yourself'), h('p', {class: 'hint'}, 'Click the cell you want to change in the sheet.'));
  }

  function cellPanel(m) {
    const g = state.grid, addr = state.gridCell; if (!addr) return null;
    const [c, r] = splitCell(addr), sheet = g.sheet;
    const list = m.cells[`${sheet}!${addr}`] || [], rowIssues = m.rows[`${sheet}!${r}`] || [];
    const value = g.rows[r - 1]?.[c] ?? '', formula = (g.formulas || []).includes(addr);
    const edits = store.get(editsKey(), {}), mine = edits[editId(sheet, addr)];
    const input = h('input', {value: mine ? mine.after : value, disabled: formula, 'aria-label': `New value for ${addr}`});
    const save = async () => {
      const all = store.get(editsKey(), {});
      // the server needs the stored value (not the shown one) to confirm nothing changed underneath
      let before = value;
      try { const d = await api('/api/spreadsheets/cells', {file_hash: state.report.file_hash, sheet, row: r}); before = (d.cells.find(x => x.cell === addr) || {}).before ?? value; } catch (e) { /* use shown value */ }
      if (input.value === before) delete all[editId(sheet, addr)];
      else all[editId(sheet, addr)] = {sheet, cell: addr, before, after: input.value, header: ''};
      store.set(editsKey(), all); drawFixes(); toast(input.value === before ? 'No change.' : `${addr} will change in the download.`);
    };
    const dec = store.get(decisionsKey(), {});
    return h('div', {class: 'cellpanel'},
      h('div', {class: 'cp-head'}, h('b', {}, `${sheet} · ${addr}`), formula ? h('span', {class: 'tagf'}, 'formula') : null,
        h('button', {type: 'button', class: 'btn ghost small', onclick: () => { state.gridCell = null; drawGrid(); }}, 'Close')),
      ...list.filter(x => x.kind === 'issue').map(x => h('p', {class: 'cp-issue ' + x.item.severity}, x.item.text, x.item.check ? h('small', {}, 'Check: ' + x.item.check) : null)),
      ...rowIssues.map(i => h('p', {class: 'cp-issue ' + i.severity}, `Row ${r}: ${i.text}`)),
      ...list.filter(x => x.kind !== 'issue' && x.item.id).map(x => h('div', {class: 'cp-fix'},
        h('span', {}, `${x.item.source}: `, h('s', {}, x.item.before || '(blank)'), ' → ', h('b', {}, x.item.after || '(blank)'), x.item.reason ? h('small', {}, x.item.reason) : null),
        h('button', {type: 'button', class: 'btn small' + (dec[x.item.id]?.state === 'accepted' ? ' on-accept' : ''), onclick: () => {
          const all = store.get(decisionsKey(), {}); all[x.item.id] = {state: all[x.item.id]?.state === 'accepted' ? undefined : 'accepted', after: x.item.after}; store.set(decisionsKey(), all); drawFixes(); }},
          dec[x.item.id]?.state === 'accepted' ? '✓ Accepted' : 'Accept'),
        h('button', {type: 'button', class: 'btn small', onclick: () => {
          const all = store.get(decisionsKey(), {}); all[x.item.id] = {state: 'rejected', after: x.item.after}; store.set(decisionsKey(), all); drawFixes(); }}, 'Reject'))),
      formula ? h('p', {class: 'note'}, `Shows Excel's saved result (${value || 'blank'}). Change the cells this formula uses instead.`)
        : h('div', {class: 'cp-edit'}, h('label', {}, 'Value', input),
            h('button', {type: 'button', class: 'btn primary small', onclick: save}, mine ? 'Update change' : 'Change this cell'),
            mine ? h('button', {type: 'button', class: 'btn small', onclick: () => { const all = store.get(editsKey(), {}); delete all[editId(sheet, addr)]; store.set(editsKey(), all); drawFixes(); }}, 'Undo my change') : null));
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
        h('p', {}, issues.length ? `${left} of ${issues.length} not yet reviewed` : 'Nothing flagged by the checks, SharePoint, the usual-day comparison or AI.'),
        issues.length ? h('div', {class: 'right'}, h('button', {type: 'button', class: 'btn primary small', onclick: () => openWalk(Math.max(0, issues.findIndex(i => !done.has(i.key))))}, left < issues.length && left ? 'Continue reviewing' : 'Go through them one by one')) : null));
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
          h('div', {class: 'side'}, h('button', {type: 'button', class: 'btn small', onclick: () => openWalk(issues.indexOf(i))}, 'Show in sheet'), fixBtn, h('label', {}, box, 'Reviewed'))));
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
        h('button', {type: 'button', class: 'btn primary', onclick: async () => { dialog.close();
          if (rv.status === 'clear') return;
          for (let i = 0; i < 40 && !(state.report?.file_hash === hash && state.fixes); i++) await new Promise(r => setTimeout(r, 250));
          if (state.report?.file_hash === hash) openWalk(0); }},
          rv.status === 'clear' ? 'Open report' : 'Go through the issues')));
    dialog.showModal();
  }
  $('result-dialog').addEventListener('click', e => { if (e.target === $('result-dialog')) $('result-dialog').close(); });

  // ---------------------------------------------------------------- start
  loadList().then(route).catch(e => { $('queue-list').replaceChildren(h('p', {class: 'queue-empty'}, e.message)); });
})();

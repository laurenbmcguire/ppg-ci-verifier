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
        h('span', {class: 'count'}, rv.sent_at ? 'Sent' : rv.approved ? 'Approved' : count),
        h('span', {class: 'file'}, `${r.filename} · v${r.version} · ${ago(r.received_at)}`)));
    }
  }
  $('search').addEventListener('input', e => { state.search = e.target.value; drawQueue(); });
  document.querySelectorAll('.segmented button').forEach(b => b.addEventListener('click', () => { state.filter = b.dataset.filter; store.set('rd-filter', state.filter); drawQueue(); }));

  // ---------------------------------------------------------------- routing
  function select(hash) { if (location.hash !== '#r=' + hash) location.hash = 'r=' + hash; else route(); }
  window.addEventListener('hashchange', route);
  async function route() {
    document.querySelectorAll('.rail .nav[href]').forEach(a => a.classList.toggle('active', a.getAttribute('href') === (location.hash === '#api' ? '#api' : '#')));
    if (location.hash === '#api') { closeChat(); document.body.classList.add('viewing'); state.selected = null; state.report = null; drawQueue(); await showApi(); return; }
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
      if (state.openSendFor && (state.openSendFor === hash || state.openSendFor === 'next' && data.fixed_from)) { state.openSendFor = null; openSend(); }
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
      h('button', {type: 'button', class: 'btn primary send-btn', id: 'send-btn', onclick: openSend}, sendLabel(r)),
      h('button', {type: 'button', class: 'btn dl-changes', onclick: downloadWithChanges}, 'Download report'),
      h('button', {type: 'button', class: 'btn chat-btn', onclick: () => openFixer(false)}, 'AI worksheet fixer'),
      h('button', {type: 'button', class: 'btn chat-btn', onclick: openChat}, 'Chat with Claude'),
      h('a', {class: 'btn', href: '/api/spreadsheets/download?id=' + r.file_hash, download: r.filename}, 'Download original'),
      h('button', {type: 'button', class: 'btn', onclick: () => rerun('sharepoint', 'Checking SharePoint…')}, 'Check SharePoint again'),
      h('button', {type: 'button', class: 'btn', onclick: () => rerun('baseline', 'Comparing with earlier days…')}, 'Compare with usual day'),
      h('button', {type: 'button', class: 'btn', onclick: () => rerun('ai-review', 'Claude is reading the report…')}, rv.ai.status === 'done' ? 'Run AI review again' : 'Run AI review')));
    wrap.append(h('div', {id: 'sendstate'}, ...[sendState(r)].filter(Boolean)));
    wrap.append(h('div', {id: 'finish'}));
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
    const perRow = rv.rejected_unit === 'rows', base = perRow ? rv.scans : rv.inspected;
    const rate = base ? (rv.rejected || 0) / base * 100 : null;
    const cell = (value, label, u, key) => h('div', {}, h('b', {}, value), h('span', {}, label),
      u ? h('em', {class: flagged.has(key) ? 'off' : ''}, `Usual ${u.median}`) : null);
    return h('div', {class: 'numbers'},
      cell(fmt(rv.scans), 'Scans / rows', usual.scans, 'scans'),
      cell(fmt(rv.inspected), 'Inspected', usual.inspected, 'inspected'),
      cell(fmt(rv.rejected), perRow ? 'Failed audits' : 'Rejected', usual.rejected, 'rejected'),
      cell(rate == null ? '—' : rate.toFixed(1) + '%', perRow ? 'Fail rate' : 'Reject rate', usual.reject_rate, 'reject_rate'));
  }

  // ---------------- fixes
  async function loadFixes(hash) {
    try {
      const data = await api('/api/spreadsheets/fixes', {file_hash: hash});
      if (state.selected !== hash) return;
      state.fixes = data.proposals || [];
      state.aiFix = data.ai || {status: 'not_run'};
      const carry = store.get('rd-carry:' + hash, null);
      if (carry) {  // what was ignored on the earlier version stays ignored on this corrected copy
        const dec = store.get(decisionsKey(), {});
        for (const f of state.fixes) if (carry.rejected.some(x => x.sheet === f.sheet && x.cell === f.cell && x.after === f.after)) dec[f.id] = {state: 'rejected'};
        store.set(decisionsKey(), dec);
        store.set('rd-reviewed:' + hash, [...new Set([...store.get('rd-reviewed:' + hash, []), ...carry.reviewed])]);
        try { localStorage.removeItem('rd-carry:' + hash); } catch (e) { /* convenience only */ }
        if (carry.reviewed.length) drawReport();
      }
    } catch (e) { state.fixes = []; state.aiFix = {status: 'not_run'}; }
    drawFixes();
    // first time a report with issues is opened, Claude drafts the fixes, then offers them in the chat
    const rv = state.report?.review || {}, open = (rv.issues || []).filter(i => i.severity !== 'info').length + (rv.dismissed || []).length;
    if (state.aiOn && (rv.ai?.status === 'not_run' || !rv.ai) && !state.reviewTried?.has(hash)) { maybeBrief(hash, true); runReview(hash); return; }
    if (state.aiOn && open && rv.verify?.status !== 'done' && !state.verifyTried?.has(hash)) { maybeBrief(hash, true); runVerify(hash); return; }
    if (state.aiOn && open && state.aiFix.status === 'not_run' && !state.aiFixTried?.has(hash)) { maybeBrief(hash, true); runAiFixes(true); }
    else maybeBrief(hash, false);
  }
  async function runReview(hash) {
    (state.reviewTried ||= new Set()).add(hash);
    state.reviewing = hash; drawChat();
    try { await api('/api/spreadsheets/ai-review', {file_hash: hash}); } catch (e) { /* the double-check and fixes still run */ }
    finally { state.reviewing = null; }
    if (state.selected === hash) { await openReport(hash, true); loadList(); }
  }
  async function runVerify(hash) {
    (state.verifyTried ||= new Set()).add(hash);
    state.verifying = hash; drawChat();
    try {
      const out = await api('/api/spreadsheets/ai-verify', {file_hash: hash});
      state.verifyResult = {hash, ...out};
    } catch (e) { state.verifyResult = {hash, error: e.message}; }
    finally { state.verifying = null; }
    if (state.selected === hash) { await openReport(hash, true); loadList(); }
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
      const dec = store.get(decisionsKey(), {});
      if (auto) { maybeBrief(hash, false); return; }  // the chat offers them
      // asked for by button: accept them; reject any you disagree with before downloading
      for (const f of state.fixes) if ((f.source === 'AI fix' || f.confidence === 'high') && !dec[f.id]) dec[f.id] = {state: 'accepted', after: f.after};
      store.set(decisionsKey(), dec);
      const n = (out.fixes || []).length, k = (out.skipped || []).length;
      const ready = state.fixes.filter(f => dec[f.id]?.state === 'accepted').length;
      toast(n || ready ? `Claude fixed ${n} cell${n === 1 ? '' : 's'}${k ? ` and left ${k} issue${k === 1 ? '' : 's'} for a person` : ''}. ${ready} fix${ready === 1 ? ' is' : 'es are'} accepted; review them, then download.`
        : `Claude found nothing it could fix safely${k ? ` (${k} issue${k === 1 ? '' : 's'} need a person)` : ''}.`);
    } catch (e) { if (!auto) toast(e.message, true); else if (state.selected === hash) maybeBrief(hash, false); }
    finally { state.aiFixing = false; if (state.selected === hash) { drawFixes(); maybeBrief(hash, false); } }
  }
  const restoredKey = () => 'rd-restored:' + state.report.file_hash;
  function allIssues(rv) {
    const back = new Set(store.get(restoredKey(), []));
    return [...(rv.issues || []), ...(rv.dismissed || []).filter(i => back.has(i.key)).map(i => ({...i, restored: true}))];
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
    state.fixSel ||= {};
    const fsel = (state.fixSel[state.report.file_hash] ||= new Set());
    for (const id of [...fsel]) if (!fixes.some(f => f.id === id)) fsel.delete(id);
    if (fixes.length) {
      const picked = fixes.filter(f => fsel.has(f.id));
      const all = h('input', {type: 'checkbox', checked: picked.length === fixes.length, 'aria-label': 'Select all fixes', onchange: e => { fixes.forEach(f => e.target.checked ? fsel.add(f.id) : fsel.delete(f.id)); drawFixes(); }});
      all.indeterminate = picked.length > 0 && picked.length < fixes.length;
      const bulk = st => { const d = store.get(decisionsKey(), {}); for (const f of picked) d[f.id] = {state: st, after: d[f.id]?.after ?? f.after}; save(d); fsel.clear(); drawFixes(); toast(`${picked.length} fix${picked.length === 1 ? '' : 'es'} ${st}.`); };
      panel.append(h('div', {class: 'selbar'}, h('label', {}, all, ' ', picked.length ? `${picked.length} of ${fixes.length} selected` : `Select all ${fixes.length}`),
        h('button', {type: 'button', class: 'btn small primary', disabled: !picked.length, onclick: () => bulk('accepted')}, `Accept selected${picked.length ? ` (${picked.length})` : ''}`),
        h('button', {type: 'button', class: 'btn small', disabled: !picked.length, onclick: () => bulk('rejected')}, `Reject selected${picked.length ? ` (${picked.length})` : ''}`),
        picked.length ? h('button', {type: 'button', class: 'btn ghost small', onclick: () => { fsel.clear(); drawFixes(); }}, 'Clear selection') : null));
    }
    for (const f of fixes) {
      const d = dec[f.id] || {};
      const input = h('input', {value: d.after ?? f.after, 'aria-label': `New value for ${f.sheet} ${f.cell}`, onchange: e => {
        const all = store.get(decisionsKey(), {}); all[f.id] = {state: all[f.id]?.state || 'accepted', after: e.target.value}; save(all); }});
      const set = st => { const all = store.get(decisionsKey(), {}); all[f.id] = {state: all[f.id]?.state === st ? undefined : st, after: input.value}; save(all); };
      const before = f.before === '' ? '(blank)' : f.source === 'Clean-up' ? f.before.replace(/ /g, '·').replace(/ /g, '⍽') : f.before;
      const pick = h('input', {type: 'checkbox', checked: fsel.has(f.id), 'aria-label': `Select ${f.sheet} ${f.cell}`, onchange: e => { e.target.checked ? fsel.add(f.id) : fsel.delete(f.id); drawFixes(); }});
      panel.append(h('div', {class: 'fix' + (d.state ? ' ' + d.state : '') + (fsel.has(f.id) ? ' picked' : '')},
        h('label', {class: 'where'}, pick, ' ', `${f.source} · ${f.sheet}, cell ${f.cell}` + (f.confidence !== 'high' ? ' · check carefully' : '')),
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
    drawFixer();
    updateFinish();
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
  async function applyFixes(accepted, dec, edits, opts = {}) {
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
      const rejected = (state.fixes || []).filter(f => dec[f.id]?.state === 'rejected').map(f => ({sheet: f.sheet, cell: f.cell, after: f.after}));
      store.set('rd-carry:' + answer.item.file_hash, {reviewed: store.get('rd-reviewed:' + state.report.file_hash, []), rejected});
      store.set(decisionsKey(), {}); store.set(editsKey(), {});
      if (opts.download !== false) {
        const link = h('a', {href: '/api/spreadsheets/download?id=' + answer.item.file_hash, download: answer.item.filename});
        document.body.append(link); link.click(); link.remove();
      }
      toast(`${opts.download !== false ? 'Saved and downloaded' : 'Saved'} "${answer.item.filename}" with ${changes.length} change${changes.length === 1 ? '' : 's'}. It has been checked again. The original is unchanged.`);
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
    for (const i of allIssues(rv).filter(x => x.severity !== 'info' && x.sheet)) {
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
  // Claude's opening message: what it found, what it can fix, and an offer to do all the fixes
  const pendingFixes = () => { const c = readyChanges(); return (state.fixes || []).filter(f => !c.edits[editId(f.sheet, f.cell)] && !c.dec[f.id]?.state); };
  function maybeBrief(hash, working) {
    if (!state.report || state.report.file_hash !== hash) return;
    const issues = (state.report.review.issues || []).filter(i => i.severity !== 'info');
    if (!issues.length) return;
    const log = store.get(chatKey(), []);
    if (log.some(m => m.kind === 'brief')) { if (!working) drawChat(); return; }
    if (working || state.aiFixing) { state.briefWaiting = hash; autoOpenChat(); return; }
    state.briefWaiting = null;
    const rv = state.report.review, ai = state.aiFix || {}, fixes = pendingFixes();
    const high = issues.filter(i => i.severity === 'high').length, rest = issues.length - high;
    const fixedCells = new Set(fixes.map(f => `${f.sheet}!${f.cell}`));
    const people = issues.filter(i => !(i.cell && fixedCells.has(`${i.sheet}!${String(i.cell).toUpperCase()}`)) && !fixes.some(f => f.sheet === i.sheet && i.row && splitCell(f.cell)?.[1] === Number(i.row)));
    const lines = [];
    if (rv.ai?.summary) lines.push(rv.ai.summary);
    if (rv.verify?.status === 'done' && rv.verify.dismissed) lines.push(`I double-checked everything that was flagged against the actual cells and dismissed ${rv.verify.dismissed} false alarm${rv.verify.dismissed === 1 ? '' : 's'} (listed under Issues if you want to look).`);
    lines.push(`I found ${issues.length} issue${issues.length === 1 ? '' : 's'}` + (high ? `: ${high} must be fixed` : '') + (high && rest ? `, ${rest} to check` : !high ? '' : '') + '.');
    lines.push(fixes.length ? `I have ${fixes.length} fix${fixes.length === 1 ? '' : 'es'} ready (listed below).` : 'None of them can be fixed safely from the file alone.');
    if (people.length) lines.push(`${people.length} need a person to decide` + (ai.skipped?.length ? ', for example: ' + ai.skipped.slice(0, 2).map(x => x.issue).join('; ') : '') + '.');
    lines.push(fixes.length ? 'Want me to do all the fixes? Or we can go through them one at a time, or you can tell me what to change.' : 'We can go through them one at a time in the sheet, or tell me what to change.');
    log.push({role: 'assistant', kind: 'brief', content: lines.join('\n\n'), offer: fixes.map(f => f.id)});
    store.set(chatKey(), log);
    autoOpenChat(); drawChat();
  }
  function autoOpenChat() {
    const hash = state.report.file_hash, key = 'rd-chat-opened:' + hash;
    if (store.get(key, false) || $('chat')?.hidden === false) return;
    const go = () => { if (state.report?.file_hash === hash) { store.set(key, true); openChat(); } };
    const dlg = $('result-dialog');
    if (dlg?.open) dlg.addEventListener('close', go, {once: true}); else go();
  }
  function doFixes(msg, ids) {
    const dec = store.get(decisionsKey(), {}), byId = Object.fromEntries((state.fixes || []).map(f => [f.id, f]));
    msg.done = [];
    for (const id of ids) if (byId[id] && !dec[id]?.state) { dec[id] = {state: 'accepted', after: byId[id].after}; msg.done.push(id); }
    store.set(decisionsKey(), dec);
    return msg.done.length;
  }
  function undoFixes(msg) {
    const dec = store.get(decisionsKey(), {});
    for (const id of msg.done || []) if (dec[id]?.state === 'accepted') delete dec[id];
    msg.done = []; store.set(decisionsKey(), dec);
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
    if (!log.length && state.briefWaiting !== state.report.file_hash) list.append(h('div', {class: 'chat-hello'},
      h('p', {}, 'Tell me what to change in this report, or ask about anything in it. I make the changes in a corrected copy; nothing is final until you download it.'),
      h('div', {class: 'chat-ideas'}, ['Why is this report on hold?', 'Fix everything you safely can', 'Make the defect reason spellings match', 'Clear any duplicate rows']
        .map(t => h('button', {type: 'button', class: 'chip', onclick: () => send(t)}, t)))));
    for (const m of log) {
      if (m.role === 'user') { list.append(h('div', {class: 'msg me'}, m.content)); continue; }
      if (m.kind === 'brief') {
        const byId = Object.fromEntries((state.fixes || []).map(f => [f.id, f])), offer = (m.offer || []).map(id => byId[id]).filter(Boolean);
        const done = (m.done || []).length;
        list.append(h('div', {class: 'msg ai brief'}, h('div', {class: 'txt'}, m.content),
          offer.length ? h('div', {class: 'chg'}, h('ul', {}, offer.slice(0, 40).map(f => h('li', {},
            h('button', {type: 'button', class: 'cellref', onclick: () => showCell(f.sheet, f.cell)}, `${f.sheet} ${f.cell}`), ' ', h('s', {}, f.before || '(blank)'), ' → ', h('b', {}, f.after || '(blank)'),
            h('small', {}, f.reason || f.source))))) : null,
          h('div', {class: 'offer'},
            offer.length ? (done ? [h('span', {class: 'tagok'}, `✓ Did ${done} fix${done === 1 ? '' : 'es'}`), h('button', {type: 'button', class: 'btn small', onclick: () => { undoFixes(m); save(); drawFixes(); drawChat(); }}, 'Undo')]
              : h('button', {type: 'button', class: 'btn primary small', onclick: () => {
                  const n = doFixes(m, m.offer || []); log.push({role: 'assistant', content: n ? `Done. I queued ${n} fix${n === 1 ? '' : 'es'}; they're green in the sheet. Download when you're ready, or tell me anything else to change.` : 'Those fixes were already decided.'});
                  save(); drawFixes(); drawChat(); if (offer[0]) showCell(offer[0].sheet, offer[0].cell, true); }}, `Do all ${offer.length} fix${offer.length === 1 ? '' : 'es'}`)) : null,
            h('button', {type: 'button', class: 'btn small', onclick: () => openWalk(0)}, 'Go through them with me'),
            offer.length && !done ? h('button', {type: 'button', class: 'btn ghost small', onclick: () => { closeChat(); $('fixes-block')?.scrollIntoView({behavior: 'smooth'}); }}, "I'll pick") : null)));
        continue;
      }
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
    else if (state.briefWaiting === state.report.file_hash && !log.some(m => m.kind === 'brief')) list.append(h('div', {class: 'msg ai'}, h('span', {class: 'spin'}),
      state.reviewing === state.report.file_hash ? ' Claude is reading the whole report…' : state.verifying === state.report.file_hash ? ' Claude is double-checking each flagged item against the actual cells…' : ' Claude is working out fixes for the real issues…'));
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
      ...(c.total ? [h('div', {class: 'chat-dl'}, h('button', {type: 'button', class: 'btn primary dl-changes', onclick: downloadWithChanges}, `Download with ${c.total} change${c.total === 1 ? '' : 's'}`))] : []));
    requestAnimationFrame(() => { list.scrollTop = list.scrollHeight; });
    async function send(text) {
      text = (text || '').trim(); if (!text || state.chatBusy) return;
      const hash = state.report.file_hash;
      log.push({role: 'user', content: text}); save();
      state.chatBusy = true; drawChat();
      try {
        const brief = [...log].reverse().find(m => m.kind === 'brief');
        if (brief && !(brief.done || []).length && /^(yes|yep|yeah|sure|ok(ay)?|please|do (it|them|all|all (the|of the) fixes)|apply (them|all)|fix (them|all|everything))[.! ]*$/i.test(text)) {
          const n = doFixes(brief, brief.offer || []);
          log.push({role: 'assistant', content: n ? `Done. I queued ${n} fix${n === 1 ? '' : 'es'}; they're green in the sheet. Download when you're ready, or tell me anything else to change.` : 'There were no waiting fixes left to do.'});
          save(); drawFixes(); return;
        }
        const suggested = pendingFixes().map(f => ({sheet: f.sheet, cell: f.cell, before: f.before, after: f.after, reason: f.reason, source: f.source}));
        const out = await api('/api/spreadsheets/chat', {file_hash: hash, messages: log.filter(m => !m.error).map(m => ({role: m.role, content: m.content})), queued: queuedList(), suggested});
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

  // ---------------- when every fix and issue is handled: offer the final download and the send
  function progress() {
    const r = state.report; if (!r || state.fixes == null || state.aiFixing || state.reviewing || state.verifying) return null;
    const dec = store.get(decisionsKey(), {}), edits = store.get(editsKey(), {});
    const undecided = state.fixes.filter(f => !dec[f.id]?.state && !edits[editId(f.sheet, f.cell)]).length;
    const reviewed = new Set(store.get('rd-reviewed:' + r.file_hash, []));
    const changed = new Set([...state.fixes.filter(f => dec[f.id]?.state === 'accepted').map(f => editId(f.sheet, f.cell)), ...Object.keys(edits)]);
    const open = allIssues(r.review).filter(i => i.severity !== 'info' && !reviewed.has(i.key) && !(i.cell && changed.has(editId(i.sheet, String(i.cell).toUpperCase())))).length;
    return {undecided, open, ready: readyChanges().total, done: !undecided && !open};
  }
  function downloadFinal() { $('finish-dialog')?.open && $('finish-dialog').close(); downloadWithChanges(); }
  function sendFinal() {
    $('finish-dialog')?.open && $('finish-dialog').close();
    const c = readyChanges();
    if (!c.total) { openSend(); return; }
    state.openSendFor = 'next';                      // the corrected copy opens with the send window
    applyFixes(c.accepted, c.dec, c.edits, {download: false});
  }
  function updateFinish() {
    const slot = $('finish'), r = state.report; if (!slot || !r) return;
    const p = progress(), sent = (r.sent || []).some(x => x.sent);
    state.finishSeen ||= {};
    if (!p || sent) { slot.replaceChildren(); return; }
    const was = state.finishSeen[r.file_hash];
    state.finishSeen[r.file_hash] = p.done;
    if (!p.done) {
      const parts = [p.undecided ? `${p.undecided} fix${p.undecided === 1 ? '' : 'es'} to accept or reject` : null, p.open ? `${p.open} issue${p.open === 1 ? '' : 's'} to fix or mark reviewed` : null].filter(Boolean);
      slot.replaceChildren(h('p', {class: 'finish-left'}, `Before the final report: ${parts.join(' and ')}.`));
      return;
    }
    slot.replaceChildren(h('div', {class: 'finish'},
      h('div', {}, h('b', {}, r.approval ? 'Approved and ready to send' : 'Every fix and issue is handled'),
        h('span', {}, p.ready ? `${p.ready} change${p.ready === 1 ? '' : 's'} will go into the final copy. Your original stays as it is.` : 'No changes are needed. This file is the final report.')),
      h('button', {type: 'button', class: 'btn', onclick: downloadFinal}, 'Download final report'),
      h('button', {type: 'button', class: 'btn primary', onclick: sendFinal}, r.approval ? 'Send to client' : 'Send it to the client for me')));
    if (was === false && !store.get('rd-finish-offered:' + r.file_hash, false)) { store.set('rd-finish-offered:' + r.file_hash, true); offerFinish(p); }
  }
  function offerFinish(p) {
    if (document.querySelector('dialog[open]:not(#finish-dialog)')) { setTimeout(() => offerFinish(p), 1500); return; }
    let dlg = $('finish-dialog');
    if (!dlg) { dlg = h('dialog', {id: 'finish-dialog', class: 'fixer finishdlg', 'aria-labelledby': 'finish-title'}); document.body.append(dlg);
      dlg.addEventListener('click', e => { if (e.target === dlg) dlg.close(); }); }
    const r = state.report;
    dlg.replaceChildren(h('div', {class: 'fixer-wait'},
      h('div', {class: 'finish-mark'}, '✓'),
      h('h2', {id: 'finish-title'}, 'All fixes are handled'),
      h('p', {}, `${r.review.job || r.filename}: ` + (p.ready ? `${p.ready} change${p.ready === 1 ? '' : 's'} will go into the final report.` : 'nothing needs changing.')
        + ' Download it, or approve it and send it to the client from data@ppgquality.com.'),
      h('div', {class: 'row'},
        h('button', {type: 'button', class: 'btn ghost', onclick: () => dlg.close()}, 'Not now'),
        h('button', {type: 'button', class: 'btn', onclick: downloadFinal}, 'Download final report'),
        h('button', {type: 'button', class: 'btn primary', onclick: sendFinal}, 'Send it to the client for me'))));
    if (!dlg.open) dlg.showModal();
  }

  // ---------------- API page: keys and how to call it
  async function showApi() {
    const report = $('report');
    const wrap = h('div', {class: 'sheetwrap apiwrap'});
    report.replaceChildren(wrap);
    const base = location.origin + '/api/v1';
    let index = {endpoints: []}, keys = [], fresh = null;
    try { [index, keys] = await Promise.all([api('/api/v1'), api('/api/keys', {}).then(d => d.keys)]); } catch (e) { toast(e.message, true); }
    const draw = () => {
      const name = h('input', {type: 'text', placeholder: 'What will use it, e.g. Power Automate', 'aria-label': 'Key name', maxlength: '60'});
      const canSend = h('input', {type: 'checkbox'});
      wrap.replaceChildren(
        h('button', {type: 'button', class: 'back', onclick: () => { location.hash = ''; }}, '‹ All reports'),
        h('div', {class: 'head'}, h('div', {}, h('h1', {}, 'API'), h('div', {class: 'meta'}, 'Let other programs upload reports, read the results, apply fixes, approve and send, with the same rules as this screen.'))),
        h('section', {class: 'block'}, h('header', {}, h('h2', {}, 'API keys'), h('p', {}, 'A program sends its key with every call. You see a key once, when you make it; revoke it any time.')),
          fresh ? h('div', {class: 'keynew'}, h('p', {}, h('b', {}, `New key for ${fresh.name}. Copy it now; it will not be shown again.`)),
            h('div', {class: 'keyline'}, h('code', {}, fresh.key), h('button', {type: 'button', class: 'btn small', onclick: e => { navigator.clipboard?.writeText(fresh.key).then(() => { e.target.textContent = 'Copied'; }, () => toast('Select the key and copy it.', true)); }}, 'Copy'))) : null,
          h('div', {class: 'keyform'}, name, h('label', {class: 'tick'}, canSend, 'Can send to clients'),
            h('button', {type: 'button', class: 'btn primary', onclick: async () => {
              try { const d = await api('/api/keys/create', {name: name.value, can_send: canSend.checked}); fresh = d.key; keys = d.keys; draw(); }
              catch (e) { toast(e.message, true); } }}, 'Make a key')),
          keys.length ? h('div', {class: 'tablewrap'}, h('table', {class: 'keys'}, h('thead', {}, h('tr', {}, ['Name', 'Key', 'Can send', 'Made', 'Last used', ''].map(t => h('th', {}, t)))),
            h('tbody', {}, keys.map(k => h('tr', {class: k.revoked_at ? 'revoked' : ''}, h('td', {}, k.name), h('td', {}, h('code', {}, k.prefix + '…')),
              h('td', {}, k.can_send ? 'Yes' : 'No'), h('td', {}, when(k.created_at)), h('td', {}, k.last_used ? ago(k.last_used) : 'Never'),
              h('td', {}, k.revoked_at ? 'Revoked' : h('button', {type: 'button', class: 'btn small', onclick: async () => {
                try { keys = (await api('/api/keys/revoke', {id: k.id})).keys; fresh = null; draw(); toast(`Revoked ${k.name}.`); } catch (e) { toast(e.message, true); } }}, 'Revoke')))))))
            : h('p', {class: 'note'}, 'No keys yet.')),
        h('section', {class: 'block'}, h('header', {}, h('h2', {}, 'How to call it')),
          h('p', {}, 'Base address: ', h('code', {}, base)),
          h('p', {}, 'While the app is behind Vercel sign-in, a program outside it also needs Vercel’s automation header, ', h('code', {}, 'x-vercel-protection-bypass'),
            '. Turn on “Protection Bypass for Automation” in the Vercel project’s Deployment Protection settings and give the program that value with its API key.'),
          h('pre', {class: 'code'}, `curl ${base}/reports \\\n  -H "Authorization: Bearer ppgv_YOUR_KEY" \\\n  -H "x-vercel-protection-bypass: YOUR_VERCEL_VALUE"\n\ncurl -X POST ${base}/reports \\\n  -H "Authorization: Bearer ppgv_YOUR_KEY" -H "Content-Type: application/json" \\\n  -d '{"filename": "2219-HANDS-HMMA 11.05.26.xlsx", "content_base64": "<file as base64>"}'`),
          h('div', {class: 'tablewrap'}, h('table', {class: 'keys endpoints'}, h('thead', {}, h('tr', {}, ['', 'Address', 'What it does'].map(t => h('th', {}, t)))),
            h('tbody', {}, index.endpoints.map(e => h('tr', {}, h('td', {}, h('span', {class: 'verb ' + e.method.toLowerCase()}, e.method)), h('td', {}, h('code', {}, e.path)), h('td', {}, e.description))))))));
    };
    draw();
  }

  // ---------------- approve, then send the final report to the client (from data@ppgquality.com)
  const when = iso => iso ? new Date(iso).toLocaleString('en-US', {month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit'}) : '';
  const kb = n => n >= 1048576 ? (n / 1048576).toFixed(1) + ' MB' : Math.max(1, Math.round(n / 1024)) + ' KB';
  const addrList = v => v.split(/[;,\s]+/).map(x => x.trim()).filter(Boolean);
  function sendState(r) {
    const a = r.approval, sent = (r.sent || []).filter(x => x.sent), last = sent[sent.length - 1];
    if (!a) return null;
    return h('div', {class: 'sendstate' + (last ? ' sent' : '')},
      h('b', {}, last ? `Sent to the client ${when(last.at)}` : 'Approved as final'),
      h('span', {}, last ? `To ${last.to.join(', ')}${last.cc.length ? `, Cc ${last.cc.length}` : ''}. Approved by ${a.by}.`
        : `By ${a.by}, ${when(a.at)}${a.overridden ? `, with ${a.open_must_fix} must-fix issue${a.open_must_fix === 1 ? '' : 's'} open` : ''}. Not sent yet.`),
      h('button', {type: 'button', class: 'btn small', onclick: () => openSend()}, last ? 'Details' : 'Send to client'));
  }
  async function openSend() {
    let dlg = $('send-dialog');
    if (!dlg) { dlg = h('dialog', {id: 'send-dialog', class: 'fixer send', 'aria-labelledby': 'send-title'}); document.body.append(dlg);
      dlg.addEventListener('click', e => { if (e.target === dlg && !state.sending) dlg.close(); }); }
    if (!dlg.open) dlg.showModal();
    const hash = state.report.file_hash;
    state.sendForm = null; state.sendConfirm = false;
    dlg.replaceChildren(h('div', {class: 'fixer-wait'}, h('span', {class: 'spin'}), h('h2', {id: 'send-title'}, 'Approve and send'),
      h('p', {}, 'Finding the last report sent for this job in data@ppgquality.com…')));
    try {
      const info = await api('/api/spreadsheets/send-info', {file_hash: hash});
      if (state.report?.file_hash !== hash) return;
      state.sendInfo = info; drawSend();
    } catch (e) {
      dlg.replaceChildren(h('div', {class: 'fixer-wait'}, h('h2', {id: 'send-title'}, 'Approve and send'), h('p', {class: 'note'}, e.message),
        h('div', {class: 'row'}, h('button', {type: 'button', class: 'btn', onclick: () => dlg.close()}, 'Close'), h('button', {type: 'button', class: 'btn primary', onclick: openSend}, 'Try again'))));
    }
  }
  function drawSend() {
    const dlg = $('send-dialog'), info = state.sendInfo, r = state.report; if (!dlg?.open || !info || !r) return;
    const rv = r.review, approval = r.approval, must = rv.counts?.high || 0, pending = readyChanges().total;
    const newer = (r.fixes_applied || []).slice(-1)[0];
    const sentBefore = (r.sent || []).filter(x => x.sent);
    // form values survive redraws while the dialog is open
    const f = state.sendForm ||= {to: info.to.join(', '), cc: info.cc.join(', '), subject: info.subject, body: info.body, name: info.attachment.name,
      reply: info.previous ? 'reply' : 'new', again: false, override: false, approver: store.get('rd-approver', ''), note: ''};
    const bind = (key, attrs, tag = 'input') => { const el = h(tag, {...attrs, oninput: e => { f[key] = e.target.value; }}); el.value = f[key]; return el; };
    const check = (key, label) => h('label', {class: 'tick'}, h('input', {type: 'checkbox', checked: f[key], onchange: e => { f[key] = e.target.checked; drawSend(); }}), label);
    // step 1: approve
    let step1;
    if (approval) {
      step1 = h('div', {class: 'sstep done'}, h('div', {class: 'shead'}, h('span', {class: 'snum'}, '✓'), h('h3', {}, 'Approved as final')),
        h('p', {}, `${approval.by} approved version ${approval.version} on ${when(approval.at)}.` + (approval.note ? ` Note: ${approval.note}` : '')
          + (approval.overridden ? ` It was approved with ${approval.open_must_fix} must-fix issue${approval.open_must_fix === 1 ? '' : 's'} open.` : '')),
        sentBefore.length ? null : h('button', {type: 'button', class: 'btn ghost small', onclick: () => approve(true)}, 'Withdraw approval'));
    } else {
      const warns = [];
      if (pending) warns.push(h('div', {class: 'swarn'}, h('p', {}, `${pending} change${pending === 1 ? ' is' : 's are'} ready but not in a corrected copy yet. Approve the corrected copy, not this one.`),
        h('button', {type: 'button', class: 'btn small', disabled: state.busy, onclick: () => { state.openSendFor = 'next'; dlg.close(); downloadWithChanges(); }}, `Apply ${pending} change${pending === 1 ? '' : 's'} and continue`)));
      if (newer) warns.push(h('div', {class: 'swarn'}, h('p', {}, `A corrected copy of this report exists: "${newer.fixed_filename}".`),
        h('button', {type: 'button', class: 'btn small', onclick: () => { state.openSendFor = newer.fixed_file_hash; dlg.close(); select(newer.fixed_file_hash); }}, 'Open the corrected copy')));
      if (must) warns.push(h('div', {class: 'swarn high'}, h('p', {}, `${must} must-fix issue${must === 1 ? ' is' : 's are'} still open on this report.`), check('override', 'Approve anyway')));
      step1 = h('div', {class: 'sstep'}, h('div', {class: 'shead'}, h('span', {class: 'snum'}, '1'), h('h3', {}, 'Approve the final report')),
        h('p', {}, `${rv.status === 'clear' ? 'Nothing is flagged.' : statusNote(rv) + '.'} The report goes to the client exactly as this version (${r.filename}, version ${r.version}).`),
        ...warns,
        h('div', {class: 'sgrid'}, h('label', {}, 'Approved by', bind('approver', {type: 'text', placeholder: 'Your name', autocomplete: 'name'})),
          h('label', {}, 'Note (optional)', bind('note', {type: 'text', placeholder: 'For example: checked against the app, 144 = 144'}))),
        h('button', {type: 'button', class: 'btn primary', disabled: !!state.sending || (must > 0 && !f.override) || (!!newer && !f.override) || pending > 0, onclick: () => approve(false)}, 'Approve as final'));
    }
    // step 2: send
    const prev = info.previous;
    const source = prev ? h('p', {class: 'ssource'}, f.reply === 'reply' ? `Reply All on "${prev.subject}", sent ${when(prev.sent_at)}. Recipients come from that email.` : 'New email (not a reply).',
        h('button', {type: 'button', class: 'linkish', onclick: () => { f.reply = f.reply === 'reply' ? 'new' : 'reply'; drawSend(); }}, f.reply === 'reply' ? 'Send as a new email instead' : 'Reply on the earlier email'))
      : h('p', {class: 'ssource warn'}, info.lookup_error ? `Could not read Sent Items: ${info.lookup_error}` :
        info.recipients_from === 'saved' ? 'No earlier report email was found for this job in Sent Items. The recipients below are the ones used the last time this app sent it. Check them.'
          : `No earlier report email was found for ${info.job || 'this job'} in Sent Items. Type the client's addresses carefully.`);
    const canSend = !!approval && info.enabled && !info.attachment.too_big && !state.sending;
    const n = addrList(f.to).length + addrList(f.cc).length;
    const step2 = h('div', {class: 'sstep' + (approval ? '' : ' locked')}, h('div', {class: 'shead'}, h('span', {class: 'snum'}, '2'), h('h3', {}, 'Send to the client')),
      !info.enabled ? h('p', {class: 'ssource warn'}, 'Sending is not set up yet. The app needs permission to use the data@ppgquality.com mailbox.') : null,
      approval ? null : h('p', {}, 'Approve the report first.'),
      source,
      h('div', {class: 'sform'},
        h('label', {}, 'From', h('input', {type: 'text', value: info.sender, disabled: true})),
        h('label', {}, 'To', bind('to', {type: 'text', placeholder: 'client@company.com'})),
        h('label', {}, 'Cc', bind('cc', {type: 'text'})),
        h('label', {}, 'Subject', bind('subject', {type: 'text'})),
        h('label', {}, 'Message', bind('body', {rows: 6}, 'textarea')),
        h('label', {}, 'Attachment', h('span', {class: 'att'}, bind('name', {type: 'text'}), h('em', {}, kb(info.attachment.size))))),
      info.attachment.too_big ? h('p', {class: 'ssource warn'}, 'The file is over 3 MB, too large to send from here. Download it and send it from Outlook.') : null,
      r.fixed_from ? h('p', {class: 'ssource'}, `This is a corrected copy with ${r.fixed_from.changes.length} changed value${r.fixed_from.changes.length === 1 ? '' : 's'}. Make the same changes in the SharePoint list, so it matches what the client receives.`) : null,
      sentBefore.length ? h('div', {class: 'swarn'}, h('p', {}, `Already sent ${when(sentBefore[sentBefore.length - 1].at)}. Only send it again if a corrected report needs to go out.`), check('again', 'Send again')) : null,
      h('div', {class: 'sbtns'},
        h('button', {type: 'button', class: 'btn', disabled: !canSend, onclick: () => deliver('draft')}, 'Save as draft in Outlook'),
        h('button', {type: 'button', class: 'btn primary' + (state.sendConfirm ? ' confirm' : ''), disabled: !canSend || (sentBefore.length > 0 && !f.again),
          onclick: () => { if (!state.sendConfirm) { state.sendConfirm = true; drawSend(); return; } deliver('send'); }},
          state.sending === 'send' ? 'Sending…' : state.sendConfirm ? `Confirm: send to ${n} ${n === 1 ? 'person' : 'people'}` : 'Send to client')),
      h('p', {class: 'note'}, 'Save as draft puts it in Drafts in data@ppgquality.com, so someone can review it in Outlook and send it from there.'));
    const history = (r.sent || []).length ? h('div', {class: 'shist'}, h('h3', {}, 'History'),
      ...[...r.sent].reverse().map(x => h('p', {}, `${x.sent ? 'Sent' : 'Draft saved'} ${when(x.at)}${x.by ? ` by ${x.by}` : ''}: "${x.subject}" to ${x.to.join(', ')}.`,
        x.web_link ? h('a', {href: x.web_link, target: '_blank', rel: 'noopener'}, x.sent ? ' Open in Outlook' : ' Open the draft') : null))) : null;
    dlg.replaceChildren(
      h('div', {class: 'fixer-top'}, h('div', {}, h('p', {class: 'eyebrow'}, 'APPROVE AND SEND'), h('h2', {id: 'send-title'}, `${rv.job || r.filename}, ${shortDay(rv.day)}`)),
        h('button', {type: 'button', class: 'btn', disabled: !!state.sending, onclick: () => dlg.close()}, 'Close')),
      h('div', {class: 'fixer-body'}, step1, step2, history));
  }
  async function approve(withdraw) {
    const f = state.sendForm, hash = state.report.file_hash;
    if (!withdraw && !f.approver.trim()) { toast('Type your name to approve the report.', true); return; }
    state.sending = 'approve'; drawSend();
    try {
      if (!withdraw) store.set('rd-approver', f.approver.trim());
      const out = await api('/api/spreadsheets/approve', {file_hash: hash, approver: f.approver, note: f.note, override: !!f.override, withdraw});
      state.report = {...state.report, ...out.item, review: state.report.review};
      toast(withdraw ? 'Approval withdrawn.' : 'Approved as final. You can send it to the client now.');
      refreshHead(); loadList();
    } catch (e) { toast(e.message, true); }
    finally { state.sending = false; drawSend(); }
  }
  async function deliver(mode) {
    const f = state.sendForm, info = state.sendInfo, hash = state.report.file_hash;
    state.sending = mode; drawSend();
    try {
      const out = await api('/api/spreadsheets/send', {file_hash: hash, mode, again: !!f.again, to: addrList(f.to), cc: addrList(f.cc),
        subject: f.subject, body: f.body, attachment_name: f.name, reply_to: f.reply === 'reply' && info.previous ? info.previous.id : null,
        by: store.get('rd-approver', '')});
      state.report = {...state.report, ...out.item, review: state.report.review};
      toast(mode === 'send' ? `Sent to ${out.record.to.join(', ')} from ${out.record.sender}.` : 'Saved as a draft in data@ppgquality.com. Review and send it from Outlook.');
      refreshHead(); loadList();
    } catch (e) { toast(e.message, true); }
    finally { state.sending = false; state.sendConfirm = false; drawSend(); }
  }
  function refreshHead() {
    const slot = $('sendstate'); if (slot) slot.replaceChildren(...[sendState(state.report)].filter(Boolean));
    const b = $('send-btn'); if (b) b.textContent = sendLabel(state.report);
    updateFinish();
  }
  const sendLabel = r => (r.sent || []).some(x => x.sent) ? 'Sent to client' : r.approval ? 'Send to client' : 'Approve and send';

  // ---------------- AI worksheet fixer: Claude's summary of the errors and of the suggested changes
  async function openFixer(refresh) {
    let dlg = $('fixer-dialog');
    if (!dlg) { dlg = h('dialog', {id: 'fixer-dialog', class: 'fixer', 'aria-labelledby': 'fixer-title'}); document.body.append(dlg);
      dlg.addEventListener('click', e => { if (e.target === dlg) dlg.close(); }); }
    if (!dlg.open) dlg.showModal();
    const hash = state.report.file_hash;
    state.fixerCache ||= {};
    if (refresh || !state.fixerCache[hash]) {
      dlg.replaceChildren(h('div', {class: 'fixer-wait'}, h('span', {class: 'spin'}), h('h2', {id: 'fixer-title'}, 'AI worksheet fixer'),
        h('p', {}, 'Claude is summarizing the errors and the suggested changes…')));
      if (!state.fixes) for (let i = 0; i < 40 && !state.fixes; i++) await new Promise(r => setTimeout(r, 250));
      const dec = store.get(decisionsKey(), {}), edits = store.get(editsKey(), {});
      const extra = Object.values(edits).map(e => ({id: 'edit:' + editId(e.sheet, e.cell), sheet: e.sheet, cell: e.cell, before: e.before, after: e.after, reason: e.reason || 'Edited by reviewer', source: e.source === 'Claude' ? 'Claude (chat)' : 'Your edit'}));
      const skip = Object.entries(dec).filter(([, d]) => d?.state === 'rejected').map(([id]) => id);
      try {
        state.fixerCache[hash] = await api('/api/spreadsheets/ai-summary', {file_hash: hash, extra, skip});
        if (state.fixerSel) delete state.fixerSel[hash];
      } catch (e) {
        if (state.report?.file_hash === hash) dlg.replaceChildren(h('div', {class: 'fixer-wait'}, h('h2', {id: 'fixer-title'}, 'AI worksheet fixer'), h('p', {class: 'note'}, e.message),
          h('div', {class: 'row'}, h('button', {type: 'button', class: 'btn', onclick: () => dlg.close()}, 'Close'), h('button', {type: 'button', class: 'btn primary', onclick: () => openFixer(true)}, 'Try again'))));
        return;
      }
      if (state.report?.file_hash !== hash) return;
    }
    drawFixer();
  }
  function drawFixer() {
    const dlg = $('fixer-dialog'); if (!dlg?.open || !state.report) return;
    const hash = state.report.file_hash, sum = state.fixerCache?.[hash]; if (!sum) return;
    const byId = Object.fromEntries((state.fixes || []).map(f => [f.id, f]));
    const edits = store.get(editsKey(), {});
    const changeOf = id => id.startsWith('edit:') ? (() => { const e = edits[id.slice(5)]; return e ? {...e, id, source: e.source === 'Claude' ? 'Claude (chat)' : 'Your edit'} : null; })() : byId[id];
    const stateOf = id => id.startsWith('edit:') ? (edits[id.slice(5)] ? 'accepted' : 'gone') : (store.get(decisionsKey(), {})[id]?.state || (edits[editId(byId[id]?.sheet, byId[id]?.cell)] ? 'replaced' : null));
    const applyIds = ids => { const d = store.get(decisionsKey(), {}); let n = 0; for (const id of ids) { const f = byId[id]; if (f && !d[id]?.state && !edits[editId(f.sheet, f.cell)]) { d[id] = {state: 'accepted', after: f.after}; n++; } } store.set(decisionsKey(), d); return n; };
    const unapply = ids => { const d = store.get(decisionsKey(), {}); for (const id of ids) if (d[id]?.state === 'accepted') delete d[id]; store.set(decisionsKey(), d); };
    const issuesNow = walkIssues();
    const allIds = sum.change_groups.flatMap(g => g.fix_ids).filter(id => changeOf(id));
    const waiting = allIds.filter(id => !stateOf(id));
    // selection: starts with everything that is still waiting; kept per report while the pop-up is used
    state.fixerSel ||= {};
    if (!state.fixerSel[hash]) state.fixerSel[hash] = new Set(waiting);
    const sel = state.fixerSel[hash];
    for (const id of [...sel]) if (!waiting.includes(id)) sel.delete(id);
    state.fixerOpen ||= new Set();
    const chosen = waiting.filter(id => sel.has(id));
    const ready = readyChanges().total;
    const redraw = () => { drawFixes(); drawFixer(); };
    const sev = {high: 'Must fix', medium: 'Should fix', low: 'Tidy-up'};
    const box = (checked, mixed, onchange, label) => { const c = h('input', {type: 'checkbox', checked, 'aria-label': label, onchange}); c.indeterminate = !!mixed; return c; };
    dlg.replaceChildren(
      h('div', {class: 'fixer-top'}, h('div', {}, h('p', {class: 'eyebrow'}, 'AI WORKSHEET FIXER'), h('h2', {id: 'fixer-title'}, state.report.review.job || state.report.filename)),
        h('button', {type: 'button', class: 'btn ghost small', onclick: () => openFixer(true)}, 'Refresh summary'),
        h('button', {type: 'button', class: 'btn ghost small', onclick: () => dlg.close()}, 'Close')),
      h('div', {class: 'fixer-body'},
        h('p', {class: 'fixer-headline'}, sum.headline),
        sum.next_steps?.length ? h('ol', {class: 'fixer-steps'}, sum.next_steps.map(x => h('li', {}, x))) : null,
        h('div', {class: 'fixer-cols'},
          h('section', {}, h('h3', {}, `Summary of errors · ${sum.error_groups.reduce((a, g) => a + (g.count || 0), 0)}`),
            sum.error_groups.length ? sum.error_groups.map(g => h('div', {class: 'fx-card err ' + g.severity},
              h('div', {class: 'fx-head'}, h('span', {class: 'chip-sev'}, sev[g.severity] || 'Check'), h('b', {}, g.title), h('span', {class: 'fx-count'}, String(g.count))),
              h('p', {}, g.explanation),
              g.examples?.length ? h('ul', {class: 'fx-ex'}, g.examples.map(e => h('li', {}, e))) : null,
              g.issue_keys?.length ? h('button', {type: 'button', class: 'btn small', onclick: () => { const i = issuesNow.findIndex(x => g.issue_keys.includes(x.key)); dlg.close(); openWalk(Math.max(0, i)); }}, 'Show in sheet') : null))
            : h('p', {class: 'note'}, 'No open errors.')),
          h('section', {},
            h('div', {class: 'fx-selhead'}, h('h3', {}, `Summary of suggested changes · ${allIds.length}`),
              waiting.length ? h('span', {class: 'fx-selall'},
                h('button', {type: 'button', class: 'linkish', onclick: () => { waiting.forEach(id => sel.add(id)); drawFixer(); }}, 'Select all'), ' · ',
                h('button', {type: 'button', class: 'linkish', onclick: () => { sel.clear(); drawFixer(); }}, 'Select none')) : null),
            sum.change_groups.length ? sum.change_groups.map((g, gi) => {
              const ids = g.fix_ids.filter(id => changeOf(id)), open = ids.filter(id => !stateOf(id)), done = ids.filter(id => stateOf(id) === 'accepted');
              const picked = open.filter(id => sel.has(id)).length, expanded = state.fixerOpen.has(gi), shown = expanded ? ids : ids.slice(0, 6);
              return h('div', {class: 'fx-card chg-card' + (open.length ? '' : ' done')},
                h('label', {class: 'fx-head'},
                  open.length ? box(picked === open.length, picked > 0 && picked < open.length, e => { open.forEach(id => e.target.checked ? sel.add(id) : sel.delete(id)); drawFixer(); }, `Select all in ${g.title}`) : null,
                  h('span', {class: 'risk ' + g.risk}, g.risk === 'safe' ? 'Safe' : 'Check first'), h('b', {}, g.title),
                  h('span', {class: 'fx-count'}, open.length ? `${picked} of ${open.length}` : String(ids.length))),
                h('p', {}, g.explanation),
                h('ul', {class: 'fx-list'}, shown.map(id => { const c = changeOf(id), st = stateOf(id);
                  return h('li', {class: st ? 'applied' : ''},
                    st ? h('span', {class: 'okmark', title: st === 'accepted' ? 'Will be in the download' : 'Replaced by your own value'}, st === 'accepted' ? '✓' : '–')
                      : box(sel.has(id), false, e => { e.target.checked ? sel.add(id) : sel.delete(id); drawFixer(); }, `${c.sheet} ${c.cell}`),
                    h('button', {type: 'button', class: 'cellref', onclick: () => { dlg.close(); showCell(c.sheet, c.cell); }}, `${c.sheet} ${c.cell}`),
                    h('span', {class: 'mono'}, h('s', {}, c.before || '(blank)'), ' → ', h('b', {}, c.after || '(blank)'))); }),
                  ids.length > 6 ? h('li', {class: 'more'}, h('button', {type: 'button', class: 'linkish', onclick: () => { expanded ? state.fixerOpen.delete(gi) : state.fixerOpen.add(gi); drawFixer(); }},
                    expanded ? 'Show fewer' : `Show all ${ids.length}`)) : null),
                done.length && !g.fix_ids.every(id => id.startsWith('edit:')) ? h('div', {class: 'fx-act'}, h('span', {class: 'tagok'}, `✓ ${done.length} applied`),
                  h('button', {type: 'button', class: 'btn ghost small', onclick: () => { unapply(done); done.forEach(id => sel.add(id)); redraw(); }}, 'Undo')) : null);
            }) : h('p', {class: 'note'}, 'No suggested changes. Use the chat or the sheet to make your own.')))),
      h('div', {class: 'fixer-bar'}, h('span', {}, (chosen.length ? `${chosen.length} of ${waiting.length} selected` : waiting.length ? 'Nothing selected' : 'All suggested changes decided') + (ready ? ` · ${ready} ready for the corrected copy` : '')),
        h('button', {type: 'button', class: 'btn', disabled: !chosen.length, onclick: () => { const n = applyIds(chosen); toast(`${n} change${n === 1 ? '' : 's'} applied.`); redraw(); }}, `Apply selected (${chosen.length})`),
        h('button', {type: 'button', class: 'btn primary', disabled: !chosen.length && !ready, onclick: () => { applyIds(chosen); dlg.close(); drawFixes(); downloadWithChanges(); }},
          chosen.length ? 'Apply selected and download' : 'Download corrected copy')));
  }

  // ---------------- guided review: one issue at a time, shown in the sheet
  const gridCache = {};
  async function sheetFor(name) {
    const hash = state.report.file_hash, key = hash + '|' + (name || '');
    if (gridCache[key]) return gridCache[key];
    if (state.grid && state.grid.sheet === name) return (gridCache[key] = state.grid);
    return (gridCache[key] = await api('/api/spreadsheets/sheet', {file_hash: hash, sheet: name}));
  }
  const walkIssues = () => allIssues(state.report.review).filter(i => i.severity !== 'info');
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
    const markDone = () => { done.add(issue.key); store.set(reviewedKey, [...done]); updateFinish(); };
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
    const issues = allIssues(rv).filter(i => i.severity !== 'info');
    const info = (rv.issues || []).filter(i => i.severity === 'info');
    const dismissed = (rv.dismissed || []).filter(i => !new Set(store.get(restoredKey(), [])).has(i.key));
    const sources = ['All', ...new Set(issues.map(i => i.source))];
    const block = h('section', {class: 'block', id: 'issues'});
    const draw = () => {
      const shown = issues.filter(i => state.issueSource === 'All' || i.source === state.issueSource);
      const left = issues.filter(i => !done.has(i.key)).length;
      block.replaceChildren(h('header', {}, h('h2', {}, 'Issues'),
        h('p', {}, issues.length ? `${left} of ${issues.length} not yet reviewed` + (rv.verify?.status === 'done' ? ' · double-checked by Claude' : state.verifying ? ' · Claude is double-checking…' : '') : dismissed.length ? 'Nothing left: Claude checked every flagged item and none are real problems.' : 'Nothing flagged by the checks, SharePoint, the usual-day comparison or AI.'),
        issues.length ? h('div', {class: 'right'}, h('button', {type: 'button', class: 'btn primary small', onclick: () => openWalk(Math.max(0, issues.findIndex(i => !done.has(i.key))))}, left < issues.length && left ? 'Continue reviewing' : 'Go through them one by one')) : null));
      if (!issues.length) { if (dismissed.length) block.append(dismissedList(dismissed)); if (info.length) block.append(infoNote(info)); return; }
      if (sources.length > 2) block.append(h('div', {class: 'filters', role: 'group', 'aria-label': 'Filter issues'}, sources.map(s =>
        h('button', {type: 'button', class: 'chip' + (state.issueSource === s ? ' on' : ''), onclick: () => { state.issueSource = s; state.showAll = false; draw(); }},
          s === 'All' ? `All ${issues.length}` : `${s} ${issues.filter(i => i.source === s).length}`))));
      const panel = h('div', {class: 'panel'});
      const limit = state.showAll ? shown.length : 25;
      for (const i of shown.slice(0, limit)) {
        const box = h('input', {type: 'checkbox', checked: done.has(i.key), onchange: e => {
          e.target.checked ? done.add(i.key) : done.delete(i.key); store.set(reviewedKey, [...done]); draw(); updateFinish(); }});
        const rowNo = Number(i.row) || Number(String(i.cell || '').replace(/^[A-Z]+/i, '')) || null;
        const badge = i.restored ? h('span', {class: 'vb unsure'}, 'Put back by you') : i.verdict === 'valid' ? h('span', {class: 'vb ok'}, '✓ Claude confirmed') : i.verdict === 'unsure' ? h('span', {class: 'vb unsure'}, 'Claude unsure') : null;
        const body = h('div', {}, h('p', {class: 'src'}, [i.source, i.where].filter(Boolean).join(' · '), badge ? ' ' : null, badge), h('p', {class: 'text'}, i.text),
          i.verdict_reason && !i.restored ? h('p', {class: 'hint'}, 'Claude: ' + i.verdict_reason) : null,
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
      if (dismissed.length) block.append(dismissedList(dismissed));
      if (info.length) block.append(infoNote(info));
    };
    draw();
    return block;
  }
  function dismissedList(list) {
    return h('details', {class: 'dismissed'}, h('summary', {}, `Claude checked and dismissed ${list.length} false alarm${list.length === 1 ? '' : 's'}`),
      h('ul', {}, list.map(i => h('li', {}, h('div', {}, h('b', {}, i.text), h('small', {}, [i.source, i.where].filter(Boolean).join(' · ') + (i.verdict_reason ? ' · Claude: ' + i.verdict_reason : ''))),
        h('button', {type: 'button', class: 'btn small', onclick: () => { const b = new Set(store.get(restoredKey(), [])); b.add(i.key); store.set(restoredKey(), [...b]); drawReport(); if (state.fixes) drawFixes(); toast('Put back in the issue list.'); }}, 'Put back')))));
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

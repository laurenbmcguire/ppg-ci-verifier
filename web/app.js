'use strict';
(() => {
  // ---------------------------------------------------------------- helpers
  const $ = id => document.getElementById(id);
  // an optional part that is empty (null/false) is left out, never printed as the word "null"
  for (const name of ['replaceChildren', 'append']) {
    const real = Element.prototype[name];
    Element.prototype[name] = function (...nodes) { return real.apply(this, nodes.flat().filter(n => n != null && n !== false)); };
  }
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
    if (response.status === 401 && data.signin) { showSignIn(); throw new Error('Sign in to continue.'); }
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
  api('/api/health').then(hh => { state.health = hh;
    state.aiOn = !!hh.ai_review_configured;
    const box = $('connections'); box.replaceChildren(
      h('span', {class: 'conn' + (hh.storage === 'vercel-postgres' ? ' on' : '')}, h('i'), 'Saved in the cloud'),
      h('span', {class: 'conn' + (hh.sharepoint_configured ? ' on' : '')}, h('i'), hh.sharepoint_configured ? 'SharePoint connected' : 'SharePoint off'),
      h('span', {class: 'conn' + (hh.ai_review_configured ? ' on' : '')}, h('i'), hh.ai_review_configured ? 'AI review on' : 'AI review off'));
  }).catch(() => {});

  // ---------------------------------------------------------------- client tabs: one per client site on SharePoint
  state.client = store.get('rd-client', '');
  const jobNumbers = job => String(job || '').match(/\d{3,}/g) || [];
  function clientOf(job) {
    if (!job) return null;
    const nums = jobNumbers(job), up = String(job).toUpperCase();
    for (const c of state.clients || []) for (const j of c.jobs) if (j.job.toUpperCase() === up || nums.some(n => j.numbers.includes(n))) return c.key;
    return null;
  }
  // A report's client comes from its saved assignment (registry, source URLs, or a person's choice), never from the job number alone:
  // the same job label can be a list on two client sites. Unsettled reports go under "Needs client".
  const SETTLED = ['manual', 'verified', 'matched', 'source'];
  function clientOfReport(r) {
    const a = r && r.assignment;
    if (a) return SETTLED.includes(a.status) ? a.client_key : null;
    return clientOf(r?.review?.job);
  }
  const inClientR = r => !state.client || (state.client === '_none' ? !clientOfReport(r) : clientOfReport(r) === state.client);
  const inClient = job => !state.client || (state.client === '_none' ? !clientOf(job) : clientOf(job) === state.client);
  const clientName = () => (state.clients || []).find(c => c.key === state.client)?.client || (state.client === '_none' ? 'other' : '');
  async function loadClients(refresh) {
    try { const out = await api('/api/clients', refresh ? {refresh: true} : {}); state.clients = out.clients || []; state.clientsError = out.error || null; }
    catch (e) { state.clients = []; }
    if (state.client && state.client !== '_none' && !state.clients.some(c => c.key === state.client)) { state.client = ''; store.set('rd-client', ''); }
    drawClientTabs(); drawQueue();
  }
  function drawClientTabs() {
    const bar = $('clienttabs'), rail = $('railclients'); if (!bar) return;
    const cl = state.clients || [];
    bar.hidden = !cl.length; if (rail) rail.hidden = !cl.length; if (!cl.length) return;
    const ofKey = (r, key) => key === '' || (key === '_none' ? !clientOfReport(r) : clientOfReport(r) === key);
    const need = key => visibleList().filter(r => ofKey(r, key) && r.review && r.review.status !== 'clear' && !r.review.sent_at).length;
    const go = key => { location.hash = key ? 'c=' + encodeURIComponent(key) : ''; };
    const tab = (key, label, title) => { const n = need(key);
      return h('button', {type: 'button', role: 'tab', class: state.client === key ? 'on' : '', 'aria-selected': String(state.client === key), title: title || null, onclick: () => go(key)},
        label, n ? h('i', {title: `${n} report${n === 1 ? '' : 's'} need attention`}, String(n)) : null); };
    if (rail) {
      const onList = !location.hash.match(/^#(today|overview|kpis|periods|checks|api|users)$/);
      const item = (key, label, title) => { const n = need(key), href = '#c=' + encodeURIComponent(key);
        return h('a', {class: 'nav client' + (onList && state.client === key ? ' active' : ''), href, title: title || null}, h('span', {}, label), n ? h('i', {title: `${n} need attention`}, String(n)) : null); };
      rail.replaceChildren(h('div', {class: 'rail-head'}, 'Clients'),
        ...[...cl.map(c => item(c.key, c.client, `${c.sites.map(x => x.location || x.site).join(', ')}: ${c.jobs.length} job${c.jobs.length === 1 ? '' : 's'} on SharePoint`)),
          state.list.some(r => !clientOfReport(r)) ? item('_none', 'Needs client', 'Reports whose client or job is not settled yet') : null].filter(Boolean));
    }
    const unmatched = state.list.some(r => !clientOfReport(r));
    bar.replaceChildren(...[tab('', 'All clients'),
      ...cl.map(c => tab(c.key, c.client, `${c.sites.map(x => x.location || x.site).join(', ')}: ${c.jobs.length} job${c.jobs.length === 1 ? '' : 's'} on SharePoint`)),
      unmatched ? tab('_none', 'Needs client', 'Reports whose client or job is not settled yet') : null].filter(Boolean));
  }

  // ---------------------------------------------------------------- queue
  // An empty inventory and a failed or refused inventory request look different: a failure never says "No reports yet".
  async function loadList() {
    let data;
    try { data = await api('/api/spreadsheets'); }
    catch (e) { state.listError = e.message || 'The report list could not be loaded.'; state.list ||= []; drawQueue(); drawClientTabs(); throw e; }
    state.listError = null;
    state.list = data.spreadsheets || [];
    state.listTotal = data.count;
    drawQueue(); drawClientTabs();
  }
  // an upload the app cleaned automatically is shown once: as its cleaned copy (the original opens from that report)
  const visibleList = () => { const cleaned = new Set(state.list.flatMap(r => r.review?.auto_hidden || [])); return state.list.filter(r => !cleaned.has(r.file_hash)); };
  function drawQueue() {
    const q = state.search.trim().toLowerCase(), shown = visibleList();
    let rows = shown.filter(r => inClientR(r) && (!q || [r.filename, r.review?.job, r.review?.day].some(v => (v || '').toLowerCase().includes(q))));
    if (state.filter === 'attention') rows = rows.filter(r => !r.review || r.review.status !== 'clear');
    if (state.filter === 'review') rows = rows.filter(r => r.review?.review_state === 'waiting');
    document.querySelectorAll('.segmented button').forEach(b => b.classList.toggle('on', b.dataset.filter === state.filter));
    const mine = shown.filter(r => inClientR(r));
    const attention = mine.filter(r => !r.review || r.review.status !== 'clear' && !r.review.sent_at).length, waiting = mine.filter(r => r.review?.review_state === 'waiting').length;
    const label = {attention: 'To do', all: 'All', review: 'For review'};
    document.querySelectorAll('.segmented button').forEach(b => { const n = b.dataset.filter === 'attention' ? attention : b.dataset.filter === 'review' ? waiting : 0;
      b.replaceChildren(label[b.dataset.filter] || b.textContent, n ? h('i', {class: 'segn'}, String(n)) : null); });
    const todo = shown.filter(r => r.review && r.review.status !== 'clear' && !r.review.sent_at).length;
    document.title = (todo ? `(${todo}) ` : '') + 'PPG Verifier';
    const list = $('queue-list'); list.replaceChildren();
    if (state.listError) {
      list.append(h('div', {class: 'listerror', role: 'alert'}, h('b', {}, 'The report list could not be loaded.'),
        h('span', {}, ` ${state.listError} Saved reports are not affected; this is a loading problem, not an empty list. `),
        h('button', {type: 'button', class: 'linkish', onclick: () => loadList().catch(() => {})}, 'Try again')));
      if (!state.list.length) return;
    }
    const client = (state.clients || []).find(c => c.key === state.client);
    if (client) {
      list.append(h('div', {class: 'clienthead'}, h('b', {}, client.client),
        h('span', {}, `${client.sites.map(x => x.location || x.site).join(' · ')} · ${client.jobs.length} job${client.jobs.length === 1 ? '' : 's'} on SharePoint`),
        client.jobs.length ? h('button', {type: 'button', class: 'linkish', onclick: async e => {
          e.target.disabled = true; e.target.textContent = 'Adding to Today…';
          try { for (const j of client.jobs) await api('/api/today', {add: j.job}); toast(`${client.client}'s ${client.jobs.length} job${client.jobs.length === 1 ? ' is' : 's are'} on Today.`); location.hash = 'today'; }
          catch (err) { toast(err.message, true); e.target.disabled = false; e.target.textContent = 'Watch these jobs on Today'; } }}, 'Watch these jobs on Today') : null));
    }
    const vh = state.vinHits;
    if (vh && vh.query === state.search) {
      list.append(h('div', {class: 'vinhits'}, h('b', {}, vh.results.length ? `“${vh.query}” is in ${vh.results.length} report row${vh.results.length === 1 ? '' : 's'}` : `No report has the VIN “${vh.query}” (search looks at VINs and file names)`),
        ...vh.results.slice(0, 12).map(m => h('button', {type: 'button', class: 'vinhit', onclick: () => select(m.file_hash)},
          h('span', {}, h('b', {}, m.identifier), ` · ${m.job || 'Unknown job'} · ${shortDay(m.day) || 'no date'}`),
          h('span', {class: 'file'}, `${m.sheet}, row ${m.row} · ${m.filename}${m.corrected ? ' (corrected copy)' : ''}${m.sent ? ' · sent' : ''}`))),
        vh.still_indexing ? h('p', {class: 'note'}, `Still indexing ${vh.still_indexing} older report(s); search again in a moment.`) : null));
    }
    if (!rows.length) {
      list.append(h('p', {class: 'queue-empty'}, state.list.length
        ? (state.client && !q && !state.list.some(r => inClientR(r)) ? `No reports for ${clientName()} yet.` : state.filter === 'review' && !q ? 'Nothing is waiting for review.' : state.filter === 'attention' && !q ? h('span', {}, 'Nothing needs your attention. ', h('button', {type: 'button', class: 'linkish', onclick: () => { state.filter = 'all'; store.set('rd-filter', 'all'); drawQueue(); }}, 'Show all reports')) : 'No reports match that search.')
        : 'No reports yet. Upload an end-of-shift report to start.'));
      return;
    }
    const dayOf = r => r.review?.day || (r.received_at || '').slice(0, 10);
    rows = [...rows].sort((a, b) => dayOf(b).localeCompare(dayOf(a)) || (b.received_at || '').localeCompare(a.received_at || ''));   // newest report day first, one heading per day
    let lastDay = null;
    for (const r of rows) {
      const day = dayOf(r);
      if (day !== lastDay) { list.append(h('div', {class: 'day'}, dayLabel(day))); lastDay = day; }
      const rv = r.review || {status: 'check', counts: {}};
      const c = rv.counts || {};
      const count = rv.status === 'clear' ? 'Clear' : rv.status === 'hold' ? `${c.high} to fix` : `${(c.medium || 0) + (c.low || 0)} to look at`;
      list.append(h('button', {type: 'button', class: 'item' + (r.file_hash === state.selected ? ' sel' : ''), onclick: () => select(r.file_hash),
        'aria-label': `${rv.job || r.filename}, ${STATUS[rv.status] || 'Check'}, ${count}`},
        h('span', {class: 'tag tag-' + rv.status}, STATUS[rv.status] || 'Check'),
        h('span', {class: 'job', title: rv.job ? null : 'No job number was found in this file. The file name is shown instead.'}, (rv.job || r.filename.replace(/\.(xlsx|xlsm|xls|csv)$/i, '').replace(/\s*\(fixed\)/g, ''))), rv.fixed_from ? h('span', {class: 'vtag', title: 'A corrected copy; the original upload is kept too'}, 'corrected') : null,
        h('span', {class: 'count'}, rv.sent_at ? 'Sent' : rv.approved ? 'Approved' : rv.review_state === 'waiting' ? 'For review' : rv.review_state === 'sent_back' ? 'Sent back' : count),
        h('span', {class: 'file'}, (r.assignment ? (SETTLED.includes(r.assignment.status) ? `${r.assignment.client} → ` : 'Needs client · ') : '') + `${r.filename} · v${r.version} · ${ago(r.received_at)}`)));
    }
  }
  // Enter in a one-line add box (job to watch, rule, key name...) presses that box's button
  document.addEventListener('keydown', e => {
    if (e.key !== 'Enter' || e.isComposing || !e.target.matches('div.keyform input[type=text], div.keyform input:not([type])')) return;
    const btn = [...e.target.closest('div.keyform').querySelectorAll('button')].find(b => !b.disabled && !b.classList.contains('ghost'));
    if (btn) { e.preventDefault(); btn.click(); }
  });
  // keyboard: J/K or ↓/↑ next and previous report, / search, U upload, Esc back to the list, ? help
  document.addEventListener('keydown', e => {
    if (e.ctrlKey || e.metaKey || e.altKey || e.target.closest('input, textarea, select, [contenteditable], dialog') || document.querySelector('dialog[open]')) return;
    const onReports = document.body.dataset.page === 'reports';
    // inside a report, J/K step through corrections and Esc closes the drawer or panel first: the reviewer never loses their place
    if (document.body.classList.contains('reportmode') && ['j', 'k', 'ArrowDown'].includes(e.key)) return;
    if (document.body.classList.contains('reportmode') && e.key === 'Escape' && (document.querySelector('.fd-drawer') || state.fdCur || document.body.classList.contains('fd-panel-open') || document.body.classList.contains('navopen'))) {
      $('fd-evidence')?.replaceChildren(); document.body.classList.remove('fd-panel-open', 'navopen');
      if (!document.querySelector('.fd-drawer') && state.fdCur) { state.fdCur = null; drawSide(); drawGrid(); }
      return;
    }
    const move = step => {
      const items = [...document.querySelectorAll('#queue-list .item')]; if (!items.length) return;
      const at = items.findIndex(b => b.classList.contains('sel'));
      items[Math.max(0, Math.min(items.length - 1, at < 0 ? 0 : at + step))].click();
    };
    if (onReports && (e.key === 'j' || e.key === 'ArrowDown' && !state.selected)) { e.preventDefault(); move(1); }
    else if (onReports && e.key === 'k') { e.preventDefault(); move(-1); }
    else if (e.key === '/') { e.preventDefault(); if (!onReports || state.selected) location.hash = state.client ? 'c=' + encodeURIComponent(state.client) : ''; setTimeout(() => $('search')?.focus(), 50); }
    else if (e.key === 'u') { e.preventDefault(); $('file-input')?.click(); }
    else if (e.key === 'Escape' && state.selected && state.issueNav != null && document.querySelector('.issuenav')) { state.issueNav = null; state.groupWalk = null; state.gridCell = null; drawGrid(); drawSide(); }
    else if (e.key === 'Escape' && state.selected) { location.hash = state.client ? 'c=' + encodeURIComponent(state.client) : ''; }
    else if (state.issueNav != null && document.querySelector('.issuenav') && ['a', 's', 'f', 'ArrowRight', 'ArrowLeft'].includes(e.key)) {
      const btn = t => [...document.querySelectorAll('.issuenav .choices button, .issuenav .in-nav button')].find(b => b.textContent.trim().startsWith(t) && !b.disabled);
      const target = {a: btn('Make this change') || btn('Take out'), s: btn('Skip'), f: btn('It\'s fine as is'), ArrowRight: btn('Next'), ArrowLeft: btn('‹ Previous')}[e.key];
      if (target) { e.preventDefault(); target.click(); }
    }
    else if (e.key === '?') toast(document.body.classList.contains('reportmode') ? 'Shortcuts: J or K next or previous correction · Esc closes the evidence, the correction, then goes back to the list · / search reports'
      : 'Shortcuts: J or K next or previous report · / search · U upload · Esc closes what is open, then back to the list · While checking: A make the change, S skip, F fine as is, ← → move');
  });
  let vinTimer;
  $('search').addEventListener('input', e => {
    state.search = e.target.value; drawQueue();
    clearTimeout(vinTimer);
    const q = state.search.replace(/[^A-Za-z0-9]/g, '');
    if (q.length < 5 || !/\d/.test(q)) { state.vinHits = null; return; }
    vinTimer = setTimeout(async () => {
      try { const out = await api('/api/vins/search', {q: state.search}); if (state.search === out.query) { state.vinHits = out; drawQueue(); } } catch (err) { /* plain list filtering still works */ }
    }, 350);
  });
  document.querySelectorAll('.segmented button').forEach(b => b.addEventListener('click', () => { state.filter = b.dataset.filter; store.set('rd-filter', state.filter); drawQueue(); }));

  // ---------------------------------------------------------------- sign-in and who is using the app
  function showSignIn(message) {
    const me = state.me || {};
    let box = $('signin');
    if (!box) { box = h('div', {id: 'signin', class: 'signin', role: 'dialog', 'aria-modal': 'true', 'aria-labelledby': 'signin-title'}); document.body.append(box); }
    const email = h('input', {type: 'email', placeholder: 'you@ppgquality.com', 'aria-label': 'Your email', autocomplete: 'email'});
    const note = h('p', {class: 'note'}, message || '');
    const close = me.user ? h('button', {type: 'button', class: 'btn ghost small', onclick: () => box.remove()}, 'Not now') : null;
    box.replaceChildren(h('div', {class: 'signin-card'},
      h('img', {src: '/assets/ppg-client-intelligence.png', alt: 'PPG Quality Client Intelligence'}),
      h('h1', {id: 'signin-title'}, 'Sign in to the PPG Verifier'),
      h('p', {}, 'You only see the clients you have been given access to. Ask an admin if something is missing.'),
      message ? note : null,
      me.microsoft !== false ? h('button', {type: 'button', class: 'btn primary ms', onclick: () => { location.href = '/auth/microsoft'; }}, 'Sign in with Microsoft') : null,
      h('div', {class: 'or'}, h('span', {}, me.microsoft !== false ? 'or get a sign-in link by email' : 'Get a sign-in link by email')),
      h('form', {class: 'keyform', onsubmit: async e => { e.preventDefault();
        try { const out = await api('/api/auth/email', {email: email.value}); note.textContent = out.message; if (!note.isConnected) e.target.after(note); }
        catch (err) { note.textContent = err.message; if (!note.isConnected) e.target.after(note); } }},
        email, h('button', {type: 'submit', class: 'btn'}, 'Email me a link')),
      close));
    (me.microsoft !== false ? box.querySelector('.ms') : email)?.focus();
  }
  function drawMe() {
    const me = state.me?.user, box = $('me'); if (!box || !me) return;
    const admin = me.role === 'admin';
    document.querySelectorAll('.rail [data-admin], .mnav [data-admin]').forEach(a => { a.hidden = !admin; });
    box.replaceChildren(me.virtual
      ? h('div', {}, h('b', {}, 'Signed in through Vercel'), h('button', {type: 'button', class: 'linkish', onclick: () => showSignIn()}, 'Sign in with your own account'))
      : h('div', {}, h('b', {}, me.name || me.email), h('span', {}, `${me.email} · ${admin ? 'Admin' : 'Member'}`),
          h('button', {type: 'button', class: 'linkish', onclick: async () => { await api('/api/auth/signout', {}); location.href = '/app'; }}, 'Sign out')));
  }
  async function showUsers() {
    const wrap = h('div', {class: 'sheetwrap apiwrap'});
    $('report').replaceChildren(wrap);
    wrap.replaceChildren(h('p', {class: 'note'}, h('span', {class: 'spin'}), ' Loading people…'));
    let data;
    try { data = await api('/api/users', {}); } catch (e) { wrap.replaceChildren(h('p', {class: 'note'}, e.message)); return; }
    if (location.hash !== '#users') return;
    const clients = state.clients || [];
    const cname = key => key === '_none' ? 'Jobs not on a client site' : clients.find(c => c.key === key)?.client || key;
    const run = async (path, body, done) => { try { data = await api(path, body); if (done) toast(done); draw(); } catch (e) { toast(e.message, true); } };
    const clientPicker = (picked, name) => h('div', {class: 'clientpick'}, ...[...clients.map(c => c.key), '_none'].map(k =>
      h('label', {}, h('input', {type: 'checkbox', name, value: k, checked: picked.includes(k)}), cname(k))));
    const draw = () => {
      const me = data.me || {}, admins = data.users.filter(u => u.role === 'admin').length;
      const form = h('form', {class: 'userform', onsubmit: e => { e.preventDefault(); const f = e.target;
        run('/api/users/save', {email: f.email.value, name: f.uname.value, role: f.role.value, clients: [...f.querySelectorAll('input[name=nc]:checked')].map(x => x.value)}, 'Added. Now send them a sign-in link.'); }},
        h('div', {class: 'row'},
          h('label', {}, 'Email', h('input', {name: 'email', type: 'email', required: true, placeholder: 'name@ppgquality.com'})),
          h('label', {}, 'Name', h('input', {name: 'uname', type: 'text', placeholder: 'Optional'})),
          h('label', {}, 'Role', h('select', {name: 'role'}, h('option', {value: 'member'}, 'Member: only their clients'), h('option', {value: 'admin'}, 'Admin: everything, and manages people')))),
        h('p', {class: 'hint'}, 'Clients this member can see:'), clientPicker([], 'nc'),
        h('div', {class: 'btns'}, h('button', {type: 'submit', class: 'btn primary'}, 'Add person')));
      const row = u => {
        const picker = clientPicker(u.clients, 'c-' + u.id);
        const role = h('select', {'aria-label': 'Role'}, h('option', {value: 'member', selected: u.role === 'member'}, 'Member'), h('option', {value: 'admin', selected: u.role === 'admin'}, 'Admin'));
        const save = () => run('/api/users/save', {id: u.id, email: u.email, name: u.name, role: role.value, clients: [...picker.querySelectorAll('input:checked')].map(x => x.value)}, 'Saved.');
        picker.addEventListener('change', save); role.addEventListener('change', save);
        return h('li', {class: 'person'},
          h('div', {class: 'who'}, h('b', {}, u.name || u.email), h('span', {}, u.email + (u.id === me.id ? ' · you' : '')),
            h('small', {}, u.last_login ? `Last signed in ${ago(u.last_login)}` : 'Has not signed in yet')),
          role,
          u.role === 'admin' ? h('p', {class: 'hint'}, 'Admins see every client.') : picker,
          h('div', {class: 'btns'},
            h('button', {type: 'button', class: 'btn small', onclick: async () => { try { const out = await api('/api/users/link', {id: u.id});
              await navigator.clipboard?.writeText(out.link).catch(() => {}); toast(`Sign-in link for ${out.email} copied. It works once, for ${out.hours} hours.`); prompt.value = out.link; prompt.hidden = false; prompt.select(); } catch (e) { toast(e.message, true); } }}, 'Copy sign-in link'),
            data.email ? h('button', {type: 'button', class: 'btn small', onclick: () => run('/api/users/email', {id: u.id}, `Sign-in link emailed to ${u.email}.`)}, 'Email sign-in link') : null,
            u.id !== me.id ? h('button', {type: 'button', class: 'btn ghost small', onclick: () => run('/api/users/remove', {id: u.id}, `${u.email} removed.`)}, 'Remove') : null));
      };
      const prompt = h('input', {type: 'text', readonly: true, hidden: true, class: 'linkbox', 'aria-label': 'Sign-in link'});
      const canRequire = !me.virtual && me.role === 'admin';
      wrap.replaceChildren(
        h('div', {class: 'head'}, h('div', {}, h('h1', {}, 'Users & access'), h('div', {class: 'meta'}, 'Who can use the verifier and which clients each person sees. Members only ever get their own clients’ reports, searches, downloads and numbers.'))),
        h('section', {class: 'block accessstate ' + (data.required ? 'on' : 'off')}, h('header', {}, h('h2', {}, data.required ? 'Sign-in is required' : 'Sign-in is not required yet'),
          h('p', {}, data.required ? 'Everyone signs in and sees only their clients. You can now turn off Vercel’s protection so coworkers can reach the app.'
            : 'Right now Vercel’s sign-in is the only gate, and anyone who gets through sees everything. Add people, sign in yourself, then turn this on.'),
          h('div', {class: 'right'}, data.required
            ? h('button', {type: 'button', class: 'btn', onclick: () => run('/api/access/required', {on: false}, 'Sign-in is no longer required.')}, 'Turn off')
            : h('button', {type: 'button', class: 'btn primary', disabled: !canRequire, title: canRequire ? '' : 'Sign in with your own account first', onclick: () => run('/api/access/required', {on: true}, 'Sign-in is now required.')}, 'Require sign-in'))),
          !canRequire && !data.required ? h('p', {class: 'hint'}, me.virtual ? 'First add yourself as an admin below, copy your own sign-in link, open it, then come back and turn this on.' : '') : null,
          data.required ? h('ol', {class: 'steps'}, h('li', {}, 'In Vercel, open ppg-ci-verifier → Settings → Deployment Protection.'), h('li', {}, 'Set Vercel Authentication to Disabled and save.'), h('li', {}, 'Share the app link with the people you added.')) : null),
        h('section', {class: 'block'}, h('header', {}, h('h2', {}, `People (${data.users.length})`), h('p', {}, `${admins} admin${admins === 1 ? '' : 's'}. Changes to roles and clients save as you click.`)),
          prompt,
          data.users.length ? h('ul', {class: 'people'}, data.users.map(row)) : h('p', {class: 'note'}, 'Nobody yet. Add yourself first, as an admin.')),
        h('section', {class: 'block'}, h('header', {}, h('h2', {}, 'Add a person')), form),
        h('section', {class: 'block'}, h('header', {}, h('h2', {}, 'How people sign in')),
          h('ul', {class: 'how'},
            h('li', {}, h('b', {}, 'Sign in with Microsoft: '), data.microsoft ? 'ready. People use their PPG Microsoft account.' : 'needs the Microsoft app settings.',
              ' In Microsoft Entra, open the PPG app registration → Authentication → add a Web redirect address ', h('code', {}, data.callback), ', then API permissions → add Microsoft Graph delegated openid, email and profile → Grant admin consent.'),
            h('li', {}, h('b', {}, 'Email sign-in link: '), data.email ? 'ready. Links come from the data@ppgquality.com mailbox.' : 'waits on the Mail.Send permission (the same one Approve and send needs). Until then, use Copy sign-in link and send it yourself.'))));
    };
    draw();
  }

  // ---------------------------------------------------------------- routing
  function select(hash) { if (location.hash !== '#r=' + hash) location.hash = 'r=' + hash; else route(); }
  window.addEventListener('hashchange', route);
  async function route() {
    document.body.classList.remove('reportmode');
    document.querySelectorAll('dialog[open]:not(#signin-dialog)').forEach(d => d.close());   // a window left open must not cover the next page
    document.body.dataset.page = (location.hash.match(/^#(today|overview|kpis|periods|checks|api|users)$/) || [])[1] || 'reports';
    const cm = location.hash.match(/^#c=(.+)$/);
    if (cm) { state.client = decodeURIComponent(cm[1]); store.set('rd-client', state.client); drawClientTabs(); }
    else if (location.hash === '' || location.hash === '#') { if (state.client) { state.client = ''; store.set('rd-client', ''); drawClientTabs(); } }
    const navOn = ['#api', '#checks', '#periods', '#today', '#overview', '#kpis', '#users'].includes(location.hash) ? location.hash : state.client ? '#c=' + encodeURIComponent(state.client) : '#';
    document.querySelectorAll('.rail .nav[href], .mnav a[href]').forEach(a => { const on = a.getAttribute('href') === navOn; a.classList.toggle('active', on); if (a.closest('.mnav')) a.setAttribute('aria-current', on ? 'page' : 'false'); });
    if (location.hash === '#overview') { closeChat(); document.body.classList.add('viewing'); state.selected = null; state.report = null; drawQueue(); await showOverview(); return; }
    if (location.hash === '#kpis') { closeChat(); document.body.classList.add('viewing'); state.selected = null; state.report = null; drawQueue(); await showKpis(); return; }
    if (location.hash === '#today') { closeChat(); document.body.classList.add('viewing'); state.selected = null; state.report = null; drawQueue(); await showToday(); return; }
    if (location.hash === '#periods') { closeChat(); document.body.classList.add('viewing'); state.selected = null; state.report = null; drawQueue(); await showPeriods(); return; }
    if (location.hash === '#checks') { closeChat(); document.body.classList.add('viewing'); state.selected = null; state.report = null; drawQueue(); await showChecks(); return; }
    if (location.hash === '#users') { closeChat(); document.body.classList.add('viewing'); state.selected = null; state.report = null; drawQueue(); await showUsers(); return; }
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
      if (state.report?.file_hash !== hash) { state.fdView = 'review'; state.fdCur = null; state.fdTier = null; state.gridSearch = ''; state.sheetScroll = {}; state.gridOnlyMarked = false; state.gridRow = null; document.body.classList.remove('fd-panel-open'); }
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

  // Reports uploaded before the automatic cleanup: make the sure fixes the first time one is opened, then say what changed
  async function autoCleanOlder(r) {
    const tried = store.get('rd-autoclean', []);
    if (r.auto_formatting || r.approval || (r.sent || []).some(x => x.sent) || tried.includes(r.file_hash)) return;
    store.set('rd-autoclean', [...tried, r.file_hash].slice(-500));
    try {
      const out = await api('/api/spreadsheets/auto-clean', {file_hash: r.file_hash});
      if (!out.changed || state.selected !== r.file_hash) return;
      const a = out.item.auto_formatting || {};
      toast(`Fixed ${a.count} formatting issue${a.count === 1 ? '' : 's'} automatically. What changed is listed at the top.`);
      await loadList(); select(out.item.file_hash);
    } catch (e) { /* the offer box still lets the person do it */ }
  }
  // Totals only, ready to paste into the client email. Never scan times, inspector names or internal check results.
  async function copyClientSummary() {
    const r = state.report, rv = r.review || {}, n = v => v == null ? '—' : Number(v).toLocaleString('en-US');
    const client = (r.assignment && SETTLED.includes(r.assignment.status)) ? r.assignment.client : null;
    const rate = rv.inspected && rv.rejected != null && !rv.rejected_partial ? ` (${(rv.rejected / rv.inspected * 100).toFixed(1)}%)` : '';
    const text = [`${client ? client + ' · ' : ''}${rv.job || r.filename}`, `End-of-shift summary for ${dayLabel(rv.day)}`, '',
      `Parts inspected: ${n(rv.inspected)}`, rv.certified != null ? `Accepted: ${n(rv.certified)}` : null, `Rejected: ${n(rv.rejected)}${rate}${rv.rejected_partial ? ' (recorded rejects; not every record had a reject count)' : ''}`, '',
      'The full end-of-shift report is attached.'].filter(x => x !== null).join('\n');
    try { await navigator.clipboard.writeText(text); toast('Client summary copied: totals only, with no scan times, inspector names or internal checks.'); }
    catch (e) { toast('Your browser blocked copying. Try again, or allow clipboard access for this site.', true); }
  }
  // a report read from a PDF or Word file: say where the data came from, what to expect from fixes, and show its text
  // formatting-only fixes the app made by itself on upload
  // Fix with AI: Claude resolves the picked issues (any number, or all) and the changes go into a new version
  async function aiFix(keys, btn) {
    if (!keys.length) return toast('Pick at least one issue.', true);
    const hash = state.report.file_hash, label = btn?.textContent;
    if (btn) { btn.disabled = true; btn.textContent = `Claude is fixing ${keys.length === 1 ? 'it' : keys.length}…`; }
    toast(`Claude is working on ${keys.length === 1 ? 'this issue' : `${keys.length} issues`}. This takes a moment.`);
    try {
      const out = await api('/api/spreadsheets/ai-fix', {file_hash: hash, keys});
      const s = out.ai_fixed || {}, did = (s.changed || []).length + (s.removed || []).length + (s.moved || []).length, ok = (s.fine || []).length, left = (s.left || []).length;
      toast(`Done. ${[did ? `${did} change${did === 1 ? '' : 's'} made` : '', ok ? `${ok} confirmed fine` : '', left ? `${left} left for you` : ''].filter(Boolean).join(', ')}. See what Claude did at the top.`, !!left && !did && !ok);
      await loadList();
      if (out.item.file_hash !== hash) select(out.item.file_hash); else if (state.selected === hash) openReport(hash);
    } catch (e) { toast(e.message, true); if (btn) { btn.disabled = false; btn.textContent = label; } }
  }
  function aiFixedBanner(r) {   // a short summary of what Claude did; every single change is one click away
    const s = r.ai_fixed; if (!s) return null;
    const plural = (n, w) => `${fmt(n)} ${w}${n === 1 ? '' : 's'}`;
    const norm = t => String(t || '').replace(/"[^"]*"|“[^”]*”/g, '"…"').replace(/\b[A-Za-z0-9-]*\d[A-Za-z0-9-]*\b/g, '#').replace(/\s+/g, ' ').trim();
    const groupBy = (rows, key) => { const g = new Map(); for (const x of rows) { const k = key(x); g.set(k, [...(g.get(k) || []), x]); } return [...g.values()].sort((a, b) => b.length - a.length); };
    const changed = s.changed || [], removed = s.removed || [], moved = s.moved || [], fine = s.fine || [], left = s.left || [];
    const checks = [...changed, ...removed, ...moved].filter(x => x.check).length;
    const lines = [];
    for (const g of groupBy(changed, x => norm(x.reason)).slice(0, 4)) { const x = g[0];
      lines.push(h('li', {}, h('b', {}, g.length === 1 ? 'Changed 1 cell' : `Changed ${fmt(g.length)} cells`), `: ${x.reason}`, h('span', {class: 'quiet'}, ` e.g. ${x.sheet} ${x.cell}: ${showWs(x.before)} → ${showWs(x.after)}`))); }
    const moreChanged = groupBy(changed, x => norm(x.reason)).slice(4).reduce((n, g) => n + g.length, 0);
    if (moreChanged) lines.push(h('li', {}, `${plural(moreChanged, 'other change')}.`));
    if (removed.length) lines.push(h('li', {}, h('b', {}, `Took out ${plural(removed.length, 'row')}`), `: ${removed[0].reason}`));
    if (moved.length) lines.push(h('li', {}, h('b', {}, `Moved ${plural(moved.length, 'row')} to the right shift`)));
    for (const g of groupBy(fine, x => norm(x.text)).slice(0, 3)) lines.push(h('li', {}, h('b', {}, `Checked ${g.length === 1 ? '1 flag' : `${fmt(g.length)} flags`}, fine as is`), `: ${g[0].text}`));
    const moreFine = groupBy(fine, x => norm(x.text)).slice(3).reduce((n, g) => n + g.length, 0);
    if (moreFine) lines.push(h('li', {}, `${plural(moreFine, 'other flag')} checked and fine.`));
    if (left.length) { const lg = groupBy(left, x => norm(x.text));
      lines.push(h('li', {class: 'check'}, h('b', {}, `${plural(left.length, 'item')} left for you`), ': ' + lg.slice(0, 2).map(g => (g.length > 1 ? `${fmt(g.length)}× ` : '') + g[0].text).join(' · ') + (lg.length > 2 ? ` and ${lg.length - 2} more kinds` : '') + '. They are in the summary below.')); }
    const did = changed.length + removed.length + moved.length;
    const item = (title, rows, f) => rows.length ? h('div', {class: 'aifx-group'}, h('b', {}, title), h('ul', {}, rows.map(x => h('li', {class: x.check ? 'check' : ''}, f(x), x.check ? h('em', {class: 'checkme'}, ' please double-check') : null)))) : null;
    return h('div', {class: 'aifixed'},
      h('div', {class: 'autofmt-top'}, h('span', {class: 'autofmt-mark ai', 'aria-hidden': 'true'}, 'AI'),
        h('div', {}, h('b', {}, [did ? `Claude fixed ${plural(did, 'thing')}` : '', fine.length ? `checked ${fmt(fine.length)} and found ${fine.length === 1 ? 'it' : 'them'} fine` : '', left.length ? `left ${fmt(left.length)} for you` : ''].filter(Boolean).join(', ').replace(/^./, c => c.toUpperCase()) || 'Claude looked at this report'),
          h('span', {}, `${s.auto ? 'Done automatically' : `${plural(s.asked, 'issue')} you picked`}, ${ago(s.made_at)}.` + (checks ? ` ${plural(checks, 'change')} ${checks === 1 ? 'is' : 'are'} Claude's best judgment: please double-check ${checks === 1 ? 'it' : 'them'}.` : ''))),
        s.from_hash && s.from_hash !== r.file_hash ? h('button', {type: 'button', class: 'linkish small', onclick: () => select(s.from_hash)}, 'Open the version before') : null),
      h('ul', {class: 'aifx-summary'}, lines),
      did + fine.length + left.length > lines.length ? h('details', {class: 'autofmt-all'}, h('summary', {}, `See every change (${fmt(did + fine.length + left.length)})`),
        item('Changed', changed, x => h('span', {}, h('button', {type: 'button', class: 'linkish', onclick: () => showCell(x.sheet, x.cell)}, `${x.sheet} ${x.cell}`), `: ${showWs(x.before)} → ${showWs(x.after)}. ${x.reason}`)),
        item('Rows taken out', removed, x => `${x.sheet} row ${x.row}: ${x.reason}`),
        item('Rows moved', moved, x => `${x.sheet} row ${x.row} → ${x.to_sheet}: ${x.reason}`),
        item('Checked and fine as is', fine, x => `${x.text} ${x.reason}`),
        item('Left for you', left, x => `${x.text} (${x.why})`)) : null);
  }
  // ---------------- SharePoint job check, change summary PDFs and the record of them
  // Client → Job → Business date, how it was decided, and a way to correct it (kept with who, when and why).
  const HOW = {manual: 'Chosen by a person', verified: 'Verified SharePoint list', matched: 'Matched to its SharePoint list', source: 'Pratt & Whitney source (no job number)',
               ambiguous: 'Needs a person: more than one client fits', conflict: 'Needs a person: the evidence disagrees', unmatched: 'Needs a person: no client list matches'};
  function assignmentLine(r) {
    const a = r.assignment; if (!a) return null;
    const ok = SETTLED.includes(a.status);
    const last = (a.history || []).slice(-1)[0];
    return h('div', {class: 'spjob assign ' + (ok ? 'ok' : 'warn'), id: 'assignline'},
      h('span', {class: 'spjob-tag'}, ok ? 'Client ✓' : 'Client'),
      h('b', {}, a.label),
      h('span', {}, HOW[a.status] || a.status, a.evidence?.length ? ': ' + a.evidence.join(' ') : ''),
      last ? h('span', {class: 'spjob-recs'}, `Changed by ${last.by} ${ago(last.at)}${last.reason ? ` (“${last.reason}”)` : ''}`) : null,
      h('button', {type: 'button', class: 'btn small', onclick: e => assignForm(r, e.target)}, ok ? 'Change' : 'Choose client and job'));
  }
  // Checked with an older version of the checker: show what the new checks would change, apply only on request.
  function recheckLine(r) {
    if (!r.recheck?.available) return null;
    const box = h('div', {class: 'spjob warn', id: 'recheckline'}, h('span', {class: 'spjob-tag'}, 'Older checks'),
      h('span', {}, 'These results were made by an earlier version of the checker. Nothing changes until you apply the new checks.'),
      h('button', {type: 'button', class: 'btn small', onclick: async e => {
        e.target.disabled = true;
        try {
          const {proposal: p} = await api('/api/spreadsheets/recheck', {file_hash: r.file_hash});
          const show = v => v == null ? 'not recorded' : Array.isArray(v) ? v.join(', ') : String(v);
          const NAMES = {record_count: 'Records', inspected: 'Inspected', certified: 'OK', rejected: 'Rejected', finding_count: 'Issues', job: 'Job', day: 'Business date',
                         client: 'Client', assignment: 'Client → Job → Date', assignment_status: 'How the client was decided', authoritative: 'Counted sheet', compared: 'Compared, not counted'};
          box.append(h('div', {class: 'recrows'}, p.changes.length
            ? h('table', {}, h('thead', {}, h('tr', {}, h('th', {}, 'What'), h('th', {}, 'Now'), h('th', {}, 'With the new checks'))),
                h('tbody', {}, ...p.changes.map(c => h('tr', {}, h('td', {}, NAMES[c.field] || c.field), h('td', {}, show(c.before)), h('td', {}, show(c.after))))))
            : h('p', {}, 'The new checks give the same numbers and client.'),
            h('button', {type: 'button', class: 'btn primary', onclick: async ev => {
              ev.target.disabled = true;
              try { await api('/api/spreadsheets/recheck', {file_hash: r.file_hash, apply: true, by: myName() || null}); await openReport(r.file_hash, true); await loadList(); toast('New checks applied. The original file and earlier results are kept.'); }
              catch (err) { toast(err.message, true); ev.target.disabled = false; } }}, 'Apply the new checks')));
          e.target.remove();
        } catch (err) { toast(err.message, true); e.target.disabled = false; } }}, 'Preview the new checks'));
    return box;
  }
  // Why the report stays on hold beyond its issues (client not settled, record set unclear, SharePoint coverage or differences).
  function holdLine(r) {
    const hs = r.holds || []; if (!hs.length) return null;
    return h('div', {class: 'spjob warn', id: 'holdline'}, h('span', {class: 'spjob-tag'}, 'On hold'),
      h('span', {}, hs.map(x => x.text).join(' ')));
  }
  const REC = {verified: 'Matches SharePoint item by item', discrepancies: 'Differs from SharePoint', incomplete: 'SharePoint coverage incomplete: not verified', unverified: 'Not verified'};
  const REC_ROW = {value_differs: 'Value differs', missing_from_report: 'On SharePoint, not in the report', not_on_sharepoint: 'Not on SharePoint',
    not_on_sharepoint_for_day: 'On SharePoint for another day', report_row_without_item_id: 'No item ID', item_listed_twice: 'Listed twice',
    changed_since_export: 'Edited after the export', export_value_differs: 'Export differs from the list', export_item_not_on_sharepoint: 'Export item gone from the list'};
  // One card at the top: what the report is, its totals, the source verdict and what is left to do.
  function reviewCard(r) {
    const rv = r.review || {}, a = r.assignment || {}, sp = rv.sharepoint || {}, c = rv.counts || {};
    const block = blockers(r), review = c.medium || 0, info = (c.low || 0) + (c.info || 0);
    const shifts = [...new Set((r.records || []).map(x => x.shift).filter(Boolean))];
    const photos = (r.photos?.images || []).filter(p => p.item || /photo|picture|image|gallery/i.test(p.sheet || '')).length;   // not logos
    const n = v => v == null ? null : fmt(Number(v));
    const totals = [`${fmt(rv.scans || 0)} record${rv.scans === 1 ? '' : 's'}`, rv.inspected != null ? `${n(rv.inspected)} inspected` : 'inspected not recorded',
      rv.certified != null ? `${n(rv.certified)} OK` : null,
      rv.rejected == null ? 'rejects not recorded' : rv.rejected_partial ? `${n(rv.rejected)} rejects recorded (some records blank)` : `${n(rv.rejected)} rejected`,
      shifts.length ? `${shifts.length} shift${shifts.length === 1 ? '' : 's'} (${shifts.join(', ')})` : null, `${photos} photo${photos === 1 ? '' : 's'}`].filter(Boolean);
    const state = (r.sent || []).some(x => x.sent) ? ['Sent', 'ok'] : r.approval ? ['Approved, not sent', 'ok']
      : block ? [`${block} blocker${block === 1 ? '' : 's'}`, 'bad'] : a.needs_person ? ['Client not settled', 'bad'] : review ? [`${review} to review`, 'warn']
      : sp.status !== 'match' ? ['Not verified against SharePoint yet', 'warn'] : ['Ready to approve', 'ok'];
    return h('section', {class: 'reviewcard', 'aria-label': 'Report summary'},
      h('div', {class: 'rc-top'},
        h('span', {class: 'rc-state ' + state[1]}, state[1] === 'ok' ? '✓ ' : state[1] === 'bad' ? '✕ ' : '! ', state[0]),
        h('b', {}, SETTLED.includes(a.status) ? a.label : `Client not settled → ${rv.job || 'job unknown'} → ${rv.day || 'date unknown'}`),
        h('span', {class: 'rc-meta'}, `version ${r.version}`),
        a.needs_person ? h('button', {type: 'button', class: 'btn small', onclick: e => assignForm(r, e.target)}, 'Choose client and job') : null),
      h('p', {class: 'rc-totals'}, totals.join(' · ')),
      (block || review || info) ? h('p', {class: 'rc-groups'}, [block ? `${block} blocker${block === 1 ? '' : 's'}` : null, review ? `${review} to review` : null, info ? `${info} for information` : null].filter(Boolean).join(' · ') + ' (listed under Fixes)') : null,
      reconcileLine(r));
  }
  function reconcileLine(r) {
    const x = r.source_reconciliation, a = r.assignment || {};
    const can = a.site && a.list_id && SETTLED.includes(a.status);
    const run = async e => {
      e.target.disabled = true; e.target.textContent = 'Reading the SharePoint list…';
      try { await api('/api/spreadsheets/source-reconcile', {file_hash: r.file_hash}); const fresh = await api('/api/spreadsheet?id=' + r.file_hash);
        Object.assign(state.report, {source_reconciliation: fresh.source_reconciliation, holds: fresh.holds, review: fresh.review}); drawReport(); }
      catch (err) { toast(err.message, true); e.target.disabled = false; e.target.textContent = 'Reconcile with SharePoint'; } };
    const btn = h('button', {type: 'button', class: 'btn small', disabled: can ? null : '', title: can ? 'Read-only: nothing on SharePoint is changed' : 'Choose the client and job first', onclick: run}, x ? 'Reconcile again' : 'Reconcile with SharePoint');
    if (!x) return h('div', {class: 'spjob none'}, h('span', {class: 'spjob-tag'}, 'Source'), h('span', {}, 'Not checked against the live SharePoint list yet.'), btn);
    const c = x.coverage || {};
    const rows = x.rows || [];
    const stale = (r.review?.sharepoint || {}).status === 'stale';
    const verified = x.status === 'verified' && !stale;
    const label = stale ? 'Re-check needed: this check was made for an earlier version' : REC[x.status] || x.status;
    const confirm = Object.entries(x.column_mapping || {}).filter(([, m]) => m.status === 'needs_confirmation' || m.status === 'conflict');
    return h('div', {class: 'spjob ' + (verified ? 'ok' : 'warn')},
      h('span', {class: 'spjob-tag'}, verified ? 'Source ✓' : 'Source'),
      h('b', {}, label),
      h('span', {}, `${x.list?.name || x.site} · ${x.sharepoint_items_for_day} SharePoint item(s) for ${x.day}, ${x.report_rows} report row(s) · checked: ${(x.fields_checked || []).join(', ') || 'nothing'}`
        + ((x.fields_not_checked || []).length ? ` · not checked: ${x.fields_not_checked.join(', ')}` : '') + ` · ${c.pages || 0} page(s) read · captured ${ago(x.captured_at)}`
        + ((x.not_verified_because || []).length ? ' · ' + x.not_verified_because.join(' ') : '') + (c.notes?.length ? ' · ' + c.notes.join(' ') : '')),
      btn,
      confirm.length && x.list?.id ? h('button', {type: 'button', class: 'btn small', onclick: async e => {
        e.target.disabled = true;
        try { await api('/api/sharepoint/confirm-columns', {list_id: x.list.id, mapping: Object.fromEntries(confirm.map(([f, m]) => [f, m.column]))}); await run({target: e.target}); }
        catch (err) { toast(err.message, true); e.target.disabled = false; } }}, `Confirm ${confirm.map(([f, m]) => `${f} = ${m.column}`).join(', ')}`) : null,
      rows.length ? h('details', {class: 'recrows', open: ''}, h('summary', {}, `${rows.length} difference${rows.length === 1 ? '' : 's'} from SharePoint` + (x.proposed_corrections?.length ? ' · the fixes are in the issues below' : '')),
        h('table', {}, h('thead', {}, h('tr', {}, ...['What', 'Item', 'Where', 'Field', 'Report', 'SharePoint'].map(t => h('th', {}, t)))),
          h('tbody', {}, ...rows.slice(0, 300).map(d => h('tr', {},
            h('td', {}, REC_ROW[d.status] || d.status, d.note ? h('small', {}, ' ' + d.note) : null),
            h('td', {}, d.url ? h('a', {href: d.url, target: '_blank', rel: 'noopener'}, d.item) : (d.item || '—')),
            h('td', {}, d.sheet ? `${d.sheet} row ${d.row}` : '—'), h('td', {}, d.field || ''), h('td', {}, d.report ?? ''), h('td', {}, d.sharepoint ?? '')))))) : null);
  }
  function assignForm(r, btn) {
    if ($('assignform')) { $('assignform').remove(); return; }
    const a = r.assignment || {};
    const opts = [];
    for (const c of a.candidates || []) opts.push({client_key: c.client_key, client: c.client, job: c.job, site: c.site, list_id: c.list_id, label: `${c.client} → ${c.job} (${c.site}${c.verified ? ', verified' : ''})`});
    for (const c of state.clients || []) for (const j of c.jobs) if (!opts.some(o => o.client_key === c.key && o.site === j.site && o.job === j.job))
      opts.push({client_key: c.key, client: c.client, job: j.job, site: j.site, list_id: j.list_id, label: `${c.client} → ${j.job} (${j.site})`});
    const pick = h('select', {'aria-label': 'Client and job'}, h('option', {value: ''}, 'Choose the client and job…'), ...opts.map((o, i) => h('option', {value: String(i), selected: a.client_key === o.client_key && a.site === o.site && a.job === o.job ? '' : null}, o.label)));
    const why = h('input', {type: 'text', placeholder: 'Why (for example: the tablets at Alexander City wrote these records)', maxlength: '300'});
    const remember = h('input', {type: 'checkbox'});
    const form = h('div', {class: 'assignform', id: 'assignform'},
      h('label', {}, 'Client and job ', pick),
      h('label', {}, 'Reason ', why),
      h('label', {class: 'check'}, remember, ' Remember this list for this job at this site (adds it to the verified registry)'),
      h('div', {}, h('button', {type: 'button', class: 'btn primary', onclick: async () => {
        const o = opts[Number(pick.value)]; if (!o) { toast('Choose the client and job.', true); return; }
        try {
          const out = await api('/api/spreadsheets/assign', {file_hash: r.file_hash, choice: o, reason: why.value, remember: remember.checked});
          form.remove();
          const fresh = await api('/api/spreadsheet?id=' + r.file_hash);   // read back from the server, not from what was typed
          Object.assign(state.report, {assignment: fresh.assignment, holds: fresh.holds, review: fresh.review});
          drawReport();
          await loadList();
          toast(`Saved: ${fresh.assignment?.label || out.assignment?.label}. The report is still on hold until it is approved.`);
        } catch (e) { toast(e.message, true); } }}, 'Save'),
        h('button', {type: 'button', class: 'btn', onclick: () => form.remove()}, 'Cancel')));
    (btn?.closest('.reviewcard') || btn).after(form);
  }
  function spJobLine(r) {
    const j = r.sp_job; if (!j || j.status === 'no_list' && state.me?.user?.role !== 'admin') return null;
    const ok = j.status === 'found' && !j.inactive;
    const recs = r.change_summaries || [];
    return h('div', {class: 'spjob ' + (ok ? 'ok' : j.status === 'no_list' ? 'none' : 'warn')},
      h('span', {class: 'spjob-tag'}, ok ? 'SharePoint ✓' : 'SharePoint'),
      h('span', {}, j.status === 'no_list' ? 'Load the SharePoint job list on Checks & shifts so every report is matched to its SharePoint job.' : j.message),
      recs.length ? h('span', {class: 'spjob-recs'}, 'Change summary: ', ...recs.slice(0, 3).map((x, i) => h('a', {href: '/api/change-summary?id=' + x.id + '&view=1', target: '_blank', rel: 'noopener'}, i ? `, ${ago(x.made_at)}` : `PDF ${ago(x.made_at)}`))) : null);
  }
  async function makeChangeSummary() {
    const hash = state.report.file_hash;
    toast('Making the change summary…');
    try {
      const {record} = await api('/api/change-summary', {file_hash: hash, by: myName() || null});
      state.report.change_summaries = [{id: record.id, made_at: record.made_at, by: record.by, reason: 'requested'}, ...(state.report.change_summaries || [])];
      refreshHead();
      const a = h('a', {href: '/api/change-summary?id=' + record.id, download: record.filename}); document.body.append(a); a.click(); a.remove();
      toast(record.differed ? `Change summary saved on record: ${record.changes} cell change${record.changes === 1 ? '' : 's'}` + (record.rows ? `, ${record.rows} row${record.rows === 1 ? '' : 's'} taken out or moved` : '') + `, ${record.differences} difference${record.differences === 1 ? '' : 's'} from SharePoint, ${record.open} still open.` : 'Change summary saved on record: this report matched SharePoint and needed no changes.');
    } catch (e) { toast(e.message, true); }
  }
  function changeRecords() {
    const box = h('section', {class: 'block'}, h('header', {}, h('h2', {}, 'Change summaries on record'),
      h('p', {}, 'A PDF is saved when a report that differed from SharePoint or was changed is approved, or when someone makes one from a report (More, Change summary). Internal only.')),
      h('p', {class: 'note'}, h('span', {class: 'spin'}), ' Loading…'));
    api('/api/change-summaries', {}).then(({records}) => {
      box.lastChild.replaceWith(records.length ? h('div', {class: 'tablewrap'}, h('table', {class: 'keys'},
        h('thead', {}, h('tr', {}, ['Job', 'Report day', 'Made', 'By', 'Why', 'SharePoint job', 'Differences', 'Cells changed', 'Rows', 'Open', ''].map(x => h('th', {}, x)))),
        h('tbody', {}, records.slice(0, 200).map(x => h('tr', {},
          h('td', {}, h('b', {}, x.job || '—')), h('td', {}, x.day ? dayLabel(x.day) : '—'), h('td', {}, ago(x.made_at)), h('td', {}, x.by || '—'),
          h('td', {}, x.reason === 'approved' ? 'Approved' : 'Asked for'), h('td', {}, {found: 'Found', differs: 'Written differently', not_found: 'Not found', ambiguous: 'On more than one site', no_list: 'No list loaded', no_job: 'No job number'}[x.job_check] || '—'),
          h('td', {}, fmt(x.differences)), h('td', {}, fmt(x.changes)), h('td', {}, fmt(x.rows || 0)), h('td', {}, x.open ? h('span', {class: 'badn'}, fmt(x.open)) : '0'),
          h('td', {}, h('a', {class: 'btn small', href: '/api/change-summary?id=' + x.id + '&view=1', target: '_blank', rel: 'noopener'}, 'Open PDF'), ' ',
            h('button', {type: 'button', class: 'btn ghost small', onclick: () => select(x.file_hash)}, 'Report'))))))) : h('p', {class: 'note'}, 'None yet. They appear here as reports are approved.'));
    }).catch(e => box.lastChild.replaceWith(h('p', {class: 'note'}, e.message)));
    return box;
  }
  function spJobsSection() {
    const box = h('section', {class: 'block'});
    const file = h('input', {type: 'file', accept: '.xlsx', hidden: true, onchange: async e => {
      const f = e.target.files[0]; e.target.value = ''; if (!f) return;
      try { const b64 = await new Promise((ok, no) => { const rd = new FileReader(); rd.onload = () => ok(String(rd.result).split(',')[1]); rd.onerror = no; rd.readAsDataURL(f); });
        paint(await api('/api/settings/sharepoint-jobs', {content_base64: b64, filename: f.name})); toast('SharePoint job list loaded. Every report is now matched to it.');
      } catch (err) { toast(err.message, true); } }});
    const paint = info => box.replaceChildren(h('header', {}, h('h2', {}, 'SharePoint job list'),
        h('p', {}, 'Every report is checked against this list: its job must be in SharePoint, written the same way, on the right client site and active. Upload the "PPG SharePoint — Clients & Jobs" Excel file whenever it changes.')),
      info.jobs ? h('p', {class: 'ssource'}, `Loaded: ${info.real} jobs (${info.active} active) as of ${info.as_of || 'unknown'}` + (info.filename ? `, from ${info.filename}` : '') + (info.uploaded_at ? `, uploaded ${ago(info.uploaded_at)}.` : '.'))
        : h('p', {class: 'ssource warn'}, 'No list loaded yet.'),
      state.me?.user?.role === 'admin' ? h('div', {class: 'btns'}, file, h('button', {type: 'button', class: 'btn', onclick: () => file.click()}, info.jobs ? 'Upload a newer list' : 'Upload the job list')) : null);
    paint({}); api('/api/settings/sharepoint-jobs', {}).then(paint).catch(() => {});
    return box;
  }
  function fixedLog(r) {   // everything fixed automatically, folded into one line you can open
    const a = r.auto_formatting?.count || 0, s = r.ai_fixed || {}, ai = (s.changed || []).length + (s.removed || []).length + (s.moved || []).length, fine = (s.fine || []).length;
    if (!a && !ai && !fine) return null;
    const parts = [a ? `${a} cleaned up` : '', ai ? `${ai} fixed by Claude` : '', fine ? `${fine} checked and fine` : ''].filter(Boolean);
    const checks = [...(s.changed || []), ...(s.removed || []), ...(s.moved || [])].filter(x => x.check).length;
    return h('details', {class: 'fixedlog'}, h('summary', {}, h('span', {class: 'autofmt-mark', 'aria-hidden': 'true'}, '✓'),
        h('b', {}, 'Fixed automatically: '), parts.join(', '), checks ? h('span', {class: 'checkme'}, ` · ${checks} to double-check`) : null, h('span', {class: 'quiet'}, ' · see what changed')),
      autoFmtBanner(r), aiFixedBanner(r));
  }
  function autoFmtBanner(r) {
    const a = r.auto_formatting; if (!a || !a.count) return null;
    const kinds = Object.entries(a.kinds || {}).map(([k, n], i) => `${i ? k : k[0].toUpperCase() + k.slice(1)}: ${n}`).join(', ');
    const onlyFmt = a.formatting === a.count || (a.formatting == null && !(a.removed_rows || []).length && Object.keys(a.kinds || {}).every(k => /capital|spaces|hidden|number stored/.test(k)));
    const rows = a.removed_rows || [];
    return h('div', {class: 'autofmt'},
      h('div', {class: 'autofmt-top'}, h('span', {class: 'autofmt-mark', 'aria-hidden': 'true'}, '✓'),
        h('div', {}, h('b', {}, onlyFmt ? `Fixed automatically: ${a.count} formatting issue${a.count === 1 ? '' : 's'}` : `Cleaned automatically: ${a.count} change${a.count === 1 ? '' : 's'} the app was sure about`),
          h('span', {}, `${kinds}. ` + (onlyFmt ? 'Only how the values were typed changed; the values themselves are the same.' : 'Nothing the app was unsure about was changed; those are listed for a person below.')))),
      (a.examples || []).length ? h('ul', {class: 'fmtexamples'}, a.examples.map(f => h('li', {},
        h('code', {}, showWs(f.before)), h('span', {class: 'arrow', 'aria-label': 'became'}, ' → '), h('code', {}, showWs(f.after)),
        h('span', {class: 'fmtkind'}, f.kind),
        h('button', {type: 'button', class: 'linkish', title: 'Show this cell in the spreadsheet', onclick: () => showCell(f.sheet, f.cell)}, `${f.sheet} ${f.cell}`)))) : null,
      rows.length ? h('p', {class: 'autofmt-rows'}, `Took out ${rows.length} exact duplicate row${rows.length === 1 ? '' : 's'}: `, rows.slice(0, 6).map(x => `${x.sheet} row ${x.row}`).join(', ') + (rows.length > 6 ? ` and ${rows.length - 6} more` : '') + '. Rows below moved up.') : null,
      (a.changes || []).length > (a.examples || []).length ? h('details', {class: 'autofmt-all'}, h('summary', {}, `See all ${a.changes.length} cell changes`),
        h('ul', {}, a.changes.map(f => h('li', {}, h('button', {type: 'button', class: 'linkish', onclick: () => showCell(f.sheet, f.cell)}, `${f.sheet} ${f.cell}`), `: ${showWs(f.before)} → ${showWs(f.after)} (${f.kind})`)))) : null,
      a.from_hash ? h('button', {type: 'button', class: 'linkish small', onclick: () => select(a.from_hash)}, 'Open the original upload') : null);
  }
  function docBanner(r) {
    const d = r.source_document; if (!d) return null;
    const plural = (n, w) => `${n} ${w}${n === 1 ? '' : 's'}`;
    const found = d.tables ? `${plural(d.tables, 'table')} with ${plural(d.rows, 'row')}` : 'no tables, only text';
    return h('div', {class: 'docbanner'},
      h('div', {}, h('b', {}, `Read from a ${d.type} file: ${d.filename}`),
        h('span', {}, `Found ${found}${d.pages ? ` on ${plural(d.pages, 'page')}` : ''}. ` +
          (d.tables ? `They are checked like any report. Fixes come back as an Excel copy; your ${d.type} file is not changed.` : 'There is nothing to check as a table, but you can read the text below or ask Claude about it.'))),
      d.text ? h('details', {}, h('summary', {}, `Show the ${d.type} text`), h('pre', {class: 'doctext'}, d.text + (d.text_truncated ? '\n…' : ''))) : null);
  }
  // ================================================================ Focus Desk: spreadsheet in the middle, corrections on the right
  // Everything shown comes from the report's own check run (review.run): the same file hash, checker and SharePoint snapshot.
  const ICON = {
    review: 'M4 4h12l4 4v12H4z M8 12h8 M8 16h6', history: 'M12 7v5l3 2 M4 12a8 8 0 1 0 2.3-5.6 M4 4v3h3', sources: 'M4 6c0-1.7 3.6-3 8-3s8 1.3 8 3-3.6 3-8 3-8-1.3-8-3z M4 6v12c0 1.7 3.6 3 8 3s8-1.3 8-3V6 M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3',
    back: 'M15 5l-7 7 7 7', menu: 'M4 7h16 M4 12h16 M4 17h16', lock: 'M7 11V8a5 5 0 0 1 10 0v3 M5 11h14v10H5z', eye: 'M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12z M12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6z',
    warn: 'M12 3l10 18H2z M12 10v5 M12 18v.5', ok: 'M4 12l5 5L20 6', x: 'M6 6l12 12 M18 6L6 18', db: 'M4 6c0-1.7 3.6-3 8-3s8 1.3 8 3-3.6 3-8 3-8-1.3-8-3z M4 6v12c0 1.7 3.6 3 8 3s8-1.3 8-3V6',
    next: 'M9 5l7 7-7 7', prev: 'M15 5l-7 7 7 7', search: 'M10.5 4a6.5 6.5 0 1 0 0 13 6.5 6.5 0 0 0 0-13z M20 20l-4.8-4.8', note: 'M4 20h4L20 8l-4-4L4 16z', panel: 'M3 4h18v16H3z M15 4v16',
  };
  const icon = (name, size = 18) => { const s = document.createElementNS('http://www.w3.org/2000/svg', 'svg'); s.setAttribute('viewBox', '0 0 24 24'); s.setAttribute('width', size); s.setAttribute('height', size);
    s.setAttribute('aria-hidden', 'true'); s.setAttribute('class', 'ic'); const p = document.createElementNS('http://www.w3.org/2000/svg', 'path'); p.setAttribute('d', ICON[name]); s.append(p); return s; };
  const plural = (n, one, many) => `${n} ${n === 1 ? one : many || one + 's'}`;
  const when2 = iso => { if (!iso) return null; const d = new Date(iso); return isNaN(d) ? null : d.toLocaleString('en-US', {month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit', timeZone: 'America/Chicago'}) + ' CT'; };
  const outdated = r => r.internal_check_status === 'outdated_checks_require_recheck';
  const revisionOf = r => Number(r.revision || (r.fixed_from ? (r.fixed_from.revision || 1) + 1 : 1));

  // one truthful status for the whole report, and the one next step when it is not ready
  function fdStatus(r) {
    const rv = r.review, sp = rv.sharepoint || {}, a = r.assignment || {}, c = fdCounts();
    if (outdated(r)) return {tone: 'amber', icon: 'warn', text: 'Checks out of date', next: 'recheck', why: `Checked with an older version of the checks (${r.recheck?.checked_with || 'earlier'}). Nothing is trusted until it is checked again.`};
    if ((r.sent || []).some(x => x.sent)) return {tone: 'green', icon: 'ok', text: 'Sent to client', next: null};
    if (a.needs_person) return {tone: 'red', icon: 'x', text: 'Client not settled', next: 'client', why: a.reason || 'Choose the client and job before anything else; job numbers repeat between clients.'};
    if (c.blockers) return {tone: 'red', icon: 'x', text: plural(c.blockers, 'blocker'), next: 'blockers'};
    if (sp.status === 'stale') return {tone: 'amber', icon: 'warn', text: 'SharePoint check out of date', next: 'reconcile', why: sp.message};
    if (sp.status !== 'match') return {tone: 'amber', icon: 'warn', text: 'Not verified against SharePoint', next: 'reconcile',
      why: sp.status === 'day_level' ? sp.message : sp.status === 'incomplete' ? 'SharePoint could not be read completely, so the report is not verified. ' + (sp.message || '') : sp.status === 'unverified' ? sp.message || 'Some fields could not be compared.' : sp.status === 'differences' ? 'Differences from SharePoint are listed as blockers.' : 'The report has not been compared with SharePoint item by item yet.'};
    if (c.review) return {tone: 'amber', icon: 'warn', text: `${c.review} to review`, next: 'review'};
    if (r.approval && rv.approval_current) return {tone: 'green', icon: 'ok', text: 'Internally approved', next: 'send'};
    if (r.approval) return {tone: 'amber', icon: 'warn', text: 'Approval out of date', next: 'approve', why: 'It was approved on an earlier check run. Approve again if it still looks right.'};
    return {tone: 'green', icon: 'ok', text: 'Ready for internal approval', next: 'approve'};
  }

  // ---------- corrections: issues and suggested changes joined, grouped by root cause, in three tiers
  const TIERS = [['blocker', 'Blockers'], ['review', 'Needs review'], ['format', 'Formatting']];
  function fdItems() {
    const r = state.report, rv = r.review, dec = store.get(decisionsKey(), {}), decided = r.review_decisions || {};
    const issues = allIssues(rv).filter(i => i.severity !== 'info'), fixes = state.fixes || [], used = new Set(), items = [];
    const make = (i, f) => {
      const key = i ? i.key : 'fix:' + f.id, d = decided[key] || (f ? decided['fix:' + f.id] : null);
      const fmt = f && fmtKind(f);
      const tier = i && i.severity === 'high' ? 'blocker' : fmt && (!i || i.only) ? 'format' : 'review';
      const cell = f ? String(f.cell).toUpperCase() : i.cell && splitCell(i.cell) ? String(i.cell).toUpperCase() : null;
      const row = cell ? splitCell(cell)[1] : i?.row ? Number(i.row) : null;
      const src = i?.source_value != null ? i.source_value : f?.source === 'SharePoint' ? f.after : null;
      const kind = i ? i : {text: f.reason || '', code: 'fix:' + (fmt || f.source)};
      const group = tier + '|' + (fmt ? 'fmt:' + fmt : i ? sameKey(i) : 'fix:' + f.source);
      const stateNow = f && dec[f.id]?.state === 'accepted' ? 'selected' : (d?.action === 'reject' || (f && dec[f.id]?.state === 'rejected')) ? 'rejected' : d?.action === 'keep' ? 'kept' : 'open';
      const evidence = f ? (f.source === 'SharePoint' || i?.source_value != null ? 'source' : isSureFix(f) ? 'mechanical' : f.source === 'AI fix' ? 'ai' : 'guess') : i?.source_value != null ? 'source_only' : 'none';
      return {key, issue: i, fix: f, tier, group, cell, row, sheet: f?.sheet || i?.sheet || null, kind, fmt, state: stateNow, note: d?.note || '', decidedBy: d?.by,
        report: f ? f.before : i?.report_value ?? null, source: src, proposed: f ? (dec[f.id]?.after ?? f.after) : null,
        reason: fmt ? `Only ${fmt} change; the value itself stays the same.` : f?.only || f?.reason || i?.why || i?.text || '',
        text: i?.text || f?.reason || '', url: i?.url || null, item: i?.item || null, evidence,
        selectable: !!f && evidence !== 'none' && stateNow !== 'rejected'};
    };
    // a change can settle more than one issue on the same cell (the export tab and the live list both disagree with J5):
    // each issue card shows it, and selecting it from either card selects the same single change
    for (const i of issues) { const f = fixes.find(x => fixCovers(x, i) && (x.source === 'SharePoint' || !used.has(x.id))) || fixes.find(x => fixCovers(x, i)); if (f) used.add(f.id); items.push(make(i, f)); }
    for (const f of fixes) if (!used.has(f.id)) items.push(make(null, f));
    return items;
  }
  function fdGroups(items) {
    const by = new Map();
    for (const it of items) { if (!by.has(it.group)) by.set(it.group, {key: it.group, tier: it.tier, items: []}); by.get(it.group).items.push(it); }
    const rank = {blocker: 0, review: 1, format: 2};
    return [...by.values()].map(g => {
      const [one, many] = kindNames(g.items[0].kind), n = g.items.length, first = g.items[0];
      // one item: its own plain sentence; several: the plural name of the shared cause
      const own = String(first.issue?.text || first.text || '').replace(/\s*\(Day-level comparison[^)]*\)/, '');
      const generic = /…/.test(one) || !one;
      g.title = (generic ? (n === 1 ? own : `${own.slice(0, 70)}${own.length > 70 ? '…' : ''} (and ${n - 1} more like it)`) : n === 1 ? one : (many || one)).replace(/^./, c => c.toUpperCase());
      if (g.title.length > 120) g.title = g.title.slice(0, 117) + '…';
      g.records = new Set(g.items.map(x => x.sheet && x.row ? x.sheet + '!' + x.row : x.key)).size;
      g.open = g.items.filter(x => x.state === 'open').length;
      return g;
    }).sort((a, b) => rank[a.tier] - rank[b.tier] || b.items.length - a.items.length);
  }
  function fdCounts() {
    if (!state.report) return {};
    const items = state.fdItems || [], open = items.filter(x => x.state === 'open' || x.state === 'selected');
    const c = readyChanges();
    return {detected: allIssues(state.report.review).filter(i => i.severity !== 'info').length, suggestions: (state.fixes || []).length,
      selected: c.total, applied: (state.report.fixed_from?.changes || []).length,
      blockers: items.filter(x => x.tier === 'blocker' && x.state !== 'rejected').length + (state.report.holds || []).filter(x => x.hard).length,
      review: open.filter(x => x.tier === 'review').length, format: open.filter(x => x.tier === 'format').length};
  }

  // ---------- the page
  function drawReport() {
    const r = state.report, rv = r.review;
    document.body.classList.add('reportmode');
    state.sideTab ||= 'fixes'; state.fdView ||= 'review';
    state.fdItems = state.fixes ? fdItems() : [];
    const a = r.assignment || {}, sp = rv.sharepoint || {}, st = fdStatus(r);
    const close = e => e.target.closest('details')?.removeAttribute('open');
    const more = h('details', {class: 'more-menu'}, h('summary', {class: 'btn'}, 'More'),
      h('div', {class: 'menu'},
        !r.approval && (r.review_request || {}).status !== 'waiting' ? h('button', {type: 'button', onclick: e => { close(e); reviewForm('request'); }}, 'Send for review') : null,
        h('button', {type: 'button', onclick: e => { close(e); copyClientSummary(); }}, 'Copy client summary for an email'),
        h('button', {type: 'button', onclick: e => { close(e); makeChangeSummary(); }}, 'Change summary (PDF)'),
        r.filename.toLowerCase().endsWith('.xlsx') ? h('button', {type: 'button', onclick: e => { close(e); downloadWithPhotos(r); }}, 'Download with SharePoint photos') : null,
        h('a', {href: '/api/spreadsheets/download?id=' + r.file_hash, download: r.filename, onclick: close}, `Download this version (v${revisionOf(r)})`),
        r.approval && rv.approval_current ? h('button', {type: 'button', onclick: e => { close(e); openSend(); }}, 'Send to client…') : null));
    const crumb = [a.client || h('em', {}, 'Client not settled'), rv.job || r.filename, rv.day ? shortDay(rv.day) : 'No business date'];
    const recordsChecked = sp.status === 'match' || sp.status === 'differences' || sp.status === 'incomplete' || sp.status === 'unverified'
      ? `${sp.matched ?? '?'} of ${rv.scans ?? '?'} records checked against SharePoint` : sp.status === 'day_level' ? 'Day totals only (not record by record)' : 'Not compared with SharePoint';
    const head = h('header', {class: 'fd-head'},
      h('div', {class: 'fd-title'},
        h('p', {class: 'fd-eyebrow'}, 'Review workspace'),
        h('h1', {}, ...crumb.flatMap((x, n) => n ? [h('span', {class: 'sep', 'aria-hidden': 'true'}, '/'), x] : [x])),
        h('p', {class: 'fd-meta'},
          h('span', {}, `v${revisionOf(r)}${r.fixed_from ? ` · made from v${r.fixed_from.revision || 1}` : ' · original upload'}`),
          h('span', {}, icon('db', 14), ' ', recordsChecked),
          sp.captured_at ? h('span', {}, `Source captured ${when2(sp.captured_at)}`) : null,
          (sp.fields_checked || []).length ? h('span', {title: (sp.fields_not_checked || []).length ? 'Not checked: ' + sp.fields_not_checked.join(', ') : null},
            `Fields checked: ${sp.fields_checked.join(', ')}${(sp.fields_not_checked || []).length ? ` · not checked: ${sp.fields_not_checked.join(', ')}` : ''}`) : null)),
      h('div', {class: 'fd-actions'},
        h('div', {class: `fd-status ${st.tone}`, role: 'status'}, icon(st.icon, 16), h('span', {}, st.text)),
        more, h('button', {type: 'button', class: 'btn dl-changes', onclick: downloadWithChanges}, 'Download')));
    const metrics = fdMetrics(r);
    const rail = h('nav', {class: 'fd-rail', 'aria-label': 'Report'},
      h('button', {type: 'button', class: 'fd-railbtn', onclick: () => { location.hash = state.client ? 'c=' + encodeURIComponent(state.client) : ''; }, 'aria-label': 'Back to all reports', title: 'All reports'}, icon('back', 22), h('span', {}, 'Reports')),
      ...[['review', 'Review'], ['history', 'History'], ['sources', 'Sources']].map(([id, label]) =>
        h('button', {type: 'button', class: 'fd-railbtn' + (state.fdView === id ? ' on' : ''), 'aria-pressed': String(state.fdView === id), onclick: () => { state.fdView = id; drawReport(); }}, icon(id, 22), h('span', {}, label))),
      h('button', {type: 'button', class: 'fd-railbtn fd-menu', onclick: () => document.body.classList.toggle('navopen'), 'aria-label': 'All pages'}, icon('menu', 22), h('span', {}, 'Menu')));
    let main;
    if (state.fdView === 'history') main = fdHistory(r);
    else if (state.fdView === 'sources') main = fdSources(r);
    else {
      main = h('div', {class: 'fd-work' + (state.fdAsideHidden ? ' aside-hidden' : ''), id: 'fd-work'},
        h('section', {class: 'fd-sheet', 'aria-label': 'Spreadsheet'},
          h('div', {class: 'sheetpane', id: 'sheet-block'}, h('p', {class: 'note fd-loading'}, h('span', {class: 'spin'}), ' Loading the spreadsheet…')),
          h('div', {id: 'fd-evidence'})),
        h('div', {class: 'fd-split', role: 'separator', 'aria-orientation': 'vertical', 'aria-label': 'Resize the corrections panel', tabindex: '0',
          onpointerdown: fdDragStart, onkeydown: e => { if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') { fdAsideWidth(fdAsideWidth() + (e.key === 'ArrowLeft' ? 24 : -24)); e.preventDefault(); } }}),
        h('aside', {class: 'sidepane fd-aside', id: 'side', 'aria-label': 'Corrections'}));
    }
    const sel = readyChanges().total;
    const foot = h('div', {class: 'fd-foot', role: 'region', 'aria-label': 'Selected changes'},
      h('span', {class: 'fd-foot-state'}, icon('lock', 16), h('b', {id: 'fd-selcount'}, sel ? plural(sel, 'change') + ' selected' : 'No changes selected'),
        h('span', {class: 'fd-foot-note'}, `Original v1 is kept unchanged${r.fixed_from ? '; this is v' + revisionOf(r) : ''}`)),
      h('button', {type: 'button', class: 'btn fd-mobile-panel', onclick: () => document.body.classList.toggle('fd-panel-open'), 'aria-controls': 'side'}, 'Corrections', h('i', {id: 'fd-mobile-count'}, String(fdCounts().blockers + fdCounts().review || ''))),
      h('button', {type: 'button', class: 'btn ' + (sel ? 'primary' : ''), id: 'fd-preview-btn', disabled: !sel || state.busy, onclick: fdPreview}, icon('eye', 16), ' Preview selected changes'));
    $('report').replaceChildren(h('div', {class: 'fd'}, rail, h('div', {class: 'fd-main'}, head, metrics, main), foot));
    const w = store.get('fd-aside-w', null); if (w) fdAsideWidth(w);
    if (state.fdView !== 'review') return;
    state.grid = state.grid?.hash === r.file_hash ? state.grid : null;
    drawSide(); updateDownloads();
    loadGrid(state.gridSheet && state.grid ? state.gridSheet : undefined);
  }
  function fdMetrics(r) {
    const rv = r.review, perRow = rv.rejected_unit === 'rows', base = perRow ? rv.scans : rv.inspected;
    const rate = base && rv.rejected != null && !rv.rejected_partial ? rv.rejected / base * 100 : null;
    const photos = r.photos?.image_count ?? null, shifts = (r.shift_summary || r.shifts || []).length || null;
    const m = (value, label, extra) => h('div', {class: 'fd-metric'}, h('strong', {}, value), h('span', {}, label), extra ? h('small', {}, extra) : null);
    return h('div', {class: 'fd-metrics', 'aria-label': 'Key totals'},
      m(fmt(rv.scans), 'records', 'audit rows, counted once'),
      m(fmt(rv.inspected), 'units inspected'),
      rv.certified != null ? m(fmt(rv.certified), 'OK') : null,
      m(rv.rejected == null ? (rv.rejected_partial ? 'Partly recorded' : 'Not recorded') : fmt(rv.rejected), perRow ? 'failed audits' : 'rejected units', rv.rejected == null ? 'blank is not zero' : null),
      m(rate == null ? 'Unavailable' : rate.toFixed(1) + '%', perRow ? 'fail rate' : 'reject rate', rate == null ? 'rejects not recorded on every record' : null),
      shifts ? m(String(shifts), shifts === 1 ? 'shift' : 'shifts', 'as recorded') : null,
      photos != null ? m(String(photos), photos === 1 ? 'photo' : 'photos', 'linked to records') : null);
  }
  // resizable / collapsible corrections panel (width remembered in this browser only)
  function fdAsideWidth(px) {
    const work = $('fd-work'); if (!work) return 0;
    if (px == null) return parseInt(getComputedStyle(work).getPropertyValue('--aside-w')) || 380;
    px = Math.max(300, Math.min(620, Math.round(px)));
    work.style.setProperty('--aside-w', px + 'px'); store.set('fd-aside-w', px); return px;
  }
  function fdDragStart(e) {
    const work = $('fd-work'); if (!work || e.button !== 0) return;
    e.preventDefault(); const right = work.getBoundingClientRect().right;
    const move = ev => fdAsideWidth(right - ev.clientX), up = () => { removeEventListener('pointermove', move); removeEventListener('pointerup', up); };
    addEventListener('pointermove', move); addEventListener('pointerup', up);
  }

  // ---------- right-hand panel
  function fdCorrections(body) {
    const r = state.report, st = fdStatus(r), c = fdCounts();
    if (!state.fixes) { body.append(h('div', {class: 'fd-panel-loading', role: 'status'}, h('span', {class: 'spin'}), ' Reading the corrections…')); return; }
    const items = state.fdItems = fdItems(), groups = fdGroups(items);
    const tier = state.fdTier && groups.some(g => g.tier === state.fdTier) ? state.fdTier : (groups.find(g => g.open)?.tier || groups[0]?.tier || 'blocker');
    state.fdTier = tier;
    body.append(h('div', {class: 'fd-panelhead'}, h('h2', {}, 'Corrections'),
      h('button', {type: 'button', class: 'fd-iconbtn fd-collapse', 'aria-label': 'Hide the corrections panel', title: 'Hide panel', onclick: () => { state.fdAsideHidden = true; $('fd-work')?.classList.add('aside-hidden'); }}, icon('panel', 18))));
    body.append(h('p', {class: 'fd-countline'}, `${plural(c.detected, 'issue')} detected · ${plural(c.suggestions, 'suggestion')} · ${c.selected} selected · ${c.applied} applied in this version`));
    const step = c.selected ? 2 : 1;
    body.append(h('ol', {class: 'fd-steps', 'aria-label': 'Steps'}, ['Compare', 'Preview', 'Recheck'].map((t, n) => h('li', {class: n + 1 === step ? 'on' : n + 1 < step ? 'done' : '', 'aria-current': n + 1 === step ? 'step' : null}, `${n + 1} ${t}`))));
    // a report that is not ready says why, with one clear next step
    const auto = fdAutoSummary(r); if (auto) body.append(auto);
    const nextStep = fdNextStep(r, st);
    if (nextStep) body.append(nextStep);
    if (!items.length) { body.append(fdClean(r, st)); return; }
    body.append(h('div', {class: 'fd-tiers', role: 'tablist', 'aria-label': 'Kinds of correction'}, TIERS.map(([id, label]) => {
      const gs = groups.filter(g => g.tier === id), recs = gs.reduce((n, g) => n + g.records, 0);
      return h('button', {type: 'button', role: 'tab', class: id === tier ? 'on' : '', 'aria-selected': String(id === tier), disabled: !gs.length,
        onclick: () => { state.fdTier = id; state.fdCur = null; drawSide(); }},
        h('span', {}, label), h('b', {'aria-label': 'open'}, String(gs.reduce((n, g) => n + g.open, 0))),
        h('small', {}, gs.length ? `${plural(gs.length, 'group')} · ${plural(recs, 'record')}${gs.some(g => g.open < g.items.length) ? ` · ${gs.reduce((n, g) => n + g.items.length - g.open, 0)} decided` : ''}` : 'none'));
    })));
    const tierGroups = groups.filter(g => g.tier === tier);
    const cur = items.find(x => x.key === state.fdCur) || null;
    if (cur) body.append(fdCard(cur, groups));
    body.append(h('div', {class: 'fd-groups'}, tierGroups.map(g => {
      const selectable = g.items.filter(x => x.selectable && x.state === 'open' && (x.evidence === 'source' || x.evidence === 'mechanical'));
      return h('section', {class: 'fd-group' + (cur && cur.group === g.key ? ' cur' : '')},
        h('button', {type: 'button', class: 'fd-grouphead', 'aria-expanded': String(!!cur && cur.group === g.key), onclick: () => fdOpen(g.items.find(x => x.state === 'open') || g.items[0])},
          h('span', {class: 'fd-gtitle'}, g.open ? '' : icon('ok', 14), g.open ? '' : ' ', g.title),
          h('span', {class: 'fd-gmeta'}, `${plural(g.items.length, 'item')} · ${plural(g.records, 'record')}${g.open < g.items.length ? ` · ${g.items.length - g.open} decided` : ''}`)),
        selectable.length > 1 || (selectable.length === 1 && g.items.length === 1 && g.tier === 'format') ? h('button', {type: 'button', class: 'btn small fd-selectall', onclick: () => fdSelect(selectable, true)},
          `Select ${selectable.length === 1 ? 'it' : 'all ' + selectable.length} for preview`) : null,
        g.tier === 'review' && !selectable.length && g.open > 1 && g.items.every(x => !x.fix) ? h('button', {type: 'button', class: 'btn small fd-selectall', onclick: () => fdDecideAll(g.items.filter(x => x.state === 'open'), 'keep')},
          `Keep all ${g.open} as recorded`) : null);
    })));
    if (tier === 'blocker' && (r.holds || []).some(x => x.hard)) body.append(h('div', {class: 'fd-hold'}, icon('lock', 16), h('span', {}, 'Internal approval blocked', h('small', {}, (r.holds || []).filter(x => x.hard).map(x => x.text).join(' ')))));
  }
  // ---------- the app fixes what it can by itself (new version, original kept), then shows only what needs a person
  function fdAutoEligible(r) {
    if (!r || r.approval || (r.sent || []).some(x => x.sent) || r.auto_fix_tried || r.auto_fixed || (r.fixes_applied || []).length) return false;
    if ((r.fixed_from?.changes || []).some(c => String(c.source || '').startsWith('Reverted'))) return false;
    const open = allIssues(r.review).filter(i => i.severity !== 'info').length;
    return open > 0 || (state.fixes || []).some(f => f.sure);
  }
  async function fdMaybeAutoFix(hash) {
    const r = state.report; if (!r || r.file_hash !== hash || !fdAutoEligible(r)) return;
    (state.autoTried ||= new Set()); if (state.autoTried.has(hash)) return; state.autoTried.add(hash);
    state.autoFixing = hash; if (state.sideTab === 'fixes') drawSide();
    try {
      const out = await api('/api/spreadsheets/auto-fix', {file_hash: hash});
      const s = out.auto_fixed || {}, n = (s.changed || []).length + (s.removed || []).length + (s.moved || []).length, fine = (s.fine || []).length;
      state.autoFixing = null;
      if (state.selected !== hash) return;
      toast(n || fine ? `Fixed ${plural(n, 'thing')}${fine ? ` and confirmed ${fine} fine` : ''} automatically. Every fix can be reverted.` : 'Nothing could be fixed automatically; what is left needs a person.');
      await loadList();
      if (out.item.file_hash !== hash) select(out.item.file_hash); else openReport(hash, true);
    } catch (e) { state.autoFixing = null; if (state.selected === hash) { toast(e.message, true); if (state.sideTab === 'fixes') drawSide(); } }
  }
  function fdAutoSummary(r) {
    if (state.autoFixing === r.file_hash) return h('div', {class: 'fd-auto working', role: 'status'}, h('span', {class: 'spin'}), h('div', {}, h('b', {}, 'Fixing what can be fixed…'),
      h('p', {}, 'SharePoint values and spacing first, then Claude works through the rest. Your original stays as it is.')));
    const s = r.auto_fixed; if (!s) return null;
    const changed = (s.changed || []).filter(c => !c.reverted), fine = (s.fine || []).filter(f => !f.reverted), rows = [...(s.removed || []), ...(s.moved || [])];
    const left = fdCounts(), todo = (left.blockers || 0) + (left.review || 0);
    const total = changed.length + fine.length + rows.length;
    const revert = async (cells, fineTexts, label) => {
      toast(`Reverting ${label}…`);
      try { const out = await api('/api/spreadsheets/revert-fix', {file_hash: r.file_hash, cells, fine: fineTexts, by: myName()});
        toast(`Reverted ${label}.${cells.length ? ' Saved as a new version; the checks ran again.' : ' It is back on your list.'}`);
        await loadList(); if (out.item.file_hash !== r.file_hash) select(out.item.file_hash); else openReport(r.file_hash, true);
      } catch (e) { toast(e.message, true); }
    };
    const line = (what, why, by, act) => h('li', {}, h('div', {}, h('b', {}, what), h('small', {}, [by, why].filter(Boolean).join(' · '))), act);
    return h('section', {class: 'fd-auto', 'aria-label': 'Fixed for you'},
      h('div', {class: 'fd-autohead'}, icon('ok', 18), h('div', {},
        h('b', {}, total ? `Fixed for you: ${plural(total, 'item')}` : 'Nothing was changed automatically'),
        h('p', {}, todo ? `${plural(todo, 'item')} left that only a person can decide.` : 'Nothing is left that needs a person.'))),
      total ? h('details', {class: 'fd-autolist', open: total <= 6 ? true : null}, h('summary', {}, 'What was done'),
        h('ul', {},
          changed.map(c => line(`${c.sheet}!${c.cell}: ${showWs(c.before)} → ${showWs(c.after)}`, c.reason, c.by === 'Claude' ? 'Claude' : c.source,
            h('button', {type: 'button', class: 'btn small', onclick: () => revert([{sheet: c.sheet, cell: c.cell}], [], `${c.sheet}!${c.cell}`)}, 'Revert'))),
          fine.map(f => line(`Confirmed fine: ${f.text}`, f.reason, 'Claude',
            h('button', {type: 'button', class: 'btn small', onclick: () => revert([], [f.text], 'that decision')}, 'Revert'))),
          rows.map(x => line(x.to_sheet ? `${x.sheet} row ${x.row} moved to ${x.to_sheet}` : `${x.sheet} row ${x.row} taken out`, x.reason, 'Claude', null))),
        h('div', {class: 'fd-autoacts'},
          changed.length + fine.length > 1 ? h('button', {type: 'button', class: 'btn small', onclick: () => revert(changed.map(c => ({sheet: c.sheet, cell: c.cell})), fine.map(f => f.text), 'all automatic fixes')}, 'Revert all') : null,
          s.from_hash ? h('a', {href: '#r=' + s.from_hash}, 'Open the version before these fixes') : null)) : null,
      (s.notes || []).length ? h('p', {class: 'fd-hint'}, s.notes.join(' ')) : null);
  }
  function fdNextStep(r, st) {
    const box = (title, text, button) => h('div', {class: 'fd-next ' + st.tone}, h('b', {}, title), text ? h('p', {}, text) : null, button);
    if (st.next === 'recheck') return box('Checks out of date', st.why, h('button', {type: 'button', class: 'btn primary', onclick: fdRecheck}, 'Re-check this report'));
    if (st.next === 'client') return box('Choose the client and job', st.why, h('button', {type: 'button', class: 'btn primary', onclick: e => assignForm(r, e.currentTarget)}, 'Choose client and job'));
    if (st.next === 'reconcile') return box(st.text, st.why, h('button', {type: 'button', class: 'btn', onclick: () => fdReconcile()}, 'Check against SharePoint'));
    return null;
  }
  function fdClean(r, st) {
    const rv = r.review, sp = rv.sharepoint || {}, a = r.assignment || {}, sel = r.dataset_selection || {};
    if (st.next !== 'approve' && st.next !== 'send') return h('div', {class: 'fd-clean muted'}, h('p', {}, 'No corrections to make. The report is not ready for approval until the step above is done.'));
    const approved = r.approval && rv.approval_current;
    const li = (ok, text) => h('li', {class: ok ? 'ok' : 'no'}, icon(ok ? 'ok' : 'warn', 15), text);
    return h('div', {class: 'fd-clean'}, icon('ok', 40),
      h('h3', {}, approved ? 'Internally approved.' : 'Everything lines up.'),
      h('ul', {},
        li(true, `Client and job: ${a.label || [a.client, rv.job].filter(Boolean).join(' → ')}`),
        li(true, `${plural(rv.scans || 0, 'record')} checked once against SharePoint${sp.captured_at ? ` (captured ${when2(sp.captured_at)})` : ''}`),
        li(!(sp.fields_not_checked || []).length, (sp.fields_not_checked || []).length ? `Not checked: ${sp.fields_not_checked.join(', ')} (no SharePoint column)` : `Fields checked: ${(sp.fields_checked || []).join(', ')}`),
        li(rv.rejected != null, rv.rejected == null ? 'Rejects not recorded: the rate is shown as unavailable' : `Rejects recorded: ${fmt(rv.rejected)}`),
        (sel.authoritative || []).length ? li(true, `One record set: ${(sel.authoritative || []).join(', ')}; summary and photo tabs are not added again`) : null),
      approved ? h('p', {class: 'fd-hint'}, `Approved by ${r.approval.by} ${ago(r.approval.at)}. Sending to the client is a separate step (More → Send to client…).`)
        : h('div', {class: 'fd-approve'},
          h('label', {}, 'Your name', h('input', {id: 'fd-approver', value: myName(), autocomplete: 'name'})),
          h('button', {type: 'button', class: 'btn primary', id: 'fd-approve-btn', onclick: fdApprove}, 'Approve internally'),
          h('p', {class: 'fd-hint'}, 'Internal approval only. Nothing is emailed; sending to the client is a separate step.')));
  }
  function fdCard(it, groups) {
    const g = groups.find(x => x.key === it.group), inGroup = g.items, pos = inGroup.indexOf(it);
    const flat = groups.filter(x => x.tier === it.tier).flatMap(x => x.items), at = flat.indexOf(it);
    const cmp = (label, value, cls) => h('div', {class: 'fd-cmp ' + (cls || '')}, h('b', {}, label), h('span', {}, value == null ? '—' : showWs(value)));
    const conf = {source: ['Source-backed', 'SharePoint has this value for the same site, list and item.'], mechanical: ['Mechanical', 'Only how the value is written changes.'],
      ai: ['Claude\'s suggestion', 'Not from a source: check it before selecting.'], guess: ['Needs your judgement', 'Worked out from the rest of the report, not from a source.'],
      source_only: ['Source value, no change prepared', 'SharePoint has a value, but no safe cell to change was found. Correct it by hand or keep it on hold.'],
      none: ['No source value', 'Nothing supports a change, so the value stays as recorded and the issue stays open.']}[it.evidence];
    const where = [it.sheet, it.cell || (it.row ? 'row ' + it.row : null), it.item ? 'SharePoint item ' + it.item : null].filter(Boolean).join(' · ');
    const decided = it.state === 'kept' ? `Kept as recorded${it.decidedBy ? ' by ' + it.decidedBy : ''}` : it.state === 'rejected' ? `Suggestion rejected${it.decidedBy ? ' by ' + it.decidedBy : ''}` : it.state === 'selected' ? 'Selected for preview' : null;
    return h('article', {class: 'fd-card ' + it.tier, 'aria-label': 'Selected correction', id: 'fd-card'},
      h('div', {class: 'fd-cardtop'}, icon(it.tier === 'blocker' ? 'x' : it.tier === 'format' ? 'note' : 'warn', 20),
        h('div', {}, h('h3', {}, g.title), h('p', {}, `${where || 'Whole report'} · item ${pos + 1} of ${inGroup.length}`)),
        h('button', {type: 'button', class: 'fd-iconbtn', 'aria-label': 'Close this correction', onclick: () => { state.fdCur = null; drawSide(); }}, icon('x', 16))),
      it.text && it.text !== g.title ? h('p', {class: 'fd-text'}, it.text) : null,
      h('div', {class: 'fd-cmps'}, cmp('Report', it.report ?? (it.fix ? '' : null)), cmp('Source', it.source, it.source == null ? 'empty' : ''), cmp('Proposed', it.proposed, it.proposed == null ? 'empty' : 'prop')),
      h('p', {class: 'fd-reason'}, it.reason),
      h('p', {class: 'fd-conf ' + it.evidence}, h('b', {}, conf[0]), ' ', conf[1]),
      decided ? h('p', {class: 'fd-decided'}, icon('ok', 14), ' ', decided, it.note ? ` · “${it.note}”` : '') : null,
      h('div', {class: 'fd-cardacts'},
        it.selectable ? h('button', {type: 'button', class: 'btn ' + (it.state === 'selected' ? '' : 'primary'), onclick: () => fdSelect([it], it.state !== 'selected')},
          it.state === 'selected' ? 'Remove from preview' : 'Select for preview') : null,
        it.fix && it.state !== 'rejected' ? h('button', {type: 'button', class: 'btn', onclick: () => fdDecide(it, 'reject')}, 'Reject suggestion') : null,
        it.state !== 'kept' ? h('button', {type: 'button', class: 'btn', onclick: () => fdDecide(it, 'keep')}, it.report === '' || it.report == null ? 'Keep blank' : 'Keep as recorded') : null,
        it.state === 'open' && !it.fix && inGroup.filter(x => x.state === 'open').length > 1 ? h('button', {type: 'button', class: 'btn', onclick: () => fdDecideAll(inGroup.filter(x => x.state === 'open'), 'keep')},
          `${it.report === '' || it.report == null ? 'Keep all' : 'Keep all'} ${inGroup.filter(x => x.state === 'open').length} ${it.report === '' || it.report == null ? 'blank' : 'as recorded'}`) : null,
        it.state !== 'open' ? h('button', {type: 'button', class: 'btn subtle', onclick: () => fdDecide(it, 'reopen')}, 'Undo decision') : null,
        h('button', {type: 'button', class: 'btn subtle', onclick: () => fdEvidence(it)}, icon('db', 15), ' Source evidence')),
      h('details', {class: 'fd-notebox', open: it.note ? true : null}, h('summary', {}, icon('note', 14), ' Note'),
        h('textarea', {id: 'fd-note', 'aria-label': 'Review note', rows: '2', placeholder: 'Why you kept, rejected or changed this…'}, it.note || ''),
        h('button', {type: 'button', class: 'btn small', onclick: () => fdDecide(it, 'note', $('fd-note').value)}, 'Save note')),
      it.tier === 'blocker' && it.evidence === 'none' ? h('p', {class: 'fd-holdnote'}, icon('lock', 14), ' Keeping it as recorded does not clear the hold; the report stays blocked until the source question is settled.') : null,
      h('div', {class: 'fd-nav'},
        h('button', {type: 'button', class: 'btn small', disabled: at <= 0, onclick: () => fdOpen(flat[at - 1]), 'aria-keyshortcuts': 'k'}, icon('prev', 15), ' Previous'),
        h('span', {}, `${at + 1} of ${flat.length}`),
        h('button', {type: 'button', class: 'btn small', disabled: at >= flat.length - 1, onclick: () => fdOpen(flat[at + 1]), 'aria-keyshortcuts': 'j'}, 'Next ', icon('next', 15))));
  }
  // open a correction: its exact cell is scrolled into view and highlighted; the sheet switches if needed
  async function fdOpen(it) {
    if (!it) return;
    state.fdCur = it.key; state.fdTier = it.tier;
    if (it.sheet && (!state.grid || state.grid.sheet !== it.sheet)) { fdKeepScroll(); await loadGrid(it.sheet, true); }
    state.gridCell = it.cell || (it.row ? `A${it.row}` : null); state.gridRow = it.row || null;
    drawSide(); drawGrid();
    const td = state.gridCell && document.querySelector(`#sheet-block td[data-cell="${state.gridCell}"]`), wrap = document.querySelector('#sheet-block .gridwrap');
    if (td && wrap) reveal(wrap, td, true);
    if (document.body.classList.contains('fd-narrow')) document.body.classList.add('fd-panel-open');
    $('fd-card')?.querySelector('h3')?.setAttribute('tabindex', '-1');
  }
  function fdSelect(list, on) {
    const dec = store.get(decisionsKey(), {});
    for (const it of list) if (it.fix) { if (on) dec[it.fix.id] = {state: 'accepted', after: it.proposed}; else delete dec[it.fix.id]; }
    store.set(decisionsKey(), dec);
    toast(on ? `${plural(list.length, 'change')} selected for preview. Nothing changes until you preview and apply.` : 'Removed from the preview.');
    fdRefresh();
  }
  async function fdDecide(it, action, note) {
    try {
      const by = myName();
      const out = await api('/api/spreadsheets/decision', {file_hash: state.report.file_hash, key: it.key, action, note: note || '', by});
      state.report.review_decisions = out.decisions;
      if (it.fix) { const dec = store.get(decisionsKey(), {}); if (action === 'reject') dec[it.fix.id] = {state: 'rejected'}; else if (action === 'reopen' || action === 'keep') delete dec[it.fix.id]; store.set(decisionsKey(), dec); }
      if (action === 'note' || action === 'reopen') toast(action === 'note' ? 'Note saved with this version.' : 'Decision undone.');
      if (action === 'reject' || action === 'keep') fdAdvance(it); else fdRefresh();
    } catch (e) { toast(e.message, true); }
  }
  // after a decision: the next open item in the same group, then the next group of the same kind, then the next kind;
  // the toast says how many are left so every click visibly moves forward
  function fdAdvance(done) {
    state.fdItems = fdItems();
    const groups = fdGroups(state.fdItems), g = groups.find(x => x.key === done.group);
    const order = [...groups.filter(x => x.tier === done.tier), ...groups.filter(x => x.tier !== done.tier)];
    const start = g ? order.indexOf(g) : 0, rotated = [...order.slice(start), ...order.slice(0, start)];
    let next = null;
    if (g) { const at = g.items.findIndex(x => x.key === done.key); next = [...g.items.slice(at + 1), ...g.items.slice(0, at)].find(x => x.state === 'open'); }
    if (!next) for (const x of rotated.slice(1)) { next = x.items.find(y => y.state === 'open'); if (next) break; }
    const leftInGroup = g ? g.items.filter(x => x.state === 'open').length : 0, leftInTier = state.fdItems.filter(x => x.tier === done.tier && x.state === 'open').length;
    const what = done.state === 'kept' || (state.report.review_decisions || {})[done.key]?.action === 'keep' ? 'Kept as recorded' : 'Suggestion rejected';
    toast(`${what}. ` + (leftInGroup ? `${leftInGroup} left in this group.` : leftInTier ? `This group is done; ${leftInTier} left in ${TIERS.find(t => t[0] === done.tier)[1].toLowerCase()}.` : `Nothing left in ${TIERS.find(t => t[0] === done.tier)[1].toLowerCase()}.`)
      + (done.tier === 'blocker' && done.evidence === 'none' ? ' The hold stays until the source question is settled.' : ''));
    if (next) { fdOpen(next); return; }
    state.fdCur = null; drawSide(); drawGrid(); fdRefreshFoot();
  }
  async function fdDecideAll(list, action) {
    try {
      const out = await api('/api/spreadsheets/decision', {file_hash: state.report.file_hash, keys: list.map(x => x.key), action, by: myName()});
      state.report.review_decisions = out.decisions;
      toast(`${plural(list.length, 'item')} kept as recorded. Saved with this version.`);
      if (list[0]) fdAdvance(list[0]); else fdRefresh();
    } catch (e) { toast(e.message, true); }
  }
  function fdRefreshFoot() {
    const sel = readyChanges().total, b = $('fd-preview-btn'), n = $('fd-selcount');
    if (b) { b.disabled = !sel || state.busy; b.classList.toggle('primary', !!sel); }
    if (n) n.textContent = sel ? plural(sel, 'change') + ' selected' : 'No changes selected';
    if (state.report) { const st = fdStatus(state.report), pill = document.querySelector('.fd-status');
      if (pill) { pill.className = 'fd-status ' + st.tone; pill.replaceChildren(icon(st.icon, 16), h('span', {}, st.text)); } }
  }
  function fdRefresh() {
    state.fdItems = fdItems(); drawSide(); drawGrid(); fdRefreshFoot();
  }

  // ---------- source evidence: shown beside the sheet, so the place in the spreadsheet is kept
  async function fdEvidence(it) {
    const slot = $('fd-evidence'); if (!slot) return;
    const sp = state.report.review.sharepoint || {};
    const close = h('button', {type: 'button', class: 'fd-iconbtn', 'aria-label': 'Close source evidence', onclick: () => slot.replaceChildren()}, icon('x', 16));
    const box = h('section', {class: 'fd-drawer', 'aria-label': 'Source evidence', tabindex: '-1'},
      h('div', {class: 'fd-drawerhead'}, h('b', {}, icon('db', 16), ` ${it.item ? 'SharePoint item ' + it.item : it.sheet ? `${it.sheet} row ${it.row || '?'}` : 'Whole report'} · source evidence`), close),
      h('dl', {}, h('dt', {}, 'Report'), h('dd', {}, it.report == null ? '—' : showWs(it.report)), h('dt', {}, 'SharePoint'), h('dd', {}, it.source == null ? 'No value for this field' : showWs(it.source)),
        h('dt', {}, 'Snapshot'), h('dd', {}, sp.snapshot_id ? `${String(sp.snapshot_id).slice(0, 12)} · ${when2(sp.captured_at) || ''}` : 'None for this version'),
        h('dt', {}, 'List'), h('dd', {}, sp.source || '—')),
      it.url || sp.web_url ? h('a', {href: it.url || sp.web_url, target: '_blank', rel: 'noopener'}, 'Open on SharePoint (read only)') : null,
      h('div', {id: 'fd-rowcells'}, h('p', {class: 'note'}, h('span', {class: 'spin'}), ' Reading the row…')));
    slot.replaceChildren(box); box.focus();
    if (!it.sheet || !it.row) { $('fd-rowcells').replaceChildren(); return; }
    try {
      const row = await api('/api/spreadsheets/cells', {file_hash: state.report.file_hash, sheet: it.sheet, row: it.row});
      $('fd-rowcells')?.replaceChildren(h('table', {class: 'fd-rowtable'}, h('tbody', {}, (row.cells || []).filter(c => c.header || c.value !== '').slice(0, 24).map(c =>
        h('tr', {class: c.cell === it.cell ? 'cur' : ''}, h('th', {}, c.header || c.cell), h('td', {}, c.value === '' || c.value == null ? '(blank)' : String(c.value)))))));
    } catch (e) { $('fd-rowcells')?.replaceChildren(h('p', {class: 'note'}, e.message)); }
  }

  // ---------- preview, then a new version, then the checks run again on it
  function fdPreview() {
    const c = readyChanges(); if (!c.total) return;
    const r = state.report, next = revisionOf(r) + 1;
    const rows = [...c.accepted.map(f => ({sheet: f.sheet, cell: f.cell, before: f.before, after: c.dec[f.id]?.after ?? f.after, why: f.reason, src: f.source})),
      ...Object.values(c.edits).map(e => ({sheet: e.sheet, cell: e.cell, before: e.before, after: e.after, why: e.reason || 'Your edit', src: 'Your edit'}))];
    const d = h('dialog', {class: 'fd-modal', 'aria-labelledby': 'fd-prev-title'},
      h('p', {class: 'fd-eyebrow'}, 'Check before applying'),
      h('h2', {id: 'fd-prev-title'}, plural(rows.length, 'change')),
      h('p', {}, `These cells change in a new version (v${next}). v${revisionOf(r)} stays exactly as it is, and the checks and the SharePoint comparison run again on v${next}.`),
      h('div', {class: 'fd-prevlist', tabindex: '0'}, h('table', {}, h('thead', {}, h('tr', {}, h('th', {}, 'Cell'), h('th', {}, 'Before'), h('th', {}, 'After'), h('th', {}, 'Why'))),
        h('tbody', {}, rows.map(x => h('tr', {}, h('td', {}, `${x.sheet}!${x.cell}`), h('td', {}, showWs(x.before)), h('td', {}, h('b', {}, showWs(x.after))), h('td', {}, `${x.src}: ${x.why || ''}`)))))),
      h('div', {class: 'fd-modalacts'},
        h('button', {type: 'button', class: 'btn', onclick: () => d.close()}, 'Back to review'),
        h('button', {type: 'button', class: 'btn primary', id: 'fd-apply', onclick: async e => {
          if (state.busy) return; e.currentTarget.disabled = true; e.currentTarget.textContent = `Saving v${next} and re-checking…`;
          const hash = await applyFixes(c.accepted, c.dec, c.edits, {download: false});
          d.close(); if (hash) toast(`v${next} saved and checked again. v${revisionOf(r)} is kept unchanged.`);
        }}, `Create v${next} and re-check`)));
    d.addEventListener('close', () => { d.remove(); $('fd-preview-btn')?.focus(); });
    document.body.append(d); d.showModal(); d.querySelector('#fd-apply').focus();
  }
  async function fdApprove() {
    const name = ($('fd-approver')?.value || '').trim(), btn = $('fd-approve-btn');
    if (!name) { toast('Type your name to approve the report.', true); $('fd-approver')?.focus(); return; }
    if (btn) { btn.disabled = true; btn.textContent = 'Approving…'; }
    try {
      store.set('rd-approver', name);
      const out = await api('/api/spreadsheets/approve', {file_hash: state.report.file_hash, approver: name, note: ''});
      toast('Internally approved. Nothing was sent; sending to the client is a separate step.');
      await openReport(out.item.file_hash, true); loadList();
    } catch (e) { toast(e.message, true); if (btn) { btn.disabled = false; btn.textContent = 'Approve internally'; } }
  }
  async function fdRecheck() {
    const name = myName() || 'Reviewer';
    toast('Re-checking with the current checks…');
    try { const out = await api('/api/spreadsheets/recheck', {file_hash: state.report.file_hash, apply: true, by: name});
      toast(out.reconciled ? 'Checked again, and SharePoint compared again.' : 'Checked again. Compare with SharePoint to verify it.');
      await openReport(state.report.file_hash, true); loadList();
    } catch (e) { toast(e.message, true); }
  }
  async function fdReconcile() {
    toast('Comparing every record with its SharePoint item…');
    try { await api('/api/spreadsheets/sharepoint', {file_hash: state.report.file_hash}); await openReport(state.report.file_hash, true); loadList();
      const sp = state.report.review.sharepoint || {}; toast(sp.status === 'match' ? 'Matches SharePoint item by item.' : sp.message || 'SharePoint compared.');
    } catch (e) { toast(e.message, true); }
  }

  // ---------- history and sources
  function fdHistory(r) {
    const rv = r.review, rows = [];
    const add = (title, text, link) => rows.push(h('article', {class: 'fd-hist'}, h('b', {}, title), h('span', {}, text), link || null));
    if (r.fixed_from) add(`v${r.fixed_from.revision || 1} · ${r.fixed_from.filename}`, 'The version these changes were made on. Kept unchanged.', h('a', {href: '#r=' + r.fixed_from.file_hash}, `Open v${r.fixed_from.revision || 1}`));
    add(`v${revisionOf(r)} · this version`, r.fixed_from ? `${plural(r.fixed_from.changes.length, 'change')} applied${r.fixed_from.applied_at ? ' ' + when2(r.fixed_from.applied_at) : ''}: ` + r.fixed_from.changes.slice(0, 8).map(c => `${c.sheet}!${c.cell} ${c.before || '(blank)'} → ${c.after || '(blank)'}`).join('; ') + (r.fixed_from.changes.length > 8 ? '…' : '') : `Uploaded ${when2(r.received_at) || ''}. Stored unchanged (SHA-256 ${r.file_hash.slice(0, 12)}…).`);
    for (const f of r.fixes_applied || []) add(`v${f.revision || '?'} · ${f.fixed_filename}`, `${plural(f.changes.length, 'change')} applied ${when2(f.applied_at) || ''}.`, h('a', {href: '#r=' + f.fixed_file_hash}, 'Open it'));
    for (const x of r.recheck_applied || []) add('Re-checked', `${x.by || 'Someone'} re-ran the checks ${when2(x.at) || ''} (from ${x.from_version || '?'} to ${x.to_version || '?'}).`);
    for (const x of (r.review_decision_log || []).slice(-30)) add({keep: 'Kept as recorded', reject: 'Suggestion rejected', note: 'Note', reopen: 'Decision undone'}[x.action] || x.action, `${x.by || 'A reviewer'} ${when2(x.at) || ''}${x.note ? ': “' + x.note + '”' : ''}`);
    if (r.approval) add(rv.approval_current ? 'Internally approved' : 'Approved on an earlier check run', `${r.approval.by} ${when2(r.approval.at) || ''}${r.approval.note ? ': ' + r.approval.note : ''}. Not sent.`);
    for (const s of (r.sent || []).filter(x => x.sent)) add('Sent to client', `${when2(s.at) || ''} to ${(s.to || []).join(', ')}`);
    return h('section', {class: 'fd-utility'}, h('h2', {}, 'Version history'), h('p', {}, 'Every version has its own file, its own checks and its own provenance. Originals are never changed.'), rows);
  }
  function fdSources(r) {
    const rec = r.source_reconciliation || {}, sp = r.review.sharepoint || {}, sel = r.dataset_selection || {}, cov = rec.coverage || {};
    const kv = (k, v) => v == null || v === '' ? null : [h('dt', {}, k), h('dd', {}, String(v))];
    return h('section', {class: 'fd-utility'}, h('h2', {}, 'Sources'),
      h('p', {}, 'Source coverage is shown separately from report correctness. A partial or conflicting read is never called verified.'),
      h('article', {class: 'fd-hist'}, h('b', {}, `SharePoint: ${({match: 'matches item by item', differences: 'differs', incomplete: 'incomplete, not verified', unverified: 'not verified', stale: 'out of date', day_level: 'day totals only'})[sp.status] || 'not compared'}`),
        h('dl', {class: 'fd-kv'}, kv('List', sp.source), kv('Snapshot', sp.snapshot_id), kv('Captured', when2(sp.captured_at)), kv('Records matched', sp.matched != null ? `${sp.matched} of ${r.review.scans}` : null),
          kv('Fields checked', (sp.fields_checked || []).join(', ')), kv('Fields not checked', (sp.fields_not_checked || []).join(', ')), kv('Pages read', cov.pages),
          kv('Denied / throttled / failed pages', [cov.denied, cov.throttled, (cov.failed_pages || []).length].some(Boolean) ? `${cov.denied || 0} / ${cov.throttled || 0} / ${(cov.failed_pages || []).length}` : null),
          kv('Undated records', cov.undated), kv('Why not verified', (rec.not_verified_because || []).join(' '))),
        sp.web_url ? h('a', {href: sp.web_url, target: '_blank', rel: 'noopener'}, 'Open the list (read only)') : null),
      h('article', {class: 'fd-hist'}, h('b', {}, 'Record set'), h('dl', {class: 'fd-kv'}, kv('Counted', (sel.authoritative || []).join(', ')), kv('Compared, not added', (sel.compared || []).join(', ')),
        kv('Status', sel.status), kv('Roles', Object.entries(sel.roles || {}).map(([k, v]) => `${k}: ${v}`).join(' · ')))),
      readBlock(r));
  }
  // switching sheets keeps each sheet's scroll position
  function fdKeepScroll() { const w = document.querySelector('#sheet-block .gridwrap'); if (w && state.grid) (state.sheetScroll ||= {})[state.grid.sheet] = [w.scrollTop, w.scrollLeft]; }
  // keyboard: j / k next and previous correction, s select, Escape closes drawers
  document.addEventListener('keydown', e => {
    if (!document.body.classList.contains('reportmode') || e.target.closest('input, textarea, select, [contenteditable]') || e.metaKey || e.ctrlKey || e.altKey) return;
    const flat = (state.fdItems || []).filter(x => x.tier === state.fdTier), at = flat.findIndex(x => x.key === state.fdCur);
    if (e.key === 'j') { fdOpen(flat[Math.min(flat.length - 1, at + 1)]); e.preventDefault(); }
    if (e.key === 'k') { fdOpen(flat[Math.max(0, at - 1)]); e.preventDefault(); }
  });
  const fdNarrow = matchMedia('(max-width: 900px)'); const syncNarrow = () => document.body.classList.toggle('fd-narrow', fdNarrow.matches);
  fdNarrow.addEventListener('change', syncNarrow); syncNarrow();

  // ---------------- right-hand panel: Fixes (issues grouped by severity and type, suggested fixes), Facts, Ask Claude
  const myName = () => store.get('rd-approver', '') || (state.me?.user && !state.me.user.virtual ? state.me.user.name || state.me.user.email || '' : '');
  const reviewedSet = () => new Set(store.get('rd-reviewed:' + state.report.file_hash, []));
  function showSide(tab) { if (state.sideTab !== tab) { state.sideTab = tab; drawSide(); } }
  function drawSideTabs() {
    const bar = $('sidetabs'); if (!bar || !state.report) return;
    const done = reviewedSet(), open = walkIssues().filter(i => !done.has(i.key)).length, tab = state.sideTab;
    const btn = (id, label, badge) => h('button', {type: 'button', role: 'tab', class: tab === id ? 'on' : '', 'aria-selected': String(tab === id),
      onclick: () => { if (id === 'claude') state.chatUnread = false; showSide(id); }}, label, badge ? h('i', {}, badge) : null);
    const c = fdCounts(), todo = (c.blockers || 0) + (c.review || 0);
    bar.replaceChildren(btn('fixes', 'Corrections', todo ? String(todo) : null), btn('facts', 'Facts'), btn('claude', 'Ask Claude', state.chatUnread ? 'new' : null));
    const mc = $('fd-mobile-count'); if (mc) mc.textContent = todo ? String(todo) : '';
    void open;
  }
  function drawSide() {
    const side = $('side'), r = state.report; if (!side || !r) return;
    const rv = r.review, tab = state.sideTab || 'fixes';
    const body = h('div', {class: 'sidebody', id: 'side-body'});
    side.replaceChildren(h('div', {class: 'sidetabs', id: 'sidetabs', role: 'tablist'}), body);
    drawSideTabs();
    if (tab === 'fixes') {
      fdCorrections(body);
      const sc = shiftCard(rv); if (sc) body.append(h('div', {id: 'shiftfix'}, sc));
    } else if (tab === 'facts') {
      body.append(h('div', {id: 'recs'}), numbers(r));
      if (r.built) body.append(builtBanner(r));
      body.append(sourcesBlock(r));
      if ((r.other_days || []).length) body.append(h('section', {class: 'block', id: 'otherdays'},
        h('header', {}, h('h2', {}, 'Already reported on another day'), h('p', {}, `${r.other_days.length >= 20 ? '20 or more' : r.other_days.length} VIN${r.other_days.length === 1 ? '' : 's'} in this report ${r.other_days.length === 1 ? 'is' : 'are'} also in another day's report for ${rv.job}. An old file sent again, or a car counted twice?`)),
        h('ul', {class: 'otherdays'}, Object.values(r.other_days.reduce((g, x) => { (g[x.file_hash] ||= {...x, vins: []}).vins.push(x.vin); return g; }, {}))
          .map(g => h('li', {}, h('b', {}, shortDay(g.day)), ' ', h('a', {href: '#r=' + g.file_hash}, g.filename), h('span', {}, g.vins.join(', ')))))));
      body.append(nexusBlock(r));
      const tl = timelineBlock(r); if (tl) body.append(tl);
      body.append(readBlock(r));
      if (rv.fixed_from || rv.fixes_applied || r.fixed_from || (r.fixes_applied || []).length) body.append(historyBlock(r));
      loadReportRecs(false);
    } else {
      body.append(h('div', {id: 'chat', class: 'chat embedded', 'aria-label': 'Ask Claude'}));
      drawChat();
      body.querySelector('textarea')?.focus();
    }
  }
  // the selected cell (and the issue being stepped through) at the top of the Fixes tab
  function drawCellSlot() {
    const slot = $('cellslot'); if (!slot || !state.report) return;
    const list = walkIssues(), idx = state.issueNav, issue = idx != null ? list[idx] : null;
    const kids = [];
    if (issue) {
      const done = reviewedSet();
      const gw = state.groupWalk && state.groupWalk.keys.has(issue.key) ? state.groupWalk : null;
      const nav = gw ? list.filter(i => gw.keys.has(i.key)) : list, pos = nav.indexOf(issue);
      const go = k => focusIssue(list.indexOf(nav[k]));
      const goByHand = k => { state.walkNote = null; go(k); };
      const markNext = () => { const done = reviewedSet(); done.add(issue.key); store.set('rd-reviewed:' + state.report.file_hash, [...done]);
        if (!nav.some(x => !done.has(x.key)) && readyChanges().total) state.celebrate = {hash: null, n: readyChanges().total, items: nav.length};   // celebrate once the new version is saved
        updateFinish(); drawSteps(); drawCleanOffer();
        const nextOpen = nav.findIndex((x, k) => k > pos && !done.has(x.key)), earlier = nav.findIndex(x => !done.has(x.key));
        const leftNow = nav.filter(x => !done.has(x.key)).length;
        if (leftNow === 1) toast('Nice! Just one more.'); else if (leftNow && leftNow === Math.ceil(nav.length / 2)) toast(`Halfway there: ${leftNow} left.`);
        const to = nextOpen >= 0 ? nextOpen : earlier;
        if (to >= 0) { const nx = nav[to], clip = t => (t || '').length > 70 ? t.slice(0, 67) + '…' : t || '';
          state.walkNote = `✓ Item ${pos + 1} is done. Now item ${to + 1} of ${nav.length}` + (issueType(nx) !== issueType(issue) ? `, a different kind of issue: ${issueType(nx)}.` : `: another one of the same kind (${issueType(nx)}).`); void clip;
          go(to); return; }
        state.walkNote = null;
        { state.issueNav = null; state.groupWalk = null; drawSide();
          const c = readyChanges(), msg = gw ? `Done! ${gw.label === 'Not sure' ? 'Every item you had to check' : `All the ${gw.label.toLowerCase()}`} handled.` : 'Done! That was the last open issue.';
          if (c.total) { toast(`${msg} Saving your ${c.total} change${c.total === 1 ? "" : "s"} into a new version and checking it again…`); applyFixes(c.accepted, c.dec, c.edits, {download: false}); }
          else toast(msg); } };
      // every open item with exactly the same issue: handled together unless the person asks to go one by one
      state.oneByOne ||= new Set(); state.bulkOff ||= new Set();
      const openNow = list.filter(x => !done.has(x.key)), kinds = [...new Set(openNow.map(sameKey))];
      const same = done.has(issue.key) || state.oneByOne.has(sameKey(issue)) ? [] : openNow.filter(x => sameKey(x) === sameKey(issue));
      const advance = async (handled, verb, fine) => {   // after a batch: say what was done and go to the next kind
        if (fine) await markFine(handled);
        const dn = reviewedSet(); handled.forEach(x => dn.add(x.key)); store.set('rd-reviewed:' + state.report.file_hash, [...dn]);
        updateFinish(); drawSteps(); drawCleanOffer(); drawSideTabs();
        const rest = list.filter(x => !dn.has(x.key)), nx = rest[0];
        if (nx) { const kindsLeft = new Set(rest.map(sameKey)).size, sameAgain = handled.length && sameKey(nx) === sameKey(handled[0]);
          state.walkNote = `✓ ${handled.length === 1 ? '1 item' : `${handled.length} items`} ${verb}. ` + (sameAgain
            ? `Now the rest of this same issue: ${rest.filter(x => sameKey(x) === sameKey(nx)).length} left that need a different answer.`
            : `Now the next kind of issue (${kindsLeft} kind${kindsLeft === 1 ? '' : 's'} left, ${rest.length} item${rest.length === 1 ? '' : 's'} in all): ${issueType(nx)}.`);
          state.groupWalk = null; focusIssue(list.indexOf(nx)); return; }
        state.walkNote = null; state.issueNav = null; state.groupWalk = null; drawSide();
        const c = readyChanges();
        if (c.total) { toast(`Done! That was the last kind of issue. Saving your ${c.total} change${c.total === 1 ? '' : 's'} into a new version and checking it again…`); applyFixes(c.accepted, c.dec, c.edits, {download: false}); }
        else toast('Done! That was the last kind of issue.');
      };
      let bulk = null;
      if (same.length > 1) {
        const ticked = same.filter(x => !state.bulkOff.has(x.key)), n = ticked.length;
        const withFix = ticked.filter(x => fixesFor(x).some(f => f.state !== 'rejected')), dups = ticked.filter(x => isDupRow(x) && x.row && !fixesFor(x).length);
        const acceptAll = () => {
          const d = store.get(decisionsKey(), {});
          for (const x of withFix) for (const f of fixesFor(x).filter(f => f.state !== 'rejected')) d[f.id] = {state: 'accepted', after: d[f.id]?.after ?? f.after};
          store.set(decisionsKey(), d); drawFixes();
          const handled = [...withFix, ...dups];
          if (dups.length) removeRows(dups.map(x => [x.sheet, Number(x.row)]));
          toast(`${handled.length} fixed at once. ${ticked.length - handled.length ? `${ticked.length - handled.length} had no suggested change, so they stay open.` : ''}`);
          advance(handled, 'fixed');
        };
        const where = x => x.sheet ? `${x.sheet}${x.cell ? ' ' + x.cell : x.row ? ' row ' + x.row : ''}` : 'Whole report';
        const fixText = x => { const f = fixesFor(x).find(f => f.state !== 'rejected'); return f ? ` → ${showWs(f.after)}` : ''; };
        bulk = h('div', {class: 'bulkbox'},
          h('p', {class: 'bulk-head'}, h('b', {}, `This exact issue is on ${same.length} rows.`), ` Kind ${kinds.indexOf(sameKey(issue)) + 1} of ${kinds.length}. Handle them all at once, or untick any that are different:`),
          h('details', {}, h('summary', {}, `${n} of ${same.length} ticked: see them`),
            h('ul', {class: 'bulk-list'}, same.map(x => h('li', {},
              h('label', {}, h('input', {type: 'checkbox', checked: !state.bulkOff.has(x.key), onchange: e => { e.target.checked ? state.bulkOff.delete(x.key) : state.bulkOff.add(x.key); drawCellSlot(); }}),
                ' ', h('span', {}, where(x), h('code', {}, fixText(x)))),
              h('button', {type: 'button', class: 'linkish small', onclick: () => { state.walkNote = null; focusIssue(list.indexOf(x)); }}, 'show'))))),
          h('div', {class: 'bulk-btns'},
            withFix.length + dups.length ? h('button', {type: 'button', class: 'btn primary small', onclick: acceptAll},
              withFix.length + dups.length === n ? `Make the suggested change on ${n === same.length ? 'all ' : ''}${n}` : `Fix the ${withFix.length + dups.length} that have a suggested change`) : null,
            state.aiOn ? h('button', {type: 'button', class: 'btn small', onclick: e => { const k = ticked.map(x => x.key); aiFix(k, e.target); }}, `Let Claude fix ${n === same.length ? 'all ' : ''}${n}`) : null,
            h('button', {type: 'button', class: 'btn small', onclick: () => { advance(ticked, 'marked fine as is', true); }}, `${n === same.length ? 'All' : 'The'} ${n} are fine as is`),
            h('button', {type: 'button', class: 'linkish small', onclick: () => { state.oneByOne.add(sameKey(issue)); drawCellSlot(); }}, 'Go through them one by one')));
      }
      kids.push(h('div', {class: 'issuenav sev-' + issue.severity},
        state.walkNote ? h('p', {class: 'walk-note', role: 'status'}, state.walkNote) : null,
        bulk,
        h('div', {class: 'in-top'}, h('span', {class: 'sev ' + issue.severity}), h('b', {}, bulk ? `For example, ${issue.where ? issue.where.replace(/^.*·\s*/, '') : 'this one'}:` : gw ? `${pos + 1} of ${nav.length} in ${gw.label}` : `Now fixing item ${pos + 1} of ${nav.length}`), gw ? null : h('span', {class: 'in-type'}, issueType(issue)),
          h('button', {type: 'button', class: 'btn ghost small', onclick: () => { state.issueNav = null; state.groupWalk = null; state.walkNote = null; state.gridCell = null; drawGrid(); drawSide(); drawCleanOffer(); }}, 'Close')),
        issue.where ? h('p', {class: 'in-where'}, `Where: ${issue.where}`) : null,
        h('p', {class: 'in-text'}, issue.text),
        issue.only ? h('p', {class: 'only-note big'}, issue.only) : null,
        issue.sure === false ? h('p', {class: 'in-unsure'}, 'The app is not sure about this one, so a person has to look at it before the report can be approved.') : null,
        issue.why ? h('p', {class: 'in-why'}, h('b', {}, 'Why it matters: '), issue.why) : null,
        issue.fix && issue.fix !== issue.check ? h('p', {class: 'in-why'}, h('b', {}, 'What to do: '), issue.fix) : null,
        issue.check ? h('p', {class: 'hint'}, 'Check: ' + issue.check) : null,
        fixChoices(issue, markNext),
        h('p', {class: 'keys-hint'}, 'Keys: A make the change · S skip · F fine as is · ← → move'),
        issue.verdict_reason ? h('p', {class: 'hint'}, 'Claude: ' + issue.verdict_reason) : null,
        !issue.sheet && !issue.row ? h('p', {class: 'hint'}, 'This one is about the report as a whole, not one cell.') : null,
        h('div', {class: 'in-nav'},
          h('button', {type: 'button', class: 'btn small', disabled: pos === 0, onclick: () => goByHand(pos - 1)}, '‹ Previous'),
          h('button', {type: 'button', class: 'btn small', disabled: pos >= nav.length - 1, onclick: () => goByHand(pos + 1)}, 'Skip for now ›'),
          h('button', {type: 'button', class: 'btn primary small', onclick: markNext}, done.has(issue.key) ? 'Already done · go to the next' : 'Done with this one ›'))));
    }
    if (state.grid && state.gridCell) { const p = cellPanel(marks(), !!issue); if (p) kids.push(p); }
    slot.replaceChildren(...kids);
  }
  async function focusIssue(index) {
    const list = walkIssues(); if (!list.length || !state.report) return;
    index = Math.max(0, Math.min(index, list.length - 1));
    const i = list[index];
    if (state.groupWalk && !state.groupWalk.keys.has(i.key)) state.groupWalk = null;
    state.issueNav = index; state.sideTab = 'fixes';
    const sheet = i.sheet || state.gridSheet;
    const cell = i.cell && splitCell(i.cell) ? String(i.cell).toUpperCase() : i.row ? `A${Number(i.row)}` : null;
    if (sheet && (!state.grid || state.grid.sheet !== sheet)) await loadGrid(sheet, true);
    state.gridCell = cell;
    drawSide(); drawGrid(); drawCleanOffer();
    const td = cell && document.querySelector(`#sheet-block td[data-cell="${cell}"]`), wrap = document.querySelector('#sheet-block .gridwrap');
    if (td && wrap) reveal(wrap, td, true);
  }

  async function showCell(sheet, cell) {
    if (!state.report || !sheet) return;
    if (!state.grid || state.grid.sheet !== sheet) await loadGrid(sheet, true);
    state.gridCell = String(cell || '').toUpperCase() || null; drawGrid();
    const td = state.gridCell && document.querySelector(`#sheet-block td[data-cell="${state.gridCell}"]`), wrap = document.querySelector('#sheet-block .gridwrap');
    if (td && wrap) reveal(wrap, td, true);
    $('sheet-block')?.scrollIntoView({behavior: 'smooth', block: 'nearest'});
  }

  function numbers(r) {
    const rv = r.review, bl = r.baseline || {}, usual = bl.usual || {}, flagged = new Set((bl.findings || []).map(f => f.field));
    const perRow = rv.rejected_unit === 'rows', base = perRow ? rv.scans : rv.inspected;
    const rate = base && rv.rejected != null && !rv.rejected_partial ? rv.rejected / base * 100 : null;
    const cell = (value, label, u, key) => h('div', {}, h('b', {}, value), h('span', {}, label),
      u ? h('em', {class: flagged.has(key) ? 'off' : ''}, `Usual ${u.median}`) : null);
    return h('div', {class: 'numbers'},
      cell(fmt(rv.scans), 'Scans / rows', usual.scans, 'scans'),
      cell(fmt(rv.inspected), 'Inspected', usual.inspected, 'inspected'),
      cell(rv.rejected == null ? 'Not recorded' : fmt(rv.rejected), perRow ? 'Failed audits' : rv.rejected_partial ? 'Rejected (recorded on some rows only; blanks are not zero)' : 'Rejected', usual.rejected, 'rejected'),
      cell(rate == null ? 'Unknown' : rate.toFixed(1) + '%', perRow ? 'Fail rate' : rate == null ? 'Reject rate (rejects not recorded on every record)' : 'Reject rate', usual.reject_rate, 'reject_rate'));
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
    // Opening a report never runs Claude by itself: its read, its double-check (which can set issues aside) and its draft
    // fixes all change what the report shows, so each is a person's choice (Ask Claude tab, or Facts → AI review).
    void open; maybeBrief(hash, false);
    fdMaybeAutoFix(hash);
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

  // ---------------- "Want me to clean this data?": every change listed in plain words before anything happens
  // a change that only alters how a value is written (capitals, spaces, hidden characters, number stored as text)
  function fmtKind(f) {
    const a = String(f.before ?? ''), b = String(f.after ?? '');
    if (a === b) return null;
    if (a.toUpperCase() === b.toUpperCase()) return 'capital letters';
    const vis = x => x.replace(/[\x00-\x1f\x7f-\x9f\xa0\u200b-\u200f\ufeff]/g, '');
    if (vis(a) !== a && vis(a) === vis(b)) return 'hidden characters';
    if (a.trim() === b.trim() || a.replace(/\s+/g, ' ').trim() === b.replace(/\s+/g, ' ').trim()) return 'extra spaces';
    if (vis(a).replace(/\s+/g, ' ').trim().toUpperCase() === vis(b).replace(/\s+/g, ' ').trim().toUpperCase()) return 'spaces and capitals';
    if (f.source === 'Number as text' || (/^\s*'?-?[\d,]+(\.\d+)?\s*$/.test(a) && Number(a.replace(/[',\s]/g, '')) === Number(b))) return 'number stored as text';
    return null;
  }
  const showWs = v => v === '' || v == null ? '(blank)' : String(v).replace(/^ +| +$/g, m => '\u00b7'.repeat(m.length)).replace(/[\xa0\u200b-\u200f\ufeff]/g, '\u25e6');
  // Claude already looked at this one and was not sure: it is for a person now (Fix myself or Ignore), not for another AI pass
  function claudeLeft(i) {
    const left = state.report?.ai_fixed?.left || [];
    return left.some(x => x.text === i.text && (x.cell && i.cell ? String(x.cell).toUpperCase() === String(i.cell).toUpperCase() && (x.sheet || null) === (i.sheet || null)
      : x.row == null || (String(x.row) === String(i.row) && (x.sheet || null) === (i.sheet || null))));
  }
  // a short plural name for a kind of issue: "122 blank cells", "12 part numbers in a different format"
  function kindNames(x) {
    const t = x.text || '', c = x.code || '';
    if (/different format from the other/i.test(t)) return /^Identifier/.test(t) ? ['identifier in a different format', 'identifiers in a different format'] : ['part number in a different format', 'part numbers in a different format'];
    if (/far (above|below) the usual/i.test(t)) return ['number far from the usual (possible typo)', 'numbers far from the usual (possible typos)'];
    if (/^missing cell/i.test(t) || /missing_cell|blank_cell/.test(c)) { const col = (String(x.cell || '').match(/^[A-Z]+/i) || [''])[0].toUpperCase();
      return col ? [`blank cell in column ${col}`, `blank cells in column ${col}`] : ['blank cell', 'blank cells']; }
    if (/sharepoint|differs_from_sharepoint/i.test(c) || /on SharePoint/i.test(t)) {
      const m = t.match(/^(?:SharePoint item \S+: )?([A-Za-z][A-Za-z /]{1,30}?) (?:is|was|has)\b/), fld = m ? m[1].trim().toLowerCase() : (t.match(/: (\w+) is .* in the report/) || [])[1];
      if (/missing_from_report|not in the report/i.test(c + t)) return ['record on SharePoint that is missing from the report', 'records on SharePoint that are missing from the report'];
      if (/not_on_sharepoint|not on SharePoint/i.test(c + t)) return ['record that is not on SharePoint', 'records that are not on SharePoint'];
      return fld && fld !== 'sharepoint item' && fld !== 'this row' ? [`${fld} value that differs from SharePoint`, `${fld} values that differ from SharePoint`] : ['value that differs from SharePoint', 'values that differ from SharePoint']; }
    if (/duplicate/.test(c)) return ['duplicate', 'duplicates'];
    if (/wrong_shift/.test(c)) return ['scan on the wrong shift', 'scans on the wrong shift'];
    if (/vin_/.test(c)) return ['VIN problem', 'VIN problems'];
    if (/typo/.test(c)) return ['possible typo', 'possible typos'];
    if (/other_day/.test(c)) return ['row already reported on another day', 'rows already reported on another day'];
    const gen = t.replace(/"[^"]*"|“[^”]*”/g, '…').replace(/\b[A-Za-z0-9-]*\d[A-Za-z0-9-]*\b/g, '…').replace(/\s*\([^)]*\)/g, '').replace(/\.$/, '').trim();
    return [gen.length > 80 ? gen.slice(0, 77) + '…' : gen || issueType(x)];
  }
  function drawCleanOffer() {   // the step-by-step guide: the one place where changes are accepted, one step at a time
    const slot = $('cleanoffer'); if (!slot || !state.report) return;
    const r = state.report, hash = r.file_hash;
    if (!state.fixes || r.approval && (r.sent || []).some(x => x.sent) || state.busy) { slot.replaceChildren(); return; }
    const dec = store.get(decisionsKey(), {}), edits = store.get(editsKey(), {});
    const undecided = (state.fixes || []).filter(f => !edits[editId(f.sheet, f.cell)] && !dec[f.id]);
    const pending = undecided.filter(f => isSureFix(f) && f.source !== 'Suggested');
    const revd = reviewedSet(), open = walkIssues().filter(i => !revd.has(i.key));
    const dupIssues = open.filter(i => isDupRow(i) && i.row && !fixesFor(i).length);
    const sugg = undecided.filter(f => f.source === 'Suggested');
    const skipKey = 'rd-guide-skip:' + hash, skipped = store.get(skipKey, []);
    const skip = id => { store.set(skipKey, [...skipped, id]); drawCleanOffer(); };
    const unskip = id => { store.set(skipKey, skipped.filter(x => x !== id)); drawCleanOffer(); };
    const show = v => v === '' || v == null ? '(blank)' : `“${v}”`;
    const plural = (n, w) => `${fmt(n)} ${w}${n === 1 ? '' : 's'}`;
    const fmtFixes = pending.filter(f => fmtKind(f)), onlyFmt = fmtFixes.length === pending.length && !dupIssues.length;
    const c = readyChanges(), walking = state.issueNav != null;
    const steps = [];
    const auto = (r.auto_formatting?.count || 0) + ((r.ai_fixed?.changed || []).length + (r.ai_fixed?.removed || []).length + (r.ai_fixed?.moved || []).length);
    if (auto) steps.push({id: 'auto', title: `Fixed automatically: ${plural(auto, 'change')}`, done: true});
    const mine = (r.fixed_from?.changes || []).filter(x => !/automatic cleanup|AI fix|AI\b/.test(x.source || ''));
    if (mine.length) steps.push({id: 'saved', title: `Saved: ${plural(mine.length, 'change')}`, done: true});
    // ---- one summary of every proposed change: accept them all (Claude takes whatever has no proposal), or review by issue type
    const openIssues = open;
    const isFix = f => f.state !== 'rejected';
    const groupsMap = new Map();
    for (const i of openIssues) { const k = sameKey(i); if (!groupsMap.has(k)) groupsMap.set(k, []); groupsMap.get(k).push(i); }
    const covered = new Set();
    const groups = [...groupsMap.values()].map(items => {
      const withFix = items.filter(x => fixesFor(x).some(isFix)), dups = items.filter(x => isDupRow(x) && x.row && !fixesFor(x).length);
      withFix.forEach(x => fixesFor(x).filter(isFix).forEach(f => covered.add(f.id)));
      const none = items.filter(x => !withFix.includes(x) && !dups.includes(x));
      const ex = withFix[0] && fixesFor(withFix[0]).find(isFix);
      const plan = dups.length === items.length ? `Take out the extra ${dups.length === 1 ? 'copy' : 'copies'}`
        : withFix.length === items.length ? `Change to the suggested value` + (ex ? `, e.g. ${show(ex.before)} → ${show(ex.after)}` : '')
        : withFix.length || dups.length ? `${withFix.length + dups.length} get the suggested change; ${none.length} ${state.aiOn ? 'Claude fixes' : 'need you'}`
        : state.aiOn ? 'Claude fixes ' + (items.length === 1 ? 'it' : 'them') : 'No change proposed: needs you';
      return {items, withFix, dups, none, plan, label: issueType(items[0]), text: items[0].text};
    });
    const seenFix = new Set();   // two flags on the same cell are settled by one change: say so instead of listing it twice
    for (const g of groups) {
      const ids = g.withFix.flatMap(x => fixesFor(x).filter(isFix).map(f => f.id));
      if (ids.length && g.withFix.length === g.items.length && ids.every(id => seenFix.has(id))) g.plan = 'Settled by the same change as above (one change fixes both flags)';
      ids.forEach(id => seenFix.add(id));
    }
    const loose = undecided.filter(f => isFix(f) && !covered.has(f.id) && f.source !== 'AI fix');
    const fmtLoose = loose.filter(f => fmtKind(f)), suggLoose = loose.filter(f => f.source === 'Suggested'), otherLoose = loose.filter(f => !fmtKind(f) && f.source !== 'Suggested' && isSureFix(f));
    const fixRows = [
      ...(() => { const by = new Map(); for (const f of fmtLoose) { const k = fmtKind(f); by.set(k, [...(by.get(k) || []), f]); }
        return [...by.entries()].map(([k, list]) => ({id: 'fmt-' + k, fmt: k, label: 'Formatting: ' + k, count: list.length, plan: 'Only how the value is typed: the value itself stays the same', fixes: list})); })(),
      suggLoose.length ? {id: 'sugg', label: 'Blank details', count: suggLoose.length, plan: 'Fill in the likely value', fixes: suggLoose} : null,
      otherLoose.length ? {id: 'other', label: 'Other sure corrections', count: otherLoose.length, plan: 'Change to the corrected value', fixes: otherLoose} : null,
    ].filter(Boolean);
    groups.forEach(g => { g.id = 'g:' + sameKey(g.items[0]); }); fixRows.forEach(r => { r.id = 'f:' + r.id; });
    // ---- every kind of issue on its own card: how many, the first one, and Approve / Fix with AI / Fix myself / Ignore for all of
    // that kind. "Review all" opens the full list, where single items can be unticked. The summary on top accepts every fix at once.
    state.planOff ||= {}; state.planOpen ||= new Set(); state.oneByOne ||= new Set();
    const off = (state.planOff[hash] ||= new Set());
    const kOf = x => x.key || x.id, isOn = x => !off.has(kOf(x));
    const idOf = i => `${i.code}|${i.text}|${i.sheet}|${i.row ?? ''}|${i.cell || ''}`;
    const where = x => x.sheet ? `${x.sheet}${x.cell ? ' ' + x.cell : x.row ? ' row ' + x.row : ''}` : 'Whole report';
    const goTo = x => { state.walkNote = null; focusIssue(walkIssues().indexOf(x)); document.querySelector('.workspace')?.scrollIntoView({behavior: 'smooth', block: 'start'}); };
    const FMT_NAMES = {'capital letters': ['capitalization error', 'capitalization errors'], 'extra spaces': ['value with extra spaces', 'values with extra spaces'],
      'hidden characters': ['value with hidden characters', 'values with hidden characters'], 'spaces and capitals': ['spacing and capitalization error', 'spacing and capitalization errors'],
      'number stored as text': ['number stored as text', 'numbers stored as text']};
    const kinds = [
      ...fixRows.map(r => { const names = r.fmt ? FMT_NAMES[r.fmt] : r.id === 'f:sugg' ? ['blank detail with a likely value', 'blank details with a likely value'] : ['sure correction', 'sure corrections'];
        return {id: r.id, entries: r.fixes, names, row: r, hasSugg: true, ai: false, sev: 'low'}; }),
      ...groups.map(g => ({id: g.id, entries: g.items, names: kindNames(g.items[0]), group: g, hasSugg: g.withFix.length + g.dups.length > 0, ai: state.aiOn,
        unsure: state.aiOn && g.items.every(x => claudeLeft(x) && !fixesFor(x).some(isFix)),
        sev: g.items.some(x => x.severity === 'high') ? 'high' : g.items.some(x => x.severity === 'medium') ? 'medium' : 'low'})),
    ].sort((x, y) => ({high: 0, medium: 1, low: 2}[x.sev] - {high: 0, medium: 1, low: 2}[y.sev]) || y.entries.length - x.entries.length);
    const titleOf = (k, n = k.entries.length) => k.names.length === 2 ? `${fmt(n)} ${n === 1 ? k.names[0] : k.names[1]}` : `${fmt(n)} × ${k.names[0]}`;
    const partsOf = (list, opt = {}) => {   // the ticked items of these kinds, sorted by what can be done with them
      const p = {fixes: new Map(), issues: [], dups: [], none: [], n: 0};
      for (const k of list) {
        if (k.row) for (const f of k.row.fixes.filter(isOn)) { p.fixes.set(f.id, f); p.n++; }
        if (k.group) for (const x of k.group.items.filter(isOn)) { p.issues.push(x); p.n++;
          const fx = fixesFor(x).filter(isFix);
          if (fx.length) fx.forEach(f => p.fixes.set(f.id, f)); else if (isDupRow(x) && x.row) p.dups.push(x); else if (opt.includeLeft || !claudeLeft(x)) p.none.push(x); }
      }
      p.fixes = [...p.fixes.values()];
      return p;
    };
    const approve = async (p, btn, quiet) => {   // suggested changes, extra copies out, and (with AI on) Claude takes whatever had no suggestion
      if (state.busy) return quiet ? hash : undefined;
      if (!p.fixes.length && !p.dups.length && !(state.aiOn && p.none.length)) return quiet ? hash : toast('Nothing ticked has a suggested change. Use Fix with AI or Fix myself.', true);
      if (btn) { btn.disabled = true; btn.textContent = 'Fixing…'; }
      const d = store.get(decisionsKey(), {}), dn = reviewedSet();
      for (const f of p.fixes) d[f.id] = {state: 'accepted', after: d[f.id]?.after ?? f.after};
      p.issues.filter(x => fixesFor(x).some(isFix) || (isDupRow(x) && x.row)).forEach(x => dn.add(x.key));   // only what this click actually settles
      store.set(decisionsKey(), d); store.set('rd-reviewed:' + hash, [...dn]);
      let cur = hash;
      const k = readyChanges();
      if (k.total) { cur = await applyFixes(k.accepted, k.dec, k.edits, {download: false, thenOpen: false}); if (!cur) { if (btn) btn.disabled = false; return quiet ? null : undefined; } }
      if (p.dups.length) { try { cur = (await api('/api/spreadsheets/remove-rows', {file_hash: cur, rows: p.dups.map(x => ({sheet: x.sheet, row: Number(x.row)}))})).item.file_hash; } catch (e) { toast(e.message, true); } }
      if (state.aiOn && p.none.length) { const want = new Set(p.none.map(idOf)); cur = await autoFixAll(cur, i => want.has(idOf(i))); }
      if (quiet) return cur;
      await loadList(); select(cur); showResult(cur, true);
    };
    const aiFix = async (p, btn) => {   // Claude decides each ticked item (any number: it works in batches)
      if (state.busy || !p.issues.length) return;
      if (btn) { btn.disabled = true; btn.textContent = `Claude is fixing ${fmt(p.issues.length)}…`; }
      const want = new Set(p.issues.map(idOf));
      const cur = await autoFixAll(hash, i => want.has(idOf(i)));
      await loadList();
      if (cur !== hash) select(cur); else await openReport(cur);
      showResult(cur, true);
    };
    const ignore = async (p, k) => {   // fine as is: saved on the report, so later versions keep them settled
      const d = store.get(decisionsKey(), {}); for (const f of p.fixes) d[f.id] = {state: 'rejected'}; store.set(decisionsKey(), d);
      if (p.issues.length) await markFine(p.issues);
      const dn = reviewedSet(); p.issues.forEach(x => dn.add(x.key)); store.set('rd-reviewed:' + hash, [...dn]);
      toast(`${k ? titleOf(k, p.n) : plural(p.n, 'item')} ignored: left as they are. Nothing in the report was changed.`);
      drawFixes(); updateFinish(); drawSideTabs();
    };
    const fixMyself = k => {
      if (k.group) { state.oneByOne.delete(sameKey(k.group.items[0])); state.groupWalk = null;
        state.walkNote = `Going through ${k.entries.length === 1 ? 'this issue' : `the ${fmt(k.entries.length)} ${k.names[1] || 'like this'}`} one at a time. Type the right value, then move on.`;
        goTo(k.group.items.find(isOn) || k.group.items[0]); }
      else { state.planOpen.add(k.id); drawCleanOffer(); const f = k.entries.find(isOn) || k.entries[0]; showCell(f.sheet, f.cell); toast('Click a cell in the list to go to it, then type the right value.'); }
    };
    const changeOf = (f, x) => f ? ['Now ', show(f.before), ' → becomes ', h('b', {}, showWs(f.after))] : x && isDupRow(x) ? 'Fix: take out the extra copy'
      : state.aiOn ? 'Fix with AI lets Claude work it out, or fix it yourself' : 'No suggested change: fix it yourself, or Ignore';
    const firstOf = k => { if (k.row) { const f = k.entries[0]; return {where: `${f.sheet} ${f.cell}`, what: f.reason || '', change: changeOf(f), go: () => showCell(f.sheet, f.cell)}; }
      const x = k.entries[0]; return {where: where(x), what: x.text, change: changeOf(fixesFor(x).find(isFix), x), go: () => goTo(x)}; };
    const card = k => {
      const n = k.entries.length, on = k.entries.filter(isOn).length, p = partsOf([k]), isOpen = state.planOpen.has(k.id), some = on < n;
      const cnt = some ? ` ${fmt(on)}` : n > 1 ? ' all' : '';
      const ex = firstOf(k);
      const box = e => h('input', {type: 'checkbox', checked: isOn(e), 'aria-label': `Include ${k.row ? `${e.sheet} ${e.cell}` : where(e)}`, onchange: ev => { ev.target.checked ? off.delete(kOf(e)) : off.add(kOf(e)); drawCleanOffer(); }});
      const listItem = e => { const f = k.row ? e : fixesFor(e).find(isFix);
        return h('li', {class: isOn(e) ? '' : 'out'}, h('label', {}, box(e), h('span', {class: 'pi-text'}, h('span', {class: 'pi-where'}, k.row ? `${e.sheet} ${e.cell}` : where(e)),
          h('span', {class: 'pi-change'}, f ? [show(f.before), ' → ', h('b', {}, showWs(f.after))] : !k.row && isDupRow(e) ? 'Extra copy: take it out' : h('em', {}, 'No suggested change')))),
          k.group && state.aiOn ? h('button', {type: 'button', class: 'linkish small', onclick: ev => aiFix({issues: [e], fixes: [], dups: [], none: [e], n: 1}, ev.target)}, 'Fix with AI') : null,
          h('button', {type: 'button', class: 'linkish small', onclick: () => k.row ? showCell(e.sheet, e.cell) : goTo(e)}, 'Show')); };
      return h('section', {class: `kind sev-${k.sev}`, id: 'kind-' + k.id.replace(/[^\w-]/g, '_')},
        h('div', {class: 'kind-head'}, h('h3', {}, `Review ${titleOf(k)}?`), k.sev === 'high' ? h('span', {class: 'kind-pill'}, 'Must fix') : null),
        h('div', {class: 'kind-ex'}, h('span', {class: 'kind-ex-k'}, n > 1 ? 'The first one' : 'The issue'),
          h('button', {type: 'button', class: 'linkish small kind-ex-where', onclick: ex.go}, ex.where), ex.what ? h('span', {class: 'kind-ex-what'}, ex.what) : null, h('span', {class: 'kind-ex-change'}, ex.change)),
        h('div', {class: 'kind-btns'},
          k.hasSugg ? h('button', {type: 'button', class: 'btn primary', disabled: !on, onclick: e => { const b = e.target; previewAll({...p, none: []}, () => approve(p, b)); }}, n > 1 ? `Change${cnt}` : 'Change it') : null,
          k.ai ? h('button', {type: 'button', class: 'btn ai', disabled: !on, onclick: e => aiFix(k.unsure ? partsOf([k], {includeLeft: true}) : p, e.target)}, n > 1 ? `Fix${cnt} with AI` : 'Fix with AI') : null,
          h('button', {type: 'button', class: 'btn', disabled: !on, onclick: () => fixMyself(k)}, 'Fix myself'),
          h('button', {type: 'button', class: 'btn ghost', disabled: !on, onclick: () => ignore(p, k)}, `Ignore${cnt}`)),
        some ? h('p', {class: 'kind-note'}, `${fmt(n - on)} unticked ${n - on === 1 ? 'is' : 'are'} left out of these buttons.`) : null,
        n > 1 ? h('button', {type: 'button', class: 'kind-more', 'aria-expanded': String(isOpen), onclick: () => { isOpen ? state.planOpen.delete(k.id) : state.planOpen.add(k.id); drawCleanOffer(); }},
          isOpen ? 'Hide the list ▴' : `See all ${fmt(n)} ▾`) : null,
        isOpen ? h('div', {class: 'kind-listwrap'},
          h('label', {class: 'kind-all'}, (() => { const a = h('input', {type: 'checkbox', checked: on === n, onchange: e => { k.entries.forEach(x => e.target.checked ? off.delete(kOf(x)) : off.add(kOf(x))); drawCleanOffer(); }}); a.indeterminate = on > 0 && on < n; return a; })(),
            h('span', {}, on === n ? `All ${fmt(n)} included` : `${fmt(on)} of ${fmt(n)} included`)),
          h('ul', {class: 'kind-list'}, k.entries.slice(0, 500).map(listItem)),
          n > 500 ? h('p', {class: 'kind-note'}, `Showing the first 500 of ${fmt(n)}. The buttons cover all of them.`) : null) : null);
    };
    if (kinds.length) {
      const all = partsOf(kinds), total = kinds.reduce((t, k) => t + k.entries.length, 0);
      const acceptAll = btn => previewAll(partsOf(kinds, {includeLeft: true}), () => { if (btn) { btn.disabled = true; btn.textContent = 'Fixing everything…'; } return fixEverythingLoop(); });
      const unsureAny = kinds.some(k => k.unsure);
      state.fixEverything = {hash, parts: opt => partsOf(kinds, opt), approve, run: () => acceptAll(document.querySelector('#cleanoffer .sum-accept'))};
      const canAll = all.fixes.length || all.dups.length || (state.aiOn && (all.none.length || unsureAny));
      const unsureN = kinds.filter(k => k.unsure).reduce((t, k) => t + k.entries.length, 0);
      const summary = h('div', {class: 'sum'},
        h('h2', {id: 'clean-title', class: 'sum-title'}, 'Issue summary'),
        h('p', {class: 'sum-line'}, `${plural(total, 'issue')} in ${plural(kinds.length, 'kind')}.` + (r.fixed_from ? ' Nothing below has been changed yet.' : ' Nothing has been changed yet.')),
        h('ul', {class: 'sum-list'}, kinds.map(k => h('li', {}, h('span', {class: 'sev ' + k.sev}),
          h('button', {type: 'button', class: 'linkish', onclick: () => document.getElementById('kind-' + k.id.replace(/[^\w-]/g, '_'))?.scrollIntoView({behavior: 'smooth', block: 'start'})}, titleOf(k))))),
        canAll ? h('button', {type: 'button', class: 'btn primary sum-accept', onclick: e => acceptAll(e.target)}, state.aiOn ? 'Fix everything with AI' : 'Accept all fixes') : null,
        h('p', {class: 'sum-note'}, canAll ? (state.aiOn ? 'One click: every suggested change is made and Claude fixes the rest. ' : 'Makes every suggested change. ') + 'Or review each kind below.' : 'Review each kind below.'));
      steps.push({id: 'review', title: `${plural(total, 'issue')} to review`, panel: h('div', {class: 'plan2'}, summary, h('div', {class: 'kinds'}, kinds.map(card)))});
    }
    const walkOpen = openIssues;
    const wentThrough = walkIssues().filter(i => revd.has(i.key)).length;
    if (!walkOpen.length && wentThrough) steps.push({id: 'walked', title: `Went through ${plural(wentThrough, 'item')}`, done: true});
    if (c.total) steps.push({id: 'save', title: `Save your ${plural(c.total, 'change')}`, say: 'They go into a new version, which is checked again. Your original stays as it is.',
      go: ['Save the changes', () => { toast('Saving your changes into a new version…'); applyFixes(c.accepted, c.dec, c.edits, {download: false}); }]});
    if (!(r.sent || []).some(x => x.sent)) steps.push({id: 'send', title: r.approval ? 'Send it to the client' : 'Approve it', say: r.approval ? 'Approved. Sending is a separate step: the email is ready to review.' : 'Everything is handled. Check the totals, then approve.',
      go: [r.approval ? 'Send to client…' : 'Approve…', sendFinal]});
    const live = steps.filter(s => !s.done && !(s.skippable && skipped.includes(s.id)));
    const current = live[0];
    const numbered = steps;
    const no = s => numbered.indexOf(s) + 1;
    // a fixed version can be downloaded right here, the moment the changes are saved
    const dlFixed = r.fixed_from ? h('a', {class: 'btn dl-fixed', href: '/api/spreadsheets/download?id=' + hash, download: r.filename}, '⬇ Download the fixed version') : null;
    if (current?.panel) {
      const doneLine = steps.filter(s => s.done).map(s => s.title).join(' · ');
      slot.replaceChildren(h('section', {class: 'cleanoffer guide plan2wrap', 'aria-labelledby': 'clean-title'},
        doneLine ? h('p', {class: 'plan2-done'}, '✓ ', doneLine) : null, dlFixed, current.panel));
      return;
    }
    slot.replaceChildren(h('section', {class: 'cleanoffer guide', 'aria-labelledby': 'clean-title'}, dlFixed,
      h('ol', {class: 'guide-steps'}, steps.map(s => h('li', {class: s.done ? 'done' : s === current ? 'now' : skipped.includes(s.id) && s.skippable ? 'skipped' : 'later'},
        h('span', {class: 'gs-dot', 'aria-hidden': 'true'}, s.done ? '✓' : String(no(s))),
        h('span', {}, s.done ? s.title : s.title.replace(/:.*/, '')),
        skipped.includes(s.id) && s.skippable ? h('button', {type: 'button', class: 'linkish small', onclick: () => unskip(s.id)}, 'skipped · go back') : null))),
      current ? h('div', {class: 'guide-now'},
        h('div', {class: 'cleanhead cleanhead-yes'}, h('div', {},
          h('p', {class: 'guide-kicker'}, `Step ${no(current)} of ${numbered.length}`),
          h('h2', {id: 'clean-title'}, current.title),
          current.say ? h('p', {}, current.say) : null),
          h('div', {class: 'btns'}, h('button', {type: 'button', class: 'btn primary', onclick: current.go[1]}, current.go[0]),
            current.alt ? h('button', {type: 'button', class: 'btn ai', onclick: current.alt[1]}, current.alt[0]) : null,
            ...(current.more || []).map(([l, f]) => h('button', {type: 'button', class: 'btn ghost', onclick: f}, l)),
            current.skippable ? h('button', {type: 'button', class: 'btn ghost', onclick: () => skip(current.id)}, current.skipLabel || 'Skip this step') : null)),
        ...(current.body || []).filter(Boolean), current.foot || null) : null));
  }
  // ---------------- Check → Clean → Approve → Send: where the report stands and the one next thing to do
  function drawSteps() {
    const bar = $('steps'), r = state.report; if (!bar || !r) return;
    const rv = r.review || {}, sent = (r.sent || []).some(x => x.sent), approved = !!r.approval;
    const dec = store.get(decisionsKey(), {}), edits = store.get(editsKey(), {});
    const pending = state.fixes ? state.fixes.filter(f => f.source !== 'Suggested' && !edits[editId(f.sheet, f.cell)] && !dec[f.id]).length : null;
    const reviewed = reviewedSet();   // a must-fix issue a person has reviewed is handled; approving still asks to confirm if it is in the file
    const mustFix = walkIssues().filter(i => i.severity === 'high' && !reviewed.has(i.key)).length;
    const unsureLeft = state.fixes ? needsCheck().total : 0;
    const sureN = state.fixes ? state.fixes.filter(f => isSureFix(f) && !edits[editId(f.sheet, f.cell)] && !dec[f.id]).length : 0;
    const cleanDone = sent || approved || (pending === 0 && !mustFix && !unsureLeft);
    const list = [
      ['Check', true, rv.status === 'clear' ? 'Nothing flagged' : `${STATUS[rv.status] || 'Check'}: ${statusNote(rv)}`],
      ['Clean', cleanDone, cleanDone ? 'Done' : [sureN ? `${sureN} sure fix${sureN === 1 ? '' : 'es'} ready` : '', unsureLeft ? `${unsureLeft} for a person to check` : '',
        !sureN && !unsureLeft && mustFix ? `${mustFix} must-fix left` : ''].filter(Boolean).join(', ') || 'Loading fixes…'],
      ['Approve', approved || sent, approved || sent ? 'Approved' : 'Not yet'],
      ['Send', sent, sent ? 'Sent to client' : 'Not sent'],
    ];
    const current = list.findIndex(x => !x[1]);
    const next = current === 1 ? (sureN ? 'Next: clean the data (see the box below), or pick fixes one by one on the right.' : unsureLeft ? 'Next: check the items the app is not sure about.' : 'Next: fix the must-fix issues on the right.')
      : current === 2 ? 'Next: approve the final copy.' : current === 3 ? 'Next: send it to the client.' : 'All done. This report has been sent.';
    const pr = progress(), total = (state.fixes || []).length + walkIssues().length, left = pr ? pr.undecided + pr.open : null;
    const meter = (i, done) => i === 1 && !done && pr && total ? (() => { const f = h('i'); f.style.width = `${Math.round((total - left) / total * 100)}%`;
      return h('span', {class: 'meter', title: `${total - left} of ${total} handled`, role: 'progressbar', 'aria-valuemin': '0', 'aria-valuemax': String(total), 'aria-valuenow': String(total - left)}, f); })() : null;
    bar.replaceChildren(...list.map(([name, done, note], i) => h('li', {class: done ? 'done' : i === current ? 'now' : '', 'aria-current': i === current ? 'step' : null},
      h('span', {class: 'dot', 'aria-hidden': 'true'}, done ? '✓' : String(i + 1)), h('b', {}, name),
      h('small', {}, i === 1 && !done && pr && total ? `${total - left} of ${total} done` : note), meter(i, done))),
      h('li', {class: 'next'}, next));
  }
  function drawFixes() {
    const block = $('fixes-block'); if (!state.fixes) return;
    drawCleanOffer(); drawSteps();
    if (!block) { state.fdItems = fdItems(); if (state.sideTab === 'fixes' && $('side') && !$('side').contains(document.activeElement)) drawSide(); else drawSideTabs();
      drawGrid(); updateDownloads(); if (!state.chatBusy) drawChat(); updateFinish(); fdRefreshFoot(); return; }
    const dec = store.get(decisionsKey(), {}), edits = store.get(editsKey(), {});
    const editList = Object.entries(edits);
    const fixes = state.fixes.filter(f => !edits[editId(f.sheet, f.cell)]);
    const accepted = fixes.filter(f => dec[f.id]?.state === 'accepted');
    const total = accepted.length + editList.length;
    block.replaceChildren();
    const save = d => { store.set(decisionsKey(), d); drawFixes(); };
    block.append(h('header', {}, h('h2', {}, 'Suggested fixes'),
      h('p', {}, fixes.length ? `${fixes.length} suggested${fixes.some(f => f.source === 'AI fix') ? ', including Claude\'s' : ''}. Accept the ones you want, or edit any cell yourself, then download a corrected copy.`
        : state.aiFixing ? 'Claude is reading the report and writing fixes for the issues above…' : 'No fixes yet. Let AI fix the issues, or click any cell in the sheet to change it.'),
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
        h('p', {class: 'why'}, f.reason),
        f.only ? h('p', {class: 'only-note'}, f.only) : null));
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
    panel.append(h('div', {class: 'fixbar'},
      h('span', {}, total ? `${total} change${total === 1 ? '' : 's'} ready` + (editList.length ? ` (${editList.length} yours)` : '') : 'No changes yet'),
      total ? h('button', {type: 'button', class: 'btn ghost small', onclick: () => { store.set(decisionsKey(), {}); store.set(editsKey(), {}); drawFixes(); }}, 'Clear all') : null,
      h('button', {type: 'button', class: 'btn', disabled: !total || state.busy, onclick: () => applyFixes(accepted, dec, edits)},
        total ? `Download fixed version (${total})` : 'Download fixed version')));
    block.append(panel);
    drawGrid();
    updateDownloads();
    if (!state.chatBusy) drawChat();
    updateFinish();
    drawSideTabs();
  }
  // Owner's rule: anything the app is not sure about is checked by a person before the report can be approved.
  const isSureFix = f => f.sure !== undefined ? f.sure === true : (f.confidence || 'high') === 'high' && f.source !== 'AI fix';
  // a fix settles an issue when it is on the same cell, or the same row and the issue names the value being fixed
  function fixCovers(f, i) {
    if (!f || !i || i.sheet !== f.sheet) return false;
    const cell = String(f.cell || '').toUpperCase();
    if (i.cell && String(i.cell).toUpperCase() === cell) return true;
    const row = cell.replace(/^[A-Z]+/, ''), val = String(f.before ?? '').trim();
    return !!val && String(i.row) === row && String(i.text || '').includes(val);
  }
  function needsCheck() {
    if (!state.report) return {fixes: [], issues: [], total: 0};
    const dec = store.get(decisionsKey(), {}), edits = store.get(editsKey(), {}), done = reviewedSet();
    const fixes = (state.fixes || []).filter(f => !isSureFix(f) && f.source !== 'Suggested' && !edits[editId(f.sheet, f.cell)] && !dec[f.id]);   // suggestions for blanks are optional
    const issues = walkIssues().filter(i => i.sure === false && !done.has(i.key) && !(state.fixes || []).some(f => fixCovers(f, i)));
    return {fixes, issues, total: fixes.length + issues.length};
  }
  function readyChanges() {
    if (!state.report) return {accepted: [], dec: {}, edits: {}, total: 0};
    const dec = store.get(decisionsKey(), {}), edits = store.get(editsKey(), {});
    const accepted = (state.fixes || []).filter(f => !edits[editId(f.sheet, f.cell)] && dec[f.id]?.state === 'accepted');
    return {accepted, dec, edits, total: accepted.length + Object.keys(edits).length};
  }
  // A copy of the report with each record's label and defect photos pulled from SharePoint (original unchanged).
  async function downloadWithPhotos(r) {
    toast('Getting the photos from SharePoint…');
    try {
      const res = await fetch('/api/spreadsheets/download-photos?id=' + r.file_hash);
      if (!res.ok) { const j = await res.json().catch(() => ({})); throw new Error(j.error || `The server answered ${res.status}.`); }
      const blob = await res.blob();
      const name = (res.headers.get('Content-Disposition') || '').match(/filename="([^"]+)"/)?.[1] || r.filename.replace(/\.xlsx$/i, ' (with photos).xlsx');
      const a = h('a', {href: URL.createObjectURL(blob), download: name}); document.body.append(a); a.click(); a.remove();
      const got = res.headers.get('X-Photos-Embedded'), miss = Number(res.headers.get('X-Photos-Missing') || 0);
      toast(`Downloaded with ${got} photo${got === '1' ? '' : 's'} on the "${res.headers.get('X-Photos-Tab') || 'Defect Photos'}" tab.` + (miss ? ` ${miss} photo${miss === 1 ? '' : 's'} could not be read; the tab says which.` : ''));
    } catch (e) { toast(e.message, true); }
  }
  function downloadWithChanges() {
    const c = readyChanges();
    if (!c.total) { const a = h('a', {href: '/api/spreadsheets/download?id=' + state.report.file_hash, download: state.report.filename}); document.body.append(a); a.click(); a.remove(); return; }
    applyFixes(c.accepted, c.dec, c.edits);
  }
  function updateDownloads() {
    const c = readyChanges(), label = c.total ? `Download with ${c.total} change${c.total === 1 ? '' : 's'}` : `Download v${state.report ? revisionOf(state.report) : 1}`;
    for (const b of document.querySelectorAll('.dl-changes')) { b.textContent = state.busy ? 'Building the fixed copy…' : label; b.disabled = !!state.busy; }
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
      if (state.celebrate) state.celebrate.hash = answer.item.file_hash;
      const rejected = (state.fixes || []).filter(f => dec[f.id]?.state === 'rejected').map(f => ({sheet: f.sheet, cell: f.cell, after: f.after}));
      store.set('rd-carry:' + answer.item.file_hash, {reviewed: store.get('rd-reviewed:' + state.report.file_hash, []), rejected});
      store.set(decisionsKey(), {}); store.set(editsKey(), {});
      if (opts.download !== false) {
        const link = h('a', {href: '/api/spreadsheets/download?id=' + answer.item.file_hash, download: answer.item.filename});
        document.body.append(link); link.click(); link.remove();
      }
      toast(`${opts.download !== false ? 'Saved and downloaded' : 'Saved'} "${answer.item.filename}" with ${changes.length} change${changes.length === 1 ? '' : 's'}. It has been checked again. The original is unchanged.`);
      if (opts.thenOpen !== false) { await loadList(); select(answer.item.file_hash); }
      return answer.item.file_hash;
    } catch (e) { state.celebrate = null; toast(e.message, true); return null; }
    finally { state.busy = false; if ($('fixes-block')) drawFixes(); }
  }
  // Take out extra copies of duplicate rows (new corrected version; original untouched). Accepted cell changes are saved first.
  async function removeRows(rows, what) {
    if (!rows.length || state.busy) return;
    let hash = state.report.file_hash;
    const c = readyChanges();
    if (c.total) { hash = await applyFixes(c.accepted, c.dec, c.edits, {download: false, thenOpen: false}); if (!hash) return; }
    state.busy = true; toast(`Taking out ${rows.length} extra row${rows.length === 1 ? '' : 's'}…`);
    try {
      const out = await api('/api/spreadsheets/remove-rows', {file_hash: hash, rows: rows.map(([sheet, row]) => ({sheet, row}))});
      toast(`${what || `Took out ${out.removed} extra row${out.removed === 1 ? '' : 's'}`}. Saved as "${out.item.filename}" and checked again. The original is unchanged.`);
      state.busy = false; await loadList(); select(out.item.file_hash);
    } catch (e) { state.busy = false; toast(e.message, true); if (hash !== state.report.file_hash) { await loadList(); select(hash); } }
  }
  const isDupRow = i => i.code === 'duplicate_row';

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
  async function loadGrid(sheet, quiet) {
    const block = $('sheet-block'); if (!block) return;
    const hash = state.report.file_hash;
    if (!sheet) { // open the sheet with the most to look at
      const m = marks(), names = (state.report.sheets || []).map(x => x.name);
      sheet = Object.entries(m.perSheet).sort((a, b) => b[1] - a[1])[0]?.[0] || (state.report.dataset_selection?.authoritative || []).find(n => names.includes(n)) || names.find(n => (state.report.sheets.find(x => x.name === n).tables || []).length) || names[0];
    }
    try {
      const g = await api('/api/spreadsheets/sheet', {file_hash: hash, sheet});
      if (state.selected !== hash) return;
      state.grid = {...g, hash}; state.gridSheet = g.sheet; if (!quiet) drawGrid(!(state.sheetScroll || {})[g.sheet]);
    } catch (e) { block.replaceChildren(h('p', {class: 'note'}, e.message)); }
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
      const isFocusRow = focusRow === r;
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
    const onlyChanged = !!state.gridOnlyMarked, q = (state.gridSearch || '').trim().toLowerCase();
    const markedRows = new Set();
    for (const k of Object.keys(m.cells)) if (k.startsWith(sheet + '!')) markedRows.add(splitCell(k.slice(sheet.length + 1))[1]);
    for (const k of Object.keys(m.rows)) if (k.startsWith(sheet + '!')) markedRows.add(Number(k.slice(sheet.length + 1)));
    let only = onlyChanged ? new Set(markedRows) : null;
    if (q) { const hit = new Set(); g.rows.forEach((row, n) => { if ((row || []).some(v => String(v ?? '').toLowerCase().includes(q))) hit.add(n + 1); });
      only = only ? new Set([...only].filter(x => hit.has(x))) : hit; }
    const cur = (state.fdItems || []).find(x => x.key === state.fdCur);
    const focus = cur && cur.sheet === sheet ? {cell: cur.cell, row: cur.row} : state.gridRow ? {row: state.gridRow} : null;
    const {table, first} = buildGrid(g, m, {onlyRows: only, selected: state.gridCell, focus});
    table.style.fontSize = (state.gridZoom || 100) / 100 * 13 + 'px';
    const flagged = Object.entries(m.cells).filter(([k, l]) => k.startsWith(sheet + '!') && l.some(x => x.kind === 'issue')).length + Object.keys(m.rows).filter(k => k.startsWith(sheet + '!')).length;
    const tabs = h('div', {class: 'fd-tabs', role: 'tablist', 'aria-label': 'Sheets'}, (g.sheets || []).map(n => h('button', {type: 'button', role: 'tab', class: n === sheet ? 'on' : '', 'aria-selected': String(n === sheet),
      onclick: () => { if (n === sheet) return; fdKeepScroll(); state.gridCell = null; state.gridRow = null; loadGrid(n); }}, n, m.perSheet[n] ? h('i', {'aria-label': `${m.perSheet[n]} flagged`}, String(m.perSheet[n])) : null)));
    table.addEventListener('click', e => {
      const td = e.target.closest('td[data-cell]'); if (!td) return;
      const addr = td.dataset.cell, row = splitCell(addr)[1];
      const hit = (state.fdItems || []).find(x => x.sheet === sheet && x.cell === addr) || (state.fdItems || []).find(x => x.sheet === sheet && !x.cell && x.row === row);
      if (hit) { if (state.sideTab !== 'fixes') state.sideTab = 'fixes'; fdOpen(hit); return; }
      state.gridCell = addr; state.gridRow = row; state.fdCur = null; drawGrid(); if (state.sideTab === 'fixes') drawSide();
    });
    const old = block.querySelector('.gridwrap');
    const keep = old && !scroll && old.dataset.sheet === sheet ? [old.scrollTop, old.scrollLeft] : (state.sheetScroll || {})[sheet] || null;
    const scroller = h('div', {class: 'gridwrap', 'data-sheet': sheet, tabindex: '0', 'aria-label': `Sheet ${sheet}`}, table);
    const search = h('input', {type: 'search', value: state.gridSearch || '', placeholder: 'Find a record, part or value…', 'aria-label': 'Find in this sheet',
      oninput: e => { state.gridSearch = e.target.value; clearTimeout(state.gridSearchT); state.gridSearchT = setTimeout(() => { drawGrid(true); const i = document.querySelector('.fd-search input'); if (i) { i.focus(); i.setSelectionRange(i.value.length, i.value.length); } }, 200); }});
    const empty = only && !only.size;
    block.replaceChildren(
      h('div', {class: 'fd-toolbar'},
        h('label', {class: 'fd-search'}, icon('search', 16), search),
        h('label', {class: 'fd-check'}, h('input', {type: 'checkbox', checked: onlyChanged, onchange: e => { state.gridOnlyMarked = e.target.checked; drawGrid(true); }}), ` Only exceptions (${markedRows.size} row${markedRows.size === 1 ? '' : 's'})`),
        h('label', {class: 'fd-zoom'}, 'Zoom ', h('select', {'aria-label': 'Sheet zoom', onchange: e => { state.gridZoom = Number(e.target.value); drawGrid(); }},
          [80, 100, 125, 150].map(z => h('option', {value: String(z), selected: (state.gridZoom || 100) === z ? true : null}, z + '%')))),
        state.fdAsideHidden ? h('button', {type: 'button', class: 'btn small', onclick: () => { state.fdAsideHidden = false; $('fd-work')?.classList.remove('aside-hidden'); drawSide(); }}, icon('panel', 15), ' Show corrections') : null),
      empty ? h('div', {class: 'fd-empty'}, h('b', {}, q ? 'No matching rows' : 'No exceptions on this sheet'), h('p', {}, q ? `Nothing on "${sheet}" contains “${state.gridSearch}”.` : 'Every row on this sheet is clear.')) : scroller,
      tabs,
      h('div', {class: 'legend'},
        h('span', {class: 'k iss high'}, `Flagged (${flagged})`),
        h('span', {class: 'k fixme'}, 'Suggested change'),
        h('span', {class: 'k ready'}, 'Selected for the new version'),
        h('span', {class: 'k fx'}, 'Formula'),
        g.truncated ? h('span', {class: 'note'}, 'Large sheet: only the first rows and columns are shown.') : null));
    if (keep && !empty) { scroller.scrollTop = keep[0]; scroller.scrollLeft = keep[1]; }
    else if (scroll && !empty) requestAnimationFrame(() => reveal(scroller, first, false));
    drawCellSlot();
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
  function autoOpenChat() {  // Claude's first read is ready: flag the Ask Claude tab rather than pulling you away from the fixes
    if (state.sideTab === 'claude') return;
    state.chatUnread = true; drawSideTabs();
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

  function openChat() { showSide('claude'); }
  function closeChat() { if (state.sideTab === 'claude') showSide('fixes'); }
  function drawChat() {
    const panel = $('chat'); if (!panel || !state.report) return;
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
            h('button', {type: 'button', class: 'btn small', onclick: () => focusIssue(0)}, 'Go through them with me'),
            offer.length && !done ? h('button', {type: 'button', class: 'btn ghost small', onclick: () => { showSide('fixes'); $('fixes-block')?.scrollIntoView({behavior: 'smooth'}); }}, "I'll pick") : null)));
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
    panel.replaceChildren(
      h('div', {class: 'chat-top'}, h('b', {}, 'Ask Claude about this report'),
        log.length ? h('button', {type: 'button', class: 'btn ghost small', onclick: () => { store.set(chatKey(), []); drawChat(); }}, 'New chat') : null),
      list,
      h('form', {class: 'chat-send', onsubmit: e => { e.preventDefault(); send(box.value); }}, box,
        h('button', {type: 'submit', class: 'btn primary', disabled: !!state.chatBusy}, 'Send')),
    );
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
    reveal(wrap, td, true);
  }

  // ---------------- when every fix and issue is handled: offer the final download and the send
  function progress() {
    const r = state.report; if (!r || state.fixes == null || state.aiFixing || state.reviewing || state.verifying) return null;
    const dec = store.get(decisionsKey(), {}), edits = store.get(editsKey(), {});
    const undecided = state.fixes.filter(f => f.source !== 'Suggested' && !dec[f.id]?.state && !edits[editId(f.sheet, f.cell)]).length;   // suggestions for blanks are optional
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
    drawSteps();
    const slot = $('finish'), r = state.report; if (!slot || !r) return;
    const p = progress(), sent = (r.sent || []).some(x => x.sent);
    state.finishSeen ||= {};
    if (!p || sent) { slot.replaceChildren(); return; }
    const was = state.finishSeen[r.file_hash];
    state.finishSeen[r.file_hash] = p.done;
    slot.replaceChildren();   // where things stand and what to do next is the step-by-step guide at the top
    drawCleanOffer();
    if (!p.done) return;
    if (state.celebrate && state.celebrate.hash === r.file_hash) { const c = state.celebrate; state.celebrate = null; store.set('rd-finish-offered:' + r.file_hash, true); offerFinish({...p, saved: c.n, items: c.items}); return; }
    if (state.celebrate) return;
    if (was === false && !store.get('rd-finish-offered:' + r.file_hash, false)) { store.set('rd-finish-offered:' + r.file_hash, true); offerFinish(p); }
  }
  function confetti() {
    if (matchMedia('(prefers-reduced-motion: reduce)').matches) return null;
    const box = h('div', {class: 'confetti', 'aria-hidden': 'true'}), colors = ['#20594d', '#8fd3b8', '#d68910', '#5b7fa8', '#c0392b', '#f2c94c'];
    for (let i = 0; i < 36; i++) { const c = h('i'); c.style.left = `${Math.random() * 100}%`; c.style.background = colors[i % colors.length];
      c.style.animationDelay = `${Math.random() * 0.4}s`; c.style.animationDuration = `${1.4 + Math.random() * 1.2}s`; c.style.transform = `rotate(${Math.random() * 360}deg)`; box.append(c); }
    setTimeout(() => box.remove(), 3200);
    return box;
  }
  function offerFinish(p) {
    if (document.querySelector('dialog[open]:not(#finish-dialog)')) { setTimeout(() => offerFinish(p), 1500); return; }
    let dlg = $('finish-dialog');
    if (!dlg) { dlg = h('dialog', {id: 'finish-dialog', class: 'fixer finishdlg', 'aria-labelledby': 'finish-title'}); document.body.append(dlg);
      dlg.addEventListener('click', e => { if (e.target === dlg) dlg.close(); }); }
    const r = state.report, handled = p.items || (state.fixes || []).length + walkIssues().length;
    const nextRep = visibleList().find(x => x.file_hash !== r.file_hash && x.review && x.review.status !== 'clear' && !x.review.sent_at && inClientR(x));
    dlg.replaceChildren(h('div', {class: 'fixer-wait celebrate'},
      confetti(),
      h('div', {class: 'finish-mark'}, '✓'),
      h('h2', {id: 'finish-title'}, 'All clear!'),
      h('p', {class: 'wins'}, handled ? `You handled ${handled === 1 ? 'the one item' : `all ${handled} items`} on ${r.review.job || r.filename}.` : `${r.review.job || r.filename} is clean.`),
      h('p', {}, (p.saved ? `${p.saved} change${p.saved === 1 ? ' is' : 's are'} saved in this version, and it has been checked again. ` : p.ready ? `${p.ready} change${p.ready === 1 ? '' : 's'} will go into the final report. ` : 'Nothing needs changing. ') + 'Next: approve it and send it to the client, or download it.'),
      h('div', {class: 'row'},
        nextRep ? h('button', {type: 'button', class: 'btn ghost', onclick: () => { dlg.close(); select(nextRep.file_hash); }}, 'Next report ›') : h('button', {type: 'button', class: 'btn ghost', onclick: () => dlg.close()}, 'Not now'),
        h('button', {type: 'button', class: 'btn', onclick: downloadFinal}, 'Download final report'),
        h('button', {type: 'button', class: 'btn primary', onclick: sendFinal}, 'Approve…'))));
    if (!dlg.open) dlg.showModal();
  }

  // ---------------- recommendations: next steps from the checks and Claude's analysis, with one-click actions
  const PRI = {high: 'Do first', medium: 'Soon', low: 'When you can'};
  function recsBlock(recs, {ask, act, scope}) {
    if (!recs) return null;
    const items = recs.items || [];
    const busy = h('span', {class: 'note recsbusy', hidden: true}, h('span', {class: 'spin'}), ' Claude is reading the results…');
    const askBtn = recs.ai_on ? h('button', {type: 'button', class: 'btn small', onclick: async e => {
      e.target.disabled = true; busy.hidden = false;
      try { await ask(); } catch (err) { toast(err.message, true); e.target.disabled = false; busy.hidden = true; } }},
      recs.source === 'ai' ? 'Ask Claude again' : 'Ask Claude for recommendations') : null;
    const src = recs.source === 'ai' ? `From Claude's analysis of ${scope}, ${ago(recs.made_at)}.` : `From the checks on ${scope}.` + (recs.ai_on ? ' Ask Claude for a deeper read.' : '');
    return h('section', {class: 'block recs', 'aria-label': 'Recommendations'},
      h('header', {}, h('h2', {}, 'Recommendations'), h('p', {}, src), h('div', {class: 'recsbar'}, askBtn, busy)),
      recs.note ? h('p', {class: 'note'}, recs.note) : null,
      items.length ? h('ol', {class: 'reclist'}, items.map(r => h('li', {class: 'rec pri-' + r.priority},
        h('div', {class: 'rectext'}, h('span', {class: 'recpri'}, PRI[r.priority] || 'Soon'), h('b', {}, r.title), h('p', {}, r.why), r.who && r.who !== 'you' ? h('small', {}, 'For the ' + r.who) : null),
        r.do && act(r) ? h('button', {type: 'button', class: 'btn small', onclick: act(r).run}, act(r).label) : null)))
        : h('p', {class: 'note'}, 'Nothing to recommend. Everything checks out.'));
  }
  async function loadReportRecs(askAi) {
    const r = state.report, slot = $('recs'); if (!r || !slot) return;
    try {
      const recs = await api('/api/recommendations', {file_hash: r.file_hash, ask_ai: askAi});
      if (state.report?.file_hash !== r.file_hash || !$('recs')) return;
      $('recs').replaceChildren(...[recsBlock(recs, {scope: 'this report', ask: () => loadReportRecs(true), act: reportAct})].filter(Boolean));
    } catch (e) { if (askAi) throw e; }
  }
  function reportAct(rec) {
    const r = state.report, newer = (r.fixes_applied || []).slice(-1)[0];
    const scrollTo = id => () => $(id)?.scrollIntoView({behavior: 'smooth', block: 'start'});
    return ({
      fix_issues: {label: 'Fix them', run: () => focusIssue(0)},
      review_fixes: {label: 'Show the fixes', run: () => { showSide('fixes'); scrollTo('fixes-block')(); }},
      fix_shifts: {label: 'Show the rows', run: () => { showSide('fixes'); scrollTo('shiftfix')(); }},
      recheck_sharepoint: {label: 'Check SharePoint', run: () => rerun('sharepoint', 'Checking SharePoint…')},
      send_for_review: {label: 'Send for review', run: () => { reviewForm('request'); scrollTo('reviewstate')(); }},
      approve_send: {label: 'Send', run: openSend},
      open_report: newer ? {label: 'Open corrected copy', run: () => select(newer.fixed_file_hash)} : null,
    })[rec.do] || null;
  }
  function periodAct(rec, go) {
    return ({open_week: {label: rec.target ? `Open ${rec.target}` : 'Open the days', run: () => go(rec.target)}})[rec.do] || null;
  }

  // ---------------- review and sign-off: send a report to the boss, who approves it or sends it back with a comment
  function reviewState(r) {
    const rr = r.review_request || {};
    const link = () => { const url = location.origin + location.pathname + '#r=' + r.file_hash; navigator.clipboard?.writeText(url).then(() => toast('Link copied. Paste it in Teams or an email to the reviewer.'), () => toast(url)); };
    if (rr.status === 'waiting') return h('div', {class: 'reviewstate', id: 'reviewbox'},
      h('div', {}, h('b', {}, 'Waiting for review'), h('span', {}, `${rr.by} asked ${ago(rr.at)}${rr.note ? `: “${rr.note}”` : '.'}`)),
      h('div', {class: 'btns'}, h('button', {type: 'button', class: 'btn primary small', onclick: () => openSend()}, 'Approve'),
        h('button', {type: 'button', class: 'btn small', onclick: () => reviewForm('send_back')}, 'Send back with a comment'),
        h('button', {type: 'button', class: 'btn ghost small', onclick: link}, 'Copy link for the reviewer')));
    if (rr.status === 'sent_back') return h('div', {class: 'reviewstate back', id: 'reviewbox'},
      h('div', {}, h('b', {}, `Sent back by ${rr.reviewer}`), h('span', {}, `“${rr.comment}” (${ago(rr.answered_at)})`)),
      h('div', {class: 'btns'}, h('button', {type: 'button', class: 'btn small', onclick: () => reviewForm('request')}, 'Send for review again')));
    return null;
  }
  function reviewForm(action) {
    const slot = $('reviewstate'); if (!slot) return;
    const who = h('input', {type: 'text', placeholder: 'Your name', value: myName(), 'aria-label': 'Your name'});
    const note = h('textarea', {rows: 2, placeholder: action === 'request' ? 'Note for the reviewer (optional), e.g. “Fixed the two duplicate VINs; please check the totals.”' : 'What needs changing?', 'aria-label': 'Note'});
    const go = async () => {
      try {
        store.set('rd-approver', who.value.trim());
        const out = await api('/api/spreadsheets/review', {file_hash: state.report.file_hash, action, by: who.value, note: note.value});
        state.report = {...state.report, ...out.item, review: {...state.report.review, review_state: out.item.review_request?.status}};
        toast(action === 'request' ? 'Sent for review. Copy the link to let the reviewer know.' : 'Sent back with your comment.');
        drawReport(); loadList();
      } catch (e) { toast(e.message, true); }
    };
    slot.replaceChildren(h('div', {class: 'reviewstate' + (action === 'send_back' ? ' back' : '')},
      h('div', {class: 'reviewform'}, h('b', {}, action === 'request' ? 'Send this report for review' : 'Send it back with a comment'), who, note,
        h('div', {class: 'btns'}, h('button', {type: 'button', class: 'btn ghost small', onclick: () => slot.replaceChildren(...[reviewState(state.report)].filter(Boolean))}, 'Cancel'),
          h('button', {type: 'button', class: 'btn primary small', onclick: go}, action === 'request' ? 'Send for review' : 'Send back')))));
    note.focus();
  }

  // ---------------- executive overview across every job
  async function showOverview() {
    const wrap = h('div', {class: 'sheetwrap apiwrap'});
    $('report').replaceChildren(wrap);
    state.ovDays ||= 30;
    wrap.replaceChildren(h('p', {class: 'note'}, h('span', {class: 'spin'}), ' Adding up every report…'));
    let o;
    try { o = state.ovRecs && state.ovRecs.days === state.ovDays ? state.ovRecs : await api('/api/overview', {days: state.ovDays}); state.ovRecs = null; } catch (e) { wrap.replaceChildren(h('p', {class: 'note'}, e.message)); return; }
    if (location.hash !== '#overview') return;
    const t = o.totals, top = Math.max(1, ...o.categories.map(c => c.count));
    const pct = v => v == null ? '—' : v.toFixed(v < 10 ? 1 : 0) + '%';
    wrap.replaceChildren(...[
      h('div', {class: 'head'}, h('div', {}, h('h1', {}, 'Quality overview'), h('div', {class: 'meta'}, `Every job: ${o.label.replace(/^Last/, 'the last')}. What the verifier caught before any client saw it, and how reliably reports went out.`))),
      h('div', {class: 'periodbar'}, h('div', {class: 'pseg'}, [7, 30, 90].map(d => h('button', {type: 'button', class: state.ovDays === d ? 'on' : '', onclick: () => { state.ovDays = d; showOverview(); }}, `${d} days`))),
      ),
      h('div', {class: 'ovhero'}, h('b', {}, fmt(t.caught)), h('span', {}, `errors caught and fixed before the client saw them, across ${fmt(t.reports)} daily reports for ${fmt(t.jobs)} job${t.jobs === 1 ? '' : 's'}. ${fmt(t.must_fix)} were must-fix.`)),
      h('div', {class: 'numbers five'},
        h('div', {}, h('b', {}, fmt(t.corrected)), h('span', {}, 'Corrected before sending')),
        h('div', {}, h('b', {}, `${fmt(t.sent)} of ${fmt(t.reports)}`), h('span', {}, 'Sent from the verifier')),
        h('div', {}, h('b', {}, pct(t.on_time_pct)), h('span', {}, 'Sent on time')),
        h('div', {}, h('b', {}, t.turnaround == null ? '—' : `${t.turnaround} h`), h('span', {}, 'Upload to sent')),
        h('div', {}, h('b', {class: t.open ? 'bad' : ''}, fmt(t.open)), h('span', {}, 'Still on Hold'))),
      recsBlock(o.recs, {scope: 'every job', ask: async () => { const n = await api('/api/overview', {days: o.days, ask_ai: true}); if (location.hash === '#overview') { state.ovRecs = n; showOverview(); } },
        act: rec => periodAct(rec, job => { state.periodQuery = {kind: o.days > 7 ? 'month' : 'week', day: o.end, job: job || ''}; location.hash = 'periods'; })}),
      h('section', {class: 'block'}, h('header', {}, h('h2', {}, 'What was caught'), h('p', {}, 'By kind of error, from the first upload of each daily report.')),
        o.categories.length ? h('div', {class: 'ovbars'}, o.categories.map(c => h('div', {class: 'ovbar'}, h('span', {}, c.reason), h('div', {}, (() => { const i = h('i'); i.style.width = `${c.count / top * 100}%`; return i; })()), h('em', {}, fmt(c.count)))))
          : h('p', {class: 'note'}, 'Nothing caught in this period.')),
      h('section', {class: 'block'}, h('header', {}, h('h2', {}, 'By job')),
        h('div', {class: 'tablewrap'}, h('table', {class: 'keys pdays'},
          h('thead', {}, h('tr', {}, ['Job', 'Days', 'Inspected', 'Reject rate', 'Errors caught', 'Corrected', 'Sent', 'On time', 'Upload to sent', ''].map(x => h('th', {}, x)))),
          h('tbody', {}, o.jobs.map(j => h('tr', {}, h('td', {}, h('b', {}, j.job)), h('td', {}, fmt(j.days)), h('td', {}, fmt(j.inspected)), h('td', {}, pct(j.rate)),
            h('td', {}, fmt(j.caught)), h('td', {}, fmt(j.corrected)), h('td', {}, `${fmt(j.sent)} of ${fmt(j.days)}`), h('td', {}, fmt(j.on_time)), h('td', {}, j.turnaround == null ? '—' : `${j.turnaround} h`),
            h('td', {}, h('button', {type: 'button', class: 'btn small', onclick: () => { state.periodQuery = {kind: o.days > 7 ? 'month' : 'week', day: o.end, job: j.job}; location.hash = 'periods'; }}, 'Details')))))))),
      changeRecords(),
    ].filter(Boolean));
  }

  // ---------------- KPI dashboard: every client, drill down to a client, then a job
  function kpiChart(points, opts) {
    // one series, one axis; bars in the app green, hover any bar for its value
    const W = 360, H = 140, pad = {l: 34, r: 6, t: 8, b: 20};
    const vals = points.map(p => p.v == null ? 0 : p.v), raw = Math.max(...vals, 0);
    const max = opts.pct ? Math.max(opts.min || 1, raw) : Math.max(1, Math.ceil(raw));
    const step = (W - pad.l - pad.r) / Math.max(points.length, 1), bw = Math.max(1, Math.min(18, step - (step > 4 ? 2 : 0)));
    const y = v => pad.t + (H - pad.t - pad.b) * (1 - v / max), NS = 'http://www.w3.org/2000/svg';
    const el = (tag, attrs, text) => { const e = document.createElementNS(NS, tag); for (const k in attrs) e.setAttribute(k, attrs[k]); if (text != null) e.textContent = text; return e; };
    const svg = el('svg', {viewBox: `0 0 ${W} ${H}`, class: 'kchart', role: 'img', 'aria-label': opts.label});
    const ticks = opts.pct || max >= 4 ? [0, max / 2, max] : [0, max];
    ticks.forEach(t => { svg.append(el('line', {x1: pad.l, x2: W - pad.r, y1: y(t), y2: y(t), class: 'grid'}), el('text', {x: pad.l - 6, y: y(t) + 3, class: 'ax', 'text-anchor': 'end'}, opts.fmt(t, true))); });
    points.forEach((p, i) => {
      const x = pad.l + i * step + (step - bw) / 2, v = p.v == null ? 0 : p.v, top = y(v), hgt = H - pad.b - top;
      const g = el('g', {class: 'bar' + (opts.bad && v > 0 ? ' bad' : '')});
      g.append(el('rect', {x: pad.l + i * step, y: pad.t, width: step, height: H - pad.t - pad.b, class: 'hit'}));
      if (v > 0) g.append(el('path', {d: `M${x},${H - pad.b}V${top + Math.min(4, hgt / 2)}q0,-${Math.min(4, hgt / 2)} ${Math.min(4, bw / 2)},-${Math.min(4, hgt / 2)}h${Math.max(0, bw - 2 * Math.min(4, bw / 2))}q${Math.min(4, bw / 2)},0 ${Math.min(4, bw / 2)},${Math.min(4, hgt / 2)}V${H - pad.b}Z`}));
      g.append(el('title', {}, `${p.label}: ${p.v == null ? 'no reports' : opts.fmt(p.v)}`));
      svg.append(g);
    });
    const lbl = [0, Math.floor((points.length - 1) / 2), points.length - 1].filter((v, i, a) => a.indexOf(v) === i);
    lbl.forEach(i => points[i] && svg.append(el('text', {x: pad.l + i * step + step / 2, y: H - 6, class: 'ax', 'text-anchor': i === 0 ? 'start' : i === points.length - 1 ? 'end' : 'middle'}, points[i].short)));
    return svg;
  }
  function kpiSeries(trend, days) {
    const md = d => { const [, m, dd] = d.split('-').map(Number); return `${['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'][m - 1]} ${dd}`; };
    if (days <= 31) return trend.map(t => ({...t, label: md(t.day), short: md(t.day)}));
    const out = [];   // weeks for longer ranges
    for (let i = 0; i < trend.length; i += 7) {
      const wk = trend.slice(i, i + 7), sum = k => wk.reduce((a, t) => a + (t[k] || 0), 0);
      out.push({day: wk[0].day, label: `Week of ${md(wk[0].day)}`, short: md(wk[0].day), reports: sum('reports'), caught: sum('caught'), stopped: sum('stopped'), reached: sum('reached'),
        inspected: sum('inspected'), rejected: sum('rejected'), rate: sum('inspected') ? sum('rejected') / sum('inspected') * 100 : null});
    }
    return out;
  }
  async function showKpis() {
    const wrap = h('div', {class: 'sheetwrap apiwrap kpis'});
    $('report').replaceChildren(wrap);
    const q = state.kpi ||= {days: 30, client: '', job: ''};
    wrap.replaceChildren(h('p', {class: 'note'}, h('span', {class: 'spin'}), ' Adding up the numbers…'));
    let k;
    try { k = await api('/api/kpis', {days: q.days, client: q.client || null, job: q.job || null}); } catch (e) { wrap.replaceChildren(h('p', {class: 'note'}, e.message)); return; }
    if (location.hash !== '#kpis' || state.kpi !== q) return;
    const t = k.totals, go = (c) => { Object.assign(q, c); showKpis(); };
    const pct = v => v == null ? '—' : (v < 10 ? v.toFixed(1) : Math.round(v)) + '%';
    const plural = (n, w) => `${fmt(n)} ${w}${n === 1 ? '' : 's'}`;
    const clientName = k.client_name || (q.client === '_none' ? 'Other' : q.client);
    const crumbs = [h('button', {type: 'button', class: 'crumb' + (!q.client ? ' on' : ''), onclick: () => go({client: '', job: ''})}, 'All clients')];
    if (q.client) crumbs.push(h('span', {class: 'sep', 'aria-hidden': 'true'}, '›'), h('button', {type: 'button', class: 'crumb' + (!q.job ? ' on' : ''), onclick: () => go({job: ''})}, clientName));
    if (q.job) crumbs.push(h('span', {class: 'sep', 'aria-hidden': 'true'}, '›'), h('span', {class: 'crumb on'}, q.job));
    const opts = [...k.clients.map(c => [c.key, c.name])];
    if (q.client && !opts.some(o => o[0] === q.client)) opts.push([q.client, clientName]);
    const sel = h('select', {'aria-label': 'Client', onchange: e => go({client: e.target.value, job: ''})},
      h('option', {value: ''}, 'All clients'), ...opts.map(([v, n]) => h('option', {value: v, selected: q.client === v ? '' : null}, n)));
    const scope = q.job ? `job ${q.job}` : q.client ? clientName : 'every client';
    const tile = (v, label, sub, cls) => h('div', {class: 'ktile' + (cls ? ' ' + cls : '')}, h('span', {}, label), h('b', {}, v), sub ? h('em', {}, sub) : null);
    const s = kpiSeries(k.trend, k.days);
    const chartBlock = (title, note, key, o) => h('div', {class: 'kcard'}, h('h3', {}, title), h('p', {}, note),
      s.some(p => p[key]) ? kpiChart(s.map(p => ({v: p[key], label: p.label, short: p.short})), {label: title, ...o}) : h('div', {class: 'kzero'}, o.zero || 'None in this period'));
    const n0 = v => fmt(Math.round(v));
    const empty = !t.reports;
    const top = Math.max(1, ...k.categories.map(c => c.count));
    const tableRow = (cells, onclick, label) => h('tr', {class: onclick ? 'go' : '', tabindex: onclick ? '0' : null, title: label || null, onclick, onkeydown: onclick ? e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onclick(); } } : null}, cells.map((c, i) => h('td', {}, i === 0 ? h('b', {}, c) : c)));
    const heads = ['Reports', 'Errors caught', 'Stopped before client', 'Reached client', 'Sent on time', 'Inspected', 'Rejected', 'Reject rate', 'On hold'];
    const cellsOf = r => [fmt(r.reports), fmt(r.caught), fmt(r.stopped), r.reached ? h('span', {class: 'badn'}, fmt(r.reached)) : '0', pct(r.on_time_pct), fmt(r.inspected), fmt(r.rejected), pct(r.rate), r.hold ? h('span', {class: 'badn'}, fmt(r.hold)) : '0'];
    const toPeriods = job => { state.periodQuery = {kind: k.days > 7 ? 'month' : 'week', day: k.end, job}; location.hash = 'periods'; };
    wrap.replaceChildren(...[
      h('div', {class: 'head'}, h('div', {}, h('h1', {}, 'KPIs'), h('div', {class: 'meta'}, `Reporting numbers for ${scope}, ${k.label}.`))),
      h('div', {class: 'kbar'},
        h('nav', {class: 'crumbs', 'aria-label': 'Drill down'}, ...crumbs),
        h('div', {class: 'kfilters'}, sel, h('div', {class: 'pseg'}, [[7, '7 days'], [30, '30 days'], [90, '90 days'], [365, '12 months']].map(([d, l]) => h('button', {type: 'button', class: q.days === d ? 'on' : '', 'aria-pressed': String(q.days === d), onclick: () => go({days: d})}, l))))),
      empty ? h('section', {class: 'block'}, h('p', {class: 'note'}, `No daily reports for ${scope} in this period. Try a longer range${q.client ? ' or All clients' : ''}.`)) : null,
      empty ? null : h('div', {class: 'khero'},
        h('div', {}, h('b', {}, fmt(t.stopped)), h('span', {}, `errors stopped before they reached a client`), h('em', {}, !t.caught ? 'Nothing caught in this period.' : `${fmt(t.stopped)} of the ${fmt(t.caught)} errors caught were fixed before sending` + (t.stopped_must ? `, ${fmt(t.stopped_must)} of them must-fix.` : '.') + (t.waiting ? ` ${fmt(t.waiting)} ${t.waiting === 1 ? 'is' : 'are'} in reports not sent yet.` : ''))),
        h('div', {class: t.reached ? 'warn' : 'good'}, h('b', {}, fmt(t.reached)), h('span', {}, t.reached === 1 ? 'error reached a client' : 'errors reached a client'), h('em', {}, t.reached ? `Still in reports that were sent. ${fmt(t.reached_must)} must-fix.` : 'Every report that went out was clean.'))),
      empty ? null : h('div', {class: 'ktiles'},
        tile(fmt(t.reports), 'Reports made', `${fmt(t.sent)} sent, ${fmt(t.unsent)} not sent yet`),
        tile(pct(t.on_time_pct), 'Sent on time', t.sent ? `${fmt(t.on_time)} of ${fmt(t.sent)} by the next day` : 'None sent yet'),
        tile(t.turnaround == null ? '—' : `${t.turnaround} h`, 'Upload to sent', 'Typical time from upload to sent'),
        tile(fmt(t.jobs), q.job ? 'Job' : 'Jobs done', q.client ? null : plural(t.clients, 'client')),
        tile(fmt(t.inspected), 'Parts inspected', null),
        tile(fmt(t.certified), 'Accepted', null),
        tile(fmt(t.rejected), 'Rejected', `Reject rate ${pct(t.rate)}`),
        tile(fmt(t.hold), 'Still on hold', t.hold ? 'Not sent, needs a fix' : 'None', t.hold ? 'bad' : ''),
        tile(fmt(t.corrected), 'Corrected before sending', `${fmt(t.uploads)} uploads in all`)),
      empty ? null : h('div', {class: 'kcharts'},
        chartBlock('Errors stopped before the client', k.days > 31 ? 'Per week' : 'Per day', 'stopped', {fmt: n0, zero: t.caught ? 'None fixed and sent yet' : 'Nothing caught in this period'}),
        chartBlock('Errors that reached a client', 'Still in a report when it was sent', 'reached', {fmt: n0, bad: true, zero: 'None. Every report sent was clean.'}),
        chartBlock('Reports made', k.days > 31 ? 'Per week' : 'Per day', 'reports', {fmt: n0}),
        chartBlock('Parts inspected', k.days > 31 ? 'Per week' : 'Per day', 'inspected', {fmt: n0}),
        chartBlock('Reject rate', 'Rejected out of inspected', 'rate', {pct: true, fmt: (v, ax) => ax ? (v < 10 ? v.toFixed(1) : Math.round(v)) + '%' : pct(v), min: 1})),
      empty || !k.categories.length ? null : h('section', {class: 'block'}, h('header', {}, h('h2', {}, 'What was caught'), h('p', {}, `By kind of error, from the first upload of each daily report for ${scope}.`)),
        h('div', {class: 'ovbars'}, k.categories.map(c => h('div', {class: 'ovbar', title: `${c.kind}: ${fmt(c.count)}`}, h('span', {}, c.kind), h('div', {}, (() => { const i = h('i'); i.style.width = `${c.count / top * 100}%`; return i; })()), h('em', {}, fmt(c.count)))))),
      !q.client && k.clients.length ? h('section', {class: 'block'}, h('header', {}, h('h2', {}, 'By client'), h('p', {}, 'Click a client to see its jobs.')),
        h('div', {class: 'tablewrap'}, h('table', {class: 'keys ktable'},
          h('thead', {}, h('tr', {}, ['Client', 'Jobs', ...heads].map(x => h('th', {}, x)))),
          h('tbody', {}, k.clients.map(c => tableRow([c.name, fmt(c.jobs), ...cellsOf(c)], () => go({client: c.key, job: ''}), `See ${c.name}`)))))) : null,
      q.client && !q.job && k.jobs.length ? h('section', {class: 'block'}, h('header', {}, h('h2', {}, `${clientName} by job`), h('p', {}, 'Click a job to see its numbers.')),
        h('div', {class: 'tablewrap'}, h('table', {class: 'keys ktable'},
          h('thead', {}, h('tr', {}, ['Job', ...heads].map(x => h('th', {}, x)))),
          h('tbody', {}, k.jobs.map(j => tableRow([j.job, ...cellsOf(j)], () => go({job: j.job}), `See ${j.job}`)))))) : null,
      q.job ? h('section', {class: 'block'}, h('header', {}, h('h2', {}, `More on ${q.job}`)),
        h('div', {class: 'btns'}, h('button', {type: 'button', class: 'btn primary small', onclick: () => toPeriods(q.job)}, `Open the ${k.days > 7 ? 'monthly' : 'weekly'} report`),
          h('button', {type: 'button', class: 'btn small', onclick: () => go({job: ''})}, `Back to ${clientName}`))) : null,
    ].filter(Boolean));
  }

  function builtBanner(r) {
    const b = r.built, rv = r.review;
    return h('div', {class: 'builtbar'},
      h('div', {}, h('b', {}, 'Built from SharePoint'),
        h('span', {}, `${fmt(b.scans)} scans from “${b.list}”, ${ago(b.built_at)}. Each scan is on the shift its scan time falls in` + (b.outside ? `; ${b.outside} scan${b.outside === 1 ? ' was' : 's were'} outside every shift and went on the nearest one.` : '.'))),
      h('div', {class: 'btns'},
        h('button', {type: 'button', class: 'btn small ghost', onclick: e => headerForm(rv, e.target.closest('.builtbar'))}, 'Report header'),
        h('button', {type: 'button', class: 'btn small', onclick: e => buildReport(rv.job, rv.day, true, e.target)}, 'Rebuild with the latest scans')));
  }
  async function headerForm(rv, bar) {
    let cur = {}, sug = {};
    try { const d = await api('/api/settings/report-header', {job: rv.job}); cur = d.header || {}; sug = d.suggested || {}; } catch (e) { toast(e.message, true); return; }
    const fields = [['customer', 'Customer', 'e.g. SL Alabama, LLC'], ['worksite', 'Worksite', 'e.g. Hyundai Mobis Alabama, Montgomery'], ['program', 'Part / program', 'Optional'], ['prepared_by', 'Prepared by', 'PPG Quality']];
    const inputs = Object.fromEntries(fields.map(([k, label, ph]) => [k, h('input', {type: 'text', value: cur[k] || sug[k] || '', placeholder: ph, 'aria-label': label,
      class: !cur[k] && sug[k] ? 'suggested' : '', oninput: e => e.target.classList.remove('suggested')})]));
    const anySug = fields.some(([k]) => !cur[k] && sug[k]);
    const form = h('div', {class: 'hdrform'}, h('b', {}, `Report header for ${rv.job}`), h('span', {class: 'quiet'}, 'Printed on the Summary tab of every report built for this job.' + (anySug ? ' Blank fields are filled with suggestions (from the SharePoint job list); change anything that is wrong.' : '')),
      ...fields.map(([k, label]) => h('label', {}, label, inputs[k], !cur[k] && sug[k] ? h('em', {class: 'sugnote'}, 'suggested') : null)),
      h('div', {class: 'btns'}, h('button', {type: 'button', class: 'btn ghost small', onclick: () => form.remove()}, 'Cancel'),
        h('button', {type: 'button', class: 'btn primary small', onclick: async e => {
          const header = Object.fromEntries(Object.entries(inputs).map(([k, el]) => [k, el.value]));
          try { await api('/api/settings/report-header', {job: rv.job, header}); form.remove(); buildReport(rv.job, rv.day, true, e.target); } catch (err) { toast(err.message, true); } }}, 'Save and rebuild')));
    bar.after(form); inputs.customer.focus();
  }
  function nexusTable(n) {
    const copy = () => { const t = n.shifts.map(x => `${x.name}\t${x.inspected}\t${x.rejected}\t${x.certified}`).join('\n'); navigator.clipboard?.writeText('Shift\tInspected\tRejected\tCertified\n' + t).then(() => toast('Copied. Paste into Nexus or Excel.'), () => toast(t)); };
    return h('div', {},
      h('div', {class: 'tablewrap'}, h('table', {class: 'keys nexus'},
        h('thead', {}, h('tr', {}, ['Nexus shift', 'Window', 'Scans', 'Inspected', 'Rejected', 'Certified'].map(x => h('th', {}, x)))),
        h('tbody', {}, n.shifts.map(x => h('tr', {}, h('td', {}, h('b', {}, x.name)), h('td', {}, `${hm12(x.start)} – ${hm12(x.end)}`),
          h('td', {class: 'num'}, fmt(x.scans)), h('td', {class: 'num'}, fmt(x.inspected)), h('td', {class: 'num'}, fmt(x.rejected)), h('td', {class: 'num'}, fmt(x.certified)))),
          h('tr', {class: 'tot'}, h('td', {}, h('b', {}, 'Day')), h('td', {}), ...['scans', 'inspected', 'rejected', 'certified'].map(k => h('td', {class: 'num'}, h('b', {}, fmt(n.total[k])))))))),
      n.unplaced ? h('p', {class: 'note'}, `${n.unplaced} scan(s) had no time or fell outside every Nexus window.`) : null,
      h('button', {type: 'button', class: 'btn small', onclick: copy}, 'Copy for Nexus'));
  }
  const hm12 = t => { const [H, M] = t.split(':').map(Number); return `${(H % 12) || 12}:${String(M).padStart(2, '0')} ${H < 12 ? 'AM' : 'PM'}`; };
  function nexusBlock(r) {
    const body = h('div', {}, r.built?.nexus ? nexusTable(r.built.nexus)
      : state.health?.sharepoint_configured === false ? h('p', {class: 'note'}, 'Connect SharePoint to split the day by scan time.')
      : h('button', {type: 'button', class: 'btn small', onclick: async e => {
          e.target.disabled = true; e.target.textContent = 'Reading the scan times…';
          try { body.replaceChildren(nexusTable(await api('/api/nexus', {file_hash: r.file_hash}))); } catch (err) { toast(err.message, true); e.target.disabled = false; e.target.textContent = 'Show Nexus numbers'; } }}, 'Show Nexus numbers'));
    return h('section', {class: 'block', id: 'nexus-block'}, h('header', {}, h('h2', {}, 'Nexus numbers'),
      h('p', {}, 'The day split into Nexus’s three shifts by scan time (plant time), ready to enter in Nexus. Internal only.')), body);
  }

  // ---------------- build a report from SharePoint
  async function buildReport(job, day, force, btn, quiet) {
    const was = btn?.textContent; if (btn) { btn.disabled = true; btn.textContent = 'Building…'; }
    try {
      const out = await api('/api/build', {job, day, force: !!force});
      const r = out.report, b = out.built;
      toast(`${job}: report built from ${fmt(b.scans)} scans. ${r.status === 'clear' ? 'Every check passed.' : r.status === 'hold' ? 'It has must-fix items.' : 'It has items to look at.'}`);
      if (!quiet) select(r.id);
      loadList();
      return out;
    } catch (e) { toast(`${job}: ${e.message}`, true); if (btn) { btn.disabled = false; btn.textContent = was; } return null; }
  }

  // ---------------- Today: every job's day at a glance
  async function showToday() {
    const wrap = h('div', {class: 'sheetwrap apiwrap'});
    $('report').replaceChildren(wrap);
    state.todayDay ||= null;
    let data;
    const load = async extra => {
      try { data = await api('/api/today', {day: state.todayDay, ...(extra || {})}); } catch (e) { wrap.replaceChildren(h('p', {class: 'note'}, e.message)); return false; }
      return location.hash === '#today';
    };
    wrap.replaceChildren(h('p', {class: 'note'}, h('span', {class: 'spin'}), ' Looking at every job…'));
    if (!await load()) return;
    const sp = {};
    const draw = () => {
      const day = data.day, rows = data.jobs, unwatched = [];
      const label = new Date(day + 'T12:00:00').toLocaleDateString('en-US', {weekday: 'long', month: 'long', day: 'numeric'});
      const counts = {send: 0, missing: 0, hold: 0};
      for (const r of rows) {
        const y = r.yesterday, s = sp[r.job];
        if (y && !y.sent_at) counts.send++;
        if (y && y.status === 'hold') counts.hold++;
        if (!y && s?.yesterday) counts.missing++;
      }
      const shift = d => { const x = new Date(day + 'T12:00:00'); x.setDate(x.getDate() + d); state.todayDay = x.toISOString().slice(0, 10); showToday(); };
      const prevDay = (() => { const x = new Date(day + 'T12:00:00'); x.setDate(x.getDate() - 1); return x.toISOString().slice(0, 10); })();
      const clock = iso => new Date(iso).toLocaleTimeString('en-US', {hour: 'numeric', minute: '2-digit'});
      const buildBtn = (job, d, label, force, primary) => h('button', {type: 'button', class: 'btn small' + (primary ? ' primary' : ''), onclick: e => buildReport(job, d, force, e.target)}, label);
      const repCell = (rep, scans, when, job) => {
        if (rep) return h('td', {}, h('a', {href: '#r=' + rep.file_hash}, h('span', {class: 'tag tag-' + rep.status}, STATUS[rep.status])), ' ',
          h('span', {class: rep.sent_at ? 'ok' : 'warn'}, rep.sent_at ? 'Sent' : rep.approved ? 'Approved, not sent' : 'Not sent'));
        const s = sp[job];
        if (when === 'today' && s?.today) {
          const rd = s.readiness || {};
          return h('td', {}, rd.state === 'running'
            ? h('span', {class: 'buildcell'}, h('span', {class: 'quiet'}, `Shift running · ready about ${clock(rd.ready_at)}`), buildBtn(job, day, 'Build now anyway', true, false))
            : h('span', {class: 'buildcell'}, h('span', {class: 'ok'}, 'Ready to build'), buildBtn(job, day, 'Build report', false, true)));
        }
        if (scans) return h('td', {}, h('span', {class: 'buildcell'}, h('span', {class: 'differs'}, `Missing (${fmt(scans)} scans on SharePoint)`), data.sharepoint_on ? buildBtn(job, prevDay, 'Build it', true, true) : null));
        return h('td', {}, h('span', {class: 'quiet'}, when === 'today' ? 'No scans yet' : '—'));
      };
      const readyJobs = rows.filter(r => !r.today && sp[r.job]?.today && sp[r.job]?.readiness?.state === 'ready');
      const addBox = h('input', {type: 'text', placeholder: 'Add a job to watch, e.g. HANDS-HMMA-2219', 'aria-label': 'Job number'});
      wrap.replaceChildren(
        h('div', {class: 'head'}, h('div', {}, h('h1', {}, 'Today'), h('div', {class: 'meta'}, 'Every job’s day at a glance: scans on SharePoint so far, and where each daily report stands.'))),
        todaysWins(),
        h('div', {class: 'periodbar'}, h('div', {class: 'pnav'}, h('button', {type: 'button', class: 'btn small', 'aria-label': 'Previous day', onclick: () => shift(-1)}, '‹'),
          h('b', {}, label), h('button', {type: 'button', class: 'btn small', 'aria-label': 'Next day', onclick: () => shift(1)}, '›')),
          readyJobs.length ? h('button', {type: 'button', class: 'btn primary', onclick: async e => {
            e.target.disabled = true;
            for (const [i, r] of readyJobs.entries()) { e.target.textContent = `Building ${i + 1} of ${readyJobs.length}…`; await buildReport(r.job, day, false, null, true); }
            showToday(); }}, `Build ${readyJobs.length} ready report${readyJobs.length === 1 ? '' : 's'}`) : null),
        h('p', {class: 'note'}, 'A job is ready to build 45 minutes after its last scan, so breaks and lunches are not taken as the end of the shift. Built reports are checked like uploads; nothing is sent until someone approves it.'),
        unwatched.length ? h('div', {class: 'clientwatch'}, h('span', {}, `${client.client} has ${unwatched.length} job${unwatched.length === 1 ? '' : 's'} on SharePoint you are not watching: ${unwatched.slice(0, 6).map(j => j.job).join(', ')}${unwatched.length > 6 ? '…' : ''}`),
          h('button', {type: 'button', class: 'btn small', onclick: async e => { e.target.disabled = true; e.target.textContent = 'Adding…';
            for (const j of unwatched) if (!await load({add: j.job})) return;
            draw(); fetchScans(); }}, `Watch ${unwatched.length === 1 ? 'it' : `all ${unwatched.length}`}`)) : null,
        h('div', {class: 'numbers five'},
          h('div', {}, h('b', {}, fmt(rows.length)), h('span', {}, 'Jobs')),
          h('div', {}, h('b', {class: counts.missing ? 'bad' : ''}, fmt(counts.missing)), h('span', {}, 'Yesterday’s reports missing')),
          h('div', {}, h('b', {class: counts.hold ? 'bad' : ''}, fmt(counts.hold)), h('span', {}, 'On Hold')),
          h('div', {}, h('b', {}, fmt(counts.send)), h('span', {}, 'Still to send')),
          h('div', {}, h('b', {}, fmt(rows.reduce((n, r) => n + (sp[r.job]?.today || 0), 0))), h('span', {}, 'Scans today so far'))),
        h('section', {class: 'block'}, h('header', {}, h('h2', {}, 'Jobs')),
          rows.length ? h('div', {class: 'tablewrap'}, h('table', {class: 'keys pdays'},
            h('thead', {}, h('tr', {}, ['Job', 'Yesterday’s report', 'Today so far', 'Today’s report', ''].map(x => h('th', {}, x)))),
            h('tbody', {}, rows.map(r => {
              const s = sp[r.job];
              const today = !data.sharepoint_on ? h('span', {class: 'quiet'}, 'SharePoint off') : s === undefined ? h('span', {class: 'quiet'}, h('span', {class: 'spin'}), ' checking…')
                : s?.error ? h('span', {class: 'quiet', title: s.error}, 'Could not read SharePoint') : !s?.found ? h('span', {class: 'quiet'}, 'No “Data #” list found')
                : s.today ? h('span', {}, h('b', {}, fmt(s.today)), ' scans', s.last ? ` · last ${new Date(s.last).toLocaleTimeString('en-US', {hour: 'numeric', minute: '2-digit', timeZone: 'America/Chicago'})}` : '',
                    Object.keys(s.shifts || {}).length ? h('span', {class: 'quiet'}, ' · ' + Object.entries(s.shifts).map(([k, v]) => `${k} ${fmt(v)}`).join(' · ')) : null)
                : h('span', {class: 'quiet'}, 'No scans yet');
              return h('tr', {},
                h('td', {}, h('b', {}, r.job)),
                repCell(r.yesterday, s?.yesterday, 'yesterday', r.job),
                h('td', {}, today),
                repCell(r.today, 0, 'today', r.job),
                h('td', {}, h('button', {type: 'button', class: 'btn small', onclick: () => { state.periodQuery = {kind: 'week', day, job: r.job}; location.hash = 'periods'; }}, 'Week'),
                  r.watched ? h('button', {type: 'button', class: 'btn ghost small', onclick: async () => { if (await load({remove: r.job})) draw(); }}, 'Stop watching') : null));
            })))) : h('p', {class: 'note'}, 'No jobs yet. Upload a daily report, or add a job to watch below.'),
          h('div', {class: 'keyform addjob'}, addBox, h('button', {type: 'button', class: 'btn', onclick: async () => { if (!addBox.value.trim()) { addBox.focus(); return toast('Type a job number to watch, for example HANDS-HMMA-2219.', true); } if (await load({add: addBox.value})) { draw(); fetchScans(); } }}, 'Watch job'))));
    };
    const fetchScans = async () => {
      if (!data.sharepoint_on) return;
      const queue = data.jobs.map(r => r.job).filter(j => sp[j] === undefined);
      const worker = async () => { while (queue.length) { const j = queue.shift(); try { sp[j] = (await api('/api/today/job', {job: j, day: data.day})).sharepoint; } catch (e) { sp[j] = {error: e.message}; } if (location.hash === '#today') draw(); } };
      await Promise.all([worker(), worker(), worker()]);
    };
    draw(); fetchScans();
  }

  // ---------------- scan timeline from SharePoint (internal: never part of what the client receives)
  function timelineBlock(r) {
    const t = (r.sharepoint || {}).timeline;
    if (!t || !t.buckets?.length) return null;
    const at = iso => new Date(iso);
    const hm = iso => at(iso).toLocaleTimeString('en-US', {hour: 'numeric', minute: '2-digit', timeZone: 'America/Chicago'});
    const t0 = at(t.buckets[0].at).getTime(), t1 = at(t.buckets[t.buckets.length - 1].at).getTime() + 30 * 60000;
    const span = Math.max(t1 - t0, 3600000), W = 1000, H = 150, top = Math.max(...t.buckets.map(b => b.scans));
    const xOf = ms => 40 + (ms - t0) / span * (W - 60);
    const svgNS = 'http://www.w3.org/2000/svg';
    const el = (tag, attrs, text) => { const n = document.createElementNS(svgNS, tag); for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v); if (text != null) n.textContent = text; return n; };
    const svg = el('svg', {viewBox: `0 0 ${W} ${H + 30}`, width: '100%', role: 'img', 'aria-label': 'Scans per half hour'});
    const colors = ['#e6f2ec', '#eef3f8', '#f7f1e3', '#f3eaf3'];
    (t.shifts || []).forEach((s, i) => {
      const a = Math.max(t0, at(s.first).getTime() - 15 * 60000), b = Math.min(t1, at(s.last).getTime() + 15 * 60000);
      svg.append(el('rect', {x: xOf(a), y: 0, width: Math.max(4, xOf(b) - xOf(a)), height: H, fill: colors[i % 4], rx: 4}));
      svg.append(el('text', {x: xOf(a) + 6, y: 14, 'font-size': 12, fill: '#55706b', 'font-weight': 600}, `${s.name} shift · ${s.scans} scans`));
    });
    const bw = (W - 60) * 30 * 60000 / span;
    for (const b of t.buckets) {
      const h = b.scans / top * (H - 28), x = xOf(at(b.at).getTime());
      svg.append(el('rect', {x: x + 1, y: H - h, width: Math.max(2, bw - 2), height: h, fill: '#20594d', rx: 2}, null));
    }
    for (const g of t.gaps || []) svg.append(el('rect', {x: xOf(at(g.start).getTime()), y: H - 4, width: Math.max(2, xOf(at(g.end).getTime()) - xOf(at(g.start).getTime())), height: 4, fill: '#c0392b'}));
    for (let ms = Math.ceil(t0 / 3600000) * 3600000; ms <= t1; ms += 3600000 * Math.max(1, Math.ceil(span / 3600000 / 9)))
      svg.append(el('text', {x: xOf(ms), y: H + 20, 'font-size': 12, 'text-anchor': 'middle', fill: '#55706b'}, hm(new Date(ms).toISOString())));
    const people = t.people || [];
    return h('section', {class: 'block'},
      h('header', {}, h('h2', {}, 'Scan timeline'),
        h('p', {}, `${fmt(t.scans)} scans from ${hm(t.first)} to ${hm(t.last)} (plant time), by half hour. Red marks are gaps of 45 minutes or more. For your review only; never sent to the client.`)),
      h('div', {class: 'panel tlpanel'}, svg),
      t.flags.length ? h('ul', {class: 'pfind tlflags'}, t.flags.map(f => h('li', {class: 'medium'}, f))) : h('p', {class: 'note'}, 'No red flags: steady scanning inside the shift times.'),
      people.length ? h('details', {class: 'tlpeople'}, h('summary', {}, `Who scanned (${people.length})`),
        h('table', {class: 'keys'}, h('thead', {}, h('tr', {}, ['Name', 'Scans', 'First', 'Last', 'Shifts'].map(x => h('th', {}, x)))),
          h('tbody', {}, people.map(p => h('tr', {}, h('td', {}, p.name), h('td', {}, fmt(p.scans)), h('td', {}, hm(p.first)), h('td', {}, hm(p.last)), h('td', {}, p.shifts.join(', '))))))) : null);
  }

  // ---------------- weekly/monthly and overview dashboards and slide shows: built from the data, changed by talking to Claude, downloadable
  async function openArtifact(ctx, rebuild) {
    let dlg = $('dash-dialog');
    if (!dlg) { dlg = h('dialog', {id: 'dash-dialog', class: 'walk dashdlg', 'aria-labelledby': 'dash-title'}); document.body.append(dlg);
      dlg.addEventListener('close', () => { state.dashFrame = null; }); }
    if (!dlg.open) dlg.showModal();
    if (!state.dash || state.dash.ctx.id !== ctx.id || rebuild) {
      state.dashFrame = null;
      dlg.replaceChildren(h('div', {class: 'fixer-wait'}, h('span', {class: 'spin'}), h('h2', {id: 'dash-title'}, rebuild ? 'Starting over from the data…' : `Building your ${ctx.noun}…`)));
      try { state.dash = {ctx, ...(await ctx.create(!!rebuild))}; }
      catch (e) { dlg.replaceChildren(h('div', {class: 'fixer-wait'}, h('h2', {id: 'dash-title'}, ctx.noun), h('p', {class: 'note'}, e.message),
        h('div', {class: 'row'}, h('button', {type: 'button', class: 'btn', onclick: () => dlg.close()}, 'Close')))); return; }
    }
    drawDash();
  }
  function drawDash() {
    const dlg = $('dash-dialog'), d = state.dash; if (!dlg?.open || !d) return;
    const ctx = d.ctx;
    if (!state.dashFrame) state.dashFrame = h('iframe', {class: 'dashframe', title: ctx.noun + ' preview', sandbox: 'allow-scripts allow-popups allow-modals'});
    if (state.dashFrame.getAttribute('src') !== d.view) state.dashFrame.setAttribute('src', d.view);
    const box = h('textarea', {rows: 3, placeholder: ctx.noun === 'slide show' ? 'Say what to change, e.g. “Add a slide comparing the two shifts”' : 'Say what to change, e.g. “Show the reject rate by shift as a donut chart”',
      'aria-label': 'Change to make', disabled: !d.ai_on || state.dashBusy});
    const send = text => { text = (text || '').trim(); if (text) sendDash(text); };
    box.addEventListener('keydown', e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(box.value); } });
    const talk = d.messages.concat(state.dashPending ? [{role: 'user', text: state.dashPending}] : []);
    const log = h('div', {class: 'dashlog'},
      h('p', {class: 'dashintro'}, `Built from the report data. Ask for any change in plain words; Claude uses only these numbers and leaves out inspector names and scan times.${ctx.noun === 'slide show' ? ' Use the arrow keys or click to move between slides.' : ''}`),
      talk.length ? null : h('div', {class: 'dashideas'}, ctx.ideas.map(t => h('button', {type: 'button', class: 'chip', disabled: !d.ai_on, onclick: () => send(t)}, t))),
      ...talk.map(m => h('div', {class: 'dmsg ' + m.role}, m.text)),
      state.dashBusy ? h('div', {class: 'dmsg assistant wait'}, h('span', {class: 'spin'}), ` Redesigning the ${ctx.noun}. This can take a minute.`) : null);
    dlg.replaceChildren(
      h('div', {class: 'fixer-top'}, h('div', {}, h('p', {class: 'eyebrow'}, ctx.noun.toUpperCase()), h('h2', {id: 'dash-title'}, ctx.title)),
        h('a', {class: 'btn', href: d.download, download: ''}, 'Download HTML'),
        h('a', {class: 'btn', href: d.view, target: '_blank', rel: 'noopener'}, ctx.noun === 'slide show' ? 'Present in a new tab' : 'Open in a new tab'),
        d.undo ? h('button', {type: 'button', class: 'btn', disabled: state.dashBusy, onclick: undoDash}, 'Undo last change') : null,
        h('button', {type: 'button', class: 'btn', disabled: state.dashBusy, onclick: e => {
          if (e.target.dataset.sure) { openArtifact(ctx, true); return; }
          e.target.dataset.sure = '1'; e.target.textContent = 'Sure? Your changes go'; setTimeout(() => { if (e.target.isConnected) { delete e.target.dataset.sure; e.target.textContent = 'Start over'; } }, 4000); }}, 'Start over'),
        h('button', {type: 'button', class: 'btn', onclick: () => dlg.close()}, 'Close')),
      h('div', {class: 'dashbody'}, h('div', {class: 'dashview'}, state.dashFrame),
        h('aside', {class: 'dashchat'}, log,
          d.ai_on ? null : h('p', {class: 'ssource warn'}, `Changes by Claude need AI review turned on. You can still view and download this ${ctx.noun}.`),
          h('div', {class: 'dashsend'}, box, h('button', {type: 'button', class: 'btn primary', disabled: !d.ai_on || state.dashBusy, onclick: () => send(box.value)}, 'Send')))));
    log.scrollTop = log.scrollHeight;
    if (!state.dashBusy && d.ai_on) box.focus();
  }
  async function sendDash(text) {
    const ctx = state.dash.ctx;
    state.dashBusy = true; state.dashPending = text; drawDash();
    try { const out = await ctx.chat(text); if (state.dash?.ctx.id === ctx.id) state.dash = {ctx, ...out}; }
    catch (e) { toast(e.message, true); }
    finally { state.dashBusy = false; state.dashPending = null; drawDash(); if (ctx.after) ctx.after(); }
  }
  async function undoDash() {
    const ctx = state.dash.ctx;
    try { state.dash = {ctx, ...(await ctx.undo())}; drawDash(); } catch (e) { toast(e.message, true); }
  }

  // ---------------- weekly & monthly: the period's daily reports and SharePoint scans, its checks, dashboard and slide show
  async function showPeriods() {
    const wrap = h('div', {class: 'sheetwrap apiwrap'});
    $('report').replaceChildren(wrap);
    const latest = state.list.map(r => r.review?.day).filter(Boolean).sort().pop();   // open on the newest week that has reports, not an empty one
    const q = state.periodQuery ||= store.get('rd-period', {kind: 'week', day: latest || new Date().toISOString().slice(0, 10), job: ''});
    wrap.replaceChildren(h('p', {class: 'note'}, h('span', {class: 'spin'}), ' Gathering the daily reports and SharePoint scans…'));
    let data;
    try { data = state.periodAsk ? state.periodAsk : await api('/api/periods', q); state.periodAsk = null; } catch (e) { wrap.replaceChildren(h('p', {class: 'note'}, e.message)); return; }
    if (location.hash !== '#periods') return;
    q.job = data.period.job; store.set('rd-period', q);
    const p = data.period, t = p.totals;
    const move = n => { const d = new Date(p.start + 'T12:00:00'); if (q.kind === 'week') d.setDate(d.getDate() + 7 * n); else d.setMonth(d.getMonth() + n); q.day = d.toISOString().slice(0, 10); showPeriods(); };
    const ctx = what => ({id: `p:${p.job}:${q.kind}:${p.start}:${what}`, noun: what === 'slides' ? 'slide show' : 'dashboard', title: `${p.job}, ${p.label}`,
      ideas: what === 'slides' ? ['Add a slide with the three worst days', 'Make it 5 slides at most', 'Add a highlights slide at the start', 'Use a white theme'] :
        ['Add a reject rate line to the daily chart', 'One clean page for printing', 'Show the weekdays side by side', 'White background for a client presentation'],
      create: rebuild => api('/api/periods/artifact', {...q, what, rebuild}), chat: message => api('/api/periods/artifact/chat', {...q, what, message}),
      undo: () => api('/api/periods/artifact/undo', {...q, what})});
    const num = v => v == null ? '—' : fmt(v);
    const pair = (a, b) => a == null && b != null ? h('td', {class: 'num'}, h('span', {class: 'sponly', title: 'From SharePoint (no daily report)'}, num(b)))
      : h('td', {class: 'num'}, num(a), b != null && a != null && Math.abs(a - b) > 0.0001 ? h('span', {class: 'differs', title: 'SharePoint shows ' + num(b)}, ` / ${num(b)}`) : null);
    const file = h('input', {type: 'file', accept: '.xlsx,.csv', hidden: true, onchange: async e => {
      const f = e.target.files[0]; if (!f) return;
      toast(`Checking ${f.name}…`);
      try {
        const id = crypto.randomUUID().replace(/-/g, ''), parts = Math.max(1, Math.ceil(f.size / CHUNK));   // big files go up in parts
        for (let i = 0; i < parts; i++) await api('/api/spreadsheets/upload/part', {upload_id: id, index: i, content_base64: await toBase64(f.slice(i * CHUNK, (i + 1) * CHUNK))});
        await api('/api/periods/check-file', {...q, filename: f.name, upload_id: id, part_count: parts}); showPeriods();
      } catch (err) { toast(err.message, true); } }});
    const fc = data.file_check;
    wrap.replaceChildren(...[
      h('button', {type: 'button', class: 'back', onclick: () => { location.hash = ''; }}, '‹ All reports'),
      h('div', {class: 'head'}, h('div', {}, h('h1', {}, 'Weekly & monthly'), h('div', {class: 'meta'}, 'A job’s week or month: every daily report and SharePoint scan, checked together, with a dashboard and slide show you can shape by talking to Claude.'))),
      h('div', {class: 'periodbar'},
        h('select', {'aria-label': 'Job', onchange: e => { q.job = e.target.value; showPeriods(); }}, data.jobs.map(j => h('option', {value: j, selected: j === p.job}, j))),
        h('div', {class: 'pseg'}, ['week', 'month'].map(k => h('button', {type: 'button', class: q.kind === k ? 'on' : '', onclick: () => { q.kind = k; showPeriods(); }}, k === 'week' ? 'Week' : 'Month'))),
        h('div', {class: 'pnav'}, h('button', {type: 'button', class: 'btn small', 'aria-label': 'Previous', onclick: () => move(-1)}, '‹'), h('b', {}, p.label), h('button', {type: 'button', class: 'btn small', 'aria-label': 'Next', onclick: () => move(1)}, '›'))),
      h('div', {class: 'numbers five'},
        h('div', {}, h('b', {}, num(t.days_worked)), h('span', {}, 'Days worked')),
        h('div', {}, h('b', {}, num(t.inspected)), h('span', {}, 'Parts inspected')),
        h('div', {}, h('b', {}, num(t.rejected)), h('span', {}, 'Rejected')),
        h('div', {}, h('b', {}, t.rate == null ? '—' : t.rate.toFixed(1) + '%'), h('span', {}, 'Reject rate')),
        h('div', {}, h('b', {}, `${t.sent} of ${t.reports}`), h('span', {}, 'Daily reports sent'))),
      recsBlock(data.recs, {scope: `${p.job}'s ${q.kind}`, ask: async () => { const n = await api('/api/periods', {...q, ask_ai: true}); if (location.hash === '#periods') { state.periodAsk = n; showPeriods(); } },
        act: rec => periodAct(rec, () => document.querySelector('.pdays')?.scrollIntoView({behavior: 'smooth'}))}),
      h('div', {class: 'periodacts'},
        h('button', {type: 'button', class: 'btn primary', onclick: () => openArtifact(ctx('dashboard'))}, data.made.dashboard ? 'Open dashboard' : 'Create dashboard'),
        h('button', {type: 'button', class: 'btn primary', onclick: () => openArtifact(ctx('slides'))}, data.made.slides ? 'Open slide show' : 'Create slide show'),
        h('button', {type: 'button', class: 'btn', onclick: () => file.click()}, `Check a ${q.kind === 'week' ? 'weekly' : 'monthly'} report file`), file),
      fc ? h('section', {class: 'block'}, h('header', {}, h('h2', {}, `${fc.filename}`),
          h('p', {}, `${fc.status === 'clear' ? 'Matches the daily reports and SharePoint.' : `${fc.findings.length} thing${fc.findings.length === 1 ? '' : 's'} to look at.`} ${fmt(fc.rows)} rows, ${fmt(fc.inspected)} parts inspected, checked ${ago(fc.checked_at)}.`)),
        fc.findings.length ? h('ul', {class: 'pfind'}, fc.findings.map(f => h('li', {class: f.severity}, f.day ? h('span', {class: 'pday'}, shortDay(f.day)) : null, f.text))) : null) : null,
      h('section', {class: 'block'}, h('header', {}, h('h2', {}, 'Checks for the ' + q.kind),
          h('p', {}, p.findings.length ? `${p.findings.length} thing${p.findings.length === 1 ? '' : 's'} to look at.` : 'Nothing to look at: every day has a clean, sent report that matches SharePoint.')),
        p.findings.length ? h('ul', {class: 'pfind'}, p.findings.map(f => h('li', {class: f.severity}, f.day ? h('span', {class: 'pday'}, shortDay(f.day)) : null, f.text))) : null,
        data.sharepoint_on ? h('p', {class: 'note'}, `SharePoint numbers from ${p.sharepoint_source}.`) : h('p', {class: 'note'}, 'SharePoint is not connected, so days are checked from the daily reports only.')),
      h('section', {class: 'block'}, h('header', {}, h('h2', {}, 'Day by day'), h('p', {}, 'Report numbers first; a red number after a slash is what SharePoint shows when it differs.')),
        h('div', {class: 'tablewrap'}, h('table', {class: 'keys pdays'},
          h('thead', {}, h('tr', {}, ['Day', 'Daily report', 'Cars / rows', 'Inspected', 'Rejected', 'Sent'].map(x => h('th', {}, x)))),
          h('tbody', {}, p.days.map(x => {
            const rep = x.report, sp = x.sharepoint || {};
            return h('tr', {class: rep || x.sharepoint ? '' : 'quiet'},
              h('td', {}, shortDay(x.day)),
              h('td', {}, rep ? h('a', {href: '#r=' + rep.file_hash}, h('span', {class: 'tag tag-' + rep.status}, STATUS[rep.status]), ' ', rep.filename) : (x.sharepoint ? h('span', {class: 'differs'}, 'Missing') : '—')),
              pair(rep?.scans, sp.scans), pair(rep?.inspected, sp.inspected), pair(rep?.rejected, sp.rejected),
              h('td', {}, rep ? (rep.sent_at ? 'Sent ' + when(rep.sent_at) : rep.approved ? 'Approved' : 'No') : ''));
          })))))].filter(Boolean));
  }

  // ---------------- Checks & shifts page: what is checked, the reviewer's extra AI rules, and shift times
  async function showChecks() {
    const wrap = h('div', {class: 'sheetwrap apiwrap'});
    $('report').replaceChildren(wrap);
    let data;
    try { data = await api('/api/settings', {}); } catch (e) { wrap.replaceChildren(h('p', {class: 'note'}, e.message)); return; }
    const save = async (path, body, done) => {
      try { data = await api(path, body); draw(); if (done) toast(done(data)); } catch (e) { toast(e.message, true); }
    };
    const shiftEditor = (list, onSave, extra) => {
      let rows = list.map(x => ({...x}));
      const box = h('div', {class: 'shiftedit'});
      const paint = () => {
        box.replaceChildren(
          h('div', {class: 'shiftrows'}, rows.map((r, i) => h('div', {class: 'shiftrow'},
            h('input', {type: 'text', value: r.name, 'aria-label': 'Shift name', maxlength: '20', oninput: e => { r.name = e.target.value; }}),
            h('label', {}, 'Starts', h('input', {type: 'time', value: r.start, oninput: e => { r.start = e.target.value; }})),
            h('label', {}, 'Ends', h('input', {type: 'time', value: r.end, oninput: e => { r.end = e.target.value; }})),
            rows.length > 1 ? h('button', {type: 'button', class: 'btn ghost small', onclick: () => { rows.splice(i, 1); paint(); }}, 'Remove') : null))),
          h('div', {class: 'shiftbtns'},
            rows.length < 4 ? h('button', {type: 'button', class: 'btn small', onclick: () => {
              const mins = t => { const [a, b] = String(t || '').split(':').map(Number); return a * 60 + b; }, cov = new Array(1440).fill(false);
              for (const r of rows) { const a = mins(r.start), b = mins(r.end); if (isNaN(a) || isNaN(b)) continue; for (let t = a; t !== b; t = (t + 1) % 1440) cov[t] = true; }
              if (cov.every(Boolean)) return toast('These shifts already cover the whole day. Change when one of them ends first, then add a shift for the time that frees up.', true);
              const last = rows[rows.length - 1]; rows.push({name: ['1st', '2nd', '3rd', '4th'][rows.length], start: last.end, end: rows[0].start}); paint(); }}, 'Add a shift') : null,
            extra || null,
            h('button', {type: 'button', class: 'btn primary small', onclick: () => onSave(rows)}, 'Save shift times')));
      };
      paint();
      return box;
    };
    const moved = d => `Saved. ${d.rechecked ? `${d.rechecked} report${d.rechecked === 1 ? '' : 's'} checked again with the new times` : 'New uploads use the new times'}${d.moved ? `; ${d.moved} row${d.moved === 1 ? ' is' : 's are'} on a different shift than in the report. Open the report to move them.` : '.'}`;
    const spSlot = spJobsSection();
    const draw = () => {
      const ruleText = h('input', {type: 'text', placeholder: 'e.g. Flag any VIN that does not start with HZA', 'aria-label': 'What Claude should look for', maxlength: '400'});
      const ruleJob = h('select', {'aria-label': 'Which jobs'}, h('option', {value: ''}, 'All jobs'), data.jobs.map(j => h('option', {value: j}, j)));
      const jobsWithOwn = Object.keys(data.shifts.jobs || {});
      const newJob = h('input', {type: 'text', list: 'job-list', placeholder: 'Job number, e.g. HANDS-HMMA-2219', 'aria-label': 'Job number'});
      wrap.replaceChildren(
        h('button', {type: 'button', class: 'back', onclick: () => { location.hash = ''; }}, '‹ All reports'),
        h('div', {class: 'head'}, h('div', {}, h('h1', {}, 'Checks & shifts'), h('div', {class: 'meta'}, 'What the verifier looks at, what else you want Claude to look for, and when each shift starts and ends.'))),
        h('section', {class: 'block'}, h('header', {}, h('h2', {}, 'What it checks')),
          h('div', {class: 'checkgroups'}, data.checks.map(g => h('div', {class: 'checkgroup'}, h('h3', {}, g.title), h('p', {class: 'note'}, g.when),
            h('ul', {}, g.items.map(t => h('li', {}, t))))))),
        h('section', {class: 'block'}, h('header', {}, h('h2', {}, 'Also look for'),
            h('p', {}, 'Tell Claude anything else to check. It reads this list every time it reviews a report, and its double-check will not dismiss what you asked for.')),
          data.ai_on ? null : h('p', {class: 'ssource warn'}, 'AI review is off, so these rules are saved but not used until it is turned on.'),
          h('div', {class: 'keyform'}, ruleText, ruleJob,
            h('button', {type: 'button', class: 'btn primary', onclick: () => save('/api/settings/rules', {text: ruleText.value, job: ruleJob.value || null}, () => 'Added. New AI reviews will look for it.')}, 'Add')),
          data.rules.length ? h('ul', {class: 'rules'}, data.rules.map(r => h('li', {}, h('span', {class: 'rjob'}, r.job || 'All jobs'), h('span', {class: 'rtext'}, r.text),
            h('button', {type: 'button', class: 'btn ghost small', onclick: () => save('/api/settings/rules', {remove: r.id}, () => 'Removed. New AI reviews will not look for it.')}, 'Remove'))))
            : h('p', {class: 'note'}, 'Nothing added yet.'),
          h('p', {class: 'note'}, 'To use a new rule on a report that was already checked, open it and click Run AI review again.')),
        h('section', {class: 'block'}, h('header', {}, h('h2', {}, 'Shift times'),
            h('p', {}, 'Plant time (Central). Each scan goes on the shift its scan time falls in, not the shift the inspector picked. A shift that runs past midnight counts toward the day it started. Saving checks recent reports again, so every scan lands in the right shift.')),
          h('h3', {class: 'subh'}, 'All jobs'),
          shiftEditor(data.shifts.default, rows => save('/api/settings/shifts', {shifts: rows}, moved)),
          ...jobsWithOwn.map(job => h('div', {class: 'jobshifts'}, h('h3', {class: 'subh'}, job),
            shiftEditor(data.shifts.jobs[job], rows => save('/api/settings/shifts', {job, shifts: rows}, moved),
              h('button', {type: 'button', class: 'btn ghost small', onclick: () => save('/api/settings/shifts', {job, remove: true}, moved)}, 'Use the all-jobs times')))),
          h('div', {class: 'keyform addjob'}, newJob, h('datalist', {id: 'job-list'}, data.jobs.filter(j => !jobsWithOwn.includes(j)).map(j => h('option', {value: j}))),
            h('button', {type: 'button', class: 'btn', onclick: () => { const j = newJob.value.trim().toUpperCase(); if (!j) { newJob.focus(); toast('Type or pick a job first, then click Set different times for a job.'); return; }
              data.shifts.jobs = {...data.shifts.jobs, [j]: data.shifts.default.map(x => ({...x}))}; draw(); toast(`Set ${j}'s times below, then save.`); }}, 'Set different times for a job'))));
      wrap.append(spSlot);
    };
    draw();
  }

  // a report with rows on the wrong shift: one click puts them on the right tab
  function shiftCard(rv) {
    const wrong = allIssues(rv).filter(i => i.code === 'wrong_shift');
    if (!wrong.length) return null;
    return h('div', {class: 'shiftcard'},
      h('div', {}, h('b', {}, `${wrong.length} row${wrong.length === 1 ? ' is' : 's are'} on the wrong shift by scan time`),
        h('span', {}, wrong.slice(0, 2).map(i => i.text).join(' ') + (wrong.length > 2 ? ` And ${wrong.length - 2} more.` : '')),
        h('span', {class: 'note'}, 'Rows move to the right shift tab. A row whose shift has no tab in this workbook is taken out (it belongs in that shift\'s report). You get a corrected copy; the original stays as it is.')),
      h('button', {type: 'button', class: 'btn primary', onclick: async e => {
        e.target.disabled = true; e.target.textContent = 'Moving…';
        try {
          const out = await api('/api/spreadsheets/shift-fix', {file_hash: state.report.file_hash});
          toast(`Moved ${out.moved} row${out.moved === 1 ? '' : 's'}${out.removed ? ` and took out ${out.removed}` : ''}. This is the corrected copy; it has been checked again.`);
          await loadList(); select(out.item.file_hash);
        } catch (err) { toast(err.message, true); e.target.disabled = false; e.target.textContent = 'Put them on the right shift'; }
      }}, 'Put them on the right shift'));
  }

  // ---------------- API page: keys and how to call it
  async function showApi() {
    const report = $('report');
    const wrap = h('div', {class: 'sheetwrap apiwrap'});
    report.replaceChildren(wrap);
    const base = location.origin + '/api/v1';
    let index = {endpoints: []}, keys = [], fresh = null, link = null, bypassReady = true;
    try { let k; [index, k] = await Promise.all([api('/api/v1'), api('/api/keys', {})]); keys = k.keys; bypassReady = k.bypass_ready !== false; } catch (e) { toast(e.message, true); }
    const copyBtn = text => h('button', {type: 'button', class: 'btn small', onclick: e => { navigator.clipboard?.writeText(text).then(() => { e.target.textContent = 'Copied'; }, () => toast('Select it and copy it.', true)); }}, 'Copy');
    const connectorBlock = () => {
      const label = h('input', {type: 'text', value: 'Claude and ChatGPT', 'aria-label': 'Connector name', maxlength: '60'});
      const canSend = h('input', {type: 'checkbox'});
      return h('section', {class: 'block'}, h('header', {}, h('h2', {}, 'Claude and ChatGPT connector'),
          h('p', {}, 'Add the verifier to Claude or ChatGPT, then ask things like “Which reports need attention today?”, “What’s wrong with the HANDS report?” or “Apply the fixes and draft the client email.”')),
        bypassReady ? null : h('p', {class: 'ssource warn'}, 'First turn on “Protection Bypass for Automation” in Vercel (the project’s Settings, Deployment Protection) and redeploy. Until then Claude and ChatGPT can’t get past the Vercel sign-in, and the link below will not work.'),
        link ? h('div', {class: 'keynew'},
          h('p', {}, h('b', {}, 'Your connector link. Copy it now; it will not be shown again. Treat it like a password.')),
          h('div', {class: 'keyline'}, h('code', {}, link.url), copyBtn(link.url)),
          link.canSend ? h('p', {class: 'note'}, 'This link can send reports to clients. Claude and ChatGPT will still ask you before each send, and every send needs an approval.') : h('p', {class: 'note'}, 'This link can read reports, apply fixes and approve, but cannot send email to clients.')) : null,
        h('div', {class: 'keyform'}, label, h('label', {class: 'tick'}, canSend, 'Can send to clients'),
          h('button', {type: 'button', class: 'btn primary', onclick: async () => {
            try { const d = await api('/api/keys/create', {name: label.value, can_send: canSend.checked, connector: true}); keys = d.keys; fresh = null;
              link = {url: d.key.connector_url, canSend: d.key.can_send}; bypassReady = d.key.bypass_ready !== false; draw(); }
            catch (e) { toast(e.message, true); } }}, 'Make connector link')),
        h('div', {class: 'howto'},
          h('div', {}, h('h3', {}, 'In Claude'), h('ol', {}, h('li', {}, 'Settings, then Connectors, then “Add custom connector”.'), h('li', {}, 'Name it PPG Verifier and paste the link. Leave the advanced settings empty.'),
            h('li', {}, 'In a chat, turn PPG Verifier on from the tools menu.'), h('li', {}, 'On a Team or Enterprise plan an owner may need to add it for the organization first.'))),
          h('div', {}, h('h3', {}, 'In ChatGPT'), h('ol', {}, h('li', {}, 'Settings, then Apps & Connectors, then Advanced settings: turn on Developer mode.'), h('li', {}, 'Back in Apps & Connectors, choose Create.'),
            h('li', {}, 'Name it PPG Verifier, paste the link, set Authentication to “No authentication”, tick that you trust it, and create it.'), h('li', {}, 'In a chat, pick it from the + menu. Needs Plus, Pro, Business or Enterprise; on Business or Enterprise an admin may need to allow connectors first.')))),
        h('p', {class: 'note'}, 'Connector links appear in the key list below. Revoke one there and it stops working at once.'));
    };
    const draw = () => {
      const name = h('input', {type: 'text', placeholder: 'What will use it, e.g. Power Automate', 'aria-label': 'Key name', maxlength: '60'});
      const canSend = h('input', {type: 'checkbox'});
      wrap.replaceChildren(
        h('button', {type: 'button', class: 'back', onclick: () => { location.hash = ''; }}, '‹ All reports'),
        h('div', {class: 'head'}, h('div', {}, h('h1', {}, 'API'), h('div', {class: 'meta'}, 'Let other programs upload reports, read the results, apply fixes, approve and send, with the same rules as this screen.'))),
        connectorBlock(),
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
    dlg.replaceChildren(h('div', {class: 'fixer-wait'}, h('span', {class: 'spin'}), h('h2', {id: 'send-title'}, state.report.approval ? 'Send to client' : 'Approve'),
      h('p', {}, 'Finding the last report sent for this job in data@ppgquality.com…')));
    try {
      const info = await api('/api/spreadsheets/send-info', {file_hash: hash});
      if (state.report?.file_hash !== hash) return;
      state.sendInfo = info; drawSend();
    } catch (e) {
      dlg.replaceChildren(h('div', {class: 'fixer-wait'}, h('h2', {id: 'send-title'}, 'Approve'), h('p', {class: 'note'}, e.message),
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
      reply: info.previous ? 'reply' : 'new', again: false, override: false, approver: myName(), note: ''};
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
      const unsure = needsCheck().total;
      if (unsure) warns.push(h('div', {class: 'swarn'}, h('p', {}, `${unsure} item${unsure === 1 ? '' : 's'} the app is not sure about ${unsure === 1 ? 'has' : 'have'} not been checked yet. A person has to accept, skip or mark ${unsure === 1 ? 'it' : 'each one'} reviewed first.`),
        h('button', {type: 'button', class: 'btn small', onclick: () => { dlg.close(); showSide('fixes'); }}, 'Check them now')));
      if (newer) warns.push(h('div', {class: 'swarn'}, h('p', {}, `A corrected copy of this report exists: "${newer.fixed_filename}".`),
        h('button', {type: 'button', class: 'btn small', onclick: () => { state.openSendFor = newer.fixed_file_hash; dlg.close(); select(newer.fixed_file_hash); }}, 'Open the corrected copy')));
      if (must) warns.push(h('div', {class: 'swarn high'}, h('p', {}, `${must} must-fix issue${must === 1 ? ' is' : 's are'} still open on this report.`), check('override', 'Approve anyway')));
      step1 = h('div', {class: 'sstep'}, h('div', {class: 'shead'}, h('span', {class: 'snum'}, '1'), h('h3', {}, 'Approve the final report')),
        h('p', {}, `${rv.status === 'clear' ? 'Nothing is flagged.' : statusNote(rv) + '.'} The report goes to the client exactly as this version (${r.filename}, version ${r.version}).`),
        ...warns,
        h('div', {class: 'sgrid'}, h('label', {}, 'Approved by', bind('approver', {type: 'text', placeholder: 'Your name', autocomplete: 'name'})),
          h('label', {}, 'Note (optional)', bind('note', {type: 'text', placeholder: 'For example: checked against the app, 144 = 144'}))),
        h('button', {type: 'button', class: 'btn primary', disabled: !!state.sending || (must > 0 && !f.override) || (!!newer && !f.override) || pending > 0 || needsCheck().total > 0, onclick: () => approve(false)}, 'Approve as final'));
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
      state.report = {...state.report, ...out.item, approval: out.item.approval || null, review: state.report.review};
      toast(withdraw ? 'Approval withdrawn.' : 'Approved as final. You can send it to the client now.' + (out.change_summary ? ' It differed from SharePoint or was changed, so a change summary PDF was saved on record.' : ''));
      if (out.change_summary) state.report.change_summaries = [{id: out.change_summary.id, made_at: out.change_summary.made_at, by: out.change_summary.by, reason: 'approved'}, ...(state.report.change_summaries || [])];
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
  // Internal approval and sending to the client are separate steps; a report with blockers never looks ready.
  const blockers = r => (r.review?.counts?.high || 0) + (r.holds || []).filter(x => x.hard).length + (r.assignment?.needs_person && !(r.holds || []).some(x => x.hard) ? 1 : 0);
  const sendLabel = r => (r.sent || []).some(x => x.sent) ? 'Sent to client' : r.approval ? 'Send to client…'
    : blockers(r) ? `${blockers(r)} blocker${blockers(r) === 1 ? '' : 's'} to resolve` : 'Approve…';

  const sameKey = i => (i.code || '') + '|' + (i.column || (String(i.cell || '').match(/^[A-Z]+/i) || [''])[0].toUpperCase()) + '|' + String(i.text || '').replace(/"[^"]*"|“[^”]*”|'[^']*'/g, '"…"').replace(/\b[A-Za-z0-9-]*\d[A-Za-z0-9-]*\b/g, '#').replace(/\s+/g, ' ').trim();
  const walkIssues = () => {
    const list = allIssues(state.report.review).filter(i => i.severity !== 'info');
    const rank = {high: 0, medium: 1, low: 2}, size = {};
    for (const i of list) { const k = i.severity + '|' + issueType(i); size[k] = (size[k] || 0) + 1; }
    return list.map((i, n) => ({i, n})).sort((a, b) => (rank[a.i.severity] ?? 3) - (rank[b.i.severity] ?? 3)
      || size[b.i.severity + '|' + issueType(b.i)] - size[a.i.severity + '|' + issueType(a.i)]
      || issueType(a.i).localeCompare(issueType(b.i)) || sameKey(a.i).localeCompare(sameKey(b.i)) || a.n - b.n).map(x => x.i);
  };
  // a person said these are fine: saved on the report (not only in this browser), so later versions keep them settled
  async function markFine(items) {
    if (!items.length || !state.report) return;
    try { await api('/api/spreadsheets/mark-fine', {file_hash: state.report.file_hash, by: myName() || null,
      issues: items.map(i => ({code: i.code || null, sheet: i.sheet || null, row: i.row ?? null, text: i.text || ''}))}); }
    catch (e) { /* still marked reviewed in this browser */ }
  }
  function fixesFor(issue) {
    const dec = store.get(decisionsKey(), {});
    const rowNo = Number(issue.row) || splitCell(issue.cell)?.[1];
    // an issue about a whole row only gets a fix that is about it: one that changes the value it names, or fills a blank it reports
    const aboutIt = f => issue.cell || fixCovers(f, issue) || (String(f.before ?? '').trim() === '' && /blank|missing|empty/i.test(`${issue.code || ''} ${issue.text || ''}`));
    return (state.fixes || []).filter(f => f.sheet === (issue.sheet || f.sheet) && (issue.cell ? f.cell === String(issue.cell).toUpperCase() : rowNo && splitCell(f.cell)?.[1] === rowNo) && aboutIt(f))
      .map(f => ({...f, after: dec[f.id]?.after ?? f.after, state: dec[f.id]?.state}));
  }
  // Every error ends in a click: accept the change, take out the extra row, let Claude write a fix, or say it is fine.
  function fixChoices(issue, next) {
    const fx = fixesFor(issue).filter(f => f.state !== 'rejected');
    const decideFix = (f, st) => { const d = store.get(decisionsKey(), {}); d[f.id] = {state: st, after: f.after}; store.set(decisionsKey(), d);
      if (st === 'accepted') {   // the change settles every issue on that cell (a typo is often flagged twice: wrong format and likely typo)
        const done = reviewedSet(); walkIssues().filter(i => i.key !== issue.key && fixCovers(f, i)).forEach(i => done.add(i.key));
        store.set('rd-reviewed:' + state.report.file_hash, [...done]); }
      drawFixes(); if (st === 'accepted' || st === 'rejected') next(); };
    const show = v => v === '' || v == null ? '(blank)' : `“${v}”`;
    const kids = [];
    for (const f of fx) kids.push(h('div', {class: 'choice'},
      h('span', {}, f.state === 'accepted' ? '✓ Will change ' : 'Change ', h('b', {}, `${f.sheet} ${f.cell}`), ` from ${show(f.before)} to ${show(f.after)}`),
      f.state === 'accepted' ? h('button', {type: 'button', class: 'btn small', onclick: () => decideFix(f, undefined)}, 'Undo')
        : h('span', {class: 'cbtns'}, h('button', {type: 'button', class: 'btn primary small', onclick: () => decideFix(f, 'accepted')}, 'Make this change'),
          h('button', {type: 'button', class: 'btn small', onclick: () => decideFix(f, 'rejected')}, 'Skip'))));
    if (!fx.length && isDupRow(issue) && issue.row) kids.push(h('div', {class: 'choice'},
      h('span', {}, `Row ${issue.row} is an exact copy of another row.`),
      h('button', {type: 'button', class: 'btn primary small', onclick: () => removeRows([[issue.sheet, Number(issue.row)]], `Took out the extra copy (${issue.sheet} row ${issue.row})`)}, 'Take out this extra row')));
    if (!fx.length && issue.code === 'duplicate_identifier' && issue.row) kids.push(h('div', {class: 'choice'},
      h('span', {}, 'If this row is the same part scanned twice:'),
      h('button', {type: 'button', class: 'btn small', onclick: () => removeRows([[issue.sheet, Number(issue.row)]], `Took out ${issue.sheet} row ${issue.row}`)}, 'Take out this row')));
    if (!fx.length && !isDupRow(issue) && state.aiOn) kids.push(h('div', {class: 'choice'},
      h('span', {}, 'No automatic fix for this one yet.'),
      state.aiOn ? h('button', {type: 'button', class: 'btn small', onclick: e => aiFix([issue.key], e.target)}, 'Fix with AI') : null));
    return h('div', {class: 'choices'}, h('b', {class: 'choices-h'}, 'How to fix it'), ...kids,
      h('div', {class: 'choice'}, h('span', {}, kids.length ? 'Or, if the value is actually right:' : 'If the value is actually right:'),
        h('button', {type: 'button', class: 'btn small', onclick: async () => { await markFine([issue]); next(); }}, 'It\'s fine as is')));
  }
  function cellPanel(m, underIssue) {
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
        underIssue ? null : h('button', {type: 'button', class: 'btn ghost small', onclick: () => { state.gridCell = null; drawGrid(); }}, 'Close')),
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
  const ISSUE_TYPES = [
    ['Scans on the wrong shift', /wrong_shift/],
    ['Does not match SharePoint', /sharepoint|not_on_sharepoint|missing_from_report|differs_from|unmatched_report_row/],
    ['Duplicates', /duplicate/],
    ['Typos and bad VINs or part numbers', /typo|vin_|identifier_unusual|fill_series|leading_zeros/],
    ['Totals that do not add up', /total|summary|mismatch|exceeds|reject_log/],
    ['Blank or missing values', /missing|blank|placeholder/],
    ['Numbers stored as text or bad values', /text|numeric|spaces|fraction|negative|scientific|excel_error|hidden|garbled|wrong_column/],
    ['Wrong or future dates', /date/],
    ['Already reported on another day', /other_day/],
    ['Different from the usual day', /unusual|baseline/],
  ];
  const SEVERITIES = [['high', 'Must fix'], ['medium', 'Check'], ['low', 'Minor']];
  function issueType(i) {
    if (i.source === 'AI review') return 'Found by AI review';
    const code = i.code || '';
    for (const [name, re] of ISSUE_TYPES) if (re.test(code)) return name;
    return i.source === 'Usual day' ? 'Different from the usual day' : 'Other checks';
  }
  // Issues grouped by severity, then by type; click one to see it in the sheet.
  // A short summary when a report has several errors: what kind they are, how many matter, where they are.
  function errorSummary(issues, done) {
    const open = issues.filter(i => !done.has(i.key));
    const types = new Set(open.map(issueType));
    if (open.length < 4 && types.size < 3) return null;
    const plural = (n, w, ws) => `${n} ${n === 1 ? w : ws || w + 's'}`;
    const writing = open.filter(i => i.only), real = open.filter(i => !i.only);
    const realSure = real.filter(i => i.sure !== false), realUnsure = real.filter(i => i.sure === false);
    const sheets = {}; for (const i of open) if (i.sheet) sheets[i.sheet] = (sheets[i.sheet] || 0) + 1;
    const topSheet = Object.entries(sheets).sort((a, b) => b[1] - a[1])[0];
    const byType = {}; for (const i of open) { const t = issueType(i); (byType[t] ||= {n: 0, only: 0, high: 0}); byType[t].n++; if (i.only) byType[t].only++; if (i.severity === 'high') byType[t].high++; }
    const clip = t => (t || '').length > 110 ? t.slice(0, 107) + '…' : t || '';
    // each line knows which errors it is about, so clicking it walks through exactly those in the spreadsheet
    const walk = (items, label) => () => {
      if (!items.length) return;
      state.groupWalk = {keys: new Set(items.map(i => i.key)), label};
      focusIssue(issues.indexOf(items[0]));
      document.querySelector('.workspace')?.scrollIntoView({behavior: 'smooth', block: 'start'});
    };
    const jump = i => e => { e.stopPropagation(); state.groupWalk = null; focusIssue(issues.indexOf(i)); document.querySelector('.workspace')?.scrollIntoView({behavior: 'smooth', block: 'start'}); };
    const lines = [
      {text: `${plural(open.length, 'open item')} on this report${done.size ? ` (${done.size} already reviewed)` : ''}.`, items: open, label: 'Open items'},
      writing.length ? {text: `${writing.length} ${writing.length === 1 ? 'is' : 'are'} only how something is written (spaces, capitals, a letter typed for a digit). The values themselves are right.`, items: writing, label: 'Written differently'} : null,
      realSure.length ? {text: `${plural(realSure.length, 'is a real problem', 'are real problems')} that change${realSure.length === 1 ? 's' : ''} the numbers, for example: `, example: realSure[0], items: realSure, label: 'Real problems'} : null,
      realUnsure.length ? {text: `${realUnsure.length} need${realUnsure.length === 1 ? 's' : ''} a person to decide, for example: `, example: realUnsure[0], items: realUnsure, label: 'To decide'} : null,
      topSheet && Object.keys(sheets).length > 1 ? {text: `Most are on the ${topSheet[0]} sheet (${topSheet[1]}).`, items: open.filter(i => i.sheet === topSheet[0]), label: `on ${topSheet[0]}`} : null,
    ].filter(Boolean);
    const rows = Object.entries(byType).sort((a, b) => b[1].high - a[1].high || b[1].n - a[1].n);
    const text = ['Summary of errors', ...lines.map(l => l.text + (l.example ? clip(l.example.text) : '')), '',
      ...rows.map(([t, c]) => `${t}: ${c.n}${c.only === c.n ? ' (only written differently)' : c.only ? ` (${c.only} only written differently)` : ''}`)].join('\n');
    return h('div', {class: 'errsum'},
      h('div', {class: 'errsum-head'}, h('h3', {}, 'Summary of errors'), h('span', {class: 'quiet'}, 'Click a line to see those errors in the spreadsheet'),
        h('button', {type: 'button', class: 'btn ghost small', onclick: async () => {
          try { await navigator.clipboard.writeText(text); toast('Summary copied.'); } catch (e) { toast('Your browser blocked copying.', true); } }}, 'Copy')),
      h('ul', {class: 'errsum-lines'}, lines.map(l => h('li', {},
        h('button', {type: 'button', class: 'errsum-go', title: `Show ${plural(l.items.length, 'error')} in the spreadsheet, one at a time`, onclick: walk(l.items, l.label)},
          l.text, l.example ? h('span', {class: 'errsum-ex', role: 'link', tabindex: '0', title: 'Show this one in the spreadsheet', onclick: jump(l.example)}, clip(l.example.text)) : null,
          h('span', {class: 'errsum-arrow', 'aria-hidden': 'true'}, ' ›'))))),
      h('details', {class: 'errsum-kinds'}, h('summary', {}, `By kind (${rows.length})`), h('table', {class: 'errsum-table'}, h('tbody', {}, rows.map(([t, c]) => h('tr', {class: 'errsum-row', tabindex: '0', title: `Show the ${t.toLowerCase()} in the spreadsheet, one at a time`,
          onclick: walk(open.filter(i => issueType(i) === t), t), onkeydown: e => { if (e.key === 'Enter') walk(open.filter(i => issueType(i) === t), t)(); }},
        h('td', {}, t), h('td', {class: 'n'}, String(c.n)),
        h('td', {class: 'note'}, c.only === c.n ? 'only written differently' : c.only ? `${c.only} only written differently` : c.high ? 'must fix' : ''), h('td', {class: 'go'}, '›')))))));
  }
  function issuesBlock(rv) {
    const reviewedKey = 'rd-reviewed:' + state.report.file_hash;
    const done = reviewedSet();
    const issues = walkIssues();
    const info = (rv.issues || []).filter(i => i.severity === 'info');
    const dismissed = (rv.dismissed || []).filter(i => !new Set(store.get(restoredKey(), [])).has(i.key));
    const block = h('section', {class: 'block', id: 'issues'});
    state.openGroups ||= {};
    const setDone = (keys, on) => { keys.forEach(k => on ? done.add(k) : done.delete(k)); store.set(reviewedKey, [...done]); draw(); updateFinish(); drawSideTabs(); };
    if (state.issueSelFor !== state.report.file_hash) { state.issueSel = new Set(); state.issueSelFor = state.report.file_hash; }
    const sel = state.issueSel;
    // accept every suggested fix for the selected issues; ask Claude for the rest when AI is on
    const fixSelected = async btn => {
      const hash = state.report.file_hash, picked = issues.filter(i => sel.has(i.key));
      const dupRows = picked.filter(i => isDupRow(i) && i.row && !fixesFor(i).length).map(i => [i.sheet, Number(i.row)]);
      const accept = () => {
        const d = store.get(decisionsKey(), {}), fixedKeys = [];
        let n = 0;
        for (const i of picked) {
          const fx = fixesFor(i).filter(f => f.state !== 'rejected');
          for (const f of fx) if (d[f.id]?.state !== 'accepted') { d[f.id] = {state: 'accepted', after: d[f.id]?.after ?? f.after}; n++; }
          if (fx.length) fixedKeys.push(i.key);
        }
        store.set(decisionsKey(), d);
        return {n, fixedKeys};
      };
      let {n, fixedKeys} = accept();
      const missing = picked.filter(i => !fixedKeys.includes(i.key) && !isDupRow(i));
      if (missing.length && state.aiOn) {
        btn.disabled = true; btn.textContent = `Claude is fixing ${missing.length}…`;
        try {
          await api('/api/spreadsheets/ai-fixes', {file_hash: hash});
          if (state.selected !== hash) return;
          const data = await api('/api/spreadsheets/fixes', {file_hash: hash});
          state.fixes = data.proposals || []; state.aiFix = data.ai || {};
          ({n, fixedKeys} = accept());
        } catch (e) { toast(e.message, true); }
      }
      fixedKeys.forEach(k => done.add(k)); store.set(reviewedKey, [...done]);
      const left = picked.length - fixedKeys.length;
      sel.clear();
      toast(fixedKeys.length ? `${n} fix${n === 1 ? '' : 'es'} accepted for ${fixedKeys.length} issue${fixedKeys.length === 1 ? '' : 's'}; they go into the download.` + (left - dupRows.length > 0 ? ` ${left - dupRows.length} have no automatic fix yet: open each one to choose what to do.` : '')
        : dupRows.length ? '' : 'None of these have an automatic fix yet. Open each one to choose what to do' + (state.aiOn ? '.' : ', or turn on AI review so Claude can write fixes.'), !fixedKeys.length && !dupRows.length);
      drawFixes(); draw(); updateFinish(); drawSideTabs();
      if (dupRows.length) await removeRows(dupRows);
    };
    const draw = () => {
      const left = issues.filter(i => !done.has(i.key)).length;
      const cur = state.issueNav != null ? issues[state.issueNav]?.key : null;
      block.replaceChildren(h('header', {}, h('h2', {}, 'All issues'),
        h('p', {}, issues.length ? `${left} of ${issues.length} still open. The guide at the top takes you through them one at a time; click any one here to jump to it.`
          + (rv.verify?.status === 'done' ? ' Double-checked by Claude.' : state.verifying ? ' Claude is double-checking…' : '')
          : dismissed.length ? 'Nothing left: Claude checked every flagged item and none are real problems.' : 'Nothing flagged by the checks, SharePoint, the usual-day comparison or AI.'),
        null));
      const summary = errorSummary(issues, done);
      if (summary) block.append(summary);
      if (sel.size) {
        const picked = issues.filter(i => sel.has(i.key)), ready = picked.filter(i => fixesFor(i).some(f => f.state !== 'rejected') || isDupRow(i)).length;
        block.append(h('div', {class: 'selbar issel'},
          h('span', {}, h('b', {}, `${picked.length} selected`), ` · ${ready} with a suggested fix` + (state.aiOn ? '. AI can fix any of them.' : picked.length > ready ? `, ${picked.length - ready} without one` : '')),
          h('button', {type: 'button', class: 'btn ghost small', onclick: () => { sel.clear(); draw(); }}, 'Clear'),
          picked.some(i => done.has(i.key)) ? h('button', {type: 'button', class: 'btn small', onclick: () => { const k = picked.map(i => i.key); sel.clear(); setDone(k, false); drawSteps(); drawCleanOffer(); }}, 'Mark not reviewed') : null,
          picked.some(i => !done.has(i.key)) ? h('button', {type: 'button', class: 'btn small', onclick: () => { const k = picked.map(i => i.key); sel.clear(); setDone(k, true); toast(`${k.length} marked reviewed.`); drawSteps(); drawCleanOffer(); }},
            `Mark ${picked.length === 1 ? 'it' : picked.length} reviewed`) : null,
          ready ? h('button', {type: 'button', class: 'btn small', onclick: e => fixSelected(e.target)}, `Make the ${ready === 1 ? 'suggested fix' : `${ready} suggested fixes`}`) : null,
          state.aiOn ? h('button', {type: 'button', class: 'btn primary small ai', onclick: e => { const k = picked.map(i => i.key); sel.clear(); aiFix(k, e.target); }}, `Fix ${picked.length === 1 ? 'it' : picked.length} with AI`) : null));
      }
      for (const [sev, label] of SEVERITIES) {
        const ofSev = issues.filter(i => i.severity === sev); if (!ofSev.length) continue;
        const openSev = ofSev.filter(i => !done.has(i.key)).length;
        const sec = h('div', {class: 'sevgroup sev-' + sev},
          h('h3', {}, h('span', {class: 'sev ' + sev}), label, h('small', {}, openSev ? `${openSev} open of ${ofSev.length}` : `all ${ofSev.length} reviewed`)));
        const groups = {};
        for (const i of ofSev) (groups[issueType(i)] ||= []).push(i);
        for (const [type, list] of Object.entries(groups).sort((a, b) => b[1].length - a[1].length || a[0].localeCompare(b[0]))) {
          const gk = state.report.file_hash + '|' + sev + '|' + type, openT = list.filter(i => !done.has(i.key)).length;
          const isOpen = state.openGroups[gk] ?? (openT > 0 && (sev === 'high' || list.some(i => i.key === cur) || Object.keys(groups).length === 1));
          const openKeys = (list.some(i => !done.has(i.key)) ? list.filter(i => !done.has(i.key)) : list).map(i => i.key), allSel = openKeys.length > 0 && openKeys.every(k => sel.has(k));
          const pick = h('input', {type: 'checkbox', class: 'typesel', checked: allSel, title: `Select all ${openKeys.length} in ${type}`, 'aria-label': `Select all ${type}`,
            onclick: e => e.stopPropagation(), onchange: e => { openKeys.forEach(k => e.target.checked ? sel.add(k) : sel.delete(k)); state.openGroups[gk] = true; draw(); }});
          if (!allSel && openKeys.some(k => sel.has(k))) pick.indeterminate = true;
          const det = h('details', {class: 'itype', open: isOpen || list.some(i => i.key === cur)},
            h('summary', {}, pick, h('b', {}, type), h('span', {class: 'cnt' + (openT ? '' : ' ok')}, openT ? `${openT} open` : '✓ reviewed'), h('span', {class: 'tot'}, String(list.length))));
          det.addEventListener('toggle', () => { state.openGroups[gk] = det.open; });
          const ul = h('ul', {class: 'ilist'});
          const limit = state.openGroups[gk + '|all'] ? list.length : 25;
          for (const i of list.slice(0, limit)) {
            const fx = fixesFor(i).filter(f => f.state !== 'rejected');
            const badge = i.restored ? 'Put back by you' : i.verdict === 'valid' ? '✓ Claude confirmed' : i.verdict === 'unsure' ? 'Claude unsure' : null;
            ul.append(h('li', {class: 'iitem' + (done.has(i.key) ? ' done' : '') + (i.key === cur ? ' cur' : '') + (sel.has(i.key) ? ' picked' : '')},
              h('input', {type: 'checkbox', checked: sel.has(i.key), 'aria-label': 'Select this error', title: 'Select this error',
                onchange: e => { e.target.checked ? sel.add(i.key) : sel.delete(i.key); draw(); }}),
              h('button', {type: 'button', class: 'iopen', onclick: () => focusIssue(issues.indexOf(i))},
                h('span', {class: 'itext'}, i.text),
                h('span', {class: 'iwhere'}, [i.where, i.source].filter(Boolean).join(' · '),
                  badge ? h('em', {}, badge) : null,
                  done.has(i.key) ? h('em', {class: 'irev'}, '✓ reviewed') : null,
                  i.only ? h('em', {class: 'ionly', title: i.only}, 'only written differently') : null,
                  fx.length ? h('em', {class: 'ifix'}, fx.some(f => f.state === 'accepted') ? '✓ fix accepted' : `${fx.length} fix ready`) : null))));
          }
          if (list.length > limit) ul.append(h('li', {}, h('button', {type: 'button', class: 'linkish', onclick: () => { state.openGroups[gk + '|all'] = true; draw(); }}, `Show all ${list.length}`)));
          {
            const openList = list.filter(i => !done.has(i.key));
            det.append(h('div', {class: 'gactions'},
              openT ? h('button', {type: 'button', class: 'btn small', onclick: () => {
                state.groupWalk = {keys: new Set(openList.map(i => i.key)), label: type};
                focusIssue(issues.indexOf(openList[0])); }}, openT === 1 ? 'Check it' : `Check one at a time (${openT})`) : null));
          }
          det.append(ul);
          sec.append(det);
        }
        block.append(sec);
      }
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
    if (!last) return;
    await loadList(); select(last.file_hash);
    showResult(last.file_hash, true);   // nothing is fixed until the person chooses: AI fixes everything, or they review first
  }
  // After an upload the app fixes everything it can by itself: the sure cleanups already ran on the server, then Claude
  // works through every open issue (in batches, so a report with hundreds of errors is still covered). The person only
  // sees what is left, one at a time.
  // "Accept all fixes" / "Let AI fix everything": one click. Each saved version is checked again, and anything new it turns up is
  // fixed too (a few rounds at most). Items Claude already tried are not asked again; whatever is left is for a person.
  // Before anything is changed: every suggested change with its cell, the value now, the new value, where it comes from
  // and why; and how many items Claude will work out. Nothing is saved until "Make these changes". The original upload is
  // always kept, and every change makes a new version that is checked again.
  function previewAll(p, go) {
    let dlg = $('preview-dialog');
    if (!dlg) { dlg = h('dialog', {id: 'preview-dialog', class: 'fixer', 'aria-labelledby': 'preview-title'}); document.body.append(dlg); }
    const fixes = (p.fixes || []).filter(f => f.state !== 'rejected');
    const bySource = {};
    for (const f of fixes) (bySource[f.source || 'Suggested'] ||= []).push(f);
    const ai = state.aiOn ? (p.none || []).length : 0;
    dlg.replaceChildren(h('div', {class: 'preview'},
      h('h2', {id: 'preview-title'}, 'Review the changes'),
      h('p', {}, `${fixes.length} suggested change${fixes.length === 1 ? '' : 's'}` + ((p.dups || []).length ? `, ${p.dups.length} duplicate row${p.dups.length === 1 ? '' : 's'} removed` : '')
        + (ai ? `, and Claude works out ${ai} more (shown in "what changed" afterwards)` : '') + '. Your original file is kept, and the new version is checked again.'),
      ...Object.entries(bySource).map(([src, list]) => h('details', {open: list.length <= 12 ? '' : null},
        h('summary', {}, `${src}: ${list.length}`),
        h('table', {class: 'preview-table'}, h('thead', {}, h('tr', {}, ...['Cell', 'Now', 'Change to', 'Why'].map(t => h('th', {}, t)))),
          h('tbody', {}, ...list.slice(0, 400).map(f => h('tr', {}, h('td', {}, `${f.sheet} ${f.cell}`), h('td', {}, f.before === '' || f.before == null ? '(blank)' : String(f.before)),
            h('td', {}, String(f.after)), h('td', {}, f.reason || ''))))))),
      h('div', {class: 'row'}, h('button', {type: 'button', class: 'btn', onclick: () => dlg.close()}, 'Cancel'),
        h('button', {type: 'button', class: 'btn primary', onclick: () => { dlg.close(); go(); }}, 'Make these changes'))));
    if (!dlg.open) dlg.showModal();
  }
  async function fixEverythingLoop() {
    if (state.fixingAll) return; state.fixingAll = true;
    const issueId = i => `${i.code}|${i.text}|${i.sheet}|${i.row ?? ''}|${i.cell || ''}`, tries = new Map(), wait = ms => new Promise(r => setTimeout(r, ms));
    let cur = state.report?.file_hash;
    try {
      for (let round = 1; round <= 5 && cur; round++) {
        for (let i = 0; i < 120 && !(state.report?.file_hash === cur && state.fixes && !state.busy); i++) await wait(250);
        drawCleanOffer();
        const fe = state.fixEverything;
        if (!fe || fe.hash !== cur) break;   // nothing left on this version
        const p = fe.parts({includeLeft: true});
        p.none = p.none.filter(x => (tries.get(issueId(x)) || 0) < 2);
        if (!p.fixes.length && !p.dups.length && !(state.aiOn && p.none.length)) break;
        const retry = p.none.some(x => tries.get(issueId(x)));
        p.none.forEach(x => tries.set(issueId(x), (tries.get(issueId(x)) || 0) + 1));
        if (round > 1) toast(retry ? `Taking a closer look at the ones Claude was not sure about…` : `Fixing what the re-check found…`);
        state.aiBatch = retry ? 20 : null;   // a second look goes in small batches, so Claude can take more care over each one
        const next = await fe.approve(p, null, true).finally(() => { state.aiBatch = null; });
        if (!next) break;
        if (next === cur && !p.fixes.length && !p.dups.length) { await openReport(cur); continue; }   // Claude only confirmed items fine: same version, new notes
        cur = next; await loadList(); select(cur);
      }
    } finally { state.fixingAll = false; }
    if (cur) { await loadList(); select(cur); showResult(cur, true, {afterAll: true}); }
  }
  async function autoFixAll(hash, only) {
    if (!state.aiOn) return hash;
    const row = h('div', {class: 'uploading'}), say = t => row.replaceChildren(h('span', {class: 'spin'}), h('span', {}, ' ' + t));
    $('uploads').prepend(row);
    const asked = new Set();
    let cur = hash;
    try {
      for (let round = 1; round <= 40; round++) {
        const data = await api('/api/spreadsheet?id=' + cur);
        const sig = i => `${i.code}|${i.text}|${i.sheet}|${i.row ?? ''}|${i.column || i.cell || ''}`;   // keys change between versions; this does not
        const open = (data.review?.issues || []).filter(i => i.severity !== 'info' && !asked.has(i.key) && !asked.has(sig(i)) && (!only || only(i)));
        if (!open.length) break;
        const size = state.aiBatch || 100, batch = open.slice(0, size);
        batch.forEach(i => { asked.add(i.key); asked.add(sig(i)); });
        say(open.length > size ? `Fixing everything it can: part ${round}, ${fmt(open.length)} issues to go…` : `Fixing everything it can: ${open.length === 1 ? '1 issue' : `${fmt(open.length)} issues`}…`);
        let out;
        try { out = await api('/api/spreadsheets/ai-fix', {file_hash: cur, keys: batch.map(i => i.key), auto: true}); }
        catch (e) { if (round === 1) toast(`Claude could not fix the issues this time (${e.message}). The sure cleanups were still made; the rest are listed for you.`, true); break; }
        cur = out.item.file_hash;
      }
    } finally { row.remove(); }
    return cur;
  }
  async function uploadOne(file) {
    const meter = h('b'), text = h('span', {}, `Preparing ${file.name}…`);
    const row = h('div', {class: 'uploading'}, text, h('div', {class: 'meter'}, meter));
    $('uploads').prepend(row);
    const problem = /\.(xls|xlsm|xlsb)$/i.test(file.name) ? `${file.name} is an older or macro Excel file. In Excel choose File, Save As, Excel Workbook (.xlsx), then upload that copy.`
      : /\.doc$/i.test(file.name) ? `${file.name} is an older Word file. In Word choose File, Save As, Word Document (.docx), then upload that copy.`
      : !/\.(csv|xlsx|pdf|docx)$/i.test(file.name) ? `${file.name} is not a file the app can check. Upload Excel (.xlsx), CSV, PDF or Word (.docx).` : file.size < 1 ? `${file.name} is empty.` : null;
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
  async function showResult(hash, fresh, opts = {}) {
    let data;
    try { data = !fresh && state.report?.file_hash === hash ? state.report : await api('/api/spreadsheet?id=' + hash); } catch (e) { return; }
    const rv = data.review, dialog = $('result-dialog'), plural = (n, w) => `${fmt(n)} ${w}${n === 1 ? '' : 's'}`;
    const a = data.auto_formatting || {}, s = data.ai_fixed || {};
    const aiDid = (s.changed || []).length + (s.removed || []).length + (s.moved || []).length, aiFine = (s.fine || []).length;
    const youDid = (data.fixed_from?.changes || []).filter(x => !/automatic cleanup|AI/.test(x.source || '')).length;
    const checks = [...(s.changed || []), ...(s.removed || []), ...(s.moved || [])].filter(x => x.check).length;
    const left = (rv.issues || []).filter(i => i.severity !== 'info'), must = left.filter(i => i.severity === 'high').length;
    const fixedLines = [
      youDid ? `Made the ${plural(youDid, 'proposed change')} you accepted.` : null,
      a.count ? `Cleaned up ${plural(a.count, 'value')} the app was sure about (capitals, spaces, duplicates and the like).` : null,
      aiDid ? `Claude fixed ${plural(aiDid, 'problem')}` + ((s.removed || []).length || (s.moved || []).length ? `, including ${[(s.removed || []).length ? plural(s.removed.length, 'row') + ' taken out' : '', (s.moved || []).length ? plural(s.moved.length, 'row') + ' moved to the right shift' : ''].filter(Boolean).join(' and ')}` : '') + '.' : null,
      aiFine ? `Claude checked ${plural(aiFine, 'flag')} and found ${aiFine === 1 ? 'it is' : 'they are'} fine as is.` : null,
    ].filter(Boolean);
    const ready = async () => { dialog.close(); for (let i = 0; i < 60 && !(state.report?.file_hash === hash && state.fixes); i++) await new Promise(r => setTimeout(r, 250)); };
    const walk = async () => { dialog.close();
      for (let i = 0; i < 40 && !(state.report?.file_hash === hash && state.fixes); i++) await new Promise(r => setTimeout(r, 250));
      if (state.report?.file_hash === hash) { const done = reviewedSet(); await focusIssue(Math.max(0, walkIssues().findIndex(i => !done.has(i.key)))); document.querySelector('.workspace')?.scrollIntoView({behavior: 'smooth', block: 'start'}); } };
    const title = left.length ? (fixedLines.length ? `${plural(left.length, 'thing')} still to decide` : `Found ${plural(left.length, 'thing')} to fix`)
      : fixedLines.length ? 'All fixed. Nothing left for you' : 'Nothing flagged';
    dialog.replaceChildren(h('div', {class: 'inner'},
      h('div', {class: `tag tag-big tag-${rv.status}`}, STATUS[rv.status], h('small', {}, statusNote(rv))),
      h('h2', {id: 'result-title'}, title),
      h('p', {class: 'note'}, `${rv.job || data.filename}, ${shortDay(rv.day)}. ${fmt(rv.scans)} rows, ${fmt(rv.inspected)} inspected, ${fmt(rv.rejected)} rejected.`),
      fixedLines.length ? h('div', {class: 'res-fixed'}, h('b', {}, 'What was fixed'), h('ul', {}, fixedLines.map(t => h('li', {}, t))),
        h('p', {class: 'quiet'}, 'Every change is listed at the top of the report, with a link to the version before.' + (checks ? ` ${plural(checks, 'change')} ${checks === 1 ? 'is' : 'are'} Claude's best judgment and marked "please double-check".` : ''))) : null,
      left.length ? h('div', {class: 'res-left'}, h('b', {}, 'What was found'),
        h('p', {}, (must ? `${plural(must, 'must-fix item')} before it goes out. ` : '') + (opts.afterAll ? `${left.length === 1 ? 'This one needs' : 'These need'} a person: ${state.aiOn ? 'Claude already tried and was not sure' : 'there was no suggested change'}. Review them on the right: Fix myself, or Ignore if ${left.length === 1 ? 'it is' : 'they are'} fine.` : (fixedLines.length ? '' : 'Nothing has been changed yet. ') + (state.aiOn ? 'Let AI fix everything in one click, or review them first: each kind shows the first one, with Approve, Fix with AI, Fix myself or Ignore for all of that kind.' : 'Approve every suggested change in one click, or review them first: each kind shows the first one, with Approve, Fix myself or Ignore for all of that kind.'))),
        (() => { const g = new Map(); for (const i of left) { const k = sameKey(i); g.set(k, [...(g.get(k) || []), i]); }
          const kinds = [...g.values()].sort((x, y) => y.length - x.length);
          return [h('ul', {}, kinds.slice(0, 5).map(k => h('li', {}, h('span', {class: 'sev ' + k[0].severity}), h('span', {}, k.length > 1 ? h('b', {}, `${fmt(k.length)}× `) : null, k[0].text)))),
            kinds.length > 5 ? h('p', {class: 'quiet'}, `and ${kinds.length - 5} more kinds.`) : null,
            kinds.length < left.length ? h('p', {class: 'quiet'}, `Only ${plural(kinds.length, 'kind')} of issue, so each kind can be handled in one go.`) : null]; })())
        : h('p', {}, 'Check the totals, then approve and send when you are ready.')),
      data.fixed_from ? h('a', {class: 'btn dl-fixed', href: '/api/spreadsheets/download?id=' + hash, download: data.filename}, '⬇ Download the fixed version') : null,
      h('div', {class: 'row'}, h('button', {type: 'button', class: 'btn', onclick: () => dialog.close()}, left.length ? 'Later' : 'Open report'),
        left.length ? h('button', {type: 'button', class: 'btn primary', onclick: async () => { await ready(); showSide('fixes'); document.querySelector('.workspace')?.scrollIntoView({behavior: 'smooth', block: 'start'}); }}, opts.afterAll ? "Review what's left" : 'Review them first') : null,
        left.length && !opts.afterAll ? h('button', {type: 'button', class: 'btn ' + (state.aiOn ? 'ai' : 'primary'), onclick: async () => { await ready(); showSide('fixes');
          if (state.fixEverything?.hash === hash) state.fixEverything.run(); else toast('Open the Fixes tab to choose what to fix.', true); }}, state.aiOn ? 'Let AI fix everything' : 'Approve all suggested changes') : null));
    dialog.showModal();
  }
  $('result-dialog').addEventListener('click', e => { if (e.target === $('result-dialog')) $('result-dialog').close(); });

  // ---------------------------------------------------------------- start
  document.addEventListener('click', e => { for (const d of document.querySelectorAll('.more-menu[open]')) if (!d.contains(e.target)) d.removeAttribute('open'); });
  boot();
  async function boot() {
    try { state.me = await api('/api/me'); } catch (e) { return; }
    const hm = location.hash.match(/^#signin=(.*)$/);
    if (!state.me.user || hm) { showSignIn(hm ? decodeURIComponent(hm[1]) : ''); if (!state.me.user) return; history.replaceState(null, '', location.pathname); }
    drawMe();
    loadClients();
    loadList().then(route).catch(e => toast(e.message, true));   // autopilot is paused for now (autopilotTick / autopilotPanel kept for later)
  }

  // ---------------------------------------------------------------- autopilot: the daily work, done when the app is opened
  function todaysWins() {
    const today = new Date().toLocaleDateString('en-CA', {timeZone: 'America/Chicago'});
    const local = iso => iso ? new Date(iso).toLocaleDateString('en-CA', {timeZone: 'America/Chicago'}) : '';
    const cleaned = state.list.filter(r => r.review?.fixed_from && local(r.received_at) === today).length;
    const sent = state.list.filter(r => local(r.review?.sent_at) === today).length;
    const waiting = visibleList().filter(r => r.review && r.review.status !== 'clear' && !r.review.sent_at).length;
    return h('div', {class: 'wins-bar'},
      h('span', {}, h('b', {}, String(cleaned)), ` report${cleaned === 1 ? '' : 's'} cleaned today`),
      h('span', {}, h('b', {}, String(sent)), ` sent today`),
      h('span', {class: waiting ? '' : 'allgood'}, waiting ? [h('b', {}, String(waiting)), ` waiting for you`] : 'Nothing waiting. Nice work!'));
  }
  async function autopilotTick() {
    let out;
    try { out = await api('/api/autopilot', {run: true}); } catch (e) { return; }
    state.autopilot = out;
    const l = out.ran && out.last;
    if (!l || !(l.built || l.cleaned)) return;
    toast(`Autopilot: ${autopilotLine(l)}`);
    await loadList();
    if (location.hash === '#today') showToday();
  }
  const autopilotLine = l => [l.built ? `built ${l.built} report${l.built === 1 ? '' : 's'}` : '', l.cleaned ? `cleaned ${l.cleaned} (${l.fixed} sure fix${l.fixed === 1 ? '' : 'es'})` : '',
    l.to_check ? `${l.to_check} thing${l.to_check === 1 ? '' : 's'} for a person to check` : '', l.waiting ? `${l.waiting} shift${l.waiting === 1 ? '' : 's'} still running` : '',
    l.errors ? `${l.errors} could not be built` : ''].filter(Boolean).join(', ') || 'nothing new to do';
  function autopilotPanel() {
    const box = h('section', {class: 'autopilot'});
    const draw = () => {
      const a = state.autopilot || {}, l = a.last;
      const toggle = on => async e => { e.target.disabled = true;
        try { state.autopilot = await api('/api/autopilot', {on}); if (on) { state.autopilot = await api('/api/autopilot', {run: true, force: true}); await loadList(); } } catch (err) { toast(err.message, true); }
        draw(); if (on && state.autopilot?.last) showToday(); };
      const runNow = async e => { e.target.disabled = true; e.target.textContent = 'Working…';
        try { state.autopilot = await api('/api/autopilot', {run: true, force: true}); await loadList(); toast(`Autopilot: ${autopilotLine(state.autopilot.last)}`); } catch (err) { toast(err.message, true); }
        showToday(); };
      const goto = x => x.file_hash ? h('button', {type: 'button', class: 'linkish', onclick: () => select(x.file_hash)}, 'Open') : null;
      const word = {built: 'Built from SharePoint', cleaned: 'Cleaned', to_check: 'Needs a person', waiting: 'Shift still running', error: 'Could not build'};
      box.className = 'autopilot ' + (a.on ? 'on' : 'off');
      box.replaceChildren(
        h('div', {class: 'aphead'},
          h('div', {}, h('h2', {}, a.on ? 'Autopilot is on' : 'Let the app do the daily work'),
            h('p', {}, a.on ? 'When anyone opens the app (at most every 15 minutes), it builds each finished shift’s report from SharePoint and makes the sure fixes. Anything it is not sure about waits for a person. Nothing is approved or sent automatically.'
              : 'Turn on autopilot and the app builds each finished shift’s report from SharePoint, makes only the fixes it is sure about, and leaves everything uncertain for a person to check. Nothing is approved or sent automatically.')),
          h('div', {class: 'apbtns'},
            a.on ? h('button', {type: 'button', class: 'btn primary', onclick: runNow}, 'Run now') : null,
            a.can_change ? h('button', {type: 'button', class: a.on ? 'btn ghost' : 'btn primary', onclick: toggle(!a.on)}, a.on ? 'Turn off' : 'Turn on autopilot') : (a.on ? null : h('span', {class: 'quiet'}, 'An admin can turn this on.')))),
        a.on && !a.sharepoint_on ? h('p', {class: 'note'}, 'SharePoint is not connected here, so autopilot can clean uploaded reports but cannot build new ones.') : null,
        l ? h('div', {class: 'aplast'}, h('p', {}, h('b', {}, `Last run ${ago(l.at)}: `), autopilotLine(l) + '.'),
          l.actions?.length ? h('ul', {}, l.actions.slice(0, 12).map(x => h('li', {class: 'ap-' + x.did}, h('span', {class: 'apwhat'}, word[x.did] || x.did), ` ${x.job} · ${shortDay(x.day)}`,
            x.did === 'cleaned' ? ` · ${x.fixed} sure fix${x.fixed === 1 ? '' : 'es'}${x.to_check ? `, ${x.to_check} to check` : ''}` : x.did === 'to_check' ? ` · ${x.to_check} to check` : '',
            x.why && x.did === 'error' ? h('small', {}, ` ${x.why}`) : null, ' ', goto(x)))) : null) : null);
    };
    draw();
    if (!state.autopilot) api('/api/autopilot', {}).then(out => { state.autopilot = out; draw(); }).catch(() => {});
    return box;
  }
})();

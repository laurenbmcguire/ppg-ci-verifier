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
      else if (k.startsWith('on')) el[k] = v; // Refreshes replace this handler instead of stacking listeners.
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
    if (body !== undefined && gridDirty() && !gridRequestAllowed(path, body)) throw new Error('Preview or discard the spreadsheet draft before another operation. Nothing changed.');
    const response = await fetch(path, body === undefined ? {headers: {Accept: 'application/json'}} : {
      method: 'POST', headers: {'Content-Type': 'application/json', 'X-Review-Token': 'same-origin'}, body: JSON.stringify(body)});
    let data = {};
    try { data = await response.json(); } catch (e) { /* non-JSON error page */ }
    if (response.status === 401 && data.signin) { showSignIn(); const error = new Error('Sign in to continue.'); error.httpStatus = 401; throw error; }
    // A crashed or timed-out server answers with an error object, not a sentence: say something a person can read.
    const said = typeof data.error === 'string' ? data.error : data.error?.message && typeof data.error.message === 'string' ? `The server could not finish this (${data.error.message}). Try again in a moment.` : '';
    if (!response.ok) { const error = new Error(said || `The server answered ${response.status}. Try again in a moment.`); error.httpStatus = response.status; error.saveCommitted = data.save_committed === true; throw error; }
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
  const statusNote = r => { const c = r.counts || {}; if (r.coverage?.truncated || r.coverage?.not_listed || r.page?.complete === false) return 'Recheck needed'; if (r.status === 'hold') return `${c.high || 0} high-severity finding${c.high === 1 ? '' : 's'}`; if (r.status === 'check') return `${(c.medium || 0) + (c.low || 0)} medium/low finding${(c.medium || 0) + (c.low || 0) === 1 ? '' : 's'}`; return 'Nothing flagged'; };

  // ---------------------------------------------------------------- state
  const state = {list: [], filter: store.get('rd-filter-v3', 'attention'), search: '', selected: null, report: null, fixes: null, issueSource: 'All', showAll: false, busy: false};

  // ---------------------------------------------------------------- connections
  api('/api/health').then(hh => { state.health = hh;
    state.aiOn = !!hh.ai_review_configured;
    if (!state.aiOn) state.fdSummaryCache = null;
    if (state.report) drawChat();
    // Configuration arrival only enables an explicit action; it never requests a summary.
    document.querySelectorAll('.fd-summary-request').forEach(button => {
      button.disabled = !state.aiOn || !!(state.aiProposalOperation || state.busy || state.rechecking || state.decisionPending);
      button.textContent = state.aiOn ? 'Generate AI summary' : 'AI summary unavailable: AI is off';
    });
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
    return null; // A job label alone cannot establish a client assignment.
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
    // Clients come from SharePoint; until that list arrives, the clients already on saved reports are used.
    let cl = state.clients || [];
    if (!cl.length) { const seen = new Map(); for (const r of state.list) { const k = clientOfReport(r); if (k && !seen.has(k)) seen.set(k, {key: k, client: r.assignment.client || k, sites: [], jobs: []}); }
      cl = [...seen.values()].sort((a, b) => a.client.localeCompare(b.client)); }
    const any = cl.length || state.list.length;
    bar.hidden = !any; if (rail) rail.hidden = !any; if (!any) return;
    const ofKey = (r, key) => key === '' || (key === '_none' ? !clientOfReport(r) : clientOfReport(r) === key);
    const need = key => visibleList().filter(r => ofKey(r, key) && queueNeedsAttention(r)).length;
    const go = key => { location.hash = key ? 'c=' + encodeURIComponent(key) : ''; };
    const tab = (key, label, title) => { const n = need(key);
      return h('button', {type: 'button', role: 'tab', class: state.client === key ? 'on' : '', 'aria-selected': String(state.client === key), title: title || null, onclick: () => go(key)},
        label, n ? h('i', {title: `${n} saved version${n === 1 ? '' : 's'} need attention`}, String(n)) : null); };
    if (rail) {
      const onList = !location.hash.match(/^#(today|overview|kpis|periods|checks|api|users)$/);
      const item = (key, label, title) => { const n = need(key), href = '#c=' + encodeURIComponent(key);
        return h('a', {class: 'nav client' + (onList && state.client === key ? ' active' : ''), href, title: title || null}, h('span', {}, label), n ? h('i', {title: `${n} saved version${n === 1 ? '' : 's'} need attention`}, String(n)) : null); };
      rail.replaceChildren(h('div', {class: 'rail-head'}, 'Clients'),
        h('a', {class: 'nav client' + (onList && !state.client ? ' active' : ''), href: '#', title: 'Every job for every client'}, h('span', {}, 'All clients'), need('') ? h('i', {title: `${need('')} saved versions need attention`}, String(need(''))) : null),
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
    state.workingCopies = data.working_copies === true;
    state.listTotal = data.count;
    state.listPartial = data.has_more === true;
    drawQueue(); drawClientTabs();
    if (typeof queueAutoRecheck === 'function') queueAutoRecheck();
  }
  // Reports last checked with older checks are checked again by themselves while the list is open: newest first,
  // one at a time, a few per visit, and never while a report is open or another step is running.
  async function queueAutoRecheck() {
    if (state.queueRechecking) return;
    state.queueRechecking = true; state.queueRechecked ||= new Set();
    try {
      for (let n = 0; n < 80; n++) {   // every out-of-date report, one after another, so finished ones return to Done by themselves
        if (state.selected || state.busy || state.rechecking || state.decisionPending || typeof document !== 'undefined' && document.hidden) break;
        const r = visibleList().find(x => x.internal_check_status === 'outdated_checks_require_recheck' && !x.review?.sent_at && !x.review?.approved && !state.queueRechecked.has(x.file_hash));
        if (!r) break;
        state.queueRechecked.add(r.file_hash);
        try { await api('/api/spreadsheets/recheck', {file_hash: r.file_hash, apply: true, by: myName() || 'Reviewer'}); } catch (e) { continue; }
        if (!state.selected) { try { const data = await api('/api/spreadsheets'); state.list = data.spreadsheets || state.list; drawQueue(); drawClientTabs(); } catch (e) { /* the next visit picks it up */ } }
      }
    } finally { state.queueRechecking = false; }
  }
  // an upload the app cleaned automatically is shown once: as its cleaned copy (the original opens from that report)
  const visibleList = () => { if (state.workingCopies) return state.list; const cleaned = new Set(state.list.flatMap(r => r.review?.auto_hidden || [])); return state.list.filter(r => !cleaned.has(r.file_hash)); };
  // Queue metadata never invents a business date or groups unrelated files by a similar name.
  const queueDay = r => r.review?.day || '';
  const queueFamily = r => JSON.stringify([r.assignment?.client_key || '', r.root_hash || r.file_hash]);
  // Finding counts, current verification, approval and delivery are separate facts.
  // Old or incomplete summaries must not acquire a successful label by omission.
  function reportVerification(r) {
    const v = r.verification, rv = r.review || r, sp = rv.sharepoint || {};
    const holds = v ? v.holds : r.holds;
    const knownHolds = Array.isArray(holds), held = knownHolds && holds.length > 0;
    const sourceVerified = v ? v.source_verified === true : sp.source_verified === true;
    const sourceStatus = v ? v.source_status : sp.status;
    const checksCurrent = v ? v.checks_current === true : ['findings', 'no_findings_in_checks_performed'].includes(r.internal_check_status);
    return {held, sourceVerified, sourceStatus, complete: knownHolds && !held && sourceVerified && sourceStatus === 'match' && checksCurrent};
  }

  // Two lists. Done: sent to the client, or checked with the current checks and no possible errors left.
  // To do: everything else. SharePoint and approval status are shown on the row; they do not move a finished report back.
  // Zero possible errors on a current check means the report is done (it moves to the Done list).
  const queueZero = r => { const rv = r.review; return !!rv && rv.status === 'clear'; };
  // A finished report stays in Done while a newer version of the checks is run on it in the background; it only
  // comes back to To do if that recheck actually finds something.
  const queueDone = r => !!r.review?.sent_at || (queueZero(r) && r.internal_check_status !== 'could_not_load_latest_check' && r.review.sharepoint?.status !== 'differences' &&
    (r.internal_check_status === 'outdated_checks_require_recheck' || !r.review.coverage?.truncated && !r.review.coverage?.not_listed && r.review.page?.complete !== false));
  // The same report (client, job and day) saved again later and finished there: the earlier copies are not work left
  // to do. They stay available under All and from the finished report's saved versions.
  const queueReportKey = r => r.review?.job && r.review?.day ? JSON.stringify([r.assignment?.client_key || '', r.review.job, r.review.day]) : null;
  function queueSuperseded(r) {
    const key = queueReportKey(r), saved = Date.parse(r.received_at || '');
    if (!key || !Number.isFinite(saved)) return false;
    return (state.list || []).some(o => o.file_hash !== r.file_hash && Date.parse(o.received_at || '') > saved && queueDone(o) && queueReportKey(o) === key);
  }
  const queueNeedsAttention = r => !queueDone(r) && !queueSuperseded(r);
  function queueNewerCopies(r, inventory) {
    if (state.workingCopies) return []; // Parallel graph leaves are independent, never timestamp winners.
    const saved = Date.parse(r.received_at || '');
    if (!Number.isFinite(saved)) return []; // An unknown save time cannot establish chronology.
    return inventory.filter(other => other.file_hash !== r.file_hash && queueFamily(other) === queueFamily(r) && Date.parse(other.received_at || '') > saved)
      .sort((a, b) => Date.parse(b.received_at) - Date.parse(a.received_at));
  }
  function queueFinal(r, inventory) { return queueDone(r) && queueNewerCopies(r, inventory).length === 0; }
  function queueClient(r) {
    const a = r.assignment;
    return a && SETTLED.includes(a.status) && a.client ? a.client : 'Client not confirmed';
  }
  function queueQuantity(r, key) {
    const q = r.review?.quantities?.[key];
    // Older summaries do not establish quantity completeness. Do not turn their missing metadata into zero.
    if (!q || q.value == null || q.value === '' || typeof q.value === 'boolean' || (!Number.isFinite(Number(q.value)) || Number(q.value) < 0)) return 'Unknown';
    const n = Number(q.value).toLocaleString('en-US', {maximumFractionDigits: 12});
    return `${n}${q.unit ? ' ' + q.unit : ' (unit unknown)'}${q.partial ? ' (partial)' : ''}`;
  }
  function queueSource(r) {
    const sp = r.review?.sharepoint || {}, verification = reportVerification(r);
    if (sp.status === 'match' && !verification.sourceVerified) return 'Source not verified';
    return ({match: 'Source comparison matched', differences: 'Source differences', incomplete: 'Source coverage incomplete', stale: 'Source check out of date',
      day_level: 'Source compared by day totals only', unverified: 'Source not verified', not_found: 'Source not found', not_configured: 'Source not connected', error: 'Source check failed'})[sp.status] || 'Source not checked';
  }
  function queueCheck(r) {
    const rv = r.review, c = rv?.counts || {};
    if (!rv || r.internal_check_status === 'could_not_load_latest_check') return ['Check unavailable', 'check'];
    // Out-of-date checks are refreshed by the app itself in the background; the list keeps showing the last result
    // (Completed or the number of possible errors) instead of asking anyone for a recheck.
    if (rv.coverage?.truncated || rv.coverage?.not_listed || rv.page?.complete === false) return ['Check incomplete', 'check'];
    if (rv.sent_at) return ['Sent to client', 'clear'];
    if (rv.status === 'clear' && rv.sharepoint?.status === 'differences') return ['Does not match SharePoint', 'check'];
    if (queueZero(r)) return ['Completed', 'clear'];
    const n = [c.high, c.medium, c.low].every(Number.isFinite) ? c.high + c.medium + c.low : null;
    return [n == null ? 'Possible errors to review' : plural(n, 'possible error'), rv.status === 'hold' ? 'hold' : 'check'];
  }
  // Report name everyone can read: date, client, job.
  function queueTitle(r) {
    const rv = r.review || {}, day = queueDay(r), d = day ? new Date(day + 'T12:00:00') : null;
    const date = d && !isNaN(d) ? d.toLocaleDateString('en-US', {month: 'short', day: 'numeric', year: 'numeric'}) : 'No date';
    return `${date} · ${queueClient(r)} · ${rv.job ? 'Job ' + rv.job : 'Job not found'}`;
  }
  const queueVersion = value => Number.isInteger(value) && value > 0 ? String(value) : 'unknown';
  const queueSearch = r => [r.filename, r.review?.job, queueDay(r), queueClient(r), r.assignment?.status, r.review?.sharepoint?.source,
    r.file_hash, r.version == null ? '' : 'v' + r.version, r.revision == null ? '' : 'revision ' + r.revision].filter(v => v != null).join(' ').toLowerCase();
  function drawQueue() {
    const q = state.search.trim().toLowerCase(), shown = visibleList();
    let rows = shown.filter(r => inClientR(r) && (!q || queueSearch(r).includes(q)));
    if (state.filter === 'attention') rows = rows.filter(queueNeedsAttention);
    if (state.filter === 'review') rows = rows.filter(r => r.review?.review_state === 'waiting');
    if (state.filter === 'final') rows = rows.filter(r => queueFinal(r, shown));
    const mine = shown.filter(r => inClientR(r));
    const attention = mine.filter(queueNeedsAttention).length, waiting = mine.filter(r => r.review?.review_state === 'waiting').length;
    const label = {attention: 'To do', all: 'All', review: 'Review requested', final: 'Done'};
    document.querySelectorAll('.segmented button').forEach(b => {
      const n = b.dataset.filter === 'attention' ? attention : b.dataset.filter === 'review' ? waiting : b.dataset.filter === 'final' ? mine.filter(r => queueFinal(r, shown)).length : 0;
      b.classList.toggle('on', b.dataset.filter === state.filter);
      b.setAttribute('aria-pressed', String(b.dataset.filter === state.filter));
      if (b.dataset.filter === 'attention') { b.setAttribute('title', `${attention} saved versions needing attention, including earlier copies. Findings are not combined across versions.`); b.setAttribute('aria-label', `To do: ${attention} saved versions needing attention`); }
      b.replaceChildren(label[b.dataset.filter] || b.textContent, n ? h('i', {class: 'segn'}, String(n)) : null);
      if (b.dataset.filter === 'review') b.hidden = !waiting && state.filter !== 'review';   // only shown when someone asked for a review
    });
    const todo = shown.filter(queueNeedsAttention).length;
    document.title = (todo ? `(${todo}) ` : '') + 'PPG Verifier';
    const list = $('queue-list'); list.replaceChildren();
    if (state.listPartial) list.append(h('p', {class: 'queue-notice', role: 'status'}, 'Only the most recent saved reports are loaded. Search, filters and counts cover this loaded list, not the full report history.'));
    if (state.listError) {
      list.append(h('div', {class: 'listerror', role: 'alert'}, h('b', {}, 'The report list could not be loaded.'),
        h('span', {}, ` ${state.listError} Saved reports are not affected; this is a loading problem, not an empty list. Previously loaded reports below may be out of date. `),
        h('button', {type: 'button', class: 'linkish', onclick: () => loadList().catch(() => {})}, 'Try again')));
      if (!state.list.length) return;
    }
    const client = (state.clients || []).find(c => c.key === state.client);
    if (client) {
      list.append(h('div', {class: 'clienthead'}, h('b', {}, client.client),
        h('span', {}, `${client.sites.map(x => x.location || x.site).join(' · ')} · ${client.jobs.length} job${client.jobs.length === 1 ? '' : 's'} on SharePoint`)));
    }
    const vh = state.vinHits;
    if (vh && vh.query === state.search) {
      list.append(h('div', {class: 'vinhits'}, h('b', {}, vh.results.length ? `“${vh.query}” is in ${vh.results.length} report row${vh.results.length === 1 ? '' : 's'}` : `No matching report identifier or file name for “${vh.query}”`),
        ...vh.results.slice(0, 12).map(m => h('button', {type: 'button', class: 'vinhit', onclick: () => select(m.file_hash)},
          h('span', {}, h('b', {}, m.identifier), ` · ${m.job || 'Unknown job'} · ${shortDay(m.day) || 'no date'}`),
          h('span', {class: 'file'}, `${m.sheet}, row ${m.row} · ${m.filename}${m.corrected ? ' (corrected copy)' : ''}${m.sent ? ' · sent' : ''}`))),
        vh.still_indexing ? h('p', {class: 'note'}, `Still indexing ${vh.still_indexing} older report(s); search again in a moment.`) : null));
    }
    if (!rows.length) {
      list.append(h('p', {class: 'queue-empty'}, state.list.length
        ? (state.client && !q && !state.list.some(r => inClientR(r)) ? `No reports for ${clientName()} yet.` : state.filter === 'review' && !q ? 'No reports have a review request. Use To do to find reports with findings.' : state.filter === 'final' && !q ? 'Nothing is done yet. A report moves here when it has no possible errors left.' : state.filter === 'attention' && !q ? h('span', {}, 'No matching to-do reports in this loaded list. ', h('button', {type: 'button', class: 'linkish', onclick: () => { state.filter = 'all'; store.set('rd-filter-v2', 'all'); drawQueue(); }}, 'Show all reports')) : 'No reports match that search.')
        : 'No reports yet. Upload an end-of-shift report to start.'));
      return;
    }
    rows = [...rows].sort((a, b) => queueDay(b).localeCompare(queueDay(a)) || (b.received_at || '').localeCompare(a.received_at || ''));
    const groups = new Map();
    for (const r of rows) { const k = state.workingCopies ? r.file_hash : queueFamily(r); if (!groups.has(k)) groups.set(k, []); groups.get(k).push(r); }
    state.openGroups ||= new Set();
    const line = (r, older) => {
      const rv = r.review || {}, [check, tone] = queueCheck(r), current = r.file_hash === (state.selected || state.lastSelected);
      const file = r.filename || 'Report name unavailable', name = queueTitle(r), done = queueDone(r);
      const newer = queueNewerCopies(r, shown), earlier = older || newer.length > 0;
      const version = `${queueVersion(r.version) === 'unknown' ? 'Version unknown' : 'Version ' + queueVersion(r.version)}`;
      const uploaded = r.received_at && !isNaN(new Date(r.received_at)) ? new Date(r.received_at).toLocaleString('en-US', {month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit'}) : 'unknown';
      const workflow = rv.sent_at ? '' : rv.approved ? (rv.approval_current ? 'Approved' : 'Earlier approval; review again') : rv.review_state === 'waiting' ? 'Review requested' : rv.review_state === 'sent_back' ? 'Sent back' : '';
      const quantities = [['inspected', 'Inspected'], ['certified', 'OK'], ['rejected', 'Rejected']].map(([key, label]) => `${label}: ${queueQuantity(r, key)}`);
      // Why a report is still to do, or what to know about a finished one, in one short line.
      const holds = (r.verification?.holds || r.holds || []).map(x => x.text).filter(Boolean);
      const why = !done && tone === 'hold' ? `${Number.isFinite(rv.counts?.high) ? plural(rv.counts.high, 'serious error') : 'Serious errors'} to fix or ignore.`
        : !done && tone === 'check' && /recheck|unavailable/i.test(check) ? 'Open it and the checks run again by themselves.' : '';
      const short = text => { const first = String(text).split(/[:.](\s|$)/)[0].trim(); return first.length > 90 ? first.slice(0, 87) + '…' : first; };
      const note = queueSource(r) === 'Source comparison matched' ? 'Matches SharePoint' : short(holds[0] || queueSource(r)).replace(/^Source/, 'SharePoint');
      const open = h('button', {type: 'button', class: 'item' + (current ? ' sel' : '') + (earlier ? ' older' : ''), 'data-report-hash': r.file_hash,
        title: `${file} · ${version} · ID ${String(r.file_hash || '').slice(0, 12)} · uploaded ${uploaded}`,
        onclick: () => select(r.file_hash), 'aria-label': `Open ${name}. ${file}. ${version}. ${earlier ? 'Earlier saved copy. ' : ''}${check}. ${why} ${note}. ${workflow}`},
        h('span', {class: 'queue-name'}, name),
        h('span', {class: 'queue-state'}, h('span', {class: 'tag tag-' + (done ? 'final' : tone)}, check), why ? h('span', {class: tone === 'hold' ? 'queue-why' : 'queue-hint'}, why) : null, workflow ? h('span', {}, workflow) : null),
        h('span', {class: 'queue-quantities'}, ...quantities.map(text => h('span', {}, text))),
        h('span', {class: 'queue-version'}, `${file} · ${version}${earlier ? ' · earlier copy' : ''}${current ? ' · last opened' : ''} · ${note}`));
      return h('div', {class: 'queue-entry' + (earlier ? ' older-entry' : '')}, open,
        sharePointUrl(r) ? h('a', {class: 'queue-sp', href: sharePointUrl(r), target: '_blank', rel: 'noopener', 'aria-label': `Open SharePoint data for ${name}`}, 'SharePoint data') : null,
        newer.length ? h('details', {class: 'queue-newer'}, h('summary', {class: 'linkish'}, `${newer.length} newer saved cop${newer.length === 1 ? 'y' : 'ies'} available`),
          h('p', {class: 'hint'}, 'These copies may be parallel branches. Each keeps its own findings and decisions.'),
          ...newer.map(copy => h('button', {type: 'button', class: 'linkish queue-newer-copy', 'data-newer-hash': copy.file_hash,
            onclick: () => select(copy.file_hash)}, `Open ${copy.filename || 'saved copy'} · File v${queueVersion(copy.version)} · Revision ${queueVersion(copy.revision)} · ID ${copy.file_hash.slice(0, 12)}`))) : null);
    };
    let lastDay = null;
    for (const [k, members] of groups) {
      members.sort((a, b) => (b.received_at || '').localeCompare(a.received_at || '') || (b.revision || 0) - (a.revision || 0));
      const latest = members[0], day = queueDay(latest), older = members.slice(1);
      if (day !== lastDay) { list.append(h('div', {class: 'day'}, day ? dayLabel(day) : 'Report date not found')); lastDay = day; }
      list.append(line(latest, false));
      if (older.length) {
        const open = state.openGroups.has(k) || older.some(r => r.file_hash === (state.selected || state.lastSelected)), left = older.filter(queueNeedsAttention).length;
        const toggle = h('button', {type: 'button', class: 'linkish older-toggle', 'aria-expanded': String(open), 'data-family': k, onclick: () => {
          if (open) { state.openGroups.delete(k); if (older.some(r => r.file_hash === state.lastSelected)) state.lastSelected = null; } else state.openGroups.add(k);
          drawQueue(); [...list.querySelectorAll('.older-toggle')].find(b => b.getAttribute('data-family') === k)?.focus();
        }}, `${open ? 'Hide' : 'Show'} ${older.length} earlier version${older.length === 1 ? '' : 's'} of this file${left ? ` (${left} with open items)` : ''}`);
        list.append(toggle);
        if (open) for (const r of older) list.append(line(r, true));
      }
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
    state.search = e.target.value; state.vinHits = null; drawQueue();
    clearTimeout(vinTimer);
    const q = state.search.replace(/[^A-Za-z0-9]/g, '');
    if (q.length < 5 || !/\d/.test(q)) { state.vinHits = null; return; }
    vinTimer = setTimeout(async () => {
      try { const out = await api('/api/vins/search', {q: state.search}); if (state.search === out.query) { state.vinHits = out; drawQueue(); } } catch (err) { /* plain list filtering still works */ }
    }, 350);
  });
  document.querySelectorAll('.segmented button').forEach(b => b.addEventListener('click', () => { state.filter = b.dataset.filter; store.set('rd-filter-v3', state.filter); drawQueue(); }));

  // ---------------------------------------------------------------- sign-in and who is using the app
  function showSignIn(message) {
    state.reportChat = null; state.dexDrafts = {};
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
      ? h('div', {}, h('b', {}, 'Local development access'), h('button', {type: 'button', class: 'linkish', onclick: () => showSignIn()}, 'Sign in with your own account'))
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
          h('p', {}, data.required ? 'Everyone signs in and sees only their assigned clients. Hosted sign-in cannot be disabled. Keep deployment protection enabled as an additional safeguard.'
            : 'This local development workspace allows owner access without sign-in. Add people, sign in yourself, then require sign-in.'),
          h('div', {class: 'right'}, data.hosted_signin_required ? h('span', {class: 'hint'}, 'Required for hosted access') : data.required
            ? h('button', {type: 'button', class: 'btn', onclick: () => run('/api/access/required', {on: false}, 'Sign-in is no longer required.')}, 'Turn off')
            : h('button', {type: 'button', class: 'btn primary', disabled: !canRequire, title: canRequire ? '' : 'Sign in with your own account first', onclick: () => run('/api/access/required', {on: true}, 'Sign-in is now required.')}, 'Require sign-in'))),
          !canRequire && !data.required ? h('p', {class: 'hint'}, me.virtual ? 'First add yourself as an admin below, copy your own sign-in link, open it, then come back and turn this on.' : '') : null,
          data.hosted_signin_required ? h('p', {class: 'hint'}, 'An existing admin account and a working Microsoft or email sign-in method are required. Deployment protection does not replace app sign-in.') : null),
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
  function select(hash, receipt = false) { if (receipt) state.receiptHash = hash; state.lastSelected = hash; state.queueScroll = {page: window.scrollY, list: $('queue-list').scrollTop}; if (location.hash !== '#r=' + hash) location.hash = 'r=' + hash; else route(); }
  window.addEventListener('hashchange', route);
  async function route() {
    // While something is being saved the screen stays on the open report (never the home page).
    const stay = () => state.reportRoute || (state.report ? '#r=' + state.report.file_hash : state.fdAIRun?.running && state.fdAIRun.hash ? '#r=' + state.fdAIRun.hash : '#');
    if ((state.busy || state.decisionPending || state.rechecking || state.aiProposalOperation || state.fdAIRun?.running) && !state.receiptHash) { history.replaceState(null, '', stay()); toast('Wait for the current operation to finish before changing reports.', true); return; }
    if (!navigationDrafts()) { history.replaceState(null, '', stay()); return; }
    state.openRequest = (state.openRequest || 0) + 1;
    if (state.gridDraft && !gridDiscard()) { history.replaceState(null, '', '#r=' + state.gridDraft.binding.file_hash); $('grid-cell-input')?.focus(); return; }
    document.body.classList.remove('reportmode');
    document.querySelectorAll('dialog[open]:not(#signin-dialog):not(#fd-aidone)').forEach(d => d.close());   // a window left open must not cover the next page
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
    const m = location.hash.match(/^#r=([0-9a-f]{64})(&history=1)?$/);
    document.body.classList.toggle('viewing', !!m);
    if (!m) { const returning = !!state.selected; state.selected = null; state.openReviewFor = null; drawQueue(); showEmpty(); if (returning) requestAnimationFrame(() => { const item = [...$('queue-list').querySelectorAll('.item')].find(b => b.getAttribute('data-report-hash') === state.lastSelected); item?.focus({preventScroll: true}); $('queue-list').scrollTop = state.queueScroll?.list || 0; window.scrollTo(0, state.queueScroll?.page || 0); }); return; }
    state.selected = m[1]; drawQueue();
    await openReport(m[1], false, !!m[2]);
  }
  const emptyState = $('empty');
  // A very large report sends its findings in pages; every page is loaded before the corrections are drawn, so nothing
  // the counts include is missing from the lists (the counts themselves always come from the server, over all findings)
  async function fillPages(data) {
    const rv = data?.review, pg = rv?.page; if (!pg) return data;
    pg.complete = false;
    const binding = pg.binding, same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
    const fail = () => { throw new Error('The findings changed or a page is missing. Reopen the report before selecting or applying changes.'); };
    if (!binding || binding.file_hash !== data.file_hash || binding.check_hash !== data.check_hash ||
        !same(binding.run, rv.run) || binding.rev !== (rv.decisions?.rev || 0) ||
        binding.issues_total !== pg.issues_total || binding.dismissed_total !== pg.dismissed_total ||
        !Number.isSafeInteger(pg.size) || pg.size < 1) fail();
    // Stage the entire inventory separately. Never expose a mix of old and new
    // pages, or let a repeated/missing stable key silently shrink Select all.
    const loaded = {}, keys = new Set();
    const add = items => {
      if (!Array.isArray(items)) fail();
      for (const it of items) {
        if (!it || typeof it.key !== 'string' || !it.key || keys.has(it.key)) fail();
        keys.add(it.key);
      }
    };
    for (const [list, total] of [['issues', pg.issues_total], ['dismissed', pg.dismissed_total]]) {
      if (!Number.isSafeInteger(total) || total < 0 || !Array.isArray(rv[list]) ||
          rv[list].length !== Math.min(pg.size, total) || pg[list + '_returned'] !== rv[list].length) fail();
      loaded[list] = [...rv[list]]; add(loaded[list]);
      while (loaded[list].length < total) {
        const offset = loaded[list].length;
        const out = await api('/api/spreadsheets/findings', {file_hash: data.file_hash, list, offset, limit: pg.size, expected_binding: binding});
        if (!out || out.list !== list || out.offset !== offset || out.total !== total || out.rev !== binding.rev ||
            !same(out.binding, binding) || !Array.isArray(out.items) || out.items.length !== Math.min(pg.size, total - offset)) fail();
        add(out.items); loaded[list].push(...out.items);
      }
    }
    rv.issues = loaded.issues; rv.dismissed = loaded.dismissed; pg.complete = true;
    return data;
  }
  function showEmpty() { $('report').replaceChildren(emptyState); }

  // ---------------------------------------------------------------- report detail
  function navigationDrafts() {
    state.fdManual?.keepDraft?.();
    const unsavedForm = [...document.querySelectorAll('#side input:not([type=checkbox]):not([type=radio]), #side textarea, .fd-review-dialog input:not([type=checkbox]), .fd-review-dialog textarea')].some(node => node.value !== node.defaultValue);
    if (unsavedForm && !state.fdManual && !window.confirm('Discard the unsaved review form and leave this report?')) return false;
    if (state.gridDraft && !gridDiscard()) return false;
    if (!state.report) return true;
    const edits = store.get(editsKey(), {}), dec = store.get(decisionsKey(), {});
    if (Object.keys(edits).length || Object.values(dec).some(d => d.state === 'accepted')) {
      if (!window.confirm('Leave this report? Typed changes stay with this exact version and will not move to another copy.')) return false;
    }
    return true;
  }
  function versionReadOnly(r = state.report) {
    return !!r && (r.version_view?.historical || r.version_view?.status !== 'current' && !!r.version_view ||
      !!r.version_view?.read_only_reason || !!r.approval || (r.sent || []).some(x => x.sent) || !!r.sending);
  }
  function versionLinks(r) {
    const v = r.version_view || {};
    return h('details', {class: 'version-history'}, h('summary', {}, 'Saved versions and original downloads'),
      h('ul', {}, [{file_hash: r.file_hash, filename: r.filename}, ...(v.history || [])].map(x => h('li', {},
        h('a', {href: '#r=' + x.file_hash + '&history=1'}, x.filename || 'Saved version'), ' · ',
        h('a', {href: '/api/spreadsheets/download?id=' + x.file_hash, download: x.filename || ''}, 'Download original bytes')))));
  }
  function fdReadOnlyReason(r) {
    const v = r.version_view || {};
    if (v.unavailable_reason === 'history_inconsistent') return v.message || 'Saved correction history is inconsistent. The working copy cannot be verified.';
    const reason = v.read_only_reason || (r.sent || []).some(x => x.sent) && 'sent' || r.sending && 'sending' || r.approval && 'approved';
    return reason ? `This version is ${reason} and is read-only.` : 'Historical version. Findings and evidence belong only to this saved copy.';
  }
  function fdHistoryRecovery(r) {
    if (r.version_view?.unavailable_reason !== 'history_inconsistent' || state.me?.user?.role !== 'admin') return null;
    const bound = Object.freeze({file_hash: r.file_hash, expected_check_hash: r.check_hash});
    const current = () => state.report === r && state.selected === bound.file_hash && state.report?.check_hash === bound.expected_check_hash;
    const message = h('p', {role: 'status'}, 'An admin can review whether this isolated history conflict is safely recoverable.');
    const run = async auto => {
      if (!current() || state.busy || state.rechecking || state.decisionPending) return;
      button.disabled = true;
      try {
        const preview = await api('/api/spreadsheets/history-recovery', bound);
        if (!current()) return;
        const plan = preview.result;
        if (plan?.file_hash !== bound.file_hash || plan?.expected_check_hash !== bound.expected_check_hash || !plan?.recovery_token) throw new Error('Recovery preview did not match this exact saved report.');
        if (!auto && !window.confirm(plan.message + '\n\nRecord this history recovery now?')) return;
        if (!current()) return;
        message.textContent = 'Saving the audited history recovery…';
        const saved = await api('/api/spreadsheets/history-recovery', {...bound, recovery_token: plan.recovery_token, apply: true, confirm: true});
        if (!saved.applied || saved.result?.file_hash !== bound.file_hash || saved.result?.history_recovery?.from_check !== bound.expected_check_hash) throw new Error('Recovery was not confirmed. Reload the report before retrying.');
        if (current()) {
          await openReport(bound.file_hash, true);
          toast(auto ? 'This report had a damaged version history. It was repaired and can be worked on again.' : 'Review controls restored. Existing findings and source holds still apply.');
        }
      } catch (error) { message.textContent = error.message + ' Reload the report before retrying an uncertain save.'; }
      finally { if (button.isConnected) button.disabled = false; }
    };
    const button = h('button', {type: 'button', class: 'btn', onclick: () => run(false)}, 'Review history recovery');
    // Working automatically: the server only ever repairs the one safe case (a version recorded as its own fix), so the
    // admin does not have to find and press this. Tried once per report; the button stays if it cannot be repaired.
    state.fdAutoRecovered ||= {};
    if (typeof fdAutoMode === 'function' && fdAutoMode() && !state.fdAutoRecovered[bound.file_hash]) {
      state.fdAutoRecovered[bound.file_hash] = true;
      message.textContent = 'Repairing this report\u2019s version history\u2026';
      globalThis.setTimeout?.(() => run(true), 0);
    }
    return h('section', {class: 'fd-history-recovery'}, message, button);
  }
  // Only saved evidence is grouped here. Browser drafts, suggestions and AI recommendations
  // never become corrections or human decisions just because a historical view was opened.
  function fdRecordedFindingGroups(r) {
    const groups = new Map();
    for (const [list, entries] of [['issues', r.review?.issues || []], ['dismissed', r.review?.dismissed || []]]) entries.forEach((entry, index) => {
      const decision = entry.decision || r.review_decisions?.[entry.key];
      const status = ({reject: 'Kept as recorded', keep: 'Kept as recorded', defer: 'Waiting on source', confirm: 'Confirmed, correction needed', reopen: 'Needs review'})[decision?.action]
        || ({claude_verify: 'AI recommendation, not a human decision', claude_fix: 'AI recommendation, not a human decision', person_legacy: 'Set aside earlier', corrected: 'Fixed in a later version'})[entry.dismissed_by]
        || (entry.severity === 'info' ? 'Informational' : list === 'dismissed' ? 'Set aside, not a correction' : 'Needs review');
      const column = String(entry.cell || '').match(/^[A-Z]+/i)?.[0]?.toUpperCase() || '';
      const rule = entry.code || entry.text || entry.message || 'Recorded finding';
      const key = JSON.stringify([rule, column, entry.sheet || null, entry.severity || null, entry.source || null, status, decision?.action || null]);
      const statusLabel = entry.dismissed_by === 'corrected' && !decision && r.version_view?.unavailable_reason === 'history_inconsistent' ? 'Recorded correction claim, history unverified' : status;
      if (!groups.has(key)) groups.set(key, {key, rule, sheet: entry.sheet, status, statusLabel, severity: entry.severity, entries: []});
      groups.get(key).entries.push({id: entry.key || `${list}:${index}`, entry: {...entry, decision}});
    });
    const rank = {high: 0, medium: 1, low: 2, info: 3};
    return [...groups.values()].map(g => {
      const names = kindNames(g.entries[0].entry), title = (g.entries.length === 1 ? names[0] : names[1] || names[0]) || String(g.rule).replaceAll('_', ' ');
      return {...g, action: `${g.severity || 'Unspecified severity'} · ${title} · ${g.statusLabel}`,
        cells: new Set(g.entries.filter(x => x.entry.cell).map(x => String(x.entry.cell).toUpperCase())).size,
        rows: new Set(g.entries.map(x => x.entry.row || String(x.entry.cell || '').match(/[0-9]+$/)?.[0]).filter(Boolean).map(String)).size};
    }).sort((a, b) => (rank[a.severity] ?? 4) - (rank[b.severity] ?? 4) || b.entries.length - a.entries.length);
  }
  function fdReportRail() {
    return h('nav', {class: 'fd-rail', 'aria-label': 'Report'},
      h('button', {type: 'button', class: 'fd-railbtn', onclick: () => { location.hash = state.client ? 'c=' + encodeURIComponent(state.client) : ''; }, 'aria-label': 'Back to all reports', title: 'All reports'}, icon('back', 22), h('span', {}, 'Reports')),
      ...[['review', 'Review'], ['history', 'History'], ['sources', 'Sources']].map(([id, label]) =>
        h('button', {type: 'button', class: 'fd-railbtn' + (state.fdView === id ? ' on' : ''), 'aria-pressed': String(state.fdView === id), onclick: () => { state.fdView = id; drawReport(); }}, icon(id, 22), h('span', {}, label))),
      h('button', {type: 'button', class: 'fd-railbtn fd-menu', onclick: () => document.body.classList.toggle('navopen'), 'aria-label': 'All pages'}, icon('menu', 22), h('span', {}, 'Menu')));
  }
  function drawReadOnlyReport(r) {
    document.querySelectorAll('#fd-aidone .fd-cardacts').forEach(node => node.remove());
    document.body.classList.add('reportmode');
    state.fdItems = []; state.fdView ||= 'review';
    // Invalidate sheets even when switching to History/Sources or redrawing the same report.
    state.readonlySheetRequest = (state.readonlySheetRequest || 0) + 1;
    if (state.grid?.hash !== r.file_hash) state.grid = null;
    const v = r.version_view || {}, rv = r.review || {}, a = r.assignment || {};
    const reason = fdReadOnlyReason(r), locked = 'Editing, saving, AI, rechecking, approval and sending are disabled in this view. Existing findings and holds remain.';
    const head = h('header', {class: 'fd-head'},
      h('div', {class: 'fd-title'}, h('p', {class: 'fd-eyebrow'}, 'Review workspace'),
        h('h1', {}, [a.client || 'Client not settled', rv.job || r.filename, rv.day ? shortDay(rv.day) : 'No business date'].join(' / ')),
        h('p', {class: 'fd-meta'}, `${r.filename} · saved revision ${r.revision || 'unknown'} · ${fdSourceSummary(r)}`),
        h('details', {class: 'fd-exact-version'}, h('summary', {}, 'Exact saved version and check'),
          h('p', {}, `SHA-256: ${r.file_hash}`), h('p', {}, `Check: ${r.check_hash || 'Unavailable'} · Checker: ${r.checker_version || 'Unknown'}`))),
      h('div', {class: 'fd-actions'}, h('span', {class: 'fd-status amber'}, icon('lock', 16), 'Read-only'),
        /^[0-9a-f]{64}$/.test(v.working_hash || '') && v.working_hash !== r.file_hash ? h('a', {class: 'btn primary', id: 'fd-back-to-working', href: '#r=' + v.working_hash}, 'Back to the fixed version') : null,
        sharePointUrl(r) ? h('a', {class: 'btn', href: sharePointUrl(r), target: '_blank', rel: 'noopener'}, 'Open SharePoint data') : null,
        h('a', {class: 'btn', href: '/api/spreadsheets/download?id=' + r.file_hash, download: r.filename}, 'Download this original version')));
    const notice = h('section', {class: 'fd-readonly-notice', role: 'status'}, h('b', {}, reason), h('p', {}, locked),
      h('p', {}, 'Saved checks are shown for inspection. Opening this view does not complete verification or authorize release.'), fdHistoryRecovery(r),
      v.status !== 'unavailable' && (v.historical || v.status !== 'current') ? h('a', {class: 'btn', href: '#r=' + r.file_hash}, 'Return to working copy') : null);
    const holds = h('details', {class: 'fd-readonly-holds', open: !!(r.holds || []).length},
      h('summary', {}, `Recorded holds · ${(r.holds || []).length}`),
      (r.holds || []).length ? h('ul', {}, r.holds.map(hold => h('li', {}, h('b', {}, hold.hard ? 'Blocking hold: ' : 'Review hold: '), hold.text || hold.code)))
        : h('p', {}, 'No holds listed in this response. That does not establish verification or release readiness.'));
    let main, showSheet;
    if (state.fdView === 'history') main = h('div', {}, fdHistory(r), fdAutoSummary(r));
    else if (state.fdView === 'sources') main = fdSources(r);
    else {
      const names = (r.sheets || []).map(x => x.name), sheet = h('div', {class: 'readonly-sheet'});
      const selector = h('select', {'aria-label': 'Saved sheet', onchange: e => showSheet(e.target.value)}, names.map(n => h('option', {value: n}, n)));
      showSheet = async (name, focus = null) => {
        if (state.report !== r || state.fdView !== 'review' || !names.includes(name)) return;
        const request = ++state.readonlySheetRequest;
        selector.value = name; state.readonlySheet = {hash: r.file_hash, name};
        sheet.replaceChildren(h('p', {role: 'status'}, 'Loading saved spreadsheet…'));
        try {
          const g = await api('/api/spreadsheets/sheet', {file_hash: r.file_hash, sheet: name});
          if (state.report !== r || request !== state.readonlySheetRequest || state.fdView !== 'review') return;
          if (g.sheet !== name) throw new Error('The saved sheet did not match the request. Reopen it to try again.');
          state.grid = {...g, hash: r.file_hash};
          const {table, focusEl} = buildGrid(g, {cells: {}, rows: {}, perSheet: {}}, {focus});
          const wrap = h('div', {class: 'gridwrap', tabindex: '0', 'aria-label': `Saved spreadsheet: ${name} (read-only)`}, table);
          sheet.replaceChildren(wrap);
          if (focus) { wrap.focus(); reveal(wrap, focusEl, true); }
        } catch (e) { if (state.report === r && request === state.readonlySheetRequest) sheet.replaceChildren(h('p', {role: 'alert'}, e.message),
          h('button', {type: 'button', class: 'btn', onclick: () => showSheet(name, focus)}, 'Retry saved sheet')); }
      };
      const groups = fdRecordedFindingGroups(r), count = groups.reduce((n, g) => n + g.entries.length, 0);
      const coverage = rv.coverage || {};
      const findings = h('aside', {class: 'fd-aside fd-readonly-findings', 'aria-label': 'Recorded findings'},
        h('header', {class: 'fd-apphead'}, h('h2', {}, 'Verification App'), h('p', {class: 'fd-errcount'}, `${count} recorded findings`)),
        h('div', {class: 'sidebody'}, h('p', {class: 'fd-hint'}, 'Grouped by rule, sheet, severity and saved status. Set-aside findings and AI recommendations are not corrections.'),
          coverage.truncated || coverage.not_listed || rv.page?.complete === false || outdated(r) ? h('p', {class: 'fd-coverage', role: 'note'}, 'Saved check coverage is incomplete or out of date. This list cannot establish complete verification.') : null,
          !count ? h('p', {}, 'No findings listed in this saved check. Review source evidence and holds before relying on this report.') : null,
          ...groups.map(g => fdHistoryGroup(g, {finding: true, notice: 'Saved finding status only. No decisions or changes can be made in this view.', renderEntry: x => [
            h('b', {}, [x.sheet || 'Report-level', x.cell || (x.row ? 'row ' + x.row : null)].filter(Boolean).join(' · ')),
            h('p', {}, x.text || x.message || x.code),
            x.decision ? h('p', {}, [x.decision.by, x.decision.reason || x.decision.note].filter(Boolean).join(' · ')) : null,
            x.source_value != null ? h('p', {}, `Recorded source value: ${x.source_value}`) : null,
            names.includes(x.sheet) ? h('button', {type: 'button', class: 'btn small', onclick: () => showSheet(x.sheet, {cell: x.cell, row: x.row})}, 'View saved location') : null]}))));
      main = h('div', {class: 'fd-work fd-readonly-work'}, h('section', {class: 'fd-sheet', 'aria-label': 'Spreadsheet'},
        h('div', {class: 'fd-sheetbar'}, h('span', {class: 'fd-sheetbar-what'}, 'Exact saved workbook · read-only'), names.length ? selector : null),
        names.length ? sheet : h('p', {class: 'note'}, 'No saved spreadsheet sheets are available.')), findings);
    }
    $('report').replaceChildren(h('div', {class: 'fd version-readonly'}, fdReportRail(), h('div', {class: 'fd-main'}, head, notice, versionLinks(r), fdMetrics(r), holds, main)));
    if (showSheet) {
      const names = (r.sheets || []).map(x => x.name), prior = state.readonlySheet;
      const name = prior?.hash === r.file_hash && names.includes(prior.name) ? prior.name : (r.dataset_selection?.authoritative || []).find(n => names.includes(n)) || names[0];
      if (name) showSheet(name);
    }
  }
  async function openReport(hash, keepScroll, historical = false) {
    if (state.gridDraft && !gridDiscard()) return;
    const report = $('report'), request = state.openRequest = (state.openRequest || 0) + 1;
    const receipt = state.receiptHash === hash; state.receiptHash = null;
    const active = () => request === state.openRequest && state.selected === hash;
    // Refreshing the report that is already on screen (after a save, a recheck or a SharePoint check) keeps it visible,
    // dimmed and not clickable, until the new copy arrives: no blank screen in between.
    const shown = state.report, side = typeof document !== 'undefined' ? document.getElementById('side') : null;
    const refresh = !!shown && !historical && (shown.file_hash === hash || receipt || shown.root_hash && shown.root_hash === hash) && !!report.querySelector?.('.sheetwrap, .fd-sheetbar, table');
    state.report = null; state.fixes = null; state.fdItems = []; closeChat();
    const settle = () => { for (const el of [report, side]) { el?.classList?.remove('fd-refreshing'); el?.removeAttribute?.('inert'); } if (typeof document !== 'undefined') document.getElementById('fd-refresh-note')?.remove(); };
    if (refresh) {
      for (const el of [report, side]) { el?.classList?.add('fd-refreshing'); el?.setAttribute?.('inert', ''); }
      if (typeof document !== 'undefined' && !document.getElementById('fd-refresh-note')) document.body.append(h('div', {id: 'fd-refresh-note', role: 'status'}, h('span', {class: 'spin'}), ' Updating this report…'));
    } else report.replaceChildren(h('div', {class: 'sheetwrap'}, h('p', {class: 'note'}, 'Opening working copy…')));
    try {
      let version = await api('/api/spreadsheets/working-copy?id=' + hash);
      if (!active()) return;
      if (!version || version.requested_hash !== hash || !['current', 'superseded', 'choice', 'unavailable'].includes(version.status)) throw new Error('The working copy could not be verified. Reload to try again.');
      if (!historical && !receipt && version.status === 'choice') {
        state.reportRoute = location.hash;
        report.replaceChildren(h('div', {class: 'sheetwrap version-choice'}, h('h1', {}, 'Choose a working copy'),
          h('p', {}, 'Separate corrected copies exist. Choose the branch you want; no copy was selected automatically.'),
          ...(version.choices || []).map(x => h('p', {}, h('a', {class: 'btn', href: '#r=' + x.file_hash}, `${x.filename || 'Working copy'} · ID ${x.file_hash.slice(0, 12)}`))),
          h('a', {href: '#r=' + hash + '&history=1'}, 'View the original version (read-only)'))); return;
      }
      if (version.status === 'unavailable' && version.unavailable_reason !== 'history_inconsistent') throw new Error('The working copy is unavailable or its history cannot be verified. No version was opened.');
      if (!historical && !receipt && version.status === 'superseded') {
        if (!/^[0-9a-f]{64}$/.test(version.working_hash || '')) throw new Error('The working copy could not be verified.');
        hash = version.working_hash; state.selected = hash;
        version = await api('/api/spreadsheets/working-copy?id=' + hash);
        if (!active()) return;
        if (version.requested_hash !== hash || version.status !== 'current') throw new Error('The working copy changed while opening. Reopen it to try again.');
        history.replaceState(null, '', '#r=' + hash);
      }
      const data = await fillPages(await api('/api/spreadsheet?id=' + hash));
      if (!active()) return;
      if (data.file_hash !== hash) throw new Error('The saved version could not be verified.');
      data.version_view = {...version, historical: historical || receipt && version.status !== 'current'};
      if (receipt && version.status !== 'current') history.replaceState(null, '', '#r=' + hash + '&history=1');
      state.reportRoute = location.hash;
      state.fdSummaryCache = null; state.fdView = 'review'; state.fdCur = null; state.fdTier = null; state.gridSearch = ''; state.sheetScroll = {}; state.gridOnlyMarked = false; state.gridRow = null;
      const sameVersion = state.report?.file_hash === data.file_hash;   // a refresh of the version on screen keeps manual work open
      state.report = data; state.fixes = null; state.showAll = false; if (!sameVersion) { state.fdManual = null; state.fdFree = null; }
      settle(); drawReport(); drawQueue();
      if (!versionReadOnly(data)) loadFixes(hash);
      if (!versionReadOnly(data) && state.openSendFor && (state.openSendFor === hash || state.openSendFor === 'next' && data.fixed_from)) { state.openSendFor = null; openSend(); }
    } catch (e) {
      if (!active()) return;
      state.report = null; state.fixes = null; settle();
      report.replaceChildren(h('div', {class: 'sheetwrap'}, h('p', {class: 'note', role: 'alert'}, e.message),
        h('button', {type: 'button', class: 'btn', onclick: () => openReport(hash, false, historical || receipt)}, 'Retry opening report')));
    }
  }

  // Reports uploaded before the automatic cleanup: make the sure fixes the first time one is opened, then say what changed
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
    toast('AI workbook changes are paused. Review and apply cell corrections instead.', true);
  }
  function aiFixedBanner(r) {   // a short summary of what Dex did; every single change is one click away
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
        h('div', {}, h('b', {}, [did ? `Dex fixed ${plural(did, 'thing')}` : '', fine.length ? `checked ${fmt(fine.length)} and found ${fine.length === 1 ? 'it' : 'them'} fine` : '', left.length ? `left ${fmt(left.length)} for you` : ''].filter(Boolean).join(', ').replace(/^./, c => c.toUpperCase()) || 'Dex looked at this report'),
          h('span', {}, `${s.auto ? 'Done automatically' : `${plural(s.asked, 'issue')} you picked`}, ${ago(s.made_at)}.` + (checks ? ` ${plural(checks, 'change')} ${checks === 1 ? 'is' : 'are'} Dex's best judgment: please double-check ${checks === 1 ? 'it' : 'them'}.` : ''))),
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
    source_item_unconfirmed: 'Source capture incomplete', source_day_unconfirmed: 'On SharePoint; business day unconfirmed', not_on_sharepoint_for_day: 'On SharePoint for another day', report_row_without_item_id: 'No item ID', item_listed_twice: 'Listed twice',
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
    const rows = (x.rows || []).map(fdPresentationRow);
    const stale = (r.review?.sharepoint || {}).status === 'stale';
    const verified = r.review?.sharepoint?.source_verified === true && x.status === 'verified' && !stale && !(r.holds || []).some(hold => String(hold.code || '').startsWith('source_'));
    const label = stale ? 'Re-check needed: this check was made for an earlier version' : verified ? REC[x.status] || x.status : r.review?.sharepoint?.message || 'Current exact-source verification is unavailable';
    const confirm = Object.entries(x.column_mapping || {}).filter(([, m]) => m.status === 'needs_confirmation' || m.status === 'conflict');
    const mappingRequest = Object.freeze({file_hash: r.file_hash, expected_check_hash: r.check_hash, expected_snapshot_id: x.snapshot_id, expected_mapping_revision: x.mapping_binding?.revision ?? null, site_id: x.site_id, list_id: x.list_id || x.list?.id, mapping: Object.freeze(Object.fromEntries(confirm.map(([f, m]) => [f, m.column])))});
    return h('div', {class: 'spjob ' + (verified ? 'ok' : 'warn')},
      h('span', {class: 'spjob-tag'}, verified ? 'Source ✓' : 'Source'),
      h('b', {}, label),
      h('span', {}, `${x.list?.name || x.site} · ${x.sharepoint_items_for_day} SharePoint item(s) for ${x.day}, ${x.report_rows} report row(s) · checked: ${(x.fields_checked || []).join(', ') || 'nothing'}`
        + ((x.fields_not_checked || []).length ? ` · not checked: ${x.fields_not_checked.join(', ')}` : '') + ` · ${c.pages || 0} page(s) read · captured ${ago(x.captured_at)}`
        + ((x.not_verified_because || []).length ? ' · ' + x.not_verified_because.join(' ') : '') + (c.notes?.length ? ' · ' + c.notes.join(' ') : '')),
      btn,
      confirm.length && mappingRequest.list_id ? h('button', {type: 'button', class: 'btn small', disabled: !mappingRequest.site_id, title: mappingRequest.site_id ? 'Confirm columns for this exact site and list' : 'Source site identity is missing. Reconcile the exact source before confirming columns.', onclick: async e => {
        if (!mappingRequest.site_id) { toast('Source site identity is missing. Reconcile the exact source first.', true); return; }
        e.target.disabled = true;
        try { await api('/api/sharepoint/confirm-columns', mappingRequest); await run({target: e.target}); }
        catch (err) { toast(err.message, true); e.target.disabled = false; } }}, `Confirm ${confirm.map(([f, m]) => `${f} = ${m.column}`).join(', ')}`) : null,
      confirm.length && !mappingRequest.site_id ? h('p', {class: 'note'}, 'Column confirmation unavailable: exact source site identity is missing.') : null,
      rows.length ? h('details', {class: 'recrows', open: ''}, h('summary', {}, `${rows.filter(d => !['source_day_unconfirmed','source_item_unconfirmed'].includes(d.status)).length} differences · ${rows.filter(d => ['source_day_unconfirmed','source_item_unconfirmed'].includes(d.status)).length} verification gaps` + (x.proposed_corrections?.length ? ' · the fixes are in the issues below' : '')),
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
    const parts = [a ? `${a} cleaned up` : '', ai ? `${ai} fixed by Dex` : '', fine ? `${fine} checked and fine` : ''].filter(Boolean);
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
          (d.tables ? `They are checked like any report. Fixes come back as an Excel copy; your ${d.type} file is not changed.` : 'There is nothing to check as a table, but you can read the text below or ask Dex about it.'))),
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
    const rv = r.review || {}, sp = rv.sharepoint || {}, a = r.assignment || {}, c = fdCounts();
    if (!r.review || r.internal_check_status === 'could_not_load_latest_check') return {tone: 'amber', icon: 'warn', text: 'Check unavailable', next: 'review', why: 'Reopen the report to load its current check.'};
    if (outdated(r)) return {tone: 'amber', icon: 'warn', text: 'Checks out of date', next: 'recheck', why: `Checked with an older version of the checks (${r.recheck?.checked_with || 'earlier'}). Nothing is trusted until it is checked again.`};
    if ((r.sent || []).some(x => x.sent)) return {tone: 'green', icon: 'ok', text: 'Sent to client', next: null};
    if (a.needs_person) return {tone: 'red', icon: 'x', text: 'Client not settled', next: 'client', why: a.reason || 'Choose the client and job before anything else; job numbers repeat between clients.'};
    if (c.blockers) return {tone: 'red', icon: 'x', text: plural(c.blockers, 'blocker'), next: 'blockers'};
    if (sp.status === 'stale') return {tone: 'amber', icon: 'warn', text: 'SharePoint check out of date', next: 'reconcile', why: sp.message};
    if (sp.status !== 'match' || sp.source_verified !== true || (r.holds || []).some(h => String(h.code || '').startsWith('source_'))) return {tone: 'amber', icon: 'warn', text: 'Not verified against SharePoint', next: 'reconcile',
      why: sp.status === 'day_level' ? sp.message : sp.status === 'incomplete' ? 'SharePoint could not be read completely, so the report is not verified. ' + (sp.message || '') : sp.status === 'unverified' ? sp.message || 'Some fields could not be compared.' : sp.status === 'differences' ? 'Differences from SharePoint are listed as blockers.' : 'The report has not been compared with SharePoint item by item yet.'};
    if ((r.holds || []).length) return {tone: 'amber', icon: 'warn', text: 'Verification held', next: 'review', why: r.holds.map(h => h.text).filter(Boolean).join(' ')};
    if (r.checked_copy && !fdCheckedState(r).ready) return {tone: 'amber', icon: 'warn', text: 'Verification not complete', next: 'review', why: fdCheckedState(r).message};
    if (r.checked_copy && fdCheckedState(r).ready) return {tone: 'green', icon: 'ok', text: '0 remaining detected errors', next: null};
    if (c.review) return {tone: 'amber', icon: 'warn', text: `${c.review} to review`, next: 'review'};
    if (r.approval && rv.approval_current) return {tone: 'green', icon: 'ok', text: 'Internally approved', next: 'send'};
    if (r.approval) return {tone: 'amber', icon: 'warn', text: 'Approval out of date', next: 'approve', why: 'It was approved on an earlier check run. Approve again if it still looks right.'};
    return {tone: 'green', icon: 'ok', text: 'Ready for internal approval', next: 'approve'};
  }

  // ---------- corrections: issues and suggested changes joined, grouped by root cause, in three tiers
  const TIERS = [['blocker', 'Blockers'], ['review', 'Needs review'], ['format', 'Formatting'], ['aside', 'Set aside']];
  // why a finding is off the open list when no person decided it on this version: a recommendation or an older record
  const ASIDE = {claude_verify: 'claude', claude_fix: 'claude', person_legacy: 'kept_earlier', corrected: 'corrected'};
  // Snapshots belong to the rendered control, never to whichever report is current at click time.
  function reviewBinding(r) {
    return Object.freeze({file_hash: r.file_hash, expected_rev: r.review?.decisions?.rev ?? 0,
      expected_run: JSON.parse(JSON.stringify(r.review?.run || null)), expected_check_hash: r.check_hash});
  }
  function findingBinding(items) {
    const first = items[0];
    if (!first?.binding || items.some(x => JSON.stringify(x.binding) !== JSON.stringify(first.binding))) throw new Error('Review these findings again before deciding.');
    return {...first.binding, expected_fps: Object.freeze(Object.fromEntries(items.map(x => [x.key, x.fp])))};
  }
  function fdItems() {
    const r = state.report, rv = r.review, dec = store.get(decisionsKey(), {}), decided = r.review_decisions || {}, edits = store.get(editsKey(), {});
    // findings a person kept as recorded are listed too (from the server's decisions), so they can be seen and undone
    const loadedIssues = allIssues(rv), loadedKeys = new Set(loadedIssues.map(i => i.key));
    const issues = [...new Map([...loadedIssues, ...(rv.dismissed || []).filter(i => !loadedKeys.has(i.key))].map(i => [i.key, i])).values()].filter(i => i.severity !== 'info'), fixes = state.fixes || [], used = new Set(), items = [];
    const make = (i, f) => {
      const key = i ? i.key : 'fix:' + f.id, fd = i?.decision || null, d = fd || decided[key] || (f ? decided['fix:' + f.id] : null);
      const fmt = f && fmtKind(f);
      const aside = i && !i.decision ? ASIDE[i.dismissed_by] : null;
      const tier = aside ? 'aside' : i && i.severity === 'high' ? 'blocker' : fmt && (!i || i.only) ? 'format' : 'review';
      const cell = f ? String(f.cell).toUpperCase() : i.cell && splitCell(i.cell) ? String(i.cell).toUpperCase() : null;
      const row = cell ? splitCell(cell)[1] : i?.row ? Number(i.row) : null;
      const src = i?.source_value != null ? i.source_value : f?.source === 'SharePoint' ? f.after : null;
      const kind = i ? i : {text: f.reason || '', code: 'fix:' + (fmt || f.source)};
      const col = (String(i?.cell || '').match(/^[A-Z]+/) || [''])[0];
      const ruleKey = i?.code ? JSON.stringify([i.code, col]) : i ? sameKey(i) : '';
      const group = aside ? `aside|${aside}|${ruleKey}` : i?.legacy_note ? `${tier}|legacy|${i.code}|${col}` : tier + '|' + (fmt ? 'fmt:' + fmt : i ? ruleKey : 'fix:' + f.source);
      const own = cell && edits[editId(f?.sheet || i?.sheet, cell)];
      const stateNow = own || (f && dec[f.id]?.state === 'accepted') ? 'selected'
        : aside ? aside
        : fd ? ({reject: 'kept', defer: 'deferred', confirm: 'confirmed', reopen: 'open'}[fd.action] || 'open')
        : (d?.action === 'reject' || (f && dec[f.id]?.state === 'rejected')) ? 'rejected' : d?.action === 'keep' ? 'kept' : 'open';
      const evidence = f ? (f.source === 'SharePoint' || i?.source_value != null ? 'source' : isSureFix(f) ? 'mechanical' : f.source === 'AI fix' ? 'ai' : 'guess') : i?.source_value != null ? 'source_only' : 'none';
      return {binding: reviewBinding(r), fp: i?.fp, key, issue: i, fix: f, tier, group: JSON.stringify([group, f?.sheet || i?.sheet || null, evidence, stateNow === 'selected' ? 'open' : stateNow, i?.severity || null, f?.source || i?.source || null, i?.code || null, fd?.action || d?.action || null]), cell, row, sheet: f?.sheet || i?.sheet || null, kind, fmt, state: stateNow, note: d?.note || d?.reason || '', decidedBy: d?.by,
        decision: fd, legacyNote: i?.legacy_note || null, previous: i?.previous_decision || null,
        report: f ? f.before : i?.report_value ?? (cell && Array.isArray(i?.observed) && i.observed.length === 1 ? (i.observed[0].type === 'NoneType' ? '' : i.observed[0].value) : null), source: src, proposed: own ? own.after : f ? (dec[f.id]?.after ?? f.after) : null, own: own || null,
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
  function fdRecordCount(items) {
    return new Set(items.map(x => x.sheet && x.row ? JSON.stringify([x.sheet, x.row]) : x.key)).size;
  }
  function fdGroupSelection(items, eligible) {
    const cellKey = it => it.sheet && it.cell ? JSON.stringify([it.sheet, String(it.cell).toUpperCase()]) : it.key;
    const supported = new Set(eligible.map(cellKey));
    const selected = new Set(eligible.filter(it => it.state === 'selected').map(cellKey));
    const individualReview = new Set(items.filter(it => !supported.has(cellKey(it)) && ['open', 'deferred', 'confirmed'].includes(it.state)).map(cellKey));
    return {eligible: supported.size, selected: selected.size, individualReview: individualReview.size};
  }
  function fdGroups(items) {
    const by = new Map();
    for (const it of items) { if (!by.has(it.group)) by.set(it.group, {key: it.group, tier: it.tier, items: []}); by.get(it.group).items.push(it); }
    const rank = {blocker: 0, review: 1, format: 2, aside: 3};
    return [...by.values()].map(g => {
      const [one, many] = kindNames(g.items[0].kind), n = g.items.length, first = g.items[0];
      // one item: its own plain sentence; several: the plural name of the shared cause
      const own = String(first.issue?.text || first.text || '').replace(/\s*\(Day-level comparison[^)]*\)/, '');
      const generic = /…/.test(one) || !one;
      g.title = (generic ? (n === 1 ? own : `${own.slice(0, 70)}${own.length > 70 ? '…' : ''} (and ${n - 1} more like it)`) : n === 1 ? one : (many || one)).replace(/^./, c => c.toUpperCase());
      const ac = first.state === 'claude' ? 'AI says this is fine: ' : first.state === 'kept_earlier' ? 'Ignored earlier: ' : first.state === 'corrected' ? 'Fixed in a later version: ' : '';
      if (ac) g.title = ac + g.title.replace(/^./, c => c.toLowerCase());
      if (g.title.length > 120) g.title = g.title.slice(0, 117) + '…';
      const selection = fdGroupSelection(g.items, fdRecommended(g.items));
      const selectionLabel = selection.eligible && selection.selected === selection.eligible ? 'All eligible changes selected'
        : selection.selected ? `${selection.selected} of ${selection.eligible} eligible changes selected`
        : g.items.some(x => x.state === 'selected') && g.items.some(x => x.state === 'open') ? 'Individual selections; review remaining items'
        : {open: 'Needs review', selected: 'Selected, not applied', deferred: 'Waiting on source', confirmed: 'Correction confirmed', kept: 'Kept as recorded', rejected: 'Excluded'}[first.state] || first.state;
      g.status = selectionLabel;
      g.records = fdRecordCount(g.items);
      g.open = g.items.filter(x => x.state === 'open').length;
      g.unresolved = g.items.filter(x => ['open', 'selected', 'deferred', 'confirmed'].includes(x.state)).length;
      g.deferred = g.items.filter(x => x.state === 'deferred').length;
      return g;
    }).sort((a, b) => rank[a.tier] - rank[b.tier] || b.items.length - a.items.length);
  }
  function fdCounts() {
    if (!state.report) return {};
    const items = state.fdItems || [], open = items.filter(x => x.state === 'open' || x.state === 'selected');
    const c = readyChanges();
    return {detected: allIssues(state.report.review).filter(i => i.severity !== 'info').length, suggestions: (state.fixes || []).length,
      selected: c.total, applied: (state.report.fixed_from?.changes || []).length,
      blockers: items.filter(x => x.tier === 'blocker' && x.state !== 'rejected' && x.state !== 'kept').length + (state.report.holds || []).filter(x => x.hard && x.code !== 'awaiting_source_owner').length,
      kept: items.filter(x => x.state === 'kept').length, deferred: items.filter(x => x.state === 'deferred').length, confirmed: items.filter(x => x.state === 'confirmed').length,
      review: open.filter(x => x.tier === 'review').length, format: open.filter(x => x.tier === 'format').length};
  }

  const fdMode = () => store.get('rd-mode:' + state.report.file_hash, null);
  function fdChooseMode(mode, binding) {
    if (!['manual', 'unchanged'].includes(mode)) { toast('Choose a current review option. No AI request was made.', true); return; }
    if (state.report?.file_hash !== binding.file_hash || state.report?.check_hash !== binding.expected_check_hash) {
      toast('The report changed. Review the current version before choosing.', true); return;
    }
    store.set('rd-mode:' + binding.file_hash, mode);
    if (mode === 'unchanged') {
      toast('File left unchanged. Your draft selections are kept for later; findings and holds remain.');
    }
    drawReport();
  }
  // Only the server's current eligibility is used for bulk selection. Local edits,
  // rejected suggestions and source-owner decisions are never silently overwritten.
  function fdRecommended(items = fdItems()) {
    if (state.report.review.page?.complete === false) return [];
    const byCell = new Map();
    for (const it of items) {
      const f = it.fix;
      if (!f || f.recommendation_eligible !== true || f.recommendation_check_hash !== state.report.check_hash ||
          !['open', 'selected'].includes(it.state) || it.own || it.previous ||
          it.proposed !== f.after || !it.selectable) continue;
      byCell.set(JSON.stringify([it.sheet, it.cell]), it);
    }
    return [...byCell.values()];
  }
  function fdPreviewRecommended(binding) {
    if (binding.file_hash !== state.report?.file_hash || binding.expected_check_hash !== state.report?.check_hash) {
      toast('The report changed. Review the current recommendations again.', true); return;
    }
    const recommended = fdRecommended();
    if (!recommended.length) { toast('No supported recommendations are available. Review the holds and individual findings.', true); return; }
    // A separate immutable preview, not a mutation of the reviewer's manual basket.
    const accepted = recommended.map(it => it.fix);
    const dec = Object.fromEntries(accepted.map(f => [f.id, {state: 'accepted', after: f.after}]));
    fdPreview({...captureCorrections(accepted, dec, {}, binding, state.report), recommendedOnly: true});
  }
  function fdHoldSummary(r) {
    const holds = r.holds || [], blocking = holds.filter(hold => hold.hard).length;
    return `${plural(blocking, 'blocking hold')} · ${plural(holds.length - blocking, 'advisory hold')}`;
  }
  function fdOccurrenceLabel(it, allItems) {
    const occurrence = allItems.findIndex(item => item.key === it.key) + 1;
    const linked = allItems.filter(item => item.fix?.id === it.fix?.id && item.sheet === it.sheet && item.cell === it.cell).length;
    return `Include change, ${it.sheet || 'Report'}, cell ${it.cell}, finding ${occurrence} of ${allItems.length}`
      + (linked > 1 ? `. ${linked} findings share this cell change; their selections stay together.` : '');
  }
  function fdModeSelector(r) {
    return h('section', {class: 'fd-mode-selector', 'aria-label': 'Choose how to review'},
      h('h2', {}, 'Verification App'));
  }
  function fdReportSummary(r) {
    const binding = reviewBinding(r), owner = h('details', {class: 'fd-report-summary', open: true});
    const current = () => state.report === r && JSON.stringify(reviewBinding(r)) === JSON.stringify(binding);
    const eligible = fdRecommended(), open = fdItems().filter(it => ['open', 'selected'].includes(it.state));
    const ignorable = open.filter(it => it.fp && it.state === 'open');
    const busy = state.busy || state.rechecking || state.decisionPending || state.aiProposalOperation;
    owner.append(h('summary', {}, 'Quick summary', h('small', {class: 'fd-summary-disclosure'}, 'From recorded findings')),
      fdSummaryPanel(r, binding, owner, current, true),
      (() => {
        const all = state.fixes ? fdItems().filter(it => ['open', 'selected', 'deferred', 'confirmed'].includes(it.state)) : [];
        if (!all.length) return null;
        // One place for the three choices: they act on the ticked errors, or on every error when none is ticked.
        const ticked = fdPicked(all), scope = ticked.length ? ticked : all, some = ticked.length > 0;
        const ai = scope.filter(fdCanAI), off = busy || state.fdAIRun?.running;
        // Values typed earlier and never saved keep their errors "in progress", which is why Fix with AI has nothing
        // to do. Say so, and offer to save them or drop them, instead of a greyed-out button with no reason.
        const typed = fdManualList(all) || fdFreeActive() || !Object.keys(store.get(editsKey(), {})).length ? 0 : readyChanges().total;
        const saveTyped = async () => {
          if (!current()) return;
          const c = readyChanges(); if (!c.total) return;
          const report = state.report, from = revisionOf(report), carried = fdDexLeft();
          const receipt = await applyFixes(c.accepted, c.dec, c.edits, {download: false, binding: c.binding, returnReceipt: true}), next = receipt?.file_hash;
          if (next) fdManualSaved({hash: next, done: true, fixed: c.total, tried: c.total, from, version: revisionOf(receipt), reused: receipt.save_outcome?.type === 'existing_version', originalHash: report.file_hash, manual: true, summary: fdAutoMode(), leftAsWritten: 0,
            items: [...c.accepted.map(f => ({where: `${f.sheet} cell ${f.cell}`, text: '', before: f.before, after: c.dec[f.id]?.after ?? f.after})), ...Object.values(c.edits).map(e => ({where: `${e.sheet} cell ${e.cell}`, text: '', before: e.before, after: e.after}))]}, receipt, carried);
          if (state.report?.file_hash === next) drawSide();
        };
        const dropTyped = () => { if (!current()) return; store.set(editsKey(), {}); store.set(decisionsKey(), {}); state.fdItems = null; drawReport(); toast('Your unsaved typed changes were removed. Nothing in the report was changed.'); };
        if (typed) return h('div', {class: 'fd-fixall'},
          h('p', {class: 'fd-fixall-q'}, `You have ${plural(typed, 'change')} waiting that ${typed === 1 ? 'is' : 'are'} not saved yet. Save ${typed === 1 ? 'it' : 'them'} to finish, or remove ${typed === 1 ? 'it' : 'them'} to start over (for example to use Fix with AI).`),
          h('div', {class: 'fd-cardacts'},
            h('button', {type: 'button', class: 'btn primary', id: 'fd-save-typed', disabled: off || null, onclick: saveTyped}, `Save my ${plural(typed, 'change')}`),
            h('button', {type: 'button', class: 'btn', id: 'fd-review-typed', disabled: off || null, onclick: () => { if (current()) fdManualStart(scope); }}, 'Look at them first'),
            h('button', {type: 'button', class: 'btn', id: 'fd-drop-typed', disabled: off || null, onclick: dropTyped}, 'Remove them')));
        return h('div', {class: 'fd-fixall'},
          h('p', {class: 'fd-fixall-q'}, some ? `${plural(ticked.length, 'possible error')} selected. What would you like to do?` : `What would you like to do with ${all.length === 1 ? 'this possible error' : `these ${all.length} possible errors`}?`),
          h('div', {class: 'fd-cardacts'},
            h('button', {type: 'button', class: 'btn primary', id: 'fd-fix-everything', disabled: off || !(ai.length || (store.get('rd-auto-tidy', true) !== false && scope.some(it => it.fp && it.state === 'open'))) || null, onclick: () => { if (current()) fdAIFixNow(scope, {everything: !some}); }}, some ? 'Fix selected with AI' : 'Fix everything with AI'),
            h('button', {type: 'button', class: 'btn', id: 'fd-fix-all-manual', disabled: off || null, onclick: () => { if (current()) fdManualStart(scope); }}, 'Fix manually'),
            h('button', {type: 'button', class: 'btn', id: 'fd-ignore-all', disabled: off || null, onclick: () => { if (current()) fdReviewDialog(scope, 'ignore'); }}, 'Ignore possible errors')),
          h('p', {class: 'fd-hint'}, ai.length ? (some ? 'Untick everything to work on the whole report.' : 'Dex fixes every error it can in one go and saves a new version. To pick only some, tick them in the list below.') : state.aiOn ? 'Dex has no safe fix for these. Fix them manually or ignore them.' : 'AI is off. Fix these manually or ignore them.'));
      })(),
      h('div', {class: 'fd-cardacts', hidden: true},
        h('button', {type: 'button', class: 'btn', disabled: busy || !eligible.length, onclick: () => { if (current()) fdPreviewRecommended(binding); }}, `Accept All · ${eligible.length} proposed changes`),
        h('button', {type: 'button', class: 'btn', disabled: busy, onclick: () => { if (current()) fdReviewDialog(open, 'manual'); }}, 'Manually Edit'),
        h('button', {type: 'button', class: 'btn', disabled: busy || !ignorable.length, onclick: () => { if (current()) fdReviewDialog(ignorable, null, 'Choose open items to ignore'); }}, `Ignore · ${ignorable.length} open items`)));
    return owner;
  }
  // A rejected application request is not evidence of a provider outage.
  function fdAIRequestFailure(error) {
    const status = Number.isInteger(error?.httpStatus) ? error.httpStatus : null;
    const message = String(error?.message || '');
    const safe = /^(Report access changed\.|The workbook does not match|The review changed\.|Unknown, ambiguous, or changed finding selection\.|A selected finding is now informational\.|Report state changed|Report access, source, assignment, or review changed\.|The report |This report |This version is approved, sent, or superseded\.|Re-check this report before preparing suggestions\.|The source |Source |SharePoint |Sign in to continue\.|Report not found\.|The AI response did not match)/.test(message) && message.length <= 500 && !/[\r\n<>]/.test(message) ? message : '';
    let category = error?.aiCategory || 'unconfirmed', reason;
    if (status === 401) { category = 'signin'; reason = 'Sign in before requesting suggestions.'; }
    else if (status === 403 || status === 404) { category = 'access'; reason = 'The application refused access to this report. Check access and reopen the report.'; }
    else if (status === 409) { category = /source|SharePoint/i.test(safe) ? 'source' : 'stale'; reason = 'The application rejected this report or finding selection. Reload and review its current state before requesting suggestions.'; }
    else if (status) { category = 'server'; reason = 'The application could not complete the suggestion request (HTTP ' + status + '). Provider availability is unconfirmed.'; }
    else if (category === 'response') reason = 'The suggestion response could not be verified. No suggestions from this request were used.';
    else if (category === 'not_started') reason = 'The suggestion request did not start. Finish pending edits or operations and check the selected report and AI setting.';
    else reason = 'No confirmed suggestion response was received. Check the connection and report state; provider availability is unconfirmed.';
    return {category, http_status: status, reason: reason + (safe ? ' ' + safe : '')};
  }
  // Own the read-only proposal request without taking ownership of another save/recheck.
  async function fdPrepareAI(scope, binding, view) {
    const notStarted = () => view.failure(Object.assign(new Error('Suggestion request did not start.'), {aiCategory: 'not_started'}));
    if (state.aiProposalOperation || state.busy || state.rechecking || state.decisionPending || !state.aiOn) { notStarted(); return; }
    const report = state.report, selectedReport = state.selected;
    if (!report || selectedReport != null && selectedReport !== report.file_hash || JSON.stringify(reviewBinding(report)) !== JSON.stringify(binding) || !scope.length || scope.length > 120 || scope.some(it => !it.fp || it.state !== 'open')) { notStarted(); return; }
    if ([...document.querySelectorAll('#side input:not([type="checkbox"]):not([type="radio"]), #side textarea, .fd-review-dialog textarea')].some(node => node.value !== node.defaultValue)) {
      toast('Finish or save your typed draft before requesting AI proposals. Your text is unchanged.', true); notStarted(); return;
    }
    const controls = new Map();
    const operation = {owner: view.owner, cancel: () => {
      if (state.aiProposalOperation !== operation) return;
      state.aiProposalOperation = null;
      if (!state.busy && !state.rechecking && !state.decisionPending) controls.forEach((disabled, node) => { if (node.isConnected) node.disabled = disabled; });
    }, lock: () => {
      if (state.report !== report || state.selected !== selectedReport) { operation.cancel(); return; }
      document.querySelectorAll('#side button, #side input, #side textarea, #side select, #fd-preview-btn, .fd-mode-selector button, .fd-review-dialog button, .fd-review-dialog input, .fd-review-dialog textarea, dialog .primary').forEach(node => {
        if (node.dataset?.aiCancel === 'true' || node.dataset?.selection === 'true') return;
        if (!controls.has(node)) controls.set(node, node.disabled);
        node.disabled = true;
      });
    }};
    state.aiProposalOperation = operation; operation.lock(); view.pending();
    const current = () => state.aiProposalOperation === operation && state.report === report && state.selected === selectedReport && JSON.stringify(reviewBinding(report)) === JSON.stringify(binding) && view.active();
    try {
      const result = await api('/api/spreadsheets/ai-proposals', {file_hash: binding.file_hash, expected_check_hash: binding.expected_check_hash,
        expected_rev: binding.expected_rev, run: binding.expected_run, ids: scope.map(it => it.key), seen: Object.fromEntries(scope.map(it => [it.key, it.fp]))});
      if (!current()) return;
      const answer = result.ai_proposals;
      if (!answer || answer.status !== 'done' || answer.file_hash !== binding.file_hash || answer.check_hash !== binding.expected_check_hash ||
          JSON.stringify([...(answer.ids || [])].sort()) !== JSON.stringify(scope.map(it => it.key).sort()) || !Array.isArray(answer.proposals) ||
          answer.proposals.some(p => !scope.some(it => it.key === p.finding_id) || p.recommendation_eligible !== false || !(p.source === 'AI fix (asked)' && p.sure === false || ['Clean-up', 'Number as text', 'Spelling'].includes(p.source) && p.sure === true && p.selected_finding_id === p.finding_id && scope.some(it => it.key === p.finding_id && it.fp === p.selected_finding_fp)))) throw Object.assign(new Error('The AI response did not match the selected scope and safety labels. No proposals were used.'), {aiCategory: 'response'});
      view.success(answer);
    } catch (error) { if (current()) view.failure(error); }
    finally { operation.cancel(); }
  }
  // AI provides a read-only explanation; the recorded inventory and action scopes stay authoritative.
  function fdSummaryInventory(answer, loadedKeys) {
    const counts = answer.counts, groups = answer.groups;
    const validCount = n => Number.isSafeInteger(n) && n >= 0;
    if (!counts || !['available', 'active', 'dismissed'].every(k => validCount(counts[k])) || counts.active + counts.dismissed !== counts.available || !Array.isArray(groups)) throw new Error('The server finding inventory is inconsistent. Reload this review.');
    const ids = new Set(), groupIds = new Set(); let total = 0;
    for (const group of groups) {
      if (!group || typeof group.id !== 'string' || groupIds.has(group.id) || !validCount(group.count) || !Array.isArray(group.ids) || group.ids.length !== group.count ||
          ['code', 'source', 'severity', 'state'].some(k => typeof group[k] !== 'string')) throw new Error('The server finding groups are inconsistent. Reload this review.');
      groupIds.add(group.id); total += group.count;
      for (const id of group.ids) { if (typeof id !== 'string' || ids.has(id)) throw new Error('The server finding identities are ambiguous. Reload this review.'); ids.add(id); }
    }
    if (total !== counts.available) throw new Error('The server finding totals do not match the groups. Reload this review.');
    const loaded = [...ids].filter(id => loadedKeys.has(id)).length;
    return h('section', {class: 'fd-server-inventory', 'aria-label': 'Server finding inventory'},
      h('h4', {}, 'Server finding inventory'),
      h('p', {}, `${counts.available} available findings: ${counts.active} active records and ${counts.dismissed} set-aside records, across ${groups.length} groups.`),
      h('p', {class: 'fd-hint'}, `${loaded} of these finding identities are loaded in this review. This inventory is read-only; the separate review actions below apply only to their exact loaded scope. Informational and historical records may not have actions.`),
      loaded < ids.size ? h('p', {class: 'fd-coverage'}, `${ids.size - loaded} available findings are not loaded here. Close and reopen the report to load its current finding pages before acting on missing items. No partial group action is offered from this inventory.`) : null,
      h('details', {open: true}, h('summary', {}, `Recorded finding groups (${groups.length})`),
        h('ul', {class: 'fd-summary-scroll', tabindex: '0', 'aria-label': 'Read-only recorded finding groups'}, groups.map(group => h('li', {},
          h('b', {}, `${group.code}: ${group.count}`), ` · ${group.severity} · ${group.source} · ${group.state}. ${group.ids.filter(id => loadedKeys.has(id)).length} of ${group.count} identities loaded.`)))));
  }
  // A plain summary from the error list itself, used whenever Dex's own words are not available.
  function fdPlainSummary() {
    // Derive from this report now, not the sidebar's possibly older item cache.
    const open = fdItems().filter(it => ['open', 'selected', 'deferred', 'confirmed'].includes(it.state));
    if (!open.length) return 'No unresolved possible errors in the loaded findings. This does not establish source completeness or readiness.';
    const groups = fdGroups(open).map(g => [g.items.length, g.title.replace(/ \(and \d+ more like it\)$/, '')]).sort((a, b) => b[0] - a[0]);
    const top = groups.slice(0, 3).map(([n, title]) => `${n} × ${title.charAt(0).toLowerCase() + title.slice(1)}`);
    return `The loaded findings contain ${plural(open.length, 'possible error')} in ${plural(groups.length, 'group')}. The biggest: ${top.join('; ')}${groups.length > 3 ? `; and ${groups.length - 3} more ${groups.length - 3 === 1 ? 'group' : 'groups'}` : ''}.`;
  }
  function fdSummaryContext(report) {
    return JSON.stringify({assignment: report.assignment, holds: report.holds, source: report.source_reconciliation,
      sharepoint: report.review?.sharepoint, coverage: report.review?.coverage, page: report.review?.page});
  }
  function fdSummaryPanel(report, binding, owner, current, compact = false) {
    const context = fdSummaryContext(report), stillCurrent = () => current() && fdSummaryContext(report) === context;
    const issues = [...new Map([...allIssues(report.review), ...(report.review.dismissed || [])].map(i => [i.key, i])).values()];
    const incomplete = report.review.page?.complete === false || report.review.coverage?.truncated || report.review.coverage?.not_listed;
    const counts = ['high', 'medium', 'low', 'info'].map(severity => `${issues.filter(i => i.severity === severity).length} ${severity === 'high' ? 'high severity' : severity === 'info' ? 'informational' : severity}`).join(' · ');
    const output = h('div', {class: 'fd-summary-answer', 'aria-live': 'polite'}, compact ? (state.fdSummaryWanted ? h('p', {role: 'status'}, 'Getting the report ready. The summary starts by itself in a moment…') : h('p', {class: 'fd-summary-text'}, fdPlainSummary())) : null);
    const renderAnswer = (answer, inventory) => {
      if (compact) {
        const narrative = answer.reply.trim(), short = narrative.length > 360 ? narrative.slice(0, 357) + '…' : narrative;
        output.replaceChildren(h('p', {class: 'fd-summary-text'}, short || fdPlainSummary()),
          h('details', {class: 'fd-summary-more'}, h('summary', {}, 'More detail'),
            h('div', {class: 'fd-summary-detail-body', tabindex: '0'},
              narrative ? h('p', {}, narrative) : null, h('p', {}, answer.narrative_notice), inventory,
              ...(report.holds || []).map(hold => h('p', {}, hold.text || hold.code)), h('p', {}, answer.notice))));
        return;
      }
      output.replaceChildren(inventory, h('h4', {}, answer.narrative_status === 'done' ? 'AI explanation of available findings' : 'AI explanation unavailable'),
          h('p', {class: 'fd-hint fd-summary-scope'}, `${answer.coverage.label} ${answer.narrative_notice || ''}`.trim()),
          answer.reply ? h('details', {open: true}, h('summary', {}, 'Read AI explanation'), h('p', {class: 'fd-summary-text fd-summary-scroll', tabindex: '0', 'aria-label': 'AI explanation text'}, answer.reply)) : null, h('p', {class: 'fd-hint'}, answer.notice),
          null);
    };
    if (state.fdSummaryCache?.report === report && state.fdSummaryCache?.context === context && state.fdSummaryCache?.key === JSON.stringify(binding)) {
      const answer = state.fdSummaryCache.answer;
      renderAnswer(answer, fdSummaryInventory(answer, new Set(issues.map(i => i.key))));
    }
    const pendingText = 'Summarizing the available findings for this exact report version. No changes are being made.';
    const pending = state.aiProposalOperation?.summary;
    if (pending?.report === report && pending.key === JSON.stringify(binding) && pending.context === context) output.replaceChildren(h('p', {role: 'status'}, pendingText));
    const button = h('button', {type: 'button', class: 'btn fd-summary-request', disabled: !state.aiOn, onclick: async () => {
      // While the report is still being made ready (recheck, formatting clean-up, SharePoint check) it reloads, which
      // would throw a summary away. The request is remembered and starts by itself once the report holds still.
      if (typeof fdBackgroundBusy === 'function' && state.aiOn && (fdBackgroundBusy() || state.busy || state.rechecking)) {
        output.replaceChildren(h('p', {role: 'status'}, 'Getting the report ready. The summary starts by itself in a moment…'));
        fdSummaryWhenReady(); return;
      }
      if (!stillCurrent() || !state.aiOn || state.aiProposalOperation || state.busy || state.rechecking || state.decisionPending) return;
      if ([...document.querySelectorAll('#side input:not([type=checkbox]):not([type=radio]), #side textarea, .fd-review-dialog input:not([type=checkbox]), .fd-review-dialog textarea')].some(node => node.value !== node.defaultValue)) {
        toast('Finish or save your typed draft before asking for a summary. Your text is unchanged.', true); return;
      }
      const controls = new Map();
      const operation = {owner, summary: {report, key: JSON.stringify(binding), context}, cancel: () => {
        if (state.aiProposalOperation !== operation) return;
        state.aiProposalOperation = null;
        if (!state.busy && !state.rechecking && !state.decisionPending) controls.forEach((disabled, node) => { if (node.isConnected) node.disabled = disabled; });
      }, lock: () => {
        if (!stillCurrent()) { operation.cancel(); return; }
        document.querySelectorAll('#side button, #side input, #side textarea, #side select, #fd-preview-btn, .fd-mode-selector button, .fd-review-dialog button, .fd-review-dialog input, .fd-review-dialog textarea, dialog .primary').forEach(node => {
          if (node.dataset?.aiCancel === 'true' || node.dataset?.selection === 'true') return;
          if (!controls.has(node)) controls.set(node, node.disabled);
          node.disabled = true;
        });
      }};
      state.aiProposalOperation = operation; operation.lock();
      output.replaceChildren(h('p', {role: 'status'}, pendingText));
      const active = () => state.aiProposalOperation === operation && stillCurrent() && owner.isConnected;
      try {
        const answer = await api('/api/spreadsheets/review-summary', {scope: 'available_findings', file_hash: binding.file_hash,
          expected_check_hash: binding.expected_check_hash, expected_rev: binding.expected_rev, run: binding.expected_run});
        // The panel may have been redrawn while Dex was writing: keep the answer for the same report version and show it there.
        const live = active();
        if (!live && !(stillCurrent() && state.aiProposalOperation === operation)) { if (owner.isConnected && !stillCurrent()) output.replaceChildren(h('p', {role: 'status'}, 'Report changed; summary discarded. Reopen the current review.')); return; }
        if (!answer || answer.read_only !== true || answer.file_hash !== binding.file_hash || answer.check_hash !== binding.expected_check_hash ||
            answer.expected_rev !== binding.expected_rev || JSON.stringify(answer.run) !== JSON.stringify(binding.expected_run) || typeof answer.reply !== 'string' || typeof answer.notice !== 'string' || typeof answer.coverage?.label !== 'string' || typeof answer.narrative_notice !== 'string') throw new Error('The summary did not match the current report review. Reload before trying again.');
        const inventory = fdSummaryInventory(answer, new Set(issues.map(i => i.key)));
        state.fdSummaryCache = {key: JSON.stringify(binding), context, report, answer};
        operation.summary = null; // A replacement panel must render the completed answer, not pending text.
        if (live) renderAnswer(answer, inventory);
        else if (compact) { const shown = document.querySelector('.fd-report-summary'); if (shown && state.report === report) shown.replaceWith(fdReportSummary(report)); }
      } catch (error) {
        // A redraw must not hide a failed explicitly requested summary.
        const target = active() ? output : compact && stillCurrent() && state.aiProposalOperation === operation
          ? document.querySelector('.fd-report-summary .fd-summary-answer') : null;
        if (target) target.replaceChildren(h('p', {role: 'alert'}, `Summary unavailable: ${error.message}. The recorded findings and actions below are unchanged.`));
        else if (owner.isConnected && !stillCurrent()) output.replaceChildren(h('p', {role: 'status'}, 'Report changed; summary discarded. Reopen the current review.'));
      }
      finally {
        operation.cancel();
        // If the panel was redrawn while Dex was writing, its new buttons were drawn greyed out and nothing switched
        // them back on. Give the three choices the state they have now that the summary is finished.
        try {
          const shown = document.querySelector('.fd-report-summary .fd-fixall');
          const fresh = shown && state.report === report ? fdReportSummary(report).querySelector('.fd-fixall') : null;
          const now = shown ? [...shown.querySelectorAll('button')] : [], want = fresh ? [...fresh.querySelectorAll('button')] : [];
          if (want.length && now.length === want.length) now.forEach((node, index) => { node.disabled = want[index].disabled; });
        } catch (e) { /* the next redraw sets them */ }
      }
    }}, state.aiOn ? 'Generate AI summary' : 'AI summary unavailable: AI is off');
    if (compact) return h('section', {class: 'fd-review-summary fd-summary-compact', 'aria-label': 'Findings summary'},
      output,
      incomplete ? h('p', {class: 'fd-coverage'}, 'Coverage incomplete. Unavailable or unlisted findings are outside this quick summary; do not treat it as a complete error list.') : null,
      h('p', {class: 'fd-hint'}, 'Only when you choose Generate AI summary, Dex (AI by Anthropic) receives bounded aggregate finding-group metadata for this report, not workbook contents. Summary text never applies changes.'),
      button,
      h('details', {class: 'fd-summary-about'}, h('summary', {}, 'About this summary'),
        h('p', {class: 'fd-hint'}, `${issues.length} loaded finding records, including information and history. This quick summary is based on recorded findings; it is not proof of source completeness or readiness.`),
        h('button', {type: 'button', class: 'btn', onclick: () => { if (stillCurrent()) fdReviewDialog(); }}, 'Go to review actions')));
    return h('section', {class: 'fd-review-summary', 'aria-label': 'Findings summary'}, h('h3', {}, 'Findings summary'),
      h('p', {}, `${issues.length} loaded finding records, including set-aside history and information: ${counts}.`),
      incomplete ? h('p', {class: 'fd-coverage'}, 'Coverage incomplete. This is not a summary of every possible error; unavailable or unlisted findings remain outside this review.') : h('p', {}, 'All currently loaded finding records are counted. A finding-free report is not proof of source completeness or readiness.'),
      ...(report.holds || []).map(hold => h('p', {class: 'fd-coverage'}, hold.text || hold.code)),
      h('p', {class: 'fd-hint'}, 'On request, Dex (AI by Anthropic) receives bounded finding-group metadata for this report only, not workbook contents or unrelated reports. AI summarizes; it cannot apply changes or ignore findings.'), button, h('button', {type: 'button', class: 'btn', onclick: () => { const toolbar = owner.querySelector('.fd-review-toolbar'); if (!toolbar) { fdReviewDialog(); return; } toolbar?.scrollIntoView({block: 'start'}); toolbar?.querySelector('button')?.focus({preventScroll: true}); }}, 'Go to review actions'), output);
  }
  function fdManualCellHeld(items, sheet, row, cell) {
    return items.some(other => other.sheet === sheet && (other.cell === cell || !other.cell && other.row === row) &&
      (fdConflict(other) || ['kept', 'deferred', 'rejected', 'kept_earlier'].includes(other.state)));
  }
  // A review scope is separate from the correction basket. Opening/selecting never writes data.
  function fdReviewDialog(initialScope = null, initialAction = null, filterLabel = null) {
    if (state.gridDraft && !gridDiscard()) return;
    if (!state.report || state.busy || state.rechecking || state.decisionPending || state.aiProposalOperation) return;
    if (Array.isArray(initialScope) && initialScope.some(it => JSON.stringify(it.binding) !== JSON.stringify(reviewBinding(state.report)))) { toast('The report changed. Reopen this review action.', true); return; }
    const report = state.report, binding = reviewBinding(report);
    const filterKeys = filterLabel && Array.isArray(initialScope) ? new Set(initialScope.map(it => it.key)) : null;
    const items = fdItems().filter(it => !filterKeys || filterKeys.has(it.key)), groups = fdGroups(items);
    const saved = state.fdReviewSelection;
    const selected = new Set(saved?.binding === JSON.stringify(binding) ? saved.keys : []), pages = new Map(), opener = document.activeElement;
    const dlg = h('dialog', {class: 'fd-review-dialog', 'aria-labelledby': 'fd-review-title'});
    const current = () => state.report === report && (state.selected == null || state.selected === report.file_hash) && JSON.stringify(reviewBinding(state.report)) === JSON.stringify(binding);
    const guard = () => { if (current()) return true; toast('The report changed. Close and reopen Needs review before continuing.', true); return false; };
    const picked = () => items.filter(it => selected.has(it.key));
    const status = h('p', {role: 'status'}), actions = h('div', {class: 'fd-cardacts'}), decision = h('div');
    const content = h('div', {class: 'fd-review-groups'}), types = h('div', {class: 'fd-review-types'});
    const byType = new Map();
    for (const it of items) { const type = it.issue?.code || it.kind?.code || it.group || 'Other findings'; if (!byType.has(type)) byType.set(type, []); byType.get(type).push(it); }
    const unresolved = items.filter(it => ['open', 'selected', 'deferred', 'confirmed', 'claude'].includes(it.state));
    let actionVersion = 0;
    const hasDraft = () => [...decision.querySelectorAll('input:not([type=checkbox]), textarea')].some(el => el.value !== el.defaultValue);
    const discard = () => !hasDraft() || window.confirm('Discard the typed values or reason in this review action?');
    const close = () => { if (!state.decisionPending && discard()) dlg.close(); };
    dlg.addEventListener('cancel', e => { if (state.decisionPending || !discard()) e.preventDefault(); });
    dlg.addEventListener('close', () => { if (state.aiProposalOperation?.owner === dlg) state.aiProposalOperation.cancel(); dlg.remove(); if (opener?.isConnected && opener.getClientRects().length) opener.focus(); else { const fallback = document.getElementById('fd-open-review'); fallback?.focus(); } });
    const choose = (list, on) => {
      const pending = !!state.aiProposalOperation;
      if (!pending && !discard()) { draw(); return; }
      actionVersion++;
      const label = document.activeElement?.getAttribute('aria-label'), focusKey = document.activeElement?.getAttribute('data-review-key');
      list.forEach(it => on ? selected.add(it.key) : selected.delete(it.key));
      state.fdReviewSelection = {binding: JSON.stringify(binding), keys: [...selected]};
      if (!pending) { dlg.classList.remove('fd-manual-active'); decision.replaceChildren(); }
      draw(); if (pending) state.aiProposalOperation?.lock?.();
      if (label || focusKey) [...dlg.querySelectorAll('input')].find(el => focusKey ? el.getAttribute('data-review-key') === focusKey : el.getAttribute('aria-label') === label)?.focus();
    };
    const prepareAI = (scope = picked()) => fdPrepareAI(scope, binding, {
      owner: dlg,
      active: () => dlg.isConnected && current(),
      pending: () => decision.replaceChildren(h('p', {role: 'status'}, 'Dex is working out fixes for your selection. No workbook changes yet.')),
      failure: error => decision.replaceChildren(h('p', {role: 'alert'}, `AI proposals unavailable: ${error.message}. No workbook changes were made. Review manually or keep/defer findings.`)),
      success: answer => {
        const include = new Set(), proposals = answer.proposals;
        const preview = h('button', {type: 'button', class: 'btn primary', disabled: true, onclick: () => {
          if (!guard() || state.aiProposalOperation) return;
          const accepted = proposals.filter((p, i) => include.has(i)); if (!accepted.length) return;
          const dec = Object.fromEntries(accepted.map(p => [p.id, {state: 'accepted', after: p.after}]));
          const capture = captureCorrections(accepted, dec, {}, binding, report); fdPreview({...capture, independentScope: true, onApplied: () => { dlg.close(); dlg.remove(); }, onRemove: row => {
            proposals.forEach((p, i) => { if (p.sheet === row.sheet && p.cell === row.cell) { include.delete(i); const box = decision.querySelectorAll('input[type=checkbox]')[i]; if (box) box.checked = false; } });
            preview.disabled = !include.size; preview.textContent = `Preview ${include.size} selected AI proposals`;
          }});
        }}, 'Preview selected AI proposals');
        if (!proposals.length) {
          decision.replaceChildren(h('h3', {}, 'AI could not suggest a fix for these'),
            h('p', {}, `Nothing was changed. Fix ${scope.length === 1 ? 'it' : 'them'} manually or ignore ${scope.length === 1 ? 'it' : 'them'}. You can keep ticking boxes.`),
            (answer.skipped || []).length ? h('details', {}, h('summary', {}, 'Why'), h('ul', {}, answer.skipped.slice(0, 50).map(p => h('li', {}, p.reason)))) : null);
          decision.querySelector('h3')?.setAttribute('tabindex', '-1'); decision.querySelector('h3')?.focus({preventScroll: true}); decision.scrollIntoView({block: 'nearest'});
          return;
        }
        decision.replaceChildren(h('h3', {}, `${proposals.length} AI proposals, not applied`),
          h('p', {}, 'AI suggestions are not verified source values. Check each proposal before including it. Existing cell safety checks and source holds still apply.'),
          h('ul', {}, proposals.map((p, i) => h('li', {}, h('label', {}, h('input', {type: 'checkbox', 'aria-label': `Include AI proposal ${p.sheet} ${p.cell}`, onchange: e => {
            e.target.checked ? include.add(i) : include.delete(i); preview.disabled = !include.size; preview.textContent = `Preview ${include.size} selected AI proposals`;
          }}), ` ${p.sheet} ${p.cell}: ${showWs(p.before)} → ${showWs(p.after)}. ${p.reason}`)))),
          ...(answer.skipped || []).map(p => h('p', {class: 'fd-hint'}, `No proposal for ${p.finding_id}: ${p.reason}`)), preview);
        decision.querySelector('h3')?.setAttribute('tabindex', '-1'); decision.querySelector('h3')?.focus({preventScroll: true});
        decision.scrollIntoView({block: 'nearest'});
      }
    });
    const decisionForm = (action, scope = picked()) => {
      if (!guard()) return;
      const defer = action === 'defer';
      if (!scope.length || scope.some(it => !it.fp || !['open', 'claude'].includes(it.state))) return;
      if (!defer && scope.length > 1 && scope.some(it => it.issue?.severity === 'high')) {
        toast('Blockers must be kept as recorded one at a time, with evidence.', true); return;
      }
      const included = new Set(scope.map(it => it.key));
      const count = h('p', {role: 'status'}, `${scope.length} findings included in this decision`);
      const reason = h('textarea', {required: true, rows: 2, 'aria-label': 'Reason for selected findings'});
      const evidence = h('input', {'aria-label': 'Evidence for selected findings', placeholder: 'Evidence references, separated by semicolons'});
      const owner = defer ? h('input', {'aria-label': 'Source owner', placeholder: 'Who needs to confirm this?'}) : null;
      decision.replaceChildren(h('form', {onsubmit: async e => {
        e.preventDefault(); if (!guard() || state.decisionPending || state.busy || state.rechecking || state.aiProposalOperation) return;
        const chosen = scope.filter(it => included.has(it.key));
        if (!chosen.length) { toast('Include at least one finding. Nothing was saved.', true); return; }
        const why = reason.value.trim(), ev = evidence.value.split(';').map(x => x.trim()).filter(Boolean);
        if (!why) { reason.focus(); return; }
        if (!defer && chosen.some(it => it.issue?.severity === 'high') && !ev.length) { evidence.focus(); toast('Evidence is required to keep a blocker as recorded.', true); return; }
        const locked = new Map([...dlg.querySelectorAll('button, input, textarea, select')].map(node => [node, node.disabled]));
        locked.forEach((wasDisabled, node) => { node.disabled = true; });
        try { await fdSendDecision({...findingBinding(chosen), ids: chosen.map(it => it.key), action, reason: why, evidence: ev, owner: owner?.value.trim() || '', from: chosen[0], batch: chosen.length > 1}); }
        finally { locked.forEach((wasDisabled, node) => { if (node.isConnected) node.disabled = wasDisabled; }); }
        if (!state.decisionFailed && !current()) dlg.close();
        else if (state.decisionFailed) decision.append(h('p', {role: 'alert'}, state.decisionFailed.message + ' Your typed reason is preserved here.'));
      }}, h('h3', {}, `${defer ? 'Send to the source owner' : 'Ignore'}: ${scope.length} ${scope.length === 1 ? 'error' : 'errors'}`),
      h('p', {class: 'fd-hint'}, 'The spreadsheet is not changed. Your reason is saved with the report.'),
      h('label', {}, defer ? 'What needs confirming?' : 'Why are you ignoring this?', reason), owner, h('label', {}, 'Evidence (needed for serious errors)', evidence),
      h('div', {class: 'fd-cardacts'}, h('button', {type: 'submit', class: 'btn primary'}, defer ? 'Save: awaiting source owner' : 'Save: keep as recorded'),
      h('button', {type: 'button', class: 'btn', onclick: () => { if (discard()) decision.replaceChildren(); }}, 'Cancel decision')),
      count, h('ul', {class: 'fd-decision-list'}, scope.map(it => h('li', {}, h('label', {}, h('input', {type: 'checkbox', checked: true, 'data-decision-key': it.key, 'aria-label': `Include ${it.sheet || 'Report'} ${it.cell || (it.row ? 'row ' + it.row : 'report finding')}, finding ${items.indexOf(it) + 1} of ${items.length} in this decision`, onchange: e => { e.target.checked ? included.add(it.key) : included.delete(it.key); count.textContent = `${included.size} findings included in this decision`; }}), `${it.sheet || 'Report'} ${it.cell || (it.row ? 'row ' + it.row : '')}: ${it.text || it.reason}`)))),
      ));
      reason.focus();
    };
    // Direct actions never mutate the global review selection or correction basket.
    const manual = async scope => {
      const version = ++actionVersion;
      dlg.classList.add('fd-manual-active');
      const box = h('section', {class: 'fd-inline-manual', 'aria-label': 'Fix manually in this review'});
      decision.replaceChildren(box);
      box.append(h('h3', {}, `Fix manually: ${scope.length} review items`), h('p', {}, 'Loading exact recorded cells. Nothing is selected for Apply yet.'), h('button', {type: 'button', class: 'btn', onclick: close}, 'Close review'));
      const rows = new Map(), cells = new Map(), unavailable = [];
      for (const it of scope) {
        if (!it.sheet || !it.row || fdConflict(it) || !['open', 'selected', 'confirmed'].includes(it.state)) {
          unavailable.push(`${it.sheet || 'Report'} ${it.cell || 'row ' + (it.row || 'unknown')}: ${fdConflict(it) ? 'Source conflict: await the source owner, then use coordinated correction.' : 'No editable current cell. Review source evidence or reopen the finding.'}`); continue;
        }
        try {
          const key = JSON.stringify([it.sheet, it.row]);
          if (!rows.has(key)) rows.set(key, await api('/api/spreadsheets/cells', {file_hash: binding.file_hash, sheet: it.sheet, row: it.row}));
          if (!current() || !dlg.isConnected || version !== actionVersion) return;
          const row = rows.get(key);
          const candidates = row.sheet === it.sheet && Array.isArray(row.cells) ? row.cells.filter(c => it.cell ? c.cell === it.cell : true) : [];
          const editable = candidates.filter(c => !c.formula && typeof c.before === 'string' && splitCell(c.cell)?.[1] === it.row &&
            !fdManualCellHeld(fdItems(), it.sheet, it.row, c.cell));
          if (editable.length < candidates.length) unavailable.push(`${it.sheet} row ${it.row}: ${candidates.length - editable.length} protected, formula, or previously decided cells are not editable here.`);
          if (!editable.length) throw new Error('Cell is unavailable or is a protected formula');
          for (const cell of editable) cells.set(editId(it.sheet, cell.cell), {it: {...it, cell: cell.cell}, cell, rowContext: !it.cell});
        } catch (error) { unavailable.push(`${it.sheet} ${it.cell}: ${error.message}. Review the original and source evidence; do not guess a value.`); }
      }
      if (!current() || !dlg.isConnected || version !== actionVersion) return;
      const edits = new Map();
      const preview = h('button', {type: 'button', class: 'btn primary', disabled: true, onclick: () => {
        if (!guard() || state.aiProposalOperation) return;
        const included = Object.fromEntries([...edits].filter(([, e]) => e.include.checked && e.input.value !== e.cell.before).map(([id, e]) => [id,
          {sheet: e.it.sheet, cell: e.it.cell, before: e.cell.before, after: e.input.value, header: e.cell.header || '', kind: 'auto', source: 'Your edit', reason: 'Typed by the reviewer in scoped review'}]));
        if (!Object.keys(included).length) return;
        // Keep this modal and its typed draft behind the preview, including on Back.
        fdPreview({...captureCorrections([], {}, included, binding, report), independentScope: true, onApplied: () => { dlg.close(); dlg.remove(); }, onRemove: row => {
          for (const e of edits.values()) if (e.it.sheet === row.sheet && e.it.cell === row.cell) { e.include.checked = false; e.include.dispatchEvent(new Event('change')); }
          update();
        }});
      }}, 'Preview manual changes');
      const update = () => {
        const n = [...edits.values()].filter(e => e.include.checked && e.input.value !== e.cell.before).length;
        preview.disabled = !n; preview.textContent = `Preview ${n} manual changes`;
        for (const e of edits.values()) e.status.textContent = e.input.value === e.cell.before ? 'Unchanged: not included in preview.' : !e.include.checked ? 'Changed but excluded from preview. Check Include to use this value.' : e.input.value === '' ? 'Included: this will clear the cell to blank, not zero.' : 'Included in preview. Nothing is applied yet.';
      };
      box.replaceChildren(h('h3', {}, `Fix manually: ${scope.length} review items`),
        h('p', {}, `${cells.size} distinct editable cells · ${unavailable.length} unavailable items. Shared-cell findings use one edit. Typing a changed value includes it in preview; uncheck Include to leave it out. Empty means clear to blank, not zero. All source holds remain.`),
        h('div', {class: 'fd-manual-preview'}, preview, h('button', {type: 'button', class: 'btn', onclick: close}, 'Close review')),
        ...[...cells].map(([id, e]) => {
          let explicitExclude = false;
          const input = h('input', {value: e.cell.before, 'aria-label': `New value for ${e.it.sheet}!${e.it.cell}`, oninput: () => {
            if (!explicitExclude) include.checked = input.value !== e.cell.before;
            update();
          }});
          const include = h('input', {type: 'checkbox', 'aria-label': `Include manual change ${e.it.sheet}!${e.it.cell}`, onchange: () => { explicitExclude = !include.checked; update(); }});
          const status = h('p', {class: 'fd-hint', role: 'status'}, 'Unchanged: not included in preview.');
          edits.set(id, {...e, input, include, status});
          return h('div', {class: 'fd-inline-cell'}, h('b', {}, `${e.it.sheet} ${e.it.cell}`), h('p', {}, e.it.text || e.it.reason),
            h('p', {}, `${e.cell.header || 'Column ' + e.it.cell.replace(/[0-9]+$/, '')} · Recorded: ${showWs(e.cell.before)}`), e.rowContext ? h('p', {class: 'fd-hint'}, 'This finding names a row, not a specific cell. Choose the correct field using source evidence; do not guess missing values.') : null, h('label', {}, 'Your value', input), h('label', {}, include, ' Include this cell in preview'), status);
        }), ...unavailable.map(text => h('p', {class: 'fd-coverage'}, text)),
        unavailable.length ? h('button', {type: 'button', class: 'btn', onclick: () => { if (!discard()) return; dlg.close(); state.fdView = 'sources'; drawReport(); }}, 'Review source evidence and recovery') : null);
      box.querySelector('h3').setAttribute('tabindex', '-1'); box.querySelector('h3').focus();
    };
    const act = (scope, action) => {
      // AI fix and Fix manually work on the errors as they are right now, so a background update of the same saved
      // version (a recheck or the SharePoint check finishing) never makes these two buttons do nothing.
      const sameVersion = !!state.report && state.report.file_hash === report.file_hash && (state.selected == null || state.selected === report.file_hash);
      if (action === 'ai' || (action === 'manual' && fdAutoMode())) {
        if (!sameVersion) { guard(); return; }
        if (state.aiProposalOperation || state.decisionPending || state.busy || !discard()) { if (state.busy || state.decisionPending || state.aiProposalOperation) toast('Another step is still finishing. Try again in a moment.'); return; }
        const keys = new Set(scope.map(it => it.key)), live = fdItems().filter(it => keys.has(it.key));
        if (!live.length) { toast('Tick at least one error first.'); return; }
        actionVersion++; dlg.close(); dlg.remove();
        if (action === 'ai') fdAIFixNow(live); else fdManualStart(live);
        return;
      }
      if (!guard() || state.aiProposalOperation || state.decisionPending || state.busy || state.rechecking || !discard()) return;
      actionVersion++; dlg.classList.remove('fd-manual-active'); decision.replaceChildren();
      if (action === 'manual') manual(scope);   // the step-by-step manual review kept for the non-automatic mode
      else decisionForm('reject', scope);
    };
    const direct = scope => fdDirectControls(scope, act, true);
    const draw = () => {
      const scope = picked(), eligible = fdRecommended(scope), canDecide = scope.length > 0 && scope.length <= 1000 && scope.every(it => it.fp && ['open', 'claude'].includes(it.state));
      status.textContent = `${scope.length} of ${items.length} selected.${filterKeys && selected.size > scope.length ? ' ' + (selected.size - scope.length) + ' selections outside this status are saved and excluded from these actions.' : ''} ${eligible.length ? `${eligible.length} ready-made fixes in this selection.` : ''}${scope.length && !canDecide ? ' Keep/defer unavailable: choose at most 1000 open findings, excluding standalone proposals and previously decided items.' : ''}`;
      actions.replaceChildren(
        h('button', {type: 'button', class: 'btn', disabled: !state.aiOn || !scope.some(it => it.fp && it.state === 'open'), onclick: () => act(picked(), 'ai')}, state.aiOn ? 'AI fix selected' : 'AI fix unavailable (AI is off)'),
        !eligible.length ? null : h('button', {type: 'button', class: 'btn primary', onclick: () => {
          if (!guard() || !discard()) return;
          const accepted = eligible.map(it => it.fix), dec = Object.fromEntries(accepted.map(f => [f.id, {state: 'accepted', after: f.after}]));
          const capture = {...captureCorrections(accepted, dec, {}, binding, report), recommendedOnly: true};
          dlg.close(); fdPreview(capture);
        }}, `Preview ${eligible.length} supported changes`),
        h('button', {type: 'button', class: 'btn', disabled: !scope.length, onclick: () => act(picked(), 'manual')}, 'Fix selected manually'),
        h('button', {type: 'button', class: 'btn', disabled: !canDecide, onclick: () => act(picked(), 'ignore')}, 'Ignore selected…'),
        h('button', {type: 'button', class: 'btn subtle', disabled: !canDecide, onclick: () => { if (discard()) { actionVersion++; decisionForm('defer'); } }}, 'Defer to source owner…'));
      types.replaceChildren(h('h3', {}, 'Tick a whole type of error'), ...[...byType].map(([type, list]) => {
        const count = list.filter(it => selected.has(it.key)).length;
        const toggle = h('input', {type: 'checkbox', 'data-selection': 'true', checked: count === list.length || null, 'aria-label': 'Select error type ' + type, onchange: e => choose(list, e.target.checked)});
        toggle.indeterminate = count > 0 && count < list.length;
        const label = kindNames(list[0].kind || list[0].issue || {})[1] || kindNames(list[0].kind || list[0].issue || {})[0] || type;
        return h('label', {}, toggle, ` ${label}: ${count} of ${list.length} selected`);
      }));
      content.replaceChildren(...groups.map((g, index) => {
        const page = pages.get(g.key) || 0, count = g.items.filter(it => selected.has(it.key)).length;
        const toggle = h('input', {type: 'checkbox', 'data-selection': 'true', checked: count === g.items.length || null, 'aria-label': `Select error group ${index + 1}: ${g.title}`, onchange: e => choose(g.items, e.target.checked)});
        toggle.indeterminate = count > 0 && count < g.items.length;
        return h('section', {class: 'fd-review-group'}, h('h3', {}, g.title),
          h('label', {}, toggle, ` Select this group (${count} of ${g.items.length} selected)`),
          direct(g.items),
          h('p', {}, `Items ${page * 25 + 1}–${Math.min((page + 1) * 25, g.items.length)} of ${g.items.length}`),
          h('ul', {}, g.items.slice(page * 25, (page + 1) * 25).map(it => h('li', {'data-direct-finding': it.key}, h('label', {},
            h('input', {type: 'checkbox', 'data-selection': 'true', checked: selected.has(it.key) || null, 'data-review-key': it.key, 'aria-label': `Select ${it.sheet || 'Report'} ${it.cell || 'report finding'}, occurrence ${items.indexOf(it) + 1} of ${items.length}`, onchange: e => choose([it], e.target.checked)}),
            ` ${it.sheet || 'Report'} ${it.cell || (it.row ? 'row ' + it.row : '')}${it.state === 'open' ? '' : ' · ' + ({kept: 'ignored', rejected: 'ignored', deferred: 'waiting on owner', confirmed: 'confirmed', selected: 'ticked for fix', claude: 'AI says fine', kept_earlier: 'ignored earlier', corrected: 'fixed later'}[it.state] || it.state)}: ${/^Missing cell\.?$/i.test(String(it.text || '').trim()) ? 'this cell is blank.' : it.text || it.reason}`), direct([it])))),
          h('div', {class: 'fd-cardacts'}, ...[['Previous items', -1], ['Next items', 1]].map(([label, step]) => h('button', {type: 'button', class: 'btn small', disabled: step < 0 ? page === 0 : (page + 1) * 25 >= g.items.length,
            onclick: () => { pages.set(g.key, page + step); draw(); const heading = content.children[index]?.querySelector('h3'); heading?.setAttribute('tabindex', '-1'); heading?.focus(); }}, label))));
      }));
    };
    dlg.append(h('button', {type: 'button', class: 'fd-dialog-x', 'aria-label': 'Close', title: 'Close', onclick: close}, icon('x', 18)),
      h('h2', {id: 'fd-review-title', tabindex: '-1'}, filterLabel || 'Needs review'),
      h('p', {}, `${filterLabel ? 'Only this selected status is listed below. ' : ''}Tick one, a few, or all. Then choose: AI fix, Fix manually (you edit the spreadsheet), or Ignore.`),
      h('p', {class: 'fd-hint'}, state.aiOn ? 'AI fix sends only the selected errors and their cells to Dex (AI by Anthropic).' : 'AI fix is off.'),
      filterLabel ? null : h('details', {class: 'fd-dialog-summary'}, h('summary', {}, 'AI summary of these errors (optional)'), fdSummaryPanel(report, binding, dlg, current)),
      !items.length ? h('section', {}, h('h3', {}, 'No loaded review items'), h('p', {}, 'No selectable findings does not establish readiness. Client, job, business date, source verification or coverage may still need attention.'), ...(report.holds || []).map(hold => h('p', {}, hold.text || hold.code)), h('button', {type: 'button', class: 'btn', onclick: () => { if (!guard()) return; close(); state.fdView = 'sources'; drawReport(); }}, 'Open source and report context')) : null,
      h('div', {class: 'fd-review-toolbar'}, status, h('div', {class: 'fd-cardacts'},
        h('button', {type: 'button', class: 'btn', onclick: () => choose(unresolved, true)}, `Select all (${unresolved.length})`),
        h('button', {type: 'button', class: 'btn', onclick: () => choose(items, false)}, 'Select none'),
        h('button', {type: 'button', class: 'btn', 'data-ai-cancel': 'true', onclick: close}, 'Close review')), actions),
      decision, types, content);
    document.body.append(dlg); draw(); dlg.showModal(); dlg.querySelector('h2').focus();
    if (Array.isArray(initialScope) && initialAction) { const keys = new Set(initialScope.map(it => it.key)); act(items.filter(it => keys.has(it.key)), initialAction); }
  }
  function fdDirectControls(scope, run = (items, action) => action === 'ai' ? fdAIFixNow(items) : action === 'manual' ? fdManualStart(items) : fdReviewDialog(items, action), listed = false) {
    const ai = state.aiOn && scope.some(it => it.fp && it.state === 'open');
    const ignore = scope.length > 0 && scope.length <= 1000 && scope.every(it => it.fp && ['open', 'claude'].includes(it.state)) && !(scope.length > 1 && scope.some(it => it.issue?.severity === 'high'));
    return h('div', {class: 'fd-direct-actions'},
      scope.length > 1 && !listed ? fdProblemList(scope) : null,
      h('div', {class: 'fd-cardacts'},
        h('button', {type: 'button', class: 'btn small', disabled: !ai, onclick: () => run(scope, 'ai')}, 'AI fix'),
        h('button', {type: 'button', class: 'btn small', onclick: () => run(scope, 'manual')}, 'Fix manually'),
        h('button', {type: 'button', class: 'btn small', disabled: !ignore, onclick: () => run(scope, 'ignore')}, 'Ignore')),
      !ai && !state.aiOn ? h('p', {class: 'fd-hint'}, 'AI fix is off. Fix manually or ignore.') : null,
      !ignore && scope.length > 1 && scope.some(it => it.issue?.severity === 'high') ? h('p', {class: 'fd-hint'}, 'These are serious errors, so each one is ignored on its own: tick one, then Ignore.') : null);
  }
  // The problems the three buttons act on, in plain words: where, and what is wrong.
  function fdProblemList(scope, max = 4) {
    const ticked = new Set(state.report ? fdPicked(scope).map(it => it.key) : []);
    state.fdShowAll ||= {};
    const showKey = JSON.stringify([state.report?.file_hash, state.report?.check_hash, scope.map(it => it.key)]);
    const all = !!state.fdShowAll[showKey], pageSize = 25;
    state.fdProblemPages ||= {};
    const page = Math.max(0, Math.min(state.fdProblemPages[showKey] || 0, Math.ceil(scope.length / pageSize) - 1));
    const shown = all ? scope.slice(page * pageSize, (page + 1) * pageSize) : scope.slice(0, max);
    const line = it => {
      const where = [it.sheet, it.cell ? 'cell ' + it.cell : it.row ? 'row ' + it.row : null].filter(Boolean).join(' ');
      const blank = /^Missing cell\.?$/i.test(String(it.text || '').trim());
      const value = blank || it.report == null || it.report === '' ? '' : ` (now “${String(it.report).slice(0, 40)}”)`;
      const text = [h('b', {}, where || 'Whole report'), `${value}: ${blank ? 'this cell is blank.' : String(it.text || it.reason || '').slice(0, 140)}`];
      return h('li', {}, state.report ? h('label', {class: 'fd-tickline'}, h('input', {type: 'checkbox', 'data-selection': 'true', 'data-tick': it.key, checked: ticked.has(it.key) || null,
        'aria-label': `Select ${where || 'report error'}`, onchange: e => fdTick([it], e.target.checked)}), h('span', {}, ...text)) : text);
    };
    return h('div', {class: 'fd-problems'},
      all ? h('p', {class: 'fd-problem-range', role: 'status', tabindex: '-1'}, `Errors ${page * pageSize + 1}–${Math.min((page + 1) * pageSize, scope.length)} of ${scope.length}. Selection covers all pages.`) : null,
      h('ul', {}, shown.map(line)),
      all && scope.length > pageSize ? h('div', {class: 'fd-cardacts'}, ...[['Previous errors', -1], ['Next errors', 1]].map(([label, step]) => h('button', {type: 'button', class: 'btn small', disabled: step < 0 ? page === 0 : (page + 1) * pageSize >= scope.length,
        onclick: e => { const parent = e.currentTarget.closest('.fd-problems'), groups = [...document.querySelectorAll('#side .fd-problems')], at = groups.indexOf(parent); state.fdProblemPages[showKey] = page + step; drawSide(); document.querySelectorAll('#side .fd-problems')[at]?.querySelector('.fd-problem-range')?.focus(); }}, label))) : null,
      scope.length > max ? h('button', {type: 'button', class: 'fd-linkbtn', onclick: () => { state.fdShowAll[showKey] = !all; drawSide(); }},
        all ? 'Show fewer' : `Show all ${scope.length}`) : null);
  }
  function fdOccurrenceList(g) {
    const eligible = fdRecommended(g.items), picked = eligible.filter(it => it.state === 'selected');
    const allItems = state.fdItems || fdItems();
    const groupLabel = `Select all ${eligible.length} eligible cell changes across all pages (${picked.length} selected)`;
    const groupToggle = h('input', {type: 'checkbox', checked: picked.length === eligible.length && !!eligible.length || null,
      disabled: !eligible.length, 'data-selection-key': 'group:' + g.key, 'aria-label': `${groupLabel}. ${g.title}`,
      onchange: e => fdSelect(eligible, e.target.checked)});
    groupToggle.indeterminate = picked.length > 0 && picked.length < eligible.length;
    const details = h('details', {class: 'fd-occurrences'}), pageSize = 25;
    const key = state.report.file_hash + ':' + state.report.check_hash + ':' + g.key;
    state.fdOccurrencePages ||= {}; state.fdExpandedGroups ||= {};
    if (state.fdExpandedGroups[key]) details.setAttribute('open', '');
    details.ontoggle = () => { state.fdExpandedGroups[key] = details.open; };
    details.append(h('summary', {}, `Show ${plural(g.items.length, 'occurrence')}`),
      h('label', {class: 'fd-group-select'}, groupToggle, ' ' + groupLabel));
    const content = h('div', {class: 'fd-occurrence-page'}), nav = h('div', {class: 'fd-cardacts'});
    const draw = focus => {
      const pages = Math.ceil(g.items.length / pageSize), page = Math.min(state.fdOccurrencePages[key] || 0, pages - 1);
      state.fdOccurrencePages[key] = page;
      const range = h('p', {class: 'fd-occurrence-range', tabindex: '-1', role: 'status'}, `Occurrences ${page * pageSize + 1}–${Math.min((page + 1) * pageSize, g.items.length)} of ${g.items.length}`);
      content.replaceChildren(range, h('ul', {}, g.items.slice(page * pageSize, (page + 1) * pageSize).map(it => {
        const selectable = eligible.some(x => x.fix.id === it.fix?.id);
        return h('li', {'data-finding-id': it.key},
          selectable ? h('label', {}, h('input', {type: 'checkbox', checked: it.state === 'selected' || null,
            'data-selection-key': 'finding:' + it.key, 'aria-label': fdOccurrenceLabel(it, allItems), onchange: e => fdSelect([it], e.target.checked)}), ' Include change') :
            h('span', {class: 'fd-hint'}, it.fix?.recommendation_eligible ? 'Your decision or edited value needs individual review.' : it.fix?.recommendation_reason || 'Needs individual review; not included in bulk fixes.'),
          h('button', {type: 'button', class: 'btn small', onclick: () => fdOpen(it)}, `${it.sheet || 'Report'}${it.cell ? ' · ' + it.cell : it.row ? ' · row ' + it.row : ''} · ${it.state}`),
          h('span', {}, it.text || it.reason), fdDirectControls([it]));
      })));
      nav.replaceChildren(...[['Previous', -1], ['Next', 1]].map(([label, direction]) => h('button', {type: 'button', class: 'btn small', disabled: direction < 0 ? page === 0 : page >= pages - 1,
        onclick: () => { state.fdOccurrencePages[key] = page + direction; draw(true); }}, label)));
      if (focus) range.focus();
    };
    details.append(content, nav); draw(false); return details;
  }

  // ---------- the page
  function fdGlobalStatus(st) { return fdShown(st).text; }
  // The header says what is going on in plain words: a report with no possible errors left is Completed, and a blocked
  // one says what is blocking it.
  function fdShown(st) {
    const r = state.report, left = r && state.fixes ? (state.fdItems?.length ? state.fdItems : fdItems()).filter(it => ['open', 'selected', 'deferred', 'confirmed'].includes(it.state)).length : null;
    // Zero possible errors is Completed, also when a SharePoint check note remains: those notes stay readable in the
    // hover text and under "SharePoint check notes", they are no longer shown as a block.
    if (left === 0 && !['Check unavailable', 'Checks out of date'].includes(st.text)) {
      const notes = (r?.holds || []).filter(x => x.hard && x.code !== 'awaiting_source_owner').map(x => x.text).filter(Boolean);
      return {tone: 'green', icon: 'ok', text: 'Completed', why: [st.tone === 'amber' ? st.text : '', st.tone === 'amber' ? st.why : '', ...notes].filter(Boolean).join('. ')};
    }
    // While Dex is working, or the list of possible errors is still loading, say that: never a passing "Blocked".
    if (state.fdAIRun && (state.fdAIRun.running || state.fdAIRun.finishing) && (!r || state.fdAIRun.hash === r.file_hash)) return {tone: 'amber', icon: st.icon, text: 'Dex is working…', why: ''};
    if (st.next === 'blockers' && r && !state.fixes) return {tone: 'amber', icon: st.icon, text: 'Loading possible errors…', why: ''};
    if (st.next === 'blockers') {
      const serious = (state.fdItems || []).filter(x => x.tier === 'blocker' && !['rejected', 'kept'].includes(x.state)).length;
      const holds = (r?.holds || []).filter(x => x.hard && x.code !== 'awaiting_source_owner').map(x => x.text).filter(Boolean);
      // Short and plain on the page; the full technical wording stays in the hover text.
      const short = [serious ? `${plural(serious, 'serious error')} must be fixed or ignored first` : '', holds.length ? 'the SharePoint check is not finished (open SharePoint check details on the right)' : ''].filter(Boolean).join('; ');
      const why = [serious ? `${plural(serious, 'serious error')} must be fixed or ignored first` : '', ...holds].filter(Boolean).join('. ');
      // Only a SharePoint check note and no serious error: say what is left to do, not "Blocked". The note stays in the hover text.
      if (!serious && left) return {tone: 'amber', icon: st.icon, text: `${plural(left, 'possible error')} to fix or ignore`, why};
      return {...st, text: serious ? `${plural(serious, 'serious error')} to fix or ignore` : short || st.text, why};
    }
    if (st.next === 'review') return {...st, text: 'Review needed' + (st.text && !/to review$/.test(st.text) ? ': ' + st.text : '')};
    return st;
  }
  function fdPanelStatus(r, st) {
    const findings = (state.fdItems || []).filter(item => item.tier === 'blocker' && !['rejected', 'kept'].includes(item.state)).length;
    const holds = (r.holds || []).filter(hold => hold.hard).length;
    return findings || holds ? [findings ? plural(findings, 'blocker') : null, holds ? plural(holds, 'approval hold') : null].filter(Boolean).join(' · ') : st.text;
  }
  function fdSourceVerified(r) {
    const rec = r.source_reconciliation || {}, sp = r.review?.sharepoint || {};
    return fdSourceComplete(r) && sp.source_verified === true && sp.status === 'match' && rec.verified === true &&
      rec.count_reconciliation?.status === 'verified' && !(r.holds || []).some(hold => String(hold.code || '').startsWith('source_'));
  }
  function fdSourceComplete(r) {
    const rec = r.source_reconciliation || {}, bound = rec.bound_to || {}, cov = rec.coverage || {};
    return !rec.stale && bound.file_hash === r.file_hash && !!bound.checker_version && bound.checker_version === r.checker_version &&
      cov.complete === true && !rec.rows_truncated && !rec.unidentified_rows && !rec.unconfirmed_rows && !(r.assignment || {}).needs_person &&
      !!rec.snapshot_id && !!rec.site_id && !!(rec.list_id || rec.list?.id) &&
      !cov.timezone_unconfirmed && !cov.truncated && !cov.denied && !cov.throttled && !(cov.failed_pages || []).length &&
      !Object.values(rec.column_mapping || {}).some(mapping => ['needs_confirmation', 'conflict'].includes(mapping.status)) &&
      !(r.holds || []).some(hold => String(hold.code || '').startsWith('source_') && /mapping|timezone|identity|incomplete|stale|assignment/.test(hold.code));
  }
  function fdSourceSummary(r) {
    const rec = r.source_reconciliation || {}, sp = r.review?.sharepoint || {}, counts = rec.count_reconciliation?.record_counts;
    const verified = fdSourceVerified(r);
    const status = verified ? 'Recorded source comparison verified' : rec.coverage?.complete === false || sp.status === 'incomplete' ? 'Source comparison incomplete' : 'Source not verified';
    if (!counts || counts.matched_unique_items == null || counts.report_rows == null) return `${status} · matched count unavailable`;
    return `${status} · ${counts.matched_unique_items} unique source items matched / ${counts.report_rows} report rows in the recorded capture`;
  }
  // Download the copy being worked on (it already has Dex's and your saved changes), or email it to the client.
  function fdSheetBar(r) {
    const name = r.filename, href = '/api/spreadsheets/download?id=' + r.file_hash;
    const download = () => { const a = h('a', {href, download: name}); document.body.append(a); a.click(); a.remove(); };
    const email = () => {
      const rv = r.review || {}, client = (r.assignment && SETTLED.includes(r.assignment.status)) ? r.assignment.client : '';
      const n = v => v == null ? '—' : Number(v).toLocaleString('en-US');
      const subject = `${client ? client + ' · ' : ''}${rv.job || name} end-of-shift report${rv.day ? ' · ' + dayLabel(rv.day) : ''}`;
      const body = [`Hello,`, '', `Attached is the end-of-shift report for ${dayLabel(rv.day)}.`, '', `Parts inspected: ${n(rv.inspected)}`,
        rv.certified != null ? `Accepted: ${n(rv.certified)}` : null, `Rejected: ${n(rv.rejected)}`, '', 'Thank you,'].filter(x => x !== null).join('\n');
      download();
      location.href = `mailto:?subject=${encodeURIComponent(subject)}&body=${encodeURIComponent(body)}`;
      toast('Your email opened with the report details. Attach the file that just downloaded, add the client, and send.');
    };
    const pending = readyChanges().total;
    return h('div', {class: 'fd-sheetbar'},
      h('span', {class: 'fd-sheetbar-what'}, `Saved working copy: version ${revisionOf(r)}${r.fixed_from ? ` · ${plural((r.fixed_from.changes || []).length, 'change')} saved` : ' · original'}${pending ? ` · ${pending} typed, not saved yet` : ''}`),
      (() => {   // the file exactly as it was first uploaded or built, opened read-only beside the fixed copy
        const history = r.version_view?.history || [], original = r.fixed_from ? (r.root_hash || history[history.length - 1]?.file_hash || r.fixed_from.file_hash) : null;
        return original && original !== r.file_hash ? h('a', {class: 'btn', id: 'fd-view-original', href: '#r=' + original + '&history=1', title: 'Opens the first version, unchanged, to look at. Nothing is edited.'}, 'View original') : null;
      })(),
      sharePointUrl(r) ? h('a', {class: 'btn', id: 'fd-open-sharepoint', href: sharePointUrl(r), target: '_blank', rel: 'noopener', title: 'Opens the SharePoint list for this report in a new tab.'}, 'Open SharePoint data') : null,
      h('button', {type: 'button', class: 'btn', onclick: download}, 'Download'),
      h('button', {type: 'button', class: 'btn primary', onclick: email}, 'Email to client'));
  }
  // The SharePoint list a report belongs to: the one it was compared with, or the list of its confirmed client and job.
  function sharePointUrl(r) {
    const a = r?.assignment || {}, cands = a.candidates || [];
    const pick = cands.find(c => c.url && a.list_id && c.list_id === a.list_id) || (cands.filter(c => c.url).length === 1 ? cands.find(c => c.url) : null);
    const url = r?.review?.sharepoint?.web_url || a.url || pick?.url || '';
    return /^https:\/\/[a-z0-9-]+\.sharepoint\.com\//i.test(url) ? url : null;
  }
  function drawReport() {
    if (versionReadOnly()) { drawReadOnlyReport(state.report); return; }
    const r = state.report, rv = r.review;
    document.body.classList.add('reportmode');
    state.sideTab ||= 'fixes'; state.fdView ||= 'review';
    state.fdItems = state.fixes ? fdItems() : [];
    const a = r.assignment || {}, sp = rv.sharepoint || {}, st = fdStatus(r);
    const close = e => e.target.closest('details')?.removeAttribute('open');
    const more = h('details', {class: 'more-menu'}, h('summary', {class: 'btn'}, 'More'),
      h('div', {class: 'menu'},
        !r.approval && (r.review_request || {}).status !== 'waiting' ? h('button', {type: 'button', onclick: e => { close(e); reviewForm('request'); }}, 'Request internal review') : null,
        h('button', {type: 'button', onclick: e => { close(e); copyClientSummary(); }}, 'Copy client summary for an email'),
        h('button', {type: 'button', onclick: e => { close(e); makeChangeSummary(); }}, 'Change summary (PDF)'),
        r.filename.toLowerCase().endsWith('.xlsx') ? h('button', {type: 'button', onclick: e => { close(e); downloadWithPhotos(r); }}, 'Download with SharePoint photos') : null,
        h('a', {href: '/api/spreadsheets/download?id=' + r.file_hash, download: r.filename, onclick: close}, `Download this version (v${revisionOf(r)})`),
        r.approval && rv.approval_current ? h('button', {type: 'button', onclick: e => { close(e); openSend(); }}, 'Approval details…') : null));
    const crumb = [a.client || h('em', {}, 'Client not settled'), rv.job || r.filename, rv.day ? shortDay(rv.day) : 'No business date'];
    const recordsChecked = fdSourceSummary(r);
    const head = h('header', {class: 'fd-head'},
      h('div', {class: 'fd-title'},
        h('p', {class: 'fd-eyebrow'}, 'Review workspace'),
        h('h1', {}, ...crumb.flatMap((x, n) => n ? [h('span', {class: 'sep', 'aria-hidden': 'true'}, '/'), x] : [x])),
        h('p', {class: 'fd-meta'},
          h('span', {}, `v${revisionOf(r)}${r.fixed_from ? ` · made from v${r.fixed_from.revision || 1}` : ' · original upload'}`),
          r.fixed_from ? h('span', {class: 'fd-saved-summary', role: 'status'}, `Corrected version v${revisionOf(r)}: ${plural((r.fixed_from.changes || []).length, 'recorded cell change')}. Original preserved. ${r.sent?.some(x => x.sent) ? 'Delivery recorded in History.' : 'Not sent.'}`) : null,
          h('span', {}, icon('db', 14), ' ', recordsChecked),
          sp.captured_at ? h('span', {}, `Source captured ${when2(sp.captured_at)}`) : null,
          (sp.fields_checked || []).length ? h('span', {title: (sp.fields_not_checked || []).length ? 'Not checked: ' + sp.fields_not_checked.join(', ') : null},
            `Fields checked: ${sp.fields_checked.join(', ')}${(sp.fields_not_checked || []).length ? ` · not checked: ${sp.fields_not_checked.join(', ')}` : ''}`) : null)),
      h('div', {class: 'fd-actions'},
        (shown => h('div', {class: `fd-status fd-global-status ${shown.tone}`, role: 'status', title: shown.why || null}, icon(shown.icon, 16), h('span', {}, shown.text)))(fdShown(st)),
        more, h('a', {class: 'btn', href: '/api/spreadsheets/download?id=' + r.file_hash, download: r.filename}, 'Download this version')));
    const metrics = fdMetrics(r);
    const rail = fdReportRail();
    let main;
    if (state.fdView === 'history') main = fdHistory(r);
    else if (state.fdView === 'sources') main = fdSources(r);
    else {
      main = h('div', {class: 'fd-work' + (state.fdAsideHidden ? ' aside-hidden' : ''), id: 'fd-work'},
        h('section', {class: 'fd-sheet', 'aria-label': 'Spreadsheet'},
          fdSheetBar(r),
          h('div', {class: 'sheetpane', id: 'sheet-block'}, h('p', {class: 'note fd-loading'}, h('span', {class: 'spin'}), ' Loading the spreadsheet…')),
          h('div', {id: 'fd-gallery-host'}),
          h('div', {id: 'fd-evidence'})),
        h('div', {class: 'fd-split', role: 'separator', 'aria-orientation': 'vertical', 'aria-label': 'Resize the corrections panel', tabindex: '0',
          onpointerdown: fdDragStart, onkeydown: e => { if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') { fdAsideWidth(fdAsideWidth() + (e.key === 'ArrowLeft' ? 24 : -24)); e.preventDefault(); } }}),
        h('aside', {class: 'sidepane fd-aside', id: 'side', 'aria-label': 'Errors'}));
    }
    const sel = readyChanges().total;
    const foot = h('div', {class: 'fd-foot', role: 'region', 'aria-label': 'Selected changes'},
      h('span', {class: 'fd-foot-state'}, icon('lock', 16), h('b', {id: 'fd-selcount'}, sel ? plural(sel, 'change') + ' selected' : 'No changes selected'),
        h('span', {class: 'fd-foot-note'}, `Original v1 is kept unchanged${r.fixed_from ? '; this is v' + revisionOf(r) : ''}`)),
      h('button', {type: 'button', class: 'btn fd-mobile-panel', onclick: () => document.body.classList.toggle('fd-panel-open'), 'aria-controls': 'side'}, 'Errors', h('i', {id: 'fd-mobile-count'}, '')),
      h('button', {type: 'button', class: 'btn ' + (sel ? 'primary' : ''), id: 'fd-preview-btn', disabled: !sel || state.busy, onclick: correctionAction(fdPreview)}, icon('eye', 16), ' Preview selected changes'));
    $('report').replaceChildren(h('div', {class: 'fd'}, rail, h('div', {class: 'fd-main'}, head, versionLinks(r), fdModeSelector(r), metrics, main), foot));
    state.aiProposalOperation?.lock?.();
    const w = store.get('fd-aside-w', null); if (w) fdAsideWidth(w);
    if (state.fdView !== 'review') return;
    state.grid = state.grid?.hash === r.file_hash ? state.grid : null;
    drawSide(); updateDownloads();
    loadGrid(state.gridSheet && state.grid ? state.gridSheet : undefined);
  }
  function fdMetrics(r) {
    const rv = r.review, quantities = rv.quantities || {};
    const quantity = key => quantities[key] || {value: rv[key], partial: true, complete: false, unit: key === 'rejected' ? rv.rejected_unit : null};
    const inspected = quantity('inspected'), certified = quantity('certified'), rejected = quantity('rejected');
    const perRow = rejected.unit === 'rows', base = perRow ? rv.scans : inspected.value;
    const compatible = !perRow && rejected.unit && inspected.unit && rejected.unit === inspected.unit;
    const rate = Number.isFinite(base) && base > 0 && Number.isFinite(rejected.value) && rejected.value >= 0 && rejected.value <= base && rejected.complete === true && !rejected.partial && (perRow || inspected.complete === true && !inspected.partial) && compatible ? rejected.value / base * 100 : null;
    const amount = q => q.value == null ? 'Unknown' : fmt(q.value);
    const unitLabel = q => q.unit === 'parts' ? 'parts' : q.unit === 'rows' ? 'rows' : 'unit unknown';
    const qualifier = q => q.value == null ? 'blank is not zero' : q.partial || q.complete !== true ? 'Partial or completeness unknown' : 'Recorded total';
    const photos = r.photos?.image_count ?? null, shifts = (r.shift_summary || r.shifts || []).length || null;
    const m = (value, label, extra) => h('div', {class: 'fd-metric'}, h('strong', {}, value), h('span', {}, label), extra ? h('small', {}, extra) : null);
    return h('div', {class: 'fd-metrics', 'aria-label': 'Key totals'},
      m(fmt(rv.scans), 'records', 'audit rows, counted once'),
      m(amount(inspected), `inspected (${unitLabel(inspected)})`, qualifier(inspected)),
      m(amount(certified), `OK (${unitLabel(certified)})`, qualifier(certified)),
      m(amount(rejected), `rejected (${unitLabel(rejected)})`, qualifier(rejected)),
      m(rate == null ? 'Unavailable' : rate.toFixed(1) + '%', perRow ? 'fail rate' : 'reject rate', rate == null ? 'quantity completeness not established' : null),
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

  // A server-wide, exact-version result. Browser selections never clear errors.
  function fdCheckedState(r, now = Date.now()) {
    const p = r?.checked_copy;
    if (!p) return {ready: false, message: 'Verification status is unavailable. Reload the report.'};
    if (p.file_hash !== r.file_hash || p.check_hash !== r.check_hash) return {ready: false, message: 'The saved check changed. Reload this version.'};
    if (!p.ready) return {ready: false, message: (p.reasons || []).join(' ') || 'Verification is not complete.'};
    if (!Number.isFinite(Date.parse(p.expires_at)) || Date.parse(p.expires_at) <= now) return {ready: false, message: 'Source evidence expired. Recheck SharePoint explicitly before downloading a checked copy.'};
    if (state.report !== r || state.busy || state.rechecking || state.reviewing || state.verifying || state.decisionPending || state.gridDraft || readyChanges().total) return {ready: false, message: 'Finish saving changes and recheck the current version.'};
    return {ready: true, message: 'This saved version passed the available checks and current exact-source comparison. Original versions are unchanged. This is not an approval or a guarantee of every possible error.'};
  }
  async function fdDownloadChecked(r) {
    const status = fdCheckedState(r);
    if (!status.ready) { toast(status.message); drawSide(); return; }
    const p = r.checked_copy, hash = r.file_hash, check = r.check_hash;
    try {
      // Construct the exact route from the bound identity, never a sibling URL.
      const response = await fetch('/api/spreadsheets/download?id=' + encodeURIComponent(hash) + '&checked=1&check=' + encodeURIComponent(check));
      if (!response.ok) {
        let message = 'The checked copy is no longer available. Reload and review this version.';
        try { const error = await response.json(); message = error.error || error.message || message; } catch (_) {}
        throw new Error(message);
      }
      const expectedType = /\.xlsx$/i.test(r.filename) ? 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' : 'text/csv';
      if (!(response.headers.get('Content-Type') || '').startsWith(expectedType)) throw new Error('The server did not return a workbook. No file was downloaded.');
      let receipt;
      try { receipt = JSON.parse(response.headers.get('X-PPG-Checked-Copy') || 'null'); } catch (_) {}
      const receiptCurrent = () => receipt?.protocol === 'ppg-checked-copy-v1' && receipt.ready === true &&
        receipt.file_hash === hash && receipt.check_hash === check && typeof receipt.expires_at === 'string' &&
        Number.isFinite(Date.parse(receipt.expires_at)) && Date.parse(receipt.expires_at) > Date.now();
      if (!receiptCurrent()) throw new Error('The server did not confirm a current checked copy for this exact version. No file was downloaded. Reload before trying again.');
      const blob = await response.blob();
      if (!receiptCurrent()) throw new Error('The checked-copy receipt expired while downloading. No file was downloaded.');
      if (!fdCheckedState(r).ready || state.report?.file_hash !== hash || state.report?.check_hash !== check) throw new Error('This version changed or its evidence expired while downloading. Reload it before trying again.');
      const disposition = response.headers.get('Content-Disposition') || '';
      const filename = disposition.match(/filename="([^"]+)"/i)?.[1] || r.filename;
      const url = URL.createObjectURL(blob), a = h('a', {href: url, download: filename});
      document.body.append(a); a.click(); a.remove(); globalThis.setTimeout?.(() => URL.revokeObjectURL(url), 1000);
    } catch (error) {
      // A refused response is never saved with a workbook extension.
      p.ready = false; p.reasons = [error.message];
      toast(error.message); if (state.report === r) { drawSide(); fdRefreshFoot(); }
    }
  }
  function fdCheckedCopy(r) {
    const status = fdCheckedState(r), p = r.checked_copy;
    // Nothing left to fix or decide: say Completed. What the SharePoint check could not confirm stays one click away.
    const ignored = p?.set_aside_unresolved || 0;
    if (!status.ready && state.fixes && r.review?.page?.complete !== false && (!p || p.coverage_complete !== false && p.file_hash === r.file_hash && p.check_hash === r.check_hash) &&
        !(state.fdItems?.length ? state.fdItems : typeof fdItems === 'function' ? fdItems() : []).some(it => ['open', 'selected', 'deferred', 'confirmed'].includes(it.state))) {
      clearTimeout(state.checkedCopyExpiryTimer);
      return h('section', {id: 'fd-checked-copy', class: 'fd-checked-copy checked fd-completed', role: 'status'},
        h('b', {}, 'Completed'),
        h('p', {}, `${r.filename} · version ${revisionOf(r)}. No possible errors left${ignored ? ` (${ignored} ignored, left as written)` : ''}.`),
        h('a', {class: 'btn primary', id: 'fd-download-fixed', href: '/api/spreadsheets/download?id=' + r.file_hash, download: r.filename}, r.fixed_from ? 'Download fixed report' : 'Download report'),
        typeof sharePointUrl === 'function' && sharePointUrl(r) ? h('a', {class: 'btn', href: sharePointUrl(r), target: '_blank', rel: 'noopener'}, 'Open SharePoint data') : null,
        h('details', {class: 'fd-completed-notes'}, h('summary', {}, 'SharePoint check notes'), h('p', {}, status.message)));
    }
    const section = h('section', {id: 'fd-checked-copy', class: 'fd-checked-copy ' + (status.ready ? 'checked' : 'pending'), role: 'status'},
      h('b', {}, status.ready ? '0 remaining detected errors' : 'Verification not complete'),
      h('p', {}, `${r.filename} · version ${revisionOf(r)}. ${status.message}`),
      !status.ready && p?.coverage_complete && Number.isInteger(p.remaining_detected_errors) ? h('p', {}, `${p.remaining_detected_errors} unresolved detected findings in the full saved check · ${p.set_aside_unresolved || 0} set aside · ${p.informational || 0} informational. Set-aside findings are not corrections.`) : null,
      status.ready ? h('button', {type: 'button', class: 'btn primary', onclick: () => fdDownloadChecked(r)}, 'Download checked copy') : null);
    clearTimeout(state.checkedCopyExpiryTimer);
    if (status.ready) state.checkedCopyExpiryTimer = globalThis.setTimeout?.(() => {
      if (state.report !== r) return;
      drawSide(); fdRefreshFoot();
    }, Math.max(1, Date.parse(p.expires_at) - Date.now() + 1));
    return section;
  }

  // ---------- right-hand panel
  function fdCorrections(body) {
    const r = state.report, st = fdStatus(r), c = fdCounts();
    if (!state.fixes) { body.append(h('div', {class: 'fd-panel-loading', role: 'status'}, h('span', {class: 'spin'}), ' Reading the corrections…')); return; }
    const items = state.fdItems = fdItems(), groups = fdGroups(items);
    const tier = state.fdTier && groups.some(g => g.tier === state.fdTier) ? state.fdTier : (groups.find(g => g.open)?.tier || groups[0]?.tier || 'blocker');
    state.fdTier = tier;
    body.append(h('div', {class: 'fd-panelhead'}, h('p', {class: 'fd-panel-intro'}, 'Tick errors, then choose AI fix, Fix manually or Ignore.'),
      h('button', {type: 'button', class: 'fd-iconbtn fd-collapse', 'aria-label': 'Hide the errors panel', title: 'Hide panel', onclick: () => { state.fdAsideHidden = true; $('fd-work')?.classList.add('aside-hidden'); }}, icon('panel', 18))));
    const approvalHolds = r.holds || []; // Advisory holds also exclude bulk selection and must remain inspectable.
    if (approvalHolds.length && st.next !== 'recheck') { const action = fdHoldAction(r, st); if (action) body.append(action); }
    else if (st.next !== 'recheck') { const nextStep = fdNextStep(r, st); if (nextStep) body.append(nextStep); }
    const cov = r.review.coverage || {}, dc = r.review.decisions || {};
    // Every error in the report is reviewed: a saved check that listed fewer than it found is checked again once, by itself.
    if ((cov.truncated || cov.not_listed || r.internal_check_status === 'outdated_checks_require_recheck') && !r.approval && !(r.sent || []).some(x => x.sent)) {
      state.fdAutoRecheck ||= {};
      if (!state.fdAutoRecheck[r.file_hash] && !state.fdAutoRecheckWaiting) { state.fdAutoRecheckWaiting = true; let tries = 0; const attempt = () => {
        if (state.report !== r || state.fdAutoRecheck[r.file_hash]) { state.fdAutoRecheckWaiting = false; return; }
        if (state.busy || state.rechecking || state.decisionPending || state.aiProposalOperation || state.gridDraft) { if (++tries < 120) globalThis.setTimeout?.(attempt, 500); else state.fdAutoRecheckWaiting = false; return; }
        state.fdAutoRecheck[r.file_hash] = true; state.fdAutoRecheckWaiting = false; fdRecheck();
      }; globalThis.setTimeout?.(attempt, 0); }
      body.append(h('p', {class: 'fd-hint', role: 'status'}, state.fdAutoRecheck[r.file_hash] && !state.rechecking ? 'Some errors were not listed by the last check. Recheck this report to list them all.' : 'Checking the whole report again so every error is listed…'));
    }
    body.append(h('p', {class: 'fd-countline', id: 'fd-countline'}, `${plural(items.filter(it => ['open', 'selected', 'deferred', 'confirmed'].includes(it.state)).length, 'error')} to fix · ${plural(c.applied, 'saved cell change')} in this version · ${c.kept} ignored`));
    body.append(fdCheckedCopy(r));
    const statuses = [['To fix', ['open', 'selected']], ['Waiting on the SharePoint owner', ['deferred']],
      ['Confirmed, to fix', ['confirmed']], ['Ignored', ['kept', 'rejected']],
      ['Ignored earlier', ['kept_earlier']], ['AI says fine', ['claude']], ['Fixed in a later version', ['corrected']]];
    const recon = h('details', {class: 'fd-recon'}, h('summary', {}, 'Possible errors by status'),
      h('div', {class: 'fd-cardacts'}, statuses.map(([label, states]) => {
        const scope = items.filter(it => states.includes(it.state));
        return scope.length ? h('button', {type: 'button', class: 'btn', onclick: () => fdReviewDialog(scope, null, label)}, `${label} · ${scope.length}`) : null;
      })));
    if (dc.stale) body.append(h('p', {class: 'fd-coverage', role: 'note'}, icon('warn', 14), ` ${plural(dc.stale, 'earlier decision')} no longer match${dc.stale === 1 ? 'es' : ''} what the finding says now, so ${dc.stale === 1 ? 'it is' : 'they are'} not applied. Decide again.`));
    if (state.decisionPending) body.append(h('p', {class: 'fd-saving', role: 'status'}, h('span', {class: 'spin'}), ' Saving your decision…'));
    if (state.decisionFailed && !state.fdAfterAIPending) body.append(h('div', {class: 'fd-failed', role: 'alert'}, icon('warn', 14), ' ', state.decisionFailed.message,
      state.decisionFailed.retry ? h('button', {type: 'button', class: 'btn small', onclick: () => fdSendDecision(state.decisionFailed.retry)}, 'Try again') : null,
      h('button', {type: 'button', class: 'btn small subtle', onclick: () => { state.decisionFailed = null; drawSide(); }}, 'Dismiss')));
    // a report that is not ready says why, with one clear next step
    if (r.review.page && !r.review.page.complete) body.append(h('p', {class: 'fd-coverage', role: 'alert'}, `Only ${(r.review.issues || []).length} of ${r.review.page.issues_total} active and ${(r.review.dismissed || []).length} of ${r.review.page.dismissed_total} set-aside findings loaded. Counts below cover loaded occurrences only. Reopen the report to load the rest; do not treat missing items as resolved.`));
    const auto = fdAutoSummary(r);
    const historyGroups = auto ? fdHistoryGroups(r.auto_fixed) : [];
    const historyApplied = historyGroups.filter(g => ['applied', 'removed', 'moved'].includes(g.status)).reduce((n, g) => n + g.entries.length, 0);
    const historyRecommendations = historyGroups.filter(g => g.status === 'recommendation').reduce((n, g) => n + g.entries.length, 0);
    const appendHistory = () => { if (auto) body.append(h('details', {class: 'fd-history-overview'}, h('summary', {}, `History · ${plural(historyApplied, 'applied change')} · ${plural(historyRecommendations, 'AI recommendation')}`), auto)); };
    const leftNow = items.filter(it => ['open', 'selected', 'deferred', 'confirmed'].includes(it.state)).length;
    if (state.fdAIRun?.hash === r.file_hash && !fdAutoMode()) body.append(fdAIRunBanner(state.fdAIRun, items.filter(it => ['open', 'selected', 'deferred', 'confirmed'].includes(it.state)).length));
    if (!items.length) { body.append(fdClean(r, st), recon); appendHistory(); return; }
    const cur = items.find(x => x.key === state.fdCur) || null;
    const tierGroups = groups.filter(g => g.unresolved || (cur && cur.group === g.key));
    const fixable = tierGroups.flatMap(g => g.items).filter(it => ['open', 'selected', 'deferred', 'confirmed'].includes(it.state));
    const run = state.fdAIRun;
    if (fixable.length) {
      const ticked = fdPicked(fixable), allBox = h('input', {type: 'checkbox', 'data-selection': 'true', 'data-tick': 'all', checked: ticked.length === fixable.length || null, onchange: e => fdTick(fixable, e.target.checked)});
      allBox.indeterminate = ticked.length > 0 && ticked.length < fixable.length;
      const busyNow = !!(state.busy || state.rechecking || state.decisionPending || run?.running);
      body.append(h('div', {class: 'fd-tickbar'},
        h('label', {class: 'fd-tickall'}, allBox, ` Select all ${plural(fixable.length, 'error')}`),
        h('p', {class: 'fd-tickcount', role: 'status'}, ticked.length ? `${ticked.length} selected` : 'Tick the errors to work on only those, or leave them all unticked.'),
        null));
    }
    if (!tierGroups.length) body.append(h('p', {class: 'fd-hint'}, 'Nothing left to fix. Errors you ignored are listed under Possible errors by status.'));
    const manual = fdManualList(items);
    if (manual) body.append(fdManualCard(manual));
    else if (cur) body.append(h('p', {id: 'fd-correction-context', class: 'fd-grid-context', role: 'status'}, `Current correction: ${cur.sheet || 'Report'} · ${cur.cell || (cur.row ? 'row ' + cur.row : 'report-level')}. Filtering the sheet does not change this correction.`), fdCard(cur, groups));
    body.append(h('div', {class: 'fd-groups'}, tierGroups.map(g => {
      const selectable = g.items.filter(x => x.selectable && x.state === 'open' && (x.evidence === 'source' || x.evidence === 'mechanical'));
      const gTicked = fdPicked(g.items).length, gBox = h('input', {type: 'checkbox', 'data-selection': 'true', 'data-tick': 'group:' + g.key, 'aria-label': 'Select every error in: ' + g.title,
        checked: gTicked === g.items.length || null, onchange: e => fdTick(g.items, e.target.checked)});
      gBox.indeterminate = gTicked > 0 && gTicked < g.items.length;
      return h('section', {class: 'fd-group' + (cur && cur.group === g.key ? ' cur' : '')},
        h('label', {class: 'fd-gtick'}, gBox),
        h('button', {type: 'button', class: 'fd-grouphead', 'aria-expanded': String(!!cur && cur.group === g.key), onclick: () => fdReviewDialog(g.items, null, g.title)},
          h('span', {class: 'fd-gtitle'}, g.unresolved ? '' : icon('ok', 14), g.unresolved ? '' : ' ', g.title),
          h('span', {class: 'fd-gmeta'}, `${plural(g.items.length, 'error')}${g.unresolved && g.unresolved < g.items.length ? ` · ${g.unresolved} to fix` : ''}${g.status && g.status !== 'Needs review' ? ` · ${g.status}` : ''}`)),
        (open => open.length > 1 ? h('div', {class: 'fd-direct-actions'}, fdProblemList(open)) : null)(g.items.filter(x => ['open', 'selected', 'deferred', 'confirmed'].includes(x.state))),
        false && r.review.page?.complete !== false && g.tier !== 'format' && !selectable.length && g.open > 1 && g.items.every(x => !x.fix) ? h('button', {type: 'button', class: 'btn small fd-selectall', disabled: state.decisionPending || state.rechecking || state.busy || null, 'data-recheck-disabled': state.rechecking && !state.decisionPending && !state.busy ? true : null,
          onclick: () => { state.fdBatch = {items: g.items.slice(), group: g.key, off: new Set(), action: 'reject', which: 'open'}; drawSide(); }}, `Keep ${g.open} as recorded…`) : null,
        r.review.page?.complete !== false && g.items[0].state === 'claude' ? h('div', {class: 'fd-cardacts'},
          h('button', {type: 'button', class: 'btn small', disabled: state.decisionPending || state.rechecking || state.busy || null, 'data-recheck-disabled': state.rechecking && !state.decisionPending && !state.busy ? true : null, onclick: () => { state.fdBatch = {items: g.items.slice(), group: g.key, off: new Set(), action: 'reject', which: 'claude',
            reason: 'Agreed with Claude: ' + (g.items[0].issue?.claude?.reason || g.items[0].issue?.verdict_reason || '')}; drawSide(); }}, `Agree with Dex for ${g.items.length}…`),
          h('button', {type: 'button', class: 'btn small subtle', disabled: state.decisionPending || state.rechecking || state.busy || null, 'data-recheck-disabled': state.rechecking && !state.decisionPending && !state.busy ? true : null, onclick: () => { state.fdBatch = {items: g.items.slice(), group: g.key, off: new Set(), action: 'reopen', which: 'claude'}; drawSide(); }}, `Put ${g.items.length} back for review…`)) : null,
        state.fdBatch && state.fdBatch.group === g.key ? fdBatchForm(g) : null);
    })));
    body.append(recon);
    body.append(h('a', {class: 'btn fd-bottom-download', href: '/api/spreadsheets/download?id=' + r.file_hash, download: r.filename}, r.fixed_from ? 'Download fixed report' : 'Download report'));
    appendHistory();
  }
  // One selection, shared by the errors panel and the review window.
  function fdPicked(items) {
    const key = JSON.stringify(reviewBinding(state.report)), saved = state.fdReviewSelection;
    const keys = new Set(saved?.binding === key ? saved.keys : []);
    return items.filter(it => keys.has(it.key));
  }
  function fdTick(list, on) {
    if (!state.report) return;
    const key = JSON.stringify(reviewBinding(state.report)), saved = state.fdReviewSelection;
    const keys = new Set(saved?.binding === key ? saved.keys : []);
    list.forEach(it => on ? keys.add(it.key) : keys.delete(it.key));
    state.fdReviewSelection = {binding: key, keys: [...keys]};
    const focus = document.activeElement?.getAttribute?.('data-tick');
    drawSide();
    if (focus) document.querySelector(`#side [data-tick="${CSS.escape(focus)}"]`)?.focus();
  }
  // AI fix: Dex fixes every selected error it can and saves a new version (the earlier one is kept).
  // The report reloads on the new version, so fixed errors drop off and only what is left stays listed.
  const fdReadyFix = it => it.fix && it.selectable && ['open', 'selected'].includes(it.state) && (it.evidence === 'source' || (!it.fp && ['mechanical', 'ai'].includes(it.evidence))) ? it.fix : null;
  const fdSourceGap = it => !fdReadyFix(it) && (String(it.issue?.source || '').startsWith('SharePoint') || it.issue?.source_value != null || /^(source_|sharepoint_)/i.test(it.issue?.code || '') || /timezone.*unconfirmed|date.*unconfirmed/i.test(it.issue?.code || ''));
  const fdCanAI = it => !!fdReadyFix(it) || !!(it.fp && it.state === 'open' && state.aiOn && !fdSourceGap(it));
  function fdUniqueAIProposals(proposals) {
    const cells = new Map();
    const fields = ['before', 'after', 'kind', 'source', 'snapshot_id', 'item', 'field', 'change_set', 'change_set_size', 'selected_finding_id', 'selected_finding_fp'];
    for (const p of proposals) {
      const key = JSON.stringify([p.sheet, String(p.cell).toUpperCase()]), prior = cells.get(key);
      if (prior && fields.some(field => JSON.stringify(prior[field] ?? null) !== JSON.stringify(p[field] ?? null))) {
        throw new Error(`Conflicting suggestions for ${p.sheet}!${p.cell}. Review this cell manually. No save was started.`);
      }
      if (!prior) cells.set(key, p);
    }
    return [...cells.values()];
  }
  // A recorded cell change is not proof that every finding on that cell was fixed.
  // Only a complete, exact saved check can clear a selected finding in the result summary.
  // The open list itself always comes from fdItems() on the newly loaded report.
  function fdSavedFindingResolution(scope, accepted, binding, savedItem, fresh, working) {
    const outcome = savedItem.save_outcome, reused = outcome?.type === 'existing_version';
    const recorded = reused ? (outcome.requested_from === binding.file_hash && outcome.expected_check_hash === binding.expected_check_hash ? outcome.changes || [] : [])
      : savedItem.fixed_from?.file_hash === binding.file_hash ? savedItem.fixed_from.changes || [] : [];
    const saved = Array.isArray(recorded) ? recorded : [];
    const cell = x => String(x?.cell || '').toUpperCase();
    const exact = p => saved.some(c => c && c.sheet === p.sheet && cell(c) === cell(p) && c.before === p.before && c.after === p.after);
    const confirmed = accepted.filter(exact), rv = fresh?.review, coverage = rv?.coverage, proof = fresh?.checked_copy;
    const hasInventory = Array.isArray(rv?.issues) && Array.isArray(rv?.dismissed);
    const inventory = hasInventory ? [...rv.issues, ...rv.dismissed].filter(x => x && typeof x === 'object') : [];
    const identity = x => typeof x === 'string' && x.trim().length > 0;
    const complete = !!(identity(fresh?.file_hash) && fresh.file_hash === savedItem.file_hash && identity(fresh.check_hash) && fresh.check_hash === savedItem.check_hash &&
      identity(fresh.checker_version) && fresh.checker_version === savedItem.checker_version && ['findings', 'no_findings_in_checks_performed'].includes(fresh.internal_check_status) &&
      proof?.file_hash === fresh.file_hash && proof.check_hash === fresh.check_hash && proof.coverage_complete === true &&
      hasInventory && rv?.page?.complete === true && rv.page.issues_total === rv.issues.length && rv.page.dismissed_total === rv.dismissed.length &&
      coverage && ['findings_total', 'findings_listed', 'all_findings'].every(k => Number.isSafeInteger(coverage[k]) && coverage[k] >= 0) && !coverage.truncated && coverage.not_listed === 0 &&
      coverage.findings_total === coverage.findings_listed && coverage.all_findings === inventory.length &&
      working?.status === 'current' && working.requested_hash === fresh.file_hash && working.working_hash === fresh.file_hash && !working.read_only_reason);
    const resolved = [], appliedProposals = [], informational = [], remaining = [];
    for (const it of scope.filter(it => ['open', 'selected', 'deferred', 'confirmed'].includes(it.state))) {
      const issue = it.issue;
      // Keys/fingerprints may change when values or a record identifier change.
      // A same-rule finding at the same place is still open, even if it was set aside.
      const current = inventory.find(x => x.key === it.key || issue && x.code === issue.code && x.source === issue.source &&
        (issue.item != null && x.item === issue.item && x.field === issue.field || x.sheet === it.sheet &&
          (it.cell ? cell(x) === cell(it) : it.row ? Number(x.row) === Number(it.row) : true)));
      const changed = it.cell && confirmed.some(p => p.sheet === it.sheet && cell(p) === cell(it));
      const checked = issue?.source === 'Checks' || issue?.source === 'SharePoint' && rv?.sharepoint?.source_verified === true && Date.parse(proof?.expires_at) > Date.now() &&
        !(fresh?.holds || []).some(h => h.hard && String(h.code || '').startsWith('source_')) ||
        issue?.source === 'AI review' && rv?.ai?.status === 'done';
      const sameBinding = it.binding && JSON.stringify(it.binding) === JSON.stringify(binding);
      const support = current?.source_support;
      const age = Date.now() - Date.parse(support?.captured_at);
      if (complete && sameBinding && current?.severity === 'info' && current.source === 'Checks' &&
          ['part_format_differs', 'identifier_format_differs'].includes(current.code) && !current.decision && !current.dismissed_by &&
          support?.kind === 'source_confirmed_pattern' && support.snapshot_id === fresh.source_reconciliation?.snapshot_id &&
          Number.isFinite(age) && age >= 0 && age <= 600000) {
        informational.push(it.key); continue; // Source support, never a saved correction or AI dismissal.
      }
      let reason;
      if (!changed) reason = 'No exact saved correction was confirmed for this finding.';
      else if (!complete || !sameBinding) reason = 'The change was saved, but finding resolution could not be verified. Reopen the saved copy and check again.';
      else if (!issue) { appliedProposals.push(it.key); continue; } // Applied suggestion, not a detected-finding resolution.
      else if (current) reason = 'This finding is still detected in the saved check. The saved cell change did not resolve it.';
      else if (!checked) reason = 'The change was saved, but this finding needs a current check from its original source before it can be considered resolved.';
      else { resolved.push(it.key); continue; }
      remaining.push({item: it, currentKey: current?.key, reason});
    }
    return {confirmed: confirmed.length, resolved, appliedProposals, informational, remaining, complete};
  }
  async function fdReadSavedCheck(savedItem) {
    try {
      const fresh = await fillPages(await api('/api/spreadsheet?id=' + savedItem.file_hash));
      const working = await api('/api/spreadsheets/working-copy?id=' + savedItem.file_hash);
      return {fresh, working};
    } catch (_) { return {}; } // The save is still committed. Never retry a mutation to recover a read.
  }
  function fdResolutionText(run) {
    if (!run.fixed) return 'No selected findings were resolved by a saved correction.' + (run.informational ? ` ${plural(run.informational, 'source-confirmed pattern hint')} informational; identifiers unchanged.` : '');
    return Number.isInteger(run.resolved) ? `${plural(run.resolved, 'selected finding')} cleared by the saved check. ${run.informational ? plural(run.informational, 'source-confirmed pattern hint') + ' informational; identifiers unchanged. ' : ''}${run.appliedProposals ? plural(run.appliedProposals, 'selected suggestion') + ' applied. ' : ''}${plural((run.unfixed || []).length, 'selected finding')} still ${(run.unfixed || []).length === 1 ? 'needs' : 'need'} review.`
      : 'Saved changes are in History. Findings are removed only when the saved check no longer detects them.';
  }
  // After an AI fix nobody is asked to pick again. The fixed version is already on screen; anything Dex had no safe change
  // for and that is not a serious error is left as written (recorded as ignored, with the reason, and can be reopened).
  // Serious errors are never set aside automatically: they stay in the list for a person. With the automatic switch off,
  // the result box with its choices is shown instead.
  const fdAutoMode = () => typeof store !== 'undefined' && store.get('rd-auto-tidy', true) !== false;
  // What Dex had left as written on the version being edited by hand: the same items are left as written again on
  // the newly saved version, so a manual fix never brings back errors that were already settled.
  const fdDexLeft = () => fdItems().filter(it => it.state === 'kept' && /^Dex had no safe change/.test(String(it.decision?.reason || ''))).map(it => ({key: it.key}));
  function fdManualSaved(run, receipt, carried) {
    if (carried.length && fdAutoMode()) fdAfterAI({...run, unfixed: carried}, receipt); else state.fdAIRun = run;
  }
  function fdAfterAI(run, report) {
    if (typeof store === 'undefined' || store.get('rd-auto-tidy', true) === false) { fdAIDoneDialog(run, report); return; }
    state.fdAfterAIPending = true;
    state.fdAIRun = {...run, finishing: true, pct: 93}; if (state.report?.file_hash === run.hash) drawSide();
    (async () => {
      const pause = ms => new Promise(r => setTimeout(r, ms)), openStates = ['open', 'selected', 'deferred', 'confirmed'];
      for (let i = 0; i < 200 && (state.report?.file_hash !== run.hash || !state.fixes || fdActiveBusy() || state.aiProposalOperation); i++) await pause(400);
      if (state.report?.file_hash !== run.hash || !state.fixes || versionReadOnly(state.report)) return;
      const mine = new Set((run.unfixed || []).map(x => x.key));
      // A difference from SharePoint is never set aside automatically either.
      // A value that is only written in a different pattern is always left as written, also when SharePoint holds the
      // very same value (that confirms it, it is not a difference).
      const soft = fdItems().filter(it => mine.has(it.key) && it.fp && it.state === 'open' && (/_format_differs$/.test(it.issue?.code || '') ||
        it.tier !== 'blocker' && it.issue?.severity !== 'high' && it.source == null && !/^SharePoint/i.test(String(it.issue?.source || ''))));
      let leftAsWritten = 0;
      // Dex's own follow-up never shows an error: if the report was updated in the meantime (its SharePoint check
      // finished, for example) the step is simply done again on the updated report.
      const softNow = () => fdItems().filter(it => mine.has(it.key) && it.fp && it.state === 'open' && (/_format_differs$/.test(it.issue?.code || '') ||
        it.tier !== 'blocker' && it.issue?.severity !== 'high' && it.source == null && !/^SharePoint/i.test(String(it.issue?.source || ''))));
      for (let attempt = 0, todo = soft; attempt < 3 && todo.length && todo.length <= 1000; attempt++) {
        try {
          await fdSendDecision({...findingBinding(todo), ids: todo.map(x => x.key), action: 'reject', from: todo[0], batch: true, quiet: true,
            reason: 'Dex had no safe change to make, so this was left as written by Fix with AI.'});
        } catch (e) { /* they stay in the list */ }
        const failed = !!state.decisionFailed; state.decisionFailed = null;
        for (let i = 0; i < 100 && (state.report?.file_hash !== run.hash || !state.fixes || fdActiveBusy()); i++) await pause(400);
        if (state.report?.file_hash !== run.hash || !state.fixes) break;
        const rest = softNow();
        leftAsWritten += todo.length - rest.length;
        if (!failed && rest.length === todo.length) break;   // nothing moved and nothing failed: leave them listed
        todo = rest;
      }
      leftAsWritten = Math.max(0, leftAsWritten);
      if (state.report?.file_hash !== run.hash) return;
      const left = fdItems().filter(it => openStates.includes(it.state)).length;
      state.fdAIRun = {...run, leftAsWritten, summary: true}; drawSide();
      globalThis.setTimeout?.(() => { const side = document.getElementById('side'); if (side) side.scrollTop = 0; }, 0);
      toast(`${run.manual ? `You changed ${plural(run.fixed, 'cell')}` : run.fixed ? `Dex fixed ${plural(run.fixed, 'cell')}` : 'Dex had nothing to change'}${leftAsWritten ? ` and left ${leftAsWritten} as written` : ''}. ` +
        (left ? `${plural(left, 'error')} still ${left === 1 ? 'needs' : 'need'} you (serious, or different from SharePoint): ${left === 1 ? 'it is' : 'they are'} listed on the right.` : 'No possible errors left.'));   // a result, not a failure: never shown as an error message
    })().finally(() => { state.fdAfterAIPending = false; if (state.fdAIRun?.finishing) { state.fdAIRun = {...state.fdAIRun, finishing: false, summary: true}; if (state.report?.file_hash === state.fdAIRun.hash) drawSide(); } });
  }
  function fdSummaryWhenReady() {
    if (state.fdSummaryWanted) return;
    state.fdSummaryWanted = true; let still = 0, tries = 0;
    const tick = () => {
      if (!state.report || location.hash.indexOf('#r=') !== 0 || ++tries > 360) { state.fdSummaryWanted = false; return; }
      const quiet = !fdBackgroundBusy() && !state.busy && !state.rechecking && !state.decisionPending && !state.aiProposalOperation && !!state.fixes;
      still = quiet ? still + 1 : 0;
      if (still < 3) {   // quiet for a second and a half, so nothing is about to reload the report
        setTimeout(tick, 500); return;
      }
      state.fdSummaryWanted = false;
      document.querySelector('.fd-report-summary .fd-summary-request')?.click();
    };
    setTimeout(tick, 500);
  }
  // A SharePoint check or recheck that starts by itself when a report opens must never make AI fix fail:
  // AI fix waits for it, then works on the report as it is now, and starts again by itself if the report updates mid-way.
  const fdBackgroundBusy = () => !!(state.fdTidying || state.fdReconciling || state.fdAutoSPWaiting || state.fdAutoRecheckWaiting || state.rechecking || state.busy || state.decisionPending);
  // Steps that are really running now. A SharePoint check that is only queued does not count: AI fix goes first and the
  // check runs afterwards on the fixed version.
  const fdActiveBusy = () => !!(state.fdTidying || state.fdReconciling || state.fdAutoRecheckWaiting || state.rechecking || state.busy || state.decisionPending);
  async function fdAIFixNow(scope, opts = {}) {
    const keys = new Set(scope.map(it => it.key)), pause = ms => new Promise(r => setTimeout(r, ms));
    for (let attempt = 0; attempt < 3; attempt++) {
      const first = state.report; if (!first) return;
      if (!state.fdAIRun?.running) { state.fdAIRun = {hash: first.file_hash, total: scope.length, running: true, waiting: true, pct: 8}; drawSide(); if (fdAutoMode()) globalThis.setTimeout?.(() => { const side = document.getElementById('side'); if (side) side.scrollTop = 0; }, 0); }
      let list = scope;
      if (attempt || fdActiveBusy()) {
        document.querySelectorAll('dialog.fd-review-dialog[open]').forEach(d => d.close());
        state.fdAIRun = {hash: first.file_hash, total: scope.length, running: true, waiting: true}; drawSide();
        for (let i = 0; (fdActiveBusy() || state.report && !state.fixes) && i < 300; i++) await pause(400);
        const now = state.report; if (!now || now.file_hash !== first.file_hash && !attempt) { state.fdAIRun = null; return; }
        const open = fdItems().filter(it => ['open', 'selected', 'deferred', 'confirmed'].includes(it.state));
        list = opts.everything ? open : open.filter(it => keys.has(it.key));
        if (!list.length) { state.fdAIRun = null; drawSide(); toast('Those errors are already cleared.'); return; }
      }
      const before = state.report;
      if (await fdAIFixRun(list, attempt === 2) !== 'changed') return;
      // The saved check moved on (for example the SharePoint check finished): read the report again before the next try.
      if (state.report === before && !fdActiveBusy()) { try { await openReport(before.file_hash, true); } catch (e) { /* the next try reports it */ } }
      state.fdAIRun = {hash: state.report?.file_hash || before.file_hash, total: scope.length, running: true, waiting: true};
    }
    state.fdAIRun = {hash: state.report?.file_hash, done: true, error: 'The report kept updating. Wait a moment and press AI fix again. Nothing was changed.'};
    toast('AI fix did not finish: ' + state.fdAIRun.error, true); drawSide();
  }
  // Cells (or whole rows) the reviewer ignored or deferred on this version: automatic fixes leave them as written.
  function fdProtectedCells() {
    const decided = (state.fdItems?.length ? state.fdItems : typeof fdItems === 'function' ? fdItems() : []).filter(it => ['kept', 'deferred', 'rejected'].includes(it.state) && it.decision && (it.cell || it.row));
    return p => decided.some(it => (!it.sheet || it.sheet === p.sheet) && (it.cell ? String(it.cell).toUpperCase() === String(p.cell).toUpperCase() : String(p.cell).replace(/^[A-Z]+/i, '') === String(it.row)));
  }
  async function fdAIFixRun(scope, lastTry = false) {
    const report = state.report, startedAt = Date.now(); if (!report) return;
    const binding = reviewBinding(report), hash = report.file_hash, operation = {};
    state.fdAIOperation = operation;
    // Errors that already have a checked answer (SharePoint's own value, or a clean-up with no open question) are applied as they are;
    // Dex is only asked about the rest, and never guesses a value it cannot know.
    const todo = scope.filter(fdCanAI), ready = todo.filter(fdReadyFix), ask = todo.filter(it => !fdReadyFix(it));
    if (!todo.length) {
      // Nothing Dex can change. In automatic mode the minor ones are still left as written, so the count can reach zero.
      if (typeof store !== 'undefined' && store.get('rd-auto-tidy', true) !== false && scope.some(it => it.fp && it.state === 'open')) {
        state.fdAIRun = {hash, done: true, fixed: 0, confirmed: 0, tried: scope.length, skipped: [], unfixed: scope.map(it => ({key: it.key})), items: []}; drawSide();
        fdAfterAI(state.fdAIRun, report); return;
      }
      if (state.fdAIRun?.waiting) { state.fdAIRun = null; drawSide(); }
      toast('Dex can only fix errors that are still open. Fix these manually or ignore them.', true); return;
    }
    document.querySelectorAll('dialog.fd-review-dialog[open]').forEach(d => d.close());
    state.fdAIRun = {hash, total: todo.length, running: true, pct: 18}; drawSide();
    for (let i = 0; state.aiProposalOperation && i < 200; i++) await new Promise(r => setTimeout(r, 300)); // let the summary finish first
    const proposals = [], skipped = [];
    try {
      if (state.busy || state.rechecking || state.decisionPending || state.aiProposalOperation || state.report !== report) throw new Error('Another step is still running. Try again in a moment.');
      for (const it of ready) proposals.push({...it.fix, finding_id: it.key, after: it.proposed ?? it.fix.after});
      for (let i = 0; i < ask.length; i += 120) {
        const chunk = ask.slice(i, i + 120);
        if (state.fdAIRun?.running && state.fdAIOperation === operation) { state.fdAIRun = {...state.fdAIRun, asking: true, part: Math.floor(i / 120) + 1, parts: Math.ceil(ask.length / 120), pct: 20 + Math.round(45 * i / ask.length)}; drawSide(); }
        const answer = await new Promise(resolve => {
          fdPrepareAI(chunk, binding, {owner: document.body, active: () => state.report === report, pending: () => {}, failure: error => resolve({error}), success: a => resolve(a)}).then(() => resolve(null), error => resolve({error}));
        });
        if (!answer || answer.error) {
          if (state.report && (state.report !== report || JSON.stringify(reviewBinding(state.report)) !== JSON.stringify(binding) || !lastTry && answer?.error?.httpStatus === 409 && (fdBackgroundBusy() || (state.fdLastBackground || 0) >= startedAt))) throw Object.assign(new Error('The report updated while Dex was working.'), {reportChanged: true});
          const failure = fdAIRequestFailure(answer?.error);
          if (!proposals.length) throw Object.assign(new Error(failure.reason), {httpStatus: failure.http_status, aiCategory: failure.category});
          skipped.push(...ask.slice(i).map(it => ({finding_id: it.key, ...failure}))); break; // guarded save still validates any already prepared fixes
        }
        proposals.push(...answer.proposals); skipped.push(...(answer.skipped || []));
      }
      if (state.report !== report || JSON.stringify(reviewBinding(report)) !== JSON.stringify(binding)) throw Object.assign(new Error('The report changed while suggestions were prepared. Reopen it before applying. No save was started.'), {reportChanged: true});
      // A cell (or whole row) the reviewer ignored or deferred stays as written: leave those out instead of failing the whole save.
      const protectedCell = fdProtectedCells();
      const accepted = fdUniqueAIProposals(proposals).filter(p => !protectedCell(p));
      const why = new Map(skipped.map(x => [x.finding_id, String(x.reason || '')]));
      const describe = (it, reason, currentKey) => ({key: currentKey || it.key, where: [it.sheet, it.cell ? 'cell ' + it.cell : it.row ? 'row ' + it.row : null].filter(Boolean).join(' ') || 'Whole report',
        text: it.text || it.reason || '', now: it.report, reason: why.get(it.key) || reason || (fdSourceGap(it) ? 'The source evidence must be verified first. AI cannot establish the source date or authority.' : '')});
      const unfixed = scope.filter(it => ['open', 'selected', 'deferred', 'confirmed'].includes(it.state)).map(it => describe(it));
      if (!accepted.length) {
        // Even a no-save run must re-read current evidence before repeating a stale warning.
        const {fresh, working} = await fdReadSavedCheck(report);
        const resolution = fresh?.file_hash === hash ? fdSavedFindingResolution(scope, [], binding, fresh, fresh, working) : null;
        if (state.fdAIOperation !== operation) return;
        const informative = new Set(resolution?.informational || []);
        state.fdAIRun = {hash, done: true, fixed: 0, confirmed: 0, resolved: 0, informational: informative.size,
          tried: scope.length, skipped: skipped.filter(x => !informative.has(x.finding_id)), unfixed: unfixed.filter(x => !informative.has(x.key)), items: []};
        if (state.report === report && location.hash === '#r=' + hash) {
          if (informative.size) {
            fresh.version_view = {...working, historical: false};
            state.report = fresh; state.fixes = null; state.fdItems = [];
            drawReport(); loadFixes(hash);
          }
          document.body.classList.add('fd-panel-open'); fdAfterAI(state.fdAIRun, fresh?.file_hash === hash ? fresh : report);
        }
        return;
      }
      const dec = Object.fromEntries(accepted.map(p => [p.id, {state: 'accepted', after: p.after}]));
      const capture = captureCorrections(accepted, dec, {}, binding, report);
      state.fdAIRun = {hash, total: todo.length, running: true, saving: accepted.length, pct: 72}; drawSide();
      const savedItem = await applyFixes(capture.accepted, capture.dec, capture.edits, {download: false, binding, independentScope: true, returnReceipt: true, throwOnError: true});
      const next = savedItem.file_hash;
      if (state.fdReviewSelection?.binding === JSON.stringify(binding)) state.fdReviewSelection = null;
      const done = accepted.map(p => { const it = todo.find(x => x.key === p.finding_id); return {where: `${p.sheet} cell ${p.cell}`, text: p.selected_finding_id ? 'Checked mechanical correction: ' + p.reason : it?.text || it?.reason || '', before: p.before, after: p.after}; });
      // The successful save receipt is authoritative even if refreshing the list or report fails.
      // Never turn a committed version into a failed save, or repeat the mutation to recover a read.
      const reused = savedItem.save_outcome?.type === 'existing_version';
      if (state.fdAIOperation === operation) { state.fdAIRun = {hash: next, total: todo.length, running: true, checking: true, pct: 86}; if (state.report?.file_hash === next) drawSide(); }
      const {fresh, working} = await fdReadSavedCheck(savedItem);
      const resolution = fdSavedFindingResolution(scope, accepted, binding, savedItem, fresh, working);
      if (state.fdAIOperation !== operation) return;
      state.fdAIRun = {hash: next, done: true, fixed: accepted.length, confirmed: resolution.confirmed, resolved: resolution.resolved.length, appliedProposals: resolution.appliedProposals.length, informational: resolution.informational.length,
        tried: scope.length, skipped: skipped.filter(x => !resolution.informational.includes(x.finding_id)), unfixed: resolution.remaining.map(x => describe(x.item, x.reason, x.currentKey)),
        from: revisionOf(report), version: revisionOf(savedItem), reused, originalHash: hash, items: done};
      // A finished save must not pull the user back from a newer navigation or open a dialog over another report.
      if ((location.hash === '' || location.hash === '#') && typeof select === 'function') select(next, true);   // never end on the home page
      if (location.hash === '#r=' + next || location.hash === '#r=' + hash) {
        document.body.classList.add('fd-panel-open'); fdAfterAI(state.fdAIRun, savedItem);
      }
    } catch (e) {
      if (e?.reportChanged && !lastTry && state.report?.file_hash === hash && state.fdAIOperation === operation) { state.fdAIRun = {hash, total: todo.length, running: true, waiting: true}; return 'changed'; }
      state.fdAIRun = {hash, done: true, saveCommitted: e?.saveCommitted === true, http_status: e?.httpStatus || null, category: e?.aiCategory || null, error: e?.message || String(e)}; toast((state.fdAIRun.saveCommitted ? '' : 'AI fix did not finish: ') + state.fdAIRun.error, !state.fdAIRun.saveCommitted); }
    finally { if (state.report?.file_hash === state.fdAIRun?.hash) drawSide(); }
  }
  // Fix manually: one error at a time, with its cell highlighted in the spreadsheet. Typed values are kept until
  // "Save my changes", which saves them as a new version (the earlier one is kept).
  function fdManualStart(scope) {
    if (!state.report || !scope.length) return;
    state.fdManual = {binding: JSON.stringify(reviewBinding(state.report)), keys: scope.map(it => it.key), at: 0};
    state.fdCur = null; document.body.classList.add('fd-panel-open');
    const first = (state.fdItems || fdItems()).find(it => it.key === scope[0].key);
    if (first) fdOpen(first); else drawSide();
    document.getElementById('side')?.scrollTo?.({top: 0});
  }
  // Edit a cell manually: pick any cell in the spreadsheet (flagged or not), type the right value, repeat, then save
  // everything as one new version. The earlier version is kept, exactly like every other fix.
  const fdFreeActive = () => !!state.fdFree && !!state.report && fdRebind(state.fdFree);
  function fdFreeStart() {
    if (!state.report || versionReadOnly()) return;
    state.fdManual = null; state.fdCur = null;
    state.fdFree = {binding: JSON.stringify(reviewBinding(state.report)), ids: [], cell: null};
    document.body.classList.remove('fd-panel-open');   // on a phone the spreadsheet has to be visible to pick a cell
    drawSide(); toast('Click the cell you want to change.');
  }
  async function fdFreePick(sheet, cell) {
    const f = state.fdFree, report = state.report, g = state.grid, row = splitCell(cell)?.[1];
    if (!fdFreeActive() || !row) return;
    fdFreeKeep();
    if ((g?.formulas || []).includes(cell)) { toast('That cell is a formula. Change the cells it adds up instead.', true); return; }
    Object.assign(f, {sheet, cell, loading: true, before: null, error: null}); document.body.classList.add('fd-panel-open'); drawSide(); drawGrid();
    try {
      const data = await api('/api/spreadsheets/cells', {file_hash: report.file_hash, sheet, row});
      if (state.fdFree !== f || state.report !== report || f.cell !== cell || f.sheet !== sheet) return;
      const exact = (data.cells || []).find(c => c.cell === cell);
      if (!exact || exact.formula || typeof exact.before !== 'string') Object.assign(f, {loading: false, error: 'This cell holds a formula or is protected, so it cannot be typed over. Pick another cell.'});
      else Object.assign(f, {loading: false, before: exact.before, header: exact.header || ''});
    } catch (e) { if (state.fdFree === f) Object.assign(f, {loading: false, error: `This cell could not be opened: ${e.message}`}); }
    if (state.fdFree === f && state.report === report) { drawSide(); globalThis.setTimeout?.(() => { const el = document.getElementById('fd-free-input'); if (el) { el.focus(); el.select(); } }, 0); }
  }
  // Remember what is typed in the open cell (nothing is saved to the report yet).
  function fdFreeKeep() {
    const f = state.fdFree, el = typeof document !== 'undefined' ? document.getElementById('fd-free-input') : null;
    if (!fdFreeActive() || !el || !f.cell || typeof f.before !== 'string') return;
    const all = store.get(editsKey(), {}), id = editId(f.sheet, f.cell);
    if (el.value === f.before) { delete all[id]; f.ids = f.ids.filter(x => x !== id); }
    else { all[id] = {sheet: f.sheet, cell: f.cell, before: f.before, after: el.value, header: '', source: 'Your edit', reason: 'Typed by the reviewer'}; if (!f.ids.includes(id)) f.ids.push(id); }
    store.set(editsKey(), all);
  }
  function fdFreeCard() {
    if (!fdFreeActive()) return null;
    const f = state.fdFree, edits = store.get(editsKey(), {}), mine = f.cell ? edits[editId(f.sheet, f.cell)] : null;
    const pending = f.ids.filter(id => edits[id] && id !== (f.cell ? editId(f.sheet, f.cell) : null));
    const show = v => v === '' || v == null ? 'blank' : '“' + String(v).slice(0, 60) + '”';
    const cancel = () => { const all = store.get(editsKey(), {}); for (const id of f.ids) delete all[id]; store.set(editsKey(), all); state.fdFree = null; drawSide(); drawGrid(); };
    const another = () => { fdFreeKeep(); f.cell = null; f.before = null; document.body.classList.remove('fd-panel-open'); drawSide(); toast('Click the next cell you want to change.'); };
    const save = async () => {
      fdFreeKeep(); const c = readyChanges();
      if (!c.total) { toast('Nothing was changed yet. Type a different value first.'); return; }
      const report = state.report, from = revisionOf(report);
      const carried = fdDexLeft();
      state.fdFree = null; drawSide();   // the typed box is captured above; left on screen it would count as an unsaved draft and block opening the saved version
      const receipt = await applyFixes(c.accepted, c.dec, c.edits, {download: false, binding: c.binding, returnReceipt: true}), next = receipt?.file_hash;
      if (next) fdManualSaved({hash: next, done: true, fixed: c.total, tried: c.total, from, version: revisionOf(receipt), reused: receipt.save_outcome?.type === 'existing_version', originalHash: report.file_hash, manual: true, summary: fdAutoMode(), leftAsWritten: 0,
        items: Object.values(c.edits).map(e => ({where: `${e.sheet} cell ${e.cell}`, text: '', before: e.before, after: e.after}))}, receipt, carried);
      if (state.report?.file_hash === next) drawSide();
    };
    const input = f.cell && typeof f.before === 'string' ? h('input', {id: 'fd-free-input', class: 'fd-manual-input', 'aria-label': `New value for ${f.sheet} cell ${f.cell}`, value: mine ? mine.after : f.before, placeholder: 'Type the correct value'}) : null;
    return h('section', {class: 'fd-manual fd-free', 'aria-label': 'Edit a cell manually'},
      h('div', {class: 'fd-manual-top'}, h('b', {}, 'Edit a cell manually'),
        h('button', {type: 'button', class: 'fd-iconbtn', 'aria-label': 'Stop editing manually', title: 'Close', onclick: cancel}, icon('x', 16))),
      !f.cell ? h('p', {role: 'status'}, 'Click the cell you want to change in the spreadsheet.')
        : f.loading ? h('p', {role: 'status'}, h('span', {class: 'spin'}), ` Opening ${f.sheet} cell ${f.cell}…`)
        : f.error ? h('p', {class: 'note', role: 'alert'}, f.error)
        : h('form', {class: 'fd-manual-form', onsubmit: e => { e.preventDefault(); save(); }},
            h('p', {class: 'fd-manual-where'}, h('b', {}, `${f.sheet} · cell ${f.cell}`), f.header ? ` · ${f.header}` : ''),
            h('p', {class: 'fd-hint'}, `Now: ${show(f.before)}. An empty box clears the cell (blank is not zero).`),
            input),
      pending.length ? h('details', {class: 'fd-saved-changes', open: true}, h('summary', {}, `${plural(pending.length, 'other change')} waiting to be saved`),
        h('ul', {class: 'fd-fixed-list'}, pending.map(id => h('li', {}, h('b', {}, `${edits[id].sheet} cell ${edits[id].cell}`), ` ${show(edits[id].before)} → ${show(edits[id].after)} `,
          h('button', {type: 'button', class: 'btn ghost small', onclick: () => { const all = store.get(editsKey(), {}); delete all[id]; store.set(editsKey(), all); f.ids = f.ids.filter(x => x !== id); drawSide(); }}, 'Remove'))))) : null,
      h('div', {class: 'fd-cardacts'},
        h('button', {type: 'button', class: 'btn primary', id: 'fd-free-save', disabled: state.busy || null, onclick: save}, 'Save my changes'),
        f.cell && !f.loading ? h('button', {type: 'button', class: 'btn', id: 'fd-free-another', onclick: another}, 'Change another cell') : null,
        h('button', {type: 'button', class: 'btn ghost', onclick: cancel}, 'Cancel')));
  }
  // Fixing by hand survives the app's own background updates of the same saved version (a recheck, the SharePoint
  // check, a refresh): the work in progress is carried over to the updated check instead of closing by itself.
  function fdRebind(m) {
    if (!m || !state.report) return false;
    const now = JSON.stringify(reviewBinding(state.report));
    if (m.binding === now) return true;
    let was = null; try { was = JSON.parse(m.binding); } catch (e) { was = null; }
    if (was?.file_hash !== state.report.file_hash) return false;
    m.binding = now; return true;
  }
  function fdManualList(items) {
    const m = state.fdManual;
    if (!m || !state.report || !fdRebind(m)) return null;
    const list = m.keys.map(k => items.find(it => it.key === k)).filter(Boolean);
    return list.length ? {list, at: Math.min(m.at, list.length - 1)} : null;
  }
  function fdManualCard({list, at}) {
    const it = list[at], edits = store.get(editsKey(), {}), mine = it.cell ? edits[editId(it.sheet, it.cell)] : null;
    const pending = Object.keys(edits).length;
    const go = n => { state.fdManual.at = n; const next = list[n]; if (next) fdOpen(next); else drawSide(); };
    const end = () => { state.fdManual = null; state.fdCur = null; drawSide(); drawGrid(); };
    const where = [it.sheet, it.cell ? 'cell ' + it.cell : it.row ? 'row ' + it.row : null].filter(Boolean).join(' · ') || 'Whole report';
    // An error about a whole row (a part number, a duplicate, a record not on SharePoint) opens every cell of that row to type in.
    // The current value always comes from the saved workbook row, never from the error text.
    const rowNo = it.row || (it.cell ? splitCell(it.cell)?.[1] : null);
    const rowKey = it.sheet && rowNo ? JSON.stringify([state.report.file_hash, it.sheet, rowNo]) : null;
    state.fdManualRows ||= {};
    let rowCells = null, rowNote = null;
    if (rowKey) {
      const got = state.fdManualRows[rowKey];
      if (!got) { state.fdManualRows[rowKey] = 'loading'; const report = state.report;
        api('/api/spreadsheets/cells', {file_hash: report.file_hash, sheet: it.sheet, row: rowNo})
          .then(row => { state.fdManualRows[rowKey] = row; }, e => { state.fdManualRows[rowKey] = {error: e.message}; })
          .finally(() => { if (state.report === report && state.fdManual) drawSide(); });
        rowNote = 'Loading this row…'; }
      else if (got === 'loading') rowNote = 'Loading this row…';
      else if (got.error) rowNote = `This row could not be opened: ${got.error}`;
      else rowCells = (got.cells || []).filter(c => !c.formula && typeof c.before === 'string');
    }
    const field = (cell, before, value, label) => h('input', {class: 'fd-manual-input', 'data-cell': cell, 'data-before': before, 'data-initial-value': value, 'aria-label': `New value for ${label}`, value, placeholder: 'Type the correct value'});
    const one = it.cell && rowCells ? rowCells.find(c => c.cell === it.cell) : null;
    const input = one ? field(it.cell, one.before, mine ? mine.after : it.source != null ? String(it.source) : it.proposed != null ? String(it.proposed) : one.before, where) : null;
    if (it.cell && rowCells && !one) rowNote = 'This cell holds a formula or is protected, so it cannot be typed over here.';
    const rowFields = rowCells && !it.cell ? rowCells.map(c => h('label', {class: 'fd-manual-rowfield'}, h('span', {}, `${c.header || 'Column'} (${c.cell})`),
      field(c.cell, c.before, edits[editId(it.sheet, c.cell)]?.after ?? c.before, `${c.header || c.cell}`))) : null;
    const manualBinding = state.report.file_hash;   // the saved version this card belongs to
    const keep = (dirtyOnly = false) => {
      let bound = null; try { bound = JSON.parse(state.fdManual?.binding || 'null'); } catch (e) { bound = null; }
      if (!state.report || bound?.file_hash !== manualBinding || state.report.file_hash !== manualBinding) return;
      const all = store.get(editsKey(), {});
      for (const el of document.querySelectorAll('.fd-manual input[data-cell]')) {
        if (dirtyOnly && el.value === el.getAttribute('data-initial-value')) continue;
        const cell = el.getAttribute('data-cell'), before = el.getAttribute('data-before') ?? '', id = editId(it.sheet, cell);
        if (el.value === String(before)) delete all[id];
        else all[id] = {sheet: it.sheet, cell, before, after: el.value, header: '', source: 'Your edit', reason: 'Typed by the reviewer'};
      }
      store.set(editsKey(), all);
    };
    if (state.fdManual) state.fdManual.keepDraft = () => keep(true);
    const save = async () => {
      keep(); const c = readyChanges();
      if (!c.total) { toast('Nothing typed yet. Nothing was changed.'); return; }
      const report = state.report, from = revisionOf(report);
      const carried = fdDexLeft();
      state.fdManual = null;
      const receipt = await applyFixes(c.accepted, c.dec, c.edits, {download: false, binding: c.binding, returnReceipt: true}), next = receipt?.file_hash;
      if (next) fdManualSaved({hash: next, done: true, fixed: c.total, tried: c.total, from, version: revisionOf(receipt), reused: receipt.save_outcome?.type === 'existing_version', originalHash: report.file_hash, manual: true,
        items: Object.values(c.edits).map(e => ({where: `${e.sheet} cell ${e.cell}`, text: '', before: e.before, after: e.after}))}, receipt, carried);
      if (state.report?.file_hash === next) drawSide();
    };
    setTimeout(() => { const el = document.querySelector('.fd-manual-input'); if (el && document.activeElement?.tagName !== 'INPUT') { el.focus(); el.select(); } }, 0);
    return h('section', {class: 'fd-manual', 'aria-label': 'Fix manually'},
      h('div', {class: 'fd-manual-top'}, h('b', {}, `Fix manually · ${at + 1} of ${list.length}`),
        h('button', {type: 'button', class: 'fd-iconbtn', 'aria-label': 'Stop fixing manually', title: 'Close', onclick: end}, icon('x', 16))),
      h('p', {class: 'fd-manual-where'}, where),
      h('p', {}, /^Missing cell\.?$/i.test(String(it.text || '').trim()) ? 'This cell is blank.' : it.text || it.reason || ''),
      one ? h('p', {class: 'fd-hint'}, `Now: ${one.before === '' ? 'blank' : '“' + one.before + '”'}${it.source != null ? ` · SharePoint: “${it.source}”` : ''}`) : null,
      input ? h('form', {class: 'fd-manual-form', onsubmit: e => { e.preventDefault(); keep(); if (at < list.length - 1) go(at + 1); else save(); }},
        input, h('button', {type: 'submit', class: 'btn primary'}, at < list.length - 1 ? 'Next' : 'Save my changes')) :
      rowFields && rowFields.length ? h('form', {class: 'fd-manual-rowform', onsubmit: e => { e.preventDefault(); keep(); if (at < list.length - 1) go(at + 1); else save(); }},
        h('p', {class: 'fd-hint'}, `Row ${rowNo}: change any cell you want.`), ...rowFields,
        h('button', {type: 'submit', class: 'btn primary'}, at < list.length - 1 ? 'Next' : 'Save my changes')) :
        h('p', {class: 'fd-hint'}, rowNote || (rowFields ? 'This row has no cells that can be typed in (formulas only).' : 'This error is about the whole report, not one row. Click a cell in the spreadsheet to change it, or ignore the error.')),
      h('div', {class: 'fd-cardacts'},
        h('button', {type: 'button', class: 'btn small', disabled: at === 0 || null, onclick: () => { keep(); go(at - 1); }}, 'Previous'),
        at < list.length - 1 ? h('button', {type: 'button', class: 'btn small', onclick: () => { keep(); go(at + 1); }}, 'Skip') : null,
        h('button', {type: 'button', class: 'btn small' + (at < list.length - 1 ? '' : ' subtle'), onclick: save}, `Save my changes${pending ? ` (${pending})` : ''}`)));
  }
  // The message box after every AI fix: what was fixed, that it is saved, what still needs an answer, and downloads.
  function fdAISummaryText(run, report) {
    const lines = ['Verification App - AI fix summary', report ? `Report: ${report.filename}` : '', `Date: ${new Date().toLocaleString()}`, '',
      `${run.reused ? 'Requested changes in existing copy' : 'Saved cell changes'}: ${run.fixed}`, run.fixed ? `${run.reused ? 'Opened existing version' : 'Saved as version'} ${run.version || (run.from || 1) + 1} (version ${run.from || 1} is kept). ${run.reused ? 'No new version was created. ' : ''}Confirmed in the saved copy: ${run.confirmed} of ${run.fixed}.` : 'Nothing was changed.', fdResolutionText(run), ''];
    for (const x of run.items || []) lines.push(`${run.reused ? 'REQUESTED' : 'SAVED'}  ${x.where}: ${x.before === '' ? 'blank' : '"' + x.before + '"'} -> "${x.after}"${x.text ? '   (' + x.text + ')' : ''}`);
    if ((run.unfixed || []).length) { lines.push('', `Still needs review: ${run.unfixed.length}`);
      for (const x of run.unfixed) lines.push(`NEEDS REVIEW  ${x.where}: ${x.text}${x.reason ? '   Reason: ' + x.reason : ''}`); }
    return lines.join('\r\n');
  }
  function fdAIDoneDialog(run, report) {
    document.getElementById('fd-aidone')?.remove();
    const allSaved = run.confirmed === run.fixed, unfixed = run.unfixed || [];
    const live = keys => { if (state.report?.file_hash !== run.hash) return []; const all = state.fdItems || fdItems(); return keys.map(k => all.find(it => it.key === k)).filter(Boolean); };
    // A part number or ID written in a different pattern from the rest is a hint, never an error by itself.
    const patterns = unfixed.filter(x => /has a different format from the other/i.test(String(x.text)));
    const unknown = unfixed.filter(x => !patterns.includes(x) && (!x.reason || /No supported cell suggestion|could not return a safe|source authority|source correction/i.test(x.reason)));
    const other = unfixed.filter(x => !unknown.includes(x) && !patterns.includes(x));
    // "Blank" is only said about cells that really are empty.
    const allBlank = unknown.length > 0 && unknown.every(x => (x.now == null || x.now === '') && /^Missing cell\.?$|blank/i.test(String(x.text).trim()));
    const d = h('dialog', {id: 'fd-aidone', class: 'fd-modal fd-aidone', 'aria-labelledby': 'fd-aidone-title'});
    const close = () => { d.close(); d.remove(); };
    const saveText = () => { const a = h('a', {href: URL.createObjectURL(new Blob([fdAISummaryText(run, report)], {type: 'text/plain'})), download: `AI fix summary - ${(report?.filename || 'report').replace(/\.[^.]+$/, '')}.txt`}); document.body.append(a); a.click(); setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 500); };
    const line = x => h('li', {}, h('b', {}, x.where), `: ${/^Missing cell\.?$/i.test(String(x.text).trim()) ? 'this cell is blank' : x.text}${x.now != null && x.now !== '' ? ` (now “${String(x.now).slice(0, 40)}”)` : ''}`);
    d.append(h('button', {type: 'button', class: 'fd-dialog-x', 'aria-label': 'Close', title: 'Close', onclick: close}, icon('x', 18)),
      h('h2', {id: 'fd-aidone-title', tabindex: '-1'}, run.reused ? 'Opened the existing matching version' : !run.fixed ? run.informational ? 'Checked the selected findings' : 'Dex could not fix these' : `Saved ${plural(run.fixed, 'cell change')}`),
      run.fixed ? h('p', {class: allSaved ? 'fd-aidone-ok' : 'fd-aidone-warn', role: 'status'}, allSaved
        ? run.reused ? `All ${plural(run.fixed, 'requested change')} match existing version ${run.version}. No new version was created. Version ${run.from || 1} is kept.`
        : `All ${plural(run.fixed, 'change')} ${run.fixed === 1 ? 'is' : 'are'} applied and saved as version ${run.version || (run.from || 1) + 1}. Version ${run.from || 1} is kept.`
        : `${run.confirmed} of ${run.fixed} changes could be confirmed in the saved version. Open the report again and check the rest.`)
        : h('p', {class: 'fd-aidone-warn', role: 'status'}, 'Nothing was changed.'),
      run.fixed || run.informational ? h('p', {class: 'fd-resolution', role: 'status'}, fdResolutionText(run)) : null,
      (run.items || []).length ? h('details', {class: 'fd-saved-changes'}, h('summary', {}, `${run.reused ? 'Requested changes matched' : 'Saved changes'} (${run.items.length})`), h('ul', {class: 'fd-fixed-list'}, run.items.slice(0, 200).map(x => h('li', {},
        h('b', {}, x.where), ` ${x.before === '' ? 'blank' : '“' + x.before + '”'} → “${x.after}”`)))) : null,
      unknown.length ? h('section', {class: 'fd-aidone-ask'}, h('h3', {}, `Dex does not know the right value for ${unknown.length === 1 ? 'this one' : 'these ' + unknown.length}`),
        h('ul', {class: 'fd-fixed-list'}, unknown.slice(0, 100).map(line)),
        h('p', {}, allBlank ? `Would you like to add ${unknown.length === 1 ? 'a value' : 'values'}, or leave ${unknown.length === 1 ? 'it' : 'them'} blank?` : `Would you like to change ${unknown.length === 1 ? 'it' : 'them'}, or leave ${unknown.length === 1 ? 'it' : 'them'} as written?`),
        h('div', {class: 'fd-cardacts'},
          h('button', {type: 'button', class: 'btn primary', onclick: () => { const list = live(unknown.map(x => x.key)); close(); if (list.length) fdManualStart(list); }}, allBlank ? (unknown.length === 1 ? 'Add a value' : 'Add values') : (unknown.length === 1 ? 'Change the value' : 'Change the values')),
          h('button', {type: 'button', class: 'btn', onclick: () => { const list = live(unknown.map(x => x.key)); close(); if (list.length) fdReviewDialog(list, 'ignore'); }}, allBlank ? (unknown.length === 1 ? 'Leave it blank' : 'Leave them blank') : (unknown.length === 1 ? 'Leave it as is' : 'Leave them as is')))) : null,
      patterns.length ? h('section', {class: 'fd-aidone-ask'}, h('h3', {}, `${plural(patterns.length, 'part number or ID is', 'part numbers or IDs are')} written in a different pattern`),
        h('p', {}, `For example: ${String(patterns[0].text).replace(/\s*\(expected like.*$/, '')}. A different pattern alone is not an error, so Dex left ${patterns.length === 1 ? 'it' : 'them'} exactly as written.`),
        h('div', {class: 'fd-cardacts'},
          h('button', {type: 'button', class: 'btn primary', onclick: () => { const list = live(patterns.map(x => x.key)); close(); if (list.length) fdReviewDialog(list, 'ignore'); }}, patterns.length === 1 ? 'Ignore it' : `Ignore all ${patterns.length}`),
          h('button', {type: 'button', class: 'btn', onclick: () => { const list = live(patterns.map(x => x.key)); close(); if (list.length) fdManualStart(list); }}, 'Fix manually'))) : null,
      other.length ? h('section', {}, h('h3', {}, `Needs a person (${other.length})`), h('ul', {class: 'fd-fixed-list'}, other.slice(0, 100).map(x => h('li', {}, h('b', {}, x.where), `: ${x.text}`, x.reason ? h('p', {class: 'fd-hint'}, x.reason) : null))),
        h('p', {class: 'fd-hint'}, 'These findings have not been confirmed resolved. Review the reason for each before deciding what to do next.'),
        h('div', {class: 'fd-cardacts'}, h('button', {type: 'button', class: 'btn', onclick: () => { const list = live(other.map(x => x.key)); close(); if (list.length) fdManualStart(list); }}, 'Fix manually'))) : null,
      h('div', {class: 'fd-modalacts'},
        report && run.reused ? h('a', {class: 'btn', href: '#r=' + report.file_hash, onclick: close}, 'Open working copy') : null,
        run.originalHash ? h('a', {class: 'btn', href: '#r=' + run.originalHash + '&history=1', onclick: close}, `Open starting version ${run.from || 1}`) : null,
        report && run.fixed ? h('a', {class: 'btn primary', href: '/api/spreadsheets/download?id=' + report.file_hash, download: report.filename}, 'Download updated report') : null,
        typeof sharePointUrl === 'function' && sharePointUrl(report || state.report) ? h('a', {class: 'btn', href: sharePointUrl(report || state.report), target: '_blank', rel: 'noopener'}, 'Open SharePoint data') : null,
        h('button', {type: 'button', class: 'btn', onclick: saveText}, 'Download summary'),
        h('button', {type: 'button', class: 'btn', onclick: close}, 'Close')));
    d.addEventListener('cancel', e => { e.preventDefault(); close(); });
    document.body.append(d); d.showModal(); d.querySelector('h2').focus();
  }
  // What changed in this run, shown on the report itself (no extra window): fixed, left as written, still open.
  function fdRevisionSummary(run, left, dismiss) {
    const r = state.report, items = run.items || [];
    // Formatting the app cleaned up by itself when the report opened is part of this version too.
    const tidied = !items.length && r?.fixed_from?.changes?.length ? r.fixed_from.changes.map(c => ({where: `${c.sheet} cell ${c.cell}`, before: c.before ?? '', after: c.after ?? ''})) : [];
    const show = v => v === '' || v == null ? 'blank' : '“' + String(v).slice(0, 80) + '”';
    const list = (title, rows) => rows.length ? h('details', {class: 'fd-saved-changes'}, h('summary', {}, title),
      h('ul', {class: 'fd-fixed-list'}, rows.slice(0, 200).map(x => h('li', {}, h('b', {}, x.where), ` ${show(x.before)} → ${show(x.after)}`)), rows.length > 200 ? h('li', {}, `…and ${rows.length - 200} more (see History).`) : null)) : null;
    return h('div', {class: 'fd-airun fd-revision ' + (left ? '' : 'good'), role: 'status'},
      h('h3', {}, 'Revision summary'),
      h('ul', {class: 'fd-revision-counts'},
        run.tidy ? null : h('li', {}, h('b', {}, String(run.fixed || 0)), run.manual ? (run.fixed === 1 ? ' cell changed by you' : ' cells changed by you') : run.fixed === 1 ? ' cell fixed by Dex' : ' cells fixed by Dex'),
        tidied.length ? h('li', {}, h('b', {}, String(tidied.length)), ' formatting clean-ups made when the report opened') : null,
        run.look ? h('li', {}, 'Report colours made consistent (one cream, no yellow patches); no value was changed') : null,
        run.tidy || run.manual ? null : h('li', {}, h('b', {}, String(run.leftAsWritten || 0)), ' left as written'),
        h('li', {class: left ? 'fd-revision-open' : ''}, h('b', {}, String(left)), left === 1 ? ' possible error still needs you' : ' possible errors still need you')),
      left === 0 ? h('p', {class: 'fd-hint'}, 'Nothing is left to fix, so the Fix buttons are not shown. Download the fixed report below.') : null,
      run.fixed ? h('p', {class: 'fd-hint'}, `${run.reused ? 'Matches saved version' : 'Saved as version'} ${run.version || (run.from || 1) + 1}. Version ${run.from || 1} is kept unchanged.`) : null,
      list(run.manual ? `See the ${plural(items.length, 'change')} you made` : `See the ${plural(items.length, 'change')} Dex made`, items), list(`See the ${plural(tidied.length, 'formatting clean-up')}`, tidied),
      h('div', {class: 'fd-cardacts'},
        // What is left can be fixed by hand or ignored right here, without looking for the buttons further down.
        left ? h('button', {type: 'button', class: 'btn primary small', id: 'fd-revision-manual', onclick: () => { const open = fdItems().filter(it => ['open', 'selected', 'deferred', 'confirmed'].includes(it.state)); if (open.length) { state.fdAIRun = null; fdManualStart(open); } }}, 'Fix manually') : null,
        left ? h('button', {type: 'button', class: 'btn small', id: 'fd-revision-ignore', onclick: () => { const open = fdItems().filter(it => ['open', 'selected', 'deferred', 'confirmed'].includes(it.state)); if (open.length) fdReviewDialog(open, 'ignore'); }}, left === 1 ? 'Ignore it' : 'Ignore them') : null,
        r && (run.fixed || tidied.length || run.look) ? h('a', {class: 'btn ' + (left ? '' : 'primary ') + 'small', href: '/api/spreadsheets/download?id=' + r.file_hash, download: r.filename}, 'Download fixed report') : null,
        run.fixed || tidied.length ? h('button', {type: 'button', class: 'btn small', onclick: () => { state.fdView = 'history'; drawReport(); }}, 'View history') : null,
        dismiss));
  }
  function fdAIRunBanner(run, left) {
    const dismiss = h('button', {type: 'button', class: 'btn small subtle', onclick: () => { state.fdAIRun = null; drawSide(); }}, 'OK');
    if (run.running || run.finishing) {
      const label = run.finishing ? 'Finishing up…' : run.waiting ? 'Getting the report ready for Dex…' : run.checking ? 'Checking the saved version…' : run.saving ? `Saving ${plural(run.saving, 'fix', 'fixes')}…`
        : run.asking && run.parts > 1 ? `Dex is fixing ${plural(run.total, 'error')} (part ${run.part} of ${run.parts})…` : `Dex is fixing ${plural(run.total, 'error')}…`;
      const pct = Math.max(4, Math.min(99, Math.round(run.pct || 12)));
      const bar = h('div', {class: 'fd-progress-bar'}); if (bar.style) bar.style.width = pct + '%';   // set through the DOM: inline style attributes are blocked
      return h('div', {class: 'fd-airun fd-airun-busy', role: 'status'}, h('p', {}, h('span', {class: 'spin'}), ' ' + label, h('span', {class: 'fd-progress-pct'}, `${pct}%`)),
        h('div', {class: 'fd-progress', role: 'progressbar', 'aria-label': 'Fix with AI progress', 'aria-valuemin': '0', 'aria-valuemax': '100', 'aria-valuenow': String(pct)}, bar),
        h('p', {class: 'fd-hint'}, 'Stay on this page. The fixed report opens here when Dex is done.'));
    }
    if (run.summary && !run.error) return fdRevisionSummary(run, left, dismiss);
    if (run.error) return h('div', {class: 'fd-airun bad', role: 'alert'}, h('p', {}, run.saveCommitted ? run.error : `AI fix did not finish: ${run.error}`), dismiss);
    const plain = t => /report holds/i.test(t) ? 'This report is on hold (for example the client or SharePoint is not confirmed yet), so Dex is not allowed to change it.'
      : /source authority|source correction/i.test(t) ? 'This one depends on SharePoint, so Dex cannot decide the right value.'
      : /unambiguous editable cell|does not identify an editable cell/i.test(t) ? 'This error is about a whole row, not one cell, so it needs a person.'
      : /No supported cell suggestion|could not return a safe/i.test(t) ? 'Dex does not know the right value (it never guesses missing numbers).' : t;
    const reasons = new Map(); for (const x of run.skipped || []) { const t = plain(String(x.reason || '')); reasons.set(t, (reasons.get(t) || 0) + 1); }
    const why = reasons.size ? h('ul', {class: 'fd-airun-why'}, [...reasons].sort((a, b) => b[1] - a[1]).slice(0, 4).map(([t, n]) => h('li', {}, `${n} not fixed: ${t}`))) : null;
    if (!run.fixed && run.informational) return h('div', {class: 'fd-airun', role: 'status'}, h('p', {}, 'Nothing was changed.'), h('p', {}, fdResolutionText(run)), h('p', {}, `Selected findings still needing review: ${(run.unfixed || []).length}.`), why, dismiss);
    if (!run.fixed) return h('div', {class: 'fd-airun', role: 'status'}, h('p', {}, `Dex could not fix ${run.tried === 1 ? 'this error' : `these ${run.tried} errors`}. Nothing was changed. Fix ${run.tried === 1 ? 'it' : 'them'} manually or ignore ${run.tried === 1 ? 'it' : 'them'}.`), why, dismiss);
    return h('div', {class: 'fd-airun good', role: 'status'},
      h('p', {}, h('b', {}, `${run.reused ? 'Requested changes match' : run.manual ? 'You saved' : 'Dex saved'} ${plural(run.fixed, 'cell change')}.`), ` ${run.reused ? 'Opened existing version' : 'Saved as version'} ${run.version || (run.from || 1) + 1}; version ${run.from || 1} is kept. ${run.reused ? 'No new version was created.' : ''}`),
      h('p', {}, fdResolutionText(run)),
      h('button', {type: 'button', class: 'btn small', onclick: () => { state.fdView = 'history'; drawReport(); }}, 'View saved changes in History'),
      h('p', {}, left ? `${plural(left, 'error')} left to fix. ${left === 1 ? 'It is' : 'They are'} listed below.` : 'Nothing left to fix.'), why, dismiss);
  }
  // ---------- the app fixes what it can by itself (new version, original kept), then shows only what needs a person
  function fdAutoEligible(r) {
    if (!r || r.approval || (r.sent || []).some(x => x.sent) || r.auto_fix_tried || r.auto_fixed || (r.fixes_applied || []).length) return false;
    if ((r.fixed_from?.changes || []).some(c => String(c.source || '').startsWith('Reverted'))) return false;
    const open = allIssues(r.review).filter(i => i.severity !== 'info').length;
    return open > 0 || (state.fixes || []).some(f => f.sure);
  }
  // the pass runs as short steps (sure fixes, then Claude in batches, then one SharePoint check), so no request runs
  // long and the rest of the page keeps loading; the reviewer can keep reading while it works
  async function fdAutoLoop(hash, phase) {
    toast('Automatic changes are paused. Review and apply cell corrections instead.', true);
  }
  // Group recorded history without changing, deduplicating or reclassifying any occurrence.
  function fdHistoryGroups(summary) {
    const groups = new Map();
    for (const [list, category] of [['changed', 'applied'], ['fine', 'recommendation'], ['removed', 'removed'], ['moved', 'moved']]) {
      (summary[list] || []).forEach((entry, index) => {
        const blank = v => v === '' || v == null;
        const action = category === 'applied' ? (blank(entry.before) && !blank(entry.after) ? 'Blank cells filled' : !blank(entry.before) && blank(entry.after) ? 'Cells cleared' : 'Cell values changed')
          : category === 'recommendation' ? 'AI recommendations to keep as recorded' : category === 'removed' ? 'Rows removed' : 'Rows moved';
        const rule = entry.rule_id || entry.rule || entry.code || entry.reason || 'Unspecified recorded reason';
        const status = entry.reverted ? 'reverted' : category;
        const key = JSON.stringify([category, status, rule, entry.sheet || null, entry.to_sheet || null, action,
          entry.source || null, entry.by || null, entry.severity || null, entry.risk || null, entry.kind || null,
          entry.decision || null, entry.held || false, entry.hold || null]);
        if (!groups.has(key)) groups.set(key, {key, category, status, action, rule, sheet: entry.sheet || null, entries: []});
        groups.get(key).entries.push({id: `${list}:${index}`, entry});
      });
    }
    return [...groups.values()].map(group => ({...group,
      cells: new Set(group.entries.filter(x => x.entry.sheet && x.entry.cell).map(x => JSON.stringify([x.entry.sheet, String(x.entry.cell).toUpperCase()]))).size,
      rows: new Set(group.entries.filter(x => x.entry.sheet && (x.entry.row || /[0-9]+$/.test(x.entry.cell || '')))
        .map(x => JSON.stringify([x.entry.sheet, Number(x.entry.row || String(x.entry.cell).match(/[0-9]+$/)[0])]))).size}));
  }
  function fdHistoryGroup(g, options = {}) {
    const pageSize = 25, total = g.entries.length;
    let page = 0;
    const summary = h('summary', {}, h('b', {}, `${options.unverified ? 'Unverified record: ' : ''}${g.action} · ${plural(total, 'occurrence')}`),
      h('span', {}, `${g.sheet || 'Report-level'} · ${plural(g.cells, 'distinct cell')} · ${plural(g.rows, 'distinct row')}`),
      h('small', {}, String(g.rule)));
    const range = h('p', {class: 'fd-history-range', tabindex: '-1', 'aria-live': 'polite'});
    const list = h('ul', {class: 'fd-history-page', tabindex: '0', 'aria-label': options.finding ? 'Recorded findings on this page' : 'Recorded occurrences on this page'});
    const previous = h('button', {type: 'button', class: 'btn small', onclick: () => { if (page > 0) { page--; render(true); } }}, 'Previous');
    const next = h('button', {type: 'button', class: 'btn small', onclick: () => { if ((page + 1) * pageSize < total) { page++; render(true); } }}, 'Next');
    const group = h('details', {class: 'fd-history-group' + (options.finding ? ' fd-finding-group' : ''), 'data-history-key': g.key, 'data-finding-status': options.finding ? g.status : null}, summary,
      h('p', {class: 'fd-hint fd-history-notice'}, options.notice || (g.status === 'reverted' ? 'Already reverted in the recorded history.' : g.category === 'recommendation'
        ? 'This recommendation does not resolve a source hold. Use the individual finding controls below to review it.'
        : 'Automatic reversal is paused. Review a new manual correction against the current cells, or open the earlier version.')),
      range, h('nav', {class: 'fd-history-pages', 'aria-label': 'Occurrence pages'}, previous, next,
        h('button', {type: 'button', class: 'btn small', onclick: () => { group.open = false; summary.focus(); }}, 'Collapse')), list);
    function render(focus) {
      const start = page * pageSize;
      range.textContent = `Occurrences ${start + 1}–${Math.min(start + pageSize, total)} of ${total} · ${pageSize} per page`;
      previous.disabled = page === 0; next.disabled = start + pageSize >= total;
      list.replaceChildren(...g.entries.slice(start, start + pageSize).map(({id, entry: x}) => h('li', {[options.finding ? 'data-finding-id' : 'data-history-id']: id},
        options.renderEntry ? options.renderEntry(x) : h('div', {}, h('b', {}, x.sheet ? `${x.sheet}${x.cell ? ' · ' + x.cell : x.row ? ' · row ' + x.row : ''}` : 'Report-level occurrence'),
          g.category === 'applied' ? h('p', {}, `${showWs(x.before)} → ${showWs(x.after)}`) : h('p', {}, x.text || (x.to_sheet ? `Moved to ${x.to_sheet}` : g.action)),
          h('small', {}, [x.source, x.by, x.reason !== g.rule ? x.reason : null].filter(Boolean).join(' · '))))));
      list.scrollTop = 0;
      if (focus) range.focus();
    }
    render(false);
    return group;
  }
  function fdAutoSummary(r) {
    const s = r.auto_fixed; if (!s) return null;
    const groups = fdHistoryGroups(s), inconsistent = r.version_view?.unavailable_reason === 'history_inconsistent';
    const applied = groups.filter(g => g.status === 'applied' || g.status === 'removed' || g.status === 'moved').reduce((n, g) => n + g.entries.length, 0);
    const recommendations = groups.filter(g => g.status === 'recommendation').reduce((n, g) => n + g.entries.length, 0);
    const appliedLabel = inconsistent ? `${plural(applied, 'recorded change claim')} (history unverified)` : plural(applied, 'recorded applied change');
    const categories = [['applied', 'Applied cell changes'], ['recommendation', 'AI recommendations, not human decisions'], ['removed', 'Applied row removals'], ['moved', 'Applied row moves'], ['reverted', 'Previously reverted records']];
    return h('section', {class: 'fd-auto' + (inconsistent ? ' fd-history-unverified' : ''), 'aria-label': 'Recorded changes and recommendations'},
      h('div', {class: 'fd-autohead'}, icon(inconsistent ? 'warn' : 'ok', 18), h('div', {},
        h('b', {}, `${appliedLabel} · ${plural(recommendations, 'AI recommendation')}`),
        h('p', {}, inconsistent ? 'History could not be verified. These entries are recorded claims, not proof of applied corrections. Existing holds still apply.' : 'History only. Suggested changes and unresolved findings are reviewed separately below. Existing holds still apply.'))),
      s.status === 'running' ? h('p', {class: 'fd-coverage', role: 'note'}, 'This earlier automatic pass stopped part way. Only recorded occurrences are shown; automatic continuation is paused.') : null,
      ...categories.map(([status, title]) => {
        const selected = groups.filter(g => g.status === status); if (!selected.length) return null;
        const count = selected.reduce((n, g) => n + g.entries.length, 0);
        const label = inconsistent ? ({applied: 'Recorded cell changes (history unverified)', removed: 'Recorded row removals (history unverified)', moved: 'Recorded row moves (history unverified)', reverted: 'Recorded reversals (history unverified)'}[status] || title) : title;
        return h('section', {class: 'fd-history-category', 'data-history-status': status}, h('h3', {}, `${label} · ${plural(count, 'occurrence')}`),
          ...selected.map(g => fdHistoryGroup(g, inconsistent ? {unverified: true, notice: 'Recorded entry only. Saved correction history could not be verified. No changes or holds are resolved by this view.'} : {})));
      }),
      s.from_hash ? h('a', {href: '#r=' + s.from_hash + '&history=1'}, inconsistent ? 'Open recorded source version (history unverified)' : 'Open the version before these changes') : null,
      (s.notes || []).length ? h('details', {}, h('summary', {}, 'Recorded notes'), h('p', {}, s.notes.join(' '))) : null);
  }
  function fdHoldAction(r, st) {
    // Keep recovery reachable without repeating the hold's long explanation.
    if (st.next === 'recheck') return h('button', {type: 'button', class: 'btn primary', onclick: fdRecheck}, 'Re-check this report');
    if (st.next === 'reconcile') return fdSourceRefresh(r);
    if (st.next === 'client') return h('button', {type: 'button', class: 'btn primary', onclick: e => assignForm(r, e.currentTarget)}, 'Choose client and job');
    return null;
  }
  function fdNextStep(r, st) {
    const box = (title, text, button) => h('div', {class: 'fd-next ' + st.tone}, h('b', {}, title), text ? h('p', {}, text) : null, button);
    if (st.next === 'recheck') return box('Checks out of date', st.why, h('button', {type: 'button', class: 'btn primary', onclick: fdRecheck}, 'Re-check this report'));
    if (st.next === 'client') return box('Choose the client and job', st.why, h('button', {type: 'button', class: 'btn primary', onclick: e => assignForm(r, e.currentTarget)}, 'Choose client and job'));
    if (st.next === 'reconcile') return box(st.text, st.why, fdSourceRefresh(r));
    return null;
  }
  function fdClean(r, st) {
    const rv = r.review, sp = rv.sharepoint || {}, a = r.assignment || {}, sel = r.dataset_selection || {};
    if (r.checked_copy) return h('p', {class: 'fd-hint'}, fdCheckedState(r).ready ? 'Download the checked copy above. No approval or sending is required.' : 'Nothing left to fix on this report.');
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
      approved ? h('p', {class: 'fd-hint'}, `Approved by ${r.approval.by} ${ago(r.approval.at)}. Sending is paused. Use More → Approval details to review or withdraw approval.`)
        : h('div', {class: 'fd-approve'},
          h('label', {}, 'Your name', h('input', {id: 'fd-approver', value: myName(), autocomplete: 'name'})),
          h('button', {type: 'button', class: 'btn primary', id: 'fd-approve-btn', onclick: (() => { const binding = reviewBinding(r); return () => fdApprove(binding); })()}, 'Approve internally'),
          h('p', {class: 'fd-hint'}, 'Internal approval only. Nothing is emailed; sending to the client is a separate step.')));
  }
  function fdCard(it, groups) {
    const g = groups.find(x => x.key === it.group), inGroup = g.items, pos = inGroup.indexOf(it);
    // Previous / Next go through what still needs a decision; "include decided" also walks the history
    const chosen = state.fdReviewScope?.binding === JSON.stringify(reviewBinding(state.report)) ? state.fdReviewScope.keys : null;
    const scoped = chosen && chosen.includes(it.key);
    const all = scoped ? groups.flatMap(x => x.items).filter(x => chosen.includes(x.key)) : groups.filter(x => x.tier === it.tier).flatMap(x => x.items);
    const flat = scoped || state.fdIncludeDecided ? all : all.filter(x => x.state === 'open' || x === it), at = flat.indexOf(it);
    const cmp = (label, value, cls) => h('div', {class: 'fd-cmp ' + (cls || '')}, h('b', {}, label), h('span', {}, value == null ? (label === 'Report' ? 'No cell value linked' : label === 'Source' ? 'Not available' : 'No change proposed') : showWs(value)));
    const conf = {source: ['Source-backed', 'SharePoint has this value for the same site, list and item.'], mechanical: ['Mechanical', 'Only how the value is written changes.'],
      ai: ['Dex\'s suggestion', 'Not from a source: check it before selecting.'], guess: ['Needs your judgement', 'Worked out from the rest of the report, not from a source.'],
      source_only: ['Source value, no change prepared', 'SharePoint has a value, but no safe cell to change was found. Correct it by hand or keep it on hold.'],
      none: ['No source value', 'Nothing supports a change, so the value stays as recorded and the issue stays open.']}[it.evidence];
    const where = [it.sheet, it.cell || (it.row ? 'row ' + it.row : null), it.item ? 'SharePoint item ' + it.item : null].filter(Boolean).join(' · ');
    const dd = it.decision, by = dd ? ` by ${dd.by || 'a reviewer'} ${when2(dd.at) || ''}` : it.decidedBy ? ' by ' + it.decidedBy : '';
    const decided = it.state === 'kept' ? `Kept as recorded${by}${it.report === '' ? ' (blank stays blank: not recorded, not zero)' : ''}`
      : it.state === 'deferred' ? `Waiting for ${dd.owner || 'the source owner'}${by}. The finding stays open and the report stays on hold.`
      : it.state === 'confirmed' ? `Confirmed: needs a correction${by}`
      : it.state === 'claude' ? `Set aside by Dex${it.issue?.claude?.at ? ' ' + when2(it.issue.claude.at) : ''}. This is Dex's recommendation, not a person's decision: “${it.issue?.claude?.reason || it.issue?.verdict_reason || 'no reason given'}”`
      : it.state === 'kept_earlier' ? `Kept as is by an earlier record (kept unchanged): “${it.issue?.legacy_record?.reason || it.issue?.verdict_reason || ''}”`
      : it.state === 'corrected' ? `${it.issue?.verdict_reason || 'Changed in a later version.'}`
      : it.state === 'rejected' ? `Suggestion rejected${by}` : it.state === 'selected' ? 'Selected for preview' : null;
    const finding = !!it.issue && !it.fix, busy = state.decisionPending || state.rechecking || state.busy || null;
    return h('article', {class: 'fd-card ' + it.tier, 'aria-label': 'Selected correction', id: 'fd-card'},
      h('div', {class: 'fd-cardtop'}, icon(it.tier === 'blocker' ? 'x' : it.tier === 'format' ? 'note' : 'warn', 20),
        h('div', {}, h('h3', {}, it.cell ? `Cell ${it.cell}` : it.row ? `Row ${it.row}` : 'Report finding'), h('p', {title: it.sheet || ''}, `Item ${pos + 1} of ${inGroup.length}${it.item ? ' · source item ' + it.item : ''}`)),
        h('button', {type: 'button', class: 'fd-iconbtn', 'aria-label': 'Close this correction', onclick: () => { state.fdCur = null; drawSide(); }}, icon('x', 16))),
      it.text && it.text !== g.title ? h('p', {class: 'fd-text'}, it.text) : null,
      h('div', {class: 'fd-cmps'}, cmp('Report', it.report ?? (it.fix ? '' : null)), cmp('Source', it.source, it.source == null ? 'empty' : ''), cmp('Proposed', it.proposed, it.proposed == null ? 'empty' : 'prop')),
      h('p', {class: 'fd-reason'}, it.reason),
      it.fix && !it.fp ? h('p', {class: 'fd-hint'}, 'Unselected suggestions stay unchanged. Only selected changes are included in your preview.') : null,
      h('p', {class: 'fd-conf ' + it.evidence}, h('b', {}, conf[0]), ' ', conf[1]),
      it.legacyNote ? h('p', {class: 'fd-coverage'}, icon('warn', 14), ' ', it.legacyNote) : null,
      it.previous ? h('p', {class: 'fd-coverage'}, icon('warn', 14), ` ${it.previous.note} (Earlier: ${it.previous.action} by ${it.previous.by || 'a reviewer'}${it.previous.reason ? ', “' + it.previous.reason + '”' : ''}.)`) : null,
      decided ? h('div', {class: 'fd-decided'}, h('p', {}, icon('ok', 14), ' ', decided),
        it.note ? h('p', {}, `Reason: “${it.note}”`) : null,
        dd && (dd.evidence || []).length ? h('p', {}, 'Evidence: ' + dd.evidence.join('; ')) : null,
        dd && dd.carried_from ? h('p', {}, 'Decided on an earlier version of this report; it still says exactly the same thing here.') : null) : null,
      state.fdForm && state.fdForm.key === it.key ? fdDecisionForm(it) : null,
      h('div', {class: 'fd-cardacts'},
        (it.selectable || it.own) ? h('button', {type: 'button', class: 'btn ' + (it.state === 'selected' ? '' : 'primary'), onclick: () => fdSelect([it], it.state !== 'selected')},
          it.state === 'selected' ? 'Remove from preview' : 'Include in preview') : null,
        h('details', {class: 'fd-source-review'}, h('summary', {class: 'btn subtle'}, 'Source review'),
        it.fix && it.fp && it.state !== 'kept' ? h('button', {type: 'button', class: 'btn', onclick: () => fdDecide(it, 'keep')}, it.report === '' ? 'Keep blank' : 'Keep as recorded') : null,
        finding && it.state === 'open' ? h('button', {type: 'button', class: 'btn', disabled: busy, 'data-recheck-disabled': state.rechecking && !state.decisionPending && !state.busy ? true : null, onclick: () => { state.fdForm = {item: it, key: it.key, action: 'reject'}; drawSide(); }},
          it.report === '' ? 'Keep blank…' : 'Keep as recorded…') : null,
        finding && it.state === 'open' ? h('button', {type: 'button', class: 'btn', disabled: busy, 'data-recheck-disabled': state.rechecking && !state.decisionPending && !state.busy ? true : null, onclick: () => { state.fdForm = {item: it, key: it.key, action: 'defer'}; drawSide(); }}, 'Await source owner…') : null,
        finding && it.state === 'open' ? h('button', {type: 'button', class: 'btn subtle', disabled: busy, 'data-recheck-disabled': state.rechecking && !state.decisionPending && !state.busy ? true : null, onclick: () => fdSendDecision({ids: [it.key], action: 'confirm', from: it})}, 'Confirm it needs a correction') : null,
        dd ? h('button', {type: 'button', class: 'btn subtle', disabled: busy, 'data-recheck-disabled': state.rechecking && !state.decisionPending && !state.busy ? true : null, onclick: () => fdSendDecision({action: 'undo', undo_of: dd.decision_id, from: it})}, 'Undo source decision') : null,
        it.state === 'claude' ? h('button', {type: 'button', class: 'btn', disabled: busy, 'data-recheck-disabled': state.rechecking && !state.decisionPending && !state.busy ? true : null, onclick: () => { state.fdForm = {item: it, key: it.key, action: 'reject', reason: 'Agreed with Claude: ' + (it.issue?.claude?.reason || it.issue?.verdict_reason || '')}; drawSide(); }}, 'Agree with Dex…') : null,
        it.state === 'claude' || it.state === 'kept_earlier' ? h('button', {type: 'button', class: 'btn subtle', disabled: busy, 'data-recheck-disabled': state.rechecking && !state.decisionPending && !state.busy ? true : null, onclick: () => fdSendDecision({ids: [it.key], action: 'reopen', reason: 'Put back for review', from: it})}, it.state === 'claude' ? 'Put back for review' : 'Review again') : null,
        it.state === 'corrected' && it.issue?.corrected?.file_hash ? h('a', {class: 'btn subtle', href: '#r=' + it.issue.corrected.file_hash + '&history=1'}, `Open v${it.issue.corrected.revision || ''}`) : null,
        h('button', {type: 'button', class: 'btn subtle', onclick: () => fdEvidence(it)}, icon('db', 15), ' Source evidence'))),
      fdConflict(it) ? fdCoordinatedForm(it) : it.cell && it.state !== 'kept' ? h('details', {class: 'fd-edit-value'}, h('summary', {class: 'btn subtle'}, 'Edit value'), fdOwnValueForm(it)) : null,
      finding ? null : h('details', {class: 'fd-notebox', open: it.note ? true : null}, h('summary', {}, icon('note', 14), ' Note'),
        h('textarea', {id: 'fd-note', 'aria-label': 'Review note', rows: '2', placeholder: 'Why you kept, rejected or changed this…'}, it.note || ''),
        h('button', {type: 'button', class: 'btn small', onclick: () => fdDecide(it, 'note', $('fd-note').value)}, 'Save note')),
      it.tier === 'blocker' && it.evidence === 'none' ? h('p', {class: 'fd-holdnote'}, icon('lock', 14), ' A blocker can only be kept as recorded with evidence of what you checked. While it waits for the source owner the report stays on hold.') : null,
      h('label', {class: 'fd-incl'}, h('input', {type: 'checkbox', checked: state.fdIncludeDecided || null, onchange: e => { state.fdIncludeDecided = e.target.checked; drawSide(); }}), ' Include decided items in Previous / Next'),
      h('div', {class: 'fd-nav'},
        h('button', {type: 'button', class: 'btn small', disabled: at <= 0, onclick: () => fdOpen(flat[at - 1]), 'aria-keyshortcuts': 'k'}, icon('prev', 15), ' Previous'),
        h('span', {}, `${at + 1} of ${flat.length}${scoped ? ' selected items' : ''}`),
        h('button', {type: 'button', class: 'btn small', disabled: at >= flat.length - 1, onclick: () => fdOpen(flat[at + 1]), 'aria-keyshortcuts': 'j'}, 'Next ', icon('next', 15))));
  }
  // open a correction: its exact cell is scrolled into view and highlighted; the sheet switches if needed
  async function fdOpen(it, selectedCell = null) {
    if (selectedCell && [...document.querySelectorAll('#side input:not([type=checkbox]):not([type=radio]), #side textarea')].some(el => el.value !== el.defaultValue)) { toast('Finish the correction-panel draft before selecting another spreadsheet cell.', true); return; }
    if (!it) return;
    const report = state.report, request = state.fdOpenRequest = (state.fdOpenRequest || 0) + 1;
    state.gridRequest = (state.gridRequest || 0) + 1; // A newer local focus also cancels an older sheet request.
    state.fdCur = it.key; state.fdTier = it.tier;
    if (it.sheet && (it.cell || it.row) && (!state.grid || state.grid.sheet !== it.sheet)) { fdKeepScroll(); await loadGrid(it.sheet, true); }
    if (state.report !== report || state.fdCur !== it.key || state.fdOpenRequest !== request) return;
    if (it.sheet && (it.cell || it.row) && state.grid?.sheet !== it.sheet) {
      state.gridCell = null; state.gridRow = null; drawSide(); drawGrid(); return;
    }
    state.gridCell = selectedCell || it.cell || (it.row ? `A${it.row}` : null); state.gridRow = it.row || null;
    drawSide(); drawGrid();
    const td = state.gridCell && document.querySelector(`#sheet-block td[data-cell="${state.gridCell}"]`), wrap = document.querySelector('#sheet-block .gridwrap');
    if (td && wrap) reveal(wrap, td, true);
    if (document.body.classList.contains('fd-narrow')) document.body.classList.add('fd-panel-open');
    const card = $('fd-card');
    card?.querySelector('h3')?.setAttribute('tabindex', '-1');
    card?.querySelector('h3')?.focus({preventScroll: true});
    card?.scrollIntoView({block: 'nearest', behavior: 'smooth'});
  }
  function fdSelect(list, on) {
    const focusLabel = typeof document !== 'undefined' ? document.activeElement?.getAttribute?.('aria-label') : null;
    const focusKey = typeof document !== 'undefined' ? document.activeElement?.getAttribute?.('data-selection-key') : null;
    if (list.some(it => it.binding && (it.binding.file_hash !== state.report?.file_hash || it.binding.expected_check_hash !== state.report?.check_hash))) { toast('The report changed. Review this correction again.', true); return; }
    const dec = store.get(decisionsKey(), {}), edits = store.get(editsKey(), {});
    for (const it of list) {
      if (on) { if (it.fix) dec[it.fix.id] = {state: 'accepted', after: it.proposed}; }
      else {
        if (it.fix) delete dec[it.fix.id];
        if (it.sheet && it.cell) {
          delete edits[editId(it.sheet, it.cell)];
          for (const f of state.fixes || []) if (f.sheet === it.sheet && String(f.cell).toUpperCase() === String(it.cell).toUpperCase()) delete dec[f.id];
        }
      }
    }
    store.set(decisionsKey(), dec); store.set(editsKey(), edits);
    toast(on ? `${plural(list.length, 'change')} selected for preview. Nothing changes until you preview and apply.` : 'Removed from the preview.');
    fdRefresh();
    if (focusKey || focusLabel) [...document.querySelectorAll('.fd-occurrences input')].find(el => focusKey ? el.getAttribute('data-selection-key') === focusKey : el.getAttribute('aria-label') === focusLabel)?.focus();
  }
  // what the reviewer is looking at, sent with every decision: the check run, and the exact reading of each finding
  function shownFor(keys) {
    const rv = state.report.review || {};
    const all = new Map([...(rv.issues || []), ...(rv.dismissed || [])].map(x => [x.key, x.fp]));
    return {run: rv.run || {}, seen: Object.fromEntries(keys.filter(k => all.has(k)).map(k => [k, all.get(k)]))};
  }
  async function fdDecide(it, action, note) {
    if (!it.fp || action === 'note') { toast('Use the finding review controls to record a decision with evidence.', true); return; }
    if (action === 'keep' || action === 'reject') {
      state.fdForm = {item: it, key: it.key, action: 'reject', reason: note || ''}; drawSide(); return;
    }
    return fdSendDecision({...findingBinding([it]), ids: [it.key], action, from: it});
  }
  // after a decision: the next open item in the same group, then the next group of the same kind, then the next kind;
  // the toast says how many are left so every click visibly moves forward
  function fdAdvanceSelected(done, msg) {
    const scope = state.fdReviewScope;
    if (!scope) return false;
    if (scope.binding !== JSON.stringify(reviewBinding(state.report))) {
      state.fdReviewScope = null; state.fdCur = null; drawSide(); drawGrid();
      toast('The review changed. Reopen Needs review to choose a current scope.', true); return true;
    }
    const next = state.fdItems.find(it => scope.keys.includes(it.key) && it.key !== done.key && it.state === 'open');
    toast(msg + (next ? ' Continuing within your selected items.' : ' No further open findings in this selection; selected changes, deferred findings and other holds may remain.'));
    refreshHeadFd();
    if (next) fdOpen(next); else { state.fdCur = null; drawSide(); drawGrid(); }
    return true;
  }
  function fdAdvance(done) {
    state.fdItems = fdItems();
    if (fdAdvanceSelected(done, 'Review choice saved for preview.')) return;
    const groups = fdGroups(state.fdItems), g = groups.find(x => x.key === done.group);
    const order = [...groups.filter(x => x.tier === done.tier), ...groups.filter(x => x.tier !== done.tier)];
    const start = g ? order.indexOf(g) : 0, rotated = [...order.slice(start), ...order.slice(0, start)];
    let next = null;
    if (g) { const at = g.items.findIndex(x => x.key === done.key); next = [...g.items.slice(at + 1), ...g.items.slice(0, at)].find(x => x.state === 'open'); }
    if (!next) for (const x of rotated.slice(1)) { next = x.items.find(y => y.state === 'open'); if (next) break; }
    const leftInGroup = g ? g.items.filter(x => x.state === 'open').length : 0, leftInTier = state.fdItems.filter(x => x.tier === done.tier && x.state === 'open').length;
    const what = store.get(editsKey(), {})[editId(done.sheet, done.cell)] ? 'Added to the preview' : (state.report.review_decisions || {})[done.key]?.action === 'keep' ? 'Kept as recorded' : 'Suggestion rejected';
    toast(`${what}. ` + (leftInGroup ? `${leftInGroup} left in this group.` : leftInTier ? `This group is done; ${leftInTier} left in ${TIERS.find(t => t[0] === done.tier)[1].toLowerCase()}.` : `Nothing left in ${TIERS.find(t => t[0] === done.tier)[1].toLowerCase()}.`)
      + (done.tier === 'blocker' && done.evidence === 'none' ? ' The hold stays until the source question is settled.' : ''));
    fdRefreshFoot();
    if (next) { fdOpen(next); return; }
    state.fdCur = null; drawSide(); drawGrid();
  }
  // the reviewer's own value for a cell: goes into the preview like any other change, applied only with the others
  function fdOwnValueForm(it) {
    const at = splitCell(it.cell), grid = state.grid;
    it = {...it, editBefore: grid?.sheet === it.sheet && at ? grid.rows[at[1] - 1]?.[at[0]] ?? it.report ?? '' : it.report ?? ''};
    const input = h('input', {value: it.own ? it.own.after : '', placeholder: it.fix ? 'Or type a different value' : 'Type the correct value', 'aria-label': `New value for ${it.sheet}!${it.cell}`});
    return h('form', {class: 'fd-own', onsubmit: e => { e.preventDefault(); fdOwnValue(it, input.value); }},
      input, h('button', {type: 'submit', class: 'btn primary'}, 'Use this value'));
  }
  function fdOwnValue(it, value) {
    if (it.binding.file_hash !== state.report?.file_hash || it.binding.expected_check_hash !== state.report?.check_hash) { toast('The report changed. Review this cell again.', true); return; }
    const edits = store.get(editsKey(), {}), id = editId(it.sheet, it.cell), dec = store.get(decisionsKey(), {});
    if (value === null) { delete edits[id]; store.set(editsKey(), edits); toast('Your value was removed from the preview.'); fdRefresh(); return; }
    edits[id] = {sheet: it.sheet, cell: it.cell, before: it.editBefore ?? it.report ?? '', after: value, header: '', source: 'Your edit', reason: 'Typed by the reviewer' + (it.note ? ': ' + it.note : '')};
    if (it.fix) delete dec[it.fix.id];
    store.set(editsKey(), edits); store.set(decisionsKey(), dec);
    toast(`${it.sheet}!${it.cell} will be ${value === '' ? 'blank' : '“' + value + '”'} in the new version. Preview to apply.`);
    fdRefresh();
  }
  async function fdDecideAll(list, action) {
    toast('Open the finding group and use its scoped batch review form.', true);
  }
  // A source conflict (a comment says "1 broken clip" while OK, NOK, Condition and Recorded defect say nothing went
  // wrong) is never fixed one cell at a time: once the source owner has confirmed the outcome (the finding is marked
  // "Confirm it needs a correction"), the reviewer types the confirmed values for every affected field together and
  // they go into the preview as one change set. Nothing is filled in for them.
  const CONFLICT_FIELDS = [['OK', /^(qty\.?\s*)?ok$|^certified|^good$/i], ['NOK', /^(qty\.?\s*)?nok$|^rejected$|^reject(ed)? qty/i], ['Condition', /^condition$/i], ['Recorded defect', /^recorded defect|^defect$/i]];
  const fdConflict = it => !!it.issue && !it.fix && it.row && /conflict|broken|comment/i.test(`${it.issue.code || ''} ${it.text || ''}`) && it.issue.severity === 'high';
  function fdRowFields(sheet, row) {
    const g = state.grid; if (!g || g.sheet !== sheet) return null;
    for (let r = 0; r < Math.min(15, g.rows.length); r++) {
      const head = (g.rows[r] || []).map(v => String(v ?? '').trim());
      const found = CONFLICT_FIELDS.map(([label, re]) => { const c = head.findIndex(v => re.test(v)); return c >= 0 ? {label, cell: colName(c) + row, value: g.rows[row - 1]?.[c] ?? ''} : null; });
      if (found.filter(Boolean).length >= 2) return found.filter(Boolean);
    }
    return null;
  }
  function fdCoordinatedForm(it) {
    const fields = fdRowFields(it.sheet, it.row);
    if (it.state !== 'confirmed') return h('p', {class: 'fd-hint'}, icon('lock', 14), ' No single-cell fix is offered for a source conflict. Record the source owner\'s answer first (Await source owner…), then mark it "Confirm it needs a correction" to prepare one change set for every affected field.');
    if (!fields) return h('p', {class: 'fd-hint'}, 'Open the sheet this finding is on to prepare the change set.');
    const inputs = fields.map(f => h('input', {'aria-label': `${f.label} (${f.cell})`, placeholder: `${f.label}: now ${f.value === '' ? '(blank)' : f.value}`}));
    return h('form', {class: 'fd-decide', onsubmit: e => { e.preventDefault();
        if (it.binding.file_hash !== state.report?.file_hash || it.binding.expected_check_hash !== state.report?.check_hash) { toast('The report changed. Review the source conflict again.', true); return; }
        const edits = store.get(editsKey(), {}), changed = [];
        fields.forEach((f, n) => { const v = inputs[n].value.trim(); if (v === '') return; changed.push(f);
          edits[editId(it.sheet, f.cell)] = {sheet: it.sheet, cell: f.cell, before: f.value, after: v, header: f.label, source: 'Your edit', change_set: 'set-' + it.row + '-' + it.sheet,
            reason: `Confirmed by the source owner (${it.key}); one change set with ${fields.map(x => x.label).join(', ')}`}; });
        if (!changed.length) { toast('Type the confirmed values first.', true); return; }
        for (const f of changed) edits[editId(it.sheet, f.cell)].change_set_size = changed.length;   // applied together or not at all
        store.set(editsKey(), edits); toast(`${plural(changed.length, 'cell')} added to the preview as one change set. Nothing is applied until you preview it.`); fdRefresh(); }},
      h('p', {class: 'fd-decide-head'}, h('b', {}, 'Coordinated correction'), ` · ${it.sheet} row ${it.row}`),
      h('p', {class: 'fd-hint'}, 'Type the values the source owner confirmed. Leave a field empty to keep it as recorded.'),
      ...fields.map((f, n) => h('label', {class: 'fd-field'}, `${f.label} · ${f.cell} · now ${f.value === '' ? '(blank)' : f.value}`, inputs[n])),
      h('button', {type: 'submit', class: 'btn primary'}, 'Add the change set to the preview'));
  }
  // ---------- a reviewer's decision on findings: saved on the server first, shown only once it is saved
  const REASONS = {reject: ['Not a field in the captured source', 'Not recorded at the source; keep it blank (unknown, not zero)', 'Checked against the source: the value is correct'],
    defer: ['Comment and quantities disagree; the source has the same conflict', 'Needs the source owner to confirm the outcome']};
  function fdDecisionForm(it) {
    it = state.fdForm.item || it;
    const f = state.fdForm, defer = f.action === 'defer', high = it.issue?.severity === 'high';
    const reason = h('textarea', {rows: '2', required: true, 'aria-label': 'Reason', placeholder: defer ? 'What is being waited for, and why' : 'Why it stays as recorded'}, f.reason || '');
    const owner = defer ? h('input', {'aria-label': 'Who is asked', placeholder: 'Who is asked (e.g. the SharePoint list owner)', value: f.owner || ''}) : null;
    const evidence = h('input', {'aria-label': 'Evidence', placeholder: high || defer ? 'Evidence (e.g. SharePoint item 6190, Inspection Detail row 19)' : 'Evidence (optional)', value: f.evidence || ''});
    const send = () => {
      const why = reason.value.trim(), ev = evidence.value.split(';').map(x => x.trim()).filter(Boolean);
      if (!why) { reason.focus(); toast('Say why first. The reason is kept with the decision.', true); return; }
      if (!defer && high && !ev.length) { evidence.focus(); toast('A blocker can only be kept as recorded with evidence of what you checked.', true); return; }
      fdSendDecision({ids: [it.key], action: f.action, reason: why, owner: owner ? owner.value.trim() : '', evidence: ev, from: it});
    };
    return h('form', {class: 'fd-decide', onsubmit: e => { e.preventDefault(); send(); }},
      h('p', {class: 'fd-decide-head'}, h('b', {}, defer ? 'Await the source owner' : it.report === '' ? 'Keep this cell blank' : 'Keep the value as recorded'),
        ` · ${it.sheet || ''} ${it.cell || (it.row ? 'row ' + it.row : '')}`.trim()),
      defer ? h('p', {class: 'fd-hint'}, 'The finding stays open and the report stays on hold until the answer and its evidence are recorded. Nothing in the report changes.')
        : h('p', {class: 'fd-hint'}, 'Only this finding is decided. The cell is not changed' + (it.report === '' ? ': a blank stays blank (not recorded, not zero).' : '.')),
      h('div', {class: 'fd-chips'}, (REASONS[f.action] || []).map(t => h('button', {type: 'button', class: 'chip', onclick: () => { reason.value = t; }}, t))),
      reason, owner, evidence,
      h('div', {class: 'fd-cardacts'}, h('button', {type: 'submit', class: 'btn primary', disabled: state.decisionPending || state.rechecking || state.busy || null}, defer ? 'Save: awaiting source owner' : 'Save: keep as recorded'),
        h('button', {type: 'button', class: 'btn subtle', onclick: () => { state.fdForm = null; drawSide(); }}, 'Cancel')));
  }
  // one reason for a precisely listed batch: every finding in it is shown and can be unticked first
  function fdBatchForm(g) {
    const b = state.fdBatch, which = b.which || 'open', open = b.items.filter(x => x.state === which), picked = open.filter(x => !b.off.has(x.key));
    const reopen = b.action === 'reopen', agree = which === 'claude' && !reopen;
    const reason = h('textarea', {rows: '2', 'aria-label': 'Reason for these findings', placeholder: reopen ? 'Why they go back on the list (optional)' : 'Why these stay as recorded'}, b.reason || '');
    const cells = new Set(open.map(x => (x.cell || '').replace(/\d+/g, ''))), mixed = cells.size > 1 || new Set(open.map(x => x.issue?.code)).size > 1;
    const head = reopen ? `Put ${picked.length} of ${open.length} back for review` : agree ? `Agree with Dex for ${picked.length} of ${open.length}` : `Keep ${picked.length} of ${open.length} as recorded`;
    return h('form', {class: 'fd-decide fd-batch', onsubmit: e => { e.preventDefault();
        const why = reason.value.trim(); if (!why && !reopen) { reason.focus(); toast('Say why first. One reason is kept for exactly these findings.', true); return; }
        if (!picked.length) { toast('Tick at least one finding.', true); return; }
        if (!reopen && picked.some(x => x.issue?.severity === 'high')) { toast('Blockers are decided one at a time, with evidence.', true); return; }
        fdSendDecision({...findingBinding(picked), ids: picked.map(x => x.key), action: b.action || 'reject', reason: why, from: picked[0], batch: true}); }},
      h('p', {class: 'fd-decide-head'}, h('b', {}, head), mixed ? ' · this group mixes columns or kinds: check each one' : ''),
      h('p', {class: 'fd-hint'}, reopen ? 'Dex\'s recommendation stays on record with its reason; these findings go back on the open list for a person.'
        : agree ? 'This records your own decision (with your name and reason) on exactly the ticked findings. Dex\'s recommendation stays on record separately. Nothing in the report changes; blanks stay blank.'
        : 'Only the ticked findings are decided. Nothing in the report changes; blanks stay blank. Findings in other groups, even on the same rows, are not touched.'),
      g.items[0].issue?.legacy_records?.length ? h('p', {class: 'fd-hint'}, `On record from earlier (kept unchanged): “${g.items[0].issue.legacy_records[0].reason}” for ${g.items[0].issue.legacy_records[0].sheet} row ${g.items[0].issue.legacy_records[0].row}, with no cell named.`) : null,
      h('ul', {class: 'fd-scope'}, open.map(x => h('li', {}, h('label', {}, h('input', {type: 'checkbox', checked: !b.off.has(x.key) || null,
        onchange: e => { e.target.checked ? b.off.delete(x.key) : b.off.add(x.key); b.reason = reason.value; drawSide(); }}), ' ', `${x.sheet || ''} ${x.cell || (x.row ? 'row ' + x.row : 'whole report')}`.trim())))),
      reopen ? null : h('div', {class: 'fd-chips'}, REASONS.reject.map(t => h('button', {type: 'button', class: 'chip', onclick: () => { reason.value = t; }}, t))), reason,
      h('div', {class: 'fd-cardacts'}, h('button', {type: 'submit', class: 'btn primary', disabled: state.decisionPending || state.rechecking || state.busy || null}, `Save for ${picked.length}`),
        h('button', {type: 'button', class: 'btn subtle', onclick: () => { state.fdBatch = null; drawSide(); }}, 'Cancel')));
  }
  // Pending -> saved / failed. A lost answer is reconciled (was it saved?) before anything is retried, and the same
  // request ID is reused, so a retry can never record a decision twice.
  async function fdSendDecision(req) {
    if (versionReadOnly()) return;
    if (state.gridDraft && gridDirty()) { toast('Preview or discard the spreadsheet draft before deciding findings.', true); return; }
    if (state.decisionPending || state.busy || state.rechecking || state.aiProposalOperation || !state.report) return;
    const binding = req.file_hash ? req : req.from ? findingBinding([req.from]) : null;
    if (!binding) { toast('Reload this review before deciding.', true); return; }
    const hash = binding.file_hash;
    if (!req.file_hash) Object.assign(req, binding);
    req.request_id ||= 'q-' + (crypto.randomUUID ? crypto.randomUUID() : Date.now().toString(36) + Math.random().toString(36).slice(2));
    const body = {file_hash: hash, ids: req.ids || [], action: req.action, reason: req.reason || '', owner: req.owner || '', evidence: req.evidence || [],
      request_id: req.request_id, expected_rev: req.expected_rev, expected_run: req.expected_run, expected_fps: req.expected_fps, undo_of: req.undo_of || null, by: myName()};
    state.decisionPending = true; state.decisionFailed = null; drawSide();
    let response = null, data = {};
    try {
      response = await fetch('/api/spreadsheets/decide', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Review-Token': 'same-origin'}, body: JSON.stringify(body)});
      try { data = await response.json(); } catch (e) { data = {}; }
    } catch (e) { response = null; }
    if (response && response.status === 401 && data.signin) { state.decisionPending = false; showSignIn(); return; }
    if (!response || response.status >= 500 || (response.ok && !data.report)) {   // no clear answer: ask whether it was saved
      let known = null;
      try { known = await api('/api/spreadsheets/decision-status', {file_hash: hash, request_id: req.request_id}); } catch (e) { known = null; }
      state.decisionPending = false;
      if (known && known.saved) { toast('Your decision was saved (the first answer was lost on the way).'); state.fdForm = null; state.fdBatch = null; await openReport(hash, true); return; }
      state.decisionFailed = {message: known ? 'Not saved: the server did not answer. Nothing was changed.' : 'Could not reach the server, so it is not known yet whether this was saved. Try again: it will not be recorded twice.', retry: req};
      drawSide(); return;
    }
    state.decisionPending = false;
    if (!response.ok) {   // refused (409 stale / unknown findings, 400 missing reason, 403 not allowed): nothing was saved
      state.decisionFailed = {message: 'Not saved: ' + (data.error || `the server answered ${response.status}.`), retry: null};
      if (response.status === 409) { if (!req.quiet) toast('Not saved. The report changed; showing its current state.', true); await openReport(hash, true); state.decisionFailed = {message: 'Not saved: ' + (data.error || 'the report changed.'), retry: null}; drawSide(); }
      else drawSide();
      return;
    }
    if (state.selected !== hash) return;
    await fillPages(data.report);
    const scoped = state.fdReviewScope?.binding === JSON.stringify(reviewBinding(state.report)) ? state.fdReviewScope : null;
    state.report = {...state.report, ...data.report};    // the one state for header, panel, sheet badges and history
    if (scoped && state.report.file_hash === hash && JSON.stringify(state.report.review?.run || null) === JSON.stringify(req.expected_run)) {
      const decidedKeys = new Set(data.entry?.ids || []);
      state.fdReviewScope = {binding: JSON.stringify(reviewBinding(state.report)), keys: scoped.keys.filter(key => !decidedKeys.has(key))};
    }
    state.fdForm = null; state.fdBatch = null;
    const e = data.entry || {}, n = (e.ids || []).length;
    const words = {reject: 'kept as recorded', defer: 'waiting for the source owner', confirm: 'confirmed as needing a correction', reopen: 'back on the list for review', undo: 'put back as it was'}[e.action] || 'saved';
    if (req.from) fdAdvanceAfter(req.from, `${n === 1 ? '1 finding' : n + ' findings'} ${words}. Saved.`);
    else { drawReport(); if (!req.quiet) toast(`Saved: ${words}.`); }
  }
  function fdAdvanceAfter(done, msg) {
    state.fdItems = fdItems();
    if (fdAdvanceSelected(done, msg)) return;
    const groups = fdGroups(state.fdItems), g = groups.find(x => x.key === done.group);
    const order = [...groups.filter(x => x.tier === done.tier), ...groups.filter(x => x.tier !== done.tier)];
    const start = g ? order.indexOf(g) : 0, rotated = [...order.slice(Math.max(start, 0)), ...order.slice(0, Math.max(start, 0))];
    let next = g ? g.items.find(x => x.state === 'open') : null;
    if (!next) for (const x of rotated) { next = x.items.find(y => y.state === 'open'); if (next) break; }
    const left = state.fdItems.filter(x => x.state === 'open').length;
    toast(`${msg} ${left ? `${left} still need a decision.` : 'Nothing left to decide.'}`);
    refreshHeadFd();
    if (next) { fdOpen(next); return; }
    state.fdCur = null; drawSide(); drawGrid();
  }
  // the header (status, metrics) redrawn from the same report state as the panel
  function refreshHeadFd() {
    fdRefreshFoot();
    const mode = document.querySelector('.fd-mode-selector');
    if (mode && state.report) mode.replaceWith(fdModeSelector(state.report));
    const m = document.querySelector('.fd-metrics'); if (m && state.report) m.replaceWith(fdMetrics(state.report));
  }
  function fdRefreshFoot() {
    const sel = readyChanges().total, b = $('fd-preview-btn'), n = $('fd-selcount');
    if (b) { b.disabled = !sel || state.busy; b.classList.toggle('primary', !!sel); b.onclick = correctionAction(fdPreview); }
    if (n) n.textContent = sel ? plural(sel, 'change') + ' selected' : 'No changes selected';
    if (state.report) { const st = fdStatus(state.report);
      document.querySelectorAll('.fd-status').forEach(pill => { const global = pill.classList.contains('fd-global-status'); const shown = global ? fdShown(st) : st; pill.className = 'fd-status ' + (global ? 'fd-global-status ' : 'fd-panel-status ') + shown.tone; pill.replaceChildren(icon(shown.icon, 16), h('span', {}, global ? shown.text : fdPanelStatus(state.report, st))); }); }
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
  function fdRemovePreview(c, row, dialog) {
    if (state.busy) return;
    if (state.report?.file_hash !== c.binding.file_hash || state.report?.check_hash !== c.binding.expected_check_hash) {
      dialog.close(); toast('The report changed. Reopen its correction list before editing the preview.', true); return;
    }
    const sameCell = x => x.sheet === row.sheet && String(x.cell).toUpperCase() === String(row.cell).toUpperCase();
    if (!c.recommendedOnly && !c.independentScope) fdSelect([{sheet: row.sheet, cell: row.cell, binding: c.binding}], false);
    c.onRemove?.(row);
    const accepted = c.accepted.filter(x => !sameCell(x));
    const edits = Object.fromEntries(Object.entries(c.edits).filter(([, x]) => !sameCell(x)));
    const next = captureCorrections(accepted, c.dec, edits, c.binding, c.report);
    dialog.close(); dialog.remove();
    if (next.total) fdPreview({...next, recommendedOnly: c.recommendedOnly, independentScope: c.independentScope, onApplied: c.onApplied, onRemove: c.onRemove, returnFocus: c.returnFocus});
  }
  function fdPreview(c) {
    const existing = $('fd-preview-dialog');
    if (existing?.open) { existing.focus(); return; }
    if (existing) existing.remove();
    if (!c?.binding || !c.total) return;
    const r = c.report, binding = c.binding, next = revisionOf(r) + 1, opener = c.returnFocus || document.activeElement;
    c = {...c, returnFocus: opener};
    const rows = [...c.accepted.map(f => ({sheet: f.sheet, cell: f.cell, before: f.before, after: c.dec[f.id]?.after ?? f.after, why: f.reason, src: f.source})),
      ...Object.values(c.edits).map(e => ({sheet: e.sheet, cell: e.cell, before: e.before, after: e.after, why: e.reason || 'Your edit', src: e.source === 'Claude' ? 'Dex (chat)' : 'Your edit'}))];
    let saving = false;
    const errorMessage = h('p', {id: 'fd-preview-error', role: 'alert', tabindex: '-1'});
    const d = h('dialog', {id: 'fd-preview-dialog', class: 'fd-modal', 'aria-labelledby': 'fd-prev-title'},
      h('button', {type: 'button', class: 'fd-dialog-x', 'aria-label': 'Close', title: 'Close', onclick: () => { if (!saving && !state.busy) d.close(); }}, icon('x', 18)),
      h('p', {class: 'fd-eyebrow'}, 'Check before applying'),
      h('h2', {id: 'fd-prev-title'}, plural(rows.length, 'change')),
      h('p', {}, `These cells change in a new version (v${next}). v${revisionOf(r)} stays exactly as it is, and the checks run again on v${next}. Source comparison runs when available.`),
      h('p', {class: 'fd-hint'}, 'Approval is not granted by applying changes. Unresolved findings and holds remain; unavailable source comparisons remain unverified.'),
      errorMessage,
      h('div', {class: 'fd-prevlist', tabindex: '0', 'aria-label': 'Exact cell changes before applying'}, h('table', {}, h('thead', {}, h('tr', {}, h('th', {}, 'Cell'), h('th', {}, 'Before'), h('th', {}, 'After'), h('th', {}, 'Why'), h('th', {}, 'Include'))),
        h('tbody', {}, rows.map(x => h('tr', {}, h('td', {class: 'fd-preview-location'}, h('span', {class: 'fd-preview-sheet'}, x.sheet), h('b', {class: 'fd-preview-cell'}, x.cell)), h('td', {}, showWs(x.before)), h('td', {}, h('b', {}, showWs(x.after))), h('td', {}, `${x.src}: ${x.why || ''}`), h('td', {}, h('button', {type: 'button', class: 'btn small fd-preview-remove', 'aria-label': `Remove ${x.sheet}, cell ${x.cell} from preview`, onclick: () => { if (!saving && !state.busy) fdRemovePreview(c, x, d); }}, 'Remove'))))))),
      h('div', {class: 'fd-modalacts'},
        h('button', {type: 'button', class: 'btn', onclick: () => { if (!saving && !state.busy) d.close(); }}, 'Back to review'),
        h('button', {type: 'button', class: 'btn primary', id: 'fd-apply', onclick: async e => {
          if (saving || state.busy) return;
          const applyButton = e.currentTarget; // DOM Event.currentTarget is null after awaiting.
          setSaving(true); errorMessage.textContent = ''; applyButton.textContent = `Saving v${next} and re-checking…`;
          let hash = null, finished = false;
          const finishApplied = () => { if (!finished) { finished = true; d.close(); d.remove(); c.onApplied?.(); } };
          try { hash = await applyFixes(c.accepted, c.dec, c.edits, {download: false, binding, recommendedOnly: c.recommendedOnly, independentScope: c.independentScope, gridDraft: c.gridDraft, beforeOpen: finishApplied}); }
          catch (error) { toast('The save could not be confirmed. Reload the report before retrying.', true); }
          finally { setSaving(false); }
          if (hash) finishApplied();
          else { applyButton.textContent = `Apply ${plural(rows.length, 'change')} and re-check`; errorMessage.textContent = 'Not confirmed saved. Review the error and reload the report before trying again; this preview remains available.'; errorMessage.focus(); }
        }}, `Apply ${plural(rows.length, 'change')} and re-check`)));
    const setSaving = pending => { saving = pending; for (const button of d.querySelectorAll('button')) button.disabled = pending; };
    d.addEventListener('cancel', e => { if (saving || state.busy) e.preventDefault(); });
    d.addEventListener('close', () => { d.remove(); if (opener?.isConnected) opener.focus(); else $('fd-preview-btn')?.focus(); });
    document.body.append(d); d.showModal(); d.querySelector('.fd-modalacts button')?.focus();
  }
  async function fdApprove(binding) {
    const name = ($('fd-approver')?.value || '').trim(), btn = $('fd-approve-btn');
    if (!name) { toast('Type your name to approve the report.', true); $('fd-approver')?.focus(); return; }
    if (btn) { btn.disabled = true; btn.textContent = 'Approving…'; }
    try {
      store.set('rd-approver', name);
      const out = await api('/api/spreadsheets/approve', {...binding, approver: name, note: ''});
      toast('Internally approved. Nothing was sent; sending is paused.');
      await openReport(out.item.file_hash, true); loadList();
    } catch (e) { toast(e.message, true); if (btn) { btn.disabled = false; btn.textContent = 'Approve internally'; } }
  }
  async function fdRecheck() {
    if (state.gridDraft && gridDirty()) { toast('Preview or discard the spreadsheet draft before re-checking. Nothing changed.', true); return; }
    if (state.decisionPending || state.busy || state.rechecking || state.aiProposalOperation) { toast('Wait for the current save or operation to finish before re-checking.', true); return; }
    const report = state.report, hash = report?.file_hash; if (!hash) return;
    if ([...document.querySelectorAll('#side input:not([type="checkbox"]):not([type="radio"]), #side textarea')].some(node => node.value !== node.defaultValue)) {
      toast('Finish or save the text you are editing before re-checking. Nothing changed.', true); return;
    }
    const controls = new Map();
    const operation = {lock: () => {
      document.querySelectorAll('#side button, #side input, #side textarea, #side select, #fd-preview-btn, .fd-mode-selector button, dialog .primary').forEach(node => {
        if (!controls.has(node)) controls.set(node, node.disabled);
        node.disabled = true;
      });
    }}; state.rechecking = operation; operation.lock();
    const name = myName() || 'Reviewer';
    toast('Re-checking with the current checks…');
    try { const out = await api('/api/spreadsheets/recheck', {file_hash: hash, apply: true, by: name});
      if (state.report !== report || state.selected !== hash) return;
      toast(out.reconciled ? 'Checked again, and SharePoint compared again.' : 'Checked again. Compare with SharePoint to verify it.');
      await openReport(hash, true); loadList();
    } catch (e) { if (state.report === report) toast(e.message, true); }
    finally {
      if (state.rechecking !== operation) return;
      state.fdLastBackground = Date.now();
      state.rechecking = false;
      if (!state.busy && !state.decisionPending) {
        controls.forEach((disabled, node) => { if (node.isConnected) node.disabled = disabled; });
        document.querySelectorAll('[data-recheck-disabled]').forEach(node => { node.disabled = false; node.removeAttribute('data-recheck-disabled'); });
      }
      if (state.report && state.selected === state.report.file_hash) document.querySelector('.fd-mode-selector')?.replaceWith(fdModeSelector(state.report));
    }
  }
  // Every report is compared with SharePoint when it is opened: once per version, without a click. Reading only.
  function fdAutoReconcile(r) {
    if (!r || versionReadOnly(r)) return false;
    const rec = r.source_reconciliation || {}, bound = rec.bound_to || {}, sp = r.review?.sharepoint || {};
    const fresh = !rec.stale && bound.file_hash === r.file_hash && !!bound.checker_version && bound.checker_version === r.checker_version && !!rec.snapshot_id && sp.status && sp.status !== 'stale';
    state.fdAutoSP ||= {};
    const key = r.file_hash + ':' + r.check_hash;
    // A saved comparison that was waiting for a column confirmation, or was made for an earlier client assignment, is
    // read again once: the columns are now settled without asking.
    const redo = Object.values(rec.column_mapping || {}).some(m => m?.status === 'needs_confirmation') || (r.holds || []).some(x => x.code === 'source_assignment_changed');
    // Never in a loop: one automatic SharePoint check per version in a visit, whatever the result. After that the page stays still; the refresh button remains.
    state.fdAutoSPRuns ||= {}; state.fdAutoSPRedone ||= {};
    const runs = state.fdAutoSPRuns[r.file_hash] || 0;
    if ((fresh && (!redo || state.fdAutoSPRedone[r.file_hash])) || runs >= 1 || state.fdAutoSP[key] || (r.assignment || {}).needs_person) return false;
    if (state.fdAutoSPWaiting) return true;
    state.fdAutoSPWaiting = true; let tries = 0;
    const attempt = async () => {
      if (state.report !== r || versionReadOnly(r) || state.fdAutoSP[key]) { state.fdAutoSPWaiting = false; return; }
      if (state.fdAIRun?.running || state.fdAfterAIPending || state.fdTidying || state.busy || state.rechecking || state.decisionPending || state.aiProposalOperation || state.fdAutoRecheckWaiting || state.fdReconciling || !state.fixes) { if (++tries < 240) setTimeout(attempt, 500); else state.fdAutoSPWaiting = false; return; }
      state.fdAutoSP[key] = true; state.fdAutoSPRuns[r.file_hash] = (state.fdAutoSPRuns[r.file_hash] || 0) + 1; if (redo) state.fdAutoSPRedone[r.file_hash] = true;
      state.fdReconciling = true; drawSide();
      try { await api('/api/spreadsheets/sharepoint', {file_hash: r.file_hash}); state.fdLastBackground = Date.now(); state.fdReconciling = false; state.fdAutoSPWaiting = false;
        if (state.report === r) { await openReport(r.file_hash, true); loadList(); } }
      catch (e) { state.fdReconciling = false; state.fdAutoSPWaiting = false; state.fdAutoSPError = {key, message: e.message}; if (state.report === r) drawSide(); }
    };
    setTimeout(attempt, 0); return true;
  }
  function fdSourceRefreshReason(r) {
    if (!r || versionReadOnly(r)) return 'Open the current working copy to refresh SharePoint.';
    const a = r.assignment || {};
    if (a.needs_person || !['manual', 'verified', 'matched', 'source'].includes(a.status) || !(a.site_id || a.site) || !a.list_id) return 'Choose the client and job with an exact SharePoint site and list before refreshing.';
    if (state.health?.sharepoint_configured === false) return 'SharePoint is not connected. Ask an administrator to connect the assigned source, then reopen this report.';
    return '';
  }
  function fdSourceRefresh(r) {
    const binding = JSON.stringify(reviewBinding(r)), assignment = JSON.stringify(r.assignment), reason = fdSourceRefreshReason(r);
    const pending = !versionReadOnly(r) && (!!state.fdReconciling || !!state.fdAutoSPWaiting);
    return h('div', {class: 'fd-source-refresh'},
      h('button', {type: 'button', class: 'btn fd-source-refresh-button', disabled: !!reason || pending || !!state.rechecking || !!state.busy || !!state.decisionPending || !!state.aiProposalOperation,
        onclick: () => fdReconcile(r, binding, assignment)}, pending && state.fdAutoSPWaiting && !state.fdReconciling ? 'Waiting for SharePoint…' : pending ? 'Refreshing SharePoint…' : 'Refresh SharePoint only'),
      h('p', {class: 'fd-hint'}, reason || 'Read the assigned SharePoint source again for this exact report. Report rows, original files and saved versions stay unchanged. No workbook re-check, AI, approval or sending.'));
  }
  async function fdReconcile(report, binding, assignment) {
    const hash = report?.file_hash;
    const current = () => !!hash && state.report === report && state.selected === hash &&
      JSON.stringify(reviewBinding(report)) === binding && JSON.stringify(report.assignment) === assignment && !versionReadOnly(report);
    if (!current()) { toast('The report changed. Reopen its current comparison before refreshing SharePoint.', true); return; }
    const reason = fdSourceRefreshReason(report);
    if (reason) { toast(reason, true); return; }
    if (state.fdReconciling || state.fdAutoSPWaiting || state.busy || state.rechecking || state.decisionPending || state.aiProposalOperation) return;
    if (state.gridDraft && gridDirty() || [...document.querySelectorAll('#side input:not([type="checkbox"]):not([type="radio"]), #side textarea, .fd-review-dialog input:not([type="checkbox"]), .fd-review-dialog textarea')].some(node => node.value !== node.defaultValue)) {
      toast('Save or discard your draft before refreshing SharePoint. Your text and report rows are unchanged.', true); return;
    }
    const controls = new Map(), key = hash + ':' + report.check_hash;
    const operation = {lock: () => {
      if (!current()) return;
      document.querySelectorAll('#side button, #side input, #side textarea, #side select, #fd-preview-btn, .fd-mode-selector button, .fd-review-dialog button, .fd-review-dialog input, .fd-review-dialog textarea, dialog .primary').forEach(node => {
        if (!controls.has(node)) controls.set(node, node.disabled);
        node.disabled = true;
      });
    }};
    state.fdReconciling = operation; state.rechecking = operation;
    state.fdAutoSP ||= {}; state.fdAutoSP[key] = true;
    // This explicit source-only action must not launch a queued or newly rendered workbook re-check.
    state.fdAutoRecheck ||= {}; state.fdAutoRecheck[hash] = true;
    state.fdAutoSPError = null;
    let posted = false, failure = null;
    try {
      operation.lock(); drawSide();
      const version = await api('/api/spreadsheets/working-copy?id=' + hash);
      if (!current()) return;
      if (version?.requested_hash !== hash || version.status !== 'current' || version.working_hash && version.working_hash !== hash) throw Error('This is no longer the current working copy. Reopen the report before refreshing.');
      posted = true;
      try {
        const answer = await api('/api/spreadsheets/sharepoint', {file_hash: hash});
        if (answer?.item?.file_hash !== hash || answer.pending || answer.status === 'pending') failure = Error('Source completion was not confirmed.');
      }
      catch (error) { failure = error; }
      if (!current()) return;
      // A transport answer is not a receipt. Read the exact stored report once, never repeat the POST.
      const fresh = await fillPages(await api('/api/spreadsheet?id=' + hash));
      if (!current()) return;
      if (fresh?.file_hash !== hash || JSON.stringify(fresh.assignment) !== assignment) throw Error('The source receipt no longer belongs to this report assignment. Reopen the report.');
      if (failure) {
        state.fdAutoSPError = {key: hash + ':' + fresh.check_hash, message: 'Refresh outcome is unconfirmed. The recorded source receipt was read; no retry was sent. Reopen the report before deciding whether to refresh again.'};
      }
      fresh.version_view = report.version_view;
      state.fdAutoSP[hash + ':' + fresh.check_hash] = true;
      state.report = fresh; state.fixes = null; state.fdSummaryCache = null;
      drawReport(); loadFixes(hash); loadList();
      toast(failure ? state.fdAutoSPError.message : 'Recorded SharePoint receipt loaded. See the comparison for matches, differences and remaining verification gaps.', !!failure);
    } catch (error) {
      if (current()) {
        state.fdAutoSPError = {key, message: (posted ? 'Refresh outcome is unconfirmed. No retry was sent. Reopen the report to read its source receipt. ' : '') + error.message};
        toast(state.fdAutoSPError.message, true);
      }
    } finally {
      state.fdLastBackground = Date.now();
      if (state.fdReconciling === operation) state.fdReconciling = null;
      if (state.rechecking === operation) state.rechecking = false;
      if (!state.busy && !state.decisionPending && !state.aiProposalOperation && !state.rechecking) controls.forEach((disabled, node) => { if (node.isConnected) node.disabled = disabled; });
      if (state.selected === hash && state.report?.file_hash === hash) drawSide();
    }
  }

  // ---------- history and sources
  function fdHistory(r) {
    const binding = reviewBinding(r);
    const rv = r.review, rows = [];
    const add = (title, text, link) => rows.push(h('article', {class: 'fd-hist'}, h('b', {}, title), h('span', {}, text), link || null));
    const inconsistent = r.version_view?.unavailable_reason === 'history_inconsistent';
    if (inconsistent) add('Saved history could not be verified', fdReadOnlyReason(r) + ' The links below are recorded references, not verified correction ancestry.');
    if (r.history_recovery) {
      const receipt = r.history_recovery;
      if (r.version_view?.history_recovery_verified !== true) add('Recorded recovery claim, unverified', 'The saved recovery receipt could not be verified against complete history. It does not establish recovery or release readiness.');
      else add('Audited history recovery', `An isolated self-link was excluded from the working-copy graph ${when2(receipt.at) || ''} by ${receipt.by?.name || receipt.by?.email || 'an admin'}. Original bytes and ${receipt.prefix_count} prior check records remain unchanged. This is not verification or approval.`);
      const claims = receipt.ignored_self_link?.fixed_from?.changes || [];
      if (claims.length) rows.push(h('details', {}, h('summary', {}, 'Earlier self-link claims, retained as unverified history'),
        ...fdHistoryGroups({changed: claims}).map(g => fdHistoryGroup(g, {unverified: true, notice: 'An earlier self-link claimed these changes. The recovery does not prove they were applied.'}))));
    }
    if (r.fixed_from) add(`v${r.fixed_from.revision || 1} · ${r.fixed_from.filename}`, inconsistent ? 'Unverified recorded parent reference.' : 'The version these changes were made on. Kept unchanged.', h('a', {href: '#r=' + r.fixed_from.file_hash + '&history=1'}, `Open v${r.fixed_from.revision || 1}`));
    add(`v${revisionOf(r)} · this version`, r.fixed_from ? `${plural((r.fixed_from.changes || []).length, 'change')} ${inconsistent ? 'recorded in unverified history' : 'applied'}${r.fixed_from.applied_at ? ' ' + when2(r.fixed_from.applied_at) : ''}: ` + (r.fixed_from.changes || []).slice(0, 8).map(c => `${c.sheet}!${c.cell} ${c.before || '(blank)'} → ${c.after || '(blank)'}`).join('; ') + ((r.fixed_from.changes || []).length > 8 ? '…' : '') : `Uploaded ${when2(r.received_at) || ''}. Stored unchanged (SHA-256 ${r.file_hash.slice(0, 12)}…).`);
    if ((r.fixed_from?.changes || []).length) rows.push(h('details', {class: 'fd-saved-history'},
      h('summary', {}, `All ${plural(r.fixed_from.changes.length, 'saved cell change')}`),
      ...fdHistoryGroups({changed: r.fixed_from.changes}).map(g => fdHistoryGroup(g, inconsistent ? {unverified: true, notice: 'Recorded changes only; correction ancestry is unverified.'} : {}))));
    for (const f of r.fixes_applied || []) add(`v${f.revision || '?'} · ${f.fixed_filename}`, `${plural((f.changes || []).length, 'change')} ${inconsistent ? 'recorded in unverified history' : 'applied'} ${when2(f.applied_at) || ''}.`, h('a', {href: '#r=' + f.fixed_file_hash + '&history=1'}, 'Open it'));
    for (const x of r.recheck_applied || []) add('Re-checked', `${x.by || 'Someone'} re-ran the checks ${when2(x.at) || ''} (from ${x.from_version || '?'} to ${x.to_version || '?'}).`);
    for (const x of (r.review_decision_log || []).slice(-30)) add({keep: 'Kept as recorded', reject: 'Suggestion rejected', note: 'Note', reopen: 'Decision undone'}[x.action] || x.action, `${x.by || 'A reviewer'} ${when2(x.at) || ''}${x.note ? ': “' + x.note + '”' : ''}`);
    // every decision on findings, with its exact scope; any one can be undone (the undo is logged too)
    const flog = r.finding_decision_log || [], undone = new Set(flog.filter(x => x.undo_of).map(x => x.undo_of));
    if (flog.length) rows.push(h('h3', {class: 'fd-histhead'}, `Decisions on findings (${flog.length})`));
    for (const x of [...flog].reverse().slice(0, 200)) {
      const cells = Object.values(x.cells || {}), word = {reject: 'Kept as recorded', defer: 'Waiting for the source owner', confirm: 'Confirmed: needs a correction', reopen: 'Put back for review', undo: 'Undone'}[x.action] || x.action;
      add(`${word} · ${plural((x.ids || []).length, 'finding')}`,
        `${x.by || 'A reviewer'} ${when2(x.at) || ''}${x.owner ? ' · asked ' + x.owner : ''}${x.reason ? ' · “' + x.reason + '”' : ''}${(x.evidence || []).length ? ' · evidence: ' + x.evidence.join('; ') : ''}`
          + ` · ${cells.slice(0, 30).join(', ')}${cells.length > 30 ? ` and ${cells.length - 30} more` : ''}${x.undo_of ? ' (undid an earlier decision)' : ''}`,
        x.action !== 'undo' && !undone.has(x.decision_id) && !versionReadOnly(r) ? h('button', {type: 'button', class: 'btn small subtle', disabled: state.decisionPending || state.rechecking || state.busy || null, 'data-recheck-disabled': state.rechecking && !state.decisionPending && !state.busy ? true : null,
          onclick: () => fdSendDecision({...binding, action: 'undo', undo_of: x.decision_id, reason: 'Undone from the history'})}, 'Undo') : undone.has(x.decision_id) ? h('span', {class: 'muted'}, 'undone') : null);
    }
    if (r.approval) add(rv.approval_current ? 'Internally approved' : 'Approved on an earlier check run', `${r.approval.by} ${when2(r.approval.at) || ''}${r.approval.note ? ': ' + r.approval.note : ''}.${(r.sent || []).some(x => x.sent) ? ' Delivery recorded below.' : r.sending ? ' Sending is recorded as in progress.' : ' Not sent.'}`);
    for (const s of (r.sent || []).filter(x => x.sent)) add('Sent to client', `${when2(s.at) || ''} to ${(s.to || []).join(', ')}`);
    return h('section', {class: 'fd-utility'}, h('h2', {}, 'Version history'), h('p', {}, 'Every version has its own file, its own checks and its own provenance. Originals are never changed.'), rows);
  }
  function fdSourceRecovery(r) {
    const rec = r.source_reconciliation || {};
    const candidates = Object.entries(rec.column_mapping || {}).filter(([, m]) => ['needs_confirmation', 'conflict'].includes(m.status));
    const changed = (r.holds || []).some(hold => hold.code === 'source_mapping_changed');
    if (!candidates.length && !changed) return null;
    if (versionReadOnly(r)) return h('section', {class: 'fd-source-recovery'}, h('h3', {}, 'Source column review'),
      h('p', {}, 'Source mapping is read-only for this saved version. Existing holds remain.'),
      h('ul', {}, candidates.map(([field, m]) => h('li', {}, `${field} → ${m.column || 'No column selected'} (${m.status.replaceAll('_', ' ')})`))));
    const binding = reviewBinding(r), snapshot = rec.snapshot_id, isAdmin = state.me?.user?.role === 'admin';
    const request = Object.freeze({file_hash: binding.file_hash, expected_check_hash: binding.expected_check_hash, expected_snapshot_id: snapshot, expected_mapping_revision: rec.mapping_binding?.revision ?? null, site_id: rec.site_id, list_id: rec.list_id || rec.list?.id,
      mapping: Object.freeze(Object.fromEntries(candidates.filter(([, m]) => typeof m.column === 'string' && m.column.trim()).map(([field, m]) => [field, m.column])))});
    const complete = !!snapshot && !!binding.expected_check_hash && !!request.site_id && !!request.list_id && candidates.length > 0 && Object.keys(request.mapping).length === candidates.length;
    const current = () => state.report === r && !versionReadOnly(r) && state.report?.file_hash === binding.file_hash && state.report?.check_hash === binding.expected_check_hash &&
      state.report?.source_reconciliation?.snapshot_id === snapshot && state.report?.source_reconciliation?.site_id === request.site_id &&
      (state.report?.source_reconciliation?.list_id || state.report?.source_reconciliation?.list?.id) === request.list_id &&
      (state.report?.source_reconciliation?.mapping_binding?.revision ?? null) === request.expected_mapping_revision;
    const message = h('p', {class: 'fd-hint', role: 'status'}, 'Saving columns changes only this exact source mapping. It does not modify the workbook, verify quantities or release holds. Compare again afterward.');
    let pending = false;
    const compare = h('button', {type: 'button', class: 'btn small', hidden: !changed, onclick: async () => {
      if (pending) return;
      if (!current()) { message.textContent = 'The displayed report or source changed. Reopen Comparison details before comparing.'; return; }
      pending = true; compare.disabled = true;
      try {
        await api('/api/spreadsheets/source-reconcile', {file_hash: binding.file_hash});
        if (current()) await openReport(binding.file_hash, true);
      } catch (error) { message.textContent = error.message; }
      finally { pending = false; compare.disabled = false; }
    }}, 'Compare again');
    const confirm = h('button', {type: 'button', class: 'btn small', disabled: !complete || !isAdmin, onclick: async () => {
      if (pending || !complete || state.me?.user?.role !== 'admin') return;
      if (!current()) { message.textContent = 'The displayed report or source changed. Reopen Comparison details before confirming columns.'; return; }
      pending = true; confirm.disabled = true;
      try {
        await api('/api/sharepoint/confirm-columns', request);
        message.textContent = 'Source columns saved. The workbook is unchanged and existing holds still apply. Compare again to capture fresh evidence.';
        compare.hidden = false;
      } catch (error) { message.textContent = error.message; confirm.disabled = false; }
      finally { pending = false; }
    }}, 'Confirm source columns');
    return h('section', {class: 'fd-source-recovery', 'aria-label': 'Source column review'},
      h('h3', {}, 'Source column review'), h('p', {}, `Exact site ID: ${request.site_id || 'Missing'} · List ID: ${request.list_id || 'Missing'}`),
      candidates.length ? h('ul', {}, candidates.map(([field, m]) => h('li', {}, `${field} → ${m.column || 'No column selected'} (${m.status.replace('_', ' ')})`))) : null,
      candidates.length && !complete ? h('p', {class: 'fd-coverage'}, 'Confirmation unavailable: exact source identity, snapshot or a selected column is missing. Capture the exact source before continuing.') : null,
      candidates.length && !isAdmin ? h('p', {class: 'fd-coverage'}, 'Ask an admin to confirm these source columns. Your account can review the evidence but cannot change the source mapping.') : null,
      message, candidates.length ? confirm : null, compare);
  }
  // One difference between the report and SharePoint, in plain words.
  function fdPresentationRow(row) {
    return row.status === 'not_on_sharepoint_for_day' && row.note === 'On SharePoint the item is dated nothing (timezone unconfirmed).'
      ? {...row, status:'source_day_unconfirmed', note:'This item exists in SharePoint, but its business day was unconfirmed in the recorded capture. Recheck SharePoint; keep the report row unchanged.'} : row;
  }
  function fdDifferenceText(row) {
    row = fdPresentationRow(row);
    const v = x => x == null ? 'unknown' : x === '' ? 'blank' : String(x);
    const where = [row.sheet, row.row ? 'row ' + row.row : null].filter(Boolean).join(' ');
    const item = row.item != null && row.item !== '' ? `SharePoint item ${row.item}` : null;
    const field = row.field ? String(row.field).replaceAll('_', ' ') : 'value';
    const at = where || item || 'Report';
    const said = {
      value_differs: `${at}: ${field} is ${v(row.report)} in the report but ${v(row.sharepoint)} in SharePoint${item && where ? ` (${item})` : ''}.`,
      export_value_differs: `${at}: ${field} is ${v(row.report)} in the report but ${v(row.sharepoint)} in SharePoint${item && where ? ` (${item})` : ''}.`,
      changed_since_export: `${item || at}: ${field} was changed in SharePoint after this report was made (now ${v(row.sharepoint)}).`,
      missing_from_report: `${item || 'A SharePoint item'} is in SharePoint for this day but missing from the report.`,
      source_item_unconfirmed: `${at}: ${item || 'this item'} could not be confirmed because the SharePoint capture is incomplete.`,
      source_day_unconfirmed: `${at}: ${item || 'this item'} exists in SharePoint, but its business day could not be confirmed.`,
      not_on_sharepoint_for_day: `${at}: this row is not in SharePoint for this day${item && where ? ` (${item})` : ''}.`,
      not_on_sharepoint: `${at}: this row is not in SharePoint${item && where ? ` (${item} does not exist in the list)` : ''}.`,
      item_listed_twice: `${at}: duplicate row. ${item || 'The same SharePoint item'} appears more than once in the report.`,
      report_row_without_item_id: `${at}: this row has no SharePoint item number, so it could not be matched.`,
      wrong_list: `${at}: this row links to a different SharePoint list.`,
      field_not_in_source: `${field[0].toUpperCase() + field.slice(1)} is not recorded in SharePoint, so it could not be compared.`,
      export_item_not_on_sharepoint: `${item || at} is in the report's export but not in SharePoint.`,
      export_source_unconfirmed: `${at}: the report does not say which SharePoint list this row came from.`,
    }[row.status];
    return said || `${at}: ${String(row.status || 'difference').replaceAll('_', ' ')}${row.note ? '. ' + String(row.note).replace(/\.$/, '') : ''}.`;
  }
  // What a report row actually holds, as "Column: value" pairs, so a difference shows the row it is about.
  function fdRowValues(sheet, rowNo) {
    const g = state.grid; if (!g || g.sheet !== sheet || !rowNo || !Array.isArray(g.rows)) return '';
    const row = g.rows[rowNo - 1]; if (!row) return '';
    // Only parsed table headers are labels. A previous data row is never a header.
    const tableHeaders = (state.report?.sheets || []).find(s => s.name === sheet)?.tables || [];
    const headerRow = tableHeaders.map(t => t.header_row).filter(n => Number.isInteger(n) && n > 0 && n < rowNo).sort((a,b) => b-a)[0];
    const head = headerRow ? g.rows[headerRow - 1] : null;
    const parts = [];
    row.forEach((value, i) => { if (value == null || String(value).trim() === '' || parts.length >= 7) return; const text = String(value); if (/^https?:/i.test(text)) return;
      parts.push(`${head && head[i] ? String(head[i]).trim() : 'Column ' + (i + 1)}: ${text.slice(0, 40)}`); });
    return parts.join(' · ');
  }
  function fdRowLine(sheet, rowNo) { const text = fdRowValues(sheet, rowNo); return text ? h('span', {class: 'fd-rowvalues'}, 'This row: ' + text) : null; }
  function fdComparison(r) {
    const rec = r.source_reconciliation || {}, counts = rec.count_reconciliation, sp = r.review?.sharepoint || {};
    const value = v => v == null ? 'Unknown' : v === '' ? 'Blank' : String(v);
    const rows = counts?.record_counts || {};
    const total = side => `${value(side?.total)} (${value(side?.known_rows)} known rows; ${value(side?.unknown_rows)} unknown rows)`;
    const pair = (label, data) => h('div', {class: 'fd-comparison-pair'}, h('dt', {}, label),
      h('dd', {}, `Report: ${total(data?.report)} · Source: ${total(data?.source)} · Recorded result: ${{matched:'Matched', differs:'Differs', unavailable:'Unavailable'}[data?.status] || 'Unavailable'}`));
    const bound = rec.bound_to || {}, current = !rec.stale && bound.file_hash === r.file_hash && !!bound.checker_version && bound.checker_version === r.checker_version;
    const sourceHolds = (r.holds || []).filter(hold => String(hold.code || '').startsWith('source_'));
    const verified = fdSourceVerified(r);
    const gapStatuses = new Set(['source_day_unconfirmed','source_item_unconfirmed']);
    const gaps = (rec.rows || []).map(fdPresentationRow).filter(row => gapStatuses.has(row.status));
    const differences = (rec.rows || []).map(fdPresentationRow).filter(row => row.status && row.status !== 'matched' && !gapStatuses.has(row.status));
    const complete = fdSourceComplete(r);
    // "No" needs the same complete, current evidence as "Yes": a difference read through an unconfirmed column or an
    // incomplete read may not be real, so it is listed as unconfirmed instead
    const differs = complete && sp.status === 'differences' && differences.length > 0;
    // totals that disagree, in plain words (records count rows; inspected and rejected count parts)
    const totals = [];
    const q = counts?.quantities || {};
    for (const [key, label] of [['inspected', 'Parts inspected'], ['rejected', 'Parts rejected']])
      if (q[key]?.status === 'differs') totals.push(`${label}: ${value(q[key].report?.total)} in the report, ${value(q[key].source?.total)} in SharePoint.`);
    if (rows.report_rows != null && rows.matched_unique_items != null && rows.report_rows !== rows.matched_unique_items && differs)
      totals.push(`Records: ${value(rows.report_rows)} rows in the report, ${value(rows.matched_unique_items)} matched in SharePoint.`);
    if (rows.duplicate_report_rows) totals.push(`${rows.duplicate_report_rows} duplicate row${rows.duplicate_report_rows === 1 ? '' : 's'} in the report.`);
    const checking = !versionReadOnly(r) && !verified && !differs && (state.fdReconciling || (typeof fdAutoReconcile === 'function' && state.report === r && fdAutoReconcile(r)));
    const possible = !verified && !differs && differences.length > 0;
    const spIssues = typeof allIssues === 'function' && r.review ? allIssues(r.review).filter(i => !i.verification_gap).filter(i => String(i.source || '').startsWith('SharePoint') || String(i.code || '').startsWith('sharepoint_')) : [];
    const failed = state.fdAutoSPError?.key === r.file_hash + ':' + r.check_hash ? state.fdAutoSPError.message : null;
    const kinds = new Map(); for (const row of differences) kinds.set(row.status, (kinds.get(row.status) || 0) + 1);
    const kindText = {value_differs: 'values differ from SharePoint', export_value_differs: 'values differ from SharePoint', changed_since_export: 'changed on SharePoint since the export',
      source_item_unconfirmed: 'not yet confirmed because source capture is incomplete', source_day_unconfirmed: 'on SharePoint with an unconfirmed business day', missing_from_report: 'on SharePoint but missing from the report', not_on_sharepoint_for_day: 'not on SharePoint for this day', not_on_sharepoint: 'not on SharePoint at all',
      item_listed_twice: 'duplicate rows', report_row_without_item_id: 'rows with no SharePoint item number', wrong_list: 'from a different SharePoint list'};
    const quick = [...kinds].sort((a, b) => b[1] - a[1]).map(([k, n]) => `${n} ${kindText[k] || String(k).replace(/_/g, ' ')}`);
    const answer = verified ? 'Recorded comparison: Matches SharePoint' : differs ? `Does not match SharePoint · ${differences.length}${rec.rows_truncated ? '+' : ''} ${differences.length === 1 ? 'difference' : 'differences'}`
      : checking ? 'Checking SharePoint…' : gaps.length ? `SharePoint verification incomplete · ${gaps.length} ${gaps.length === 1 ? 'gap' : 'gaps'}` : possible ? `SharePoint verification incomplete · ${differences.length} ${differences.length === 1 ? 'item' : 'items'} to check` : spIssues.length ? `${spIssues.length} ${spIssues.length === 1 ? 'difference' : 'differences'} from SharePoint` : failed ? 'SharePoint could not be checked' : 'SharePoint check is not complete';
    return h('details', {class: 'fd-comparison ' + (verified ? 'ok' : differs || (!possible && spIssues.length) ? 'bad' : 'unknown'), open: differs || possible || (!verified && spIssues.length > 0) || null},
      h('summary', {}, verified ? icon('ok', 15) : differs ? icon('x', 15) : checking ? h('span', {class: 'spin'}) : icon('warn', 15), ' ', answer),
      (differs || possible) && quick.length ? h('p', {class: 'fd-sp-quick'}, quick.join(' · ') + '.') : null,
      failed && !checking ? h('p', {class: 'fd-hint', role: 'status'}, failed) : null,
      fdSourceRefresh(r),
      verified ? h('p', {}, 'The saved source comparison matched every record and quantity. This records that comparison, not current checked-copy eligibility; see the verification status above.') : null,
      differs ? h('section', {}, h('h3', {}, `What is different (${differences.length}${rec.rows_truncated ? '+' : ''})`),
        totals.length ? h('ul', {class: 'fd-difference-totals'}, totals.map(t => h('li', {}, t))) : null,
        h('ul', {class: 'fd-difference-list', tabindex: '0', 'aria-label': 'Differences from SharePoint'}, differences.map(row => h('li', {}, fdDifferenceText(row),
          row.url ? [' ', h('a', {href: row.url, target: '_blank', rel: 'noopener'}, 'Open in SharePoint')] : null))),
        null) : null,
      !verified && !differs && !checking ? h('p', {class: 'fd-hint'}, current && rec.rows ? `SharePoint was read, but part of the comparison could not be confirmed${sp.message ? ': ' + String(sp.message).split('. ')[0] + '.' : '.'}`
        : (r.assignment || {}).needs_person ? 'Choose the client and job first; then SharePoint is checked automatically.' : 'SharePoint has not answered for this report yet.') : null,
      gaps.length ? h('section', {}, h('h3', {}, `Verification gaps (${gaps.length})`),
        h('p', {class: 'fd-hint'}, 'These are not confirmed report errors. Keep the rows unchanged and complete the source check.'),
        h('ul', {class: 'fd-difference-list'}, gaps.map(row => h('li', {}, fdDifferenceText(row), row.note ? ' ' + row.note : '',
          row.url ? [' ', h('a', {href: row.url, target: '_blank', rel: 'noopener'}, 'Open in SharePoint')] : null, fdRowLine(row.sheet,row.row))))) : null,
      possible ? h('section', {}, h('h3', {}, 'What needs verification'),
        h('ul', {class: 'fd-difference-list', tabindex: '0', 'aria-label': 'Differences from SharePoint'}, differences.map(row => h('li', {}, fdDifferenceText(row), row.note && (['source_day_unconfirmed','source_item_unconfirmed'].includes(row.status) || /dated/.test(row.note)) ? ' ' + row.note : '',
          row.url ? [' ', h('a', {href: row.url, target: '_blank', rel: 'noopener'}, 'Open in SharePoint')] : null, fdRowLine(row.sheet, row.row)))),
        h('p', {class: 'fd-hint'}, 'Earlier comparison, not verified: SharePoint could not be fully confirmed, so some of these may not be real differences.')) : null,
      !verified && !differs && !possible && spIssues.length ? h('section', {}, h('h3', {}, 'What is different'),
        h('ul', {class: 'fd-difference-list', tabindex: '0', 'aria-label': 'Differences from SharePoint'}, spIssues.map(i => h('li', {}, [i.sheet, i.cell ? 'cell ' + i.cell : i.row ? 'row ' + i.row : null].filter(Boolean).join(' ') + ': ' + (i.text || i.message || ''), fdRowLine(i.sheet, i.row))))) : null,
      fdSourceRecovery(r),
      sp.web_url ? h('a', {href: sp.web_url, target: '_blank', rel: 'noopener'}, 'Open the SharePoint list (read only)') : null,
      h('details', {class: 'fd-comparison-tech'}, h('summary', {}, 'Technical details'),
      !verified ? h('p', {class: 'fd-coverage', role: 'note'}, sp.message || 'Current exact-source verification is unavailable. Stored totals do not establish client readiness.') : null,
        sourceHolds.length ? h('ul', {class: 'fd-coverage'}, sourceHolds.map(hold => h('li', {}, hold.text || hold.code))) : null,
        h('p', {}, 'Inspection records count rows. Inspected and rejected quantities count units. A match is evidence for this snapshot only, not permission to send to a client.'),
        h('p', {}, `Source: ${rec.list?.name || (typeof rec.list === 'string' ? rec.list : null) || sp.source || 'Unknown'} · Site ID: ${rec.site_id || 'Unknown'} · List ID: ${rec.list_id || rec.list?.id || 'Unknown'} · Snapshot: ${rec.snapshot_id || sp.snapshot_id || 'Unknown'} · Captured: ${rec.captured_at || sp.captured_at || 'Unknown'}`),
        h('p', {}, `Compared workbook: ${bound.file_hash || 'Unknown'} · Checker: ${bound.checker_version || 'Unknown'}${current ? '' : ' · Binding is missing or out of date; re-check before relying on these totals.'}`),
        counts ? h('dl', {},
          h('dt', {}, 'Inspection records'), h('dd', {}, `Report: ${value(rows.report_rows)} · Source rows read: ${value(rows.source_rows)} · Unique source items matched: ${value(rows.matched_unique_items)}`),
          h('dt', {}, 'Coverage gaps'), h('dd', {}, `Duplicate report rows: ${value(rows.duplicate_report_rows)} · Missing source items: ${value(rows.missing_source_items)} · Unmatched report rows: ${value(rows.unmatched_report_rows)}`),
          pair('Inspected quantity', counts.quantities?.inspected), pair('Rejected quantity', counts.quantities?.rejected), pair('Records with rejected quantity', counts.rejected_records))
          : h('p', {}, 'Record and quantity comparison is unavailable for this snapshot. Unknown counts are not zero.'),
        rec.coverage?.complete !== true ? h('p', {class: 'fd-coverage', role: 'note'}, 'Source coverage is incomplete or unknown. Rows read are not a complete source total; no missing quantity is treated as zero.') : null,
        h('p', {}, `Fields checked: ${(rec.fields_checked || sp.fields_checked || []).join(', ') || 'Unknown'}. Fields not checked: ${(rec.fields_not_checked || sp.fields_not_checked || []).join(', ') || 'Not specified'}. This does not verify photos, source correctness or release readiness.`),
        (rec.not_verified_because || []).length ? h('ul', {}, rec.not_verified_because.map(reason => h('li', {}, reason))) : null,
        ...[['duplicates', 'Duplicate report references'], ['exclusions', 'Source records outside the confirmed business date']].map(([key, title]) =>
          (counts?.[key] || []).length ? h('details', {}, h('summary', {}, `${title} · ${counts[key].length}`), h('ul', {}, counts[key].slice(0, 25).map((x, n) =>
            h('li', {'data-comparison-id': `${key}:${n}`}, `${x.sheet ? x.sheet + ' · row ' + x.row + ' · ' : ''}Source item ${value(x.item)}${x.day ? ' · ' + x.day : ''}: ${x.evidence || x.reason || 'Evidence unavailable'}`,
              x.day_basis ? h('small', {}, ` Date basis: ${x.day_basis}`) : null))), counts[key].length > 25 ? h('p', {class: 'fd-hint'}, `…and ${counts[key].length - 25} more.`) : null) : null),
        h('p', {}, counts?.note || 'No report rows are deleted or excluded by this display.'),
        null));
  }
  function fdSources(r) {
    const rec = r.source_reconciliation || {}, sp = r.review.sharepoint || {}, sel = r.dataset_selection || {}, cov = rec.coverage || {};
    const extra = [];
      if ((r.other_days || []).length) extra.push(h('section', {class: 'block', id: 'otherdays'},
        h('header', {}, h('h2', {}, 'Identifiers also found on another day'), h('p', {}, `${r.other_days.length} identifier/day matches shown${r.other_days.length >= 20 ? ' (display limited to 20)' : ''}; these identifiers also occur in another day's report for ${r.review.job || 'this job'}. Repeated identifiers alone do not prove duplicate records or double-counted units.`)),
        h('ul', {class: 'otherdays'}, Object.values(r.other_days.reduce((g, x) => { (g[x.file_hash] ||= {...x, vins: []}).vins.push(x.vin); return g; }, {}))
          .map(g => h('li', {}, h('b', {}, shortDay(g.day)), ' ', h('a', {href: '#r=' + g.file_hash}, g.filename), h('span', {}, g.vins.join(', ')))))));
    const kv = (k, v) => v == null || v === '' ? null : [h('dt', {}, k), h('dd', {}, String(v))];
    return h('section', {class: 'fd-utility'}, h('h2', {}, 'Sources'),
      h('p', {}, 'Source coverage is shown separately from report correctness. A partial or conflicting read is never called verified.'),
      h('article', {class: 'fd-hist'}, h('b', {}, `SharePoint: ${fdSourceVerified(r) ? 'matches item by item' : ({match: 'not verified', differences: 'differs', incomplete: 'incomplete, not verified', unverified: 'not verified', stale: 'out of date', day_level: 'day totals only'})[sp.status] || 'not compared'}`),
        h('dl', {class: 'fd-kv'}, kv('List', sp.source), kv('Snapshot', sp.snapshot_id), kv('Captured', when2(sp.captured_at)), kv('Matching evidence', fdSourceSummary(r)),
          kv('Fields checked', (sp.fields_checked || []).join(', ')), kv('Fields not checked', (sp.fields_not_checked || []).join(', ')), kv('Pages read', cov.pages),
          kv('Denied / throttled / failed pages', [cov.denied, cov.throttled, (cov.failed_pages || []).length].some(Boolean) ? `${cov.denied || 0} / ${cov.throttled || 0} / ${(cov.failed_pages || []).length}` : null),
          kv('Undated records', cov.undated), kv('Why not verified', (rec.not_verified_because || []).join(' '))),
        sp.web_url ? h('a', {href: sp.web_url, target: '_blank', rel: 'noopener'}, 'Open the list (read only)') : null),
      h('article', {class: 'fd-hist'}, h('b', {}, 'Record set'), h('dl', {class: 'fd-kv'}, kv('Counted', (sel.authoritative || []).join(', ')), kv('Compared, not added', (sel.compared || []).join(', ')),
        kv('Status', sel.status), kv('Roles', Object.entries(sel.roles || {}).map(([k, v]) => `${k}: ${v}`).join(' · ')))),
      fdComparison(r), ...extra, readBlock(r));
  }
  // switching sheets keeps each sheet's scroll position
  function fdKeepScroll() { const w = document.querySelector('#sheet-block .gridwrap'); if (w && state.grid) (state.sheetScroll ||= {})[state.grid.sheet] = [w.scrollTop, w.scrollLeft]; }
  // keyboard: j / k next and previous correction, s select, Escape closes drawers
  document.addEventListener('keydown', e => {
    if (document.querySelector('dialog[open]') || !document.body.classList.contains('reportmode') || e.target.closest('input, textarea, select, [contenteditable]') || e.metaKey || e.ctrlKey || e.altKey) return;
    const flat = (state.fdItems || []).filter(x => x.tier === state.fdTier && (state.fdIncludeDecided || x.state === 'open' || x.key === state.fdCur)), at = flat.findIndex(x => x.key === state.fdCur);
    if (e.key === 'j') { fdOpen(flat[Math.min(flat.length - 1, at + 1)]); e.preventDefault(); }
    if (e.key === 'k') { fdOpen(flat[Math.max(0, at - 1)]); e.preventDefault(); }
  });
  const fdNarrow = matchMedia('(max-width: 900px)'); const syncNarrow = () => document.body.classList.toggle('fd-narrow', fdNarrow.matches);
  fdNarrow.addEventListener('change', syncNarrow); syncNarrow();

  // ---------------- right-hand panel: Fixes (issues grouped by severity and type, suggested fixes), Facts, Ask Claude
  const myName = () => store.get('rd-approver', '') || (state.me?.user && !state.me.user.virtual ? state.me.user.name || state.me.user.email || '' : '');
  const reviewedSet = () => new Set(store.get('rd-reviewed:' + state.report.file_hash, []));
  function showSide(tab) { if (tab === 'facts') tab = 'fixes'; if (state.sideTab !== tab) { state.sideTab = tab; drawSide(); } }
  function drawSideTabs() {
    const bar = $('sidetabs'); if (!bar || !state.report) return;
    const done = reviewedSet(), open = walkIssues().filter(i => !done.has(i.key)).length, tab = state.sideTab;
    const btn = (id, label, badge) => h('button', {type: 'button', role: 'tab', class: tab === id ? 'on' : '', 'aria-selected': String(tab === id),
      onclick: () => { if (id === 'claude') state.chatUnread = false; showSide(id); }}, label, badge ? h('i', {}, badge) : null);
    bar.replaceChildren(btn('fixes', 'Errors', null), btn('claude', 'Ask Dex', state.chatUnread ? 'new' : null));
    const mc = $('fd-mobile-count'); if (mc) mc.textContent = '';
    void open;
  }
  function drawSide() {
    // Paging, selection and async refreshes must not discard typed manual text.
    state.fdManual?.keepDraft?.();
    const side = $('side'), r = state.report; if (!side || !r) return;
    // The header status follows every redraw of the panel (for example when Dex starts or finishes).
    { const pill = typeof document !== 'undefined' && document.querySelector?.('.fd-global-status');
      if (pill) { const shown = fdShown(fdStatus(r)); pill.className = 'fd-status fd-global-status ' + shown.tone; pill.title = shown.why || ''; pill.replaceChildren(icon(shown.icon, 16), h('span', {}, shown.text)); } }
    if (state.sideTab === 'facts') state.sideTab = 'fixes';
    const rv = r.review, tab = state.sideTab || 'fixes';
    const body = h('div', {class: 'sidebody', id: 'side-body'});
    const open = state.fixes ? fdItems().filter(it => ['open', 'selected', 'deferred', 'confirmed'].includes(it.state)).length : null;
    side.replaceChildren(h('header', {class: 'fd-apphead'},
        h('p', {class: 'fd-errcount' + (open === 0 ? ' done' : '')}, open == null ? 'Counting possible errors…' : open === 0 ? [icon('ok', 18), ' 0 possible errors'] : plural(open, 'possible error')),
        // Nothing left: the copy with every fix applied is one click away, right at the top.
        open === 0 ? h('a', {class: 'btn primary', id: 'fd-download-top', href: '/api/spreadsheets/download?id=' + r.file_hash, download: r.filename}, r.fixed_from ? 'Download fixed report' : 'Download report') : null,
        // Change any cell by hand, whether or not it was flagged.
        !versionReadOnly(r) ? h('button', {type: 'button', class: 'btn', id: 'fd-edit-any', disabled: !state.fixes || fdBackgroundBusy() || !!state.fdAIRun?.running || null, onclick: () => fdFreeStart()}, 'Edit a cell manually') : null,
        h('button', {type: 'button', class: 'btn', id: 'fd-open-review', disabled: state.busy || state.rechecking || !state.fixes, onclick: () => fdReviewDialog()}, 'Review all possible errors')),
      fdFreeCard(),
      // Progress of an AI fix, then its revision summary, sit at the very top where the button was pressed.
      state.fdTidying && fdAutoMode() ? h('div', {class: 'fd-airun fd-airun-busy', role: 'status'}, h('p', {}, h('span', {class: 'spin'}), state.fdTidying.look ? ' Making the report colours consistent…' : ` Fixing formatting in ${plural(state.fdTidying.count || 0, 'cell')}…`),
        h('div', {class: 'fd-progress fd-progress-moving', role: 'progressbar', 'aria-label': 'Formatting clean-up progress'}, h('div', {class: 'fd-progress-bar'})),
        h('p', {class: 'fd-hint'}, 'This takes up to a minute. The buttons below turn on when it is saved.'))
        : state.fdAIRun?.hash === r.file_hash && fdAutoMode() ? fdAIRunBanner(state.fdAIRun, open ?? 0) : null,
      fdReportSummary(r), fdComparison(r), h('div', {class: 'sidetabs', id: 'sidetabs', role: 'tablist'}), body);
    drawSideTabs();
    if (tab === 'fixes') {
      fdCorrections(body);
      body.append(h('button', {type: 'button', class: 'btn', disabled: state.busy || state.rechecking || state.decisionPending || !!state.aiProposalOperation, onclick: fdRecheck}, 'Recheck this report'));
      body.append(h('button', {type: 'button', class: 'btn small', onclick: () => { state.fdView = 'sources'; drawReport(); }}, 'Source evidence and coverage'));
      body.append(h('label', {class: 'fd-autotidy'}, h('input', {type: 'checkbox', 'data-selection': 'true', checked: store.get('rd-auto-tidy', true) !== false || null,
        onchange: e => { store.set('rd-auto-tidy', e.target.checked); if (e.target.checked && state.report) { delete (state.fdAutoTidied || {})[state.report.file_hash]; fdAutoTidy(state.report); } }}),
        ' Work automatically: fix formatting when a report opens, and after an AI fix leave what Dex cannot change as written without asking'));
      const sc = shiftCard(rv); if (sc) body.append(h('div', {id: 'shiftfix'}, sc));
    } else {
      body.append(h('div', {id: 'chat', class: 'chat embedded', 'aria-label': 'Ask Dex'}));
      drawChat();
      body.querySelector('textarea')?.focus();
    }
    state.rechecking?.lock?.(); state.aiProposalOperation?.lock?.();
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
          if (c.total) toast('Selected changes are ready. Preview and apply them when you are ready.');
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
          if (c.total) toast('Selected changes are ready. Preview and apply them when you are ready.');
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
            state.aiOn ? h('button', {type: 'button', disabled: true, title: 'AI workbook changes are paused pending reviewed-plan safety checks', class: 'btn small', onclick: e => { const k = ticked.map(x => x.key); aiFix(k, e.target); }}, `Let Dex fix ${n === same.length ? 'all ' : ''}${n}`) : null,
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
        issue.verdict_reason ? h('p', {class: 'hint'}, 'Dex: ' + issue.verdict_reason) : null,
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
  // Formatting-only clean-ups (capital letters, extra spaces, hidden characters, numbers typed as text) are never shown
  // as errors: the checked ones are made as soon as the report opens, saved as a new version (the original is kept).
  const fdTidyOnly = it => !!it.fix && !!fmtKind(it.fix) && it.selectable && ['open', 'selected'].includes(it.state) && !it.own && !it.previous && it.proposed === it.fix.after;
  async function fdAutoTidy(report) {
    state.fdAutoTidied ||= {};
    if (!report || state.report !== report || versionReadOnly(report) || state.fdAutoTidied[report.file_hash] || state.fdAIRun?.running || store.get('rd-auto-tidy', true) === false) return;
    let tidy;
    try { if (state.report.review?.page?.complete === false) return; const cells = new Map(); const kept = fdProtectedCells(); for (const it of fdItems()) if (fdTidyOnly(it) && !kept(it.fix)) cells.set(JSON.stringify([it.sheet, it.cell]), it); tidy = [...cells.values()]; } catch (e) { return; }
    // No cell to clean, but the workbook's colours are a patchwork: save one copy with only the look tidied.
    const lookOnly = !tidy.length && state.fixesRestyle === report.file_hash;
    if (!tidy.length && !lookOnly) return;
    state.fdAutoTidied[report.file_hash] = true;   // once per version, never in a loop
    state.fdTidying = {count: tidy.length, look: lookOnly};   // the SharePoint check waits for the tidied version instead of running twice
    if ($('side')) drawSide();   // show the progress bar straight away: the buttons are off until this is saved
    try {
      for (let i = 0; (state.fdReconciling || state.fdAutoRecheckWaiting || state.rechecking || state.busy || state.decisionPending || state.aiProposalOperation) && i < 150; i++) await new Promise(r => setTimeout(r, 400));
      if (state.report !== report || versionReadOnly(report) || state.gridDraft || state.fdAIRun?.running) return;
      const binding = reviewBinding(report), accepted = tidy.map(it => it.fix);
      if (lookOnly) {
        state.busy = true;
        let answer; try { answer = await api('/api/spreadsheets/apply-fixes', {...binding, changes: [], restyle: true}); } finally { state.busy = false; }
        if (answer?.item?.file_hash && state.report === report && location.hash === '#r=' + report.file_hash) {
          state.fdAIRun = {hash: answer.item.file_hash, done: true, tidy: true, look: true, fixed: 0, leftAsWritten: 0, summary: true, items: []};
          select(answer.item.file_hash, true); loadList().catch(() => {});
        }
        return;
      }
      const dec = Object.fromEntries(accepted.map(f => [f.id, {state: 'accepted', after: f.after}]));
      const capture = captureCorrections(accepted, dec, {}, binding, report);
      const receipt = await applyFixes(capture.accepted, capture.dec, capture.edits, {download: false, binding, independentScope: true, quiet: true, throwOnError: true, returnReceipt: true});
      // Say what was done on the fixed version, so an empty to-do list is never a mystery.
      if (receipt?.file_hash && !state.fdAIRun?.running) state.fdAIRun = {hash: receipt.file_hash, done: true, tidy: true, fixed: 0, leftAsWritten: 0, summary: true, items: []};
    } catch (e) { /* the clean-ups stay available under Fix everything with AI */ }
    finally { state.fdTidying = false; if (state.report === report && $('side')) drawSide(); }
  }
  async function loadFixes(hash) {
    const report = state.report;
    if (!report || versionReadOnly(report)) return;
    try {
      const data = await api('/api/spreadsheets/fixes', {file_hash: hash});
      if (state.selected !== hash || state.report !== report || versionReadOnly(report)) return;
      state.fixes = data.proposals || []; state.fixesError = null; state.fixesRestyle = data.restyle === true ? hash : null;
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
    } catch (e) { if (state.selected !== hash || state.report !== report || versionReadOnly(report)) return; state.fixes = []; state.fixesError = e.message; state.aiFix = {status: 'not_run'}; }
    drawFixes();
    fdAutoTidy(report);
    document.querySelector('.fd-mode-selector')?.replaceWith(fdModeSelector(state.report));
    // first time a report with issues is opened, Claude drafts the fixes, then offers them in the chat
    const rv = state.report?.review || {}, open = (rv.issues || []).filter(i => i.severity !== 'info').length + (rv.dismissed || []).length;
    // Opening a report never runs Claude by itself: its read, its double-check (which can set issues aside) and its draft
    // fixes all change what the report shows, so each is a person's choice (Ask Claude tab, or Facts → AI review).
    void open; maybeBrief(hash, false);
    if (state.openReviewFor === hash && state.report?.file_hash === hash) { state.openReviewFor = null; fdReviewDialog(); }
    // nothing is changed automatically on opening: suggestions are prepared, and a person selects, previews and applies them
  }
  async function runReview(hash) {
    if (hash === state.report?.file_hash) fdReviewDialog();
  }
  async function runVerify(hash) {
    if (hash === state.report?.file_hash) fdReviewDialog();
  }
  async function runAiFixes(auto, binding) {
    if (!binding || !state.aiOn) return;
    if (state.report?.file_hash !== binding.file_hash || state.report?.check_hash !== binding.expected_check_hash) { toast('The report changed. Review the current version before preparing suggestions.', true); return; }
    // Legacy entrypoints only open the selection chooser. They never broaden a request to the whole report.
    fdReviewDialog();
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
    const r = state.report, hash = r.file_hash, displayedBinding = reviewBinding(r);
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
        : withFix.length || dups.length ? `${withFix.length + dups.length} get the suggested change; ${none.length} ${state.aiOn ? 'Dex fixes' : 'need you'}`
        : state.aiOn ? 'Dex fixes ' + (items.length === 1 ? 'it' : 'them') : 'No change proposed: needs you';
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
      p.correction = captureCorrections(p.fixes, Object.fromEntries(p.fixes.map(f => [f.id, {after: dec[f.id]?.after ?? f.after}])), {}, displayedBinding, r);
      return p;
    };
    const approve = async (p, btn, quiet) => {   // suggested changes, extra copies out, and (with AI on) Dex takes whatever had no suggestion
      if (state.busy) return quiet ? hash : undefined;
      if (!p.fixes.length && !p.dups.length && !(state.aiOn && p.none.length)) return quiet ? hash : toast('Nothing ticked has a suggested cell change. Review a manual correction; AI changes are paused.', true);
      if (btn) { btn.disabled = true; btn.textContent = 'Fixing…'; }
      if (p.dups.length || p.none.length) { toast('Review cell corrections separately. Automatic and row-deletion batches are paused.', true); if (btn) btn.disabled = false; return null; }
      const k = p.correction;
      if (!k) { toast('Reopen the correction preview.', true); return null; }
      let cur = hash;
      if (k.total) { cur = await applyFixes(k.accepted, k.dec, k.edits, {download: false, thenOpen: false}); if (!cur) { if (btn) btn.disabled = false; return null; } }
      if (quiet) return cur;
      await loadList(); select(cur); showResult(cur, true);
    };
    const aiFix = async (p, btn) => {   // Dex decides each ticked item (any number: it works in batches)
      if (state.busy || !p.issues.length) return;
      if (btn) { btn.disabled = true; btn.textContent = `Dex is fixing ${fmt(p.issues.length)}…`; }
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
      : state.aiOn ? 'AI workbook changes are paused; review a manual correction' : 'No suggested change: fix it yourself, or Ignore';
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
          k.group && state.aiOn ? h('button', {type: 'button', disabled: true, title: 'AI workbook changes are paused pending reviewed-plan safety checks', class: 'linkish small', onclick: ev => aiFix({issues: [e], fixes: [], dups: [], none: [e], n: 1}, ev.target)}, 'Fix with AI') : null,
          h('button', {type: 'button', class: 'linkish small', onclick: () => k.row ? showCell(e.sheet, e.cell) : goTo(e)}, 'Show')); };
      return h('section', {class: `kind sev-${k.sev}`, id: 'kind-' + k.id.replace(/[^\w-]/g, '_')},
        h('div', {class: 'kind-head'}, h('h3', {}, `Review ${titleOf(k)}?`), k.sev === 'high' ? h('span', {class: 'kind-pill'}, 'Must fix') : null),
        h('div', {class: 'kind-ex'}, h('span', {class: 'kind-ex-k'}, n > 1 ? 'The first one' : 'The issue'),
          h('button', {type: 'button', class: 'linkish small kind-ex-where', onclick: ex.go}, ex.where), ex.what ? h('span', {class: 'kind-ex-what'}, ex.what) : null, h('span', {class: 'kind-ex-change'}, ex.change)),
        h('div', {class: 'kind-btns'},
          k.hasSugg ? h('button', {type: 'button', class: 'btn primary', disabled: !on, onclick: e => { const b = e.target; previewAll({...p, none: []}, () => approve(p, b)); }}, n > 1 ? `Change${cnt}` : 'Change it') : null,
          k.ai ? h('button', {type: 'button', disabled: true, title: 'AI workbook changes are paused pending reviewed-plan safety checks', class: 'btn ai',  onclick: e => aiFix(k.unsure ? partsOf([k], {includeLeft: true}) : p, e.target)}, n > 1 ? `Fix${cnt} with AI` : 'Fix with AI') : null,
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
      const all = partsOf(kinds), allDisplayed = partsOf(kinds, {includeLeft: true}), total = kinds.reduce((t, k) => t + k.entries.length, 0);
      const acceptAll = btn => { const displayed = allDisplayed; return previewAll(displayed, () => approve(displayed, btn)); };
      const unsureAny = kinds.some(k => k.unsure);
      state.fixEverything = {hash, parts: opt => partsOf(kinds, opt), approve, run: () => acceptAll(document.querySelector('#cleanoffer .sum-accept'))};
      const canAll = all.fixes.length || all.dups.length || (state.aiOn && (all.none.length || unsureAny));
      const unsureN = kinds.filter(k => k.unsure).reduce((t, k) => t + k.entries.length, 0);
      const summary = h('div', {class: 'sum'},
        h('h2', {id: 'clean-title', class: 'sum-title'}, 'Issue summary'),
        h('p', {class: 'sum-line'}, `${plural(total, 'issue')} in ${plural(kinds.length, 'kind')}.` + (r.fixed_from ? ' Nothing below has been changed yet.' : ' Nothing has been changed yet.')),
        h('ul', {class: 'sum-list'}, kinds.map(k => h('li', {}, h('span', {class: 'sev ' + k.sev}),
          h('button', {type: 'button', class: 'linkish', onclick: () => document.getElementById('kind-' + k.id.replace(/[^\w-]/g, '_'))?.scrollIntoView({behavior: 'smooth', block: 'start'})}, titleOf(k))))),
        canAll ? h('button', {type: 'button', class: 'btn primary sum-accept', onclick: e => acceptAll(e.target)}, 'Review suggested cell changes') : null,
        h('p', {class: 'sum-note'}, canAll ? ('Only the displayed cell changes can be applied. AI and row changes are paused. ') + 'Or review each kind below.' : 'Review each kind below.'));
      steps.push({id: 'review', title: `${plural(total, 'issue')} to review`, panel: h('div', {class: 'plan2'}, summary, h('div', {class: 'kinds'}, kinds.map(card)))});
    }
    const walkOpen = openIssues;
    const wentThrough = walkIssues().filter(i => revd.has(i.key)).length;
    if (!walkOpen.length && wentThrough) steps.push({id: 'walked', title: `Went through ${plural(wentThrough, 'item')}`, done: true});
    if (c.total) steps.push({id: 'save', title: `Save your ${plural(c.total, 'change')}`, say: 'They go into a new version, which is checked again. Your original stays as it is.',
      go: ['Save the changes', () => { toast('Saving your changes into a new version…'); applyFixes(c.accepted, c.dec, c.edits, {download: false}); }]});
    if (!(r.sent || []).some(x => x.sent)) steps.push({id: 'send', title: r.approval ? 'Approval recorded' : 'Approve it', say: r.approval ? 'Sending and draft creation are paused. Download the approved report for manual delivery.' : 'Everything is handled. Check the totals, then approve.',
      go: [r.approval ? 'Approval details…' : 'Approve…', correctionAction(sendFinal)]});
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
      : current === 2 ? 'Next: approve the final copy.' : current === 3 ? 'External sending is paused. Download the approved copy for manual delivery.' : 'All done. This report has been sent.';
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
    const correction = captureCorrections(accepted, dec, edits, reviewBinding(state.report), state.report);
    block.replaceChildren();
    const save = d => { store.set(decisionsKey(), d); drawFixes(); };
    block.append(h('header', {}, h('h2', {}, 'Suggested fixes'),
      h('p', {}, fixes.length ? `${fixes.length} suggested${fixes.some(f => f.source === 'AI fix') ? ', including Dex\'s' : ''}. Accept the ones you want, or edit any cell yourself, then download a corrected copy.`
        : state.aiFixing ? 'Dex is reading the report and writing fixes for the issues above…' : 'No fixes yet. Let AI fix the issues, or click any cell in the sheet to change it.'),
      h('div', {class: 'right'},
        state.aiOn ? h('button', {type: 'button', class: 'btn small' + (state.aiFix?.status === 'done' ? '' : ' primary'), disabled: state.aiFixing, onclick: (() => { const binding = reviewBinding(state.report); return () => runAiFixes(false, binding); })()},
          'Choose findings for AI proposals') : null,
        fixes.length ? h('button', {type: 'button', class: 'btn small', onclick: () => {
          const d = store.get(decisionsKey(), {}); fixes.filter(f => f.confidence === 'high' || f.source === 'AI fix').forEach(f => { d[f.id] = {state: 'accepted', after: d[f.id]?.after ?? f.after}; }); save(d);
        }}, 'Accept all') : null)));
    const ai = state.aiFix || {};
    if (ai.status === 'done' && (ai.skipped || []).length) block.append(h('details', {class: 'skipped'},
      h('summary', {}, `Dex left ${ai.skipped.length} issue${ai.skipped.length === 1 ? '' : 's'} for a person to fix`),
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
        h('div', {class: 'where'}, `${e.source === 'Claude' ? 'Dex (chat)' : 'Your edit'} · ${e.sheet}, cell ${e.cell}` + (e.header ? ` (${e.header})` : '')),
        e.reason ? h('p', {class: 'why'}, e.reason) : null,
        h('div', {class: 'choose'}, h('button', {type: 'button', class: 'btn small', onclick: () => {
          const all = store.get(editsKey(), {}); delete all[id]; store.set(editsKey(), all); drawFixes(); }}, 'Remove')),
        h('div', {class: 'change'}, h('span', {class: 'before'}, e.before === '' ? '(blank)' : e.before), h('span', {'aria-hidden': 'true'}, '→'),
          e.after === '' ? h('span', {class: 'blank'}, '(blank)') : null, input)));
    }
    panel.append(h('div', {class: 'fixbar'},
      h('span', {}, total ? `${total} change${total === 1 ? '' : 's'} ready` + (editList.length ? ` (${editList.length} yours)` : '') : 'No changes yet'),
      total ? h('button', {type: 'button', class: 'btn ghost small', onclick: () => { store.set(decisionsKey(), {}); store.set(editsKey(), {}); drawFixes(); }}, 'Clear all') : null,
      h('button', {type: 'button', class: 'btn', disabled: !total || state.busy, onclick: () => applyFixes(correction.accepted, correction.dec, correction.edits)},
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
    // A value mentioned in a row-level explanation is not a cell identity.
    // Keep row-only findings separate from proposals until a reviewer chooses a field.
    return false;
  }
  function needsCheck() {
    if (!state.report) return {fixes: [], issues: [], total: 0};
    const dec = store.get(decisionsKey(), {}), edits = store.get(editsKey(), {}), done = reviewedSet();
    const fixes = (state.fixes || []).filter(f => !isSureFix(f) && f.source !== 'Suggested' && !edits[editId(f.sheet, f.cell)] && !dec[f.id]);   // suggestions for blanks are optional
    const issues = walkIssues().filter(i => i.sure === false && !done.has(i.key) && !(state.fixes || []).some(f => fixCovers(f, i)));
    return {fixes, issues, total: fixes.length + issues.length};
  }
  const correctionSnapshots = new WeakMap();
  function captureCorrections(accepted, dec, edits, binding, report) {
    const frozen = value => {
      if (value && typeof value === 'object') { Object.values(value).forEach(frozen); Object.freeze(value); }
      return value;
    };
    const snap = frozen(JSON.parse(JSON.stringify({accepted, dec, edits, binding,
      report: {file_hash: report.file_hash, filename: report.filename, revision: revisionOf(report)}})));
    correctionSnapshots.set(snap.accepted, snap);
    return {...snap, total: snap.accepted.length + Object.keys(snap.edits).length};
  }
  function correctionAction(action) {
    const captured = readyChanges();
    return () => action(captured);
  }
  function readyChanges() {
    if (!state.report) return {accepted: [], dec: {}, edits: {}, total: 0};
    const dec = store.get(decisionsKey(), {}), edits = store.get(editsKey(), {});
    const accepted = (state.fixes || []).filter(f => !edits[editId(f.sheet, f.cell)] && dec[f.id]?.state === 'accepted');
    return captureCorrections(accepted, dec, edits, reviewBinding(state.report), state.report);
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
  function downloadWithChanges(c) {
    if (!c?.binding) return;
    if (!c.total) { const a = h('a', {href: '/api/spreadsheets/download?id=' + c.report.file_hash, download: c.report.filename}); document.body.append(a); a.click(); a.remove(); return; }
    fdPreview(c);
  }
  function updateDownloads() {
    const c = readyChanges(), label = c.total ? `Download with ${c.total} change${c.total === 1 ? '' : 's'}` : `Download v${state.report ? revisionOf(state.report) : 1}`;
    for (const b of document.querySelectorAll('.dl-changes')) { b.textContent = state.busy ? 'Building the fixed copy…' : label; b.disabled = !!state.busy; b.onclick = () => downloadWithChanges(c); }
  }
  async function applyFixes(accepted, dec, edits, opts = {}) {
    if (versionReadOnly()) { toast('This saved version is read-only. Open its working copy first.', true); return null; }
    const captured = correctionSnapshots.get(accepted);
    if (!captured?.binding?.expected_check_hash) { const error = new Error('Reopen the correction preview before applying. No save was started.'); if (opts.throwOnError) throw error; toast(error.message, true); return null; }
    ({accepted, dec, edits} = captured);
    let binding = captured.binding;
    if (state.busy || state.decisionPending || state.rechecking || state.aiProposalOperation) { const error = new Error('Another step is still running. No save was started. Wait for it to finish.'); if (opts.throwOnError) throw error; toast(error.message, true); return null; }
    // Typing a new value into a cell whose error was ignored or set aside puts that error back on the list first
    // (recorded as a reopen in the history), so the correction is saved instead of being refused.
    try {
      const typed = Object.values(edits || {}).filter(e => e.source !== 'Claude');
      const held = typed.length && state.report?.file_hash === binding.file_hash ? fdItems().filter(it => ['kept', 'deferred', 'rejected'].includes(it.state) && it.decision && it.binding && (it.cell || it.row) &&
        typed.some(e => (!it.sheet || it.sheet === e.sheet) && (it.cell ? String(it.cell).toUpperCase() === String(e.cell).toUpperCase() : String(e.cell).replace(/^[A-Z]+/i, '') === String(it.row)))) : [];
      if (held.length) {
        await fdSendDecision({...findingBinding(held), ids: held.map(it => it.key), action: 'reopen', reason: 'Put back on the list because the reviewer typed a correction for this cell.', quiet: true, batch: true});
        if (state.decisionFailed) { const message = state.decisionFailed.message; state.decisionFailed = null; throw new Error(message); }
        if (state.report?.file_hash === binding.file_hash) binding = {...binding, ...reviewBinding(state.report)};
      }
    } catch (e) { const error = new Error('The ignored error could not be put back on the list, so nothing was saved. ' + (e?.message || '')); if (opts.throwOnError) throw error; toast(error.message, true); return null; }
    const reportAtSave = state.report, openAtSave = state.openRequest, selectedAtSave = state.selected, routeAtSave = globalThis.location?.hash;
    if (state.busy || state.decisionPending || state.rechecking || state.aiProposalOperation) { const error = new Error('Another step is still running. No save was started. Wait for it to finish.'); if (opts.throwOnError) throw error; toast(error.message, true); return null; }
    state.busy = true; drawFixes(); updateDownloads();
    if (!opts.quiet) toast('Building the fixed copy with your changes…');
    let requestStarted = false, savedItem = null;
    try {
      const changes = accepted.map(f => {
        const after = dec[f.id]?.after ?? f.after;
        return {id: f.id, sheet: f.sheet, cell: f.cell, before: f.before, after, reason: f.reason, source: f.source, snapshot_id: f.snapshot_id, item: f.item, field: f.field,
          change_set: f.change_set, change_set_size: f.change_set_size,
          selected_finding_id: f.selected_finding_id, selected_finding_fp: f.selected_finding_fp,
          kind: f.kind === 'auto' ? 'auto' : after === f.after ? f.kind : (f.kind === 'number' && after.trim() !== '' && !isNaN(Number(after)) ? 'number' : 'text')};
      });
      for (const e of Object.values(edits || {})) {
        const numeric = e.after.trim() !== '' && !isNaN(Number(e.after.replace(/,/g, ''))) && (e.before === '' || !isNaN(Number(e.before)));
        changes.push({sheet: e.sheet, cell: e.cell, before: e.before, after: e.after, reason: e.reason || 'Edited by reviewer', source: e.source === 'Claude' ? 'Claude (chat)' : 'Your edit', kind: e.kind === 'auto' ? 'auto' : numeric ? 'number' : 'text',
          change_set: e.change_set || undefined, change_set_size: e.change_set_size || undefined});
      }
      requestStarted = true;
      const answer = await api('/api/spreadsheets/apply-fixes', {...binding, changes, ...(opts.recommendedOnly ? {recommended_only: true} : {})});
      if (!answer?.item?.file_hash) throw new Error('The server did not return a saved-version receipt.');
      savedItem = answer.item;
      if (opts.gridDraft && state.gridDraft === opts.gridDraft) state.gridDraft = null;
      if (state.celebrate) state.celebrate.hash = answer.item.file_hash;
      // Do not carry local review markers into a new file or erase edits made while this request ran.
      if (!opts.independentScope) for (const [key, shown] of [['rd-fixes:' + binding.file_hash, dec], ['rd-edits:' + binding.file_hash, edits]]) {
        if (JSON.stringify(store.get(key, {})) === JSON.stringify(shown)) store.set(key, {});
      }
      if (opts.download !== false) {
        const link = h('a', {href: '/api/spreadsheets/download?id=' + answer.item.file_hash, download: answer.item.filename});
        document.body.append(link); link.click(); link.remove();
      }
      const existingVersion = answer.already_applied === true || answer.item.already_applied === true;
      const recordedChanges = existingVersion ? null : answer.item.fixed_from?.changes;
      if (!opts.quiet) toast(`${existingVersion ? 'Opened the existing matching version' : 'Saved the corrected version'} "${answer.item.filename}"${Array.isArray(recordedChanges) ? ` with ${plural(recordedChanges.length, 'recorded change')}` : ''}. ${existingVersion ? 'No new version was created. ' : ''}The original is unchanged.`);
      if (opts.thenOpen !== false) {
        // These reads cannot undo the successful, committed save above.
        // Open the saved version straight away; the report list catches up behind it instead of holding the screen.
        if (state.report === reportAtSave && state.openRequest === openAtSave && state.selected === selectedAtSave && globalThis.location?.hash === routeAtSave) {
          opts.beforeOpen?.(); // A confirmed preview's saved inputs are no longer unsaved drafts.
          select(savedItem.file_hash, true);
        }
        loadList().catch(() => { toast('The corrected version is saved, but the report list could not refresh. Open the saved copy or reload the list.', true); });
      }
      return opts.returnReceipt ? savedItem : savedItem.file_hash;
    } catch (e) {
      state.celebrate = null;
      if (savedItem) {
        toast('The corrected version is saved, but the screen could not refresh. Open the saved copy or reload the report.', true);
        return opts.returnReceipt ? savedItem : savedItem.file_hash;
      }
      // A lost response or server error does not prove the transaction failed. Never retry it here.
      const error = !e.saveCommitted && requestStarted && (!e.httpStatus || e.httpStatus >= 500)
        ? new Error('The save could not be confirmed. Reload this report and check its saved versions before trying again. ' + (e.message || '')) : e;
      if (!opts.quiet) toast(error.message, true);
      if (opts.throwOnError) throw error;
      return null;
    }
    finally { state.busy = false; if ($('fixes-block')) drawFixes(); }
  }
  // Take out extra copies of duplicate rows (new corrected version; original untouched). Accepted cell changes are saved first.
  async function removeRows(rows, what) {
    toast('Row deletion is paused until it can be bound to a reviewed source snapshot. No changes were made.', true);
  }
  const isDupRow = i => i.code === 'duplicate_row';

  // A single exact-cell draft, kept in memory only. Typing never writes a workbook.
  function gridDirty() { const d = state.gridDraft; return !!d && d.after !== d.before; }
  function gridCurrent(d) { return !!d && state.report === d.report && state.selected === d.binding.file_hash && JSON.stringify(reviewBinding(state.report)) === JSON.stringify(d.binding); }
  function gridDiscard() {
    if (gridDirty() && !window.confirm('Discard the typed spreadsheet cell draft?')) return false;
    state.gridDraft = null; state.gridEditRequest = (state.gridEditRequest || 0) + 1; return true;
  }
  function gridRequestAllowed(path, body) {
    if (['/api/spreadsheets/chat', '/api/spreadsheets/sheet', '/api/spreadsheets/cells', '/api/spreadsheets/fixes', '/api/spreadsheets/findings'].includes(path)) return true;
    const d = state.gridDraft, x = body.changes?.[0];
    return path === '/api/spreadsheets/apply-fixes' && d?.previewing && gridCurrent(d) && Array.isArray(body.changes) && body.changes.length === 1 && !!x &&
      Object.entries(d.binding).every(([k,v]) => JSON.stringify(body[k]) === JSON.stringify(v)) && x.sheet === d.sheet && x.cell === d.cell && x.before === d.before && x.after === d.after && x.kind === 'auto';
  }
  window.addEventListener('beforeunload', e => { if (gridDirty()) { e.preventDefault(); e.returnValue = ''; } });
  function gridCellScope(sheet, cell) {
    const row = splitCell(cell)?.[1], items = fdItems();
    if (!row || fdManualCellHeld(items, sheet, row, cell)) return false;
    return items.some(it => it.sheet === sheet && (it.cell === cell || !it.cell && it.row === row) && !fdConflict(it) && ['open','selected','confirmed'].includes(it.state));
  }
  async function gridStartEdit(sheet, cell) {
    if (versionReadOnly()) return;
    if ([...document.querySelectorAll('#side input:not([type=checkbox]):not([type=radio]), #side textarea')].some(el => el.value !== el.defaultValue)) { toast('Finish the correction-panel draft before editing the spreadsheet.', true); return; }
    if (state.busy || state.rechecking || state.decisionPending || state.aiProposalOperation) return;
    if (state.gridDraft && !gridDiscard()) return;
    const report = state.report, binding = reviewBinding(report), row = splitCell(cell)?.[1], g = state.grid;
    const unavailable = () => !gridCellScope(sheet, cell) || g.sheet !== sheet || g.hash !== binding.file_hash ||
      (g.formulas || []).includes(cell) || (g.merges || []).some(ref => { const [a,b] = ref.split(':').map(splitCell), c = splitCell(cell); return a && b && c[0] >= a[0] && c[0] <= b[0] && c[1] >= a[1] && c[1] <= b[1]; });
    if (unavailable()) { toast('This cell is not editable here. Review its source or reopen its finding first.', true); return; }
    const ticket = state.gridEditRequest = (state.gridEditRequest || 0) + 1;
    try {
      const data = await api('/api/spreadsheets/cells', {file_hash: binding.file_hash, sheet, row});
      if (state.busy || state.rechecking || state.decisionPending || state.aiProposalOperation || ticket !== state.gridEditRequest || state.report !== report || state.selected !== binding.file_hash || state.grid !== g || JSON.stringify(reviewBinding(report)) !== JSON.stringify(binding)) return;
      const exact = data.sheet === sheet && data.row === row && Array.isArray(data.cells) && data.cells.find(c => c.cell === cell);
      if (!exact || exact.formula || typeof exact.before !== 'string' || unavailable()) throw new Error('Exact editable cell is unavailable. No value was guessed.');
      state.gridDraft = {report, binding, sheet, cell, before: exact.before, after: exact.before, header: exact.header || '', included: true};
      state.gridCell = cell; document.body.classList.remove('fd-panel-open'); drawGrid(); $('grid-cell-input')?.focus();
    } catch (e) { toast(e.message, true); }
  }
  function gridPreview() {
    const d = state.gridDraft;
    if (!gridCurrent(d) || !gridCellScope(d.sheet, d.cell)) { toast('The report or finding changed. Discard this draft and reopen the current cell.', true); return; }
    if (!gridDirty() || !d.included) return;
    d.previewing = true;
    const edits = {[editId(d.sheet,d.cell)]: {sheet:d.sheet,cell:d.cell,before:d.before,after:d.after,header:d.header,kind:'auto',source:'Your edit',reason:'Typed by reviewer in spreadsheet cell'}};
    fdPreview({...captureCorrections([], {}, edits, d.binding, d.report), independentScope:true, gridDraft:d,
      onRemove: () => { d.included = false; d.previewing = false; drawGrid(); }, onApplied: () => { if (state.gridDraft === d) state.gridDraft = null; }});
  }
  function gridDraftControls() {
    const d = state.gridDraft;
    if (!d) return null;
    return h('div', {class:'fd-grid-draft', role:'group', 'aria-label':'Spreadsheet draft'},
      h('span', {role:'status'}, `Draft: ${d.sheet}!${d.cell}. Not applied. ${d.after === '' ? 'Empty clears the cell, not zero.' : ''}`),
      h('label', {}, h('input', {type:'checkbox', checked:d.included, onchange:e => { d.included=e.target.checked; drawGrid(); }}), ' Include cell in preview'),
      h('button', {type:'button',class:'btn primary',id:'grid-preview',disabled:!gridDirty() || !d.included,onclick:gridPreview}, 'Preview changes'),
      h('button', {type:'button',class:'btn',onclick:() => { if(gridDiscard()) drawGrid(); }}, 'Discard cell draft'));
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
  async function loadGrid(sheet, quiet) {
    const block = $('sheet-block'); if (!block) return;
    const hash = state.report.file_hash, request = state.gridRequest = (state.gridRequest || 0) + 1;
    if (!sheet) { // open the sheet with the most to look at
      const m = marks(), names = (state.report.sheets || []).map(x => x.name);
      sheet = Object.entries(m.perSheet).sort((a, b) => b[1] - a[1])[0]?.[0] || (state.report.dataset_selection?.authoritative || []).find(n => names.includes(n)) || names.find(n => (state.report.sheets.find(x => x.name === n).tables || []).length) || names[0];
    }
    try {
      const g = await api('/api/spreadsheets/sheet', {file_hash: hash, sheet});
      if (state.selected !== hash || state.gridRequest !== request) return;
      state.grid = {...g, hash}; state.gridSheet = g.sheet; { const box = document.querySelector('#side .fd-comparison'); if (box && state.report?.file_hash === hash) box.replaceWith(fdComparison(state.report)); } if (!quiet) drawGrid(!(state.sheetScroll || {})[g.sheet]);
    } catch (e) {   // a failed sheet keeps the one on screen and says what went wrong
      if (state.selected !== hash || state.gridRequest !== request) return;
      if (state.grid) { drawGrid(); toast(`Could not open "${sheet}": ${e.message}`, true); } else block.replaceChildren(h('p', {class: 'note', role: 'alert'}, `Could not open "${sheet}": ${e.message}`)); }
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
    const contextRow = cur?.row || splitCell(cur?.cell || '')?.[1];
    const relation = !cur || !contextRow ? '' : cur.sheet !== sheet ? ' (on another sheet)' : contextRow > g.height || (only && !only.has(contextRow)) ? ' (outside the visible rows)' : '';
    const contextText = cur ? `Correction retained: ${cur.sheet || 'Report'} · ${cur.cell || (cur.row ? 'row ' + cur.row : 'report-level')}${relation}. ` : 'No correction focused. ';
    const contextSlot = $('fd-correction-context'); if (contextSlot) contextSlot.textContent = contextText + 'Filtering does not change this correction or your draft.';
    const focus = cur && cur.sheet === sheet ? {cell: cur.cell, row: cur.row} : state.gridRow ? {row: state.gridRow} : null;
    const {table, first} = buildGrid(g, m, {onlyRows: only, selected: state.gridCell, focus});
    table.style.fontSize = (state.gridZoom || 100) / 100 * 13 + 'px';
    // the workbook's own links stay clickable (http, https and mailto only); its pictures show where they are anchored
    for (const [ref, ln] of Object.entries(g.links || {})) {
      const td = table.querySelector(`td[data-cell="${ref}"]`); if (!td) continue;
      const text = td.textContent || ln.url;
      td.replaceChildren(ln.safe ? h('a', {href: ln.url, target: '_blank', rel: 'noopener noreferrer', class: 'cell-link', title: ln.url}, text)
        : h('span', {class: 'cell-link unsafe', title: `Not opened: ${ln.url.slice(0, 120)} is not a web link`}, text));
    }
    const mediaUrl = p => `/api/spreadsheets/media?id=${state.report.file_hash}&media=${encodeURIComponent(p.media)}`;
    for (const p of g.images || []) {
      const td = p.cell && table.querySelector(`td[data-cell="${p.cell}"]`); if (!td || p.missing) continue;
      td.classList.add('has-pic');
      td.append(h('a', {href: mediaUrl(p), target: '_blank', rel: 'noopener', class: 'cell-pic', title: `Picture at ${p.cell}${p.item ? ' · SharePoint item ' + p.item : ''}${p.historical ? ' · reference only' : ''}`},
        h('img', {src: mediaUrl(p), alt: `Photo at ${sheet} ${p.cell}${p.item ? ', item ' + p.item : ''}`, loading: 'lazy'})));
    }
    const draft = state.gridDraft;
    if (draft && gridCurrent(draft) && draft.sheet === sheet) {
      const td = [...table.querySelectorAll('td[data-cell]')].find(td => td.dataset.cell === draft.cell);
      if (td) {
        const input = h('input', {id:'grid-cell-input',type:'text',value:draft.after,'aria-label':`New value in spreadsheet ${sheet}!${draft.cell}`,
          oninput:e => { draft.after=e.target.value; draft.previewing=false; const button=$('grid-preview'); if(button) button.disabled=!gridDirty() || !draft.included; },
          onkeydown:e => { if(e.key === 'Enter') { e.preventDefault(); $('grid-preview')?.focus(); } if(e.key === 'Escape') { e.preventDefault(); $('grid-preview')?.focus(); } }});
        td.replaceChildren(input); td.classList.add('grid-editing');
      }
    }
    table.addEventListener('keydown', e => { if (e.target.matches('td[data-cell]') && ['Enter',' '].includes(e.key)) { e.preventDefault(); e.target.click(); } });
    table.querySelectorAll('td[data-cell]').forEach(td => td.setAttribute('tabindex','0'));
    const flagged = Object.entries(m.cells).filter(([k, l]) => k.startsWith(sheet + '!') && l.some(x => x.kind === 'issue')).length + Object.keys(m.rows).filter(k => k.startsWith(sheet + '!')).length;
    const tabs = h('div', {class: 'fd-tabs', role: 'tablist', 'aria-label': 'Sheets'}, (g.sheets || []).map(n => h('button', {type: 'button', role: 'tab', class: n === sheet ? 'on' : '', 'aria-selected': String(n === sheet),
      onclick: e => { if (n === sheet) return; fdKeepScroll(); state.gridCell = null; state.gridRow = null; e.currentTarget.classList.add('loading'); e.currentTarget.setAttribute('aria-busy', 'true');
        block.querySelector('.gridwrap')?.classList.add('loading'); loadGrid(n); }}, n, m.perSheet[n] ? h('i', {'aria-label': `${m.perSheet[n]} flagged`}, String(m.perSheet[n])) : null)));
    table.addEventListener('click', e => {
      if (e.target.closest('input,button,a')) return;
      const td = e.target.closest('td[data-cell]'); if (!td) return;
      const addr = td.dataset.cell, row = splitCell(addr)[1];
      const hit = (state.fdItems || []).find(x => x.sheet === sheet && x.cell === addr) || (state.fdItems || []).find(x => x.sheet === sheet && !x.cell && x.row === row);
      state.gridCell = addr; state.gridRow = row;
      if (fdFreeActive()) { fdFreePick(sheet, addr); return; }
      if (hit) { if (state.sideTab !== 'fixes') state.sideTab = 'fixes'; fdOpen(hit, addr); return; }
      state.gridRequest = (state.gridRequest || 0) + 1; state.fdOpenRequest = (state.fdOpenRequest || 0) + 1;
      state.gridCell = addr; state.gridRow = row; state.fdCur = null; drawGrid(); if (state.sideTab === 'fixes') drawSide();
    });
    const old = block.querySelector('.gridwrap');
    const keep = old && !scroll && old.dataset.sheet === sheet ? [old.scrollTop, old.scrollLeft] : (state.sheetScroll || {})[sheet] || null;
    const scroller = h('div', {class: 'gridwrap', 'data-sheet': sheet, tabindex: '0', 'aria-label': `Sheet ${sheet}`}, table);
    const search = h('input', {type: 'search', value: state.gridSearch || '', placeholder: 'Find a record, part or value…', 'aria-label': 'Find in this sheet',
      oninput: e => { const report = state.report, grid = state.grid; state.gridSearch = e.target.value; clearTimeout(state.gridSearchT); state.gridSearchT = setTimeout(() => { if (state.report !== report || state.grid !== grid) return; drawGrid(true); const i = document.querySelector('.fd-search input'); if (i) { i.focus(); i.setSelectionRange(i.value.length, i.value.length); } }, 200); }});
    const empty = only && !only.size;
    block.replaceChildren(
      h('div', {class: 'fd-toolbar'},
        h('label', {class: 'fd-search'}, icon('search', 16), search),
        h('label', {class: 'fd-check'}, h('input', {type: 'checkbox', checked: onlyChanged, onchange: e => { state.gridOnlyMarked = e.target.checked; drawGrid(true); }}), ` Only exceptions (${markedRows.size} row${markedRows.size === 1 ? '' : 's'})`),
        h('label', {class: 'fd-zoom'}, 'Zoom ', h('select', {'aria-label': 'Sheet zoom', onchange: e => { state.gridZoom = Number(e.target.value); drawGrid(); }},
          [80, 100, 125, 150].map(z => h('option', {value: String(z), selected: (state.gridZoom || 100) === z ? true : null}, z + '%')))),
        state.fdAsideHidden ? h('button', {type: 'button', class: 'btn small', onclick: () => { state.fdAsideHidden = false; $('fd-work')?.classList.remove('aside-hidden'); drawSide(); }}, icon('panel', 15), ' Show corrections') : null),
      gridDraftControls(),
      !state.gridDraft && state.gridCell ? h('button', {type:'button',class:'btn',id:'grid-edit-cell',onclick:() => gridStartEdit(sheet,state.gridCell)}, `Edit this cell (${sheet}!${state.gridCell})`) : null,
      h('p', {class: 'fd-grid-context', role: 'status'}, `${table.querySelectorAll('tbody tr').length} rows shown on ${sheet}. `,
        contextText,
        'Filtering does not select a correction or change your draft. Click a flagged cell to inspect it.'),
      empty ? h('div', {class: 'fd-empty'}, h('b', {}, q ? 'No matching rows' : 'No exceptions on this sheet'), h('p', {}, q ? `Nothing on "${sheet}" contains “${state.gridSearch}”.` : 'Every row on this sheet is clear.')) : scroller,
      (g.images || []).length ? h('details', {class: 'fd-gallery', open: (g.images || []).length <= 6 || null},
        h('summary', {}, `Pictures on this sheet: ${g.image_summary.placements} placed, ${g.image_summary.unique} unique${g.image_summary.missing ? `, ${g.image_summary.missing} missing from the file` : ''}`),
        h('ul', {}, g.images.map(p => h('li', {class: p.historical ? 'historical' : ''},
          p.missing ? h('span', {class: 'pic-missing'}, 'Missing from the file') : h('a', {href: mediaUrl(p), target: '_blank', rel: 'noopener'}, h('img', {src: mediaUrl(p), alt: `Photo at ${sheet} ${p.cell}`, loading: 'lazy'})),
          h('span', {}, h('b', {}, p.cell || 'Floating'), p.item ? ` · item ${p.item}` : ' · no record', p.field ? ` · ${p.field}` : '', ` · ${p.purpose}`, p.business_date ? ` · ${p.business_date}` : ''),
          p.historical ? h('small', {}, p.note) : null)))) : null,
      tabs,
      h('div', {class: 'legend'},
        h('span', {class: 'k iss high'}, `Flagged (${flagged})`),
        h('span', {class: 'k fixme'}, 'Suggested change'),
        h('span', {class: 'k ready'}, 'Selected for the new version'),
        h('span', {class: 'k fx'}, 'Formula'),
        g.truncated ? h('span', {class: 'note'}, 'Large sheet: only the first rows and columns are shown.') : null));
    const gallery = block.querySelector('.fd-gallery');
    const galleryHost = $('fd-gallery-host');
    if (galleryHost) galleryHost.replaceChildren(...(gallery ? [gallery] : []));
    if (keep && !empty) { scroller.scrollTop = keep[0]; scroller.scrollLeft = keep[1]; }
    else if (scroll && !empty) requestAnimationFrame(() => reveal(scroller, first, false));
    drawCellSlot();
  }

  // ---------------- Dex: read-only questions, bound to the displayed report/check
  // Conversation stays in memory, never in localStorage, and never selects corrections.
  function maybeBrief(hash, working) {
    if (state.report?.file_hash === hash && !working) drawChat();
  }
  function openChat() { showSide('claude'); }
  function closeChat() { if (state.sideTab === 'claude') showSide('fixes'); }
  function chatViewKey(r) {
    return r ? JSON.stringify([r.file_hash, r.check_hash, r.version, r.revision, r.assignment, r.holds, r.finding_decisions_rev, r.review_decisions, r.accepted_issues]) : '';
  }
  function chatState() {
    const r = state.report, key = chatViewKey(r);
    state.dexDrafts ||= {};
    if (!state.reportChat || state.reportChat.key !== key) state.reportChat = {key, messages: [], draft: state.dexDrafts[key] || '', busy: false, error: ''};
    return state.reportChat;
  }
  async function askDex(chat, binding) {
    const r = state.report;
    if (chat !== state.reportChat || chat.key !== chatViewKey(r) || chat.busy || !state.aiOn || !r || state.selected !== binding.file_hash || r.file_hash !== binding.file_hash || r.check_hash !== binding.expected_check_hash) return;
    const question = chat.draft.trim(); if (!question || question.length > 4000) return;
    chat.busy = true; chat.error = ''; delete state.dexDrafts[chat.key]; drawChat();
    const conversation = [...chat.messages.slice(-14), {role: 'user', content: question}];
    try {
      const out = await api('/api/spreadsheets/chat', {...binding, messages: conversation, expected_context_hash: chat.contextHash || null});
      if (chat !== state.reportChat || chat.key !== chatViewKey(state.report) || state.selected !== binding.file_hash || state.report?.file_hash !== binding.file_hash || state.report?.check_hash !== binding.expected_check_hash) return;
      if (out.read_only !== true || out.file_hash !== binding.file_hash || out.check_hash !== binding.expected_check_hash || typeof out.reply !== 'string' || !out.reply.trim()) throw new Error('Dex returned an answer for different report evidence. Reload before asking again.');
      chat.messages = [...conversation, {role: 'assistant', content: out.reply}]; chat.draft = ''; delete state.dexDrafts[chat.key];
      chat.truncated = out.context_truncated; chat.provider = out.provider; chat.contextHash = out.context_hash;
    } catch (e) {
      if (chat === state.reportChat && state.selected === binding.file_hash && state.report?.check_hash === binding.expected_check_hash) { chat.error = e.message; state.dexDrafts[chat.key] = chat.draft; }
    } finally {
      chat.busy = false;
      if (chat === state.reportChat && state.selected === binding.file_hash && state.report?.check_hash === binding.expected_check_hash) drawChat();
    }
  }
  function drawChat() {
    const panel = $('chat'); if (!panel || !state.report) return;
    const r = state.report, chat = chatState(), available = !!state.aiOn && !!r.check_hash;
    const binding = {file_hash: r.file_hash, expected_check_hash: r.check_hash};
    const send = h('button', {type: 'button', class: 'btn primary', disabled: !available || chat.busy || !chat.draft.trim(), onclick: () => askDex(chat, binding)}, chat.busy ? 'Asking Dex…' : 'Send');
    const input = h('textarea', {rows: 3, maxlength: '4000', disabled: !available || chat.busy, 'aria-label': 'Message to Dex', placeholder: available ? 'Ask about a finding, quantity or source hold…' : 'Ask an administrator to configure Claude.', oninput: e => { chat.draft = e.target.value; state.dexDrafts[chat.key] = chat.draft; const keys = Object.keys(state.dexDrafts); if (keys.length > 10) delete state.dexDrafts[keys[0]]; send.disabled = !available || chat.busy || !chat.draft.trim(); }});
    input.value = chat.draft;
    panel.replaceChildren(
      h('div', {class: 'chat-top'}, h('b', {}, 'Ask Dex about this report')),
      h('div', {class: 'chat-hello'},
        h('p', {}, `${r.filename || 'Selected report'} · file version ${r.version ?? 'unknown'} · revision ${r.revision ?? 'unknown'} · ${r.file_hash.slice(0, 12)}`),
        h('p', {}, available
          ? 'Sending shares your question, summary and relevant findings, referenced cells and column headers from this report version with Dex (AI by Anthropic). Other cells and sheets are not sent. Answers explain evidence only; they cannot change, approve or send a report.'
          : 'Dex is unavailable because AI preparation is not configured. Ask an administrator to configure Dex on the server. Do not paste an API key here.'),
        h('p', {}, 'Check answers against the report. Blank values stay unknown and source-review holds remain in place. This conversation is not saved after reload.'),
        h('button', {type: 'button', class: 'btn', onclick: () => { showSide('fixes'); document.querySelector('.fd-mode-selector')?.scrollIntoView({behavior: 'smooth', block: 'center'}); }}, 'Go to review modes')),
      h('div', {class: 'dex-messages', 'aria-live': 'polite'}, chat.messages.map(m => h('div', {class: 'dex-message'}, h('b', {}, m.role === 'user' ? 'You' : 'Dex'), h('p', {}, m.content)))),
      chat.truncated ? h('p', {class: 'hint'}, 'Dex received only relevant evidence excerpts, not the full workbook. This answer does not establish complete report coverage.') : null,
      chat.error ? h('p', {class: 'hint', role: 'alert'}, chat.error) : null,
      h('label', {for: 'dex-question'}, 'Your question'), input, send,
      h('p', {class: 'hint', role: 'status'}, chat.busy ? 'Waiting for Dex. Your report is unchanged.' : 'No changes are selected or applied by chat.'));
    input.id = 'dex-question';
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
  function downloadFinal(c) { $('finish-dialog')?.open && $('finish-dialog').close(); downloadWithChanges(c); }
  function sendFinal(c) {
    if (!c?.binding || c.binding.file_hash !== state.report?.file_hash) return;
    $('finish-dialog')?.open && $('finish-dialog').close();
    if (!c.total) { openSend(); return; }
    state.openSendFor = 'next';                      // the corrected copy opens with the send window
    fdPreview(c);
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
    const r = state.report, status = fdStatus(r), handled = p.items || (state.fixes || []).length + walkIssues().length;
    const nextRep = visibleList().find(x => x.file_hash !== r.file_hash && x.review && x.review.status !== 'clear' && !x.review.sent_at && inClientR(x));
    dlg.replaceChildren(h('div', {class: 'fixer-wait celebrate'},
      h('h2', {id: 'finish-title'}, 'Selection review finished'),
      h('p', {class: 'wins'}, handled ? `You reviewed ${handled} visible items on ${r.review.job || r.filename}.` : 'No visible corrections are selected.'),
      h('p', {class: 'fd-coverage'}, `${status.text}. ${status.why || 'Review source coverage and holds before internal approval.'}`),
      h('p', {}, (p.saved ? `${p.saved} change${p.saved === 1 ? ' is' : 's are'} saved in this version, and it has been checked again. ` : p.ready ? `${p.ready} selected change${p.ready === 1 ? ' has' : 's have'} not been applied yet. ` : 'No selected changes are pending. ') + 'Next: review the selected changes, approve internally when ready, or download this version. External sending is paused.'),
      h('div', {class: 'row'},
        nextRep ? h('button', {type: 'button', class: 'btn ghost', onclick: () => { dlg.close(); select(nextRep.file_hash); }}, 'Next report ›') : h('button', {type: 'button', class: 'btn ghost', onclick: () => dlg.close()}, 'Not now'),
        h('button', {type: 'button', class: 'btn', onclick: correctionAction(downloadFinal)}, 'Review or download this version'),
        status.next === 'approve' && !p.ready ? h('button', {type: 'button', class: 'btn primary', onclick: correctionAction(sendFinal)}, 'Approve internally…') : h('button', {type: 'button', class: 'btn primary', onclick: () => dlg.close()}, 'Back to review'))));
    if (!dlg.open) dlg.showModal();
  }

  // ---------------- recommendations: next steps from the checks and Claude's analysis, with one-click actions
  const PRI = {high: 'Do first', medium: 'Soon', low: 'When you can'};
  function recsBlock(recs, {ask, act, scope}) {
    if (!recs) return null;
    const items = recs.items || [];
    const busy = h('span', {class: 'note recsbusy', hidden: true}, h('span', {class: 'spin'}), ' Dex is reading the results…');
    const askBtn = recs.ai_on ? h('button', {type: 'button', class: 'btn small', onclick: async e => {
      e.target.disabled = true; busy.hidden = false;
      try { await ask(); } catch (err) { toast(err.message, true); e.target.disabled = false; busy.hidden = true; } }},
      recs.source === 'ai' ? 'Ask Dex again' : 'Ask Dex for recommendations') : null;
    const src = recs.source === 'ai' ? `From Dex's analysis of ${scope}, ${ago(recs.made_at)}.` : `From the checks on ${scope}.` + (recs.ai_on ? ' Ask Dex for a deeper read.' : '');
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
      send_for_review: {label: 'Request internal review', run: () => { reviewForm('request'); scrollTo('reviewstate')(); }},
      approve_send: {label: 'Internal approval details', run: openSend},
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
        h('button', {type: 'button', class: 'btn small', onclick: () => reviewForm('send_back')}, 'Record changes needed'),
        h('button', {type: 'button', class: 'btn ghost small', onclick: link}, 'Copy link for the reviewer')));
    if (rr.status === 'sent_back') return h('div', {class: 'reviewstate back', id: 'reviewbox'},
      h('div', {}, h('b', {}, `Sent back by ${rr.reviewer}`), h('span', {}, `“${rr.comment}” (${ago(rr.answered_at)})`)),
      h('div', {class: 'btns'}, h('button', {type: 'button', class: 'btn small', onclick: () => reviewForm('request')}, 'Request internal review again')));
    return null;
  }
  function reviewForm(action) {
    const slot = $('reviewstate'); if (!slot) return;
    const who = h('input', {type: 'text', placeholder: 'Your name', value: myName(), 'aria-label': 'Your name'});
    const note = h('textarea', {rows: 2, placeholder: action === 'request' ? 'Note for the reviewer (optional), e.g. “Reviewed the two repeated identifiers; please check the totals.”' : 'What needs changing?', 'aria-label': 'Note'});
    const go = async () => {
      try {
        store.set('rd-approver', who.value.trim());
        const out = await api('/api/spreadsheets/review', {file_hash: state.report.file_hash, action, by: who.value, note: note.value});
        state.report = {...state.report, ...out.item, review: {...state.report.review, review_state: out.item.review_request?.status}};
        toast(action === 'request' ? 'Internal review requested. No notification was sent. Copy the link to contact the reviewer yourself.' : 'Changes-needed comment recorded. No notification was sent.');
        drawReport(); loadList();
      } catch (e) { toast(e.message, true); }
    };
    slot.replaceChildren(h('div', {class: 'reviewstate' + (action === 'send_back' ? ' back' : '')},
      h('div', {class: 'reviewform'}, h('b', {}, action === 'request' ? 'Request internal review (no notification sent)' : 'Record a return-for-changes comment'), who, note,
        h('div', {class: 'btns'}, h('button', {type: 'button', class: 'btn ghost small', onclick: () => slot.replaceChildren(...[reviewState(state.report)].filter(Boolean))}, 'Cancel'),
          h('button', {type: 'button', class: 'btn primary small', onclick: go}, action === 'request' ? 'Request internal review' : 'Record changes needed')))));
    note.focus();
  }

  // ---------------- executive overview across every job
  const statisticNumber = value => value == null || (typeof value === 'number' && !Number.isFinite(value)) ? 'Unknown' : String(value);
  const statisticFindings = (totals, field = 'caught') => statisticNumber(totals[field]) + (totals[field + '_complete'] === false ? ' (basis incomplete or uncertain)' : '');
  function statisticQuantity(totals, field) {
    return `${totals[field + '_range_exceeded'] ? 'Unknown: total exceeds the supported numeric range. ' : ''}Known subtotal: ${statisticNumber(totals[field + '_known'])} · ${statisticNumber(totals[field + '_complete_reports'])} complete reports · ${statisticNumber(totals[field + '_missing_reports'])} missing/partial · ${statisticNumber(totals[field + '_incompatible_reports'])} incompatible units`;
  }
  const statisticSource = row => h('span', {}, row.job, h('small', {}, `Source scope: ${row.source_label || 'Source label unavailable'}`), h('details', {onclick: e => e.stopPropagation(), onkeydown: e => e.stopPropagation()}, h('summary', {}, 'Exact source scope'), h('small', {}, row.source || 'Unknown')));
  function statisticNotes(data) {
    const definitions = data.statistics?.definitions || {}, coverage = data.statistics?.coverage || {};
    return h('details', {class: 'statistics-notes'}, h('summary', {}, 'Definitions and data coverage'),
      h('p', {}, 'Findings on first uploads uses their current reviews, not an immutable first-check history. Findings may describe the same underlying problem. An incomplete or uncertain basis is not a lower-bound count. Client exposure is unknown.'),
      h('p', {}, 'Quantities are reported by selected versions, not source-verified totals. Job navigation combines source groups; source-scoped rows remain separate below.'),
      h('dl', {}, ...Object.entries(definitions).flatMap(([key, text]) => [h('dt', {}, key.replaceAll('_', ' ')), h('dd', {}, text)])),
      h('p', {}, data.statistics?.coverage_scope || 'Coverage scope unavailable; exclusions are not the selected scope denominator.'),
      h('p', {}, Object.entries(coverage).map(([key, value]) => `${key.replaceAll('_', ' ')}: ${statisticNumber(value)}`).join(' · ') || 'Coverage metadata unavailable.'));
  }
  async function showOverview() {
    const wrap = h('div', {class: 'sheetwrap apiwrap'});
    $('report').replaceChildren(wrap);
    state.ovDays ||= 30;
    wrap.replaceChildren(h('p', {class: 'note'}, h('span', {class: 'spin'}), ' Adding up every report…'));
    let o;
    try { o = state.ovRecs && state.ovRecs.days === state.ovDays ? state.ovRecs : await api('/api/overview', {days: state.ovDays}); state.ovRecs = null; } catch (e) { wrap.replaceChildren(h('p', {class: 'note'}, e.message)); return; }
    if (location.hash !== '#overview') return;
    const t = o.totals, top = Math.max(1, ...o.categories.map(c => c.count));
    const pct = v => v == null ? 'Unknown' : v.toFixed(v < 10 ? 1 : 0) + '%';
    wrap.replaceChildren(...[
      h('div', {class: 'head'}, h('div', {}, h('h1', {}, 'Quality overview'), h('div', {class: 'meta'}, `Every job: ${o.label.replace(/^Last/, 'the last')}. Findings on first uploads, selected-version quantities, and recorded sends.`))),
      h('div', {class: 'periodbar'}, h('div', {class: 'pseg'}, [7, 30, 90, 365].map(d => h('button', {type: 'button', class: state.ovDays === d ? 'on' : '', onclick: () => { state.ovDays = d; showOverview(); }}, `${d} days`))),
      ),
      h('div', {class: 'ovhero'}, h('b', {}, statisticFindings(t)), h('span', {}, `Findings on first uploads · current reviews across ${fmt(t.reports)} daily reports and ${fmt(t.jobs)} job/source groups. ${fmt(t.must_fix)} high-priority findings. This does not establish what clients saw.`)),
      h('div', {class: 'numbers five'},
        h('div', {}, h('b', {}, fmt(t.corrected)), h('span', {}, 'Reports with saved corrected copies')),
        h('div', {}, h('b', {}, `${fmt(t.sent)} of ${fmt(t.reports)}`), h('span', {}, 'Sent from the verifier')),
        h('div', {}, h('b', {}, pct(t.on_time_pct)), h('span', {}, 'Sent on time')),
        h('div', {}, h('b', {}, t.turnaround == null ? 'Unknown' : `${t.turnaround} h`), h('span', {}, 'Upload to sent')),
        h('div', {}, h('b', {class: t.open ? 'bad' : ''}, fmt(t.open)), h('span', {}, 'Still on Hold'))),
      h('details', {class: 'statistics-notes'}, h('summary', {}, `Inspected quantity: ${statisticNumber(t.inspected)} · Rejected quantity: ${statisticNumber(t.rejected)} · Reject rate: ${pct(t.rate)}`),
        ...['inspected', 'rejected', 'certified'].map(field => h('p', {}, `${field}: ${statisticNumber(t[field])} · ${statisticQuantity(t, field)}`))),
      h('p', {class: 'note'}, `Selected-version findings: ${statisticFindings(t, 'current_issues')}. Recorded changes: ${statisticNumber(t.changes)}. Client exposure remains unknown.`),
      statisticNotes(o),
      recsBlock(o.recs, {scope: 'every job', ask: async () => { const n = await api('/api/overview', {days: o.days, ask_ai: true}); if (location.hash === '#overview') { state.ovRecs = n; showOverview(); } },
        act: rec => periodAct(rec, job => { state.periodQuery = {kind: o.days > 7 ? 'month' : 'week', day: o.end, job: job || ''}; location.hash = 'periods'; })}),
      h('section', {class: 'block'}, h('header', {}, h('h2', {}, 'Finding categories'), h('p', {}, 'Listed findings in current reviews of first uploads, one per source/job/day. Categories use the same population as the headline.')),
        o.categories.length ? h('div', {class: 'ovbars'}, o.categories.map(c => h('div', {class: 'ovbar'}, h('span', {}, c.reason), h('div', {}, (() => { const i = h('i'); i.style.width = `${c.count / top * 100}%`; return i; })()), h('em', {}, fmt(c.count)))))
          : h('p', {class: 'note'}, 'No listed findings in this period; check coverage before interpreting this as complete.')),
      h('section', {class: 'block'}, h('header', {}, h('h2', {}, 'By job/source group')),
        h('div', {class: 'tablewrap'}, h('table', {class: 'keys pdays'},
          h('thead', {}, h('tr', {}, ['Job', 'Days', 'Inspected', 'Reject rate', 'Findings on first uploads', 'Corrected', 'Sent', 'On time', 'Upload to sent', ''].map(x => h('th', {}, x)))),
          h('tbody', {}, o.jobs.map(j => h('tr', {}, h('td', {}, statisticSource(j)), h('td', {}, fmt(j.days)), h('td', {}, statisticNumber(j.inspected), h('small', {}, statisticQuantity(j, 'inspected'))), h('td', {}, pct(j.rate)),
            h('td', {}, statisticFindings(j)), h('td', {}, fmt(j.corrected)), h('td', {}, `${fmt(j.sent)} of ${fmt(j.days)}`), h('td', {}, fmt(j.on_time)), h('td', {}, j.turnaround == null ? 'Unknown' : `${j.turnaround} h`),
            h('td', {}, h('button', {type: 'button', class: 'btn small', onclick: () => { state.periodQuery = {kind: o.days > 7 ? 'month' : 'week', day: o.end, job: j.job}; location.hash = 'periods'; }}, 'View job reports')))))))),
      !t.reports ? h('p', {class: 'note'}, 'No eligible daily reports in this period. See data coverage for excluded or ambiguous records.') : null,
      changeRecords(),
    ].filter(Boolean));
  }

  // ---------------- KPI dashboard: every client, drill down to a client, then a job
  function kpiChart(points, opts) {
    // one series, one axis; bars in the app green, hover any bar for its value
    const W = 360, H = 140, pad = {l: 34, r: 6, t: 8, b: 20};
    const vals = points.filter(p => p.v != null).map(p => p.v), raw = Math.max(...vals, 0);
    const max = opts.pct ? Math.max(opts.min || 1, raw) : Math.max(1, Math.ceil(raw));
    const step = (W - pad.l - pad.r) / Math.max(points.length, 1), bw = Math.max(1, Math.min(18, step - (step > 4 ? 2 : 0)));
    const y = v => pad.t + (H - pad.t - pad.b) * (1 - v / max), NS = 'http://www.w3.org/2000/svg';
    const el = (tag, attrs, text) => { const e = document.createElementNS(NS, tag); for (const k in attrs) e.setAttribute(k, attrs[k]); if (text != null) e.textContent = text; return e; };
    const svg = el('svg', {viewBox: `0 0 ${W} ${H}`, class: 'kchart', role: 'img', 'aria-label': `${opts.label}. ${points.filter(p => p.v == null).length} unknown values, marked with a question mark.`});
    const ticks = opts.pct || max >= 4 ? [0, max / 2, max] : [0, max];
    ticks.forEach(t => { svg.append(el('line', {x1: pad.l, x2: W - pad.r, y1: y(t), y2: y(t), class: 'grid'}), el('text', {x: pad.l - 6, y: y(t) + 3, class: 'ax', 'text-anchor': 'end'}, opts.fmt(t, true))); });
    points.forEach((p, i) => {
      if (p.v == null) { const missing = el('g', {class: 'unknown'}); missing.append(el('text', {x: pad.l + i * step + step / 2, y: H - pad.b - 5, 'text-anchor': 'middle', class: 'ax'}, '?'), el('title', {}, `${p.label}: Unknown`)); svg.append(missing); return; }
      const x = pad.l + i * step + (step - bw) / 2, v = p.v, top = y(v), hgt = H - pad.b - top;
      const g = el('g', {class: 'bar' + (opts.bad && v > 0 ? ' bad' : '')});
      g.append(el('rect', {x: pad.l + i * step, y: pad.t, width: step, height: H - pad.t - pad.b, class: 'hit'}));
      if (v > 0) g.append(el('path', {d: `M${x},${H - pad.b}V${top + Math.min(4, hgt / 2)}q0,-${Math.min(4, hgt / 2)} ${Math.min(4, bw / 2)},-${Math.min(4, hgt / 2)}h${Math.max(0, bw - 2 * Math.min(4, bw / 2))}q${Math.min(4, bw / 2)},0 ${Math.min(4, bw / 2)},${Math.min(4, hgt / 2)}V${H - pad.b}Z`}));
      g.append(el('title', {}, `${p.label}: ${opts.fmt(p.v)}`));
      svg.append(g);
    });
    const lbl = [0, Math.floor((points.length - 1) / 2), points.length - 1].filter((v, i, a) => a.indexOf(v) === i);
    lbl.forEach(i => points[i] && svg.append(el('text', {x: pad.l + i * step + step / 2, y: H - 6, class: 'ax', 'text-anchor': i === 0 ? 'start' : i === points.length - 1 ? 'end' : 'middle'}, points[i].short)));
    return svg;
  }
  function kpiSeries(trend, days) {
    const md = d => { const [, m, dd] = d.split('-').map(Number); return `${['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'][m - 1]} ${dd}`; };
    // Retain canonical daily totals and coverage; do not sum partial or incompatible units in the browser.
    return trend.map(t => ({...t, label: md(t.day), short: md(t.day)}));
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
    const pct = v => v == null ? 'Unknown' : (v < 10 ? v.toFixed(1) : Math.round(v)) + '%';
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
      s.some(p => p[key] != null) ? kpiChart(s.map(p => ({v: p[key + '_complete'] === false ? null : p[key], label: p.label, short: p.short})), {label: title, ...o}) : h('div', {class: 'kzero'}, 'Unknown for this period; no complete values available'));
    const n0 = v => fmt(Math.round(v));
    const empty = !t.reports;
    const top = Math.max(1, ...k.categories.map(c => c.count));
    const tableRow = (cells, onclick, label) => h('tr', {class: onclick ? 'go' : '', tabindex: onclick ? '0' : null, title: label || null, onclick, onkeydown: onclick ? e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onclick(); } } : null}, cells.map((c, i) => h('td', {}, i === 0 ? h('b', {}, c) : c)));
    const heads = ['Reports', 'Findings on first uploads', 'Selected-version findings', 'Client exposure', 'Sent on time', 'Inspected', 'Rejected', 'Reject rate', 'On hold'];
    const cellsOf = r => [fmt(r.reports), statisticFindings(r), statisticFindings(r, 'current_issues'), 'Unknown', pct(r.on_time_pct), h('span', {}, statisticNumber(r.inspected), h('small', {}, statisticQuantity(r, 'inspected'))), h('span', {}, statisticNumber(r.rejected), h('small', {}, statisticQuantity(r, 'rejected'))), pct(r.rate), statisticNumber(r.hold)];
    const toPeriods = job => { state.periodQuery = {kind: k.days > 7 ? 'month' : 'week', day: k.end, job}; location.hash = 'periods'; };
    wrap.replaceChildren(...[
      h('div', {class: 'head'}, h('div', {}, h('h1', {}, 'KPIs'), h('div', {class: 'meta'}, `Reporting numbers for ${scope}, ${k.label}.`))),
      h('div', {class: 'kbar'},
        h('nav', {class: 'crumbs', 'aria-label': 'Drill down'}, ...crumbs),
        h('div', {class: 'kfilters'}, sel, h('div', {class: 'pseg'}, [[7, '7 days'], [30, '30 days'], [90, '90 days'], [365, '12 months']].map(([d, l]) => h('button', {type: 'button', class: q.days === d ? 'on' : '', 'aria-pressed': String(q.days === d), onclick: () => go({days: d})}, l))))),
      empty ? h('section', {class: 'block'}, h('p', {class: 'note'}, `No eligible daily reports for ${scope} in this period. Try a longer range${q.client ? ' or All clients' : ''}.`)) : null,
      empty ? null : h('div', {class: 'khero'},
        h('div', {}, h('b', {}, statisticFindings(t)), h('span', {}, 'Findings on first uploads'), h('em', {}, 'Current review of the first original per source/job/day; findings are not confirmed distinct errors.')),
        h('div', {}, h('b', {}, statisticFindings(t, 'current_issues')), h('span', {}, 'Selected-version findings'), h('em', {}, 'Client exposure: Unknown. No immutable at-send finding snapshots or evidence of client viewing.'))),
      statisticNotes(k),
      empty ? null : h('div', {class: 'ktiles'},
        tile(fmt(t.reports), 'Reports made', `${fmt(t.sent)} sent, ${fmt(t.unsent)} not sent yet`),
        tile(pct(t.on_time_pct), 'Sent on time', `${fmt(t.on_time)} on time · ${statisticNumber(t.on_time_unknown)} unknown timing · ${fmt(t.sent)} recorded sends`),
        tile(t.turnaround == null ? 'Unknown' : `${t.turnaround} h`, 'Upload to sent', `Median for ${statisticNumber(t.turnaround_reports)} reports with valid timestamps`),
        tile(fmt(t.jobs), q.job ? 'Job/source group' : 'Job/source groups', q.client ? null : plural(t.clients, 'client')),
        tile(statisticNumber(t.inspected), 'Inspected quantity', statisticQuantity(t, 'inspected')),
        tile(statisticNumber(t.certified), 'Certified quantity', statisticQuantity(t, 'certified')),
        tile(statisticNumber(t.rejected), 'Rejected quantity', `Reject rate ${pct(t.rate)} · ${statisticQuantity(t, 'rejected')}`),
        tile(fmt(t.hold), 'Still on hold', t.hold ? 'Not sent, needs a fix' : 'None', t.hold ? 'bad' : ''),
        tile(fmt(t.corrected), 'Reports with saved corrected copies', `${fmt(t.uploads)} uploads in all`)),
      empty ? null : h('div', {class: 'kcharts'},
        chartBlock('Findings on first uploads', 'Per day, current reviews', 'caught', {fmt: n0}),
        chartBlock('Selected-version findings', 'Per day, current reviews', 'current_issues', {fmt: n0}),
        chartBlock('Reports made', 'Per day; unknown points are not zero', 'reports', {fmt: n0}),
        chartBlock('Inspected quantity', 'Per day; unknown points are not zero', 'inspected', {fmt: statisticNumber}),
        chartBlock('Reject rate', 'Rejected out of inspected', 'rate', {pct: true, fmt: (v, ax) => ax ? (v < 10 ? v.toFixed(1) : Math.round(v)) + '%' : pct(v), min: 1})),
      empty || !k.categories.length ? null : h('section', {class: 'block'}, h('header', {}, h('h2', {}, 'Finding categories'), h('p', {}, `Current-review findings on first uploads for ${scope}, one per source/job/day.`)),
        h('div', {class: 'ovbars'}, k.categories.map(c => h('div', {class: 'ovbar', title: `${c.kind}: ${fmt(c.count)}`}, h('span', {}, c.kind), h('div', {}, (() => { const i = h('i'); i.style.width = `${c.count / top * 100}%`; return i; })()), h('em', {}, fmt(c.count)))))),
      !q.client && k.clients.length ? h('section', {class: 'block'}, h('header', {}, h('h2', {}, 'By client'), h('p', {}, 'Click a client to see its jobs.')),
        h('div', {class: 'tablewrap'}, h('table', {class: 'keys ktable'},
          h('thead', {}, h('tr', {}, ['Client', 'Jobs', ...heads].map(x => h('th', {}, x)))),
          h('tbody', {}, k.clients.map(c => tableRow([c.name, fmt(c.jobs), ...cellsOf(c)], () => go({client: c.key, job: ''}), `See ${c.name}`)))))) : null,
      q.client && !q.job && k.jobs.length ? h('section', {class: 'block'}, h('header', {}, h('h2', {}, `${clientName} by job`), h('p', {}, 'Rows are source-specific. View job numbers combines all eligible source groups for that job.')),
        h('div', {class: 'tablewrap'}, h('table', {class: 'keys ktable'},
          h('thead', {}, h('tr', {}, ['Job', ...heads].map(x => h('th', {}, x)))),
          h('tbody', {}, k.jobs.map(j => tableRow([statisticSource(j), ...cellsOf(j)], () => go({job: j.job}), `View job numbers for ${j.job}, across source groups`)))))) : null,
      q.job ? h('section', {class: 'block'}, h('header', {}, h('h2', {}, `More on ${q.job}`)),
        h('div', {class: 'btns'}, h('button', {type: 'button', class: 'btn primary small', onclick: () => toPeriods(q.job)}, 'View job reports'),
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
  function nexusCopyAllowed(n) {
    const fields = ['inspected', 'rejected', 'certified'], coverage = n.coverage || {};
    return n.status === 'compared' && coverage.complete === true && coverage.source_records != null &&
      coverage.placed_records === coverage.source_records && !n.unplaced && Array.isArray(n.shifts) && n.shifts.length > 0 &&
      fields.every(key => n.total?.[key] != null) && n.shifts.every(shift => fields.every(key => shift[key] != null));
  }
  function nexusTable(n) {
    const canCopy = nexusCopyAllowed(n);
    const copyText = 'Shift\tInspected\tRejected\tCertified\n' + (n.shifts || []).map(x => `${x.name}\t${x.inspected}\t${x.rejected}\t${x.certified}`).join('\n');
    const copy = () => { if (!canCopy) { toast('Nexus copy is unavailable while quantities or shift coverage are incomplete.', true); return; }
      navigator.clipboard?.writeText(copyText).then(() => toast('Captured shift totals copied. Confirm the capture covers the intended period before using them.'), () => toast('Clipboard unavailable. Review the displayed totals.')); };
    return h('div', {},
      h('div', {class: 'tablewrap'}, h('table', {class: 'keys nexus'},
        h('thead', {}, h('tr', {}, ['Nexus shift', 'Window', 'Scans', 'Inspected', 'Rejected', 'Certified'].map(x => h('th', {}, x)))),
        h('tbody', {}, n.shifts.map(x => h('tr', {}, h('td', {}, h('b', {}, x.name)), h('td', {}, `${hm12(x.start)} – ${hm12(x.end)}`),
          h('td', {class: 'num'}, fmt(x.scans)), h('td', {class: 'num'}, fmt(x.inspected)), h('td', {class: 'num'}, fmt(x.rejected)), h('td', {class: 'num'}, fmt(x.certified)))),
          h('tr', {class: 'tot'}, h('td', {}, h('b', {}, 'Day')), h('td', {}), ...['scans', 'inspected', 'rejected', 'certified'].map(k => h('td', {class: 'num'}, h('b', {}, fmt(n.total[k])))))))),
      h('p', {class: 'note', role: 'status'}, canCopy ? 'Captured records are placed and quantities are known. This does not confirm the shift has ended.' : 'Nexus totals held: quantity or shift coverage is incomplete. Unknown values are shown as a dash; copying is unavailable.'),
      h('p', {class: 'note'}, `Placed records: ${n.coverage?.placed_records ?? 'Unknown'} of ${n.coverage?.source_records ?? 'Unknown'} captured source records. Day totals include all source records, including unplaced ones.`),
      n.unplaced ? h('p', {class: 'note'}, `${n.unplaced} scan(s) had no time or fell outside every Nexus window.`) : null,
      h('button', {type: 'button', class: 'btn small', disabled: !canCopy, onclick: copy}, 'Copy captured Nexus totals'));
  }
  const hm12 = t => { const [H, M] = t.split(':').map(Number); return `${(H % 12) || 12}:${String(M).padStart(2, '0')} ${H < 12 ? 'AM' : 'PM'}`; };
  function nexusBlock(r) {
    const body = h('div', {}, r.built?.nexus ? nexusTable(r.built.nexus)
      : state.health?.sharepoint_configured === false ? h('p', {class: 'note'}, 'Connect SharePoint to split the day by scan time.')
      : h('button', {type: 'button', class: 'btn small', onclick: async e => {
          e.target.disabled = true; e.target.textContent = 'Reading the scan times…';
          try { body.replaceChildren(nexusTable(await api('/api/nexus', {file_hash: r.file_hash}))); } catch (err) { toast(err.message, true); e.target.disabled = false; e.target.textContent = 'Show Nexus numbers'; } }}, 'Show Nexus numbers'));
    return h('section', {class: 'block', id: 'nexus-block'}, h('header', {}, h('h2', {}, 'Nexus numbers'),
      h('p', {}, 'The day split into Nexus’s three shifts by scan time (plant time), for review against the captured source. Internal only; capture totals do not confirm shift completion.')), body);
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

  // ---------------- Today: loaded daily reports and source captures
  async function showToday() {
    const wrap = h('div', {class: 'sheetwrap apiwrap'});
    $('report').replaceChildren(wrap);
    state.todayDay ||= null;
    let data;
    const load = async () => {
      try { data = await api('/api/today', {day: state.todayDay}); } catch (e) { wrap.replaceChildren(h('p', {class: 'note'}, e.message)); return false; }
      return location.hash === '#today';
    };
    wrap.replaceChildren(h('p', {class: 'note'}, h('span', {class: 'spin'}), ' Loading this dashboard…'));
    if (!await load()) return;
    const sp = {};
    const draw = () => {
      const day = data.day, rows = data.jobs;
      const label = new Date(day + 'T12:00:00').toLocaleDateString('en-US', {weekday: 'long', month: 'long', day: 'numeric'});
      const counts = {send: 0, missing: 0, hold: 0};
      for (const r of rows) {
        const y = r.yesterday, s = sp[r.job];
        if (y && !y.sent_at) counts.send++;
        if (y && (y.status === 'hold' || reportVerification({review: y, verification: y.verification}).held)) counts.hold++;
        if (!y && s?.yesterday) counts.missing++;
      }
      const shift = d => { const x = new Date(day + 'T12:00:00'); x.setDate(x.getDate() + d); state.todayDay = x.toISOString().slice(0, 10); showToday(); };
      const prevDay = (() => { const x = new Date(day + 'T12:00:00'); x.setDate(x.getDate() - 1); return x.toISOString().slice(0, 10); })();
      const clock = iso => new Date(iso).toLocaleTimeString('en-US', {hour: 'numeric', minute: '2-digit'});
      const buildBtn = (job, d, label, force, primary) => h('button', {type: 'button', class: 'btn small' + (primary ? ' primary' : ''), onclick: e => buildReport(job, d, force, e.target)}, label);
      const repCell = (rep, scans, when, job) => {
        if (rep) {
          const [label, tone] = queueCheck({review: rep, verification: rep.verification});
          return h('td', {}, h('a', {href: '#r=' + rep.file_hash}, h('span', {class: 'tag tag-' + tone}, label)), ' ',
            h('span', {class: rep.sent_at ? 'ok' : 'warn'}, rep.sent_at ? 'Sent' : rep.approved ? (rep.approval_current ? 'Internally approved, not sent' : 'Approval out of date, not sent') : 'Not sent'));
        }
        const s = sp[job];
        if (when === 'today' && s?.today) {
          const rd = s.readiness || {};
          return h('td', {}, h('span', {class: 'buildcell'}, h('span', {class: 'quiet'}, rd.note || 'Provisional through this capture; shift end is not confirmed.'),
            rd.state === 'no_scans' ? null : buildBtn(job, day, 'Build captured report', false, true)));
        }
        if (scans) return h('td', {}, h('span', {class: 'buildcell'}, h('span', {class: 'differs'}, `Missing (${fmt(scans)} scans on SharePoint)`), data.sharepoint_on ? buildBtn(job, prevDay, 'Build it', true, true) : null));
        return h('td', {}, h('span', {class: 'quiet'}, when === 'today' ? (s?.readiness?.state === 'no_scans' || s?.today === 0 ? 'No scans in this capture' : 'Source capture not loaded') : '—'));
      };
      // This legacy dashboard is not the background check ledger. Keep its limited scope explicit.
      wrap.replaceChildren(
        h('div', {class: 'head'}, h('div', {}, h('h1', {}, 'Today'), h('div', {class: 'meta'}, 'Daily report status and captured SharePoint scans for the jobs in this view.'))),
        todaysWins(),
        h('p', {class: 'note today-scope'}, 'This dashboard shows jobs from recent reports and previously saved monitoring settings. It does not establish complete client or source coverage, or confirm that background checks have run.'),
        data.coverage?.capped ? h('p', {class: 'note', role: 'status'}, 'Only part of the saved report history was loaded. Jobs and report counts in this view may be incomplete.') : null,
        data.coverage?.discovery?.search_failed || data.coverage?.discovery?.unreadable_sites?.length ? h('p', {class: 'note', role: 'status'}, 'SharePoint discovery was incomplete. Unreadable or undiscovered sources are not zero activity.') : null,
        h('div', {class: 'periodbar'}, h('div', {class: 'pnav'}, h('button', {type: 'button', class: 'btn small', 'aria-label': 'Previous day', onclick: () => shift(-1)}, '‹'),
          h('b', {}, label), h('button', {type: 'button', class: 'btn small', 'aria-label': 'Next day', onclick: () => shift(1)}, '›'))),
        h('p', {class: 'note'}, 'Build a provisional report from the records captured so far. A gap in scans does not confirm shift completion. Review source coverage and quantities before internal approval; external sending remains paused.'),
        h('div', {class: 'numbers five'},
          h('div', {}, h('b', {}, fmt(rows.length)), h('span', {}, 'Jobs in this view')),
          h('div', {}, h('b', {class: counts.missing ? 'bad' : ''}, fmt(counts.missing)), h('span', {}, 'Yesterday’s reports missing')),
          h('div', {}, h('b', {class: counts.hold ? 'bad' : ''}, fmt(counts.hold)), h('span', {}, 'Yesterday on hold')),
          h('div', {}, h('b', {}, fmt(counts.send)), h('span', {}, 'Yesterday not sent')),
          h('div', {}, h('b', {}, rows.some(r => !sp[r.job] || sp[r.job].error || sp[r.job].today == null) ? 'Unknown' : fmt(rows.reduce((n, r) => n + sp[r.job].today, 0))), h('span', {}, 'Scans in this view'))),
        h('section', {class: 'block'}, h('header', {}, h('h2', {}, 'Jobs')),
          rows.length ? h('div', {class: 'tablewrap'}, h('table', {class: 'keys pdays'},
            h('thead', {}, h('tr', {}, ['Job', 'Yesterday’s report', 'Today so far', 'Today’s report', ''].map(x => h('th', {}, x)))),
            h('tbody', {}, rows.map(r => {
              const s = sp[r.job];
              const today = !data.sharepoint_on ? h('span', {class: 'quiet'}, 'SharePoint off') : s === undefined ? h('span', {class: 'quiet'}, h('span', {class: 'spin'}), ' checking…')
                : s?.error ? h('details', {class: 'source-read-error'}, h('summary', {}, 'Source check incomplete'), h('p', {class: 'quiet'}, s.error)) : !s?.found ? h('span', {class: 'quiet'}, 'No “Data #” list found')
                : s.today ? h('span', {}, h('b', {}, fmt(s.today)), ' scans', s.last ? ` · last ${new Date(s.last).toLocaleTimeString('en-US', {hour: 'numeric', minute: '2-digit', timeZone: 'America/Chicago'})}` : '',
                    Object.keys(s.shifts || {}).length ? h('span', {class: 'quiet'}, ' · ' + Object.entries(s.shifts).map(([k, v]) => `${k} ${fmt(v)}`).join(' · ')) : null)
                : h('span', {class: 'quiet'}, 'No scans yet');
              return h('tr', {},
                h('td', {}, h('b', {}, r.job)),
                repCell(r.yesterday, s?.yesterday, 'yesterday', r.job),
                h('td', {}, today),
                repCell(r.today, 0, 'today', r.job),
                h('td', {}, h('button', {type: 'button', class: 'btn small', onclick: () => { state.periodQuery = {kind: 'week', day, job: r.job}; location.hash = 'periods'; }}, 'Week')));
            })))) : h('p', {class: 'note'}, 'No jobs are loaded in this dashboard. This does not mean there are no clients, jobs, or source records.')));
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
      h('p', {class: 'dashintro'}, `Built from the report data. Ask for any change in plain words; Dex uses only these numbers and leaves out inspector names and scan times.${ctx.noun === 'slide show' ? ' Use the arrow keys or click to move between slides.' : ''}`),
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
          d.ai_on ? null : h('p', {class: 'ssource warn'}, `Changes by Dex need AI review turned on. You can still view and download this ${ctx.noun}.`),
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
      h('div', {class: 'head'}, h('div', {}, h('h1', {}, 'Weekly & monthly'), h('div', {class: 'meta'}, 'A job’s week or month: every daily report and SharePoint scan, checked together, with a dashboard and slide show you can shape by talking to Dex.'))),
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
          h('thead', {}, h('tr', {}, ['Day', 'Daily report', 'Record rows', 'Inspected', 'Rejected', 'Sent'].map(x => h('th', {}, x)))),
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
      const ruleText = h('input', {type: 'text', placeholder: 'e.g. Flag any VIN that does not start with HZA', 'aria-label': 'What Dex should look for', maxlength: '400'});
      const ruleJob = h('select', {'aria-label': 'Which jobs'}, h('option', {value: ''}, 'All jobs'), data.jobs.map(j => h('option', {value: j}, j)));
      const jobsWithOwn = Object.keys(data.shifts.jobs || {});
      const newJob = h('input', {type: 'text', list: 'job-list', placeholder: 'Job number, e.g. HANDS-HMMA-2219', 'aria-label': 'Job number'});
      wrap.replaceChildren(
        h('button', {type: 'button', class: 'back', onclick: () => { location.hash = ''; }}, '‹ All reports'),
        h('div', {class: 'head'}, h('div', {}, h('h1', {}, 'Checks & shifts'), h('div', {class: 'meta'}, 'What the verifier looks at, what else you want Dex to look for, and when each shift starts and ends.'))),
        h('section', {class: 'block'}, h('header', {}, h('h2', {}, 'What it checks')),
          h('div', {class: 'checkgroups'}, data.checks.map(g => h('div', {class: 'checkgroup'}, h('h3', {}, g.title), h('p', {class: 'note'}, g.when),
            h('ul', {}, g.items.map(t => h('li', {}, t))))))),
        h('section', {class: 'block'}, h('header', {}, h('h2', {}, 'Also look for'),
            h('p', {}, 'Tell Dex anything else to check. It reads this list every time it reviews a report, and its double-check will not dismiss what you asked for.')),
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
    return h('div', {class: 'shiftcard'}, h('b', {}, `${wrong.length} rows need shift review`),
      h('p', {class: 'note'}, 'Moving or deleting rows is paused until the full affected-cell plan can be reviewed.'),
      h('button', {type: 'button', class: 'btn', disabled: true}, 'Shift correction paused'));
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
        h('div', {class: 'head'}, h('div', {}, h('h1', {}, 'API'), h('div', {class: 'meta'}, 'Let other programs upload reports, read the results, apply reviewed cell corrections and approve, with the same safety rules as this screen. Sending is paused.'))),
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
      h('b', {}, last ? `Sent to the client ${when(last.at)}` : 'Internal approval recorded'),
      h('span', {}, last ? `To ${last.to.join(', ')}${last.cc.length ? `, Cc ${last.cc.length}` : ''}. Approved by ${a.by}.`
        : `By ${a.by}, ${when(a.at)}${a.overridden ? `, with ${a.open_must_fix} must-fix issue${a.open_must_fix === 1 ? '' : 's'} open` : ''}. Not sent yet.`),
      h('button', {type: 'button', class: 'btn small', onclick: () => openSend()}, last ? 'Details' : 'Approval details'));
  }
  async function openSend() {
    let dlg = $('send-dialog');
    if (!dlg) { dlg = h('dialog', {id: 'send-dialog', class: 'fixer send', 'aria-labelledby': 'send-title'}); document.body.append(dlg);
      dlg.addEventListener('click', e => { if (e.target === dlg && !state.sending) dlg.close(); }); }
    if (!dlg.open) dlg.showModal();
    const hash = state.report.file_hash;
    state.sendForm = null; state.sendConfirm = false;
    dlg.replaceChildren(h('div', {class: 'fixer-wait'}, h('span', {class: 'spin'}), h('h2', {id: 'send-title'}, state.report.approval ? 'Send to client' : 'Approve'),
      h('p', {}, 'Loading approval details…')));
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
    const f = state.sendForm ||= {...reviewBinding(r), to: info.to.join(', '), cc: info.cc.join(', '), subject: info.subject, body: info.body, name: info.attachment.name,
      reply: info.previous ? 'reply' : 'new', again: false, override: false, approver: myName(), note: ''};
    const bind = (key, attrs, tag = 'input') => { const el = h(tag, {...attrs, oninput: e => { f[key] = e.target.value; }}); el.value = f[key]; return el; };
    const check = (key, label) => h('label', {class: 'tick'}, h('input', {type: 'checkbox', checked: f[key], onchange: e => { f[key] = e.target.checked; drawSend(); }}), label);
    // step 1: approve
    let step1;
    if (approval) {
      step1 = h('div', {class: 'sstep done'}, h('div', {class: 'shead'}, h('span', {class: 'snum'}, '✓'), h('h3', {}, 'Internal approval recorded')),
        h('p', {}, `${approval.by} approved version ${approval.version} on ${when(approval.at)}.` + (approval.note ? ` Note: ${approval.note}` : '')
          + (approval.overridden ? ` It was approved with ${approval.open_must_fix} must-fix issue${approval.open_must_fix === 1 ? '' : 's'} open.` : '')),
        sentBefore.length ? null : h('button', {type: 'button', class: 'btn ghost small', onclick: () => approve(true)}, 'Withdraw approval'));
    } else {
      const warns = [];
      if (pending) warns.push(h('div', {class: 'swarn'}, h('p', {}, `${pending} change${pending === 1 ? ' is' : 's are'} ready but not in a corrected copy yet. Approve the corrected copy, not this one.`),
        h('button', {type: 'button', class: 'btn small', disabled: state.busy, onclick: correctionAction(c => { state.openSendFor = 'next'; dlg.close(); downloadWithChanges(c); })}, `Apply ${pending} change${pending === 1 ? '' : 's'} and continue`)));
      const unsure = needsCheck().total;
      if (unsure) warns.push(h('div', {class: 'swarn'}, h('p', {}, `${unsure} item${unsure === 1 ? '' : 's'} the app is not sure about ${unsure === 1 ? 'has' : 'have'} not been checked yet. A person has to accept, skip or mark ${unsure === 1 ? 'it' : 'each one'} reviewed first.`),
        h('button', {type: 'button', class: 'btn small', onclick: () => { dlg.close(); showSide('fixes'); }}, 'Check them now')));
      if (newer) warns.push(h('div', {class: 'swarn'}, h('p', {}, `A corrected copy of this report exists: "${newer.fixed_filename}".`),
        h('button', {type: 'button', class: 'btn small', onclick: () => { state.openSendFor = newer.fixed_file_hash; dlg.close(); select(newer.fixed_file_hash); }}, 'Open the corrected copy')));
      if (must) warns.push(h('div', {class: 'swarn high'}, h('p', {}, `${must} must-fix issue${must === 1 ? ' is' : 's are'} still open on this report.`), check('override', 'Approve anyway')));
      step1 = h('div', {class: 'sstep'}, h('div', {class: 'shead'}, h('span', {class: 'snum'}, '1'), h('h3', {}, 'Approve this exact version internally')),
        h('p', {}, `${rv.status === 'clear' ? 'Nothing is flagged.' : statusNote(rv) + '.'} Approval applies only to this version (${r.filename}, version ${r.version}).`),
        ...warns,
        h('div', {class: 'sgrid'}, h('label', {}, 'Approved by', bind('approver', {type: 'text', placeholder: 'Your name', autocomplete: 'name'})),
          h('label', {}, 'Note (optional)', bind('note', {type: 'text', placeholder: 'For example: checked against the app, 144 = 144'}))),
        h('button', {type: 'button', class: 'btn primary', disabled: !!state.sending || (must > 0 && !f.override) || (!!newer && !f.override) || pending > 0 || needsCheck().total > 0, onclick: () => approve(false)}, 'Record internal approval'));
    }
    const step2 = h('div', {class: 'sstep'}, h('h3', {}, 'Sending is paused'),
      h('p', {}, 'Sending and draft creation are unavailable while delivery safety checks are completed. Download the approved report for manual delivery.'));
    const history = (r.sent || []).length ? h('div', {class: 'shist'}, h('h3', {}, 'History'),
      ...[...r.sent].reverse().map(x => h('p', {}, `${x.sent ? 'Sent' : 'Draft saved'} ${when(x.at)}${x.by ? ` by ${x.by}` : ''}: "${x.subject}" to ${x.to.join(', ')}.`,
        x.web_link ? h('a', {href: x.web_link, target: '_blank', rel: 'noopener'}, x.sent ? ' Open in Outlook' : ' Open the draft') : null))) : null;
    dlg.replaceChildren(
      h('div', {class: 'fixer-top'}, h('div', {}, h('p', {class: 'eyebrow'}, 'INTERNAL APPROVAL'), h('h2', {id: 'send-title'}, `${rv.job || r.filename}, ${shortDay(rv.day)}`)),
        h('button', {type: 'button', class: 'btn', disabled: !!state.sending, onclick: () => dlg.close()}, 'Close')),
      h('div', {class: 'fixer-body'}, step1, step2, history));
  }
  const reviewBindingFields = f => ({file_hash: f.file_hash, expected_rev: f.expected_rev,
    expected_run: f.expected_run, expected_check_hash: f.expected_check_hash});
  async function approve(withdraw) {
    const f = state.sendForm, hash = state.report.file_hash;
    if (!withdraw && !f.approver.trim()) { toast('Type your name to approve the report.', true); return; }
    state.sending = 'approve'; drawSend();
    try {
      if (!withdraw) store.set('rd-approver', f.approver.trim());
      const out = await api('/api/spreadsheets/approve', {...reviewBindingFields(f), approver: f.approver, note: f.note, override: !!f.override, withdraw});
      state.report = {...state.report, ...out.item, approval: out.item.approval || null, review: state.report.review};
      toast(withdraw ? 'Approval withdrawn.' : 'Approved as final. Sending is paused; download the approved report for manual delivery.' + (out.change_summary ? ' It differed from SharePoint or was changed, so a change summary PDF was saved on record.' : ''));
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
  const sendLabel = r => (r.sent || []).some(x => x.sent) ? 'Sent to client' : r.approval ? 'Approval details…'
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
    try { await api('/api/spreadsheets/mark-fine', {file_hash: state.report.file_hash, by: myName() || null, ...shownFor(items.map(i => i.key).filter(Boolean)),
      issues: items.map(i => ({code: i.code || null, sheet: i.sheet || null, row: i.row ?? null, cell: i.cell || null, key: i.key || null, text: i.text || ''}))}); }
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
      state.aiOn ? h('button', {type: 'button', disabled: true, title: 'AI workbook changes are paused pending reviewed-plan safety checks', class: 'btn small', onclick: e => aiFix([issue.key], e.target)}, 'Fix with AI') : null));
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
    // Retained legacy panel: selected issues only open the guarded review chooser.
    const fixSelected = () => {
      const binding = reviewBinding(state.report), available = new Set(fdItems().map(it => it.key));
      state.fdReviewSelection = {binding: JSON.stringify(binding), keys: issues.filter(i => sel.has(i.key) && available.has(i.key)).map(i => i.key)};
      fdReviewDialog();
    };
    const draw = () => {
      const left = issues.filter(i => !done.has(i.key)).length;
      const cur = state.issueNav != null ? issues[state.issueNav]?.key : null;
      block.replaceChildren(h('header', {}, h('h2', {}, 'All issues'),
        h('p', {}, issues.length ? `${left} of ${issues.length} still open. The guide at the top takes you through them one at a time; click any one here to jump to it.`
          + (rv.verify?.status === 'done' ? ' Double-checked by Dex.' : state.verifying ? ' Dex is double-checking…' : '')
          : dismissed.length ? 'Nothing left: Dex checked every flagged item and none are real problems.' : 'Nothing flagged by the checks, SharePoint, the usual-day comparison or AI.'),
        null));
      const summary = errorSummary(issues, done);
      if (summary) block.append(summary);
      if (sel.size) {
        const picked = issues.filter(i => sel.has(i.key)), ready = picked.filter(i => fixesFor(i).some(f => f.state !== 'rejected') || isDupRow(i)).length;
        block.append(h('div', {class: 'selbar issel'},
          h('span', {}, h('b', {}, `${picked.length} selected`), ` · ${ready} with a suggested fix` + (picked.length > ready ? `, ${picked.length - ready} without one` : '')),
          h('button', {type: 'button', class: 'btn ghost small', onclick: () => { sel.clear(); draw(); }}, 'Clear'),
          picked.some(i => done.has(i.key)) ? h('button', {type: 'button', class: 'btn small', onclick: () => { const k = picked.map(i => i.key); sel.clear(); setDone(k, false); drawSteps(); drawCleanOffer(); }}, 'Mark not reviewed') : null,
          picked.some(i => !done.has(i.key)) ? h('button', {type: 'button', class: 'btn small', onclick: () => { const k = picked.map(i => i.key); sel.clear(); setDone(k, true); toast(`${k.length} marked reviewed.`); drawSteps(); drawCleanOffer(); }},
            `Mark ${picked.length === 1 ? 'it' : picked.length} reviewed`) : null,
          ready ? h('button', {type: 'button', class: 'btn small', onclick: e => fixSelected(e.target)}, 'Review selected suggested changes') : null,
          state.aiOn ? h('button', {type: 'button', disabled: true, title: 'AI workbook changes are paused pending reviewed-plan safety checks', class: 'btn primary small ai', onclick: e => { const k = picked.map(i => i.key); sel.clear(); aiFix(k, e.target); }}, `Fix ${picked.length === 1 ? 'it' : picked.length} with AI`) : null));
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
            const badge = i.restored ? 'Put back by you' : i.verdict === 'valid' ? '✓ Dex confirmed' : i.verdict === 'unsure' ? 'Dex unsure' : null;
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
    return h('details', {class: 'dismissed'}, h('summary', {}, `Dex checked and dismissed ${list.length} false alarm${list.length === 1 ? '' : 's'}`),
      h('ul', {}, list.map(i => h('li', {}, h('div', {}, h('b', {}, i.text), h('small', {}, [i.source, i.where].filter(Boolean).join(' · ') + (i.verdict_reason ? ' · Dex: ' + i.verdict_reason : ''))),
        h('button', {type: 'button', class: 'btn small', onclick: () => { const b = new Set(store.get(restoredKey(), [])); b.add(i.key); store.set(restoredKey(), [...b]); drawReport(); if (state.fixes) drawFixes(); toast('Put back in the issue list.'); }}, 'Put back')))));
  }
  const infoNote = info => { const texts = [...new Set(info.map(i => i.text))]; return h('p', {class: 'note'}, 'For information: ' + texts.slice(0, 3).join(' ') + (texts.length > 3 ? ` (and ${texts.length - 3} more)` : '')); };

  // ---------------- sources
  function sourcesBlock(r) {
    // the same source verdict as the header (the item-by-item reconciliation of this version), never the older daily export
    const rv = r.review, sp = rv.sharepoint || {}, bl = r.baseline || {}, ai = r.ai_review || {};
    const spState = {match: ['Matches SharePoint item by item', 'good'], differences: ['Differs from SharePoint', 'bad'], incomplete: ['SharePoint read incomplete: not verified', 'bad'],
      unverified: ['Not verified', ''], stale: ['SharePoint check out of date', 'bad'], day_level: ['Day totals only (not record by record)', ''], not_found: ['No SharePoint data found', ''],
      not_configured: ['SharePoint not connected', ''], error: ['SharePoint check failed', 'bad']}[sp.status] || ['Not checked yet', ''];
    const scope = (sp.fields_checked || []).length ? ` Fields checked: ${sp.fields_checked.join(', ')}.` + ((sp.fields_not_checked || []).length ? ` Not checked: ${sp.fields_not_checked.join(', ')}.` : '')
      + ' A match covers only these fields: not identifiers, photos, duplicates, source correctness or release readiness.' : '';
    const spText = (sp.status === 'match' ? `${sp.matched ?? '?'} of ${rv.scans ?? '?'} records agree with ${sp.source || 'the SharePoint list'}${sp.captured_at ? ' (captured ' + when2(sp.captured_at) + ')' : ''}.`
      : sp.status === 'differences' ? `${sp.differences || 0} difference(s), listed under Corrections${sp.captured_at ? ' (captured ' + when2(sp.captured_at) + ')' : ''}.`
      : sp.message || 'The report has not been compared with SharePoint item by item yet.') + scope;
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
        source('AI review', aiState, ai.summary || ai.message || 'Dex reads every sheet and points out anything that looks wrong.', null,
          h('button', {type: 'button', class: 'btn small', onclick: () => rerun('ai-review', 'Dex is reading the report…')}, ai.status === 'done' ? 'Run again' : 'Run AI review'))));
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
      h('a', {href: '#r=' + f.fixed_file_hash + '&history=1'}, 'Open it')));
    return h('section', {class: 'block'}, h('header', {}, h('h2', {}, 'History')), ...items);
  }

  async function rerun(kind, message) {
    if (['ai-review', 'ai-verify', 'ai-fixes'].includes(kind)) { fdReviewDialog(); return; }
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
  // Explicit preview of one captured correction batch. No automatic follow-up rounds.
  function previewAll(p, go) {
    let dlg = $('preview-dialog');
    if (!dlg) { dlg = h('dialog', {id: 'preview-dialog', class: 'fixer', 'aria-labelledby': 'preview-title'}); document.body.append(dlg); }
    const fixes = (p.correction ? p.correction.accepted.map(f => ({...f, after: p.correction.dec[f.id]?.after ?? f.after})) : p.fixes || []).filter(f => f.state !== 'rejected');
    const bySource = {};
    for (const f of fixes) (bySource[f.source || 'Suggested'] ||= []).push(f);
    const ai = state.aiOn ? (p.none || []).length : 0;
    dlg.replaceChildren(h('div', {class: 'preview'},
      h('h2', {id: 'preview-title'}, 'Review the changes'),
      h('p', {}, `${fixes.length} suggested change${fixes.length === 1 ? '' : 's'}` + ((p.dups || []).length ? `, ${p.dups.length} duplicate row${p.dups.length === 1 ? '' : 's'} removed` : '')
        + (ai ? `, and Dex works out ${ai} more (shown in "what changed" afterwards)` : '') + '. Your original file is kept, and the new version is checked again.'),
      ...Object.entries(bySource).map(([src, list]) => h('details', {open: list.length <= 12 ? '' : null},
        h('summary', {}, `${src}: ${list.length}`),
        h('table', {class: 'preview-table'}, h('thead', {}, h('tr', {}, ...['Cell', 'Now', 'Change to', 'Why'].map(t => h('th', {}, t)))),
          h('tbody', {}, ...list.slice(0, 400).map(f => h('tr', {}, h('td', {}, `${f.sheet} ${f.cell}`), h('td', {}, f.before === '' || f.before == null ? '(blank)' : String(f.before)),
            h('td', {}, String(f.after)), h('td', {}, f.reason || ''))))))),
      h('div', {class: 'row'}, h('button', {type: 'button', class: 'btn', onclick: () => dlg.close()}, 'Cancel'),
        h('button', {type: 'button', class: 'btn primary', onclick: () => { dlg.close(); go(); }}, 'Make these changes'))));
    if (!dlg.open) dlg.showModal();
  }
  async function autoFixAll(hash, only) {
    toast('AI workbook changes are paused. Review and apply cell corrections instead.', true);
    return hash;
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
      let result;
      try { result = await api('/api/spreadsheets/upload/finish', {upload_id: id, filename: file.name, part_count: parts, sha256: hash}); }
      catch (e) {   // no clear answer: the file's own SHA-256 is its ID, so ask whether it was saved before calling it failed
        let saved = null; try { saved = await api('/api/spreadsheet?id=' + hash); } catch (e2) { saved = null; }
        if (!saved) throw e;
        result = {item: saved, recovered: true};
      }
      meter.style.width = '100%';
      receipt(row, file, result.item, result.recovered);
      if (result.item.idempotent) toast(`${file.name} was already uploaded, so the saved copy was opened.`);
      return result.item;
    } catch (e) { row.classList.add('failed'); text.textContent = `${file.name}: ${e.message} Nothing was saved; upload it again (the same file is never stored twice).`; return null; }
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
      aiDid ? `Dex fixed ${plural(aiDid, 'problem')}` + ((s.removed || []).length || (s.moved || []).length ? `, including ${[(s.removed || []).length ? plural(s.removed.length, 'row') + ' taken out' : '', (s.moved || []).length ? plural(s.moved.length, 'row') + ' moved to the right shift' : ''].filter(Boolean).join(' and ')}` : '') + '.' : null,
      aiFine ? `Dex checked ${plural(aiFine, 'flag')} and found ${aiFine === 1 ? 'it is' : 'they are'} fine as is.` : null,
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
        h('p', {class: 'quiet'}, 'Every change is listed at the top of the report, with a link to the version before.' + (checks ? ` ${plural(checks, 'change')} ${checks === 1 ? 'is' : 'are'} Dex's best judgment and marked "please double-check".` : ''))) : null,
      left.length ? h('div', {class: 'res-left'}, h('b', {}, 'What was found'),
        h('p', {}, (must ? `${plural(must, 'must-fix item')} before it goes out. ` : '') + (opts.afterAll ? `${left.length === 1 ? 'This one needs' : 'These need'} a person: ${state.aiOn ? 'Dex already tried and was not sure' : 'there was no suggested change'}. Review them on the right: Fix myself, or Ignore if ${left.length === 1 ? 'it is' : 'they are'} fine.` : (fixedLines.length ? '' : 'Nothing has been changed yet. ') + (state.aiOn ? 'AI and row changes are paused. Review suggested cell corrections and apply the displayed plan.' : 'Approve every suggested change in one click, or review them first: each kind shows the first one, with Approve, Fix myself or Ignore for all of that kind.'))),
        (() => { const g = new Map(); for (const i of left) { const k = sameKey(i); g.set(k, [...(g.get(k) || []), i]); }
          const kinds = [...g.values()].sort((x, y) => y.length - x.length);
          return [h('ul', {}, kinds.slice(0, 5).map(k => h('li', {}, h('span', {class: 'sev ' + k[0].severity}), h('span', {}, k.length > 1 ? h('b', {}, `${fmt(k.length)}× `) : null, k[0].text)))),
            kinds.length > 5 ? h('p', {class: 'quiet'}, `and ${kinds.length - 5} more kinds.`) : null,
            kinds.length < left.length ? h('p', {class: 'quiet'}, `Only ${plural(kinds.length, 'kind')} of issue, so each kind can be handled in one go.`) : null]; })())
        : h('p', {}, 'Check the totals, then approve when you are ready. Sending is paused.')),
      data.fixed_from ? h('a', {class: 'btn dl-fixed', href: '/api/spreadsheets/download?id=' + hash, download: data.filename}, '⬇ Download the fixed version') : null,
      h('div', {class: 'row'}, h('button', {type: 'button', class: 'btn', onclick: () => dialog.close()}, left.length ? 'Later' : 'Open report'),
        left.length ? h('button', {type: 'button', class: 'btn primary', onclick: async () => { await ready(); showSide('fixes'); document.querySelector('.workspace')?.scrollIntoView({behavior: 'smooth', block: 'start'}); }}, opts.afterAll ? "Review what's left" : 'Review them first') : null,
        left.length && !opts.afterAll ? h('button', {type: 'button', class: 'btn ' + (state.aiOn ? 'ai' : 'primary'), onclick: async () => { await ready(); showSide('fixes');
          if (state.fixEverything?.hash === hash) state.fixEverything.run(); else toast('Open the Fixes tab to choose what to fix.', true); }}, 'Review suggested cell changes') : null));
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
    const waiting = visibleList().filter(queueNeedsAttention).length;
    const uncertain = state.listError || state.listPartial;
    return h('div', {class: 'wins-bar'},
      h('span', {}, h('b', {}, String(cleaned)), ` report${cleaned === 1 ? '' : 's'} cleaned today`),
      h('span', {}, h('b', {}, String(sent)), ` sent today`),
      h('span', {class: waiting || uncertain ? '' : 'allgood'}, waiting ? [h('b', {}, String(waiting)), ` saved report${waiting === 1 ? '' : 's'} need${waiting === 1 ? 's' : ''} attention`] : uncertain ? 'Report list incomplete. Waiting count unavailable.' : 'No saved reports waiting in this list.'));
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

// Slow answers must never be drawn into, or reload, a report the reviewer has since left.
// Runs the page's real fdEvidence, fdApprove and recheckLine in memory with stubbed network and drawing. No server, no model.
const test = require('node:test'), assert = require('node:assert/strict'), vm = require('node:vm'), fs = require('node:fs');
const src = fs.readFileSync(process.env.PPG_UI_SOURCE || 'web/app.js', 'utf8');
const cut = (from, to) => { const a = src.indexOf(from), b = src.indexOf(to, a); assert.ok(a > 0 && b > a, from); return src.slice(a, b); };
const gate = () => { let open, fail; const wait = new Promise((r, j) => { open = r; fail = j; }); return {wait, open, fail}; };
const tick = async () => { for (let n = 0; n < 12; n++) await Promise.resolve(); };
const rpt = (hash, extra = {}) => ({file_hash: hash, filename: hash + '.xlsx', review: {sharepoint: {}}, recheck: {available: true}, ...extra});
// A very small DOM: elements know their parent, ids are found only while attached under the root.
function dom() {
  const el = (tag, attrs, ...kids) => { const e = {tag, attrs: attrs || {}, kids: [], parent: null, disabled: false, textContent: '',
      replaceChildren(...k) { for (const c of e.kids) if (c && typeof c === 'object') c.parent = null; e.kids = []; e.append(...k); },
      append(...k) { for (const c of k.flat(9)) { if (c == null) continue; if (typeof c === 'object') c.parent = e; e.kids.push(c); } },
      remove() { if (e.parent) { e.parent.kids = e.parent.kids.filter(c => c !== e); e.parent = null; } }, focus() {},
      get isConnected() { let n = e; while (n.parent) n = n.parent; return n === root; }};
    e.append(...kids); return e; };
  const root = el('body', {});
  const find = (n, id) => { if (!n || typeof n !== 'object') return null; if (n.attrs.id === id) return n; for (const c of n.kids) { const f = find(c, id); if (f) return f; } return null; };
  const text = n => n == null ? '' : typeof n !== 'object' ? String(n) : n.kids.map(text).join('') + (n.textContent || '');
  const all = (n, tag, out = []) => { if (n && typeof n === 'object') { if (n.tag === tag) out.push(n); n.kids.forEach(c => all(c, tag, out)); } return out; };
  return {el, root, $: id => find(root, id), text, all};
}

// ---------- source evidence drawer
function evidence() {
  const d = dom(), slot = d.el('div', {id: 'fd-evidence'}); d.root.append(slot);
  const calls = [], state = {selected: 'A', report: rpt('A')};
  const ctx = vm.createContext({state, $: d.$, h: d.el, icon: () => '', showWs: String, when2: () => '', String,
    api: (path, body) => { const g = gate(); calls.push({body, ...g}); return g.wait; }});
  vm.runInContext(cut('  async function fdEvidence(', '\n  // ---------- preview, then a new version') + '\nthis.run = fdEvidence;', ctx);
  const goTo = hash => { state.selected = hash; state.report = rpt(hash); slot.replaceChildren(); };
  return {d, slot, calls, state, open: it => { ctx.run(it); }, goTo};
}
const row = (sheet, n, value) => ({sheet, row: n, cell: 'B' + n, report: value});
const cells = value => ({cells: [{cell: 'B2', header: 'Part', value}]});

test('a row read for report A is never shown in the drawer opened on report B', async () => {
  const e = evidence();
  e.open(row('S', 2, 'a')); assert.equal(e.calls[0].body.file_hash, 'A');
  e.goTo('B'); e.open(row('S', 2, 'b')); assert.equal(e.calls[1].body.file_hash, 'B');
  e.calls[0].open(cells('OLD REPORT A')); await tick();
  assert.doesNotMatch(e.d.text(e.slot), /OLD REPORT A/); assert.match(e.d.text(e.slot), /Reading the row/);
  e.calls[1].open(cells('value from B')); await tick();
  assert.match(e.d.text(e.slot), /value from B/); assert.doesNotMatch(e.d.text(e.slot), /OLD REPORT A|Reading the row/);
});
test('on one report, a slow answer for row 1 never lands in the drawer for row 2, in either order', async () => {
  for (const order of [[0, 1], [1, 0]]) {
    const e = evidence();
    e.open(row('S', 1, 'x')); e.open(row('S', 2, 'y'));
    for (const n of order) { e.calls[n].open(cells('row ' + (n + 1))); await tick(); }
    assert.match(e.d.text(e.slot), /row 2/); assert.doesNotMatch(e.d.text(e.slot), /row 1/);
  }
});
test('a failed row read for report A is not reported in the drawer of report B, nor in a later drawer', async () => {
  const e = evidence();
  e.open(row('S', 2, 'a')); e.goTo('B'); e.open(row('S', 2, 'b'));
  e.calls[0].fail(new Error('Report A could not be read')); await tick();
  assert.doesNotMatch(e.d.text(e.slot), /Report A could not be read/);
  e.calls[1].fail(new Error('B is busy')); await tick(); assert.match(e.d.text(e.slot), /B is busy/);
});
test('a closed drawer, or a report that was left, is not redrawn by a late answer', async () => {
  const e = evidence();
  e.open(row('S', 2, 'a')); e.d.all(e.slot, 'button')[0].attrs.onclick(); e.calls[0].open(cells('late')); await tick();
  assert.equal(e.d.text(e.slot), '');
  const f = evidence(); f.open(row('S', 2, 'a')); f.goTo('B'); f.calls[0].open(cells('late')); await tick(); assert.equal(f.d.text(f.slot), '');
});
test('the ordinary case still shows the row, also after the same version was reloaded on screen', async () => {
  const e = evidence();
  e.open(row('S', 2, 'a')); e.state.report = rpt('A', {reloaded: true}); e.calls[0].open(cells('the row')); await tick();
  assert.match(e.d.text(e.slot), /the row/);
  const whole = evidence(); whole.open({report: 'x'}); assert.equal(whole.calls.length, 0); assert.doesNotMatch(whole.d.text(whole.slot), /Reading the row/);
});

// ---------- internal approval
function approval({openReport} = {}) {
  const d = dom(), name = d.el('input', {id: 'fd-approver'}), btn = d.el('button', {id: 'fd-approve-btn'}); name.value = 'Reviewer'; d.root.append(name, btn);
  const log = {opened: [], toasts: [], lists: 0, sent: []}, answer = gate(), state = {selected: 'A', report: rpt('A')};
  const ctx = vm.createContext({state, $: d.$, store: {set() {}}, toast: (m, bad) => log.toasts.push([m, !!bad]), loadList: () => { log.lists++; },
    api: (path, body) => { log.sent.push(body); return answer.wait; },
    openReport: openReport || (async hash => { log.opened.push(hash); state.selected = hash; state.report = rpt(hash, {reloaded: true}); })});
  vm.runInContext(cut('  async function fdApprove(', '  async function fdRecheck(') + '\nthis.run = fdApprove;', ctx);
  return {state, log, answer, btn, run: () => ctx.run({file_hash: 'A', expected_rev: 3}), goTo: hash => { state.selected = hash; state.report = rpt(hash); }};
}
test('approving A, then opening B: the late answer does not reopen A or disturb B, and says what was saved', async () => {
  const a = approval(); const done = a.run(); a.goTo('B'); const b = a.state.report;
  a.answer.open({item: {file_hash: 'A'}}); await done;
  assert.deepEqual(a.log.sent.map(x => x.file_hash), ['A']);                       // the request was about A all along
  assert.deepEqual(a.log.opened, []); assert.equal(a.state.selected, 'B'); assert.equal(a.state.report, b);
  assert.equal(a.log.toasts.length, 1); assert.match(a.log.toasts[0][0], /A\.xlsx/); assert.match(a.log.toasts[0][0], /approved/i); assert.match(a.log.toasts[0][0], /Nothing was sent/);
  assert.equal(a.log.toasts[0][1], false); assert.equal(a.log.lists, 1);
});
test('approving A and staying on A reloads A once, as before', async () => {
  const a = approval(); const done = a.run(); a.answer.open({item: {file_hash: 'A'}}); await done;
  assert.deepEqual(a.log.opened, ['A']); assert.match(a.log.toasts[0][0], /Internally approved\. Nothing was sent/); assert.equal(a.log.lists, 1);
});
const refusal = (message = 'This report changed.', status = 409) => Object.assign(new Error(message), {httpStatus: status, saveCommitted: false});
test('a refused approval of A is named as A when B is on screen, and is not said to be saved', async () => {
  const a = approval(); const done = a.run(); a.goTo('B'); a.answer.fail(refusal()); await done;
  assert.deepEqual(a.log.opened, []); assert.equal(a.state.selected, 'B');
  assert.match(a.log.toasts[0][0], /A\.xlsx/); assert.match(a.log.toasts[0][0], /not approved/i); assert.match(a.log.toasts[0][0], /This report changed\./); assert.equal(a.log.toasts[0][1], true);
  const same = approval(); const again = same.run(); same.answer.fail(refusal()); await again;
  assert.deepEqual(same.log.toasts, [['This report changed.', true]]); assert.equal(same.btn.disabled, false);
});
test('approval saved but the reload failed: it is not reported as a failed approval', async () => {
  const a = approval({openReport: async () => { throw new Error('network'); }}); const done = a.run(); a.answer.open({item: {file_hash: 'A'}}); await done;
  assert.ok(a.log.toasts.some(t => /Internally approved/.test(t[0]) && !t[1]));
  assert.ok(a.log.toasts.some(t => /could not be refreshed/.test(t[0])));
  assert.ok(!a.log.toasts.some(t => t[0] === 'network'));
});

// An answer that never arrived says nothing about whether the approval was recorded, now or a moment later.
const unknown = {'lost response': () => new Error('Failed to fetch'), 'server error 500': () => Object.assign(new Error('The server answered 500. Try again in a moment.'), {httpStatus: 500, saveCommitted: false}),
  'gateway timeout 504': () => Object.assign(new Error('The server answered 504. Try again in a moment.'), {httpStatus: 504})};
const noClaim = said => { assert.match(said, /could not be confirmed/); assert.match(said, /may or may not have been recorded/); assert.match(said, /check its approval status/);
  assert.doesNotMatch(said, /not approved|was recorded\.|is not recorded|approve again|try again|Internally approved|was internally approved/i); };
// What the page happens to hold when the answer is lost. None of it is proof about this request.
const onPage = {
  'an earlier approval by someone else, for older checks, no longer in effect': {approval: {by: 'Previous Reviewer', reviewed_check_hash: 'old-check'}, approval_effective: false, review: {approved: null, sharepoint: {}}},
  'an approval bound to a different check': {approval: {by: 'Reviewer', reviewed_check_hash: 'other-check'}, check_hash: 'this-check', approval_effective: false},
  'an approval that looks current': {approval: {by: 'Reviewer', reviewed_check_hash: 'c'}, check_hash: 'c', approval_effective: true, review: {approved: true, sharepoint: {}}},
  'no approval at all (it may still be recorded a moment later)': {}};
for (const [name, make] of Object.entries(unknown)) {
  test(`${name} after the reviewer opened B: A is not called "not approved", B is untouched, nothing is sent again`, async () => {
    const a = approval(); const done = a.run(); a.goTo('B'); const b = a.state.report; a.answer.fail(make()); await done;
    assert.equal(a.log.sent.length, 1); assert.deepEqual(a.log.opened, []); assert.equal(a.state.selected, 'B'); assert.equal(a.state.report, b);
    assert.equal(a.log.toasts.length, 1); const [said, bad] = a.log.toasts[0];
    noClaim(said); assert.match(said, /A\.xlsx/); assert.match(said, /Reopen A\.xlsx/); assert.match(said, /report on screen was not changed/); assert.equal(bad, true);
  });
  for (const [what, extra] of Object.entries(onPage)) test(`${name} while still on A, the page showing ${what}: the outcome is unknown, nothing is claimed, read or offered`, async () => {
    const a = approval(); Object.assign(a.state.report, extra); const done = a.run(); a.answer.fail(make()); await done;
    assert.equal(a.log.sent.length, 1); assert.deepEqual(a.log.opened, []);          // not sent again, and no read is taken as proof
    assert.equal(a.log.toasts.length, 1); noClaim(a.log.toasts[0][0]); assert.equal(a.log.toasts[0][1], true);
    assert.equal(a.btn.disabled, true); assert.equal(a.btn.textContent, 'Outcome not confirmed');
  });
}
test('lost answer while A is being reloaded, or after a newer version of A was opened: still unknown, and named correctly', async () => {
  const a = approval(); const done = a.run(); a.state.report = null; a.answer.fail(new Error('Failed to fetch')); await done;   // same selection, mid-load
  assert.equal(a.log.toasts.length, 1); noClaim(a.log.toasts[0][0]); assert.match(a.log.toasts[0][0], /Reload this report/); assert.deepEqual(a.log.opened, []);
  const v = approval(); const later = v.run(); v.goTo('A2'); v.answer.fail(new Error('Failed to fetch')); await later;          // a corrected copy is another version
  assert.equal(v.log.toasts.length, 1); noClaim(v.log.toasts[0][0]); assert.match(v.log.toasts[0][0], /Reopen A\.xlsx/); assert.equal(v.state.selected, 'A2'); assert.deepEqual(v.log.opened, []);
});

// The page's real openReport: it shows a failed load on the page itself, leaves no report loaded, and returns normally.
function realOpen(state, d, apiImpl) {
  const report = d.el('div', {id: 'report'}); d.root.append(report);
  const ctx = vm.createContext({state, $: d.$, h: d.el, api: apiImpl, closeChat() {}, gridDiscard: () => true, location: {hash: ''}, history: {replaceState() {}},
    fillPages: async x => x, drawReport() {}, drawQueue() {}, versionReadOnly: () => true, loadFixes() {}, openSend() {}});
  vm.runInContext(cut('  async function openReport(', '\n  // Reports uploaded before the automatic cleanup') + '\nthis.open = openReport;', ctx);
  return {open: ctx.open, report};
}
test('approval saved, then the real openReport cannot load the report: said as saved and not refreshed, never as failed or silent', async () => {
  let reads = 0;
  const page = dom(), s = approval({openReport: (...args) => opener.open(...args)}); const opener = realOpen(s.state, page, async () => { reads++; throw new Error('Failed to fetch'); });
  const done = s.run(); s.answer.open({item: {file_hash: 'A'}}); await done;
  assert.ok(reads >= 1); assert.equal(s.state.report, null); assert.equal(s.state.selected, 'A');          // the real function swallowed the failure
  assert.match(page.text(opener.report), /Failed to fetch/);
  assert.deepEqual(s.log.toasts, [['Internally approved. Nothing was sent; sending is paused.', false],
    ['Internally approved, but this page could not be refreshed. Reload the report to see its current state.', true]]);
  assert.equal(s.log.sent.length, 1);
});
test('approval saved, the reviewer opens B while the real openReport is reloading A: B is left alone and nothing false is said', async () => {
  const page = dom(), read = gate(), s = approval({openReport: (...args) => opener.open(...args)}); const opener = realOpen(s.state, page, () => read.wait);
  const done = s.run(); s.answer.open({item: {file_hash: 'A'}}); await tick();
  s.state.openRequest++; s.goTo('B'); const b = s.state.report; read.fail(new Error('Failed to fetch')); await done;
  assert.equal(s.state.selected, 'B'); assert.equal(s.state.report, b);
  assert.deepEqual(s.log.toasts, [['Internally approved. Nothing was sent; sending is paused.', false]]);
});
test('a server error that states the approval was saved is reported as approved, on A and from B', async () => {
  const committed = () => Object.assign(new Error('The server answered 500.'), {httpStatus: 500, saveCommitted: true});
  const a = approval(); const done = a.run(); a.answer.fail(committed()); await done;
  assert.deepEqual(a.log.opened, ['A']); assert.match(a.log.toasts[0][0], /^Internally approved\. Nothing was sent/);
  const b = approval(); const later = b.run(); b.goTo('B'); b.answer.fail(committed()); await later;
  assert.deepEqual(b.log.opened, []); assert.match(b.log.toasts[0][0], /A\.xlsx was internally approved/);
});
test('a request that was never sent, a sign-in refusal and a 4xx refusal are refusals: the button comes back', async () => {
  for (const err of [Object.assign(new Error('Preview or discard the spreadsheet draft before another operation. Nothing changed.'), {notSent: true}), refusal('Sign in to continue.', 401), refusal('Evidence is required.', 400)]) {
    const a = approval(); const done = a.run(); a.answer.fail(err); await done;
    assert.deepEqual(a.log.toasts, [[err.message, true]]); assert.deepEqual(a.log.opened, []); assert.equal(a.btn.disabled, false);
  }
});

// ---------- the older-checks line ("Preview the new checks" / "Apply the new checks")
function older() {
  const d = dom(), log = {opened: [], toasts: [], lists: 0}, calls = [], state = {selected: 'A', report: rpt('A')};
  const ctx = vm.createContext({state, h: d.el, toast: (m, bad) => log.toasts.push([m, !!bad]), myName: () => 'Reviewer', String, Array,
    loadList: async () => { log.lists++; }, api: (path, body) => { const g = gate(); calls.push({body, ...g}); return g.wait; },
    openReport: async hash => { log.opened.push(hash); state.selected = hash; state.report = rpt(hash, {reloaded: true}); }});
  vm.runInContext(cut('  function recheckLine(', '  // Why the report stays on hold') + '\nthis.run = recheckLine;', ctx);
  const box = ctx.run(state.report); d.root.append(box);
  const press = async label => { const b = d.all(box, 'button').find(x => d.text(x) === label); const p = b.attrs.onclick({target: b}); await tick(); return p; };
  return {d, box, log, calls, state, press, goTo: hash => { state.selected = hash; state.report = rpt(hash); box.remove(); }};
}
test('new checks applied to A after the reviewer opened B: A is not reopened over B, and the notice names A', async () => {
  const o = older(); o.press('Preview the new checks'); await tick(); o.calls[0].open({proposal: {changes: []}}); await tick();
  const done = o.press('Apply the new checks'); await tick(); assert.equal(o.calls[1].body.apply, true); assert.equal(o.calls[1].body.file_hash, 'A');
  o.goTo('B'); const b = o.state.report; o.calls[1].open({}); await done; await tick();
  assert.deepEqual(o.log.opened, []); assert.equal(o.state.selected, 'B'); assert.equal(o.state.report, b);
  assert.equal(o.log.toasts.length, 1); assert.match(o.log.toasts[0][0], /A\.xlsx/); assert.match(o.log.toasts[0][0], /New checks applied/); assert.equal(o.log.lists, 1);
});
test('new checks applied while still on A reload A, as before; a refusal about A is named when B is on screen', async () => {
  const o = older(); o.press('Preview the new checks'); await tick(); o.calls[0].open({proposal: {changes: []}}); await tick();
  const done = o.press('Apply the new checks'); await tick(); o.calls[1].open({}); await done; await tick();
  assert.deepEqual(o.log.opened, ['A']); assert.match(o.log.toasts[0][0], /^New checks applied\./);
  const p = older(); p.press('Preview the new checks'); await tick(); p.goTo('B'); p.calls[0].fail(new Error('busy')); await tick();
  assert.deepEqual(p.log.toasts, [['A.xlsx: busy', true]]); assert.deepEqual(p.log.opened, []);
});

// ---------- "Check again" / "Compare again" on the report page
function again() {
  const log = {opened: [], toasts: [], lists: 0}, answer = gate(), state = {selected: 'A', report: rpt('A'), busy: false}, sent = [];
  const done = hash => rpt(hash, {review: {sharepoint: {status: 'match'}, usual: {}, ai: {}}});
  const ctx = vm.createContext({state, toast: (m, bad) => log.toasts.push([m, !!bad]), fdReviewDialog() {}, loadList: async () => { log.lists++; },
    api: (path, body) => { sent.push(body); return answer.wait; },
    openReport: async hash => { log.opened.push(hash); state.selected = hash; state.report = done(hash); }});
  vm.runInContext(cut('  async function rerun(', '  // ---------------------------------------------------------------- upload') + '\nthis.run = rerun;', ctx);
  return {state, log, answer, sent, run: () => ctx.run('sharepoint', 'Checking SharePoint…'), goTo: hash => { state.selected = hash; state.report = hash ? rpt(hash) : null; }};
}
test('a SharePoint check of A that finishes after B was opened does not reload B or report A\'s result as B\'s', async () => {
  const a = again(); const done = a.run(); a.goTo('B'); const b = a.state.report; a.answer.open({}); await done;
  assert.deepEqual(a.sent.map(x => x.file_hash), ['A']); assert.deepEqual(a.log.opened, []); assert.equal(a.state.report, b);
  assert.equal(a.log.toasts.length, 2); assert.match(a.log.toasts[1][0], /A\.xlsx/); assert.doesNotMatch(a.log.toasts[1][0], /Matches SharePoint/);
  assert.equal(a.state.busy, false); assert.equal(a.log.lists, 1);
});
test('the same check while another report is still loading does not fail with a page error', async () => {
  const a = again(); const done = a.run(); a.goTo(null); a.state.selected = 'B'; a.answer.open({}); await done;
  assert.deepEqual(a.log.opened, []); assert.ok(!a.log.toasts.some(t => /Cannot read|undefined|null/.test(t[0]))); assert.equal(a.state.busy, false);
});
test('a check that finishes on the report it was started on reloads it and reports its result, as before', async () => {
  const a = again(); const done = a.run(); a.answer.open({}); await done;
  assert.deepEqual(a.log.opened, ['A']); assert.deepEqual(a.log.toasts[1], ['Matches SharePoint.', false]);
  const f = again(); const failed = f.run(); f.goTo('B'); f.answer.fail(new Error('SharePoint is busy')); await failed;
  assert.deepEqual(f.log.toasts[1], ['A.xlsx: SharePoint is busy', true]);
});

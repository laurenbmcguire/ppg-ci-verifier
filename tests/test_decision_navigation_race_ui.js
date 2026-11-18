// A decision is saved on report A while the reviewer moves to report B (or reloads A). The answer must never be drawn
// into another report, navigation must never be undone, and a refresh that fails after a save is reported as exactly that.
// Runs the page's real fdSendDecision in memory with stubbed network and drawing. No server, no model.
const test = require('node:test'), assert = require('node:assert/strict'), vm = require('node:vm'), fs = require('node:fs');
const src = fs.readFileSync(process.env.PPG_UI_SOURCE || 'web/app.js', 'utf8');
const start = src.indexOf('  async function fdSendDecision('), end = src.indexOf('  function fdAdvanceAfter(', start);
const gate = () => { let open; const wait = new Promise(r => { open = r; }); return {wait, open}; };
const rpt = (hash, extra = {}) => ({file_hash: hash, filename: hash + '.xlsx', review: {run: {n: 1}, counts: {medium: 1}}, ...extra});

function harness({fetchAnswer, status, fillPages, openReport}) {
  const log = {opened: [], toasts: [], drew: 0, drewReport: 0, advanced: 0};
  const state = {selected: 'A', report: rpt('A'), decisionPending: false, busy: false, rechecking: false, aiProposalOperation: null, fdForm: 1, fdBatch: 1};
  const ctx = vm.createContext({state, crypto: {randomUUID: () => 'u1'}, Date, Math, JSON, Object, Set, Promise,
    versionReadOnly: () => false, gridDirty: () => false, findingBinding: () => ({file_hash: 'A', expected_rev: 0, expected_run: {n: 1}, expected_fps: {}}),
    myName: () => 'reviewer', drawSide: () => { log.drew++; }, drawReport: () => { log.drewReport++; }, showSignIn: () => {}, reviewBinding: r => ({h: r?.file_hash}),
    toast: (m, bad) => log.toasts.push(m), fetch: fetchAnswer, api: status || (async () => ({saved: false})),
    fillPages: fillPages || (async () => {}), fdAdvanceAfter: () => { log.advanced++; },
    openReport: openReport || (async (h) => { log.opened.push(h); state.selected = h; state.report = rpt(h, {reloaded: true}); })});
  vm.runInContext(src.slice(start, end) + '\nthis.send = fdSendDecision;', ctx);
  const goTo = h => { state.selected = h; state.report = rpt(h); };
  return {state, log, send: req => ctx.send(req), goTo};
}
const ok = body => async () => ({ok: true, status: 200, json: async () => body});
const saved = {report: rpt('A', {review: {run: {n: 1}, counts: {medium: 0}}, saved: true}), entry: {ids: ['k'], action: 'reject'}};
const req = () => ({ids: ['k'], action: 'reject', reason: 'r', from: {key: 'k'}});
const consistent = (state) => assert.equal(state.report.filename, state.selected + '.xlsx');

test('A is saved, the reviewer opens B while the pages load: B stays B', async () => {
  const pages = gate();
  const h = harness({fetchAnswer: ok(saved), fillPages: async () => { await pages.wait; }});
  const done = h.send(req());
  await new Promise(r => setImmediate(r));
  h.goTo('B'); pages.open(); await done;
  assert.equal(h.state.selected, 'B'); assert.equal(h.state.report.file_hash, 'B'); consistent(h.state);
  assert.equal(h.state.report.saved, undefined);            // nothing of A was merged into B
  assert.deepEqual(h.log.opened, []);                       // and nobody was sent back to A
  assert.equal(h.log.advanced, 0);
  assert.match(h.log.toasts.join(' '), /report you left was saved/);
});

test('A to B and back to A while the pages load: the freshly loaded A is shown from the server, nothing is merged', async () => {
  const pages = gate();
  const h = harness({fetchAnswer: ok(saved), fillPages: async () => { await pages.wait; }});
  const done = h.send(req());
  await new Promise(r => setImmediate(r));
  h.goTo('B'); h.goTo('A'); pages.open(); await done;
  assert.equal(h.state.selected, 'A'); consistent(h.state);
  assert.deepEqual(h.log.opened, ['A']);                    // one reload of A, its own current state
  assert.equal(h.state.report.saved, undefined); assert.equal(h.state.report.reloaded, true);
  assert.equal(h.log.advanced, 0);
});

test('the reviewer leaves before the answer arrives: the answer for A is not applied to B', async () => {
  const net = gate();
  const h = harness({fetchAnswer: async () => { await net.wait; return {ok: true, status: 200, json: async () => saved}; }});
  const done = h.send(req());
  h.goTo('B'); net.open(); await done;
  assert.equal(h.state.report.file_hash, 'B'); consistent(h.state); assert.deepEqual(h.log.opened, []); assert.equal(h.state.report.saved, undefined);
});

test('409 on A while staying on A: A is reloaded and says not saved', async () => {
  const h = harness({fetchAnswer: async () => ({ok: false, status: 409, json: async () => ({error: 'changed'})})});
  await h.send(req());
  assert.deepEqual(h.log.opened, ['A']); assert.match(h.state.decisionFailed.message, /^Not saved: changed/);
});

test('staying on A works as before', async () => {
  const h = harness({fetchAnswer: ok(saved)});
  await h.send(req());
  assert.equal(h.state.report.saved, true); assert.equal(h.state.selected, 'A'); assert.equal(h.log.advanced, 1); assert.equal(h.state.fdForm, null);
});

test('lost answer, saved, but the reviewer is on B: no jump back to A', async () => {
  const h = harness({fetchAnswer: async () => { throw new Error('offline'); }, status: async () => { h.goTo('B'); return {saved: true}; }});
  await h.send(req());
  assert.equal(h.state.selected, 'B'); consistent(h.state); assert.deepEqual(h.log.opened, []);
  assert.match(h.log.toasts.join(' '), /was saved/);
  assert.equal(h.state.decisionFailed, null);
});

test('lost answer, saved, still on A: A is reloaded', async () => {
  const h = harness({fetchAnswer: async () => { throw new Error('offline'); }, status: async () => ({saved: true})});
  await h.send(req());
  assert.deepEqual(h.log.opened, ['A']); assert.equal(h.state.selected, 'A');
});

test('lost answer, not known, reviewer on B: B gets no failure box and no retry for A', async () => {
  const h = harness({fetchAnswer: async () => ({ok: false, status: 502, json: async () => ({})}), status: async () => { h.goTo('B'); throw new Error('offline'); }});
  await h.send(req());
  assert.equal(h.state.selected, 'B'); assert.equal(h.state.decisionFailed, null); assert.deepEqual(h.log.opened, []);
  assert.match(h.log.toasts.join(' '), /not known yet/);
});

test('409 after the reviewer moved to B: B is left alone', async () => {
  const net = gate();
  const h = harness({fetchAnswer: async () => { await net.wait; return {ok: false, status: 409, json: async () => ({error: 'The check run changed.'})}; }});
  const done = h.send(req());
  h.goTo('B'); net.open(); await done;
  assert.equal(h.state.selected, 'B'); consistent(h.state); assert.deepEqual(h.log.opened, []); assert.equal(h.state.decisionFailed, null);
});

test('409 on A, and the reviewer moves to B while A reloads: no failure box lands on B', async () => {
  const h = harness({fetchAnswer: async () => ({ok: false, status: 409, json: async () => ({error: 'changed'})}),
    openReport: async (hash) => { h.log.opened.push(hash); h.goTo('B'); }});
  await h.send(req());
  assert.equal(h.state.selected, 'B'); assert.equal(h.state.decisionFailed, null);   // no failure box about A is left on B
  assert.equal(h.log.drew, 1);                              // only the pending draw: nothing was redrawn onto B afterwards
  assert.match(h.log.toasts.join(' '), /Not saved/);
});

test('400 refusal on A while still on A is shown on A; after moving to B it is only a notice', async () => {
  let h = harness({fetchAnswer: async () => ({ok: false, status: 400, json: async () => ({error: 'Say why.'})})});
  await h.send(req());
  assert.match(h.state.decisionFailed.message, /Not saved: Say why/);
  const net = gate();
  h = harness({fetchAnswer: async () => { await net.wait; return {ok: false, status: 400, json: async () => ({error: 'Say why.'})}; }});
  const done = h.send(req()); h.goTo('B'); net.open(); await done;
  assert.equal(h.state.decisionFailed, null); assert.match(h.log.toasts.join(' '), /report you left was not saved/);
});

test('saved, but loading the rest of the list fails: said as saved-but-not-refreshed, never as not saved', async () => {
  const h = harness({fetchAnswer: ok(saved), fillPages: async () => { throw new Error('page 2 failed'); }});
  await h.send(req());
  assert.match(h.state.decisionFailed.message, /^Saved, but this page could not be refreshed/);
  assert.equal(h.state.decisionFailed.retry, null);         // retrying would not be a new decision
  assert.equal(h.state.report.saved, undefined);            // the partial answer is not drawn as the full state
  assert.equal(h.log.advanced, 0); assert.equal(h.state.decisionPending, false);
});

test('a stale answer for an older load of A does not overwrite the newer load', async () => {
  const net = gate();
  const h = harness({fetchAnswer: async () => { await net.wait; return {ok: true, status: 200, json: async () => saved}; }});
  const done = h.send(req());
  h.state.report = rpt('A', {newer: true});                 // A was reloaded (same selection, a newer object)
  net.open(); await done;
  assert.deepEqual(h.log.opened, ['A']); assert.equal(h.state.report.saved, undefined);
});

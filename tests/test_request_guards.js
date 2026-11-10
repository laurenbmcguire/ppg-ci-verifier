// No DOM, server, database or provider: execute only isolated request-building functions.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('web/app.js', 'utf8');
function section(start, end) { return source.slice(source.indexOf(start), source.indexOf(end, source.indexOf(start))); }
const bindingCode = section('  function reviewBinding(', '  function fdItems(');
const sendCode = section('  async function fdSendDecision(', '  function fdAdvanceAfter(');
const sent = [];
let response = {ok: false, status: 400, json: async () => ({error: 'synthetic refusal'})};
const ctx = vm.createContext({state: {report: {}}, crypto: {randomUUID: () => 'fixed-test-request'},
  toast() {}, drawSide() {}, myName: () => 'Synthetic Reviewer',
  fetch: async (_, init) => { sent.push(JSON.parse(init.body)); if (!response) throw Error('lost response'); return response; },
  api: async () => ({saved: false})});
vm.runInContext(section('  function versionReadOnly(', '  function versionLinks(') + bindingCode + sendCode, ctx);
(async () => {
  vm.runInContext(`
    var report = {file_hash: 'old-file', check_hash: 'old-check', review: {run: {checker: 'old'}, decisions: {rev: 4}}};
    var old = {key: 'finding-1', fp: 'old-fp', binding: reviewBinding(report)};
    var old2 = {key: 'finding-2', fp: 'second-fp', binding: reviewBinding(report)};
    report.review.run.checker = 'new'; report.review.decisions.rev = 5;
    state.report = {file_hash: 'new-file', review: {decisions: {rev: 90}}};
    var req = {ids: ['finding-1'], action: 'confirm', from: old};
  `, ctx);
  await vm.runInContext('fdSendDecision(req)', ctx);
  assert.equal(sent[0].file_hash, 'old-file');
  assert.equal(sent[0].expected_rev, 4);
  assert.deepEqual(sent[0].expected_run, {checker: 'old'});
  assert.deepEqual(sent[0].expected_fps, {'finding-1': 'old-fp'});
  response = null;
  await vm.runInContext('fdSendDecision(req)', ctx);
  assert.deepEqual(sent[1], sent[0], 'uncertain retry must retain exact request and snapshot');
  await vm.runInContext(`fdSendDecision({...findingBinding([old, old2]), ids: ['finding-1','finding-2'], action: 'reject', reason: 'Checked synthetic source'})`, ctx);
  assert.deepEqual(sent[2].expected_fps, {'finding-1': 'old-fp', 'finding-2': 'second-fp'});
  assert.throws(() => vm.runInContext(`findingBinding([old, {...old2, binding: reviewBinding(report)}])`, ctx), /Review these findings again/);
  const count = sent.length;
  await vm.runInContext(`fdSendDecision({ids: ['finding-1'], action: 'confirm'})`, ctx);
  assert.equal(sent.length, count, 'no fallback to current report');
  assert.match(source, /state\.fdForm = \{item: it,/);
  assert.match(source, /state\.fdBatch = \{items: g\.items\.slice\(\),/);
  assert.match(source, /open = b\.items\.filter/);
  assert.match(source, /return \(\) => fdApprove\(binding\)/);
  assert.match(source, /state\.sendForm \|\|= \{\.\.\.reviewBinding\(r\)/);
  assert.doesNotMatch(source, /fdMaybeAutoFix|autoCleanOlder/);
  assert.doesNotMatch(source, /Saving your \$\{c.total\}/);
  assert.match(source, /download: false, binding/);
  assert.doesNotMatch(section('  async function openReport(', '  // Totals only'), /auto-fix|ai-fix|auto-clean/);
  assert.doesNotMatch(section('  async function loadFixes(', '  async function runReview('), /runAiFixes\(|runVerify\(|runReview\(|fdAutoLoop\(/);
  const applied = [];
  Object.assign(ctx, {revisionOf: r => r.revision || 1, drawFixes() {}, updateDownloads() {},
    $: () => null, store: {get: () => ({}), set() {}},
    api: async (_, body) => { applied.push(JSON.parse(JSON.stringify(body))); throw Error('synthetic refusal'); }});
  vm.runInContext(section('  const correctionSnapshots =', '  function readyChanges()') +
    section('  async function applyFixes(', '  // Take out extra copies'), ctx);
  vm.runInContext(`
    var originalFix = {id: 'f1', sheet: 'Synthetic', cell: 'A1', before: 'old', after: 'shown', kind: 'text'};
    var originalDec = {f1: {after: 'shown'}}, originalEdits = {};
    var shownReport = {file_hash: 'shown-file', check_hash: 'shown-check', filename: 'synthetic.xlsx', review: {run: {checker: 'shown'}, decisions: {rev: 2}}};
    var correction = captureCorrections([originalFix], originalDec, originalEdits, reviewBinding(shownReport), shownReport);
    originalFix.after = 'unseen'; originalDec.f1.after = 'unseen'; originalEdits.x = {after: 'unseen'};
    shownReport.check_hash = 'new-check'; state.report = shownReport;
  `, ctx);
  await vm.runInContext('applyFixes(correction.accepted, originalDec, originalEdits)', ctx);
  assert.equal(applied[0].expected_check_hash, 'shown-check');
  assert.deepEqual(applied[0].expected_run, {checker: 'shown'});
  assert.equal(applied[0].changes.length, 1);
  assert.equal(applied[0].changes[0].after, 'shown');
  await vm.runInContext('applyFixes(correction.accepted, {}, {})', ctx);
  assert.deepEqual(applied[1], applied[0], 'retry retains entire displayed correction plan');
  await vm.runInContext('applyFixes([originalFix], originalDec, originalEdits)', ctx);
  assert.equal(applied.length, 2, 'unregistered correction arrays cannot fall back to current state');
  vm.runInContext(`var newest = correction; function readyChanges() { return newest; }
    var capturedAction = correctionAction(c => c); newest = {total: 999};`, ctx);
  assert.equal(vm.runInContext('capturedAction().binding.expected_check_hash', ctx), 'shown-check');
  assert.match(source, /b\.onclick = \(\) => downloadWithChanges\(c\)/);
  assert.doesNotMatch(source, /opts\.binding \|\||fixEverythingLoop/);
  assert.doesNotMatch(section('  async function removeRows(', '  const isDupRow'), /api\(|applyFixes\(/);
  console.log('PASS: stale click, immutable retry, batch fingerprints, mixed snapshots, missing binding, approval/form capture, observational open, immutable correction selection, correction retry, unbound correction refusal, render-time control capture');
})().catch(e => { console.error(e); process.exitCode = 1; });

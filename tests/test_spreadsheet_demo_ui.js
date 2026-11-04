'use strict';
// Run: node --test tests/test_spreadsheet_demo_ui.js
// Synthetic, controlled DOM/fetch tests. This is not real-browser or backend verification.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');
const script = fs.readFileSync(path.join(__dirname, '../web/spreadsheet-ui.js'), 'utf8');
const tick = () => new Promise(resolve => setImmediate(resolve));
const response = (value, ok = true) => ({ok, json: async () => value});
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((done, fail) => { resolve = done; reject = fail; });
  return {promise, resolve, reject};
};
class Element {
  constructor(tag) {
    Object.assign(this, {tag, children: [], listeners: {}, dataset: {}, attributes: {},
      textContent: '', disabled: false, checked: false, hidden: false, files: []});
  }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  setAttribute(name, value) { this.attributes[name] = value; }
  querySelectorAll(tag) {
    return this.children.flatMap(child => [
      ...(child.tag === tag ? [child] : []), ...child.querySelectorAll(tag)
    ]);
  }
}
const A = 'a'.repeat(64), B = 'b'.repeat(64), DRAFT = 'd'.repeat(64);
const item = id => ({file_hash: id, filename: id === DRAFT ? 'synthetic-draft.xlsx' : 'synthetic-' + id[0] + '.xlsx',
  version: id === DRAFT ? 2 : 1, template_id: 'synthetic-template', source_sha256: 'c'.repeat(64),
  record_count: 1, numeric_cells_checked: 2, formula_count: 1, findings: []});
const review = (id = A, overrides = {}) => ({eligible: true, file_hash: id,
  check_hash: 'e'.repeat(64), source_sha256: 'c'.repeat(64), proposals: [
    {id: 'p1', sheet: 'Synthetic', cell: 'B2', field: 'accepted', before: 3, after: 4},
    {id: 'p2', sheet: 'Synthetic', cell: 'C2', field: 'rejected', before: 2, after: 1}
  ], decisions: [], events: [], ...overrides});
const binding = value => ({file_hash: value.file_hash, check_hash: value.check_hash, source_sha256: value.source_sha256});

async function harness(initialReview = review()) {
  const nodes = {};
  ['spreadsheet-message', 'spreadsheet-file', 'spreadsheet-refresh', 'spreadsheet-detail',
    'spreadsheet-list', 'development-lab', 'development-examples'].forEach(id => { nodes[id] = new Element('div'); });
  const calls = [], actions = [], queuedReviews = new Map();
  const reviews = new Map([[A, initialReview], [B, review(B)], [DRAFT, review(DRAFT, {proposals: [], download_url: '/api/spreadsheets/demo/download?id=' + DRAFT})]]);
  vm.runInNewContext(script, {
    CI_DEVELOPMENT_EXAMPLES: [],
    document: {getElementById: id => nodes[id], createElement: tag => new Element(tag)},
    fetch: (url, options) => {
      calls.push({url, options});
      if (url === '/api/health') return Promise.resolve(response({development_demo: true}));
      if (url === '/api/spreadsheets') return Promise.resolve(response({spreadsheets: [item(A), item(B), item(DRAFT)]}));
      if (url.startsWith('/api/spreadsheet?id=')) return Promise.resolve(response(item(url.split('=')[1])));
      if (url.startsWith('/api/spreadsheets/demo/review?id=')) {
        const id = url.split('=')[1], queue = queuedReviews.get(id);
        return queue?.length ? queue.shift().promise : Promise.resolve(response(reviews.get(id)));
      }
      if (url === '/api/spreadsheets/demo/decisions' || url === '/api/spreadsheets/demo/apply') {
        assert.ok(actions.length, 'A controlled action response is required');
        return actions.shift().promise;
      }
      throw Error('Unexpected request: ' + url);
    }
  });
  await tick();
  const detail = nodes['spreadsheet-detail'];
  const choose = async id => {
    await nodes['spreadsheet-list'].querySelectorAll('button').find(b => b.dataset.id === id).listeners.click();
    await tick();
  };
  const buttons = text => detail.querySelectorAll('button').filter(b => b.textContent === text);
  const click = (text, index = 0) => {
    const button = buttons(text)[index]; assert.ok(button, 'Expected button: ' + text);
    return button.listeners.click();
  };
  const checks = () => detail.querySelectorAll('input').filter(i => i.type === 'checkbox');
  const posts = () => calls.filter(c => c.options?.method === 'POST');
  const busy = expected => {
    for (const id of ['spreadsheet-file', 'spreadsheet-refresh']) assert.equal(nodes[id].disabled, expected);
    for (const button of [...detail.querySelectorAll('button'), ...nodes['spreadsheet-list'].querySelectorAll('button')]) assert.equal(button.disabled, expected);
    for (const input of detail.querySelectorAll('input')) assert.equal(input.disabled, expected);
  };
  await choose(A);
  return {nodes, detail, calls, actions, queuedReviews, reviews, choose, buttons, click, checks, posts, busy};
}

for (const [label, decision, index] of [['Demo Accept change', 'approve', 0], ['Demo Reject change', 'deny', 1]]) {
  test(label + ' binds a single proposal to the exact review', async () => {
    const h = await harness(), finish = deferred(); h.actions.push(finish);
    const pending = h.click(label, index);
    const call = h.posts()[0];
    assert.equal(call.url, '/api/spreadsheets/demo/decisions');
    assert.equal(call.options.headers['X-Review-Token'], 'same-origin');
    assert.deepEqual(JSON.parse(call.options.body), {...binding(review()), proposal_ids: [index ? 'p2' : 'p1'], decision});
    finish.resolve(response({review: review(A, {decisions: [{proposal_id: index ? 'p2' : 'p1', decision}], events: [{type: 'decision'}]})}));
    await pending; h.busy(false);
    assert.ok(h.detail.querySelectorAll('p').some(p => p.textContent === 'Demo decision: ' + decision));
  });
}

test('batch decisions require explicit checkbox selection and clear it after success', async () => {
  const h = await harness();
  await h.click('Demo Accept selected changes');
  assert.equal(h.posts().length, 0);
  assert.match(h.nodes['spreadsheet-message'].textContent, /Select at least one/);
  assert.ok(h.checks().every(check => !check.checked));
  h.checks()[0].checked = true; h.checks()[1].checked = true;
  const finish = deferred(); h.actions.push(finish);
  const pending = h.click('Demo Reject selected changes');
  assert.deepEqual(JSON.parse(h.posts()[0].options.body).proposal_ids, ['p1', 'p2']);
  finish.resolve(response({review: review(A, {decisions: ['p1', 'p2'].map(proposal_id => ({proposal_id, decision: 'deny'}))})}));
  await pending;
  assert.ok(h.checks().every(check => !check.checked));
  assert.match(h.nodes['spreadsheet-message'].textContent, /Selection cleared/);
});

for (const decision of [null, 'deny', 'hold']) test('apply rejects ' + (decision || 'unreviewed') + ' proposal without a POST', async () => {
  const h = await harness(review(A, {decisions: decision ? [{proposal_id: 'p1', decision}] : []}));
  h.checks()[0].checked = true;
  await h.click('Create Excel draft from selected accepted changes');
  assert.equal(h.posts().length, 0);
  assert.match(h.nodes['spreadsheet-message'].textContent, /must be demo-accepted first/);
  h.busy(false);
});

test('accepted selected proposal creates and displays a new unapproved draft with cleared decisions', async () => {
  const h = await harness(review(A, {decisions: [{proposal_id: 'p1', decision: 'approve'}]}));
  h.checks()[0].checked = true;
  const finish = deferred(); h.actions.push(finish);
  const pending = h.click('Create Excel draft from selected accepted changes');
  assert.equal(h.posts()[0].url, '/api/spreadsheets/demo/apply');
  assert.deepEqual(JSON.parse(h.posts()[0].options.body), {...binding(review()), proposal_ids: ['p1']});
  finish.resolve(response({item: item(DRAFT), review: h.reviews.get(DRAFT), download_url: '/api/spreadsheets/demo/download?id=' + DRAFT}));
  await pending; await tick();
  assert.equal(h.detail.querySelectorAll('h3')[0].textContent, 'synthetic-draft.xlsx · version 2');
  assert.match(h.nodes['spreadsheet-message'].textContent, /original workbook and source are unchanged/);
  assert.match(h.nodes['spreadsheet-message'].textContent, /decisions do not carry forward/);
  assert.equal(h.detail.querySelectorAll('a')[0].href, '/api/spreadsheets/demo/download?id=' + DRAFT);
  assert.equal(h.checks().length, 0); h.busy(false);
});

test('pending action prevents a second action and navigation even with direct handler invocation', async () => {
  const h = await harness(), finish = deferred(); h.actions.push(finish);
  const pending = h.click('Demo Accept change'); h.busy(true);
  const before = h.calls.length;
  await h.click('Demo Reject change'); await h.choose(B);
  assert.equal(h.calls.length, before);
  assert.match(h.detail.querySelectorAll('h3')[0].textContent, /synthetic-a/);
  finish.resolve(response({review: review()})); await pending; h.busy(false);
  await h.choose(B); assert.match(h.detail.querySelectorAll('h3')[0].textContent, /synthetic-b/);
});

test('late review response cannot replace newer selected spreadsheet detail', async () => {
  const h = await harness(), old = deferred(); h.queuedReviews.set(A, [old]);
  await h.choose(A); await h.choose(B);
  const latest = h.detail.children.slice();
  old.resolve(response(review(A, {notice: 'STALE REVIEW MUST NOT RENDER'}))); await tick();
  assert.deepEqual(h.detail.children, latest);
  assert.ok(!h.detail.querySelectorAll('p').some(p => p.textContent.includes('STALE')));
  assert.match(h.detail.querySelectorAll('h3')[0].textContent, /synthetic-b/);
});

for (const apply of [false, true]) for (const networkFailure of [false, true]) test('failed ' + (apply ? 'apply ' : 'decision ') + (networkFailure ? 'network' : 'HTTP') + ' action reloads full spreadsheet and review without retrying POST', async () => {
  const h = await harness(review(A, {decisions: [{proposal_id: 'p1', decision: 'approve'}]})), finish = deferred(); h.actions.push(finish);
  if (apply) h.checks()[0].checked = true;
  h.reviews.set(A, review(A, {decisions: [{proposal_id: 'p1', decision: 'approve'}]}));
  const before = h.calls.filter(c => c.url.includes('/demo/review?')).length;
  const start = h.calls.length;
  const pending = h.click(apply ? 'Create Excel draft from selected accepted changes' : 'Demo Accept change');
  if (networkFailure) finish.reject(Error('Synthetic interrupted response'));
  else finish.resolve(response({error: 'Synthetic stale binding'}, false));
  await pending;
  assert.equal(h.posts().length, 1);
  const later = h.calls.slice(start + 1).map(call => call.url);
  assert.deepEqual(later, ['/api/spreadsheet?id=' + A, '/api/spreadsheets/demo/review?id=' + A, '/api/spreadsheets']);
  assert.equal(h.calls.filter(c => c.url.includes('/demo/review?')).length, before + 1);
  assert.ok(h.detail.querySelectorAll('p').some(p => p.textContent === 'Demo decision: approve'));
  assert.match(h.nodes['spreadsheet-message'].textContent, /Synthetic/);
  assert.ok(h.checks().every(check => !check.checked)); h.busy(false);
});

test('ineligible review displays notice and no decision or apply controls', async () => {
  const h = await harness(review(A, {eligible: false, notice: 'Synthetic file is not eligible.'}));
  assert.equal(h.detail.querySelectorAll('button').length, 0);
  assert.equal(h.checks().length, 0);
  assert.ok(h.detail.querySelectorAll('p').some(p => p.textContent === 'Synthetic file is not eligible.'));
  assert.equal(h.posts().length, 0);
});

test('download links accept only exact same-origin draft endpoint and lowercase 64-character hash', async () => {
  const safe = '/api/spreadsheets/demo/download?id=' + DRAFT;
  for (const url of ['https://example.invalid/draft.xlsx', '//example.invalid/draft.xlsx', 'javascript:alert(1)',
    safe + '&redirect=x', '/api/spreadsheets/demo/download?id=short', safe.replace(DRAFT, DRAFT.toUpperCase()), safe]) {
    const h = await harness(review(A, {eligible: false, download_url: url}));
    const links = h.detail.querySelectorAll('a');
    assert.equal(links.length, url === safe ? 1 : 0, url);
    if (links.length) assert.equal(links[0].href, safe);
  }
});

test('historical review exposes audit history, blocks decisions and navigates to its corrected draft', async () => {
  const h = await harness(review(A, {current_version: false, superseded_by: DRAFT,
    decisions: [{proposal_id: 'p1', decision: 'approve'}], events: [{type: 'synthetic-applied'}]}));
  assert.equal(h.checks().length, 0);
  assert.deepEqual(h.detail.querySelectorAll('button').map(button => button.textContent), ['Open latest corrected draft']);
  assert.ok(h.detail.querySelectorAll('summary').some(summary => summary.textContent === 'Historical spreadsheet review'));
  assert.ok(h.detail.querySelectorAll('p').some(p => p.textContent.includes('audit history only')));
  const history = h.detail.querySelectorAll('details').find(element =>
    element.querySelectorAll('summary').some(summary => summary.textContent === 'Historical spreadsheet review'));
  const stored = JSON.parse(history.querySelectorAll('pre')[0].textContent);
  assert.deepEqual(stored.binding, binding(review()));
  assert.deepEqual(stored.decisions, [{proposal_id: 'p1', decision: 'approve'}]);
  assert.deepEqual(stored.events, [{type: 'synthetic-applied'}]);
  const before = h.calls.length;
  await h.click('Open latest corrected draft'); await tick();
  assert.equal(h.calls[before].url, '/api/spreadsheet?id=' + DRAFT);
  assert.equal(h.detail.querySelectorAll('h3')[0].textContent, 'synthetic-draft.xlsx · version 2');
  assert.equal(h.posts().length, 0);
});

test('historical review with invalid successor hash has no navigation or mutation controls', async () => {
  for (const superseded_by of [null, '', 'short', DRAFT.toUpperCase(), DRAFT + '&redirect=x',
    'https://example.invalid/' + DRAFT]) {
    const h = await harness(review(A, {current_version: false, superseded_by}));
    assert.equal(h.detail.querySelectorAll('button').length, 0, String(superseded_by));
    assert.equal(h.checks().length, 0);
    assert.ok(h.detail.querySelectorAll('summary').some(summary => summary.textContent === 'Historical spreadsheet review'));
    assert.equal(h.posts().length, 0);
  }
});

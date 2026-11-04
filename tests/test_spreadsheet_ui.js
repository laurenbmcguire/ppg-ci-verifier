'use strict';

// Run: node --test tests/test_spreadsheet_ui.js
// Controlled DOM/fetch regression tests. No server, network, or production writes.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');

const script = fs.readFileSync(path.join(__dirname, '../web/spreadsheet-ui.js'), 'utf8');
const tick = () => new Promise(resolve => setImmediate(resolve));
const deferred = () => {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return {promise, resolve};
};

class Element {
  constructor(tag) {
    this.tag = tag;
    this.children = [];
    this.listeners = {};
    this.dataset = {};
    this.attributes = {};
    this.textContent = '';
    this.disabled = false;
    this.files = [];
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

const report = id => ({
  file_hash: id, filename: id + '.xlsx', version: 1,
  template_id: 'synthetic-template', record_count: 1,
  numeric_cells_checked: 2, formula_count: 1, findings: []
});
const response = (value, ok = true) => ({ok, json: async () => value});

async function harness() {
  const nodes = {};
  for (const id of ['spreadsheet-message', 'spreadsheet-file', 'spreadsheet-refresh',
    'spreadsheet-detail', 'spreadsheet-list']) nodes[id] = new Element('div');
  const calls = [];
  const selectionResponses = new Map();
  const listResponses = [];
  const reconciliationResponses = [];
  const fetch = (url, options) => {
    calls.push({url, options});
    if (url === '/api/spreadsheets') {
      return listResponses.length ? listResponses.shift().promise :
        Promise.resolve(response({spreadsheets: [report('A'), report('B')]}));
    }
    if (url.startsWith('/api/spreadsheet?id=')) {
      const id = url.split('=')[1];
      return selectionResponses.has(id) ? selectionResponses.get(id).promise :
        Promise.resolve(response(report(id)));
    }
    if (url === '/api/spreadsheets/reconcile') {
      assert.ok(reconciliationResponses.length, 'A controlled reconciliation response is required');
      return reconciliationResponses.shift().promise;
    }
    throw Error('Unexpected fetch: ' + url);
  };
  vm.runInNewContext(script, {
    document: {getElementById: id => nodes[id], createElement: tag => new Element(tag)},
    fetch
  });
  await tick();
  const buttons = () => nodes['spreadsheet-list'].querySelectorAll('button');
  const choose = id => buttons().find(button => button.dataset.id === id).listeners.click();
  const input = () => nodes['spreadsheet-detail'].querySelectorAll('input')[0];
  const title = () => nodes['spreadsheet-detail'].children[0].textContent;
  const start = (text = '{}') => {
    const file = input();
    file.files = [{size: text.length, text: async () => text}];
    return file.listeners.change();
  };
  const busy = expected => {
    assert.equal(nodes['spreadsheet-file'].disabled, expected);
    assert.equal(nodes['spreadsheet-refresh'].disabled, expected);
    assert.ok(buttons().every(button => button.disabled === expected));
    assert.equal(input().disabled, expected);
  };
  await choose('A');
  return {nodes, calls, selectionResponses, listResponses, reconciliationResponses,
    buttons, choose, input, title, start, busy};
}

test('pending comparison locks navigation and restores it after completion', async () => {
  const h = await harness();
  const finish = deferred(); h.reconciliationResponses.push(finish);
  const pending = h.start(); await tick(); h.busy(true);
  const before = h.calls.length;
  // Invoke the handler directly too: the guard must work beyond disabled styling.
  await h.choose('B');
  assert.equal(h.calls.length, before);
  assert.equal(h.title(), 'A.xlsx · version 1');
  assert.equal(JSON.parse(h.calls.find(c => c.url.endsWith('/reconcile')).options.body).file_hash, 'A');
  finish.resolve(response({item: report('A')})); await pending; h.busy(false);
  await h.choose('B'); assert.equal(h.title(), 'B.xlsx · version 1');
});

test('starting comparison invalidates an earlier selection request', async () => {
  const h = await harness();
  const oldSelection = deferred(); h.selectionResponses.set('B', oldSelection);
  const selecting = h.choose('B');
  const finish = deferred(); h.reconciliationResponses.push(finish);
  const pending = h.start(); await tick(); h.busy(true);
  oldSelection.resolve(response(report('B'))); await selecting;
  assert.equal(h.title(), 'A.xlsx · version 1');
  finish.resolve(response({item: report('A')})); await pending;
  assert.equal(h.title(), 'A.xlsx · version 1'); h.busy(false);
});

test('a delayed old selection cannot overwrite a completed comparison', async () => {
  const h = await harness();
  const oldSelection = deferred(); h.selectionResponses.set('B', oldSelection);
  const selecting = h.choose('B');
  const finish = deferred(); h.reconciliationResponses.push(finish);
  const pending = h.start(); await tick();
  finish.resolve(response({item: report('A')})); await pending;
  oldSelection.resolve(response(report('B'))); await selecting;
  assert.equal(h.title(), 'A.xlsx · version 1'); h.busy(false);
});

test('replacement source inputs and refreshed list buttons inherit pending state', async () => {
  const h = await harness();
  const finish = deferred(); h.reconciliationResponses.push(finish);
  const list = deferred(); h.listResponses.push(list);
  const pending = h.start(); await tick();
  const previousInput = h.input();
  finish.resolve(response({item: report('A')})); await tick();
  assert.notEqual(h.input(), previousInput); h.busy(true);
  const before = h.calls.length;
  await h.start(); assert.equal(h.calls.length, before);
  const observedStates = [];
  const container = h.nodes['spreadsheet-list'];
  const append = container.append.bind(container);
  container.append = (...children) => {
    observedStates.push(...children.filter(c => c.tag === 'button').map(c => c.disabled));
    append(...children);
  };
  list.resolve(response({spreadsheets: [report('A'), report('B')]})); await pending;
  assert.deepEqual(observedStates, [true, true]); h.busy(false);
});

test('failed comparison unlocks navigation without applying a late selection', async () => {
  const h = await harness();
  const oldSelection = deferred(); h.selectionResponses.set('B', oldSelection);
  const selecting = h.choose('B');
  const finish = deferred(); h.reconciliationResponses.push(finish);
  const pending = h.start(); await tick(); h.busy(true);
  finish.resolve(response({error: 'Synthetic comparison failure'}, false)); await pending;
  assert.equal(h.nodes['spreadsheet-message'].textContent, 'Synthetic comparison failure');
  h.busy(false);
  oldSelection.resolve(response(report('B'))); await selecting;
  assert.equal(h.title(), 'A.xlsx · version 1');
  h.selectionResponses.delete('B'); await h.choose('B');
  assert.equal(h.title(), 'B.xlsx · version 1');
});

test('malformed source input releases the navigation lock', async () => {
  const h = await harness();
  await h.start('{broken'); h.busy(false);
  assert.equal(h.calls.filter(c => c.url.endsWith('/reconcile')).length, 0);
  await h.choose('B'); assert.equal(h.title(), 'B.xlsx · version 1');
});

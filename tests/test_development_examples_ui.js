'use strict';

// Run: node --test tests/test_development_examples_ui.js
// Controlled DOM/fetch tests only: no real browser, network, server, or client data.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');
const read = name => fs.readFileSync(path.join(__dirname, '../web', name), 'utf8');
const tick = () => new Promise(resolve => setImmediate(resolve));
const response = (value, ok = true) => ({ok, json: async () => value});
const deferred = () => {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return {promise, resolve};
};
class Element {
  constructor(tag) {
    Object.assign(this, {tag, children: [], listeners: {}, dataset: {}, attributes: {},
      textContent: '', disabled: false, hidden: false, files: []});
  }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  setAttribute(name, value) { this.attributes[name] = value; }
  scrollIntoView() { this.scrolled = true; }
  querySelectorAll(tag) {
    return this.children.flatMap(child => [
      ...(child.tag === tag ? [child] : []), ...child.querySelectorAll(tag)
    ]);
  }
}
const report = (id, filename = id + '.xlsx') => ({file_hash: id, filename, version: 1,
  template_id: filename.endsWith('.csv') ? null : 'synthetic-template',
  record_count: 1, numeric_cells_checked: 2, formula_count: 1, findings: []});

async function harness(development_demo = true) {
  const nodes = {};
  ['spreadsheet-message', 'spreadsheet-file', 'spreadsheet-refresh', 'spreadsheet-detail',
    'spreadsheet-list', 'spreadsheet-upload', 'development-lab', 'development-examples',
    'development-message'].forEach(id => { nodes[id] = new Element('div'); });
  nodes['development-lab'].hidden = true;
  const calls = [], imports = [], comparisons = [], lists = [];
  const context = vm.createContext({document: {
    getElementById: id => nodes[id], createElement: tag => new Element(tag)
  }, fetch: (url, options) => {
    calls.push({url, options});
    if (url === '/api/health') return Promise.resolve(response({development_demo}));
    if (url === '/api/spreadsheets') return lists.length ? lists.shift().promise :
      Promise.resolve(response({spreadsheets: [report('original'), report('other')]}));
    if (url.startsWith('/api/spreadsheet?id=')) return Promise.resolve(response(report(url.split('=')[1])));
    if (url === '/api/spreadsheets/import') {
      assert.ok(imports.length, 'Import must have a controlled response');
      return imports.shift().promise;
    }
    if (url === '/api/spreadsheets/reconcile') {
      assert.ok(comparisons.length, 'Comparison must have a controlled response');
      return comparisons.shift().promise;
    }
    throw Error('Unexpected fetch: ' + url);
  }});
  vm.runInContext(read('development-examples.js'), context);
  vm.runInContext(read('spreadsheet-ui.js'), context);
  await tick();
  const examples = context.CI_DEVELOPMENT_EXAMPLES;
  const buttons = () => nodes['development-examples'].querySelectorAll('button');
  const choose = id => nodes['spreadsheet-list'].querySelectorAll('button')
    .find(button => button.dataset.id === id).listeners.click();
  const click = example => buttons().find(button => button.dataset.example === example.id).listeners.click();
  const title = () => nodes['spreadsheet-detail'].querySelectorAll('h3')[0]?.textContent;
  const busy = expected => {
    assert.equal(nodes['spreadsheet-file'].disabled, expected);
    assert.equal(nodes['spreadsheet-refresh'].disabled, expected);
    for (const id of ['spreadsheet-list', 'development-examples'])
      assert.ok(nodes[id].querySelectorAll('button').every(button => button.disabled === expected));
    assert.ok(nodes['spreadsheet-detail'].querySelectorAll('input').every(input => input.disabled === expected));
  };
  return {nodes, calls, imports, comparisons, lists, examples, buttons, choose, click, title, busy};
}

test('health must explicitly enable development demo before revealing four bundled samples', async () => {
  for (const enabled of [false, undefined, 'true']) {
    const h = await harness(enabled === undefined ? null : enabled);
    assert.equal(h.nodes['development-lab'].hidden, true);
    assert.equal(h.buttons().length, 0);
    assert.equal(h.calls.filter(call => call.options?.method === 'POST').length, 0);
  }
  const h = await harness();
  assert.equal(h.nodes['development-lab'].hidden, false);
  assert.equal(h.examples.length, 4);
  assert.equal(h.buttons().length, 4);
  assert.deepEqual(h.buttons().map(button => button.dataset.example), Array.from(h.examples, example => example.id));
});

test('each source-backed sample imports original bytes before comparing its separate evidence', async () => {
  const h = await harness();
  for (const example of h.examples.filter(example => example.source)) {
    const imported = deferred(), compared = deferred();
    h.imports.push(imported); h.comparisons.push(compared);
    const start = h.calls.length;
    const pending = h.click(example);
    const importCall = h.calls[start];
    assert.equal(importCall.url, '/api/spreadsheets/import');
    assert.equal(importCall.options.method, 'POST');
    assert.deepEqual(JSON.parse(importCall.options.body), {
      filename: example.filename, content_base64: example.content_base64
    });
    assert.equal(h.calls.length, start + 1, 'Comparison waits for import completion');
    imported.resolve(response({item: report(example.id, example.filename)})); await tick();
    const comparison = h.calls[start + 1];
    assert.equal(comparison.url, '/api/spreadsheets/reconcile');
    assert.equal(comparison.options.method, 'POST');
    assert.deepEqual(JSON.parse(comparison.options.body), JSON.parse(JSON.stringify({
      file_hash: example.id, source: example.source
    })));
    for (const call of [importCall, comparison])
      assert.equal(call.options.headers['X-Review-Token'], 'same-origin');
    compared.resolve(response({item: {...report(example.id, example.filename), version: 2}}));
    await pending;
    assert.equal(h.title(), example.filename + ' · version 2');
    assert.match(h.nodes['spreadsheet-message'].textContent, /Original retained; release held/);
    h.busy(false);
  }
});

test('sample loading locks concurrent sample clicks and selection through import, comparison and refresh', async () => {
  const h = await harness(); await h.choose('original');
  const example = h.examples.find(example => example.source);
  const imported = deferred(), compared = deferred(), refreshed = deferred();
  h.imports.push(imported); h.comparisons.push(compared); h.lists.push(refreshed);
  const pending = h.click(example); h.busy(true);
  const tryConcurrent = async () => {
    const before = h.calls.length;
    await h.click(h.examples[1]); await h.choose('other');
    assert.equal(h.calls.length, before, 'Handlers guard against direct invocation while pending');
  };
  await tryConcurrent();
  imported.resolve(response({item: report(example.id, example.filename)})); await tick();
  h.busy(true); await tryConcurrent();
  compared.resolve(response({item: report(example.id, example.filename)})); await tick();
  h.busy(true); await tryConcurrent();
  refreshed.resolve(response({spreadsheets: [report('original'), report('other')]})); await pending;
  h.busy(false); await h.choose('other');
  assert.equal(h.title(), 'other.xlsx · version 1');
});

test('plain CSV sample imports without source reconciliation', async () => {
  const h = await harness();
  const example = h.examples.find(example => example.filename.endsWith('.csv'));
  assert.equal(example.source, null);
  const imported = deferred(); h.imports.push(imported);
  const pending = h.click(example);
  imported.resolve(response({item: report(example.id, example.filename)})); await pending;
  assert.equal(h.calls.filter(call => call.url.endsWith('/reconcile')).length, 0);
  assert.equal(h.title(), example.filename + ' · version 1');
  h.busy(false);
});

for (const phase of ['import', 'comparison']) test(phase + ' error unlocks controls and retains the available original', async () => {
  const h = await harness(); await h.choose('original');
  const originalDetail = h.nodes['spreadsheet-detail'].children.slice();
  const example = h.examples.find(example => example.source);
  const originalBytes = example.content_base64;
  const imported = deferred(), compared = deferred();
  h.imports.push(imported);
  if (phase === 'comparison') h.comparisons.push(compared);
  const pending = h.click(example);
  if (phase === 'comparison') {
    imported.resolve(response({item: report(example.id, example.filename)})); await tick();
    compared.resolve(response({error: 'Synthetic comparison failure'}, false));
  } else imported.resolve(response({error: 'Synthetic import failure'}, false));
  await pending;
  h.busy(false);
  assert.match(h.nodes['development-message'].textContent, /Synthetic .* failure/);
  assert.equal(h.nodes['development-message'].className, 'intake-error');
  if (phase === 'import') {
    assert.deepEqual(h.nodes['spreadsheet-detail'].children, originalDetail);
  } else {
    assert.equal(h.title(), example.filename + ' · version 1');
    assert.match(h.nodes['development-message'].textContent, /Original sample was saved/);
    assert.equal(h.calls.filter(call => call.url === '/api/spreadsheets').length, 2);
  }
  assert.equal(example.content_base64, originalBytes, 'Bundled original bytes remain unchanged');
  assert.equal(h.calls.filter(call => call.options?.method === 'POST').length, phase === 'import' ? 1 : 2);
  await h.choose('other'); assert.equal(h.title(), 'other.xlsx · version 1');
});

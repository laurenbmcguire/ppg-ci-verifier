const test = require('node:test'), assert = require('node:assert/strict'), vm = require('node:vm'), fs = require('node:fs');
const source = fs.readFileSync('web/app.js', 'utf8');
const between = (a, b) => source.slice(source.indexOf(a), source.indexOf(b, source.indexOf(a)));
class Element {
  constructor(tag) { this.tag = tag; this.nodeType = 1; this.children = []; }
  append(...nodes) { this.children.push(...nodes.flat().filter(n => n != null && n !== false)); }
  replaceChildren(...nodes) { this.children = nodes.flat().filter(n => n != null && n !== false); }
  setAttribute(key, value) { this[key] = value; }
}
const textOf = node => typeof node === 'string' ? node : (node.children || []).map(textOf).join(' ');
const descendants = node => (node.children || []).flatMap(child => typeof child === 'object' ? [child, ...descendants(child)] : []);
const settle = async () => { for (let i = 0; i < 20; i++) await Promise.resolve(); };
const hSource = between('  const h = (', '  const store =');
const todaySource = between('  async function showToday()', '  // ---------------- scan timeline');
async function render(data, scans = {}) {
  const root = new Element('main'), calls = [], builds = [], original = JSON.stringify(data);
  const state = {}, location = {hash: '#today'};
  const ctx = vm.createContext({document: {createElement: tag => new Element(tag)}, $: () => root, state, location,
    fmt: value => value == null ? '—' : String(value), STATUS: {hold: 'Hold', check: 'Check', clear: 'Clear'},
    todaysWins: () => new Element('div'), buildReport: (...args) => builds.push(args),
    api: async (path, body) => {
      calls.push({path, body: JSON.parse(JSON.stringify(body))});
      if (path === '/api/today') return {...data, day: body.day || data.day};
      if (path === '/api/today/job') return {sharepoint: scans[body.job] || {error: 'Synthetic source unavailable'}};
      throw new Error('Unexpected API path');
    }});
  vm.runInContext(hSource + todaySource, ctx);
  await ctx.showToday(); await settle();
  assert.equal(JSON.stringify(data), original, 'existing watched metadata and report data are not mutated');
  return {root, calls, builds, state, location, ctx};
}
test('client catalog header keeps source count without an add-all-jobs control or write request', () => {
  const list = new Element('section'), ctx = vm.createContext({list, document: {createElement: tag => new Element(tag)},
    state: {client: 'synthetic', clients: [{key: 'synthetic', client: 'Synthetic client', sites: [{location: 'Plant'}], jobs: [{job: 'A'}, {job: 'B'}]}]}});
  vm.runInContext(hSource + between('    const client = (state.clients || []).find(c => c.key === state.client);', '    const vh = state.vinHits;'), ctx);
  assert.match(textOf(list), /Synthetic client/); assert.match(textOf(list), /2 jobs on SharePoint/);
  assert.equal(descendants(list).filter(el => el.tag === 'button').length, 0);
});
test('Today preserves existing rows but removes every manual watch control and setting write', async () => {
  const data = {day: '2026-11-10', sharepoint_on: true, jobs: [
    {job: 'SYNTHETIC-A', watched: true, yesterday: {file_hash: 'old', status: 'hold'}, today: null},
    {job: 'SYNTHETIC-B', watched: false, today: {file_hash: 'today', status: 'check'}}]};
  const {root, calls, builds} = await render(data, {'SYNTHETIC-A': {found: true, today: 3, yesterday: 2, readiness: {state: 'provisional'}}, 'SYNTHETIC-B': {found: true, today: 0}});
  const text = textOf(root), controls = descendants(root).filter(el => ['button', 'input'].includes(el.tag));
  for (const job of data.jobs) assert.ok(text.includes(job.job));
  assert.ok(controls.some(el => textOf(el) === 'Week')); assert.match(text, /Build captured report/);
  assert.ok(!controls.some(el => /watch|add.*job/i.test(textOf(el)) || el['aria-label'] === 'Job number'));
  assert.ok(calls.every(({body}) => !('add' in body) && !('remove' in body)));
  assert.equal(calls.filter(call => call.path === '/api/today/job').length, 2);
  assert.equal(builds.length, 0, 'reading Today does not build reports');
  assert.match(text, /Jobs in this view/); assert.match(text, /Scans in this view/);
  assert.match(text, /does not establish complete client or source coverage/);
  assert.match(text, /or confirm that background checks have run/);
  assert.ok(!text.includes('Every job’s day'));
});
test('empty dashboard never claims no clients/jobs and no watcher is offered', async () => {
  const {root, calls} = await render({day: '2026-11-10', sharepoint_on: false, jobs: []});
  assert.match(textOf(root), /This does not mean there are no clients, jobs, or source records/);
  assert.equal(descendants(root).filter(el => el.tag === 'input').length, 0);
  assert.equal(calls.length, 1);
});
test('partial history and failed discovery are displayed instead of implying complete coverage', async () => {
  const {root} = await render({day: '2026-11-10', sharepoint_on: true, jobs: [], coverage: {capped: true, discovery: {search_failed: 'Synthetic failure', unreadable_sites: ['Synthetic site']}}});
  assert.match(textOf(root), /Only part of the saved report history was loaded/);
  assert.match(textOf(root), /SharePoint discovery was incomplete/);
  assert.match(textOf(root), /not zero activity/);
});
test('repeat date navigation remains read-only and weekly navigation still works', async () => {
  const {root, calls, state, location} = await render({day: '2026-11-10', sharepoint_on: false, jobs: [{job: 'SYNTHETIC-A', watched: true}]});
  const button = label => descendants(root).find(el => el.tag === 'button' && el['aria-label'] === label);
  button('Previous day').onclick(); await settle();
  assert.equal(state.todayDay, '2026-11-09');
  button('Next day').onclick(); await settle();
  assert.equal(state.todayDay, '2026-11-10');
  descendants(root).find(el => el.tag === 'button' && textOf(el) === 'Week').onclick();
  assert.equal(location.hash, 'periods'); assert.equal(state.periodQuery.job, 'SYNTHETIC-A');
  assert.ok(calls.every(({path, body}) => path === '/api/today' && !('add' in body) && !('remove' in body)));
});
test('no UI path retains manual Today watchlist mutation requests', () => {
  for (const label of ['Watch these jobs on Today', 'Watch job', 'Stop watching', 'Adding to Today…', 'Add a job to watch']) assert.ok(!source.includes(label), label);
  assert.ok(!todaySource.includes('load({add:')); assert.ok(!todaySource.includes('load({remove:'));
  assert.ok(!source.includes("api('/api/today', {add:"));
});

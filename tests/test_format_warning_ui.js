'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');
const start = source.indexOf('  function errorSummary(');
const end = source.indexOf('  function issuesBlock(', start);
assert.ok(start >= 0 && end > start);
function render(issues) {
  const context = {Set, issueType: i => i.code,
    h: (tag, attrs, ...children) => ({tag, attrs, children: children.flat(Infinity).filter(x => x != null)})};
  vm.createContext(context);
  vm.runInContext(source.slice(start, end), context);
  const tree = context.errorSummary(issues, new Set());
  const text = n => typeof n === 'string' ? n : (n?.children || []).map(text).join(' ');
  return text(tree);
}
// Python's paired regression checks that summarize emits only=null and sure=false
// for these codes. Exercise the actual UI renderer with that wire contract.
function hints() {
  return ['part_format_differs', 'identifier_format_differs', 'part_format_differs', 'identifier_format_differs']
    .map((code, i) => ({key: String(i), code, severity: 'high', only: null, sure: false,
      sheet: 'Inspection Detail', text: 'Part number 00356871-V-RH has a different format.'}));
}
test('format-only group never claims its identifier values are right', () => {
  const text = render(hints());
  assert.match(text, /4 need a person to decide/);
  assert.doesNotMatch(text, /values themselves are right|only written differently|letter typed for a digit/);
  assert.match(text, /00356871-V-RH/);
});
test('mixed actual mismatch and format hints keep distinct uncertainty', () => {
  const issues = hints();
  issues[0] = {...issues[0], code: 'sharepoint_value_differs', sure: true, text: 'Exact source value differs.'};
  const text = render(issues);
  assert.match(text, /3 need a person to decide/);
  assert.match(text, /Exact source value differs/);
  assert.doesNotMatch(text, /values themselves are right|only written differently/);
});

const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const source = fs.readFileSync('web/app.js', 'utf8');
const start = source.indexOf('  function fdSelect(list, on) {');
const code = source.slice(start, source.indexOf('  // what the reviewer', start));
test('Remove from preview clears manual edit and every proposal on the same cell after restoration', () => {
  // JSON round trip models restored local-storage values rather than live object aliases.
  const persisted = {choices: {p1:{state:'accepted'}, p2:{state:'accepted'}, other:{state:'accepted'}}, edits: {'S|I18':{after:'normalized'}, 'S|J18':{after:'untouched'}}};
  const ctx = vm.createContext({state:{fixes:[{id:'p1',sheet:'S',cell:'I18'},{id:'p2',sheet:'S',cell:'i18'},{id:'other',sheet:'S',cell:'J18'}]},
    store:{get:k=>JSON.parse(JSON.stringify(persisted[k])),set:(k,v)=>{persisted[k]=JSON.parse(JSON.stringify(v));}},
    decisionsKey:()=> 'choices', editsKey:()=> 'edits', editId:(s,c)=>`${s}|${c.toUpperCase()}`, toast(){},fdRefresh(){},plural(){}});
  vm.runInContext(code, ctx);
  vm.runInContext("fdSelect([{fix:{id:'p1'},sheet:'S',cell:'I18',own:{after:'normalized'}}], false)",ctx);
  assert.deepEqual(persisted.choices,{other:{state:'accepted'}});
  assert.deepEqual(persisted.edits,{'S|J18':{after:'untouched'}});
  vm.runInContext("fdSelect([{fix:{id:'p1'},sheet:'S',cell:'I18'}], false)",ctx);
  assert.deepEqual(persisted.choices,{other:{state:'accepted'}},'repeated exclusion remains harmless');
});

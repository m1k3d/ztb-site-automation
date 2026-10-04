const {test}=require('node:test');
const assert=require('node:assert/strict');
const {Catalog,choices,selection}=require('../ui/interface-picker.js');
const port=(name,type,bond_member=false)=>({name,type,bond_member});
const data={gateways:[
  {id:'Gateway-1',interfaces:[port('ge1','management'),port('ge2','lan'),port('ge3','lan'),port('ge5','wan'),port('ge6','ha'),port('xe10','wan'),port('xe7','wan'),port('ge9','lan',true),port('vpn0','vpn')]},
  {id:'Gateway-2',interfaces:[port('ge1','management'),port('ge2','wan'),port('ge3','lan'),port('ge6','ha')]}
]};
test('WAN choices use the selected gateway and natural port order',()=>{
  assert.deepEqual(choices(data,'wan',['Gateway-1']).map(p=>p.value),['ge5','xe7','xe10']);
  assert.deepEqual(choices(data,'wan',['Gateway-2']).map(p=>p.value),['ge2']);
});
test('VLAN choices exclude WAN, HA, bond members, and ports unavailable on a peer',()=>{
  assert.deepEqual(choices(data,'vlan',['Gateway-1'],['ge1']).map(p=>p.value),['ge2','ge3','lo0']);
  assert.deepEqual(choices(data,'vlan',['Gateway-1','Gateway-2']).map(p=>p.value),['ge1','ge3','lo0']);
  assert.deepEqual(choices(data,'ha',['Gateway-1','Gateway-2']).map(p=>p.value),['ge6']);
});
test('template changes flag current assignments without replacing them',()=>{
  const original='ge2, custom0,ge2';
  const result=selection(choices(data,'vlan',['Gateway-1']),original,true,true);
  assert.match(result.warning,/custom0/);
  assert.equal(result.options.at(-1).value,'custom0');
  assert.equal(original,'ge2, custom0,ge2');
  assert.equal(selection([], 'xe7',false,false).options[0].value,'xe7');
});
test('cache shares in-flight lookups and reuses completed results until refresh',async()=>{
  let calls=0,complete;
  const catalog=new Catalog(()=>{calls++;return new Promise(resolve=>complete=resolve);});
  const fields={template_name:'Branch'};
  const first=catalog.load(fields),second=catalog.load(fields);
  assert.equal(first,second);assert.equal(calls,1);
  complete(data);await first;await catalog.load({template_name:'branch'});
  assert.equal(calls,1);
  const refresh=catalog.load(fields,true);assert.equal(calls,2);complete(data);await refresh;
});
test('connection changes discard stale responses and template caches',async()=>{
  let complete,changes=0;
  const catalog=new Catalog(()=>new Promise(resolve=>complete=resolve),()=>changes++);
  const pending=catalog.load({template_name:'Old'});catalog.reset();complete(data);await pending;
  assert.equal(catalog.get({template_name:'Old'}),undefined);assert.equal(changes,0);
});
test('failed lookup stays local until explicit retry and does not affect another template',async()=>{
  let calls=0;
  const catalog=new Catalog(async fields=>{calls++;if(fields.template_name==='Bad')throw Error('Lookup unavailable');return data;});
  await catalog.load({template_name:'Good'});await catalog.load({template_name:'Bad'});await catalog.load({template_name:'Bad'});
  assert.equal(calls,2);assert.equal(catalog.get({template_name:'Good'}).state,'ready');
  assert.equal(catalog.get({template_name:'Bad'}).state,'error');
  await catalog.load({template_name:'Bad'},true);assert.equal(calls,3);
});

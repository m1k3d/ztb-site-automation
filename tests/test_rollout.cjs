const {test}=require('node:test');
const assert=require('node:assert/strict');
const {selectBranches}=require('../ui/rollout.js');
const batch=()=>['a','b','c'].map(name=>({fields:{site_name:name,post:'0'}}));

test('group selection resolves all eligibility before changing the draft fingerprint',()=>{
  const sites=batch(),before=JSON.stringify(sites);
  selectBranches(sites,[0,1,2],true,()=>JSON.stringify(sites)===before);
  assert.deepEqual(sites.map(s=>s.fields.post),['1','1','1']);
  assert.deepEqual(sites.map(s=>s.fields.site_name),['a','b','c']);
});
test('select all ready excludes invalid or completed entries, including stale selections',()=>{
  const sites=batch();sites[2].fields.post='1';
  selectBranches(sites,[0,1,2],true,i=>i<2,true);
  assert.deepEqual(sites.map(s=>s.fields.post),['1','1','0']);
});
test('country selection and deselection preserve branches outside that country',()=>{
  const sites=batch();sites[2].fields.post='1';
  selectBranches(sites,[0,1],true,()=>true);
  selectBranches(sites,[0,1],false,()=>false);
  assert.deepEqual(sites.map(s=>s.fields.post),['0','0','1']);
});

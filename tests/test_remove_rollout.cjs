const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync(require('node:path').join(__dirname,'../ui/app.js'),'utf8');

function workspace() {
  const elements=new Map(),prompts=[],saves=[];
  const get=id=>{if(!elements.has(id))elements.set(id,{});return elements.get(id);};
  const context=vm.createContext({$:get,batch:['Reference','Branch A','Branch B'].map((name,i)=>({fields:{site_name:name,post:i?'1':'0'},reference:i===0})),
    current:2,busy:false,projectWorking:false,dirty:false,localReviewReady:true,previewSnapshot:'approved',deploymentJob:{state:'ready'},
    deploymentActive:()=>['deploying','previewing'].includes(context.deploymentJob.state),
    confirm:message=>{prompts.push(message);return context.confirmed;},confirmed:true,
    rollout:{reset(){context.resetCount++;},refresh(){context.refreshCount++;}},resetCount:0,refreshCount:0,
    render(){},renderDeployment(){},updateSummary(){},
    projectSaver:{touch(){saves.push(JSON.parse(JSON.stringify(context.batch)));}},
    api(){throw new Error('Removal must not call the tenant');}});
  const portion=(from,to)=>source.slice(source.indexOf(from),source.indexOf(to,source.indexOf(from)));
  vm.runInContext(portion('function markDirty()','function updateSummary()')+
    portion('function removeRolloutSite(', '$("remove-site").onclick'),context);
  return {context,prompts,saves};
}

test('removing a selected branch saves the remaining queue and expires deployment approval',()=>{
  const {context:c,prompts,saves}=workspace();const editing=c.batch[2];
  c.removeRolloutSite(c.batch[1]);
  assert.match(prompts[0],/Branch A/);assert.match(prompts[0],/local draft only/);assert.match(prompts[0],/Zscaler is unchanged/);
  assert.equal(c.batch.length,2);assert.equal(c.batch[c.current],editing);
  assert.deepEqual(saves[0].map(site=>site.fields.site_name),['Reference','Branch B']);
  assert.equal(c.previewSnapshot,null);assert.equal(c.deploymentJob.state,'expired');assert.equal(c.localReviewReady,false);
  assert.equal(c.resetCount,1);assert.equal(c.refreshCount,1);
});

test('canceling removal leaves the saved draft and preview unchanged',()=>{
  const {context:c,saves}=workspace();c.confirmed=false;
  c.removeRolloutSite(c.batch[1]);assert.equal(c.batch.length,3);assert.equal(c.current,2);
  assert.equal(c.previewSnapshot,'approved');assert.equal(saves.length,0);
});

test('removing a reference saves its removal without changing branch copies',()=>{
  const {context:c,prompts,saves}=workspace();
  const branches=JSON.stringify(c.batch.slice(1)),editing=c.batch[2];
  c.removeRolloutSite(c.batch[0]);
  assert.match(prompts[0],/Remove reference “Reference”/);
  assert.match(prompts[0],/local reference only/);
  assert.match(prompts[0],/Branches already created from it and the site in Zscaler are unchanged/);
  assert.equal(JSON.stringify(c.batch),branches);assert.equal(c.batch[c.current],editing);
  assert.equal(JSON.stringify(saves[0]),branches);
});

test('canceling reference removal keeps it in the saved project',()=>{
  const {context:c,saves}=workspace();c.confirmed=false;
  const reference=c.batch[0];c.removeRolloutSite(reference);
  assert.equal(c.batch[0],reference);assert.equal(c.batch.length,3);assert.equal(saves.length,0);
});

test('active deployment, preview, project change, and local validation block removal',()=>{
  for(const state of ['deploying','previewing','projectWorking','busy']){
    const {context:c,prompts,saves}=workspace();
    if(['deploying','previewing'].includes(state))c.deploymentJob.state=state;else c[state]=true;
    c.removeRolloutSite(c.batch[1]);assert.equal(c.batch.length,3);assert.equal(prompts.length,0);assert.equal(saves.length,0);
  }
});

test('stale row callbacks cannot remove another branch and the last removal clears selection',()=>{
  const {context:c,prompts,saves}=workspace();const site=c.batch[1];
  c.removeRolloutSite(site);c.removeRolloutSite(site);
  assert.equal(c.batch.length,2);assert.equal(prompts.length,1);assert.equal(saves.length,1);
  c.removeRolloutSite(c.batch[1]);assert.equal(c.current,0);
  c.removeRolloutSite(c.batch[0]);assert.equal(c.batch.length,0);assert.equal(c.current,-1);
});

test('remove selected takes out only selected branches after one confirmation',()=>{
  const {context:c,prompts,saves}=workspace();
  c.batch.push({fields:{site_name:'Branch C',post:'0'}},{fields:{site_name:'Branch D',post:'1'}});c.current=3;
  const editing=c.batch[3];
  c.removeSelectedSites();
  assert.equal(prompts.length,1);assert.match(prompts[0],/Remove 3 selected branches/);
  assert.match(prompts[0],/Branch A\nBranch B\nBranch D/);assert.match(prompts[0],/local drafts only/);
  assert.deepEqual(c.batch.map(s=>s.fields.site_name),['Reference','Branch C']);assert.equal(c.batch[c.current],editing);
  assert.equal(saves.length,1);assert.deepEqual(saves[0].map(s=>s.fields.site_name),['Reference','Branch C']);
  assert.equal(c.previewSnapshot,null);assert.equal(c.deploymentJob.state,'expired');
  assert.equal(c.resetCount,1);assert.equal(c.refreshCount,1);
});

test('remove selected moves the editor to the next remaining site and caps the confirmation list',()=>{
  const {context:c,prompts}=workspace();
  for(let i=0;i<20;i++)c.batch.push({fields:{site_name:`Copy ${i}`,post:'1'}});
  c.batch.push({fields:{site_name:'Keep',post:'0'}});
  c.removeSelectedSites();
  assert.match(prompts[0],/Remove 22 selected branches/);assert.match(prompts[0],/and 10 more/);
  assert.deepEqual(c.batch.map(s=>s.fields.site_name),['Reference','Keep']);assert.equal(c.current,1);
});

test('remove selected does nothing when canceled, nothing is selected, or work is active',()=>{
  const canceled=workspace();canceled.context.confirmed=false;canceled.context.removeSelectedSites();
  assert.equal(canceled.context.batch.length,3);assert.equal(canceled.saves.length,0);assert.equal(canceled.context.previewSnapshot,'approved');
  const none=workspace();none.context.batch.forEach(s=>s.fields.post='0');none.context.removeSelectedSites();
  assert.equal(none.prompts.length,0);assert.equal(none.context.batch.length,3);
  for(const state of ['deploying','previewing','projectWorking','busy']){
    const {context:c,prompts,saves}=workspace();
    if(['deploying','previewing'].includes(state))c.deploymentJob.state=state;else c[state]=true;
    c.removeSelectedSites();assert.equal(c.batch.length,3);assert.equal(prompts.length,0);assert.equal(saves.length,0);
  }
});

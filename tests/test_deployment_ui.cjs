const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const path=require('node:path');

// Exercise the real poller, result renderer, and diagram controls without a tenant.
function workspace() {
  function element(tag='div',text,cls='') {
    return {tagName:tag.toUpperCase(),textContent:text || '',className:cls,dataset:{},children:[],open:false,
      classList:{toggle(){},remove(){}},setAttribute(){},removeAttribute(){},closest(){return this;},showModal(){this.open=true;},
      append(...items){for(const item of items){if(item.parent)item.parent.children.splice(item.parent.children.indexOf(item),1);this.children.push(item);item.parent=this;}},
      replaceChildren(...items){for(const child of this.children)child.parent=null;this.children=[];this.append(...items);},
      querySelectorAll(){return this.children.flatMap(child=>[...(child.tagName==='DETAILS' && child.dataset.disclosureKey ? [child] : []),...child.querySelectorAll()]);}};
  }
  const elements=new Map(),get=id=>{if(!elements.has(id))elements.set(id,element());return elements.get(id);};
  let touches=0,requests=0;
  const context=vm.createContext({document:{getElementById:get,createElement:element},$:get,node:element,
    setTimeout(){return 1;},clearTimeout(){},updateSummary(){},
    api:async(url,payload)=>{requests++;return context.handleApi ? context.handleApi(url,payload) : context.deploymentJob;},
    projectSaver:{project:{id:'project-a'},touch(){touches++;}},
    batch:[],projectWorking:false,busy:false,localReviewReady:true,previewSnapshot:null,
    deploymentSnapshot:()=>'',diagramFinishedRun:'',deploymentPoll:null,workspaceView:'review',
    deploymentActive:()=>['previewing','deploying'].includes(context.deploymentJob.state),
    currentSite:()=>null,rollout:{refresh(){}},deploymentJob:{id:'run-a',project_id:'project-a',state:'deploying',sites:[]}});
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../ui/diagrams.js'),'utf8'),context);
  context.diagrams=context.ZtbDiagrams.install({api:context.api,currentSite:()=>null,projectId:()=>context.projectSaver.project.id,touch(){}});
  const source=fs.readFileSync(path.join(__dirname,'../ui/app.js'),'utf8');
  const part=(start,end)=>source.slice(source.indexOf(start),source.indexOf(end,source.indexOf(start)));
  vm.runInContext(part('function showView(','function addSite(')+part('function renderDeployment()',"$('deployment-connections').onclick"),context);
  return {context,get,touches:()=>touches,requests:()=>requests,
    details:()=>get('deployment-results').querySelectorAll(),
    detail:(site,type)=>get('deployment-results').querySelectorAll().find(d=>d.dataset.disclosureKey===JSON.stringify([site,type]))};
}

test('status polling leaves saved projects and unchanged result nodes alone',async()=>{
  const w=workspace();w.context.deploymentJob.details=[{name:'Branch',vlans:0}];
  await w.context.pollDeployment();const detail=w.detail('Branch','configuration');detail.open=true;
  for(let i=0;i<4;i++)await w.context.pollDeployment();
  assert.equal(w.requests(),5);assert.equal(w.touches(),0);
  assert.equal(w.detail('Branch','configuration'),detail);assert.equal(detail.open,true);
  w.context.workspaceView='overview';await w.context.pollDeployment();
  assert.equal(w.touches(),1);assert.equal(w.context.workspaceView,'review');
  await w.context.pollDeployment();assert.equal(w.touches(),1);
  w.context.showView('sites');assert.equal(w.touches(),2,'real navigation still saves');
});

test('configuration, nested details, and download menu stay open when progress changes',async()=>{
  const w=workspace();
  w.context.deploymentJob.sites=[{name:'Branch',status:'partial',stages:{Site:true},
    artifacts:{run:'run-a',site:'0001',formats:['png','svg','vsdx']},
    ucaas:{primary:'WAN1',secondary:'WAN2',objects:[{key:'dest',name:'Destinations',values:['192.0.2.0/24']}],
      rules:[{name:'Web',destination_keys:['dest'],ports:['TCP 443'],port_key:'web'}]}}];
  await w.context.pollDeployment();
  for(const detail of w.details())detail.open=true;
  const menu=w.detail('Branch','diagram-download');
  assert.equal(menu.children.filter(child=>child.tagName==='BUTTON' && !child.disabled).length,5);
  w.context.deploymentJob.stage_progress={Branch:{VLANs:'running'}};
  await w.context.pollDeployment();assert.equal(w.details().length,4);
  assert.ok(w.details().every(detail=>detail.open));
  w.detail('Branch','configuration').open=false;
  w.context.deploymentJob.state='incomplete';w.context.deploymentJob.sites[0].stages.VLANs=false;
  await w.context.pollDeployment();assert.equal(w.detail('Branch','configuration').open,false,'respect a user closing an incomplete site');
  w.context.deploymentJob.id='run-b';w.context.renderDeployment();
  assert.equal(w.detail('Branch','configuration').open,true,'new run uses error defaults');
  assert.equal(w.detail('Branch','diagram-download').open,false);
  w.context.projectSaver.project.id='project-b';w.context.renderDeployment();assert.equal(w.details().length,0);
});

test('failed site creation explains why a deployed diagram is unavailable',()=>{
  const w=workspace();w.context.deploymentJob.state='incomplete';
  w.context.deploymentJob.sites=[{name:'Branch',status:'template_only',stages:{Site:false}}];
  w.context.renderDeployment();
  const messages=w.get('deployment-results').children[0].children.map(child=>child.textContent).join(' ');
  assert.match(messages,/No deployed site diagram/);assert.match(messages,/planned diagram/);
});

test('layout update shows progress, refreshes available exports, and opens the regenerated diagram',async()=>{
  const w=workspace(),calls=[];let finish;
  w.context.handleApi=(url,payload)=>{
    calls.push({url,payload});
    if(url.endsWith('/regenerate'))return new Promise(resolve=>{finish=resolve;});
    if(url.endsWith('/list'))return {runs:[]};
    if(url.endsWith('/download'))return {content:'updated-svg'};
    throw new Error(url);
  };
  const box=w.context.diagrams.actions({run:'run-a',site:'0001',formats:[],warning:'Export needs regeneration'},'Branch');
  const view=box.children[0],menu=box.children[1],feedback=box.children[2],warning=box.children[3],retry=menu.children.at(-1);
  assert.equal(view.disabled,true);assert.equal(retry.textContent,'Regenerate diagram');
  const updating=retry.onclick();assert.equal(retry.disabled,true);assert.equal(retry.textContent,'Updating layout…');
  assert.match(feedback.textContent,/Updating/);
  finish({sites:[{id:'0001',formats:['svg','png','vsdx'],warning:''}]});await updating;
  assert.equal(retry.disabled,false);assert.equal(retry.textContent,'Update diagram layout');
  assert.equal(view.disabled,false);assert.equal(warning.hidden,true);
  assert.equal(menu.children.filter(c=>c.tagName==='BUTTON' && c.disabled).length,0);
  assert.equal(feedback.textContent,'Diagram layout updated.');
  assert.equal(w.get('diagram-dialog').open,true);assert.equal(w.get('diagram-save-png').disabled,false);
  assert.match(w.get('diagram-image').src,/updated-svg$/);
  assert.deepEqual(calls.map(c=>c.url),['/api/diagrams/regenerate','/api/diagrams/list','/api/diagrams/download']);
});

test('layout update errors stay visible beside the control and allow retry',async()=>{
  const w=workspace();w.context.handleApi=async()=>{throw new Error('Diagram could not be updated');};
  const box=w.context.diagrams.actions({run:'run-a',site:'0001',formats:['svg']},'Branch');
  const menu=box.children[1],feedback=box.children[2],retry=menu.children.at(-1);
  menu.open=true;await retry.onclick();
  assert.equal(retry.disabled,false);assert.equal(menu.open,true);
  assert.equal(feedback.textContent,'Diagram could not be updated');assert.equal(w.get('diagram-dialog').open,false);
});

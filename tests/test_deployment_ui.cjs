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

test('summary overlaps require each checkbox, survive polling and reset on changed preview',async()=>{
  const w=workspace();w.context.previewSnapshot='';
  const overlap=id=>({confirmation_id:id,site_name:'Branch',new_subnet:'10.48.1.0/24',existing_subnet:'10.48.0.0/16',application_name:'Summary',enabled:true});
  w.context.deploymentJob={id:'preview-a',state:'ready',site_count:1,zpa_overlaps:[overlap('one'),overlap('two')]};
  w.context.renderDeployment();
  const host=w.get('zpa-overlap-review');
  const inputs=()=>host.children.filter(e=>e.className==='zpa-overlap-entry').map(e=>e.children.at(-1).children[0]);
  assert.equal(host.hidden,false);assert.equal(w.get('start-deployment').disabled,true);
  const first=inputs()[0];first.checked=true;first.onchange();
  assert.equal(w.get('start-deployment').disabled,true);
  const second=inputs()[1];second.checked=true;second.onchange();
  assert.equal(w.get('start-deployment').disabled,false);
  await w.context.pollDeployment();
  assert.equal(inputs()[0],first);assert.equal(first.checked,true);
  assert.equal(w.get('start-deployment').disabled,false);
  w.context.deploymentJob.zpa_overlaps[1]=overlap('changed');w.context.renderDeployment();
  assert.equal(host.confirmed.size,0);assert.equal(w.get('start-deployment').disabled,true);
  for(const input of inputs()){input.checked=true;input.onchange();}
  w.context.deploymentJob.id='preview-b';w.context.renderDeployment();
  assert.equal(host.confirmed.size,0);assert.equal(w.get('start-deployment').disabled,true);
});

test('rollouts without summary overlaps need no extra confirmation',()=>{
  const w=workspace();w.context.previewSnapshot='';
  w.context.deploymentJob={id:'preview',state:'ready',site_count:1,zpa_overlaps:[]};
  w.context.renderDeployment();
  assert.equal(w.get('zpa-overlap-review').hidden,true);
  assert.equal(w.get('start-deployment').disabled,false);
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

test('preview and results list copied forwarding rules and unchanged shared objects',()=>{
  const w=workspace(),texts=el=>[el.textContent,...el.children.flatMap(texts)];
  const forwarding={reference:'Utrecht-NL-BR',placement:'Same order as the reference site, around the platform default rules',skipped:0,
    rules:[{gateway_target:'a',display_name:'Guest-Block',source:'Guest Networks',destination:'All ZPA App Segments',ports:'Any',egress:'ge2.79 via 172.17.69.1',path:'None'}],
    objects:[{key:'id:152',action:'reuse',name:'Guest Networks',display_name:'Guest Networks',missing:['172.17.69.0/24']}]};
  w.context.deploymentJob={id:'preview-a',state:'ready',site_count:1,sites:[{name:'Branch',status:'preview',forwarding:{...forwarding,status:'planned'}}],details:[{name:'Branch',vlans:2,forwarding}]};
  w.context.renderDeployment();
  let shown=texts(w.get('deployment-results'));
  assert.ok(shown.includes('Forwarding rules'));
  assert.ok(shown.includes('Guest Networks → All ZPA App Segments · Ports: Any · ge2.79 via 172.17.69.1 · Path: None'));
  assert.ok(shown.includes('Shared object Guest Networks · unchanged · doesn’t include 172.17.69.0/24'));
  w.context.deploymentJob={id:'run-b',state:'completed',sites:[{name:'Branch',status:'success',forwarding:{...forwarding,status:'configured',
    objects:[{name:'Guest Networks',action:'reuse',status:'reused',missing:['172.17.69.0/24']}],
    policies:[{display_name:'Guest-Block',name:'Guest-Blo-Ab12Cd34',id:2001,status:'verified',gateway_target:'a'}]}}]};
  w.context.renderDeployment();
  shown=texts(w.get('deployment-results'));
  assert.ok(shown.includes('Shared object Guest Networks · unchanged · doesn’t include 172.17.69.0/24'));
  assert.ok(shown.includes('Guest-Block · Guest-Blo-Ab12Cd34 · ID 2001 · verified'));
});

test('site details list copied rules read-only, Gateway B only for HA',()=>{
  const elements=new Map(),element=(tag='div',text='')=>({tagName:tag,textContent:text,children:[],hidden:false,
    append(...items){this.children.push(...items);},replaceChildren(...items){this.children=[...items];}});
  const get=id=>{if(!elements.has(id))elements.set(id,element());return elements.get(id);};
  const rule=(target,name,extra={})=>({gateway_target:target,display_name:name,nh_interfaces_type:'lan_wan',src_group_id_set:[152],
    dst_group_id_set:[34],dst_app_segment_id_set:[],dst_port_group_id:-1,primary_int:'ge2.69',secondary_int:'ge2.69',...extra});
  const data={rules:[rule('a','Guest-Block'),rule('b','Guest-Block'),rule('a','Syslog',{nh_interfaces_type:'zpa',src_group_id_set:[35],dst_group_id_set:[],dst_app_segment_id_set:['7205']})],
    objects:[{id:152,name:'Guest Networks',display_name:'Guest Networks'},{id:34,name:'System-All-ZPA-Apps-Group',display_name:'All ZPA App Segments'},{id:35,name:'System-Local-Traffic-Group',display_name:'Local interface IPs'}]};
  const site={ha_enabled:false,fields:{forwarding_rules_json:JSON.stringify(data)}};
  const context=vm.createContext({$:get,node:element,currentSite:()=>site});
  const source=fs.readFileSync(path.join(__dirname,'../ui/app.js'),'utf8');
  const start=source.indexOf('function renderForwardingRules(');
  vm.runInContext(source.slice(start,source.indexOf('$("gateway-setup")',start)),context);
  context.renderForwardingRules();
  const rows=()=>get('forwarding-rule-rows').children.map(item=>item.children.map(child=>child.textContent).join(' | '));
  assert.equal(get('forwarding-rules').hidden,false);
  assert.equal(get('forwarding-rules-summary').textContent,'Forwarding rules from reference · 2 rules');
  assert.deepEqual(rows(),['Guest-Block | Guest Networks → All ZPA App Segments · Ports: Any · ge2.69',
    'Syslog | Local interface IPs → 1 ZPA application segment(s) · Ports: Any · ZPA']);
  site.ha_enabled=true;context.renderForwardingRules();
  assert.equal(rows().length,3);assert.ok(rows()[1].startsWith('Gateway B · Guest-Block'));
  site.fields.forwarding_rules_json='';context.renderForwardingRules();
  assert.equal(get('forwarding-rules').hidden,true);
});

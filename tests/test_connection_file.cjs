const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync(require('node:path').join(__dirname,'../ui/app.js'),'utf8');
function setup(result={tenant:'customer.example.com',sites:[],limited:false}){
  const elements=new Map(),calls=[];
  const get=id=>{if(!elements.has(id))elements.set(id,{value:'old-secret',hidden:false});return elements.get(id);};
  const c=vm.createContext({$:get,TextDecoder,referenceBusy:false,zpaBusy:false,active:false,
    deploymentActive:()=>c.active,referenceWorking:v=>c.referenceBusy=v,referenceError:m=>get('reference-error').textContent=m,
    clearZoneChoices(){},interfacesConnected:true,interfaceConnectionKey:'old',interfaceCatalog:{reset(){}},renderInterfaceFeedback(){},
    api:async(path,body)=>{calls.push({path,body:JSON.parse(JSON.stringify(body))});return result;},
    renderReferences(){},refreshConnectionStatus(){},pollDeployment(){},renderZoneStatus(){},
    previewSnapshot:'old-preview',tenantSites:[],selectedReference:'old'});
  vm.runInContext(source.slice(source.indexOf('async function connectReference('),source.indexOf('for(const id of ["pull-reference"'))+
    source.slice(source.indexOf('function showZpaConnection('),source.indexOf('async function connectZpa(')),c);
  return {c,get,calls};
}
const file=content=>({size:Buffer.byteLength(content),arrayBuffer:async()=>Buffer.from(content)});
test('file upload uses its own endpoint, clears form secrets, and shows tenant without retaining file',async()=>{
  const {c,get,calls}=setup();await c.connectReference(undefined,file('API_KEY=synthetic-key'));
  assert.equal(calls[0].path,'/api/connections/import');assert.equal(calls[0].body.content,'API_KEY=synthetic-key');
  assert.equal(get('connected-tenant').textContent,'customer.example.com');
  for(const id of ['tenant-key','zpa-client-secret','zpa-client-id'])assert.equal(get(id).value,'');
  assert.equal(get('connection-fields').hidden,true);assert.equal(c.previewSnapshot,null);assert.equal(c.referenceBusy,false);
});
test('ZPA failure is explicit while retaining the successful ZTB connection',async()=>{
  const {c,get}=setup({tenant:'customer.example.com',sites:[],zpa_error:'Unable to verify ZPA.'});
  await c.connectReference(undefined,file('synthetic-content'));
  assert.equal(get('zpa-connection').open,true);assert.equal(get('zpa-connection-error').hidden,false);
  assert.match(get('reference-error').textContent,/ZTB connected, but ZPA/);
  assert.equal(get('connected-tenant').textContent,'customer.example.com');
});
test('oversize, invalid encoding, and unreadable files fail before any connection request',async()=>{
  for(const f of [{size:65537}, {size:2,arrayBuffer:async()=>new Uint8Array([255,255])},
    {size:2,arrayBuffer:async()=>{throw new Error('private-content');}}]){
    const {c,get,calls}=setup();await c.connectReference(undefined,f);
    assert.equal(calls.length,0);assert.ok(get('reference-error').textContent);assert.doesNotMatch(get('reference-error').textContent,/private-content/);
    assert.equal(c.referenceBusy,false);
  }
});
test('deployment and other connection work block file uploads',async()=>{
  for(const flag of ['active','referenceBusy','zpaBusy']){
    const {c,calls}=setup();c[flag]=true;await c.connectReference(undefined,file('content'));assert.equal(calls.length,0);
  }
});

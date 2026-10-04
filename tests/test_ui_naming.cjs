const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync(require('node:path').join(__dirname,'../ui/app.js'),'utf8');

// Exercise the editor's real naming functions and input/branch handlers offline.
function editor() {
  const elements=new Map();
  function element() {
    return {value:'',type:'text',tagName:'INPUT',dataset:{},children:[],options:[],
      addEventListener(event,handler){this[event]=handler;},
      setAttribute(){},append(...children){this.children.push(...children);},
      replaceChildren(...children){this.children=children;},close(){},add(option){this.options.push(option);}};
  }
  const get=id=>{if(!elements.has(id))elements.set(id,element());return elements.get(id);};
  const fields=['site_name','gateway_name','gateway_name_b','location_type','zia_location_name','template_mode','new_template_name','dns_split','dns_private_domains','private_dns','wan_dns'];
  const inputs=fields.map(field=>Object.assign(get(field),{dataset:{field}}));
  const context=vm.createContext({structuredClone,$:get,
    document:{querySelectorAll:selector=>selector==='[data-field]' ? inputs : [],
      querySelector:selector=>get(selector.match(/"([^"]+)"/)[1])},
    node:()=>element(),markDirty(){},renderList(){},renderUcaas(){},notice(){}});
  const portion=(from,to)=>source.slice(source.indexOf(from),source.indexOf(to,source.indexOf(from)));
  vm.runInContext(`let batch=[],current=0,branchDrafts=[],branchSource=null,activeTab='';
    const currentSite=()=>batch[current];const dhcpRangeModes=new WeakMap(),vlanDnsModes=new WeakMap();
    function render(){renderLocationName();renderGatewayNames();renderTemplateName();}
    `+portion('const gatewayNameModes','const newSite')+
    portion('function renderDns()','function ucaasSelected(')+
    portion('document.querySelectorAll("[data-field]").forEach(input => input.addEventListener','$("site-form").addEventListener')+
    portion('function copySite(','function dhcpService(')+
    portion('function renderBranchDrafts()','$("add-vlan").onclick'),context);
  return {get,run:code=>vm.runInContext(code,context),
    load(site){context.loaded=site;vm.runInContext('batch=[loaded];current=0;render()',context);},
    edit(field,value){get(field).value=value;get(field).input();}};
}
const site=(fields={})=>({fields:{site_name:'Branch',location_type:'new',...fields},vlans:[]});

test('private-domain DNS is optional, reflects resolver edits, and follows branch copies',()=>{
  const e=editor();e.load(site());e.run('renderDns()');
  assert.equal(e.get('dns-options').hidden,true);
  e.get('dns_split').type='checkbox';e.get('dns_split').checked=true;e.get('dns_split').input();
  assert.equal(e.get('dns-options').hidden,false);
  assert.match(e.get('dns-preview-domains').textContent,/Enter private domains above/);
  assert.match(e.get('dns-preview-private').textContent,/Enter Private DNS servers above/);
  assert.match(e.get('dns-preview-wan-other').textContent,/Enter WAN DNS servers above/);
  e.edit('private_dns','172.30.150.253');e.edit('wan_dns','1.1.1.1');
  e.edit('dns_private_domains','mikedsecure.corp');
  assert.equal(e.get('dns-preview-domains').textContent,'mikedsecure.corp');
  assert.equal(e.get('dns-preview-private').textContent,'172.30.150.253');
  assert.equal(e.get('dns-preview-wan-system').textContent,'1.1.1.1');
  assert.equal(e.get('dns-preview-wan-other').textContent,'1.1.1.1');
  assert.equal(e.run('copySite(currentSite()).fields.dns_private_domains'),'mikedsecure.corp');
  e.get('dns_split').checked=false;e.get('dns_split').input();
  assert.equal(e.get('dns-options').hidden,true);
  assert.equal(e.run('currentSite().fields.dns_private_domains'),'mikedsecure.corp');
});

test('blank-site gateway and new location follow repeated site-name edits',()=>{
  const e=editor();e.load(site({site_name:''}));
  e.edit('site_name','Amsterdam');
  assert.equal(e.get('gateway_name').value,'Amsterdam-GW');
  assert.equal(e.get('zia_location_name').value,'Amsterdam');
  e.edit('site_name','Rotterdam');
  assert.equal(e.get('gateway_name').value,'Rotterdam-GW');
  assert.equal(e.get('zia_location_name').value,'Rotterdam');
});

test('new location choice replaces an inherited reuse-or-create location name',()=>{
  const e=editor();e.load(site({location_type:'auto',zia_location_name:'Utrecht-DTLS'}));
  e.edit('site_name','Amsterdam');e.edit('location_type','new');
  assert.equal(e.get('zia_location_name').value,'Amsterdam');
  assert.equal(e.get('location-use-site-name').checked,true);
  e.edit('site_name','Amsterdam-2');
  assert.equal(e.get('zia_location_name').value,'Amsterdam-2');
});

test('custom gateway and custom or existing locations remain unchanged',()=>{
  const e=editor();e.load(site({gateway_name:'ImportedGateway',zia_location_name:'ImportedLocation'}));
  e.edit('site_name','Amsterdam');
  assert.equal(e.get('gateway_name').value,'ImportedGateway');
  assert.equal(e.get('zia_location_name').value,'ImportedLocation');
  e.edit('location_type','existing');e.edit('site_name','Rotterdam');
  assert.equal(e.get('zia_location_name').value,'ImportedLocation');
  e.edit('gateway_name','CustomGateway');e.edit('site_name','Delft');
  assert.equal(e.get('gateway_name').value,'CustomGateway');
});

test('location override stays custom until use-site-name is re-enabled',()=>{
  const e=editor();e.load(site());
  e.get('location-use-site-name').checked=false;e.get('location-use-site-name').change();
  e.edit('zia_location_name','CustomLocation');e.edit('site_name','Amsterdam');
  assert.equal(e.get('zia_location_name').value,'CustomLocation');
  e.get('location-use-site-name').checked=true;e.get('location-use-site-name').change();
  assert.equal(e.get('zia_location_name').value,'Amsterdam');
});

test('generated HA peer names follow edits without creating a standalone peer',()=>{
  const e=editor();e.load({...site(),ha_enabled:true});
  e.edit('site_name','Amsterdam');
  assert.equal(e.get('gateway_name').value,'Amsterdam-GW');
  assert.equal(e.get('gateway_name_b').value,'Amsterdam-GW-B');
  e.edit('gateway_name_b','Custom-B');e.edit('site_name','Delft');
  assert.equal(e.get('gateway_name_b').value,'Custom-B');
  const other=editor();other.load(site());other.edit('site_name','Solo');
  assert.equal(other.get('gateway_name_b').value,'');
});

test('branch table renames generated gateways, preserves overrides, and copies resolved names',()=>{
  const e=editor();e.load({...site({site_name:'Source',gateway_name:'SourceCustom',location_type:'auto',zia_location_name:'Source-DTLS'}),reference:{id:'source'}});
  e.run('branchSource=copySite(batch[0])');e.get('branch-count').value='2';e.run('renderBranchDrafts()');
  const rows=e.get('branch-rows').children;
  const edit=(row,column,value)=>{const input=rows[row].children[column].children[0];input.value=value;input.oninput();};
  edit(0,0,'Amsterdam');assert.equal(rows[0].children[1].children[0].value,'Amsterdam-GW');
  edit(1,1,'CustomGateway');edit(1,0,'Delft');assert.equal(rows[1].children[1].children[0].value,'CustomGateway');
  e.get('branches-form').onsubmit({preventDefault(){}});
  assert.equal(e.run('batch[1].fields.gateway_name'),'Amsterdam-GW');
  assert.equal(e.run('batch[1].fields.zia_location_name'),'Amsterdam');
  assert.equal(e.run('batch[1].fields.location_type'),'new');
  assert.equal(e.run('batch[2].fields.gateway_name'),'CustomGateway');
  e.edit('site_name','Amsterdam-2');
  assert.equal(e.get('gateway_name').value,'Amsterdam-2-GW');
  assert.equal(e.get('zia_location_name').value,'Amsterdam-2');
  assert.equal(e.run('batch[0].fields.zia_location_name'),'Source-DTLS');
  assert.equal(e.run('batch[0].fields.gateway_name'),'SourceCustom');
  assert.equal(e.run('batch[0].fields.location_type'),'auto');
});

test('branch copies get their own new location instead of inheriting source reuse choices',()=>{
  for(const mode of ['new','auto','existing']) {
    const e=editor();e.load(site({site_name:'Source',location_type:mode,zia_location_name:'Shared location'}));
    e.run('branchSource=copySite(batch[0])');e.get('branch-count').value='2';e.run('renderBranchDrafts()');
    e.get('branches-form').onsubmit({preventDefault(){}});
    for(const index of [1,2]) {
      assert.equal(e.run(`batch[${index}].fields.location_type`),'new');
      assert.equal(e.run(`batch[${index}].fields.zia_location_name`),`Source-copy-0${index}`);
      assert.equal(e.run(`batch[${index}].fields.post`),'0');
    }
    assert.equal(e.get('location-use-site-name').checked,true);
    e.edit('site_name','Amsterdam');
    assert.equal(e.get('zia_location_name').value,'Amsterdam');
    assert.equal(e.run('batch[0].fields.location_type'),mode);
    assert.equal(e.run('batch[0].fields.zia_location_name'),'Shared location');
    assert.equal(e.run('batch[2].fields.zia_location_name'),'Source-copy-02');
    // Reuse remains an explicit choice after copying.
    e.edit('location_type','existing');e.edit('zia_location_name','Shared location');
    e.edit('site_name','Amsterdam-2');
    assert.equal(e.get('zia_location_name').value,'Shared location');
    assert.equal(e.run('batch[1].fields.location_type'),'existing');
  }
});

test('branch copies preserve an explicit no-location choice',()=>{
  const e=editor();e.load(site({location_type:'none',zia_location_name:'Unused'}));
  e.run('branchSource=copySite(batch[0])');e.get('branch-count').value='1';e.run('renderBranchDrafts()');
  e.get('branches-form').onsubmit({preventDefault(){}});
  assert.equal(e.run('batch[1].fields.location_type'),'none');
  assert.equal(e.get('location-name-option').hidden,true);
  e.edit('location_type','new');
  assert.equal(e.get('zia_location_name').value,'Branch-copy-01');
  assert.equal(e.get('location-use-site-name').checked,true);
});

test('creating branches preserves the reference WAN port and static addressing',()=>{
  const e=editor();
  e.load({...site({wan_interface_name:'ge5',wan0_ip:'192.0.2.157',wan0_mask:'24',wan0_gw:'192.0.2.1'}),reference:{id:'source'}});
  e.run('branchSource=copySite(batch[0])');e.get('branch-count').value='2';e.run('renderBranchDrafts()');
  e.get('branches-form').onsubmit({preventDefault(){}});
  for(const index of [0,1,2]) {
    assert.equal(e.run(`batch[${index}].fields.wan_interface_name`),'ge5');
    assert.equal(e.run(`batch[${index}].fields.wan0_ip`),'192.0.2.157');
    assert.equal(e.run(`batch[${index}].fields.wan0_gw`),'192.0.2.1');
  }
  e.run("batch[1].fields.wan_interface_name='ge6'");
  assert.equal(e.run('batch[0].fields.wan_interface_name'),'ge5');
  assert.equal(e.run('batch[2].fields.wan_interface_name'),'ge5');
});

test('template reuse is the default; clone names follow the site without changing the source',()=>{
  const e=editor();e.load(site({template_name:'Reference-Design',template_id:'source-id'}));
  assert.equal(e.get('template_mode').value,'existing');
  assert.equal(e.get('new-template-field').hidden,true);
  e.edit('template_mode','clone');e.edit('site_name','Amsterdam');
  assert.equal(e.get('new_template_name').value,'Amsterdam');
  assert.equal(e.get('template-use-site-name').checked,true);
  assert.equal(e.run('batch[0].fields.template_id'),'source-id');
  assert.equal(e.run('batch[0].fields.template_name'),'Reference-Design');
  e.get('template-use-site-name').checked=false;e.get('template-use-site-name').change();
  e.edit('new_template_name','Custom-Template');e.edit('site_name','Delft');
  assert.equal(e.get('new_template_name').value,'Custom-Template');
  e.edit('template_mode','existing');e.edit('template_mode','clone');
  assert.equal(e.get('new_template_name').value,'Custom-Template');
  e.get('template-use-site-name').checked=true;e.get('template-use-site-name').change();
  assert.equal(e.get('new_template_name').value,'Delft');
});

test('branch builder creates independent template names and preserves the reference and WAN',()=>{
  const e=editor();e.load({...site({site_name:'Reference',template_name:'Source-Design',wan_interface_name:'ge5'}),reference:{id:'ref'}});
  e.run('branchSource=copySite(batch[0])');e.get('branch-count').value='2';
  e.get('branches-template-mode').value='clone';e.run('renderBranchDrafts()');
  e.get('branches-form').onsubmit({preventDefault(){}});
  for(const index of [1,2]) {
    assert.equal(e.run(`batch[${index}].fields.template_mode`),'clone');
    assert.equal(e.run(`batch[${index}].fields.new_template_name`),`Reference-copy-0${index}`);
    assert.equal(e.run(`batch[${index}].fields.template_name`),'Source-Design');
    assert.equal(e.run(`batch[${index}].fields.wan_interface_name`),'ge5');
  }
  e.edit('site_name','Amsterdam');
  assert.equal(e.get('new_template_name').value,'Amsterdam');
  assert.equal(e.run('batch[0].fields.template_mode'),'existing');
  assert.equal(e.run('batch[0].fields.new_template_name'),undefined);
  assert.equal(e.run('batch[2].fields.new_template_name'),'Reference-copy-02');
});

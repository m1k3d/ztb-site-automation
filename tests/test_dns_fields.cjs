const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync(require('node:path').join(__dirname,'../ui/app.js'),'utf8');

function render(fields){
  const elements=new Map(),inputs={private_dns:{placeholder:''},dhcp_server_ip:{placeholder:''}};
  const context=vm.createContext({$:id=>{if(!elements.has(id))elements.set(id,{});return elements.get(id);},
    document:{querySelector:selector=>inputs[selector.match(/^\[data-field="(\w+)"\]$/)[1]]},
    currentSite:()=>({fields})});
  vm.runInContext(source.slice(source.indexOf('function renderDns()'),source.indexOf('function ucaasSelected(')),context);
  context.renderDns();
  return inputs;
}

test('blank private DNS shows that the WAN DNS servers are used',()=>{
  assert.equal(render({wan_dns:'1.1.1.1,8.8.8.8',private_dns:''}).private_dns.placeholder,'Uses WAN DNS: 1.1.1.1, 8.8.8.8');
  assert.equal(render({wan_dns:'',private_dns:''}).private_dns.placeholder,'Uses WAN DNS');
});

test('DHCP server IP hint follows the LAN DHCP mode instead of showing an example address',()=>{
  const hint=mode=>render({dhcp_service_mode:mode}).dhcp_server_ip.placeholder;
  assert.equal(hint('server'),'Not used in Server mode');
  assert.equal(hint('relay'),'Required for Relay mode');
  for(const mode of ['','inherit',undefined])assert.equal(hint(mode),'Only used for Relay mode');
});

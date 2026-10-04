const {test}=require('node:test');
const assert=require('node:assert/strict');
const {uplinks}=require('../ui/diagrams.js');
test('HA slots and additional WANs have distinct stable circuit keys',()=>{
  const site={ha_enabled:true,fields:{wan_interface_name:'ge7',wan1_interface_name:'ge7',copy_additional_wans:'1',additional_wans_json:JSON.stringify([{gateway_target:'b',interface:'ge8',tag:'20'}])}};
  assert.deepEqual(uplinks(site).map(w=>w.key),['a:ge7:','b:ge7:','b:ge8:20']);
  site.ha_enabled=false;site.fields.copy_additional_wans='0';
  assert.deepEqual(uplinks(site).map(w=>w.key),['a:ge7:']);
});

const test = require('node:test');
const assert = require('node:assert/strict');
const {automaticRange,addressPrefixes,addressSubnet,changeAddressing} = require('../ui/network.js');

test('range starts after the gateway and excludes broadcast', () => {
  assert.deepEqual(automaticRange('192.168.20.1', '24'), {start:'192.168.20.2',end:'192.168.20.254'});
  assert.deepEqual(automaticRange('192.168.20.10', '24'), {start:'192.168.20.11',end:'192.168.20.254'});
});
test('supports CIDR and dotted masks including non-octet boundaries', () => {
  for (const mask of ['26', '255.255.255.192', '10.20.0.128/26']) {
    assert.deepEqual(automaticRange('10.20.0.129', mask), {start:'10.20.0.130',end:'10.20.0.190'});
  }
});
test('handles high IPv4 addresses without signed integer overflow', () => {
  assert.deepEqual(automaticRange('200.1.2.1', '24'), {start:'200.1.2.2',end:'200.1.2.254'});
});
test('supports a one-address pool in a /30', () => {
  assert.deepEqual(automaticRange('10.0.0.1', '30'), {start:'10.0.0.2',end:'10.0.0.2'});
});
test('does not include or wrap around a gateway at the end of a subnet', () => {
  assert.throws(() => automaticRange('10.0.0.254', '24'), /No usable addresses remain/);
});
test('rejects /31, /32, and loopback interfaces', () => {
  for (const prefix of ['31','32']) assert.throws(() => automaticRange('10.0.0.1', prefix), /DHCP off/);
  assert.throws(() => automaticRange('10.0.0.1', '24', 'lo0,lo0'), /Loopback/);
});
test('rejects malformed addresses, invalid masks, and mismatched CIDRs', () => {
  for (const ip of ['10.0.0', '10.0.0.256', '010.0.0.1', '', '-1.0.0.1']) assert.throws(() => automaticRange(ip, '24'));
  for (const mask of ['', '33', '-1', '255.0.255.0', '10.1.0.0/24', '10.0.0.0/24/24']) assert.throws(() => automaticRange('10.0.0.1', mask));
});
test('rejects network and broadcast gateways', () => {
  for (const ip of ['10.0.0.0','10.0.0.255']) assert.throws(() => automaticRange(ip, '24'), /usable host/);
});

const network=(fields={})=>({name:'Users',default_gateway:'10.20.10.1',subnet:'24',interface:'ge2',dhcp_service:'inherit',dhcp_start:'10.20.10.40',dhcp_end:'10.20.10.90',...fields});
test('prefix changes preserve host octets and recalculate Automatic without mutating source',()=>{
  const vlan=network(),before=structuredClone(vlan);
  const next=changeAddressing(vlan,'10.20','10.30');
  assert.equal(next.default_gateway,'10.30.10.1');assert.equal(next.subnet,'24');
  assert.equal(next.dhcp_start,'10.30.10.2');assert.equal(next.dhcp_end,'10.30.10.254');
  assert.deepEqual(vlan,before);
});
test('prefix matching uses complete octets and leaves unrelated networks alone',()=>{
  assert.equal(changeAddressing(network(),'10.2','10.3'),null);
  assert.equal(changeAddressing(network(),'192.168','172.16'),null);
  assert.equal(changeAddressing(network(),'10','172').default_gateway,'172.20.10.1');
  assert.equal(changeAddressing(network(),'10.20.10','10.30.40').default_gateway,'10.30.40.1');
});
test('prefix replacement rejects malformed, unequal, and identical prefixes',()=>{
  for(const [from,to] of [['10.20','10'],['10.20','10.20'],['10.256','10.30'],['10.020','10.30'],['10.20.*.*','10.30'],['','10'],['10.20','10.30.0.0']])assert.throws(()=>addressPrefixes(from,to));
});
test('Custom ranges retain host endpoints and unmatched valid endpoints stay unchanged',()=>{
  const next=changeAddressing(network(),'10.20','10.30','custom');
  assert.equal(next.dhcp_start,'10.30.10.40');assert.equal(next.dhcp_end,'10.30.10.90');
  const broad=changeAddressing(network({subnet:'8',dhcp_start:'10.99.0.40',dhcp_end:'10.99.0.90'}),'10.20','10.30','custom');
  assert.equal(broad.dhcp_start,'10.99.0.40');assert.equal(broad.dhcp_end,'10.99.0.90');
});
test('invalid Custom endpoints block prefix changes rather than producing a partial change',()=>{
  for(const fields of [{dhcp_end:''},{dhcp_start:'10.99.0.40'},{dhcp_start:'10.20.10.1'},{dhcp_end:'10.20.10.255'},{dhcp_start:'10.20.10.100'}]) {
    assert.throws(()=>changeAddressing(network(fields),'10.20','10.30','custom'),/Custom DHCP/);
  }
});
test('CIDR and dotted masks retain subnet size when moving addresses',()=>{
  const cidr=changeAddressing(network({subnet:'10.20.10.0/25'}),'10.20','10.30');
  assert.equal(cidr.subnet,'10.30.10.0/25');assert.equal(cidr.dhcp_end,'10.30.10.126');
  const dotted=changeAddressing(network({subnet:'255.255.255.128'}),'10.20','10.30');
  assert.equal(dotted.subnet,'255.255.255.128');assert.equal(dotted.dhcp_end,'10.30.10.126');
  assert.throws(()=>changeAddressing(network({subnet:'10.99.0.0/24'}),'10.20','10.30'),/belong/);
  assert.throws(()=>changeAddressing(network({default_gateway:'10.20.10.0'}),'10.20','10.30'),/usable/);
});
test('DHCP-off management /32 can move without bringing stale ranges along',()=>{
  const next=changeAddressing(network({interface:'lo0',subnet:'32',dhcp_service:'off',dhcp_range:'stale',range_list:'stale'}),'10.20','10.30');
  assert.equal(next.default_gateway,'10.30.10.1');assert.equal(next.subnet,'32');
  assert.equal(next.dhcp_start,'');assert.equal(next.dhcp_end,'');
  assert.equal(next.dhcp_range,undefined);assert.equal(next.range_list,undefined);
  assert.throws(()=>changeAddressing(network({dhcp_service:'unexpected'}),'10.20','10.30'),/valid DHCP/);
});
test('network intervals account for non-octet subnet boundaries',()=>{
  const a=addressSubnet(network({default_gateway:'10.30.10.129',subnet:'25'}));
  const b=addressSubnet(network({default_gateway:'10.30.10.1',subnet:'24'}));
  assert.ok(a.first>=b.first && a.last<=b.last);
  const c=addressSubnet(network({default_gateway:'10.30.11.1',subnet:'24'}));
  assert.ok(c.first>b.last);
});
test('gateway DNS follows bulk changes while Custom DNS remains explicit',()=>{
  assert.equal(changeAddressing(network(),'10.20','10.30').per_network_dns,'10.30.10.1');
  assert.equal(changeAddressing(network({per_network_dns:'10.20.10.1'}),'10.20','10.30').per_network_dns,'10.30.10.1');
  assert.equal(changeAddressing(network({per_network_dns:'9.9.9.9'}),'10.20','10.30').per_network_dns,'9.9.9.9');
  assert.equal(changeAddressing(network({per_network_dns:'10.20.10.1'}),'10.20','10.30','auto','custom').per_network_dns,'10.20.10.1');
  assert.equal(changeAddressing(network({dhcp_service:'off'}),'10.20','10.30').per_network_dns,undefined);
});

"use strict";

// Pure IPv4 calculation shared by the editor and its offline tests.
(() => {
  function address(value) {
    const parts = String(value).trim().split(".");
    if (parts.length !== 4 || parts.some(part => !/^(0|[1-9]\d{0,2})$/.test(part) || Number(part) > 255)) {
      throw new Error("Enter a valid IPv4 gateway address.");
    }
    return parts.reduce((total, part) => total * 256 + Number(part), 0);
  }
  function format(value) {
    return [24, 16, 8, 0].map(shift => Math.floor(value / 2 ** shift) % 256).join(".");
  }
  function prefix(value) {
    if (/^\d{1,2}$/.test(value) && Number(value) <= 32) return Number(value);
    if (value.includes(".")) {
      const bits = address(value).toString(2).padStart(32, "0");
      if (/^1*0*$/.test(bits)) return bits.indexOf("0") === -1 ? 32 : bits.indexOf("0");
    }
    throw new Error("Enter a valid subnet prefix, CIDR, or subnet mask.");
  }
  function automaticRange(gateway, subnet, interfaces = "") {
    if (String(interfaces).split(",").some(port => port.trim().toLowerCase() === "lo0")) {
      throw new Error("Loopback management uses /32 with DHCP off.");
    }
    const ip = address(gateway), parts = String(subnet).trim().split("/");
    if (parts.length > 2) throw new Error("Enter a valid subnet prefix, CIDR, or subnet mask.");
    const length = prefix(parts.at(-1));
    if (length >= 31) throw new Error("Automatic DHCP needs a subnet of /30 or larger. Use DHCP off for /31 and /32.");
    const size = 2 ** (32 - length), first = Math.floor(ip / size) * size, last = first + size - 1;
    if (parts.length === 2 && Math.floor(address(parts[0]) / size) * size !== first) {
      throw new Error("The gateway must belong to the VLAN subnet.");
    }
    if (ip === first || ip === last) throw new Error("The gateway must be a usable host address.");
    if (ip + 1 > last - 1) throw new Error("No usable addresses remain after the gateway. Choose a custom range before the gateway, or change the addressing.");
    return {start: format(ip + 1), end: format(last - 1)};
  }
  function addressPrefixes(from, to) {
    const parse = value => {
      const parts = String(value).trim().split('.');
      if(parts.length < 1 || parts.length > 3 || parts.some(part=>!/^(0|[1-9]\d{0,2})$/.test(part) || Number(part)>255)) {
        throw new Error('Enter 1–3 complete IPv4 octets, such as 10.20.');
      }
      return parts;
    };
    const oldParts=parse(from),newParts=parse(to);
    if(oldParts.length!==newParts.length)throw new Error('Use the same number of octets in both prefixes.');
    if(oldParts.join('.')===newParts.join('.'))throw new Error('Enter a different new prefix.');
    return {oldParts,newParts};
  }
  function addressSubnet(vlan) {
    const ip=address(vlan.default_gateway),parts=String(vlan.subnet).trim().split('/');
    if(parts.length>2)throw new Error('Enter a valid subnet prefix, CIDR, or subnet mask.');
    const length=prefix(parts.at(-1)),size=2**(32-length),first=Math.floor(ip/size)*size,last=first+size-1;
    if(parts.length===2 && Math.floor(address(parts[0])/size)*size!==first)throw new Error('The gateway must belong to the VLAN subnet.');
    if(size>2 && (ip===first || ip===last))throw new Error('The gateway must be a usable host address.');
    return {ip,first,last,length};
  }
  function changeAddressing(vlan, from, to, rangeMode='auto', dnsMode) {
    const {oldParts,newParts}=addressPrefixes(from,to);
    const matches=value=>String(value).split('.').slice(0,oldParts.length).join('.')===oldParts.join('.');
    if(!matches(vlan.default_gateway))return null;
    const original=addressSubnet(vlan);
    const replace=value=>{
      address(value);
      return matches(value) ? [...newParts,...String(value).split('.').slice(oldParts.length)].join('.') : value;
    };
    const next={...vlan,default_gateway:replace(vlan.default_gateway)};
    if(String(vlan.subnet).includes('/')) {
      const size=2**(32-original.length);
      next.subnet=`${format(Math.floor(address(next.default_gateway)/size)*size)}/${original.length}`;
    }
    const network=addressSubnet(next);
    const service=String(vlan.dhcp_service || '').trim().toLowerCase().replaceAll('-','_');
    if(!['','on','inherit','non_airgapped','off','no_dhcp'].includes(service))throw new Error('Choose a valid DHCP service before changing addressing.');
    const enabled=['on','inherit','non_airgapped'].includes(service) || (!service && vlan.dhcp_start && vlan.dhcp_end);
    dnsMode ||= !vlan.per_network_dns || vlan.per_network_dns===vlan.default_gateway ? 'gateway' : 'custom';
    if(enabled && dnsMode==='gateway')next.per_network_dns=next.default_gateway;
    if(enabled) {
      if(rangeMode==='auto') {
        const range=automaticRange(next.default_gateway,next.subnet,next.interface);
        next.dhcp_start=range.start;next.dhcp_end=range.end;
      } else {
        if(Boolean(vlan.dhcp_start)!==Boolean(vlan.dhcp_end))throw new Error('Custom DHCP needs both range endpoints.');
        for(const key of ['dhcp_start','dhcp_end'])next[key]=vlan[key] ? replace(vlan[key]) : '';
        if(next.dhcp_start && next.dhcp_end) {
          const first=address(next.dhcp_start),last=address(next.dhcp_end);
          if(first>last || first<network.first || last>network.last || (network.length<31 && (first===network.first || last===network.last)) || (first<=network.ip && last>=network.ip)) {
            throw new Error('Custom DHCP range would be invalid. Adjust its endpoints or choose Automatic first.');
          }
        }
      }
    } else {next.dhcp_start='';next.dhcp_end='';}
    delete next.dhcp_range;delete next.range_list;
    return next;
  }
  const api = {automaticRange,addressPrefixes,addressSubnet,changeAddressing};
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else globalThis.ZtbNetwork = api;
})();

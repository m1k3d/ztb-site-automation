"""Copy secondary WAN networks after site creation, with exact API read-back."""
from copy import deepcopy
import ipaddress
import json
import re

from template_cloning import read_rows


class WanError(ValueError):
    pass


def selections(row):
    if row.get('copy_additional_wans', '0') != '1':
        return []
    try:
        values = json.loads(row.get('additional_wans_json', '[]'))
        if not isinstance(values, list) or not 1 <= len(values) <= 16:
            raise ValueError()
        result, seen = [], set()
        for raw in values:
            target, interface = raw['gateway_target'], raw['interface']
            if target not in ('a','b') or (target == 'b' and not row.get('gateway_name_b')):
                raise ValueError()
            if not isinstance(interface,str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_.:-]*',interface):
                raise ValueError()
            tag = str(int(raw.get('tag', '1')))
            if not 1 <= int(tag) <= 4094 or (target,interface,tag) in seen:
                raise ValueError()
            seen.add((target,interface,tag))
            primary = row.get('wan_interface_name' if target == 'a' else 'wan1_interface_name')
            if interface == primary:
                raise ValueError()
            name = raw.get('name','').strip()
            if not name or len(name) > 255 or raw.get('mode') not in ('dhcp','static'):
                raise ValueError()
            item = dict(name=name,gateway_target=target,interface=interface,tag=tag,mode=raw['mode'],
                        ip='',mask='',gateway='',dns=str(raw.get('dns','')).strip())
            if item['mode'] == 'static':
                host = ipaddress.IPv4Interface(f"{raw['ip']}/{raw['mask']}")
                next_hop = ipaddress.IPv4Address(raw['gateway'])
                if (host.ip.is_unspecified or host.ip.is_multicast or host.network.prefixlen > 30
                        or host.ip in (host.network.network_address,host.network.broadcast_address)
                        or next_hop not in host.network or next_hop in (host.ip,host.network.network_address,host.network.broadcast_address)):
                    raise ValueError()
                item.update(ip=str(host.ip),mask=str(host.network.prefixlen),gateway=str(next_hop))
            if item['dns']:
                item['dns'] = ','.join(str(ipaddress.IPv4Address(ip.strip())) for ip in item['dns'].split(','))
            result.append(item)
        return result
    except (ValueError,TypeError,KeyError,AttributeError):
        raise WanError('Review additional WANs: choose a separate WAN port, a valid gateway assignment, and complete DHCP or static addressing.') from None


def from_reference(networks, gateways, fields):
    result = []
    if len(gateways) <= 1 and len(networks) <= 1:
        return result
    owners = {g.get('gateway_id') or g.get('id'):target for g,target in zip(gateways,('a','b'))}
    for network in networks:
        if network.get('is_deleted'):
            continue
        target = owners.get(network.get('gateway_id'))
        if target is None:
            raise WanError('Cannot identify an additional WAN gateway')
        primary = fields.get('wan_interface_name' if target == 'a' else 'wan1_interface_name')
        if not primary:
            raise WanError('Cannot distinguish the primary WAN from additional WANs')
        if network.get('interface') == primary:
            continue
        if type(network.get('dhcp_client')) is not bool:
            raise WanError('Cannot identify additional WAN addressing mode')
        result.append(dict(name=network.get('display_name') or network['name'], gateway_target=target,
            interface=network['interface'],tag=str(network['tag']),mode='dhcp' if network['dhcp_client'] else 'static',
            ip='' if network['dhcp_client'] else network['default_gateway'],
            mask='' if network['dhcp_client'] else str(network['subnet']),
            gateway='' if network['dhcp_client'] else network['wan_nexthop_ip'],dns=network.get('per_network_dns') or ''))
    return sorted(result,key=lambda w:(w['gateway_target'],w['interface'],int(w['tag'])))


class AdditionalWans:
    def __init__(self,engine):
        self.engine=engine

    def plan(self,row,template_id):
        wans=selections(row)
        if not wans:
            return []
        ports=read_rows(self.engine.template_cloner.get,'/templates/'+template_id+'/interfaces')
        for wan in wans:
            slot='Gateway-1' if wan['gateway_target']=='a' else 'Gateway-2'
            if len([p for p in ports if p.get('gateway_id')==slot and p.get('name')==wan['interface']
                    and p.get('interface_type')=='wan'])!=1:
                raise WanError('Additional WAN interface is not a WAN port on the selected template gateway.')
        return wans

    @staticmethod
    def matches(network,wan,gateway_id):
        if (network.get('gateway_id')!=gateway_id or network.get('interface')!=wan['interface']
                or str(network.get('tag'))!=wan['tag'] or network.get('zone')!='WAN Zone'
                or network.get('dhcp_client') is not (wan['mode']=='dhcp')):
            return False
        if wan['mode']=='static' and any(str(network.get(k))!=wan[v] for k,v in
                [('default_gateway','ip'),('subnet','mask'),('wan_nexthop_ip','gateway')]):
            return False
        return str(network.get('per_network_dns') or '')==wan['dns']

    def apply(self,wans,site_id,gw_ids,cluster_id,row,report):
        report.update(status='incomplete',networks=[],requested=deepcopy(wans))
        e=self.engine
        targets=e.vlan_gateway_targets(site_id,gw_ids,wans,row)
        existing=e.list_site_vlans_v2(site_id)
        # Reject conflicting existing assignments before issuing any writes.
        for wan in wans:
            same=[n for n in existing if n.get('gateway_id')==targets[id(wan)] and n.get('interface')==wan['interface'] and str(n.get('tag'))==wan['tag']]
            if len(same)>1 or (same and not self.matches(same[0],wan,targets[id(wan)])):
                raise WanError('An existing network conflicts with an additional WAN. Inspect the site before recovery.')
        for wan in wans:
            gid=targets[id(wan)]
            found=[n for n in existing if self.matches(n,wan,gid)]
            if not found:
                ip=wan['ip'] or '0.0.0.0'; mask=wan['mask'] or '0'
                payload=dict(name=wan['name'][:16],display_name=wan['name'],cluster_id=cluster_id,gateways=gid,
                    interface=wan['interface'],tag=wan['tag'],zone='WAN Zone',ip_range=str(ipaddress.IPv4Network(f'{ip}/{mask}',strict=False).network_address),
                    subnet=mask,default_gateway=ip,dhcp_client=wan['mode']=='dhcp',wan_nexthop_ip=wan['gateway'],
                    dhcp_service='no_dhcp',dhcp_range='',network_type='slash32',event_type='addnetwork',
                    per_network_dns=wan['dns'],dns_forwarding=False,airgap_plus_mask=30,slash30_range='')
                report['pending_write']=dict(method='POST',payload=payload)
                ok,_=e.post_vlan(payload)
                if not ok:
                    raise WanError('Additional WAN creation failed or is uncertain. Inspect recorded resources before recovery.')
                existing=e.list_site_vlans_v2(site_id)
                found=[n for n in existing if self.matches(n,wan,gid)]
            if len(found)!=1:
                raise WanError('Additional WAN read-back did not match the plan. Inspect the site before recovery.')
            network=found[0]
            report['networks'].append(dict(id=network['id'],gateway_id=gid,**wan))
            report.pop('pending_write',None)
            if network.get('status')!='provisioned':
                payload=dict(name=network.get('name') or wan['name'][:16],subnet=str(network['subnet']),per_network_dns=wan['dns'],status='provisioned')
                report['pending_write']=dict(method='PUT',id=network['id'],payload=payload)
                response=e.put_json(e.API_V2+'/Network/update/'+network['id'],payload,headers=e._v3_headers())
                if response.status_code not in (200,204):
                    raise WanError('Additional WAN enablement is uncertain. Inspect the recorded network before recovery.')
            existing=e.list_site_vlans_v2(site_id)
            if len([n for n in existing if n.get('id')==network['id'] and n.get('status')=='provisioned' and self.matches(n,wan,gid)])!=1:
                raise WanError('Additional WAN configuration could not be verified.')
            report.pop('pending_write',None)
        report['status']='verified'
        return True

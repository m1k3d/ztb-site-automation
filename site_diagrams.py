"""Credential-free topology snapshots. Read-back is bounded, GET-only and best effort."""
from copy import deepcopy
from datetime import datetime, timezone
import colorsys
import hashlib
import ipaddress
import json
from urllib.parse import quote


def text(value):
    return str(value or '').strip()


def yes(value):
    return str(value).lower() in ('1', 'true', 'yes', 'provisioned')


def optional_bool(value):
    value = str(value).strip().lower()
    if value in ('true', '1', 'yes', 'y', 'on'):return True
    if value in ('false', '0', 'no', 'n', 'off'):return False
    return None


def network_access(raw, state):
    """VLAN defaults only: DHCP-off does not prove device isolation is off."""
    service = text(raw.get('dhcp_service')).lower().replace('-', '_')
    if not service and state == 'planned':
        has_range = bool(raw.get('dhcp_range') or raw.get('range_list') or
                         (raw.get('dhcp_start') and raw.get('dhcp_end')))
        service = 'inherit' if has_range else 'no_dhcp'
    airgap = {'inherit':'on', 'on':'on', 'non_airgapped':'off',
              'no_dhcp':'dhcp_off', 'off':'dhcp_off'}.get(service, 'unknown')
    share = raw.get('share_over_vpn', False if state == 'planned' else None)
    return dict(share_over_rt=optional_bool(share), airgap=airgap, state=state)


def options(value):
    if isinstance(value, str):
        try:
            value = json.loads(value or '{}')
        except ValueError:
            raise ValueError('Check the diagram labels.') from None
    if not isinstance(value, dict):
        raise ValueError('Check the diagram labels.')
    switch = value.get('switch', '')
    uplinks = value.get('uplinks', {})
    if not isinstance(switch, str) or len(switch) > 120 or not isinstance(uplinks, dict) or len(uplinks) > 34:
        raise ValueError('Diagram labels must be short names (up to 120 characters).')
    result = {'switch': switch.strip(), 'uplinks': {}}
    if value.get('logo') is not None:
        from diagram_branding import logo
        result['logo'] = logo(value['logo'])
    for key, item in uplinks.items():
        if (not isinstance(key, str) or len(key) > 120 or not isinstance(item, dict)
                or any(not isinstance(item.get(k, ''), str) or len(item.get(k, '')) > 120 for k in ('name', 'circuit'))):
            raise ValueError('Check the ISP names and circuit labels.')
        result['uplinks'][key] = {k: item.get(k, '').strip() for k in ('name', 'circuit')}
    return result


def address(ip, mask):
    try:
        host = ipaddress.IPv4Interface(f'{ip}/{mask}')
        if host.ip.is_unspecified:
            return '', ''
        return str(host.ip), str(host.network)
    except ValueError:
        return text(ip), ''


def wan_key(slot, interface, tag=''):
    return f'{slot}:{interface}:{tag}'


def ha_interfaces(ports, mode, state='planned'):
    """Infer one HA port per peer; ha-data is the enhanced-HA WAN Transit port."""
    links,notes=[],[]
    roles=['ha','ha-data'] if mode=='wan_edge_mode_ha' else ['ha']
    for slot in ('a','b'):
        inventory=ports.get(slot,[])
        for role in roles:
            names=sorted({text(p.get('name')) for p in inventory
                if (p.get('type') or p.get('interface_type'))==role and text(p.get('name'))
                and not (p.get('bond_member') or p.get('bonding_parent'))})
            label='WAN Transit' if role=='ha-data' else 'HA'
            if len(names)==1:
                links.append(dict(slot=slot,interface=names[0],role=role,state=state))
            else:
                notes.append(f"Gateway {slot.upper()}: {label} interface {'is not loaded' if not names else 'is ambiguous'}. Check the template interface assignments.")
    return links,notes


def network(raw, state='planned'):
    ip, subnet = address(raw.get('default_gateway') or raw.get('start_ip'), raw.get('subnet'))
    zone = text(raw.get('zone') or 'LAN Zone')
    ports = text(raw.get('interface'))
    kind = ('wan' if zone.lower().startswith('wan') else 'ha' if zone.lower().startswith('ha') else
            'management' if zone.lower().startswith('management') or 'lo0' in ports.lower().split(',') else 'lan')
    return dict(name=text(raw.get('display_name') or raw.get('name')), tag=text(raw.get('tag')),
                zone=zone, ip=ip, subnet=subnet, interface=ports, kind=kind,
                enabled=yes(raw.get('enabled', raw.get('status', True))), state=state,
                target=text(raw.get('gateway_target') or 'all'), owners=text(raw.get('gateway_id')),
                next_hop=text(raw.get('wan_nexthop_ip')), dhcp=yes(raw.get('dhcp_client')),
                access=network_access(raw, state))


def planned(row, vlans, settings=None, ports=None):
    settings = settings or {}
    opts = options(row.get('diagram_options_json', '{}'))
    ha = bool(row.get('gateway_name_b'))
    model = dict(version=2, name=text(row.get('site_name') or 'Untitled site'),
        location=' · '.join(text(row.get(k)) for k in ('city', 'country') if row.get(k)),
        template=text(row.get('new_template_name') if row.get('template_mode') == 'clone' else row.get('template_name') or row.get('template_id')),
        mode=settings.get('deployment_type') or ('ha_unverified' if ha else 'standalone'),
        platform=text(settings.get('platform_type') or settings.get('platform')),
        captured_at=datetime.now(timezone.utc).isoformat(), evidence='planned', site_id='',
        status='planned', gateways=[], wans=[], networks=[network(v) for v in vlans],
        ha_links=[], ha_notes=[], switch=opts['switch'] or 'Downstream switch', warnings=[],
        services=[], breakout=yes(row.get('ucaas_local_breakout')), options=opts)
    segment_name = ''
    try:
        from zpa_segments import build_segment_plan
        segment_plan = build_segment_plan(model['name'], [{**v, 'zpa_include':optional_bool(v.get('zpa_include')) is True} for v in vlans])
        if segment_plan:segment_name = segment_plan.application_name
    except (ValueError, KeyError, TypeError):
        pass  # Draft diagrams can be opened before all addressing is valid.
    for net, raw in zip(model['networks'], vlans):
        selected = optional_bool(raw.get('zpa_include', False)) is True
        net['ip_app_segment'] = dict(state='planned' if selected else 'not_selected', name=segment_name if selected else '')
    for index, slot in enumerate(('a', 'b') if ha else ('a',)):
        name = row.get('gateway_name_b' if index else 'gateway_name') or f'Gateway {slot.upper()}'
        iface = text(row.get('wan1_interface_name' if index else 'wan_interface_name'))
        model['gateways'].append(dict(slot=slot, name=text(name), id='', state='planned'))
        ip, subnet = address(row.get(f'wan{index}_ip'), row.get(f'wan{index}_mask'))
        model['wans'].append(dict(key=wan_key(slot, iface), slot=slot, interface=iface,
            tag='', ip=ip, subnet=subnet, next_hop=text(row.get(f'wan{index}_gw')),
            dhcp=not bool(ip), state='planned'))
    if row.get('copy_additional_wans') == '1':
        from additional_wans import selections
        for wan in selections(row):
            ip, subnet = address(wan['ip'], wan['mask'])
            model['wans'].append(dict(key=wan_key(wan['gateway_target'], wan['interface'], wan['tag']),
                slot=wan['gateway_target'], interface=wan['interface'], tag=wan['tag'], ip=ip, subnet=subnet,
                next_hop=wan['gateway'], dhcp=wan['mode']=='dhcp', state='planned'))
    if ha:
        model['ha_links'],model['ha_notes']=ha_interfaces(ports or {},model['mode'])
    if row.get('location_type', 'none') != 'none':
        model['services'].append(dict(name='ZIA', state='planned', label='Internet & SaaS'))
    if row.get('appc_provision') == '1':
        model['services'].append(dict(name='ZPA', state='planned', label='Private applications'))
    decorate(model)
    return model


def apply_segment_result(model, outcome):
    """Use this run's verified ZPA report; do not infer existence from ZPA enablement."""
    from zpa_segments import destination_networks
    report = outcome.zpa_segments or {}
    resources = report.get('resources') or {}
    app = resources.get('application') or {}
    subnets = destination_networks(report.get('subnets'))
    verified = (outcome.stages.get('ZPA segments') is True and report.get('status') == 'staged_disabled'
                and report.get('enabled') is False and app.get('verified') is True and bool(app.get('id'))
                and bool(app.get('name')) and app.get('name') == report.get('application_name') and subnets)
    for net in model['networks']:
        if net['kind'] not in ('lan', 'management'):continue
        current = net.get('ip_app_segment', dict(state='not_selected', name=''))
        covered = False
        if verified and net['state'] in ('confirmed', 'binding unverified'):
            try:
                network = ipaddress.IPv4Network(net['subnet'])
                covered = any(network.subnet_of(ipaddress.IPv4Network(value)) for value in subnets)
            except ValueError:
                pass
        if covered:
            net['ip_app_segment'] = dict(state='created_disabled', name=app['name'], id=str(app['id']))
        elif current['state'] != 'not_selected':
            # Failed writes can have uncertain outcomes; a checkbox is not proof.
            blocked = outcome.diagnostics.get('ZPA segments') == 'prerequisite_failed'
            net['ip_app_segment'] = {**current, 'state':'not_created' if blocked else 'unverified'}
        else:
            net['ip_app_segment'] = current


def decorate(model):
    """Assign stable VLAN colors and explicit circuit groups (never guess shared ISPs)."""
    used = set()
    hues = []
    for net in sorted(model['networks'], key=lambda n: (n['tag'], n['zone'], n['name'], n['interface'])):
        key = '|'.join(net[k] for k in ('tag', 'zone', 'name', 'interface'))
        hue = int(hashlib.sha256(key.encode()).hexdigest()[:8], 16) % 360
        spacing = min(27, 170/max(1,len(model['networks'])))
        while any(min(abs(hue-h),360-abs(hue-h)) < spacing for h in hues):
            hue = (hue + 137.508) % 360
        hues.append(hue)
        color = ''
        while not color or color in used:
            rgb = colorsys.hls_to_rgb(hue / 360, .39, .65)
            color = '#' + ''.join(f'{round(v*255):02x}' for v in rgb)
            hue = (hue + 137.508) % 360
        used.add(color)
        net['color'] = color
    for i, wan in enumerate(model['wans'], 1):
        custom = model['options']['uplinks'].get(wan['key'], {})
        wan['circuit'] = custom.get('circuit') or wan['key']
        wan['label'] = custom.get('name') or custom.get('circuit') or f'ISP {i}'


def read_rows(data):
    body = data.get('result', data) if isinstance(data, dict) else data
    rows = body.get('rows') if isinstance(body, dict) else body
    if not isinstance(rows, list) or any(not isinstance(x, dict) for x in rows):
        raise ValueError('Unrecognized inventory')
    if isinstance(body, dict):
        for k in ('count', 'total', 'total_count'):
            if k in body and int(body[k]) > len(rows):
                raise ValueError('Incomplete inventory')
    return rows


def capture(engine, prepared, outcome):
    """Read just this site. No retry loop, no writes, no raw response persistence."""
    model = planned(prepared.row, prepared.vlans, prepared.template_settings)
    model.update(evidence='readback', status=outcome.status, site_id=outcome.site_id)
    incomplete=[name for name,success in outcome.stages.items() if success is False]
    if incomplete:
        model['warnings'].append('Incomplete deployment stages: '+', '.join(incomplete)+'. Inspect the run report before recovery.')
    for item in model['gateways'] + model['wans'] + model['networks'] + model['ha_links'] + model['services']:
        item['state'] = 'unverified'
    for net in model['networks']:
        net['access']['state'] = 'unverified'
        if net['ip_app_segment']['state'] == 'planned':net['ip_app_segment']['state'] = 'unverified'

    def get(base, path, params=None):
        response = engine.client.request('GET', base + path, params=params, headers=engine._v3_headers(), timeout=8)
        if response.status_code != 200:
            raise ValueError('Read-back unavailable')
        return response.json()

    try:
        rows = read_rows(get(engine.API_V3, '/Gateway/', dict(search=model['name'], page=0, limit=100,
            gateway_type='isolation', template_id='', sort='location', sortdir='asc', refresh_token='enabled')))
        matches = [r for r in rows if any(text(r.get(k)) == model['name'] for k in ('site_name', 'location', 'location_display_name'))]
        if len(matches) != 1:
            raise ValueError('Ambiguous site')
        site = matches[0]
        cluster = site.get('cluster_info') or {}
        sid = text(cluster.get('site_id') or site.get('site_id'))
        if not sid or (outcome.site_id and sid != outcome.site_id):
            raise ValueError('Site identity mismatch')
        gateways = site.get('gateways') or cluster.get('gateways') or []
        known_ids = set(outcome.gateway_ids)
        observed_ids = {text(g.get('gateway_id') or g.get('id')) for g in gateways}
        if known_ids and not known_ids.issubset(observed_ids):
            raise ValueError('Gateway identity mismatch')
        model['site_id'] = sid
        for gateway in model['gateways']:
            found = [g for g in gateways if text(g.get('gateway_name') or g.get('name')) == gateway['name']]
            if len(found) == 1:
                gateway.update(id=text(found[0].get('gateway_id') or found[0].get('id')), state='confirmed')
        location = site.get('location_id') or cluster.get('location_id') or site.get('zia_location_id')
        for service in model['services']:
            if service['name'] == 'ZIA' and location:
                service['state'] = 'configured'
            elif service['name'] == 'ZPA' and outcome.stages.get('ZPA'):
                service['state'] = 'configured'
        template_id = text(outcome.template.get('id') or prepared.template_id)
        try:
            detail = get(engine.API_V3, '/templates/' + quote(template_id, safe=''))
            detail = detail.get('result', detail)
            if detail.get('deployment_type') in ('standalone', 'standard_mode_ha', 'wan_edge_mode_ha'):
                model['mode'] = detail['deployment_type']
            model['platform']=text(detail.get('platform_type') or model['platform'])
            if len(model['gateways'])==2:
                template_ports=read_rows(get(engine.API_V3,'/templates/'+quote(template_id,safe='')+'/interfaces',{'page':0,'size':100}))
                ports={slot:[p for p in template_ports if p.get('gateway_id')==gateway]
                    for slot,gateway in [('a','Gateway-1'),('b','Gateway-2')]}
                model['ha_links'],model['ha_notes']=ha_interfaces(ports,model['mode'],'unverified')
        except Exception:
            model['warnings'].append('HA mode/template settings use the deployment preview; post-deployment read-back was unavailable.')
        try:
            observed = [network(n, 'confirmed') for n in read_rows(get(engine.API_V2, '/Network/', {'siteId': sid, 'refresh_token': 'enabled'})) if not n.get('is_deleted')]
            for requested in model['networks']:
                found = [n for n in observed if n['kind'] == requested['kind'] and n['tag'] == requested['tag'] and
                         (n['name'] == requested['name'] or n['name'] == requested['name'][:16])]
                if len(found) == 1:
                    actual = found[0]
                    actual['target'] = requested['target']
                    for k in ('ip', 'subnet', 'interface', 'zone'):
                        if actual[k] != requested[k]:
                            model['warnings'].append(f"{requested['name']}: {k} differs from the request; showing API values.")
                    for k in ('share_over_rt', 'airgap'):
                        if actual['access'][k] != requested['access'][k] and actual['access'][k] not in (None, 'unknown'):
                            model['warnings'].append(f"{requested['name']}: {'routed-tunnel sharing' if k == 'share_over_rt' else 'Airgap mode'} differs from the request; showing API values.")
                    requested.update(actual)
                    if requested['kind'] == 'management' and (not requested['interface'] or outcome.stages.get('Loopback binding') is False):
                        requested['state'] = 'binding unverified'
                else:
                    requested['state'] = 'not confirmed'
            requested_keys = {(n['kind'], n['tag'], n['name']) for n in model['networks']}
            model['networks'].extend(n for n in observed if n['kind'] != 'wan' and (n['kind'], n['tag'], n['name']) not in requested_keys)
            for wan in model['wans']:
                gw = next(g for g in model['gateways'] if g['slot'] == wan['slot'])
                found = [n for n in observed if n['kind']=='wan' and n['interface']==wan['interface'] and
                         gw['id'] and n['owners']==gw['id'] and (not wan['tag'] or n['tag']==wan['tag'])]
                if len(found) == 1:
                    wan.update({k: found[0][k] for k in ('ip', 'subnet', 'next_hop', 'dhcp', 'state', 'enabled')})
            for actual in (n for n in observed if n['kind']=='wan'):
                owners=[g for g in model['gateways'] if g['id'] and g['id']==actual['owners']]
                if len(owners)!=1:continue
                slot=owners[0]['slot']
                if any(w['slot']==slot and w['interface']==actual['interface'] and (not w['tag'] or w['tag']==actual['tag']) for w in model['wans']):continue
                model['wans'].append(dict(key=wan_key(slot,actual['interface'],actual['tag']),slot=slot,
                    **{k:actual[k] for k in ('interface','tag','ip','subnet','next_hop','dhcp','state','enabled')}))
        except Exception:
            model['warnings'].append('Network read-back unavailable. Unverified addresses show requested configuration.')
        try:
            inventory = read_rows(get(engine.API_V2, '/Gateway/interfaces', {'siteID': sid, 'refresh_token': 'enabled'}))
            ports={}
            for gateway in model['gateways']:
                match = [g for g in inventory if g.get('gateway_id') == gateway['id']]
                if len(match) != 1:
                    continue
                ports[gateway['slot']]=match[0].get('interfaces', [])
            if len(model['gateways'])==2:
                links,notes=ha_interfaces(ports,model['mode'],'confirmed')
                by_role={(p['slot'],p['role']):p for p in model['ha_links']}
                for link in links:
                    key=(link['slot'],link['role']);expected=by_role.get(key)
                    if expected and expected['interface']!=link['interface']:
                        model['warnings'].append(f"Gateway {link['slot'].upper()}: {'WAN Transit' if link['role']=='ha-data' else 'HA'} interface differs from the template; showing site API values.")
                    by_role[key]=link
                model['ha_links']=list(by_role.values())
                # Retained template assignments are still useful when the site
                # inventory has not populated yet; do not describe them as missing.
                confirmed={(p['slot'],p['role']) for p in links}
                model['ha_notes']=[n for n in notes if not any(
                    n.startswith(f"Gateway {p['slot'].upper()}: {'WAN Transit' if p['role']=='ha-data' else 'HA'} interface")
                    for p in by_role.values())]
                for key,link in by_role.items():
                    if key not in confirmed:
                        model['ha_notes'].append(f"Gateway {link['slot'].upper()}: {'WAN Transit' if link['role']=='ha-data' else 'HA'} uses template port {link['interface']}; site verification is pending.")
        except Exception:
            model['warnings'].append('Interface read-back unavailable; HA links need verification.')
        apply_segment_result(model, outcome)
    except Exception:
        model['warnings'].append('Site identity/read-back could not be verified. Diagram shows the submitted configuration.')
    if any(n['state'] not in ('confirmed', 'configured') for n in model['networks'] + model['wans'] + model['gateways']):
        model['warnings'].append('Unverified labels require configuration review.')
    decorate(model)
    return model


def capture_results(engine, plan, result, progress=None):
    by_name = {s.row['site_name']: s for s in plan.sites}
    for outcome in result.sites:
        if not outcome.stages.get('Site') or outcome.name not in by_name:
            continue
        try:
            if progress:progress(outcome.name,'Site diagram','reading configuration')
            outcome.diagram = capture(engine, by_name[outcome.name], outcome)
            if progress:progress(outcome.name,'Site diagram','snapshot captured')
        except Exception:
            outcome.diagram = {'error': 'Diagram capture unavailable. Deployment outcome is unchanged.'}

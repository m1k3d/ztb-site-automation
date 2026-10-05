import base64
from copy import deepcopy
import io
import json
import struct
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import zipfile
from xml.etree import ElementTree as ET

from app import normalize_batch
from deployment_engine import SiteResult, BatchResult
from diagram_store import DiagramStore
from diagram_render import svg, vsdx, png, scene, edge_paths, appliance_model, vlan_badges, circuit_colors, schedule_blocks, NS, BLUE, TEAL, PURPLE, TRANSIT
from site_diagrams import planned, capture, options, decorate, ha_interfaces, network, apply_segment_result
from run_report import reserve_report


def example(ha=False, enhanced=False, count=4):
    row=dict(site_name='Amsterdam · Engineering',gateway_name='AMS-GW',template_name='ZT800 Branch',
        city='Amsterdam',country='Netherlands',wan_interface_name='ge7',wan0_ip='40.40.40.2',wan0_mask='29',wan0_gw='40.40.40.1',
        location_type='new',appc_provision='1',ucaas_local_breakout='1',post='1',
        copy_additional_wans='1',additional_wans_json=json.dumps([dict(name='Secondary WAN',gateway_target='a',interface='ge8',tag='1',mode='dhcp')]))
    if ha:row.update(gateway_name_b='AMS-GW-B',wan1_interface_name='ge7',wan1_ip='40.40.40.3',wan1_mask='29',wan1_gw='40.40.40.1',vrrp_link_interface='ge4')
    vlans=[dict(name=['Corporate','Voice','Printers','Guest'][i%4]+(f' {i+1}' if i>3 else ''),tag=str((i+1)*10),default_gateway=f'172.30.{i+1}.1',subnet='24',zone=['Corporate','Voice','Printers','Guest'][i%4]+' Zone',interface='ge6',enabled=True) for i in range(count)]
    vlans.append(dict(name='Management',tag='1',default_gateway='172.30.65.1',subnet='32',zone='Management Zone',interface='lo0',enabled=True))
    settings={'deployment_type':'wan_edge_mode_ha' if enhanced else 'standard_mode_ha' if ha else 'standalone','platform_type':'zt800'}
    ports={slot:[dict(name='ge4',type='ha'),*([dict(name='ge5',type='ha-data')] if enhanced else [])] for slot in ('a','b')}
    return row,vlans,settings,ports


class DiagramTests(unittest.TestCase):
    def test_zpa_service_is_visible_without_provisioning_or_segment_selection(self):
        row,vlans,settings,ports=example()
        row['appc_provision']='0'
        model=planned(row,vlans,settings,ports)
        self.assertEqual(next(s['state'] for s in model['services'] if s['name']=='ZPA'),'context')
        self.assertTrue(all(net['ip_app_segment']['state']=='not_selected' for net in model['networks']))
        for legacy in (False,True):
            if legacy:model['services']=[s for s in model['services'] if s['name']!='ZPA']
            before=deepcopy(model)
            drawing=scene(model)
            self.assertIn('service-ZPA',drawing.groups)
            self.assertTrue(any(e['source']=='gw-a' and e['target']=='service-ZPA' for e in drawing.edges))
            self.assertIn(b'Zscaler Private Access',svg(model))
            self.assertEqual(model,before,'rendering must not rewrite saved evidence')

    def test_addressing_exclusion_colors_and_shared_circuits(self):
        row,vlans,settings,ports=example(True)
        row['diagram_options_json']=json.dumps({'switch':'Access stack','uplinks':{'a:ge7:':{'circuit':'Fiber','name':'ISP A'},'b:ge7:':{'circuit':'Fiber'}}})
        model=planned(row,vlans,settings,ports)
        self.assertEqual(model['wans'][0]['subnet'],'40.40.40.0/29')
        self.assertEqual(model['wans'][0]['circuit'],model['wans'][1]['circuit'])
        self.assertNotEqual(model['wans'][0]['circuit'],model['wans'][2]['circuit'])
        self.assertEqual(model['networks'][-1]['kind'],'management')
        colors={n['name']:n['color'] for n in model['networks']}
        self.assertEqual(len(set(colors.values())),len(colors))
        reversed_model=planned(row,list(reversed(vlans)),settings,ports)
        self.assertEqual(colors,{n['name']:n['color'] for n in reversed_model['networks']})

    def test_vlan_access_modes_missing_values_and_legacy_snapshots(self):
        for mode,expected in [('inherit','On'),('on','On'),('non_airgapped','Off (Lite)'),
                              ('non-airgapped','Off (Lite)'),('no_dhcp','DHCP off'),('off','DHCP off')]:
            net=network(dict(dhcp_service=mode,share_over_vpn='FALSE'),'confirmed')
            labels=[label for label,_ in vlan_badges(net)]
            self.assertEqual(labels[:2],['Routed tunnel: No','Airgap: '+expected])
        net=network(dict(share_over_vpn='yes',dhcp_service='inherit'),'confirmed')
        self.assertEqual(vlan_badges(net)[0][0],'Routed tunnel: Yes')
        for net in (network({},'confirmed'),{},dict(access={'state':'unverified','airgap':'on','share_over_rt':True})):
            self.assertEqual([label for label,_ in vlan_badges(net)][:2],['Routed tunnel: Unknown','Airgap: Unknown'])
        model=planned(*example())
        for net in model['networks']:net.pop('access');net.pop('ip_app_segment')
        self.assertIn(b'Routed tunnel: Unknown',svg(model))
        self.assertIn(b'IP app: Unknown',svg(model))

    def test_ip_segment_badge_requires_verified_creation_and_matching_destinations(self):
        row,vlans,settings,ports=example()
        vlans[0]['zpa_include']=True;vlans[-1]['zpa_include']=True
        model=planned(row,vlans,settings,ports)
        self.assertEqual(model['networks'][0]['ip_app_segment']['state'],'planned')
        self.assertEqual(model['networks'][1]['ip_app_segment']['state'],'not_selected')
        for net in model['networks']:net['state']='confirmed'
        name=model['networks'][0]['ip_app_segment']['name']
        report=dict(status='staged_disabled',enabled=False,application_name=name,
                    subnets=['172.30.1.0/24','172.30.65.1'],
                    resources={'application':dict(id='app-1',name=name,verified=True)})
        result=SiteResult(row['site_name'],'success',{'Site':True,'ZPA segments':True},zpa_segments=report)
        saved=deepcopy(model);apply_segment_result(saved,result)
        for index in (0,-1):self.assertEqual(saved['networks'][index]['ip_app_segment']['state'],'created_disabled')
        self.assertEqual(saved['networks'][1]['ip_app_segment']['state'],'not_selected')
        self.assertIn(b'IP app: Created (disabled)',svg(saved))
        for change in ('unverified','wrong_subnet','partial_coverage','readback_missing','failed_stage'):
            saved=deepcopy(model);outcome=deepcopy(result)
            if change=='unverified':outcome.zpa_segments['resources']['application']['verified']=False
            if change=='wrong_subnet':outcome.zpa_segments['subnets']=['172.31.1.0/24']
            if change=='partial_coverage':outcome.zpa_segments['subnets']=['172.30.1.0/25']
            if change=='readback_missing':saved['networks'][0]['state']='unverified'
            if change=='failed_stage':outcome.stages['ZPA segments']=False
            apply_segment_result(saved,outcome)
            self.assertEqual(saved['networks'][0]['ip_app_segment']['state'],'unverified',change)
        outcome=deepcopy(result);outcome.stages['ZPA segments']=False
        outcome.diagnostics['ZPA segments']='prerequisite_failed'
        saved=deepcopy(model);apply_segment_result(saved,outcome)
        self.assertEqual(saved['networks'][0]['ip_app_segment']['state'],'not_created')

    def test_vlan_access_readback_overrides_draft_without_extra_api_calls(self):
        row,vlans,settings,_=example()
        vlans[0].update(share_over_vpn=False,dhcp_service='inherit',zpa_include=True)
        engine=self.engine(row);responses=list(engine.client.request.side_effect)
        responses[2].json.return_value['rows'][0].update(share_over_vpn=True,dhcp_service='non_airgapped')
        engine.client.request.side_effect=responses
        result=SiteResult(row['site_name'],'success',{'Site':True},site_id='site-1',gateway_ids=['gw-1'])
        saved=capture(engine,SimpleNamespace(row=row,vlans=vlans,template_settings=settings,template_id='t'),result)
        labels=[label for label,_ in vlan_badges(saved['networks'][0])]
        self.assertEqual(labels,['Routed tunnel: Yes','Airgap: Off (Lite)','IP app: Unverified'])
        self.assertEqual(engine.client.request.call_count,4)
        self.assertTrue(any('Airgap mode differs' in warning for warning in saved['warnings']))

    def test_svg_safe_complete_and_native_vsdx_structure(self):
        row,vlans,settings,ports=example(True,True,40)
        row['site_name']='Branch <script>alert(1)</script> & café'
        model=planned(row,vlans,settings,ports)
        xml=svg(model);root=ET.fromstring(xml)
        self.assertNotIn(b'<script>',xml)
        self.assertIn(b'&lt;script&gt;',xml)
        self.assertNotIn(b'foreignObject',xml)
        self.assertGreater(float(root.get('width')),float(root.get('height')))
        self.assertEqual(len(root.findall('{http://www.w3.org/2000/svg}g[@id="zte"]')),1)
        self.assertIn('40.40.40.0/29',xml.decode())
        self.assertIn('ZTB .2',xml.decode())
        with zipfile.ZipFile(io.BytesIO(vsdx(model))) as archive:
            for name in archive.namelist():
                if name.endswith(('.xml','.rels')):ET.fromstring(archive.read(name))
            self.assertIn(b'<Pages xmlns=',archive.read('visio/pages/pages.xml'))
            self.assertIn(b'r:id="rId1"',archive.read('visio/pages/pages.xml'))
            page=ET.fromstring(archive.read('visio/pages/page1.xml'))
            ns={'v':NS}
            groups=page.findall('.//v:Shape[@Type="Group"]',ns)
            self.assertGreaterEqual(len(groups),7)
            self.assertGreater(len(page.findall('.//v:Connect',ns)),6)
            self.assertGreater(len(page.findall('.//v:Text',ns)),50)
            self.assertEqual(len(page.findall('.//v:ForeignData',ns)),1)
            self.assertTrue(archive.read('visio/media/appliance.png').startswith(b'\x89PNG'))
            pages=ET.fromstring(archive.read('visio/pages/pages.xml'))
            self.assertEqual(len(pages.findall('v:Page',ns)),1)
            dimensions={c.get('N'):float(c.get('V')) for c in pages.findall('.//v:PageSheet/v:Cell',ns)}
            self.assertGreater(dimensions['PageWidth'],dimensions['PageHeight'])
        rendered=scene(model)
        direct=[e for e in rendered.edges if e['source'].startswith('gw-') and e['target'].startswith('gw-')]
        self.assertEqual(len(direct),1)
        self.assertEqual(direct[0]['color'],TRANSIT)

    def test_ha_uses_template_ports_and_only_wan_transit_connects_peers(self):
        for enhanced in (False,True):
            row,vlans,settings,_=example(True,enhanced)
            row['vrrp_link_interface']='wrong-override'
            ports={slot:[dict(name='ge9' if slot=='a' else 'ge10',type='ha'),
                dict(name='xe2' if slot=='a' else 'xe3',type='ha-data')] for slot in ('a','b')}
            model=planned(row,vlans,settings,ports);drawing=scene(model)
            control=[l for l in model['ha_links'] if l['role']=='ha']
            self.assertEqual({l['interface'] for l in control},{'ge9','ge10'})
            self.assertEqual(len(control),2)  # Exactly one HA interface per gateway.
            ha_edges=[e for e in drawing.edges if e['color']==PURPLE and e['target']=='switch']
            self.assertEqual(len(ha_edges),2)
            center=sum(e['points'][-1][1] for e in ha_edges)/2
            self.assertEqual(len(ha_edges[0]['points']),len(ha_edges[1]['points']))
            for a,b in zip(ha_edges[0]['points'],ha_edges[1]['points']):
                self.assertAlmostEqual(a[0],b[0])
                self.assertAlmostEqual(a[1]+b[1],2*center)
            peer_edges=[e for e in drawing.edges if e['source']=='gw-a' and e['target']=='gw-b']
            self.assertEqual(len(peer_edges),int(enhanced))
            if enhanced:
                device=drawing.groups['gw-a'];center_x=device['x']+device['w']/2
                self.assertTrue(all(abs(p[0]-center_x)<.01 for p in peer_edges[0]['points']))
            labels=' '.join(i['text'] for i in drawing.items if i['kind']=='text')
            self.assertNotIn('evidence',labels.lower());self.assertNotIn('schedule',labels.lower())
            if enhanced:self.assertIn('A xe2 ↔ B xe3',labels)
        links,notes=ha_interfaces({'a':[dict(name='ge1',type='ha'),dict(name='ge2',type='ha')]},'wan_edge_mode_ha')
        self.assertFalse(links);self.assertTrue(any('ambiguous' in n for n in notes))

    def test_appliance_photo_follows_platform_including_custom_and_cloned_templates(self):
        for platform in ('zt400','zt600','zt800','zt8010'):
            row,vlans,settings,ports=example()
            row.update(template_mode='clone',new_template_name='Amsterdam-specific',template_name='Misleading ZT400')
            model=planned(row,vlans,{**settings,'platform_type':platform},ports)
            self.assertEqual(appliance_model(model),platform.upper())
            drawing=scene(model);photos=[i for i in drawing.items if i['kind']=='image']
            self.assertEqual(len(photos),1);self.assertLess(photos[0]['w'],drawing.width*.1)
            self.assertIsNone(photos[0]['group'])  # Reference photo is not a topology node.
        self.assertIsNone(appliance_model({'platform':'vm','template':'ZT800 style'}))
        self.assertIsNone(appliance_model({'platform':'unknown','template':'ZT800 style'}))
        self.assertEqual(appliance_model({'template':'zt600-ha-default'}),'ZT600')

    def test_landscape_keeps_all_network_rows_and_notes_on_the_page(self):
        for ha,count in [(False,0),(False,4),(True,8),(True,40)]:
            model=planned(*example(ha,ha,count))
            model['warnings']=['Check the management binding before activation.']
            rendered=scene(model)
            self.assertGreater(rendered.width,rendered.height)
            labels=[v['text'] for v in rendered.items if v['kind']=='text']
            for net in model['networks']:
                self.assertTrue(any(net['ip'] in label for label in labels),net['name'])
            self.assertTrue(any('management binding' in label for label in labels))
            for item in rendered.items:
                if 'x' in item:
                    self.assertGreaterEqual(item['x'],0)
                    self.assertGreaterEqual(item['y'],0)
                    self.assertLessEqual(item['x']+item['w'],rendered.width+1)
                    self.assertLessEqual(item['y']+item['h'],rendered.height+1)

    def test_visio_prints_on_one_landscape_page_and_services_start_at_each_gateway(self):
        for ha,enhanced in [(False,False),(True,False),(True,True)]:
            model=planned(*example(ha,enhanced))
            rendered=scene(model)
            self.assertGreaterEqual(rendered.width/rendered.height,1.8-1e-9)
            for gateway in model['gateways']:
                for service in model['services']:
                    edges=[e for e in rendered.edges if e['source']=='gw-'+gateway['slot'] and e['target']=='service-'+service['name']]
                    self.assertEqual(len(edges),1)
                    self.assertTrue(edges[0]['dashed'])
                service_edges={e['target']:e for e in rendered.edges if e['source']=='gw-'+gateway['slot'] and e['target'].startswith('service-')}
                upper=service_edges['service-ZIA']['points'];lower=service_edges['service-ZPA']['points']
                # Equal horizontal runs; neither service projects farther out.
                self.assertEqual([p[0] for p in upper],[p[0] for p in lower])
                gateway_center=(upper[0][1]+lower[0][1])/2
                for a,b in zip(upper[:3],lower[:3]):self.assertAlmostEqual(a[1]+b[1],2*gateway_center)
                cards=[rendered.groups['service-'+name] for name in ('ZIA','ZPA')]
                center=sum(card['y']+card['h']/2 for card in cards)/2
                self.assertAlmostEqual(upper[-1][1]+lower[-1][1],2*center)
            approaches={e['source']:e['points'] for e in rendered.edges if e['target']=='zte'}
            for a,b in zip(approaches['service-ZIA'],approaches['service-ZPA']):
                self.assertAlmostEqual(a[0],b[0]);self.assertAlmostEqual(a[1]+b[1],2*center)
            self.assertFalse(any(e['source'].startswith('isp-') and e['target'].startswith('service-') for e in rendered.edges))
            with zipfile.ZipFile(io.BytesIO(vsdx(model))) as archive:
                root=ET.fromstring(archive.read('visio/pages/pages.xml'))
                cells={c.get('N'):float(c.get('V')) for c in root.findall('.//{'+NS+'}PageSheet/{'+NS+'}Cell')}
                self.assertEqual(cells['PrintPageOrientation'],2)
                self.assertEqual(cells['OnPage'],1)
                self.assertEqual((cells['PagesX'],cells['PagesY']),(1,1))
                self.assertEqual(cells['PaperKind'],8)  # A3; landscape orientation is separate.

    def test_service_crossings_have_gaps_and_remain_single_glued_visio_connectors(self):
        model=planned(*example(True,True))
        drawing=scene(model);gapped=0
        for edge in drawing.edges:
            paths=edge_paths(edge,drawing.edges)
            self.assertEqual(paths[0][0],edge['points'][0])
            self.assertEqual(paths[-1][-1],edge['points'][-1])
            if len(paths)>1:
                self.assertTrue(edge['dashed'])
                gapped+=1
                self.assertTrue(all(a[-1]!=b[0] for a,b in zip(paths,paths[1:])))
            if not edge['dashed']:self.assertEqual(paths,[edge['points']])
        self.assertGreater(gapped,0)
        with zipfile.ZipFile(io.BytesIO(vsdx(model))) as archive:
            page=ET.fromstring(archive.read('visio/pages/page1.xml'));ns={'v':NS}
            connectors=page.findall('.//v:Shape[@NameU="Connection"]',ns)
            self.assertEqual(len(connectors),len(drawing.edges))
            connects=page.findall('.//v:Connect',ns)
            self.assertEqual(len(connects),2*len(connectors))
            underpasses=[n for n in connectors if len(n.findall('v:Section[@N="Geometry"]/v:Row[@T="MoveTo"]',ns))>1]
            self.assertEqual(len(underpasses),gapped)
            for connector in underpasses:
                self.assertEqual(len([c for c in connects if c.get('FromSheet')==connector.get('ID')]),2)

    def test_standalone_wan_paths_mirror_around_gateway_without_overlapping(self):
        for count in (1,2,3,4):
            model=planned(*example());wan=model['wans'][0]
            model['wans']=[{**wan,'circuit':f'ISP {i+1}','label':f'ISP {i+1}','interface':f'ge{i+1}'} for i in range(count)]
            # Uneven annotation lengths must not pull one ISP closer to the gateway.
            model['wans'][0]['interface']='ethernet-uplink-interface-1'
            drawing=scene(model)
            edges=[e for e in drawing.edges if e['target'].startswith('isp-')]
            services=[e for e in drawing.edges if e['source']=='gw-a' and e['target'].startswith('service-')]
            center=sum(e['points'][0][1] for e in services)/len(services)
            for upper,lower in zip(edges,reversed(edges)):
                for a,b in zip(upper['points'],lower['points']):
                    self.assertAlmostEqual(a[0],b[0])
                    self.assertAlmostEqual(a[1]+b[1],2*center)
            for i,a in enumerate(edges):
                for b in edges[i+1:]:
                    if a['points'][1][0]==b['points'][1][0]:
                        ar=sorted(p[1] for p in a['points'][1:3]);br=sorted(p[1] for p in b['points'][1:3])
                        self.assertLessEqual(min(ar[1],br[1]),max(ar[0],br[0]))
            if count==1:self.assertTrue(all(p[1]==center for p in edges[0]['points']))
            lowest_label=max(i['y']+i['h'] for i in drawing.items if i['kind']=='text' and i['w']==336)
            zpa=next(e for e in services if e['target']=='service-ZPA')
            self.assertGreater(zpa['points'][-1][1],lowest_label+10)

    def test_three_isp_ha_keeps_each_circuit_visible_and_consistent_across_exports(self):
        model=planned(*example(True,True))
        model['wans']=[]
        for circuit,interface in [('Fiber','ge7'),('Backup','ge8'),('Third','ge2')]:
            for slot in ('a','b'):
                model['wans'].append(dict(circuit=circuit,label=circuit+' ISP',slot=slot,interface=interface,
                    tag='',ip='',subnet='',next_hop='',dhcp=True,state='planned'))
        colors=circuit_colors(model);drawing=scene(model)
        self.assertEqual(len(set(colors.values())),3)
        self.assertFalse(set(colors.values()) & {BLUE,TEAL,PURPLE,TRANSIT})
        reordered={**model,'wans':list(reversed(model['wans']))}
        self.assertEqual(circuit_colors(reordered),colors)
        edges=[e for e in drawing.edges if e['target'].startswith('isp-')]
        self.assertEqual(len(edges),6)
        # No vertical WAN segment can hide another circuit's path.
        self.assertEqual(len({e['points'][1][0] for e in edges}),6)
        self.assertEqual(len({e['points'][0] for e in edges}),6)
        for ci,circuit in enumerate(('Fiber','Backup','Third')):
            key='isp-'+str(ci);links=[e for e in edges if e['target']==key]
            self.assertEqual({e['color'] for e in links},{colors[circuit]})
            self.assertEqual({e['source'] for e in links},{'gw-a','gw-b'})
            cloud=next(i for i in drawing.items if i['group']==key and i['kind']=='poly')
            self.assertEqual(cloud['stroke'],colors[circuit])
        blocks=[b for b in schedule_blocks(model) if b['kind']=='wan']
        for wan,block in zip(model['wans'],blocks):self.assertEqual(block['color'],colors[wan['circuit']])
        with zipfile.ZipFile(io.BytesIO(vsdx(model))) as archive:
            page=ET.fromstring(archive.read('visio/pages/page1.xml'));ns={'v':NS}
            native=page.findall('.//v:Shape[@NameU="Connection"]',ns)
            for edge,connector in zip(drawing.edges,native):
                self.assertEqual(connector.find('v:Cell[@N="LineColor"]',ns).get('V'),edge['color'])
            self.assertEqual(len(page.findall('.//v:Connect',ns)),2*len(drawing.edges))
        # Equal display names must not merge distinct physical circuits.
        for wan in model['wans']:wan['label']='Same provider'
        self.assertEqual(circuit_colors(model),colors)

    def engine(self,row):
        e=Mock(API_V3='https://tenant.invalid/api/v3',API_V2='https://tenant.invalid/api/v2')
        e._v3_headers.return_value={}
        native=dict(site_name=row['site_name'],cluster_info={'site_id':'site-1'},location_id=22,
            gateways=[dict(gateway_id='gw-1',gateway_name=row['gateway_name'])])
        nets=[dict(name='Corporate',display_name='Corporate',zone='Corporate Zone',tag='10',default_gateway='172.31.1.1',subnet='24',interface='ge6',gateway_id='gw-1',status='provisioned'),
              dict(name='WAN',zone='WAN Zone',tag='1',default_gateway='40.40.40.2',subnet='29',wan_nexthop_ip='40.40.40.1',interface='ge7',gateway_id='gw-1',dhcp_client=False,status='provisioned')]
        responses=[{'rows':[native]}, {'deployment_type':'standalone'}, {'rows':nets}, [{'gateway_id':'gw-1','interfaces':[]}]]
        e.client.request.side_effect=[Mock(status_code=200,json=Mock(return_value=value)) for value in responses]
        return e

    def test_readback_uses_actual_values_and_only_bounded_gets(self):
        row,vlans,settings,_=example()
        prepared=SimpleNamespace(row=row,vlans=vlans,template_settings=settings,template_id='template')
        result=SiteResult(row['site_name'],'partial',{'Site':True,'ZPA':True},site_id='site-1',gateway_ids=['gw-1'])
        e=self.engine(row);model=capture(e,prepared,result)
        self.assertEqual(model['networks'][0]['ip'],'172.31.1.1')
        self.assertEqual(model['networks'][0]['state'],'confirmed')
        self.assertEqual(model['networks'][1]['state'],'not confirmed')
        self.assertEqual(model['wans'][0]['state'],'confirmed')
        self.assertTrue(any('differs' in w for w in model['warnings']))
        for call in e.client.request.call_args_list:
            self.assertEqual(call.args[0],'GET');self.assertEqual(call.kwargs['timeout'],8)

    def test_ha_readback_uses_template_platform_and_interface_roles(self):
        row,vlans,settings,_=example(True,True)
        row.update(template_mode='clone',new_template_name='Site-specific template')
        prepared=SimpleNamespace(row=row,vlans=vlans,template_settings=settings,template_id='template')
        result=SiteResult(row['site_name'],'success',{'Site':True},site_id='site-1',gateway_ids=['gw-1','gw-2'])
        e=self.engine(row)
        gateways=[dict(gateway_id=identifier,gateway_name=row[name]) for identifier,name in [('gw-1','gateway_name'),('gw-2','gateway_name_b')]]
        template_ports=[dict(gateway_id=gateway,name=name,interface_type=role)
            for gateway in ('Gateway-1','Gateway-2') for name,role in [('ge4','ha'),('xe10','ha-data')]]
        observed=[dict(gateway_id=identifier,interfaces=[dict(name='ge4',interface_type='ha'),dict(name='xe10',interface_type='ha-data')]) for identifier in ('gw-1','gw-2')]
        responses=[{'rows':[dict(site_name=row['site_name'],cluster_info={'site_id':'site-1'},gateways=gateways)]},
            {'deployment_type':'wan_edge_mode_ha','platform_type':'zt8010'},
            {'result':template_ports,'count':4},{'rows':[]},observed]
        e.client.request.side_effect=[Mock(status_code=200,json=Mock(return_value=value)) for value in responses]
        model=capture(e,prepared,result)
        self.assertEqual(model['platform'],'zt8010')
        self.assertEqual(appliance_model(model),'ZT8010')
        self.assertEqual(len(model['ha_links']),4)
        self.assertEqual({p['interface'] for p in model['ha_links'] if p['role']=='ha-data'},{'xe10'})
        self.assertTrue(all(p['state']=='confirmed' for p in model['ha_links']))
        calls=e.client.request.call_args_list
        self.assertEqual(len(calls),5)
        self.assertIn('/templates/template/interfaces',calls[2].args[1])
        self.assertTrue(all(c.args[0]=='GET' and c.kwargs['timeout']==8 for c in calls))
        responses[-1]=[]  # The site's inventory may lag behind template creation.
        e.client.request.side_effect=[Mock(status_code=200,json=Mock(return_value=value)) for value in responses]
        pending=capture(e,prepared,result)
        self.assertEqual(len(pending['ha_links']),4)
        self.assertTrue(all(p['state']=='unverified' for p in pending['ha_links']))
        self.assertEqual(len(pending['ha_notes']),4)
        self.assertTrue(all('site verification is pending' in n for n in pending['ha_notes']))

    def test_failed_identity_does_not_confirm_another_site(self):
        row,vlans,settings,_=example();e=self.engine(row)
        result=SiteResult(row['site_name'],'partial',{'Site':True},site_id='wrong-site',gateway_ids=['gw-1'])
        model=capture(e,SimpleNamespace(row=row,vlans=vlans,template_settings=settings,template_id='t'),result)
        self.assertEqual(e.client.request.call_count,1)
        self.assertTrue(all(w['state']=='unverified' for w in model['wans']))

    def test_snapshots_survive_restart_isolate_projects_and_regenerate_without_api(self):
        model=planned(*example())
        with tempfile.TemporaryDirectory() as directory:
            path=reserve_report(directory);store=DiagramStore(directory)
            site=SiteResult(model['name'],'success',{'Site':True},diagram=model)
            store.save(path,BatchResult([site]),'a'*32)
            model['name']='Changed draft'
            new=DiagramStore(directory);run=new.list('a'*32)[0]
            self.assertEqual(new.list('b'*32),[])
            for bad in ('../secrets',path.stem):
                with self.assertRaises(ValueError):new.download(bad,'b'*32,'0001')
            file=new.download(path.stem,'a'*32,'0001')
            self.assertNotIn(b'Changed draft',base64.b64decode(file['content']))
            bundle=new.download(path.stem,'a'*32,format='zip')
            with zipfile.ZipFile(io.BytesIO(base64.b64decode(bundle['content']))) as z:
                self.assertEqual(len(z.namelist()),4)
                name=next(n for n in z.namelist() if n.endswith('.png'))
                self.assertTrue(z.read(name).startswith(b'\x89PNG\r\n\x1a\n'))
            new.regenerate(path.stem,'a'*32,'0001')
            self.assertEqual(new.list('a'*32)[0]['sites'][0]['formats'],['svg','vsdx','png'])

    def test_png_is_a_complete_bounded_landscape_image(self):
        for ha,count in [(False,4),(True,40)]:
            raw=png(planned(*example(ha,ha,count)))
            self.assertEqual(raw[:8],b'\x89PNG\r\n\x1a\n')
            width,height=struct.unpack('>II',raw[16:24])
            self.assertGreater(width,height)
            self.assertGreaterEqual(width,2400)
            self.assertLessEqual(width,4096)
            self.assertTrue(raw.endswith(b'IEND\xaeB`\x82'))

    def test_site_zip_contains_only_the_selected_sites_three_formats(self):
        with tempfile.TemporaryDirectory() as directory:
            store=DiagramStore(directory);path=reserve_report(directory)
            model=planned(*example())
            sites=[SiteResult(name,'success',{'Site':True},diagram={**model,'name':name}) for name in ['First','Second']]
            store.save(path,BatchResult(sites),'a'*32)
            bundle=store.download(path.stem,'a'*32,'0002','zip')
            self.assertEqual(bundle['filename'],'0002-Second.zip')
            with zipfile.ZipFile(io.BytesIO(base64.b64decode(bundle['content']))) as z:
                self.assertEqual(set(z.namelist()),{'0002-Second.svg','0002-Second.vsdx','0002-Second.png','README.txt'})
            image=store.download(path.stem,'a'*32,'0002','png')
            self.assertEqual(image['mime'],'image/png')
            with self.assertRaises(ValueError):store.download(path.stem,'a'*32,'0003','zip')

    def test_old_snapshot_gains_png_offline_and_png_failure_keeps_vector_exports(self):
        with tempfile.TemporaryDirectory() as directory:
            store=DiagramStore(directory);path=reserve_report(directory)
            site=SiteResult('Branch','success',{'Site':True},diagram=planned(*example()))
            with patch('diagram_render.png',side_effect=ValueError('renderer unavailable')):
                store.save(path,BatchResult([site]),'a'*32)
            self.assertEqual(site.status,'success')
            self.assertEqual(site.artifacts['formats'],['svg','vsdx'])
            self.assertTrue(site.artifacts['warning'])
            saved=(store.root/path.stem/'0001.json').read_bytes()
            with patch('requests.sessions.Session.request',side_effect=AssertionError('Must remain offline')):
                store.regenerate(path.stem,'a'*32,'0001')
            self.assertEqual((store.root/path.stem/'0001.json').read_bytes(),saved)
            self.assertEqual(store.list('a'*32)[0]['sites'][0]['formats'],['svg','vsdx','png'])

    def test_export_failure_keeps_snapshot_and_does_not_change_outcome(self):
        with tempfile.TemporaryDirectory() as directory:
            path=reserve_report(directory);site=SiteResult('Branch','success',{'Site':True},diagram=planned(*example()))
            with patch('diagram_render.vsdx',side_effect=ValueError('render error')):
                DiagramStore(directory).save(path,BatchResult([site]),'a'*32)
            self.assertEqual(site.status,'success');self.assertEqual(site.artifacts['formats'],['svg'])
            self.assertTrue((Path(directory)/'diagrams'/path.stem/'0001.json').exists())

    def test_documentation_metadata_never_enters_site_payload(self):
        from site_payload import build_site_payload
        row,vlans,_,_=example()
        batch=[{'fields':row,'vlans':vlans,'diagram':{'switch':'Floor 2','uplinks':{}}}]
        normalized=normalize_batch(batch)[0]
        self.assertEqual(json.loads(normalized['diagram_options_json'])['switch'],'Floor 2')
        self.assertNotIn('diagram',json.dumps(build_site_payload(normalized,{'location_type':'none'})))
        with self.assertRaises(ValueError):options({'switch':'x'*121})

    def test_http_downloads_are_token_and_project_scoped_and_offline(self):
        from app import Handler
        from project_store import ProjectStore
        with tempfile.TemporaryDirectory() as directory:
            projects=ProjectStore(Path(directory)/'projects',normalize_batch)
            workspace=dict(version=1,batch=[],current=-1,active_tab='site')
            for pid in ('a'*32,'b'*32):projects.create(pid,pid,workspace)
            store=DiagramStore(directory);path=reserve_report(directory)
            store.save(path,BatchResult([SiteResult('Branch','success',{'Site':True},diagram=planned(*example()))]),'a'*32)
            h=object.__new__(Handler)
            h.server=SimpleNamespace(server_address=('127.0.0.1',1234),token='local',projects=projects,diagrams=store)
            h.path='/api/diagrams/download'
            def request(pid,token='local',run=path.stem):
                payload=json.dumps(dict(project_id=pid,run=run,site='0001',format='svg')).encode()
                h.rfile=io.BytesIO(payload)
                h.headers={'Host':'127.0.0.1:1234','Origin':'http://127.0.0.1:1234','X-Local-Token':token,'Content-Type':'application/json','Content-Length':str(len(payload))}
                h.reply=Mock();h.do_POST();return h.reply.call_args.args[0]
            with patch('requests.sessions.Session.request',side_effect=AssertionError('Downloads must be offline')):
                self.assertEqual(request('a'*32),200)
                self.assertEqual(request('a'*32,token='wrong'),403)
                self.assertEqual(request('b'*32),400)
                self.assertEqual(request('a'*32,run='../secrets'),400)


if __name__=='__main__':unittest.main()

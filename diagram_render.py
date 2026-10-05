"""Deterministic vector scene shared by SVG and native, editable VSDX exports.

Bundled Zscaler Exchange artwork and native vector geometry; no remote assets.
Coordinates use a top-left origin in pixels; VSDX converts them to inches/bottom-left.
"""
import io
import base64
import colorsys
import json
import math
import re
import struct
import textwrap
import zipfile
from functools import lru_cache
from pathlib import Path
from xml.etree import ElementTree as ET

BLUE = '#236bf5'
INK = '#10264a'
MUTED = '#526782'
LINE = '#cbd7e7'
PURPLE = '#7652b8'
TEAL = '#127e83'
TRANSIT = '#ab6813'
WHITE = '#ffffff'
WAN_PALETTE = ('#526f91', '#477a59', '#b05c48', '#8f5c7b', '#827338', '#356c78', '#675e8b', '#80684c')


def circuit_colors(model):
    """Keep circuit identity consistent across peers, tables, and saved layouts."""
    circuits=sorted({w['circuit'] for w in model['wans']},key=lambda key:(key.casefold(),key))
    colors={};used=set(WAN_PALETTE) | {BLUE,INK,PURPLE,TEAL,TRANSIT}
    for index,key in enumerate(circuits):
        if index<len(WAN_PALETTE):color=WAN_PALETTE[index]
        else:
            hue=(index*.61803398875)%1
            while True:
                rgb=colorsys.hls_to_rgb(hue,.36,.32)
                color='#'+''.join(f'{round(value*255):02x}' for value in rgb)
                if color not in used:break
                hue=(hue+.1375)%1
        colors[key]=color;used.add(color)
    return colors


def pale(color):
    return '#'+''.join(f'{round(int(color[i:i+2],16)*.07+255*.93):02x}' for i in (1,3,5))


def clean(value):
    return ''.join(c for c in str(value) if c in '\n\t' or 32 <= ord(c) <= 0xd7ff or 0xe000 <= ord(c) <= 0xfffd or 0x10000 <= ord(c) <= 0x10ffff)


def brief(value, limit):
    return value if len(value)<=limit else value[:limit-1]+'…'


class Scene:
    def __init__(self, width):
        self.width, self.height = width, 1200
        self.items, self.edges = [], []
        self.groups = {}
        self.group = None

    def add(self, kind, **values):
        self.items.append(dict(kind=kind, group=self.group, **values))

    def rect(self, x, y, w, h, fill=WHITE, stroke='none', radius=0):
        self.add('rect', x=x, y=y, w=w, h=h, fill=fill, stroke=stroke, radius=radius)

    def poly(self, points, fill='none', stroke=INK, weight=2, closed=True):
        self.add('poly', points=points, fill=fill, stroke=stroke, weight=weight, closed=closed)

    def circle(self, x, y, r, fill=BLUE, stroke='none'):
        self.add('ellipse', x=x-r, y=y-r, w=r*2, h=r*2, fill=fill, stroke=stroke)

    def compound(self, contours, fill):
        self.add('compound', contours=contours, fill=fill, stroke='none')

    def label(self, x, y, value, size=16, color=INK, bold=False, width=None, align='left'):
        value = clean(value)
        lines = textwrap.wrap(value, width=max(8, int((width or self.width-100)/(size*.59))), break_long_words=True) or ['']
        for index, line in enumerate(lines):
            self.add('text', x=x, y=y+index*size*1.35, w=width or self.width-100, h=size*1.35,
                     text=line, size=size, fill=color, bold=bold, align=align)
        return len(lines)*size*1.35

    def entity(self, key, x, y, w, h):
        self.groups[key] = dict(x=x, y=y, w=w, h=h, anchors=[])
        self.group = key

    def edge(self, points, source, target, color=BLUE, dashed=False, width=2):
        for key, point in ((source, points[0]), (target, points[-1])):
            self.groups[key]['anchors'].append(point)
        self.edges.append(dict(points=points, source=source, target=target, color=color,
            dashed=dashed, width=width, source_anchor=len(self.groups[source]['anchors'])-1,
            target_anchor=len(self.groups[target]['anchors'])-1))


@lru_cache(maxsize=1)
def exchange_artwork():
    return json.loads((Path(__file__).parent/'data'/'diagram_artwork.json').read_text())


def exchange(s, x, y, width=210):
    art=exchange_artwork();scale=width/art['width'];height=art['height']*scale
    s.entity('zte',x,y,width,height)
    for part in art['shapes']:
        s.compound([[(x+px*scale,y+py*scale) for px,py in contour] for contour in part['contours']],part['fill'])
    s.group=None
    return height


def appliance_model(model):
    """Prefer the template's platform field; names are a legacy-snapshot fallback."""
    supported={'ZT400','ZT600','ZT800','ZT8010'}
    if model.get('platform'):
        value=re.sub(r'[\s_-]','',model['platform']).upper()
        return value if value in supported else None
    matches=set(re.findall(r'\bZT[\s_-]?(400|600|800|8010)\b',model.get('template',''),re.I))
    return 'ZT'+matches.pop() if len(matches)==1 else None


@lru_cache(maxsize=4)
def appliance_photo(name):
    if name not in ('ZT400','ZT600','ZT800','ZT8010'):raise ValueError('Unknown appliance model')
    data=(Path(__file__).parent/'data'/'appliances'/(name+'.png')).read_bytes()
    return data,struct.unpack('>II',data[16:24])


@lru_cache(maxsize=1)
def wordmark_artwork():
    return json.loads((Path(__file__).parent/'data'/'diagram_wordmark.json').read_text())


def wordmark(s, x, y, width, color=BLUE):
    art=wordmark_artwork();scale=width/art['width']
    for part in art['shapes']:
        s.compound([[(x+px*scale,y+py*scale) for px,py in contour] for contour in part['contours']],color)


def appliance(s, key, cx, cy, label, note, gateway=True, label_above=False):
    """Flat rack symbols with native, editable geometry and the vendor wordmark.

    These are topology symbols, not illustrations of a particular port layout.
    The model-specific physical photo is kept separately in the sheet header.
    """
    h=line_count(brief(label,58),19,280)*19*1.35
    s.entity(key,cx-142,cy-120 if label_above else cy-49,284,180 if label_above else max(160,129+h))
    # A straight-on chassis with mounting ears, without perspective or shadows.
    color=BLUE if gateway else INK
    for dx in (-89,76):
        s.poly([(cx+dx,cy-27),(cx+dx+13,cy-27),(cx+dx+13,cy+27),(cx+dx,cy+27)],WHITE,color,2)
        for dy in (-16,16):s.circle(cx+dx+6.5,cy+dy,1.8,color)
    s.rect(cx-76,cy-36,152,72,color if gateway else WHITE,color,radius=4)
    if gateway:
        wordmark(s,cx-57,cy-22,114,WHITE)
        s.label(cx-70,cy+10,'Zero Trust Branch',12,WHITE,True,140,'center')
    else:
        # A small, schematic Ethernet bank makes this recognizable as an access
        # switch. It is a symbol, not an inventory of physical ports.
        for dy,direction in [(-19,1),(-5,-1)]:
            start=cx-39*direction;end=cx+39*direction
            s.poly([(start,cy+dy),(end,cy+dy)],stroke=color,weight=2.4,closed=False)
            s.poly([(end-7*direction,cy+dy-5),(end,cy+dy),(end-7*direction,cy+dy+5)],stroke=color,weight=2.4,closed=False)
        for i in range(6):
            x=cx-58+i*20;y=cy+12
            s.poly([(x,y),(x+16,y),(x+16,y+9),(x+12,y+9),(x+12,y+12),(x+4,y+12),(x+4,y+9),(x,y+9)],'#e8eff9',color,1.2)
    label_y=cy-70-h if label_above else cy+57
    s.label(cx-140,label_y,brief(label,58),19,INK,True,280,'center')
    s.label(cx-140,label_y+3+h,note,12,MUTED,width=280,align='center')
    s.group=None


def isp_icon(s,key,cx,cy,name,color):
    s.entity(key,cx-125,cy-46,250,96)
    points=[];start=(.16,.70)
    for a,b,end in [((-.03,.70),(-.02,.37),(.19,.35)),((.20,.02),(.66,-.01),(.71,.28)),((1.05,.21),(1.12,.71),(.83,.70))]:
        for step in range(21):
            t=step/20;u=1-t
            points.append((cx-69+(u**3*start[0]+3*u*u*t*a[0]+3*u*t*t*b[0]+t**3*end[0])*138,cy-43+(u**3*start[1]+3*u*u*t*a[1]+3*u*t*t*b[1]+t**3*end[1])*82))
        start=end
    s.poly(points,pale(color),color,1.8)
    s.label(cx-117,cy+29,brief(name,44),16,color,True,234,'center')
    s.group=None


def link_label(s,x,y,text,width=220,color=MUTED,size=12,bold=False):
    h=line_count(text,size,width)*size*1.35
    s.rect(x-5,y-2,width+10,h+4,WHITE)
    s.label(x,y,text,size,color,bold,width=width,align='center')


def port_networks(model, slot):
    ports={}
    for net in model['networks']:
        if net['kind']!='lan' or net['target'] not in ('all',slot):continue
        assignments=[v.strip() for v in net['interface'].split(',') if v.strip()]
        selected=([assignments[min(0 if slot=='a' else 1,len(assignments)-1)]] if len(model['gateways'])==2 and assignments else assignments)
        for port in selected:ports.setdefault(port,[]).append(net)
    return ports


def trunk_entries(model, slot):
    """Keep every enabled VLAN visible in a grid sized for this gateway's links."""
    entries=[];label_width=230 if len(model['gateways'])==1 else 150
    for port,nets in sorted(port_networks(model,slot).items()):
        tags={}
        for net in nets:
            if not net.get('enabled',True):continue
            raw=str(net.get('tag','')).strip()
            tag=int(raw) if raw.isdigit() and 1<=int(raw)<=4094 else None
            tags.setdefault(tag,net.get('color',MUTED))
        chips=[dict(label=f'VL{tag}' if tag is not None else 'ID ?',color=tags[tag])
               for tag in sorted(tags,key=lambda tag:tag if tag is not None else 4095)]
        chip_width=max((math.ceil(len(chip['label'])*11*.59)+8 for chip in chips),default=0)
        for chip in chips:chip['width']=chip_width
        rows=[];row=[];used=0
        for chip in chips:
            if row and (len(row)==5 or used+4+chip['width']>label_width):
                rows.append(row);row=[];used=0
            used+=chip['width']+(4 if row else 0);row.append(chip)
        if row:rows.append(row)
        entries.append(dict(port=port,chips=chips,rows=rows,
                            height=20+23*max(1,len(rows)),width=label_width))
    return entries


def wan_annotation(wan):
    ip,hop=wan['ip'],wan['next_hop']
    short=bool(ip and hop and ip.split('.')[:3]==hop.split('.')[:3])
    title=f"{wan['slot'].upper()} / {wan['interface'] or 'WAN'} · {wan['subnet'] or ('DHCP' if wan['dhcp'] else 'Subnet unknown')}"
    address=f"ZTB {'.'+ip.split('.')[-1] if short else ip or 'DHCP'} → GW {'.'+hop.split('.')[-1] if short else hop or 'unknown'}"
    title_height=line_count(title,14,336)*14*1.35
    height=max(46,title_height+line_count(address,14,336)*14*1.35+8)
    return title,address,title_height,height


def topology(model):
    """A horizontal physical path with explicit gateway-to-Exchange service overlays."""
    circuits={}
    for wan in model['wans']:circuits.setdefault(wan['circuit'],[]).append(wan)
    colors=circuit_colors(model)
    gateways=model['gateways'];services=list(model['services']);ha=len(gateways)>1
    # Also show the ZPA service on older saved snapshots when their layout is updated.
    if not any(service['name']=='ZPA' for service in services):
        services.append(dict(name='ZPA',state='context',label='Private applications'))
    mirrored_wans=not ha and bool(circuits) and all(len(wans)==1 for wans in circuits.values())
    trunks={g['slot']:trunk_entries(model,g['slot']) for g in gateways}
    # Each circuit gets enough space for every gateway address; never abbreviate away data.
    circuit_y=[];cursor=158
    for wans in circuits.values():
        circuit_y.append(cursor);cursor+=125+sum(wan_annotation(w)[3] for w in wans)
    if mirrored_wans:
        # Equal circuit spacing accommodates the longest address annotation.
        pitch=max(125+wan_annotation(wans[0])[3] for wans in circuits.values())
        circuit_y=[158+i*pitch for i in range(len(circuits))]
        cursor=158+len(circuits)*pitch
    height=max(450 if not ha else 590,cursor+75)
    trunk_space=max((sum(entry['height']+5 for entry in entries) for entries in trunks.values()),default=0)+70
    height=max(height,(354 if ha else 32)+2*trunk_space)
    if mirrored_wans and any(service['name']=='ZPA' for service in services):
        # Leave space below the lowest ISP's addresses before the ZPA path.
        # The service pair stays mirrored about the same gateway centreline.
        annotation=max(wan_annotation(wans[0])[3] for wans in circuits.values())
        half_span=(circuit_y[-1]-circuit_y[0])/2
        height=max(height,2*(94+half_span+8+58+annotation+24+16))
    cy=height/2-16
    if mirrored_wans:
        # The connection enters each cloud eight pixels above its origin.
        # Centre those connection points, not the cloud-plus-label bounding box.
        midpoint=(circuit_y[0]+circuit_y[-1])/2
        circuit_y=[y+cy+8-midpoint for y in circuit_y]
    s=Scene(1800);s.height=height
    sx,gx,ix,zx=160,590,1040,1530
    gy={g['slot']:(cy if not ha else cy+(i-.5)*(height-354)) for i,g in enumerate(gateways)}
    for x,label in [(40,'01   LOCAL NETWORK'),(450,'02   ZERO TRUST BRANCH'),(895,'03   WAN TRANSPORT'),(1450,'04   ZERO TRUST EXCHANGE')]:
        s.label(x,0,label,11,MUTED,True,300)
    appliance(s,'switch',sx,cy,model['switch'],'Downstream access switch',False)
    for g in gateways:
        appliance(s,'gw-'+g['slot'],gx,gy[g['slot']],g['name'],('Gateway '+g['slot'].upper()+' · ' if ha else 'Zero Trust Branch · ')+g['state'],label_above=ha and g['slot']=='a')
    cloud_y=cy-83
    exchange(s,zx-34,cloud_y,270)
    s.label(zx-30,cloud_y+180,'Zscaler Zero Trust Exchange',16,INK,True,300,'center')
    if not services:s.label(zx-30,cloud_y+208,'ZIA / ZPA not requested',12,MUTED,width=300,align='center')
    # Service paths leave the top/bottom of the chassis, away from WAN ports.
    # Each gateway uses the same bend column for both services. Card entries and
    # Exchange approaches mirror vertically, with separate HA lanes kept apart.
    for j,service in enumerate(services):
        is_zia=service['name']=='ZIA';color=BLUE if is_zia else TEAL
        service_x=1250;service_y=34 if is_zia else 2*cy-154
        service_width=220;service_height=120
        key='service-'+service['name']
        s.entity(key,service_x,service_y,service_width,service_height)
        s.rect(service_x,service_y,service_width,service_height,color,radius=8)
        icon_x,icon_y=service_x+29,service_y+29
        if is_zia:
            # Globe: public internet and SaaS destinations.
            s.circle(icon_x,icon_y,13,'none',WHITE)
            s.add('ellipse',x=icon_x-6,y=icon_y-13,w=12,h=26,fill='none',stroke=WHITE)
            s.poly([(icon_x-13,icon_y),(icon_x+13,icon_y)],stroke=WHITE,weight=1.5,closed=False)
        else:
            # Application grid: private applications, independent of location.
            for dx in (-12,2):
                for dy in (-12,2):s.rect(icon_x+dx,icon_y+dy,10,10,'none',WHITE,radius=1)
        s.label(service_x+54,service_y+8,service['name'],32,WHITE,True,150)
        s.label(service_x+16,service_y+49,'Zscaler Internet Access' if is_zia else 'Zscaler Private Access',12,WHITE,width=188)
        s.label(service_x+16,service_y+70,'Internet & SaaS' if is_zia else 'Private Applications',15,WHITE,True,188)
        s.group=None
        direction=-1 if is_zia else 1
        end_y=cy+direction*37
        s.edge([(service_x+service_width,service_y+60),(zx-46,service_y+60),(zx-46,end_y),(zx-17,end_y)],key,'zte',color,True,3)
        for gi,g in enumerate(gateways):
            lane_offset=(gi-(len(gateways)-1)/2)*20
            lane=service_y+60-direction*lane_offset
            exit_x=gx+174+gi*16
            start_y=gy[g['slot']]+direction*36
            shoulder_y=gy[g['slot']]+direction*48
            s.edge([(gx+60,start_y),(gx+60,shoulder_y),(exit_x,shoulder_y),(exit_x,lane),(service_x,lane)],'gw-'+g['slot'],key,color,True,2.6)
    if not services:
        s.label(1190,height-25,'Service connections are not requested.',12,MUTED,width=560)
    # Give every physical WAN its own vertical lane. Shared columns would hide
    # circuit colors beneath one another. Keep the bundle centred and uniformly
    # spaced; reverse the upper peer's lanes to reduce crossings within a peer.
    wan_lanes=[];port_offsets={}
    for gi,g in enumerate(gateways):
        keys=[(ci,wi) for ci,wans in enumerate(circuits.values()) for wi,w in enumerate(wans) if w['slot']==g['slot']]
        pitch=min(14,52/max(1,len(keys)-1))
        port_offsets.update({key:(i-(len(keys)-1)/2)*pitch for i,key in enumerate(keys)})
        wan_lanes.extend(reversed(keys) if gi==0 else keys)
    lane_pitch=min(12,180/max(1,len(wan_lanes)-1))
    wan_routes={key:(gx+89+ix-67)/2+(i-(len(wan_lanes)-1)/2)*lane_pitch for i,key in enumerate(wan_lanes)}
    if mirrored_wans:
        # Opposite halves can share a bend column without sharing a segment.
        # Mirror outer/inner pairs; a single ISP leaves the gateway straight.
        pairs=(len(circuits)+1)//2
        for ci in range(len(circuits)):
            pair=min(ci,len(circuits)-1-ci)
            wan_routes[ci,0]=(gx+89+ix-67)/2+(pair-(pairs-1)/2)*lane_pitch
    for ci,(circuit,wans) in enumerate(circuits.items()):
        iy=circuit_y[ci]
        color=colors[circuit]
        isp_icon(s,'isp-'+str(ci),ix,iy,wans[0]['label'],color)
        top=iy+58
        for wi,wan in enumerate(wans):
            slot=wan['slot'];y=gy[slot]
            route=wan_routes[ci,wi];offset=port_offsets[ci,wi]
            cloud_y=iy-8+(wi-(len(wans)-1)/2)*min(12,30/max(1,len(wans)-1))
            # Terminate inside the opaque cloud so each entry meets its curved
            # outline rather than stopping short at one fixed boundary x.
            s.edge([(gx+89,y+offset),(route,y+offset),(route,cloud_y),(ix-10,cloud_y)],'gw-'+slot,'isp-'+str(ci),color,False,2.5)
            label,address,title_height,row_height=wan_annotation(wan)
            link_label(s,ix-168,top,label,336,INK,14,True)
            link_label(s,ix-168,top+title_height+3,address,336,INK,14)
            top+=row_height
    # HA connects through the switch. Enhanced HA's WAN Transit is direct between peers.
    for gi,g in enumerate(gateways):
        slot=g['slot'];y=gy[slot];label_offset=0
        for pi,trunk in enumerate(trunks[slot]):
            port=trunk['port']
            bend=340+pi*20;offset=pi*13
            s.edge([(sx+89,cy+offset),(bend,cy+offset),(bend,y+offset),(gx-88,y+offset)],'switch','gw-'+slot,INK,False,2.4)
            lower_peer=ha and gi==1
            label_width=trunk['width'];lx=498-label_width
            ly=y-trunk['height']-1-label_offset if lower_peer else y+10+label_offset
            link_label(s,lx,ly,port,label_width,INK,14,True)
            grid_width=max((sum(chip['width']+4 for chip in chips)-4 for chips in trunk['rows']),default=0)
            for ri,chips in enumerate(trunk['rows']):
                x=lx+(label_width-grid_width)/2
                for chip in chips:
                    s.rect(x,ly+20+ri*23,chip['width'],19,pale(chip['color']),chip['color'],radius=4)
                    s.label(x+4,ly+22+ri*23,chip['label'],11,chip['color'],True,chip['width']-8,'center')
                    x+=chip['width']+4
            if not trunk['chips']:link_label(s,lx,ly+20,'No enabled VLANs',label_width,INK,11)
            label_offset+=trunk['height']+5
        links=[l for l in model['ha_links'] if l['slot']==slot and l['role']=='ha'][:1]
        for link in links:
            direction=-1 if gi==0 else 1
            lane=y+direction*24;switch_y=cy+direction*24;bend=300
            # Mirror both HA paths about the pair's midpoint: one clean right-
            # angle route, with no extra rise/drop next to either gateway.
            s.edge([(gx-89,lane),(bend,lane),(bend,switch_y),(sx+89,switch_y)],'gw-'+slot,'switch',PURPLE,False,2.1)
            link_label(s,335,lane-23 if direction<0 else lane+7,link['interface']+' · HA',130,PURPLE,13,True)
    transit={slot:[l for l in model['ha_links'] if l['slot']==slot and l['role']=='ha-data'] for slot in ('a','b')}
    if ha and model['mode']=='wan_edge_mode_ha' and all(len(transit[slot])==1 for slot in ('a','b')):
        x=gx;middle=(gy['a']+gy['b'])/2
        s.edge([(x,gy['a']+36),(x,gy['b']-36)],'gw-a','gw-b',TRANSIT,False,2.6)
        s.label(x+17,middle-25,'WAN Transit',13,TRANSIT,True,110)
        s.label(x+17,middle-4,f"A {transit['a'][0]['interface']} ↔ B {transit['b'][0]['interface']}",13,INK,width=130)
        s.label(x+17,middle+16,'Direct peer link',11,MUTED,width=130)
    if model['ha_links']:s.label(25,height-23,'HA links via access switch',11,PURPLE,width=330)
    return s


def transplant(target, source, x, y, scale=1):
    """Move the whole topology, including native connector anchors, into its panel."""
    for item in source.items:
        v=dict(item)
        if 'x' in v:
            v['x']=x+v['x']*scale;v['y']=y+v['y']*scale
            v['w']*=scale;v['h']*=scale
        if 'points' in v:v['points']=[(x+px*scale,y+py*scale) for px,py in v['points']]
        if 'contours' in v:v['contours']=[[(x+px*scale,y+py*scale) for px,py in contour] for contour in v['contours']]
        for k in ('size','weight','radius'):
            if k in v:v[k]*=scale
        target.items.append(v)
    for key,g in source.groups.items():
        target.groups[key]=dict(x=x+g['x']*scale,y=y+g['y']*scale,w=g['w']*scale,h=g['h']*scale,anchors=[(x+px*scale,y+py*scale) for px,py in g['anchors']])
    for e in source.edges:target.edges.append({**e,'points':[(x+px*scale,y+py*scale) for px,py in e['points']],'width':e['width']*scale})


def edge_paths(edge, edges):
    """Small underpasses distinguish crossings from junctions in every format.

    Physical cables stay continuous. Service paths pass beneath physical cables
    and earlier service paths, retaining one native connector and both anchors.
    """
    if not edge['dashed']:return [edge['points']]
    index=next(i for i,other in enumerate(edges) if other is edge)
    blockers=[other for i,other in enumerate(edges) if other is not edge and (not other['dashed'] or i<index)]
    paths=[];current=[edge['points'][0]];gap=4.5
    for a,b in zip(edge['points'],edge['points'][1:]):
        vertical=a[0]==b[0];length=abs(b[1]-a[1]) if vertical else abs(b[0]-a[0])
        if not length:continue
        cuts=[]
        for other in blockers:
            for c,d in zip(other['points'],other['points'][1:]):
                if vertical and c[1]==d[1] and min(c[0],d[0])<a[0]<max(c[0],d[0]) and min(a[1],b[1])<c[1]<max(a[1],b[1]):
                    distance=abs(c[1]-a[1])
                elif not vertical and c[0]==d[0] and min(c[1],d[1])<a[1]<max(c[1],d[1]) and min(a[0],b[0])<c[0]<max(a[0],b[0]):
                    distance=abs(c[0]-a[0])
                else:continue
                cuts.append((max(0,distance-gap),min(length,distance+gap)))
        merged=[]
        for start,end in sorted(cuts):
            if merged and start<=merged[-1][1]:merged[-1]=(merged[-1][0],max(end,merged[-1][1]))
            else:merged.append((start,end))
        def point(distance):return (a[0]+(b[0]-a[0])*distance/length,a[1]+(b[1]-a[1])*distance/length)
        for start,end in merged:
            p=point(start)
            if current[-1]!=p:current.append(p)
            if len(current)>1:paths.append(current)
            current=[point(end)]
        if current[-1]!=b:current.append(b)
    if len(current)>1:paths.append(current)
    return paths


def line_count(value, size, width):
    return max(1,len(textwrap.wrap(clean(value),max(8,int(width/(size*.59))),break_long_words=True)))


def vlan_badges(net):
    access=net.get('access') or {}
    known=access.get('state') in ('planned','confirmed')
    shared=access.get('share_over_rt') if known else None
    sharing='Yes' if shared is True else 'No' if shared is False else 'Unknown'
    airgap={'on':'On','off':'Off (Lite)','dhcp_off':'DHCP off'}.get(access.get('airgap'),'Unknown') if known else 'Unknown'
    segment=net.get('ip_app_segment') or {}
    app={'planned':'Planned','not_selected':'Not selected','created_disabled':'Created (disabled)',
         'not_created':'Not created','unverified':'Unverified'}.get(segment.get('state'),'Unknown')
    return [('Routed tunnel: '+sharing,152),('Segmentation: '+airgap,164),('IP app: '+app,180)]


def vlan_badge_row(s,x,y,net):
    for label,width in vlan_badges(net):
        s.rect(x,y,width,22,WHITE,LINE,radius=4)
        s.label(x+4,y+3,label,11,INK,width=width-8)
        x+=width+6


def schedule_blocks(model):
    blocks=[]
    colors=circuit_colors(model)
    lan=sorted((n for n in model['networks'] if n['kind']=='lan'),key=lambda n:(int(n['tag']) if n['tag'].isdigit() else 0,n['name']))
    for n in lan:
        lines=max(line_count(n['name'],14,154)+line_count(n['zone'],11,154),line_count(n['subnet'] or 'Unknown',14,166)+line_count('GW '+(n['ip'] or 'Unknown'),13,166),line_count(n['interface'] or 'Unbound',14,105)+line_count(n['state']+(' · disabled' if not n['enabled'] else ''),11,105))
        blocks.append(dict(section='VLAN gateways',kind='vlan',value=n,height=max(61,lines*18+17)+25))
    if not lan:blocks.append(dict(section='VLAN gateways',kind='text',value='No LAN gateway networks configured.',height=40))
    for w in model['wans']:
        title=f"{w['label']} · Gateway {w['slot'].upper()} / {w['interface']}"+(' / VLAN '+w['tag'] if w['tag'] else '')
        values=[title,w['subnet'] or ('DHCP · lease not available' if w['dhcp'] else 'Subnet unknown'),f"ZTB {w['ip'] or 'DHCP'}    Next hop {w['next_hop'] or 'unknown'}",w['state']+(' · disabled' if w.get('enabled') is False else '')]
        height=sum(line_count(v,14 if i<3 else 11,486)*(21 if i<3 else 17) for i,v in enumerate(values))+16
        blocks.append(dict(section='WAN circuits',kind='wan',value=values,height=height,color=colors[w['circuit']]))
    for n in model['networks']:
        if n['kind'] not in ('management','ha'):continue
        value=f"{n['name']} · VLAN {n['tag']} · {n['zone']}\n{n['subnet'] or 'Subnet unknown'} · GW {n['ip'] or 'unknown'}\n{n['interface'] or 'Unbound'} · {n['state']}"
        if n['kind']=='management':value+='\n'+' · '.join(label for label,_ in vlan_badges(n))
        height=sum(line_count(v,12,486)*17 for v in value.split('\n'))+16
        blocks.append(dict(section='Management & HA networks',kind='multiline',value=value,height=height))
    notes=list(dict.fromkeys(note.replace('Airgap mode differs','Segmentation mode differs') for note in model['warnings']+model.get('ha_notes',[])))
    if lan:
        notes.append('Segmentation shows the VLAN default, not individual asset exceptions. DHCP off does not establish device isolation.')
        notes.append('IP app describes this rollout’s IP-based ZPA segment. Created segments remain disabled; Not selected means no creation was requested.')
        if any('Unknown' in label for n in model['networks'] if n['kind'] in ('lan','management') for label,_ in vlan_badges(n)):
            notes.append('Unknown means the setting was not captured or could not be verified.')
    segment_names=sorted({n.get('ip_app_segment',{}).get('name','') for n in model['networks']} - {''})
    for name in segment_names:notes.append('IP app segment: '+name)
    if model['ha_links']:
        notes.append('Each gateway’s HA interface connects through the downstream switch.')
        if model['mode']=='wan_edge_mode_ha' and any(l['role']=='ha-data' for l in model['ha_links']):
            notes.append('WAN Transit connects directly between the two ZTB appliances.')
    if model['breakout']:notes.append('UCaaS local breakout: selected collaboration traffic uses WAN directly and bypasses ZIA.')
    for value in [g['name'] for g in model['gateways'] if len(g['name'])>58]+([model['switch']] if len(model['switch'])>58 else []):notes.append('Full device label: '+value)
    for note in notes:blocks.append(dict(section='Configuration notes',kind='text',value=note,height=line_count(note,12,486)*17+18))
    return blocks


def pack_blocks(blocks, capacity):
    columns=[];current=[];used=0;section=None
    for block in blocks:
        heading=57 if block['section']=='VLAN gateways' else 36
        needed=block['height']+(heading if section!=block['section'] else 0)
        if current and used+needed>capacity:
            columns.append(current);current=[];used=0;section=None
        if section!=block['section']:
            current.append(dict(kind='heading',value=block['section'],height=heading));used+=heading;section=block['section']
        current.append(block);used+=block['height']
    if current:columns.append(current)
    return columns


def scene(model):
    topo=topology(model);blocks=schedule_blocks(model)
    identity=' · '.join(filter(None,[model['location'],'Template: '+model['template']]))
    # Schedules run below the topology, making the complete drawing horizontal.
    # Expand for dense sites while keeping all text and at least a 1.8:1 page ratio.
    candidates=[]
    for count in range(3,min(9,max(3,len(blocks)))+1):
        width=max(1880,count*568+76)
        title_h=line_count(model['name'],32,width-640)*43.2
        header=68+title_h+line_count(identity,14,width-100)*19+25
        low=max((b['height']+57 for b in blocks),default=0);high=sum(b['height']+57 for b in blocks)+1
        for _ in range(20):
            capacity=(low+high)/2
            if len(pack_blocks(blocks,capacity))<=count:high=capacity
            else:low=capacity
        schedule_y=header+topo.height+39
        height=schedule_y+high+85
        width=max(width,height*1.8)
        columns=pack_blocks(blocks,high+1)
        candidates.append((width*height,width,height,header,title_h,schedule_y,columns))
    _,width,height,header,title_h,schedule_y,columns=min(candidates,key=lambda c:c[0])
    s=Scene(width);s.height=height
    s.rect(0,0,width,height,WHITE)
    s.rect(0,0,width,6,BLUE)
    from diagram_branding import logo
    brand=logo(model.get('options',{}).get('logo'))
    if brand:
        scale=min(120/brand['width'],36/brand['height'])
        w,h=brand['width']*scale,brand['height']*scale
        s.add('image',x=38,y=16+(36-h)/2,w=w,h=h,data=base64.b64decode(brand['content']),
              name='Customer logo',asset='customer-logo.png')
    else:
        wordmark(s,38,23,115)
    s.label(174,28,'ZERO TRUST BRANCH   /   SITE DESIGN & AS-BUILT',12,BLUE,True,width-610)
    s.label(38,55,model['name'],32,INK,True,width-640)
    hardware=appliance_model(model)
    if hardware:
        photo,(pw,ph)=appliance_photo(hardware);photo_width=170
        s.add('image',x=width-555,y=23,w=photo_width,h=photo_width*ph/pw,data=photo,name=hardware+' appliance reference')
        s.label(width-577,89,hardware+' · appliance reference',11,MUTED,width=214,align='center')
    s.label(38,63+title_h,identity,14,MUTED,width=width-100)
    modes={'standalone':'STANDALONE','standard_mode_ha':'STANDARD HA','wan_edge_mode_ha':'ENHANCED HA','ha_unverified':'HA · MODE UNVERIFIED'}
    s.label(width-330,29,modes.get(model['mode'],'HA'),14,INK,True,292,'right')
    badge='PLANNED CONFIGURATION' if model['evidence']=='planned' else 'CONFIGURATION SNAPSHOT'
    s.label(width-330,52,badge,11,BLUE,True,292,'right')
    s.label(width-330,72,model['captured_at'][:19].replace('T',' ')+' UTC',11,MUTED,width=292,align='right')
    s.poly([(38,header-17),(width-38,header-17)],stroke=LINE,weight=1,closed=False)
    transplant(s,topo,(width-topo.width)/2,header)
    s.poly([(38,schedule_y-20),(width-38,schedule_y-20)],stroke=LINE,weight=1,closed=False)
    margin=(width-len(columns)*568+48)/2
    for col,items in enumerate(columns):
        x=margin+col*568;y=schedule_y
        for block in items:
            kind=block['kind'];value=block['value']
            if kind=='heading':
                s.label(x,y+5,value,18,INK,True,520)
                if value=='VLAN gateways':
                    for dx,label in [(0,'VLAN'),(45,'NAME / ZONE'),(218,'SUBNET / GATEWAY'),(400,'PORT / STATUS')]:s.label(x+dx,y+34,label,9,MUTED,True,170)
            elif kind=='vlan':
                n=value;h=block['height']
                s.rect(x,y,520,h-5,'#f5f8fc',radius=6);s.rect(x,y,3,h-5,n['color'],radius=1)
                s.label(x+10,y+12,n['tag'],13,n['color'],True,32)
                nh=s.label(x+47,y+10,n['name'],14,INK,True,154)
                s.label(x+47,y+13+nh,n['zone'],11,MUTED,width=154)
                nh=s.label(x+220,y+10,n['subnet'] or 'Unknown',14,INK,width=166)
                s.label(x+220,y+13+nh,'GW '+(n['ip'] or 'Unknown'),13,INK,width=166)
                nh=s.label(x+402,y+10,n['interface'] or 'Unbound',14,INK,True,105)
                s.label(x+402,y+13+nh,n['state']+(' · disabled' if not n['enabled'] else ''),11,MUTED,width=105)
                vlan_badge_row(s,x+10,y+h-29,n)
            elif kind=='wan':
                yy=y+6
                s.rect(x,y+6,3,block['height']-16,block['color'],radius=1)
                for i,line in enumerate(value):yy+=s.label(x+12,yy,line,14 if i<3 else 11,block['color'] if i==0 else INK if i<3 else MUTED,i==0,486)+2
                s.poly([(x+12,y+block['height']-5),(x+516,y+block['height']-5)],stroke=LINE,weight=.7,closed=False)
            else:
                yy=y+5
                for line in value.split('\n'):yy+=s.label(x+12,yy,line,12,MUTED,width=486)+1
            y+=block['height']
    footer=height-48
    s.poly([(38,footer-12),(width-38,footer-12)],stroke=LINE,weight=1,closed=False)
    s.label(width-205,footer,'ZTB   /   01',11,BLUE,True,167,'right')
    return s


def svg(model):
    s=scene(model)
    root=ET.Element('svg',xmlns='http://www.w3.org/2000/svg',width=str(s.width),height=str(round(s.height)),viewBox=f'0 0 {s.width} {s.height}',role='img',attrib={'aria-labelledby':'title description'})
    ET.SubElement(root,'title',id='title').text=clean(model['name']+' — site configuration')
    ET.SubElement(root,'desc',id='description').text='Configuration diagram with WAN addressing, VLAN gateway legend and verification notes. Physical cabling is a design assumption.'
    def element(item,parent):
        kind=item['kind']
        style={'fill':item.get('fill','none'),'stroke':item.get('stroke','none')}
        if kind in ('rect','ellipse'):
            if kind=='rect':attrs={k:str(item[k]) for k in ('x','y')};attrs.update(width=str(item['w']),height=str(item['h']),rx=str(item.get('radius',0)))
            else:attrs=dict(cx=str(item['x']+item['w']/2),cy=str(item['y']+item['h']/2),rx=str(item['w']/2),ry=str(item['h']/2))
            ET.SubElement(parent,kind,{**attrs,**style})
        elif kind=='poly':
            ET.SubElement(parent,'polygon' if item['closed'] else 'polyline',points=' '.join(f'{x},{y}' for x,y in item['points']),attrib={**style,'stroke-width':str(item['weight']),'stroke-linejoin':'round'})
        elif kind=='compound':
            path=' '.join('M '+' L '.join(f'{x},{y}' for x,y in contour)+' Z' for contour in item['contours'])
            ET.SubElement(parent,'path',d=path,attrib={**style,'fill-rule':'evenodd'})
        elif kind=='text':
            align=item['align'];x=item['x']+(item['w']/2 if align=='center' else item['w'] if align=='right' else 0)
            ET.SubElement(parent,'text',x=str(x),y=str(item['y']+item['size']),attrib={'font-family':'Arial, Helvetica, sans-serif','font-size':str(item['size']),'font-weight':'700' if item['bold'] else '400','fill':item['fill'],'text-anchor':{'left':'start','center':'middle','right':'end'}[align]}).text=item['text']
        elif kind=='image':
            ET.SubElement(parent,'image',x=str(item['x']),y=str(item['y']),width=str(item['w']),height=str(item['h']),href='data:image/png;base64,'+base64.b64encode(item['data']).decode(),attrib={'aria-label':item['name']})
    element(s.items[0],root)
    for edge in s.edges:
        for path in edge_paths(edge,s.edges):
            ET.SubElement(root,'polyline',points=' '.join(f'{x},{y}' for x,y in path),fill='none',stroke=edge['color'],attrib={'stroke-width':str(edge['width']),'stroke-linejoin':'round',**({'stroke-dasharray':'7 5'} if edge['dashed'] else {})})
    groups={}
    for item in s.items[1:]:
        parent=root
        if item['group']:
            if item['group'] not in groups:groups[item['group']]=ET.SubElement(root,'g',id=item['group'])
            parent=groups[item['group']]
        element(item,parent)
    return ET.tostring(root,encoding='utf-8',xml_declaration=True)


NS='http://schemas.microsoft.com/office/visio/2012/main'
REL='http://schemas.openxmlformats.org/package/2006/relationships'
R='http://schemas.openxmlformats.org/officeDocument/2006/relationships'
# Visio importers (including libvisio) expect the standard default/r prefixes.
ET.register_namespace('', NS)
ET.register_namespace('r', R)


def png(model):
    """Rasterize our generated SVG locally, with portable fonts and bounded size."""
    import resvg_py
    source=svg(model)
    dimensions=ET.fromstring(source)
    width=min(4096,max(2400,round(float(dimensions.get('width'))*2)))
    fonts=Path(__file__).parent/'data'/'fonts'
    return resvg_py.svg_to_bytes(svg_string=source.decode(),width=width,
        background=WHITE,skip_system_fonts=True,font_family='Liberation Sans',
        sans_serif_family='Liberation Sans',
        font_files=[str(fonts/name) for name in ('LiberationSans-Regular.ttf','LiberationSans-Bold.ttf')])


def vsdx(model):
    """Write an OPC package with native shapes, grouped icons and glued connectors."""
    s=scene(model)
    def node(parent,name,**attrs):return ET.SubElement(parent,'{'+NS+'}'+name,{k:str(v) for k,v in attrs.items()})
    def cell(parent,name,value,formula=None):
        return node(parent,'Cell',N=name,V=value,**({'F':formula} if formula else {}))
    def xml(root):return ET.tostring(root,encoding='utf-8',xml_declaration=True)
    contents=ET.Element('{'+NS+'}PageContents');shapes=node(contents,'Shapes')
    pictures={};picture_ids={}
    next_id=0
    def shape(parent,name,x,y,w,h,kind='Shape',page_height=None):
        nonlocal next_id
        next_id+=1
        n=node(parent,'Shape',ID=next_id,NameU=clean(name),Type=kind)
        ph=s.height if page_height is None else page_height
        for k,v in dict(PinX=(x+w/2)/96,PinY=(ph-y-h/2)/96,Width=w/96,Height=h/96,LocPinX=w/192,LocPinY=h/192,Angle=0).items():cell(n,k,v)
        return n,next_id
    def geometry(n,points,x,y,w,h,closed,paths=None):
        sec=node(n,'Section',N='Geometry',IX=0);cell(sec,'NoFill',0 if closed else 1)
        index=0
        for path in paths if paths is not None else [points]:
            for i,(px,py) in enumerate(path+([path[0]] if closed else [])):
                index+=1
                row=node(sec,'Row',T='MoveTo' if i==0 else 'LineTo',IX=index)
                cell(row,'X',(px-x)/96);cell(row,'Y',(y+h-py)/96)
    def primitive(item,parent,ox=0,oy=0,ph=None):
        v=dict(item);kind=v['kind']
        if kind in ('poly','compound'):
            contours=[[(x-ox,y-oy) for x,y in path] for path in (v['contours'] if kind=='compound' else [v['points']])]
            points=[p for path in contours for p in path];x=min(p[0] for p in points);y=min(p[1] for p in points)
            w=max(.01,max(p[0] for p in points)-x);h=max(.01,max(p[1] for p in points)-y)
        else:x=v['x']-ox;y=v['y']-oy;w=v['w'];h=v['h']
        n,_=shape(parent,v.get('text',v.get('name',kind)),x,y,w,h,kind='Foreign' if kind=='image' else 'Shape',page_height=ph)
        if kind=='image':
            key=v.get('asset','appliance.png');pictures[key]=v['data']
            picture_ids.setdefault(key,'rIdImage'+str(len(picture_ids)+1))
            for k,val in dict(ImgOffsetX=0,ImgOffsetY=0,ImgWidth=w/96,ImgHeight=h/96,
                    FillPattern=1,FillForegnd=WHITE,FillBkgnd=WHITE,LinePattern=0,ShdwPattern=0).items():cell(n,k,val)
            data=node(n,'ForeignData',ForeignType='Bitmap',CompressionType='PNG')
            node(data,'Rel',**{'{'+R+'}id':picture_ids[key]})
            return n
        fill=v.get('fill','none');stroke=v.get('stroke','none')
        cell(n,'FillPattern',0 if fill=='none' or kind=='text' else 1)
        if fill!='none':cell(n,'FillForegnd',fill)
        cell(n,'LinePattern',0 if stroke=='none' else 1)
        if stroke!='none':cell(n,'LineColor',stroke);cell(n,'LineWeight',v.get('weight',1)/96)
        if kind=='text':
            for k,val in dict(LeftMargin=0,RightMargin=0,TopMargin=0,BottomMargin=0,VerticalAlign=0).items():cell(n,k,val)
            sec=node(n,'Section',N='Character');row=node(sec,'Row',IX=0)
            for k,val in dict(Font=0,Size=v['size']/96,Color=fill,Style=1 if v['bold'] else 0).items():cell(row,k,val)
            sec=node(n,'Section',N='Paragraph');row=node(sec,'Row',IX=0);cell(row,'HorzAlign',{'left':0,'center':1,'right':2}[v['align']])
            node(n,'Text').text=v['text']
        elif kind=='ellipse':
            sec=node(n,'Section',N='Geometry',IX=0);row=node(sec,'Row',T='Ellipse',IX=1)
            for k,val in dict(X=w/192,Y=h/192,A=w/96,B=h/192,C=w/192,D=h/96).items():cell(row,k,val)
        elif kind=='compound':
            sec=node(n,'Section',N='Geometry',IX=0);cell(sec,'NoFill',0)
            index=0
            for path in contours:
                for i,(px,py) in enumerate(path+[path[0]]):
                    index+=1;row=node(sec,'Row',T='MoveTo' if i==0 else 'LineTo',IX=index)
                    cell(row,'X',(px-x)/96);cell(row,'Y',(y+h-py)/96)
        elif kind=='poly':geometry(n,points,x,y,w,h,v['closed'])
        else:
            geometry(n,[(x,y),(x+w,y),(x+w,y+h),(x,y+h)],x,y,w,h,True)
            if v.get('radius'):cell(n,'Rounding',v['radius']/96)
        return n
    primitive(s.items[0],shapes)
    group_nodes={};ids={}
    for key,g in s.groups.items():
        n,identifier=shape(shapes,key,g['x'],g['y'],g['w'],g['h'],'Group');ids[key]=identifier
        cell(n,'SelectMode',1)
        sec=node(n,'Section',N='Connection')
        for i,(x,y) in enumerate(g['anchors']):
            row=node(sec,'Row',IX=i,T='Connection')
            for k,v in dict(X=(x-g['x'])/96,Y=(g['y']+g['h']-y)/96,DirX=0,DirY=0,Type=0).items():cell(row,k,v)
        group_nodes[key]=node(n,'Shapes')
    # Native lines attach to the groups, not to decorative icon subshapes.
    connections=[]
    for e in s.edges:
        points=e['points'];x=min(p[0] for p in points);y=min(p[1] for p in points)
        w=max(.01,max(p[0] for p in points)-x);h=max(.01,max(p[1] for p in points)-y)
        n,identifier=shape(shapes,'Connection',x,y,w,h)
        for name,val in dict(BeginX=points[0][0]/96,BeginY=(s.height-points[0][1])/96,EndX=points[-1][0]/96,EndY=(s.height-points[-1][1])/96,LineColor=e['color'],LineWeight=e['width']/96,LinePattern=2 if e['dashed'] else 1,FillPattern=0,ObjType=2).items():cell(n,name,val)
        geometry(n,points,x,y,w,h,False,edge_paths(e,s.edges))
        for end,target,anchor in [('Begin',e['source'],e['source_anchor']),('End',e['target'],e['target_anchor'])]:
            connections.append(dict(FromSheet=identifier,FromCell=end+'X',FromPart=9 if end=='Begin' else 12,ToSheet=ids[target],ToCell=f'Connections.X{anchor+1}',ToPart=100+anchor))
    for item in s.items[1:]:
        key=item['group']
        if key:
            g=s.groups[key];primitive(item,group_nodes[key],g['x'],g['y'],g['h'])
        else:primitive(item,shapes)
    # Put connectors behind device groups while preserving unique IDs.
    children=list(shapes);shapes[:]=[children[0]]+[c for c in children[1:] if c.get('NameU')=='Connection']+[c for c in children[1:] if c.get('NameU')!='Connection']
    connects=node(contents,'Connects')
    for c in connections:node(connects,'Connect',**c)
    document=ET.Element('{'+NS+'}VisioDocument')
    faces=node(document,'FaceNames');node(faces,'FaceName',ID=0,NameU='Arial',UnicodeRanges='-1 -1 -1 -1',CharSets='0 0',Panose='2 11 6 4 2 2 2 2 2 4',Flags=0)
    pages=ET.Element('{'+NS+'}Pages');page=node(pages,'Page',ID=0,NameU='Site configuration',Name='Site configuration')
    sheet=node(page,'PageSheet')
    cell(sheet,'PageWidth',s.width/96);cell(sheet,'PageHeight',s.height/96);cell(sheet,'DrawingScale',1);cell(sheet,'PageScale',1)
    # Drawing geometry alone does not set the printer's orientation. Explicitly
    # use landscape A3 and fit the whole drawing to one physical printer page.
    for name,value in dict(PrintPageOrientation=2,PaperKind=8,OnPage=1,PagesX=1,PagesY=1,
            CenterX=1,CenterY=1,PageLeftMargin=.2,PageRightMargin=.2,
            PageTopMargin=.2,PageBottomMargin=.2).items():cell(sheet,name,value)
    node(page,'Rel',**{'{'+R+'}id':'rId1'})
    def relationships(items):
        root=ET.Element('Relationships',xmlns=REL)
        for identifier,kind,target in items:ET.SubElement(root,'Relationship',Id=identifier,Type=(R+'/image' if kind=='image' else 'http://schemas.microsoft.com/visio/2010/relationships/'+kind),Target=target)
        return xml(root)
    types=ET.Element('Types',xmlns='http://schemas.openxmlformats.org/package/2006/content-types')
    ET.SubElement(types,'Default',Extension='rels',ContentType='application/vnd.openxmlformats-package.relationships+xml')
    ET.SubElement(types,'Default',Extension='xml',ContentType='application/xml')
    if pictures:ET.SubElement(types,'Default',Extension='png',ContentType='image/png')
    for path,mime in [('document','drawing.main'),('pages/pages','pages'),('pages/page1','page')]:
        ET.SubElement(types,'Override',PartName='/visio/'+path+'.xml',ContentType='application/vnd.ms-visio.'+mime+'+xml')
    buffer=io.BytesIO()
    with zipfile.ZipFile(buffer,'w',zipfile.ZIP_DEFLATED) as z:
        for path,data in {'[Content_Types].xml':xml(types),'_rels/.rels':relationships([('rId1','document','visio/document.xml')]),
            'visio/document.xml':xml(document),'visio/_rels/document.xml.rels':relationships([('rId1','pages','pages/pages.xml')]),
            'visio/pages/pages.xml':xml(pages),'visio/pages/_rels/pages.xml.rels':relationships([('rId1','page','page1.xml')]),
            'visio/pages/page1.xml':xml(contents)}.items():z.writestr(path,data)
        if pictures:
            z.writestr('visio/pages/_rels/page1.xml.rels',relationships([(picture_ids[key],'image','../media/'+key) for key in pictures]))
            for name,data in pictures.items():z.writestr('visio/media/'+name,data)
    return buffer.getvalue()

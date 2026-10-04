"""Credential-free starter files for the browser's existing CSV importer."""
import base64
import io
import zipfile
from csv_safety import csv_text


SITE_COLUMNS = ['site_name', 'gateway_name', 'template_name', 'city', 'country',
                'wan_interface_name', 'wan0_ip', 'wan0_mask', 'wan0_gw', 'wan_dns',
                'location_type', 'vlans_file', 'post', 'appc_provision']
VLAN_COLUMNS = ['name', 'tag', 'subnet', 'default_gateway', 'interface', 'zone',
                'enabled', 'share_over_vpn', 'dhcp_service', 'dhcp_start', 'dhcp_end',
                'per_network_dns', 'zpa_include']
HA_SITE_COLUMNS = SITE_COLUMNS + ['gateway_name_b', 'wan1_interface_name',
                                 'wan1_ip', 'wan1_mask', 'wan1_gw',
                                 'vrrp_link_interface', 'vrrp_vrid']
HA_VLAN_COLUMNS = VLAN_COLUMNS + ['gateway_target']


def csv_content(columns, rows):
    return csv_text(rows, columns)


GUIDE = """ZERO TRUST BRANCH — CSV STARTER KIT

These are editable examples for standalone branches. No tenant connection,
credentials, or Python installation is needed to download or import them.
Downloading and importing do not deploy anything.

1. EXTRACT AND EDIT
Extract this ZIP first. Open sites.csv in Excel, another spreadsheet editor,
or a text editor. Each row describes one branch. Replace the example names,
template, interfaces, locations, and addressing with your intended settings.
Save as CSV (UTF-8, comma-separated), not XLSX. Keep the column headers.

2. MATCH THE VLAN FILES
Each site's vlans_file points to its network file:
  Example-Branch-01 -> vlans/branch-01.csv
  Example-Branch-02 -> vlans/branch-02.csv
Each VLAN file contains one row per network. Duplicate a site row and its VLAN
file for each additional branch. Give each VLAN file a unique filename and
update that site's vlans_file value to match. Review each branch's addresses.
Delete any example site rows and VLAN files you do not need.

3. IMPORT INTO THE TOOL
Choose Import CSVs, then Choose CSV files. Select sites.csv first; the tool
will ask for the referenced VLAN files. Select them from the extracted vlans
folder. You can also select all CSV files together if they are in one folder.
Do not select the ZIP or this README. File matching uses the filename, so the
VLAN files must have unique names even if they are in different folders.
Import replaces the current project draft after confirmation. Use Save as copy
or New project first if you want to keep a separate rollout.

4. REVIEW BEFORE DEPLOYMENT
Example sites start unselected (post=0). Open each branch and review its details
and VLANs. Select ready branches in the overview, choose Review selected
branches, then Preview deployment to check the real tenant. The template name,
WAN/LAN ports and zone in these examples are placeholders; offline readiness
does not verify that they exist in your tenant. Deployment requires a separate
approval. Existing-site checks still apply.

SITES.CSV COLUMN GUIDE
site_name           Required. Unique new branch name; not an existing site name.
gateway_name        Required. Appliance name for this branch.
template_name       Required unless using an advanced template_id override.
                    Replace REPLACE_WITH_TENANT_TEMPLATE with the exact name
                    of an existing standalone template in your tenant.
city                Optional branch city.
country             Country name or two-letter code, e.g. Netherlands or NL.
wan_interface_name  Required. A WAN port on that template. ge5 is an example.
wan0_ip             Leave all three wan0 fields blank for DHCP. For Static IP,
wan0_mask           supply the address, prefix length (e.g. 24) or subnet mask,
wan0_gw             and default gateway together. Use your assigned WAN values.
wan_dns             DNS server IPs. Quote comma-separated lists when editing
                    by hand, e.g. "1.1.1.1,8.8.8.8". Spreadsheet editors handle
                    CSV quoting when saving.
location_type       none in this kit: no ZIA location. Configure ZIA location
                    creation or reuse in the editor after import if needed.
vlans_file          Matching VLAN CSV filename, e.g. vlans/branch-01.csv.
post                0 = unselected, 1 = included in rollout. Keep 0 while editing.
appc_provision      0 = off, 1 = provision App Connector. This kit uses 0.
                    Enable and configure ZPA in the editor if needed.

VLAN CSV COLUMN GUIDE
name                Network name, e.g. Users.
tag                 VLAN ID, e.g. 10. Use the IDs intended for your branch.
subnet              Prefix length, e.g. 24 for a /24 network.
default_gateway     VLAN gateway IP, e.g. 10.20.10.1 (not the network address).
interface           Template LAN port, e.g. ge2. Use the correct tenant port.
zone                Exact tenant zone name. LAN Zone is an example.
enabled             true or false; this kit enables the example network.
share_over_vpn      true or false; this kit uses false.
dhcp_service        off, inherit (On / Inherit), or non_airgapped. This kit uses
                    off. Configure DHCP and scope choices in the editor.
dhcp_start          Leave both scope fields blank with DHCP off. For enabled
dhcp_end            DHCP, set a valid range within the VLAN subnet. Imported
                    enabled scopes default to Automatic in the editor; choose
                    Custom there if you need to preserve a narrower range.
per_network_dns     DNS server IPs used with DHCP. Blank uses the VLAN gateway
                    automatically in the editor; custom lists need CSV quoting.
zpa_include         0 = not selected for a ZPA application segment, 1 = selected.
                    Keep 0 unless App Connector provisioning is configured.

MORE ADVANCED SETTINGS
This kit keeps optional features off. After import, use the editor for template
cloning, ZIA locations, ZPA, additional WANs, private-domain DNS and UCaaS.
Review > Download CSV bundle exports your configured fields for later reuse.
HA configuration can be exported as CSV, but UI HA deployment remains gated.
Never put API keys, passwords, or other credentials in these files.
"""


HA_GUIDE = """ZERO TRUST BRANCH — HA CSV STARTER KIT
Preparation and CSV export only

HA deployment from the browser is not enabled yet: reference WAN and management
mapping still need verification. Importing this kit does not deploy anything
or enable HA deployment. No credentials or tenant connection are needed.

1. EXTRACT AND EDIT
Extract the ZIP. sites.csv contains ONE branch with TWO gateways, not two sites.
Its vlans_file points to vlans/ha-branch-01.csv. That file contains a shared
Users LAN and separate Management-A and Management-B loopbacks.
Replace all example names, template, ports, zones, locations and addresses.
The 192.0.2.x WAN addresses are documentation examples, not usable ISP settings.
Keep the headers and save as CSV (UTF-8, comma-separated), not XLSX.
To add another HA branch, duplicate the site row and VLAN file, give the file
a unique filename, and update vlans_file. Assign unique gateway names and
management addresses, and review the new branch's LAN and WAN addressing.

2. IMPORT AND REVIEW
Choose Import CSVs > Choose CSV files. Select sites.csv first, then the matching
VLAN file when prompted. You can also select all CSVs together from one folder.
Do not select the ZIP or README. Matching uses the filename, so every VLAN file
must have a unique name. Import replaces the current draft after confirmation;
use New project or Save as copy first to keep a separate rollout.
The example starts unselected (post=0). Open it in the editor and review the
HA setup, both WANs, HA link, and VLAN gateway assignments. Template and zone
names are placeholders; offline validation does not verify tenant resources.

3. EXPORT YOUR PREPARED CONFIGURATION
Select the branch when ready to review, then use Review > Download CSV bundle.
You can save the project locally and return to it later. The browser's HA
deployment gate remains in place; a valid CSV is not approval to deploy HA.

SITES.CSV COLUMN GUIDE
site_name           One unique branch name for the HA pair.
gateway_name        Gateway A name; unique across your branches.
gateway_name_b      Gateway B name; must differ from Gateway A.
template_name       Replace REPLACE_WITH_HA_TEMPLATE with your tenant's exact
                    HA template name. Ports and zones must match that template.
city, country       Branch location; country accepts a name or two-letter code.
wan_interface_name  Gateway A primary WAN port (ge5 is an example).
wan0_ip             Gateway A static WAN address, prefix length or subnet mask,
wan0_mask           and default gateway. Fill all three together, or leave all
wan0_gw             three blank for DHCP.
wan1_interface_name Gateway B primary WAN port, NOT a second WAN on Gateway A.
wan1_ip             Gateway B static WAN address, prefix length or subnet mask,
wan1_mask           and default gateway. Fill all three together, or leave all
wan1_gw             three blank for DHCP, independently of Gateway A.
wan_dns             DNS server IPs. Quote comma-separated lists when editing
                    manually; spreadsheet editors handle CSV quoting for you.
vrrp_link_interface HA link port (ge3 here); use the intended template port.
vrrp_vrid           VRRP ID from 1 to 255; 1 is an example, not a tenant lookup.
location_type       none = no ZIA location configured in this example.
vlans_file          Matching VLAN filename, here vlans/ha-branch-01.csv.
post                0 = unselected, 1 = included for review/export. Keep 0 while
                    editing. Setting 1 does not bypass the HA deployment gate.
appc_provision      0 = App Connector provisioning off in this kit.

VLAN CSV COLUMN GUIDE
name                Network name, e.g. Users or Management-A.
tag                 VLAN ID. The two management rows can share tag 1 because
                    they target different gateways. Users uses tag 10.
subnet              Prefix length: 24 for the Users LAN, 32 for lo0 loopbacks.
default_gateway     LAN gateway IP, or the unique management loopback IP.
interface           ge2 for the example LAN; lo0 for each management loopback.
zone                Exact tenant zone name; examples use LAN Zone and
                    Management Zone. Replace as appropriate for your template.
gateway_target      all = both gateways; a = Gateway A; b = Gateway B.
                    Regular HA LANs must use all. Use a/b only for management
                    networks, never to express another WAN. Configure additional
                    WANs separately in the editor.
enabled             true or false; all example networks use true.
share_over_vpn      true or false; this kit uses false.
dhcp_service        off, inherit, or non_airgapped. Examples use off; keep
                    loopback management DHCP off.
dhcp_start          Leave both blank with DHCP off. For LAN DHCP, configure a
dhcp_end            valid scope in the editor. Imported enabled scopes default
                    to Automatic; choose Custom to preserve a narrower range.
per_network_dns     DNS IPs for LAN DHCP; blank uses the VLAN gateway in the
                    editor. Quote comma-separated lists when editing by hand.
zpa_include         0 = excluded from ZPA application segment; this kit uses 0.

Optional services are off in this kit. Never put API keys, passwords or other
credentials in these files. Keep the standalone kit for single-gateway sites.
"""


def ha_template_bundle():
    output = io.BytesIO()
    site = dict(site_name='Example-HA-Branch-01', gateway_name='Example-HA-01-A',
                gateway_name_b='Example-HA-01-B', template_name='REPLACE_WITH_HA_TEMPLATE',
                city='Amsterdam', country='NL', wan_interface_name='ge5',
                wan0_ip='192.0.2.10', wan0_mask='24', wan0_gw='192.0.2.1',
                wan1_interface_name='ge5', wan1_ip='192.0.2.11', wan1_mask='24',
                wan1_gw='192.0.2.1', wan_dns='1.1.1.1,8.8.8.8',
                vrrp_link_interface='ge3', vrrp_vrid='1', location_type='none',
                vlans_file='vlans/ha-branch-01.csv', post='0', appc_provision='0')
    common = dict(enabled='true', share_over_vpn='false', dhcp_service='off', zpa_include='0')
    vlans = [dict(common, name='Users', tag='10', subnet='24', default_gateway='10.30.10.1',
                  interface='ge2', zone='LAN Zone', gateway_target='all')]
    for target, address in [('a', '10.30.255.1'), ('b', '10.30.255.2')]:
        vlans.append(dict(common, name=f'Management-{target.upper()}', tag='1', subnet='32',
                          default_gateway=address, interface='lo0', zone='Management Zone',
                          gateway_target=target))
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('sites.csv', csv_content(HA_SITE_COLUMNS, [site]))
        archive.writestr(site['vlans_file'], csv_content(HA_VLAN_COLUMNS, vlans))
        archive.writestr('READ-ME-FIRST.txt', HA_GUIDE)
    return {'filename': 'zero-trust-branch-ha-csv-templates.zip',
            'content': base64.b64encode(output.getvalue()).decode()}


def template_bundle(mode='standalone'):
    if mode == 'ha':
        return ha_template_bundle()
    if mode != 'standalone':
        raise ValueError('Choose standalone or ha CSV templates')
    output = io.BytesIO()
    sites = []
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for index, city, country in [(1, 'Amsterdam', 'NL'), (2, 'Brussels', 'BE')]:
            filename = f'vlans/branch-{index:02d}.csv'
            sites.append(dict(site_name=f'Example-Branch-{index:02d}',
                              gateway_name=f'Example-Branch-{index:02d}-GW',
                              template_name='REPLACE_WITH_TENANT_TEMPLATE',
                              city=city, country=country, wan_interface_name='ge5',
                              wan_dns='1.1.1.1,8.8.8.8', location_type='none',
                              vlans_file=filename, post='0', appc_provision='0'))
            archive.writestr(filename, csv_content(VLAN_COLUMNS, [dict(
                name='Users', tag='10', subnet='24', default_gateway=f'10.{index*10+10}.10.1',
                interface='ge2', zone='LAN Zone', enabled='true', share_over_vpn='false',
                dhcp_service='off', zpa_include='0')]))
        archive.writestr('sites.csv', csv_content(SITE_COLUMNS, sites))
        archive.writestr('READ-ME-FIRST.txt', GUIDE)
    return {'filename': 'zero-trust-branch-csv-templates.zip',
            'content': base64.b64encode(output.getvalue()).decode()}

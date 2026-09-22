# Optional ZPA provisioning

Use the main deployment workflow to create ZPA App Connector resources for selected new ZTB sites. Start with the [setup guide](readme.md); this integration is optional and is disabled per site unless `appc_provision` is enabled.

## What the integration does

For each selected new site with `appc_provision=1`, the deployment engine:

1. Creates an App Connector Group named after the site, using its city/country and the configured enrollment certificate.
2. Creates a provisioning key with the site name, that group, the configured enrollment certificate, and maximum usage of 2.
3. Attaches the provisioning key to the ZTB cluster.
4. If any VLAN has `zpa_include=1`, stages one disabled LAN application segment using that new App Connector group.

Creating these resources does not independently verify that an App Connector has registered or can pass traffic. Existing ZTB sites are blocked by the main duplicate check; this is not a command for adding ZPA to an existing site.

## Prerequisites and credentials

You need ZPA API credentials with access to enrollment certificates, App Connector Groups, and provisioning keys. The named enrollment certificate must already exist.

LAN segment staging additionally needs list/read/create access to application segments, segment groups, and server groups. The tool can issue a corrective update only to an application it just created if read-back shows the requested disabled state was not applied. Use the same ZPA credentials; no new `.env` setting is needed. Confirm the customer's segment allowance before deploying; the tool reports counts but does not determine license entitlement.

Add the following to the same credential file used for ZTB:

```dotenv
ZPA_ENABLED=true
ZPA_CLIENT_ID="<client-id>"
ZPA_CLIENT_SECRET="<client-secret>"
ZPA_CUSTOMER_ID="<customer-id>"
ZPA_ENROLLMENT_CERT_NAME="Connector"
ZPA_BASE_URL="https://config.private.zscaler.com"
```

Use the base URL for your ZPA cloud. The example is not a universal endpoint. `ZPA_CUSTOMER_ID` may be omitted if the authentication token supplies `custId`; an explicitly configured customer ID is checked against the token when available. The certificate name defaults to `Connector` but can be changed. The integration resolves that name once during preflight and supplies the same certificate ID to both the group and provisioning key; no additional credential or certificate setting is needed.

Both ZTB and ZPA use the selected `--env-file`; environment variables take precedence. Authentication can update tokens in that file. Do not share or commit it.

## Select sites and deploy

In `sites.csv`, set both `post=1` and `appc_provision=1` for each intended new site. For example:

```csv
site_name,gateway_name,template_name,wan_interface_name,location_type,city,country,post,appc_provision
Branch1,Branch1-gw-1,Your-Template,ge3,none,Amsterdam,Netherlands,1,1
```

Replace the template and interface with values appropriate for your tenant. With the WAN address fields omitted, this example requests WAN DHCP.

```bash
python3 bulk_create.py --csv sites.csv --validate-only
python3 bulk_create.py --csv sites.csv --dry-run
python3 bulk_create.py --csv sites.csv
```

Windows users can use `.\.venv\Scripts\python.exe` in place of `python3` after following the main setup guide.

Offline validation makes no network requests. Preview authenticates to ZPA and checks the customer/certificate before any deployment writes. The named certificate must resolve; there is no arbitrary certificate fallback. Preview does not create groups or keys, or attach keys to ZTB.

A selected `appc_provision=1` row conflicts with `ZPA_ENABLED=false` and blocks preflight. Optional ZPA preflight failures block the whole batch before site creation.

## Stage a disabled LAN application segment

Add `zpa_include` to the VLAN CSV and set it to `1` only for the LAN subnets to include. Missing, blank, or `0` means excluded. The site's existing `post=1` and `appc_provision=1` settings are required. There is no additional site flag or segment-mode setting.

```csv
name,tag,subnet,default_gateway,dhcp_start,dhcp_end,interface,zone,enabled,share_over_vpn,dhcp_service,zpa_include
Printers,20,24,10.20.0.1,,,ge2,LAN Zone,true,false,off,1
Servers,30,24,10.30.0.1,,,ge2,LAN Zone,true,false,off,1
Guests,40,24,10.40.0.1,,,ge2,LAN Zone,true,false,off,0
```

For site `Utrecht-NL-BR`, these selections create:

| Object | Name / configuration |
| --- | --- |
| Application segment | `ztb-utrecht-nl-br-lan`: `10.20.0.0/24` and `10.30.0.0/24`, **disabled** |
| Segment group | `ztb-utrecht-nl-br-segments`, enabled |
| Server group | `ztb-utrecht-nl-br-servers`, enabled, dynamic discovery, linked only to the App Connector group created for this site |

TCP and UDP ranges are `1–52, 54–65535`; DNS port 53 is excluded. ICMP is disabled (`NONE`), and health reporting is `ON_ACCESS`. These are broad discovery definitions: the customer must review destinations and policy before enabling access, preferably for a pilot group first. Disabled staging does not discover applications or prove reachability. No access policies are created or modified.

Names are lowercase, with spaces and unsupported characters replaced by hyphens. Names that normalize to empty or exceed the tool's 100-character site-name bound are rejected rather than truncated. Only selected, enabled LAN VLANs are eligible. Management/loopback, recognized WAN/HA zones, and configured WAN/HA link interfaces are rejected. Custom LAN zone names are supported; review their actual purpose and interface roles before selection.

Preflight reads all three ZPA inventories with pagination, rejects existing generated names and literal IP/subnet overlaps (including disabled applications), and checks selected sites against each other. DNS names are not resolved for overlap analysis. Conflicts require review; existing objects are never adopted or overwritten. Checks repeat just before staging, but concurrent deployments are not coordinated.

Staging runs only after VLAN and App Connector provisioning succeed. Each new object is read back to verify its identity and associations; the application must match the selected subnets, disabled state, ports, and ICMP setting. Preview shows the names, subnets, and settings without creating resources. With no selected VLANs, the existing App Connector-only workflow is unchanged.

Reference CSV exports set `zpa_include=0` on every VLAN, including on re-export; ZTB does not store this local selection. Reapply selections deliberately after pulling a reference site.

## Results and troubleshooting

Read the short report under `out/runs/` and inspect the `ZPA` stage in its JSON counterpart. API-stage success is separate from connector registration and connectivity.

LAN staging has its own `ZPA segments` stage. JSON includes planned settings, created object names/IDs, verification flags, and status (`preview`, `blocked`, `incomplete`, or `staged_disabled`). Partial failures retain IDs already returned by the API for deliberate recovery; do not rerun creation blindly. A timeout can leave an object created without a recorded ID, so also inspect by name.

| Problem | What to check |
| --- | --- |
| Authentication or customer mismatch | Client credentials, cloud URL, and customer ID. |
| Enrollment certificate unavailable | Exact configured name and permission to read it. |
| Group or provisioning key creation fails | Console error, API permissions, and any resources already created during this run. |
| Attaching the key to ZTB fails | The target cluster and whether the ZPA group/key already exist. |
| ZPA segments blocked | Resolve failed VLAN or App Connector stages before deliberate recovery. |
| Existing name or overlapping subnet | Review existing ZPA definitions; changing a name alone does not resolve overlapping access. |
| Staging HTTP error or read-back mismatch | Inspect the recorded objects and disabled state. Check API permissions and tenant capacity. |
| Token expires during a batch | Inspect partial results; mid-batch ZPA token refresh is not implemented. |

## Limitations

- Groups, keys, and segments are not automatically reused or rolled back after failure. Inspect resources before repair; rerunning site creation will stop at the existing-site check.
- The current country mapping defaults unrecognized values to `NL`. Review the country settings before deploying outside the supported mappings in `zpa_provisioning.py`.
- Group creation attempts OpenStreetMap geocoding using the site city and country. Lookup failures fall back to coordinates `0,0`.
- A fresh standalone Netherlands site has passed VLAN creation, group/key creation, ZTB key attachment, and disabled LAN segment staging in a single deployment run. Read-back verified selected subnets, connector associations, disabled state, ports, and ICMP off. Appliance registration after activation and HA still need live validation. Offline tests do not establish connector readiness.
- Resources are created in the default ZPA microtenant; selecting other microtenants is not implemented.
- The integration provisions App Connector resources and optional disabled LAN segments; it does not configure security policies.

[Return to the setup guide](readme.md)

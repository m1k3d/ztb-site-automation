# Configuration reference

Detailed CSV fields, export behavior, and authentication settings. Start with the [setup guide](../readme.md) for the deployment workflow.

## Site inputs


See [sites.csv](../examples/sites.csv) for a minimal standalone example. Only rows with `post=1` are deployed. `post=0` or blank skips a row; other values are errors. Site and gateway names must be unique within the selected batch.

- Required: `site_name`, `gateway_name`, `wan_interface_name`, and either `template_name` or `template_id`. ID takes precedence; names resolve against the tenant.
- `template_mode`: `existing` (default) reuses the selected template; `clone` creates an independent copy before deploying the site. `template_name`/`template_id` identify the source. `new_template_name` defaults to `site_name`; it must be unique across the selected batch and absent from the tenant. Review/preview are read-only. Clone failures block site creation, and partial results record the new template ID for manual recovery.
- Exports include `template_name`, not `template_id`. You can delete the entire `template_id` column when using names; re-export will not add it back. Existing ID columns and overrides are preserved for compatibility.
- `city` and `country`: used for optional ZPA group location. New ZIA locations require `country`; the current ZTB site payload does not send `city`. The UI offers a searchable country list. Country names, vendor API values, common aliases and two-letter codes are normalized using [the country catalog](../data/countries.json). For example, `Netherlands` and `NL` map to `THE_NETHERLANDS` for location creation and `NL` for ZPA. Unknown nonempty values fail validation. A blank optional country omits ZPA's `countryCode` instead of assuming the Netherlands.
- WAN DHCP: leave `wan0_ip`, `wan0_mask`, and `wan0_gw` blank. Static WAN: provide all three. Masks accept a prefix length or dotted mask and normalize to dotted form.
- HA: add `gateway_name_b` and `wan1_interface_name`. WAN1 uses the same DHCP/static rules with `wan1_ip`, `wan1_mask`, `wan1_gw`. Names/interface roles must match the chosen template.
- `wan_dns`: comma-separated IPv4 DNS servers, quoted in CSV. Used in the site payload and as per-network DNS for VLANs.
- `private_dns`: comma-separated IPv4 addresses or prefixes for `System-Private-DNS-Servers-Group`; plain addresses become `/32`.
- `dhcp_service_mode`: blank, `relay`, `server`, or `inherit`. A nonempty `dhcp_server_ip` with blank mode infers relay; explicit relay requires this address.
  Site creation inherits DHCP mode from the selected template. Tenant preview verifies that an explicit mode matches it; the initial POST cannot override template mode. A relay template always requires `dhcp_server_ip`, including when all VLANs disable DHCP. Preview also checks that the gateway count matches standalone or HA template mode.
- `vlans_file`: optional VLAN CSV or exported JSON. Absolute paths remain supported. Relative paths now resolve from the site CSV, not the current working directory.
- Optional VRRP overrides: `vrrp_link_interface`, `vrrp_track_extra` (comma-separated), `vrrp_vrid` (1–255, default 16). Otherwise HA/WAN/LAN tracking is inferred from inventory and VLAN inputs.
- `appc_provision`: blank/0/false disables ZPA; 1/true enables it. See [ZPA provisioning](../ZPA_PROVISIONING_README.md) for setup.

### ZIA location choices

| `location_type` | Behavior |
| --- | --- |
| `new` | Requires `country`; creates `zia_location_name` or defaults to `site_name`. Resolves `location_template_name`, defaulting to `Default Location Template`. |
| `existing` | Requires a uniquely matching `zia_location_name`; fails if absent or ambiguous. |
| `none` | Creates the site without a ZIA location. |
| blank / `auto` | Retains legacy behavior: reuse a matching name, otherwise create a new location (requires country). |

`location_template_id` is an optional positive-ID override. These choices belong in the site CSV; `ZIA_LOCATION_TEMPLATE_NAME` and `ZIA_LOCATION_TEMPLATE_ID` environment settings are not used.

Exports include `location_template_name`, not `location_template_id`. When using name resolution, you can delete the entire ID column; subsequent pulls will not add it back. Existing ID columns and overrides remain supported and are preserved on re-export.

Exported rows default to `auto` and `Default Location Template`; these defaults do not reconstruct original creation settings. Re-export preserves existing location choices and custom columns, but resets `post=0`.

In the browser, `auto` is labeled **Use existing if found, otherwise create**. **Create branches** defaults each copy to `new`, using the new branch's site name for its location; a source set to `none` keeps `none` in its copies. The source and imported rows retain their existing choices. Copies can be edited before deployment.

## VLAN inputs

See [vlans.csv](../examples/vlans.csv). CSV fields:

```csv
name,tag,subnet,default_gateway,dhcp_start,dhcp_end,interface,zone,enabled,share_over_vpn,dhcp_service
Users,10,10.10.10.0/24,10.10.10.1,10.10.10.100,10.10.10.150,ge5,LAN Zone,true,false,inherit
```

Required: name, tag (1–4094), subnet, default gateway, and interface. Subnet accepts CIDR, prefix length, or dotted mask. When DHCP is enabled or inferred from a range, DHCP endpoints must both be supplied or both blank; ranges must be ordered, inside the subnet, and exclude the gateway/network/broadcast addresses. VLAN names must be unique within a site, including after the API's 16-character truncation. Tags must be unique on each interface.

`enabled` defaults to true; `share_over_vpn` defaults to false. Both accept true/false, 1/0, yes/no, y/n. DHCP service accepts `inherit`/`on`, `no_dhcp`/`off`, or `non_airgapped`/`non-airgapped`. If blank, a supplied range implies inherit; otherwise no_dhcp. Explicit `off` / `no_dhcp` ignores scope fields, including stale or malformed ranges, and sends no DHCP range. Other VLAN fields are still validated. The CLI uses explicit CSV ranges; the UI defaults DHCP-enabled VLANs to Automatic and exports calculated endpoints. Zone defaults to `LAN Zone`.

Optional `per_network_dns` is a comma-separated list of IPv4 DNS server addresses for this VLAN. An explicit value is used in both network creation and its enable/update call. Missing or blank values retain the legacy site `wan_dns` fallback. The UI can populate the VLAN gateway automatically; exported CSVs store its resolved IP, and the CLI does not recalculate it. Reference exports preserve this field.

Regular HA LAN VLANs apply to the site and both gateways; they appear once and have no gateway selector. Per-appliance management networks retain `gateway_target` (`a` or `b`), shown as **Apply network to**. A single-gateway network requires one interface; Gateway B requires an HA site. Deployment resolves the new gateway by its requested name rather than API response order. CSV export preserves the assignment. HA deployment through the UI remains gated while loopback binding on unactivated gateways is unresolved.

Reference pulls detect secondary WANs separately from the VLAN list. A collapsed **Additional WANs** section appears only when extras exist; **Copy additional WANs from reference** defaults on. Each entry retains its appliance, interface, VLAN tag, DHCP/static mode, addresses, next hop, and DNS servers. Review static addresses for the new branch. The initial site POST creates each appliance's primary WAN, then an independent stage creates and reads back the additional WANs before dependent forwarding policies. Exact existing configurations may be reused; conflicting networks are never overwritten. CSV/project exports retain `copy_additional_wans` and `additional_wans_json`. The live Brussels verification covered static secondary WANs; DHCP secondary WANs have automated coverage but have not yet been exercised against the live API.

Optional `zpa_include` defaults to false and accepts the same boolean values. Set it to `1` on selected LAN or management VLANs to create **one disabled ZPA application segment per site** containing their subnets. A selected `lo0` contributes its exact `/32` using the same flag; no separate management option is required. Requires `appc_provision=1` on the site; there is no new site flag. It is independent of `share_over_vpn` and is never sent to the ZTB network API. Exports reset this local choice to `0`; existing CSVs without the column keep their original behavior. See [LAN segment staging](../ZPA_PROVISIONING_README.md#stage-a-disabled-lan-application-segment) for naming, eligibility, ports, and recovery.

JSON accepts a list or a list under `rows`, `result.rows`, or `vlans`. Invalid structures fail instead of silently producing no VLANs. IPv6 is not supported by this workflow.

Reference exports write JSON and CSV under `vlans/`, and update `sites.csv`. CSV excludes WAN and HA-internal VLANs by default; those are typically template-managed. `--include-wan` and `--include-ha` include them for inspection, not automatic safe redeployment. `--json-only` skips the CSV and points the exported row at the JSON, which includes unfiltered networks.

Private DNS is read from the site's group membership before any export files are written. The exporter supports `result[].membership_info.ip_prefix` and the legacy `member_attributes.ip_prefix` response. Only a recognized empty membership exports a blank field; failed requests or malformed responses stop the export and leave existing files untouched. Host `/32` suffixes are removed for readability; other prefix lengths are preserved.

### Loopback management (`lo0`)

For creation, a loopback management row must use `/32`. Leave DHCP endpoints blank and set `dhcp_service=off`. For example:

```csv
name,tag,subnet,default_gateway,dhcp_start,dhcp_end,interface,zone,enabled,share_over_vpn,dhcp_service
MGMT,1,32,172.16.65.1,,,lo0,Management Zone,true,false,off
```

During deployment, the engine submits the loopback management network even if `lo0` is not yet exposed by the gateway, and continues submitting the other VLANs. After submission it checks for an unambiguous `lo0` interface ID on every target gateway. If that check fails or cannot be completed, the UI and run report show a separate incomplete **Loopback binding** stage. This keeps the overall run incomplete, but allows the disabled ZPA LAN segment to be staged when VLAN configuration and App Connector provisioning succeed. Actual VLAN creation, enablement, or sharing failures still block segment staging. An accepted POST is not proof that management is operational; check the binding after activation. A rejected or uncertain loopback POST is reported separately, and does not stop the other VLAN submissions. Requests are not automatically retried, and completed reports do not automatically update after activation. Preview does not perform this post-creation check.

Review exported management rows against these requirements before deploying them to a new site.

## Credential configuration

Copy `.env.example` to a new local `.env` and populate the tenant URL and API key. Keep existing credentials when updating the project.

- `ZTB_API_BASE` accepts an HTTPS root URL or a URL ending in `/api/v2` or `/api/v3`. `ZIA_API_BASE` is a legacy fallback.
- `API_KEY` is needed to obtain or refresh a token. Leave `BEARER` blank or `AUTO_POPULATED` for automatic login.
- `--env-file` selects another credential file. Process environment variables override its values.
- The client refreshes and retries once after HTTP 401. Authentication writes refreshed tokens to the selected credential file.
- Standalone `ztb_login.py` and `zpa_login.py` commands print bearer tokens for shell use; their output should not be shared.
- `.env` is Git-ignored. Other credential filenames and files under `vlans/` are not automatically protected by that rule. Keep credentials and real tenant exports out of commits.

The site CSV resolves VLAN paths relative to its own directory. Relative `--env-file`, `--csv`, and `--report-dir` paths resolve from the working directory. The exporter writes `sites.csv` and `vlans/` beside `pull_site.py`.

## Site inventory API contract

Read-only checks on 4 October 2026 verified `GET /api/v3/Gateway` with `gateway_type=isolation`, `template_id=`, `sort=location`, `sortdir=asc`, `search=`, and `refresh_token=enabled`:

- `page` is a zero-based page number, not a row offset. Limits of one and two returned consecutive slices of the same six-site inventory.
- The response has `rows` and `count`. `count` is the total matching inventory size, not the number of rows on the current page. It stays present on empty pages beyond the end.
- `search` filters both rows and count. A query matching one site returned `count: 1`, including on the empty next page.

Deployment reads up to 10,000 sites in pages of 100 and blocks creation when completeness cannot be established. It fetches another page after every full page, even if the total has already been reached, and checks page one before accepting an empty page zero. Total metadata, when present, must be valid and consistent on every page. Validation and exact matching use stripped, casefolded names. Each execution refreshes its inventory; there is no coordination between separate operators or sessions.

The live tenant available for verification had six sites; inventories over 100 and the 10,000-site boundary have offline test coverage, not live scale qualification. The browser reference picker still lists at most 100 sites.

[Return to the setup guide](../readme.md)

# UCaaS local breakout

Enable **UCaaS local breakout** on a new branch to send selected collaboration
destinations directly through its WAN interfaces, bypassing ZIA. It is off by
default. Teams, Zoom Meetings and Webex are selected when first enabled; Google
Meet is optional. Branch copies retain the choice. Saved projects and CSV export
retain the flag, service selection, secondary WAN override and path selection.

The deployment preview lists two shared rules, their combined destination/protocol/port sets, and whether each
reusable destination or Port object will be created or reused. IP address objects contain **IPv4 only**. Domain objects are
separate; this does not claim to configure IPv6 routing. This feature is forwarding
configuration, not a complete firewall allowlist or a replacement for vendor
connectivity requirements.

## Deployment behavior

1. Preview reads the template WANs, SaaS application catalog and all pages of network,
   domain, Port and SaaS App objects. It creates nothing. Only exact content
   matches can be reused; a broad or outdated object named “Zoom” is insufficient.
2. The site's selected WAN is primary. If exactly one other template WAN exists,
   it becomes secondary. With more WANs, choose a secondary explicitly. Two
   distinct WANs are required. **Path selection** defaults to **Best**. Choose
   **None** for primary/secondary failover, or **Balanced** to distribute traffic
   across both WANs. HA remains blocked pending separate verification.
3. Any requested template clone is created first, followed by the new site.
4. After gateway discovery, the deployed gateway's WAN roles are checked. Missing
   destination and Port objects are created with stable, content-versioned names. Objects
   already matching the selection are reused across branches. Existing objects
   are never edited or broadened.
5. All objects are confirmed before two site rules are created: **UCaaS Web Breakout**
   combines published TCP 80/443 and UDP 443 across all selected UCaaS destination
   objects; **UCaaS Media Breakout** combines the remaining published ports across
   the selected media destination objects. Every original destination/port pairing
   remains covered. The combined match also allows one selected provider’s ports
   to apply to another selected provider’s destinations within that rule.
   Only ports published for selected services are included. Each rule uses the site's network source
   group, its destination objects, a reusable `l4port` object through
   `dst_port_group_id`, both WANs and the selected path behavior. There is no Any-port rule.
   WAN next-hop overrides are empty, using each WAN's configured routing instead
   of copying reference-site addresses.
6. These site rules are moved above existing site rules, preserving their relative
   order. Inherited template rules are excluded from the reorder request. Policy
   and compiled-rule read-back must confirm priority above inherited rules before
   the stage is marked complete.

Reports retain object names, IDs, destination values, catalog versions, gateway
and every policy name, ID, Port object ID and verification status. Timeouts and uncertain POST outcomes are not blindly retried.
An incomplete stage leaves a partial deployment and a recovery message; inspect
the named resources before recovery. Do not repeat site creation. No resources
are automatically deleted on failure.

## Destination and Port objects

Names identify the provider or shared purpose, for example
`UCaaS-Teams-Media-IPv4-<hash>` and `UCaaS-Media-Ports-<hash>`. They are
normal reusable tenant objects. Identical contents within a plan share an object;
exact existing objects retain their names. Port reuse compares normalized TCP/UDP
sets, so reordered or equivalent ranges can match, while broader sets cannot.
Deployment reads the inventory again: an object created by an earlier branch
after preview can be reused. Changing path selection does not change object
contents, hashes or names.

Destination objects keep their provider names. The two shared Port objects use
`UCaaS-Web-Ports-<hash>` and `UCaaS-Media-Ports-<hash>`, unless an exact existing
object can be reused, in which case its name is retained. Existing provider Port
objects remain available and are not deleted or changed.

For Teams, Zoom and Webex, Web uses TCP 80/443 and UDP 443. Media uses TCP
5004/8801–8802 and UDP 3478–3481/5004/8801–8810/9000. Selecting Google Meet adds
UDP 19302–19309 to Media. Selecting only one provider includes only that
provider’s published ports. All selections still produce two rules.

## Path selection

| Choice | Behavior | Native API flags |
| --- | --- | --- |
| Best (default) | Select the WAN with the best loss, latency and jitter score. | `best_link=true`, `ecmp_enabled=false` |
| None | Use primary WAN; switch to secondary if primary goes down. | Both flags `false` |
| Balanced | Balance traffic across primary and secondary WANs. | `best_link=false`, `ecmp_enabled=true` |

The same choice applies to both UCaaS rules. Review and reports show the chosen
mode, and deployment verifies both flags on read-back. Old drafts and CSV files
without a choice default to Best. The dropdown uses local options and makes no
live lookup when opened.

Verified against the native portal and tenant API schema on 2026-09-27, and
[Zscaler's WAN link selection documentation](https://help.zscaler.com/zero-trust-branch/release-upgrade-summary-2025).

## Vendor destinations and refresh

The editor reads `data/ucaas_endpoints.json` locally. Opening a dropdown does not
fetch vendor publications. Under **Selected destinations, ports & sources**, **Refresh
vendor lists** fetches official sources and replaces the catalog only if all
parsers and validation succeed. It invalidates any ready deployment preview.
Catalogs older than 30 days block tenant preview. Refreshing the catalog does not
change objects or policies on already deployed sites.

The catalog records retrieval time, source SHA-256 hashes, content versions and
excluded IPv6 counts. Parsers read bounded sections, avoiding obsolete addresses
in revision histories. A changed page structure fails closed and preserves the
previous catalog. The selected snapshot is pinned in the deployment approval.

| Service | Included scope | Official source |
| --- | --- | --- |
| Microsoft Teams | Required Worldwide Teams/Skype service-area IPv4 and domains. Other Microsoft 365 services and Common dependencies keep existing forwarding. | [Microsoft endpoint service](https://learn.microsoft.com/en-us/microsoft-365/enterprise/microsoft-365-ip-web-service) |
| Zoom Meetings | Meetings/Webinars IPv4 feed and Zoom service domains. Phone, Contact Center and shared third-party website dependencies are excluded. | [Zoom network settings](https://support.zoom.com/hc/en/article?id=zm_kb&sysparm_article=KB0060548) |
| Webex | Media, including Teams video integration, core micro-services and content storage. Cisco explicitly includes `*.cisco.com`; additional integrations and third-party domains are excluded. | [Webex network requirements](https://help.webex.com/article/WBX000028782) |
| Google Meet | Workspace and consumer media, static resources, API and streaming domains. Exact Docs/Chat hosts are included as published; path-specific feedback URLs are excluded. | [Google Meet network preparation](https://knowledge.workspace.google.com/admin/meet/prepare-your-network-for-meet-meetings-and-live-streams) |

The public refresh can also be run with `python3 ucaas_catalog.py --refresh`.
It does not authenticate to or write to a ZTB tenant.

## CSV fields

| Field | Values |
| --- | --- |
| `ucaas_local_breakout` | `0` (default) or `1` |
| `ucaas_services` | Comma-separated `teams,zoom,webex,meet`; default `teams,zoom,webex` if omitted. Empty is invalid while enabled. |
| `ucaas_secondary_wan` | Optional exact WAN name; omitted means infer the unique other WAN. |
| `ucaas_path_selection` | `best` (default), `none`, or `balanced`. |

## Verification status

The Utrecht 172.30 acceptance test verified live template cloning, network
configuration, enabled ZPA resources, UCaaS objects and rule ordering. Its ten
original UCaaS rules were replaced with the two shared rules, verified in the API
and native portal, preserving all other rules and existing objects. Offline
tests cover all three path modes, object reuse across modes, invalid choices,
review invalidation and mismatched policy flags. None and Balanced have not been
deployed in the live tenant. Tests also cover all 15 service selections, retention of every original published
destination/port pairing, exact combined port sets, object reuse, WAN inference,
ordering and uncertain writes. Gateway activation, call-quality and WAN failover
tests remain necessary to validate appliance behavior.

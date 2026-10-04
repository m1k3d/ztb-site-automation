# Private-domain DNS during deployment

**Use private DNS only for selected domains** is optional and off by default,
under **Site details → DNS & DHCP services**. Enter private zones, one per line
or separated by commas. Each includes its apex and wildcard subdomains, e.g.
`mikedsecure.corp` includes `mikedsecure.corp` and `*.mikedsecure.corp`.
Local hosts still need records in private DNS.

Enabling the option reveals a read-only **Expected DNS policy order** preview
below the domain list. It updates locally as domains and DNS server addresses
change, highlights the private-domain rule, and prompts for missing values.
It makes no tenant calls or policy changes. Tenant preview and deployment
read-back remain responsible for verifying the actual rule order.

The resulting order is:

1. Existing Zscaler domains → WAN DNS.
2. Existing ZPA application domains → ZPA DNS.
3. Selected private domains → Private DNS.
4. Existing all-domains rule → WAN DNS.

ZPA retains priority when an application name also matches a private domain.
The private-domain rule sits immediately above the catch-all, never above ZPA.
Automatic policies are retained. Only the final rule's resolver changes; its
name, matching conditions, logging and cache settings are preserved.

Explicit IPv4 **Private DNS servers** and **WAN DNS servers** are required.
The private DNS deployment stage must succeed, and its exact site membership
must verify before policy writes. The DNS step configures and verifies the
site's WAN DNS membership, which may initially be empty even when WAN DNS
addresses were supplied during creation. No global system object is edited.

## Objects and preview

Reuse requires an exact full domain-set match in a visible, static user object.
Otherwise the step creates a readable name such as `DNS-Private-mikedsecure.corp`,
without a hash. Two zones include both names in alphabetical order; longer lists
use the first zone followed by `-plus-N-more`. The apex and wildcard count as one
zone. Long names are shortened to fit 255 characters. If another object has the
same name but different contents, the new name receives `-2`, `-3`, etc.; the
existing object is never overwritten. Names are also reserved across a rollout
so different lists in the same batch cannot claim the same name. Exact matching
objects retain their existing names, including older hashed names. System, hidden,
dynamic, nested and broader domain groups are excluded from content-based reuse.
Built-in WAN, private and ZPA resolver objects retain their identities and are
used in the new site's context.

Tenant preview shows domains, resolver addresses, object reuse/creation and rule
order. Preview creates nothing. Editing domains or resolvers, or changes to
reviewed object definitions, require a new preview. Saved projects, branch
copies and CSV exports retain the optional choice.

## Deployment and recovery

`dns_policy.DnsPolicy` owns planning, inspection, application and verification
independently of site creation. The deployment engine invokes it after private
DNS configuration and reports **DNS policy** as a separate stage.

The private rule's position is verified through policy and compiled-rule APIs
before the catch-all changes to WAN DNS. Final definitions, object contents,
order and resolver memberships must all verify before success is reported.

Uncertain writes are not blindly retried. Reports retain the pending write,
known IDs, previous policies and previous WAN DNS membership. Inspect those
resources before recovering only DNS. The shared component can reuse exact
resources from a partial run; normal deployment still refuses an existing site.

V1 accepts the three verified automatic policies plus its own exact private rule
when recovering. Unexpected/inherited policies, changed default matches,
different site scopes or ambiguous objects stop the step for review. Existing-site
editing and arbitrary reference-policy copying are deferred to V2. HA remains
blocked until separately validated. API checks do not establish appliance DNS
reachability; test resolution after activation.

## CSV fields

| Field | Meaning |
| --- | --- |
| `dns_split` | `1` enables private-domain DNS; blank or `0` retains automatic policies. |
| `dns_private_domains` | Private zones separated by commas, whitespace or semicolons. `*.zone` also includes its apex. |
| `private_dns` | Required private resolver IPv4 host addresses when enabled. |
| `wan_dns` | Required WAN resolver IPv4 host addresses when enabled. |

## Live API verification

On 2026-09-27, the shared DNS deployment component was applied to the existing,
unactivated `Utrecht-Test-172-30` acceptance site using the example domain
`mikedsecure.corp`. It created `DNS-Private-Domains-28f33c4a` (ID 255), containing
the apex and wildcard, and one private-domain rule (ID 1178) in position 3.
The test object was subsequently renamed to `DNS-Private-mikedsecure.corp`,
preserving ID 255, its contents and every policy reference. Independent read-back
confirmed that only its name and display name changed.
The original Zscaler and ZPA rules remained unchanged in positions 1 and 2.
The original catch-all retained its ID and settings, apart from moving to
position 4 and using WAN DNS.

Private DNS membership verified as `172.30.150.253,172.30.150.252`; the initially
empty WAN DNS membership was populated and verified as `1.1.1.1,8.8.8.8`.
Separate authenticated read-back checked policy definitions, compiled-rule
order, object reuse and both resolver memberships. All nine acceptance checks
passed. Artifacts are in `out/dns-verification/`.

This verifies the DNS configuration component on a staged standalone site. No
new site was created for this test, no appliance was activated, and DNS query
resolution or HA DNS behavior was not exercised. Existing-site editing remains
a future UI feature; this test used a restricted acceptance helper.

Administrators can add further domain objects to the policy's destination list
in ZTB when those domains should use the same private DNS servers. That is a
native policy edit; this version of the tool does not reconcile manual changes
on existing sites or broaden its strict DNS recovery checks automatically.

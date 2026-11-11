# Forwarding rules from the reference site

Pulling a reference site also reads its own forwarding (PBR) rules. Each new
branch created from that reference recreates them after its networks exist.
There is no switch to turn this on: when the reference has its own rules,
**Site details → Forwarding rules from reference** lists them read-only, and
tenant preview shows the final values for each branch.

## What is copied

- **Copied:** the reference site's own rules on each gateway, with their source,
  destination, port object, ZPA application segments, next-hop type, primary and
  secondary interface and next hop, path selection (Best, None, Balanced),
  negation, HA and other rule flags.
- **Not copied:**
  - **Template rules.** They come with the template, or its clone.
  - **Platform default rules** (Default-ZPA-PBR, Default-App-Segment-PBR,
    Default ZScaler Rule and Default ZIA Rule). The platform creates these on
    every new site, and they are never duplicated or edited.
  - **UCaaS breakout rules made by this tool.** The UCaaS setting controls them.
  - **Source ports.** The native rule editor has no source port.
  - **WAN interface monitoring.**

Copied rules keep their reference order and their position relative to the
default rules. UCaaS breakout rules, when enabled, stay above everything.
Each copied rule gets a name in the native style: the first nine characters of
its display name plus a stable suffix. HA peers use the same name, as in the portal.

## How each branch differs from the reference

| Reference value | On the branch |
| --- | --- |
| Shared object containing reference networks (e.g. `Guest Networks` = `172.16.69.0/24`) | Reused **unchanged**. The tool never edits shared objects, because that would change other sites' rules. Preview and the report list any branch network the object doesn't include (e.g. `172.29.69.0/24`). Until you add it in ZTB, the copied rule won't match that branch network. |
| Object typed directly into a rule (hidden `-custom-internal-…`) | A new hidden object for the new rule, as the portal does. Addresses inside reference networks move to the branch's matching network. |
| System and other shared objects, SaaS apps, ports, ZPA application segments | Reused unchanged. |
| VLAN sub-interface (e.g. `ge2.69`) | The branch's matching VLAN, including a changed VLAN ID (`ge2.79`). |
| LAN next hop | The same position in the branch's matching network. A VLAN gateway maps to the branch's VLAN gateway. |
| WAN gateway next hop | The branch WAN's static gateway. For a DHCP WAN it is left empty, so the WAN's own routing is used. |
| ZIA tunnel and ZIA gateway | Follows the branch's own Default ZIA Rule. Reference tunnels are site-specific. |

The branch's matching network is found by **name**, then by interface and VLAN ID.
A rule is blocked at **Validate & review** if a value can't be mapped. Examples:

- No matching network.
- A rule WAN that isn't configured on the branch.
- A next hop that is a reference address on a different interface.
- A ZIA rule that uses a different tunnel from the reference's Default ZIA Rule.

The message names the rule. To fix it, keep the network's name or VLAN ID on
the branch, keep **Copy additional WANs** on, or correct the rule on the
reference and pull it again.

## Preview and deployment

Tenant preview creates nothing. It checks that every reused object still exists
with the same type and that each interface belongs to the template gateway. It
also lists any branch network a shared object doesn't include. Approval is bound
to those results.
If an object changes afterwards, preview must run again.

Deployment runs the **Forwarding rules** stage after VLANs, additional WANs,
UCaaS and VRRP, and only when those stages succeeded. It runs these steps in order:

1. **Check each gateway.** The interfaces must exist. Before activation a
   gateway lists only its ports, so a VLAN sub-interface such as `ge2.69` is
   accepted when the site has a provisioned network on that port and VLAN for
   that gateway. The site may have only the default rules and this run's UCaaS rules.
2. **Create the rule-specific objects.**
3. **Create the rules.** Each rule is read back field by field.
4. **Restore the reference order.** The order is verified through the policy and
   compiled-rule APIs. The default, template and UCaaS rules must be unchanged.

Shared objects are never written.

Uncertain writes are reported with the pending write and are never retried
automatically. Inspect the named objects and rules before recovery, and don't
repeat site creation. Reports keep the rule names and IDs, the object actions,
and any branch network a shared object doesn't include.

## Saved data and verification

The reference snapshot is stored in the draft field `forwarding_rules_json`. It
contains the rules, the objects they use and the reference networks and WANs
needed for mapping. Saved projects, branch copies and CSV export keep it. Only
the browser reference pull captures it; `pull_site.py` does not.

Rule, object and network shapes were read from the live tenant on 2026-11-11 for:

- a standalone site with a guest-network rule, ZIA DTLS and WAN next hops;
- a standard HA site with ZIA IPsec and ZPA application-segment rules;
- a rule with hidden source and destination objects.

**Live acceptance, 2026-11-11.** A standalone branch, `FwdRules-Test-Utrecht-1111`,
was built from Utrecht-NL-BR with `172.16` changed to `172.29`. It reused the
template, created a new ZIA location, and requested no App Connector, ZPA
segment, UCaaS or private-domain DNS.

The first run created the site, the additional WAN, private DNS and the VLANs.
It stopped before writing any rules because the pre-activation gateway did not
list `ge2.69`; this led to the staged sub-interface check above.

The forwarding stage was then run on that site and passed:

- `o365` and `Guest-Block` were created at positions 1–2, above the four
  unchanged default rules, with the template rule last.
- `o365` follows the site's IPsec Default ZIA Rule.
- `Guest-Block` uses `ge2.69` via `172.29.69.1`.
- During that test the tool still added `172.29.69.0/24` to `Guest Networks`.
  This was removed on 2026-11-12 at the user's request; shared objects are now
  never edited.
- Policy and compiled-rule read-back confirmed the order.

HA has offline coverage only. Traffic and activation were not tested.

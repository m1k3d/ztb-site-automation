# 🧠 ZTB Site Automation
**Automate (Zscaler ZTB) site creation and VLAN provisioning — API-driven, reliable, and HA-aware.**

Author: **Mike Dechow (@m1k3d)**  
Repo: [github.com/m1k3d/ztb-site-automation](https://github.com/m1k3d/ztb-site-automation)  
License: MIT  
Version: 1.3.3

---

## 🚀 Overview

This automation suite is designed to **rapidly deploy Zscaler ZTB sites** — including VLANs, HA pairs, and template-based configurations — using a CSV-driven workflow.  
It mirrors the same behavior and API calls used by the ZTB UI, but at **scale**.

### ✨ Key Capabilities

- **Create sites** using Jinja2-templated payloads derived from a golden site.
- **Deploy VLANs** in bulk with DHCP, DNS, and zone metadata.
- **Supports both Standalone and HA** gateway architectures.
- **Template resolution** (by name → ID) for dynamic site creation.
- **Post-provision actions**: VLAN enablement, share-over-VPN, DHCP service patching.
- **Dry-run and Debug** modes for safe validation.
- **Future-ready** for VRRP, multi-WAN, and ZPA integration.

---

## 🧩 Directory Layout

Project root (ztb-site-automation/):
- bulk_create.py — Creates sites and VLANs from CSV definitions.
- pull_site.py — Pulls a site configuration from the API into sites.csv and VLAN CSVs.
- vlans_convert.py — Converts VLAN API output ↔ CSV format for editing or comparison.
- ztb_login.py — Authenticates and exports the BEARER token automatically to .env.
- site_template.json.j2 — Jinja2 site creation template (used by bulk_create.py).
- sites.csv — Master CSV with one row per site (includes template, WAN, and VLAN file references).
- vlans/ — Folder containing VLAN definitions per site (e.g., Manufacturing-Site.csv).
- .env — Environment variables (tenant API URL, BEARER token, and optional referer path).

Optional folders (recommended):
- logs/ — Stores execution logs, debug traces, and run summaries.
- archive/ — Keeps historical VLAN CSVs or site exports for version tracking.
- examples/ — Contains sample templates, CSVs, and example payloads for reference.
---

## ⚙️ Before You Begin

Before using the automation, set up your environment and your **golden template**.

### 1️⃣ Create a Golden Template in the UI
- Log in to **Zscaler ZTB**.
- Create a new **template** for your branch site type.
- Configure base values such as:
  - DNS servers
  - DHCP service mode (Server or Relay)
  - Default WAN/LAN zones
  - Gateway model and interface layout
- Save and note your **Template Name**.

> **Tip:** Name templates consistently, e.g. `Branch-HA` or `Single-Gateway`.

---

### 2️⃣ Create a Golden Site
1. In the ZTB UI, create a **site** using that template.  
2. Populate **all mandatory fields** (WAN IPs, DHCP relay IP, DNS, etc.).
3. Once deployed, this will serve as your **“source of truth”** for future sites.

---

### 3️⃣ Prepare Environment Variables (`.env`)

Create a file named `.env` in the repo root:

```bash
ZTB_API_BASE="https://<tenant>-api.goairgap.com/api/v3"
BEARER="auto filled by ztb_login.py"
API_KEY="CREATE IN UI"

You can use ztb_login.py to fetch and export this automatically.

Example:

python3 ztb_login.py && set -a && source .env && set +a


⸻

🧠 Step-by-Step Workflow

1️⃣ Pull Your Golden Site

Use pull_site.py to extract your golden site configuration and VLANs.

python3 pull_site.py --site "<Your Golden Site Name>" --include-wans

Arguments:

Flag	Description
--site	Site name to pull from API
--include-wans	Include WAN interface info in output
--list-templates	Optional: list all available templates
--debug	Verbose API output

This creates:
	•	sites.csv → one row for your golden site
	•	vlans_<sitename>.csv → VLAN configuration

⸻

2️⃣ Prepare sites.csv

Duplicate the sites.csv and create additional rows for every site you want to deploy.

Example:

site_name,template_name,template_id,gateway_name,gateway_name_b,wan0_ip,wan0_mask,wan0_gw,wan0_interface_name,wan1_ip,wan1_mask,wan1_gw,wan1_interface_name,dhcp_server_ip,per_site_dns,vlans_file,post
Amsterdam,Branch-HA,,BRANCH-A-GW-A,BRANCH-A-GW-B,192.0.2.10,255.255.255.252,192.0.2.9,ge3,198.51.100.10,255.255.255.252,198.51.100.9,ge4,10.0.0.1,"1.1.1.1,8.8.8.8",vlans_amsterdam.csv,1

post=1 marks which rows will actually deploy.
Use template_name (preferred) or template_id.
DHCP relay IPs must be defined when the template requires it.

⸻

3️⃣ Prepare VLAN CSVs

Each site references a VLAN CSV file.
You can duplicate your pulled VLAN CSV (from the golden site) and adjust as needed.

Example (vlans_branch.csv):

name,tag,subnet,default_gateway,dhcp_start,dhcp_end,interface,zone,enabled,share_over_vpn,dhcp_service
10-Users,10,10.10.10.0/24,10.10.10.1,10.10.10.100,10.10.10.150,ge5,LAN Zone,true,false,inherit
20-IoT,20,10.20.20.0/24,10.20.20.1,10.20.20.10,10.20.20.50,ge6,IoT Zone,true,true,non_airgapped


⸻

4️⃣ Run the Automation

Dry Run (Validation Only)

python3 bulk_create.py --dry-run

Outputs rendered payloads and VLAN counts, but does not POST to API.

Full Deployment

python3 bulk_create.py

Debug Mode

python3 bulk_create.py --debug

Prints each REST call (useful for troubleshooting tenant-specific /api/v3 quirks).

⸻

5️⃣ Post-Deployment Behavior

After each site is created:
	1.	The script polls /api/v3/Gateway until gateway and cluster IDs appear.
	2.	VLANs are POSTed via /api/v2/Network/.
	3.	VLANs are enabled (status=provisioned).
	4.	Optional flags (share_over_vpn, dhcp_service) are patched post-deploy.

⸻

🧱 File Reference

File	Description
bulk_create.py	Creates sites and VLANs from CSV definitions
pull_site.py	Extracts an existing site and its VLANs
vlans_convert.py	Converts raw VLAN JSON to human-readable CSV
ztb_login.py	Retrieves API bearer token automatically
site_template.json.j2	Jinja2 template defining payload structure
sites.csv	Source of truth for site creation
vlan_.csv	VLAN definitions per site
.env	Tenant API configuration


⸻

🧩 Extending Functionality (Future Enhancements)

⚙️ Planned: VRRP Interface and Tracking

Currently, VRRP is not implemented, but can easily be added using:

vrrp_interface,vrrp_track_interfaces

and a small Jinja2 block:

"vrrp": {
  "interface": "{{ vrrp_interface }}",
  "track_interfaces": "{{ vrrp_track_interfaces }}"
}


⸻

🌐 Planned: Multi-WAN / Dual ISP Support

Future support for multi-WAN deployments where each device connects to multiple ISPs.

wan2_ip,wan2_mask,wan2_gw,wan2_interface_name

The automation will dynamically create a second WAN object when detected.

⸻

🧠 Other Future Ideas

Enhancement	Description	Status
DHCP Option Injection	Support custom DHCP options via CSV	Future
ZPA App Connector Hook	Auto-Create ZPA provision key and post into branch


⸻

💡 Best Practices

✅ Create and validate all templates in the UI first
✅ Maintain consistent naming conventions
✅ Test golden site pulls regularly
✅ Use dry-run before live deployments
✅ Version-control your CSVs and templates in Git

⸻

🧩 Example Workflow Summary
	1.	Create template in UI → with DHCP/DNS preconfigured.
	2.	Create golden site → verify VLAN and WAN setup.
	3.	Run pull_site.py → export site and VLAN configs.
	4.	Duplicate and edit sites.csv → one row per site.
	5.	Duplicate VLAN CSVs → per site or site type.
	6.	Run bulk_create.py → sit back and watch automation magic.
	7.	Validate in ZTB UI → confirm site and VLANs appear as expected.

⸻

🏁 Example Commands Recap

# Authenticate and export token
python3 ztb_login.py && set -a && source .env && set +a

# Pull a golden site
python3 pull_site.py --site "Branch-Golden" --include-wans

# Create multiple new sites (dry run)
python3 bulk_create.py --dry-run

# Deploy for real
python3 bulk_create.py


⸻

🧰 Troubleshooting Tips

Symptom	Likely Cause	Fix
Missing template_id	Template not specified or typo in name	Add template_name or ID
gateway/cluster not ready	API delay after site creation	Increase retries in bulk_create.py
VLAN ERR 400	Duplicate VLAN tag or HA VLAN conflict	Exclude HA VLANs during pull
401 Unauthorized	Expired BEARER token	Re-run ztb_login.py
VLANs not visible	Template missing zone mapping	Check UI template config


⸻

🧭 License

This project is licensed under the MIT License — feel free to modify and extend it for your own organization.


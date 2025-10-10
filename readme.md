# 🧠 ZTB Site Automation
**Automate (Zscaler ZTB) site creation and VLAN provisioning — API-driven, reliable, and HA-aware.**

Author: **Mike Dechow (@m1k3d)**  
Repo: [github.com/m1k3d/ztb-site-automation](https://github.com/m1k3d/ztb-site-automation)  
License: MIT  
Version: 1.4.0

---

## 🚀 Overview

This automation suite is designed to **rapidly deploy Zscaler ZTB sites** — including VLANs, HA pairs, and template-based configurations — using a CSV-driven workflow.  
It mirrors the same behavior and API calls used by the ZTB UI, but at **scale**.

### ✨ Key Capabilities

- **Create sites** using Jinja2-templated payloads derived from a reference site.  
- **Deploy VLANs** in bulk with DHCP, DNS, and zone metadata.  
- **Supports both Standalone and HA** gateway architectures.  
- **Template resolution** (by name → ID) for dynamic site creation.  
- **Post-provision actions**: VLAN enablement, share-over-VPN, DHCP service patching.  
- **VRRP auto-configuration** — dynamically identifies HA, WAN, and LAN interfaces.  
- **Dry-run and Debug** modes for safe validation.  

---

## 🧩 Directory Layout

**Project root (`ztb-site-automation/`):**
- `bulk_create.py` — Creates sites, VLANs, and applies VRRP  
- `pull_site.py` — Pulls a site configuration from the API into sites.csv and VLAN CSVs  
- `vlans_convert.py` — Converts VLAN API output ↔ CSV format for editing or comparison  
- `ztb_login.py` — Authenticates and exports the BEARER token automatically to .env  
- `site_template.json.j2` — Jinja2 site creation template (used by bulk_create.py)  
- `sites.csv` — Master CSV with one row per site (includes template, WAN, and VLAN references)  
- `vlans/` — Folder containing VLAN definitions per site (e.g., `Manufacturing-Site.csv`)  
- `.env` — Environment variables (tenant API URL, BEARER token, and optional referer path)  

**Optional folders (recommended):**
- `logs/` — Stores execution logs, debug traces, and run summaries  
- `archive/` — Keeps historical VLAN CSVs or site exports for version tracking  
- `examples/` — Contains sample templates, CSVs, and example payloads for reference  

---

## ⚙️ Before You Begin

Before using the automation, set up your environment and your **reference template**.

### 1️⃣ Create a Reference Template in the UI

1. Log in to **Zscaler ZTB**.  
2. Create a new **template** for your branch site type.  
3. Configure base values such as:
   - DNS servers  
   - DHCP service mode (Server or Relay)  
   - Default WAN/LAN zones  
   - Gateway model and interface layout  
4. Save and note your **Template Name**.  

> 💡 **Tip:** Name templates consistently, e.g. `Branch-HA` or `Single-Gateway`.

---

### 2️⃣ Create Zones

Before you create your reference site, make sure the necessary zones exist.

1. In the **ZTB UI**, go to:  
   **Resources → Objects → Add → Zone**
2. Create zones for each traffic type you plan to reference in VLAN CSVs — for example:
   - LAN Zone  
   - IoT Zone  
   - Guest Zone  
   - Voice Zone  
3. These zone names must exactly match the names you reference in your VLAN CSVs.

> ⚠️ **Note:** Zone names are case-sensitive and must match exactly in your automation CSVs.

---

### 3️⃣ Create a Reference Site

1. In the ZTB UI, create a **site** using your chosen template.  
2. Populate **all mandatory fields** (WAN IPs, DHCP relay IP, DNS, etc.).  
3. Once deployed, this becomes your **reference site** — one you can easily replicate for future branches.

> 🧩 **Example:**  
> Use this reference site as a base for other locations that share a similar VLAN architecture.  
> For example, if VLAN 10 at the reference site uses `172.16.10.0/24`, you might configure a new site with the same structure but shift the addressing pattern (e.g., `172.17.10.0/24`) to maintain consistency.

---

### 4️⃣ Prepare Environment Variables (`.env`)

```bash
# 🔐 Environment Setup (.env)

ZTB_API_BASE="https://<tenant>-api.goairgap.com/api/v3"
BEARER="auto filled by ztb_login.py"
API_KEY="CREATE IN UI"

💡 Note:
The BEARER value is automatically generated and updated by ztb_login.py when you first run any script.
You do not need to manually edit or export environment variables — everything is handled automatically.

⸻

🔁 How Authentication Works
	•	If no valid BEARER token exists in .env, the script automatically runs:

python3 ztb_login.py


	•	It fetches a new token and updates .env.
	•	The updated .env is reloaded automatically.
	•	If any API call returns 401 Unauthorized, the script re-authenticates once and retries.
	•	All scripts (pull_site.py, bulk_create.py, etc.) support this behavior natively.

⸻

🧠 Step-by-Step Workflow

1️⃣ Pull Your Reference Site

Use pull_site.py to extract your reference site configuration and VLANs.

python3 pull_site.py --site-name "<Your Reference Site Name>" --include-wans

Arguments:

Flag	Description
--site-name	Site name to pull from API
--include-wans	Include WAN interface info in output
--list-templates	(Optional) List all available templates
--debug	Verbose API output

Creates:
	•	sites.csv → one row for your reference site
	•	vlans_<sitename>.csv → VLAN configuration

⸻

2️⃣ Prepare sites.csv

Duplicate the generated sites.csv and create additional rows for each site you want to deploy.

site_name,template_name,template_id,gateway_name,gateway_name_b,wan0_ip,wan0_mask,wan0_gw,wan0_interface_name,wan1_ip,wan1_mask,wan1_gw,wan1_interface_name,dhcp_server_ip,per_site_dns,vlans_file,post
Amsterdam,Branch-HA,,BRANCH-A-GW-A,BRANCH-A-GW-B,192.0.2.10,255.255.255.252,192.0.2.9,ge3,198.51.100.10,255.255.255.252,198.51.100.9,ge4,10.0.0.1,"1.1.1.1,8.8.8.8",vlans_amsterdam.csv,1

	•	post=1 marks which rows to deploy.
	•	Use template_name (preferred) or template_id.
	•	DHCP relay IPs must be defined when required by the template.

⸻

3️⃣ Prepare VLAN CSVs

Each site references a VLAN CSV file.
Duplicate your pulled VLAN CSV (from the reference site) and adjust as needed.

name,tag,subnet,default_gateway,dhcp_start,dhcp_end,interface,zone,enabled,share_over_vpn,dhcp_service
10-Users,10,10.10.10.0/24,10.10.10.1,10.10.10.100,10.10.10.150,ge5,LAN Zone,true,false,inherit
20-IoT,20,10.20.20.0/24,10.20.20.1,10.20.20.10,10.20.20.50,ge6,IoT Zone,true,true,non_airgapped


⸻

4️⃣ Run the Automation

Dry Run (Validation Only):

python3 bulk_create.py --dry-run

Full Deployment:

python3 bulk_create.py

Debug Mode:

python3 bulk_create.py --debug


⸻

5️⃣ Post-Deployment Behavior

After each site is created:
	1.	The script polls /api/v3/Gateway until gateway and cluster IDs appear.
	2.	VLANs are POSTed via /api/v2/Network/.
	3.	VLANs are enabled (status = provisioned).
	4.	VRRP configuration is automatically applied:
	•	HA link inferred from the template’s HA interface.
	•	LAN and WAN tracking inferred from the CSVs.
	•	Uses a fixed virtual_router_id = 16 defined in code (no .env variable required).
	5.	Optional flags (share_over_vpn, dhcp_service) are patched post-deploy.

⸻

🧱 File Reference

File	Description
bulk_create.py	Creates sites, VLANs, and applies VRRP
pull_site.py	Extracts an existing site and its VLANs
vlans_convert.py	Converts raw VLAN JSON to human-readable CSV
ztb_login.py	Retrieves API bearer token automatically
site_template.json.j2	Jinja2 template defining payload structure
sites.csv	Source of truth for site creation
vlan_.csv	VLAN definitions per site
.env	Tenant API configuration


⸻

💡 Best Practices

✅ Validate all templates in the UI before automating
✅ Maintain consistent naming conventions
✅ Use dry-run before live deployments
✅ Version-control your CSVs and templates
✅ Keep .env minimal — only core variables (no VRRP or experimental fields)

⸻

🧩 Example Workflow Summary
	1.	Create zones in Resources → Objects → Add → Zone
	2.	Create template in UI → with DHCP/DNS preconfigured
	3.	Create reference site → verify VLAN and WAN setup
	4.	Run pull_site.py → export site and VLAN configs
	5.	Duplicate and edit sites.csv → one row per site
	6.	Duplicate VLAN CSVs → per site or site type
	7.	Run bulk_create.py → sit back and watch automation magic
	8.	Validate in ZTB UI → confirm sites, VLANs, and VRRP applied

⸻

🏁 Example Commands Recap

# Authenticate and export token
python3 ztb_login.py && set -a && source .env && set +a

# Pull a reference site
python3 pull_site.py --site-name "Branch-Reference" --include-wans

# Create multiple new sites (dry run)
python3 bulk_create.py --dry-run

# Deploy for real
python3 bulk_create.py


⸻

🧰 Troubleshooting Tips

Symptom	Likely Cause	Fix
Missing template_id	Template not specified or typo in name	Add template_name or ID
Gateway/cluster not ready	API delay after site creation	Increase retries in bulk_create.py
VLAN ERR 400	Duplicate VLAN tag or HA VLAN conflict	Exclude HA VLANs during pull
VRRP 405 or 500	Interface mapping incomplete	Ensure HA and tracked interfaces resolved properly
VLANs not visible	Template missing zone mapping	Check UI template config


⸻

🧭 License

This project is licensed under the MIT License — feel free to modify and extend it for your own organization.
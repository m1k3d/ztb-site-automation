# 🚀 ZTB Site Automation

Automation scripts for creating and managing **Zscaler ZTB sites** via API  
(v3 for site creation, v2 for VLANs).

---

## 📂 Project Files

- **pull_site.py** — pulls site + VLAN info from ZIA API, writes JSON and CSV, updates `sites.csv`.
- **bulk_create.py** — bulk creates sites from `sites.csv` using Jinja2 templates, then pushes VLANs.
- **site_template.json.j2** — Jinja2 template for site payloads.
- **vlans/\*.csv** — VLAN definition files per site (exported by `pull_site.py` or `vlans_convert.py`).
- **vlans/\*.json** — raw VLAN JSON output (also from `pull_site.py`).
- **vlans_convert.py** — helper to convert VLAN JSON ↔ CSV (now includes `dhcp_service`).
- **.env** — holds environment variables (`ZIA_API_BASE`, `BEARER`).

---

🔑 Authentication & Environment Setup

Before running any automation scripts, you need a valid bearer token. This repo includes a helper script: ztb_login.py.

1. Configure your .env

At minimum, set the following:

ZIA_API_BASE=https://<tenant>-api.goairgap.com/api/v3
API_KEY=<your_api_key>
BEARER=""

2. Generate a Bearer Token

Run the login helper:

python3 ztb_login.py

This will:
	•	Call the ZTB API with your API key
	•	Write the BEARER="Bearer <delegate_token>" value into your .env
	•	Print an export BEARER=... line for convenience

3. Load the Environment Variables

You have two options:

Option A — Load everything from .env

set -a
source .env
set +a

This makes all variables (ZIA_API_BASE, API_KEY, BEARER, etc.) available in your shell.

Option B — Load just the BEARER token

eval "$(python3 ztb_login.py | tail -n1)"

This executes the printed export BEARER=... line from the script, updating your shell with only the new bearer token.

4. Test Your Token

Confirm it works:

curl -s -H "Authorization: $BEARER" \
  "$ZIA_API_BASE/api/v3/gateway?limit=1&refresh_tokenenabled=" | head

If you see JSON output instead of Unauthorized, your token is valid.

⸻

👉 Next time, you just run:

python3 ztb_login.py && set -a && source .env && set +a

or use the eval shortcut, and you’re good to go.


🛰️ pull_site.py

Pull site info, save VLANs, and update sites.csv.
	•	List available sites (no args):

python3 pull_site.py

	•	Pull a specific site:

python3 pull_site.py --site-name "Utrecht-Branch"

Writes:
	•	VLANs JSON → vlans/Utrecht-Branch.json
	•	VLANs CSV → vlans/Utrecht-Branch.csv
	•	Upserts row in sites.csv
	•	JSON only (skip CSV):

python3 pull_site.py --site-name "Utrecht-Branch" --json-only

	•	Include WAN VLAN in CSV (default is to exclude it):

python3 pull_site.py --site-name "Utrecht-Branch" --include-wan

CLI reference

usage: pull_site.py [--site-name NAME] [--json-only] [--include-wan]

options:
  --site-name NAME   Pull exactly this site; omit to list sites and exit.
  --json-only        Save only JSON (skip VLAN CSV & sites.csv update).
  --include-wan      Include the WAN VLAN in the generated CSV (default: off).

Notes
	•	WAN detection uses API fields (e.g., zone == “WAN Zone”). Use --include-wan if you want that row in your CSV.

⸻

🏗️ bulk_create.py

Create sites (API v3) and add VLANs (API v2).
Also flips per-VLAN flags from the CSV.
	•	Dry run (render only; no POSTs):

python3 bulk_create.py --dry-run

	•	Create + push VLANs:

python3 bulk_create.py

How it works
	1.	Reads sites.csv, selecting rows where post=1.
	2.	Renders site_template.json.j2 with that row and creates the site (v3).
	3.	Polls until both gateway_id and cluster_id are ready.
	4.	Loads VLANs from the vlans_file path in the row and POSTs each VLAN (v2).
	5.	Per VLAN:
	•	enabled=TRUE → send "status":"provisioned"
	•	enabled=FALSE → leave disabled
	•	share_over_vpn=TRUE/FALSE → PATCH the share_over_vpn flag
	•	dhcp_service (see schema below) → passed through to API

CLI reference

usage: bulk_create.py [--csv PATH] [--dry-run]

options:
  --csv PATH   Path to sites.csv (default: sites.csv)
  --dry-run    Render payloads and list VLANs but do not POST anything


⸻

📑 sites.csv

Columns used by the template and deployment:

site_name,gateway_name,city,country,wan0_ip,wan0_mask,wan0_gw,
template_name,template_id,per_site_dns,dhcp_server_ip,zia_location_name,
lan_interface_name,wan_interface_name,vlans_file,post

	•	vlans_file → path to vlans/<site>.csv
	•	post=1 → include this row in the next bulk_create.py run

⸻

🔄 VLAN CSV schema (consumed by bulk_create.py)

tag,name,display_name,subnet,default_gateway,dhcp_start,dhcp_end,dhcp_service,interface,zone,enabled,share_over_vpn

	•	enabled → maps to UI “Disable/Enable” (via "status")
	•	share_over_vpn → controls the Share-over-VPN toggle (API v2 PATCH)
	•	default_gateway → replaces start_ip field
	•	dhcp_service options:
	•	on → inherit (DHCP enabled) (airgapped)
	•	non_airgapped → DHCP on (non-airgapped)
	•	no_dhcp → DHCP disabled If left blank:
	•	If dhcp_start/dhcp_end present → defaults to inherit
	•	If no range → defaults to no_dhcp

⸻

🧰 vlans_convert.py

Convert a pulled VLAN JSON to a clean CSV (or vice-versa in your workflow).
	•	By site name (reads/writes under vlans/):

python3 vlans_convert.py --site-name "Utrecht-Branch"

	•	From a specific JSON file:

python3 vlans_convert.py --from-json vlans/Utrecht-Branch.json --to-csv vlans/Utrecht-Branch.csv

	•	Include the raw vlan_id column:

python3 vlans_convert.py --site-name "Utrecht-Branch" --include-id

CLI reference

usage: vlans_convert.py (--site-name NAME | --from-json PATH) [--to-csv PATH] [--include-id]

options:
  --site-name NAME   Convenience mode: read vlans/<site>.json, write vlans/<site>.csv
  --from-json PATH   Explicit JSON input path
  --to-csv PATH      Explicit CSV output path (default: alongside input)
  --include-id       Add a vlan_id column to the CSV

CSV columns emitted (default):

tag,name,display_name,subnet,default_gateway,dhcp_start,dhcp_end,dhcp_service,interface,zone,enabled,share_over_vpn


⸻

✅ Typical Workflow
	1.	List available sites:

python3 pull_site.py

	2.	Pull an existing site (default excludes WAN VLAN):

python3 pull_site.py --site-name "Utrecht-Branch"

(use --include-wan to keep the WAN VLAN in CSV)
	3.	Edit sites.csv:
	•	Fill WAN IP/mask/gw, DNS, etc.
	•	Set post=1 for new/changed sites.
	4.	Dry-run site creation:

python3 bulk_create.py --dry-run

	5.	Deploy site + VLANs:

python3 bulk_create.py

	6.	Verify VLANs (v2):

curl -s -H "Authorization: Bearer $BEARER" \
  "$ZIA_API_BASE/../api/v2/Network/?siteId=<site_id>&refresh_token=enabled" \
| jq -r '.result.rows[] | [.display_name,.interface,.tag,.default_gateway,.status,.share_over_vpn,.dhcp_service] | @tsv'

Replace <site_id> with the id from the Sites list.

⸻

📝 Notes & Tips
	•	🔑 Bearer expires → refresh .env and re-export env vars as needed.
	•	⏱️ Propagation → gateway/cluster readiness can take ~30–60s; bulk_create.py polls.
	•	🌐 WAN VLAN → typically auto-created; keep or exclude it from CSV to your preference.
	•	🗃️ History → JSON/CSV under vlans/ provide a change record across pulls/deploys.
	•	⚡ New: dhcp_service support and default_gateway field in CSV align closer with API behavior.

⸻

👤 Author
	•	Author: Mike Dechow (@m1k3d)
	•	Repo: github.com/m1k3d/ztb-site-automation
	•	License: MIT

---
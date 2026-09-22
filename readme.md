# ZTB Site Automation

Create Zscaler Zero Trust Branch sites from CSV files, with VLANs, private DNS, HA/VRRP, and optional ZPA App Connector provisioning. Use an existing site as a reference, prepare a batch of new branches, validate the inputs, and deploy with per-site results.

Maintained by [Mike Dechow](https://github.com/m1k3d) · [Repository](https://github.com/m1k3d/ztb-site-automation)

**Current delivery:** a Python command-line application. The browser UI and packaged desktop application are planned; they are not included in this repository yet.

[Setup](#setup) · [Deploy your first batch](#deploy-your-first-batch) · [Reports](#reports-and-exit-codes) · [Troubleshooting](#troubleshooting) · [Configuration reference](docs/configuration.md) · [Development](docs/development.md)

## What it does

- Creates standalone or HA sites using templates from your tenant.
- Provisions VLANs, private DNS, and HA VRRP; optionally creates and attaches ZPA App Connector provisioning resources.
- Validates selected CSV rows before deployment and offers an authenticated preview.
- Stops an existing site from being recreated or modified by a sequential rerun.
- Saves a short text report and structured JSON for deployment and preview runs.

The tool creates configuration. Successful API calls do **not** confirm appliance activation, interface binding, or traffic connectivity. Automatic recovery, rollback, and updates to existing sites are not implemented. See [current limitations](#current-limitations).

## Before you begin

You need:

- Python 3.11 or newer and network access to your tenant API.
- A ZTB tenant URL and an API key with access to the required operations.
- A ZTB site template matching your appliance model and standalone/HA design.
- The zones referenced by your VLAN files, already created in the tenant.
- Site and gateway names and network addressing for the new branches.

A working reference site is the easiest starting point. Optional ZPA provisioning also requires [ZPA credentials and an enrollment certificate](ZPA_PROVISIONING_README.md).

## Setup

Download or clone this repository, open a terminal in its directory, and install the dependencies in a virtual environment.

**macOS / Linux**

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
python3 bulk_create.py --csv examples/sites.csv --validate-only
```

**Windows PowerShell**

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe bulk_create.py --csv examples/sites.csv --validate-only
```

For the remaining commands, Windows users should replace `python3` with `.\.venv\Scripts\python.exe`.

The example check runs offline, needs no credentials, and should print:

```text
Validated 1 selected site(s), 1 VLAN(s).
```

The example contains placeholder template, interface, zone, and address values. It is for validation practice; adapt it to your tenant before deployment.

Create a local `.env` by copying `.env.example` if you do not already have one. Set:

```dotenv
ZTB_API_BASE="https://<your-tenant>-api.goairgap.com/api/v3"
API_KEY="<your-api-key>"
BEARER="AUTO_POPULATED"
```

The scripts obtain and refresh the bearer token automatically. You do not need to run a separate login command. Existing environment variables take precedence over `.env`; use `--env-file path/to/tenant.env` to select another credential file. Keep credentials and real tenant exports out of version control.

## Deploy your first batch

### 1. Export a reference site

List the available sites, then export the one you want to copy:

```bash
python3 pull_site.py
python3 pull_site.py --site-name "Branch-Reference"
```

The export updates `sites.csv` and writes `vlans/Branch-Reference.csv` and `.json` beside the scripts. It sets the exported site to `post=0`, so the reference is not selected for deployment. Pulling the same site again refreshes local exports; it does not change the tenant.

### 2. Prepare the new branches

In `sites.csv`:

1. Duplicate the reference row for each new branch; leave the original at `post=0`.
2. Assign unique site and gateway names.
3. Review the template, WAN settings, DNS, country, and ZIA location choice. For a separate ZIA location, use `location_type=new` and a new `zia_location_name`.
4. Copy the VLAN CSV for each branch that needs different networks, adjust its addressing, and update `vlans_file`.
5. Set `post=1` only on the rows you intend to create.

Relative `vlans_file` paths are resolved from the directory containing the site CSV. Exported data needs review: location defaults and older interface/DHCP settings are not guaranteed to be suitable for a new deployment.

For WAN DHCP, leave the WAN address, mask, and gateway blank. For static WAN addressing, fill all three. See the [site and VLAN field reference](docs/configuration.md) for HA, location choices, DHCP modes, and optional settings.

### 3. Validate and preview

```bash
python3 bulk_create.py --csv sites.csv --validate-only
python3 bulk_create.py --csv sites.csv --dry-run
```

| Mode | What happens |
| --- | --- |
| `--validate-only` | Checks selected site/VLAN inputs locally. No authentication or network requests. |
| `--dry-run` | Resolves tenant references, checks for existing sites, and performs optional ZPA preflight. May refresh tokens, but creates no deployment resources. |
| No mode flag | Performs the checks, then deploys the selected new sites. |

Fix validation and preview errors before proceeding. Preview does not prove that every API call or interface binding will succeed; device readiness and some interface checks are only evaluated during deployment.

Input validation and template/location resolution cover the whole selected batch before deployment writes. A preflight error blocks the batch. Once execution begins, a failure or existing-site stop affects that site; later sites can still proceed.

### 4. Deploy and review

```bash
python3 bulk_create.py --csv sites.csv
```

Read the per-site results and run report, then verify configuration in the Zscaler console. After appliance activation, verify interface bindings and connectivity separately. Set completed rows back to `post=0` to keep later batches focused on new work.

## Reports and exit codes

Runs that reach the deployment engine save reports under **`out/runs/`**, relative to your working directory:

```text
out/runs/
  <UTC-timestamp>-<run-id>.txt
  <UTC-timestamp>-<run-id>.json
```

Read the `.txt` for the outcome and next action. Use the `.json` for site statuses and stage results. Reports exclude credentials, request payloads, and raw API responses; detailed error messages remain in the console output.

To choose another location:

```bash
python3 bulk_create.py --csv sites.csv --report-dir out/customer-rollout
```

| Site status | Meaning / next action |
| --- | --- |
| `success` | Requested API stages completed; verify the appliance separately. |
| `preview` | Preview completed without deploying resources. |
| `already_exists` | The site was left unchanged. A rerun does not repair it. |
| `lookup_failed` | Site existence could not be established; no changes were made to that site. |
| `partial` | The site was created, but configuration is incomplete. Inspect failed stages before repair. |
| `failed` | Creation failed or its outcome is uncertain. Inspect the tenant before retrying. |

Exit code **0** means no reported failure, including a no-op with no selected rows. **1** includes validation errors, existing-site stops, lookup failures, and incomplete deployments. **130** means the run was interrupted.

Validation-only runs, unselected batches, and failures before the engine starts do not create reports. An interrupted or unexpectedly terminated run can leave a JSON report marked `started`; this is not evidence that no changes occurred.

## Current limitations

- **Recovery:** no automatic resume or rollback. Successful stages remain after a later failure. An existing-site stop prevents full recreation but does not complete missing stages.
- **Inventory size:** duplicate detection does not yet paginate. If the first page of up to 100 records cannot establish absence, creation is blocked. Reference listing/export is also limited to its first inventory page.
- **Concurrent runs:** duplicate checks protect sequential reruns, not two simultaneous creators. Coordinate deployments to the same tenant.
- **Coverage:** standalone creation, private DNS, VLAN provisioning, and existing-site stops have live test coverage. HA and ZPA have offline coverage but still need broader live validation. IPv6 is not supported.

## Troubleshooting

| Message or symptom | Action |
| --- | --- |
| Nothing selected | Set `post=1` on the intended new-site rows. |
| Validation error with file, row, and field | Correct that field and rerun `--validate-only`. DHCP ranges must exclude the gateway address. |
| Template or location name cannot be resolved | List tenant templates/locations and check the spelling or explicit ID override. |
| Already exists | Inspect the existing site. Do not rename it merely to bypass duplicate protection. |
| Existence lookup failed | Check credentials, tenant access, and inventory size. No creation was attempted for that site. |
| Loopback subnet or interface validation fails | Use `/32`, disable DHCP, and verify that each target gateway exposes `lo0`. See the [configuration requirements](docs/configuration.md#loopback-management-lo0). |
| Partial result, timeout, or interrupted run | Read the report and inspect the tenant before another deployment. Completed resources are not rolled back. |

Use `--debug` for HTTP request/status diagnostics. Review console output before sharing it; reports intentionally omit raw API responses.

## Command reference

```bash
python3 bulk_create.py --help
python3 pull_site.py --help
python3 pull_site.py --list-templates
python3 pull_site.py --list-locations
python3 -m unittest discover -s tests -v
```

Further documentation: [Configuration and export reference](docs/configuration.md) · [Optional ZPA provisioning](ZPA_PROVISIONING_README.md) · [Architecture and offline checks](docs/development.md)

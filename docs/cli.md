# Python and CSV workflow

Use this if you prefer terminal commands. For the browser workspace, start with the [Docker setup](../readme.md#docker-quick-start).

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

For WAN DHCP, leave the WAN address, mask, and gateway blank. For static WAN addressing, fill all three. See the [site and VLAN field reference](configuration.md) for HA, location choices, DHCP modes, and optional settings.

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


[Return to the README](../readme.md)

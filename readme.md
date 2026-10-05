# Zero Trust Branch Site Automation

Prepare and deploy new Zscaler Zero Trust Branch sites from a local browser workspace. Start from a working site or a CSV template, build your branch rollout, and review the changes before deploying.

Maintained by [Mike Dechow](https://github.com/m1k3d). Standalone deployment is available in the UI. HA configurations can be prepared and exported, but HA deployment from the UI is not enabled yet.

[Start with Docker](#docker-quick-start) · [Use the workspace](#your-first-rollout) · [Import CSVs](#importing-sites-and-vlans) · [Save your work](#saving-and-returning-later) · [Troubleshooting](#troubleshooting)

## Before you start

Have these ready:

- Docker Desktop running on Windows or macOS, or Docker Engine with Compose on Linux. See [Docker requirements](docs/docker.md).
- Your tenant API URL and a ZTB API key with permission to create the required resources.
- An existing site template and the zones your networks will use.
- Names, WAN settings, and VLAN addressing for the new branches.

A working reference site is the easiest starting point. You can prepare drafts without connecting to a tenant. ZPA provisioning needs [separate ZPA settings](ZPA_PROVISIONING_README.md).

## Docker quick start

Clone this repository and start the app:

```bash
git clone https://github.com/m1k3d/ztb-site-automation.git
cd ztb-site-automation
docker compose up --build -d
```

If you downloaded the repository as a ZIP, extract it and run the last command from that folder.

Open **[http://localhost:8765](http://localhost:8765)**. The first build takes longer because Docker downloads the base image and Python dependencies. No Python or Node.js installation is needed on your computer.

This package builds the Docker image locally. It does not require a registry account or a prebuilt image. Startup opens the workspace; it does not create anything in your tenant.

If port 8765 is in use, follow the [alternate-port instructions](docs/docker.md#alternate-port). Keep the app bound to localhost; this package is for one operator on one computer.

## Your first rollout

The left sidebar follows the rollout in three steps. **How to use**, below the navigation, explains the controls without leaving the app.

### 1. Reference site

Name your **Rollout project** at the top of the page.

Choose **Pull reference site**, enter your tenant connection details, and select a working site to copy. Review the imported settings before using **Create branches**. Pulling a reference reads its configuration; it does not change that site.

To reuse credentials without retyping them, choose **Download example .env**, populate it, and save it as `customer-a.env`. Then use **Load credentials from file**. The file stays visible because its name does not start with a dot. Credentials are held in app memory only; reload the file after restarting the app. Keep your original file private—it contains plaintext keys. See [connection file instructions](docs/browser-workspace.md).

You can also choose **Import CSVs** or **Add blank branch**. The example rollout is practice data: replace its names, template, interfaces, and addresses before deployment.

### 2. New branches

This page shows the rollout overview. Click a branch name to open its **Site details** and **VLANs** tabs. Check:

- Site and gateway names, country, and template.
- WAN interface and DHCP or static addressing.
- VLAN interfaces, tags, gateways, DHCP ranges, and DNS.
- ZIA location choice and any optional ZPA, private-domain DNS, or UCaaS settings.

Use **Rollout overview** to return to the list. Select one branch for a pilot or several for a batch. **Group by country** uses each branch's Country field; it only groups the list and does not change deployment order.

**Ready** means the local input checks passed. Tenant checks happen in the next step. **Completed** is a recorded result for this project and connected tenant, not a live appliance health check. A site created in another project or tool may have no recorded result here; the tenant existence check still runs before creation.

### 3. Review rollout

Choose **Review selected branches**. Check the selected names, networks, and optional resources. If you have not connected yet, use **Connections** on this page.

Click **Preview deployment**. This checks the destination tenant and required resources without creating them. Resolve any errors, then review the destination and approve **Deploy N sites**.

Branches run one at a time. Keep the app running until the batch finishes. Changes to the draft or connection, or a preview older than five minutes, require another preview.

Read the per-site results and download the report. Deployment creates configuration; appliance activation, connector registration, interface binding, and traffic connectivity still need to be checked in the Zscaler console. Saved deployment diagrams are available as PNG, SVG, and editable Visio files. See the [diagram guide](docs/site-diagrams.md).

## Importing sites and VLANs

Use **CSV templates**, next to **Import CSVs**, to download a starter ZIP:

| Kit | Contents |
| --- | --- |
| Standalone | Two example branches, each with a gateway and a matching VLAN file |
| High availability (HA) | One branch with Gateway A/B, both WANs, an HA link, shared LAN, and separate management networks; preparation and CSV export only |

Each kit includes a guide explaining its columns. Extract the ZIP before importing.

1. Edit `sites.csv` and the files in its `vlans` folder. Keep the headers and save as CSV, not XLSX.
2. Choose **Import CSVs**, then **Choose CSV files**, and select `sites.csv`.
3. The tool reads each row's `vlans_file` value and asks you to select the matching VLAN CSVs. For example, `vlans/branch-01.csv` means you select `branch-01.csv` from the extracted folder.
4. Review each imported branch and its **VLANs** tab. Examples start unselected.

You can select the site CSV and all matching VLAN CSVs together if they are in one folder. A blank `vlans_file` imports that site without VLANs. File matching uses the filename, so give each branch's VLAN file a unique name.

Import replaces the current draft after confirmation. Use **New project** or **Save as copy** first if you want to keep the current rollout separately. Importing does not deploy anything.

Keep credentials out of CSVs. Imports reject credential fields, and exports remove them from older drafts. CSV export also blocks values or headers that begin like spreadsheet formulas; use plain text for configuration names. **Download CSV bundle** on Review exports a portable copy for reuse or the [Python CLI](docs/cli.md).

## Saving and returning later

Edits save automatically. Wait for **Saved on this computer** before closing the tab.

- **New project** starts an empty rollout.
- **Open project** returns to a saved rollout.
- **Save as copy** keeps a separate version.

With Docker, projects, reports, diagrams, and refreshed catalogs live in the `workspace-data` Docker volume, not in the downloaded repository. They survive normal container rebuilds. Credentials entered in the UI and deployment approvals are not saved; reconnect and preview again after restarting.

To stop and restart:

```bash
docker compose stop
docker compose start
```

Wait for deployments to finish before stopping. **Do not use `docker compose down --volumes` for routine updates**; it deletes the saved workspace.

For updates, [back up the workspace](docs/docker.md#stop-back-up-and-upgrade), obtain the new source, then run:

```bash
git pull --ff-only
docker compose up --build -d
```

Use the same repository folder, Compose project name, and port setting so Docker reuses your existing volume.

## Existing sites and incomplete runs

The tool is for creating new sites. A matching existing site name blocks creation; a rerun does not overwrite or repair that site. If the inventory cannot be read completely, creation is blocked.

A failed or interrupted run can leave resources behind. Read the report and inspect the tenant before retrying. There is no automatic rollback or resume. Do not rename a branch just to get around an existing-site check.

Coordinate work with other operators: separate app instances and CLI processes do not share a deployment lock.

## Troubleshooting

| What you see | What to do |
| --- | --- |
| The page will not open | Check Docker is running. Run `docker compose ps` and `docker compose logs --tail 100 workspace`. |
| You return to page 2 | Saved projects reopen their last view. New projects start at Reference site. |
| VLAN files are requested after importing sites | Select the CSV files named in the `vlans_file` column. They are in the starter ZIP's `vlans` folder. |
| Needs changes | Open the branch and correct the listed fields. |
| Tenant connection fails | Check the API URL, key permissions, and whether your VPN/firewall allows Docker to reach the tenant. |
| Existing site | Inspect that site in the tenant. This workflow creates new sites; it does not update existing ones. |
| Partial, failed, or interrupted result | Download the report and inspect any resources already created before recovering the run. |
| Loopback binding unverified | Check the management interface after appliance activation. See [management loopbacks](docs/configuration.md#loopback-management-lo0). |
| Credential fields rejected | Remove credential columns from the CSV. Enter tenant credentials in the connection dialog. |
| CSV export blocked for formula characters | Change the affected configuration text or column name so it does not begin with `=`, `+`, `-`, or `@`. |

## Current limits

- HA deployment from the browser is blocked while WAN and management mapping are being verified.
- A project supports up to 500 sites and 10,000 VLANs. Deployment checks read a paginated inventory of up to 10,000 tenant sites; an incomplete or inconsistent result blocks creation. The reference-site picker currently lists the first 100 sites.
- IPv6 configuration is not supported.
- Management `lo0` binding on pending appliances and live traffic behavior need separate verification after activation.
- Remote browser access, multiple operators sharing one workspace, automatic rollback, and automatic recovery are not supported.

The release review fixed the CSV export findings. The container still has upstream advisories awaiting packaged fixes; see the dated [security scan status](docs/docker.md#security-scan-status).

## More documentation

- [Browser fields and behavior](docs/browser-workspace.md)
- [Docker ports, credentials, backups, and upgrades](docs/docker.md)
- [Python and CSV commands](docs/cli.md)
- [Site and VLAN field reference](docs/configuration.md)
- [ZPA provisioning](ZPA_PROVISIONING_README.md)
- [Private-domain DNS](docs/private-domain-dns.md) and [UCaaS local breakout](docs/ucaas-local-breakout.md)
- [Development and tests](docs/development.md)

## License

Source code is licensed under the [MIT License](LICENSE). Bundled fonts have their [own license](data/fonts/LICENSE.txt). Zscaler names, logos, and appliance images belong to Zscaler; see [asset sources](data/appliances/sources.json).

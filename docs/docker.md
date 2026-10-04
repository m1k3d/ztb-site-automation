# Docker workspace

The Docker package runs the existing Python backend and browser interface as one local, single-operator application. It does not deploy or activate any tenant resources on startup. Docker Engine/Desktop 28 or newer and Docker Compose v2 or newer are required. Docker Desktop must be running before using the commands below.

## Start

From the repository directory:

```bash
docker compose up --build -d
docker compose ps
```

Open **http://localhost:8765**. Wait for the service to become healthy. The initial build downloads the Python image and pinned runtime dependencies. Enter credentials in **Connections** / **Pull reference site**. The Docker image does not contain the developer's credentials, projects, tenant exports, or run reports.

The image uses Python 3.13 on Debian 13 (trixie) and supports building on Linux AMD64 and ARM64 (including Apple Silicon through Docker Desktop). On 2026-10-04, startup, worker, persistence, request-boundary, CSV safety, and shutdown checks passed on ARM64 and on AMD64 through Docker Desktop emulation. This does not constitute a separate Windows or native Linux host qualification. Node.js is not needed to run the tool. The native Python/CSV workflow remains available outside Docker.

The Compose configuration publishes only to `127.0.0.1`, runs as UID/GID `10001:10001`, and keeps the application filesystem read-only. The application retains its Host, Origin, and local-token checks. Keep this configuration local: remote browser access and multi-user sessions are not supported by this package.

## Alternate port

If the native app already uses port 8765, select another port before running Compose. The same port is used inside and outside the container so request validation remains consistent.

macOS/Linux:

```bash
export ZTB_PORT=8766
docker compose up --build -d
```

Windows PowerShell:

```powershell
$env:ZTB_PORT = "8766"
docker compose up --build -d
```

Open `http://localhost:8766`. Keep the same environment setting for subsequent Compose commands. You can also add `ZTB_PORT=8766` to the local, ignored `.env` file.

## Saved data

The named `workspace-data` volume is mounted at `/data`:

| Path | Contents |
| --- | --- |
| `/data/projects/workspace.sqlite3` | Saved rollout drafts and editor preferences |
| `/data/runs/` | Deployment reports |
| `/data/runs/diagrams/` | Saved deployment diagram snapshots and exports |
| `/data/catalog/ucaas_endpoints.json` | Last successful explicit UCaaS refresh |

Before the first refresh, the tool reads the bundled UCaaS snapshot. A successful refresh writes to the volume; updates survive container replacement. Invalid refreshed data does not replace the last good catalog. The normal freshness checks still apply.

Projects survive `docker compose stop`, `start`, `down`, and container rebuilds as long as the named volume remains. **Do not use `docker compose down --volumes` for routine updates:** that deletes the workspace volume. Use one running workspace per volume and coordinate deployments to the same tenant.

Credentials, live connections, deployment approvals, and in-progress jobs do not survive restart. Reconnect and preview again. An interrupted deployment is not automatically resumed; inspect its report and tenant resources before recovery.

The Docker volume is separate from the native app's `out/projects/` and `out/runs/`. Existing native drafts are not moved automatically. CSV export/import transfers branch configuration, but not all editor-only draft preferences.

## Optional local credentials

Entering credentials in the UI keeps them in server memory for that session. If you prefer **Use existing local settings**, create a local `.env` from `.env.example`, then create `compose.credentials.local.yaml`:

```yaml
services:
  workspace:
    env_file:
      - .env
```

Launch with both files:

```bash
docker compose -f compose.yaml -f compose.credentials.local.yaml up --build -d
```

These credentials are supplied to the running container as environment variables, never copied into the image. Both local files are excluded from version control. Use the same `-f` options for later commands. Without this override, Compose does not pass tenant credentials from `.env` into the container.

## Stop, back up, and upgrade

Wait for active deployments to finish before stopping the workspace. Normal container stop sends SIGINT and allows up to ten minutes for the active worker to finish its batch and save its report. The server keeps draining progress while it waits; repeated SIGINT/SIGHUP signals do not abandon the worker. A forced stop or elapsed grace period can still interrupt a deployment; shutdown is not a resume mechanism.

For a backup using a macOS/Linux shell, choose a new archive name and run:

```bash
mkdir -p backups
docker compose stop
docker compose run --rm --no-deps -T --entrypoint tar workspace -C /data -czf - . > backups/workspace-backup.tar.gz
docker compose start
```

The stopped application keeps the SQLite backup consistent. Store backups as sensitive deployment configuration; credentials entered only in the UI are not included.

To restore into a separate, fresh workspace volume (macOS/Linux shell), keep the original app stopped and run:

```bash
docker compose -p ztb-restored run --rm --no-deps -T --entrypoint tar workspace -C /data -xzf - < backups/workspace-backup.tar.gz
docker compose -p ztb-restored up -d
```

Use the `ztb-restored` project name for later commands on that restored workspace. Restoration must target an empty volume; do not merge unrelated databases. On Windows, use WSL for these archive commands to avoid older PowerShell versions changing binary output.

After saving a backup and obtaining updated source, rebuild and recreate:

```bash
docker compose up --build -d
```

Do not switch Compose project names during a routine upgrade, because the project name identifies the volume. The Python base tag follows the 3.13 patch series on Debian trixie; dependency versions are in `requirements.lock`. The build installs available Debian updates and removes pip/ensurepip from the runtime image. To refresh cached base and OS layers, use `docker compose build --pull --no-cache` before recreating the container. Releases should record the built image digest. Never update during an active deployment.

## Verification and troubleshooting

```bash
docker compose ps
docker compose logs --tail 100 workspace
```

`healthy` confirms local HTTP responsiveness, not tenant API access or deployment success. A connection error in the UI may require allowing Docker's network traffic through your VPN, proxy, or firewall. Keep TLS verification enabled; use your organization's supported trust configuration when required.

Run the isolated package test with host Python (no host Python packages required):

```bash
python3 tools/check_container.py
```

It builds the image, uses temporary containers and volumes, verifies local request protection, non-root/read-only operation, background-worker startup, persisted drafts/reports/catalogs, token rotation, graceful shutdown both idle and during an active mocked deployment, and archive backup/restore. It removes only its own temporary resources. It never connects to a tenant. Use `--skip-build` to check an already-built image, or `--platform linux/amd64 --image ztb-site-automation:check-amd64` to qualify another architecture.

If Docker Desktop was just installed and `docker` is not found, enable its CLI tools or open a new terminal. The verification script can also find Docker Desktop at its standard macOS installation path.

[Return to the README](../readme.md)

## Security scan status

Checked 4 October 2026. Both CSV export issues found during review are fixed: credential fields are filtered, and formula-like cells block export. The eight pinned Python runtime dependencies have no known vulnerabilities reported by pip-audit at this check.

Docker Scout examined the ARM64 runtime image, including OS packages and bundled libraries. Moving to the updated trixie base and removing unused Python installers reduced the findings from 75 to 30: **0 critical, 3 high, 2 medium, and 25 low**. None of the remaining findings had a fixed package version available in that scan. A passing dependency audit is not a clean container scan.

The three high findings are upstream advisories against GCC runtime libraries and zlib:

- [CVE-2026-95619](https://security-tracker.debian.org/tracker/CVE-2026-95619): libstdc++ aligned allocation.
- [CVE-2026-102010](https://security-tracker.debian.org/tracker/CVE-2026-102010): libstdc++ priority-queue erase behavior.
- [CVE-2026-85091](https://security-tracker.debian.org/tracker/CVE-2026-85091): zlib non-blocking gzip writes. The tracker also discusses uncertainty in the affected version range.

These remain tracked findings; this review did not prove exploitation or rule out reachability through native libraries. Use the localhost-only configuration, rebuild when upstream fixes become available, and rescan images before distributing them. AMD64 passed the functional container checks; its vulnerability inventory was not separately scanned.

GitHub checks run tests, the Python dependency audit, secret detection, and the Docker smoke test. Full container CVE scanning remains a release check, not part of that workflow.

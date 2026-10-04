# Development

The CLI and local browser editor share input validation. The workspace connects to tenants, prepares inline inputs, and uses the shared engine for authenticated preview and standalone deployment. CSV export remains available. Docker Compose packaging runs this same workspace locally; HA UI deployment and a native desktop installer are not ready.

## Modules and integration

- `Dockerfile`, `compose.yaml`, `.dockerignore`: a non-root Python 3.13 image, localhost-only published port, read-only application filesystem, and persistent `/data` volume. The build context uses an explicit runtime-file allowlist; add new runtime modules/assets to it. `requirements.lock` pins direct and transitive Python dependencies. `docker/healthcheck.py` checks local `/healthz` only. SIGINT preserves the existing graceful worker shutdown path; the Compose stop grace period is ten minutes.
- `runtime_paths.py`: `ZTB_DATA_DIR` selects the persistent state root for projects, reports, and refreshed UCaaS catalogs, including spawned workers. Without it, existing native paths are preserved. `--data-dir` configures the same environment for worker inheritance; `--project-dir` remains a projects-only override. `ZTB_PORT` or `--port` selects the HTTP port; `--bind 0.0.0.0` is used only inside the container and does not relax Host/Origin/token validation.
- `bulk_create.py`: CLI arguments, local validation, report generation, and exit status.
- `app.py`, `ui/`: local HTTP server and browser batch editor. Uploads are parsed in memory, validation uses inline VLANs, and exports contain a sites CSV plus referenced VLAN CSVs. The server binds to loopback and enforces Host, Origin, and per-process token checks; tenant credentials and refreshed tokens remain on the server. Tenant operations occur only through explicit connection, reference-pull, preview, and confirmed deployment actions.
- `site_reference.py`: session-scoped reference reader using the shared ZTB client and pure export mapping from `pull_site.py`. Also holds a separate optional ZPA connection, checked through `/api/zpa/connect` using shared `prepare_zpa()` with token persistence and helper logging disabled. Browser responses contain selected configuration fields and connection status, never raw API objects or credentials. ZPA check failures clear the previous ZPA context and preserve the ZTB reference connection. These session credentials are not handed to the separate CLI process.
- `ui/interface-picker.js`: template interface cache, role/gateway filtering, single-port WAN selects, and multi-port VLAN controls. `/api/tenant/interfaces` resolves a tenant template by name or explicit ID, paginates its interfaces, and returns only model, gateway-slot, port, role, and bond-member metadata. ZTB reconnection clears server caches; browser caches discard stale responses after connection changes. Explicit refresh reloads metadata. `lo0` is offered as the tool's supported management loopback because template inventory omits implicit interfaces. Incompatible current assignments remain visible for review; this is editor guidance, not a new deployment validation gate.
- `ui_deployment.py`: one worker process per operation, private credential snapshots, five-minute approvals bound to the normalized rollout and connection revision, and fresh preflight before creation. Duplicate start requests return the existing job. Only sanitized statuses and summaries cross the process boundary; legacy stdout/stderr are discarded. Refresh restores run status while the server lives; reports persist on disk, but restart does not resume jobs.
- `run_report.py`: concise text summaries and structured JSON results.
- `site_diagrams.py`, `diagram_render.py`, `diagram_store.py`: allowlisted topology snapshots, bounded GET-only post-deployment read-back, a shared vector scene for SVG/native VSDX, and atomic project-scoped artifacts under the run directory. Downloads and rendering retries never call the tenant. `ui/diagrams.js` provides optional documentation labels, planned previews, per-site actions, and saved-run downloads. New HTTP APIs `/api/diagrams/preview`, `/list`, `/download`, and `/regenerate` retain the existing local token boundary; stored downloads additionally require the owning project ID. `SiteResult` now carries site/gateway identity, snapshot data, and artifact references. Only references and fixed warnings enter the short report.
- `project_store.py`, `ui/projects.js`: durable local drafts in SQLite, atomic saves with optimistic revision checks, and safe replay after a lost response. `/api/projects/list`, `/load`, `/create`, and `/save` share the local Host/Origin/token boundary. Draft validation checks structure and limits while accepting incomplete configurations. Only workspace data and editor choices are saved; connection state, credentials, lookup caches, and deployment approval are excluded. The browser serializes saves and preserves edits made during an in-flight request. Conflicts retain the local draft for Save as copy. Project operations never invoke deployment. `--project-dir` supports isolated test storage.
- `input_validation.py`: CSV/inline-row validation and normalized VLAN snapshots.
- `automation_config.py`, `api_client.py`: explicit configuration and shared ZTB authentication/session.
- `template_cloning.py`: native `POST /api/v3/templates/{id}/clone` with `{name}` through the existing ZTB client. The contract was verified from the tenant's `/swagger.json` on 2026-09-26; the SDK is not required. The documented response has no resource ID, so the engine resolves a unique new name and checks its settings before deploying. Complete paginated inventories guard name collisions. Source settings, interfaces, VLANs, access policies, and PBR policies are fingerprinted during preview and rechecked before cloning. Tokens and site-use counts are excluded. Clone requests are never automatically repeated after uncertain outcomes; reports retain the new ID if site creation fails.
- `deployment_engine.py`: plan, execute, and structured per-site results; no import-time I/O.
- `site_payload.py`: JSON-compatible payload construction; names are not interpolated into JSON text. Payload customizations belong here with tests.
- `location_config.py`: location mode rules.
- `pull_site.py`: reference export and listing using the same configuration/client.
- `ztb_login.py`, `zpa_login.py`, `zpa_provisioning.py`: authentication and optional ZPA operations.
- `zpa_segments.py`: selected LAN subnet planning, paginated conflict checks, and verified creation of disabled ZPA segments linked to the new App Connector group.

The editor supplies site dictionaries with an inline `vlans` list to `validate_rows()`. The deployment screen uses `DeploymentEngine.plan()` and `execute()` in an isolated worker, with structured stage progress callbacks. No CSV is required by the engine. Use the same engine for planning/execution and re-plan after editing inputs or changing tenants. Plans contain runtime credentials for optional ZPA and should not be serialized or persisted. `BatchResult` exposes `sites`, `issues`, and `exit_code`; each site has status, stage results, and errors. Engine progress accepts an `emit` callback; legacy ZPA helpers also print details.

The browser uses `ui/network.js` for immediate IPv4 DHCP range calculations and pure address-prefix changes. The optional Change addressing dialog prepares a local preview, detects subnet collisions, and applies only selected VLAN edits; it introduces no CSV schema or deployment-engine changes. Automatic/custom mode is editor state and follows branch copies; CSV exports contain explicit ranges. The shared Python validator still checks exported range endpoints. Node.js is needed only to run the calculator tests, not to launch the application.

## Execution order

The CLI validates all selected rows and snapshots their VLAN inputs. The engine resolves template/location references and optional ZPA authentication and certificate data before deployment writes. A failed plan blocks deployment. Each prepared site then receives an existence check before creation, followed by gateway/cluster discovery, private DNS, VLANs, HA VRRP, and optional ZPA. Stage failures are recorded; later sites can continue. Plans do not reread VLAN files during execution.

The CLI reserves a report before running the engine and writes the final summary after it returns. Unexpected termination can leave only a `started` record; reports are not a durable stage-by-stage recovery journal. Calling the engine directly does not automatically persist reports.

LAN staging adds a `ZPA segments` stage after successful VLAN and ZPA provisioning. The exact App Connector group ID is passed from creation; no name lookup or fallback group is used. `SiteResult.zpa_segments` contains only planned settings and resource metadata, never a provisioning key or bearer token. ZPA inventory failures/conflicts block preflight; staging rechecks conflicts and reads back new objects before reporting success. POSTs are never automatically retried. A corrective PUT is allowed only to the just-created application if its disabled state was not applied.

## Offline checks

```bash
python3 -m unittest discover -s tests -v
node --test tests/*.cjs
python3 -m compileall -q bulk_create.py deployment_engine.py input_validation.py automation_config.py api_client.py site_payload.py pull_site.py ztb_login.py zpa_login.py zpa_provisioning.py zpa_segments.py run_report.py
python3 bulk_create.py --csv examples/sites.csv --validate-only
```

Tests block unmocked HTTP and cover cross-site targeting, false success, all-row preflight gates, malformed inputs, CSV preservation, credential precedence, one-time 401 retry, side-effect-free imports/help, JSON escaping, sequential duplicate prevention, failed inventory checks, loopback validation, ZPA group/key certificate consistency, and report output. No deployment is needed to run them.

For future changes, check: quickstart matches CLI; validation precedes writes; site targets are isolated; requested-stage failures affect exit status; rerun limits are explicit; credentials are not logged; existing payload contracts and offline regressions still pass.

## Container qualification

With Docker running, `python3 tools/check_container.py` builds the local image and runs the isolated startup, persistence, request-boundary, worker, and shutdown checks. It does not use the developer's live workspace or credentials. Keep architecture qualification distinct: a successful ARM64 run does not verify AMD64. The current package builds from source; publishing a registry image is a separate release step. See [Docker operations](docker.md).

[Return to the setup guide](../readme.md)

## GitHub checks

Pushes to main and pull requests run Python and JavaScript tests, offline example validation, a dependency audit, secret detection, and an isolated Docker smoke test. Workflow permissions are read-only and third-party Actions are pinned to commit IDs. The workflow builds the Docker package; it does not publish an image to a registry or connect to a tenant.

The reviewed `.secrets.baseline` contains hashes for placeholder credentials, synthetic tests, UI labels, and public catalog checksums. Secret detection runs without contacting credential providers. Review every new finding before updating this baseline; do not automatically accept scan results.

CSV import rejects credential fields; exports filter credential fields from legacy drafts. Browser and CLI CSV exports reject formula-like headers and cells rather than changing their values. Run `tests/test_credential_fields.py` and `tests/test_csv_safety.py` through unittest discovery when changing these boundaries. These checks do not detect a secret pasted into an ordinary name or notes field.

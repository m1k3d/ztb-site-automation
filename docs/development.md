# Development

The CLI and a future UI should use the same validation and deployment engine. The UI and desktop packaging are not implemented.

## Modules and integration

- `bulk_create.py`: CLI arguments, local validation, report generation, and exit status.
- `run_report.py`: concise text summaries and structured JSON results.
- `input_validation.py`: CSV/inline-row validation and normalized VLAN snapshots.
- `automation_config.py`, `api_client.py`: explicit configuration and shared ZTB authentication/session.
- `deployment_engine.py`: plan, execute, and structured per-site results; no import-time I/O.
- `site_payload.py`: JSON-compatible payload construction; names are not interpolated into JSON text. Payload customizations belong here with tests.
- `location_config.py`: location mode rules.
- `pull_site.py`: reference export and listing using the same configuration/client.
- `ztb_login.py`, `zpa_login.py`, `zpa_provisioning.py`: authentication and optional ZPA operations.

A future UI can supply site dictionaries with an inline `vlans` list to `validate_rows()`, then use `DeploymentEngine.plan()` and `execute()` (or `run()`). No CSV is required by the engine. Use the same engine for planning/execution and re-plan after editing inputs or changing tenants. Plans contain runtime credentials for optional ZPA and should not be serialized or persisted. `BatchResult` exposes `sites`, `issues`, and `exit_code`; each site has status, stage results, and errors. Engine progress accepts an `emit` callback; legacy ZPA helpers also print details.

## Execution order

The CLI validates all selected rows and snapshots their VLAN inputs. The engine resolves template/location references and optional ZPA authentication and certificate data before deployment writes. A failed plan blocks deployment. Each prepared site then receives an existence check before creation, followed by gateway/cluster discovery, private DNS, VLANs, HA VRRP, and optional ZPA. Stage failures are recorded; later sites can continue. Plans do not reread VLAN files during execution.

The CLI reserves a report before running the engine and writes the final summary after it returns. Unexpected termination can leave only a `started` record; reports are not a durable stage-by-stage recovery journal. Calling the engine directly does not automatically persist reports.

## Offline checks

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q bulk_create.py deployment_engine.py input_validation.py automation_config.py api_client.py site_payload.py pull_site.py ztb_login.py zpa_login.py zpa_provisioning.py run_report.py
python3 bulk_create.py --csv examples/sites.csv --validate-only
```

Tests block unmocked HTTP and cover cross-site targeting, false success, all-row preflight gates, malformed inputs, CSV preservation, credential precedence, one-time 401 retry, side-effect-free imports/help, JSON escaping, sequential duplicate prevention, failed inventory checks, loopback validation, ZPA group/key certificate consistency, and report output. No deployment is needed to run them.

For future changes, check: quickstart matches CLI; validation precedes writes; site targets are isolated; requested-stage failures affect exit status; rerun limits are explicit; credentials are not logged; existing payload contracts and offline regressions still pass.

[Return to the setup guide](../readme.md)

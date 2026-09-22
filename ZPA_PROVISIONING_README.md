# Optional ZPA App Connector provisioning

Use the main deployment workflow to create ZPA App Connector resources for selected new ZTB sites. Start with the [setup guide](readme.md); this integration is optional and is disabled per site unless `appc_provision` is enabled.

## What the integration does

For each selected new site with `appc_provision=1`, the deployment engine:

1. Creates an App Connector Group named after the site.
2. Creates a provisioning key with the site name, that group, the configured enrollment certificate, and maximum usage of 2.
3. Attaches the provisioning key to the ZTB cluster.

Creating these resources does not independently verify that an App Connector has registered or can pass traffic. Existing ZTB sites are blocked by the main duplicate check; this is not a command for adding ZPA to an existing site.

## Prerequisites and credentials

You need ZPA API credentials with access to enrollment certificates, App Connector Groups, and provisioning keys. The named enrollment certificate must already exist.

Add the following to the same credential file used for ZTB:

```dotenv
ZPA_ENABLED=true
ZPA_CLIENT_ID="<client-id>"
ZPA_CLIENT_SECRET="<client-secret>"
ZPA_CUSTOMER_ID="<customer-id>"
ZPA_ENROLLMENT_CERT_NAME="Connector"
ZPA_BASE_URL="https://config.private.zscaler.com"
```

Use the base URL for your ZPA cloud. The example is not a universal endpoint. `ZPA_CUSTOMER_ID` may be omitted if the authentication token supplies `custId`; an explicitly configured customer ID is checked against the token when available. The certificate name defaults to `Connector` but can be changed.

Both ZTB and ZPA use the selected `--env-file`; environment variables take precedence. Authentication can update tokens in that file. Do not share or commit it.

## Select sites and deploy

In `sites.csv`, set both `post=1` and `appc_provision=1` for each intended new site. For example:

```csv
site_name,gateway_name,template_name,wan_interface_name,location_type,city,country,post,appc_provision
Branch1,Branch1-gw-1,Your-Template,ge3,none,Amsterdam,Netherlands,1,1
```

Replace the template and interface with values appropriate for your tenant. With the WAN address fields omitted, this example requests WAN DHCP.

```bash
python3 bulk_create.py --csv sites.csv --validate-only
python3 bulk_create.py --csv sites.csv --dry-run
python3 bulk_create.py --csv sites.csv
```

Windows users can use `.\.venv\Scripts\python.exe` in place of `python3` after following the main setup guide.

Offline validation makes no network requests. Preview authenticates to ZPA and checks the customer/certificate before any deployment writes. The named certificate must resolve; there is no arbitrary certificate fallback. Preview does not create groups or keys, or attach keys to ZTB.

A selected `appc_provision=1` row conflicts with `ZPA_ENABLED=false` and blocks preflight. Optional ZPA preflight failures block the whole batch before site creation.

## Results and troubleshooting

Read the short report under `out/runs/` and inspect the `ZPA` stage in its JSON counterpart. API-stage success is separate from connector registration and connectivity.

| Problem | What to check |
| --- | --- |
| Authentication or customer mismatch | Client credentials, cloud URL, and customer ID. |
| Enrollment certificate unavailable | Exact configured name and permission to read it. |
| Group or provisioning key creation fails | Console error, API permissions, and any resources already created during this run. |
| Attaching the key to ZTB fails | The target cluster and whether the ZPA group/key already exist. |
| Token expires during a batch | Inspect partial results; mid-batch ZPA token refresh is not implemented. |

## Limitations

- Groups and keys are not automatically reused or rolled back after failure. Inspect resources before repair; rerunning site creation will stop at the existing-site check.
- The current country mapping defaults unrecognized values to `NL`. Review the country settings before deploying outside the supported mappings in `zpa_provisioning.py`.
- Group creation attempts OpenStreetMap geocoding using the site city and country. Lookup failures fall back to coordinates `0,0`.
- HA and ZPA workflows need broader live validation. Offline tests do not establish connector readiness.
- The integration provisions App Connector resources; it does not configure security policies.

[Return to the setup guide](readme.md)

# test_ztb.py
from ztb_api import ZTBSession

api = ZTBSession()  # reads .env, logs in, exchanges token

# quick sanity: "releases" (v2) and "Gateway list" (v3)
rel = api.get("/v2/Gateway/releases?refresh_token=enabled")
print("Releases:", rel)

sites = api.get("/Gateway/?gateway_type=isolation&limit=5&refresh_token=enabled")
# Pretty-print a compact view
rows = sites.get("rows", [])
for r in rows:
    gw = r.get("gateways", [{}])[0]
    print({
        "location": r.get("location"),
        "gateway":  gw.get("gateway_name"),
        "state":    gw.get("operational_state"),
        "version":  gw.get("running_version"),
    })
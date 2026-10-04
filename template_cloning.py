"""Native ZTB template cloning through the existing authenticated client.

Contract verified against the tenant's /swagger.json (2026-09-26):
POST /api/v3/templates/{id}/clone accepts {"name": "..."}. Its documented
response has no resource ID, so creation must be followed by a unique lookup.
"""

from dataclasses import dataclass
import hashlib
import json
import time
from urllib.parse import quote


class TemplateCloneError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def read_rows(get, path, **filters):
    """Read all pages, refusing incomplete, changing, or repeated inventory."""
    result, seen, total = [], set(), None
    for page in range(1000):
        data = get(path, {"page": page, "size": 100, **filters})
        rows = data if isinstance(data, list) else data.get("result") if isinstance(data, dict) else None
        count = data.get("count") if isinstance(data, dict) else None
        if isinstance(rows, dict):
            count = rows.get("count", count)
            rows = rows.get("rows")
        if not isinstance(rows, list) or (count is not None and (type(count) is not int or count < 0)):
            raise ValueError("Invalid template inventory")
        if page and count != total:
            raise ValueError("Template inventory changed while reading")
        total = count
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"] or row["id"] in seen:
                raise ValueError("Invalid or repeated template inventory entry")
            seen.add(row["id"])
            result.append(row)
        if count is not None and len(result) > count:
            raise ValueError("Inconsistent template inventory count")
        if (count is not None and len(result) == count) or (count is None and len(rows) < 100):
            return result
        if not rows:
            break
    raise ValueError("Incomplete template inventory")


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def configuration(value):
    """Exclude session tokens and usage counters from reference comparison."""
    if isinstance(value, dict):
        return {key: configuration(item) for key, item in value.items()
                if key not in {"token", "cluster_token", "sites_count"}}
    if isinstance(value, list):
        return [configuration(item) for item in value]
    return value


@dataclass(frozen=True)
class TemplateClonePlan:
    source_id: str
    source_name: str
    name: str
    source_digest: str
    settings: dict

    def summary(self):
        return {"source_id": self.source_id, "source_name": self.source_name, "name": self.name}


class TemplateCloner:
    def __init__(self, engine):
        self.engine = engine
        self.snapshots = {}

    def get(self, path, params=None):
        return self.engine.get_json(self.engine.API_V3 + path, params=params, headers=self.engine._v3_headers())

    def inventory(self, **filters):
        rows = read_rows(self.get, "/templates", sort="name", sortdir="asc", **filters)
        if any(not isinstance(row.get("name"), str) or not row["name"].strip() for row in rows):
            raise ValueError("Template inventory contains an invalid name")
        return rows

    def detail(self, identifier):
        value = self.get("/templates/" + quote(identifier, safe=""))
        if (not isinstance(value, dict) or value.get("id") != identifier or
                not isinstance(value.get("name"), str) or not value["name"].strip() or
                not value.get("platform_type") or not value.get("deployment_type")):
            raise ValueError("Invalid template detail")
        return configuration(value)

    def snapshot(self, identifier):
        detail = self.detail(identifier)
        base = "/templates/" + quote(identifier, safe="")
        snapshot = {"settings": detail}
        for kind in ("interfaces", "vlans", "policies"):
            snapshot[kind] = sorted(read_rows(self.get, base + "/" + kind), key=lambda row: row["id"])
        gateways = {row["gateway_id"] for row in snapshot["interfaces"] if row.get("gateway_id")}
        if not gateways:
            raise ValueError("Template has no gateway interfaces")
        snapshot["pbr"] = {}
        for gateway in sorted(gateways):
            data = self.get("/templates/pbr/policies", {"template_id": identifier, "gateway_id": gateway})
            if not isinstance(data, dict) or "policies" not in data or not isinstance(data["policies"], (list, type(None))):
                raise ValueError("Invalid template routing policies")
            # Preserve policy order: routing priority is meaningful.
            snapshot["pbr"][gateway] = data["policies"] or []
        return detail, fingerprint(configuration(snapshot))

    def plan(self, source_id, name, inventory):
        if any(str(item.get("name", "")).strip().casefold() == name.casefold() for item in inventory):
            raise TemplateCloneError("template_name_exists")
        if source_id not in self.snapshots:
            self.snapshots[source_id] = self.snapshot(source_id)
        detail, digest = self.snapshots[source_id]
        settings = {key: detail.get(key) for key in
                    ("platform_type", "deployment_type", "dhcp_service", "private_dns", "connect_to_hub", "nat_enabled")}
        return TemplateClonePlan(source_id, detail["name"], name, digest, settings)

    def create(self, plan):
        # Recheck immediately before the write; never reuse an unrelated template.
        try:
            if any(str(item.get("name", "")).strip().casefold() == plan.name.casefold()
                   for item in self.inventory(search=plan.name)):
                raise TemplateCloneError("template_name_exists")
            if self.snapshot(plan.source_id)[1] != plan.source_digest:
                raise TemplateCloneError("template_source_changed")
        except TemplateCloneError:
            raise
        except Exception:
            raise TemplateCloneError("template_check_failed") from None
        try:
            response = self.engine.post_json(
                self.engine.API_V3 + "/templates/" + quote(plan.source_id, safe="") + "/clone",
                {"name": plan.name}, headers=self.engine._v3_headers())
            if response.status_code not in (200, 201, 202):
                raise TemplateCloneError("template_creation_unconfirmed")
        except Exception:
            # No POST retry after timeouts/errors; it may already have created it.
            raise TemplateCloneError("template_creation_unconfirmed") from None
        try:
            for attempt in range(5):
                matches = [item for item in self.inventory(search=plan.name)
                           if str(item.get("name", "")).strip().casefold() == plan.name.casefold()]
                if len(matches) > 1:
                    break
                if matches:
                    identifier = matches[0]["id"]
                    if identifier == plan.source_id:
                        break
                    detail = self.detail(identifier)
                    if detail["name"] != plan.name or any(detail.get(key) != value for key, value in plan.settings.items()):
                        break
                    return identifier
                if attempt < 4:
                    time.sleep(1)
        except Exception:
            pass
        raise TemplateCloneError("template_verification_failed")

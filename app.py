#!/usr/bin/env python3
"""Local workspace for saved rollouts, reference imports, and deployment."""

import argparse
import base64
import csv
from contextlib import contextmanager
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from html import escape
import io
import json
import os
from pathlib import Path
import re
import secrets
import signal
import threading
import webbrowser
import zipfile

from input_validation import validate_rows, without_disabled_dhcp_range
from zpa_segments import build_segment_plan
from site_reference import ReferenceSites
from ui_deployment import DeploymentSession
from country_catalog import COUNTRIES
from project_store import ProjectStore, ProjectConflict
from rollout_overview import readiness, recorded_results
from csv_templates import template_bundle
from credential_fields import clean_values, reject_credential_fields
from csv_safety import csv_text
from ucaas_catalog import load_catalog, refresh_catalog, DEFAULT_SERVICES
from runtime_paths import data_directory
from diagram_store import DiagramStore
from diagram_branding import logo as diagram_logo
from site_diagrams import planned as planned_diagram, options as diagram_options
from diagram_render import svg as diagram_svg, png as diagram_png


UI_ROOT = Path(__file__).resolve().parent / "ui"
MAX_BODY = 4 * 1024 * 1024
SITE_FIELDS = ["site_name", "gateway_name", "template_name", "city", "country", "wan_interface_name",
               "wan_dns", "private_dns", "dhcp_server_ip", "location_type", "zia_location_name",
               "location_template_name", "vlans_file", "post", "appc_provision", "dns_split", "dns_private_domains"]
VLAN_FIELDS = ["name", "tag", "subnet", "default_gateway", "dhcp_start", "dhcp_end", "interface",
               "zone", "enabled", "share_over_vpn", "dhcp_service", "zpa_include", "per_network_dns", "gateway_target"]


class MissingVlanFiles(ValueError):
    def __init__(self, names):
        self.names = sorted(names)
        super().__init__("Choose the referenced VLAN files: " + ", ".join(self.names))


def parse_csv(text, name):
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")), strict=True)
    headers = reader.fieldnames
    if not headers:
        raise ValueError(f"{name}: missing CSV header")
    headers = [h.strip() for h in headers]
    if not all(headers) or len(headers) != len(set(headers)):
        raise ValueError(f"{name}: headers must be unique and nonempty")
    reject_credential_fields(dict.fromkeys(headers))
    reader.fieldnames = headers
    rows = []
    for row in reader:
        if None in row or any(value is None for value in row.values()):
            raise ValueError(f"{name}: row {reader.line_num} has a different number of values than the header")
        reject_credential_fields(row)
        rows.append(row)
    return headers, rows


def import_batch(files):
    if not isinstance(files, list) or not files or len(files) > 501:
        raise ValueError("Choose a sites CSV and its referenced VLAN CSV files")
    parsed, site_files = {}, []
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not isinstance(item.get("content"), str):
            raise ValueError("Invalid uploaded CSV")
        name = item["name"].replace("\\", "/").rsplit("/", 1)[-1]
        if name in parsed:
            raise ValueError(f"Two files have the name {name}; import files with unique names")
        headers, rows = parse_csv(item["content"], name)
        parsed[name] = rows
        if "site_name" in headers:
            site_files.append(name)
    if len(site_files) != 1:
        raise ValueError("Select exactly one CSV containing a site_name column, along with its VLAN CSV files")
    referenced = {row.get("vlans_file", "").replace("\\", "/").rsplit("/", 1)[-1]
                  for row in parsed[site_files[0]]}
    missing = referenced - parsed.keys() - {""}
    if missing:
        raise MissingVlanFiles(missing)
    batch = []
    for row in parsed[site_files[0]]:
        name = row.get("vlans_file", "").replace("\\", "/").rsplit("/", 1)[-1]
        batch.append({"fields": row, "vlans": parsed[name] if name else []})
    normalize_batch(batch)
    return batch


DEFAULT_BRANDING = object()


def normalize_batch(batch, branding=DEFAULT_BRANDING):
    if not isinstance(batch, list) or len(batch) > 500:
        raise ValueError("A workspace supports up to 500 sites")
    rows = []
    brand = diagram_logo(branding) if branding is not DEFAULT_BRANDING else None
    count = 0
    for item in batch:
        if not isinstance(item, dict) or not isinstance(item.get("fields"), dict) or not isinstance(item.get("vlans"), list):
            raise ValueError("Each site needs fields and a VLAN list")
        fields = item["fields"]
        if any(not isinstance(k, str) or not isinstance(v, (str, bool, int)) for k, v in fields.items()):
            raise ValueError("Site fields must contain text, numbers, or booleans")
        for vlan in item["vlans"]:
            if not isinstance(vlan, dict) or any(not isinstance(k, str) or not isinstance(v, (str, bool, int)) for k, v in vlan.items()):
                raise ValueError("VLAN fields must contain text, numbers, or booleans")
        count += len(item["vlans"])
        if count > 10000:
            raise ValueError("A workspace supports up to 10,000 VLANs")
        # Uploaded file references must never become server-side filesystem reads.
        row = {k: v for k, v in fields.items() if k not in ("vlans_file", "vlans")}
        if "ha_enabled" in item and not isinstance(item["ha_enabled"], bool):
            raise ValueError("Gateway setup must be Standalone or High availability")
        if item.get("ha_enabled") is False:
            for field in ("gateway_name_b", "wan1_interface_name", "wan1_ip", "wan1_mask", "wan1_gw", "vrrp_link_interface"):
                row[field] = ""
        modes = item.get("wan_modes", {})
        if not isinstance(modes, dict) or any(key not in ("0", "1") or mode not in ("dhcp", "static") for key, mode in modes.items()):
            raise ValueError("WAN addressing must be DHCP or Static IP")
        for index, mode in modes.items():
            if mode == "dhcp":
                for part in ("ip", "mask", "gw"):
                    row[f"wan{index}_{part}"] = ""
        row["vlans"] = [without_disabled_dhcp_range(vlan) for vlan in item["vlans"]]
        if 'diagram' in item:
            row['diagram_options_json'] = json.dumps(diagram_options(item['diagram']))
        if branding is not DEFAULT_BRANDING:
            opts = diagram_options(row.get('diagram_options_json', '{}'))
            if brand is None:opts.pop('logo', None)
            else:opts['logo'] = brand
            row['diagram_options_json'] = json.dumps(opts)
        rows.append(row)
    return rows


def validate_batch(batch):
    validation = validate_rows(normalize_batch(batch), source="Workspace")
    issues = [asdict(issue) for issue in validation.issues]
    for number, item in enumerate(batch, 2):
        if str(item["fields"].get("post", "")).strip() != "1":
            continue
        if item.get("ha_enabled") is True:
            for field in ("gateway_name_b", "wan1_interface_name"):
                if not str(item["fields"].get(field, "")).strip() and not any(issue["row"] == number and issue["field"] == field for issue in issues):
                    issues.append(dict(source="Workspace", row=number, field=field,
                        message="required for a high-availability site"))
        for index, mode in item.get("wan_modes", {}).items():
            if index == "1" and item.get("ha_enabled") is False:
                continue
            if mode != "static":
                continue
            required = [f"wan{index}_{part}" for part in ("ip", "mask", "gw")]
            if index == "1":
                required.append("gateway_name_b")
            for field in required:
                if not str(item["fields"].get(field, "")).strip() and not any(issue["row"] == number and issue["field"] == field for issue in issues):
                    issues.append(dict(source="Workspace", row=number, field=field,
                        message="required when Static IP is selected"))
    plans = []
    if not issues:
        for site in validation.sites:
            segment = build_segment_plan(site.row["site_name"], site.vlans)
            plans.append(dict(name=site.row["site_name"], vlans=len(site.vlans),
                              template={"mode": site.row.get("template_mode", "existing"),
                                        "source": site.row.get("template_id") or site.row.get("template_name"),
                                        "name": site.row.get("new_template_name")},
                              ucaas={"services": site.row.get("ucaas_services", DEFAULT_SERVICES),
                                     "path_selection": site.row["ucaas_path_selection"].capitalize()} if site.row.get("ucaas_local_breakout") == "1" else None,
                              dns={"domains": site.row.get("dns_private_domains", "")} if site.row.get("dns_split") == "1" else None,
                              additional_wans=json.loads(site.row.get('additional_wans_json','[]')) if site.row.get('copy_additional_wans')=='1' else [],
                              zpa=segment.report() if segment else None))
    return dict(valid=not issues, selected_sites=len(validation.sites), issues=issues, sites=plans)


def export_batch(batch, branding=DEFAULT_BRANDING):
    batch = clean_values(batch)
    result = validate_batch(batch)
    if not result["valid"] or not result["selected_sites"]:
        raise ValueError("Select at least one site and resolve validation errors before exporting")
    data = io.BytesIO()
    rows = []
    normalized = normalize_batch(batch, branding)
    with zipfile.ZipFile(data, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for index, item in enumerate(batch, 1):
            row = dict(normalized[index - 1])
            row.pop("vlans", None)
            slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(row.get("site_name", "site"))).strip("-")[:80] or "site"
            filename = f"vlans/{index:03d}-{slug}.csv"
            row["vlans_file"] = filename if item["vlans"] else ""
            rows.append(row)
            if item["vlans"]:
                archive.writestr(filename, csv_text(normalized[index - 1]["vlans"], VLAN_FIELDS))
        archive.writestr("sites.csv", csv_text(rows, SITE_FIELDS))
        archive.writestr("NEXT-STEPS.txt", "Extract this folder before use. Review sites.csv selections.\n"
                         "From the automation directory, preview with:\n"
                         "python3 bulk_create.py --csv /path/to/extracted/sites.csv --dry-run\n"
                         "Offline validation does not verify tenant templates, zones, capacity, or reachability.\n"
                         "Selected ZPA LAN segments are created disabled; review policy before enabling.\n")
    return dict(filename="ztb-batch.zip", content=base64.b64encode(data.getvalue()).decode())


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, env_file=None, project_dir=None):
        self.token = secrets.token_urlsafe(32)
        self.references = ReferenceSites(env_file or UI_ROOT.parent / ".env")
        self.deployment = DeploymentSession(self.references, data_directory() / "runs")
        self.diagrams = DiagramStore(data_directory() / 'runs')
        self.projects = ProjectStore(project_dir or data_directory() / "projects", normalize_batch)
        super().__init__(address, Handler)

    def server_close(self):
        self.deployment.close()
        self.references.close()
        super().server_close()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def reply(self, status, data, content_type="application/json"):
        if not isinstance(data, bytes):
            data = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self.end_headers()
        self.wfile.write(data)

    def allowed_host(self):
        port = self.server.server_address[1]
        return self.headers.get("Host") in (f"127.0.0.1:{port}", f"localhost:{port}")

    def do_GET(self):
        if not self.allowed_host():
            return self.reply(403, {"error": "Local requests only"})
        if self.path == "/healthz":
            return self.reply(200, {"status": "ok"})
        files = {"/": ("index.html", "text/html; charset=utf-8"),
                 "/theme.js": ("theme.js", "text/javascript; charset=utf-8"),
                 "/network.js": ("network.js", "text/javascript; charset=utf-8"),
                 "/country-picker.js": ("country-picker.js", "text/javascript; charset=utf-8"),
                 "/interface-picker.js": ("interface-picker.js", "text/javascript; charset=utf-8"),
                 "/projects.js": ("projects.js", "text/javascript; charset=utf-8"),
                 "/rollout.js": ("rollout.js", "text/javascript; charset=utf-8"),
                 "/rollout.css": ("rollout.css", "text/css; charset=utf-8"),
                 "/diagrams.js": ("diagrams.js", "text/javascript; charset=utf-8"),
                 "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                 "/customer-example.env": ("customer-example.env", "text/plain; charset=utf-8"),
                 "/style.css": ("style.css", "text/css; charset=utf-8")}
        if self.path not in files:
            return self.reply(404, {"error": "Not found"})
        filename, content_type = files[self.path]
        content = (UI_ROOT / filename).read_bytes()
        if filename == "index.html":
            content = content.replace(b"__LOCAL_TOKEN__", self.server.token.encode())
            options = "".join(
                f'<option value="{escape(c["name"], quote=True)}" '
                f'data-search="{escape("|".join([c["value"], c["code"], *c["aliases"]]), quote=True)}"></option>'
                for c in COUNTRIES)
            content = content.replace(b"__COUNTRY_OPTIONS__", options.encode("utf-8"))
        self.reply(200, content, content_type)

    def do_POST(self):
        origin = self.headers.get("Origin")
        host = self.headers.get("Host", "")
        if (not self.allowed_host() or origin != f"http://{host}"
                or not secrets.compare_digest(self.headers.get("X-Local-Token", ""), self.server.token)):
            return self.reply(403, {"error": "Reload the local workspace and try again"})
        if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            return self.reply(415, {"error": "Expected JSON"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_BODY:
                return self.reply(413, {"error": "Request must be between 1 byte and 4 MB"})
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("Expected an object")
            if self.path == "/api/projects/list":
                result = {"projects": self.server.projects.list()}
            elif self.path == "/api/projects/load":
                result = self.server.projects.load(payload.get("id"))
            elif self.path == "/api/projects/create":
                result = self.server.projects.create(payload.get("id"), payload.get("name"), payload.get("workspace"))
            elif self.path == "/api/projects/save":
                result = self.server.projects.save(payload.get("id"), payload.get("revision"), payload.get("name"), payload.get("workspace"))
            elif self.path == "/api/csv-templates":
                result = template_bundle(payload.get('mode', 'standalone'))
            elif self.path == "/api/import":
                result = {"batch": import_batch(payload.get("files"))}
                logos=[diagram_options(item['fields'].get('diagram_options_json','{}')).get('logo') for item in result['batch']]
                unique={json.dumps(logo,sort_keys=True) for logo in logos}
                if len(unique)>1:
                    raise ValueError('These sites use different diagram logos. Import them into separate projects.')
                if logos and logos[0]:
                    result['diagram_logo']=logos[0]
                    for item in result['batch']:
                        opts=diagram_options(item['fields'].get('diagram_options_json','{}'));opts.pop('logo',None)
                        item['fields']['diagram_options_json']=json.dumps(opts)
            elif self.path == "/api/tenant/connect":
                result = self.server.deployment.connection_action(lambda: self.server.references.connect(payload.get("connection")))
            elif self.path == "/api/connections/import":
                result = self.server.deployment.connection_action(lambda: self.server.references.connect_file(payload.get("content")))
            elif self.path == "/api/tenant/pull":
                result = self.server.references.pull(payload.get("site_id"))
            elif self.path == "/api/tenant/zones":
                result = self.server.references.zones()
            elif self.path == "/api/tenant/interfaces":
                result = self.server.references.interfaces(payload.get("template_name", ""),
                    payload.get("template_id", ""), payload.get("refresh", False))
            elif self.path == "/api/zpa/connect":
                result = self.server.deployment.connection_action(lambda: self.server.references.connect_zpa(payload.get("connection")))
            elif self.path == "/api/connections":
                result = self.server.references.connection_status()
            elif self.path == "/api/ucaas/catalog":
                try:
                    result = load_catalog()
                except Exception:
                    raise ValueError("UCaaS destinations are unavailable. Refresh the vendor lists.") from None
            elif self.path == "/api/ucaas/refresh":
                try:
                    result = self.server.deployment.connection_action(refresh_catalog)
                except Exception:
                    raise ValueError("Could not refresh all vendor lists. The previous catalog was kept. Wait for any deployment to finish, then try again.") from None
            elif self.path == "/api/deployment/status":
                result = self.server.deployment.status()
            elif self.path == "/api/deployment/report":
                result = self.server.deployment.report()
            elif self.path == '/api/diagrams/logo':
                result = {'logo': diagram_logo(payload.get('logo'))}
            elif self.path == '/api/diagrams/preview':
                format=payload.get('format','svg')
                if format not in ('svg','png'):
                    raise ValueError('Choose SVG or PNG for a planned diagram')
                items = payload.get('batch')
                if not isinstance(items, list) or len(items) != 1:
                    raise ValueError('Choose one site for a diagram preview')
                row = normalize_batch(items, payload.get('diagram_logo', DEFAULT_BRANDING))[0]
                settings, ports = {}, {}
                # Reuse already loaded template metadata; no tenant call on preview/download.
                for cached in self.server.references.interfaces_cache.values():
                    template=cached['template']
                    if (row.get('template_id') == template['id'] if row.get('template_id') else row.get('template_name','').casefold() == template['name'].casefold()):
                        settings=template
                        ports={('a' if g['id']=='Gateway-1' else 'b'):g['interfaces'] for g in cached['gateways']}
                        break
                model=planned_diagram(row,row['vlans'],settings,ports)
                renderer=diagram_png if format=='png' else diagram_svg
                result={'filename':'planned-site-diagram.'+format,'content':base64.b64encode(renderer(model)).decode(),'mime':'image/png' if format=='png' else 'image/svg+xml'}
            elif self.path in ('/api/diagrams/list', '/api/diagrams/download', '/api/diagrams/regenerate'):
                project = self.server.projects.load(payload.get('project_id'))
                pid=project['id']
                if self.path.endswith('/list'):
                    result={'runs':self.server.diagrams.list(pid)}
                elif self.path.endswith('/download'):
                    result=self.server.diagrams.download(payload.get('run'),pid,payload.get('site'),payload.get('format','svg'))
                else:
                    result=self.server.diagrams.regenerate(payload.get('run'),pid,payload.get('site'))
            elif self.path in ("/api/deployment/preview", "/api/deployment/start"):
                batch = payload.get("batch")
                checked = validate_batch(batch)
                if not checked["valid"] or not checked["selected_sites"]:
                    raise ValueError("Select sites and resolve local validation errors first.")
                if any(item.get("reference") and str(item["fields"].get("post")) == "1" for item in batch):
                    raise ValueError("Reference sites cannot be deployed. Create a branch copy first.")
                rows = normalize_batch(batch, payload.get('diagram_logo', DEFAULT_BRANDING))
                pid = payload.get('project_id', '')
                if pid:
                    self.server.projects.load(pid)
                if self.path.endswith('/start') and pid != self.server.deployment.project_id:
                    raise ValueError('The rollout project changed. Preview again before deploying.')
                result = (self.server.deployment.preview(rows, pid) if self.path.endswith("preview") else
                          self.server.deployment.deploy(payload.get("preview_id"), rows))
            elif self.path == '/api/rollout/check':
                batch = payload.get('batch')
                normalize_batch(batch)
                pid = payload.get('project_id', '')
                if pid:
                    self.server.projects.load(pid)
                tenant = self.server.references.connection_status()['tenant']
                result = {'branches': readiness(batch, validate_batch), 'tenant': tenant,
                          'results': recorded_results(self.server.deployment.report_dir, pid, tenant)}
            elif self.path == "/api/validate":
                result = validate_batch(payload.get("batch"))
            elif self.path == "/api/export":
                result = export_batch(payload.get("batch"), payload.get('diagram_logo', DEFAULT_BRANDING))
            else:
                return self.reply(404, {"error": "Not found"})
            self.reply(200, result)
        except MissingVlanFiles as exc:
            self.reply(400, {"error": str(exc), "missing_files": exc.names})
        except ProjectConflict as exc:
            self.reply(409, {"error": str(exc)})
        except (ValueError, TypeError, csv.Error, UnicodeError) as exc:
            self.reply(400, {"error": str(exc)})
        except Exception:
            self.reply(500, {"error": "Unable to complete this request"})


@contextmanager
def terminal_shutdown_signals():
    """First interrupt/hangup stops serving; further ones let cleanup finish."""
    stopping = False

    def stop_server(signum, frame):
        nonlocal stopping
        if not stopping:
            stopping = True
            raise KeyboardInterrupt

    previous = {}
    try:
        for name in ("SIGINT", "SIGHUP"):
            number = getattr(signal, name, None)
            if number is not None:
                previous[number] = signal.signal(number, stop_server)
        yield
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Open the local ZTB rollout workspace")
    parser.add_argument("--port", type=int, default=os.environ.get("ZTB_PORT", "8765"))
    parser.add_argument("--bind", choices=("127.0.0.1", "0.0.0.0"), default="127.0.0.1",
                        help="Listen address; use 0.0.0.0 only inside a container published to localhost")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--env-file", default=str(UI_ROOT.parent / ".env"), help="Local credentials used when connecting to pull a reference site")
    parser.add_argument("--data-dir", default=os.environ.get("ZTB_DATA_DIR"),
                        help="Persistent projects, reports and refreshed catalog directory (or ZTB_DATA_DIR)")
    parser.add_argument("--project-dir", help="Override the saved project directory only")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if args.data_dir:
        # Spawned workers must read the same catalog as the preview/UI process.
        os.environ["ZTB_DATA_DIR"] = str(Path(args.data_dir).expanduser().resolve())
    with terminal_shutdown_signals(), LocalServer((args.bind, args.port), args.env_file, args.project_dir) as server:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        print(f"ZTB workspace: {url}", flush=True)
        print("Prepare, preview, and deploy selected branches, or export CSVs. Keep this app running during deployment. Press Ctrl+C to stop.", flush=True)
        if not args.no_browser:
            threading.Timer(0.3, lambda: webbrowser.open(url)).start()
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()

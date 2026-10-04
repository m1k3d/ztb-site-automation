"""Versioned, IPv4-only collaboration destinations parsed from vendor publications.

Reading the editor never fetches vendor pages. Refresh is explicit and atomic;
preview pins the exact selected contents, not a mutable object name.
"""
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
import hashlib
import ipaddress
import json
from pathlib import Path
import re
import threading

import requests
from runtime_paths import catalog_path

ROOT = Path(__file__).resolve().parent
CATALOG_PATH = ROOT / "data/ucaas_endpoints.json"
MAX_AGE_DAYS = 30
LOCK = threading.RLock()
SOURCES = {
    "microsoft": "https://endpoints.office.com/endpoints/worldwide?clientrequestid=15abdd3c-06de-4db4-921b-3cfc408ef894&ServiceAreas=Skype",
    "zoom": "https://support.zoom.com/hc/en/article?id=zm_kb&sysparm_article=KB0060548",
    "zoom_ipv4": "https://assets.zoom.us/docs/ipranges/ZoomMeetings.txt",
    "zoom_ipv6": "https://assets.zoom.us/docs/ipranges/ZoomMeetings-IPv6.txt",
    "webex": "https://help.webex.com/article/WBX000028782",
    "google": "https://knowledge.workspace.google.com/admin/meet/prepare-your-network-for-meet-meetings-and-live-streams",
}
SERVICES = {"teams": "Microsoft Teams", "zoom": "Zoom Meetings", "webex": "Webex", "meet": "Google Meet"}
DEFAULT_SERVICES = "teams,zoom,webex"
DOMAIN = re.compile(r"(?:\*\.)?(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,63}", re.I)
CIDR = re.compile(r"(?<![\w.:])(?:[0-9a-f]*:[0-9a-f:]+|(?:\d{1,3}\.){3}\d{1,3})/\d{1,3}", re.I)


def canonical_ports(values):
    """Normalize exact TCP/UDP destination-port sets, rejecting Any and catch-alls."""
    if not isinstance(values, list) or not values:
        raise ValueError("A nonempty destination port object is required")
    ranges = {}
    for value in values:
        if not isinstance(value, str) or value.count(":") != 1:
            raise ValueError("Invalid protocol/port entry")
        protocol, ports = value.lower().split(":")
        if protocol not in ("tcp", "udp"):
            raise ValueError("Only TCP and UDP destination ports are supported")
        for item in ports.split(","):
            if not re.fullmatch(r"\d{1,5}(?:-\d{1,5})?", item):
                raise ValueError("Invalid destination port range")
            limits = [int(n) for n in item.split("-")]
            first, last = limits[0], limits[-1]
            if not 1 <= first <= last <= 65535:
                raise ValueError("Destination port outside 1-65535")
            ranges.setdefault(protocol, []).append((first, last))
    result = []
    for protocol, intervals in sorted(ranges.items()):
        merged = []
        for first, last in sorted(intervals):
            if merged and first <= merged[-1][1] + 1:
                merged[-1][1] = max(merged[-1][1], last)
            else:
                merged.append([first, last])
        if merged == [[1, 65535]]:
            raise ValueError("All-port UCaaS objects are not supported")
        result.append(protocol + ":" + ",".join(str(a) if a == b else f"{a}-{b}" for a, b in merged))
    return result


def published_ports(text):
    """Port cells, not arbitrary prose. Strip formatting without dropping tokens."""
    text = re.sub(r"\band\b", ",", text.lower()).replace("–", "-").replace("—", "-")
    text = re.sub(r"\s+", "", text)
    return text


def endpoint(identifier, label, ips=(), names=(), ports=()):
    result = dict(id=identifier, label=label, ipv4=networks(ips), domains=domains(names), ports=canonical_ports(list(ports)))
    if not result["ipv4"] and not result["domains"]:
        raise ValueError("An endpoint set cannot have an empty destination")
    return result


class TableParser(HTMLParser):
    """Preserve each published row's destination/protocol/port association."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows, self.row, self.cell = [], None, None

    def handle_starttag(self, tag, attrs):
        if tag == "tr": self.row = []
        if tag in ("td", "th") and self.row is not None: self.cell = []

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self.cell is not None:
            self.row.append("\n".join(self.cell)); self.cell = None
        if tag == "tr" and self.row is not None:
            self.rows.append(self.row); self.row = None

    def handle_data(self, text):
        if self.cell is not None and text.strip(): self.cell.append(text.strip())


def table_rows(source):
    parser = TableParser(); parser.feed(source)
    return parser.rows


def heading_section(source, title):
    match = re.search(r"<h[1-6]\b[^>]*>\s*" + re.escape(title) + r"\s*</h[1-6]>", source, re.I)
    if not match:
        raise ValueError("Vendor section heading changed")
    remaining = source[match.end():]
    end = re.search(r"<h[1-6]\b", remaining, re.I)
    return remaining[:end.start()] if end else remaining


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def networks(values, version=4):
    result = set()
    for value in values:
        network = ipaddress.ip_network(value, strict=True)
        if network.prefixlen == 0 or network.is_private or network.is_multicast or network.is_loopback or network.is_link_local:
            raise ValueError("Vendor feed contains a non-public or catch-all network")
        if network.version == version:
            result.add(str(network))
    return sorted(result)


def domains(values):
    result = set()
    for value in values:
        value = value.strip().lower().rstrip(".")
        if not DOMAIN.fullmatch(value):
            raise ValueError("Invalid vendor domain")
        result.add(value)
    return sorted(result)


class TextParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skip = max(0, self.skip - 1)

    def handle_data(self, data):
        if not self.skip and data.strip():
            self.parts.append(data.strip())


def html_text(source):
    parser = TextParser()
    parser.feed(source)
    return "\n".join(parser.parts)


def between(text, start, end):
    first = text.index(start) + len(start)
    return text[first:text.index(end, first)]


def parsed_service(key, ips, names, source_keys, scope, excluded=()):
    ipv4, ipv6 = networks(ips), networks(ips, 6)
    names = domains(names)
    if not ipv4 or not names or len(ipv4) > 1000 or len(names) > 1000:
        raise ValueError("Vendor endpoint section is empty or unexpectedly large")
    value = dict(id=key, name=SERVICES[key], ipv4=ipv4, domains=names,
                 excluded_ipv6=len(ipv6), excluded_domains=sorted(excluded), scope=scope,
                 sources=[SOURCES[k] for k in source_keys])
    return value


def zoom_endpoints(article, title, kind):
    result, protocols = [], set()
    for row in table_rows(heading_section(article, title)):
        if not row or row[0].lower() == "protocol":
            continue
        if len(row) != 4 or row[0].lower() not in ("tcp", "udp"):
            raise ValueError("Unexpected Zoom endpoint row")
        protocol = row[0].lower(); protocols.add(protocol)
        ips = CIDR.findall(row[3]) if kind == "media" else []
        names = [v for v in DOMAIN.findall(row[3]) if v in ("*.zoom.us", "*.zoom.com")] if kind == "web" else []
        item = endpoint(kind, "Media" if kind == "media" else "Web", ips, names,
                        [protocol + ":" + published_ports(row[1])])
        existing = next((r for r in result if r["ipv4"] == item["ipv4"] and r["domains"] == item["domains"]), None)
        if existing:
            existing["ports"] = canonical_ports(existing["ports"] + item["ports"])
        else:
            result.append(item)
    if protocols != {"tcp", "udp"}:
        raise ValueError("Zoom protocol sections changed")
    if len(result) > 1:
        for i, item in enumerate(result, 1):
            item.update(id=f"{kind}-{i}", label=f"{item['label']} {i}")
    return result


def parse_sources(source, now=None):
    """Parse bounded sections only. History and other products cannot leak into lists."""
    rows = json.loads(source["microsoft"])
    teams = [r for r in rows if r.get("serviceArea") == "Skype" and r.get("required") is True]
    if not {11, 12}.issubset({r.get("id") for r in teams}):
        raise ValueError("Microsoft Teams core endpoint sets are missing")
    team_ips = [v for r in teams for v in r.get("ips", [])]
    team_domains = [v for r in teams for v in r.get("urls", [])]
    services = [parsed_service("teams", team_ips, team_domains, ["microsoft"],
        "Required Microsoft Teams (Skype service area) Worldwide endpoints. Other Microsoft 365 services and Common dependencies keep their existing forwarding policy.")]
    labels = {11: "Media", 12: "Signaling", 16: "Streaming", 17: "Links", 27: "Assets", 127: "Skype"}
    services[0]["endpoints"] = [endpoint(f"set-{r['id']}", labels.get(r['id'], f"Set {r['id']}"),
        r.get("ips", []), r.get("urls", []),
        [p + ":" + published_ports(r[key]) for p, key in (("tcp", "tcpPorts"), ("udp", "udpPorts")) if r.get(key)])
        for r in sorted(teams, key=lambda r: r["id"])]
    # Zoom publishes its article HTML in JSON-LD as well as the page body.
    article = None
    for match in re.finditer(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', source["zoom"], re.S):
        data = json.loads(match[1])
        if isinstance(data, dict) and isinstance(data.get("articleBody"), str):
            article = data["articleBody"]
    zoom = html_text(article if article is not None else source["zoom"])
    zoom = zoom[zoom.rindex("Firewall rules for Zoom\n"):]
    section = between(zoom, "Firewall rules for Zoom\n", "Firewall rules for Zoom Meetings and Webinars")
    published = domains([line for line in section.splitlines() if DOMAIN.fullmatch(line)])
    zoom_domains = [v for v in published if v in ("*.zoom.us", "*.zoom.com")]
    if len(zoom_domains) != 2:
        raise ValueError("Zoom core domain section changed")
    zoom_ips = source["zoom_ipv4"].split() + source["zoom_ipv6"].split()
    services.append(parsed_service("zoom", zoom_ips, zoom_domains, ["zoom", "zoom_ipv4", "zoom_ipv6"],
        "Zoom Meetings and Webinars IP ranges and Zoom service domains. Phone, Contact Center, shared third-party website dependencies and certificate services keep their existing forwarding policy.",
        set(published) - set(zoom_domains)))
    services[-1]["endpoints"] = (zoom_endpoints(article or source["zoom"], "Firewall rules for Zoom Meetings and Webinars", "media")
                                + zoom_endpoints(article or source["zoom"], "Firewall rules for Zoom", "web"))
    if sorted({ip for item in services[-1]["endpoints"] for ip in item["ipv4"]}) != services[-1]["ipv4"]:
        raise ValueError("Zoom table and downloadable IPv4 feed disagree")
    webex = html_text(source["webex"])
    media = between(webex, "IPv4 Subnets for Media Services", "* Azure data centers")
    core = between(webex, "Cisco Webex Services URLs", "Additional Webex related services - Cisco Owned domains")
    webex_domains = domains(DOMAIN.findall(core))
    if "*.webex.com" not in webex_domains or "*.webexapis.com" not in webex_domains:
        raise ValueError("Webex core domain section changed")
    # Cisco explicitly includes the top-level domain as well as subdomains.
    webex_domains += [v[2:] for v in webex_domains if v.startswith("*.")]
    services.append(parsed_service("webex", CIDR.findall(media), webex_domains, ["webex"],
        "Webex media (including Teams video integration), core micro-services and content storage. Includes Cisco's published *.cisco.com domain. Additional integrations and shared third-party services keep their existing forwarding policy."))
    media_ports, signaling_ports, media_protocols = [], [], set()
    for r in table_rows(source["webex"]):
        if len(r) != 4:
            continue
        if "Webex HTTPS signaling" in r[2] and r[1] == "TLS":
            signaling_ports.append("tcp:" + published_ports(r[0]))
        if r[1].startswith("SRTP over ") and "Webex App" in r[3] and "IP subnets for Webex media services" in r[2]:
            protocol = r[1].removeprefix("SRTP over "); media_protocols.add(protocol)
            if protocol not in ("TCP", "UDP", "TLS"):
                raise ValueError("Unknown Webex media protocol")
            media_ports.append(("udp" if protocol == "UDP" else "tcp") + ":" + published_ports(r[0]))
    if media_protocols != {"UDP", "TCP", "TLS"} or len(signaling_ports) != 1:
        raise ValueError("Webex port table changed")
    services[-1]["endpoints"] = [endpoint("media", "Media", services[-1]["ipv4"], ports=media_ports),
                                  endpoint("signaling", "Signaling", names=webex_domains, ports=signaling_ports)]
    google = html_text(source["google"])
    google_media = between(google, "Step 3: Allow access to Google IP address ranges", "Step 4: Review bandwidth requirements")
    google_core = between(google, "Domains for static resources", "Domains for user feedback & event log uploads")
    google_domains = [line for line in google_core.splitlines() if DOMAIN.fullmatch(line)]
    google_domains += re.findall(r"SNI:\s*([a-z0-9.-]+)", google_media)
    if "meet.google.com" not in google_domains or "workspace.turns.goog" not in google_domains:
        raise ValueError("Google Meet endpoint sections changed")
    services.append(parsed_service("meet", CIDR.findall(google_media), google_domains, ["google"],
        "Meet Workspace and consumer media, static resources, API and streaming domains. Includes the exact Docs and Chat hosts listed by Google. Path-specific feedback URLs keep their existing forwarding policy."))
    port_section = between(google, "Step 1: Set up outbound ports for media traffic", "Step 2: Allow access")
    udp = re.search(r"For audio and video, set up outbound UDP ports ([^.]+)\.", port_section)
    web = re.search(r"For web traffic and user authentication, use outbound UDP and TCP port (\d+)\.", port_section)
    fallback = re.search(r"support Meet traffic over port (\d+)", google_media)
    if not udp or not web or not fallback:
        raise ValueError("Google Meet port guidance changed")
    services[-1]["endpoints"] = [
        endpoint("media", "Media", services[-1]["ipv4"], ports=["udp:" + published_ports(udp[1])]),
        endpoint("fallback", "Media TLS", services[-1]["ipv4"], re.findall(r"SNI:\s*([a-z0-9.-]+)", google_media), ["tcp:" + fallback[1]]),
        endpoint("web", "Web", names=[line for line in google_core.splitlines() if DOMAIN.fullmatch(line)],
                 ports=["tcp:" + web[1], "udp:" + web[1]])]
    for service in services:
        service["version"] = fingerprint(service["endpoints"])[:12]
    return {"schema": 2, "retrieved_at": (now or datetime.now(timezone.utc)).isoformat(),
            "services": services, "source_sha256": {key: hashlib.sha256(value.encode()).hexdigest() for key, value in source.items()}}


def validate_catalog(data, *, fresh=False):
    if not isinstance(data, dict) or data.get("schema") != 2 or len(data["services"]) != len(SERVICES) or {r["id"] for r in data["services"]} != set(SERVICES):
        raise ValueError("Invalid UCaaS catalog")
    age = datetime.now(timezone.utc) - datetime.fromisoformat(data["retrieved_at"])
    if age.days < -1 or (fresh and age.days > MAX_AGE_DAYS):
        raise ValueError("Refresh UCaaS destinations before previewing deployment")
    for service in data["services"]:
        if (not service["ipv4"] or not service["domains"] or networks(service["ipv4"]) != service["ipv4"]
                or domains(service["domains"]) != service["domains"]
                or any(ipaddress.ip_network(v).version != 4 for v in service["ipv4"])
                or fingerprint(service["endpoints"])[:12] != service["version"]):
            raise ValueError("Invalid UCaaS destinations")
        endpoints = service["endpoints"]
        if not endpoints or len(endpoints) > 100 or len({e["id"] for e in endpoints}) != len(endpoints):
            raise ValueError("Invalid UCaaS endpoint sets")
        for item in endpoints:
            if (not re.fullmatch(r"[a-z0-9-]{1,32}", item["id"]) or not re.fullmatch(r"[A-Za-z0-9 -]{1,20}", item["label"])
                    or endpoint(item["id"], item["label"], item["ipv4"], item["domains"], item["ports"]) != item):
                raise ValueError("Invalid UCaaS destination/port pair")
        for key in ("ipv4", "domains"):
            if sorted({v for item in endpoints for v in item[key]}) != service[key]:
                raise ValueError("Unpaired UCaaS destinations")
    return data


def load_catalog(*, fresh=False, path=None):
    with LOCK:
        source = Path(path) if path is not None else catalog_path()
        # New installations start with the bundled snapshot; a damaged saved
        # catalog must still fail validation, rather than silently falling back.
        if path is None and source != CATALOG_PATH and not source.exists():
            source = CATALOG_PATH
        return validate_catalog(json.loads(source.read_text()), fresh=fresh)


def refresh_catalog(*, path=None, fetch=None):
    """No tenant calls or writes; a failed refresh preserves the last good catalog."""
    def download(url):
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        if len(response.content) > 5_000_000:
            raise ValueError("Unexpectedly large vendor publication")
        return response.text
    with LOCK:
        with ThreadPoolExecutor(max_workers=4) as pool:
            jobs = {key: pool.submit(fetch or download, url) for key, url in SOURCES.items()}
            source = {key: job.result() for key, job in jobs.items()}
        data = validate_catalog(parse_sources(source), fresh=True)
        destination = Path(path) if path is not None else catalog_path()
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = destination.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, indent=2) + "\n")
        temporary.replace(destination)
        return data


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh", action="store_true", required=True)
    parser.parse_args()
    catalog = refresh_catalog()
    for item in catalog["services"]:
        print(f"{item['name']}: {len(item['ipv4'])} IPv4 prefixes, {len(item['domains'])} domains; IPv6 excluded")

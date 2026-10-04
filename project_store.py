"""Durable local drafts, separate from connections and deployment approvals."""

from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
from credential_fields import clean_values


class ProjectConflict(ValueError):
    pass


def project_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{32}", value):
        raise ValueError("Invalid project identifier")
    return value


def project_name(value):
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 120:
        raise ValueError("Enter a project name between 1 and 120 characters")
    return value.strip()


def clean_workspace(workspace, normalize_batch):
    if not isinstance(workspace, dict) or workspace.get("version") != 1:
        raise ValueError("Unsupported project format")
    batch = workspace.get("batch")
    normalize_batch(batch)  # Shape and size checks only: incomplete drafts are valid.
    sites = []
    for site in batch:
        saved = {key: site[key] for key in ("fields", "vlans", "ha_enabled", "wan_modes", "gateway_b_draft", "interface_gateways", "reference", "editor", "diagram") if key in site}
        if 'diagram' in saved:
            from site_diagrams import options
            saved['diagram'] = options(saved['diagram'])
        if "reference" in saved:
            reference = saved["reference"]
            if not isinstance(reference, dict) or any(not isinstance(reference.get(key, ""), str) for key in ("id", "name")):
                raise ValueError("Invalid saved reference")
            saved["reference"] = {key: reference[key] for key in ("id", "name") if key in reference}
        for key in ("gateway_b_draft", "editor"):
            if key in saved and not isinstance(saved[key], dict):
                raise ValueError("Invalid editor settings")
        slots = saved.get("interface_gateways", [])
        if not isinstance(slots, list) or len(slots) > 2 or any(not isinstance(slot, str) for slot in slots):
            raise ValueError("Invalid saved gateway slots")
        sites.append(clean_values(saved))
    current = workspace.get("current", -1)
    if type(current) is not int or (sites and not 0 <= current < len(sites)) or (not sites and current != -1):
        raise ValueError("Invalid selected site")
    tab = workspace.get("active_tab", "site")
    if tab not in ("site", "vlans"):
        raise ValueError("Invalid editor tab")
    result = {"version": 1, "batch": sites, "current": current, "active_tab": tab}
    if 'rollout_view' in workspace:
        if workspace['rollout_view'] not in ('reference', 'overview', 'sites', 'review'):
            raise ValueError('Invalid workspace view')
        result['rollout_view'] = workspace['rollout_view']
    if 'group_by_country' in workspace:
        if type(workspace['group_by_country']) is not bool:
            raise ValueError('Invalid country grouping')
        result['group_by_country'] = workspace['group_by_country']
    if workspace.get('diagram_logo') is not None:
        from diagram_branding import logo
        result['diagram_logo'] = logo(workspace['diagram_logo'])
    return result


class ProjectStore:
    def __init__(self, directory, normalize_batch):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.directory / "workspace.sqlite3"
        self.normalize_batch = normalize_batch
        with closing(self.connect()) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, revision INTEGER NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL, opened_at TEXT NOT NULL,
                workspace TEXT NOT NULL)""")
        self.path.chmod(0o600)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def now():
        return datetime.now(timezone.utc).isoformat()

    def encode(self, workspace):
        text = json.dumps(clean_workspace(workspace, self.normalize_batch), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(text.encode()) > 4 * 1024 * 1024:
            raise ValueError("Project is too large (maximum 4 MB)")
        return text

    @staticmethod
    def public(row, full=False):
        result = {key: row[key] for key in ("id", "name", "revision", "created_at", "updated_at")}
        workspace = clean_values(json.loads(row["workspace"]))
        result["site_count"] = len(workspace["batch"])
        if full:
            result["workspace"] = workspace
        return result

    def list(self):
        with closing(self.connect()) as db:
            return [self.public(row) for row in db.execute("SELECT * FROM projects ORDER BY opened_at DESC, updated_at DESC")]

    def load(self, identifier):
        identifier = project_id(identifier)
        with closing(self.connect()) as db, db:
            row = db.execute("SELECT * FROM projects WHERE id=?", (identifier,)).fetchone()
            if row is None:
                raise ValueError("Saved project was not found")
            # Opening never changes the draft or its revision.
            db.execute("UPDATE projects SET opened_at=? WHERE id=?", (self.now(), identifier))
            return self.public(row, full=True)

    def create(self, identifier, name, workspace):
        identifier, name, encoded = project_id(identifier), project_name(name), self.encode(workspace)
        now = self.now()
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM projects WHERE id=?", (identifier,)).fetchone()
            if row is not None:
                if row["name"] == name and row["workspace"] == encoded:
                    return self.public(row)  # Safe replay after a lost response.
                raise ProjectConflict("This project already exists. Open it or save a new copy.")
            db.execute("INSERT INTO projects VALUES (?,?,?,?,?,?,?)", (identifier, name, 1, now, now, now, encoded))
            return self.public(db.execute("SELECT * FROM projects WHERE id=?", (identifier,)).fetchone())

    def save(self, identifier, revision, name, workspace):
        identifier, name, encoded = project_id(identifier), project_name(name), self.encode(workspace)
        if type(revision) is not int or revision < 1:
            raise ValueError("Invalid project revision")
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM projects WHERE id=?", (identifier,)).fetchone()
            if row is None:
                raise ValueError("Saved project was not found")
            if row["revision"] != revision:
                if row["revision"] == revision + 1 and row["name"] == name and row["workspace"] == encoded:
                    return self.public(row)  # A previous save committed but its response was lost.
                raise ProjectConflict("Another tab saved changes to this project. Save your work as a copy to keep both versions.")
            db.execute("UPDATE projects SET name=?, revision=revision+1, updated_at=?, workspace=? WHERE id=?", (name, self.now(), encoded, identifier))
            return self.public(db.execute("SELECT * FROM projects WHERE id=?", (identifier,)).fetchone())

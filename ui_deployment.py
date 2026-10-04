"""One local deployment job, with credential-free status and explicit preview approval."""
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
import multiprocessing
import os
import signal
from pathlib import Path
from queue import Empty
import threading
import time
from urllib.parse import urlsplit
from uuid import uuid4

from deployment_engine import DeploymentEngine, BatchResult, SiteResult
from input_validation import validate_rows
from run_report import reserve_report, save_report, public_diagnostics
from ucaas_breakout import MESSAGES as UCAAS_MESSAGES
from dns_policy import MESSAGES as DNS_MESSAGES
from zpa_segments import SegmentConflictIssue
from site_diagrams import capture_results
from diagram_store import DiagramStore


ACTIVE = {"previewing", "deploying"}
NEXT_ACTION = {
    "preview": "Ready for review. No resources created.",
    "success": "Requested configuration completed. Verify appliance activation and connectivity.",
    "already_exists": "Existing site left unchanged. Inspect it before recovery; do not repeat creation.",
    "lookup_failed": "Could not verify the complete site inventory. Review the diagnostic before retrying; no changes made to this site.",
    "partial": "Site created, but configuration is incomplete. Inspect the failed stages before recovery.",
    "failed": "Creation failed or its outcome is uncertain. Inspect the tenant before retrying.",
    "template_failed": "Site creation was not attempted. Inspect the template name and failed clone stage before retrying.",
    "template_only": "The new template exists, but site creation failed or is uncertain. Inspect the tenant; do not clone again. Recover using the verified template ID after checking the site.",
}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def plan_digest(plan):
    return digest({"sites": [{"payload": site.payload, "vlans": site.vlans, "row": site.row,
                              "template_settings": site.template_settings,
                              "template_clone": {**site.template_clone.summary(), "source_digest": site.template_clone.source_digest}
                              if site.template_clone else None,
                              "ucaas": site.ucaas_plan.summary() if site.ucaas_plan else None,
                              "dns": site.dns_plan.summary() if site.dns_plan else None,
                              "additional_wans": site.additional_wans} for site in plan.sites],
                   "zpa": {"base": plan.zpa_context.base_url, "customer": plan.zpa_context.customer_id,
                           "certificate": plan.zpa_context.enrollment_cert_id} if plan.zpa_context else None})


def public_sites(result):
    return [{"name": site.name, "status": site.status, "stages": dict(site.stages),
             "diagnostics": public_diagnostics(site),
             "template": dict(site.template),
             "ucaas": deepcopy(site.ucaas),
             "dns": deepcopy(site.dns),
             "additional_wans": deepcopy(site.additional_wans),
             "artifacts": deepcopy(site.artifacts),
             "diagram_warning": site.diagram.get('error', ''),
             "next_action": NEXT_ACTION.get(site.status, "Inspect this site before retrying."),
             "segment": {key: site.zpa_segments[key] for key in ("application_name", "status", "subnets") if key in site.zpa_segments}}
            for site in result.sites]


def public_issues(issues):
    # Exception strings can contain raw HTTP bodies. Expose a useful, fixed action per field.
    from site_payload import TEMPLATE_MESSAGES
    messages = {**TEMPLATE_MESSAGES, **UCAAS_MESSAGES, **DNS_MESSAGES, "template_name/template_id": "Check the site template name or ID and permission to read templates.",
                "Template clone": "Cannot verify the source template and its configuration. Check the source template and read permissions.",
                "new_template_name": "Each new template needs a unique name that is not already in use. Choose a new name, or select Use an existing template.",
                "location": "Check the ZIA location choice, name, template, and tenant permissions.",
                "credentials": "Check the ZTB connection and, when requested, the ZPA connection settings.",
                "ZPA preflight": "Check ZPA credentials, customer, enrollment certificate, and conflicting segment names or subnets.",
                "ZPA segments": "Review selected LAN subnets and ZPA segment settings.",
                "Additional WANs": "Check additional WAN addresses, gateway assignments, and WAN ports on the selected template."}
    return [{"row": issue.row, "field": issue.field,
             "message": issue.message if isinstance(issue, SegmentConflictIssue) else
             messages.get(issue.field, "Check this field using Validate & review before trying again.")} for issue in issues]


def ignore_terminal_signals():
    """Only the server handles terminal shutdown; never interrupt a worker's writes."""
    for name in ("SIGINT", "SIGHUP"):
        number = getattr(signal, name, None)
        if number is not None:
            signal.signal(number, signal.SIG_IGN)


def run_worker(rows, config, mode, expected_plan, report_dir, channel, project_id=''):
    """Runs in its own process; legacy helper output is discarded, never served or logged."""
    ignore_terminal_signals()
    report_path = None
    engine = None
    with open(os.devnull, "w") as sink, redirect_stdout(sink), redirect_stderr(sink):
        try:
            engine = DeploymentEngine(replace(config, env_path=None), emit=lambda *_: None)
            plan = engine.plan(validate_rows(rows, source="Workspace"))
            if plan.issues:
                channel.put({"done": True, "state": "blocked", "issues": public_issues(plan.issues), "sites": [],
                             "message": "Tenant checks failed. No deployment resources were created."})
                return
            resolved = plan_digest(plan)
            if mode == "deployment" and resolved != expected_plan:
                channel.put({"done": True, "state": "blocked", "sites": [], "message": "Tenant references changed. Preview the rollout again before deploying."})
                return
            preview = engine.execute(plan, dry_run=True)
            if preview.exit_code:
                channel.put({"done": True, "state": "blocked", "sites": public_sites(preview),
                             "message": "Some sites could not pass tenant checks. No deployment resources were created."})
                return
            if mode == "preview":
                details = [{"name": site.row["site_name"], "template": site.row.get("template_name") or site.template_id,
                            "template_clone": site.template_clone.summary() if site.template_clone else None,
                            "ucaas": site.ucaas_plan.summary() if site.ucaas_plan else None,
                            "dns": site.dns_plan.summary() if site.dns_plan else None,
                            "additional_wans": site.additional_wans,
                            "vlans": len(site.vlans), "app_connector": site.row.get("appc_provision") == "1",
                            "segment": site.zpa_segment_plan.application_name if site.zpa_segment_plan else None}
                           for site in plan.sites]
                channel.put({"done": True, "state": "ready", "sites": public_sites(preview), "details": details,
                             "zpa_customer": plan.zpa_context.customer_id if plan.zpa_context else None,
                             "plan_digest": resolved, "message": "Tenant checks passed. Review the destination and selected sites before deploying."})
                return
            report_path = reserve_report(report_dir)
            channel.put({"report": report_path.name})
            def progress(site, stage, state):
                channel.put({"progress": {"site": site, "stage": stage, "state": state}})
            result = engine.execute(plan, progress=progress)
            capture_results(engine, plan, result, progress=progress)
            try:
                DiagramStore(report_dir).save(report_path, result, project_id)
            except Exception:
                for site in result.sites:
                    if site.stages.get('Site'):
                        site.diagram['error'] = 'Diagram files could not be saved. Deployment outcome is unchanged.'
            # Never persist raw exception text from the engine.
            safe_result = BatchResult(sites=[SiteResult(site.name, site.status, dict(site.stages),
                zpa_segments=deepcopy(site.zpa_segments), diagnostics=dict(site.diagnostics), template=dict(site.template),
                ucaas=deepcopy(site.ucaas), dns=deepcopy(site.dns), additional_wans=deepcopy(site.additional_wans),
                site_id=site.site_id, gateway_ids=list(site.gateway_ids), artifacts=deepcopy(site.artifacts),
                diagram={'error':site.diagram.get('error','')}) for site in result.sites], issues=result.issues)
            save_report(report_path, safe_result, workspace={'project_id': project_id,
                        'tenant': urlsplit(config.ztb_api_base).hostname})
            channel.put({"done": True, "state": "completed" if result.exit_code == 0 else "incomplete",
                         "sites": public_sites(result), "message": "Deployment completed." if result.exit_code == 0 else
                         "Deployment is incomplete. Inspect the affected sites before recovery; creation will not be retried automatically.",
                         "report": report_path.name})
        except BaseException:
            channel.put({"done": True, "state": "interrupted" if mode == "deployment" else "blocked", "sites": [],
                         "message": "Deployment stopped unexpectedly. Inspect the tenant and run report before retrying." if mode == "deployment" else
                         "Preview could not finish. Check the connections and try again. No deployment resources were created.",
                         "report": report_path.name if report_path else None})
        finally:
            if engine is not None:
                engine.client.close()


class DeploymentSession:
    def __init__(self, references, report_dir):
        self.references = references
        self.report_dir = Path(report_dir)
        self.lock = threading.RLock()
        self.job = None
        self.process = self.channel = None
        self.rows = self.config = self.resolved = None
        self.revision = None
        self.exit_seen = None
        self.project_id = ''
        self.closing = False

    def _drain(self):
        if self.channel is None:
            return
        while True:
            try:
                event = self.channel.get_nowait()
            except Empty:
                break
            if "progress" in event:
                self.job["progress"] = event["progress"]
                p = event["progress"]
                self.job.setdefault("stage_progress", {}).setdefault(p["site"], {})[p["stage"]] = p["state"]
            if "report" in event:
                self.job["report"] = event["report"]
            if event.get("done"):
                self.resolved = event.pop("plan_digest", None) or self.resolved
                event.pop("done")
                self.job.update(event)
                self.job["progress"] = None
                self.job["finished"] = time.time()
        if self.process is not None and self.process.exitcode is not None and self.job["state"] in ACTIVE:
            if self.exit_seen is None:
                self.exit_seen = time.monotonic()
            elif time.monotonic() - self.exit_seen > 0.2:
                self.job.update(state="interrupted", message="The worker stopped. Inspect the tenant and report before retrying.")

    def status(self):
        with self.lock:
            self._drain()
            return deepcopy(self.job) if self.job else {"state": "idle"}

    def connection_action(self, action):
        with self.lock:
            self._drain()
            if self.job and self.job["state"] in ACTIVE:
                raise ValueError("Wait for the current preview or deployment before changing connections.")
            if self.job and self.job["state"] == "ready":
                self.job.update(state="expired", message="Connection changed. Preview again before deploying.")
            return action()

    def _start(self, mode):
        if self.process is not None:
            self.process.join(timeout=0.2)
        context = multiprocessing.get_context("spawn")
        self.exit_seen = None
        self.channel = context.Queue()
        self.process = context.Process(target=run_worker, args=(self.rows, self.config, mode, self.resolved, str(self.report_dir), self.channel, self.project_id))
        try:
            self.process.start()
        except Exception:
            self.process = None
            self.job.update(state="interrupted", message="Unable to start the worker. Preview again before deploying.")
            raise ValueError("Unable to start the deployment worker.") from None

    def preview(self, rows, project_id=''):
        with self.lock:
            if self.closing:
                raise ValueError("The workspace is shutting down. Restart it before previewing.")
            self._drain()
            if self.job and self.job["state"] in ACTIVE:
                raise ValueError("A preview or deployment is already running.")
            validation = validate_rows(rows, source="Workspace")
            if not validation.valid or not validation.sites:
                raise ValueError("Select sites and resolve local validation errors first.")
            if any(site.row.get("gateway_name_b") for site in validation.sites):
                raise ValueError("HA deployment is not enabled in the UI yet: reference WAN and management mapping still need verification. CSV export remains available.")
            revision, config = self.references.deployment_settings()
            wants_zpa = any(site.row.get("appc_provision") == "1" for site in validation.sites)
            if config.errors(require_zpa=wants_zpa):
                raise ValueError("Check your ZTB connection and configure ZPA when App Connector provisioning is selected.")
            self.rows, self.config, self.revision = deepcopy(rows), config, revision
            self.project_id = project_id
            self.resolved = None
            self.job = {"id": uuid4().hex, "state": "previewing", "tenant": urlsplit(config.ztb_api_base).hostname,
                        "project_id": project_id,
                        "zpa_cloud": urlsplit(config.zpa_base_url).hostname if wants_zpa else None,
                        "site_count": len(validation.sites), "sites": [], "issues": [], "details": [], "report": None,
                        "message": "Checking tenant templates, locations, existing sites, and requested ZPA and UCaaS settings."}
            self._start("preview")
            return deepcopy(self.job)

    def deploy(self, job_id, rows):
        with self.lock:
            if self.closing:
                raise ValueError("The workspace is shutting down. Restart it and preview again before deploying.")
            self._drain()
            if not self.job or job_id != self.job["id"] or digest(rows) != digest(self.rows):
                raise ValueError("The rollout changed or has no matching preview. Preview it again.")
            if self.job["state"] in ("deploying", "completed", "incomplete", "interrupted"):
                return deepcopy(self.job)  # An approval is consumed only once, including uncertain outcomes.
            revision, _ = self.references.deployment_settings()
            if self.job["state"] != "ready" or revision != self.revision or time.time() - self.job.get("finished", 0) > 300:
                raise ValueError("The preview expired or the connection changed. Preview again before deploying.")
            self.job.update(state="deploying", sites=[], issues=[], progress=None,
                            message="Rechecking tenant references before creating resources. Keep the local app running.")
            self._start("deployment")
            return deepcopy(self.job)

    def report(self):
        with self.lock:
            self._drain()
            if not self.job or not self.job.get("report") or self.job["state"] in ACTIVE:
                raise ValueError("A finished run report is not available yet.")
            path = self.report_dir / self.job["report"]
            summary = path.with_suffix(".txt")
            target = summary if summary.exists() else path
            return {"filename": target.name, "content": target.read_text()}

    def close(self):
        # app.main ignores repeated terminal signals during shutdown. Keep draining
        # the queue: joining without a reader can deadlock its worker feeder thread.
        # The lock also lets an in-progress request finish starting its worker.
        with self.lock:
            self.closing = True
            if self.process is not None:
                while self.process.is_alive():
                    self.process.join(timeout=0.1)
                    self._drain()
                self._drain()
            if self.channel is not None:
                self.channel.close()
                self.channel = None

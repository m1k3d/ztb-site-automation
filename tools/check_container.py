#!/usr/bin/env python3
"""Build and smoke-test an isolated Docker workspace. No tenant connections.

Uses only Python's standard library on the host. Test containers and the empty
volume created by this script are removed afterward; existing workspaces are
never opened or stopped.
"""
import argparse
import base64
import http.client
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import zipfile
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
DOCKER = shutil.which("docker") or "/Applications/Docker.app/Contents/Resources/bin/docker"
DOCKER_ENV = dict(os.environ)
# Docker Desktop can be installed before its CLI symlinks are added to PATH.
# Its credential helper lives beside the CLI; keep this adjustment process-local.
DOCKER_ENV["PATH"] = str(Path(DOCKER).resolve().parent) + os.pathsep + os.environ.get("PATH", "")


def docker(*args, timeout=90):
    result = subprocess.run([DOCKER, *args], cwd=ROOT, env=DOCKER_ENV,
                            text=True, capture_output=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"docker {args[0]} failed: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout.strip()


def archive_command(*args, content=None):
    result = subprocess.run([DOCKER, *args], cwd=ROOT, env=DOCKER_ENV,
                            input=content, capture_output=True, timeout=90)
    if result.returncode:
        raise RuntimeError("Archive check failed: " + result.stderr.decode(errors="replace"))
    return result.stdout


def request(port, path, payload=None, token="", origin=None, host=None):
    client = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {"Host": host or f"127.0.0.1:{port}"}
    if payload is not None:
        headers.update({"Content-Type": "application/json", "X-Local-Token": token,
                        "Origin": origin or f"http://127.0.0.1:{port}"})
    try:
        client.request("POST" if payload is not None else "GET", path,
                       json.dumps(payload) if payload is not None else None, headers)
        response = client.getresponse()
        return response.status, response.read().decode()
    finally:
        client.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--image", default="ztb-site-automation:local")
    parser.add_argument("--platform", choices=("linux/amd64", "linux/arm64"),
                        help="Build and run for a specific architecture (emulation may be required)")
    args = parser.parse_args()
    docker("version", "--format", "{{.Server.Version}}")
    docker("compose", "config", "--quiet")
    if not args.skip_build:
        print("Building Docker image…", flush=True)
        platform_args = ["--platform", args.platform] if args.platform else []
        docker("build", *platform_args, "--tag", args.image, ".", timeout=900)
    else:
        platform_args = ["--platform", args.platform] if args.platform else []
    print("Image architecture: " + docker("image", "inspect", "--format", "{{.Os}}/{{.Architecture}}", args.image), flush=True)
    name = "ztb-smoke-" + uuid4().hex[:12]
    volume = name + "-data"
    restore_volume = name + "-restore"
    created = False
    restore_created = False
    # Choose an ephemeral host port, then use the same application port so the
    # production Host/Origin checks are exercised without adding exceptions.
    import socket
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    try:
        docker("volume", "create", volume)
        created = True
        old_token = None
        saved_catalog = None
        for generation in range(2):
            docker("run", *platform_args, "--detach", "--name", name, "--init", "--read-only", "--cap-drop", "ALL",
                   "--security-opt", "no-new-privileges:true", "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m,mode=1777",
                   "--publish", f"127.0.0.1:{port}:{port}", "--env", f"ZTB_PORT={port}",
                   "--mount", f"type=volume,src={volume},dst=/data", args.image)
            deadline = time.monotonic() + 45
            while True:
                try:
                    status, body = request(port, "/healthz")
                    if status == 200 and json.loads(body) == {"status": "ok"}:
                        break
                except (OSError, http.client.HTTPException):
                    pass
                if time.monotonic() >= deadline:
                    raise RuntimeError("Container failed to become ready: " + docker("logs", name))
                time.sleep(0.25)
            status, html = request(port, "/")
            assert status == 200 and "ZTB" in html
            token = re.search(r'<meta name="local-token" content="([^"]+)"', html).group(1)
            for asset in ("/app.js", "/style.css", "/rollout.js", "/rollout.css", "/projects.js", "/interface-picker.js", "/diagrams.js"):
                assert request(port, asset)[0] == 200, asset
            status, body = request(port, '/api/csv-templates', {}, token=token)
            assert status == 200
            with zipfile.ZipFile(io.BytesIO(base64.b64decode(json.loads(body)['content']))) as kit:
                uploads = [{'name': n, 'content': kit.read(n).decode()} for n in kit.namelist() if n.endswith('.csv')]
                assert 'READ-ME-FIRST.txt' in kit.namelist()
            status, body = request(port, '/api/import', {'files': uploads}, token=token)
            assert status == 200
            assert len(json.loads(body)['batch']) == 2
            assert all(s['fields']['post'] == '0' for s in json.loads(body)['batch'])
            status, body = request(port, '/api/csv-templates', {'mode': 'ha'}, token=token)
            assert status == 200
            with zipfile.ZipFile(io.BytesIO(base64.b64decode(json.loads(body)['content']))) as kit:
                uploads = [{'name': n, 'content': kit.read(n).decode()} for n in kit.namelist() if n.endswith('.csv')]
            status, body = request(port, '/api/import', {'files': uploads}, token=token)
            assert status == 200
            ha_batch = json.loads(body)['batch']
            assert len(ha_batch) == 1 and ha_batch[0]['fields']['post'] == '0'
            assert ha_batch[0]['fields']['gateway_name_b']
            assert [v['gateway_target'] for v in ha_batch[0]['vlans']] == ['all', 'a', 'b']
            assert request(port, '/api/csv-templates', {'mode': 'unknown'}, token=token)[0] == 400
            status, body = request(port, '/api/import', {'files': [
                {'name': 'sites.csv', 'content': 'site_name,api_key\nBranch,SYNTHETIC-PRIVATE-VALUE\n'}]}, token=token)
            assert status == 400 and 'credential fields' in body and 'SYNTHETIC-PRIVATE-VALUE' not in body
            ha_batch[0]['fields'].update(post='1', api_key='SYNTHETIC-PRIVATE-VALUE')
            ha_batch[0]['vlans'][0]['client_secret'] = 'SYNTHETIC-VLAN-PRIVATE-VALUE'
            status, body = request(port, '/api/export', {'batch': ha_batch}, token=token)
            assert status == 200
            with zipfile.ZipFile(io.BytesIO(base64.b64decode(json.loads(body)['content']))) as bundle:
                assert all(b'SYNTHETIC-' not in bundle.read(n) for n in bundle.namelist())
            assert request(port, "/healthz", host=f"untrusted.invalid:{port}")[0] == 403
            assert request(port, "/api/projects/list", {}, token="wrong")[0] == 403
            assert request(port, "/api/projects/list", {}, token=token, origin="https://untrusted.invalid")[0] == 403
            assert request(port, "/api/connections", {}, token=token)[0] == 200
            docker("exec", name, "python", "/app/docker/healthcheck.py")
            logo_content = docker("exec", name, "python", "-c",
                "from PIL import Image; import io,base64; "
                "b=io.BytesIO(); Image.new('RGB',(180,60),'navy').save(b,format='JPEG'); "
                "print(base64.b64encode(b.getvalue()).decode())")
            status, body = request(port, '/api/diagrams/logo',
                {'logo': {'content': logo_content}}, token=token)
            assert status == 200
            logo = json.loads(body)['logo']
            docker("exec", name, "python", "-c",
                   "import os; from pathlib import Path; assert os.getuid() == 10001; "
                   "assert not Path('/app/.env').exists(); assert not Path('/app/.git').exists(); "
                   "assert not Path('/app/out').exists(); assert not Path('/app/sites.csv').exists(); "
                   "assert not Path('/app/tests').exists(); assert not os.access('/app/app.py', os.W_OK)")
            if generation == 0:
                workspace = {"version": 1, "batch": [], "current": -1, "active_tab": "site", "diagram_logo": logo}
                status, _ = request(port, "/api/projects/create",
                    {"id": "a" * 32, "name": "Docker persistence check", "workspace": workspace}, token=token)
                assert status == 200
                docker('exec',name,'python','-c',
                    "from site_diagrams import planned; from diagram_store import DiagramStore; "
                    "from deployment_engine import SiteResult, BatchResult; from run_report import reserve_report; "
                    "model=planned({'site_name':'Container diagram','gateway_name':'GW','wan_interface_name':'ge5'},[]); "
                    "result=BatchResult([SiteResult('Container diagram','success',{'Site':True},diagram=model)]); "
                    "DiagramStore('/data/runs').save(reserve_report('/data/runs'),result,'a'*32)")
                # Exercise writable reports and catalog selection without vendor
                # traffic, tenant credentials, or deployment requests.
                saved_catalog = docker("exec", name, "python", "-c",
                    "import json; from datetime import datetime, timezone; "
                    "from ucaas_catalog import load_catalog; from runtime_paths import catalog_path, data_directory; "
                    "from run_report import reserve_report; "
                    "data=load_catalog(); data['retrieved_at']=datetime.now(timezone.utc).isoformat(); "
                    "p=catalog_path(); p.parent.mkdir(parents=True, exist_ok=True); p.write_text(json.dumps(data)); "
                    "reserve_report(data_directory()/'runs'); print(data['retrieved_at'])")
                docker("exec", name, "python", "-c",
                    "import multiprocessing as mp; c=mp.get_context('spawn'); q=c.Queue(); "
                    "p=c.Process(target=q.put, args=('worker-ok',)); p.start(); "
                    "assert q.get(timeout=10)=='worker-ok'; p.join(10); assert p.exitcode==0; q.close()")
                old_token = token
            else:
                assert token != old_token
                assert request(port, "/api/projects/list", {}, token=old_token)[0] == 403
                status, body = request(port, "/api/projects/load", {"id": "a" * 32}, token=token)
                assert status == 200 and json.loads(body)["name"] == "Docker persistence check"
                assert json.loads(body)['workspace']['diagram_logo'] == logo
                status, body = request(port, "/api/ucaas/catalog", {}, token=token)
                assert status == 200 and json.loads(body)["retrieved_at"] == saved_catalog
                docker("exec", name, "python", "-c",
                    "from pathlib import Path; assert list(Path('/data/runs').glob('*.json'))")
            status,body=request(port,'/api/diagrams/list',{'project_id':'a'*32},token=token)
            assert status==200 and len(json.loads(body)['runs'])==1
            run=json.loads(body)['runs'][0]
            for format in ('svg','vsdx','png','zip'):
                status,body=request(port,'/api/diagrams/download',{'project_id':'a'*32,'run':run['run'],'site':'0001','format':format},token=token)
                assert status==200 and json.loads(body)['content']
                raw=base64.b64decode(json.loads(body)['content'])
                if format=='png':assert raw.startswith(b'\x89PNG\r\n\x1a\n') and json.loads(body)['mime']=='image/png'
                if format=='zip':
                    with zipfile.ZipFile(io.BytesIO(raw)) as z:
                        assert {Path(n).suffix for n in z.namelist()}=={'.png','.svg','.vsdx','.txt'}
            for format in ('svg','png'):
                status,body=request(port,'/api/diagrams/preview',{'format':format,'diagram_logo':logo,'batch':[{'fields':{'site_name':'Preview','gateway_name':'GW','wan_interface_name':'ge5'},'vlans':[]}]},token=token)
                assert status==200 and json.loads(body)['mime']==('image/png' if format=='png' else 'image/svg+xml')
                if format == 'svg':assert b'Customer logo' in base64.b64decode(json.loads(body)['content'])
            docker("stop", "--timeout", "30", name)
            assert docker("inspect", "--format", "{{.State.ExitCode}}", name) == "0"
            docker("rm", name)
        # Run the actual app and run_worker with a fake deployment that waits
        # for release. This exercises Docker's SIGINT forwarding while the
        # worker has an active report and more progress than the queue can hold.
        # The fixture is mounted only in this isolated test, never in the image.
        docker("run", *platform_args, "--detach", "--name", name, "--init", "--network", "none",
               "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
               "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m,mode=1777",
               "--env", "PYTHONPATH=/app", "--mount", f"type=volume,src={volume},dst=/data",
               "--mount", f"type=bind,src={ROOT/'tests/shutdown_fixture.py'},dst=/tmp/shutdown_fixture.py,readonly",
               "--entrypoint", "python", args.image, "/tmp/shutdown_fixture.py",
               "--no-browser", "--data-dir", "/data/shutdown-test")
        deadline = time.monotonic()+20
        while docker("exec", name, "python", "-c",
                     "from pathlib import Path; print(Path('/data/shutdown-test/worker-ready').exists())") != "True":
            if time.monotonic() > deadline:
                raise RuntimeError("Active worker did not start: " + docker("logs", name))
            time.sleep(.1)
        docker("exec", "--detach", name, "python", "-c",
               "import time; from pathlib import Path; time.sleep(2); "
               "assert Path('/data/shutdown-test/server-closing').exists(); "
               "Path('/data/shutdown-test/release').touch()")
        docker("stop", "--timeout", "30", name)
        assert docker("inspect", "--format", "{{.State.ExitCode}}", name) == "0", docker("logs", name)
        docker("rm", name)
        docker("run", *platform_args, "--rm", "--network", "none", "--read-only",
               "--mount", f"type=volume,src={volume},dst=/data,readonly", "--entrypoint", "python", args.image,
               "-c", "import json; from pathlib import Path; p=Path('/data/shutdown-test'); "
               "assert json.loads((p/'server-finished').read_text())['state']=='completed'; "
               "report,=list((p/'runs').glob('*.json')); "
               "assert json.loads(report.read_text())['sites'][0]['status']=='success'")
        # Check the documented tar backup/restore method on a fresh volume,
        # without taking either the native or user's Docker workspace offline.
        archive = archive_command("run", *platform_args, "--rm", "--network", "none", "--read-only",
                                  "--mount", f"type=volume,src={volume},dst=/data,readonly",
                                  "--entrypoint", "tar", args.image, "-C", "/data", "-czf", "-", ".")
        docker("volume", "create", restore_volume)
        restore_created = True
        archive_command("run", *platform_args, "--rm", "--interactive", "--network", "none", "--read-only",
                        "--mount", f"type=volume,src={restore_volume},dst=/data",
                        "--entrypoint", "tar", args.image, "-C", "/data", "-xzf", "-", content=archive)
        docker("run", *platform_args, "--rm", "--network", "none", "--read-only",
               "--mount", f"type=volume,src={restore_volume},dst=/data", "--entrypoint", "python", args.image,
               "-c", "from pathlib import Path; from app import normalize_batch; from project_store import ProjectStore; "
               "from ucaas_catalog import load_catalog; import sys; "
               "assert ProjectStore('/data/projects', normalize_batch).load('a'*32)['name']=='Docker persistence check'; "
               "assert list(Path('/data/runs').glob('*.json')); assert list(Path('/data/runs/diagrams').glob('*/0001.vsdx')); assert load_catalog()['retrieved_at']==sys.argv[1]",
               saved_catalog)
        print("PASS: startup, assets, local access checks, non-root/read-only runtime, worker spawn, "
              "project/report/catalog persistence, token rotation, idle and active-worker graceful shutdown, and backup/restore.", flush=True)
    finally:
        subprocess.run([DOCKER, "rm", "--force", name], env=DOCKER_ENV, capture_output=True)
        if created:
            subprocess.run([DOCKER, "volume", "rm", volume], env=DOCKER_ENV, capture_output=True)
        if restore_created:
            subprocess.run([DOCKER, "volume", "rm", restore_volume], env=DOCKER_ENV, capture_output=True)


if __name__ == "__main__":
    main()

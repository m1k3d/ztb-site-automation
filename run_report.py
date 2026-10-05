"""Operator summaries without payloads, credentials, or raw API responses."""
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
from ucaas_breakout import MESSAGES as UCAAS_MESSAGES
from dns_policy import MESSAGES as DNS_MESSAGES


DIAGNOSTIC_MESSAGES = {
    ("Site", "site_timeout"): "The site creation request timed out. Its outcome is unknown. Check the ZTB site list before recovery; the request was not automatically repeated after the timeout.",
    ("Site", "site_connection_failed"): "The connection failed during site creation. Its outcome is unknown. Check connectivity and the ZTB site list before recovery.",
    ("Site", "site_creation_unconfirmed"): "Site creation could not be confirmed. Inspect the ZTB site list before recovery; do not repeat template cloning.",
    ("Existing site check", "inventory_unreadable"): "The site inventory could not be read completely. No changes were made to this site. Check inventory access and retry after inventory changes settle. Repeated or inconsistent pages are blocked; the supported inventory limit is 10,000 sites.",
    **{("UCaaS local breakout", code): message for code, message in UCAAS_MESSAGES.items()},
    **{("DNS policy", code): message for code, message in DNS_MESSAGES.items()},
    ("Template clone", "template_name_exists"): "A template with this name already exists. No clone or site was created. Choose a new name, or explicitly select the existing template.",
    ("Template clone", "template_source_changed"): "The source template changed after preview. No clone or site was created. Preview the rollout again.",
    ("Template clone", "template_check_failed"): "Could not recheck the template inventory or source configuration. No clone or site was created. Restore template read access before retrying.",
    ("Template clone", "template_creation_unconfirmed"): "Template creation failed or its outcome is uncertain. Site creation was not attempted. Inspect the named template before retrying; the clone request will not be repeated automatically.",
    ("Template clone", "template_verification_failed"): "The clone request was accepted, but the new template could not be uniquely verified. Site creation was not attempted. Inspect the named template before retrying.",
    ("VLANs", "loopback_unavailable"): "Requested management interface lo0 is missing or ambiguous on a target gateway. No VLANs were written. Verify gateway activation and loopback binding before recovering VLAN configuration; do not repeat site creation.",
    ("VLANs", "loopback_binding_unverified"): "Management network accepted for staging; lo0 binding is unverified. Check after activation. Other VLANs were attempted; inspect their configuration before recovery. Do not repeat site creation.",
    ("VLANs", "loopback_submission_failed"): "Management network submission failed or its outcome is uncertain. Other VLANs were attempted. Inspect existing networks before recovery; do not repeat site creation.",
    ("Loopback binding", "loopback_binding_unverified"): "Management network accepted for staging; lo0 binding is unverified. Check after activation. This warning does not block disabled ZPA LAN segment staging when VLAN configuration and App Connector provisioning succeed.",
    ("ZPA segments", "prerequisite_failed"): "Disabled application segment was not created because VLAN configuration or App Connector provisioning failed. Inspect those stages before recovery.",
}


def public_diagnostics(site):
    # Only known codes become messages; never serialize API bodies or exception text.
    messages = {stage: DIAGNOSTIC_MESSAGES[(stage, code)] for stage, code in site.diagnostics.items()
                if isinstance(code, str) and (stage, code) in DIAGNOSTIC_MESSAGES}
    code = site.diagnostics.get('Site')
    if isinstance(code, str) and re.fullmatch(r'site_http_[1-5][0-9]{2}', code):
        status = int(code.rsplit('_', 1)[1])
        hint = {
            400: 'Check the site settings and the selected template’s deployment requirements.',
            401: 'Reconnect to ZTB and check authentication.',
            403: 'Check that the ZTB account has permission to deploy sites, not only create templates.',
            404: 'Check that the verified template is still available in this tenant.',
            409: 'Check for conflicting site, gateway, or location names.',
            422: 'Check the site settings and the selected template’s deployment requirements.',
            429: 'The API rate limit was reached; allow it to clear before recovery.',
        }.get(status, 'Check the ZTB service and site configuration.')
        messages['Site'] = f'Site creation returned HTTP {status}. {hint} Inspect the site list before recovery; do not repeat template cloning.'
    return messages


def reserve_report(directory='out/runs'):
    folder = Path(directory)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    path = folder / f'{stamp}-{uuid4().hex[:8]}.json'
    with path.open('x') as stream:
        json.dump({'status': 'started', 'next_action': 'If this report stays started, inspect resources before retrying.'}, stream)
    return path


def save_report(path, result, *, dry_run=False, workspace=None):
    actions = {
        'already_exists': 'Existing site left unchanged. Inspect incomplete stages before recovery.',
        'lookup_failed': 'Review the inventory diagnostic before retrying; this site was not changed.',
        'partial': 'Inspect failed stages. Do not repeat site creation.',
        'failed': 'Inspect the site before retrying; creation outcome may be uncertain.',
        'success': 'No further deployment action required.',
        'preview': 'Review the preview before deployment.',
        'template_failed': 'Site creation was not attempted. Inspect the template and clone stage before retrying.',
        'template_only': 'The new template exists, but site creation failed or is uncertain. Inspect the tenant; do not clone again. Recover using the verified template ID after checking the site.',
    }
    sites = [dict(name=s.name, status=s.status, stages=s.stages,
                  next_action=actions.get(s.status, 'Inspect the site before retrying.')) for s in result.sites]
    for site, outcome in zip(sites, result.sites):
        details = public_diagnostics(outcome)
        if details:
            site['diagnostics'] = details
        if outcome.template:
            site['template'] = {key: outcome.template[key] for key in ('source_id', 'source_name', 'name', 'id', 'status') if key in outcome.template}
        if outcome.ucaas:
            site['ucaas'] = outcome.ucaas
        if outcome.dns:
            site['dns'] = outcome.dns
        if outcome.additional_wans:
            site['additional_wans'] = outcome.additional_wans
        if outcome.artifacts:
            site['diagrams'] = outcome.artifacts
        if outcome.diagram.get('error'):
            site['diagram_warning'] = outcome.diagram['error']
        if outcome.site_id:
            site['site_id'] = outcome.site_id
        if outcome.gateway_ids:
            site['gateway_ids'] = outcome.gateway_ids
        if outcome.zpa_segments:
            site['zpa_segments'] = outcome.zpa_segments
            if outcome.zpa_segments.get('status') == 'staged_disabled':
                site['next_action'] = ('Review ZPA destinations and access policy before enabling the staged application segment.'
                                       if outcome.status == 'success' else site['next_action'] + ' ZPA application segment remains disabled.')
    report = dict(mode='preview' if dry_run else 'deployment',
                  finished_at=datetime.now(timezone.utc).isoformat(), exit_code=result.exit_code,
                  preflight_issue_count=len(result.issues), sites=sites)
    if workspace:
        report['workspace'] = {key: workspace[key] for key in ('project_id', 'tenant')}
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(report, indent=2)+'\n')
    temporary.replace(path)
    lines = [f"{'Preview' if dry_run else 'Deployment'}: {'completed' if not result.exit_code else 'stopped or incomplete'}"]
    if result.issues:
        lines.append('Preflight failed. Correct the reported input or reference errors before retrying.')
    for site in sites:
        failed = ', '.join(k for k,v in site['stages'].items() if not v)
        lines.append(f"{site['name']}: {site['status']}" + (f' — failed stages: {failed}' if failed else ''))
        for stage, message in site.get('diagnostics', {}).items():
            lines.append(f'{stage}: {message}')
        if site.get('template'):
            template = site['template']
            lines.append(f"Template: {template['name']} — {template['status']}; source: {template['source_name']}; new ID: {template.get('id', 'not verified')}")
        if site.get('zpa_segments'):
            staged = site['zpa_segments']
            lines.append(f"ZPA LAN: {staged['application_name']} — {staged['status']}; {len(staged['subnets'])} subnet(s); requested disabled, ICMP off.")
        if site.get('ucaas'):
            breakout = site['ucaas']
            lines.append(f"UCaaS local breakout: {breakout['status']}; primary {breakout['primary']}, secondary {breakout['secondary']}; Best; {len(breakout.get('rules', []))} port-specific rules; IPv4 address objects only.")
        if site.get('dns'):
            dns = site['dns']
            lines.append(f"DNS policy: {dns['status']}; private domains {', '.join(dns['domains'])}; order: " + ' → '.join(dns['order']))
        lines.append(site['next_action'])
    path.with_suffix('.txt').write_text('\n'.join(lines)+'\n')

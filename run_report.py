"""Operator summaries without payloads, credentials, or raw API responses."""
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


def reserve_report(directory='out/runs'):
    folder = Path(directory)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    path = folder / f'{stamp}-{uuid4().hex[:8]}.json'
    with path.open('x') as stream:
        json.dump({'status': 'started', 'next_action': 'If this report stays started, inspect resources before retrying.'}, stream)
    return path


def save_report(path, result, *, dry_run=False):
    actions = {
        'already_exists': 'Existing site left unchanged. Inspect incomplete stages before recovery.',
        'lookup_failed': 'Restore inventory access and retry; this site was not changed.',
        'partial': 'Inspect failed stages. Do not repeat site creation.',
        'failed': 'Inspect the site before retrying; creation outcome may be uncertain.',
        'success': 'No further deployment action required.',
        'preview': 'Review the preview before deployment.',
    }
    sites = [dict(name=s.name, status=s.status, stages=s.stages,
                  next_action=actions.get(s.status, 'Inspect the site before retrying.')) for s in result.sites]
    report = dict(mode='preview' if dry_run else 'deployment',
                  finished_at=datetime.now(timezone.utc).isoformat(), exit_code=result.exit_code,
                  preflight_issue_count=len(result.issues), sites=sites)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(report, indent=2)+'\n')
    temporary.replace(path)
    lines = [f"{'Preview' if dry_run else 'Deployment'}: {'completed' if not result.exit_code else 'stopped or incomplete'}"]
    if result.issues:
        lines.append('Preflight failed. Correct the reported input or reference errors before retrying.')
    for site in sites:
        failed = ', '.join(k for k,v in site['stages'].items() if not v)
        lines.append(f"{site['name']}: {site['status']}" + (f' — failed stages: {failed}' if failed else ''))
        lines.append(site['next_action'])
    path.with_suffix('.txt').write_text('\n'.join(lines)+'\n')

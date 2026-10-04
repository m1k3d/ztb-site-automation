"""Offline branch readiness and recorded results, never a tenant inventory cache."""
from copy import deepcopy
import json
from pathlib import Path
import re


def readiness(batch, validate):
    # Validate every draft, including unselected branches, without altering selection.
    candidates = deepcopy(batch)
    for site in candidates:
        site['fields']['post'] = '0' if site.get('reference') else '1'
    checked = validate(candidates)
    result = [{'issues': [], 'reference': bool(site.get('reference'))} for site in batch]
    for issue in checked['issues']:
        match = re.search(r'Workspace row (\d+) VLANs', issue['source'])
        index = int(match[1] if match else issue['row']) - 2
        targets = [index] if 0 <= index < len(result) else range(len(result))
        for target in targets:
            result[target]['issues'].append(issue)
    for item, site in zip(result, batch):
        item['ready'] = not item['reference'] and not item['issues']
        item['ha'] = bool(site.get('ha_enabled') or site['fields'].get('gateway_name_b'))
    return result


def recorded_results(report_dir, project_id, tenant):
    """Use only final reports for this project and connected destination."""
    if not project_id or not tenant:
        return []
    results = {}
    for path in sorted(Path(report_dir).glob('*.json'), reverse=True):
        try:
            report = json.loads(path.read_text())
            context = report.get('workspace', {})
            if (report.get('mode') != 'deployment' or context.get('project_id') != project_id
                    or context.get('tenant') != tenant):
                continue
            for site in report.get('sites', []):
                key = site['name'].strip().casefold()
                results.setdefault(key, {k: site[k] for k in ('name', 'status', 'next_action') if k in site}
                                   | {'report': path.name, 'finished_at': report.get('finished_at', '')})
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return list(results.values())

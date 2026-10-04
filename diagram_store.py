"""Persistent, project-scoped diagram artifacts; downloads never contact the tenant."""
import base64
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import re
import zipfile

import diagram_render


def atomic(path, value):
    path=Path(path)
    temporary=path.with_suffix(path.suffix+'.tmp')
    temporary.write_bytes(value if isinstance(value,bytes) else json.dumps(value,ensure_ascii=False,indent=2).encode())
    temporary.replace(path)


class DiagramStore:
    def __init__(self, report_dir):
        self.root=Path(report_dir)/'diagrams'

    def save(self, report_path, result, project_id=''):
        run=Path(report_path).stem
        folder=self.root/run
        entries=[]
        for index,site in enumerate(result.sites):
            if not site.diagram or site.diagram.get('error'):
                continue
            folder.mkdir(parents=True,exist_ok=True)
            identifier=f'{index+1:04d}'
            entry=dict(id=identifier,name=site.name,status=site.status,formats=[],warning='')
            atomic(folder/(identifier+'.json'),site.diagram)
            try:
                self._render(folder,entry)
            except Exception:
                entry['warning']='Diagram export needs regeneration. Configuration snapshot is saved.'
            entries.append(entry)
            site.artifacts=dict(run=run,site=identifier,formats=entry['formats'],warning=entry['warning'])
        if entries:
            manifest=dict(version=1,run=run,project_id=project_id,finished_at=datetime.now(timezone.utc).isoformat(),sites=entries)
            atomic(folder/'manifest.json',manifest)
        return entries

    def _render(self, folder, entry):
        model=json.loads((folder/(entry['id']+'.json')).read_text())
        for fmt,renderer in [('svg',diagram_render.svg),('vsdx',diagram_render.vsdx),('png',diagram_render.png)]:
            atomic(folder/(entry['id']+'.'+fmt),renderer(model))
            if fmt not in entry['formats']:entry['formats'].append(fmt)
        entry['warning']=''

    def manifest(self, run, project_id):
        if not isinstance(run,str) or not re.fullmatch(r'\d{8}T\d{6}Z-[a-f0-9]{8}',run):
            raise ValueError('Unknown diagram run')
        try:manifest=json.loads((self.root/run/'manifest.json').read_text())
        except (OSError,ValueError):raise ValueError('Saved diagrams are unavailable for this run') from None
        if not project_id or manifest.get('project_id')!=project_id:
            raise ValueError('This diagram belongs to another rollout project')
        return manifest

    def list(self, project_id):
        result=[]
        for path in self.root.glob('*/manifest.json'):
            try:
                manifest=json.loads(path.read_text())
                if project_id and manifest.get('project_id')==project_id:result.append(manifest)
            except (OSError,ValueError):continue
        return sorted(result,key=lambda m:m['finished_at'],reverse=True)

    def download(self, run, project_id, site=None, format='svg'):
        manifest=self.manifest(run,project_id)
        if format not in ('svg','vsdx','png','zip'):raise ValueError('Choose PNG, SVG, editable Visio, or a ZIP package')
        entries=manifest['sites']
        if site is not None:
            entries=[e for e in entries if e['id']==site]
            if not entries:raise ValueError('Unknown site diagram')
        def filename(entry,fmt):
            slug=re.sub(r'[^a-zA-Z0-9_-]+','-',entry['name']).strip('-')[:80] or 'site'
            return f"{entry['id']}-{slug}.{fmt}"
        if format=='zip':
            buffer=io.BytesIO()
            with zipfile.ZipFile(buffer,'w',zipfile.ZIP_DEFLATED) as archive:
                for entry in entries:
                    for fmt in entry['formats']:
                        archive.writestr(filename(entry,fmt),(self.root/run/(entry['id']+'.'+fmt)).read_bytes())
                archive.writestr('README.txt','Site configuration diagrams.\nOpen PNG for a quick preview, SVG for a scalable image, or VSDX for editable shapes and connectors.\nPNG is a preview image; use SVG or VSDX for large prints or very dense sites.\nPhysical cabling follows the documented design. Configured service paths do not establish live tunnel health.\nOlder saved diagrams may lack PNG; use Update diagram layout in the app to add it.\n')
            name=filename(entries[0],'zip') if site is not None else run+'-diagrams.zip'
            return dict(filename=name,content=base64.b64encode(buffer.getvalue()).decode(),mime='application/zip')
        entry=next((e for e in manifest['sites'] if e['id']==site),None)
        if not entry or format not in entry['formats']:raise ValueError('This diagram export is unavailable; regenerate the saved snapshot')
        raw=(self.root/run/(entry['id']+'.'+format)).read_bytes()
        return dict(filename=filename(entry,format),content=base64.b64encode(raw).decode(),mime={'svg':'image/svg+xml','png':'image/png','vsdx':'application/vnd.ms-visio.drawing'}[format])

    def regenerate(self, run, project_id, site):
        manifest=self.manifest(run,project_id)
        entry=next((e for e in manifest['sites'] if e['id']==site),None)
        if not entry:raise ValueError('Unknown site diagram')
        self._render(self.root/run,entry)
        atomic(self.root/run/'manifest.json',manifest)
        return manifest

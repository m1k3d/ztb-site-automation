import base64
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
from xml.etree import ElementTree as ET
import zipfile

from PIL import Image
from app import Handler, normalize_batch, export_batch, import_batch
from deployment_engine import SiteResult, BatchResult
from diagram_branding import logo
from diagram_render import scene, svg, png, vsdx, appliance_photo
from diagram_store import DiagramStore
from project_store import ProjectStore
from run_report import reserve_report
from site_diagrams import planned
from test_app import sample_batch
from test_diagrams import example


def uploaded(format='JPEG',size=(300,100),mode='RGB'):
    picture=Image.new(mode,size,'white');data=io.BytesIO()
    exif=Image.Exif();exif[270]='private metadata';exif[274]=1
    picture.save(data,format=format,exif=exif)
    return dict(content=base64.b64encode(data.getvalue()).decode())


class BrandingTests(unittest.TestCase):
    def test_jpeg_png_cmyk_orientation_and_metadata_are_normalized(self):
        for format,mode in [('JPEG','RGB'),('JPEG','CMYK'),('PNG','RGBA')]:
            result=logo(uploaded(format,(1200,400),mode))
            self.assertEqual((result['width'],result['height']),(600,200))
            data=base64.b64decode(result['content']);self.assertNotIn(b'private metadata',data)
            with Image.open(io.BytesIO(data)) as picture:
                self.assertEqual(picture.format,'PNG');self.assertFalse(picture.getexif())
            self.assertEqual(logo(result),result)
        picture=Image.new('RGB',(200,100));exif=Image.Exif();exif[274]=6
        data=io.BytesIO();picture.save(data,format='JPEG',exif=exif)
        result=logo({'content':base64.b64encode(data.getvalue()).decode()})
        self.assertEqual((result['width'],result['height']),(100,200))

    def test_invalid_or_excessive_uploads_are_rejected(self):
        for value in ({},{'content':'%%%bad'},{'content':base64.b64encode(b'<svg><script/></svg>').decode()},
                      uploaded('GIF'),uploaded(size=(4097,2)),{'content':'A'*(3*1024*1024)}):
            with self.subTest(value=str(value)[:40]),self.assertRaises(ValueError):logo(value)
        self.assertIsNone(logo(None))

    def test_logo_is_project_scoped_saved_once_and_reset_is_default(self):
        batch=sample_batch();brand=logo(uploaded())
        workspace=dict(version=1,batch=batch*10,current=0,active_tab='site',diagram_logo=brand)
        with tempfile.TemporaryDirectory() as folder:
            store=ProjectStore(folder,normalize_batch)
            store.create('a'*32,'Customer A',workspace)
            store.create('b'*32,'Customer B',{**workspace,'diagram_logo':None})
            loaded=ProjectStore(folder,normalize_batch).load('a'*32)['workspace']
            self.assertEqual(loaded['diagram_logo'],brand)
            self.assertEqual(json.dumps(loaded).count(brand['content']),1)
            self.assertNotIn('diagram_logo',store.load('b'*32)['workspace'])
        row=normalize_batch(batch,brand)[0]
        self.assertEqual(json.loads(row['diagram_options_json'])['logo'],brand)
        batch[0]['fields']['diagram_options_json']=row['diagram_options_json']
        reset=normalize_batch(batch,None)[0]
        self.assertNotIn('logo',json.loads(reset['diagram_options_json']))

    def test_all_exports_embed_logo_and_keep_appliance_and_device_branding(self):
        model=planned(*example());original=scene(model)
        model['options']['logo']=logo(uploaded())
        rendered=scene(model)
        self.assertEqual(original.edges,rendered.edges)
        self.assertEqual([i for i in original.items if i['group']],[i for i in rendered.items if i['group']])
        pictures=[i for i in rendered.items if i['kind']=='image']
        self.assertEqual(len(pictures),2)
        customer=next(i for i in pictures if i['name']=='Customer logo')
        self.assertLessEqual(customer['w'],240);self.assertLessEqual(customer['h'],72)
        self.assertAlmostEqual(customer['w']/customer['h'],3)
        self.assertEqual(svg(model).count(b'data:image/png;base64,'),2)
        self.assertTrue(png(model).startswith(b'\x89PNG'))
        with zipfile.ZipFile(io.BytesIO(vsdx(model))) as archive:
            self.assertEqual(archive.read('visio/media/appliance.png'),appliance_photo('ZT800')[0])
            self.assertEqual(archive.read('visio/media/customer-logo.png'),base64.b64decode(model['options']['logo']['content']))
            rels=ET.fromstring(archive.read('visio/pages/_rels/page1.xml.rels'))
            self.assertEqual(len(rels),2);self.assertEqual(len({r.get('Id') for r in rels}),2)

    def test_csv_and_saved_snapshot_preserve_branding(self):
        brand=logo(uploaded());export=export_batch(sample_batch(),brand)
        with zipfile.ZipFile(io.BytesIO(base64.b64decode(export['content']))) as archive:
            inputs=[dict(name=name,content=archive.read(name).decode()) for name in archive.namelist() if name.endswith('.csv')]
            imported=import_batch(inputs)
        self.assertEqual(json.loads(imported[0]['fields']['diagram_options_json'])['logo'],brand)
        model=planned(*example());model['options']['logo']=brand
        with tempfile.TemporaryDirectory() as folder:
            store=DiagramStore(folder);path=reserve_report(folder)
            store.save(path,BatchResult([SiteResult(model['name'],'preview',{},diagram=model)]),'a'*32)
            model['options'].pop('logo')
            store.regenerate(path.stem,'a'*32,'0001')
            file=store.download(path.stem,'a'*32,'0001','svg')
            self.assertIn(b'Customer logo',base64.b64decode(file['content']))

    def test_upload_preview_and_rollout_receive_same_logo_without_tenant_upload(self):
        def request(path,payload):
            handler=object.__new__(Handler);handler.path=path
            body=json.dumps(payload).encode()
            handler.headers={'Host':'127.0.0.1:8765','Origin':'http://127.0.0.1:8765','X-Local-Token':'test','Content-Length':str(len(body)),'Content-Type':'application/json'}
            handler.server=Mock(token='test',server_address=('127.0.0.1',8765))
            handler.server.deployment.project_id='a'*32
            handler.server.references.interfaces_cache={}
            handler.reply=Mock();handler.rfile=io.BytesIO(body);handler.do_POST()
            self.assertEqual(handler.server.references.method_calls,[])
            return handler.reply.call_args.args,handler.server
        (status,result),_=request('/api/diagrams/logo',{'logo':uploaded()})
        self.assertEqual(status,200);brand=result['logo']
        (status,result),_=request('/api/diagrams/preview',{'batch':sample_batch(),'diagram_logo':brand})
        self.assertEqual(status,200);self.assertIn(b'Customer logo',base64.b64decode(result['content']))
        for path,method in [('/api/deployment/preview','preview'),('/api/deployment/start','deploy')]:
            payload={'batch':sample_batch(),'diagram_logo':brand,'project_id':'a'*32}
            (status,_),server=request(path,payload)
            self.assertEqual(status,200)
            args=getattr(server.deployment,method).call_args.args
            row=args[0 if method=='preview' else 1][0]
            self.assertEqual(json.loads(row['diagram_options_json'])['logo'],brand)

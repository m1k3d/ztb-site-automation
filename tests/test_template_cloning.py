"""Template-before-site ordering and failure behavior, without tenant writes."""
from copy import deepcopy
import json
from pathlib import Path
from queue import SimpleQueue
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests

from app import export_batch, import_batch, validate_batch
from automation_config import Settings
from deployment_engine import DeploymentEngine
from input_validation import validate_rows
from run_report import save_report
from template_cloning import read_rows
from ui_deployment import plan_digest, public_sites, run_worker


def row(name="Branch", **overrides):
    return {**dict(site_name=name, gateway_name=name+'-GW', template_name="Reference",
                wan_interface_name="ge5", template_mode="clone", location_type="none",
                post="1", vlans=[]), **overrides}


class TemplateCloningTests(unittest.TestCase):
    def setUp(self):
        self.no_http = patch.object(requests.Session, 'request', side_effect=AssertionError('Unexpected HTTP'))
        self.http = self.no_http.start()
        self.addCleanup(self.no_http.stop)
        self.config = Settings(ztb_api_base='https://example.invalid', bearer='offline')
        self.engine = DeploymentEngine(self.config, emit=lambda *_: None)
        self.addCleanup(self.engine.client.close)
        self.templates = {'source-id': dict(id='source-id', name='Reference', platform_type='vm', deployment_type='standalone',
                                          private_dns='', dhcp_service='server', nat_enabled=True, connect_to_hub=False, sites_count=2)}
        self.interfaces = [dict(id='port-1', name='ge5', gateway_id='Gateway-1', interface_type='wan')]
        self.events = []
        self.engine.get_json = Mock(side_effect=self.get)
        self.engine.post_json = Mock(side_effect=self.clone)
        self.engine.list_site_inventory = Mock(return_value=[])
        self.engine.site_exists = Mock(return_value=False)
        self.engine.create_site = Mock(side_effect=self.deploy)
        self.engine.resolve_gateway_ids_and_cluster = Mock(return_value=('gw-1', 1))
        sleeper = patch('template_cloning.time.sleep')
        sleeper.start();self.addCleanup(sleeper.stop)

    def tearDown(self):
        self.http.assert_not_called()

    def get(self, url, params=None, **kwargs):
        path = url.removeprefix(self.engine.API_V3)
        if path == '/templates':
            values = list(self.templates.values())
            if params.get('search'):
                values = [v for v in values if params['search'].casefold() in v['name'].casefold()]
            return {'count':len(values), 'result':deepcopy(values)}
        if path == '/templates/pbr/policies':return {'policies':None}
        if path.endswith('/interfaces'):return {'count':len(self.interfaces), 'result':deepcopy(self.interfaces)}
        if path.endswith(('/vlans','/policies')):return {'count':0, 'result':[]}
        return deepcopy(self.templates[path.rsplit('/',1)[-1]])

    def clone(self, url, payload, **kwargs):
        self.events.append(('clone', url, deepcopy(payload)))
        identifier = 'new-' + payload['name']
        self.templates[identifier] = {**self.templates['source-id'], 'id':identifier, 'name':payload['name'], 'sites_count':0}
        # Native contract has no template ID in its body.
        return Mock(status_code=200, json=lambda: {'token':'not-for-reporting'})

    def deploy(self, identifier, payload):
        self.events.append(('site', identifier, payload['name']))
        return True, 'created', 1

    def plan(self, rows=None):
        plan = self.engine.plan(validate_rows(rows or [row()]))
        self.assertFalse(plan.issues, plan.issues)
        return plan

    def test_preview_is_read_only_and_describes_order(self):
        plan = self.plan()
        result = self.engine.execute(plan, dry_run=True)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.sites[0].template['name'], 'Branch')
        self.assertEqual(result.sites[0].template['status'], 'planned')
        self.engine.post_json.assert_not_called()
        self.engine.create_site.assert_not_called()

    def test_each_clone_is_created_and_verified_before_its_site_uses_the_new_id(self):
        result = self.engine.execute(self.plan([row('One'),row('Two')]))
        self.assertEqual(result.exit_code, 0)
        self.assertEqual([e[0] for e in self.events], ['clone','site','clone','site'])
        self.assertTrue(self.events[0][1].endswith('/templates/source-id/clone'))
        self.assertEqual(self.events[0][2], {'name':'One'})
        self.assertEqual([e[1] for e in self.events if e[0]=='site'], ['new-One','new-Two'])
        self.assertEqual(self.templates['source-id']['name'], 'Reference')
        self.assertEqual([s.template['id'] for s in result.sites], ['new-One','new-Two'])

    def test_default_reuse_never_calls_clone(self):
        values=row();values.pop('template_mode')
        result=self.engine.execute(self.plan([values]))
        self.assertEqual(result.exit_code, 0)
        self.engine.post_json.assert_not_called()
        self.assertEqual(self.engine.create_site.call_args.args[0], 'source-id')

    def test_unknown_mode_and_duplicate_clone_names_block_whole_batch(self):
        for values in [[row(template_mode='typo')], [row('One',new_template_name='Shared'),row('Two',new_template_name='shared')], [row(new_template_name='Reference')]]:
            plan=self.engine.plan(validate_rows(values))
            self.assertTrue(plan.issues)
            self.assertEqual(self.engine.execute(plan).exit_code, 1)
        self.engine.post_json.assert_not_called();self.engine.create_site.assert_not_called()

    def test_existing_name_blocks_entire_plan_even_if_later_row(self):
        self.templates['existing']={**self.templates['source-id'],'id':'existing','name':'Two'}
        plan=self.engine.plan(validate_rows([row('One'),row('Two')]))
        self.assertTrue(plan.issues)
        self.engine.execute(plan)
        self.engine.post_json.assert_not_called();self.engine.create_site.assert_not_called()

    def test_existing_site_stops_before_cloning(self):
        plan=self.plan();self.engine.site_exists.return_value=True
        self.assertEqual(self.engine.execute(plan).sites[0].status,'already_exists')
        self.engine.post_json.assert_not_called();self.engine.create_site.assert_not_called()

    def test_failed_site_inventory_stops_before_cloning(self):
        plan=self.plan();self.engine.site_exists.side_effect=ValueError('unavailable')
        self.assertEqual(self.engine.execute(plan).sites[0].status,'lookup_failed')
        self.engine.post_json.assert_not_called();self.engine.create_site.assert_not_called()

    def test_name_collision_after_preview_cannot_be_reused(self):
        plan=self.plan()
        self.templates['other']={**self.templates['source-id'],'id':'other','name':'branch'}
        result=self.engine.execute(plan)
        self.assertEqual(result.sites[0].diagnostics['Template clone'],'template_name_exists')
        self.engine.post_json.assert_not_called();self.engine.create_site.assert_not_called()

    def test_source_interface_change_blocks_clone_after_preview(self):
        plan=self.plan();self.interfaces[0]['name']='ge6'
        result=self.engine.execute(plan)
        self.assertEqual(result.sites[0].diagnostics['Template clone'],'template_source_changed')
        self.engine.post_json.assert_not_called();self.engine.create_site.assert_not_called()

    def test_preview_digest_covers_source_configuration_but_not_site_usage(self):
        before=plan_digest(self.plan())
        self.templates['source-id']['sites_count']=200
        self.assertEqual(before,plan_digest(self.plan()))
        self.interfaces[0]['name']='ge6'
        self.assertNotEqual(before,plan_digest(self.plan()))

    def test_post_failure_or_timeout_never_retries_or_deploys_site(self):
        for failure in [requests.Timeout('unknown'),Mock(status_code=403),Mock(status_code=500)]:
            with self.subTest(failure=failure):
                plan=self.plan();self.engine.post_json.reset_mock()
                self.engine.post_json.side_effect=failure if isinstance(failure,Exception) else None
                self.engine.post_json.return_value=failure
                result=self.engine.execute(plan)
                self.assertEqual(result.sites[0].status,'template_failed')
                self.assertEqual(result.sites[0].diagnostics['Template clone'],'template_creation_unconfirmed')
                self.engine.post_json.assert_called_once();self.engine.create_site.assert_not_called()

    def test_accepted_but_invisible_clone_blocks_site_without_reposting(self):
        self.engine.post_json.side_effect=None;self.engine.post_json.return_value=Mock(status_code=200)
        result=self.engine.execute(self.plan())
        self.assertEqual(result.sites[0].diagnostics['Template clone'],'template_verification_failed')
        self.engine.post_json.assert_called_once();self.engine.create_site.assert_not_called()

    def test_wrong_clone_settings_or_ambiguous_name_blocks_site(self):
        for ambiguous in (False,True):
            with self.subTest(ambiguous=ambiguous):
                self.templates.pop('new-Branch',None);self.templates.pop('duplicate',None)
                plan=self.plan()
                def bad_clone(*args,**kwargs):
                    response=self.clone(*args,**kwargs)
                    if ambiguous:self.templates['duplicate']={**self.templates['new-Branch'],'id':'duplicate'}
                    else:self.templates['new-Branch']['platform_type']='wrong'
                    return response
                self.engine.post_json.side_effect=bad_clone
                result=self.engine.execute(plan)
                self.assertEqual(result.sites[0].status,'template_failed')
                self.engine.create_site.assert_not_called()

    def test_site_failure_preserves_created_template_id_and_reports_recovery(self):
        self.engine.create_site.side_effect=requests.Timeout('private raw error')
        result=self.engine.execute(self.plan())
        self.assertEqual(result.sites[0].status,'template_only')
        self.assertEqual(public_sites(result)[0]['template']['id'],'new-Branch')
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'run.json';save_report(path,result)
            saved=json.loads(path.read_text());summary=path.with_suffix('.txt').read_text()
        self.assertEqual(saved['sites'][0]['template']['id'],'new-Branch')
        self.assertIn('do not clone again',summary)
        self.assertNotIn('private raw error',summary)

    def test_review_and_export_preserve_clone_choice_and_names(self):
        import base64,io,zipfile
        values=row(new_template_name='Custom-Branch-Template');values.pop('vlans')
        batch=[{'fields':values,'vlans':[]}]
        review=validate_batch(batch)
        self.assertEqual(review['sites'][0]['template'],{'mode':'clone','source':'Reference','name':'Custom-Branch-Template'})
        exported=export_batch(batch)
        with zipfile.ZipFile(io.BytesIO(base64.b64decode(exported['content']))) as archive:
            imported=import_batch([{'name':name,'content':archive.read(name).decode()} for name in archive.namelist() if name.endswith('.csv')])
        self.assertEqual(imported[0]['fields']['template_mode'],'clone')
        self.assertEqual(imported[0]['fields']['new_template_name'],'Custom-Branch-Template')

    def test_ui_preview_replans_and_displays_clone_without_writes(self):
        channel=SimpleQueue()
        with patch('ui_deployment.DeploymentEngine',return_value=self.engine), patch('ui_deployment.ignore_terminal_signals'):
            run_worker([row()],self.config,'preview',None,'/unused',channel)
        event=channel.get_nowait()
        self.assertEqual(event['state'],'ready')
        self.assertEqual(event['details'][0]['template_clone']['name'],'Branch')
        self.engine.post_json.assert_not_called();self.engine.create_site.assert_not_called()


class TemplateInventoryTests(unittest.TestCase):
    def test_full_pagination_and_duplicate_name_beyond_page_one(self):
        pages=[{'count':101,'result':[{'id':str(i),'name':'Name-'+str(i)} for i in range(100)]},
               {'count':101,'result':[{'id':'last','name':'Branch'}]}]
        get=Mock(side_effect=pages)
        result=read_rows(get,'/templates')
        self.assertEqual(len(result),101)
        self.assertEqual(result[-1]['name'],'Branch')
        self.assertEqual(get.call_args.args[1]['page'],1)

    def test_repeated_truncated_or_invalid_inventory_is_not_absence(self):
        cases=[[{'count':2,'result':[{'id':'1'}]},{'count':2,'result':[]}],
               [{'count':2,'result':[{'id':'1'}]},{'count':2,'result':[{'id':'1'}]}],
               [{'count':2,'result':[{'id':'1'}]},{'count':3,'result':[{'id':'2'}]}],
               [{'count':0,'result':[{'id':'1'}]}], [{'bad':[]}]]
        for pages in cases:
            with self.subTest(pages=pages),self.assertRaises(ValueError):read_rows(Mock(side_effect=pages),'/templates')


if __name__=='__main__':unittest.main()

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from automation_config import Settings
from deployment_engine import DeploymentEngine, BatchResult, SiteResult
from input_validation import validate_rows
from run_report import reserve_report, save_report

class SafeRerunTests(unittest.TestCase):
    def setUp(self):
        self.engine=DeploymentEngine(Settings(ztb_api_base='https://example.invalid',bearer='secret'),emit=lambda _:None)
        self.addCleanup(self.engine.client.close)
        self.validation=validate_rows([dict(site_name='Amsterdam',gateway_name='gw',wan_interface_name='ge5',template_id='template',location_type='none',post='1')])

    def test_same_input_twice_creates_once(self):
        rows=[]
        def create(*args):
            rows.append({'location_display_name':'Amsterdam'})
            return True,'created',None
        with patch.object(self.engine,'get_json_v3_gateway_list',side_effect=lambda _: {'rows':list(rows)}), patch.object(self.engine,'create_site',side_effect=create) as post, patch.object(self.engine,'resolve_gateway_ids_and_cluster',return_value=('gw',123)) as resolve:
            first=self.engine.run(self.validation)
            second=self.engine.run(self.validation)
        self.assertEqual(first.sites[0].status,'success')
        self.assertEqual(second.sites[0].status,'already_exists')
        self.assertEqual(second.sites[0].stages,{})
        self.assertEqual(second.exit_code,1)
        post.assert_called_once()
        resolve.assert_called_once()

    def test_lookup_failure_and_malformed_or_truncated_inventory_never_create(self):
        for response in ({}, {'rows':[{}]}, {'rows':[{'location':'Other'}]*100}, {'rows':[], 'total':3}):
            with self.subTest(response=str(response)[:60]), patch.object(self.engine,'get_json_v3_gateway_list',return_value=response), patch.object(self.engine,'create_site') as create:
                result=self.engine.run(self.validation)
                self.assertEqual(result.sites[0].status,'lookup_failed')
                create.assert_not_called()
        with patch.object(self.engine,'get_json_v3_gateway_list',side_effect=RuntimeError('secret')), patch.object(self.engine,'create_site') as create:
            self.assertEqual(self.engine.run(self.validation).sites[0].status,'lookup_failed')
            create.assert_not_called()

    def test_case_insensitive_existing_site_blocks_preview_and_creation(self):
        for dry in (True,False):
            with patch.object(self.engine,'get_json_v3_gateway_list',return_value={'result':{'rows':[{'location':' AMSTERDAM '}]}}), patch.object(self.engine,'create_site') as create:
                self.assertEqual(self.engine.run(self.validation,dry_run=dry).sites[0].status,'already_exists')
                create.assert_not_called()

    def test_reports_are_unique_short_and_do_not_serialize_errors_or_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            path=reserve_report(directory)
            other=reserve_report(directory)
            self.assertNotEqual(path,other)
            result=BatchResult(sites=[SiteResult('Amsterdam','already_exists',errors=['token=secret'])])
            save_report(path,result)
            data=json.loads(path.read_text())
            self.assertEqual(data['sites'][0]['status'],'already_exists')
            self.assertNotIn('secret',path.read_text()+path.with_suffix('.txt').read_text())
            self.assertLess(len(path.with_suffix('.txt').read_text().splitlines()),5)

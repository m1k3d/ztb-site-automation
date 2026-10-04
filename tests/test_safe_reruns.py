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
        self.engine.get_template_settings = Mock(return_value={"deployment_type":"standalone", "dhcp_service":"server"})
        self.addCleanup(self.engine.client.close)
        self.validation=validate_rows([dict(site_name='Amsterdam',gateway_name='gw',wan_interface_name='ge5',template_id='template',location_type='none',post='1')])

    def test_same_input_twice_creates_once(self):
        rows=[]
        def create(*args):
            rows.append({'location_display_name':'Amsterdam'})
            return True,'created',None
        with patch.object(self.engine,'get_json_v3_gateway',side_effect=lambda _: {'rows':list(rows)}), patch.object(self.engine,'create_site',side_effect=create) as post, patch.object(self.engine,'resolve_gateway_ids_and_cluster',return_value=('gw',123)) as resolve:
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
            with self.subTest(response=str(response)[:60]), patch.object(self.engine,'get_json_v3_gateway',return_value=response), patch.object(self.engine,'create_site') as create:
                result=self.engine.run(self.validation)
                self.assertEqual(result.sites[0].status,'lookup_failed')
                create.assert_not_called()
        with patch.object(self.engine,'get_json_v3_gateway',side_effect=RuntimeError('secret')), patch.object(self.engine,'create_site') as create:
            self.assertEqual(self.engine.run(self.validation).sites[0].status,'lookup_failed')
            create.assert_not_called()

    def test_case_insensitive_existing_site_blocks_preview_and_creation(self):
        for dry in (True,False):
            with patch.object(self.engine,'get_json_v3_gateway',return_value={'result':{'rows':[{'location':' AMSTERDAM '}]}}), patch.object(self.engine,'create_site') as create:
                self.assertEqual(self.engine.run(self.validation,dry_run=dry).sites[0].status,'already_exists')
                create.assert_not_called()

    def test_unicode_duplicate_selected_names_block_the_entire_batch(self):
        validation = validate_rows([dict(site_name=name, gateway_name=f'gw-{i}',
            wan_interface_name='ge5', template_id='template', location_type='none', post='1')
            for i, name in enumerate(('Straße', 'STRASSE'))])
        self.assertFalse(validation.valid)
        self.assertTrue(any('duplicate selected site' in issue.message for issue in validation.issues))
        with patch.object(self.engine, 'get_json_v3_gateway') as get, patch.object(self.engine, 'create_site') as create:
            self.assertEqual(self.engine.run(validation).exit_code, 1)
        get.assert_not_called()
        create.assert_not_called()

    def test_inventory_is_read_once_per_batch_and_refreshed_on_next_execute(self):
        validation = validate_rows([dict(site_name=name, gateway_name=f'gw-{i}',
            wan_interface_name='ge5', template_id='template', location_type='none', post='1')
            for i, name in enumerate(('One', 'Two', 'Three'))])
        existing = [{'site_name': 'Unrelated'}]
        with patch.object(self.engine, 'get_json_v3_gateway', side_effect=lambda _: {'rows': existing}) as get:
            plan = self.engine.plan(validation)
            self.assertEqual(self.engine.execute(plan, dry_run=True).exit_code, 0)
            get.assert_called_once()
            existing.append({'site_name': 'Two'})
            outcomes = self.engine.execute(plan, dry_run=True)
        self.assertEqual(get.call_count, 2)
        self.assertEqual([s.status for s in outcomes.sites], ['preview', 'already_exists', 'preview'])

    def test_paginated_absence_allows_creation_but_later_existing_name_blocks_it(self):
        for exists in (False, True):
            inventory = [{'id': str(i), 'site_name': f'Other-{i}'} for i in range(103)]
            if exists:
                inventory[-1]['site_name'] = 'AMSTERDAM'
            with self.subTest(exists=exists), patch.object(self.engine, 'get_json_v3_gateway',
                    side_effect=lambda p: {'rows': inventory[p['page']*100:(p['page']+1)*100], 'count':103}) as get, \
                    patch.object(self.engine, 'create_site', return_value=(True, '', None)) as create, \
                    patch.object(self.engine, 'resolve_gateway_ids_and_cluster', return_value=('gw', 123)):
                result = self.engine.run(self.validation)
                self.assertEqual(result.sites[0].status, 'already_exists' if exists else 'success')
                self.assertEqual(create.call_count, 0 if exists else 1)
                self.assertEqual([call.args[0]['page'] for call in get.call_args_list], [0, 1])

    def test_reports_are_unique_short_and_do_not_serialize_errors_or_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            path=reserve_report(directory)
            other=reserve_report(directory)
            self.assertNotEqual(path,other)
            result=BatchResult(sites=[SiteResult('Amsterdam','already_exists',errors=['token=secret'], diagnostics={'VLANs':'token=secret'})])
            save_report(path,result)
            data=json.loads(path.read_text())
            self.assertEqual(data['sites'][0]['status'],'already_exists')
            self.assertNotIn('secret',path.read_text()+path.with_suffix('.txt').read_text())
            self.assertLess(len(path.with_suffix('.txt').read_text().splitlines()),5)


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.engine = DeploymentEngine(Settings(ztb_api_base='https://example.invalid', bearer='offline'), emit=lambda _: None)
        self.addCleanup(self.engine.client.close)
        self.http = patch('requests.sessions.Session.request', side_effect=AssertionError('Unexpected HTTP')).start()
        self.addCleanup(patch.stopall)

    @staticmethod
    def rows(start, stop):
        return [{'site_name': f'Site-{i}', 'cluster_info': {'site_id': str(i)}} for i in range(start, stop)]

    def read(self, pages, search=''):
        self.engine.get_json_v3_gateway = Mock(side_effect=lambda params: pages[params['page']])
        return self.engine.list_site_inventory(search)

    def test_full_page_requires_an_additional_page_even_when_total_matches(self):
        first = self.rows(0, 100)
        self.assertEqual(self.read([{'rows': first, 'count':100}, {'rows': [], 'count':100}]), first)
        self.assertEqual(self.engine.get_json_v3_gateway.call_count, 2)

    def test_empty_inventory_confirms_page_one_and_small_inventory_needs_one_request(self):
        for body in ({'rows':[]}, {'rows':[], 'count':0}):
            self.assertEqual(self.read([body, body]), [])
            self.assertEqual(self.engine.get_json_v3_gateway.call_count, 2)
        self.assertEqual(self.read([{'result':{'rows':self.rows(0, 3), 'total':'3'}}]), self.rows(0, 3))
        self.assertEqual(self.engine.get_json_v3_gateway.call_count, 1)

    def test_incomplete_ambiguous_and_repeated_pages_raise(self):
        first = self.rows(0, 100)
        cases = [
            [{}, {}],
            [{'rows':[{}]}],
            [{'rows':[{'id':'missing-name'}]}],
            [{'rows':[None]}],
            [{'rows':[{'site_name':'A', 'cluster_info':'invalid'}]}],
            [{'rows':[{'site_name':'A', 'id':[]}]}],
            [{'rows':self.rows(0, 3), 'total':4}],
            [{'rows':first}, {'rows':first}],
            [{'rows':first}, {'rows':self.rows(99, 102)}],
            [{'rows':first, 'count':100}, {'rows':self.rows(100, 103), 'count':3}],
            [{'rows':first, 'count':100}, {'rows':self.rows(100, 103), 'count':100}],
            [{'rows':first, 'count':103}, {'rows':self.rows(100, 103), 'count':104}],
            [{'rows':first, 'count':103}, {'rows':self.rows(100, 103)}],
            [{'rows':first}, {'rows':self.rows(100, 103), 'count':103}],
            [{'rows':first, 'total':103, 'count':100}],
            [{'rows':[], 'count':3}, {'rows':[]}],
            [{'rows':[]}, {'rows':self.rows(0, 3)}],
            [{'rows':[], 'count':0}, {'rows':self.rows(0, 3), 'count':3}],
            [{'rows':self.rows(0, 101)}],
        ]
        for pages in cases:
            with self.subTest(pages=str(pages)[:100]), self.assertRaises(ValueError):
                self.read(pages)

    def test_malformed_totals_raise(self):
        for value in (-1, True, None, 1.5, '1.0', 'bad', [], {}):
            with self.subTest(total=value), self.assertRaises(ValueError):
                self.read([{'rows':self.rows(0, 1), 'count':value}])

    def test_failed_later_page_never_authorizes_creation_and_diagnostic_is_safe(self):
        validation = validate_rows([dict(site_name='New', gateway_name='gw', wan_interface_name='ge5',
            template_id='template', location_type='none', post='1')])
        self.engine.get_template_settings = Mock(return_value={'deployment_type':'standalone', 'dhcp_service':'server'})
        for second in (RuntimeError('private-token'), {'rows':self.rows(0, 100), 'count':103},
                       {'rows':[], 'count':103}):
            with self.subTest(second=str(second)[:50]), patch.object(self.engine, 'get_json_v3_gateway',
                    side_effect=[{'rows':self.rows(0, 100), 'count':103}, second]), \
                    patch.object(self.engine, 'create_site') as create, tempfile.TemporaryDirectory() as directory:
                result = self.engine.run(validation)
                self.assertEqual(result.sites[0].status, 'lookup_failed')
                create.assert_not_called()
                path = reserve_report(directory)
                save_report(path, result)
                report = json.loads(path.read_text())
                self.assertIn('completely', report['sites'][0]['diagnostics']['Existing site check'])
                self.assertNotIn('private-token', path.read_text()+path.with_suffix('.txt').read_text())

    def test_supported_size_boundary_with_and_without_totals(self):
        for count in (9999, 10000, 10001):
            for with_total in (False, True):
                def page(params):
                    start = params['page'] * 100
                    return {'rows': self.rows(start, min(start+100, count)), **({'count':count} if with_total else {})}
                self.engine.get_json_v3_gateway = Mock(side_effect=page)
                with self.subTest(count=count, total=with_total):
                    if count > 10000:
                        with self.assertRaisesRegex(ValueError, 'supported size'):
                            self.engine.list_site_inventory()
                    else:
                        self.assertEqual(len(self.engine.list_site_inventory()), count)
                        self.assertEqual(self.engine.get_json_v3_gateway.call_count, count//100+1)

    def test_identity_normalization_and_name_fallback_detect_duplicates(self):
        for rows in ([{'id':1, 'site_name':'A'}, {'id':'1', 'site_name':'B'}],
                     [{'site_name':'Straße'}, {'location_display_name':' STRASSE '} ]):
            with self.subTest(rows=rows), self.assertRaisesRegex(ValueError, 'repeated'):
                self.read([{'rows':rows}])

    def test_search_finds_exact_name_on_later_page_and_handles_no_match(self):
        first = self.rows(0, 100)
        target = {'site_name':'STRASSE', 'id':'target'}
        self.engine.get_json_v3_gateway = Mock(side_effect=[{'rows':first, 'count':101}, {'rows':[target], 'count':101}])
        self.assertEqual(self.engine.find_site_row_by_name('Straße'), target)
        self.assertEqual([c.args[0]['search'] for c in self.engine.get_json_v3_gateway.call_args_list], ['Straße']*2)
        self.engine.get_json_v3_gateway = Mock(return_value={'rows':first[:3], 'count':3})
        self.assertIsNone(self.engine.find_site_row_by_name('No exact match'))

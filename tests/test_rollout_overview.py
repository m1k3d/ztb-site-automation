import copy
import json
from pathlib import Path
import tempfile
import unittest

from app import validate_batch, normalize_batch
from rollout_overview import readiness, recorded_results
from project_store import clean_workspace
from test_app import sample_batch


class RolloutOverviewTests(unittest.TestCase):
    def test_unselected_drafts_are_validated_without_changing_selection(self):
        batch = sample_batch()
        batch[0]['fields']['post'] = '0'
        bad = copy.deepcopy(batch[0])
        bad['fields'].update(site_name='Another branch', gateway_name='')
        batch.append(bad)
        original = copy.deepcopy(batch)
        result = readiness(batch, validate_batch)
        self.assertTrue(result[0]['ready'])
        self.assertFalse(result[1]['ready'])
        self.assertEqual(batch, original)

    def test_reference_is_excluded_and_vlan_errors_belong_to_the_right_branch(self):
        batch = sample_batch()
        reference = copy.deepcopy(batch[0])
        reference['reference'] = {'id': 'source'}
        batch.insert(0, reference)
        batch[1]['vlans'][0]['subnet'] = 'invalid'
        result = readiness(batch, validate_batch)
        self.assertFalse(result[0]['ready'])
        self.assertFalse(result[0]['issues'])
        self.assertTrue(result[1]['issues'])

    def test_history_is_scoped_to_project_tenant_and_final_reports(self):
        with tempfile.TemporaryDirectory() as folder:
            def write(name, project='p', tenant='one.example', mode='deployment', status='success'):
                Path(folder, name+'.json').write_text(json.dumps({'mode': mode,
                    'workspace': {'project_id': project, 'tenant': tenant},
                    'sites': [{'name': ' Branch ', 'status': status, 'next_action': 'Inspect'}]}))
            write('1');write('2',status='partial');write('3',tenant='two.example')
            write('4',project='q');write('5',mode='preview')
            self.assertEqual(recorded_results(folder,'p','one.example')[0]['status'],'partial')
            self.assertEqual(recorded_results(folder,'p','two.example')[0]['status'],'success')
            self.assertEqual(recorded_results(folder,'p',None),[])

    def test_view_and_grouping_survive_project_cleaning_without_approval(self):
        workspace={'version':1,'batch':sample_batch(),'current':0,'active_tab':'site',
                   'rollout_view':'overview','group_by_country':True,'approval':'ignore'}
        saved=clean_workspace(workspace,normalize_batch)
        self.assertEqual(saved['rollout_view'],'overview')
        self.assertTrue(saved['group_by_country'])
        self.assertNotIn('approval',saved)

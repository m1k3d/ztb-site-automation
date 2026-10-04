import base64
import copy
import io
import unittest
import zipfile

from app import export_batch, import_batch, MissingVlanFiles, validate_batch
from csv_templates import template_bundle


class CsvTemplateTests(unittest.TestCase):
    def files(self, mode='standalone'):
        bundle=template_bundle(mode)
        with zipfile.ZipFile(io.BytesIO(base64.b64decode(bundle['content']))) as archive:
            self.assertIn('READ-ME-FIRST.txt',archive.namelist())
            return [{'name':name,'content':archive.read(name).decode()} for name in archive.namelist() if name.endswith('.csv')]

    def test_complete_kit_round_trips_through_the_real_importer_and_validator(self):
        batch=import_batch(self.files())
        self.assertEqual(len(batch),2)
        self.assertTrue(all(s['fields']['post']=='0' for s in batch))
        self.assertEqual([len(s['vlans']) for s in batch],[1,1])
        self.assertEqual(batch[0]['fields']['wan_dns'],'1.1.1.1,8.8.8.8')
        selected=copy.deepcopy(batch)
        for site in selected:site['fields']['post']='1'
        result=validate_batch(selected)
        self.assertTrue(result['valid'],result['issues'])
        self.assertEqual(result['selected_sites'],2)

    def test_sites_first_prompts_for_exact_matching_vlan_files(self):
        files=self.files()
        with self.assertRaises(MissingVlanFiles) as error:
            import_batch([f for f in files if f['name']=='sites.csv'])
        self.assertEqual(error.exception.names,['branch-01.csv','branch-02.csv'])

    def test_example_networks_are_distinct_and_optional_services_are_off(self):
        batch=import_batch(self.files())
        self.assertNotEqual(batch[0]['vlans'][0]['default_gateway'],batch[1]['vlans'][0]['default_gateway'])
        for site in batch:
            self.assertEqual(site['fields']['appc_provision'],'0')
            self.assertEqual(site['vlans'][0]['zpa_include'],'0')

    def test_ha_kit_imports_validates_and_exports_both_gateways_and_network_ownership(self):
        batch=import_batch(self.files('ha'))
        self.assertEqual(len(batch),1)
        site=batch[0]
        self.assertEqual(site['fields']['post'],'0')
        self.assertNotEqual(site['fields']['gateway_name'],site['fields']['gateway_name_b'])
        self.assertEqual([v['gateway_target'] for v in site['vlans']],['all','a','b'])
        self.assertTrue(all(v['zpa_include']=='0' for v in site['vlans']))
        site['fields']['post']='1'
        site['ha_enabled']=True
        result=validate_batch(batch)
        self.assertTrue(result['valid'],result['issues'])
        exported=export_batch(batch)
        with zipfile.ZipFile(io.BytesIO(base64.b64decode(exported['content']))) as archive:
            files=[{'name':n,'content':archive.read(n).decode()} for n in archive.namelist() if n.endswith('.csv')]
        restored=import_batch(files)[0]
        for key in ('gateway_name','gateway_name_b','wan0_ip','wan1_ip','wan1_interface_name','vrrp_link_interface','vrrp_vrid'):
            self.assertEqual(restored['fields'][key],site['fields'][key])
        self.assertEqual([v['gateway_target'] for v in restored['vlans']],['all','a','b'])
        self.assertEqual([v['default_gateway'] for v in restored['vlans']],
                         [v['default_gateway'] for v in site['vlans']])

    def test_ha_sites_first_prompts_for_ha_vlan_file(self):
        with self.assertRaises(MissingVlanFiles) as error:
            import_batch([f for f in self.files('ha') if f['name']=='sites.csv'])
        self.assertEqual(error.exception.names,['ha-branch-01.csv'])

    def test_template_mode_is_explicit_and_invalid_modes_are_rejected(self):
        self.assertNotEqual(template_bundle()['filename'],template_bundle('ha')['filename'])
        for mode in ('unknown',None,[],{}):
            with self.assertRaises(ValueError):
                template_bundle(mode)

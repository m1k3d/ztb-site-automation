import base64
import copy
import csv
import io
import json
import unittest
import zipfile

from app import csv_text, export_batch, import_batch, parse_csv, validate_batch
from credential_fields import clean_values, is_credential_field
from csv_templates import template_bundle
from project_store import ProjectStore


def kit_files(mode='standalone'):
    with zipfile.ZipFile(io.BytesIO(base64.b64decode(template_bundle(mode)['content']))) as archive:
        return [{'name': n, 'content': archive.read(n).decode()}
                for n in archive.namelist() if n.endswith('.csv')]


class CredentialBoundaryTests(unittest.TestCase):
    def test_credential_headers_are_rejected_even_without_rows_or_values(self):
        for key in ('API_KEY', 'api-key-backup', 'ZTB_BEARER', 'Authorization',
                    'clientSecret', 'refresh_token', 'password', 'private_key',
                    'provisioning_key', 'credentials_json', 'ＡＰＩ＿ＫＥＹ'):
            with self.subTest(key=key):
                self.assertTrue(is_credential_field(key))
                for content in (f'site_name,{key}\n', f'site_name,{key}\nBranch,\n'):
                    with self.assertRaisesRegex(ValueError, 'CSV contains credential fields'):
                        parse_csv(content, 'sites.csv')

    def test_site_and_vlan_imports_reject_secrets_without_echoing_them(self):
        for filename in ('sites.csv', 'vlans/branch-01.csv'):
            files = kit_files()
            selected = next(f for f in files if f['name'] == filename)
            reader = csv.DictReader(io.StringIO(selected['content']))
            rows = list(reader)
            rows[0]['API_KEY'] = 'SYNTHETIC-PRIVATE-VALUE'
            output = io.StringIO()
            writer = csv.DictWriter(output, fieldnames=[*reader.fieldnames, 'API_KEY'])
            writer.writeheader()
            writer.writerows(rows)
            selected['content'] = output.getvalue()
            with self.assertRaises(ValueError) as error:
                import_batch(files)
            self.assertIn('credential fields', str(error.exception))
            self.assertNotIn('SYNTHETIC-PRIVATE-VALUE', str(error.exception))

    def test_nested_json_credentials_are_rejected_on_import(self):
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(['site_name', 'extra_json'])
        writer.writerow(['Branch', json.dumps({'nested': [{'client_secret': 'SYNTHETIC-PRIVATE-VALUE'}]})])
        with self.assertRaisesRegex(ValueError, 'credential fields'):
            import_batch([{'name': 'sites.csv', 'content': output.getvalue()}])

    def test_export_scrubs_legacy_site_vlan_and_json_credentials_without_mutating_draft(self):
        for mode in ('standalone', 'ha'):
            with self.subTest(mode=mode):
                batch = import_batch(kit_files(mode))
                for site in batch:
                    site['fields'].update(post='1', API_KEY='SYNTHETIC-SITE-SECRET',
                                          extra_json=json.dumps({'label': 'keep', 'nested': [{'access_token': 'SYNTHETIC-JSON-SECRET'}]}))
                    site['vlans'][0]['client_secret'] = 'SYNTHETIC-VLAN-SECRET'
                original = copy.deepcopy(batch)
                exported = export_batch(batch)
                with zipfile.ZipFile(io.BytesIO(base64.b64decode(exported['content']))) as archive:
                    files = [{'name': n, 'content': archive.read(n).decode()}
                             for n in archive.namelist() if n.endswith('.csv')]
                self.assertNotIn('SYNTHETIC-', str(files))
                restored = import_batch(files)
                self.assertTrue(validate_batch(restored)['valid'])
                self.assertEqual(json.loads(restored[0]['fields']['extra_json']), {'label': 'keep', 'nested': [{}]})
                self.assertEqual(restored[0]['fields']['wan_dns'], batch[0]['fields']['wan_dns'])
                self.assertEqual(batch, original)

    def test_csv_writer_does_not_restore_credential_headers_from_defaults(self):
        text = csv_text([{'site_name': 'Branch', 'password': 'SYNTHETIC-SECRET'}], ['site_name', 'password'])
        self.assertEqual(text, 'site_name\nBranch\n')

    def test_legacy_saved_project_is_filtered_before_returning_to_browser(self):
        workspace = {'batch': [{'fields': {'api_key': 'SYNTHETIC-SECRET', 'site_name': 'Branch'},
                                'vlans': [{'credentials_json': '{"password":"SYNTHETIC-SECRET"}'}]}]}
        row = dict(id='a'*32, name='Project', revision=1, created_at='now', updated_at='now',
                   workspace=json.dumps(workspace))
        result = ProjectStore.public(row, full=True)
        self.assertNotIn('SYNTHETIC-', json.dumps(result))
        self.assertEqual(result['site_count'], 1)

    def test_normal_configuration_and_unchanged_json_keep_exact_values(self):
        value = {'private_dns': '10.0.0.1', 'zpa_customer_id': '42', 'gateway_target': 'b',
                 'template_id': 't1', 'additional_wans_json': '[ { "gateway_target": "b" } ]'}
        self.assertEqual(clean_values(value), value)

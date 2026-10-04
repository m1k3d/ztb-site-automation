import base64
import copy
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from app import export_batch, import_batch
from csv_safety import csv_text
from csv_templates import template_bundle
import pull_site


class CsvSafetyTests(unittest.TestCase):
    def test_formula_headers_and_values_are_rejected_without_echoing_content(self):
        for value in ('=1+1', '+SUM(1,1)', '-1+2', '@SUM(1,1)', '\t=1+1',
                      '\r\n=1+1', '  =1+1', '\ufeff=1+1', '＝1+1', '\u200b@SUM(1,1)'):
            for rows, headers in (([{'name': value}], ['name']), ([{value: 'plain'}], [value])):
                with self.subTest(value=value), self.assertRaises(ValueError) as error:
                    csv_text(rows, headers)
                self.assertIn('CSV export blocked', str(error.exception))
                self.assertNotIn(value, str(error.exception))

    def test_plain_text_quotes_commas_and_json_round_trip_unchanged(self):
        rows = [{'name': 'Branch-A', 'notes': 'a,b "quoted"\nnext line',
                 'settings_json': '{"label":"=literal inside JSON"}'}]
        import csv
        self.assertEqual(list(csv.DictReader(io.StringIO(csv_text(rows, [])))), rows)

    def test_ui_export_rejects_site_vlan_and_unknown_column_formulas(self):
        kit = template_bundle()
        with zipfile.ZipFile(io.BytesIO(base64.b64decode(kit['content']))) as archive:
            source = import_batch([{'name': n, 'content': archive.read(n).decode()}
                                   for n in archive.namelist() if n.endswith('.csv')])
        for target, key in [('site', 'site_name'), ('site', 'extra'), ('vlan', 'name')]:
            batch = copy.deepcopy(source)
            batch[0]['fields']['post'] = '1'
            fields = batch[0]['fields'] if target == 'site' else batch[0]['vlans'][0]
            fields[key] = '=1+1'
            original = copy.deepcopy(batch)
            with self.assertRaisesRegex(ValueError, 'CSV export blocked'):
                export_batch(batch)
            self.assertEqual(batch, original)

    def test_cli_site_export_keeps_old_file_when_formula_check_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'sites.csv'
            original = 'site_name,custom\nExisting,keep\n'
            path.write_text(original)
            with patch.object(pull_site, 'CSV_PATH', path):
                with self.assertRaisesRegex(ValueError, 'CSV export blocked'):
                    pull_site.upsert_sites_csv_row({'site_name': '=1+1'})
            self.assertEqual(path.read_text(), original)

    def test_cli_vlan_export_checks_before_truncating_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'vlans.csv'
            path.write_text('existing file\n')
            with patch.object(pull_site, 'vlans_to_csv_rows', return_value=[{'name': '=1+1'}]):
                with self.assertRaisesRegex(ValueError, 'CSV export blocked'):
                    pull_site.write_vlans_csv([], path)
            self.assertEqual(path.read_text(), 'existing file\n')

    def test_cli_reexport_removes_existing_credential_columns(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'sites.csv'
            path.write_text('site_name,api_key\nExisting,SYNTHETIC-SECRET\n')
            with patch.object(pull_site, 'CSV_PATH', path):
                pull_site.upsert_sites_csv_row({'site_name': 'New'})
            self.assertNotIn('SYNTHETIC-', path.read_text())
            self.assertNotIn('api_key', path.read_text())

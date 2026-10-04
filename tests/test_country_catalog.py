import base64
import contextlib
from html.parser import HTMLParser
import io
import unittest
from unittest.mock import Mock, patch
import zipfile

from app import Handler, export_batch, import_batch, validate_batch
from country_catalog import COUNTRIES, resolve_country
from site_payload import build_site_payload
import zpa_provisioning as zpa


class CountryTests(unittest.TestCase):
    def test_every_choice_and_alias_maps_to_exact_api_value(self):
        self.assertEqual(len(COUNTRIES), 245)
        self.assertEqual(len({c['value'] for c in COUNTRIES}), 245)
        self.assertEqual(len({c['code'] for c in COUNTRIES}), 245)
        for country in COUNTRIES:
            for value in [country['name'], country['value'], country['code'], *country['aliases']]:
                with self.subTest(value=value):
                    payload = build_site_payload(
                        {'site_name': 'Branch', 'gateway_name': 'Branch-ZT',
                         'wan_interface_name': 'ge5', 'country': value},
                        {'location_type': 'new', 'location_template_id': 123})
                    self.assertEqual(payload['location']['details']['country'], country['value'])

    def test_special_names_and_codes(self):
        for value, enum, code in [
            (' Netherlands ', 'THE_NETHERLANDS', 'NL'),
            ('UK', 'UNITED_KINGDOM', 'GB'), ('usa', 'UNITED_STATES', 'US'),
            ('Côte d’Ivoire', 'IVORY_COAST', 'CI'),
            ('Åland Islands', 'ALAND', 'AX'), ('Eswatini', 'SWAZILAND', 'SZ'),
            ('Congo - Kinshasa', 'DR_CONGO', 'CD'),
            ('Congo - Brazzaville', 'CONGO_REPUBLIC', 'CG'),
        ]:
            with self.subTest(value=value):
                country = resolve_country(value)
                self.assertEqual((country['value'], country['code']), (enum, code))

    def test_unknown_or_partial_names_are_rejected(self):
        for value in ['Neth', 'Atlantis', 'ZZ', '']:
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'supported country'):
                resolve_country(value)

    def test_ui_validation_and_csv_roundtrip(self):
        batch = [{'fields': {'site_name': 'Branch', 'gateway_name': 'Branch-ZT',
                             'template_name': 'Example', 'wan_interface_name': 'ge5',
                             'location_type': 'new', 'post': '1', 'country': 'CI'}, 'vlans': []}]
        self.assertTrue(validate_batch(batch)['valid'])
        exported = export_batch(batch)
        with zipfile.ZipFile(io.BytesIO(base64.b64decode(exported['content']))) as archive:
            uploads = [{'name': name, 'content': archive.read(name).decode()}
                       for name in archive.namelist() if name.endswith('.csv')]
        imported = import_batch(uploads)
        self.assertEqual(resolve_country(imported[0]['fields']['country'])['value'], 'IVORY_COAST')
        batch[0]['fields']['country'] = 'Atlantis'
        invalid = validate_batch(batch)
        self.assertFalse(invalid['valid'])
        self.assertIn('country', [issue['field'] for issue in invalid['issues']])
        with self.assertRaises(ValueError):
            export_batch(batch)
        batch[0]['fields'].update(country='', location_type='none')
        self.assertTrue(validate_batch(batch)['valid'])

    def test_served_page_contains_all_options_without_a_connection(self):
        handler = object.__new__(Handler)
        handler.path = '/'
        handler.server = Mock(token='test-token')
        handler.allowed_host = lambda: True
        handler.reply = Mock()
        handler.do_GET()
        status, content, _ = handler.reply.call_args.args
        self.assertEqual(status, 200)
        self.assertNotIn(b'__COUNTRY_OPTIONS__', content)
        self.assertIn(b'list="country-options"', content)
        class Options(HTMLParser):
            def __init__(self):
                super().__init__()
                self.active = False
                self.values = []
            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if tag == 'datalist':
                    self.active = attrs.get('id') == 'country-options'
                if self.active and tag == 'option':
                    self.values.append(attrs['value'])
            def handle_endtag(self, tag):
                if tag == 'datalist':
                    self.active = False
        parser = Options()
        parser.feed(content.decode())
        self.assertEqual(parser.values, [c['name'] for c in COUNTRIES])

    def test_zpa_country_codes_do_not_default_to_netherlands(self):
        response = Mock()
        response.json.return_value = {'id': 'group'}
        for country, code in [('Brazil', 'BR'), ('THE_NETHERLANDS', 'NL'),
                              ('South Africa', 'ZA'), ('AX', 'AX'), ('', None)]:
            with self.subTest(country=country), \
                    patch.object(zpa, 'get_geo_location', return_value=('0', '0')), \
                    patch.object(zpa.requests, 'post', return_value=response) as post, \
                    contextlib.redirect_stdout(io.StringIO()):
                zpa.create_app_connector_group('https://example.invalid', 'customer', 'token',
                                               'Branch', '', country, enrollment_cert_id='cert')
                self.assertEqual(post.call_args.kwargs['json'].get('countryCode'), code)
        with patch.object(zpa.requests, 'post') as post, patch.object(zpa, 'get_geo_location') as geo:
            with self.assertRaises(ValueError):
                zpa.create_app_connector_group('https://example.invalid', 'customer', 'token',
                                               'Branch', '', 'Atlantis', enrollment_cert_id='cert')
            post.assert_not_called()
            geo.assert_not_called()

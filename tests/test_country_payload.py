import unittest
from site_payload import build_site_payload


class CountryPayloadTests(unittest.TestCase):
    def test_dutch_location_country_matches_tenant_api_enum(self):
        for country in ('Netherlands', 'The Netherlands', 'THE_NETHERLANDS'):
            with self.subTest(country=country):
                payload = build_site_payload(
                    {'site_name': 'Amsterdam-Test', 'gateway_name': 'Amsterdam-GW',
                     'wan_interface_name': 'ge5', 'country': country},
                    {'location_type': 'new', 'location_template_id': 123},
                )
                self.assertEqual(payload['location']['details']['country'], 'THE_NETHERLANDS')

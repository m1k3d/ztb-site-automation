import contextlib
import io
import unittest
from unittest.mock import Mock, patch

import zpa_provisioning as zpa


class GroupCertificateTests(unittest.TestCase):
    def test_group_creation_supplies_resolved_certificate(self):
        response = Mock(status_code=201)
        response.json.return_value = {"id": "group-id"}
        with (
            patch.object(zpa, "get_geo_location", return_value=("52.37", "4.89")),
            patch.object(zpa.requests, "post", return_value=response) as post,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            group_id = zpa.create_app_connector_group(
                "https://example.invalid", "customer", "token", "Branch",
                "Amsterdam", "Netherlands", enrollment_cert_id="custom-cert",
            )
        self.assertEqual(group_id, "group-id")
        self.assertEqual(post.call_args.kwargs["json"]["enrollmentCertId"], "custom-cert")

    def test_missing_certificate_prevents_network_requests(self):
        with (
            patch.object(zpa.requests, "post") as post,
            patch.object(zpa, "get_geo_location") as geo,
        ):
            with self.assertRaisesRegex(ValueError, "certificate ID"):
                zpa.create_app_connector_group(
                    "https://example.invalid", "customer", "token", "Branch",
                    "Amsterdam", "Netherlands",
                )
            post.assert_not_called()
            geo.assert_not_called()

    def test_group_and_key_use_same_preflight_certificate(self):
        context = zpa.ZPAContext("https://example.invalid", "customer", "token", "custom-cert")
        with (
            patch.object(zpa, "create_app_connector_group", return_value="group-id") as group,
            patch.object(zpa, "create_provisioning_key", return_value="private-key") as key,
            patch.object(zpa, "update_ztb_site_zpa", return_value=True),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertTrue(zpa.provision_zpa_for_site(
                {"site_name": "Branch"}, Mock(), "https://example.invalid", 123,
                context=context,
            ))
        self.assertEqual(group.call_args.kwargs["enrollment_cert_id"], "custom-cert")
        self.assertEqual(key.call_args.args[5], "custom-cert")

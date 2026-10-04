import unittest
from unittest.mock import Mock, patch

from automation_config import Settings
from site_reference import ReferenceError, ReferenceSites
from zpa_provisioning import ZPAContext


class ReferenceSitesTests(unittest.TestCase):
    def setUp(self):
        self.config = Settings(ztb_api_base="https://example-api.goairgap.com", api_key="test-secret")
        self.client = Mock(base_root="https://example-api.goairgap.com", api_v3="https://example-api.goairgap.com/api/v3",
                           api_v2="https://example-api.goairgap.com/api/v2", origin="https://example.goairgap.com", referer="https://example.goairgap.com/")
        self.load_patch = patch("site_reference.Settings.load", return_value=self.config)
        self.client_patch = patch("site_reference.ZTBClient", return_value=self.client)
        self.load = self.load_patch.start()
        self.factory = self.client_patch.start()
        self.addCleanup(self.load_patch.stop)
        self.addCleanup(self.client_patch.stop)
        self.reader = ReferenceSites("ignored.env")
        self.site = {"location_display_name": "Reference", "cluster_info": {"site_id": "42", "template_name": "Branch"},
                     "gateway_name": "Reference-ZT", "location": {"city": "Amsterdam", "country": "Netherlands"}}

    def response(self, data, status=200):
        result = Mock(status_code=status)
        result.json.return_value = data
        return result

    def connect(self):
        self.client.request.return_value = self.response({"rows": [self.site]})
        return self.reader.connect()

    def test_connect_returns_only_display_fields_and_keeps_tokens_in_memory(self):
        self.site["provisioning_key"] = "must-not-reach-browser"
        result = self.connect()
        self.assertEqual(result["sites"][0]["id"], "42")
        self.assertNotIn("must-not-reach-browser", str(result))
        self.assertIsNone(self.factory.call_args.args[0].env_path)
        self.assertNotIn("test-secret", str(result))

    def test_manual_connection_does_not_read_environment_file(self):
        self.client.request.return_value = self.response({"rows": []})
        self.reader.connect({"tenant_url": "https://example-api.goairgap.com", "api_key": "session-key"})
        self.load.assert_not_called()
        self.assertEqual(self.factory.call_args.args[0].api_key, "session-key")

    def test_zone_lookup_returns_only_assignable_names_and_types(self):
        self.connect()
        self.client.request.return_value = self.response({"result": [
            {"display_name": "Guest-Zone", "name": "internal-name", "type": "lan_zone", "members": ["private"]},
            {"display_name": "Management Zone", "type": "mgt_zone"},
            {"display_name": "Hidden", "type": "lan_zone", "hidden": True}], "count": 3, "token": "private-token"})
        result = self.reader.zones()
        self.assertEqual(result, {"zones": [{"name": "Guest-Zone", "type": "lan_zone"},
                                            {"name": "Management Zone", "type": "mgt_zone"}]})
        args, kwargs = self.client.request.call_args
        self.assertEqual(args, ("GET", self.client.api_v2 + "/groups"))
        self.assertEqual(kwargs['params']['group_type'], 'lan_zone,mgt_zone')
        self.assertNotIn('private', str(result))

    def test_zone_lookup_reads_all_pages(self):
        self.connect()
        self.client.request.side_effect = [self.response({"result": [
            {"display_name": f"Zone {i:03}", "type": "lan_zone"} for i in range(100)], "count": 101}),
            self.response({"result": [{"display_name": "Zone 100", "type": "lan_zone"}], "count": 101})]
        self.assertEqual(len(self.reader.zones()['zones']), 101)
        self.assertEqual(self.client.request.call_args.kwargs['params']['page'], 1)

    def test_zone_lookup_rejects_partial_repeated_and_malformed_lists(self):
        self.connect()
        row = {"display_name": "LAN Zone", "type": "lan_zone"}
        for responses in [
            [{'result': [row], 'count': 2}, {'result': [], 'count': 2}],
            [{'result': [row], 'count': 2}, {'result': [row], 'count': 2}],
            [{'result': [{'display_name': 'WAN Zone', 'type': 'wan_zone'}], 'count': 1}],
            [{'result': [{'type': 'lan_zone'}], 'count': 1}],
            [{'result': 'private response', 'count': 1}],
        ]:
            with self.subTest(responses=responses):
                self.client.request.side_effect = [self.response(data) for data in responses]
                with self.assertRaises(ReferenceError):
                    self.reader.zones()

    def test_zone_lookup_failure_is_safe_and_does_not_disconnect_reference(self):
        self.connect()
        self.client.request.return_value = self.response({'secret': 'private'}, status=403)
        with self.assertRaisesRegex(ReferenceError, 'HTTP 403') as error:
            self.reader.zones()
        self.assertNotIn('private', str(error.exception))
        self.assertIs(self.reader.client, self.client)
        self.assertIn('42', self.reader.rows)

    def test_zone_lookup_requires_connection(self):
        with self.assertRaisesRegex(ReferenceError, 'Connect'):
            self.reader.zones()
        self.client.request.assert_not_called()

    def test_deployment_snapshot_combines_connections_without_persisting_tokens(self):
        self.connect()
        self.client.config = self.config
        self.client.token = "refreshed-token"
        self.reader.zpa_override = True
        self.reader.zpa_config = Settings(zpa_base_url="https://config.example.com", zpa_client_id="client", zpa_client_secret="zpa-secret")
        revision, config = self.reader.deployment_settings()
        self.assertEqual(revision, self.reader.revision)
        self.assertEqual(config.bearer, "refreshed-token")
        self.assertEqual(config.zpa_client_secret, "zpa-secret")
        self.assertIsNone(config.env_path)
        status = self.reader.connection_status()
        self.assertNotIn("refreshed-token", str(status))
        self.assertNotIn("zpa-secret", str(status))

    def test_failed_zpa_override_cannot_fall_back_to_old_saved_credentials(self):
        self.connect()
        self.client.config = Settings(ztb_api_base="https://example.invalid", bearer="token", zpa_client_secret="old-secret")
        self.client.token = "token"
        with self.assertRaises(ReferenceError):self.reader.connect_zpa({})
        _, config = self.reader.deployment_settings()
        self.assertEqual(config.zpa_client_secret, "")

    def zpa_details(self):
        return dict(base_url="https://config.example.com", client_id="test-client",
                    client_secret="session-secret", customer_id="42", enrollment_cert_name="Connector")

    def test_manual_zpa_uses_separate_settings_and_returns_no_credentials(self):
        context = ZPAContext("https://config.example.com", "42", "private-token", "cert-1")
        with patch("site_reference.prepare_zpa", return_value=context) as prepare:
            result = self.reader.connect_zpa(self.zpa_details())
        self.load.assert_not_called()
        self.factory.assert_not_called()
        config = prepare.call_args.args[0]
        self.assertIsNone(config.env_path)
        self.assertEqual(config.zpa_client_secret, "session-secret")
        self.assertEqual(prepare.call_args.kwargs, dict(write_env=False, quiet=True))
        self.assertEqual(result, dict(cloud="config.example.com", customer_id="42", certificate="Connector"))
        self.assertIs(self.reader.zpa_context, context)
        self.reader.close()
        self.assertIsNone(self.reader.zpa_context)
        self.assertIsNone(self.reader.zpa_config)

    def test_saved_zpa_settings_do_not_write_to_environment_file(self):
        from dataclasses import replace
        from pathlib import Path
        self.load.return_value = Settings(zpa_base_url="https://config.example.com", zpa_client_id="client",
                                          zpa_client_secret="secret", env_path=Path("ignored.env"))
        context = ZPAContext("https://config.example.com", "42", "token", "cert")
        with patch("site_reference.prepare_zpa", return_value=context) as prepare:
            self.reader.connect_zpa()
        self.assertEqual(prepare.call_args.args[0], replace(self.load.return_value, env_path=None))
        self.assertFalse(prepare.call_args.kwargs["write_env"])

    def test_zpa_errors_are_redacted_and_clear_stale_context_without_losing_ztb(self):
        self.connect()
        self.reader.zpa_context = object()
        for error, expected in [(RuntimeError("raw-session-secret"), "Unable to connect to ZPA"),
                                (ValueError("ZPA_CUSTOMER_ID: raw-session-secret"), "customer ID"),
                                (ValueError("ZPA_ENROLLMENT_CERT_NAME: raw-session-secret"), "certificate")]:
            with self.subTest(error=type(error)), patch("site_reference.prepare_zpa", side_effect=error):
                with self.assertRaisesRegex(ReferenceError, expected) as raised:
                    self.reader.connect_zpa(self.zpa_details())
                self.assertNotIn("raw-session-secret", str(raised.exception))
                self.assertIsNone(self.reader.zpa_context)
                self.assertIsNone(self.reader.zpa_config)
                self.assertIn("42", self.reader.rows)
                self.assertIs(self.reader.client, self.client)

    def test_incomplete_zpa_details_fail_before_authentication(self):
        details = self.zpa_details()
        details["client_secret"] = ""
        with patch("site_reference.prepare_zpa") as prepare:
            with self.assertRaisesRegex(ReferenceError, "Client secret"):
                self.reader.connect_zpa(details)
        prepare.assert_not_called()

    def test_zpa_check_only_authenticates_and_reads_certificate_without_writes_or_logs(self):
        import contextlib
        import io
        output = io.StringIO()
        with patch("zpa_login.requests.post", return_value=self.response({"access_token": "opaque-token"})) as post, \
             patch("zpa_provisioning.requests.get", return_value=self.response({"list": [{"id": "cert", "name": "Connector"}]})) as get, \
             patch("zpa_login.write_tokens") as write, contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            result = self.reader.connect_zpa(self.zpa_details())
        self.assertEqual(result["customer_id"], "42")
        self.assertEqual(post.call_count, 1)
        self.assertTrue(post.call_args.args[0].endswith("/signin"))
        self.assertEqual(get.call_count, 1)
        self.assertTrue(get.call_args.args[0].endswith("/enrollmentCert"))
        write.assert_not_called()
        self.assertEqual(output.getvalue(), "")

    def test_zpa_failed_certificate_read_does_not_log_raw_errors(self):
        import contextlib
        import io
        output = io.StringIO()
        with patch("zpa_login.requests.post", return_value=self.response({"access_token": "opaque-token"})), \
             patch("zpa_provisioning.requests.get", side_effect=RuntimeError("raw-session-secret")), \
             contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            with self.assertRaisesRegex(ReferenceError, "certificate"):
                self.reader.connect_zpa(self.zpa_details())
        self.assertEqual(output.getvalue(), "")

    def test_pull_reuses_mapping_and_excludes_wan_and_ha(self):
        self.connect()
        self.client.request.side_effect = [self.response({"rows": [
            {"name": "Management", "tag": 1, "subnet": "24", "start_ip": "10.1.0.1", "interface": "lo0", "zone": "Mgmt Zone", "dhcp_service": "inherit"},
            {"name": "LAN", "tag": 20, "subnet": "24", "start_ip": "10.20.0.1", "interface": "ge2", "zone": "LAN Zone", "status": "provisioned"},
            {"name": "WAN", "zone": "WAN Zone"}, {"name": "HA-Link", "zone": "HA Zone"}]}),
            self.response({"result": [{"site_id": "42", "membership_info": {"ip_prefix": ["10.2.0.1/32"]}}]})]
        with patch("pathlib.Path.open", side_effect=AssertionError("No file writes")):
            result = self.reader.pull("42")
        fields, vlans = result["site"]["fields"], result["site"]["vlans"]
        self.assertEqual(fields["private_dns"], "10.2.0.1")
        self.assertEqual(fields["post"], "0")
        self.assertEqual(fields["appc_provision"], "0")
        self.assertEqual(fields["vlans_file"], "")
        self.assertEqual(len(vlans), 2)
        self.assertEqual(vlans[0]["subnet"], "32")
        self.assertEqual(vlans[0]["dhcp_service"], "off")
        self.assertTrue(all(v["zpa_include"] == "0" for v in vlans))
        self.assertEqual(result["loopbacks"], 1)
        self.assertTrue(all(call.args[0] == "GET" for call in self.client.request.call_args_list))

    def test_unknown_id_is_rejected_before_network_request(self):
        self.connect()
        self.client.request.reset_mock()
        with self.assertRaises(ReferenceError):
            self.reader.pull("other-tenant-id")
        self.client.request.assert_not_called()

    def test_bad_vlan_shape_does_not_import_an_empty_network(self):
        self.connect()
        self.client.request.return_value = self.response({"unexpected": []})
        with self.assertRaisesRegex(ReferenceError, "Unexpected VLAN"):
            self.reader.pull("42")

    def test_dns_failure_does_not_return_partial_reference(self):
        self.connect()
        self.client.request.side_effect = [self.response({"rows": []}), self.response({}, 403)]
        with self.assertRaisesRegex(ReferenceError, "private DNS.*403"):
            self.reader.pull("42")

    def test_network_error_does_not_expose_raw_details(self):
        self.client.request.side_effect = RuntimeError("must-not-expose-token")
        with self.assertRaises(ReferenceError) as result:
            self.reader.connect()
        self.assertNotIn("must-not-expose-token", str(result.exception))
        self.assertIsNone(self.reader.client)

    def test_reconnect_failure_clears_previous_selection(self):
        self.connect()
        self.client.request.return_value = self.response({}, 401)
        with self.assertRaises(ReferenceError):
            self.reader.connect()
        self.assertEqual(self.reader.rows, {})
        with self.assertRaises(ReferenceError):
            self.reader.pull("42")


if __name__ == "__main__":
    unittest.main()

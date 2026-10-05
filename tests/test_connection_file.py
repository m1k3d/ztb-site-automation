import contextlib
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from app import Handler
from connection_file import parse_connection_file
from site_reference import ReferenceSites
from ui_deployment import DeploymentSession
from zpa_provisioning import ZPAContext

BASE = 'ZTB_API_BASE="https://customer-api.example.com/api/v3"\nAPI_KEY="synthetic-key"\n'
ZPA = '\nZPA_ENABLED=true\nZPA_BASE_URL="https://config.example.com"\nZPA_CLIENT_ID="synthetic-client"\nZPA_CLIENT_SECRET="synthetic-secret"\n'


class ConnectionFileTests(unittest.TestCase):
    def test_quotes_comments_bom_crlf_and_literal_values_without_environment(self):
        with patch.dict(os.environ, {'API_KEY': 'wrong-customer', 'ZPA_ENABLED': 'true'}):
            connection, zpa = parse_connection_file('\ufeff# Comment\r\n' + BASE.replace('synthetic-key', '${API_KEY}$(literal)#key') + 'ZPA_ENABLED=false # disabled\n')
        self.assertEqual(connection['api_key'], '${API_KEY}$(literal)#key')
        self.assertIsNone(zpa)

    def test_templates_work_when_populated_and_old_cli_tokens_are_ignored(self):
        for path in ('ui/customer-example.env', '.env.example'):
            text = Path(path).read_text().replace('YOUR_TENANT', 'customer').replace('<your-tenant>', 'customer')
            text = text.replace('YOUR_API_KEY', 'synthetic-key').replace('YOUR_ZPA_CLIENT_ID', 'synthetic-client').replace('YOUR_ZPA_CLIENT_SECRET', 'synthetic-secret')
            connection, zpa = parse_connection_file(text)
            self.assertEqual(set(connection), {'tenant_url', 'api_key'})
            self.assertEqual(connection['api_key'], 'synthetic-key')
            if zpa:self.assertNotIn('BEARER', str(zpa))
        connection, zpa = parse_connection_file(BASE + 'BEARER="old-token"\n')
        self.assertNotIn('old-token', str(connection))

    def test_missing_invalid_duplicate_unknown_and_oversize_input_is_redacted(self):
        for text in (None, '', 'x'*65537, BASE+'API_KEY=raw-secret\n', BASE+'RAW_SECRET=raw-secret\n',
                     BASE+'API_KEY="raw-secret', BASE+'BROKEN', BASE+'\x00raw-secret', BASE+'\ud800',
                     BASE.replace('synthetic-key', 'YOUR_API_KEY'), BASE.replace('https:', 'http:'),
                     BASE+'ZPA_ENABLED=raw-secret\n', BASE+'ZPA_ENABLED=true\n'):
            with self.subTest(text_type=type(text)), contextlib.redirect_stderr(io.StringIO()) as errors:
                with self.assertRaises(ValueError) as exc:parse_connection_file(text)
                self.assertNotIn('raw-secret', str(exc.exception))
                self.assertEqual(errors.getvalue(), '')

    def test_zpa_explicit_disable_and_legacy_implicit_enable(self):
        _, zpa = parse_connection_file(BASE + ZPA)
        self.assertEqual(zpa['enrollment_cert_name'], 'Connector')
        self.assertEqual(zpa['customer_id'], '')
        self.assertIsNone(parse_connection_file(BASE + ZPA.replace('true', 'false'))[1])
        self.assertIsNotNone(parse_connection_file(BASE + ZPA.replace('ZPA_ENABLED=true', ''))[1])

    def reader(self):
        reader = ReferenceSites('never-read.env')
        client = Mock(base_root='https://customer-api.example.com', api_v3='https://customer-api.example.com/api/v3')
        client.request.return_value = Mock(status_code=200)
        client.request.return_value.json.return_value = {'rows': []}
        return reader, client

    def test_file_switch_clears_old_zpa_and_never_reads_or_writes_local_credentials(self):
        reader, client = self.reader()
        reader.zpa_context=object();reader.zpa_config=object();reader.zpa_override=True
        with patch('site_reference.ZTBClient', return_value=client) as factory, patch('site_reference.Settings.load') as load:
            result=reader.connect_file(BASE)
        self.assertEqual(result['tenant'], 'customer-api.example.com')
        self.assertIsNone(reader.zpa_context);self.assertIsNone(reader.zpa_config)
        self.assertFalse(reader.zpa_override);load.assert_not_called()
        config=factory.call_args.args[0]
        self.assertIsNone(config.env_path);self.assertFalse(config.zpa_client_secret)
        self.assertNotIn('synthetic-key', json.dumps(result))

    def test_zpa_file_success_and_partial_failure_have_safe_status(self):
        for failure in (False, True):
            reader, client = self.reader()
            context=ZPAContext('https://config.example.com', '42', 'private-token', 'cert')
            with patch('site_reference.ZTBClient', return_value=client), patch('site_reference.prepare_zpa', return_value=context,
                    side_effect=RuntimeError('raw-secret') if failure else None) as prepare:
                result=reader.connect_file(BASE+ZPA)
            self.assertIn('zpa_error' if failure else 'zpa', result)
            self.assertEqual(prepare.call_args.kwargs, {'write_env':False, 'quiet':True})
            self.assertIsNone(prepare.call_args.args[0].env_path)
            for secret in ('raw-secret','synthetic-secret','private-token','synthetic-key'):
                self.assertNotIn(secret,json.dumps(result))
            if failure:self.assertIsNone(reader.zpa_context);self.assertIsNone(reader.zpa_config)

    def test_invalid_file_preserves_connection_but_failed_auth_clears_both(self):
        reader, client=self.reader();reader.client=client;old=object();reader.zpa_context=old
        with self.assertRaises(ValueError):reader.connect_file('invalid')
        self.assertIs(reader.client,client);self.assertIs(reader.zpa_context,old)
        with patch('site_reference.ZTBClient', side_effect=RuntimeError('raw-secret')):
            with self.assertRaises(ValueError) as exc:reader.connect_file(BASE)
        self.assertIsNone(reader.client);self.assertIsNone(reader.zpa_context)
        self.assertNotIn('raw-secret',str(exc.exception))

    def test_upload_route_requires_local_auth_expires_preview_and_blocks_active_jobs(self):
        refs=Mock();refs.connect_file.return_value={'tenant':'customer-api.example.com','sites':[]}
        session=DeploymentSession(refs,'/unused')
        handler=object.__new__(Handler)
        handler.server=SimpleNamespace(server_address=('127.0.0.1',9876),token='local-token',references=refs,deployment=session)
        handler.path='/api/connections/import'
        body=json.dumps({'content':BASE}).encode()
        def call(token='local-token',origin='http://localhost:9876'):
            handler.headers={'Host':'localhost:9876','Origin':origin,'X-Local-Token':token,'Content-Type':'application/json','Content-Length':str(len(body))}
            handler.rfile=io.BytesIO(body);handler.reply=Mock();handler.do_POST()
            return handler.reply.call_args.args
        self.assertEqual(call(token='wrong')[0],403)
        self.assertEqual(call(origin='https://external.example.com')[0],403)
        refs.connect_file.assert_not_called()
        session.job={'state':'ready'}
        self.assertEqual(call()[0],200);self.assertEqual(session.job['state'],'expired')
        refs.connect_file.assert_called_once_with(BASE)
        for state in ('previewing','deploying'):
            session.job={'state':state};self.assertEqual(call()[0],400)
        self.assertEqual(refs.connect_file.call_count,1)

    def test_example_download_is_static_and_no_arbitrary_env_file_is_served(self):
        handler=object.__new__(Handler)
        handler.server=SimpleNamespace(server_address=('127.0.0.1',9876))
        handler.headers={'Host':'localhost:9876'};handler.reply=Mock()
        handler.path='/customer-example.env';handler.do_GET()
        status,content,mime=handler.reply.call_args.args
        self.assertEqual(status,200);self.assertIn(b'YOUR_API_KEY',content)
        self.assertTrue(mime.startswith('text/plain'))
        for path in ('/.env','/customer-a.env','/../.env'):
            handler.path=path;handler.do_GET();self.assertEqual(handler.reply.call_args.args[0],404)

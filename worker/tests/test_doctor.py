"""Metadata-only diagnostics must not turn private-helper tokens into env errors."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from worker import __main__ as cli
from worker.crm_transport import ORIGIN


class MetadataDoctorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = {'iva_root':str(self.root),'enabled':True,'mode':'ingest',
            'crm_transport':'iva_vpn','crm_url':ORIGIN}
        self.token = self.root/'fixed-private-token'
        self.token.write_text('PRIVATE_SECRET_MUST_NOT_BE_OPENED')

    def test_private_helper_credential_uses_only_fixed_metadata_and_no_env_value(self):
        with patch.object(cli,'CRM_TOKEN',self.token), patch.object(cli,'CRM_SOCKET',self.root/'missing-socket'), \
                patch.dict(os.environ,{'LEAD_CRM_AGENT_TOKEN':''}), \
                patch.object(Path,'read_text',side_effect=AssertionError('no credential read')), \
                patch.object(Path,'read_bytes',side_effect=AssertionError('no credential read')):
            value = cli.doctor(self.config)
        self.assertTrue(value['crm_token_present'])
        self.assertFalse(value['crm_socket_present'])
        self.assertEqual(value['crm_credential_source'],'private_transport_helper_file_metadata')
        self.assertFalse(value['crm_credential_contents_checked'])
        self.assertFalse(value['crm_authentication_verified'])
        self.assertFalse(value['network_checks_performed'])
        self.assertFalse(value['email_sending'])
        self.assertNotIn('PRIVATE_SECRET',json.dumps(value))

    def test_missing_token_is_not_replaced_by_environment_or_caller_path(self):
        config = {**self.config,'crm_token_file':str(self.token)}
        with patch.object(cli,'CRM_TOKEN',self.root/'missing'), patch.dict(os.environ,{'LEAD_CRM_AGENT_TOKEN':'ENV_SECRET'}):
            value = cli.doctor(config)
        self.assertFalse(value['crm_token_present'])
        self.assertNotIn('ENV_SECRET',json.dumps(value))

    def test_direct_transport_is_explicitly_uninspected(self):
        with patch.object(cli,'fixed_metadata',side_effect=AssertionError('no private helper in direct mode')):
            value = cli.doctor({**self.config,'crm_transport':'direct'})
        self.assertIsNone(value['crm_token_present'])
        self.assertEqual(value['crm_credential_source'],'worker_environment_not_inspected')
        self.assertFalse(value['crm_authentication_verified'])

    def test_unreadable_metadata_reports_unknown_and_invalid_transport_fails(self):
        with patch.object(Path,'lstat',side_effect=PermissionError()):
            self.assertIsNone(cli.fixed_metadata(self.token))
        with self.assertRaises(ValueError):
            cli.doctor({**self.config,'crm_transport':'unbounded_proxy'})


if __name__ == '__main__':
    unittest.main()

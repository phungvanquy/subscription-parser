import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from subscription_parser import common


class EnvironmentTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / '.env'
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    def test_literal_assignments_and_process_environment_precedence(self):
        self.path.write_text(
            '# Saved identities\nexport DEVICE_ID="authorized-device-id" # comment\n'
            "SECOND_DEVICE_ID='second-authorized-id=='\n"
            'LITERAL=$(do-not-execute)\nREFERENCE=${DEVICE_ID}\n')
        result = common.load_environment(self.path)
        self.assertEqual(result['DEVICE_ID'], 'authorized-device-id')
        self.assertEqual(result['SECOND_DEVICE_ID'], 'second-authorized-id==')
        self.assertEqual(result['LITERAL'], '$(do-not-execute)')
        self.assertEqual(result['REFERENCE'], '${DEVICE_ID}')
        self.assertNotIn('DEVICE_ID', os.environ)
        with patch.dict(os.environ, {'DEVICE_ID': 'process-authorized-id'}):
            self.assertEqual(common.load_environment(self.path)['DEVICE_ID'], 'process-authorized-id')

    def test_invalid_env_syntax_does_not_disclose_values(self):
        self.path.write_text('DEVICE_ID="private-unterminated-value\n')
        with self.assertRaises(common.SubscriptionError) as caught:
            common.load_environment(self.path)
        self.assertEqual(str(caught.exception), 'invalid .env assignment on line 1')

    def test_missing_environment_does_not_create_device_id(self):
        self.assertEqual(common.load_environment(self.path), {})
        self.assertFalse(self.path.exists())

    def test_atomic_replacement_keeps_private_permissions(self):
        self.path.write_text('DEVICE_ID=old-authorized-id\n')
        self.path.chmod(0o644)
        common.atomic_write(self.path, 'DEVICE_ID=updated-authorized-id\n')
        self.assertEqual(self.path.read_text(), 'DEVICE_ID=updated-authorized-id\n')
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])


if __name__ == '__main__':
    unittest.main()

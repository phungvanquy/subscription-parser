import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import unittest

from scripts.import_state import import_archive, import_state
from subscription_parser import common
from subscription_parser.storage import Store


class StateImportTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.source, self.destination = self.root / 'source', self.root / 'destination'
        common.atomic_write(self.source / '.env', 'DEVICE_ID=existing-authorized-id\nPARSER_ADMIN_TOKEN=existing-admin-key\nPARSER_SUBSCRIPTION_TOKEN=existing-link-token\n')
        common.atomic_write(self.source / '.parser-state/providers.json', json.dumps({'refresh_seconds': 3600, 'providers': []}))
        common.atomic_write(self.source / '.parser-state/snapshot.json', json.dumps({'exports': {'v2board.txt': 'synthetic'}}))
        common.atomic_write(self.source / '.parser-state/cache/source.json', json.dumps({'body': 'synthetic'}))
        common.atomic_write(self.source / '.parser-state/app.lock', 'old lock must not be copied')

    def import_source(self):
        return import_state(self.source, self.destination, uid=os.getuid(), gid=os.getgid())

    def test_import_preserves_identities_and_snapshot_with_private_permissions(self):
        self.assertEqual(self.import_source(), 4)
        for relative in ['.env', '.parser-state/providers.json', '.parser-state/snapshot.json', '.parser-state/cache/source.json']:
            self.assertEqual((self.source / relative).read_bytes(), (self.destination / relative).read_bytes())
            self.assertEqual((self.destination / relative).stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.destination / '.parser-state/app.lock').read_text(), '')

    def test_existing_data_cannot_be_overwritten(self):
        self.import_source()
        before = (self.destination / '.env').read_bytes()
        with self.assertRaisesRegex(common.SubscriptionError, 'already contains'):
            self.import_source()
        self.assertEqual((self.destination / '.env').read_bytes(), before)

    def test_source_symlink_and_invalid_json_are_rejected_before_copying(self):
        path = self.source / '.parser-state/cache/source.json'
        path.unlink()
        path.symlink_to(self.source / '.env')
        with self.assertRaisesRegex(common.SubscriptionError, 'regular files'):
            self.import_source()
        self.assertFalse((self.destination / '.env').exists())
        path.unlink()
        (self.source / '.parser-state/providers.json').write_text('not-json')
        with self.assertRaisesRegex(common.SubscriptionError, 'invalid JSON'):
            self.import_source()
        self.assertFalse((self.destination / '.env').exists())

    def test_import_refuses_a_running_application_lock(self):
        with common.process_lock(self.destination / '.parser-state/app.lock'):
            with self.assertRaisesRegex(common.SubscriptionError, 'already running'):
                self.import_source()
        self.assertFalse((self.destination / '.env').exists())

    def test_streamed_archive_and_path_traversal(self):
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode='w:gz') as archive:
            archive.add(self.source / '.env', arcname='.env')
            archive.add(self.source / '.parser-state', arcname='.parser-state')
        stream.seek(0)
        self.assertEqual(import_archive(stream, self.destination), 4)
        self.assertEqual((self.source / '.env').read_bytes(), (self.destination / '.env').read_bytes())
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode='w:gz') as archive:
            member = tarfile.TarInfo('../outside')
            archive.addfile(member, io.BytesIO())
        stream.seek(0)
        with self.assertRaisesRegex(common.SubscriptionError, 'invalid state archive'):
            import_archive(stream, self.root / 'other')

    def test_sqlite_backup_restore_preserves_tokens_records_and_cache(self):
        original = Store(self.source)
        original.add_hwid('Second device', 'second-authorized-id')
        original.rotate_subscription_token()
        token = original.credential('subscription_token')
        original.save_subscription({'name': 'Additional subscription', 'all_providers': True})
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode='w:gz') as archive:
            for name in ('app.sqlite3', '.parser-state', 'legacy-backup'):
                archive.add(self.source / name, arcname=name)
        stream.seek(0)
        self.assertEqual(import_archive(stream, self.destination), 5)
        restored = Store(self.destination)
        self.assertEqual(restored.credential('subscription_token'), token)
        self.assertEqual(restored.subscriptions(), original.subscriptions())
        self.assertEqual(restored.hwids(), original.hwids())
        self.assertEqual(restored.config(), original.config())
        self.assertEqual((self.destination/'.parser-state/cache/source.json').read_bytes(),
                         (self.source/'.parser-state/cache/source.json').read_bytes())
        self.assertFalse((self.destination/'.env').exists())
        with self.assertRaisesRegex(common.SubscriptionError, 'already contains'):
            self.import_source()

    def test_invalid_database_and_unfinished_journal_are_rejected(self):
        (self.source/'app.sqlite3').write_bytes(b'not a database')
        with self.assertRaisesRegex(common.SubscriptionError, 'invalid'):
            self.import_source()
        self.assertFalse(self.destination.exists())
        (self.source/'app.sqlite3').unlink()
        Store(self.source)
        (self.source/'app.sqlite3-journal').write_bytes(b'unfinished transaction')
        with self.assertRaisesRegex(common.SubscriptionError, 'journal'):
            self.import_source()
        self.assertFalse(self.destination.exists())


if __name__ == '__main__':
    unittest.main()

import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from subscription_parser import common
from subscription_parser.app import create_app
from subscription_parser.storage import Store, SCHEMA_VERSION, CREDENTIALS_SCHEMA


class StorageTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.environment = patch.dict(os.environ, {'PARSER_ADMIN_TOKEN': 'original-admin-key'}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def legacy(self):
        env = ('DEVICE_ID=first-authorized-id\nSECOND_DEVICE_ID=second-authorized-id\n'
               'UNUSED_DEVICE_ID=\nPARSER_ADMIN_TOKEN=original-admin-key\n'
               'PARSER_SUBSCRIPTION_TOKEN=original-link-key\nPARSER_PUBLIC_URL=https://subscriptions.example\n')
        config = {'refresh_seconds': 900, 'providers': [
            {'id': 'one', 'name': 'First', 'url': 'https://one.example/private', 'device_id_env': 'DEVICE_ID', 'user_agent': 'Original/1', 'enabled': True},
            {'id': 'two', 'name': 'Second', 'url': 'https://two.example/private', 'device_id_env': 'SECOND_DEVICE_ID', 'user_agent': 'Original/2', 'enabled': False}]}
        common.atomic_write(self.root/'.env', env)
        common.atomic_write(self.root/'.parser-state/providers.json', json.dumps(config))
        common.atomic_write(self.root/'.parser-state/snapshot.json', '{"exports":{"v2board.txt":"original-export"}}')
        common.atomic_write(self.root/'.parser-state/cache/one.json', '{"body":"original-cache"}')
        return env, config

    def test_migration_preserves_records_tokens_references_order_and_cached_files(self):
        env, original = self.legacy()
        files = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in [
            self.root/'.parser-state/snapshot.json', self.root/'.parser-state/cache/one.json']}
        with patch.dict(os.environ, {'DEVICE_ID': 'wrong-process-identity', 'PARSER_ADMIN_TOKEN': 'wrong-admin-key'}):
            store = Store(self.root)
        with store.connection() as connection:
            self.assertEqual([row[0] for row in connection.execute('SELECT id FROM subscriptions')], ['default'])
        self.assertEqual(store.credential('subscription_token'), 'original-link-key')
        hwids = {item['id']: item for item in store.hwids()}
        actual = store.config()
        self.assertEqual(actual['refresh_seconds'], original['refresh_seconds'])
        for old, new in zip(original['providers'], actual['providers'], strict=True):
            self.assertEqual(hwids[new['hwid_id']]['name'], old['device_id_env'])
            self.assertEqual({k:v for k,v in new.items() if k not in {'hwid_id', 'server_limit'}}, {k:v for k,v in old.items() if k != 'device_id_env'})
            self.assertIsNone(new['server_limit'])
        self.assertEqual(store.hwid_value(actual['providers'][0]['hwid_id']), 'first-authorized-id')
        self.assertEqual(store.hwid_value(actual['providers'][1]['hwid_id']), 'second-authorized-id')
        self.assertFalse(next(h for h in hwids.values() if h['name']=='UNUSED_DEVICE_ID')['configured'])
        self.assertFalse((self.root/'.env').exists())
        self.assertFalse((self.root/'.parser-state/providers.json').exists())
        self.assertEqual((self.root/'legacy-backup/environment.txt').read_text(), env)
        for path in [store.database_path, self.root/'legacy-backup/environment.txt', self.root/'legacy-backup/providers.json']:
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        for path, digest in files.items():
            self.assertEqual(hashlib.sha256(Path(path).read_bytes()).hexdigest(), digest)
        self.assertEqual(Store(self.root).config(), actual)

    def test_atomic_migration_failure_leaves_originals_and_retry_preserves_keys(self):
        env, config = self.legacy()
        with patch('subscription_parser.storage.os.replace', side_effect=OSError('simulated disk failure')):
            with self.assertRaises(OSError):
                Store(self.root)
        self.assertFalse((self.root/'app.sqlite3').exists())
        self.assertEqual((self.root/'.env').read_text(), env)
        self.assertEqual(json.loads((self.root/'.parser-state/providers.json').read_text()), config)
        self.assertEqual(Store(self.root).credential('subscription_token'), 'original-link-key')

    def test_restart_resumes_archiving_without_reimporting_or_rotating(self):
        self.legacy()
        with patch.object(Store, '_archive_legacy', side_effect=RuntimeError('simulated interruption')):
            with self.assertRaises(RuntimeError):
                Store(self.root)
        self.assertTrue((self.root/'app.sqlite3').exists())
        self.assertTrue((self.root/'.env').exists())
        store = Store(self.root)
        before = store.config()
        store.rotate_subscription_token()
        token = store.credential('subscription_token')
        shutil.copyfile(self.root/'legacy-backup/environment.txt', self.root/'.env')
        reloaded = Store(self.root)
        self.assertEqual(reloaded.credential('subscription_token'), token)
        self.assertEqual(reloaded.config(), before)
        self.assertFalse((self.root/'.env').exists())

    def test_changed_legacy_data_after_commit_is_not_discarded(self):
        self.legacy()
        with patch.object(Store, '_archive_legacy', side_effect=RuntimeError):
            with self.assertRaises(RuntimeError):
                Store(self.root)
        with (self.root/'.env').open('a') as output:
            output.write('NEW_DEVICE_ID=additional-fixed-id\n')
        with self.assertRaisesRegex(common.SubscriptionError, 'changed after migration'):
            Store(self.root)
        self.assertIn('NEW_DEVICE_ID', (self.root/'.env').read_text())

    def test_missing_legacy_credentials_or_hwid_refuse_migration(self):
        self.legacy()
        (self.root/'.env').write_text('DEVICE_ID=first-authorized-id\nSECOND_DEVICE_ID=second-authorized-id\n')
        with self.assertRaisesRegex(common.SubscriptionError, 'credentials are missing'):
            Store(self.root)
        self.assertFalse((self.root/'app.sqlite3').exists())
        self.legacy()
        (self.root/'.env').write_text('DEVICE_ID=first-authorized-id\nPARSER_ADMIN_TOKEN=original-admin-key\nPARSER_SUBSCRIPTION_TOKEN=original-link-key\n')
        with self.assertRaisesRegex(common.SubscriptionError, 'missing HWID'):
            Store(self.root)
        self.assertFalse((self.root/'app.sqlite3').exists())

    def test_running_legacy_app_blocks_migration(self):
        self.legacy()
        with common.process_lock(self.root/'.parser-state/app.lock'):
            with self.assertRaisesRegex(common.SubscriptionError, 'already running'):
                Store(self.root)
        self.assertFalse((self.root/'app.sqlite3').exists())

    def test_public_url_must_be_explicit_during_web_migration(self):
        self.legacy()
        with self.assertRaisesRegex(common.SubscriptionError, 'PARSER_PUBLIC_URL'):
            create_app(self.root, start_scheduler=False)
        self.assertFalse((self.root/'app.sqlite3').exists())
        with patch.dict(os.environ, {'PARSER_PUBLIC_URL': 'https://subscriptions.example'}):
            app = create_app(self.root, start_scheduler=False)
        self.assertEqual(app.state.manager.store.credential('subscription_token'), 'original-link-key')

    def test_config_only_env_is_never_modified_by_application_writes(self):
        text = 'PARSER_PUBLIC_URL=https://subscriptions.example\nPARSER_PORT=8099\nPARSER_ADMIN_TOKEN=deployment-admin-key\n'
        common.atomic_write(self.root/'.env', text)
        store = Store(self.root)
        first = store.add_hwid('My phone', 'existing-authorized-id')
        store.rotate_subscription_token()
        self.assertEqual((self.root/'.env').read_text(), text)
        self.assertEqual(store.hwid_value(first['id']), 'existing-authorized-id')
        self.assertFalse((self.root/'legacy-backup').exists())

    def test_failed_provider_write_rolls_back_settings_and_all_records(self):
        self.legacy()
        store = Store(self.root)
        before = store.config()
        store.save_subscription({'name': 'Bound subscription', 'provider_ids': [before['providers'][0]['id']]})
        bindings = store.subscriptions()
        invalid = store.config()
        invalid['refresh_seconds'] = 1800
        invalid['providers'][0]['hwid_id'] = 'missing'
        with self.assertRaises(common.SubscriptionError):
            store.save_config(invalid)
        self.assertEqual(store.config(), before)
        self.assertEqual(store.subscriptions(), bindings)
        with store.connection(write=True) as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute('DELETE FROM hwids WHERE id = ?', (before['providers'][0]['hwid_id'],))

    def test_concurrent_writes_preserve_all_identities(self):
        store = Store(self.root)
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda i: store.add_hwid(f'Device {i}', f'authorized-identity-{i}'), range(20)))
        self.assertEqual(len(store.hwids()), 20)
        self.assertEqual(len({h['id'] for h in store.hwids()}), 20)
        with store.connection() as connection:
            self.assertEqual(connection.execute('PRAGMA integrity_check').fetchone()[0], 'ok')

    def test_missing_or_future_database_never_regenerates_credentials(self):
        self.legacy()
        store = Store(self.root)
        with store.connection(write=True) as connection:
            connection.execute('PRAGMA user_version = 99')
        with self.assertRaisesRegex(common.SubscriptionError, 'unsupported'):
            Store(self.root)
        store.database_path.unlink()
        with self.assertRaisesRegex(common.SubscriptionError, 'credentials are missing'):
            Store(self.root)
        self.assertFalse(store.database_path.exists())

    def test_version_three_connectivity_migration_preserves_data_and_checks_rollback(self):
        self.legacy()
        store = Store(self.root)
        before = store.config()
        identities = store.hwids()
        token = store.credential('subscription_token')
        self.downgrade_subscriptions(store)
        with store.connection(write=True) as connection:
            connection.execute('ALTER TABLE settings DROP COLUMN check_seconds')
            connection.execute('PRAGMA user_version = 3')
        connect = sqlite3.connect
        class FailingConnection(sqlite3.Connection):
            def execute(self, sql, *args):
                if sql == 'PRAGMA user_version = 4':
                    raise sqlite3.OperationalError('simulated disk failure')
                return super().execute(sql, *args)
        with patch('subscription_parser.storage.sqlite3.connect', side_effect=lambda *a, **kw: connect(*a, **kw, factory=FailingConnection)):
            with self.assertRaises(common.SubscriptionError):
                Store(self.root)
        with store.connection() as connection:
            self.assertEqual(connection.execute('PRAGMA user_version').fetchone()[0], 3)
            self.assertNotIn('check_seconds', [row[1] for row in connection.execute('PRAGMA table_info(settings)')])
        upgraded = Store(self.root)
        self.assertEqual(upgraded.config(), before)
        self.assertEqual(upgraded.hwids(), identities)
        self.assertEqual(upgraded.credential('subscription_token'), token)
        changed = upgraded.config(); changed['check_seconds'] = 900
        upgraded.save_config(changed)
        self.assertEqual(Store(self.root).config()['check_seconds'], 900)

    def downgrade_subscriptions(self, store):
        token = store.credential('subscription_token')
        with store.connection(write=True) as connection:
            for table in ('auth_settings', 'auth_sessions', 'auth_recovery', 'auth_limits'):
                connection.execute('DROP TABLE ' + table)
            connection.execute(CREDENTIALS_SCHEMA)
            connection.execute('INSERT INTO credentials VALUES (?, ?)', ('subscription_token', token))
            connection.execute('DROP TABLE subscription_providers')
            connection.execute('DROP TABLE subscriptions')
            connection.execute('PRAGMA user_version = 4')

    def test_subscription_migration_is_atomic_and_preserves_existing_link_and_data(self):
        self.legacy()
        store = Store(self.root)
        config, devices, token = store.config(), store.hwids(), store.credential('subscription_token')
        self.downgrade_subscriptions(store)
        connect = sqlite3.connect
        class FailingConnection(sqlite3.Connection):
            def execute(self, sql, *args):
                if sql == 'PRAGMA user_version = 5':
                    raise sqlite3.OperationalError('simulated migration failure')
                return super().execute(sql, *args)
        with patch('subscription_parser.storage.sqlite3.connect', side_effect=lambda *a, **kw: connect(*a, **kw, factory=FailingConnection)):
            with self.assertRaises(common.SubscriptionError):
                Store(self.root)
        with store.connection() as connection:
            self.assertEqual(connection.execute('PRAGMA user_version').fetchone()[0], 4)
            self.assertEqual(connection.execute('SELECT value FROM credentials').fetchone()[0], token)
            self.assertIsNone(connection.execute("SELECT 1 FROM sqlite_master WHERE name = 'subscriptions'").fetchone())
        upgraded = Store(self.root)
        self.assertEqual(upgraded.config(), config)
        self.assertEqual(upgraded.hwids(), devices)
        self.assertEqual(upgraded.subscriptions(), [{'id': 'default', 'name': 'All providers', 'token': token,
                                                     'all_providers': True, 'provider_ids': [],
                                                     'server_policy': {'revision': 0, 'all_servers': True}}])
        with upgraded.connection() as connection:
            self.assertEqual(connection.execute('PRAGMA user_version').fetchone()[0], SCHEMA_VERSION)
            self.assertEqual(connection.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_many_subscriptions_persist_without_a_fixed_count_limit(self):
        store = Store(self.root)
        for number in range(101):
            store.save_subscription({'name': f'Subscription {number}', 'all_providers': True})
        reloaded = Store(self.root)
        self.assertEqual(len(reloaded.subscriptions()), 102)
        self.assertEqual(len({s['token'] for s in reloaded.subscriptions()}), 102)

    def version_one(self):
        self.legacy()
        store = Store(self.root)
        self.downgrade_subscriptions(store)
        with store.connection(write=True) as connection:
            connection.execute('ALTER TABLE credentials RENAME TO new_credentials')
            connection.execute("CREATE TABLE credentials (name TEXT PRIMARY KEY CHECK(name IN ('admin_token', 'subscription_token')), value TEXT NOT NULL CHECK(length(value) > 0))")
            connection.execute('INSERT INTO credentials SELECT * FROM new_credentials')
            connection.execute("INSERT INTO credentials VALUES ('admin_token', 'original-admin-key')")
            connection.execute('DROP TABLE new_credentials')
            connection.execute('ALTER TABLE hwids DROP COLUMN revision')
            connection.execute('ALTER TABLE settings DROP COLUMN check_seconds')
            connection.execute('PRAGMA user_version = 1')
        return store

    def test_sqlite_upgrade_removes_admin_key_and_preserves_all_application_data(self):
        previous = self.version_one()
        config = previous.config()
        with previous.connection() as connection:
            hwids = [tuple(row) for row in connection.execute('SELECT id, name, value FROM hwids ORDER BY id')]
        cookie_files = {str(path): path.read_bytes() for path in self.root.rglob('*.json')}
        store = Store(self.root)
        self.assertEqual(store.config(), config)
        self.assertEqual(store.credential('subscription_token'), 'original-link-key')
        with store.connection() as connection:
            self.assertEqual([tuple(row) for row in connection.execute('SELECT id, name, value FROM hwids ORDER BY id')], hwids)
            self.assertEqual(connection.execute('PRAGMA user_version').fetchone()[0], SCHEMA_VERSION)
            self.assertEqual([row[0] for row in connection.execute('SELECT id FROM subscriptions')], ['default'])
            self.assertEqual(connection.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        for path, data in cookie_files.items():
            self.assertEqual(Path(path).read_bytes(), data)
        self.assertEqual(Store(self.root).hwids(), store.hwids())

    def test_sqlite_upgrade_requires_configured_admin_key_and_stopped_old_app(self):
        store = self.version_one()
        before = store.database_path.read_bytes()
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(common.SubscriptionError, 'PARSER_ADMIN_TOKEN'):
                Store(self.root)
        self.assertEqual(store.database_path.read_bytes(), before)
        with common.process_lock(store.directory / 'app.lock'):
            with self.assertRaisesRegex(common.SubscriptionError, 'already running'):
                Store(self.root)
        self.assertEqual(store.database_path.read_bytes(), before)

    def test_sqlite_upgrade_failure_rolls_back_both_credentials_and_version(self):
        store = self.version_one()
        connect = sqlite3.connect

        class FailingConnection(sqlite3.Connection):
            def execute(self, sql, *args):
                if sql == 'DROP TABLE old_credentials':
                    raise sqlite3.OperationalError('simulated migration failure')
                return super().execute(sql, *args)

        with patch('subscription_parser.storage.sqlite3.connect', side_effect=lambda *a, **kw: connect(*a, **kw, factory=FailingConnection)):
            with self.assertRaises(common.SubscriptionError):
                Store(self.root)
        with store.connection() as connection:
            self.assertEqual(connection.execute('PRAGMA user_version').fetchone()[0], 1)
            self.assertEqual(dict(connection.execute('SELECT name, value FROM credentials')),
                             {'admin_token': 'original-admin-key', 'subscription_token': 'original-link-key'})
        self.assertEqual(Store(self.root).credential('subscription_token'), 'original-link-key')

    def test_version_two_device_migration_preserves_records_and_allows_explicit_none(self):
        self.legacy()
        store=Store(self.root)
        original=store.config()
        self.downgrade_subscriptions(store)
        with store.connection(write=True) as connection:
            connection.execute('ALTER TABLE hwids DROP COLUMN revision')
            connection.execute('ALTER TABLE settings DROP COLUMN check_seconds')
            connection.execute('PRAGMA user_version = 2')
        with common.process_lock(store.directory/'app.lock'):
            with self.assertRaisesRegex(common.SubscriptionError,'already running'):
                Store(self.root)
        store=Store(self.root)
        self.assertEqual(store.config(),original)
        self.assertEqual(store.credential('subscription_token'),'original-link-key')
        self.assertTrue(all(h['revision']==1 for h in store.hwids()))
        original['providers'][0]['hwid_id']=None
        store.save_config(original)
        self.assertIsNone(Store(self.root).config()['providers'][0]['hwid_id'])

    def test_device_migration_failure_rolls_back_schema_and_assignments(self):
        self.legacy()
        store=Store(self.root)
        before=store.config()
        self.downgrade_subscriptions(store)
        with store.connection(write=True) as connection:
            connection.execute('ALTER TABLE hwids DROP COLUMN revision')
            connection.execute('ALTER TABLE settings DROP COLUMN check_seconds')
            connection.execute('PRAGMA user_version = 2')
        connect=sqlite3.connect
        class FailingConnection(sqlite3.Connection):
            def execute(self,sql,*args):
                if sql=='DROP TABLE old_providers':
                    raise sqlite3.OperationalError('simulated disk failure')
                return super().execute(sql,*args)
        with patch('subscription_parser.storage.sqlite3.connect',side_effect=lambda *a,**kw:connect(*a,**kw,factory=FailingConnection)):
            with self.assertRaises(common.SubscriptionError):
                Store(self.root)
        self.assertEqual(store.config(),before)
        with store.connection() as connection:
            self.assertEqual(connection.execute('PRAGMA user_version').fetchone()[0],2)
            self.assertNotIn('revision',[row[1] for row in connection.execute('PRAGMA table_info(hwids)')])
        self.assertEqual(Store(self.root).config(),before)

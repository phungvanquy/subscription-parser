"""Per-provider random selections bound both probes and published servers."""
import base64
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from subscription_parser import common, connectivity, protocols, service
from subscription_parser.app import create_app
from subscription_parser.storage import Store, SCHEMA_VERSION


def source(count=200, prefix='node'):
    return json.dumps({'outbounds': [
        {'type': 'vless', 'tag': f'{prefix}-{i}', 'server': f'{prefix}-{i}.example',
         'server_port': 443, 'uuid': '01234567-89ab-4cde-8fab-0123456789ab'}
        for i in range(count)]}).encode()


class SamplingTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        environment = patch.dict(os.environ, {'PARSER_ADMIN_TOKEN': 'sampling-test'}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        validation = patch.object(service, 'checked_combine', side_effect=lambda nodes, root:
                                  protocols.combine(nodes) | {'validation': {}})
        self.validation = validation.start()
        self.addCleanup(validation.stop)
        self.body = source()
        self.downloads, self.probes = [], []
        self.fail_download = False
        self.failed = set()
        def fetch(url, *args):
            self.downloads.append(url)
            if self.fail_download:
                raise common.SubscriptionError('synthetic download failure')
            return self.body, {'fetched_at': common.now()}
        def tester(node, core, *args):
            self.probes.append((node.identity, core))
            return connectivity.result('failed' if node.identity in self.failed else 'passed', latency_ms=1)
        def checker(nodes, root, **kwargs):
            return connectivity.check_entries(nodes, root, tester=tester, **kwargs)
        self.fetcher, self.checker = fetch, checker
        self.app = create_app(self.root, start_scheduler=False, fetcher=fetch, checker=checker)
        self.manager = self.app.state.manager
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)
        self.assertEqual(self.client.post('/api/login', json={'token': 'sampling-test'}).status_code, 200)

    def add_provider(self, limit=20, name='Provider'):
        response = self.client.post('/api/providers', json={
            'name': name, 'url': f'https://{name.lower()}.example/sub', 'hwid_id': None, 'server_limit': limit})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def refresh(self, seed='first-draw', **kwargs):
        original = service.secrets.token_hex
        with patch.object(service.secrets, 'token_hex', side_effect=lambda n: seed if n == 16 else original(n)):
            self.manager.refresh(**kwargs)
        self.assertFalse(self.manager.snapshot.get('last_error'))
        return {identity.split(':')[0] for identity in self.manager.snapshot['connectivity']}

    def cache_path(self, provider):
        return self.manager.store.directory / 'cache' / (provider['id'] + '.json')

    def test_random_twenty_of_two_hundred_bound_probes_and_every_feed(self):
        provider = self.add_provider()
        selected = self.refresh()
        first_twenty = {node.identity for node in protocols.parse(self.body, 'Provider')[:20]}
        self.assertEqual(len(selected), 20)
        self.assertNotEqual(selected, first_twenty)
        self.assertEqual(len(self.probes), 60)
        self.assertEqual({identity for identity, core in self.probes}, selected)
        state = self.client.get('/api/state').json()
        row = state['providers'][0]
        self.assertEqual((row['server_limit'], row['count'], row['fetched_count']), (20, 20, 200))
        self.assertEqual(state['total'], 20)
        self.assertEqual(set(state['counts'].values()), {20})
        for name, url in state['links'].items():
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200, name)
            if name == 'combined.json':
                self.assertEqual({node['id'] for node in response.json()['servers']}, {i[:16] for i in selected})
                self.assertFalse(response.json()['excluded_servers'])
        self.assertEqual(base64.b64decode(json.loads(self.cache_path(provider).read_text())['body']), self.body)

    def test_duplicates_and_fetch_order_do_not_bias_selection(self):
        provider = {'name': 'Provider', 'server_limit': 20}
        entries, count, _ = service.Manager.provider_entries(self.body, provider, 'fixed-seed')
        document = json.loads(self.body)
        document['outbounds'] = list(reversed(document['outbounds'])) + document['outbounds'][:1] * 100
        reordered, new_count, _ = service.Manager.provider_entries(json.dumps(document).encode(), provider, 'fixed-seed')
        self.assertEqual((count, new_count), (200, 200))
        self.assertEqual({n.identity for n in entries}, {n.identity for n in reordered})

    def test_rechecks_failed_downloads_and_restart_keep_the_same_draw(self):
        provider = self.add_provider()
        selected = self.refresh()
        seed = json.loads(self.cache_path(provider).read_text())['sample_seed']
        self.assertEqual(self.refresh(download=False, check=True), selected)
        self.assertEqual(len(self.downloads), 1)
        self.fail_download = True
        self.assertEqual(self.refresh(seed='must-not-replace'), selected)
        self.assertEqual(json.loads(self.cache_path(provider).read_text())['sample_seed'], seed)
        restarted = service.Manager(self.root, self.fetcher, self.checker)
        restarted.refresh(download=False, check=True)
        self.assertFalse(restarted.snapshot.get('last_error'))
        self.assertEqual(set(restarted.snapshot['connectivity']), selected)
        self.assertEqual(len(self.downloads), 2)

    def test_each_successful_fetch_draws_again_even_for_an_unchanged_body(self):
        self.add_provider()
        first = self.refresh(seed='first-draw')
        self.probes.clear()
        second = self.refresh(seed='second-draw')
        self.assertEqual(len(second), 20)
        self.assertNotEqual(first, second)
        self.assertEqual({identity for identity, core in self.probes}, second)
        self.assertEqual(len(self.probes), 60)
        self.assertEqual(len(self.manager.snapshot['inventory']), 20)

    def test_limit_edits_use_cached_data_and_omitted_field_preserves_limit(self):
        provider = self.add_provider()
        self.refresh()
        path = '/api/providers/' + provider['id']
        for limit, expected in ((7, 7), (500, 200), (None, 200)):
            response = self.client.put(path, json={'name': 'Provider', 'server_limit': limit})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()['server_limit'], limit)
            self.assertEqual(self.manager.snapshot['total'], expected)
            self.assertEqual(len(self.downloads), 1)
        self.client.put(path, json={'name': 'Provider', 'server_limit': 7})
        selected = {row['id'] for row in self.manager.snapshot['inventory']}
        response = self.client.put(path, json={'name': 'Renamed'})
        self.assertEqual(response.json()['server_limit'], 7)
        self.assertEqual({row['id'] for row in self.manager.snapshot['inventory']}, selected)
        self.assertEqual(len(self.downloads), 1)

    def test_failed_sampled_servers_are_not_replaced_or_published(self):
        self.add_provider()
        selected = self.refresh()
        self.failed = set(sorted(selected)[:5])
        self.probes.clear()
        self.assertEqual(self.refresh(download=False, check=True), selected)
        self.assertEqual(len(self.probes), 60)
        self.assertEqual(self.manager.snapshot['published_total'], 15)
        self.assertEqual(self.manager.snapshot['total'], 20)
        archive = json.loads(self.manager.snapshot['exports']['combined.json'])
        self.assertEqual({row['id'] for row in archive['servers']}, {i[:16] for i in selected - self.failed})

    def test_preview_validates_only_sample_and_save_reuses_the_exact_draw(self):
        values = {'name': 'Preview', 'url': 'https://preview.example/sub', 'hwid_id': None, 'server_limit': 20}
        self.validation.reset_mock()
        preview = self.client.post('/api/providers/test', json=values)
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertEqual((preview.json()['fetched_count'], preview.json()['selected_count']), (200, 20))
        validated = {node.identity for node in self.validation.call_args.args[0]}
        self.assertEqual(len(validated), 20)
        saved = self.client.post('/api/providers', json=values | {'test_token': preview.json()['test_token']})
        self.assertEqual(saved.status_code, 201, saved.text)
        self.assertEqual(len(self.downloads), 1)
        self.assertFalse(self.probes)
        self.assertEqual(self.refresh(download=False, check=True), validated)
        self.assertEqual(len(self.downloads), 1)

    def test_preview_cannot_be_saved_with_a_different_limit(self):
        values = {'name': 'Preview', 'url': 'https://preview.example/sub', 'hwid_id': None, 'server_limit': 20}
        preview = self.client.post('/api/providers/test', json=values).json()
        response = self.client.post('/api/providers', json=values | {'test_token': preview['test_token'], 'server_limit': 21})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(self.manager.store.config()['providers'])

    def test_invalid_limits_are_rejected_before_saving_or_fetching(self):
        for limit in (0, -1, 100001, 1.5, True, '20'):
            with self.subTest(limit=limit):
                values = {'name': 'Invalid', 'url': 'https://invalid.example/sub', 'hwid_id': None, 'server_limit': limit}
                self.assertEqual(self.client.post('/api/providers', json=values).status_code, 422)
                self.assertEqual(self.client.post('/api/providers/test', json=values).status_code, 422)
                with self.assertRaises(common.SubscriptionError):
                    self.manager.store.provider(values)
        self.assertFalse(self.downloads)
        self.assertFalse(self.manager.store.config()['providers'])

    def test_old_cache_gets_one_persisted_draw_when_a_limit_is_added(self):
        provider = self.add_provider(None)
        self.refresh()
        path = self.cache_path(provider)
        cache = json.loads(path.read_text())
        del cache['sample_seed']
        common.atomic_write(path, common.json_text(cache))
        response = self.client.put('/api/providers/' + provider['id'], json={'name': 'Provider', 'server_limit': 20})
        self.assertEqual(response.status_code, 200, response.text)
        selected = {row['id'] for row in self.manager.snapshot['inventory']}
        seed = json.loads(path.read_text())['sample_seed']
        restarted = service.Manager(self.root, self.fetcher, self.checker)
        restarted.refresh(download=False, check=True)
        self.assertEqual({row['id'] for row in restarted.snapshot['inventory']}, selected)
        self.assertEqual(json.loads(path.read_text())['sample_seed'], seed)
        self.assertEqual(len(self.downloads), 1)

    def test_failed_rebuild_after_lowering_limit_cannot_serve_old_large_feeds(self):
        provider = self.add_provider(None)
        self.refresh()
        link = self.client.get('/api/state').json()['links']['xray.json']
        self.assertEqual(self.client.get(link).status_code, 200)
        with patch.object(service, 'checked_combine', side_effect=RuntimeError('synthetic failure')):
            response = self.client.put('/api/providers/' + provider['id'], json={'name': 'Provider', 'server_limit': 20})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get(link).status_code, 503)
        restarted = create_app(self.root, start_scheduler=False, fetcher=self.fetcher, checker=self.checker)
        with TestClient(restarted) as client:
            self.assertEqual(client.get(link).status_code, 503)

    def test_expiry_after_restart_does_not_expand_or_redraw_the_sample(self):
        self.add_provider()
        selected = self.refresh()
        for cores in self.manager.snapshot['connectivity'].values():
            for result in cores.values():
                result['checked_ts'] = time.time() - 2000
        self.manager.snapshot['valid_until'] = time.time() - 1
        common.atomic_write(self.manager.snapshot_path, common.json_text(self.manager.snapshot))
        restarted = service.Manager(self.root, self.fetcher, self.checker)
        restarted.ensure_publications()
        self.assertEqual({row['id'] for row in restarted.snapshot['inventory']}, {i[:16] for i in selected})
        self.assertEqual(restarted.snapshot['published_total'], 0)
        self.assertEqual(len(self.downloads), 1)

    def test_provider_limits_apply_before_combining_scoped_subscriptions(self):
        first = self.add_provider(20, 'First')
        self.refresh()
        first_selection = {row['id'] for row in self.manager.snapshot['inventory']}
        second = self.add_provider(7, 'Second')
        self.body = source(100, 'second')
        self.refresh(selected=second['id'])
        values = {'name': 'Second only', 'provider_ids': [second['id']]}
        response = self.client.post('/api/subscriptions', json=values)
        self.assertEqual(response.status_code, 201, response.text)
        scoped = next(item for item in self.client.get('/api/state').json()['subscriptions'] if item['name'] == 'Second only')
        self.assertEqual((self.manager.snapshot['total'], scoped['total']), (27, 7))
        feed = self.client.get(scoped['links']['combined.json']).json()
        self.assertEqual(len(feed['servers']), 7)
        self.assertFalse({row['id'] for row in feed['servers']} & first_selection)

    def test_selected_native_nodes_keep_unselected_dependencies(self):
        document = json.loads(source(5))
        for node in document['outbounds'][1:]:
            node['detour'] = document['outbounds'][0]['tag']
        raw = json.dumps(document).encode()
        entries, _, _ = service.Manager.provider_entries(raw, {'name': 'Native', 'server_limit': 1}, 'dependency-draw')
        self.assertEqual(len(entries), 1)
        self.assertIn('detour', entries[0].outputs['hiddify'])
        bundled = protocols.bundle(entries[0], 'hiddify', 'Selected')
        self.assertEqual(len(bundled), 2)


class LimitMigrationTests(unittest.TestCase):
    def test_version_six_upgrade_is_atomic_and_preserves_existing_data(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = Store(root)
            config = store.config()
            config['providers'] = [store.provider({'name': 'Existing', 'url': 'https://existing.example/sub', 'hwid_id': None})]
            store.save_config(config)
            token = store.credential('subscription_token')
            with store.connection(write=True) as connection:
                connection.execute('ALTER TABLE providers DROP COLUMN server_limit')
                connection.execute('PRAGMA user_version = 6')
            connect = sqlite3.connect
            class FailingConnection(sqlite3.Connection):
                def execute(self, sql, *args):
                    if sql == 'PRAGMA user_version = 7':
                        raise sqlite3.OperationalError('synthetic migration failure')
                    return super().execute(sql, *args)
            with patch('subscription_parser.storage.sqlite3.connect', side_effect=lambda *a, **kw:
                       connect(*a, **kw, factory=FailingConnection)):
                with self.assertRaises(common.SubscriptionError):
                    Store(root)
            with store.connection() as connection:
                self.assertEqual(connection.execute('PRAGMA user_version').fetchone()[0], 6)
                self.assertNotIn('server_limit', {row['name'] for row in connection.execute('PRAGMA table_info(providers)')})
            upgraded = Store(root)
            self.assertEqual(upgraded.config(), config)
            self.assertEqual(upgraded.credential('subscription_token'), token)
            with upgraded.connection() as connection:
                self.assertEqual(connection.execute('PRAGMA user_version').fetchone()[0], SCHEMA_VERSION)

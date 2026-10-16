import copy
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import yaml
from fastapi.testclient import TestClient
from subscription_parser import common, connectivity, publication
from subscription_parser.app import create_app

UUID = '01234567-89ab-4cde-8fab-0123456789ab'


class SubscriptionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        env = patch.dict(common.os.environ, {'PARSER_ADMIN_TOKEN': 'subscription-test-admin'}, clear=True)
        env.start(); self.addCleanup(env.stop)
        self.fetches, self.checks, self.failed = [], [], set()
        def fetch(url, *args):
            self.fetches.append(url)
            host = url.split('/')[2]
            return json.dumps({'outbounds': [
                {'type': 'vless', 'tag': host, 'server': host, 'server_port': 443, 'uuid': UUID},
                {'type': 'vless', 'tag': 'Shared', 'server': 'shared.example', 'server_port': 443, 'uuid': UUID}
            ]}).encode(), {'fetched_at': common.now()}
        def tester(entry, core, *args):
            self.checks.append((entry.identity, core))
            return {'status': 'failed' if entry.name in self.failed else 'passed', 'latency_ms': 10}
        def checker(entries, root, **kwargs):
            return connectivity.check_entries(entries, root, **kwargs, tester=tester)
        self.app = create_app(self.root, start_scheduler=False, fetcher=fetch, checker=checker)
        self.manager = self.app.state.manager
        self.client = TestClient(self.app)
        self.client.__enter__(); self.addCleanup(self.client.__exit__, None, None, None)
        self.client.post('/api/login', json={'token': 'subscription-test-admin'})
        self.providers = [self.client.post('/api/providers', json={
            'name': name, 'url': 'https://' + name + '.example/private', 'hwid_id': None}).json()
            for name in ('Alpha', 'Beta', 'Gamma')]
        self.manager.refresh()
        self.assertNotIn('last_error', self.manager.snapshot)

    def create(self, name, providers=None, all_providers=False):
        response = self.client.post('/api/subscriptions', json={
            'name': name, 'provider_ids': [p['id'] for p in providers or []], 'all_providers': all_providers})
        self.assertEqual(response.status_code, 201, response.text)
        self.assertNotIn('last_error', self.manager.snapshot)
        return response.json()['id']

    def groups(self):
        response = self.client.get('/api/state')
        self.assertEqual(response.status_code, 200)
        return {s['id']: s for s in response.json()['subscriptions']}

    def test_provider_refresh_only_downloads_and_checks_its_own_servers(self):
        self.create('Beta only', self.providers[1:2])
        provider = self.providers[0]
        before = copy.deepcopy(self.manager.snapshot)
        deadlines = (time.time() + 1800, time.time() + 300)
        self.manager.next_refresh, self.manager.next_check = deadlines
        selected_ids = {node['id'] for node in before['inventory'] if provider['name'] in node['sources']}
        expected = {(identity, core) for identity, values in before['connectivity'].items()
                    if identity[:16] in selected_ids for core in values}
        self.fetches.clear(); self.checks.clear()
        response = self.client.post('/api/refresh', params={'provider_id': provider['id']})
        self.assertEqual(response.status_code, 202)
        self.manager.worker.join(5)
        self.assertFalse(self.manager.busy)
        self.assertNotIn('last_error', self.manager.snapshot)
        self.assertEqual(self.fetches, ['https://Alpha.example/private'])
        self.assertEqual(set(self.checks), expected)
        self.assertEqual(len(self.checks), 6)  # Alpha's own node plus the shared node, in three cores.
        for identity, values in before['connectivity'].items():
            if identity[:16] not in selected_ids:
                self.assertEqual(self.manager.snapshot['connectivity'][identity], values)
        self.assertEqual((self.manager.next_refresh, self.manager.next_check), deadlines)
        self.assertEqual(self.manager.snapshot['history'][0]['provider_id'], provider['id'])
        self.assertEqual(self.groups()['default']['published_total'], 4)
        checked = next(row['context'] for row in self.manager.logs.recent()['entries'] if row['event'] == 'checks_completed')
        self.assertEqual(checked['passed'], 6)

    def test_saving_a_tested_provider_checks_only_missing_results_in_background(self):
        before = copy.deepcopy(self.manager.snapshot['connectivity'])
        next_due = min(value['checked_ts'] for values in before.values() for value in values.values()) + 600
        self.fetches.clear(); self.checks.clear()
        body = {'name': 'Delta', 'url': 'https://Delta.example/private', 'hwid_id': None}
        preview = self.client.post('/api/providers/test', json=body)
        self.assertEqual(preview.status_code, 200)
        saved = self.client.post('/api/providers', json={**body, 'test_token': preview.json()['test_token']})
        self.assertEqual(saved.status_code, 201)
        self.assertEqual(self.checks, [])
        self.manager.begin_check(reuse_recent=True)
        self.manager.worker.join(5)
        self.assertFalse(self.manager.busy)
        self.assertNotIn('last_error', self.manager.snapshot)
        self.assertEqual(self.fetches, ['https://Delta.example/private'])
        self.assertEqual(len(self.checks), 3)
        self.assertTrue(all(identity not in before for identity, _ in self.checks))
        for identity, values in before.items():
            self.assertEqual(self.manager.snapshot['connectivity'][identity], values)
        self.assertAlmostEqual(self.manager.next_check, next_due, places=2)
        self.assertEqual(self.manager.check_progress, {'done': 3, 'total': 3})

    def test_paused_or_unknown_provider_refresh_cannot_start_global_work(self):
        provider = self.providers[0]
        self.client.put('/api/providers/' + provider['id'], json={'name': 'Alpha', 'enabled': False})
        self.fetches.clear(); self.checks.clear()
        for identifier in (provider['id'], 'missing', ''):
            with self.subTest(identifier=identifier):
                response = self.client.post('/api/refresh', params={'provider_id': identifier})
                self.assertEqual(response.status_code, 400)
                self.assertFalse(self.manager.busy)
        self.assertEqual(self.fetches, [])
        self.assertEqual(self.checks, [])

    def test_changed_or_failed_provider_response_never_rechecks_unrelated_nodes(self):
        provider = self.providers[0]
        original = self.manager.fetcher
        def changed(url, *args):
            body, metadata = original(url, *args)
            document = json.loads(body)
            document['outbounds'] = document['outbounds'][:1]
            document['outbounds'][0]['server_port'] = 8443
            return json.dumps(document).encode(), metadata
        unrelated = {node['id'] for node in self.manager.snapshot['inventory'] if provider['name'] not in node['sources']}
        before = copy.deepcopy(self.manager.snapshot['connectivity'])
        self.fetches.clear(); self.checks.clear()
        self.manager.fetcher = changed
        self.manager.refresh(selected=provider['id'])
        self.assertNotIn('last_error', self.manager.snapshot)
        self.assertEqual(self.fetches, ['https://Alpha.example/private'])
        self.assertEqual(len(self.checks), 3)
        self.assertTrue(all(identity not in before for identity, _ in self.checks))
        self.assertEqual(self.groups()['default']['published_total'], 4)
        new_records = copy.deepcopy(self.manager.snapshot['connectivity'])
        for identity in unrelated:
            key = next(key for key in before if key[:16] == identity)
            self.assertEqual(new_records[key], before[key])
        self.checks.clear()
        with patch.object(self.manager, 'fetcher', side_effect=common.SubscriptionError('provider returned HTTP 503')):
            self.manager.refresh(selected=provider['id'])
        self.assertNotIn('last_error', self.manager.snapshot)
        self.assertEqual(len(self.checks), 3)
        self.assertEqual(self.manager.snapshot['providers'][provider['id']]['status'], 'cached')
        for identity in unrelated:
            key = next(key for key in before if key[:16] == identity)
            self.assertEqual(self.manager.snapshot['connectivity'][key], before[key])

    def test_metadata_edits_preserve_checks_and_explicit_full_operations_recheck_everything(self):
        self.fetches.clear(); self.checks.clear()
        self.manager.next_check = time.time() + 300
        deadline = self.manager.next_check
        provider = self.providers[0]
        self.client.put('/api/providers/' + provider['id'], json={'name': 'Renamed Alpha'})
        self.create('Alpha selection', [provider])
        self.assertEqual(self.fetches, [])
        self.assertEqual(self.checks, [])
        self.assertEqual(self.manager.next_check, deadline)
        self.manager.begin_check()
        self.manager.worker.join(5)
        self.assertEqual(len(self.checks), 12)
        self.assertEqual(self.fetches, [])
        self.checks.clear()
        self.manager.refresh()
        self.assertEqual(len(self.fetches), 3)
        self.assertEqual(len(self.checks), 12)

    def test_scheduled_refresh_reuses_recent_checks_after_downloading(self):
        before = copy.deepcopy(self.manager.snapshot['connectivity'])
        next_due = min(value['checked_ts'] for values in before.values() for value in values.values()) + 600
        self.fetches.clear(); self.checks.clear()
        self.manager.begin_refresh(reuse_recent=True)
        self.manager.worker.join(5)
        self.assertFalse(self.manager.busy)
        self.assertNotIn('last_error', self.manager.snapshot)
        self.assertEqual(len(self.fetches), 3)
        self.assertEqual(self.checks, [])
        self.assertEqual(self.manager.snapshot['connectivity'], before)
        self.assertAlmostEqual(self.manager.next_check, next_due, places=2)

    def test_restart_labels_saved_uri_counts_until_native_feed_is_built(self):
        self.create('Scoped', self.providers[:1])
        snapshot = copy.deepcopy(self.manager.snapshot)
        for saved in [snapshot, *snapshot.get('publications', {}).values()]:
            saved['exports'].pop('v2board.json')
            saved['counts']['v2board'] = saved['counts'].pop('v2board_uri')
            for server in saved['inventory']:
                server['clients'] = [client for client in server['clients'] if client != 'v2board_uri']
        common.atomic_write(self.manager.snapshot_path, common.json_text(snapshot))
        restarted = create_app(self.root, start_scheduler=False).state.manager
        for saved in [restarted.snapshot, *restarted.snapshot.get('publications', {}).values()]:
            self.assertEqual(saved['counts']['v2board'], 0)
            self.assertGreater(saved['counts']['v2board_uri'], 0)
            self.assertTrue(all('v2board' not in server['clients'] for server in saved['inventory']))
        self.assertEqual(restarted.snapshot['exports']['v2board.txt'], snapshot['exports']['v2board.txt'])

    def assert_feeds(self, group, hosts, sources):
        for file, link in group['links'].items():
            response = self.client.get(link)
            self.assertEqual(response.status_code, 200, (file, response.text))
            self.assertEqual(response.headers['cache-control'], 'no-store')
            if file == 'combined.json':
                archive = response.json()
                self.assertEqual(len(archive['servers']), group['published_total'])
                self.assertEqual({source for node in archive['servers'] for source in node['sources']}, sources)
                self.assertTrue(all(note['source'] in sources for note in archive['diagnostics']))
                actual = {n['server'] for n in archive['hiddify']['outbounds']}
            elif file == 'v2board.json':
                actual = {n['server'] for n in response.json()['nodes']}
            elif file == 'hiddify.json':
                actual = {n['server'] for n in response.json()['outbounds']}
            elif file == 'mihomo.yaml':
                actual = {n['server'] for n in yaml.safe_load(response.text)['proxies']}
            elif file == 'xray.json':
                actual = {n['settings']['vnext'][0]['address'] for n in response.json()['outbounds']}
            else:
                from urllib.parse import urlsplit
                actual = {urlsplit(line).hostname for line in response.text.splitlines()}
                hosts = {host.lower() for host in hosts}
            self.assertEqual({host.lower() for host in actual}, {host.lower() for host in hosts}, file)

    def test_one_multiple_and_all_sources_have_independent_links_and_exact_feeds(self):
        legacy = self.groups()['default']['links']
        a = self.create('Phone', self.providers[:1])
        ab = self.create('Family', self.providers[:2])
        another = self.create('Same selection', self.providers[:2])
        all_id = self.create('Everything', all_providers=True)
        groups = self.groups()
        self.assertEqual(len({g['links']['v2board.txt'] for g in groups.values()}), 5)
        self.assertEqual(groups['default']['links'], legacy)
        self.assert_feeds(groups[a], {'Alpha.example', 'shared.example'}, {'Alpha'})
        self.assert_feeds(groups[ab], {'Alpha.example', 'Beta.example', 'shared.example'}, {'Alpha', 'Beta'})
        self.assertEqual(groups[all_id]['published_total'], 4)
        self.assertEqual(groups[another]['counts'], groups[ab]['counts'])
        # Equivalent selections reuse the published artifact, regardless of name/token.
        self.assertEqual(len(self.manager.snapshot['publications']), 2)
        self.assertEqual(len(self.fetches), 3)
        self.assertEqual(len(self.checks), 12)
        self.checks.clear()
        self.manager.refresh(download=False, check=True)
        self.assertEqual(len(self.fetches), 3)
        self.assertEqual(len(self.checks), 12)
        public = self.client.get('/api/state').text
        for private in (UUID, '/private', 'publications', 'fingerprint', 'subscription-test-admin'):
            self.assertNotIn(private, public)

    def test_selection_updates_keep_urls_and_cannot_fall_back_to_global_exports(self):
        identifier = self.create('Phone', self.providers[:1])
        before = self.groups()[identifier]['links']
        # A failed rebuild leaves the newly selected, unpublished scope unavailable.
        with patch('subscription_parser.service.checked_combine', side_effect=RuntimeError('synthetic failure')):
            response = self.client.put('/api/subscriptions/' + identifier, json={
                'name': 'Phone renamed', 'provider_ids': [self.providers[1]['id']]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get(before['combined.json']).status_code, 503)
        self.manager.refresh(download=False, check=False)
        after = self.groups()[identifier]
        self.assertEqual(before, after['links'])
        self.assert_feeds(after, {'Beta.example', 'shared.example'}, {'Beta'})

    def test_independent_rotation_deletion_and_restart_persist_scopes(self):
        first = self.create('Phone', self.providers[:1])
        second = self.create('Family', self.providers[1:])
        before = self.groups()
        response = self.client.post('/api/subscriptions/' + first + '/rotate-link')
        self.assertEqual(response.status_code, 200)
        after = self.groups()
        self.assertNotEqual(after[first]['links'], before[first]['links'])
        self.assertEqual(after[second]['links'], before[second]['links'])
        self.assertEqual(after['default']['links'], before['default']['links'])
        self.assertEqual(self.client.get(before[first]['links']['hiddify.json']).status_code, 404)
        restarted = create_app(self.root, start_scheduler=False, fetcher=lambda *args: self.fail('must not fetch'))
        client = TestClient(restarted)
        self.addCleanup(client.close)
        for group in after.values():
            self.assertEqual(client.get(group['links']['v2board.txt']).status_code, 200)
        self.assertEqual(self.client.delete('/api/subscriptions/' + second).status_code, 200)
        self.assertEqual(self.client.get(before[second]['links']['combined.json']).status_code, 404)
        self.assertEqual(self.client.get(after[first]['links']['combined.json']).status_code, 200)
        self.assertEqual(len(self.manager.store.config()['providers']), 3)
        self.assertEqual(self.client.delete('/api/subscriptions/default').status_code, 400)

    def test_pause_rename_settings_and_delete_preserve_binding_and_empty_scope_never_means_all(self):
        identifier = self.create('Phone', self.providers[:1])
        provider = self.providers[0]
        self.client.put('/api/providers/' + provider['id'], json={'name': 'New label', 'enabled': False})
        self.client.put('/api/settings', json={'refresh_seconds': 900})
        group = self.groups()[identifier]
        self.assertEqual(group['provider_ids'], [provider['id']])
        self.assertEqual((group['total'], group['published_total']), (0, 0))
        self.assertEqual(self.client.get(group['links']['xray.json']).status_code, 503)
        archive = self.client.get(group['links']['combined.json']).json()
        self.assertEqual(archive['servers'], [])
        self.client.put('/api/providers/' + provider['id'], json={'name': 'New label', 'enabled': True})
        self.assert_feeds(self.groups()[identifier], {'Alpha.example', 'shared.example'}, {'New label'})
        self.client.delete('/api/providers/' + provider['id'])
        group = self.groups()[identifier]
        self.assertFalse(group['all_providers'])
        self.assertEqual(group['provider_ids'], [])
        self.assertEqual(group['total'], 0)
        self.assertEqual(self.groups()['default']['total'], 3)
        self.assertEqual(self.client.get(group['links']['v2board.txt']).status_code, 503)

    def test_failed_rebuild_after_pausing_provider_cannot_serve_its_old_exports_even_after_restart(self):
        identifier = self.create('Phone', self.providers[:1])
        link = self.groups()[identifier]['links']['combined.json']
        with patch('subscription_parser.service.checked_combine', side_effect=RuntimeError('synthetic failure')):
            self.client.put('/api/providers/' + self.providers[0]['id'], json={'name': 'Alpha', 'enabled': False})
        self.assertEqual(self.client.get(link).status_code, 503)
        self.assertEqual(self.groups()[identifier]['published_total'], 0)
        restarted = create_app(self.root, start_scheduler=False)
        client = TestClient(restarted)
        self.addCleanup(client.close)
        self.assertEqual(client.get(link).status_code, 503)

    def test_unselected_future_provider_only_joins_all_provider_subscriptions(self):
        identifier = self.create('Phone', self.providers[:1])
        self.client.post('/api/providers', json={'name': 'New', 'url': 'https://new.example/private', 'hwid_id': None})
        self.manager.refresh()
        groups = self.groups()
        self.assertEqual(groups['default']['total'], 5)
        self.assertEqual(groups[identifier]['total'], 2)
        self.assert_feeds(groups[identifier], {'Alpha.example', 'shared.example'}, {'Alpha'})

    def test_expiry_failed_checks_and_default_selection_are_scoped(self):
        identifier = self.create('Phone', self.providers[:1])
        self.client.put('/api/subscriptions/default', json={'name': 'Original', 'provider_ids': [self.providers[1]['id']]})
        self.failed.add('Alpha.example')
        self.manager.refresh(download=False, check=True)
        groups = self.groups()
        self.assertEqual(groups[identifier]['published_total'], 1)
        self.assert_feeds(groups[identifier], {'shared.example'}, {'Alpha'})
        self.assert_feeds(groups['default'], {'Beta.example', 'shared.example'}, {'Beta'})
        key = publication.scope_key(groups[identifier])
        self.manager.snapshot['publications'][key]['valid_until'] = time.time() - 1
        expired = self.groups()[identifier]
        self.assertEqual(expired['published_total'], 0)
        for link in expired['links'].values():
            self.assertEqual(self.client.get(link).status_code, 503)
        self.assertEqual(self.client.get(groups['default']['links']['xray.json']).status_code, 200)

    def test_admin_auth_validation_and_busy_changes(self):
        for body in ({'name': ' '}, {'name': 'empty', 'provider_ids': []},
                     {'name': 'unknown', 'provider_ids': ['missing']},
                     {'name': 'duplicate', 'provider_ids': [self.providers[0]['id']] * 2},
                     {'name': 'ambiguous', 'all_providers': True, 'provider_ids': [self.providers[0]['id']]},
                     {'name': 'All Providers', 'all_providers': True},
                     {'name': 'line\nbreak', 'all_providers': True}):
            self.assertEqual(self.client.post('/api/subscriptions', json=body).status_code, 400, body)
        self.assertEqual(len(self.groups()), 1)
        identifier = self.create('Phone', self.providers[:1])
        self.manager.busy = True
        try:
            self.assertEqual(self.client.delete('/api/subscriptions/' + identifier).status_code, 409)
        finally:
            self.manager.busy = False
        self.client.post('/api/logout')
        for method, url, body in [('get', '/api/subscriptions', None),
                ('post', '/api/subscriptions', {'name': 'new', 'all_providers': True}),
                ('put', '/api/subscriptions/' + identifier, {'name': 'new', 'all_providers': True}),
                ('post', '/api/subscriptions/' + identifier + '/rotate-link', None),
                ('delete', '/api/subscriptions/' + identifier, None)]:
            self.assertEqual(self.client.request(method, url, json=body).status_code, 401)

    def test_duplicate_native_variants_use_their_own_core_results(self):
        original = self.manager.fetcher
        def fetch(url, *args):
            body, meta = original(url, *args)
            nodes = json.loads(body)['outbounds']
            # Same portable identity, different native Xray mux settings.
            from subscription_parser import protocols
            entries = protocols.parse(json.dumps({'outbounds': nodes}).encode(), 'Beta')
            outbounds = [copy.deepcopy(entry.outputs['xray']) for entry in entries]
            if 'Beta' in url:
                outbounds[1]['mux'] = {'enabled': True, 'concurrency': 8}
            return json.dumps({'outbounds': outbounds}).encode(), meta
        self.manager.fetcher = fetch
        beta = self.create('Beta only', self.providers[1:2])
        self.manager.refresh()
        self.assertNotIn('last_error', self.manager.snapshot)
        group = self.groups()[beta]
        self.assertEqual(group['counts']['xray'], 2)
        response = self.client.get(group['links']['xray.json']).json()
        self.assertTrue(any(node.get('mux', {}).get('enabled') for node in response['outbounds']))
        self.assertTrue(any(':' in key for key in self.manager.snapshot['connectivity']))
        previous = copy.deepcopy(self.manager.snapshot['connectivity'])
        variant_key = next(key for key in previous if ':' in key and 'xray' in previous[key])
        primary_key = variant_key.split(':')[0]
        self.fetches.clear(); self.checks.clear()
        self.manager.refresh(selected=self.providers[1]['id'])
        self.assertNotIn('last_error', self.manager.snapshot)
        self.assertEqual(self.fetches, ['https://Beta.example/private'])
        self.assertEqual(len(self.checks), 6)
        self.assertEqual(self.manager.snapshot['connectivity'][primary_key]['xray'], previous[primary_key]['xray'])
        self.assertGreater(self.manager.snapshot['connectivity'][variant_key]['xray']['checked_ts'],
                           previous[variant_key]['xray']['checked_ts'])
        self.assertEqual(self.groups()[beta]['counts']['xray'], 2)

    def test_validation_rejection_in_another_scope_does_not_remove_selected_native_variant(self):
        from subscription_parser.validation import checked_combine
        beta = self.create('Beta only', self.providers[1:2])
        def validator(nodes, root):
            # Simulate a native variant rejected only in the global selection.
            if any(node.source == 'Alpha' for node in nodes):
                for node in nodes:
                    if node.name == 'Shared':
                        node.outputs.pop('xray', None)
            return checked_combine(nodes, root)
        with patch('subscription_parser.service.checked_combine', side_effect=validator):
            self.manager.refresh(download=False, check=True)
        self.assertNotIn('last_error', self.manager.snapshot)
        self.assertEqual(self.groups()[beta]['counts']['xray'], 2)


if __name__ == '__main__':
    unittest.main()

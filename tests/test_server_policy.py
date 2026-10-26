"""Subscription bindings survive provider churn; only tested variants publish."""
import base64
import copy
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from subscription_parser import common, connectivity, protocols, server_policy, service
from subscription_parser.app import create_app
from subscription_parser.storage import Store, SCHEMA_VERSION

UUID = '01234567-89ab-4cde-8fab-0123456789ab'


def node(name='Japan', host='japan.example', port=443):
    return {'type': 'vless', 'tag': name, 'server': host, 'server_port': port, 'uuid': UUID,
            'tls': {'enabled': True, 'server_name': 'tls.example'}}


class ServerPolicyTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        env = patch.dict(os.environ, {'PARSER_ADMIN_TOKEN': 'policy-test'}, clear=True)
        env.start(); self.addCleanup(env.stop)
        validation = patch.object(service, 'checked_combine', side_effect=lambda nodes, root:
                                  protocols.combine(nodes) | {'validation': {}})
        validation.start(); self.addCleanup(validation.stop)
        self.document = [node(), node('Berlin', 'berlin.example')]
        self.fetches, self.probes, self.failed = [], [], set()
        def fetch(url, *args):
            self.fetches.append(url)
            return json.dumps({'outbounds': self.document}).encode(), {'fetched_at': common.now()}
        def tester(entry, core, *args):
            endpoint, address, port = server_policy.endpoint(entry.outputs[core], core)
            self.probes.append((entry.identity, core, endpoint[address], endpoint.get(port)))
            return connectivity.result('failed' if (endpoint[address], core) in self.failed else 'passed', latency_ms=1)
        def checker(entries, root, **kwargs):
            return connectivity.check_entries(entries, root, tester=tester, **kwargs)
        self.app = create_app(self.root, start_scheduler=False, fetcher=fetch, checker=checker)
        self.manager = self.app.state.manager
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)
        self.client.post('/api/login', json={'token': 'policy-test'})
        self.provider = self.client.post('/api/providers', json={
            'name': 'Provider', 'url': 'https://provider.example/sub', 'hwid_id': None}).json()
        self.manager.refresh()
        self.subscription = self.create('Phone')
        self.probes.clear(); self.fetches.clear()

    def create(self, name):
        response = self.client.post('/api/subscriptions', json={
            'name': name, 'all_providers': True, 'provider_ids': []})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()['id']

    def policy(self, identifier=None, **params):
        response = self.client.get('/api/subscriptions/' + (identifier or self.subscription) + '/servers', params=params)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def record(self, source_name='Japan', **edits):
        candidate = next(row for row in self.policy()['available'] if row['source']['name'] == source_name)
        return {k: candidate[k] for k in ('provider_id', 'source_identity')} | edits

    def save(self, records, *, all_servers=False, identifier=None, revision=None, status=200):
        identifier = identifier or self.subscription
        policy = self.policy(identifier)
        response = self.client.put('/api/subscriptions/' + identifier + '/servers', json={
            'revision': policy['revision'] if revision is None else revision, 'all_servers': all_servers,
            'records': records})
        self.assertEqual(response.status_code, status, response.text)
        return response

    def saved_payload(self, record):
        return {k: record[k] for k in ('id', 'provider_id', 'source_identity', 'selected', 'name', 'address', 'port')}

    def check(self, reuse=True):
        self.manager.refresh(download=False, check=True, reuse_recent=reuse)
        self.assertFalse(self.manager.snapshot.get('last_error'))

    def feed(self, identifier=None, file='hiddify.json'):
        sub = self.manager.store.subscription(identifier or self.subscription)
        return self.client.get('/sub/' + sub['token'] + '/' + file)

    def test_selected_server_overrides_every_format_only_after_new_checks(self):
        original = copy.deepcopy(self.manager.snapshot['exports'])
        self.save([self.record(address='custom.example', port=8443, name='My Japan')])
        self.assertEqual(self.feed().status_code, 503)
        self.assertEqual(self.manager.snapshot['exports'], original)
        self.check()
        self.assertEqual(len(self.probes), 3)
        self.assertEqual({p[2:] for p in self.probes}, {('custom.example', 8443)})
        configured = self.feed().json()['outbounds']
        self.assertEqual(len(configured), 1)
        self.assertEqual((configured[0]['server'], configured[0]['server_port']), ('custom.example', 8443))
        self.assertIn('My Japan', configured[0]['tag'])
        self.assertEqual(configured[0]['tls']['server_name'], 'tls.example')
        self.assertEqual(configured[0]['uuid'], UUID)
        for filename in ('mihomo.yaml', 'xray.json', 'v2board.json', 'v2board.txt', 'combined.json'):
            response = self.feed(file=filename)
            self.assertEqual(response.status_code, 200, filename)
        self.assertEqual(self.fetches, [])
        self.assertEqual(self.manager.snapshot['exports'], original)

    def test_name_only_and_same_endpoint_reuse_source_checks(self):
        self.save([self.record(name='My Japan', address='japan.example', port=443)])
        self.assertEqual(self.feed().status_code, 200)
        self.check()
        self.assertEqual(self.probes, [])
        self.assertIn('My Japan', self.feed().json()['outbounds'][0]['tag'])

    def test_two_subscriptions_are_isolated_and_identical_variants_share_checks(self):
        router = self.create('Router')
        value = self.record(address='custom.example', port=8443)
        self.save([value])
        self.check()
        self.assertEqual(self.feed(router).json()['outbounds'][0]['server'], 'japan.example')
        self.probes.clear()
        self.save([value], identifier=router)
        self.check()
        self.assertEqual(self.probes, [])
        self.assertEqual(self.feed().json(), self.feed(router).json())
        record = self.saved_payload(self.policy(router)['records'][0]) | {'address': 'router.example'}
        self.save([record], identifier=router)
        self.check()
        self.assertEqual(self.feed(router).json()['outbounds'][0]['server'], 'router.example')
        self.assertEqual(self.feed().json()['outbounds'][0]['server'], 'custom.example')

    def test_failed_custom_endpoint_does_not_block_provider_or_other_subscription(self):
        self.failed = {('custom.example', core) for core in connectivity.CORES}
        self.save([self.record(address='custom.example')])
        self.check()
        self.assertEqual(self.feed().status_code, 503)
        self.assertEqual(self.feed('default').status_code, 200)
        self.assertEqual(self.manager.snapshot.get('selection_checks'), {})
        self.assertEqual(self.policy()['records'][0]['status'], 'pending_or_failed')

    def test_source_failure_blocks_previously_passing_override_and_skips_its_probe(self):
        self.save([self.record(address='custom.example')]); self.check()
        self.failed = {('japan.example', core) for core in connectivity.CORES}
        self.probes.clear(); self.check(reuse=False)
        self.assertEqual(self.feed().status_code, 503)
        self.assertNotIn('custom.example', {row[2] for row in self.probes})
        self.assertEqual(self.policy()['records'][0]['status'], 'source_failed')
        self.failed.clear(); self.check(reuse=False)
        self.assertEqual(self.feed().status_code, 200)

    def test_source_and_custom_health_are_required_for_each_core(self):
        self.failed = {('japan.example', 'mihomo'), ('custom.example', 'xray')}
        self.check(reuse=False)
        self.probes.clear()
        self.save([self.record(address='custom.example')]); self.check()
        self.assertEqual(self.feed().status_code, 200)
        self.assertEqual(self.feed(file='mihomo.yaml').status_code, 503)
        self.assertEqual(self.feed(file='xray.json').status_code, 503)
        self.assertNotIn(('custom.example', 'mihomo'), {(p[2], p[1]) for p in self.probes})

    def test_rejected_source_format_cannot_be_enabled_by_an_override(self):
        value = self.record(address='custom.example')
        def validate(nodes, root):
            for entry in nodes:
                if entry.source_entry is None and entry.name == 'Japan':
                    entry.outputs.pop('mihomo', None)
            return protocols.combine(nodes) | {'validation': {}}
        with patch.object(service, 'checked_combine', side_effect=validate):
            self.save([value]); self.check()
            self.assertEqual(self.feed(file='mihomo.yaml').status_code, 503)
            self.assertNotIn(('custom.example', 'mihomo'), {(p[2], p[1]) for p in self.probes})
            scope = self.manager.published_scopes[service.publication.scope_key(self.manager.store.subscription(self.subscription))]
            self.assertEqual(connectivity.current_result(scope[0][0], 'mihomo', self.manager.snapshot['connectivity'], 1200)['status'], 'unsupported')

    def test_custom_scope_revalidates_when_global_source_acceptance_changes(self):
        self.client.put('/api/subscriptions/' + self.subscription, json={
            'name': 'Phone', 'all_providers': False, 'provider_ids': [self.provider['id']]})
        def validate(nodes, root):
            if any(entry.source == 'Second' for entry in nodes):
                for entry in nodes:
                    entry.outputs.pop('mihomo', None)
            return protocols.combine(nodes) | {'validation': {}}
        with patch.object(service, 'checked_combine', side_effect=validate):
            self.save([self.record(address='custom.example')]); self.check()
            self.assertEqual(self.feed(file='mihomo.yaml').status_code, 200)
            self.client.post('/api/providers', json={'name': 'Second', 'url': 'https://second.example/sub', 'hwid_id': None})
            self.manager.refresh()
            self.assertEqual(self.feed(file='mihomo.yaml').status_code, 503)
            scope = self.manager.published_scopes[service.publication.scope_key(self.manager.store.subscription(self.subscription))]
            self.assertNotIn('mihomo', scope[0][0].source_entry.outputs)

    def test_missing_server_keeps_binding_through_restart_and_source_rename(self):
        self.save([self.record(address='custom.example', name='My Japan')]); self.check()
        saved = self.policy()['records'][0]
        original = self.document.pop(0)
        self.manager.refresh()
        missing = self.policy()['records'][0]
        self.assertEqual(missing['id'], saved['id'])
        self.assertEqual(missing['status'], 'unavailable')
        self.assertEqual(self.feed().status_code, 503)
        restarted = service.Manager(self.root, self.manager.fetcher, self.manager.checker)
        self.assertEqual(restarted.store.server_policy(self.subscription)['records'][0]['address'], 'custom.example')
        original['tag'] = 'Tokyo renamed'
        self.document.append(original)
        self.manager.refresh()
        returned = self.policy()['records'][0]
        self.assertEqual(returned['id'], saved['id'])
        self.assertEqual(returned['source_identity'], saved['source_identity'])
        self.assertEqual(returned['source']['name'], 'Tokyo renamed')
        self.assertIn('My Japan', self.feed().json()['outbounds'][0]['tag'])

    def test_changed_source_requires_explicit_relink_and_keeps_overrides(self):
        self.save([self.record(address='custom.example', name='My Japan')]); self.check()
        saved = self.policy()['records'][0]
        self.document[0]['uuid'] = '11234567-89ab-4cde-8fab-0123456789ab'
        self.manager.refresh()
        missing = self.policy()['records'][0]
        self.assertEqual(missing['status'], 'unavailable')
        suggestion = missing['suggestions'][0]
        self.assertNotEqual(suggestion['source_identity'], saved['source_identity'])
        updated = self.saved_payload(missing) | {'source_identity': suggestion['source_identity']}
        self.save([updated], status=400)
        self.save([updated | {'relink': True}]); self.check()
        returned = self.policy()['records'][0]
        self.assertEqual(returned['id'], saved['id'])
        self.assertEqual(returned['address'], 'custom.example')
        self.assertEqual(self.feed().json()['outbounds'][0]['uuid'], self.document[0]['uuid'])

    def test_provider_sampling_stays_authoritative_without_pinning_saved_servers(self):
        self.save([self.record(name='Remember Japan')])
        identity = protocols.parse(json.dumps({'outbounds': [node()]}).encode(), 'Provider')[0].identity
        self.document = [node(f'Node {i}', f'node-{i}.example') for i in range(20)] + [node()]
        config = self.manager.store.config(); config['providers'][0]['server_limit'] = 1
        self.manager.store.save_config(config)
        with patch.object(service.random.Random, 'sample', side_effect=lambda values, size:
                          [value for value in values if value != identity][:size]):
            self.manager.refresh()
            self.assertEqual(self.manager.snapshot['total'], 1)
        self.assertEqual(self.policy()['records'][0]['status'], 'unavailable')
        self.assertEqual(self.feed().status_code, 503)
        self.document = [node()]; self.manager.refresh()
        self.assertIn('Remember Japan', self.feed().json()['outbounds'][0]['tag'])

    def test_reset_and_deselect_keep_or_remove_edits_explicitly(self):
        self.save([self.record(address='custom.example')]); self.check()
        saved = self.saved_payload(self.policy()['records'][0])
        self.save([saved | {'selected': False}])
        self.assertEqual(self.feed().status_code, 503)
        self.assertEqual(self.policy()['records'][0]['address'], 'custom.example')
        self.save([saved | {'address': None}])
        self.assertEqual(self.feed().json()['outbounds'][0]['server'], 'japan.example')
        self.save([], all_servers=True)
        self.assertEqual(len(self.feed().json()['outbounds']), 2)

    def test_stale_revision_foreign_records_duplicates_and_invalid_edits_are_rejected(self):
        value = self.record()
        self.save([value])
        self.save([], revision=0, status=409)
        saved = self.saved_payload(self.policy()['records'][0])
        self.save([saved, saved], status=400)
        router = self.create('Router')
        self.save([saved], identifier=router, status=400)
        for field, bad in [('address', 'https://wrong.example'), ('address', 'has space'),
                           ('name', 'bad\nname'), ('address', '0.0.0.0')]:
            self.save([saved | {field: bad}], status=400)
        for value in (0, 65536, True, '443'):
            self.save([saved | {'port': value}], status=422)
        self.save([saved | {'tls': False}], status=422)

    def test_new_bindings_require_current_working_server(self):
        value = self.record()
        self.failed = {('japan.example', core) for core in connectivity.CORES}
        self.check(reuse=False)
        self.save([value], status=400)
        self.save([value | {'source_identity': '0' * 64}], status=400)
        self.assertEqual(self.policy()['revision'], 0)

    def test_editing_missing_or_removed_provider_record_is_preserved(self):
        self.save([self.record(address='custom.example')]); self.check()
        saved = self.saved_payload(self.policy()['records'][0])
        self.client.delete('/api/providers/' + self.provider['id'])
        self.save([saved | {'name': 'Still saved'}])
        self.assertEqual(self.policy()['records'][0]['status'], 'unavailable')
        self.assertEqual(self.policy()['records'][0]['name'], 'Still saved')

    def test_default_subscription_policy_does_not_filter_global_library(self):
        self.save([self.record(name='Chosen')], identifier='default')
        self.assertEqual(len(self.feed('default').json()['outbounds']), 1)
        self.assertEqual(len(self.manager.snapshot['inventory']), 2)
        self.assertEqual(len(self.feed().json()['outbounds']), 2)

    def test_search_pagination_authentication_and_busy_guard(self):
        first = self.policy(limit=1)
        second = self.policy(limit=1, offset=1)
        self.assertEqual(first['available_total'], 2)
        self.assertNotEqual(first['available'][0], second['available'][0])
        self.assertEqual(self.policy(q='japan')['available_total'], 1)
        self.manager.busy = True
        self.save([], status=409)
        self.manager.busy = False
        self.client.post('/api/logout')
        self.assertEqual(self.client.get('/api/subscriptions/' + self.subscription + '/servers').status_code, 401)

    def test_failed_rebuild_cannot_serve_previous_subscription_policy(self):
        self.save([self.record()])
        self.assertEqual(self.feed().status_code, 200)
        with patch.object(self.manager, '_publish', side_effect=RuntimeError('synthetic failure')):
            self.save([], all_servers=False)
        self.assertEqual(self.feed().status_code, 503)
        self.assertIn('check', self.manager.jobs.items)

    def test_restart_recovers_policy_commit_before_check_intent_was_written(self):
        value = self.record(address='custom.example')
        with patch.object(self.manager.jobs, 'put', side_effect=OSError('synthetic ledger failure')):
            self.save([value], status=500)
        self.assertEqual(self.feed().status_code, 503)
        restarted = service.Manager(self.root, self.manager.fetcher, self.manager.checker)
        self.assertIn('check', restarted.jobs.items)
        restarted.refresh(download=False, check=True, reuse_recent=True)
        self.assertEqual(restarted.subscription_snapshot(restarted.store.subscription(self.subscription))['published_total'], 1)

    def test_original_and_variant_checks_share_one_batch_deadline(self):
        self.save([self.record(address='custom.example')])
        checker = self.manager.checker
        deadlines = []
        def observe(*args, **kwargs):
            deadlines.append(kwargs['deadline'])
            return checker(*args, **kwargs)
        with patch.object(self.manager, 'checker', side_effect=observe):
            self.check()
        self.assertEqual(len(deadlines), 2)
        self.assertEqual(deadlines[0], deadlines[1])

    def test_source_expiry_after_restart_excludes_a_fresh_custom_check(self):
        self.save([self.record(address='custom.example')]); self.check()
        source_identity = protocols.parse(json.dumps({'outbounds': [node()]}).encode(), 'Provider')[0].identity
        old = time.time() - 2000
        for record_id, cores in self.manager.snapshot['connectivity'].items():
            if record_id.split(':')[0] == source_identity:
                for result in cores.values():
                    result['checked_ts'] = old
        for snapshot in [self.manager.snapshot, *self.manager.snapshot['publications'].values()]:
            snapshot['valid_until'] = old
        common.atomic_write(self.manager.snapshot_path, common.json_text(self.manager.snapshot))
        restarted = service.Manager(self.root, self.manager.fetcher, self.manager.checker)
        restarted.ensure_publications()
        snapshot = restarted.subscription_snapshot(restarted.store.subscription(self.subscription))
        self.assertEqual(snapshot['published_total'], 0)
        self.assertEqual(self.fetches, [])

    def test_late_variant_results_cannot_restore_a_disappeared_source(self):
        self.save([self.record(address='custom.example')]); self.check()
        old_results = copy.deepcopy(self.manager.snapshot['connectivity'])
        old_versions = dict(self.manager.snapshot['active_selections'])
        self.document.pop(0); self.manager.refresh()
        self.assertTrue(self.manager._publish(old_results, old_versions))
        self.assertEqual(self.feed().status_code, 503)
        self.assertEqual(self.policy()['records'][0]['status'], 'unavailable')

    def test_duplicate_sources_can_have_distinct_subscription_endpoints(self):
        self.client.post('/api/providers', json={'name': 'Second', 'url': 'https://second.example/sub', 'hwid_id': None})
        self.manager.refresh()
        matches = [row for row in self.policy()['available'] if row['source']['name'] == 'Japan']
        rows = [{k: match[k] for k in ('provider_id', 'source_identity')} | {'address': f'custom-{i}.example'}
                for i, match in enumerate(matches)]
        self.save(rows); self.check()
        self.assertEqual({node['server'] for node in self.feed().json()['outbounds']}, {'custom-0.example', 'custom-1.example'})

    def test_policy_removal_cascades_only_with_its_subscription(self):
        self.save([self.record(address='custom.example')])
        self.client.delete('/api/subscriptions/' + self.subscription)
        self.assertNotIn(self.subscription, self.manager.store.server_policies())

    def test_all_mode_applies_saved_overrides_and_keeps_other_servers(self):
        self.save([self.record(address='custom.example')], all_servers=True); self.check()
        self.assertEqual({node['server'] for node in self.feed().json()['outbounds']}, {'custom.example', 'berlin.example'})

    def test_native_only_upstream_changes_require_relinking(self):
        original = protocols.parse(json.dumps({'outbounds': [node()]}).encode(), 'Provider')[0]
        self.document = [original.outputs['xray'] | {'tag': 'Japan', 'mux': {'enabled': False}}]
        self.manager.refresh()
        self.save([self.record(address='custom.example')]); self.check()
        previous = self.policy()['records'][0]['source_identity']
        portable = self.manager.snapshot['inventory'][0]['id']
        self.document[0]['mux'] = {'enabled': True, 'concurrency': 8}
        self.manager.refresh()
        self.assertEqual(self.manager.snapshot['inventory'][0]['id'], portable)
        record = self.policy()['records'][0]
        self.assertEqual(record['status'], 'unavailable')
        self.assertNotEqual(record['suggestions'][0]['source_identity'], previous)
        self.assertEqual(self.feed().status_code, 503)

    def test_policy_request_size_is_bounded_without_relaxing_other_endpoints(self):
        path = '/api/subscriptions/' + self.subscription + '/servers'
        body = json.dumps({'revision': 0, 'all_servers': True, 'records': []})
        headers = {'content-type': 'application/json'}
        self.assertEqual(self.client.put(path, content=' ' * 20000 + body, headers=headers).status_code, 200)
        self.assertEqual(self.client.post('/api/subscriptions', content=' ' * 20000 + body, headers=headers).status_code, 413)
        self.assertEqual(self.client.put(path, content=' ' * 1048577, headers=headers).status_code, 413)
        self.assertEqual(self.client.put(path, content=iter([b' ' * 600000, b' ' * 600000]), headers=headers).status_code, 413)
        self.assertEqual(self.client.put(path, content=body, headers=headers | {'origin': 'https://foreign.example'}).status_code, 403)


class ServerPolicyFormatTests(unittest.TestCase):
    def test_source_bindings_ignore_uri_and_encoded_vmess_display_names(self):
        raw = {'v': '2', 'ps': 'First name', 'add': 'vmess.example', 'port': '443', 'id': UUID,
               'aid': '0', 'net': 'tcp', 'type': 'none', 'tls': ''}
        def parse_vmess():
            uri = 'vmess://' + base64.b64encode(json.dumps(raw).encode()).decode()
            return protocols.parse(uri.encode(), 'Provider')[0]
        before = server_policy.source_key(parse_vmess())
        raw['ps'] = 'New name'
        self.assertEqual(before, server_policy.source_key(parse_vmess()))
        raw['port'] = '8443'
        self.assertNotEqual(before, server_policy.source_key(parse_vmess()))
        uri = f'vless://{UUID}@server.example:443#'
        one = protocols.parse((uri + 'First').encode(), 'Provider')[0]
        two = protocols.parse((uri + 'Renamed').encode(), 'Provider')[0]
        self.assertEqual(server_policy.source_key(one), server_policy.source_key(two))

    def test_source_binding_survives_output_validation_and_retains_native_settings(self):
        original = protocols.parse(json.dumps({'outbounds': [node()]}).encode(), 'Provider')[0].outputs['xray']
        def parse(value):
            return protocols.parse(json.dumps({'outbounds': [value]}).encode(), 'Provider')[0]
        first = parse(original | {'mux': {'enabled': False}})
        second = parse(original | {'mux': {'enabled': True}})
        self.assertEqual(first.identity, second.identity)
        self.assertNotEqual(server_policy.source_key(first), server_policy.source_key(second))
        fresh = parse(original | {'mux': {'enabled': False}})
        fresh.outputs.pop('hiddify', None)
        self.assertEqual(server_policy.source_key(first), server_policy.source_key(fresh))

    def test_native_extras_and_dependency_are_preserved(self):
        raw = {'proxies': [
            {'type': 'vless', 'name': 'Main', 'server': 'source.example', 'port': 443, 'uuid': UUID,
             'dialer-proxy': 'Hop', 'native-extra': {'preserve': True}},
            {'type': 'socks5', 'name': 'Hop', 'server': 'hop.example', 'port': 1080}]}
        entry = protocols.parse(json.dumps(raw).encode(), 'Provider')[0]
        original = copy.deepcopy(entry)
        modified = server_policy.customize(entry, {'address': 'custom.example', 'port': 8443, 'name': 'Custom'})
        bundle = protocols.bundle(modified, 'mihomo', 'Custom')
        self.assertEqual(bundle[0]['native-extra'], {'preserve': True})
        self.assertEqual(bundle[0]['server'], 'custom.example')
        self.assertEqual(bundle[1]['server'], 'hop.example')
        self.assertEqual(entry, original)
        self.assertIs(modified.source_entry, entry)

    def test_hopping_ports_are_replaced_by_single_override(self):
        entry = protocols.parse(json.dumps({'outbounds': [{'type': 'hysteria2', 'tag': 'Hopping',
            'server': 'source.example', 'server_ports': ['443:445'], 'password': 'test-password',
            'tls': {'enabled': True, 'server_name': 'tls.example'}}]}).encode(), 'Provider')[0]
        edited = server_policy.customize(entry, {'port': 8443})
        self.assertNotIn('server_ports', edited.outputs['hiddify'])
        self.assertEqual(edited.outputs['hiddify']['server_port'], 8443)
        self.assertNotIn('ports', edited.outputs['mihomo'])
        self.assertEqual(edited.outputs['mihomo']['port'], 8443)

    def test_endpoint_validation_and_unknown_native_shapes(self):
        self.assertEqual(server_policy.overrides({'address': '[2001:db8::1]', 'name': '  '})['address'], '2001:db8::1')
        with self.assertRaises(common.SubscriptionError):
            server_policy.overrides({'port': True})
        for output in ({'settings': {}}, {'settings': {'servers': [{'address': 'a', 'port': 1}, {'address': 'b', 'port': 2}]}}):
            with self.assertRaises(common.SubscriptionError):
                server_policy.endpoint(output, 'xray')
        entry = protocols.Entry('Unknown', 'Provider', 'unknown', 'singbox', {}, outputs={'xray': {'settings': {}}})
        self.assertIsNone(server_policy.describe(entry)['address'])
        self.assertFalse(server_policy.customize(entry, {'address': 'custom.example'}).outputs)

    def test_migration_is_transactional_and_keeps_existing_links(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'PARSER_ADMIN_TOKEN': 'migration'}):
            root = Path(directory)
            store = Store(root)
            token = store.credential('subscription_token')
            with store.connection(write=True) as connection:
                connection.execute('DROP TABLE subscription_server_policies')
                connection.execute('PRAGMA user_version = 7')
            original = sqlite3.connect
            class FailingConnection(sqlite3.Connection):
                def execute(self, sql, *args):
                    if sql == 'PRAGMA user_version = 8':
                        raise sqlite3.OperationalError('synthetic migration failure')
                    return super().execute(sql, *args)
            with patch('subscription_parser.storage.sqlite3.connect', side_effect=lambda *a, **kw:
                       original(*a, **kw, factory=FailingConnection)):
                with self.assertRaises(common.SubscriptionError):
                    Store(root)
            with store.connection() as connection:
                self.assertEqual(connection.execute('PRAGMA user_version').fetchone()[0], 7)
                self.assertFalse(connection.execute("SELECT name FROM sqlite_master WHERE name='subscription_server_policies'").fetchall())
            upgraded = Store(root)
            self.assertEqual(upgraded.credential('subscription_token'), token)
            self.assertEqual(upgraded.server_policy('default'), {'revision': 0, 'all_servers': True, 'records': []})
            with upgraded.connection() as connection:
                self.assertEqual(connection.execute('PRAGMA user_version').fetchone()[0], SCHEMA_VERSION)

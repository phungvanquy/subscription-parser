import copy
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from subscription_parser import common, connectivity as c, protocols
from subscription_parser.app import create_app
from fastapi.testclient import TestClient


def entries():
    return protocols.parse(json.dumps({'outbounds': [
        {'type': 'vless', 'tag': name, 'server': name + '.example', 'server_port': 443,
         'uuid': '01234567-89ab-4cde-8fab-0123456789ab'} for name in ('good', 'bad')]}).encode(), 'Synthetic')


class ConnectivityTests(unittest.TestCase):
    def setUp(self):
        self.nodes = entries()
        self.root = Path(tempfile.mkdtemp())
        import shutil
        self.addCleanup(shutil.rmtree, self.root)

    def records(self, tester=None):
        return c.check_entries(self.nodes, self.root, tester=tester or (
            lambda node, core, *args: c.result('passed', latency_ms=15) if node.name == 'good' else c.result('failed', 'Test failed')))

    def test_filter_excludes_failed_nodes_without_destroying_originals(self):
        original = copy.deepcopy(self.nodes)
        filtered = c.filtered_entries(self.nodes, self.records(), 1200)
        self.assertEqual(set(filtered[0].outputs), {'mihomo', 'xray', 'hiddify'})
        self.assertFalse(filtered[1].outputs)
        self.assertEqual(self.nodes, original)

    def test_core_results_are_independent_and_unknown_never_passes(self):
        results = self.records(lambda node, core, *args: c.result('passed', latency_ms=10) if core == 'xray' else c.result('failed'))
        self.assertEqual(set(c.filtered_entries(self.nodes, results, 1200)[0].outputs), {'hiddify', 'xray'})
        self.assertTrue(all(not node.outputs for node in c.filtered_entries(self.nodes, {}, 1200)))

    def test_changed_credentials_conversion_or_dependency_cannot_reuse_health(self):
        results = self.records()
        changed = copy.deepcopy(self.nodes[0])
        changed.outputs['mihomo']['port'] = 8443
        self.assertEqual(c.current_result(changed, 'mihomo', results, 1200)['status'], 'pending')
        changed.identity = 'new-account'
        self.assertEqual(c.current_result(changed, 'xray', results, 1200)['status'], 'pending')

    def test_expired_and_future_results_are_not_reused(self):
        results = self.records()
        for offset in (-1300, 10):
            for value in results[self.nodes[0].identity].values():
                value['checked_ts'] = time.time() + offset
            self.assertFalse(c.filtered_entries(self.nodes, results, 1200)[0].outputs)

    def test_unsupported_cores_and_missing_binaries_are_distinct(self):
        self.nodes[0].outputs.pop('xray')
        results = self.records()
        self.assertEqual(results[self.nodes[0].identity]['xray']['status'], 'unsupported')
        with patch.dict(common.os.environ, {'PARSER_TOOLS_DIR': str(self.root)}):
            self.assertEqual(c.probe(self.nodes[0], 'mihomo', self.root, threading.Event(), time.monotonic()+5)['status'], 'error')

    def test_duplicate_nodes_are_checked_once_and_exceptions_are_redacted(self):
        calls = []
        def tester(*args):
            calls.append(True)
            raise RuntimeError('secret-password private-subscription-url')
        values = c.check_entries([self.nodes[0], self.nodes[0]], self.root, tester=tester)
        self.assertEqual(len(calls), 2)
        self.assertNotIn('secret-password', json.dumps(values))
        self.assertTrue(all(v['status'] == 'error' for v in values[self.nodes[0].identity].values()))

    def test_cancelled_or_over_budget_batches_leave_nodes_pending(self):
        stop = threading.Event(); stop.set()
        with patch.object(c, 'BATCH_TIMEOUT', 0):
            values = c.check_entries(self.nodes, self.root, stop=stop, tester=lambda *args: self.fail('must not probe'))
        self.assertTrue(all(v['status'] == 'pending' for row in values.values() for v in row.values()))
        self.assertFalse(c.filtered_entries(self.nodes, values, 1200)[0].outputs)

    def test_probe_configs_bind_only_to_loopback_and_route_to_exact_node(self):
        xray = c.configuration(self.nodes[0], 'xray', 24680)
        self.assertEqual(xray['inbounds'][0]['listen'], '127.0.0.1')
        self.assertEqual(xray['outbounds'][0]['protocol'], 'blackhole')
        self.assertEqual(xray['routing']['rules'][0]['outboundTag'], 'probe')
        mihomo = c.configuration(self.nodes[0], 'mihomo', 24681)
        self.assertFalse(mihomo['allow-lan'])
        self.assertEqual(mihomo['bind-address'], '127.0.0.1')
        self.assertEqual(mihomo['rules'], ['MATCH,probe'])


class ConnectivityWebTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        env = patch.dict(common.os.environ, {'PARSER_ADMIN_TOKEN': 'test-admin'})
        env.start(); self.addCleanup(env.stop)
        self.calls, self.mode = [], 'mixed'
        body = json.dumps({'outbounds': [entry.original for entry in entries()]}).encode()
        def fetch(*args):
            self.calls.append(args)
            return body, {'fetched_at': common.now()}
        def tester(node, core, *args):
            return c.result('passed', latency_ms=10) if self.mode == 'all' or node.name == 'good' else c.result('failed', 'HTTPS failed')
        def checker(nodes, root, **kwargs):
            return c.check_entries(nodes, root, **kwargs, tester=tester)
        self.app = create_app(self.root, start_scheduler=False, fetcher=fetch, checker=checker)
        self.manager = self.app.state.manager
        self.client = TestClient(self.app)
        self.client.__enter__(); self.addCleanup(self.client.__exit__, None, None, None)
        self.client.post('/api/login', json={'token': 'test-admin'})
        response = self.client.post('/api/providers', json={'name': 'Synthetic', 'url': 'https://provider.example/test', 'hwid_id': None})
        self.assertEqual(response.status_code, 201)

    def refresh(self):
        self.assertEqual(self.client.post('/api/refresh').status_code, 202)
        self.manager.worker.join(5)
        self.assertFalse(self.manager.busy)
        self.assertNotIn('last_error', self.manager.snapshot)
        return self.client.get('/api/state').json()

    def test_all_feeds_exclude_failed_nodes_and_inventory_keeps_them(self):
        state = self.refresh()
        self.assertEqual(state['total'], 2)
        self.assertEqual(state['published_total'], 1)
        self.assertEqual(state['counts'], {'hiddify': 1, 'mihomo': 1, 'xray': 1, 'v2board': 1, 'v2board_uri': 1})
        self.assertNotIn('connectivity', state)
        for name, link in state['links'].items():
            response = self.client.get(link)
            self.assertEqual(response.status_code, 200)
            self.assertNotIn('bad.example', response.text)
            if name == 'combined.json':
                self.assertEqual(len(response.json()['servers']), 1)
                self.assertEqual(len(response.json()['excluded_servers']), 1)

    def test_routing_helpers_do_not_inflate_provider_inventory_or_log_counts(self):
        document = [{'outbounds': [entry.original for entry in entries()]},
                    {'outbounds': [{'tag': f'helper-{index}', 'protocol': 'loopback',
                                    'settings': {'inboundTag': f'route-{index}'}} for index in range(6)]}]
        self.manager.fetcher = lambda *args: (json.dumps(document).encode(), {'fetched_at': common.now()})
        state = self.refresh()
        self.assertEqual((state['total'], state['published_total']), (2, 1))
        self.assertEqual(state['providers'][0]['count'], 2)
        self.assertEqual(len(state['inventory']), 2)
        self.assertTrue(all(node['protocol'] != 'loopback' for node in state['inventory']))
        summary = next(row['context'] for row in self.manager.logs.recent()['entries'] if row['event'] == 'run_completed')
        self.assertEqual((summary['servers'], summary['published']), (2, 1))
        archive = self.client.get(state['links']['combined.json']).json()
        self.assertEqual((len(archive['servers']), len(archive['excluded_servers'])), (1, 1))

    def test_manual_recheck_recovers_failed_node_without_fetch_or_new_hwid(self):
        state = self.refresh(); before = list(self.calls)
        self.mode = 'all'
        self.assertEqual(self.client.post('/api/check-servers').status_code, 202)
        self.manager.worker.join(5)
        after = self.client.get('/api/state').json()
        self.assertEqual(after['counts']['mihomo'], 2)
        self.assertEqual(after['links'], state['links'])
        self.assertEqual(self.calls, before)

    def test_expired_and_legacy_snapshots_cannot_serve_old_nodes(self):
        state = self.refresh()
        self.manager.snapshot['valid_until'] = time.time() - 1
        for link in state['links'].values():
            self.assertEqual(self.client.get(link).status_code, 503)
        expired = self.client.get('/api/state').json()
        self.assertEqual(expired['published_total'], 0)
        self.assertFalse(any(node['clients'] for node in expired['inventory']))
        legacy = self.manager.snapshot.copy(); legacy.pop('connectivity_policy')
        common.atomic_write(self.manager.snapshot_path, common.json_text(legacy))
        restarted = create_app(self.root, start_scheduler=False).state.manager
        self.assertFalse(restarted.publication_current(restarted.snapshot))
        self.assertFalse(restarted.snapshot['exports'])

    def test_schedule_persists_and_busy_checks_reject_overlapping_work(self):
        self.refresh()
        self.assertEqual(self.client.put('/api/settings', json={'check_seconds': 900}).status_code, 200)
        self.assertEqual(create_app(self.root, start_scheduler=False).state.manager.store.config()['check_seconds'], 900)
        self.manager.busy = True
        for path in ('/api/check-servers', '/api/refresh'):
            self.assertEqual(self.client.post(path).status_code, 409)
        self.assertEqual(self.client.put('/api/settings', json={'check_seconds': 300}).status_code, 409)
        self.manager.busy = False
        self.client.post('/api/logout')
        self.assertEqual(self.client.post('/api/check-servers').status_code, 401)

    def test_scheduled_recheck_uses_cache_and_not_provider_fetch(self):
        self.refresh(); before = list(self.calls)
        self.manager.next_refresh = time.time() + 3600
        self.manager.next_check = 0
        worker = threading.Thread(target=self.manager.scheduler)
        worker.start()
        deadline = time.monotonic() + 6
        while self.manager.next_check == 0 and time.monotonic() < deadline:
            time.sleep(.05)
        self.manager.stop.set(); worker.join(3)
        self.assertGreater(self.manager.next_check, time.time())
        self.assertEqual(self.calls, before)

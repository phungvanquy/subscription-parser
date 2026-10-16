import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from subscription_parser import common, connectivity, protocols, publication, v2board_native

NOW = 1767225600
STAMP = '2026-01-01T00:00:00+00:00'
UUID = '11111111-2222-4333-8444-555555555555'


def sample_entries():
    nodes = []
    for index in range(2):
        nodes.append({'tag': f'Hysteria {index + 1}', 'protocol': 'hysteria',
            'settings': {'version': 2, 'address': f'hy{index}.example.com', 'port': 443},
            'streamSettings': {'network': 'hysteria', 'security': 'tls',
                'hysteriaSettings': {'version': 2, 'auth': 'synthetic:password'},
                'tlsSettings': {'serverName': 'tls.example.com'},
                'finalmask': {'quicParams': {'congestion': 'bbr', 'debug': False}}}})
    for index in range(6):
        node = {'tag': f'VLESS {index + 1}', 'protocol': 'vless',
            'settings': {'vnext': [{'address': f'vless{index}.example.com', 'port': 443,
                'users': [{'id': UUID, 'encryption': 'none'}]}]},
            'streamSettings': {'network': 'tcp', 'security': 'tls', 'tlsSettings': {'serverName': 'tls.example.com'}}}
        if index < 2:
            node['streamSettings'].update(network='ws', wsSettings={'path': '/private-path',
                'headers': {'X-Whitelist-Header': 'synthetic-header-secret'}})
            if index == 1:
                node['streamSettings']['wsSettings']['heartbeatPeriod'] = 15
        elif index < 5:
            node['mux'] = {'enabled': True, 'concurrency': 8, 'xudpConcurrency': 16, 'xudpProxyUDP443': 'reject'}
        nodes.append(node)
    return protocols.parse(json.dumps({'outbounds': nodes}).encode(), 'Synthetic provider')


def checks(entries):
    return {entry.identity: {core: {'status': 'passed', 'fingerprint': connectivity.fingerprint(entry, core),
            'checked_ts': NOW, 'checked_at': STAMP, 'latency_ms': 12}
            for core in connectivity.CORES if core in entry.outputs} for entry in entries}


def sample_feed():
    entries = sample_entries()
    snapshot, _ = publication.build(entries, checks(entries), 1200, NOW, STAMP, {}, STAMP)
    return json.loads(snapshot['exports']['v2board.json'])


class NativeV2BoardTests(unittest.TestCase):
    def test_seven_advanced_nodes_preserve_native_settings_and_uri_counts_stay_separate(self):
        nodes = sample_entries()
        snapshot, _ = publication.build(nodes, checks(nodes), 1200, NOW, STAMP, {}, STAMP)
        document = json.loads(snapshot['exports']['v2board.json'])
        self.assertEqual(snapshot['counts']['v2board'], 8)
        self.assertEqual(snapshot['counts']['v2board_uri'], 1)
        self.assertEqual(len(document['nodes']), 8)
        self.assertEqual(document['valid_until'], NOW + 1200)
        self.assertEqual(document['nodes'][0]['configs']['xray'][0]['streamSettings']['finalmask']['quicParams']['congestion'], 'bbr')
        self.assertEqual(document['nodes'][3]['configs']['xray'][0]['streamSettings']['wsSettings']['heartbeatPeriod'], 15)
        self.assertEqual(document['nodes'][3]['configs']['hiddify'][0]['transport']['headers']['X-Whitelist-Header'], 'synthetic-header-secret')
        self.assertTrue(document['nodes'][4]['configs']['xray'][0]['mux']['enabled'])
        self.assertTrue(all('uri' not in row for row in document['nodes'][:7]))
        self.assertIn('uri', document['nodes'][7])
        self.assertTrue(all('v2board' in row['clients'] for row in snapshot['inventory']))

    def test_failed_or_stale_variants_are_not_reintroduced(self):
        entries = sample_entries()
        results = checks(entries)
        results[entries[0].identity]['mihomo']['status'] = 'failed'
        results[entries[1].identity]['xray']['checked_ts'] = NOW - 1300
        for core in connectivity.CORES:
            results[entries[2].identity][core]['status'] = 'failed'
        snapshot, _ = publication.build(entries, results, 1200, NOW, STAMP, {}, STAMP)
        nodes = {row['id']: row for row in json.loads(snapshot['exports']['v2board.json'])['nodes']}
        self.assertNotIn('mihomo', nodes[entries[0].identity]['configs'])
        self.assertNotIn('xray', nodes[entries[1].identity]['configs'])
        self.assertNotIn(entries[2].identity, nodes)

    def test_dependency_bundles_are_preserved_and_scoped(self):
        raw = {'outbounds': [{'type': 'vless', 'tag': 'one', 'server': 'node.example.com', 'server_port': 443,
                            'uuid': UUID, 'detour': 'two'},
                           {'type': 'direct', 'tag': 'two'}]}
        entry = protocols.parse(json.dumps(raw).encode(), 'Chain')[0]
        bundle = v2board_native.checked_bundle(entry, 'hiddify')
        self.assertEqual(bundle[0]['detour'], bundle[1]['tag'])
        self.assertEqual(bundle[0]['tag'], 'node')

    def test_global_file_settings_and_header_injection_are_rejected(self):
        for key, value in [('certificate_path', '/private/key'), ('routing', {}),
                           ('sockopt', {'mark': 3}), ('sockopt', {'tcpNoDelay': 'true'})]:
            with self.subTest(key=key):
                entry = copy.deepcopy(sample_entries()[2])
                entry.outputs['hiddify']['tls'][key] = value
                with self.assertRaises(common.SubscriptionError):
                    v2board_native.checked_bundle(entry, 'hiddify')
        entry = sample_entries()[2]
        entry.outputs['hiddify']['transport']['headers']['X-Required'] = 'one\r\ntwo'
        with self.assertRaises(common.SubscriptionError):
            v2board_native.checked_bundle(entry, 'hiddify')

    def test_xray_socket_dependency_preserves_fragment_chain(self):
        root = copy.deepcopy(sample_entries()[-1].outputs['xray'])
        root['streamSettings']['sockopt'] = {'dialerProxy': 'fragment'}
        helper = {'tag': 'fragment', 'protocol': 'freedom',
                  'settings': {'fragment': {'packets': 'tlshello', 'length': '100-200', 'interval': '10-20'}},
                  'streamSettings': {'sockopt': {'tcpNoDelay': True}}}
        entry = protocols.parse(json.dumps({'outbounds': [root, helper]}).encode(), 'Chain')[0]
        bundle = v2board_native.checked_bundle(entry, 'xray')
        self.assertEqual(bundle[0]['streamSettings']['sockopt']['dialerProxy'], bundle[1]['tag'])
        self.assertEqual(bundle[1]['settings'], helper['settings'])
        self.assertEqual(bundle[1]['streamSettings'], helper['streamSettings'])
        entry.outputs['xray']['streamSettings']['sockopt']['interface'] = 'eth0'
        with self.assertRaises(common.SubscriptionError):
            v2board_native.checked_bundle(entry, 'xray')

    def test_envelope_and_per_node_limits_keep_json_valid(self):
        entries = sample_entries()
        with patch.object(v2board_native, 'MAX_NODE_BYTES', 10):
            result = v2board_native.export(entries, checks(entries), 1200, NOW, STAMP, NOW + 1200)
            self.assertEqual(json.loads(result['body'])['nodes'], [])
            self.assertEqual(len(result['diagnostics']), 8)
        with patch.object(v2board_native.v2board, 'MAX_BYTES', 4000):
            result = v2board_native.export(entries, checks(entries), 1200, NOW, STAMP, NOW + 1200)
            self.assertLessEqual(len(result['body'].encode()), 4000)
            self.assertEqual(result['count'], len(json.loads(result['body'])['nodes']))

    def test_shared_contract_fixture_matches_exporter(self):
        fixture = Path(__file__).parent / 'fixtures' / 'v2board-native-v1.json'
        self.assertEqual(json.loads(fixture.read_text()), sample_feed())


if __name__ == '__main__':
    unittest.main()

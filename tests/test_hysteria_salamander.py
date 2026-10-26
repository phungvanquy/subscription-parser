"""Xray Salamander conversion, upgrade stability, and actual Hysteria2 traffic."""
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from scripts.connectivity_fixture import fixture
from subscription_parser import common, connectivity, protocols, server_policy, service, v2board
from subscription_parser.validation import checked_combine

CORES = Path(os.environ.get('PARSER_TOOLS_DIR', '/opt/client-cores'))
AUTH = 'user:p@ss:+ &/#?é'
OBFS = 'salamander:+ separate &/#?密码'


def outbound():
    return {'tag': 'Synthetic', 'protocol': 'hysteria',
            'settings': {'address': '2001:db8::1', 'port': 443, 'version': 2},
            'streamSettings': {'network': 'hysteria', 'security': 'tls',
                'hysteriaSettings': {'version': 2, 'auth': AUTH},
                'tlsSettings': {'serverName': 'cover.example', 'alpn': ['h3'],
                                'allowInsecure': False, 'fingerprint': 'firefox'},
                'finalmask': {'udp': [{'type': 'salamander', 'settings': {'password': OBFS}}]}}}


def parse(raw):
    return protocols.parse(json.dumps({'outbounds': [raw]}).encode(), 'Synthetic')[0]


def legacy_converter(raw, notes):
    raise protocols.Error('unsupported FinalMask settings: udp')


class SalamanderTests(unittest.TestCase):
    def test_conversion_preserves_auth_obfuscation_and_tls_without_mutating_source(self):
        raw = outbound()
        original = copy.deepcopy(raw)
        entry = parse(raw)
        self.assertEqual(set(entry.outputs), set(protocols.TARGETS), entry.issues)
        self.assertEqual(entry.outputs['xray'], raw)
        mihomo, hiddify = entry.outputs['mihomo'], entry.outputs['hiddify']
        self.assertEqual(mihomo['password'], AUTH)
        self.assertEqual(hiddify['password'], AUTH)
        self.assertEqual((mihomo['obfs'], mihomo['obfs-password']), ('salamander', OBFS))
        self.assertEqual(hiddify['obfs'], {'type': 'salamander', 'password': OBFS})
        self.assertEqual((mihomo['server'], mihomo['port']), ('2001:db8::1', 443))
        self.assertEqual(mihomo['sni'], 'cover.example')
        self.assertEqual(hiddify['tls']['server_name'], 'cover.example')
        self.assertEqual(mihomo['alpn'], ['h3'])
        self.assertEqual(hiddify['tls']['alpn'], ['h3'])
        self.assertFalse(mihomo.get('skip-cert-verify', False))
        self.assertFalse(hiddify['tls'].get('insecure', False))
        self.assertNotIn('fingerprint', mihomo)  # A TLS client fingerprint is not a certificate pin.
        self.assertNotIn('utls', hiddify['tls'])
        hiddify['obfs']['password'] = 'edited'
        hiddify['tls']['alpn'].append('edited')
        self.assertEqual(entry.outputs['xray'], original)
        self.assertEqual(raw, original)

    def test_explicit_tls_verification_boolean_is_preserved(self):
        for insecure in (False, True):
            raw = outbound()
            raw['streamSettings']['tlsSettings']['allowInsecure'] = insecure
            entry = parse(raw)
            self.assertEqual(entry.outputs['mihomo'].get('skip-cert-verify', False), insecure)
            self.assertEqual(entry.outputs['hiddify']['tls'].get('insecure', False), insecure)
        for invalid in ('false', 'true', 0, 1, None):
            raw['streamSettings']['tlsSettings']['allowInsecure'] = invalid
            with self.subTest(invalid=invalid):
                self.assertEqual(set(parse(raw).outputs), {'xray'})

    def test_nonportable_or_malformed_masks_keep_only_the_original_xray(self):
        layer = outbound()['streamSettings']['finalmask']['udp'][0]
        masks = [
            'invalid', {'udp': 'invalid'}, {'udp': {}}, {'udp': False}, {'udp': [None]},
            {'udp': [layer, layer]}, {'udp': [{'type': 'unknown', 'settings': {'password': OBFS}}]},
            {'udp': [{'type': 'salamander'}]},
            {'udp': [{**layer, 'extra': False}]},
            {'udp': [{**layer, 'settings': None}]}, {'udp': [{**layer, 'settings': {}}]},
            {'udp': [{**layer, 'settings': {'password': ''}}]},
            {'udp': [{**layer, 'settings': {'password': 123}}]},
            {'udp': [{**layer, 'settings': {'password': OBFS, 'packetSize': '100-200'}}]},
            {'udp': [{**layer, 'settings': {'password': OBFS, 'packetSize': ''}}]},
            {'udp': [layer], 'tcp': [layer]},
            {'udp': [layer], 'quicParams': 'invalid'},
            {'udp': [layer], 'quicParams': {'initialStreamReceiveWindow': 10000}},
        ]
        for index, mask in enumerate(masks):
            with self.subTest(index=index):
                raw = outbound()
                raw['streamSettings']['finalmask'] = mask
                entry = parse(raw)
                self.assertEqual(set(entry.outputs), {'xray'}, entry.issues)
                self.assertEqual(entry.outputs['xray'], raw)
                self.assertNotIn(AUTH, json.dumps(entry.issues, ensure_ascii=False))
                self.assertNotIn(OBFS, json.dumps(entry.issues, ensure_ascii=False))

    def test_mask_requires_hysteria2_transport_and_tls(self):
        for key, value in [('network', 'tcp'), ('security', 'none'),
                           ('hysteriaSettings', {'version': 1, 'auth': AUTH})]:
            raw = outbound()
            raw['streamSettings'][key] = value
            with self.subTest(key=key):
                self.assertEqual(set(parse(raw).outputs), {'xray'})

    def test_supported_quic_tuning_keeps_obfuscation_and_explains_defaults(self):
        raw = outbound()
        raw['streamSettings']['finalmask']['quicParams'] = {'debug': False, 'congestion': 'bbr'}
        entry = parse(raw)
        self.assertEqual(set(entry.outputs), set(protocols.TARGETS))
        self.assertEqual(entry.outputs['mihomo']['obfs-password'], OBFS)
        self.assertTrue(any('congestion' in note for note in entry.notes))

    def test_empty_udp_masks_keep_existing_unmasked_identity(self):
        raw = outbound()
        raw['streamSettings'].pop('finalmask')
        original = parse(raw)
        for empty in ([], None):
            raw['streamSettings']['finalmask'] = {'udp': empty}
            entry = parse(raw)
            self.assertEqual(entry.identity, original.identity)
            self.assertEqual(entry.outputs['mihomo'], original.outputs['mihomo'])
            self.assertNotIn('obfs', entry.outputs['hiddify'])

    def test_legacy_identity_and_source_bindings_survive_conversion_and_rename(self):
        raw = outbound()
        with patch.object(protocols, 'xray_to_native', legacy_converter):
            old = parse(raw)
        new = parse(raw)
        self.assertEqual(new.identity, old.identity)
        self.assertEqual(server_policy.source_key(new), server_policy.source_key(old))
        without_name = {k: v for k, v in raw.items() if k != 'tag'}
        self.assertEqual(new.identity, hashlib.sha256(json.dumps(
            without_name, sort_keys=True, ensure_ascii=False).encode()).hexdigest())
        raw['tag'] = 'Renamed'
        self.assertEqual(server_policy.source_key(parse(raw)), server_policy.source_key(old))
        raw['streamSettings']['finalmask']['udp'][0]['settings']['password'] = 'changed'
        self.assertNotEqual(parse(raw).identity, old.identity)

    def test_new_outputs_require_their_own_checks_but_reuse_existing_xray_result(self):
        raw = outbound()
        with patch.object(protocols, 'xray_to_native', legacy_converter):
            old = parse(raw)
        new = parse(raw)
        with tempfile.TemporaryDirectory() as folder:
            checks = connectivity.check_entries([old], Path(folder), tester=lambda *a:
                connectivity.result('passed', latency_ms=1))
            filtered = connectivity.filtered_entries([new], checks, 1200)
            self.assertEqual(set(filtered[0].outputs), {'xray'})
            calls = []
            def tester(entry, core, *args):
                calls.append(core)
                return connectivity.result('passed', latency_ms=1)
            fresh = connectivity.check_entries([new], Path(folder), previous=checks,
                reuse_seconds=1200, tester=tester)
            self.assertEqual(set(calls), {'mihomo', 'hiddify'})
            self.assertEqual(fresh[new.identity]['xray'], checks[old.identity]['xray'])
            self.assertEqual(set(connectivity.filtered_entries([new], fresh, 1200)[0].outputs),
                             set(protocols.TARGETS))
        # URI export still rejects source settings it cannot represent.
        self.assertEqual(v2board.export([new])['count'], 0)

    def test_restart_preserves_sample_saved_selection_and_existing_xray_checks(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {
                'PARSER_ADMIN_TOKEN': 'salamander-upgrade-test'}), patch.object(service,
                'checked_combine', side_effect=lambda entries, root:
                    protocols.combine(entries) | {'validation': {}}):
            raw = [outbound() for _ in range(5)]
            for index, node in enumerate(raw):
                node['settings']['address'] = f'node-{index}.example'
                node['tag'] = f'Node {index}'
            calls, probes = [], []
            def fetch(*args):
                calls.append(args)
                return json.dumps({'outbounds': raw}).encode(), {'fetched_at': common.now()}
            def tester(entry, core, *args):
                probes.append((entry.identity, core))
                return connectivity.result('passed', latency_ms=1)
            def check(entries, root, **kwargs):
                return connectivity.check_entries(entries, root, tester=tester, **kwargs)
            old_policy = protocols.adapter_policy() | {'version': 1}
            root = Path(folder)
            with patch.object(protocols, 'xray_to_native', legacy_converter), patch.object(
                    protocols, 'adapter_policy', return_value=old_policy):
                manager = service.Manager(root, fetch, check)
                manager.change_provider({'name': 'Test', 'url': 'https://provider.example/sub',
                                         'hwid_id': None, 'server_limit': 2})
                manager.refresh()
                identifier = manager.change_subscription({'name': 'Phone', 'all_providers': True,
                                                          'provider_ids': []})
                available = manager.subscription_servers(identifier)['available'][0]
                manager.change_subscription_servers(identifier, {'revision': 0, 'all_servers': False,
                    'records': [{k: available[k] for k in ('provider_id', 'source_identity')}]})
                manager.refresh(download=False, check=True, reuse_recent=True)
                self.assertNotIn('last_error', manager.snapshot)
                before = copy.deepcopy(manager.snapshot)
                saved = manager.store.server_policy(identifier)
                subscriptions = manager.store.subscriptions()
            calls.clear(); probes.clear()
            upgraded = service.Manager(root, fetch, check)
            self.assertEqual(set(upgraded.jobs.items), {'check'})
            upgraded.refresh(download=False, check=True, reuse_recent=True)
            self.assertNotIn('last_error', upgraded.snapshot)
            self.assertEqual(calls, [])
            self.assertEqual(len(probes), 4)
            self.assertEqual({core for _, core in probes}, {'mihomo', 'hiddify'})
            self.assertEqual(upgraded.snapshot['active_selections'], before['active_selections'])
            self.assertEqual({row['id'] for row in upgraded.snapshot['inventory']},
                             {row['id'] for row in before['inventory']})
            self.assertEqual(upgraded.store.server_policy(identifier), saved)
            self.assertEqual(upgraded.store.subscriptions(), subscriptions)
            self.assertEqual(upgraded.snapshot['adapter_policy'], protocols.adapter_policy())
            self.assertEqual(upgraded.subscription_snapshot(upgraded.store.subscription(identifier))['counts'],
                             {'mihomo': 1, 'hiddify': 1, 'xray': 1, 'v2board_uri': 0, 'v2board': 1})
            self.assertFalse(service.Manager(root, fetch, check).jobs.items)


@unittest.skipUnless(all((CORES / name).is_file() for name in ('mihomo', 'xray', 'hiddify-core')),
                     'Docker image with client cores required')
class SalamanderCoreTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / '.parser-state').mkdir()
        manager = fixture(self.root / 'fixture', CORES / 'xray', protocol='hysteria2', salamander=OBFS)
        self.local = manager.__enter__()
        self.addCleanup(manager.__exit__, None, None, None)
        env = patch.dict(os.environ, {'PARSER_TOOLS_DIR': str(CORES),
                                     'SSL_CERT_FILE': str(self.local['certificate'])})
        env.start(); self.addCleanup(env.stop)
        native = copy.deepcopy(self.local['node'])
        obfs = native.pop('obfs')
        self.raw = protocols.native_to_xray(native)
        self.raw['streamSettings']['finalmask'] = {'udp': [
            {'type': obfs['type'], 'settings': {'password': obfs['password']}}]}

    def probe(self, entry, core):
        return connectivity.probe(entry, core, self.root, threading.Event(), time.monotonic() + 15)

    def test_all_cores_validate_and_tunnel_verified_https(self):
        entry = parse(self.raw)
        validated = checked_combine([entry], self.root)
        self.assertEqual(validated['counts'], dict.fromkeys(protocols.TARGETS, 1))
        for core in connectivity.CORES:
            with self.subTest(core=core):
                self.assertEqual(self.probe(entry, core)['status'], 'passed')
        self.assertEqual(self.local['server'].requests, len(connectivity.CORES))
        self.assertFalse(list((self.root / '.parser-state').iterdir()))

    def test_wrong_obfuscation_password_cannot_connect(self):
        self.raw['streamSettings']['finalmask']['udp'][0]['settings']['password'] = 'wrong-obfs'
        self.assert_all_fail()

    def test_missing_obfuscation_cannot_connect(self):
        self.raw['streamSettings'].pop('finalmask')
        self.assert_all_fail()

    def test_wrong_authentication_cannot_connect(self):
        self.raw['streamSettings']['hysteriaSettings']['auth'] = 'wrong-auth'
        self.assert_all_fail()

    def test_wrong_tls_name_cannot_connect(self):
        self.raw['streamSettings']['tlsSettings']['serverName'] = 'untrusted.example'
        self.assert_all_fail()

    def assert_all_fail(self):
        entry = parse(self.raw)
        for core in connectivity.CORES:
            with self.subTest(core=core), patch.object(connectivity, 'REQUEST_TIMEOUT', 1):
                self.assertEqual(self.probe(entry, core)['status'], 'failed')
        self.assertEqual(self.local['server'].requests, 0)
        self.assertFalse(list((self.root / '.parser-state').iterdir()))


if __name__ == '__main__':
    unittest.main()

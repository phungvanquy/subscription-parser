"""XHTTP interoperability and explicitly enabled Mihomo REALITY client tuning."""
import base64
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
from urllib.parse import urlencode

import yaml

from scripts.connectivity_fixture import fixture
from subscription_parser import common, connectivity, protocols, publication, server_policy, service
from subscription_parser.config import env_bool
from subscription_parser.validation import checked_combine

OPTION = 'PARSER_MIHOMO_REALITY_COMPATIBILITY'
UUID = '01234567-89ab-4cde-8fab-0123456789ab'
KEY = base64.urlsafe_b64encode(bytes(range(32))).decode().rstrip('=')
CORES = Path(os.environ.get('PARSER_TOOLS_DIR', '/opt/client-cores'))


def native():
    return {'type': 'vless', 'tag': 'Synthetic', 'server': 'proxy.example', 'server_port': 443,
            'uuid': UUID, 'tls': {'enabled': True, 'server_name': 'cover.example',
                'utls': {'enabled': True, 'fingerprint': 'firefox'},
                'reality': {'enabled': True, 'public_key': KEY, 'short_id': '01ab'}}}


def parse(node):
    return protocols.parse(json.dumps({'outbounds': [node]}).encode(), 'Synthetic')[0]


class ConversionTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {OPTION: 'false'})
        env.start()
        self.addCleanup(env.stop)

    def test_xhttp_preserves_all_common_options_across_native_formats(self):
        for mode in ('auto', 'stream-one', 'stream-up', 'packet-up'):
            node = native()
            options = {'path': '/tunnel?key=a%26b', 'host': 'cover.example', 'mode': mode,
                       'headers': {'X-Required': 'a&b'}}
            node['transport'] = {'type': 'xhttp', **options}
            original = copy.deepcopy(node)
            entry = parse(node)
            with self.subTest(mode=mode):
                self.assertEqual(set(entry.outputs), set(protocols.TARGETS))
                self.assertEqual(entry.outputs['mihomo']['network'], 'xhttp')
                self.assertEqual(entry.outputs['mihomo']['xhttp-opts'], options)
                self.assertEqual(entry.outputs['xray']['streamSettings']['xhttpSettings'], options)
                self.assertEqual(node, original)
                for target in ('mihomo', 'xray'):
                    converted = protocols.parse(json.dumps(entry.outputs[target]).encode(), target)[0]
                    self.assertEqual(converted.outputs['hiddify']['transport'], node['transport'])
                    self.assertEqual(converted.outputs['mihomo']['xhttp-opts'], options)

    def test_xhttp_share_link_preserves_tls_and_defaults(self):
        query = urlencode({'type': 'xhttp', 'security': 'reality', 'pbk': KEY,
                           'sid': '01ab', 'sni': 'cover.example', 'fp': 'firefox'})
        entry = protocols.parse(f'vless://{UUID}@proxy.example:443?{query}'.encode(), 'URI')[0]
        proxy = entry.outputs['mihomo']
        self.assertEqual(proxy['xhttp-opts'], {'path': '/', 'mode': 'auto'})
        self.assertEqual(proxy['servername'], 'cover.example')
        self.assertEqual(proxy['reality-opts'], {'public-key': KEY, 'short-id': '01ab'})
        self.assertEqual(entry.outputs['xray']['streamSettings']['network'], 'xhttp')

    def test_existing_native_mihomo_xhttp_keeps_its_legacy_identity(self):
        proxy = protocols.native_to_mihomo(native())
        proxy.update(name='Existing XHTTP', network='xhttp', **{'xhttp-opts': {'path': '/old'}})
        old_identity = {k: v for k, v in proxy.items() if k != 'name'}
        expected = hashlib.sha256(json.dumps(old_identity, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        with patch.dict(os.environ, {OPTION: 'true'}):
            entry = protocols.parse(yaml.safe_dump({'proxies': [proxy]}).encode(), 'Existing')[0]
        self.assertEqual(entry.identity, expected)
        self.assertEqual(entry.outputs['mihomo'], proxy)
        self.assertEqual(set(entry.outputs), set(protocols.TARGETS))

    def test_advanced_xhttp_remains_only_in_its_original_format(self):
        node = native()
        node['transport'] = {'type': 'xhttp', 'path': '/tunnel', 'mode': 'auto'}
        portable = parse(node)
        for target, field, extra in [
                ('hiddify', 'transport', {'download_settings': {'server': 'download.example'}}),
                ('mihomo', 'xhttp-opts', {'download-settings': {'server': 'download.example'}}),
                ('xray', 'xhttpSettings', {'extra': {'noSSEHeader': True}})]:
            raw = copy.deepcopy(portable.outputs[target])
            (raw['streamSettings'] if target == 'xray' else raw)[field].update(extra)
            entry = protocols.parse(json.dumps(raw).encode(), 'Advanced')[0]
            with self.subTest(target=target):
                self.assertEqual(entry.outputs, {target: raw})
                self.assertTrue(entry.issues)

    def test_xhttp_rejects_wrong_protocol_modes_and_lossy_options(self):
        invalid = [({'mode': 'unknown'}, 'unsupported XHTTP mode'),
                   ({'mode': []}, 'unsupported XHTTP mode'),
                   ({'path': 123}, 'path and host must be strings'),
                   ({'host': []}, 'path and host must be strings'),
                   ({'headers': []}, 'headers must contain string'),
                   ({'headers': {'X-Test': ['one', 'two']}}, 'headers must contain string'),
                   ({'download-settings': {}}, 'advanced XHTTP settings'),
                   ({'unsupported-setting': False}, 'advanced XHTTP settings')]
        for options, reason in invalid:
            node = native()
            node['transport'] = {'type': 'xhttp', **options}
            with self.subTest(options=options):
                entry = parse(node)
                self.assertEqual(set(entry.outputs), {'hiddify'})
                self.assertIn(reason, entry.issues['mihomo'])
        node = native()
        node.update(type='vmess', transport={'type': 'xhttp'})
        self.assertIn('requires VLESS', parse(node).issues['mihomo'])
        with self.assertRaisesRegex(protocols.Error, 'must be an object'):
            protocols.xhttp_options([], 'vless')

    def test_reality_option_keeps_sources_ids_and_other_cores_stable(self):
        raw = native()
        original = copy.deepcopy(raw)
        for payload in (raw, protocols.native_to_xray(raw)):
            before = parse(payload)
            with patch.dict(os.environ, {OPTION: 'true'}):
                after = parse(payload)
            with self.subTest(kind=before.kind):
                self.assertEqual(after.identity, before.identity)
                self.assertEqual(server_policy.source_key(after), server_policy.source_key(before))
                self.assertEqual(after.original, before.original)
                for target in ('xray', 'hiddify'):
                    self.assertEqual(after.outputs[target], before.outputs[target])
                    self.assertEqual(connectivity.fingerprint(after, target), connectivity.fingerprint(before, target))
                expected = copy.deepcopy(before.outputs['mihomo'])
                expected['client-fingerprint'] = 'chrome'
                expected['reality-opts']['support-x25519mlkem768'] = True
                self.assertEqual(after.outputs['mihomo'], expected)
                self.assertNotEqual(connectivity.fingerprint(after, 'mihomo'), connectivity.fingerprint(before, 'mihomo'))
                self.assertTrue(any('X25519-MLKEM768' in note for note in after.notes))
        self.assertEqual(raw, original)

    def test_option_preserves_native_mihomo_tls_and_other_protocols(self):
        proxy = parse(native()).outputs['mihomo']
        proxy['reality-opts']['support-x25519mlkem768'] = True
        with patch.dict(os.environ, {OPTION: 'true'}):
            entry = protocols.parse(yaml.safe_dump({'proxies': [proxy]}).encode(), 'Mihomo')[0]
            self.assertEqual(entry.outputs['mihomo'], proxy)
            self.assertFalse(entry.notes)
            for kind in ('vless', 'vmess'):
                node = native()
                node['type'] = kind
                if kind == 'vless':
                    node['tls'].pop('reality')
                proxy = parse(node).outputs['mihomo']
                self.assertEqual(proxy['client-fingerprint'], 'firefox')
                self.assertNotIn('support-x25519mlkem768', proxy.get('reality-opts', {}))

    def test_changed_reality_output_requires_its_own_passing_check(self):
        before = parse(native())
        with tempfile.TemporaryDirectory() as folder:
            old = connectivity.check_entries([before], Path(folder), tester=lambda *a: {'status': 'passed'})
        with patch.dict(os.environ, {OPTION: 'true'}):
            after = parse(native())
        self.assertEqual(connectivity.current_result(after, 'mihomo', old, 1200)['status'], 'pending')
        filtered = connectivity.filtered_entries([after], old, 1200)[0]
        self.assertNotIn('mihomo', filtered.outputs)
        self.assertEqual(set(filtered.outputs), {'hiddify', 'xray'})
        with tempfile.TemporaryDirectory() as folder:
            fresh = connectivity.check_entries([after], Path(folder), tester=lambda *a: {'status': 'passed'})
        published, _ = publication.build([after], fresh, 1200, time.time(), common.now(), {}, common.now())
        proxy = yaml.safe_load(published['exports']['mihomo.yaml'])['proxies'][0]
        self.assertEqual(proxy['client-fingerprint'], 'chrome')
        self.assertTrue(proxy['reality-opts']['support-x25519mlkem768'])
        native_feed = json.loads(published['exports']['v2board.json'])
        bundled = native_feed['nodes'][0]['configs']['mihomo'][0]
        self.assertEqual({k: v for k, v in bundled.items() if k != 'name'},
                         {k: v for k, v in proxy.items() if k != 'name'})

    def test_environment_option_is_strict_and_does_not_disclose_values(self):
        for value, expected in [('true', True), ('1', True), ('TRUE', True), ('false', False), ('0', False)]:
            with self.subTest(value=value), patch.dict(os.environ, {OPTION: value}):
                self.assertIs(protocols.adapter_policy()['mihomo_reality_compatibility'], expected)
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(env_bool(OPTION))
            self.assertTrue(env_bool(OPTION, True))
        with patch.dict(os.environ, {OPTION: 'private-invalid-value'}):
            with self.assertRaises(common.SubscriptionError) as caught:
                protocols.adapter_policy()
            self.assertEqual(str(caught.exception), OPTION + ' must be true or false')


class UpgradeTests(unittest.TestCase):
    def test_restart_rechecks_changed_configs_without_fetching_or_redrawing(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {
                OPTION: 'false', 'PARSER_ADMIN_TOKEN': 'upgrade-test'}), patch.object(
                service, 'checked_combine', side_effect=lambda entries, root:
                    protocols.combine(entries) | {'validation': {}}):
            calls, probes = [], []
            def fetch(*args):
                calls.append(args)
                return json.dumps({'outbounds': [native()]}).encode(), {'fetched_at': common.now()}
            def tester(entry, core, *args):
                probes.append((entry.identity, core))
                return {'status': 'passed', 'latency_ms': 1}
            def check(entries, root, **kwargs):
                return connectivity.check_entries(entries, root, tester=tester, **kwargs)
            root = Path(folder)
            manager = service.Manager(root, fetch, check)
            manager.change_provider({'name': 'Test', 'url': 'https://provider.example/sub', 'hwid_id': None, 'server_limit': 1})
            manager.refresh()
            self.assertNotIn('last_error', manager.snapshot)
            active = manager.snapshot['active_selections']
            old_ids = [row['id'] for row in manager.snapshot['inventory']]
            calls.clear(); probes.clear()
            with patch.dict(os.environ, {OPTION: 'true'}):
                upgraded = service.Manager(root, fetch, check)
                self.assertEqual(set(upgraded.jobs.items), {'check'})
                upgraded.refresh(download=False, check=True, reuse_recent=True)
                self.assertNotIn('last_error', upgraded.snapshot)
                self.assertEqual(calls, [])
                self.assertEqual([core for _, core in probes], ['mihomo'])
                self.assertEqual(upgraded.snapshot['active_selections'], active)
                self.assertEqual([row['id'] for row in upgraded.snapshot['inventory']], old_ids)
                self.assertEqual(upgraded.snapshot['adapter_policy'], protocols.adapter_policy())
                restarted = service.Manager(root, fetch, check)
                self.assertFalse(restarted.jobs.items)
                saved = json.loads(restarted.snapshot_path.read_text())
                saved.pop('adapter_policy')
                restarted.snapshot_path.write_text(json.dumps(saved))
                legacy = service.Manager(root, fetch, check)
                self.assertEqual(set(legacy.jobs.items), {'check'})


@unittest.skipUnless(all((CORES / name).is_file() for name in ('mihomo', 'xray', 'hiddify-core')),
                     'Docker image with client cores required')
class RealCoreCompatibilityTests(unittest.TestCase):
    def test_xhttp_tunnels_verified_https_through_all_cores(self):
        for mode in ('packet-up', 'stream-up', 'stream-one'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                (root / '.parser-state').mkdir()
                with fixture(root / 'fixture', CORES / 'xray', xhttp={
                        'path': '/test-xhttp', 'mode': mode, 'headers': {'X-Required': 'synthetic'}}) as local:
                    with patch.dict(os.environ, {'PARSER_TOOLS_DIR': str(CORES),
                            'SSL_CERT_FILE': str(local['certificate']), OPTION: 'false'}):
                        entry = parse(local['node'])
                        validated = checked_combine([entry], root)
                        self.assertEqual(validated['counts'], dict.fromkeys(protocols.TARGETS, 1))
                        for core in connectivity.CORES:
                            with self.subTest(core=core):
                                value = connectivity.probe(entry, core, root, threading.Event(), time.monotonic() + 20)
                                self.assertEqual(value['status'], 'passed', value)
                        self.assertEqual(local['server'].requests, 3)
                self.assertFalse(list((root / '.parser-state').iterdir()))

    def test_reality_compatibility_authenticates_and_rejects_wrong_keys(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / '.parser-state').mkdir()
            with fixture(root / 'fixture', CORES / 'xray', reality=True) as local:
                with patch.dict(os.environ, {'PARSER_TOOLS_DIR': str(CORES),
                        'SSL_CERT_FILE': str(local['certificate']), OPTION: 'true'}):
                    entry = parse(local['node'])
                    value = connectivity.probe(entry, 'mihomo', root, threading.Event(), time.monotonic() + 20)
                    self.assertEqual(value['status'], 'passed', value)
                    self.assertEqual(local['server'].requests, 1)
                    entry.outputs['mihomo']['reality-opts']['public-key'] = KEY
                    with patch.object(connectivity, 'REQUEST_TIMEOUT', 1):
                        value = connectivity.probe(entry, 'mihomo', root, threading.Event(), time.monotonic() + 10)
                    self.assertEqual(value['status'], 'failed', value)
                    self.assertEqual(local['server'].requests, 1)
            self.assertFalse(list((root / '.parser-state').iterdir()))

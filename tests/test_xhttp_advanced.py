"""Mihomo-only advanced XHTTP, exact source bindings, and real transport checks."""
from contextlib import contextmanager
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

from scripts.connectivity_fixture import fixture
from subscription_parser import common, connectivity, protocols, publication, server_policy, service, xhttp
from subscription_parser.validation import checked_combine

CORES = Path(os.environ.get('PARSER_TOOLS_DIR', '/opt/client-cores'))
UUID = '01234567-89ab-4cde-8fab-0123456789ab'


def options():
    return {'path': '/advanced', 'host': 'localhost', 'mode': 'packet-up', 'extra': {
        'headers': {'X-Required': 'synthetic-value'}, 'uplinkHTTPMethod': 'GET',
        'uplinkDataPlacement': 'header', 'uplinkDataKey': 'X-Synthetic-Upload',
        'scMaxEachPostBytes': '1536-6144', 'scMinPostsIntervalMs': '10-30', 'scMaxBufferedPosts': 64,
        'xPaddingObfsMode': True, 'xPaddingMethod': 'tokenish', 'xPaddingPlacement': 'query',
        'xPaddingKey': 'synthetic_padding', 'xPaddingBytes': '48-320',
        'seqPlacement': 'query', 'seqKey': 'synthetic_seq',
        'xmux': {'maxConcurrency': '8-16', 'cMaxReuseTimes': '128-256', 'hKeepAlivePeriod': 30,
                 'hMaxRequestTimes': '600-1000', 'hMaxReusableSecs': '1800-3600'}}}


def outbound():
    return {'tag': 'Advanced', 'protocol': 'vless', 'settings': {'vnext': [{
        'address': 'proxy.example', 'port': 443, 'users': [{'id': UUID, 'encryption': 'none'}]}]},
        'streamSettings': {'network': 'xhttp', 'security': 'tls', 'tlsSettings': {
            'serverName': 'cover.example', 'allowInsecure': False, 'alpn': ['h2'], 'fingerprint': 'firefox'},
            'xhttpSettings': options()}}


def parse(raw):
    return protocols.parse(json.dumps({'outbounds': [raw]}).encode(), 'Test')[0]


def legacy(*args):
    raise protocols.Error('advanced XHTTP settings require original configuration')


class AdvancedXHTTPTests(unittest.TestCase):
    def test_adds_only_mihomo_preserving_xray_tls_auth_and_raw_identity(self):
        raw = outbound()
        original = copy.deepcopy(raw)
        with patch.object(protocols, 'xray_xhttp_to_mihomo', legacy):
            old = parse(raw)
        new = parse(raw)
        self.assertEqual(set(new.outputs), {'mihomo', 'xray'}, new.issues)
        self.assertNotIn('mihomo', new.issues)
        self.assertEqual(new.outputs['xray'], old.outputs['xray'])
        self.assertEqual(new.issues['hiddify'], old.issues['hiddify'])
        self.assertEqual(connectivity.fingerprint(new, 'xray'), connectivity.fingerprint(old, 'xray'))
        self.assertEqual(new.identity, old.identity)
        self.assertEqual(server_policy.source_key(new), server_policy.source_key(old))
        without_tag = {k: v for k, v in raw.items() if k != 'tag'}
        self.assertEqual(new.identity, hashlib.sha256(json.dumps(without_tag,
            sort_keys=True, ensure_ascii=False).encode()).hexdigest())
        proxy = new.outputs['mihomo']
        self.assertEqual((proxy['server'], proxy['port'], proxy['uuid']), ('proxy.example', 443, UUID))
        self.assertEqual(proxy['servername'], 'cover.example')
        self.assertEqual(proxy['alpn'], ['h2'])
        self.assertEqual(proxy['client-fingerprint'], 'firefox')
        self.assertFalse(proxy.get('skip-cert-verify', False))
        self.assertEqual(proxy['xhttp-opts']['uplink-data-key'], 'X-Synthetic-Upload')
        self.assertEqual(proxy['xhttp-opts']['x-padding-key'], 'synthetic_padding')
        self.assertEqual(proxy['xhttp-opts']['reuse-settings']['max-concurrency'], '8-16')
        proxy['xhttp-opts']['headers']['X-Required'] = 'edited'
        self.assertEqual(new.outputs['xray'], original)
        self.assertEqual(raw, original)

    def test_exact_extra_precedence_preserves_base_and_drops_shadowed_options(self):
        raw = options()
        raw.update(headers={'X-Outer': 'must-not-send'}, xPaddingBytes='1-2', uplinkHTTPMethod='POST')
        raw['extra'].update(path='/ignored', host='ignored.example', mode='stream-one')
        converted, _ = xhttp.mihomo_options(raw)
        self.assertEqual((converted['path'], converted['host'], converted['mode']),
                         ('/advanced/', 'localhost', 'packet-up'))
        self.assertEqual(converted['headers'], {'X-Required': 'synthetic-value'})
        self.assertEqual(converted['x-padding-bytes'], '48-320')
        self.assertEqual(converted['uplink-http-method'], 'GET')
        # An empty extra object also replaces outer non-base options in Xray.
        raw['extra'] = {}
        converted, _ = xhttp.mihomo_options(raw)
        self.assertNotIn('headers', converted)
        self.assertEqual(converted['uplink-http-method'], 'POST')
        self.assertEqual(converted['x-padding-bytes'], '100-1000')

    def test_top_level_options_and_legacy_transport_name_are_mapped(self):
        raw = outbound()
        settings = raw['streamSettings'].pop('xhttpSettings')
        settings.update(settings.pop('extra'))
        raw['streamSettings'].update(network='splithttp', splithttpSettings=settings)
        converted = parse(raw)
        self.assertEqual(set(converted.outputs), {'xray', 'mihomo'})
        self.assertEqual(converted.outputs['mihomo']['xhttp-opts']['seq-key'], 'synthetic_seq')

    def test_source_defaults_ranges_and_chunk_sizes_are_preserved(self):
        converted, _ = xhttp.mihomo_options({'mode': 'packet-up', 'extra': {}})
        self.assertEqual(converted['reuse-settings'], {'max-concurrency': '1', 'max-connections': '0',
            'c-max-reuse-times': '0', 'h-max-request-times': '600-900', 'h-max-reusable-secs': '1800-3000',
            'h-keep-alive-period': 0})
        self.assertEqual(converted['sc-min-posts-interval-ms'], '30')
        for placement, expected in [('header', '3000-4000'), ('cookie', '2048-3072'), ('body', '1000000')]:
            raw = {'mode': 'packet-up', 'uplinkDataPlacement': placement, 'xmux': {'maxConnections': 2}}
            converted, _ = xhttp.mihomo_options(raw)
            self.assertEqual(converted['uplink-chunk-size'], expected)
            self.assertEqual(converted['reuse-settings']['max-concurrency'], '0')
            raw['uplinkChunkSize'] = '1-10'
            self.assertEqual(xhttp.mihomo_options(raw)[0]['uplink-chunk-size'], '64')
        for value, expected in [(1, '1'), ('0', '0'), ('08-016', '8-16'), ('2-2', '2')]:
            self.assertEqual(xhttp.range_value(value), expected)
        for value in (True, None, 1.5, -1, 2147483648, '2-1', '-1', '1-2-3', {}, 'secret'):
            with self.subTest(value=value), self.assertRaises(protocols.Error):
                xhttp.range_value(value)

    def test_matching_redundant_session_aliases_preserve_canonical_semantics(self):
        raw = options()
        raw['extra'].update(sessionPlacement='query', sessionKey='synthetic_session',
                            sessionIDPlacement='query', sessionIDKey='synthetic_session')
        converted, notes = xhttp.mihomo_options(raw)
        self.assertEqual(converted['path'], '/advanced/')
        self.assertEqual(converted['session-placement'], 'query')
        self.assertEqual(converted['session-key'], 'synthetic_session')
        self.assertTrue(any('Redundant' in message for message in notes))
        self.assertTrue(any('server-only' in message for message in notes))

    def test_unmapped_malformed_or_ambiguous_options_keep_the_original(self):
        invalid = [
            {'downloadSettings': {}}, {'noSSEHeader': True}, {'unknown': False},
            {'sessionIDPlacement': 'query'}, {'sessionPlacement': 'query', 'sessionIDPlacement': 'header'},
            {'xmux': {'unknown': 0}}, {'xmux': []}, {'xmux': {'hKeepAlivePeriod': True}},
            {'xmux': {'maxConnections': 1, 'maxConcurrency': 2}},
            {'headers': {'Host': 'private.example'}}, {'headers': {'X-Test': 'secret\r\nvalue'}},
            {'headers': {'Bad Name': 'secret'}}, {'headers': []}, {'headers': {'X-A': '1', 'x-a': '2'}},
            {'xPaddingBytes': '0-10'}, {'xPaddingObfsMode': 'false'}, {'scMaxBufferedPosts': -1},
            {'uplinkHTTPMethod': 'DELETE'}, {'uplinkDataPlacement': 'query'},
            {'xPaddingMethod': 'unknown'}, {'seqPlacement': 'unknown'}, {'seqKey': None},
            {'uplinkDataKey': ['private-value']}, {'extra': {}},
        ]
        for index, extra in enumerate(invalid):
            raw = outbound()
            raw['streamSettings']['xhttpSettings']['extra'].update(extra)
            entry = parse(raw)
            with self.subTest(index=index):
                self.assertEqual(entry.outputs, {'xray': raw}, entry.issues)
                self.assertNotIn('private-value', json.dumps(entry.issues))
                self.assertNotIn('private.example', json.dumps(entry.issues))
        for settings in ([], {'extra': []}, {'mode': 'stream-one'},
                         {'mode': 'packet-up', 'path': '/query?secret=1', 'extra': {}},
                         {'mode': 'packet-up', 'unmapped': False}):
            with self.subTest(settings=settings), self.assertRaises(protocols.Error):
                xhttp.mihomo_options(settings)

    def test_fallback_cannot_bypass_other_protocol_or_tls_restrictions(self):
        changes = [lambda r: r['settings']['vnext'][0]['users'][0].update(encryption='unsupported'),
            lambda r: r['streamSettings'].update(finalmask={'tcp': [{'type': 'unknown'}]}),
            lambda r: r['streamSettings']['tlsSettings'].update(allowInsecure='false'),
            lambda r: r['streamSettings']['tlsSettings'].update(enableSessionResumption=True),
            lambda r: r['streamSettings'].update(splithttpSettings=options()),
            lambda r: r['streamSettings'].update(network=[]),
            lambda r: r.update(proxySettings={'tag': 'private-upstream'}),
            lambda r: r.update(protocol='vmess')]
        for index, change in enumerate(changes):
            raw = outbound(); change(raw)
            with self.subTest(index=index):
                self.assertNotIn('mihomo', parse(raw).outputs)
        raw = outbound()
        raw['streamSettings']['tlsSettings']['allowInsecure'] = True
        self.assertTrue(parse(raw).outputs['mihomo']['skip-cert-verify'])

    def test_existing_native_mihomo_and_basic_xhttp_outputs_stay_unchanged(self):
        raw = outbound()
        raw['streamSettings']['xhttpSettings'].pop('extra')
        before = parse(raw)
        with patch.object(protocols, 'xray_xhttp_to_mihomo', side_effect=AssertionError('fallback not needed')):
            after = parse(raw)
        self.assertEqual(after, before)
        native = parse(outbound()).outputs['mihomo']
        parsed = protocols.parse(json.dumps({'proxies': [native]}).encode(), 'Native')[0]
        self.assertEqual(parsed.outputs, {'mihomo': native})

    def test_advanced_reality_keeps_the_existing_opt_in_policy_and_source_keys(self):
        raw = outbound()
        stream = raw['streamSettings']
        stream.pop('tlsSettings')
        stream.update(security='reality', realitySettings={'serverName': 'cover.example',
            'fingerprint': 'firefox', 'shortId': '01ab',
            'publicKey': base64.urlsafe_b64encode(bytes(range(32))).decode().rstrip('=')})
        with patch.dict(os.environ, {'PARSER_MIHOMO_REALITY_COMPATIBILITY': 'false'}):
            before = parse(raw)
        with patch.dict(os.environ, {'PARSER_MIHOMO_REALITY_COMPATIBILITY': 'true'}):
            after = parse(raw)
        self.assertEqual(before.identity, after.identity)
        self.assertEqual(before.outputs['xray'], after.outputs['xray'])
        self.assertEqual(before.outputs['mihomo']['client-fingerprint'], 'firefox')
        self.assertEqual(after.outputs['mihomo']['client-fingerprint'], 'chrome')
        self.assertTrue(after.outputs['mihomo']['reality-opts']['support-x25519mlkem768'])
        self.assertTrue(any('X25519-MLKEM768' in note for note in after.notes))

    def test_new_variant_requires_its_own_check_and_preserves_other_exports(self):
        raw = outbound()
        with patch.object(protocols, 'xray_xhttp_to_mihomo', legacy):
            old = parse(raw)
        new = parse(raw)
        checks = connectivity.check_entries([old], Path('/unused'), tester=lambda *a: connectivity.result('passed'))
        before, _ = publication.build([old], checks, 1200, time.time(), common.now(), {}, common.now())
        after, _ = publication.build([new], checks, 1200, time.time(), common.now(), {}, common.now())
        for filename in ('xray.json', 'hiddify.json', 'v2board.txt'):
            self.assertEqual(after['exports'][filename], before['exports'][filename])
        self.assertEqual(after['counts']['mihomo'], 0)
        calls = []
        def tester(entry, core, *args):
            calls.append(core)
            return connectivity.result('passed')
        fresh = connectivity.check_entries([new], Path('/unused'), previous=checks, reuse_seconds=1200, tester=tester)
        self.assertEqual(calls, ['mihomo'])
        self.assertEqual(set(connectivity.filtered_entries([new], fresh, 1200)[0].outputs), {'xray', 'mihomo'})

    def test_restart_preserves_saved_servers_samples_and_existing_checks(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {
                'PARSER_ADMIN_TOKEN': 'advanced-xhttp-upgrade'}), patch.object(service, 'checked_combine',
                side_effect=lambda entries, root: protocols.combine(entries) | {'validation': {}}):
            nodes = [outbound() for _ in range(5)]
            for i, node in enumerate(nodes):
                node['tag'] = f'Advanced {i}'
                node['settings']['vnext'][0]['address'] = f'node-{i}.example'
            fetches, probes = [], []
            def fetch(*args):
                fetches.append(args)
                return json.dumps({'outbounds': nodes}).encode(), {'fetched_at': common.now()}
            def tester(entry, core, *args):
                probes.append(core)
                return connectivity.result('passed')
            def checker(entries, root, **kwargs):
                return connectivity.check_entries(entries, root, tester=tester, **kwargs)
            old_policy = protocols.adapter_policy() | {'version': 2}
            with patch.object(protocols, 'xray_xhttp_to_mihomo', legacy), patch.object(
                    protocols, 'adapter_policy', return_value=old_policy):
                old = service.Manager(Path(folder), fetch, checker)
                old.change_provider({'name': 'Test', 'url': 'https://provider.example/sub',
                                     'hwid_id': None, 'server_limit': 2})
                old.refresh()
                identifier = old.change_subscription({'name': 'Phone', 'all_providers': True, 'provider_ids': []})
                available = old.subscription_servers(identifier)['available'][0]
                old.change_subscription_servers(identifier, {'revision': 0, 'all_servers': False,
                    'records': [{k: available[k] for k in ('provider_id', 'source_identity')}]})
                old.refresh(download=False, check=True, reuse_recent=True)
                before = copy.deepcopy(old.snapshot)
                saved = old.store.server_policy(identifier)
                config, subscriptions = old.store.config(), old.store.subscriptions()
            fetches.clear(); probes.clear()
            new = service.Manager(Path(folder), fetch, checker)
            self.assertEqual(set(new.jobs.items), {'check'})
            new.refresh(download=False, check=True, reuse_recent=True)
            self.assertNotIn('last_error', new.snapshot)
            self.assertEqual(fetches, [])
            self.assertEqual(probes, ['mihomo', 'mihomo'])
            self.assertEqual(new.snapshot['active_selections'], before['active_selections'])
            self.assertEqual({r['id'] for r in new.snapshot['inventory']}, {r['id'] for r in before['inventory']})
            self.assertEqual(new.store.server_policy(identifier), saved)
            self.assertEqual(new.store.config(), config)
            self.assertEqual(new.store.subscriptions(), subscriptions)
            self.assertEqual(new.snapshot['counts']['xray'], before['counts']['xray'])
            self.assertEqual(new.snapshot['counts']['mihomo'], 2)
            self.assertFalse(service.Manager(Path(folder), fetch, checker).jobs.items)


@contextmanager
def local_entry(tls=True, session_query=False):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / '.parser-state').mkdir()
        settings = options()
        if session_query:
            settings['extra'].update(sessionPlacement='query', sessionKey='synthetic_session',
                                      sessionIDPlacement='query', sessionIDKey='synthetic_session')
        with fixture(root / 'fixture', CORES / 'xray', xhttp=settings, tls=tls) as local:
            native = copy.deepcopy(local['node'])
            native['transport'] = {'type': 'xhttp', 'mode': 'packet-up', 'path': '/advanced'}
            raw = protocols.native_to_xray(native)
            raw['streamSettings']['xhttpSettings'] = settings
            with patch.dict(os.environ, {'PARSER_TOOLS_DIR': str(CORES), 'SSL_CERT_FILE': str(local['certificate'])}):
                yield root, local, parse(raw)
            assert not list((root / '.parser-state').iterdir())


@unittest.skipUnless(all((CORES / name).is_file() for name in ('mihomo', 'xray', 'hiddify-core')),
                     'Docker image with client cores required')
class AdvancedXHTTPCoreTests(unittest.TestCase):
    def probe(self, root, entry, core='mihomo'):
        return connectivity.probe(entry, core, root, threading.Event(), time.monotonic() + 15)

    def test_advanced_headers_padding_and_xmux_tunnel_over_http_and_tls(self):
        for tls, session_query in [(False, False), (True, False), (True, True)]:
            with self.subTest(tls=tls, session_query=session_query), local_entry(tls, session_query) as (root, local, entry):
                checked = checked_combine([entry], root)
                self.assertEqual(checked['counts'], {'hiddify': 0, 'mihomo': 1, 'xray': 1}, checked['validation'])
                for core in ('mihomo', 'xray'):
                    value = self.probe(root, entry, core)
                    self.assertEqual(value['status'], 'passed', value)
                self.assertEqual(local['server'].requests, 2)

    def test_discarding_required_advanced_options_fails_the_same_server(self):
        with local_entry() as (root, local, entry):
            opts = entry.outputs['mihomo']['xhttp-opts']
            entry.outputs['mihomo']['xhttp-opts'] = {k: v for k, v in opts.items() if k in {'path', 'host', 'mode'}}
            with patch.object(connectivity, 'REQUEST_TIMEOUT', 1):
                self.assertEqual(self.probe(root, entry)['status'], 'failed')
            self.assertEqual(local['server'].requests, 0)

    def test_wrong_credentials_fail_without_direct_fallback(self):
        with local_entry() as (root, local, entry):
            entry.outputs['mihomo']['uuid'] = '11234567-89ab-4cde-8fab-0123456789ab'
            with patch.object(connectivity, 'REQUEST_TIMEOUT', 1):
                self.assertEqual(self.probe(root, entry)['status'], 'failed')
            self.assertEqual(local['server'].requests, 0)

    def test_tls_certificate_verification_remains_enforced(self):
        with local_entry() as (root, local, entry):
            entry.outputs['mihomo']['servername'] = 'untrusted.example'
            with patch.object(connectivity, 'REQUEST_TIMEOUT', 1):
                self.assertEqual(self.probe(root, entry)['status'], 'failed')
            self.assertEqual(local['server'].requests, 0)


if __name__ == '__main__':
    unittest.main()

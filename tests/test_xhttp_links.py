"""Rejected XHTTP URI fallback: lossless settings, isolated exports and real cores."""
import base64
import copy
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qsl, quote, urlencode, urlsplit

from subscription_parser import common, connectivity, protocols, publication, server_policy, service
from subscription_parser.validation import checked_combine
from test_parser_protocols import native
from test_xhttp_advanced import CORES, UUID, local_entry, outbound


def link(raw=None, **changes):
    raw = raw or outbound()
    server = raw['settings']['vnext'][0]
    stream = raw['streamSettings']
    tls = stream.get('tlsSettings', {})
    settings = stream['xhttpSettings']
    query = {'encryption': 'none', 'type': 'xhttp', 'security': stream['security'],
             'sni': tls.get('serverName'), 'fp': tls.get('fingerprint'),
             'alpn': ','.join(tls['alpn']) if tls.get('alpn') else None,
             'insecure': '1' if tls.get('allowInsecure') else '0',
             'quicSecurity': 'none', 'headerType': 'none', 'pcs': '', 'serviceName': '',
             **{k: settings[k] for k in ('path', 'host', 'mode') if k in settings},
             'extra': json.dumps(settings['extra']) if 'extra' in settings else None}
    query.update(changes)
    host = server['address']
    if ':' in host:
        host = '[' + host + ']'
    return (f"vless://{server['users'][0]['id']}@{host}:{server['port']}?"
            + urlencode({k: v for k, v in query.items() if v is not None})
            + '#' + quote(raw.get('tag', 'Advanced')))


def adapt(uri):
    return protocols.adapt('uri', uri, 'Test', 'Advanced')


class XHTTPLinkTests(unittest.TestCase):
    def test_preserves_exact_extra_auth_tls_and_original_uri(self):
        for insecure in (False, True):
            raw = outbound()
            raw.pop('tag')
            raw['streamSettings']['tlsSettings']['allowInsecure'] = insecure
            uri = link(raw)
            entry = protocols.parse(base64.b64encode(uri.encode()), 'Test')[0]
            expected = copy.deepcopy(raw)
            if not insecure:
                expected['streamSettings']['tlsSettings'].pop('allowInsecure')
            self.assertEqual(set(entry.outputs), {'mihomo', 'xray'})
            self.assertEqual(entry.outputs['xray'], expected)
            equivalent = protocols.adapt('xray', expected, 'Test', 'Advanced')
            self.assertEqual(entry.outputs['mihomo'], equivalent.outputs['mihomo'])
            self.assertEqual(entry.original, uri)
            self.assertEqual(entry.kind, 'uri')
            self.assertIn('no verified Hiddify mapping', entry.issues['hiddify'])
            self.assertEqual(entry.outputs['mihomo'].get('skip-cert-verify', False), insecure)
            entry.outputs['mihomo']['xhttp-opts']['headers']['X-Required'] = 'changed'
            self.assertEqual(entry.outputs['xray'], expected)

    def test_inactive_quic_default_and_empty_extra_preserve_basic_conversions(self):
        baseline = adapt(link(extra=None, quicSecurity=None))
        for extra in (None, '{}'):
            for mode in ('auto', 'packet-up', 'stream-up', 'stream-one'):
                uri = link(extra=extra, mode=mode)
                expected = adapt(link(extra=None, quicSecurity=None, mode=mode))
                outputs = adapt(uri).outputs
                outputs['xray']['streamSettings']['xhttpSettings'].pop('extra', None)
                # The native Xray adapter makes its VLESS UDP default explicit.
                expected.outputs['hiddify']['packet_encoding'] = 'xudp'
                expected.outputs['mihomo']['packet-encoding'] = 'xudp'
                self.assertEqual(outputs, expected.outputs)
        self.assertEqual(set(baseline.outputs), set(protocols.TARGETS))

    def test_new_advanced_link_does_not_require_quic_parameter(self):
        self.assertEqual(adapt(link(quicSecurity=None)).outputs, adapt(link()).outputs)

    def test_unsupported_malformed_and_ambiguous_links_are_not_published(self):
        cases = [dict(quicSecurity='aes-128-gcm'), dict(pcs='private-pin'), dict(extra='[]'),
            dict(extra='null'), dict(extra='false'), dict(extra='{'),
            dict(extra='{"uplinkHTTPMethod":"GET","uplinkHTTPMethod":"POST"}'),
            dict(extra='{"xmux":{"maxConnections":1,"maxConnections":2}}'),
            dict(extra='{"xPaddingBytes":NaN}'), dict(extra='{"xPaddingBytes":1e999}'),
            dict(extra=json.dumps({'uplinkDataKey': 'x' * 65536})),
            dict(extra='{"headers":{"X-Nested":' + '[' * 600 + '0' + ']' * 600 + '}}'),
            dict(extra='{"path":[]}'), dict(extra='{"downloadSettings":{}}'),
            dict(extra='{"unknown":false}'), dict(extra='{"uplinkDataKey":["secret"]}'),
            dict(extra='{"xPaddingObfsMode":"false"}'), dict(extra='{"scMaxEachPostBytes":false}'),
            dict(extra='{"headers":{"Host":"private.example"}}'),
            dict(mode='stream-one'), dict(serviceName='required'), dict(authority='required'),
            dict(insecure='sometimes'), dict(insecure='1', allowInsecure='false'),
            dict(peer='different.example'), dict(spx='/probe'),
            dict(security='none'), dict(type='ws'), dict(encryption='new-cipher')]
        for index, changes in enumerate(cases):
            with self.subTest(index=index):
                entry = adapt(link(**changes))
                self.assertEqual(entry.outputs, {})
                self.assertEqual(set(entry.issues), set(protocols.TARGETS))
                self.assertNotIn('secret', json.dumps(entry.issues))
                self.assertNotIn('private.example', json.dumps(entry.issues))
        for suffix in ('&mode=packet-up', '&type=ws', '&bad=%ZZ', '&bad=%FF'):
            uri = link().split('#')[0] + suffix
            self.assertFalse(adapt(uri).outputs)
        for uri in (link().replace(':443?', ':443/unmapped?'),
                    link().replace('@', ':password@'), link().replace('vless:', 'trojan:'),
                    link().replace('mode=packet-up', 'mode=packet-\tup')):
            self.assertFalse(adapt(uri).outputs)

    def test_reality_probe_path_is_kept_in_xray_with_existing_client_restrictions(self):
        uri = link(security='reality', spx='/probe', pbk=base64.urlsafe_b64encode(bytes(range(32))).decode(),
                   sid='01ab', alpn=None)
        entry = adapt(uri)
        self.assertEqual(entry.outputs['xray']['streamSettings']['realitySettings']['spiderX'], '/probe')
        self.assertNotIn('mihomo', entry.outputs)
        self.assertNotIn('hiddify', entry.outputs)

    def test_old_raw_identity_and_saved_source_binding_survive_upgrade(self):
        uri = link()
        with patch.object(protocols, 'xhttp_uri_to_xray', return_value=None):
            old = adapt(uri)
        new = adapt(uri)
        self.assertFalse(old.outputs)
        self.assertEqual(new.identity, old.identity)
        self.assertEqual(server_policy.source_key(new), server_policy.source_key(old))
        record = {'source_identity': server_policy.source_key(old), '_snapshot': server_policy.freeze(old)}
        restored = server_policy.restore(record, 'Test')
        self.assertEqual(restored.outputs, new.outputs)
        self.assertEqual(restored.identity, new.identity)

    def test_customized_endpoints_and_both_v2board_formats_preserve_extra(self):
        entry = adapt(link())
        custom = server_policy.customize(entry, {'name': 'Custom', 'address': 'custom.example', 'port': 8443})
        custom.pinned = True
        checks = connectivity.check_entries([custom], Path('/unused'), tester=lambda *a: connectivity.result('passed'))
        view, _ = publication.build([custom], checks, 1200, time.time(), common.now(), {}, common.now())
        uri = view['exports']['v2board.txt'].strip()
        parsed = urlsplit(uri)
        self.assertEqual((parsed.hostname, parsed.port), ('custom.example', 8443))
        self.assertEqual(dict(parse_qsl(parsed.query))['extra'], dict(parse_qsl(urlsplit(entry.original).query))['extra'])
        exported = json.loads(view['exports']['v2board.json'])['nodes'][0]
        self.assertEqual(exported['uri'], uri)
        self.assertEqual((exported['server'], exported['port']), ('custom.example', 8443))
        self.assertEqual(custom.outputs['xray']['streamSettings'], entry.outputs['xray']['streamSettings'])
        self.assertEqual(custom.outputs['mihomo']['xhttp-opts'], entry.outputs['mihomo']['xhttp-opts'])
        self.assertEqual(custom.original, entry.original)

    def test_new_outputs_require_their_own_checks_before_publication(self):
        uri = link()
        with patch.object(protocols, 'xhttp_uri_to_xray', return_value=None):
            old = adapt(uri)
        checks = connectivity.check_entries([old], Path('/unused'), tester=lambda *a: self.fail('unsupported probe'))
        new = adapt(uri)
        pending, _ = publication.build([new], checks, 1200, time.time(), common.now(), {}, common.now())
        self.assertEqual(pending['published_total'], 0)
        calls = []
        def tester(entry, core, *args):
            calls.append(core)
            return connectivity.result('passed' if core == 'mihomo' else 'failed')
        fresh = connectivity.check_entries([new], Path('/unused'), previous=checks, reuse_seconds=1200, tester=tester)
        self.assertCountEqual(calls, ['mihomo', 'xray'])
        self.assertEqual(set(connectivity.filtered_entries([new], fresh, 1200)[0].outputs), {'mihomo'})

    def test_other_protocols_transports_and_all_six_formats_stay_identical(self):
        basic = [link(extra=None, quicSecurity=None, type=transport, mode=None)
                 for transport in ('tcp', 'ws', 'httpupgrade', 'xhttp')]
        basic += [f'trojan://password@proxy.example:443?type=ws#Trojan',
                  'hysteria://proxy.example:443?auth=secret&upmbps=20&downmbps=100',
                  'hy2://secret@proxy.example:443?obfs=salamander&obfs-password=synthetic',
                  f'tuic://{UUID}:password@proxy.example:443',
                  'socks://user:password@proxy.example:1080', 'ss://YWVzLTEyOC1nY206cGFzcw@proxy.example:443']
        documents = ['\n'.join(basic).encode(), json.dumps({'outbounds': [native(kind) for kind in
            ('vless', 'vmess', 'trojan', 'shadowsocks', 'hysteria', 'hysteria2', 'tuic', 'socks', 'http')]}).encode(),
            json.dumps({'outbounds': [outbound()]}).encode()]
        for document in documents:
            before = protocols.parse(document, 'Regression')
            with patch.object(protocols, 'xhttp_uri_to_xray', side_effect=AssertionError('fallback not needed')):
                after = protocols.parse(document, 'Regression')
            self.assertEqual(before, after)
            checks = connectivity.check_entries(before, Path('/unused'), tester=lambda *a: connectivity.result('passed'))
            now, timestamp = time.time(), common.now()
            old, _ = publication.build(before, checks, 1200, now, timestamp, {}, timestamp)
            new, _ = publication.build(after, checks, 1200, now, timestamp, {}, timestamp)
            self.assertEqual(set(new['exports']), set(publication.FILES))
            self.assertEqual(new['exports'], old['exports'])

    def test_restart_reuses_existing_checks_without_fetch_or_redraw_or_policy_changes(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {
                'PARSER_ADMIN_TOKEN': 'xhttp-uri-upgrade'}), patch.object(service, 'checked_combine',
                side_effect=lambda entries, root: protocols.combine(entries) | {'validation': {}}):
            fetches, probes = [], []
            body = '\n'.join([f'vless://{UUID}@basic.example:443?security=tls#Basic', link(),
                              link().replace('proxy.example', 'second.example')]).encode()
            def fetch(*args):
                fetches.append(True)
                return body, {'fetched_at': common.now()}
            def tester(entry, core, *args):
                probes.append(core)
                return connectivity.result('passed')
            def checker(entries, root, **kwargs):
                return connectivity.check_entries(entries, root, tester=tester, **kwargs)
            with patch.object(protocols, 'xhttp_uri_to_xray', return_value=None), patch.object(
                    protocols, 'adapter_policy', return_value={'version': 5, 'mihomo_reality_compatibility': False}):
                old = service.Manager(Path(folder), fetch, checker)
                old.change_provider({'name': 'Test', 'url': 'https://provider.example/sub', 'hwid_id': None})
                old.refresh()
                identifier = old.change_subscription({'name': 'Phone', 'all_providers': True, 'provider_ids': []})
                available = next(r for r in old.subscription_servers(identifier)['available'] if r['source']['name'] == 'Basic')
                old.change_subscription_servers(identifier, {'revision': 0, 'all_servers': False,
                    'records': [{k: available[k] for k in ('provider_id', 'source_identity')}]})
                old.refresh(download=False, check=True, reuse_recent=True)
                before = copy.deepcopy(old.snapshot)
                saved, config, subs = old.store.server_policy(identifier), old.store.config(), old.store.subscriptions()
            fetches.clear(); probes.clear()
            new = service.Manager(Path(folder), fetch, checker)
            self.assertEqual(set(new.jobs.items), {'check'})
            new.refresh(download=False, check=True, reuse_recent=True)
            self.assertNotIn('last_error', new.snapshot)
            self.assertFalse(fetches)
            self.assertCountEqual(probes, ['mihomo', 'xray', 'mihomo', 'xray'])
            self.assertEqual(new.snapshot['active_selections'], before['active_selections'])
            self.assertEqual({r['id'] for r in new.snapshot['inventory']}, {r['id'] for r in before['inventory']})
            self.assertEqual(new.store.server_policy(identifier), saved)
            self.assertEqual(new.store.config(), config)
            self.assertEqual(new.store.subscriptions(), subs)
            self.assertEqual(new.snapshot['counts']['hiddify'], before['counts']['hiddify'])
            self.assertEqual(new.snapshot['counts']['mihomo'], 3)
            self.assertFalse(service.Manager(Path(folder), fetch, checker).jobs.items)


@unittest.skipUnless(all((CORES / name).is_file() for name in ('mihomo', 'xray', 'hiddify-core')),
                     'Docker image with client cores required')
class XHTTPLinkCoreTests(unittest.TestCase):
    def test_imported_links_tunnel_https_with_all_required_extra_options(self):
        for tls in (False, True):
            with self.subTest(tls=tls), local_entry(tls) as (root, local, original):
                entry = adapt(link(original.outputs['xray']))
                checked = checked_combine([entry], root)
                self.assertEqual(checked['counts'], {'hiddify': 0, 'mihomo': 1, 'xray': 1}, checked['validation'])
                for core in ('mihomo', 'xray'):
                    value = connectivity.probe(entry, core, root, threading.Event(), time.monotonic() + 15)
                    self.assertEqual(value['status'], 'passed', value)
                self.assertEqual(local['server'].requests, 2)

    def test_bad_link_credentials_or_tls_name_cannot_bypass_checks(self):
        with local_entry() as (root, local, original), patch.object(connectivity, 'REQUEST_TIMEOUT', 1):
            valid = link(original.outputs['xray'])
            for uri in (valid.replace(UUID, '11234567-89ab-4cde-8fab-0123456789ab'),
                        link(original.outputs['xray'], sni='untrusted.example')):
                entry = adapt(uri)
                for core in ('mihomo', 'xray'):
                    value = connectivity.probe(entry, core, root, threading.Event(), time.monotonic() + 15)
                    self.assertEqual(value['status'], 'failed', value)
            self.assertEqual(local['server'].requests, 0)


if __name__ == '__main__':
    unittest.main()

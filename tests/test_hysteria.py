"""Protocol fidelity and publication boundaries for V2Board Hysteria feeds."""
import base64
import copy
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from urllib.parse import parse_qs, urlsplit

from subscription_parser import common, connectivity, hysteria, protocols, v2board
from subscription_parser.validation import checked_combine

PASSWORD = 'user:p@ss:+ &/#?é'
PIN = '12' * 32


def proxy(kind='hysteria2'):
    node = {'type': kind, 'name': 'Synthetic', 'server': '2001:db8::1', 'port': 443,
            'sni': 'cover.example', 'alpn': ['hysteria' if kind == 'hysteria' else 'h3'],
            'skip-cert-verify': False}
    if kind == 'hysteria':
        node.update({'auth-str': PASSWORD, 'up': 20, 'down': 100, 'obfs': 'xplus:+ secret'})
    else:
        node.update(password=PASSWORD, obfs='salamander', **{'obfs-password': 'salamander:+ secret'})
    return node


def parse(node):
    return protocols.parse(json.dumps({'proxies': [node]}).encode(), 'Synthetic')


class HysteriaTests(unittest.TestCase):
    def test_native_formats_roundtrip_all_connection_fields(self):
        for kind in ('hysteria', 'hysteria2'):
            original = proxy(kind)
            original.update(up=20, down=100, ports='443,5000-6000')
            native = protocols.mihomo_to_native(original, [])
            for document in ({'proxies': [original]}, {'outbounds': [native]}):
                with self.subTest(kind=kind, format=list(document)[0]):
                    entries = protocols.parse(json.dumps(document).encode(), 'Test')
                    result = v2board.export(entries)
                    self.assertEqual(result['count'], 1, result['diagnostics'])
                    restored = protocols.parse(result['body'].encode(), 'Roundtrip')[0].outputs['mihomo']
                    for key in ('server', 'sni', 'alpn', 'skip-cert-verify', 'obfs', 'up', 'down'):
                        self.assertEqual(restored[key], original[key])
                    self.assertEqual(hysteria.ports(restored['ports']), ['443-443', '5000-6000'] if 'outbounds' in document else ['443', '5000-6000'])
                    auth = 'auth-str' if kind == 'hysteria' else 'password'
                    self.assertEqual(restored[auth], PASSWORD)
                    if kind == 'hysteria2':
                        self.assertEqual(restored['obfs-password'], original['obfs-password'])

    def test_original_links_keep_userpass_plus_decoding_and_default_port(self):
        cases = [
            ('hy2://user:pass+word@proxy.example/?sni=cover.example&obfs=salamander&obfs-password=a%2Bb+c#Name', 'user:pass+word', 'a+b c'),
            ('hysteria://proxy.example:8443?auth=a%2Bb+c&upmbps=20&downmbps=100&obfs=xplus&obfsParam=a%2Bb+c#Name', 'a+b c', 'a+b c'),
            ('hysteria2://proxy.example#NoAuth', '', None),
            ('HY2://p@proxy.example#Uppercase', 'p', None),
        ]
        for uri, auth, obfs in cases:
            with self.subTest(scheme=uri.split(':')[0]):
                entries = protocols.parse(uri.encode(), 'Test')
                result = v2board.export(entries)
                self.assertEqual(result['count'], 1, result['diagnostics'])
                self.assertFalse(result['body'].startswith('hy2:'))
                restored = protocols.parse(result['body'].encode(), 'Roundtrip')[0].outputs['mihomo']
                v1 = restored['type'] == 'hysteria'
                self.assertEqual(restored['auth-str' if v1 else 'password'], auth)
                self.assertEqual(restored['port'], 8443 if v1 else 443)
                if obfs:
                    self.assertEqual(restored['obfs' if v1 else 'obfs-password'], obfs)

    def test_hysteria2_does_not_invent_bandwidth(self):
        result = v2board.export(parse(proxy()))
        params = parse_qs(urlsplit(result['body'].strip()).query)
        self.assertNotIn('upmbps', params)
        self.assertNotIn('downmbps', params)
        restored = protocols.parse(result['body'].encode(), 'Roundtrip')[0]
        self.assertNotIn('up_mbps', restored.outputs['hiddify'])

    def test_port_hopping_authority_and_mport_reach_both_supported_clients(self):
        for kind in ('hysteria', 'hy2'):
            credentials = 'auth=secret&upmbps=20&downmbps=100&' if kind == 'hysteria' else ''
            user = '' if kind == 'hysteria' else 'secret@'
            for address, query in [('443,5000-6000', ''), ('443', 'mport=443,5000-6000')]:
                uri = f'{kind}://{user}[2001:db8::1]:{address}?{credentials}{query}#Hop'
                record = protocols.parse(uri.encode(), 'Test')[0]
                self.assertEqual(record.outputs['mihomo']['ports'], '443,5000-6000')
                self.assertEqual(record.outputs['hiddify']['server_ports'], ['443:443', '5000:6000'])
                self.assertNotIn('server_port', record.outputs['hiddify'])
                self.assertNotIn('xray', record.outputs)
                self.assertEqual(v2board.export([record])['count'], 1)

    def test_certificate_pin_is_preserved_without_weakening_other_clients(self):
        node = proxy(); node['fingerprint'] = PIN
        for records in (parse(node), protocols.parse(f'hy2://secret@proxy.example?pinSHA256={PIN}'.encode(), 'Test')):
            self.assertEqual(set(records[0].outputs), {'mihomo'})
            result = v2board.export(records)
            self.assertEqual(parse_qs(urlsplit(result['body'].strip()).query)['pinSHA256'], [PIN])
            restored = protocols.parse(result['body'].encode(), 'Roundtrip')[0]
            self.assertEqual(restored.outputs['mihomo']['fingerprint'], PIN)
            self.assertEqual(set(restored.outputs), {'mihomo'})

    def test_hysteria1_carrier_retained_only_for_mihomo(self):
        for carrier in ('wechat-video', 'faketcp'):
            node = proxy('hysteria'); node['protocol'] = carrier
            records = parse(node)
            self.assertEqual(set(records[0].outputs), {'mihomo'})
            uri = v2board.export(records)['body']
            restored = protocols.parse(uri.encode(), 'Roundtrip')[0]
            self.assertEqual(restored.outputs['mihomo']['protocol'], carrier)
            self.assertEqual(set(restored.outputs), {'mihomo'})

    def test_base64_text_authentication_preserved_but_binary_rejected(self):
        node = proxy('hysteria'); node.pop('auth-str')
        node['auth'] = base64.b64encode(PASSWORD.encode()).decode()
        uri = v2board.export(parse(node))['body']
        self.assertEqual(parse_qs(urlsplit(uri.strip()).query)['auth'], [PASSWORD])
        node['auth'] = base64.b64encode(b'\xff\x00').decode()
        result = v2board.export(parse(node))
        self.assertEqual(result['count'], 0)
        self.assertIn('binary', result['diagnostics'][0]['reason'])

    def test_xray_hysteria2_exports_without_changing_password_or_tls(self):
        node = proxy(); node.pop('obfs'); node.pop('obfs-password')
        native = protocols.mihomo_to_native(node, [])
        raw = protocols.native_to_xray(native)
        raw['streamSettings']['tlsSettings']['fingerprint'] = 'firefox'
        records = protocols.parse(json.dumps({'outbounds': [raw]}).encode(), 'Xray')
        result = v2board.export(records)
        self.assertEqual(result['count'], 1, result['diagnostics'])
        restored = protocols.parse(result['body'].encode(), 'Roundtrip')[0].outputs['xray']
        self.assertEqual(restored['streamSettings']['hysteriaSettings'], raw['streamSettings']['hysteriaSettings'])
        self.assertEqual(restored['streamSettings']['tlsSettings'],
                         {k: v for k, v in raw['streamSettings']['tlsSettings'].items() if k != 'fingerprint'})
        self.assertNotIn('pinSHA256', result['body'])
        self.assertNotIn('firefox', result['body'])
        raw['streamSettings']['tlsSettings']['allowInsecure'] = 'false'
        self.assertEqual(v2board.export(protocols.parse(json.dumps(raw).encode(), 'Xray'))['count'], 0)

    def test_unrepresentable_source_settings_are_excluded_with_safe_diagnostics(self):
        for key, value in [('hop-interval', 12), ('dialer-proxy', 'secret-upstream'),
                           ('client-fingerprint', 'chrome'), ('recv-window', 123456)]:
            node = proxy(); node[key] = value
            result = v2board.export(parse(node))
            self.assertEqual(result['count'], 0, key)
            self.assertNotIn(PASSWORD, json.dumps(result['diagnostics']))
            self.assertNotIn('secret-upstream', json.dumps(result['diagnostics']))
        raw = protocols.native_to_xray(protocols.mihomo_to_native({k: v for k, v in proxy().items() if k not in {'obfs', 'obfs-password'}}, []))
        raw['streamSettings']['finalmask'] = {'quicParams': {'congestion': 'bbr'}}
        result = v2board.export(protocols.parse(json.dumps(raw).encode(), 'Xray'))
        self.assertEqual(result['count'], 0)

    def test_malformed_and_ambiguous_links_cannot_be_exported(self):
        invalid = [
            'hy2://p@proxy.example:0', 'hy2://p@proxy.example:5000-4000',
            'hy2://p@proxy.example:65536', 'hy2://p@proxy.example:01',
            'hy2://p@proxy.example:443,5000-6000?mport=7000-8000',
            'hy2://p@proxy.example?sni=a&sni=b', 'hy2://p@proxy.example?insecure=0&allowInsecure=1',
            'hy2://p@proxy.example?pinSHA256=invalid', 'hy2://p@proxy.example?obfs=unknown&obfs-password=p',
            'hy2://p@proxy.example?obfs=salamander', 'hy2://p@proxy.example?obfs-password=p',
            'hy2://p@proxy.example?ech=secret-config', 'hy2://p@proxy.example?insecure=yes',
            'hy2://p@proxy.example?upmbps=1.5', 'hy2://p@proxy.example?alpn=h3,',
            'hy2://p%ZZ@proxy.example', 'hy2://p%00@proxy.example', 'hy2://p@proxy.example/path',
            'hy2://p@p@proxy.example', 'hysteria://p@proxy.example:443?upmbps=1&downmbps=2',
            'hysteria://proxy.example:443?auth=p',
            'hysteria://proxy.example:443?auth=p&upmbps=1&downmbps=2&sni=a&peer=b',
        ]
        for uri in invalid:
            with self.subTest(uri=uri):
                record = protocols.adapt('uri', uri, 'Test', 'Invalid')
                self.assertFalse(record.outputs)
                self.assertEqual(v2board.export([record])['count'], 0)
        with self.assertRaises(common.SubscriptionError):
            hysteria.ports(','.join(['443'] * 65))

    def test_health_filter_allows_only_fresh_passing_nodes(self):
        nodes = parse(proxy('hysteria')) + parse(proxy())
        original = copy.deepcopy(nodes)
        checks = connectivity.check_entries(nodes, Path('/unused'), tester=lambda entry, core, *args:
            connectivity.result('passed' if entry.protocol == 'hysteria' else 'failed'))
        result = v2board.export(connectivity.filtered_entries(nodes, checks, 1200))
        self.assertEqual(result['count'], 1)
        self.assertTrue(result['body'].startswith('hysteria://'))
        for row in checks.values():
            for value in row.values():
                value['checked_ts'] = time.time() - 1300
        self.assertEqual(v2board.export(connectivity.filtered_entries(nodes, checks, 1200))['count'], 0)
        self.assertEqual(v2board.export(connectivity.filtered_entries(nodes, {}, 1200))['count'], 0)
        self.assertEqual(nodes, original)

    def test_tuic_and_anytls_stay_out_of_v2board_feed(self):
        node = proxy(); node['type'] = 'anytls'; node.pop('obfs'); node.pop('obfs-password')
        self.assertEqual(v2board.export(parse(node))['count'], 0)
        node.update(type='tuic', uuid='01234567-89ab-4cde-8fab-0123456789ab')
        self.assertEqual(v2board.export(parse(node))['count'], 0)

    @unittest.skipUnless((Path(os.environ.get('PARSER_TOOLS_DIR', '/opt/client-cores')) / 'mihomo').is_file(), 'Docker image with client cores required')
    def test_generated_hysteria_configs_validate_with_installed_cores(self):
        nodes = []
        for kind in ('hysteria', 'hysteria2'):
            node = proxy(kind); node['ports'] = '443,5000-6000'
            nodes.extend(protocols.parse(v2board.export(parse(node))['body'].encode(), 'Test'))
        simple = proxy(); simple.pop('obfs'); simple.pop('obfs-password')
        nodes.extend(parse(simple))
        pinned = proxy(); pinned['fingerprint'] = PIN
        nodes.extend(protocols.parse(v2board.export(parse(pinned))['body'].encode(), 'Test'))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / '.parser-state').mkdir()
            result = checked_combine(nodes, root)
        self.assertEqual(result['counts'], {'mihomo': 4, 'hiddify': 3, 'xray': 1})
        self.assertTrue(all(item['rejected'] == 0 for item in result['validation'].values()), result['validation'])


if __name__ == '__main__':
    unittest.main()

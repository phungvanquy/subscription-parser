import base64
import copy
import json
from urllib.parse import parse_qs, unquote, urlsplit
import unittest
from unittest.mock import patch

from subscription_parser import hysteria, protocols, server_policy, v2board

UUID = '01234567-89ab-4cde-8fab-0123456789ab'


def node(kind='vless'):
    result = {'type': kind, 'tag': 'Test', 'server': 'proxy.example', 'server_port': 443}
    if kind in {'vless', 'vmess'}:
        result['uuid'] = UUID
    if kind in {'trojan', 'shadowsocks', 'hysteria2'}:
        result['password'] = 'pass:a&b#é'
    if kind == 'shadowsocks':
        result['method'] = 'aes-128-gcm'
    if kind in {'trojan', 'hysteria', 'hysteria2'}:
        result['tls'] = {'enabled': True, 'server_name': 'cover.example'}
    if kind == 'hysteria':
        result.update(auth_str='pass:a&b#é', up_mbps=20, down_mbps=100)
    return result


def entries(nodes):
    return protocols.parse(json.dumps({'outbounds': nodes}).encode(), 'Example')


class V2BoardTests(unittest.TestCase):
    def test_supported_protocols_roundtrip_credentials(self):
        result = v2board.export(entries([node(kind) for kind in ['vless', 'vmess', 'trojan', 'shadowsocks', 'hysteria', 'hysteria2']]))
        self.assertEqual(result['count'], 6)
        self.assertEqual(result['diagnostics'], [])
        self.assertEqual({line.split('://')[0] for line in result['body'].splitlines()}, {'vless', 'vmess', 'trojan', 'ss', 'hysteria', 'hysteria2'})
        decoded = protocols.parse(result['body'].encode(), 'Roundtrip')
        for entry in decoded:
            native = entry.outputs['hiddify']
            if entry.protocol in {'vless', 'vmess'}:
                self.assertEqual(native['uuid'], UUID)
            elif entry.protocol == 'hysteria':
                self.assertEqual(native['auth_str'], 'pass:a&b#é')
            else:
                self.assertEqual(native['password'], 'pass:a&b#é')

    def test_reality_and_websocket_fields_preserved(self):
        raw = node()
        raw.update(tls={'enabled': True, 'server_name': 'cover.example',
            'reality': {'enabled': True, 'public_key': base64.urlsafe_b64encode(bytes(range(32))).decode().rstrip('='), 'short_id': '01ab'},
            'utls': {'enabled': True, 'fingerprint': 'firefox'}},
            transport={'type': 'ws', 'path': '/ws?token=a%26b', 'headers': {'Host': 'cdn.example'}})
        result = v2board.export(entries([raw]))
        params = parse_qs(urlsplit(result['body'].strip()).query)
        self.assertEqual(params['security'], ['reality'])
        self.assertEqual(params['sni'], ['cover.example'])
        self.assertEqual(params['sid'], ['01ab'])
        self.assertEqual(params['fp'], ['firefox'])
        self.assertEqual(params['path'], ['/ws?token=a%26b'])
        self.assertEqual(params['host'], ['cdn.example'])

    def test_xhttp_settings_retained_for_uri_clients(self):
        raw = protocols.native_to_xray(node())
        raw['streamSettings'] = {'network': 'xhttp', 'security': 'none', 'xhttpSettings': {
            'path': '/api', 'mode': 'auto', 'extra': {'noSSEHeader': True}}}
        result = v2board.export(entries([raw]))
        self.assertEqual(result['count'], 1)
        params = parse_qs(urlsplit(result['body'].strip()).query)
        self.assertEqual(json.loads(params['extra'][0]), {'noSSEHeader': True})

    def test_custom_headers_and_dependencies_are_not_dropped(self):
        headers = node()
        headers['transport'] = {'type': 'ws', 'path': '/', 'headers': {'X-Required': 'secret-value'}}
        chained = node()
        chained['detour'] = 'upstream'
        result = v2board.export(entries([headers, chained]))
        self.assertEqual(result['count'], 0)
        self.assertEqual(len(result['diagnostics']), 2)
        self.assertNotIn('secret-value', json.dumps(result['diagnostics']))

    def test_original_uri_preserves_plugin_and_rejects_duplicate_parameters(self):
        auth = base64.urlsafe_b64encode(b'aes-128-gcm:synthetic-password').decode().rstrip('=')
        uri = f'ss://{auth}@[2001:db8::1]:443?plugin=v2ray-plugin%3Btls#Test'
        result = v2board.export(protocols.parse(uri.encode(), 'Source'))
        self.assertEqual(result['count'], 1)
        self.assertEqual(parse_qs(urlsplit(result['body'].strip()).query)['plugin'], ['v2ray-plugin;tls'])
        repeated = f'vless://{UUID}@proxy.example:443?type=tcp&type=tcp#Test'
        result = v2board.export(protocols.parse(repeated.encode(), 'Source'))
        self.assertEqual(result['count'], 0)
        self.assertIn('repeated', result['diagnostics'][0]['reason'])

    def test_custom_uri_endpoints_preserve_source_parameters_and_credentials(self):
        auth = base64.urlsafe_b64encode(b'aes-128-gcm:synthetic-password').decode().rstrip('=')
        vmess = {'v': '2', 'ps': 'Original', 'add': 'proxy.example', 'port': '443',
                 'id': UUID, 'aid': '0', 'net': 'ws', 'type': 'none', 'host': 'cdn.example',
                 'path': '/private?token=a&b', 'tls': 'tls', 'sni': 'cover.example', 'fp': 'firefox'}
        uris = [
            f'vless://{UUID}@proxy.example:443?type=ws&security=tls&sni=cover.example&path=%2Fprivate&host=cdn.example#Original',
            'trojan://pass%3Aa%26b%23%C3%A9@[2001:db8::1]:443?security=tls&sni=cover.example#Original',
            f'ss://{auth}@proxy.example:443?plugin=v2ray-plugin%3Btls#Original',
            'ss://' + base64.b64encode(b'aes-128-gcm:synthetic-password@proxy.example:443').decode() + '#Original',
            'vmess://' + base64.b64encode(json.dumps(vmess).encode()).decode(),
            'hy2://user:pass+word@proxy.example/?sni=cover.example&obfs=salamander&obfs-password=a%2Bb#Original',
            'hysteria2://proxy.example?pinSHA256=' + '12' * 32 + '#Original',
            'hysteria://proxy.example:443?auth=pass%3Aa%26b&upmbps=20&downmbps=100&peer=cover.example#Original',
        ]
        for uri in uris:
            source = protocols.parse(uri.encode(), 'Source')[0]
            original = copy.deepcopy(source)
            for edits in ({'name': 'Custom'}, {'address': 'custom.example'}, {'port': 8443},
                          {'name': 'Custom', 'address': '2001:db8::2', 'port': 8443}):
                with self.subTest(scheme=uri.split('://')[0], edits=edits):
                    entry = server_policy.customize(source, edits)
                    result = v2board.export([entry])
                    self.assertEqual(result['count'], 1, result['diagnostics'])
                    exported = result['body'].strip()
                    before = v2board.normalize_uri(uri, entry.name)
                    if uri.startswith('vmess://'):
                        expected = json.loads(protocols.b64(before[8:]))
                        actual = json.loads(protocols.b64(exported[8:]))
                        expected.update(add=edits.get('address', expected['add']),
                                        port=edits.get('port', expected['port']))
                        self.assertIn(entry.name, actual.pop('ps'))
                        expected.pop('ps')
                        self.assertEqual(actual, expected)
                    else:
                        expected, actual = urlsplit(before), urlsplit(exported)
                        self.assertEqual(actual.hostname, edits.get('address', expected.hostname))
                        self.assertEqual(actual.port, edits.get('port', expected.port))
                        self.assertEqual(actual.username, expected.username)
                        self.assertEqual(actual.password, expected.password)
                        self.assertEqual(parse_qs(actual.query), parse_qs(expected.query))
                        self.assertIn(entry.name, unquote(actual.fragment))
                    self.assertEqual(source, original)
                    self.assertEqual(entry.original, source.original)

    def test_custom_hysteria_native_and_uri_hopping_match_effective_endpoint(self):
        for kind in ('hysteria', 'hysteria2'):
            native = node(kind) | {'server_ports': ['443:445', '5000:5001']}
            native.pop('server_port')
            mihomo = protocols.native_to_mihomo(native)
            user = '' if kind == 'hysteria' else 'synthetic-password@'
            query = 'auth=synthetic-password&upmbps=20&downmbps=100&' if kind == 'hysteria' else ''
            sources = entries([native]) + protocols.parse(json.dumps({'proxies': [mihomo]}).encode(), 'Source')
            for authority, mport in [('443-445,5000-5001', ''), ('443', 'mport=443-445,5000-5001&')]:
                sources += protocols.parse(f'{kind}://{user}proxy.example:{authority}?{query}{mport}sni=cover.example'.encode(), 'Source')
            for source in sources:
                original = copy.deepcopy(source)
                before = hysteria.parse_uri(v2board.entry_link(source, 'Original'))
                for edits in ({'address': '2001:db8::2'}, {'port': 443}, {'port': 8443},
                              {'address': 'custom.example', 'port': 8443}):
                    for core in source.outputs:
                        with self.subTest(kind=kind, source=source.kind, edits=edits, core=core):
                            entry = server_policy.customize(source, edits)
                            # Publication can retain only one passing native variant.
                            entry.outputs = {core: entry.outputs[core]}
                            result = v2board.export([entry])
                            self.assertEqual(result['count'], 1, result['diagnostics'])
                            _, host, port, ranges, auth, params = hysteria.parse_uri(result['body'].strip())
                            self.assertEqual(host, edits.get('address', before[1]))
                            self.assertEqual(port, edits.get('port', before[2]))
                            self.assertEqual(auth, before[4])
                            expected_params = dict(before[5])
                            if 'port' in edits:
                                self.assertEqual(ranges, [str(edits['port'])])
                                expected_params.pop('mport', None)
                            else:
                                self.assertEqual(ranges, before[3])
                            self.assertEqual(params, expected_params)
                            self.assertEqual(source, original)

    def test_custom_xray_hysteria_keeps_tls_and_rejects_unrepresentable_settings(self):
        raw = protocols.native_to_xray(node('hysteria2'))
        for extra in ({}, {'quicParams': {'congestion': 'bbr'}}):
            value = copy.deepcopy(raw)
            if extra:
                value['streamSettings']['finalmask'] = extra
            source = entries([value])[0]
            entry = server_policy.customize(source, {'address': 'custom.example', 'port': 8443})
            result = v2board.export([entry])
            if extra:
                self.assertEqual(result['count'], 0)
                self.assertTrue(result['diagnostics'])
            else:
                self.assertEqual(result['count'], 1, result['diagnostics'])
                _, host, port, _, auth, params = hysteria.parse_uri(result['body'].strip())
                self.assertEqual((host, port), ('custom.example', 8443))
                self.assertEqual(auth, 'pass:a&b#é')
                self.assertEqual(params['sni'], 'cover.example')

    def test_distinct_accounts_keep_distinct_names_after_importer_truncation(self):
        first, second = node(), node()
        first['tag'] = second['tag'] = 'Long name ' * 30
        second['uuid'] = '11234567-89ab-4cde-8fab-0123456789ab'
        data = entries([first, second, copy.deepcopy(first)])
        result = v2board.export(data)
        self.assertEqual(result['count'], 2)
        labels = [urlsplit(line).fragment for line in result['body'].splitlines()]
        self.assertNotEqual(labels[0], labels[1])

    def test_feed_limits_exclude_entries_with_diagnostics(self):
        data = entries([node('vless'), node('vmess')])
        with patch.object(v2board, 'MAX_NODES', 1):
            result = v2board.export(data)
        self.assertEqual(result['count'], 1)
        self.assertEqual(len(result['diagnostics']), 1)
        with patch.object(v2board, 'MAX_BYTES', 10):
            self.assertEqual(v2board.export(data)['count'], 0)
        with patch.object(v2board, 'MAX_ENTRY_BYTES', 10):
            self.assertEqual(v2board.export(data)['count'], 0)

    def test_invalid_hostname_excluded_without_breaking_good_nodes(self):
        bad = node()
        bad['server'] = 'invalid_host.example'
        result = v2board.export(entries([node(), bad]))
        self.assertEqual(result['count'], 1)
        self.assertEqual(len(result['diagnostics']), 1)


if __name__ == '__main__':
    unittest.main()

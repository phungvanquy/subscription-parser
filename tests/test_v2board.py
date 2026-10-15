import base64
import copy
import json
from urllib.parse import parse_qs, urlsplit
import unittest
from unittest.mock import patch

from subscription_parser import protocols, v2board

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

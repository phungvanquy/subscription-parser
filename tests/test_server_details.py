import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import yaml
from fastapi.testclient import TestClient

from subscription_parser import common, connectivity, protocols, server_details
from subscription_parser.app import create_app
from subscription_parser.service import Manager


class ServerDetailTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        env = patch.dict(common.os.environ, {'PARSER_ADMIN_TOKEN': 'server-detail-test-admin'}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.nodes = [
            {'type': 'vless', 'tag': 'Berlin <private>', 'server': 'berlin.example', 'server_port': 443,
             'uuid': '01234567-89ab-4cde-8fab-0123456789ab',
             'tls': {'enabled': True, 'server_name': 'tls.example'},
             'transport': {'type': 'ws', 'path': '/private-path'}},
            {'type': 'shadowsocks', 'tag': 'Failed server', 'server': 'failed.example', 'server_port': 8443,
             'method': 'aes-128-gcm', 'password': 'synthetic-server-password'},
        ]
        self.fetches = 0

        def fetch(*args):
            self.fetches += 1
            return json.dumps({'outbounds': self.nodes}).encode(), {'fetched_at': common.now()}

        def checker(entries, root, **kwargs):
            return connectivity.check_entries(entries, root, **kwargs, tester=lambda node, *args:
                {'status': 'failed' if node.name == 'Failed server' else 'passed', 'latency_ms': 12})

        self.app = create_app(self.root, start_scheduler=False, fetcher=fetch, checker=checker)
        self.manager = self.app.state.manager
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.client.post('/api/login', json={'token': 'server-detail-test-admin'})
        self.provider = self.client.post('/api/providers', json={
            'name': 'Example provider', 'url': 'https://provider.example/private-provider-token', 'hwid_id': None,
        }).json()
        self.manager.refresh()
        self.inventory = self.client.get('/api/state').json()['inventory']
        self.identifier = self.inventory[0]['id']

    def test_authenticated_on_demand_settings_and_formats_do_not_fetch(self):
        before = copy.deepcopy(self.manager.snapshot)
        response = self.client.get('/api/servers/' + self.identifier)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers['cache-control'], 'no-store')
        detail = response.json()
        fields = {field['label']: field['value'] for field in detail['fields']}
        self.assertEqual(fields, {'Address': 'berlin.example', 'Port': '443', 'Transport': 'WS',
                                  'TLS': 'Enabled', 'Server name (SNI)': 'tls.example'})
        configs = {config['id']: config for config in detail['configurations']}
        self.assertEqual(json.loads(configs['original']['content']), self.nodes[0])
        self.assertEqual(yaml.safe_load(configs['mihomo']['content'])['proxies'][0]['server'], 'berlin.example')
        self.assertEqual(json.loads(configs['xray']['content'])['outbounds'][0]['protocol'], 'vless')
        self.assertEqual(json.loads(configs['hiddify']['content'])['outbounds'][0]['uuid'], self.nodes[0]['uuid'])
        self.assertNotIn('private-provider-token', response.text)
        self.assertNotIn('synthetic-server-password', response.text)
        self.assertNotIn(self.nodes[0]['uuid'], self.client.get('/api/state').text)
        self.assertEqual(self.fetches, 1)
        self.assertEqual(self.manager.snapshot, before)
        self.client.post('/api/logout')
        self.assertEqual(self.client.get('/api/servers/' + self.identifier).status_code, 401)

    def test_excluded_server_settings_remain_available(self):
        failed = next(server for server in self.inventory if server['name'] == 'Failed server')
        self.assertEqual(failed['clients'], [])
        response = self.client.get('/api/servers/' + failed['id'])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.json()['configurations'][0]['content']), self.nodes[1])

    def test_inspector_reads_published_selection_during_download_and_after_restart(self):
        original = copy.deepcopy(self.nodes[0])
        self.nodes[0]['server'] = 'new.example'
        provider = self.manager.store.config()['providers'][0]
        self.manager.fetch(provider, True)
        for manager in (self.manager, Manager(self.root)):
            with patch.object(manager, 'fetcher', side_effect=AssertionError('must not download')):
                detail = manager.server_detail(self.identifier)
                self.assertEqual(json.loads(detail['configurations'][0]['content']), original)

    def test_missing_and_removed_servers_return_not_found(self):
        self.assertEqual(self.client.get('/api/servers/not-a-server').status_code, 404)
        self.client.delete('/api/providers/' + self.provider['id'])
        self.assertEqual(self.client.get('/api/servers/' + self.identifier).status_code, 404)


class ServerConfigurationTests(unittest.TestCase):
    def test_original_uri_yaml_and_native_settings_are_preserved(self):
        fixtures = [
            ('uri', 'trojan://synthetic-password@proxy.example:443?sni=tls.example#Private'),
            ('mihomo', {'name': 'Proxy', 'type': 'ss', 'server': 'proxy.example', 'port': 443,
                        'cipher': 'aes-128-gcm', 'password': 'synthetic-password'}),
            ('xray', {'tag': 'Proxy', 'protocol': 'trojan', 'settings': {'servers': [
                {'address': 'proxy.example', 'port': 443, 'password': 'synthetic-password'}]},
                'streamSettings': {'network': 'tcp', 'security': 'tls', 'tlsSettings': {'serverName': 'tls.example'}}}),
        ]
        for kind, raw in fixtures:
            with self.subTest(kind=kind):
                entry = protocols.adapt(kind, raw, 'Provider', 'Proxy')
                original = server_details.configurations(entry)[0]
                decoded = (original['content'] if kind == 'uri' else yaml.safe_load(original['content'])
                           if kind == 'mihomo' else json.loads(original['content']))
                self.assertEqual(decoded, raw)
                fields = {field['label']: field['value'] for field in server_details.describe(entry)}
                self.assertEqual(fields['Address'], 'proxy.example')
                self.assertEqual(fields['Port'], '443')

    def test_client_settings_include_only_required_dependencies(self):
        nodes = [
            {'type': 'socks', 'tag': 'Chained', 'server': 'proxy.example', 'server_port': 1080, 'detour': 'upstream'},
            {'type': 'socks', 'tag': 'upstream', 'server': 'upstream.example', 'server_port': 1080},
            {'type': 'socks', 'tag': 'unrelated', 'server': 'unrelated.example', 'server_port': 1080},
        ]
        before = copy.deepcopy(nodes)
        entry = protocols.adapt('singbox', nodes[0], 'Provider', 'Chained', nodes)
        configs = {config['id']: config for config in server_details.configurations(entry)}
        output = json.loads(configs['hiddify']['content'])['outbounds']
        self.assertEqual(len(output), 2)
        self.assertEqual(output[0]['detour'], output[1]['tag'])
        self.assertNotIn('unrelated.example', configs['hiddify']['content'])
        self.assertEqual(nodes, before)

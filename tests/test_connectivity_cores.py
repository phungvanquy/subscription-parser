"""Actual client-core traffic through local proxy + HTTPS fixtures; no Internet."""
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from scripts.connectivity_fixture import fixture
from subscription_parser import connectivity as c, protocols, server_policy, v2board

CORES = Path(os.environ.get('PARSER_TOOLS_DIR', '/opt/client-cores'))


@unittest.skipUnless(all((CORES / ('hiddify-core' if core == 'hiddify' else core)).is_file() for core in c.CORES),
                     'Docker image with client cores required')
class RealCoreTests(unittest.TestCase):
    protocol = 'vless'

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / '.parser-state').mkdir()
        manager = fixture(self.root / 'fixture', CORES / 'xray', protocol=self.protocol)
        self.local = manager.__enter__()
        self.addCleanup(manager.__exit__, None, None, None)
        env = patch.dict(os.environ, {'PARSER_TOOLS_DIR': str(CORES),
                        'SSL_CERT_FILE': str(self.local['certificate']), 'HTTPS_PROXY': 'http://127.0.0.1:1',
                        'NO_PROXY': '*', 'http_proxy': 'http://127.0.0.1:1'})
        env.start(); self.addCleanup(env.stop)

    def node(self, **changes):
        node = {**self.local['node'], **changes}
        return protocols.parse(json.dumps({'outbounds': [node]}).encode(), 'Local')[0]

    def probe(self, node, core, **kwargs):
        return c.probe(node, core, self.root, threading.Event(), time.monotonic()+15, **kwargs)

    def test_all_real_cores_tunnel_verified_https_and_cleanup(self):
        for core in c.CORES:
            with self.subTest(core=core):
                value = self.probe(self.node(), core)
                self.assertEqual(value['status'], 'passed', value)
                self.assertGreater(value['latency_ms'], 0)
        self.assertEqual(self.local['server'].requests, len(c.CORES))
        self.assertFalse(list((self.root / '.parser-state').iterdir()))

    def test_dead_node_never_falls_back_to_direct(self):
        with socket.socket() as closed:
            closed.bind(('127.0.0.1', 0))
            for core in c.CORES:
                with self.subTest(core=core), patch.object(c, 'REQUEST_TIMEOUT', 1):
                    value = self.probe(self.node(server_port=closed.getsockname()[1]), core)
                    self.assertEqual(value['status'], 'failed', value)
        self.assertEqual(self.local['server'].requests, 0)

    def test_custom_address_and_port_reach_the_configured_endpoint(self):
        source = self.node()
        modified = server_policy.customize(source, {'address': 'localhost', 'name': 'Customized'})
        for core in c.CORES:
            with self.subTest(core=core):
                value = self.probe(modified, core)
                self.assertEqual(value['status'], 'passed', value)
        self.assertEqual(self.local['server'].requests, len(c.CORES))
        with socket.socket() as closed:
            closed.bind(('127.0.0.1', 0))
            unavailable = server_policy.customize(source, {'port': closed.getsockname()[1]})
            for core in c.CORES:
                with self.subTest(core=core), patch.object(c, 'REQUEST_TIMEOUT', 1):
                    self.assertEqual(self.probe(unavailable, core)['status'], 'failed')
        self.assertEqual(self.local['server'].requests, len(c.CORES))

    def test_wrong_credentials_fail_actual_handshake(self):
        credential = {'uuid': '11234567-89ab-4cde-8fab-0123456789ab'} if 'uuid' in self.local['node'] else {
            'password': 'wrong-proxy-password'}
        for core in c.CORES:
            with self.subTest(core=core), patch.object(c, 'REQUEST_TIMEOUT', 1):
                value = self.probe(self.node(**credential), core)
                self.assertEqual(value['status'], 'failed', value)
        self.assertEqual(self.local['server'].requests, 0)

    def test_custom_uri_export_tunnels_through_the_overridden_endpoint(self):
        with socket.socket() as closed:
            closed.bind(('127.0.0.1', 0))
            original = self.node(server_port=closed.getsockname()[1])
            source_uri = v2board.entry_link(original, 'Source')
            source = protocols.parse(source_uri.encode(), 'Local')[0]
            modified = server_policy.customize(source, {'address': 'localhost',
                'port': self.local['node']['server_port'], 'name': 'Customized'})
            exported = v2board.export([modified])
            self.assertEqual(exported['count'], 1, exported['diagnostics'])
            imported = protocols.parse(exported['body'].encode(), 'Client')[0]
            for core in c.CORES:
                with self.subTest(core=core):
                    value = self.probe(imported, core)
                    self.assertEqual(value['status'], 'passed', value)
        self.assertEqual(self.local['server'].requests, len(c.CORES))
        self.assertFalse(list((self.root / '.parser-state').iterdir()))

    def test_tls_errors_and_wrong_http_status_are_not_success(self):
        for core in c.CORES:
            with self.subTest(core=core), patch.dict(os.environ, {'SSL_CERT_FILE': '/dev/null', 'SSL_CERT_DIR': '/nonexistent'}):
                self.assertEqual(self.probe(self.node(), core)['status'], 'failed')
        self.assertEqual(self.local['server'].requests, 0)
        self.local['server'].response_status = 503
        for core in c.CORES:
            with self.subTest(core=core):
                self.assertEqual(self.probe(self.node(), core)['status'], 'failed')
        self.assertEqual(self.local['server'].requests, 2 * len(c.CORES))


class VMessCoreTests(RealCoreTests):
    protocol = 'vmess'


class ShadowsocksCoreTests(RealCoreTests):
    protocol = 'shadowsocks'


class TrojanCoreTests(RealCoreTests):
    protocol = 'trojan'

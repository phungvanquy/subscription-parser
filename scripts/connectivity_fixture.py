"""Local proxy server and HTTPS origin for deterministic client-core tests."""
from contextlib import contextmanager
import http.server
import json
import os
from pathlib import Path
import signal
import socket
import ssl
import subprocess
import sys
import threading
import time

UUID = '01234567-89ab-4cde-8fab-0123456789ab'
PASSWORD = 'synthetic-proxy-password'


@contextmanager
def fixture(folder, core, port=0, *, protocol='vless', xhttp=None, reality=False, salamander=None, tls=False):
    if tls and reality:
        raise ValueError('TLS and REALITY fixtures are mutually exclusive')
    if (xhttp is not None or reality) and protocol != 'vless':
        raise ValueError('XHTTP/REALITY fixtures require VLESS')
    if salamander is not None and protocol != 'hysteria2':
        raise ValueError('Salamander fixtures require Hysteria2')
    settings = {
        'vless': {'decryption': 'none', 'clients': [{'id': UUID}]},
        'vmess': {'clients': [{'id': UUID, 'alterId': 0}]},
        'shadowsocks': {'method': 'aes-128-gcm', 'password': PASSWORD, 'network': 'tcp'},
        'trojan': {'clients': [{'password': PASSWORD}]},
        # The pinned Xray core reads Hysteria inbound users from "clients".
        'hysteria2': {'version': 2, 'clients': [{'auth': PASSWORD}]},
    }[protocol]
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    certificate, key = folder / 'test-ca.pem', folder / 'test-key.pem'
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                    '-keyout', str(key), '-out', str(certificate), '-subj', '/CN=Connectivity test',
                    '-addext', 'subjectAltName=DNS:www.gstatic.com,DNS:cp.cloudflare.com,DNS:localhost,IP:127.0.0.1'],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
    certificate.chmod(0o600); key.chmod(0o600)
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.server.requests += 1
            self.send_response(self.server.response_status)
            self.send_header('Content-Length', '0')
            self.end_headers()
        def log_message(self, *args):
            pass
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.response_status, server.requests = 204, 0
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    if not port:
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
    inbound = {'listen': '127.0.0.1', 'port': port, 'protocol': protocol, 'settings': settings}
    node = {'type': protocol, 'tag': 'Synthetic', 'server': '127.0.0.1', 'server_port': port}
    if protocol in ('vless', 'vmess'):
        node['uuid'] = UUID
    else:
        node['password'] = PASSWORD
    if protocol == 'shadowsocks':
        node['method'] = 'aes-128-gcm'
    if protocol in {'trojan', 'hysteria2'} or tls:
        inbound['streamSettings'] = {'security': 'tls', 'tlsSettings': {
            'certificates': [{'certificateFile': str(certificate), 'keyFile': str(key)}]}}
        node['tls'] = {'enabled': True, 'server_name': 'localhost'}
    if protocol == 'hysteria2':
        inbound['protocol'] = 'hysteria'
        inbound['streamSettings'].update(network='hysteria', hysteriaSettings={'version': 2})
        inbound['streamSettings']['tlsSettings']['alpn'] = ['h3']
        node['tls']['alpn'] = ['h3']
        if salamander is not None:
            inbound['streamSettings']['finalmask'] = {
                'udp': [{'type': 'salamander', 'settings': {'password': salamander}}]}
            node['obfs'] = {'type': 'salamander', 'password': salamander}
    if xhttp is not None:
        inbound.setdefault('streamSettings', {}).update(network='xhttp', xhttpSettings=xhttp)
        node['transport'] = {'type': 'xhttp', **xhttp}
    if reality:
        keys = dict(line.split(': ', 1) for line in subprocess.check_output(
            [str(core), 'x25519'], text=True, timeout=5).splitlines() if ': ' in line)
        inbound.setdefault('streamSettings', {}).update(security='reality', realitySettings={
            'target': f'127.0.0.1:{server.server_port}', 'serverNames': ['localhost'],
            'privateKey': keys['PrivateKey'], 'shortIds': ['01ab']})
        node['tls'] = {'enabled': True, 'server_name': 'localhost',
                       'utls': {'enabled': True, 'fingerprint': 'firefox'},
                       'reality': {'enabled': True, 'public_key': keys['Password (PublicKey)'], 'short_id': '01ab'}}
    document = {'log': {'loglevel': 'none'}, 'inbounds': [inbound],
        'outbounds': [{'protocol': 'freedom', 'settings': {'redirect': f'127.0.0.1:{server.server_port}'}}]}
    readiness_port = port
    if protocol == 'hysteria2':
        # Hysteria2 listens on UDP. A separate loopback TCP inbound tells us
        # when the process has finished starting all its listeners.
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            readiness_port = listener.getsockname()[1]
        document['inbounds'].append({'listen': '127.0.0.1', 'port': readiness_port,
                                     'protocol': 'http', 'settings': {}})
    config = folder / 'test-server.json'
    config.write_text(json.dumps(document)); config.chmod(0o600)
    process = None
    try:
        process = subprocess.Popen([str(core), 'run', '-config', str(config)],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 5
        while True:
            if process.poll() is not None or time.monotonic() >= deadline:
                raise RuntimeError(f'local {protocol} fixture did not start')
            try:
                with socket.create_connection(('127.0.0.1', readiness_port), timeout=.1):
                    break
            except OSError:
                time.sleep(.05)
        yield {'server': server, 'port': port, 'certificate': certificate, 'node': node}
    finally:
        if process:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait(timeout=3)
        server.shutdown(); server.server_close(); thread.join(3)


if __name__ == '__main__':
    if sys.argv[1:] != ['--app']:
        raise SystemExit('test fixture requires --app')
    with fixture(Path('/data/test-support'), '/opt/client-cores/xray', port=20901) as local:
        env = dict(os.environ, SSL_CERT_FILE=str(local['certificate']))
        process = subprocess.Popen([sys.executable, '-m', 'subscription_parser.app', '--host', '0.0.0.0', '--port', '8099'], env=env)
        for signum in (signal.SIGTERM, signal.SIGINT):
            signal.signal(signum, lambda signum, frame: process.send_signal(signum))
        raise SystemExit(process.wait())

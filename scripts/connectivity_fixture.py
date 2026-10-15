"""Local VLESS server and HTTPS origin for deterministic client-core tests."""
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


@contextmanager
def fixture(folder, core, port=0):
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
    document = {'log': {'loglevel': 'none'}, 'inbounds': [{'listen': '127.0.0.1', 'port': port,
        'protocol': 'vless', 'settings': {'decryption': 'none', 'clients': [{'id': UUID}]}}],
        'outbounds': [{'protocol': 'freedom', 'settings': {'redirect': f'127.0.0.1:{server.server_port}'}}]}
    config = folder / 'test-server.json'
    config.write_text(json.dumps(document)); config.chmod(0o600)
    process = None
    try:
        process = subprocess.Popen([str(core), 'run', '-config', str(config)],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 5
        while True:
            if process.poll() is not None or time.monotonic() >= deadline:
                raise RuntimeError('local VLESS fixture did not start')
            try:
                with socket.create_connection(('127.0.0.1', port), timeout=.1):
                    break
            except OSError:
                time.sleep(.05)
        yield {'server': server, 'port': port, 'certificate': certificate,
               'node': {'type': 'vless', 'tag': 'Synthetic', 'server': '127.0.0.1', 'server_port': port, 'uuid': UUID}}
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

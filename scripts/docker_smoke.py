#!/usr/bin/env python3
"""Verify Docker deployment with an isolated volume and synthetic credentials."""
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import tarfile
import tempfile
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
UUID = '01234567-89ab-4cde-8fab-0123456789ab'


def main():
    project = 'subscription-parser-test-' + secrets.token_hex(4)
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
    environment = os.environ | {'PARSER_PORT': str(port), 'PARSER_BIND_ADDRESS': '127.0.0.1'}
    command = ['docker', 'compose', '-f', str(ROOT / 'compose.yaml'), '-p', project]
    temporary = tempfile.TemporaryDirectory(prefix='parser-docker-smoke-')
    override = Path(temporary.name) / 'fixture.yaml'
    override.write_text(json.dumps({'services': {'app': {'command': ['python', '/data/connection-fixture.py', '--app']}}}))
    command.extend(['-f', str(override)])

    def compose(*arguments, data=None):
        result = subprocess.run([*command, *arguments], input=data, capture_output=True, cwd=ROOT, env=environment, timeout=120)
        if result.returncode:
            raise RuntimeError(result.stderr.decode(errors='replace'))
        return result.stdout

    base = f'http://127.0.0.1:{port}'
    environment['PARSER_PUBLIC_URL'] = base
    admin = 'docker-smoke-admin-key'
    environment['PARSER_ADMIN_TOKEN'] = admin
    subscription = 'docker-smoke-subscription-key'
    checks = []
    session_cookie = ''

    def api(path, method='GET', body=None, proxy=False):
        nonlocal session_cookie
        headers = {'Cookie': session_cookie} if session_cookie else {}
        if body is not None:
            headers['Content-Type'] = 'application/json'
        if proxy:
            headers.update({'Host': 'docker-proxy.example', 'Origin': 'https://docker-proxy.example', 'X-Forwarded-Proto': 'https'})
        request = urllib.request.Request(base + path, method=method, headers=headers,
            data=None if body is None else json.dumps(body).encode())
        with urllib.request.urlopen(request, timeout=10) as response:
            if response.headers.get('Set-Cookie'):
                session_cookie = response.headers['Set-Cookie'].split(';', 1)[0]
            return json.load(response)

    def wait_for_refresh():
        deadline = time.monotonic() + 70
        while time.monotonic() < deadline:
            state = api('/api/state')
            if not state['busy'] and state['counts'].get('v2board') == 1:
                assert not state.get('last_error')
                return state
            time.sleep(0.5)
        raise RuntimeError('synthetic cached provider did not refresh')

    provider_url = 'https://provider.invalid/docker-smoke'
    body = json.dumps({'outbounds': [{'type': 'vless', 'tag': 'Synthetic', 'server': '127.0.0.1',
        'server_port': 20901, 'uuid': UUID}]}).encode()
    files = {
        '.env': f'DEVICE_ID=docker-authorized-id\nPARSER_ADMIN_TOKEN={admin}\nPARSER_SUBSCRIPTION_TOKEN={subscription}\nPARSER_PUBLIC_URL={base}\n',
        '.parser-state/providers.json': json.dumps({'refresh_seconds': 3600, 'providers': [{
            'id': 'synthetic', 'name': 'Docker test', 'url': provider_url, 'enabled': True,
            'device_id_env': 'DEVICE_ID', 'user_agent': 'subscription-parser-docker-smoke'}]}),
        '.parser-state/cache/synthetic.json': json.dumps({'fingerprint': hashlib.sha256(provider_url.encode()).hexdigest(),
            'metadata': {'fetched_at': '2026-01-01T00:00:00+00:00'}, 'body': base64.b64encode(body).decode()}),
    }
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode='w:gz') as output:
        for name, content in files.items():
            encoded = content.encode()
            member = tarfile.TarInfo(name)
            member.mode, member.size = 0o600, len(encoded)
            output.addfile(member, io.BytesIO(encoded))
    try:
        compose('run', '--rm', '-T', '--no-deps', 'app', 'python', 'scripts/import_state.py', '-', data=archive.getvalue())
        checks.append('streamed import preserves fixed HWID and existing application keys')
        compose('run', '--rm', '-T', '--no-deps', 'app', 'python', '-c',
                "import pathlib, sys; pathlib.Path('/data/connection-fixture.py').write_bytes(sys.stdin.buffer.read())",
                data=(ROOT / 'scripts/connectivity_fixture.py').read_bytes())
        compose('up', '-d', '--no-build', '--wait', '--wait-timeout', '60')
        assert api('/healthz') == {'status': 'ok'}
        assert api('/api/login', 'POST', {'token': admin})['authenticated']
        state = wait_for_refresh()
        assert state['providers'][0]['status'] == 'cached'
        assert all(state['validation'][target]['status'] == 'passed' for target in ('hiddify', 'mihomo', 'xray'))
        assert all(state['inventory'][0]['connectivity'][target]['status'] == 'passed' for target in ('mihomo', 'xray', 'hiddify'))
        for link in state['links'].values():
            with urllib.request.urlopen(link, timeout=10) as response:
                assert response.status == 200 and UUID.encode() in response.read()
        checks.append('healthy production container serves all six feeds after real Mihomo/Xray/Hiddify HTTPS checks through the local VLESS fixture')
        saved = api('/api/hwids', 'POST', {'name': 'Second device', 'value': 'docker-second-authorized-id'})
        api('/api/settings', 'PUT', {'refresh_seconds': 600}, proxy=True)
        checks.append('HWID writes and HTTPS reverse-proxy requests work with the writable data volume')
        old = state['links']['v2board.txt']
        api('/api/rotate-link', 'POST')
        state = api('/api/state')
        new = state['links']['v2board.txt']
        assert new != old
        scoped_id = api('/api/subscriptions', 'POST', {'name': 'Docker scoped', 'provider_ids': ['synthetic']})['id']
        scoped = next(item for item in api('/api/subscriptions') if item['id'] == scoped_id)
        assert scoped['counts']['v2board'] == 1 and scoped['links'] != state['links']
        scoped_link = scoped['links']['v2board.txt']
        setup = api('/api/security/2fa/setup', 'POST', {'token': admin})
        code = compose('exec', '-T', 'app', 'python', '-c',
                       'import pyotp, sys; print(pyotp.TOTP(sys.stdin.read()).now())', data=setup['secret'].encode()).decode().strip()
        recovery_codes = api('/api/security/2fa/confirm', 'POST', {'code': code})['recovery_codes']
        assert api('/api/security')['enabled']
        assert not api('/api/login', 'POST', {'token': admin})['authenticated']
        checks.append('authenticator enrollment requires a valid code and then enforces the second factor')
        log_entry = api('/api/logs')['entries'][0]
        before_restart = api('/api/state')
        compose('up', '-d', '--no-build', '--force-recreate', '--wait', '--wait-timeout', '60')
        state = wait_for_refresh()
        for field in ('next_check', 'last_download', 'last_check'):
            assert state[field] == before_restart[field], (field, 'changed on restart')
        checks.append('container replacement preserves the check schedule and does not trigger another batch')
        assert api('/api/security')['enabled']
        assert api('/api/security')['recovery_codes_remaining'] == 10
        assert state['links']['v2board.txt'] == new
        assert state['refresh_seconds'] == 600
        assert any(item['id'] == saved['id'] and item['name'] == 'Second device' and item['configured'] for item in state['hwids'])
        restored_scope = next(item for item in state['subscriptions'] if item['id'] == scoped_id)
        assert restored_scope['provider_ids'] == ['synthetic'] and restored_scope['links'] == scoped['links']
        with urllib.request.urlopen(scoped_link, timeout=10) as response:
            assert response.status == 200 and UUID.encode() in response.read()
        checks.append('named subscription selections, independent tokens and passing feeds survive container replacement')
        try:
            urllib.request.urlopen(old, timeout=10)
            raise AssertionError('old subscription token remained valid')
        except urllib.error.HTTPError as error:
            assert error.code == 404
        checks.append('container replacement preserves HWIDs, rotated subscription links, settings and cache')
        logs = api('/api/logs')
        assert logs['available'] and log_entry in logs['entries']
        assert logs['max_bytes'] == 4 * 1024 * 1024 and logs['retention_days'] == 7
        assert all(secret not in json.dumps(logs) for secret in (admin, subscription, provider_url, UUID, 'docker-authorized-id'))
        try:
            urllib.request.urlopen(base + '/api/logs', timeout=10)
            raise AssertionError('debug logs were accessible without admin authentication')
        except urllib.error.HTTPError as error:
            assert error.code == 401
        checks.append('private bounded debug history survives container replacement without exposing credentials')
        verification = '''
import os
from pathlib import Path
from subscription_parser.storage import Store
assert os.geteuid() == 10001
assert not Path('/app/.env').exists()
assert not Path('/app/.parser-state').exists()
assert os.environ['PARSER_ADMIN_TOKEN'] == 'docker-smoke-admin-key'
assert not Path('/data/.env').exists()
assert Path('/data/app.sqlite3').stat().st_mode & 0o777 == 0o600
assert Path('/data/logs').stat().st_mode & 0o777 == 0o700
assert all(path.stat().st_mode & 0o777 == 0o600 for path in Path('/data/logs').iterdir())
store = Store(Path('/data'))
with store.connection() as connection:
    assert connection.execute('SELECT count(*) FROM subscriptions').fetchone()[0] == 2
    assert connection.execute('PRAGMA user_version').fetchone()[0] == 7
identities = {item['name']: item['id'] for item in store.hwids()}
assert store.hwid_value(identities['DEVICE_ID']) == 'docker-authorized-id'
assert store.hwid_value(identities['Second device']) == 'docker-second-authorized-id'
assert store.config()['providers'][0]['hwid_id'] == identities['DEVICE_ID']
assert Path('/data/legacy-backup/environment.txt').stat().st_mode & 0o777 == 0o600
print('passed')
'''
        assert compose('exec', '-T', 'app', 'python', '-c', verification).strip() == b'passed'
        checks.append('non-root runtime, private SQLite storage, archived legacy data and credentials excluded from application image')
        previous_admin = admin
        admin = 'changed-docker-smoke-admin-key'
        environment['PARSER_ADMIN_TOKEN'] = admin
        compose('up', '-d', '--no-build', '--wait', '--wait-timeout', '60')
        try:
            api('/api/state')
            raise AssertionError('previous session remained valid after admin key change')
        except urllib.error.HTTPError as error:
            assert error.code == 401
        assert api('/api/login', 'POST', {'token': admin, 'code': recovery_codes[0]})['authenticated']
        assert api('/api/security')['enabled']
        assert api('/api/security')['recovery_codes_remaining'] == 9
        try:
            api('/api/login', 'POST', {'token': previous_admin})
            raise AssertionError('previous admin key remained valid after configuration change')
        except urllib.error.HTTPError as error:
            assert error.code == 401
        state = wait_for_refresh()
        assert state['links']['v2board.txt'] == new
        checks.append('admin key changes via deployment environment without changing subscription links')
        checks.append('two-factor enrollment, hashed recovery codes and sessions persist across container replacement; key rotation revokes sessions without removing 2FA')
        print(json.dumps({'passed': len(checks), 'checks': checks}, indent=2))
    finally:
        compose('down', '--volumes', '--remove-orphans', '--timeout', '20')
        temporary.cleanup()


if __name__ == '__main__':
    main()

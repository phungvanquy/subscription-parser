"""Security boundaries, replay resistance, persistence and enrollment recovery."""
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import hmac
import io
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
import pyotp

from subscription_parser import common
from subscription_parser.app import create_app
from subscription_parser.auth import Auth, AuthError, AUTH_TABLES, MAX_SESSIONS, SESSION_SECONDS
from subscription_parser.storage import Store
from subscription_parser.manage import main as manage


class AuthTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.key = 'synthetic-authentication-test-key'
        env = patch.dict(common.os.environ, {'PARSER_ADMIN_TOKEN': self.key}, clear=True)
        env.start(); self.addCleanup(env.stop)
        self.now = 1700000000.0
        self.app = create_app(self.root, start_scheduler=False)
        self.auth = self.app.state.auth
        self.auth.clock = lambda: self.now
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)

    def login(self, code='', client=None, token=None):
        return (client or self.client).post('/api/login', json={'token': token or self.key, 'code': code})

    def setup_factor(self):
        self.assertEqual(self.login().status_code, 200)
        response = self.client.post('/api/security/2fa/setup', json={'token': self.key})
        self.assertEqual(response.status_code, 200)
        return response.json()['secret']

    def enroll(self):
        secret = self.setup_factor()
        response = self.client.post('/api/security/2fa/confirm', json={'code': pyotp.TOTP(secret).at(self.now)})
        self.assertEqual(response.status_code, 200)
        return secret, response.json()['recovery_codes']

    def test_admin_key_is_not_an_api_bearer_token(self):
        for path in ('/api/state', '/api/security', '/api/logs', '/api/subscriptions'):
            self.assertEqual(self.client.get(path, headers={'Authorization': 'Bearer ' + self.key}).status_code, 401)

    def test_old_forgeable_timestamp_cookie_is_rejected(self):
        stamp = str(int(self.now))
        signature = hmac.new(self.key.encode(), stamp.encode(), hashlib.sha256).hexdigest()
        self.client.cookies.set('parser_session', stamp + '.' + signature)
        self.assertFalse(self.client.get('/api/session').json()['authenticated'])

    def test_sessions_are_random_hashed_expire_and_logout_revokes_copied_cookie(self):
        first = self.login()
        cookie = self.client.cookies.get('parser_session')
        self.assertIn('HttpOnly', first.headers['set-cookie'])
        self.assertIn('SameSite=strict', first.headers['set-cookie'])
        with self.auth.store.connection() as connection:
            self.assertNotIn(cookie, repr([tuple(r) for r in connection.execute('SELECT * FROM auth_sessions')]))
        self.assertTrue(self.auth.valid_session(cookie))
        self.client.post('/api/logout')
        self.assertFalse(self.auth.valid_session(cookie))
        self.login()
        second = self.client.cookies.get('parser_session')
        self.assertNotEqual(cookie, second)
        self.now += SESSION_SECONDS
        self.assertFalse(self.auth.valid_session(second))

    def test_sessions_survive_restart_and_admin_key_rotation_revokes_them(self):
        self.login(); cookie = self.client.cookies.get('parser_session')
        reopened = Auth(Store(self.root), self.key, clock=lambda: self.now)
        self.assertTrue(reopened.valid_session(cookie))
        self.assertFalse(Auth(Store(self.root), 'changed-key', clock=lambda: self.now).valid_session(cookie))

    def test_session_storage_is_bounded(self):
        cookies = [self.auth.login(self.key, '')[1] for _ in range(MAX_SESSIONS + 4)]
        with self.auth.store.connection() as connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM auth_sessions').fetchone()[0], MAX_SESSIONS)
        self.assertTrue(self.auth.valid_session(cookies[-1]))

    def test_enrollment_needs_session_key_and_verified_code(self):
        self.assertEqual(self.client.post('/api/security/2fa/setup', json={'token': self.key}).status_code, 401)
        self.login()
        self.assertEqual(self.client.post('/api/security/2fa/setup', json={'token': 'wrong'}).status_code, 403)
        setup = self.client.post('/api/security/2fa/setup', json={'token': self.key}).json()
        self.assertTrue(setup['qr'].startswith('data:image/svg+xml;base64,'))
        self.assertIn(b'<svg', base64.b64decode(setup['qr'].split(',', 1)[1]))
        self.assertFalse(self.client.get('/api/security').json()['enabled'])
        self.assertEqual(self.client.post('/api/security/2fa/confirm', json={'code': 'invalid'}).status_code, 403)
        self.assertFalse(self.client.get('/api/security').json()['enabled'])
        response = self.client.post('/api/security/2fa/confirm', json={'code': pyotp.TOTP(setup['secret']).at(self.now)})
        self.assertTrue(response.json()['enabled'])
        self.assertEqual(len(response.json()['recovery_codes']), 10)
        self.assertEqual(response.headers['cache-control'], 'no-store')

    def test_setup_is_bound_to_session_and_expires(self):
        secret = self.setup_factor()
        code = pyotp.TOTP(secret).at(self.now)
        other = TestClient(self.app)
        self.addCleanup(other.close)
        self.login(client=other)
        self.assertEqual(other.post('/api/security/2fa/confirm', json={'code': code}).status_code, 409)
        self.now += 601
        self.assertEqual(self.client.post('/api/security/2fa/confirm', json={'code': pyotp.TOTP(secret).at(self.now)}).status_code, 409)
        self.assertFalse(self.client.get('/api/security').json()['enabled'])

    def test_setup_restart_replaces_old_seed_and_logout_clears_it(self):
        first = self.setup_factor()
        second = self.client.post('/api/security/2fa/setup', json={'token': self.key}).json()['secret']
        self.assertNotEqual(first, second)
        self.assertEqual(self.client.post('/api/security/2fa/confirm', json={'code': pyotp.TOTP(first).at(self.now)}).status_code, 403)
        self.client.post('/api/logout')
        with self.auth.store.connection() as connection:
            self.assertIsNone(connection.execute('SELECT pending_secret FROM auth_settings').fetchone()[0])

    def test_enabling_revokes_other_and_legacy_sessions(self):
        other = TestClient(self.app); self.addCleanup(other.close)
        self.login(client=other)
        _, codes = self.enroll()
        self.assertFalse(other.get('/api/session').json()['authenticated'])
        self.assertTrue(self.client.get('/api/session').json()['authenticated'])
        self.assertEqual(other.get('/api/state', headers={'Authorization': 'Bearer ' + self.key}).status_code, 401)
        self.assertEqual(self.client.post('/api/security/2fa/setup', json={'token': self.key}).status_code, 409)

    def test_first_factor_does_not_issue_session_and_second_factor_is_required(self):
        secret, _ = self.enroll()
        self.client.post('/api/logout')
        response = self.login()
        self.assertEqual(response.json(), {'authenticated': False, 'requires_two_factor': True})
        self.assertNotIn('set-cookie', response.headers)
        self.assertEqual(self.client.get('/api/state').status_code, 401)
        rejected = self.login('wrong')
        self.assertEqual(rejected.status_code, 401)
        self.assertIn('verification code is incorrect', rejected.json()['detail'])
        self.assertNotIn('admin key', rejected.json()['detail'])
        self.assertNotIn('set-cookie', rejected.headers)
        self.assertEqual(self.client.get('/api/state').status_code, 401)
        self.now += 30
        self.assertTrue(self.login(pyotp.TOTP(secret).at(self.now)).json()['authenticated'])

    def test_codes_cannot_be_replayed_even_after_restart(self):
        secret, _ = self.enroll()
        self.client.post('/api/logout')
        used = pyotp.TOTP(secret).at(self.now)
        self.assertEqual(self.login(used).status_code, 401)
        self.now += 30
        code = pyotp.TOTP(secret).at(self.now)
        self.assertEqual(self.login(code).status_code, 200)
        reopened = Auth(Store(self.root), self.key, clock=lambda: self.now)
        with self.assertRaises(AuthError):
            reopened.login(self.key, code)

    def test_concurrent_code_use_can_succeed_only_once(self):
        secret, _ = self.enroll()
        self.now += 30
        code = pyotp.TOTP(secret).at(self.now)
        def login(_):
            try:
                return self.auth.login(self.key, code)[0]['authenticated']
            except AuthError:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sum(pool.map(login, range(2))), 1)

    def test_recovery_codes_are_hashed_one_use_and_require_admin_key(self):
        secret, codes = self.enroll()
        serialized = self.root.joinpath('app.sqlite3').read_bytes()
        for code in codes:
            self.assertNotIn(code.encode(), serialized)
            self.assertNotIn(code.replace('-', '').encode(), serialized)
        self.client.post('/api/logout')
        self.assertEqual(self.login(codes[0], token='wrong-key').status_code, 401)
        response = self.login(codes[0].upper())
        self.assertTrue(response.json()['recovery_used'])
        self.assertEqual(self.client.get('/api/security').json()['recovery_codes_remaining'], 9)
        self.client.post('/api/logout')
        self.assertEqual(self.login(codes[0]).status_code, 401)

    def test_recovery_login_revokes_other_sessions(self):
        _, codes = self.enroll()
        cookie = self.client.cookies.get('parser_session')
        other = TestClient(self.app); self.addCleanup(other.close)
        self.assertTrue(self.login(codes[0], client=other).json()['authenticated'])
        self.assertFalse(self.auth.valid_session(cookie))

    def test_replace_recovery_codes_needs_both_factors_invalidates_old_codes_and_sessions(self):
        _, codes = self.enroll()
        cookie = self.client.cookies.get('parser_session')
        self.assertEqual(self.client.post('/api/security/2fa/recovery', json={'token': self.key}).status_code, 403)
        response = self.client.post('/api/security/2fa/recovery', json={'token': self.key, 'code': codes[0]})
        new_codes = response.json()['recovery_codes']
        self.assertTrue(set(codes).isdisjoint(new_codes))
        self.assertFalse(self.auth.valid_session(cookie))
        self.client.post('/api/logout')
        self.assertEqual(self.login(codes[1]).status_code, 401)
        self.assertEqual(self.login(new_codes[0]).status_code, 200)

    def test_disabling_needs_both_factors_and_revokes_sessions(self):
        _, codes = self.enroll()
        cookie = self.client.cookies.get('parser_session')
        self.assertEqual(self.client.post('/api/security/2fa/disable', json={'token': self.key}).status_code, 403)
        self.assertTrue(self.client.get('/api/security').json()['enabled'])
        result = self.client.post('/api/security/2fa/disable', json={'token': self.key, 'code': codes[0]})
        self.assertFalse(result.json()['enabled'])
        self.assertFalse(self.auth.valid_session(cookie))
        self.assertEqual(self.client.get('/api/security').json()['recovery_codes_remaining'], 0)
        self.client.post('/api/logout')
        self.assertTrue(self.login().json()['authenticated'])

    def test_mfa_survives_admin_key_change(self):
        secret, codes = self.enroll()
        reopened = Auth(Store(self.root), 'new-admin-key', clock=lambda: self.now)
        self.assertTrue(reopened.status()['enabled'])
        self.assertFalse(reopened.login('new-admin-key', '')[0]['authenticated'])
        self.assertTrue(reopened.login('new-admin-key', codes[0])[0]['authenticated'])

    def test_no_seed_or_recovery_codes_leak_in_state_logs_or_status(self):
        secret, codes = self.enroll()
        for path in ('/api/state', '/api/security', '/api/session', '/api/logs'):
            response = self.client.get(path)
            for value in [secret, self.key, *codes]:
                self.assertNotIn(value, response.text)
        self.assertIn('auth_enabled', self.client.get('/api/logs').text)

    def test_per_client_rate_limit_has_retry_after_and_persists_across_restart(self):
        for _ in range(10):
            self.assertEqual(self.login(token='bad').status_code, 401)
        response = self.login()
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers['retry-after'], '300')
        self.assertEqual(response.headers['cache-control'], 'no-store')
        reopened = Auth(Store(self.root), self.key, clock=lambda: self.now)
        with self.assertRaises(AuthError):
            reopened.limit_request('testclient')
        self.now += 300
        self.assertEqual(self.login().status_code, 200)

    def test_malformed_requests_and_all_security_actions_share_rate_limit(self):
        for _ in range(10):
            self.assertEqual(self.client.post('/api/login', content='invalid-json').status_code, 422)
        for path in ('/api/login/', '/api/security/2fa/setup', '/api/security/2fa/confirm',
                     '/api/security/2fa/disable', '/api/security/2fa/recovery'):
            self.assertEqual(self.client.post(path, json={}).status_code, 429)

    def test_forwarded_header_spoofing_cannot_reset_limit(self):
        for index in range(10):
            self.assertEqual(self.client.post('/api/login', json={'token': 'bad'},
                headers={'X-Forwarded-For': f'203.0.113.{index}', 'X-Real-IP': f'203.0.113.{index}'}).status_code, 401)
        self.assertEqual(self.login().status_code, 429)

    def test_ipv6_rotation_within_same_network_shares_limit(self):
        for index in range(10):
            self.auth.limit_request(f'2001:db8::{index}')
        with self.assertRaises(AuthError):
            self.auth.limit_request('2001:db8::ff')

    def test_global_limit_bounds_database_and_blocks_rotating_clients(self):
        for index in range(100):
            self.auth.limit_request(f'198.51.100.{index}')
        for index in range(100, 160):
            with self.assertRaises(AuthError):
                self.auth.limit_request(f'198.51.100.{index}')
        with self.auth.store.connection() as connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM auth_limits').fetchone()[0], 101)
        self.now += 300
        self.auth.limit_request('203.0.113.1')
        with self.auth.store.connection() as connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM auth_limits').fetchone()[0], 2)

    def test_account_factor_limit_survives_client_changes_and_cannot_be_extended(self):
        secret, _ = self.enroll()
        self.now += 300
        for _ in range(10):
            with self.assertRaises(AuthError) as raised:
                self.auth.login(self.key, 'invalid')
            self.assertEqual(raised.exception.status, 401)
        with self.assertRaises(AuthError) as raised:
            self.auth.login(self.key, pyotp.TOTP(secret).at(self.now))
        self.assertEqual(raised.exception.status, 429)
        self.now += 200
        with self.assertRaises(AuthError) as raised:
            self.auth.login(self.key, 'invalid')
        self.assertEqual(raised.exception.retry_after, 100)
        self.now += 100
        self.assertTrue(self.auth.login(self.key, pyotp.TOTP(secret).at(self.now))[0]['authenticated'])

    def test_invalid_keys_cannot_lock_account_factor_bucket(self):
        self.enroll()
        self.now += 300
        for _ in range(15):
            with self.assertRaises(AuthError):
                self.auth.login('bad-key', 'invalid')
        with self.auth.store.connection() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM auth_limits WHERE bucket = 'factor' AND expires > ?",
                                                (self.now,)).fetchone()[0], 0)

    def test_rate_limit_does_not_block_feeds_health_or_logged_in_admin(self):
        self.login()
        for _ in range(10):
            self.login(token='bad')
        self.assertEqual(self.login().status_code, 429)
        self.assertEqual(self.client.get('/healthz').status_code, 200)
        self.assertEqual(self.client.get('/api/state').status_code, 200)
        self.assertEqual(self.client.get('/sub/missing/v2board.txt').status_code, 404)

    def test_csrf_and_secure_cookie_policy(self):
        self.assertEqual(self.client.post('/api/login', json={'token': self.key},
                                         headers={'Origin': 'https://evil.example'}).status_code, 403)
        with patch.dict(common.os.environ, {'PARSER_PUBLIC_URL': 'https://subscriptions.example'}):
            app = create_app(self.root, start_scheduler=False)
        with TestClient(app, base_url='https://subscriptions.example') as client:
            response = self.login(client=client)
            self.assertIn('Secure', response.headers['set-cookie'])
            self.assertEqual(client.post('/api/security/2fa/setup', json={'token': self.key},
                                         headers={'Origin': 'https://evil.example'}).status_code, 403)

    def test_local_reset_requires_confirmation_and_stopped_app(self):
        self.enroll()
        with patch('sys.argv', ['manage', 'reset-two-factor', '--data-dir', str(self.root)]), patch('sys.stderr', new_callable=io.StringIO):
            with self.assertRaises(SystemExit):
                manage()
        with common.process_lock(self.auth.store.directory / 'app.lock'):
            with patch('sys.argv', ['manage', 'reset-two-factor', '--confirm', '--data-dir', str(self.root)]), patch('sys.stderr', new_callable=io.StringIO):
                with self.assertRaises(SystemExit):
                    manage()
        self.assertTrue(self.auth.status()['enabled'])
        with patch('sys.argv', ['manage', 'reset-two-factor', '--confirm', '--data-dir', str(self.root)]), patch('sys.stdout', new_callable=io.StringIO):
            manage()
        self.assertFalse(self.auth.status()['enabled'])
        self.assertFalse(self.client.get('/api/session').json()['authenticated'])
        self.assertEqual(self.client.post('/api/security/2fa/reset').status_code, 404)


class AuthMigrationTests(unittest.TestCase):
    def test_version_five_upgrade_is_atomic_and_preserves_all_existing_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = Store(Path(temporary))
            store.add_hwid('Existing', 'existing-authorized-device')
            store.save_subscription({'name': 'Other group', 'all_providers': True})
            before = (store.config(), store.hwids(), store.subscriptions())
            with store.connection(write=True) as connection:
                for table in AUTH_TABLES:
                    connection.execute('DROP TABLE ' + table)
                connection.execute('PRAGMA user_version = 5')
            connect = sqlite3.connect
            class FailingConnection(sqlite3.Connection):
                def execute(self, sql, *args):
                    if sql == 'PRAGMA user_version = 6':
                        raise sqlite3.OperationalError('simulated disk failure')
                    return super().execute(sql, *args)
            with patch('subscription_parser.storage.sqlite3.connect', side_effect=lambda *a, **kw: connect(*a, **kw, factory=FailingConnection)):
                with self.assertRaises(common.SubscriptionError):
                    Store(Path(temporary))
            with store.connection() as connection:
                self.assertEqual(connection.execute('PRAGMA user_version').fetchone()[0], 5)
                self.assertIsNone(connection.execute("SELECT 1 FROM sqlite_master WHERE name = 'auth_settings'").fetchone())
            reopened = Store(Path(temporary))
            self.assertEqual((reopened.config(), reopened.hwids(), reopened.subscriptions()), before)
            self.assertFalse(Auth(reopened, 'test-key').status()['enabled'])
            self.assertEqual(reopened.database_path.stat().st_mode & 0o777, 0o600)

    def test_rfc6238_sha1_vectors_as_six_digit_codes_and_tolerance(self):
        secret = base64.b32encode(b'12345678901234567890').decode()
        for timestamp, code in ((59, '287082'), (1111111109, '081804'), (1111111111, '050471'),
                                (1234567890, '005924'), (2000000000, '279037'), (20000000000, '353130')):
            self.assertEqual(Auth._totp_step(secret, code, timestamp), timestamp // 30)
            self.assertIsNone(Auth._totp_step(secret, code, timestamp, last_step=timestamp // 30))
        self.assertEqual(Auth._totp_step(secret, '287082', 89), 1)
        self.assertIsNone(Auth._totp_step(secret, '287082', 90))
        self.assertIsNone(Auth._totp_step(secret, '２８７０８２', 59))

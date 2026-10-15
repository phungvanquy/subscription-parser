"""Single-admin authentication; secrets and bounded counters stay in SQLite."""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import math
import re
import secrets
import time

import pyotp
import qrcode
from qrcode.image.svg import SvgPathImage

SESSION_SECONDS = 8 * 3600
SETUP_SECONDS = 600
RATE_SECONDS = 300
CLIENT_ATTEMPTS = 10
GLOBAL_ATTEMPTS = 100
FACTOR_ATTEMPTS = 10
MAX_SESSIONS = 20
AUTH_TABLES = ('auth_settings', 'auth_sessions', 'auth_recovery', 'auth_limits')
AUTH_SCHEMA = """
CREATE TABLE auth_settings (
    id INTEGER PRIMARY KEY CHECK(id = 1),
    totp_secret TEXT,
    last_step INTEGER NOT NULL DEFAULT -1,
    pending_secret TEXT,
    pending_session TEXT,
    pending_expires REAL
);
INSERT INTO auth_settings (id) VALUES (1);
CREATE TABLE auth_sessions (token_hash TEXT PRIMARY KEY, expires REAL NOT NULL);
CREATE TABLE auth_recovery (code_hash TEXT PRIMARY KEY);
CREATE TABLE auth_limits (bucket TEXT PRIMARY KEY, count INTEGER NOT NULL, expires REAL NOT NULL);
"""


class AuthError(Exception):
    def __init__(self, status, detail, retry_after=None):
        super().__init__(detail)
        self.status, self.detail, self.retry_after = status, detail, retry_after


class Auth:
    def __init__(self, store, admin_key, clock=time.time):
        self.store, self.key, self.clock = store, admin_key.encode(), clock

    def _session_hash(self, value):
        # Changing the deployment key invalidates all sessions, but retains 2FA.
        return hmac.new(self.key, ('session:' + value).encode(), hashlib.sha256).hexdigest()

    def _key_valid(self, value):
        return hmac.compare_digest(value.encode(), self.key)

    def _valid_session(self, connection, value):
        if not value or len(value) > 128:
            return False
        return connection.execute('SELECT 1 FROM auth_sessions WHERE token_hash = ? AND expires > ?',
                                  (self._session_hash(value), self.clock())).fetchone() is not None

    def valid_session(self, value):
        with self.store.connection() as connection:
            return self._valid_session(connection, value)

    def _require_session(self, connection, value):
        if not self._valid_session(connection, value):
            raise AuthError(401, 'sign in to manage subscriptions')

    def _new_session(self, connection):
        connection.execute('DELETE FROM auth_sessions WHERE expires <= ?', (self.clock(),))
        connection.execute('DELETE FROM auth_sessions WHERE token_hash IN '
                           '(SELECT token_hash FROM auth_sessions ORDER BY expires DESC LIMIT -1 OFFSET ?)',
                           (MAX_SESSIONS - 1,))
        value = secrets.token_urlsafe(32)
        connection.execute('INSERT INTO auth_sessions VALUES (?, ?)',
                           (self._session_hash(value), self.clock() + SESSION_SECONDS))
        return value

    def logout(self, value):
        with self.store.connection(write=True) as connection:
            connection.execute('DELETE FROM auth_sessions WHERE token_hash = ?', (self._session_hash(value),))
            connection.execute('UPDATE auth_settings SET pending_secret = NULL, pending_session = NULL, '
                               'pending_expires = NULL WHERE pending_session = ?', (self._session_hash(value),))

    def status(self):
        with self.store.connection() as connection:
            enabled = bool(connection.execute('SELECT totp_secret FROM auth_settings').fetchone()[0])
            remaining = connection.execute('SELECT count(*) FROM auth_recovery').fetchone()[0]
        return {'enabled': enabled, 'recovery_codes_remaining': remaining, 'session_hours': SESSION_SECONDS // 3600,
                'login_attempts': CLIENT_ATTEMPTS, 'login_window_seconds': RATE_SECONDS}

    def _limit(self, buckets):
        now = self.clock()
        blocked_until = 0
        with self.store.connection(write=True) as connection:
            connection.execute('DELETE FROM auth_limits WHERE expires <= ?', (now,))
            # Check every bucket before incrementing any. Blocked traffic cannot
            # extend a cooldown or grow the table with arbitrary client addresses.
            for name, maximum in buckets:
                row = connection.execute('SELECT count, expires FROM auth_limits WHERE bucket = ?', (name,)).fetchone()
                if row and row['count'] >= maximum:
                    blocked_until = max(blocked_until, row['expires'])
            if not blocked_until:
                for name, _ in buckets:
                    connection.execute('INSERT INTO auth_limits VALUES (?, 1, ?) ON CONFLICT(bucket) '
                                       'DO UPDATE SET count = count + 1', (name, now + RATE_SECONDS))
        if blocked_until:
            wait = max(1, math.ceil(blocked_until - now))
            raise AuthError(429, f'too many authentication attempts; try again in {wait} seconds', wait)

    def limit_request(self, client):
        # request.client is normalized by Uvicorn's trusted-proxy middleware.
        # Never accept a raw X-Forwarded-For / X-Real-IP header here.
        try:
            address = ipaddress.ip_address(client)
            client = str(ipaddress.ip_network(f'{address}/64', strict=False)) if address.version == 6 else str(address)
        except ValueError:
            pass
        identity = hashlib.sha256(client.encode()).hexdigest()
        self._limit([('global', GLOBAL_ATTEMPTS), ('client:' + identity, CLIENT_ATTEMPTS)])

    def _limit_factor(self):
        # Account-wide OTP limit: rotating addresses cannot reset this counter.
        # Applied only after the correct admin key (or an authenticated setup).
        self._limit([('factor', FACTOR_ATTEMPTS)])

    @staticmethod
    def _totp_step(secret, code, now, last_step=-1):
        if not re.fullmatch(r'[0-9]{6}', code):
            return None
        current = int(now // 30)
        totp = pyotp.TOTP(secret)
        for step in (current, current - 1, current + 1):
            if step > last_step and step >= 0 and hmac.compare_digest(totp.at(step * 30), code):
                return step
        return None

    @staticmethod
    def _recovery_hash(code):
        return hashlib.sha256(code.replace('-', '').replace(' ', '').lower().encode()).hexdigest()

    def _consume_factor(self, connection, settings, code):
        step = self._totp_step(settings['totp_secret'], code, self.clock(), settings['last_step'])
        if step is not None:
            connection.execute('UPDATE auth_settings SET last_step = ? WHERE id = 1', (step,))
            return False
        if connection.execute('DELETE FROM auth_recovery WHERE code_hash = ?', (self._recovery_hash(code),)).rowcount:
            return True
        raise AuthError(403, 'incorrect or already used verification code; wait for a new code or use a recovery code')

    def login(self, token, code):
        if not self._key_valid(token):
            raise AuthError(401, 'incorrect admin key or verification code')
        if code:
            self._limit_factor()
        with self.store.connection(write=True) as connection:
            settings = connection.execute('SELECT * FROM auth_settings').fetchone()
            recovered = False
            if settings['totp_secret']:
                if not code:
                    return {'authenticated': False, 'requires_two_factor': True}, None
                try:
                    recovered = self._consume_factor(connection, settings, code.strip())
                except AuthError:
                    raise AuthError(401, 'incorrect admin key or verification code; wait for a new code or use a recovery code') from None
                if recovered:
                    connection.execute('DELETE FROM auth_sessions')
            return {'authenticated': True, 'recovery_used': recovered}, self._new_session(connection)

    def begin_setup(self, session, token, account):
        if not self._key_valid(token):
            raise AuthError(403, 'incorrect admin key')
        secret = pyotp.random_base32()
        uri = pyotp.TOTP(secret).provisioning_uri(name=account, issuer_name='subscription_parser')
        svg = qrcode.make(uri, image_factory=SvgPathImage).to_string()
        with self.store.connection(write=True) as connection:
            self._require_session(connection, session)
            if connection.execute('SELECT totp_secret FROM auth_settings').fetchone()[0]:
                raise AuthError(409, 'two-factor authentication is already enabled')
            connection.execute('UPDATE auth_settings SET pending_secret = ?, pending_session = ?, pending_expires = ? '
                               'WHERE id = 1', (secret, self._session_hash(session), self.clock() + SETUP_SECONDS))
        return {'secret': secret, 'qr': 'data:image/svg+xml;base64,' + base64.b64encode(svg).decode(),
                'expires_in': SETUP_SECONDS}

    def _new_recovery_codes(self, connection):
        # Ten independent 128-bit codes, shown once; only hashes are retained.
        codes = []
        for _ in range(10):
            value = secrets.token_hex(16)
            codes.append('-'.join(value[i:i+8] for i in range(0, 32, 8)))
        connection.execute('DELETE FROM auth_recovery')
        connection.executemany('INSERT INTO auth_recovery VALUES (?)', [(self._recovery_hash(code),) for code in codes])
        return codes

    def confirm_setup(self, session, code):
        self._limit_factor()
        with self.store.connection(write=True) as connection:
            self._require_session(connection, session)
            settings = connection.execute('SELECT * FROM auth_settings').fetchone()
            if (settings['totp_secret'] or not settings['pending_secret'] or
                    settings['pending_session'] != self._session_hash(session) or
                    settings['pending_expires'] <= self.clock()):
                raise AuthError(409, 'setup expired or changed; start two-factor setup again')
            step = self._totp_step(settings['pending_secret'], code.strip(), self.clock())
            if step is None:
                raise AuthError(403, 'incorrect verification code; check your authenticator and device time')
            connection.execute('UPDATE auth_settings SET totp_secret = pending_secret, last_step = ?, '
                               'pending_secret = NULL, pending_session = NULL, pending_expires = NULL WHERE id = 1', (step,))
            codes = self._new_recovery_codes(connection)
            connection.execute('DELETE FROM auth_sessions')
            return {'enabled': True, 'recovery_codes': codes}, self._new_session(connection)

    def change_factor(self, session, token, code, *, disable=False):
        if not self._key_valid(token):
            raise AuthError(403, 'incorrect admin key or verification code')
        self._limit_factor()
        with self.store.connection(write=True) as connection:
            self._require_session(connection, session)
            settings = connection.execute('SELECT * FROM auth_settings').fetchone()
            if not settings['totp_secret']:
                raise AuthError(409, 'two-factor authentication is not enabled')
            self._consume_factor(connection, settings, code.strip())
            if disable:
                self._clear_factor(connection)
                result = {'enabled': False}
            else:
                result = {'enabled': True, 'recovery_codes': self._new_recovery_codes(connection)}
            connection.execute('DELETE FROM auth_sessions')
            return result, self._new_session(connection)

    @staticmethod
    def _clear_factor(connection):
        connection.execute('UPDATE auth_settings SET totp_secret = NULL, last_step = -1, pending_secret = NULL, '
                           'pending_session = NULL, pending_expires = NULL WHERE id = 1')
        connection.execute('DELETE FROM auth_recovery')

    def reset_factor(self):
        """Local, explicit disaster recovery only; deliberately no HTTP endpoint."""
        with self.store.connection(write=True) as connection:
            self._clear_factor(connection)
            connection.execute('DELETE FROM auth_sessions')
            connection.execute('DELETE FROM auth_limits')

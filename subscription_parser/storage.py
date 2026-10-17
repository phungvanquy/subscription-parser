"""Transactional application data, separate from deployment configuration."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import tempfile
from urllib.parse import urlsplit

from . import common
from .config import MAX_PROVIDER_SERVERS, admin_key
from .auth import AUTH_SCHEMA

SCHEMA_VERSION = 7
CREDENTIALS_SCHEMA = """CREATE TABLE credentials (
    name TEXT PRIMARY KEY CHECK(name = 'subscription_token'),
    value TEXT NOT NULL CHECK(length(value) > 0)
);"""
PROVIDERS_SCHEMA = """CREATE TABLE providers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL COLLATE NOCASE UNIQUE,
    url TEXT NOT NULL,
    hwid_id TEXT REFERENCES hwids(id) ON DELETE RESTRICT,
    user_agent TEXT NOT NULL,
    enabled INTEGER NOT NULL CHECK(enabled IN (0, 1)),
    position INTEGER NOT NULL,
    server_limit INTEGER CHECK(server_limit IS NULL OR server_limit BETWEEN 1 AND 100000)
);"""
SUBSCRIPTIONS_SCHEMA = """CREATE TABLE subscriptions (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL COLLATE NOCASE UNIQUE,
    token TEXT NOT NULL UNIQUE CHECK(length(token) > 0),
    all_providers INTEGER NOT NULL CHECK(all_providers IN (0, 1))
);"""
MEMBERSHIPS_SCHEMA = """CREATE TABLE subscription_providers (
    subscription_id TEXT NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
    provider_id TEXT NOT NULL REFERENCES providers(id) ON DELETE CASCADE,
    PRIMARY KEY (subscription_id, provider_id)
);"""
SCHEMA = """
CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
""" + SUBSCRIPTIONS_SCHEMA + """
CREATE TABLE hwids (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL COLLATE NOCASE UNIQUE,
    value TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1 CHECK(revision > 0)
);
""" + PROVIDERS_SCHEMA + MEMBERSHIPS_SCHEMA + """
CREATE TABLE settings (
    id INTEGER PRIMARY KEY CHECK(id = 1),
    refresh_seconds INTEGER NOT NULL CHECK(refresh_seconds BETWEEN 300 AND 86400),
    check_seconds INTEGER NOT NULL DEFAULT 600 CHECK(check_seconds BETWEEN 300 AND 86400)
);
""" + AUTH_SCHEMA
PROVIDER_INSERT = ('INSERT INTO providers '
                   '(id, name, url, hwid_id, user_agent, enabled, position, server_limit) '
                   'VALUES (?, ?, ?, ?, ?, ?, ?, ?)')


class Store:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.directory = self.root / '.parser-state'
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.database_path = self.root / 'app.sqlite3'
        if self.database_path.is_symlink():
            raise common.SubscriptionError('application database must not be a symlink')
        if not self.database_path.exists():
            with common.process_lock(self.directory / 'app.lock'):
                if not self.database_path.exists():
                    self._initialize()
                self._archive_legacy()
        else:
            self._check_schema()
            if self._pending_legacy():
                with common.process_lock(self.directory / 'app.lock'):
                    self._archive_legacy()

    @contextmanager
    def connection(self, *, write=False):
        # Each operation owns its connection, including fetches on worker threads.
        connection = sqlite3.connect(self.database_path.as_uri() + '?mode=rw', uri=True,
                                     timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys = ON')
        try:
            connection.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _check_schema(self):
        try:
            with self.connection() as connection:
                version = connection.execute('PRAGMA user_version').fetchone()[0]
            if version == 1:
                self._migrate_admin_key()
            if version in {1, 2}:
                self._migrate_devices()
            if version in {1, 2, 3}:
                self._migrate_connectivity()
            if version in {1, 2, 3, 4}:
                self._migrate_subscriptions()
            if version in {1, 2, 3, 4, 5}:
                self._migrate_auth()
            if version in {1, 2, 3, 4, 5, 6}:
                self._migrate_server_limits()
            with self.connection() as connection:
                if connection.execute('PRAGMA user_version').fetchone()[0] != SCHEMA_VERSION:
                    raise common.SubscriptionError('unsupported application database version')
                if not connection.execute("SELECT 1 FROM subscriptions WHERE id = 'default'").fetchone():
                    raise common.SubscriptionError('application credentials are missing; restore a backup')
                if connection.execute('SELECT count(*) FROM settings').fetchone()[0] != 1:
                    raise common.SubscriptionError('application settings are missing; restore a backup')
                if connection.execute('SELECT count(*) FROM auth_settings').fetchone()[0] != 1:
                    raise common.SubscriptionError('authentication settings are missing; restore a backup')
        except sqlite3.DatabaseError:
            raise common.SubscriptionError('application database could not be read; restore a backup') from None

    def _migrate_admin_key(self):
        # Do not remove the old login key until deployment configuration exists.
        admin_key()
        with common.process_lock(self.directory / 'app.lock'):
            with self.connection(write=True) as connection:
                if connection.execute('PRAGMA user_version').fetchone()[0] != 1:
                    return
                keys = {row[0] for row in connection.execute('SELECT name FROM credentials')}
                if keys != {'admin_token', 'subscription_token'}:
                    raise common.SubscriptionError('application credentials are missing; restore a backup')
                connection.execute('PRAGMA secure_delete = ON')
                connection.execute('ALTER TABLE credentials RENAME TO old_credentials')
                connection.execute(CREDENTIALS_SCHEMA)
                connection.execute("INSERT INTO credentials SELECT name, value FROM old_credentials WHERE name = 'subscription_token'")
                connection.execute('DROP TABLE old_credentials')
                connection.execute('PRAGMA user_version = 2')

    def _migrate_devices(self):
        with common.process_lock(self.directory / 'app.lock'):
            with self.connection(write=True) as connection:
                if connection.execute('PRAGMA user_version').fetchone()[0] != 2:
                    return
                connection.execute('ALTER TABLE hwids ADD COLUMN revision INTEGER NOT NULL DEFAULT 1 CHECK(revision > 0)')
                connection.execute('ALTER TABLE providers RENAME TO old_providers')
                connection.execute(PROVIDERS_SCHEMA)
                connection.execute('INSERT INTO providers (id, name, url, hwid_id, user_agent, enabled, position) '
                                   'SELECT id, name, url, hwid_id, user_agent, enabled, position FROM old_providers')
                connection.execute('DROP TABLE old_providers')
                connection.execute('PRAGMA user_version = 3')

    def _migrate_connectivity(self):
        with common.process_lock(self.directory / 'app.lock'):
            with self.connection(write=True) as connection:
                if connection.execute('PRAGMA user_version').fetchone()[0] != 3:
                    return
                connection.execute('ALTER TABLE settings ADD COLUMN check_seconds INTEGER NOT NULL '
                                   'DEFAULT 600 CHECK(check_seconds BETWEEN 300 AND 86400)')
                connection.execute('PRAGMA user_version = 4')

    def _migrate_subscriptions(self):
        with common.process_lock(self.directory / 'app.lock'):
            with self.connection(write=True) as connection:
                if connection.execute('PRAGMA user_version').fetchone()[0] != 4:
                    return
                keys = dict(connection.execute('SELECT name, value FROM credentials'))
                if set(keys) != {'subscription_token'} or not keys['subscription_token']:
                    raise common.SubscriptionError('application credentials are missing; restore a backup')
                connection.execute(SUBSCRIPTIONS_SCHEMA)
                connection.execute(MEMBERSHIPS_SCHEMA)
                connection.execute('INSERT INTO subscriptions VALUES (?, ?, ?, 1)',
                                   ('default', 'All providers', keys['subscription_token']))
                connection.execute('DROP TABLE credentials')
                connection.execute('PRAGMA user_version = 5')

    def _legacy_inputs(self):
        env_path = self.root / '.env'
        config_path = self.directory / 'providers.json'
        for path in (env_path, config_path):
            if path.is_symlink():
                raise common.SubscriptionError('legacy application data must not be a symlink')
        env = common.load_environment(env_path, inherit_process=False)
        hwids = {name: value for name, value in env.items()
                 if name == 'DEVICE_ID' or name.endswith('_DEVICE_ID')}
        for value in hwids.values():
            if value:
                common.validate_device_id(value)
        try:
            config = json.loads(common.read_limited(config_path)) if config_path.exists() else {
                'refresh_seconds': 3600, 'providers': []}
            if not isinstance(config, dict) or not isinstance(config['providers'], list):
                raise ValueError
            interval = config['refresh_seconds']
            if type(interval) is not int or not 300 <= interval <= 86400:
                raise ValueError
            for provider in config['providers']:
                if provider['device_id_env'] not in hwids:
                    raise common.SubscriptionError('legacy provider references a missing HWID; migration refused')
                if not isinstance(provider['id'], str) or not provider['id']:
                    raise ValueError
                self._provider_fields(provider)
                if type(provider['enabled']) is not bool:
                    raise ValueError
        except (ValueError, TypeError, KeyError, AttributeError):
            raise common.SubscriptionError('invalid legacy provider data; migration refused') from None
        # Lost credentials must never silently become new identities or links.
        if config['providers'] or (self.directory / 'snapshot.json').exists() or (self.root / 'legacy-backup').exists():
            if not env.get('PARSER_SUBSCRIPTION_TOKEN'):
                raise common.SubscriptionError('legacy credentials are missing; migration refused')
        sources = {}
        if hwids or 'PARSER_SUBSCRIPTION_TOKEN' in env:
            sources['.env'] = hashlib.sha256(env_path.read_bytes()).hexdigest()
        if config_path.exists():
            sources['.parser-state/providers.json'] = hashlib.sha256(config_path.read_bytes()).hexdigest()
        return env, hwids, config, sources

    def _migrate_auth(self):
        with common.process_lock(self.directory / 'app.lock'):
            with self.connection(write=True) as connection:
                if connection.execute('PRAGMA user_version').fetchone()[0] != 5:
                    return
                # executescript commits implicitly: execute individual statements
                # so a failed migration rolls back both tables and schema version.
                for statement in AUTH_SCHEMA.split(';'):
                    if statement.strip():
                        connection.execute(statement)
                connection.execute('PRAGMA user_version = 6')

    def _migrate_server_limits(self):
        with common.process_lock(self.directory / 'app.lock'):
            with self.connection(write=True) as connection:
                if connection.execute('PRAGMA user_version').fetchone()[0] != 6:
                    return
                # Earlier device migrations create the current provider table.
                columns = {row['name'] for row in connection.execute('PRAGMA table_info(providers)')}
                if 'server_limit' not in columns:
                    connection.execute('ALTER TABLE providers ADD COLUMN server_limit INTEGER '
                                       'CHECK(server_limit IS NULL OR server_limit BETWEEN 1 AND 100000)')
                connection.execute('PRAGMA user_version = 7')

    def _initialize(self):
        env, hwids, config, sources = self._legacy_inputs()
        # Publish a complete database with one atomic rename. Failed migrations
        # leave the originals in place and never publish partially imported data.
        with tempfile.TemporaryDirectory(prefix='.database-', dir=self.root) as temporary:
            path = Path(temporary) / 'app.sqlite3'
            connection = sqlite3.connect(path)
            try:
                connection.execute('PRAGMA foreign_keys = ON')
                connection.executescript(SCHEMA)
                connection.execute('BEGIN IMMEDIATE')
                identities = {}
                for name, value in hwids.items():
                    identifier = secrets.token_hex(8)
                    identities[name] = identifier
                    connection.execute('INSERT INTO hwids (id, name, value) VALUES (?, ?, ?)', (identifier, name, value))
                connection.execute('INSERT INTO subscriptions VALUES (?, ?, ?, 1)',
                                   ('default', 'All providers', env.get('PARSER_SUBSCRIPTION_TOKEN') or secrets.token_urlsafe(32)))
                connection.execute('INSERT INTO settings (id, refresh_seconds) VALUES (1, ?)', (config['refresh_seconds'],))
                for position, provider in enumerate(config['providers']):
                    connection.execute(PROVIDER_INSERT, (provider['id'], provider['name'], provider['url'],
                        identities[provider['device_id_env']], provider['user_agent'], int(provider['enabled']), position, None))
                for name, digest in sources.items():
                    connection.execute('INSERT INTO metadata VALUES (?, ?)', ('legacy:' + name, digest))
                connection.execute(f'PRAGMA user_version = {SCHEMA_VERSION}')
                connection.commit()
            except sqlite3.IntegrityError:
                raise common.SubscriptionError('legacy data conflicts; migration refused') from None
            finally:
                connection.close()
            path.chmod(0o600)
            with path.open('rb') as database:
                os.fsync(database.fileno())
            os.replace(path, self.database_path)
            descriptor = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    def _pending_legacy(self):
        with self.connection() as connection:
            return any((self.root / row['key'][7:]).exists()
                       for row in connection.execute("SELECT key FROM metadata WHERE key LIKE 'legacy:%'"))

    def _archive_legacy(self):
        with self.connection() as connection:
            sources = list(connection.execute("SELECT key, value FROM metadata WHERE key LIKE 'legacy:%'"))
        moves = []
        for row in sources:
            relative = row['key'][7:]
            if relative not in {'.env', '.parser-state/providers.json'}:
                raise common.SubscriptionError('invalid migration metadata')
            source = self.root / relative
            if not source.exists():
                continue
            target = self.root / 'legacy-backup' / ('environment.txt' if relative == '.env' else 'providers.json')
            if source.is_symlink() or target.is_symlink() or target.parent.is_symlink():
                raise common.SubscriptionError('legacy backup must not use symlinks')
            for path in (source, target):
                if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() != row['value']:
                    raise common.SubscriptionError('legacy data changed after migration; review it before continuing')
            moves.append((source, target))
        for source, target in moves:
            target.parent.mkdir(mode=0o700, exist_ok=True)
            if target.exists():
                source.unlink()
            else:
                os.replace(source, target)
            target.chmod(0o600)

    def credential(self, name):
        if name != 'subscription_token':
            raise ValueError('unknown application credential')
        with self.connection() as connection:
            row = connection.execute("SELECT token FROM subscriptions WHERE id = 'default'").fetchone()
        if not row or not row['token']:
            raise common.SubscriptionError('application credential is missing; restore a backup')
        return row['token']

    def rotate_subscription_token(self, identifier='default'):
        with self.connection(write=True) as connection:
            if not connection.execute('UPDATE subscriptions SET token = ? WHERE id = ?',
                                      (secrets.token_urlsafe(32), identifier)).rowcount:
                raise common.SubscriptionError('subscription does not exist')

    @staticmethod
    def _subscription(connection, row):
        return dict(row) | {'all_providers': bool(row['all_providers']), 'provider_ids': [p[0] for p in
            connection.execute('SELECT provider_id FROM subscription_providers WHERE subscription_id = ? ORDER BY provider_id',
                               (row['id'],))]}

    def subscriptions(self):
        with self.connection() as connection:
            return [self._subscription(connection, row) for row in connection.execute(
                "SELECT * FROM subscriptions ORDER BY id != 'default', name COLLATE NOCASE")]

    def subscription_for_token(self, token):
        if not token or len(token) > 256:
            return None
        with self.connection() as connection:
            row = connection.execute('SELECT * FROM subscriptions WHERE token = ?', (token,)).fetchone()
            return self._subscription(connection, row) if row else None

    def save_subscription(self, values, identifier=None):
        name = values['name'].strip()
        if not name or len(name) > 80 or any(ord(char) < 32 for char in name):
            raise common.SubscriptionError('subscription name must contain 1–80 characters without control characters')
        selected = values.get('provider_ids', [])
        all_providers = values.get('all_providers', False)
        if len(selected) != len(set(selected)):
            raise common.SubscriptionError('choose each provider only once')
        if (all_providers and selected) or (not all_providers and not selected):
            raise common.SubscriptionError('choose one or more providers, or all enabled providers')
        with self.connection(write=True) as connection:
            if identifier and not connection.execute('SELECT 1 FROM subscriptions WHERE id = ?', (identifier,)).fetchone():
                raise common.SubscriptionError('subscription does not exist')
            if any(row['name'].casefold() == name.casefold() for row in connection.execute(
                    'SELECT name FROM subscriptions WHERE id != ?', (identifier or '',))):
                raise common.SubscriptionError('subscription name must be unique')
            known = {row[0] for row in connection.execute('SELECT id FROM providers')}
            if not set(selected) <= known:
                raise common.SubscriptionError('a selected provider no longer exists; review the selection')
            if identifier:
                connection.execute('UPDATE subscriptions SET name = ?, all_providers = ? WHERE id = ?',
                                   (name, int(all_providers), identifier))
                connection.execute('DELETE FROM subscription_providers WHERE subscription_id = ?', (identifier,))
            else:
                identifier = secrets.token_hex(8)
                connection.execute('INSERT INTO subscriptions VALUES (?, ?, ?, ?)',
                                   (identifier, name, secrets.token_urlsafe(32), int(all_providers)))
            connection.executemany('INSERT INTO subscription_providers VALUES (?, ?)',
                                   [(identifier, provider) for provider in selected])
        return identifier

    def delete_subscription(self, identifier):
        if identifier == 'default':
            raise common.SubscriptionError('the original subscription cannot be deleted; edit its providers or rotate its links')
        with self.connection(write=True) as connection:
            if not connection.execute('DELETE FROM subscriptions WHERE id = ?', (identifier,)).rowcount:
                raise common.SubscriptionError('subscription does not exist')

    def hwids(self):
        with self.connection() as connection:
            providers = list(connection.execute('SELECT id, name, enabled, hwid_id FROM providers ORDER BY position'))
            return [{'id': row['id'], 'name': row['name'], 'configured': bool(row['value']),
                     'masked_value': '••••' + row['value'][-4:] if row['value'] else 'Missing',
                     'revision': row['revision'],
                     'providers': [{'id': p['id'], 'name': p['name'], 'enabled': bool(p['enabled'])}
                                   for p in providers if p['hwid_id'] == row['id']]}
                    for row in connection.execute('SELECT * FROM hwids ORDER BY name')]

    def hwid_revision(self, identifier):
        with self.connection() as connection:
            row = connection.execute('SELECT revision FROM hwids WHERE id = ?', (identifier,)).fetchone()
        return row['revision'] if row else None

    def hwid_value(self, identifier):
        with self.connection() as connection:
            row = connection.execute('SELECT value FROM hwids WHERE id = ?', (identifier,)).fetchone()
        if not row or not row['value']:
            raise common.SubscriptionError('saved HWID is missing; select an authorized device identity')
        return common.validate_device_id(row['value'])

    @staticmethod
    def _hwid_name(name):
        name = name.strip()
        if not name or len(name) > 100 or any(ord(char) < 32 for char in name):
            raise common.SubscriptionError('use an HWID label containing 1–100 characters')
        return name

    def add_hwid(self, name, value):
        name = self._hwid_name(name)
        common.validate_device_id(value)
        identifier = secrets.token_hex(8)
        with self.connection(write=True) as connection:
            if any(row['name'].casefold() == name.casefold() for row in connection.execute('SELECT name FROM hwids')):
                raise common.SubscriptionError('this HWID label already exists; choose another label')
            if connection.execute('SELECT 1 FROM hwids WHERE value = ?', (value,)).fetchone():
                raise common.SubscriptionError('this device ID is already saved; select the existing device')
            connection.execute('INSERT INTO hwids (id, name, value) VALUES (?, ?, ?)', (identifier, name, value))
        return {'id': identifier, 'name': name, 'configured': True}

    def rename_hwid(self, identifier, name):
        name = self._hwid_name(name)
        with self.connection(write=True) as connection:
            if not connection.execute('SELECT 1 FROM hwids WHERE id = ?', (identifier,)).fetchone():
                raise common.SubscriptionError('device does not exist')
            if any(row['name'].casefold() == name.casefold() for row in connection.execute(
                    'SELECT name FROM hwids WHERE id != ?', (identifier,))):
                raise common.SubscriptionError('this HWID label already exists; choose another label')
            connection.execute('UPDATE hwids SET name = ? WHERE id = ?', (name, identifier))

    def delete_hwid(self, identifier):
        with self.connection(write=True) as connection:
            if connection.execute('SELECT 1 FROM providers WHERE hwid_id = ?', (identifier,)).fetchone():
                raise common.SubscriptionError('this device is assigned to subscriptions; reassign them before deleting it')
            if not connection.execute('DELETE FROM hwids WHERE id = ?', (identifier,)).rowcount:
                raise common.SubscriptionError('device does not exist')

    def replace_hwid(self, identifier, value, revision, provider_ids):
        common.validate_device_id(value)
        with self.connection(write=True) as connection:
            row = connection.execute('SELECT value, revision FROM hwids WHERE id = ?', (identifier,)).fetchone()
            if not row:
                raise common.SubscriptionError('device does not exist')
            affected = {p[0] for p in connection.execute('SELECT id FROM providers WHERE hwid_id = ?', (identifier,))}
            if row['revision'] != revision or affected != set(provider_ids):
                raise common.SubscriptionError('device or assignments changed; review the affected subscriptions again')
            if row['value'] == value:
                raise common.SubscriptionError('the replacement is the same device ID')
            if connection.execute('SELECT 1 FROM hwids WHERE value = ? AND id != ?', (value, identifier)).fetchone():
                raise common.SubscriptionError('this device ID is already saved; reassign subscriptions to that device')
            connection.execute('UPDATE hwids SET value = ?, revision = revision + 1 WHERE id = ?', (value, identifier))

    def config(self):
        with self.connection() as connection:
            providers = [dict(row) for row in connection.execute(
                'SELECT id, name, url, hwid_id, user_agent, enabled, server_limit FROM providers ORDER BY position')]
            for provider in providers:
                provider['enabled'] = bool(provider['enabled'])
            settings = dict(connection.execute('SELECT * FROM settings WHERE id = 1').fetchone())
            return {'refresh_seconds': settings['refresh_seconds'], 'check_seconds': settings.get('check_seconds', 600),
                    'providers': providers}

    def save_config(self, config):
        try:
            with self.connection(write=True) as connection:
                connection.execute('UPDATE settings SET refresh_seconds = ? WHERE id = 1', (config['refresh_seconds'],))
                if 'check_seconds' in config:
                    connection.execute('UPDATE settings SET check_seconds = ? WHERE id = 1', (config['check_seconds'],))
                # Retain rows for existing providers: deleting/reinserting them
                # would cascade-delete every subscription's provider selection.
                keep = {provider['id'] for provider in config['providers']}
                for row in connection.execute('SELECT id FROM providers').fetchall():
                    if row['id'] not in keep:
                        connection.execute('DELETE FROM providers WHERE id = ?', (row['id'],))
                for position, provider in enumerate(config['providers']):
                    connection.execute(PROVIDER_INSERT + ' ON CONFLICT(id) DO UPDATE SET name=excluded.name, '
                        'url=excluded.url, hwid_id=excluded.hwid_id, user_agent=excluded.user_agent, '
                        'enabled=excluded.enabled, position=excluded.position, server_limit=excluded.server_limit',
                        (provider['id'], provider['name'], provider['url'], provider['hwid_id'], provider['user_agent'],
                         int(provider['enabled']), position, provider.get('server_limit')))
        except sqlite3.IntegrityError:
            raise common.SubscriptionError('provider settings conflict or reference a missing HWID') from None

    @staticmethod
    def _provider_fields(values):
        name = values.get('name', '').strip()
        if not name or len(name) > 80:
            raise common.SubscriptionError('provider name must contain 1–80 characters')
        url = values.get('url', '')
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except ValueError:
            raise common.SubscriptionError('invalid subscription URL') from None
        if len(url) > 4096 or parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
            raise common.SubscriptionError('use an HTTPS subscription URL without embedded login credentials')
        if port is not None and not 1 <= port <= 65535:
            raise common.SubscriptionError('invalid subscription port')
        agent = values.get('user_agent', 'Happ/2.3.1').strip()
        if not agent or len(agent) > 200 or any(c in agent for c in '\r\n'):
            raise common.SubscriptionError('invalid client user agent')
        return name, url, agent

    def provider(self, values, previous=None):
        supplied = values
        values = (previous or {}) | values | {'url': values.get('url') or (previous or {}).get('url', '')}
        name, url, agent = self._provider_fields(values)
        if 'hwid_id' not in supplied and previous is None:
            raise common.SubscriptionError('choose a saved device or explicitly select no device ID')
        identifier = values.get('hwid_id')
        if identifier is not None:
            self.hwid_value(identifier)
        limit = values.get('server_limit')
        if limit is not None and (type(limit) is not int or not 1 <= limit <= MAX_PROVIDER_SERVERS):
            raise common.SubscriptionError(f'server limit must be an integer between 1 and {MAX_PROVIDER_SERVERS}, or null for unlimited')
        return {'id': previous['id'] if previous else secrets.token_hex(6), 'name': name, 'url': url,
                'hwid_id': identifier, 'user_agent': agent, 'enabled': bool(values.get('enabled', True)),
                'server_limit': limit}

    def public_provider(self, provider):
        parsed = urlsplit(provider['url'])
        return {k: v for k, v in provider.items() if k != 'url'} | {'host': parsed.hostname,
                'url_display': f'{parsed.scheme}://{parsed.netloc}/••••'}

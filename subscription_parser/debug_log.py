"""Small, private debug history with fixed byte and age limits; no raw payloads."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import threading
import time
import traceback

MAX_BYTES = 1024 * 1024
BACKUPS = 3
RETENTION_SECONDS = 7 * 86400
READ_LIMIT = 200

# Messages and string fields are deliberately allowlisted. Upstream responses,
# exception messages, URLs, names and core stdout/stderr must never be logged.
EVENTS = {
    'started': ('info', 'Application started'),
    'stopped': ('info', 'Application stopped'),
    'run_started': ('info', 'Subscription job started'),
    'run_completed': ('info', 'Subscription job completed'),
    'run_failed': ('error', 'Subscription job failed; previous snapshot retained'),
    'provider_failed': ('warning', 'Provider fetch or conversion failed'),
    'cache_used': ('warning', 'Using the saved provider response'),
    'cache_failed': ('warning', 'Saved provider response could not be used'),
    'provider_tested': ('info', 'Provider fetch test passed'),
    'provider_test_failed': ('warning', 'Provider fetch test failed'),
    'validation_failed': ('warning', 'Client validation rejected configurations'),
    'validator_missing': ('warning', 'Client validator is not installed'),
    'node_failed': ('warning', 'Server connection check failed'),
    'node_error': ('error', 'Server connection checker encountered an error'),
    'node_recovered': ('info', 'Server connection check recovered'),
    'checks_completed': ('info', 'Server connection checks completed'),
    'request_failed': ('error', 'Application request failed'),
    'scheduler_failed': ('error', 'Background scheduler encountered an error'),
}
CHOICES = {
    'operation': {'refresh', 'connection check', 'configuration update', 'provider test'},
    'stage': {'fetch', 'parse', 'validate', 'check', 'publish', 'schedule'},
    'core': {'mihomo', 'xray', 'hiddify'},
}
NUMBERS = {'servers', 'published', 'cached', 'seconds', 'passed', 'failed', 'errors', 'rejected'}
REASONS = {
    'provider rejected the device ID header',
    'device limit reached; reuse an authorized HWID or free a device slot',
    'subscription download failed (network, timeout or TLS error)',
    'subscription exceeds the 12 MiB limit',
    'provider response has no client-accepted servers',
    'no compatible servers were returned; check the subscription and device ID',
    'client validator unavailable or timed out',
    'combined subscription validation failed',
    'Client core is not installed', 'Check interrupted; retry scheduled',
    'Client core could not start this configuration', 'Client core startup timed out',
    'Client core stopped during the check', 'Connection checker could not run this configuration',
    'Connection checker failed; retry scheduled',
    'HTTPS request failed or returned an unexpected response on both endpoints',
}


def safe_reason(value):
    if isinstance(value, str) and (value in REASONS or re.fullmatch(r'provider returned HTTP [1-5][0-9]{2}', value)):
        return value
    return None


class DebugLog:
    def __init__(self, root, *, max_bytes=MAX_BYTES, backups=BACKUPS,
                 retention=RETENTION_SECONDS, clock=time.time):
        self.directory = Path(root) / 'logs'
        self.max_bytes, self.backups, self.retention = max_bytes, backups, retention
        self.clock = clock
        self.lock = threading.RLock()
        self.next_cleanup = 0
        self.available = True

    def paths(self):
        return [self.directory / ('debug.jsonl' + (f'.{i}' if i else '')) for i in range(self.backups + 1)]

    def _records(self, path):
        if not path.exists():
            return []
        with path.open('rb') as stream:
            # Bound reads even if a file was modified outside the application.
            stream.seek(max(0, path.stat().st_size - self.max_bytes))
            data = stream.read(self.max_bytes)
        records = []
        cutoff = self.clock() - self.retention
        for line in data.splitlines():
            try:
                record = json.loads(line)
                if (isinstance(record, dict) and isinstance(record.get('timestamp'), (int, float))
                        and cutoff <= record['timestamp'] <= self.clock()
                        and isinstance(record.get('event'), str) and record['event'] in EVENTS
                        and record.get('level') == EVENTS[record['event']][0]
                        and record.get('message') == EVENTS[record['event']][1]
                        and isinstance(record.get('context'), dict)):
                    records.append(record)
            except (ValueError, UnicodeError):
                continue
        return records

    @staticmethod
    def _encode(record):
        return (json.dumps(record, ensure_ascii=True, separators=(',', ':')) + '\n').encode()

    def maintain(self):
        """Clean on startup and hourly, including while there are no new events."""
        with self.lock:
            if self.clock() < self.next_cleanup:
                return
            try:
                self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
                self.directory.chmod(0o700)
                for path in self.paths():
                    if not path.exists():
                        continue
                    records = self._records(path)
                    if not records:
                        path.unlink()
                        continue
                    data = b''.join(self._encode(record) for record in records)
                    # In-place cleanup keeps even temporary disk use bounded.
                    # A crash can lose debug history, never application state.
                    with path.open('wb') as stream:
                        os.fchmod(stream.fileno(), 0o600)
                        stream.write(data[-self.max_bytes:])
                self.available = True
                self.next_cleanup = self.clock() + 3600
            except OSError:
                self.available = False
                self.next_cleanup = self.clock() + 60

    def write(self, event, *, error=None, **details):
        with self.lock:
            self.maintain()
            if event not in EVENTS:
                return
            level, message = EVENTS[event]
            stamp = self.clock()
            record = {'timestamp': stamp, 'time': datetime.fromtimestamp(stamp, timezone.utc).isoformat(),
                      'level': level, 'event': event, 'message': message}
            context = {}
            for key, value in details.items():
                if key in CHOICES and isinstance(value, str) and value in CHOICES[key]:
                    context[key] = value
                elif key in NUMBERS and type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1e9:
                    context[key] = value
                elif key in {'provider_id', 'node_id'} and isinstance(value, str) and re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', value):
                    context[key] = value
                elif key == 'reason' and safe_reason(value):
                    context[key] = value
            if error is not None:
                context['error_type'] = type(error).__name__[:80]
                reason = safe_reason(str(error))
                if reason:
                    context['reason'] = reason
                # No exception text, source lines, locals or full filesystem paths.
                context['trace'] = [f'{Path(frame.f_code.co_filename).name}:{line} ({frame.f_code.co_name})'
                                    for frame, line in traceback.walk_tb(error.__traceback__)][-5:]
            record['context'] = context
            data = self._encode(record)
            if len(data) > min(4096, self.max_bytes):
                context.pop('trace', None)
                data = self._encode(record)
            if len(data) > self.max_bytes:
                return
            try:
                paths = self.paths()
                if paths[0].exists() and paths[0].stat().st_size + len(data) > self.max_bytes:
                    paths[-1].unlink(missing_ok=True)
                    for previous, following in reversed(list(zip(paths, paths[1:]))):
                        if previous.exists():
                            previous.replace(following)
                with os.fdopen(os.open(paths[0], os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), 'ab') as stream:
                    os.fchmod(stream.fileno(), 0o600)
                    stream.write(data)
                self.available = True
            except OSError:
                # Logging must not interrupt fetching, checking or publication.
                self.available = False

    def recent(self, level='all'):
        with self.lock:
            self.maintain()
            records = []
            try:
                for path in self.paths():
                    for record in reversed(self._records(path)):
                        if level == 'all' or record['level'] == 'error' or (level == 'warning' and record['level'] == 'warning'):
                            records.append(record)
                        if len(records) == READ_LIMIT:
                            break
                    if len(records) == READ_LIMIT:
                        break
            except OSError:
                self.available = False
            return {'entries': records, 'available': self.available, 'limit': READ_LIMIT,
                    'max_bytes': self.max_bytes * (self.backups + 1), 'retention_days': self.retention // 86400}

"""Fetch, normalize, cache, and publish complete subscription snapshots."""
from __future__ import annotations

import base64
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
import copy
import hashlib
import json
from pathlib import Path
import random
import secrets
import threading
import time


from . import common, connectivity, protocols, publication, pipeline, server_details, server_policy
from .config import MAX_PROVIDERS, env_int
from .fetch import bounded_fetch_subscription
from .debug_log import DebugLog
from .storage import Store
from .validation import checked_combine


class Busy(common.SubscriptionError):
    pass


class Manager:
    def __init__(self, root: Path, fetcher=bounded_fetch_subscription, checker=None):
        self.adapter_policy = protocols.adapter_policy()
        self.store = Store(root)
        self.logs = DebugLog(self.store.root)
        self.fetcher = fetcher
        self.checker = checker or connectivity.check_entries
        self.guard = threading.RLock()
        self.publish_guard = threading.RLock()
        self.cache_guard = threading.RLock()
        self.fetch_workers = env_int('PARSER_FETCH_WORKERS', 8, 1, 32)
        self.fetch_timeout = env_int('PARSER_FETCH_TIMEOUT_SECONDS', 30, 1, 120)
        env_int('PARSER_CHECK_WORKERS', connectivity.WORKERS, 1, 128)
        env_int('PARSER_CHECK_BATCH_SECONDS', connectivity.BATCH_TIMEOUT, 1, 300)
        self.parsed_cache = {}
        self.selection_cache = {}
        self.latest_selections = {}
        self.inflight_selections = set()
        self.selection_directory = self.store.directory / 'selections'
        self.scope_cache = {}
        self.published_scopes = {}
        self.publication_keys = {}
        self.state_revision = secrets.token_hex(8)
        self.busy = False
        self.busy_action = None
        self.busy_provider_id = None
        self.check_progress = {'done': 0, 'total': 0}
        self.next_refresh = time.time()
        self.next_check = time.time()
        self.stop = threading.Event()
        self.worker = None
        self.refresh_worker = self.check_worker = None
        self.refresh_running = self.check_running = False
        self.refresh_provider_id = None
        self.refresh_scheduled = False
        self.check_provider_ids = None
        self.refresh_progress = {'done': 0, 'total': 0}
        self.check_phase = 'checking'
        self.running_check_id = None
        self.previews = {}
        self.job_path = self.store.directory / 'job.json'
        self.jobs = pipeline.Jobs(self.store.directory)
        self.snapshot_path = self.store.directory / "snapshot.json"
        self.snapshot = json.loads(self.snapshot_path.read_text()) if self.snapshot_path.exists() else {
            "generated_at": None, "providers": {}, "inventory": [], "diagnostics": [], "counts": {}, "total": 0,
            "exports": {}, "history": [], "duplicates_removed": 0}
        self.snapshot.setdefault('last_download', max((value['fetched_at']
            for value in self.snapshot.get('providers', {}).values() if value.get('fetched_at')), default=None))
        self.next_check_batch = self.snapshot.get('next_check_batch', time.time())
        self.next_check = connectivity.next_check_at(self.snapshot.get('connectivity', {}),
            self.store.config()['check_seconds'], batch_due=self.next_check_batch)
        self.next_check = min(self.next_check, self.snapshot.get('next_check', self.next_check))
        if self.snapshot.get('generated_at'):
            self.next_refresh = self.snapshot.get('next_refresh', time.time() + self.store.config()['refresh_seconds'])
        # A saved pre-JSON publication uses "v2board" for its URI count.
        # Keep those feeds available with correct labels until the first check.
        for saved in [self.snapshot, *self.snapshot.get('publications', {}).values()]:
            counts = saved.get('counts', {})
            if 'v2board_uri' not in counts and 'v2board.txt' in saved.get('exports', {}):
                counts['v2board_uri'] = counts.pop('v2board', 0)
                counts['v2board'] = 0
                for server in saved.get('inventory', []):
                    server['clients'] = ['v2board_uri' if client == 'v2board' else client for client in server['clients']]
                for note in saved.get('diagnostics', []):
                    if note.get('client') == 'v2board':
                        note['client'] = 'v2board_uri'
        if self.snapshot.get('connectivity_policy') != connectivity.POLICY_VERSION:
            # Legacy exports have never been connection-tested. Do not publish
            # them during the first background check after an upgrade.
            self.snapshot['exports'] = {}
            self.snapshot['counts'] = {}
            self.snapshot['published_total'] = 0
            for server in self.snapshot.get('inventory', []):
                server['clients'] = []
            self.next_check_batch = self.next_check = time.time()
        self._upgrade_cached_requests()
        if self.snapshot.get('generated_at') and 'sources_key' not in self.snapshot:
            # Bind pre-upgrade snapshots once before accepting configuration
            # changes, including across a restart after a failed rebuild.
            self.snapshot['sources_key'] = self.sources_key()
            common.atomic_write(self.snapshot_path, common.json_text(self.snapshot))
        if self.job_path.exists():
            try:
                job = json.loads(common.read_limited(self.job_path))
                if job.get('kind') not in {'refresh', 'check'} or not isinstance(job.get('provider_id'), (str, type(None))):
                    raise ValueError('invalid saved job')
                if job['kind'] not in self.jobs.items:
                    self.jobs.put(job['kind'], provider_ids=[job['provider_id']] if job.get('provider_id') else None,
                                  provider_id=job.get('provider_id'), reuse_recent=True, regular=job['kind'] == 'check')
                self.job_path.unlink()
            except (OSError, ValueError, AttributeError, common.SubscriptionError) as error:
                self.logs.write('scheduler_failed', error=error)
        # Pin the old published selection before any worker can replace a cache.
        # Existing schema-7 deployments need no database migration.
        if self.snapshot.get('generated_at') and 'active_selections' not in self.snapshot:
            active = {}
            if self.snapshot.get('sources_key') == self.sources_key():
                for provider in self.store.config()['providers']:
                    if provider['enabled']:
                        _, status = self.fetch(provider, False)
                        if status.get('_generation'):
                            active[provider['id']] = status['_generation']
            self.snapshot['active_selections'] = active
            common.atomic_write(self.snapshot_path, common.json_text(self.snapshot))
        self.provider_statuses = dict(self.snapshot.get('providers', {}))
        if any(subscription.get('server_policy', {}).get('revision')
               and publication.scope_key(subscription) not in self.snapshot.get('publications', {})
               for subscription in self.store.subscriptions()):
            # Recover a crash after the database commit but before the job file
            # or publication could be written. The database remains authoritative.
            self._queue_check(reuse_recent=True)
            self.next_check = min(self.next_check, time.time())
        if (self.snapshot.get('generated_at')
                and self.snapshot.get('adapter_policy') != self.adapter_policy):
            self._queue_check(reuse_recent=True)
            self.next_check = min(self.next_check, time.time())

    def pin_selection(self, provider, cache, parsed):
        """An immutable response + draw, referenced by the atomic snapshot."""
        document = {**cache, 'name': provider['name'], 'server_limit': provider.get('server_limit')}
        encoded = common.json_text(document)
        generation = hashlib.sha256(encoded.encode()).hexdigest()
        path = self.selection_directory / (generation + '.json')
        if not path.exists():
            common.atomic_write(path, encoded)
        parsed['generation'] = generation
        self.selection_cache[generation] = parsed
        self.latest_selections[provider['id']] = generation
        return generation

    def read_selection(self, provider, generation):
        if not isinstance(generation, str) or len(generation) != 64 or any(c not in '0123456789abcdef' for c in generation):
            return None
        key = (self.request_fingerprint(provider), provider['name'], provider.get('server_limit'), checked_combine)
        with self.cache_guard:
            parsed = self.selection_cache.get(generation)
            if parsed and parsed['key'] == key:
                return parsed
        try:
            cache = json.loads(common.read_limited(self.selection_directory / (generation + '.json'),
                                                   common.MAX_BYTES * 4 // 3 + 65536))
            if (cache['fingerprint'], cache['name'], cache['server_limit']) != key[:3]:
                return None
            body = base64.b64decode(cache['body'], validate=True)
            entries, count, digest = self.provider_entries(body, provider, cache.get('sample_seed'))
            parsed = {'key': key, 'entries': entries, 'fetched_count': count, 'digest': digest,
                      'metadata': cache['metadata'], 'generation': generation}
            with self.cache_guard:
                self.selection_cache[generation] = parsed
            return parsed
        except (OSError, ValueError, KeyError, common.SubscriptionError):
            return None

    def request_fingerprint(self, provider, revisions=None):
        identifier = provider['hwid_id']
        request = [provider['url'], provider['user_agent'], identifier,
                   (revisions.get(identifier) if revisions is not None else self.store.hwid_revision(identifier))
                   if identifier else None]
        return hashlib.sha256(json.dumps(request).encode()).hexdigest()

    def sources_key(self, config=None):
        providers = (config or self.store.config())['providers']
        with self.store.connection() as connection:
            revisions = dict(connection.execute('SELECT id, revision FROM hwids'))
        # Preserve existing snapshot keys for unlimited providers on upgrade.
        values = [[p['id'], p['name'], self.request_fingerprint(p, revisions)]
                  + ([p['server_limit']] if p.get('server_limit') is not None else [])
                  for p in providers if p['enabled']]
        return hashlib.sha256(json.dumps(values).encode()).hexdigest()

    def _upgrade_cached_requests(self):
        # Old caches were keyed only by URL. Bind each to its original saved
        # assignment once, before any provider/device changes can be accepted.
        for provider in self.store.config()['providers']:
            path = self.store.directory / 'cache' / (provider['id'] + '.json')
            try:
                cache = json.loads(common.read_limited(path, common.MAX_BYTES * 4 // 3 + 65536))
            except (FileNotFoundError, ValueError, common.SubscriptionError):
                continue
            if cache.get('fingerprint') == hashlib.sha256(provider['url'].encode()).hexdigest():
                cache['fingerprint'] = self.request_fingerprint(provider)
                common.atomic_write(path, common.json_text(cache))

    @staticmethod
    def cache_stamp(path):
        stat = path.stat()
        return stat.st_mtime_ns, stat.st_size

    @staticmethod
    def provider_entries(body, provider, seed):
        entries = protocols.parse(body, provider['name'])
        identities = {entry.identity for entry in entries}
        fetched_count = len(identities)
        limit = provider.get('server_limit')
        digest = hashlib.sha256(body).hexdigest()
        if limit is not None:
            # Sample distinct servers, so duplicate rows cannot bias the draw.
            # A saved random seed keeps rechecks/restarts on the same selection.
            selected = set(random.Random(seed).sample(sorted(identities), min(limit, fetched_count)))
            entries = [entry for entry in entries if entry.identity in selected]
            digest = hashlib.sha256(json.dumps([digest, sorted(selected)]).encode()).hexdigest()
        return entries, fetched_count, digest

    def read_cached(self, provider, fingerprint, path):
        key = (fingerprint, provider['name'], provider.get('server_limit'), checked_combine)
        with self.cache_guard:
            stamp = self.cache_stamp(path)
            cached = self.parsed_cache.get(provider['id'])
            if cached and cached['key'] == key and cached['stamp'] == stamp:
                return cached
            cache = json.loads(common.read_limited(path, common.MAX_BYTES * 4 // 3 + 65536))
            if cache['fingerprint'] != fingerprint:
                raise LookupError
            if provider.get('server_limit') is not None and not cache.get('sample_seed'):
                # First limit on a pre-upgrade cache: save the draw before use.
                # Share the write lock with downloads to avoid replacing a new response.
                cache['sample_seed'] = secrets.token_hex(16)
                common.atomic_write(path, common.json_text(cache))
                stamp = self.cache_stamp(path)
        body = base64.b64decode(cache['body'], validate=True)
        entries, fetched_count, digest = self.provider_entries(body, provider, cache.get('sample_seed'))
        cached = {'key': key, 'stamp': stamp, 'digest': digest, 'body_digest': hashlib.sha256(body).hexdigest(),
                  'entries': entries, 'metadata': cache['metadata'], 'fetched_count': fetched_count, 'validated': False}
        with self.cache_guard:
            if self.cache_stamp(path) != stamp:
                # A download won the race while the old response was parsing.
                # Never replace its generation pointer with the older response.
                return self.read_cached(provider, fingerprint, path)
            self.pin_selection(provider, cache, cached)
            self.parsed_cache[provider['id']] = cached
        return cached

    def fetch(self, provider, download=True):
        cache_path = self.store.directory / "cache" / (provider["id"] + ".json")
        fingerprint = self.request_fingerprint(provider)
        status = {"id": provider["id"], "status": "unavailable", "count": 0, "fetched_count": 0, "protocols": {}}
        entries = []
        stage = 'fetch'
        try:
            if not download:
                raise LookupError
            hwid = self.store.hwid_value(provider['hwid_id']) if provider['hwid_id'] is not None else None
            body, metadata = self.fetcher(provider["url"], hwid, provider["user_agent"], self.fetch_timeout)
            body_digest = hashlib.sha256(body).hexdigest()
            seed = secrets.token_hex(16)
            key = (fingerprint, provider['name'], provider.get('server_limit'), checked_combine)
            with self.cache_guard:
                cached = self.parsed_cache.get(provider['id'])
            stage = 'parse'
            if (cached and cached['key'] == key and cached['body_digest'] == body_digest
                    and cached['validated'] and provider.get('server_limit') is None):
                entries = cached['entries']
                fetched_count, digest = cached['fetched_count'], cached['digest']
            else:
                entries, fetched_count, digest = self.provider_entries(body, provider, seed)
                stage = 'validate'
                accepted = checked_combine(entries, self.store.root)
                self.log_validation(accepted, provider['id'])
                if not any(accepted["counts"].values()):
                    raise common.SubscriptionError("provider response has no client-accepted servers")
            with self.cache_guard:
                response = {"fingerprint": fingerprint, "metadata": metadata,
                            "body": base64.b64encode(body).decode(), 'sample_seed': seed}
                parsed = {'key': key,
                    'digest': digest, 'body_digest': body_digest, 'entries': entries, 'metadata': metadata,
                    'fetched_count': fetched_count, 'validated': True}
                generation = self.pin_selection(provider, response, parsed)
                common.atomic_write(cache_path, common.json_text(response))
                parsed['stamp'] = self.cache_stamp(cache_path)
                self.parsed_cache[provider['id']] = parsed
            status.update(status="fresh", fetched_at=metadata.get("fetched_at", common.now()),
                          fetched_count=fetched_count, _content_key=digest, _generation=generation)
        except (common.SubscriptionError, OSError, ValueError, LookupError, RecursionError) as exc:
            if not isinstance(exc, LookupError):
                status["error"] = str(exc) if isinstance(exc, common.SubscriptionError) else "provider could not be fetched or parsed"
                self.logs.write('provider_failed', provider_id=provider['id'], stage=stage, error=exc)
            try:
                cache = self.read_cached(provider, fingerprint, cache_path)
                entries = cache['entries']
                previous = getattr(self, 'provider_statuses', self.snapshot.get('providers', {})).get(provider['id'], {})
                status.update(status="cached" if download else previous.get("status", "cached"),
                              fetched_at=cache["metadata"].get("fetched_at"),
                              fetched_count=cache['fetched_count'], _content_key=cache['digest'], _generation=cache['generation'])
                if not download and previous.get("error"):
                    status["error"] = previous["error"]
                if download:
                    self.logs.write('cache_used', provider_id=provider['id'])
            except (OSError, ValueError, LookupError, common.SubscriptionError, RecursionError) as error:
                entries = []
                if not isinstance(error, (FileNotFoundError, LookupError)):
                    self.logs.write('cache_failed', provider_id=provider['id'], error=error)
        unique = {entry.identity: entry for entry in entries}
        status.update(count=len(unique), protocols=dict(Counter(e.protocol for e in unique.values())))
        return entries, status

    def prepare_scopes(self, config, by_provider, statuses, *, namespace='active'):
        enabled = [p for p in config['providers'] if p['enabled']]
        selections = {'all': enabled}
        policies = self.store.server_policies()
        scoped_policies = {}
        for subscription in self.store.subscriptions():
            key = publication.scope_key(subscription)
            if key not in selections:
                selected = set(subscription['provider_ids'])
                selections[key] = [p for p in enabled if subscription['all_providers'] or p['id'] in selected]
            if subscription['id'] in policies:
                scoped_policies[key] = policies[subscription['id']]
        scopes = {}
        source_configurations = None
        source_signature = tuple((p['id'], p['name'], statuses[p['id']].get('_content_key')) for p in enabled)
        for key, providers in selections.items():
            signature = (checked_combine, tuple((p['id'], p['name'], statuses[p['id']].get('_content_key'))
                                               for p in providers))
            if key in scoped_policies:
                signature += (source_signature,)
            with self.cache_guard:
                cached = self.scope_cache.get((namespace, key))
            if cached and cached[0] == signature:
                scopes[key] = cached[1]
                continue
            # Validation removes outputs. Each selection owns its mutable entries.
            nodes = server_policy.apply(by_provider, [p['id'] for p in providers], scoped_policies.get(key, {}))
            for node in nodes:
                if node.source_entry is None:
                    continue
                if source_configurations is None:
                    source_configurations = pipeline.configurations(scopes['all'][0])
                source = server_policy.customize(node.source_entry, {})
                for core in list(source.outputs):
                    try:
                        accepted_source = (source.identity, core, connectivity.fingerprint(source, core)) in source_configurations
                    except common.SubscriptionError:
                        accepted_source = False
                    if not accepted_source:
                        source.outputs.pop(core)
                node.source_entry = source
            accepted = checked_combine(nodes, self.store.root)
            self.log_validation(accepted)
            scopes[key] = (nodes, accepted['validation'])
            with self.cache_guard:
                self.scope_cache[namespace, key] = (signature, scopes[key])
        with self.cache_guard:
            self.scope_cache = {key: value for key, value in self.scope_cache.items() if key[1] in selections}
            identifiers = {p['id'] for p in config['providers']}
            self.parsed_cache = {key: value for key, value in self.parsed_cache.items() if key in identifiers}
        return scopes

    def log_validation(self, combined, provider_id=None):
        for core, result in combined.get('validation', {}).items():
            if result.get('rejected'):
                self.logs.write('validation_failed', core=core, provider_id=provider_id, rejected=result['rejected'])
            elif result.get('status') == 'not_installed':
                self.logs.write('validator_missing', core=core, provider_id=provider_id)

    def log_checks(self, previous, results, *, changed_only=False):
        counts = Counter()
        for identity, cores in results.items():
            for core, value in cores.items():
                status = value.get('status')
                key = value.get('fingerprint')
                old = (connectivity.matching_result(identity.split(':')[0], core, key, previous) if key
                       else previous.get(identity, {}).get(core, {}))
                if changed_only and (value == old or value.get('deferred')):
                    continue
                counts[status] += 1
                if status in {'failed', 'error'} and (status, value.get('reason')) != (old.get('status'), old.get('reason')):
                    self.logs.write('node_error' if status == 'error' else 'node_failed', node_id=identity[:16],
                                    core=core, reason=value.get('reason'))
                elif status == 'passed' and old.get('status') in {'failed', 'error'}:
                    self.logs.write('node_recovered', node_id=identity[:16], core=core)
        self.logs.write('checks_completed', passed=counts['passed'], failed=counts['failed'], errors=counts['error'])
        return counts['passed'] + counts['failed'] + counts['error']

    def build_publications(self, scopes, results, max_age, evaluated_at, generated, last_check):
        health = {}
        for identity, cores in results.items():
            health.setdefault(identity.split(':')[0], []).append((identity, {
                core: {key: value for key, value in record.items() if key != 'deferred'}
                for core, record in cores.items()}))
        publications, keys, pending = {}, {}, False
        for key, (nodes, validation) in scopes.items():
            digest = hashlib.sha256(json.dumps([health.get(identity) for identity in
                sorted({node.identity for node in nodes} | {node.source_entry.identity for node in nodes
                                                           if node.source_entry is not None})], sort_keys=True).encode()).hexdigest()
            signature = (id(nodes), max_age, digest)
            saved = self.snapshot if key == 'all' else self.snapshot.get('publications', {}).get(key, {})
            if (self.publication_keys.get(key) == signature
                    and saved.get('evaluated_at', 0) <= evaluated_at < saved.get('valid_until', 0)):
                publications[key] = saved
            else:
                published, waiting = publication.build(nodes, results, max_age, evaluated_at, generated, validation, last_check)
                publications[key] = published
                pending |= waiting
            keys[key] = signature
        return publications, keys, pending

    def ensure_publications(self):
        """Prune expired nodes even while downloads or connection checks run."""
        now = time.time()
        with self.guard:
            snapshot = self.snapshot
            saved = {'all': snapshot, **snapshot.get('publications', {})}
            expired = {key for key, value in saved.items() if value.get('generated_at')
                       and not value.get('evaluated_at', 0) <= now < value.get('valid_until', 0)}
            if not expired or snapshot.get('connectivity_policy') != connectivity.POLICY_VERSION:
                return
        with self.publish_guard:
            with self.guard:
                if self.snapshot is not snapshot:
                    return
                config = self.store.config()
                sources_key = self.sources_key(config)
                if snapshot.get('sources_key') != sources_key:
                    return
                scopes = self.published_scopes
            try:
                if not scopes:
                    # Restore prepared configurations once after a restart. No
                    # provider requests or connectivity probes happen here.
                    by_provider, statuses = self._collect(config)
                    active, statuses, _ = self._active_sources(config, by_provider, statuses)
                    scopes = self.prepare_scopes(config, active, statuses)
                updated = dict(snapshot)
                publications = dict(snapshot.get('publications', {}))
                for key in expired:
                    if key not in scopes:
                        continue
                    nodes, validation = scopes[key]
                    published, _ = publication.build(nodes, snapshot.get('connectivity', {}),
                        config['check_seconds'] * 2, now, common.now(), validation, snapshot.get('last_check'))
                    if key == 'all':
                        updated.update(published)
                    else:
                        publications[key] = published
                updated['publications'] = publications
                with self.guard:
                    if self.snapshot is not snapshot or self.sources_key() != sources_key:
                        return
                    common.atomic_write(self.snapshot_path, common.json_text(updated))
                    self.snapshot = updated
                    self.published_scopes = scopes
                    for key in expired:
                        self.publication_keys.pop(key, None)
                    self.state_revision = secrets.token_hex(8)
            except Exception as error:
                self.logs.write('run_failed', operation='publication expiry', stage='publish', error=error)

    def _sync_busy(self):
        self.busy = self.refresh_running or self.check_running
        self.busy_action = ('fetching_and_checking' if self.refresh_running and self.check_running else
                            'fetching' if self.refresh_running else self.check_phase if self.check_running else None)
        self.busy_provider_id = (self.refresh_provider_id if self.refresh_running else
                                self.check_provider_ids[0] if self.check_running and self.check_provider_ids
                                and len(self.check_provider_ids) == 1 else None)

    def _collect(self, config):
        nodes, statuses = {}, {}
        for provider in config['providers']:
            if provider['enabled']:
                nodes[provider['id']], statuses[provider['id']] = self.fetch(provider, False)
            else:
                statuses[provider['id']] = {'id': provider['id'], 'status': 'disabled', 'count': 0, 'protocols': {}}
        return nodes, statuses

    def _active_sources(self, config, nodes, statuses):
        active_nodes, active_statuses, versions = {}, dict(statuses), {}
        for provider in config['providers']:
            if not provider['enabled']:
                continue
            identifier = provider['id']
            generation = self.snapshot.get('active_selections', {}).get(identifier)
            saved = self.read_selection(provider, generation)
            if saved:
                active_nodes[identifier] = saved['entries']
                active_statuses[identifier] = {**statuses[identifier], '_content_key': saved['digest']}
                versions[identifier] = generation
            else:
                # New provider or changed credentials/limit: never retain a
                # selection which violates the current provider configuration.
                active_nodes[identifier] = nodes[identifier]
                if statuses[identifier].get('_generation'):
                    versions[identifier] = statuses[identifier]['_generation']
        return active_nodes, active_statuses, versions

    def _queue_check(self, identifiers=None, *, reuse_recent=True, regular=False):
        with self.guard:
            old = self.jobs.items.get('check')
            if old:
                previous = old.get('provider_ids')
                identifiers = None if previous is None or identifiers is None else sorted(set(previous) | set(identifiers))
                if old['id'] != self.running_check_id:
                    reuse_recent = reuse_recent and old.get('reuse_recent', True)
                    regular = regular or old.get('regular', False)
            job = self.jobs.put('check', provider_ids=identifiers, reuse_recent=reuse_recent, regular=regular)
            return job

    def _record_failure(self, operation, error):
        self.logs.write('run_failed', operation=operation, stage='fetch' if operation == 'refresh' else 'check', error=error)
        with self.guard:
            self.snapshot = {**self.snapshot,
                'last_error': f'{operation} failed; the last completed subscription is still available'}

    def _download(self, job, *, autostart=True):
        started = time.monotonic()
        selected = job.get('provider_id')
        self.logs.write('run_started', operation='refresh', provider_id=selected)
        try:
            config = self.store.config()
            requested = job.get('provider_ids')
            providers = [p for p in config['providers'] if p['enabled']
                         and (requested is None or p['id'] in requested)]
            remaining_ids = {p['id'] for p in providers}
            with self.guard:
                self.refresh_progress = {'done': 0, 'total': len(providers)}
            with ThreadPoolExecutor(max_workers=self.fetch_workers, thread_name_prefix='provider-fetch') as pool:
                remaining, active = iter(providers), {}
                def fill():
                    while len(active) < self.fetch_workers and not self.stop.is_set():
                        provider = next(remaining, None)
                        if provider is None:
                            break
                        active[pool.submit(self.fetch, provider)] = provider
                fill()
                while active:
                    finished, _ = wait(active, timeout=.25, return_when=FIRST_COMPLETED)
                    for future in finished:
                        provider = active.pop(future)
                        _, status = future.result()
                        identifier = provider['id']
                        with self.guard:
                            self.provider_statuses[identifier] = status
                            # Persist check intent before acknowledging this download.
                            self._queue_check([identifier], reuse_recent=job.get('reuse_recent', True))
                            remaining_ids.discard(identifier)
                            job = self.jobs.put('refresh', provider_ids=sorted(remaining_ids), provider_id=selected,
                                                reuse_recent=job.get('reuse_recent', True),
                                                scheduled=job.get('scheduled', False))
                            self.refresh_progress['done'] += 1
                    fill()
            with self.publish_guard, self.guard:
                generated = common.now()
                statuses = dict(self.provider_statuses)
                last_download = max((stamp for stamp in [self.snapshot.get('last_download'),
                    *(s.get('fetched_at') for s in statuses.values())] if stamp), default=None)
                event = {'at': generated, 'seconds': round(time.monotonic() - started, 2),
                         'action': 'refresh', 'scheduled': self.refresh_scheduled,
                         'servers': sum(s.get('count', 0) for s in statuses.values()),
                         'published': self.snapshot.get('published_total', 0), 'checked': 0,
                         'fresh': sum(s.get('status') == 'fresh' for s in statuses.values()),
                         'cached': sum(s.get('status') == 'cached' for s in statuses.values())}
                if selected:
                    event['provider_id'] = selected
                if selected is None:
                    self.next_refresh = time.time() + config['refresh_seconds']
                snapshot = {**self.snapshot, 'providers': statuses, 'last_download': last_download,
                            'next_refresh': self.next_refresh,
                            'history': ([event] + self.snapshot.get('history', []))[:20]}
                common.atomic_write(self.snapshot_path, common.json_text(snapshot))
                self.snapshot = snapshot
                self.state_revision = secrets.token_hex(8)
                if not remaining_ids:
                    self.jobs.clear('refresh', job)
                self.logs.write('run_completed', operation='refresh', servers=event['servers'],
                                published=event['published'], cached=event['cached'], seconds=event['seconds'])
        except Exception as error:
            self._record_failure('refresh', error)
            with self.guard:
                if self.jobs.items.get('refresh'):
                    self._retry_job('refresh', self.jobs.items['refresh'], 30)
        finally:
            with self.guard:
                self.refresh_running = False
                self.refresh_provider_id = None
                self.refresh_scheduled = False
                self._sync_busy()
            self._clean_selections()
            if autostart and not self.stop.is_set():
                self._start_queued_check()

    def _publish(self, incoming=None, captured=None, *, event=None, next_batch=None, global_check=False):
        """Commit health and selection promotion together; probes run outside this lock."""
        with self.publish_guard:
            config = self.store.config()
            sources_key = self.sources_key(config)
            candidates, statuses = self._collect(config)
            versions = {identifier: status.get('_generation') for identifier, status in statuses.items()}
            candidate_scopes = self.prepare_scopes(config, candidates, statuses, namespace='candidate')
            active_nodes, active_statuses, active = self._active_sources(config, candidates, statuses)
            with self.guard:
                previous = self.snapshot
            results = previous.get('connectivity', {})
            candidate_entries = [entry for nodes, _ in candidate_scopes.values() for entry in nodes]
            by_source = {p['name']: [] for p in config['providers'] if p['enabled']}
            for entry in candidate_entries:
                by_source[entry.source].append(entry)
            # Provider promotion depends on its original configurations. Failed
            # or pending custom endpoints must never hold up a provider draw.
            all_required = {p['id']: pipeline.configurations(by_source[p['name']])
                        for p in config['providers'] if p['enabled']}
            required = {identifier: pipeline.configurations(e for e in by_source[p['name']] if e.source_entry is None)
                        for p in config['providers'] if p['enabled'] for identifier in [p['id']]}
            allowed = set().union(*(keys for identifier, keys in all_required.items()
                                  if captured is None or captured.get(identifier) == versions.get(identifier)))
            if incoming is not None:
                results = pipeline.merge_results(results, incoming, allowed)
            coverage = {}
            now = time.time()
            for provider in config['providers']:
                identifier = provider['id']
                generation = versions.get(identifier)
                if not provider['enabled'] or not generation:
                    continue
                if active.get(identifier) == generation:
                    continue
                saved = previous.get('selection_checks', {}).get(identifier, {})
                done = set(saved.get('done', [])) if saved.get('generation') == generation else set()
                for identity, core, fingerprint in required[identifier]:
                    value = connectivity.matching_result(identity, core, fingerprint, results)
                    if (value.get('status') in {'passed', 'failed', 'error'} and not value.get('deferred')
                            and 0 <= now - value.get('checked_ts', 0) < config['check_seconds'] * 2):
                        done.add(fingerprint)
                if all(key in done for _, _, key in required[identifier]):
                    active[identifier] = generation
                    active_nodes[identifier] = candidates[identifier]
                    active_statuses[identifier] = statuses[identifier]
                else:
                    coverage[identifier] = {'generation': generation, 'done': sorted(done)}
            scopes = self.prepare_scopes(config, active_nodes, active_statuses)
            needed = set().union(*all_required.values(), pipeline.configurations(
                entry for nodes, _ in scopes.values() for entry in nodes))
            if incoming is not None:
                results = pipeline.retained_results(results, needed)
            checked = self.log_checks(previous.get('connectivity', {}), results, changed_only=True) if incoming is not None else 0
            generated = common.now()
            last_check = generated if checked else previous.get('last_check')
            publications, keys, pending = self.build_publications(scopes, results, config['check_seconds'] * 2,
                                                                   now, generated, last_check)
            combined = publications.pop('all')
            batch = self.next_check_batch if next_batch is None else next_batch
            due = self.next_check
            if incoming is not None:
                candidate_health = pipeline.retained_results(results, set().union(*all_required.values()))
                proposed = connectivity.next_check_at(candidate_health, config['check_seconds'], batch_due=batch)
                due = proposed if next_batch is not None or global_check else min(due, proposed)
            elif pending or coverage:
                due = min(due, now + 5)
            history = previous.get('history', [])
            if event:
                event = {**event, 'servers': combined['total'], 'published': combined['published_total'],
                         'fresh': sum(s['status'] == 'fresh' for s in statuses.values()),
                         'cached': sum(s['status'] == 'cached' for s in statuses.values()), 'checked': checked}
                history = ([event] + history)[:20]
            snapshot = {**combined, 'providers': statuses, 'publications': publications, 'connectivity': results,
                        'adapter_policy': self.adapter_policy,
                        'sources_key': sources_key, 'active_selections': active, 'selection_checks': coverage,
                        'last_download': max((stamp for stamp in [previous.get('last_download'),
                            *(status.get('fetched_at') for status in statuses.values())] if stamp), default=None),
                        'next_refresh': self.next_refresh,
                        'next_check_batch': batch, 'next_check': due, 'history': history}
            with self.cache_guard, self.guard:
                # A download may finish during validation/building. Never promote
                # its predecessor using a late check, or overwrite concurrent metadata.
                if (self.sources_key() != sources_key or any(self.latest_selections.get(identifier) != generation
                        for identifier, generation in versions.items() if generation)):
                    return False
                # Failed downloads retain the same generation. Their new error
                # status must survive a check which started before the failure.
                for identifier, status in list(statuses.items()):
                    latest = self.provider_statuses.get(identifier, {})
                    if latest.get('_generation') and latest['_generation'] == versions.get(identifier):
                        statuses[identifier] = {**status, **latest}
                if event:
                    event.update(fresh=sum(s['status'] == 'fresh' for s in statuses.values()),
                                 cached=sum(s['status'] == 'cached' for s in statuses.values()))
                if self.snapshot is not previous:
                    snapshot['history'] = ([event] if event else []) + self.snapshot.get('history', [])
                    snapshot['history'] = snapshot['history'][:20]
                    snapshot['last_download'] = self.snapshot.get('last_download')
                    if self.snapshot.get('last_error'):
                        snapshot['last_error'] = self.snapshot['last_error']
                common.atomic_write(self.snapshot_path, common.json_text(snapshot))
                self.snapshot = snapshot
                self.provider_statuses = statuses
                self.published_scopes, self.publication_keys = scopes, keys
                self.next_check_batch, self.next_check = batch, due
                self.state_revision = secrets.token_hex(8)
            self._clean_selections()
            return True

    def _check(self, job):
        started, started_at = time.monotonic(), time.time()
        identifiers = job.get('provider_ids')
        self.logs.write('run_started', operation='connection check',
                        provider_id=identifiers[0] if identifiers and len(identifiers) == 1 else None)
        success, retry_delay = False, 5
        try:
            config = self.store.config()
            nodes, statuses = self._collect(config)
            captured = {identifier: status.get('_generation') for identifier, status in statuses.items()}
            with self.cache_guard:
                self.inflight_selections = {value for value in captured.values() if value}
            scopes = self.prepare_scopes(config, nodes, statuses, namespace='candidate')
            entries = [entry for nodes, _ in scopes.values() for entry in nodes]
            names = {p['name'] for p in config['providers'] if identifiers is None or p['id'] in identifiers}
            targets = None if identifiers is None else pipeline.configurations(e for e in entries if e.source in names)
            def progress(done, total):
                with self.guard:
                    self.check_progress = {'done': done, 'total': total}
            with self.guard:
                previous = self.snapshot.get('connectivity', {})
            regular = job.get('regular', False)
            reuse = ((connectivity.BATCH_REUSE_SECONDS if regular else config['check_seconds'])
                     if job.get('reuse_recent') else 0)
            originals = [entry for entry in entries if entry.source_entry is None]
            variants = [entry for entry in entries if entry.source_entry is not None]
            deadline = time.monotonic() + env_int('PARSER_CHECK_BATCH_SECONDS', connectivity.BATCH_TIMEOUT, 1, 300)
            results = self.checker(originals, self.store.root, stop=self.stop, previous=previous,
                                   progress=progress, targets=targets, reuse_seconds=reuse, deadline=deadline)
            if variants:
                # Check replacements only after their source passes, per core.
                merged = pipeline.merge_results(previous, results, pipeline.configurations(originals))
                eligible = []
                for entry in variants:
                    candidate = copy.copy(entry)
                    candidate.outputs = dict(entry.outputs)
                    for core in list(candidate.outputs):
                        if connectivity.current_result(entry.source_entry, core, merged,
                                                       config['check_seconds'] * 2)['status'] != 'passed':
                            candidate.outputs.pop(core)
                    if candidate.outputs:
                        eligible.append(candidate)
                if eligible:
                    completed = self.check_progress['done']
                    def variant_progress(done, total):
                        progress(completed + done, completed + total)
                    checked = self.checker(eligible, self.store.root, stop=self.stop, previous=merged,
                        progress=variant_progress, targets=targets, reuse_seconds=reuse, deadline=deadline)
                    results = pipeline.merge_results(results, checked, pipeline.configurations(variants))
            with self.guard:
                self.check_phase = 'updating'
                self._sync_busy()
            event = {'at': common.now(), 'seconds': round(time.monotonic() - started, 2), 'action': 'connection check'}
            if identifiers and len(identifiers) == 1:
                event['provider_id'] = identifiers[0]
            next_batch = started_at + config['check_seconds'] if regular else None
            success = self._publish(results, captured, event=event, next_batch=next_batch,
                                    global_check=identifiers is None)
            if success:
                self.logs.write('run_completed', operation='connection check', servers=self.snapshot['total'],
                                published=self.snapshot['published_total'], seconds=event['seconds'])
        except Exception as error:
            retry_delay = 30
            self._record_failure('connection check', error)
        finally:
            with self.guard:
                try:
                    current = self.jobs.items.get('check')
                    if current and current['id'] == job['id']:
                        if success and not self.stop.is_set():
                            self.jobs.clear('check', job)
                        else:
                            retry = {**job, 'reuse_recent': True, 'regular': False} if success else job
                            self._retry_job('check', retry, retry_delay)
                    if not success:
                        self.next_check = time.time() + retry_delay
                except Exception as error:
                    success = False
                    self._record_failure('connection check', error)
                    self._retry_job('check', job, 30)
                finally:
                    self.check_running = False
                    self.check_phase = 'checking'
                    self.running_check_id = None
                    self.inflight_selections = set()
                    self._sync_busy()
            if success and not self.stop.is_set():
                self._start_queued_check()

    def _retry_job(self, kind, job, delay):
        fields = {k: v for k, v in job.items() if k not in {'id', 'retry_at'}}
        retry_at = time.time() + delay
        try:
            self.jobs.put(kind, **fields, retry_at=retry_at)
        except OSError as error:
            # The existing durable intent remains. Back off in memory too, and
            # always release the worker's running flag when storage is full.
            self.jobs.items[kind] = {**job, 'retry_at': retry_at}
            self.logs.write('scheduler_failed', error=error)

    def _clean_selections(self):
        with self.cache_guard:
            if self.refresh_running:
                return
            identifiers = {provider['id'] for provider in self.store.config()['providers']}
            self.latest_selections = {key: value for key, value in self.latest_selections.items() if key in identifiers}
            keep = (set(self.snapshot.get('active_selections', {}).values())
                    | set(self.latest_selections.values()) | self.inflight_selections)
            for path in self.selection_directory.glob('*.json'):
                if path.stem not in keep:
                    try:
                        path.unlink(missing_ok=True)
                    except OSError as error:
                        self.logs.write('cache_failed', error=error)
            self.selection_cache = {key: value for key, value in self.selection_cache.items() if key in keep}

    def refresh(self, selected=None, download=True, check=None, reuse_recent=False):
        """Synchronous maintenance helper; HTTP jobs use the independent workers."""
        check = download if check is None else check
        if download:
            with self.guard:
                job = self.jobs.put('refresh', provider_ids=[selected] if selected else None,
                                    provider_id=selected, reuse_recent=reuse_recent)
                self.refresh_running, self.refresh_provider_id = True, selected
                self._sync_busy()
            self._download(job, autostart=False)
        if check:
            with self.guard:
                regular = selected is None and (not reuse_recent or time.time() >= self.next_check_batch)
                job = self._queue_check([selected] if selected else None, reuse_recent=reuse_recent, regular=regular)
                self.check_running, self.check_provider_ids, self.running_check_id = True, job['provider_ids'], job['id']
                self.check_progress = {'done': 0, 'total': 0}
                self._sync_busy()
            self._check(job)
        elif not download:
            try:
                self._publish(event={'at': common.now(), 'seconds': 0, 'action': 'configuration updated'})
            except Exception as error:
                self._record_failure('refresh', error)
            finally:
                with self.guard:
                    self._sync_busy()

    def begin_refresh(self, selected=None, *, reuse_recent=False, scheduled=False):
        with self.guard:
            if self.refresh_running or (self.busy and not self.check_running):
                raise Busy('a provider refresh is already running')
            if selected is not None:
                provider = next((p for p in self.store.config()['providers'] if p['id'] == selected), None)
                if provider is None:
                    raise common.SubscriptionError('provider does not exist')
                if not provider['enabled']:
                    raise common.SubscriptionError('provider is paused; enable it before refreshing')
            job = self.jobs.put('refresh', provider_ids=[selected] if selected else None,
                                provider_id=selected, reuse_recent=reuse_recent, scheduled=scheduled)
            self._start_refresh(job)

    def _start_refresh(self, job):
        self.refresh_running, self.refresh_provider_id = True, job.get('provider_id')
        self.refresh_scheduled = job.get('scheduled', False)
        self.refresh_progress = {'done': 0, 'total': 0}
        if not self.check_running:
            self.check_progress = {'done': 0, 'total': 0}
        self._sync_busy()
        self.refresh_worker = threading.Thread(target=self._download, args=(job,), daemon=True, name='provider-refresh')
        self.worker = self.refresh_worker
        self.refresh_worker.start()

    def _start_queued_check(self):
        with self.guard:
            job = self.jobs.items.get('check')
            if (not job or self.check_running or self.stop.is_set() or job.get('retry_at', 0) > time.time()
                    or (self.busy and not self.refresh_running)):
                return
            self.check_running, self.check_provider_ids, self.running_check_id = True, job.get('provider_ids'), job['id']
            self.check_progress = {'done': 0, 'total': 0}
            self.check_phase = 'checking'
            self._sync_busy()
            self.check_worker = threading.Thread(target=self._check, args=(job,), daemon=True, name='connection-check')
            self.worker = self.check_worker
            self.check_worker.start()

    def begin_check(self, *, reuse_recent=False):
        with self.guard:
            if self.check_running or (self.busy and not self.refresh_running):
                raise Busy('a connection check is already running')
            regular = not reuse_recent or time.time() >= self.next_check_batch
            self._queue_check(reuse_recent=reuse_recent, regular=regular)
            self._start_queued_check()

    def wait_for_idle(self, timeout=60):
        deadline = time.monotonic() + timeout
        while True:
            with self.guard:
                workers = [worker for worker in (self.refresh_worker, self.check_worker)
                           if worker and worker is not threading.current_thread() and worker.is_alive()]
            if not workers:
                return
            for worker in workers:
                worker.join(max(0, deadline - time.monotonic()))
            if time.monotonic() >= deadline:
                return

    def publication_current(self, snapshot, sources_key=None):
        return (snapshot.get('connectivity_policy') == connectivity.POLICY_VERSION
                and snapshot.get('evaluated_at', 0) <= time.time() < snapshot.get('valid_until', 0)
                and self.snapshot.get('sources_key') == (sources_key or self.sources_key()))

    def change_settings(self, values):
        with self.guard:
            if self.busy:
                raise Busy('wait for the current refresh or connection check before changing schedules')
            config = self.store.config()
            config.update(values)
            self.store.save_config(config)
            if 'refresh_seconds' in values:
                self.next_refresh = time.time() + config['refresh_seconds']
                snapshot = {**self.snapshot, 'next_refresh': self.next_refresh}
                common.atomic_write(self.snapshot_path, common.json_text(snapshot))
                self.snapshot = snapshot
            if 'check_seconds' in values:
                self.busy = True
                self.next_check_batch = time.time()
        if 'check_seconds' in values:
            self.refresh(download=False, check=False)
            with self.guard:
                self.next_check = time.time()

    def change_subscription(self, values=None, identifier=None, delete=False):
        with self.guard:
            if self.busy:
                raise Busy('wait for the current refresh or connection check before changing subscriptions')
            if delete:
                self.store.delete_subscription(identifier)
            else:
                identifier = self.store.save_subscription(values, identifier)
            self.busy = True
        # Selections are applied before publication. If a rebuild fails, an
        # unmatched selection stays unavailable instead of serving other sources.
        self.refresh(download=False, check=False)
        return identifier

    def subscription_snapshot(self, subscription):
        key = publication.scope_key(subscription)
        return self.snapshot if key == 'all' else self.snapshot.get('publications', {}).get(key, {})

    def _subscription_sources(self, subscription):
        """Resolve bindings against the pinned sample, never an unchecked fetch."""
        config = self.store.config()
        providers = {p['id']: p for p in config['providers']}
        matches = {}
        if self.snapshot.get('sources_key') == self.sources_key(config):
            for provider in providers.values():
                if not provider['enabled'] or (not subscription['all_providers']
                        and provider['id'] not in subscription['provider_ids']):
                    continue
                saved = self.read_selection(provider, self.snapshot.get('active_selections', {}).get(provider['id']))
                if saved:
                    for entry in saved['entries']:
                        key = (provider['id'], server_policy.source_key(entry))
                        if key not in matches or entry.group_size < matches[key].group_size:
                            matches[key] = entry
        return config, providers, matches

    def subscription_servers(self, identifier, query='', offset=0, limit=50):
        self.ensure_publications()
        with self.publish_guard:
            subscription = self.store.subscription(identifier)
            policy = self.store.server_policy(identifier)
            config, providers, matches = self._subscription_sources(subscription)
            results, now = self.snapshot.get('connectivity', {}), time.time()
            max_age = config['check_seconds'] * 2
            original_inventory = {row['id']: row for row in self.snapshot.get('inventory', [])}
            effective_inventory = {row['id']: row for row in self.subscription_snapshot(subscription).get('inventory', [])}
            def health(entry):
                return {core: {key: value for key, value in connectivity.current_result(
                    entry, core, results, max_age, now).items() if key not in {'fingerprint', 'checked_ts', 'deferred'}}
                    for core in connectivity.CORES}
            def working(entry):
                accepted = original_inventory.get(entry.identity[:16], {}).get('clients', [])
                return any(core in accepted and value['status'] == 'passed' for core, value in health(entry).items())
            bound = {(r['provider_id'], r['source_identity']) for r in policy['records']}
            available = []
            for (provider_id, identity), entry in matches.items():
                if (provider_id, identity) in bound or not working(entry):
                    continue
                available.append({'provider_id': provider_id, 'provider_name': providers[provider_id]['name'],
                                  'source_identity': identity, 'source': server_policy.describe(entry)})
            records = []
            for record in policy['records']:
                entry = matches.get((record['provider_id'], record['source_identity']))
                source = server_policy.describe(entry) if entry else record['source']
                effective = server_policy.customize(entry, record) if entry else None
                published = effective_inventory.get(effective.identity[:16]) if effective else None
                checks = published.get('connectivity', {}) if published else health(effective) if effective else {}
                provider = providers.get(record['provider_id'])
                source_working = working(entry) if entry else False
                status = ('unavailable' if entry is None else 'source_failed' if not source_working else
                          'working' if any(v['status'] == 'passed' for v in checks.values()) else 'pending_or_failed')
                suggestions = []
                if entry is None:
                    suggestions = [candidate for candidate in available
                        if candidate['provider_id'] == record['provider_id']
                        and candidate['source']['protocol'] == source['protocol']
                        and (candidate['source']['address'] == source['address']
                             or candidate['source']['name'] == source['name'])][:3]
                records.append({**record, 'source': source, 'source_present': entry is not None,
                    'provider_name': provider['name'] if provider else record['provider_name'],
                    'status': status, 'checks': checks, 'suggestions': suggestions})
            query = query.strip().casefold()
            available = [row for row in available if query in ' '.join(str(value or '') for value in
                (row['provider_name'], *row['source'].values())).casefold()]
            available.sort(key=lambda row: (row['source']['name'].casefold(), row['provider_id'], row['source_identity']))
            return {'subscription_id': identifier, 'revision': policy['revision'], 'all_servers': policy['all_servers'],
                    'records': records, 'available': available[offset:offset + limit], 'available_total': len(available),
                    'offset': offset, 'limit': limit, 'max_records': server_policy.MAX_RECORDS}

    def change_subscription_servers(self, identifier, values):
        with self.publish_guard, self.guard:
            if self.busy:
                raise Busy('wait for the current refresh or connection check before changing server selections')
            subscription = self.store.subscription(identifier)
            previous = self.store.server_policy(identifier)
            if previous['revision'] != values['revision']:
                raise Busy('server selection changed; reopen the editor before saving')
            config, providers, matches = self._subscription_sources(subscription)
            original_inventory = {row['id']: row for row in self.snapshot.get('inventory', [])}
            old = {r['id']: r for r in previous['records']}
            records, seen, record_ids = [], set(), set()
            if len(values['records']) > server_policy.MAX_RECORDS:
                raise common.SubscriptionError('too many saved servers in this subscription')
            for value in values['records']:
                record_id = value.get('id')
                before = old.get(record_id) if record_id else None
                if record_id and before is None:
                    raise common.SubscriptionError('saved server does not belong to this subscription')
                key = (value['provider_id'], value['source_identity'])
                if key in seen or record_id and record_id in record_ids:
                    raise common.SubscriptionError('choose each source server only once')
                seen.add(key)
                if record_id:
                    record_ids.add(record_id)
                relink = before is not None and key != (before['provider_id'], before['source_identity'])
                if relink and not value.get('relink'):
                    raise common.SubscriptionError('explicitly relink this saved server to change its source')
                entry = matches.get(key)
                if before is None or relink:
                    accepted = original_inventory.get(entry.identity[:16], {}).get('clients', []) if entry else []
                    if entry is None or not any(core in accepted and connectivity.current_result(entry, core,
                            self.snapshot.get('connectivity', {}), config['check_seconds'] * 2)['status'] == 'passed'
                            for core in connectivity.CORES):
                        raise common.SubscriptionError('selected server is no longer working in this provider selection; reload servers')
                edits = server_policy.overrides(value)
                if entry and not server_policy.customize(entry, edits).outputs:
                    raise common.SubscriptionError('this server does not support address or port customization')
                records.append({'id': record_id or secrets.token_hex(8), 'provider_id': key[0],
                    'provider_name': providers[key[0]]['name'] if key[0] in providers else before['provider_name'],
                    'source_identity': key[1], 'source': server_policy.describe(entry) if entry else before['source'],
                    'selected': value.get('selected', True), **edits})
            revision = self.store.save_server_policy(identifier, values['revision'], values['all_servers'], records)
            self.state_revision = secrets.token_hex(8)
            # A durable intent survives a restart between saving and rebuilding.
            self._queue_check(reuse_recent=True)
            self.busy = True
        self.refresh(download=False, check=False)
        return {'revision': revision}

    def public_subscriptions(self, base_url, *, ensure=True, sources_key=None):
        if ensure:
            self.ensure_publications()
        with self.guard:
            result = []
            sources_key = sources_key or self.sources_key()
            for subscription in self.store.subscriptions():
                snapshot = self.subscription_snapshot(subscription)
                current = self.publication_current(snapshot, sources_key)
                token = subscription['token']
                result.append({k: v for k, v in subscription.items() if k != 'token'} | {
                    'total': snapshot.get('total', 0),
                    'published_total': snapshot.get('published_total', 0) if current else 0,
                    'counts': snapshot.get('counts', {}) if current else {},
                    'generated_at': snapshot.get('generated_at'), 'ready': current,
                    'links': {name: f"{base_url.rstrip('/')}/sub/{token}/{name}" for name in publication.FILES}})
            return result

    def change_provider(self, values=None, provider_id=None, delete=False):
        with self.guard:
            if self.busy:
                raise Busy("wait for the current refresh before changing providers")
            config = self.store.config()
            previous = next((p for p in config["providers"] if p["id"] == provider_id), None)
            if provider_id and previous is None:
                raise common.SubscriptionError("provider does not exist")
            if not delete:
                if previous is None and len(config["providers"]) >= MAX_PROVIDERS:
                    raise common.SubscriptionError(f"maximum of {MAX_PROVIDERS} providers reached")
                provider = self.store.provider(values, previous)
                if any(p["id"] != provider["id"] and p["name"].casefold() == provider["name"].casefold() for p in config["providers"]):
                    raise common.SubscriptionError("provider name must be unique")
                preview = None
                if values.get('test_token'):
                    preview = self.previews.get(values['test_token'])
                    comparable = {k: v for k, v in provider.items() if k != 'id'}
                    if (not preview or preview['expires'] < time.monotonic() or preview['previous'] != previous
                            or comparable != {k: v for k, v in preview['provider'].items() if k != 'id'}
                            or preview['fingerprint'] != self.request_fingerprint(provider)):
                        raise common.SubscriptionError('fetch test expired or settings changed; test again before saving')
            config["providers"] = [p for p in config["providers"] if p["id"] != provider_id]
            if not delete:
                config["providers"].append(provider)
            if not delete and preview:
                cache_path = self.store.directory / 'cache' / (provider['id'] + '.json')
                old_cache = cache_path.read_text() if cache_path.exists() else None
                with self.cache_guard:
                    common.atomic_write(cache_path, common.json_text({'fingerprint': preview['fingerprint'],
                        'metadata': preview['metadata'], 'body': base64.b64encode(preview['body']).decode(),
                        'sample_seed': preview['sample_seed']}))
                try:
                    self.store.save_config(config)
                except Exception:
                    if old_cache is None:
                        cache_path.unlink(missing_ok=True)
                    else:
                        common.atomic_write(cache_path, old_cache)
                    raise
                self.previews.pop(values['test_token'])
                self.snapshot.setdefault('providers', {})[provider['id']] = {'status': 'fresh'}
                self.provider_statuses[provider['id']] = {'status': 'fresh'}
            else:
                self.store.save_config(config)
            self.busy = True
        # Rebuild immediately: disabled/deleted providers disappear from public links.
        self.refresh(download=False)
        return None if delete else self.store.public_provider(provider)

    def test_provider(self, values, provider_id=None):
        with self.guard:
            if self.busy:
                raise Busy('wait for the current refresh or fetch test')
            config = self.store.config()
            previous = next((p for p in config['providers'] if p['id'] == provider_id), None)
            if provider_id and previous is None:
                raise common.SubscriptionError('provider does not exist')
            provider = self.store.provider(values, previous)
            if any(p['id'] != provider['id'] and p['name'].casefold() == provider['name'].casefold() for p in config['providers']):
                raise common.SubscriptionError('provider name must be unique')
            if previous is None and len(config['providers']) >= MAX_PROVIDERS:
                raise common.SubscriptionError(f'maximum of {MAX_PROVIDERS} providers reached')
            hwid = self.store.hwid_value(provider['hwid_id']) if provider['hwid_id'] is not None else None
            fingerprint = self.request_fingerprint(provider)
            self.busy = True
            self.busy_action = 'testing'
            self.busy_provider_id = provider_id
        try:
            body, metadata = self.fetcher(provider['url'], hwid, provider['user_agent'], self.fetch_timeout)
            seed = secrets.token_hex(16)
            entries, fetched_count, _ = self.provider_entries(body, provider, seed)
            result = checked_combine(entries, self.store.root)
            self.log_validation(result, provider['id'])
            if not any(result['counts'].values()):
                raise common.SubscriptionError('no compatible servers were returned; check the subscription and device ID')
            token = secrets.token_urlsafe(24)
            with self.guard:
                self.previews = {key: p for key, p in self.previews.items() if p['expires'] > time.monotonic()}
                while self.previews and (len(self.previews) >= 50 or sum(len(p['body']) for p in self.previews.values()) + len(body) > 64 * 1024 * 1024):
                    self.previews.pop(next(iter(self.previews)))
                self.previews[token] = {'provider': provider, 'previous': previous, 'fingerprint': fingerprint,
                    'body': body, 'metadata': metadata, 'sample_seed': seed, 'expires': time.monotonic() + 600}
            self.logs.write('provider_tested', provider_id=provider['id'], servers=len(entries))
            return {'test_token': token, 'counts': result['counts'], 'tested_at': common.now(),
                    'fetched_count': fetched_count, 'selected_count': len({entry.identity for entry in entries})}
        except common.SubscriptionError as error:
            self.logs.write('provider_test_failed', provider_id=provider['id'], error=error)
            message = str(error).replace(provider['url'], '[subscription]')
            if hwid:
                message = message.replace(hwid, '[device ID]')
            raise common.SubscriptionError(message) from None
        except (OSError, ValueError, RecursionError) as error:
            self.logs.write('provider_test_failed', provider_id=provider['id'], error=error)
            raise common.SubscriptionError('fetch test failed; check the subscription URL and provider availability') from None
        finally:
            with self.guard:
                self.busy = False
                self.busy_action = None
                self.busy_provider_id = None

    def change_hwid(self, identifier, *, name=None, replacement=None, delete=False):
        with self.guard:
            if self.busy:
                raise Busy('wait for the current refresh or fetch test before changing devices')
            if delete:
                self.store.delete_hwid(identifier)
            elif replacement is not None:
                self.store.replace_hwid(identifier, replacement['value'], replacement['revision'], replacement['provider_ids'])
                self.busy = True
            else:
                self.store.rename_hwid(identifier, name)
        if replacement is not None:
            # Old responses no longer describe the replacement identity.
            # Rebuild from matching caches without fetching/registering a device.
            self.refresh(download=False)

    def scheduler(self):
        while not self.stop.wait(2):
            self.logs.maintain()
            self.ensure_publications()
            try:
                with self.guard:
                    now = time.time()
                    if not self.refresh_running and (not self.busy or self.check_running):
                        job = self.jobs.items.get('refresh')
                        if job and job.get('retry_at', 0) <= now:
                            self._start_refresh(job)
                        elif not job and now >= self.next_refresh:
                            self.begin_refresh(reuse_recent=True, scheduled=True)
                    if not self.check_running and (not self.busy or self.refresh_running):
                        job = self.jobs.items.get('check')
                        if now >= self.next_check and not job:
                            self._queue_check(reuse_recent=True, regular=now >= self.next_check_batch)
                        self._start_queued_check()
            except Busy:
                pass
            except Exception as error:
                self.logs.write('scheduler_failed', error=error)

    def server_detail(self, identifier):
        # Read the pinned library selection, never a newer, unchecked download.
        # Configuration reads do not contact providers or run client cores.
        with self.publish_guard:
            with self.guard:
                snapshot = self.snapshot
                config = self.store.config()
                if snapshot.get('sources_key') != self.sources_key(config):
                    return None
                server = next((node for node in snapshot.get('inventory', []) if node['id'] == identifier), None)
                if server is None:
                    return None
            matches = []
            for provider in config['providers']:
                if not provider['enabled'] or provider['name'] not in server['sources']:
                    continue
                saved = self.read_selection(provider, snapshot.get('active_selections', {}).get(provider['id']))
                if saved:
                    matches.extend(entry for entry in saved['entries'] if entry.identity[:16] == identifier)
            if not matches:
                return None
            entry = min(matches, key=lambda node: node.group_size)
            detail = {key: server[key] for key in ('id', 'name', 'protocol', 'sources')}
            detail.update({'fields': server_details.describe(entry),
                           'configurations': server_details.configurations(entry)})
            with self.guard:
                if snapshot.get('sources_key') != self.sources_key():
                    return None
            return detail

    def public_state(self, base_url, revision=None):
        self.ensure_publications()
        with self.guard:
            snapshot = self.snapshot
            config = self.store.config()
            sources_key = self.sources_key(config)
            subscriptions = self.public_subscriptions(base_url, ensure=False, sources_key=sources_key)
            refresh_job = self.jobs.items.get('refresh') if self.refresh_running else None
            refresh_ids = refresh_job.get('provider_ids') if refresh_job else []
            refreshing = [p['id'] for p in config['providers'] if p['enabled']
                          and (refresh_ids is None or p['id'] in refresh_ids)]
            providers = []
            for provider in config['providers']:
                status = self.provider_statuses.get(provider['id'], {'status': 'pending', 'count': 0, 'protocols': {}})
                providers.append(self.store.public_provider(provider) | {key: value for key, value in status.items()
                    if not key.startswith('_')} | {'selection_pending': bool(provider['enabled'] and status.get('_generation')
                        and status['_generation'] != snapshot.get('active_selections', {}).get(provider['id']))})
            public = {k: v for k, v in snapshot.items() if k not in {"providers", "exports", "connectivity", "publications",
                "sources_key", "next_check_batch", 'active_selections', 'selection_checks'}}
            current = self.publication_current(snapshot, sources_key)
            state_revision = self.state_revision + ':' + str(int(current))
            unchanged = revision == state_revision
            if not current:
                public['counts'] = {target: 0 for target in (*protocols.TARGETS, 'v2board', 'v2board_uri')}
                public['published_total'] = 0
                if not unchanged:
                    public['inventory'] = [{**server, 'clients': [], 'connectivity': {
                    core: {**value, 'status': 'stale', 'reason': 'Connection check expired'}
                    if value['status'] == 'passed' else value
                    for core, value in server.get('connectivity', {}).items()}}
                    for server in snapshot.get('inventory', [])]
            if unchanged:
                public.pop('inventory', None)
                public.pop('diagnostics', None)
            return public | {
                'revision': state_revision, 'unchanged': unchanged,
                'last_error': snapshot.get('last_error'),
                "providers": providers, "hwids": self.store.hwids(), "busy": self.busy,
                'refresh_running': self.refresh_running, 'check_running': self.check_running,
                'check_phase': self.check_phase if self.check_running else None,
                'refresh_provider_id': self.refresh_provider_id, 'check_provider_ids': self.check_provider_ids,
                'refresh_provider_ids': refreshing, 'refresh_scheduled': self.refresh_scheduled,
                'refresh_progress': dict(self.refresh_progress), 'check_queued': 'check' in self.jobs.items,
                "refresh_seconds": config["refresh_seconds"], "next_refresh": self.next_refresh,
                "check_seconds": config['check_seconds'], "next_check": self.next_check,
                "busy_action": self.busy_action, "busy_provider_id": self.busy_provider_id,
                "check_progress": self.check_progress,
                "subscriptions": subscriptions,
                "links": next(s['links'] for s in subscriptions if s['id'] == 'default')}

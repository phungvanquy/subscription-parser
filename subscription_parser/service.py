"""Fetch, normalize, cache, and publish complete subscription snapshots."""
from __future__ import annotations

import base64
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import copy
import hashlib
import json
from pathlib import Path
import secrets
import threading
import time


from . import common, connectivity, protocols, publication
from .fetch import fetch_subscription
from .debug_log import DebugLog
from .storage import Store
from .validation import checked_combine


class Busy(common.SubscriptionError):
    pass


class Manager:
    def __init__(self, root: Path, fetcher=fetch_subscription, checker=None):
        self.store = Store(root)
        self.logs = DebugLog(self.store.root)
        self.fetcher = fetcher
        self.checker = checker or connectivity.check_entries
        self.guard = threading.RLock()
        self.busy = False
        self.busy_action = None
        self.busy_provider_id = None
        self.check_progress = {'done': 0, 'total': 0}
        self.next_refresh = time.time()
        self.next_check = time.time()
        self.stop = threading.Event()
        self.worker = None
        self.previews = {}
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
            self.next_refresh = time.time() + self.store.config()['refresh_seconds']
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

    def request_fingerprint(self, provider):
        identifier = provider['hwid_id']
        request = [provider['url'], provider['user_agent'], identifier,
                   self.store.hwid_revision(identifier) if identifier else None]
        return hashlib.sha256(json.dumps(request).encode()).hexdigest()

    def sources_key(self, config=None):
        providers = (config or self.store.config())['providers']
        values = [[p['id'], p['name'], self.request_fingerprint(p)] for p in providers if p['enabled']]
        return hashlib.sha256(json.dumps(values).encode()).hexdigest()

    def _upgrade_cached_requests(self):
        # Old caches were keyed only by URL. Bind each to its original saved
        # assignment once, before any provider/device changes can be accepted.
        for provider in self.store.config()['providers']:
            path = self.store.directory / 'cache' / (provider['id'] + '.json')
            try:
                cache = json.loads(common.read_limited(path))
            except (FileNotFoundError, ValueError, common.SubscriptionError):
                continue
            if cache.get('fingerprint') == hashlib.sha256(provider['url'].encode()).hexdigest():
                cache['fingerprint'] = self.request_fingerprint(provider)
                common.atomic_write(path, common.json_text(cache))

    def fetch(self, provider, download=True):
        cache_path = self.store.directory / "cache" / (provider["id"] + ".json")
        fingerprint = self.request_fingerprint(provider)
        status = {"id": provider["id"], "status": "unavailable", "count": 0, "protocols": {}}
        entries = []
        stage = 'fetch'
        try:
            if not download:
                raise LookupError
            hwid = self.store.hwid_value(provider['hwid_id']) if provider['hwid_id'] is not None else None
            body, metadata = self.fetcher(provider["url"], hwid, provider["user_agent"], 25)
            stage = 'parse'
            entries = protocols.parse(body, provider["name"])
            stage = 'validate'
            accepted = checked_combine(entries, self.store.root)
            self.log_validation(accepted, provider['id'])
            if not any(accepted["counts"].values()):
                raise common.SubscriptionError("provider response has no client-accepted servers")
            common.atomic_write(cache_path, common.json_text({"fingerprint": fingerprint, "metadata": metadata,
                                                             "body": base64.b64encode(body).decode()}))
            status.update(status="fresh", fetched_at=metadata.get("fetched_at", common.now()))
        except (common.SubscriptionError, OSError, ValueError, LookupError, RecursionError) as exc:
            if not isinstance(exc, LookupError):
                status["error"] = str(exc) if isinstance(exc, common.SubscriptionError) else "provider could not be fetched or parsed"
                self.logs.write('provider_failed', provider_id=provider['id'], stage=stage, error=exc)
            try:
                cache = json.loads(common.read_limited(cache_path))
                if cache["fingerprint"] == fingerprint:
                    entries = protocols.parse(base64.b64decode(cache["body"], validate=True), provider["name"])
                    previous = self.snapshot.get("providers", {}).get(provider["id"], {})
                    status.update(status="cached" if download else previous.get("status", "cached"),
                                  fetched_at=cache["metadata"].get("fetched_at"))
                    if not download and previous.get("error"):
                        status["error"] = previous["error"]
                    if download:
                        self.logs.write('cache_used', provider_id=provider['id'])
            except (OSError, ValueError, KeyError, common.SubscriptionError, RecursionError) as error:
                if not isinstance(error, FileNotFoundError):
                    self.logs.write('cache_failed', provider_id=provider['id'], error=error)
        unique = {entry.identity: entry for entry in entries}
        status.update(count=len(unique), protocols=dict(Counter(e.protocol for e in unique.values())))
        return entries, status

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

    def refresh(self, selected=None, download=True, check=None, reuse_recent=False):
        started = time.monotonic()
        started_at = time.time()
        completed = False
        check = download if check is None else check
        operation = 'refresh' if download else 'connection check' if check else 'configuration update'
        stage = 'fetch'
        self.logs.write('run_started', operation=operation, provider_id=selected)
        try:
            with self.guard:
                self.busy = True
                self.busy_action = 'fetching' if download else 'checking' if check else 'updating'
                self.busy_provider_id = selected
                self.check_progress = {'done': 0, 'total': 0}
            config = self.store.config()
            batch_check = check and selected is None and (not reuse_recent or started_at >= self.next_check_batch)
            sources_key = self.sources_key(config)
            enabled = [p for p in config["providers"] if p["enabled"]]
            entries, statuses, by_provider = [], {}, {}
            with ThreadPoolExecutor(max_workers=4) as pool:
                jobs = [(provider, pool.submit(self.fetch, provider, download and (selected is None or provider["id"] == selected))) for provider in enabled]
                for provider, job in jobs:
                    found, status = job.result()
                    by_provider[provider['id']] = found
                    entries.extend(found)
                    statuses[provider["id"]] = status
            for provider in config["providers"]:
                if not provider["enabled"]:
                    statuses[provider["id"]] = {"id": provider["id"], "status": "disabled", "count": 0, "protocols": {}}
            stage = 'validate'
            # Build each distinct selection once. All links share upstream
            # downloads and exact-configuration connection-check results.
            scopes = {'all': entries}
            for subscription in self.store.subscriptions():
                key = publication.scope_key(subscription)
                if key in scopes:
                    continue
                selected_ids = set(subscription['provider_ids'])
                scopes[key] = copy.deepcopy([entry for provider in enabled if provider['id'] in selected_ids
                                            for entry in by_provider[provider['id']]])
            # Validation can reject an identity's native variant in one scope.
            # Keep selections independent before that mutation takes place.
            for key, nodes in list(scopes.items()):
                accepted = checked_combine(nodes, self.store.root)
                self.log_validation(accepted)
                scopes[key] = (nodes, accepted['validation'])
            results = self.snapshot.get('connectivity', {})
            checked = 0
            if check:
                stage = 'check'
                with self.guard:
                    self.busy_action = 'checking'
                def progress(done, total):
                    with self.guard:
                        self.check_progress = {'done': done, 'total': total}
                previous = results
                check_entries = [entry for nodes, _ in scopes.values() for entry in connectivity.unique_entries(nodes)]
                targets = None
                if selected is not None:
                    names = {provider['name'] for provider in enabled if provider['id'] == selected}
                    # Include the selected provider's own native variants, even
                    # when another provider supplies the default shared variant;
                    # preserve other providers' exact variants without probing.
                    check_entries = [entry for nodes, _ in scopes.values() for entry in nodes]
                    selected_entries = [entry for entry in check_entries if entry.source in names]
                    targets = set()
                    for entry in selected_entries:
                        for core in connectivity.CORES:
                            if core not in entry.outputs:
                                continue
                            try:
                                targets.add((entry.identity, core, connectivity.fingerprint(entry, core)))
                            except protocols.Error:
                                continue
                results = self.checker(check_entries, self.store.root, stop=self.stop,
                                       previous=results, progress=progress, targets=targets,
                                       reuse_seconds=(connectivity.BATCH_REUSE_SECONDS if batch_check else config['check_seconds'])
                                       if reuse_recent else 0)
                checked = self.log_checks(previous, results, changed_only=True)
            stage = 'publish'
            max_age = config['check_seconds'] * 2
            evaluated_at = time.time()
            generated = common.now()
            last_check = generated if checked else self.snapshot.get('last_check')
            last_download = max((stamp for stamp in [self.snapshot.get('last_download'),
                *(status.get('fetched_at') for status in statuses.values())] if stamp), default=None)
            next_batch = started_at + config['check_seconds'] if batch_check else self.next_check_batch
            publications, pending = {}, False
            for key, (nodes, validation) in scopes.items():
                published, waiting = publication.build(nodes, results, max_age, evaluated_at, generated, validation, last_check)
                publications[key] = published
                pending |= waiting
            combined = publications.pop('all')
            next_check = self.next_check
            if check and selected is None:
                next_check = connectivity.next_check_at(results, config['check_seconds'], batch_due=next_batch)
            elif check:
                next_check = connectivity.next_check_at(results, config['check_seconds'], batch_due=next_check)
            elif not check and pending:
                next_check = min(next_check, time.time() + 5)
            event = {"at": generated, "seconds": round(time.monotonic() - started, 2), "servers": combined["total"],
                     "published": combined['published_total'],
                     "fresh": sum(s["status"] == "fresh" for s in statuses.values()),
                     "cached": sum(s["status"] == "cached" for s in statuses.values()),
                     "action": "refresh" if download else "connection check" if check else "configuration updated"}
            if selected is not None:
                event['provider_id'] = selected
            event['checked'] = checked
            snapshot = {k: v for k, v in combined.items() if k not in protocols.TARGETS}
            snapshot.update(providers=statuses, publications=publications, connectivity=results, sources_key=sources_key,
                            last_download=last_download, next_check_batch=next_batch, next_check=next_check,
                            history=([event] + self.snapshot.get("history", []))[:20])
            common.atomic_write(self.snapshot_path, common.json_text(snapshot))
            with self.guard:
                self.snapshot = snapshot
                self.next_check_batch = next_batch
                self.next_check = next_check
                completed = True
            self.logs.write('run_completed', operation=operation, servers=event['servers'], published=event['published'],
                            cached=event['cached'], seconds=event['seconds'])
        except Exception as error:
            self.logs.write('run_failed', operation=operation, stage=stage, error=error)
            # Details can contain upstream secrets; expose only a fixed error.
            with self.guard:
                self.snapshot["last_error"] = "refresh failed; the last completed subscription is still available"
        finally:
            with self.guard:
                self.busy = False
                self.busy_action = None
                self.busy_provider_id = None
                config = self.store.config()
                if download and selected is None:
                    self.next_refresh = time.time() + config['refresh_seconds']
                if check and selected is None and not completed:
                    self.next_check = time.time() + 30

    def begin_refresh(self, selected=None, *, reuse_recent=False):
        with self.guard:
            if self.busy:
                raise Busy("a refresh is already running")
            if selected is not None:
                provider = next((p for p in self.store.config()['providers'] if p['id'] == selected), None)
                if provider is None:
                    raise common.SubscriptionError('provider does not exist')
                if not provider['enabled']:
                    raise common.SubscriptionError('provider is paused; enable it before refreshing')
            self.busy = True
            self.busy_action = 'fetching'
            self.busy_provider_id = selected
            self.check_progress = {'done': 0, 'total': 0}
            self.worker = threading.Thread(target=self.refresh, args=(selected,), kwargs={'reuse_recent': reuse_recent},
                                           daemon=True, name="subscription-refresh")
            self.worker.start()

    def begin_check(self, *, reuse_recent=False):
        with self.guard:
            if self.busy:
                raise Busy('wait for the current refresh or connection check')
            self.busy = True
            self.busy_action = 'checking'
            self.busy_provider_id = None
            self.check_progress = {'done': 0, 'total': 0}
            self.worker = threading.Thread(target=self.refresh, kwargs={'download': False, 'check': True, 'reuse_recent': reuse_recent},
                                           daemon=True, name='server-check')
            self.worker.start()

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

    def public_subscriptions(self, base_url):
        with self.guard:
            result = []
            sources_key = self.sources_key()
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
                if previous is None and len(config["providers"]) >= 50:
                    raise common.SubscriptionError("maximum of 50 providers reached")
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
                common.atomic_write(cache_path, common.json_text({'fingerprint': preview['fingerprint'],
                    'metadata': preview['metadata'], 'body': base64.b64encode(preview['body']).decode()}))
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
            if previous is None and len(config['providers']) >= 50:
                raise common.SubscriptionError('maximum of 50 providers reached')
            hwid = self.store.hwid_value(provider['hwid_id']) if provider['hwid_id'] is not None else None
            fingerprint = self.request_fingerprint(provider)
            self.busy = True
        try:
            body, metadata = self.fetcher(provider['url'], hwid, provider['user_agent'], 25)
            entries = protocols.parse(body, provider['name'])
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
                    'body': body, 'metadata': metadata, 'expires': time.monotonic() + 600}
            self.logs.write('provider_tested', provider_id=provider['id'], servers=len(entries))
            return {'test_token': token, 'counts': result['counts'], 'tested_at': common.now()}
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
            with self.guard:
                due = not self.busy and time.time() >= self.next_refresh
                check_due = not self.busy and time.time() >= self.next_check
            try:
                if due:
                    self.begin_refresh(reuse_recent=True)
                elif check_due:
                    self.begin_check(reuse_recent=True)
            except Busy:
                pass
            except Exception as error:
                self.logs.write('scheduler_failed', error=error)
                self.stop.wait(30)

    def public_state(self, base_url):
        with self.guard:
            snapshot = self.snapshot
            config = self.store.config()
            subscriptions = self.public_subscriptions(base_url)
            providers = [self.store.public_provider(p) | snapshot.get("providers", {}).get(p["id"], {"status": "pending", "count": 0, "protocols": {}}) for p in config["providers"]]
            public = {k: v for k, v in snapshot.items() if k not in {"providers", "exports", "connectivity", "publications", "sources_key", "next_check_batch"}}
            if not self.publication_current(snapshot):
                public['counts'] = {target: 0 for target in (*protocols.TARGETS, 'v2board', 'v2board_uri')}
                public['published_total'] = 0
                public['inventory'] = [{**server, 'clients': [], 'connectivity': {
                    core: {**value, 'status': 'stale', 'reason': 'Connection check expired'}
                    if value['status'] == 'passed' else value
                    for core, value in server.get('connectivity', {}).items()}}
                    for server in snapshot.get('inventory', [])]
            return public | {
                "providers": providers, "hwids": self.store.hwids(), "busy": self.busy,
                "refresh_seconds": config["refresh_seconds"], "next_refresh": self.next_refresh,
                "check_seconds": config['check_seconds'], "next_check": self.next_check,
                "busy_action": self.busy_action, "busy_provider_id": self.busy_provider_id,
                "check_progress": self.check_progress,
                "subscriptions": subscriptions,
                "links": next(s['links'] for s in subscriptions if s['id'] == 'default')}

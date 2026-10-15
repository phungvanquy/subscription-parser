"""Fetch, normalize, cache, and publish complete subscription snapshots."""
from __future__ import annotations

import base64
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import secrets
import threading
import time

import yaml

from . import common, connectivity, protocols, v2board
from .fetch import fetch_subscription
from .storage import Store
from .validation import checked_combine


class Busy(common.SubscriptionError):
    pass


class Manager:
    def __init__(self, root: Path, fetcher=fetch_subscription, checker=None):
        self.store = Store(root)
        self.fetcher = fetcher
        self.checker = checker or connectivity.check_entries
        self.guard = threading.RLock()
        self.busy = False
        self.busy_action = None
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
        if self.snapshot.get('generated_at'):
            self.next_refresh = time.time() + self.store.config()['refresh_seconds']
        if self.snapshot.get('connectivity_policy') != connectivity.POLICY_VERSION:
            # Legacy exports have never been connection-tested. Do not publish
            # them during the first background check after an upgrade.
            self.snapshot['exports'] = {}
            self.snapshot['counts'] = {}
            self.snapshot['published_total'] = 0
            for server in self.snapshot.get('inventory', []):
                server['clients'] = []
        self._upgrade_cached_requests()

    def request_fingerprint(self, provider):
        identifier = provider['hwid_id']
        request = [provider['url'], provider['user_agent'], identifier,
                   self.store.hwid_revision(identifier) if identifier else None]
        return hashlib.sha256(json.dumps(request).encode()).hexdigest()

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
        try:
            if not download:
                raise LookupError
            hwid = self.store.hwid_value(provider['hwid_id']) if provider['hwid_id'] is not None else None
            body, metadata = self.fetcher(provider["url"], hwid, provider["user_agent"], 25)
            entries = protocols.parse(body, provider["name"])
            accepted = checked_combine(entries, self.store.root)
            if not any(accepted["counts"].values()):
                raise common.SubscriptionError("provider response has no client-accepted servers")
            common.atomic_write(cache_path, common.json_text({"fingerprint": fingerprint, "metadata": metadata,
                                                             "body": base64.b64encode(body).decode()}))
            status.update(status="fresh", fetched_at=metadata.get("fetched_at", common.now()))
        except (common.SubscriptionError, OSError, ValueError, LookupError, RecursionError) as exc:
            if not isinstance(exc, LookupError):
                status["error"] = str(exc) if isinstance(exc, common.SubscriptionError) else "provider could not be fetched or parsed"
            try:
                cache = json.loads(common.read_limited(cache_path))
                if cache["fingerprint"] == fingerprint:
                    entries = protocols.parse(base64.b64decode(cache["body"], validate=True), provider["name"])
                    previous = self.snapshot.get("providers", {}).get(provider["id"], {})
                    status.update(status="cached" if download else previous.get("status", "cached"),
                                  fetched_at=cache["metadata"].get("fetched_at"))
                    if not download and previous.get("error"):
                        status["error"] = previous["error"]
            except (OSError, ValueError, KeyError, common.SubscriptionError, RecursionError):
                pass
        unique = {entry.identity: entry for entry in entries}
        status.update(count=len(unique), protocols=dict(Counter(e.protocol for e in unique.values())))
        return entries, status

    def refresh(self, selected=None, download=True, check=None):
        started = time.monotonic()
        check = download if check is None else check
        try:
            with self.guard:
                self.busy_action = 'fetching' if download else 'checking' if check else 'updating'
            config = self.store.config()
            enabled = [p for p in config["providers"] if p["enabled"]]
            entries, statuses = [], {}
            with ThreadPoolExecutor(max_workers=4) as pool:
                jobs = [(provider, pool.submit(self.fetch, provider, download and (selected is None or provider["id"] == selected))) for provider in enabled]
                for provider, job in jobs:
                    found, status = job.result()
                    entries.extend(found)
                    statuses[provider["id"]] = status
            for provider in config["providers"]:
                if not provider["enabled"]:
                    statuses[provider["id"]] = {"id": provider["id"], "status": "disabled", "count": 0, "protocols": {}}
            accepted = checked_combine(entries, self.store.root)
            results = self.snapshot.get('connectivity', {})
            if check:
                with self.guard:
                    self.busy_action = 'checking'
                def progress(done, total):
                    with self.guard:
                        self.check_progress = {'done': done, 'total': total}
                results = self.checker(entries, self.store.root, stop=self.stop,
                                       previous=results, progress=progress)
            max_age = config['check_seconds'] * 2
            evaluated_at = time.time()
            filtered = connectivity.filtered_entries(entries, results, max_age, evaluated_at)
            combined = protocols.combine(filtered)
            combined['validation'] = accepted['validation']
            uri_feed = v2board.export(filtered)
            combined['counts']['v2board'] = uri_feed['count']
            combined['diagnostics'].extend(uri_feed['diagnostics'])
            for server in combined['inventory']:
                if server['id'] in uri_feed['identities']:
                    server['clients'].append('v2board')
            unique = {entry.identity[:16]: entry for entry in connectivity.unique_entries(entries)}
            expiries, pending = [], False
            for server in combined['inventory']:
                entry = unique[server['id']]
                health = {core: connectivity.current_result(entry, core, results, max_age, evaluated_at) for core in connectivity.CORES}
                server['connectivity'] = {core: {k: v for k, v in value.items() if k not in {'fingerprint', 'checked_ts'}}
                                          for core, value in health.items()}
                pending |= any(value['status'] in {'pending', 'stale'} for value in health.values())
                expiries.extend(value['checked_ts'] + max_age for value in health.values() if value['status'] == 'passed')
            combined['published_total'] = sum(bool(server['clients']) for server in combined['inventory'])
            combined['connectivity_policy'] = connectivity.POLICY_VERSION
            combined['evaluated_at'] = evaluated_at
            combined['valid_until'] = min(expiries) if expiries else time.time() + max_age
            combined['connectivity'] = {entry.identity: results.get(entry.identity, {}) for entry in unique.values()}
            combined['last_check'] = common.now() if check else self.snapshot.get('last_check')
            generated = common.now()
            exports = {"hiddify.json": common.json_text(combined["hiddify"]),
                       "mihomo.yaml": yaml.safe_dump(combined["mihomo"], allow_unicode=True, sort_keys=False),
                       "xray.json": common.json_text(combined["xray"]),
                       "v2board.txt": uri_feed['body']}
            exports["combined.json"] = common.json_text({"schema_version": 2, "generated_at": generated,
                "servers": [server for server in combined['inventory'] if server['clients']],
                "excluded_servers": [server for server in combined['inventory'] if not server['clients']],
                "hiddify": combined["hiddify"], "mihomo": combined["mihomo"], "xray": combined["xray"],
                "diagnostics": combined["diagnostics"]})
            event = {"at": generated, "seconds": round(time.monotonic() - started, 2), "servers": combined["total"],
                     "published": combined['published_total'],
                     "fresh": sum(s["status"] == "fresh" for s in statuses.values()),
                     "cached": sum(s["status"] == "cached" for s in statuses.values()),
                     "action": "refresh" if download else "connection check" if check else "configuration updated"}
            snapshot = {k: v for k, v in combined.items() if k not in protocols.TARGETS}
            snapshot.update(generated_at=generated, providers=statuses, exports=exports,
                            history=([event] + self.snapshot.get("history", []))[:20])
            common.atomic_write(self.snapshot_path, common.json_text(snapshot))
            with self.guard:
                self.snapshot = snapshot
                if not check and pending:
                    # Let consecutive saves in a bulk import finish first.
                    self.next_check = time.time() + 5
        except Exception:
            # Details can contain upstream secrets; expose only a fixed error.
            with self.guard:
                self.snapshot["last_error"] = "refresh failed; the last completed subscription is still available"
        finally:
            with self.guard:
                self.busy = False
                self.busy_action = None
                config = self.store.config()
                if download:
                    self.next_refresh = time.time() + config['refresh_seconds']
                if check:
                    self.next_check = time.time() + config['check_seconds']

    def begin_refresh(self, selected=None):
        with self.guard:
            if self.busy:
                raise Busy("a refresh is already running")
            if selected and selected not in {p["id"] for p in self.store.config()["providers"]}:
                raise common.SubscriptionError("provider does not exist")
            self.busy = True
            self.busy_action = 'fetching'
            self.worker = threading.Thread(target=self.refresh, args=(selected,), daemon=True, name="subscription-refresh")
            self.worker.start()

    def begin_check(self):
        with self.guard:
            if self.busy:
                raise Busy('wait for the current refresh or connection check')
            self.busy = True
            self.busy_action = 'checking'
            self.check_progress = {'done': 0, 'total': 0}
            self.worker = threading.Thread(target=self.refresh, kwargs={'download': False, 'check': True},
                                           daemon=True, name='server-check')
            self.worker.start()

    @staticmethod
    def publication_current(snapshot):
        return (snapshot.get('connectivity_policy') == connectivity.POLICY_VERSION
                and snapshot.get('evaluated_at', 0) <= time.time() < snapshot.get('valid_until', 0))

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
        if 'check_seconds' in values:
            self.refresh(download=False, check=False)
            with self.guard:
                self.next_check = time.time()

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
            if not any(result['counts'].values()):
                raise common.SubscriptionError('no compatible servers were returned; check the subscription and device ID')
            token = secrets.token_urlsafe(24)
            with self.guard:
                self.previews = {key: p for key, p in self.previews.items() if p['expires'] > time.monotonic()}
                while self.previews and (len(self.previews) >= 50 or sum(len(p['body']) for p in self.previews.values()) + len(body) > 64 * 1024 * 1024):
                    self.previews.pop(next(iter(self.previews)))
                self.previews[token] = {'provider': provider, 'previous': previous, 'fingerprint': fingerprint,
                    'body': body, 'metadata': metadata, 'expires': time.monotonic() + 600}
            return {'test_token': token, 'counts': result['counts'], 'tested_at': common.now()}
        except common.SubscriptionError as error:
            message = str(error).replace(provider['url'], '[subscription]')
            if hwid:
                message = message.replace(hwid, '[device ID]')
            raise common.SubscriptionError(message) from None
        except (OSError, ValueError, RecursionError):
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
            with self.guard:
                due = not self.busy and time.time() >= self.next_refresh
                check_due = not self.busy and time.time() >= self.next_check
            try:
                if due:
                    self.begin_refresh()
                elif check_due:
                    self.begin_check()
            except Busy:
                pass

    def public_state(self, base_url):
        with self.guard:
            snapshot = self.snapshot
            config = self.store.config()
            token = self.store.credential('subscription_token')
            providers = [self.store.public_provider(p) | snapshot.get("providers", {}).get(p["id"], {"status": "pending", "count": 0, "protocols": {}}) for p in config["providers"]]
            public = {k: v for k, v in snapshot.items() if k not in {"providers", "exports", "connectivity"}}
            if not self.publication_current(snapshot):
                public['counts'] = {target: 0 for target in (*protocols.TARGETS, 'v2board')}
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
                "busy_action": self.busy_action, "check_progress": self.check_progress,
                "links": {name: f"{base_url.rstrip('/')}/sub/{token}/{name}" for name in ("hiddify.json", "mihomo.yaml", "xray.json", "v2board.txt", "combined.json")}}

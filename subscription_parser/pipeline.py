"""Durable job intents and exact-configuration results for independent workers."""
from __future__ import annotations

import json
import secrets

from . import common, connectivity, protocols


class Jobs:
    """The manager's guard serializes this small, atomically replaced ledger."""

    def __init__(self, directory):
        self.path = directory / 'jobs.json'
        self.items = {}
        if self.path.exists():
            value = json.loads(common.read_limited(self.path))
            if not isinstance(value, dict) or any(key not in {'refresh', 'check'} for key in value):
                raise ValueError('invalid job ledger')
            for job in value.values():
                if (not isinstance(job, dict) or not isinstance(job.get('id'), str)
                        or not isinstance(job.get('provider_ids'), (list, type(None)))
                        or any(not isinstance(identifier, str) for identifier in job.get('provider_ids') or [])):
                    raise ValueError('invalid saved job')
            self.items = value

    def put(self, kind, **fields):
        job = {'id': secrets.token_hex(8), **fields}
        updated = {**self.items, kind: job}
        common.atomic_write(self.path, common.json_text(updated))
        self.items = updated
        return job

    def clear(self, kind, job):
        if self.items.get(kind, {}).get('id') != job['id']:
            return False
        updated = {key: value for key, value in self.items.items() if key != kind}
        common.atomic_write(self.path, common.json_text(updated))
        self.items = updated
        return True


def configurations(entries):
    """Configuration fingerprints include the core and all native dependencies."""
    values = set()
    for entry in entries:
        for core in connectivity.CORES:
            if core in entry.outputs:
                try:
                    values.add((entry.identity, core, connectivity.fingerprint(entry, core)))
                except protocols.Error:
                    pass
    return values


def merge_results(previous, incoming, allowed):
    merged = {identity: dict(cores) for identity, cores in previous.items()}
    for record_id, cores in incoming.items():
        identity = record_id.split(':')[0]
        for core, value in cores.items():
            key = value.get('fingerprint')
            if (identity, core, key) not in allowed:
                continue
            existing = merged.setdefault(identity, {}).get(core, {})
            target = identity if existing.get('fingerprint') in {None, key} else identity + ':' + key
            merged.setdefault(target, {})[core] = value
    return merged


def retained_results(results, configurations):
    """Bound saved health to the active and candidate configurations only."""
    return {identity: retained for identity, cores in results.items()
            if (retained := {core: value for core, value in cores.items()
                if (identity.split(':')[0], core, value.get('fingerprint')) in configurations})}

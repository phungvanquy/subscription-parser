"""Build a feed from exactly the selected providers and shared check results."""
from __future__ import annotations

import hashlib
import json

import yaml

from . import common, connectivity, protocols, v2board

FILES = ('hiddify.json', 'mihomo.yaml', 'xray.json', 'v2board.txt', 'combined.json')


def scope_key(subscription):
    if subscription['all_providers']:
        return 'all'
    return hashlib.sha256(json.dumps(sorted(subscription['provider_ids'])).encode()).hexdigest()


def build(entries, results, max_age, evaluated_at, generated, validation, last_check):
    filtered = connectivity.filtered_entries(entries, results, max_age, evaluated_at)
    combined = protocols.combine(filtered)
    combined['validation'] = validation
    uri_feed = v2board.export(filtered)
    combined['counts']['v2board'] = uri_feed['count']
    combined['diagnostics'].extend(uri_feed['diagnostics'])
    unique = {entry.identity[:16]: entry for entry in connectivity.unique_entries(entries)}
    expiries, pending = [], False
    for server in combined['inventory']:
        if server['id'] in uri_feed['identities']:
            server['clients'].append('v2board')
        entry = unique[server['id']]
        health = {core: connectivity.current_result(entry, core, results, max_age, evaluated_at)
                  for core in connectivity.CORES}
        server['connectivity'] = {core: {k: v for k, v in value.items() if k not in {'fingerprint', 'checked_ts'}}
                                  for core, value in health.items()}
        pending |= any(value['status'] in {'pending', 'stale'} for value in health.values())
        expiries.extend(value['checked_ts'] + max_age for value in health.values() if value['status'] == 'passed')
    combined['published_total'] = sum(bool(server['clients']) for server in combined['inventory'])
    combined['connectivity_policy'] = connectivity.POLICY_VERSION
    combined['evaluated_at'] = evaluated_at
    combined['valid_until'] = min(expiries) if expiries else evaluated_at + max_age
    combined['last_check'] = last_check
    exports = {'hiddify.json': common.json_text(combined['hiddify']),
               'mihomo.yaml': yaml.safe_dump(combined['mihomo'], allow_unicode=True, sort_keys=False),
               'xray.json': common.json_text(combined['xray']), 'v2board.txt': uri_feed['body']}
    exports['combined.json'] = common.json_text({'schema_version': 2, 'generated_at': generated,
        'servers': [server for server in combined['inventory'] if server['clients']],
        'excluded_servers': [server for server in combined['inventory'] if not server['clients']],
        'hiddify': combined['hiddify'], 'mihomo': combined['mihomo'], 'xray': combined['xray'],
        'diagnostics': combined['diagnostics']})
    return {k: v for k, v in combined.items() if k not in protocols.TARGETS} | {
        'generated_at': generated, 'exports': exports}, pending

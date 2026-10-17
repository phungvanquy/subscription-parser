#!/usr/bin/env python3
"""Measure library overhead with synthetic providers and instant passing probes.

No real subscriptions or network access are used. Client validation is mocked;
these numbers measure application overhead, not live connection throughput.
"""
import argparse
import inspect
import json
import os
from pathlib import Path
import resource
import sys
import tempfile
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from subscription_parser import common, connectivity, protocols, service


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--providers', type=int, default=50)
    parser.add_argument('--nodes', type=int, default=10000)
    args = parser.parse_args()
    if not 1 <= args.providers <= 200 or not args.providers <= args.nodes <= 100000:
        parser.error('use 1–200 providers and between providers and 100000 nodes')
    providers, sources = [], {}
    for index in range(args.providers):
        url = f'https://provider{index}.invalid/sub'
        providers.append({'id': f'p{index}', 'name': f'Provider {index}', 'url': url,
                          'hwid_id': None, 'user_agent': 'synthetic', 'enabled': True})
        count = args.nodes // args.providers + (index < args.nodes % args.providers)
        sources[url] = json.dumps({'outbounds': [
            {'type': 'vless', 'tag': f'Node {j}', 'server': f'n{index}-{j}.invalid',
             'server_port': 443, 'uuid': '01234567-89ab-4cde-8fab-0123456789ab'}
            for j in range(count)]}).encode()
    stats = {}
    def download(url, *args):
        stats['downloads'] += 1
        return sources[url], {'fetched_at': common.now()}
    def validate(nodes, root):
        stats['validations'] += 1
        return protocols.combine(nodes) | {'validation': {}}
    def tester(*args):
        stats['probes'] += 1
        return {'status': 'passed', 'latency_ms': 1}
    def checker(nodes, root, **kwargs):
        return connectivity.check_entries(nodes, root, tester=tester, **kwargs)
    report = {'providers': args.providers, 'nodes': args.nodes, 'mocked_network_and_cores': True}
    with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ,
            {'PARSER_ADMIN_TOKEN': 'synthetic-benchmark'}, clear=True), \
            patch.object(service, 'checked_combine', side_effect=validate):
        manager = service.Manager(Path(folder), download, checker)
        config = manager.store.config()
        # Populate the fixture in one transaction, without repeated UI rebuilds.
        config['providers'] = providers
        manager.store.save_config(config)
        for phase, options in [('initial_refresh', {}), ('reuse_recent', {
                'download': False, 'check': True, 'reuse_recent': True})]:
            stats.update(downloads=0, validations=0, probes=0)
            started = time.monotonic()
            manager.refresh(**options)
            if manager.snapshot.get('last_error'):
                raise RuntimeError(manager.snapshot['last_error'])
            report[phase] = dict(stats, seconds=round(time.monotonic() - started, 3))
        started = time.monotonic()
        state = manager.public_state('https://synthetic.invalid')
        state_bytes = len(json.dumps(state).encode())
        report['state'] = {'bytes': state_bytes, 'seconds': round(time.monotonic() - started, 3)}
        if 'revision' in inspect.signature(manager.public_state).parameters:
            report['state']['progress_bytes'] = len(json.dumps(manager.public_state(
                'https://synthetic.invalid', revision=state['revision'])).encode())
        report['snapshot_bytes'] = manager.snapshot_path.stat().st_size
        report['peak_rss_MiB'] = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()

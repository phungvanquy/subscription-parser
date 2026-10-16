"""Bounded HTTPS checks through each isolated client core."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import copy
import hashlib
import http.client
import json
import os
from pathlib import Path
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit

import yaml

from . import common, protocols

CORES = ('mihomo', 'xray', 'hiddify')
URLS = ('https://www.gstatic.com/generate_204', 'https://cp.cloudflare.com/generate_204')
REQUEST_TIMEOUT = 8
START_TIMEOUT = 5
BATCH_TIMEOUT = 300
WORKERS = 4
POLICY_VERSION = 2


def unique_entries(entries):
    unique = {}
    for entry in entries:
        if entry.identity not in unique or entry.group_size < unique[entry.identity].group_size:
            unique[entry.identity] = entry
    return list(unique.values())


def fingerprint(entry, target):
    # Includes the exact converted settings and every dependency, not just a name.
    document = protocols.bundle(entry, target, 'probe')
    value = json.dumps([POLICY_VERSION, target, document], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(value.encode()).hexdigest()


def result(status, reason=None, **fields):
    value = {'status': status, **fields}
    if reason:
        value['reason'] = reason
    return value


def matching_result(identity, target, key, results):
    # The primary variant can change when providers or selections are reordered.
    # Always match the configuration, including for preserved and deferred checks.
    for record_id in (identity, identity + ':' + key):
        value = results.get(record_id, {}).get(target, {})
        if value.get('fingerprint') == key:
            return value
    return {}


def current_result(entry, target, results, max_age, now=None):
    if target not in entry.outputs:
        return result('unsupported', 'No compatible configuration for this core')
    try:
        key = fingerprint(entry, target)
    except protocols.Error:
        return result('unsupported', 'Configuration dependencies are unavailable')
    value = matching_result(entry.identity, target, key, results)
    if not value:
        return result('pending', 'Waiting for a connection check')
    if not value.get('checked_ts'):
        return value
    now = time.time() if now is None else now
    checked = value.get('checked_ts', 0)
    if not 0 <= now - checked < max_age:
        return {**value, 'status': 'stale', 'reason': 'Connection check expired'}
    return value


def filtered_entries(entries, results, max_age, now=None):
    now = time.time() if now is None else now
    filtered = copy.deepcopy(entries)
    for entry in filtered:
        statuses = {core: current_result(entry, core, results, max_age, now) for core in CORES}
        for core, value in statuses.items():
            if value['status'] != 'passed':
                entry.outputs.pop(core, None)
                entry.issues[core] = value.get('reason', 'Connection check did not pass')
    return filtered


def configuration(entry, target, port):
    nodes = protocols.bundle(entry, target, 'probe')
    if target == 'mihomo':
        return {'port': port, 'allow-lan': False, 'bind-address': '127.0.0.1',
                'mode': 'rule', 'log-level': 'silent', 'find-process-mode': 'off',
                'profile': {'store-selected': False, 'store-fake-ip': False},
                'proxies': nodes, 'rules': ['MATCH,probe']}
    if target == 'hiddify':
        return {'log': {'disabled': True},
                'dns': {'servers': [{'type': 'local', 'tag': 'local'}]},
                'inbounds': [{'type': 'mixed', 'tag': 'probe-in', 'listen': '127.0.0.1', 'listen_port': port}],
                'outbounds': nodes,
                'route': {'final': 'probe', 'default_domain_resolver': 'local',
                          'rules': [{'inbound': ['probe-in'], 'action': 'route', 'outbound': 'probe'}]}}
    return {'log': {'loglevel': 'none'},
            'inbounds': [{'tag': 'probe-in', 'listen': '127.0.0.1', 'port': port,
                          'protocol': 'http', 'settings': {}}],
            'outbounds': [{'tag': 'blocked', 'protocol': 'blackhole', 'settings': {}}, *nodes],
            'routing': {'rules': [{'type': 'field', 'inboundTag': ['probe-in'], 'outboundTag': 'probe'}]}}


def request(port, url):
    """Explicit CONNECT + verified TLS: environment proxies cannot bypass the node."""
    target = urlsplit(url)
    if target.scheme != 'https' or target.username or target.password or not target.hostname:
        raise ValueError('HTTPS probe endpoint required')
    connection = http.client.HTTPSConnection('127.0.0.1', port, timeout=REQUEST_TIMEOUT,
                                            context=ssl.create_default_context())
    connection.set_tunnel(target.hostname, target.port or 443)
    started = time.monotonic()
    try:
        connection.request('GET', target.path + ('?' + target.query if target.query else ''),
                           headers={'User-Agent': 'subscription-parser-connectivity/1.0', 'Connection': 'close'})
        response = connection.getresponse()
        return {'status_code': response.status, 'latency_ms': max(1, round((time.monotonic() - started) * 1000))}
    finally:
        connection.close()


def terminate(process):
    if process is None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=2)


def probe(entry, target, root, stop, deadline, urls=URLS):
    executable = Path(os.environ.get('PARSER_TOOLS_DIR') or root / '.tools') / ('hiddify-core' if target == 'hiddify' else target)
    if not executable.is_file():
        return result('error', 'Client core is not installed')
    process = None
    try:
        with tempfile.TemporaryDirectory(prefix='connection-', dir=root / '.parser-state') as temporary:
            folder = Path(temporary)
            with socket.socket() as listener:
                listener.bind(('127.0.0.1', 0))
                port = listener.getsockname()[1]
            document = configuration(entry, target, port)
            path = folder / ('config.yaml' if target == 'mihomo' else 'config.json')
            common.atomic_write(path, yaml.safe_dump(document) if target == 'mihomo' else common.json_text(document))
            command = ([str(executable), '-d', str(folder), '-f', str(path)] if target == 'mihomo'
                       else [str(executable), 'srun', '-c', str(path)] if target == 'hiddify'
                       else [str(executable), 'run', '-config', str(path)])
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, cwd=folder, start_new_session=True)
            ready_by = min(deadline, time.monotonic() + START_TIMEOUT)
            while True:
                if stop.is_set() or time.monotonic() >= deadline:
                    return result('error', 'Check interrupted; retry scheduled')
                if process.poll() is not None:
                    return result('error', 'Client core could not start this configuration')
                try:
                    with socket.create_connection(('127.0.0.1', port), timeout=0.1):
                        break
                except OSError:
                    if time.monotonic() >= ready_by:
                        return result('error', 'Client core startup timed out')
                    stop.wait(0.05)
            attempts = 0
            for url in urls:
                if stop.is_set() or time.monotonic() >= deadline:
                    return result('error', 'Check interrupted; retry scheduled', attempts=attempts)
                attempts += 1
                # A child bounds the entire CONNECT/TLS/HTTP exchange, including
                # peers that trickle bytes indefinitely. Never print core logs.
                timeout = min(REQUEST_TIMEOUT, max(0.01, deadline - time.monotonic()))
                try:
                    response = subprocess.run([sys.executable, '-m', 'subscription_parser.connectivity',
                                               '--request', str(port), url], stdin=subprocess.DEVNULL,
                                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                              timeout=timeout, cwd=Path(__file__).resolve().parent.parent)
                    value = json.loads(response.stdout) if response.returncode == 0 else {}
                    if value.get('status_code') == 204:
                        return result('passed', latency_ms=value['latency_ms'], attempts=attempts,
                                      endpoint=urlsplit(url).hostname)
                except (subprocess.TimeoutExpired, ValueError):
                    pass
                if process.poll() is not None:
                    return result('error', 'Client core stopped during the check', attempts=attempts)
            return result('failed', 'HTTPS request failed or returned an unexpected response on both endpoints',
                          attempts=attempts)
    except (OSError, protocols.Error, subprocess.SubprocessError):
        return result('error', 'Connection checker could not run this configuration')
    finally:
        terminate(process)


def next_check_at(results, interval, now=None):
    now = time.time() if now is None else now
    deadlines = []
    for values in results.values():
        for value in values.values():
            if value.get('status') == 'unsupported':
                continue
            checked = value.get('checked_ts', 0)
            complete = value.get('status') in {'passed', 'failed', 'error'} and 0 < checked <= now
            deadlines.append(checked + interval if complete else now)
    # Pending/deferred batches get a short backoff; recent results retain their
    # individual deadlines instead of being postponed by unrelated new servers.
    return max(now + 5, min(deadlines, default=now + interval))


def check_entries(entries, root, *, stop=None, previous=None, progress=None, tester=probe,
                  targets=None, reuse_seconds=0):
    stop = stop or threading.Event()
    previous = previous or {}
    records, jobs, seen = {}, [], set()
    evaluated_at = time.time()
    for entry in [*unique_entries(entries), *entries]:
        records.setdefault(entry.identity, {})
        for core in CORES:
            if core not in entry.outputs:
                records[entry.identity].setdefault(core, result('unsupported', 'No compatible configuration for this core'))
                continue
            try:
                key = fingerprint(entry, core)
            except protocols.Error:
                records[entry.identity].setdefault(core, result('unsupported', 'Configuration dependencies are unavailable'))
                continue
            if (entry.identity, core, key) in seen:
                continue
            seen.add((entry.identity, core, key))
            record_id = entry.identity if core not in records[entry.identity] else entry.identity + ':' + key
            cached = matching_result(entry.identity, core, key, previous)
            current = (cached.get('status') in {'passed', 'failed', 'error'}
                       and 0 < cached.get('checked_ts', 0) <= evaluated_at
                       and evaluated_at - cached['checked_ts'] < reuse_seconds)
            selected = targets is None or (entry.identity, core, key) in targets
            records.setdefault(record_id, {})[core] = (
                dict(cached) if cached and (not selected or current) else result('pending', fingerprint=key))
            if selected and not current:
                jobs.append((entry, core, key, record_id, cached))
    # Oldest/never-checked nodes go first so large lists cannot starve at the tail.
    jobs.sort(key=lambda job: job[4].get('checked_ts', 0))
    deadline = time.monotonic() + BATCH_TIMEOUT
    def run(job):
        entry, core, key, record_id, cached = job
        if stop.is_set() or time.monotonic() >= deadline:
            if (cached.get('status') == 'passed' and cached.get('fingerprint') == key
                    and 0 < cached.get('checked_ts', 0) <= time.time()):
                # Keep the original timestamp: publication still expires this
                # result normally. A deferred job must not erase a valid check
                # or make it appear to have just been tested.
                return dict(cached)
            return result('pending', 'Batch time limit reached; retry scheduled', fingerprint=key)
        try:
            value = tester(entry, core, root, stop, deadline)
        except Exception:
            value = result('error', 'Connection checker failed; retry scheduled')
        return {**value, 'fingerprint': key, 'checked_at': common.now(), 'checked_ts': time.time()}
    if progress:
        progress(0, len(jobs))
    with ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix='connection-check') as pool:
        futures = {pool.submit(run, job): job for job in jobs}
        for done, future in enumerate(as_completed(futures), 1):
            _, core, _, record_id, _ = futures[future]
            records[record_id][core] = future.result()
            if progress:
                progress(done, len(jobs))
    return records


if __name__ == '__main__':
    try:
        if len(sys.argv) != 4 or sys.argv[1] != '--request':
            raise ValueError
        print(json.dumps(request(int(sys.argv[2]), sys.argv[3])))
    except Exception:
        # Exception strings may contain endpoint details; the caller needs only failure.
        sys.exit(1)

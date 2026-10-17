"""Scheduling, availability and bounded resource use with a large library."""
from concurrent.futures import ThreadPoolExecutor
import copy
import json
import os
from pathlib import Path
import http.server
import ssl
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from subscription_parser import common, connectivity as c, fetch, protocols, publication, service
from subscription_parser.app import create_app
from scripts.connectivity_fixture import fixture

UUID = '01234567-89ab-4cde-8fab-0123456789ab'


def body(hosts):
    return json.dumps({'outbounds': [
        {'type': 'vless', 'tag': host, 'server': host, 'server_port': 443, 'uuid': UUID}
        for host in hosts]}).encode()


class QueueTests(unittest.TestCase):
    def test_failed_and_error_results_do_not_starve_never_checked_nodes(self):
        nodes = protocols.parse(body([f'n{i}.example' for i in range(100)]), 'Synthetic')
        for status in ('failed', 'error'):
            with self.subTest(status=status):
                clock, seen, previous = [10000.0], set(), {}
                next_batch = clock[0]
                def tester(node, core, root, stop, deadline):
                    seen.add((node.identity, core))
                    clock[0] = min(deadline, clock[0] + 16)
                    return c.result(status)
                with patch.dict(os.environ, {}, clear=True), patch.object(c, 'WORKERS', 1), \
                        patch.object(c, 'BATCH_TIMEOUT', 300), \
                        patch.object(c.time, 'time', side_effect=lambda: clock[0]), \
                        patch.object(c.time, 'monotonic', side_effect=lambda: clock[0]):
                    for _ in range(20):
                        regular = clock[0] >= next_batch
                        if regular:
                            next_batch = clock[0] + 600
                        previous = c.check_entries(nodes, Path('/unused'), previous=previous,
                            tester=tester, reuse_seconds=60 if regular else 600)
                        clock[0] = c.next_check_at(previous, 600, batch_due=next_batch)
                self.assertEqual(len(seen), 300)

    def test_only_active_checks_are_submitted_and_stop_leaves_the_queue_pending(self):
        nodes = protocols.parse(body([f'n{i}.example' for i in range(1000)]), 'Synthetic')
        ready, release, stop = threading.Event(), threading.Event(), threading.Event()
        submitted, outcomes, errors = [], [], []
        class Pool(ThreadPoolExecutor):
            def submit(self, *args, **kwargs):
                submitted.append(True)
                return super().submit(*args, **kwargs)
        def tester(*args):
            if len(submitted) == 3:
                ready.set()
            release.wait(5)
            return c.result('passed')
        def run():
            try:
                outcomes.append(c.check_entries(nodes, Path('/unused'), stop=stop, tester=tester))
            except Exception as error:
                errors.append(error)
        with patch.dict(os.environ, {'PARSER_CHECK_WORKERS': '3'}), patch.object(c, 'ThreadPoolExecutor', Pool):
            worker = threading.Thread(target=run)
            worker.start()
            try:
                self.assertTrue(ready.wait(5))
                self.assertEqual(len(submitted), 3)
            finally:
                stop.set()
                release.set()
                worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertFalse(errors)
        self.assertEqual(len(submitted), 3)
        self.assertEqual(sum(value['status'] == 'pending' for cores in outcomes[0].values()
                             for value in cores.values()), 2997)


class DownloadDeadlineTests(unittest.TestCase):
    @unittest.skipUnless(Path('/opt/client-cores/xray').is_file(), 'local TLS fixture requires the development image')
    def test_https_worker_preserves_headers_and_bounds_a_trickling_response(self):
        seen, slow = [], threading.Event()
        payload = body(['local.example'])
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(dict(self.headers))
                self.send_response(200)
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                try:
                    if slow.is_set():
                        for byte in payload:
                            self.wfile.write(bytes([byte]))
                            self.wfile.flush()
                            time.sleep(0.05)
                    else:
                        self.wfile.write(payload)
                except (OSError, ssl.SSLError):
                    pass
            def log_message(self, *args):
                pass
        with tempfile.TemporaryDirectory() as folder, fixture(folder, '/opt/client-cores/xray') as local:
            local['server'].RequestHandlerClass = Handler
            with patch.dict(os.environ, {'SSL_CERT_FILE': str(local['certificate']), 'NO_PROXY': '127.0.0.1'}):
                url = f"https://127.0.0.1:{local['server'].server_port}/private"
                downloaded, metadata = fetch.bounded_fetch_subscription(url, 'authorized-device', 'fixture-agent', 5)
                self.assertEqual(downloaded, payload)
                self.assertEqual(metadata['http_status'], 200)
                self.assertEqual(seen[0]['X-Hwid'], 'authorized-device')
                self.assertEqual(seen[0]['User-Agent'], 'fixture-agent')
                slow.set()
                started = time.monotonic()
                with self.assertRaisesRegex(common.SubscriptionError, 'total time limit'):
                    fetch.bounded_fetch_subscription(url, None, 'fixture-agent', 1)
                self.assertLess(time.monotonic() - started, 3)

    def test_total_deadline_kills_and_reaps_a_stuck_download_worker(self):
        with tempfile.TemporaryDirectory() as folder:
            pidfile = Path(folder) / 'pid'
            command = (sys.executable, '-c',
                'import os, pathlib, time; pathlib.Path(' + repr(str(pidfile)) +
                ').write_text(str(os.getpid())); time.sleep(30)')
            started = time.monotonic()
            with patch.object(fetch, 'WORKER_COMMAND', command):
                with self.assertRaisesRegex(common.SubscriptionError, 'total time limit'):
                    fetch.bounded_fetch_subscription('https://private.example/secret', 'authorized-device', 'test', 1)
            self.assertLess(time.monotonic() - started, 3)
            with self.assertRaises(ProcessLookupError):
                os.kill(int(pidfile.read_text()), 0)

    def test_worker_reports_validation_errors_without_exposing_credentials(self):
        with self.assertRaises(common.SubscriptionError) as caught:
            fetch.bounded_fetch_subscription('http://private.example/secret', None, 'test', 5)
        self.assertIn('HTTPS', str(caught.exception))
        self.assertNotIn('secret', str(caught.exception))


class ScalingWebTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        environment = patch.dict(os.environ, {'PARSER_ADMIN_TOKEN': 'scaling-test'}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        validator = patch.object(service, 'checked_combine', wraps=service.checked_combine)
        self.validator = validator.start()
        self.addCleanup(validator.stop)
        self.downloads, self.probes = [], []
        def download(url, *args):
            self.downloads.append(url)
            return body([url.split('/')[2]]), {'fetched_at': common.now()}
        def tester(node, core, *args):
            self.probes.append((node.identity, core))
            return c.result('passed', latency_ms=1)
        def checker(nodes, root, **kwargs):
            return c.check_entries(nodes, root, tester=tester, **kwargs)
        self.app = create_app(self.root, start_scheduler=False, fetcher=download, checker=checker)
        self.manager = self.app.state.manager
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)
        self.addCleanup(self.manager.stop.set)
        self.client.post('/api/login', json={'token': 'scaling-test'})
        self.providers = [self.client.post('/api/providers', json={
            'name': name, 'url': f'https://{name}.example/sub', 'hwid_id': None}).json()
            for name in ('first', 'second')]
        self.manager.refresh()
        self.assertNotIn('last_error', self.manager.snapshot)

    def age_first_node(self):
        identity = next(node.identity for node in self.manager.published_scopes['all'][0] if node.source == 'first')
        for record in self.manager.snapshot['connectivity'][identity].values():
            record['checked_ts'] = time.time() - 1201
        self.manager.snapshot['valid_until'] = time.time() - 1

    def test_expiry_keeps_other_healthy_nodes_available_during_a_running_job(self):
        self.age_first_node()
        self.downloads.clear()
        self.probes.clear()
        self.manager.busy = True
        try:
            state = self.client.get('/api/state').json()
            self.assertEqual(state['published_total'], 1)
            self.assertTrue(state['busy'])
            for filename, link in state['links'].items():
                response = self.client.get(link)
                self.assertEqual(response.status_code, 200, response.text)
                if filename == 'combined.json':
                    self.assertEqual(len(response.json()['servers']), 1)
                else:
                    self.assertNotIn('first.example', response.text)
                    self.assertIn('second.example', response.text)
        finally:
            self.manager.busy = False
        self.assertEqual(self.downloads, [])
        self.assertEqual(self.probes, [])

    def test_expiry_after_restart_uses_cached_sources_without_downloads(self):
        self.age_first_node()
        common.atomic_write(self.manager.snapshot_path, common.json_text(self.manager.snapshot))
        restarted = create_app(self.root, start_scheduler=False,
            fetcher=lambda *args: self.fail('expiry must not fetch')).state.manager
        restarted.ensure_publications()
        self.assertTrue(restarted.publication_current(restarted.snapshot))
        self.assertEqual(restarted.snapshot['published_total'], 1)
        self.assertNotIn('first.example', restarted.snapshot['exports']['xray.json'])

    def test_expiry_invalidates_the_cached_publication_health_signature(self):
        previous = copy.deepcopy(self.manager.snapshot['connectivity'])
        self.age_first_node()
        self.manager.ensure_publications()
        self.assertEqual(self.manager.snapshot['published_total'], 1)
        self.manager.snapshot['connectivity'] = previous
        self.manager.refresh(download=False, check=False)
        self.assertEqual(self.manager.snapshot['published_total'], 2)

    def test_expiry_cannot_overwrite_a_concurrent_job_failure(self):
        self.age_first_node()
        original = publication.build
        def build(*args, **kwargs):
            with patch.object(self.manager, 'checker', side_effect=RuntimeError('synthetic failure')):
                self.manager.refresh(download=False, check=True)
            return original(*args, **kwargs)
        with patch.object(publication, 'build', side_effect=build):
            self.manager.ensure_publications()
        self.assertIn('last_error', self.manager.snapshot)

    def test_recent_checks_reuse_parsing_validation_and_publications(self):
        self.validator.reset_mock()
        self.downloads.clear()
        self.probes.clear()
        with patch.object(protocols, 'parse', side_effect=AssertionError('must reuse parsed data')), \
                patch.object(publication, 'build', side_effect=AssertionError('must reuse unchanged publications')):
            self.manager.refresh(download=False, check=True, reuse_recent=True)
        self.assertNotIn('last_error', self.manager.snapshot)
        self.validator.assert_not_called()
        self.assertEqual(self.downloads, [])
        self.assertEqual(self.probes, [])

    def test_provider_refresh_rebuilds_only_affected_selections(self):
        for provider in self.providers:
            response = self.client.post('/api/subscriptions', json={
                'name': provider['name'], 'provider_ids': [provider['id']]})
            self.assertEqual(response.status_code, 201)
        with patch.object(publication, 'build', wraps=publication.build) as build:
            self.manager.refresh(selected=self.providers[0]['id'])
        self.assertNotIn('last_error', self.manager.snapshot)
        self.assertEqual(build.call_count, 2)  # Global feed and the first provider's selection.
        self.assertFalse(any({node.source for node in call.args[0]} == {'second'} for call in build.call_args_list))

    def test_conditional_poll_keeps_progress_and_omits_unchanged_inventory(self):
        initial = self.client.get('/api/state').json()
        self.manager.busy = True
        self.manager.busy_action = 'checking'
        self.manager.check_progress = {'done': 2, 'total': 6}
        try:
            update = self.client.get('/api/state', params={'revision': initial['revision']}).json()
            self.assertTrue(update['unchanged'])
            self.assertNotIn('inventory', update)
            self.assertNotIn('diagnostics', update)
            self.assertEqual(update['check_progress'], {'done': 2, 'total': 6})
            self.age_first_node()
            expired = self.client.get('/api/state', params={'revision': initial['revision']}).json()
            self.assertFalse(expired['unchanged'])
            self.assertEqual(expired['published_total'], 1)
            self.assertIn('inventory', expired)
        finally:
            self.manager.busy = False

    def test_cached_checks_continue_while_one_provider_download_is_blocked(self):
        entered, release, checked = threading.Event(), threading.Event(), threading.Event()
        original_fetch, original_check = self.manager.fetcher, self.manager.checker
        def download(url, *args):
            if 'first.example' in url:
                entered.set()
                release.wait(8)
            return original_fetch(url, *args)
        def checker(*args, **kwargs):
            value = original_check(*args, **kwargs)
            if entered.is_set() and not release.is_set():
                checked.set()
            return value
        self.manager.next_check = self.manager.next_check_batch = 0
        with patch.object(self.manager, 'fetcher', side_effect=download), \
                patch.object(self.manager, 'checker', side_effect=checker):
            self.manager.begin_refresh()
            scheduler = threading.Thread(target=self.manager.scheduler)
            scheduler.start()
            try:
                self.assertTrue(entered.wait(2))
                self.assertTrue(checked.wait(5), 'slow download blocked scheduled connection checks')
                self.assertTrue(self.manager.busy)
            finally:
                release.set()
                self.manager.wait_for_idle(5)
                self.manager.stop.set()
                scheduler.join(3)
        self.assertFalse(self.manager.busy)
        self.assertNotIn('last_error', self.manager.snapshot)

    def test_interrupted_job_resumes_after_restart_and_completed_jobs_are_cleared(self):
        for kind, selected in [('refresh', self.providers[0]['id']), ('check', None), ('refresh', 'deleted')]:
            with self.subTest(kind=kind, selected=selected):
                common.atomic_write(self.manager.job_path, common.json_text({'kind': kind, 'provider_id': selected}))
                restarted = create_app(self.root, start_scheduler=False, fetcher=self.manager.fetcher,
                    checker=self.manager.checker).state.manager
                with patch.object(restarted.stop, 'wait', side_effect=[False, True]):
                    restarted.scheduler()
                restarted.wait_for_idle(5)
                self.assertFalse(restarted.busy)
                self.assertFalse(restarted.jobs.items)
                self.assertFalse(restarted.job_path.exists())
                self.assertNotIn('last_error', restarted.snapshot)
                if kind == 'refresh' and selected != 'deleted':
                    self.assertEqual(restarted.snapshot['history'][0]['provider_id'], selected)

    def test_deferred_failure_timestamps_survive_restart(self):
        for cores in self.manager.snapshot['connectivity'].values():
            for record in cores.values():
                record['status'] = 'failed'
        timestamps = [record['checked_ts'] for cores in self.manager.snapshot['connectivity'].values()
                      for record in cores.values()]
        with patch.object(c, 'BATCH_TIMEOUT', 0):
            self.manager.refresh(download=False, check=True)
        restarted = create_app(self.root, start_scheduler=False).state.manager
        records = [record for cores in restarted.snapshot['connectivity'].values() for record in cores.values()]
        self.assertEqual([record['checked_ts'] for record in records], timestamps)
        self.assertTrue(all(record['status'] == 'failed' and record['deferred'] for record in records))
        self.assertLessEqual(restarted.next_check, time.time() + 5)

    def test_invalid_concurrency_settings_fail_at_startup(self):
        for name, value in [('PARSER_CHECK_WORKERS', '0'), ('PARSER_CHECK_WORKERS', '129'),
                            ('PARSER_FETCH_WORKERS', 'many'), ('PARSER_FETCH_TIMEOUT_SECONDS', '121'),
                            ('PARSER_CHECK_BATCH_SECONDS', '0')]:
            with self.subTest(name=name, value=value), patch.dict(os.environ, {name: value}):
                with self.assertRaisesRegex(common.SubscriptionError, name):
                    create_app(self.root, start_scheduler=False)

    def test_two_hundred_providers_and_explicit_selection_are_supported(self):
        config = self.manager.store.config()
        template = config['providers'][0]
        config['providers'] = [dict(template, id=f'p{i}', name=f'Provider {i}') for i in range(199)]
        self.manager.store.save_config(config)
        response = self.client.post('/api/providers', json={
            'name': 'Provider 200', 'url': 'https://last.example/sub', 'hwid_id': None})
        self.assertEqual(response.status_code, 201, response.text)
        providers = self.manager.store.config()['providers']
        self.assertEqual(len(providers), 200)
        selected = self.client.post('/api/subscriptions', json={
            'name': 'All explicitly selected', 'provider_ids': [p['id'] for p in providers]})
        self.assertEqual(selected.status_code, 201, selected.text)
        rejected = self.client.post('/api/providers', json={
            'name': 'Provider 201', 'url': 'https://extra.example/sub', 'hwid_id': None})
        self.assertEqual(rejected.status_code, 400)
        self.assertIn('200 providers', rejected.text)


if __name__ == '__main__':
    unittest.main()

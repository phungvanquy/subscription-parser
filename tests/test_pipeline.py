"""Independent jobs, durable selections, and atomic publication under races."""
import copy
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from subscription_parser import common, connectivity as c, pipeline, protocols, publication, service
from subscription_parser.app import create_app
from test_provider_sampling import source


class PipelineTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        env = patch.dict(os.environ, {'PARSER_ADMIN_TOKEN': 'pipeline-test'}, clear=True)
        env.start(); self.addCleanup(env.stop)
        validator = patch.object(service, 'checked_combine', side_effect=lambda entries, root:
                                 protocols.combine(entries) | {'validation': {}})
        validator.start(); self.addCleanup(validator.stop)
        self.bodies = {'a': source(8, 'old-a'), 'b': source(8, 'old-b')}
        self.downloads, self.probes, self.releases = [], [], []
        def fetch(url, *args):
            name = url.split('//')[1].split('.')[0]
            self.downloads.append(name)
            return self.bodies[name], {'fetched_at': common.now()}
        def tester(entry, core, *args):
            self.probes.append((entry.identity, core))
            return c.result('passed', latency_ms=1)
        def checker(entries, root, **kwargs):
            return c.check_entries(entries, root, tester=tester, **kwargs)
        self.fetcher, self.checker = fetch, checker
        self.app = create_app(self.root, start_scheduler=False, fetcher=fetch, checker=checker)
        self.manager = self.app.state.manager
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)
        self.client.post('/api/login', json={'token': 'pipeline-test'})
        self.providers = [self.manager.change_provider({'name': name.upper(), 'url': f'https://{name}.example/sub',
                           'hwid_id': None, 'server_limit': 2}) for name in self.bodies]
        self.manager.refresh()
        self.assertNotIn('last_error', self.manager.snapshot)
        self.addCleanup(self.finish)
        self.downloads.clear(); self.probes.clear()

    def finish(self):
        self.manager.stop.set()
        for event in self.releases:
            event.set()
        self.manager.wait_for_idle(10)

    def held_checker(self):
        started, release = threading.Event(), threading.Event()
        self.releases.append(release)
        original = self.manager.checker
        def check(*args, **kwargs):
            started.set()
            if not release.wait(10):
                raise RuntimeError('test checker timed out')
            return original(*args, **kwargs)
        self.manager.checker = check
        return started, release

    def test_download_finishes_while_a_check_is_running_and_actions_are_independent(self):
        before = self.manager.snapshot['exports']['xray.json']
        started, release = self.held_checker()
        self.assertEqual(self.client.post('/api/check-servers').status_code, 202)
        self.assertTrue(started.wait(3))
        self.assertEqual(self.client.post('/api/check-servers').status_code, 409)
        self.bodies['a'] = source(8, 'new-a')
        self.assertEqual(self.client.post('/api/refresh').status_code, 202)
        self.manager.refresh_worker.join(5)
        self.assertFalse(self.manager.refresh_running)
        self.assertTrue(self.manager.check_running)
        self.assertCountEqual(self.downloads, ['a', 'b'])
        state = self.client.get('/api/state').json()
        self.assertTrue(state['check_running'])
        self.assertFalse(state['refresh_running'])
        self.assertEqual(state['refresh_progress'], {'done': 2, 'total': 2})
        self.assertTrue(next(p for p in state['providers'] if p['name'] == 'A')['selection_pending'])
        self.assertEqual(self.manager.snapshot['exports']['xray.json'], before)
        for key in ('active_selections', 'selection_checks', 'connectivity'):
            self.assertNotIn(key, state)
        release.set(); self.manager.wait_for_idle(10)
        self.assertFalse(self.manager.busy)
        self.assertFalse(self.manager.jobs.items)
        self.assertIn('new-a', self.manager.snapshot['exports']['xray.json'])
        self.assertNotIn('old-a', self.manager.snapshot['exports']['xray.json'])

    def test_single_provider_refresh_is_distinct_from_the_next_scheduled_refresh(self):
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        original = self.manager.fetcher
        def fetch(*args):
            entered.set(); release.wait(10)
            return original(*args)
        self.manager.fetcher = fetch
        identifier = self.providers[0]['id']
        schedules = (self.manager.next_refresh, self.manager.next_check)
        revision = self.client.get('/api/state').json()['revision']
        self.assertEqual(self.client.post('/api/refresh', params={'provider_id': identifier}).status_code, 202)
        self.assertTrue(entered.wait(3))
        state = self.client.get('/api/state', params={'revision': revision}).json()
        self.assertTrue(state['unchanged'])
        self.assertEqual(state['refresh_provider_id'], identifier)
        self.assertEqual(state['refresh_provider_ids'], [identifier])
        self.assertFalse(state['refresh_scheduled'])
        release.set(); self.manager.wait_for_idle(10)
        self.assertEqual(self.downloads, ['a'])
        self.assertEqual((self.manager.next_refresh, self.manager.next_check), schedules)
        self.assertEqual(self.client.get('/api/state').json()['refresh_provider_ids'], [])

        entered.clear(); release.clear()
        self.manager.next_refresh = 0
        with patch.object(self.manager.stop, 'wait', side_effect=[False, True]):
            self.manager.scheduler()
        self.assertTrue(entered.wait(3))
        state = self.client.get('/api/state').json()
        self.assertIsNone(state['refresh_provider_id'])
        self.assertCountEqual(state['refresh_provider_ids'], [p['id'] for p in self.providers])
        self.assertTrue(state['refresh_scheduled'])
        release.set(); self.manager.wait_for_idle(10)
        state = self.client.get('/api/state').json()
        self.assertEqual(state['refresh_provider_ids'], [])
        self.assertFalse(state['refresh_scheduled'])
        refreshes = [event for event in state['history'] if event['action'] == 'refresh']
        self.assertTrue(refreshes[0]['scheduled'])
        self.assertFalse(refreshes[1]['scheduled'])

    def test_refresh_progress_only_lists_unfinished_downloads(self):
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        original = self.manager.fetcher
        def fetch(*args):
            if 'b.example' in args[0]:
                entered.set(); release.wait(10)
            return original(*args)
        self.manager.fetcher = fetch
        self.manager.fetch_workers = 1
        revision = self.client.get('/api/state').json()['revision']
        self.assertEqual(self.client.post('/api/refresh').status_code, 202)
        self.assertTrue(entered.wait(3))
        state = self.client.get('/api/state', params={'revision': revision}).json()
        self.assertTrue(state['unchanged'])
        self.assertIsNone(state['refresh_provider_id'])
        self.assertEqual(state['refresh_provider_ids'], [self.providers[1]['id']])
        self.assertEqual(state['refresh_progress'], {'done': 1, 'total': 2})
        self.assertFalse(state['refresh_scheduled'])
        release.set(); self.manager.wait_for_idle(10)
        self.assertEqual(self.client.get('/api/state').json()['refresh_provider_ids'], [])

    def test_slow_check_cannot_stop_download_pool_from_filling_its_slots(self):
        entered, release_fetch = threading.Event(), threading.Event()
        self.releases.append(release_fetch)
        original = self.manager.fetcher
        def fetch(*args):
            if 'a.example' in args[0]:
                entered.set(); release_fetch.wait(10)
            return original(*args)
        self.manager.fetch_workers = 1
        self.manager.fetcher = fetch
        self.manager.begin_refresh()
        self.assertTrue(entered.wait(3))
        started, release_check = self.held_checker()
        self.assertEqual(self.client.post('/api/check-servers').status_code, 202)
        self.assertTrue(started.wait(3))
        self.assertEqual(self.client.post('/api/refresh').status_code, 409)
        state = self.client.get('/api/state').json()
        self.assertTrue(state['refresh_running'] and state['check_running'])
        self.assertEqual(state['busy_action'], 'fetching_and_checking')
        release_fetch.set()
        self.manager.refresh_worker.join(5)
        self.assertFalse(self.manager.refresh_running)
        self.assertTrue(self.manager.check_running)
        self.assertCountEqual(self.downloads, ['a', 'b'])
        release_check.set(); self.manager.wait_for_idle(10)
        self.assertFalse(self.manager.busy)

    def test_late_results_cannot_publish_a_superseded_random_selection(self):
        old = self.manager.snapshot['exports']['xray.json']
        identifier = self.providers[0]['id']
        self.bodies['a'] = source(8, 'superseded')
        started, release = self.held_checker()
        self.manager.begin_refresh(identifier)
        self.assertTrue(started.wait(3))
        superseded = self.manager.latest_selections[identifier]
        self.bodies['a'] = source(8, 'newest')
        self.manager.begin_refresh(identifier)
        self.manager.refresh_worker.join(5)
        self.assertNotEqual(self.manager.latest_selections[identifier], superseded)
        with patch.object(self.manager, '_start_queued_check'):
            release.set(); self.manager.wait_for_idle(5)
        self.assertEqual(self.manager.snapshot['exports']['xray.json'], old)
        self.assertNotEqual(self.manager.snapshot['active_selections'][identifier], superseded)
        self.assertIn('check', self.manager.jobs.items)
        self.manager.begin_check(reuse_recent=True)
        self.manager.wait_for_idle(10)
        self.assertIn('newest', self.manager.snapshot['exports']['xray.json'])
        self.assertNotIn('superseded', self.manager.snapshot['exports']['xray.json'])
        self.assertEqual(self.manager.snapshot['total'], 4)

    def test_partial_replacement_keeps_old_feeds_and_resumes_same_draw_after_restart(self):
        identifier = self.providers[0]['id']
        old = self.manager.snapshot['exports']['xray.json']
        self.bodies['a'] = source(8, 'replacement')
        self.manager.refresh(selected=identifier, check=False)
        selection = self.manager.latest_selections[identifier]
        calls = []
        def partial(entry, core, *args):
            calls.append(entry.identity)
            return c.result('passed' if len(calls) <= 3 else 'pending', latency_ms=1)
        self.manager.checker = lambda nodes, root, **kw: c.check_entries(nodes, root, tester=partial, **kw)
        with patch.dict(os.environ, {'PARSER_CHECK_WORKERS': '1'}):
            self.manager.refresh(selected=identifier, download=False, check=True)
        self.assertEqual(self.manager.snapshot['exports']['xray.json'], old)
        self.assertEqual(self.manager.snapshot['selection_checks'][identifier]['generation'], selection)
        self.assertEqual(len(self.manager.snapshot['selection_checks'][identifier]['done']), 3)
        previous_downloads = list(self.downloads)
        restarted = service.Manager(self.root, self.fetcher, self.checker)
        self.probes.clear()
        restarted.refresh(download=False, check=True, reuse_recent=True)
        self.assertNotIn('last_error', restarted.snapshot)
        self.assertEqual(restarted.snapshot['active_selections'][identifier], selection)
        self.assertEqual(self.downloads, previous_downloads)
        self.assertEqual(len(self.probes), 3)
        self.assertNotIn('old-a', restarted.snapshot['exports']['xray.json'])
        self.assertEqual(restarted.snapshot['published_total'], 4)
        self.assertFalse(restarted.snapshot['selection_checks'])

    def test_old_selection_expires_while_its_replacement_is_pending_after_restart(self):
        identifier = self.providers[0]['id']
        self.bodies['a'] = source(8, 'pending')
        self.manager.refresh(selected=identifier, check=False)
        for cores in self.manager.snapshot['connectivity'].values():
            for value in cores.values():
                value['checked_ts'] = time.time() - 2000
        self.manager.snapshot['valid_until'] = time.time() - 1
        common.atomic_write(self.manager.snapshot_path, common.json_text(self.manager.snapshot))
        restarted = service.Manager(self.root, self.fetcher, self.checker)
        restarted.ensure_publications()
        self.assertEqual(restarted.snapshot['published_total'], 0)
        self.assertNotIn('pending', json.dumps(restarted.snapshot['inventory']))
        self.assertIn('old-a', json.dumps(restarted.snapshot['inventory']))

    def test_completed_coverage_survives_expiry_during_a_large_replacement(self):
        identifier = self.providers[0]['id']
        self.bodies['a'] = source(8, 'replacement')
        self.manager.refresh(selected=identifier, check=False)
        nodes, statuses = self.manager._collect(self.manager.store.config())
        first, second = nodes[identifier]
        def partial(entry, core, *args):
            return c.result('passed' if entry.identity == first.identity else 'pending', latency_ms=1)
        self.manager.checker = lambda entries, root, **kw: c.check_entries(entries, root, tester=partial, **kw)
        self.manager.refresh(selected=identifier, download=False, check=True)
        self.assertEqual(len(self.manager.snapshot['selection_checks'][identifier]['done']), 3)
        for value in self.manager.snapshot['connectivity'][first.identity].values():
            value['checked_ts'] = time.time() - 2000
        def rest(entry, core, *args):
            return c.result('passed' if entry.identity == second.identity else 'pending', latency_ms=1)
        self.manager.checker = lambda entries, root, **kw: c.check_entries(entries, root, tester=rest, **kw)
        self.manager.refresh(selected=identifier, download=False, check=True)
        self.assertEqual(self.manager.snapshot['active_selections'][identifier], statuses[identifier]['_generation'])
        self.assertNotIn(identifier, self.manager.snapshot['selection_checks'])
        self.assertNotIn('old-a', self.manager.snapshot['exports']['xray.json'])
        self.assertEqual(self.manager.snapshot['published_total'], 3)

    def test_check_failure_does_not_roll_back_a_completed_download(self):
        self.bodies['a'] = source(8, 'new')
        with patch.object(self.manager, 'checker', side_effect=RuntimeError('synthetic check failure')):
            self.manager.begin_refresh()
            self.manager.wait_for_idle(10)
        self.assertFalse(self.manager.refresh_running or self.manager.check_running)
        self.assertNotIn('refresh', self.manager.jobs.items)
        self.assertIn('check', self.manager.jobs.items)
        self.assertIn('connection check failed', self.manager.snapshot['last_error'])
        self.assertIn('old-a', self.manager.snapshot['exports']['xray.json'])
        restarted = service.Manager(self.root, self.fetcher, self.checker)
        self.assertIn('check', restarted.jobs.items)
        before = list(self.downloads)
        restarted.begin_check(reuse_recent=True); restarted.wait_for_idle(10)
        self.assertEqual(self.downloads, before)
        self.assertIn('new', restarted.snapshot['exports']['xray.json'])
        self.assertFalse(restarted.jobs.items)

    def test_scheduler_can_start_both_jobs_in_the_same_tick(self):
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        original = self.manager.fetcher
        def fetch(*args):
            entered.set(); release.wait(10)
            return original(*args)
        self.manager.fetcher = fetch
        check_started, check_release = self.held_checker()
        self.manager.next_refresh = self.manager.next_check = self.manager.next_check_batch = 0
        with patch.object(self.manager.stop, 'wait', side_effect=[False, True]):
            self.manager.scheduler()
        self.assertTrue(entered.wait(3))
        self.assertTrue(check_started.wait(3))
        self.assertTrue(self.manager.refresh_running and self.manager.check_running)
        release.set(); check_release.set(); self.manager.wait_for_idle(10)

    def test_restart_download_intent_only_fetches_unfinished_providers(self):
        identifier = self.providers[1]['id']
        self.manager.jobs.put('refresh', provider_ids=[identifier], provider_id=None, reuse_recent=True, scheduled=True)
        self.manager.jobs.put('check', provider_ids=[self.providers[0]['id']], reuse_recent=True, regular=False)
        restarted = service.Manager(self.root, self.fetcher, self.checker)
        entered, release = threading.Event(), threading.Event()
        def fetch(*args):
            entered.set(); release.wait(10)
            return self.fetcher(*args)
        restarted.fetcher = fetch
        try:
            with patch.object(restarted.stop, 'wait', side_effect=[False, True]):
                restarted.scheduler()
            self.assertTrue(entered.wait(3))
            state = restarted.public_state('http://testserver')
            self.assertIsNone(state['refresh_provider_id'])
            self.assertEqual(state['refresh_provider_ids'], [identifier])
            self.assertTrue(state['refresh_scheduled'])
        finally:
            release.set(); restarted.wait_for_idle(10)
        self.assertEqual(self.downloads, ['b'])
        self.assertFalse(restarted.jobs.items)
        self.assertEqual(restarted.snapshot['published_total'], 4)
        self.assertTrue(next(event for event in restarted.snapshot['history'] if event['action'] == 'refresh')['scheduled'])

    def test_snapshot_commit_failure_keeps_selection_and_saved_check_intent(self):
        self.bodies['a'] = source(8, 'replacement')
        self.manager.refresh(selected=self.providers[0]['id'], check=False)
        old = copy.deepcopy(self.manager.snapshot)
        atomic_write = common.atomic_write
        def fail(path, value):
            if path == self.manager.snapshot_path:
                raise OSError('synthetic disk failure')
            return atomic_write(path, value)
        with patch.object(common, 'atomic_write', side_effect=fail):
            self.manager.refresh(download=False, check=True)
        self.assertEqual(self.manager.snapshot['active_selections'], old['active_selections'])
        self.assertEqual(self.manager.snapshot['exports'], old['exports'])
        self.assertIn('check', self.manager.jobs.items)
        self.manager.refresh(download=False, check=True, reuse_recent=True)
        self.assertFalse(self.manager.jobs.items)

    def test_configuration_change_cannot_publish_a_late_result_or_exceed_lowered_cap(self):
        started, release = self.held_checker()
        self.manager.begin_check()
        self.assertTrue(started.wait(3))
        # Exercise the commit guard even if configuration is changed by a
        # maintenance import; interactive edits remain blocked during jobs.
        config = self.manager.store.config()
        config['providers'][0]['server_limit'] = 1
        self.manager.store.save_config(config)
        release.set(); self.manager.wait_for_idle(10)
        self.assertEqual(self.manager.snapshot['total'], 3)
        count = sum('A' in row['sources'] for row in self.manager.snapshot['inventory'])
        self.assertEqual(count, 1)

    def test_selection_files_are_bounded_and_current_selection_survives_failed_fetch(self):
        for index in range(4):
            self.bodies['a'] = source(8, f'draw-{index}')
            self.manager.refresh(selected=self.providers[0]['id'])
        self.assertEqual(len(list(self.manager.selection_directory.glob('*.json'))), 2)
        versions = dict(self.manager.snapshot['active_selections'])
        with patch.object(self.manager, 'fetcher', side_effect=common.SubscriptionError('upstream unavailable')):
            self.manager.refresh()
        self.assertEqual(self.manager.snapshot['active_selections'], versions)
        self.assertEqual(self.manager.snapshot['published_total'], 4)

    def test_job_ledger_rejects_malformed_data_and_does_not_clear_newer_intent(self):
        jobs = pipeline.Jobs(self.manager.store.directory)
        old = jobs.put('check', provider_ids=None)
        new = jobs.put('check', provider_ids=[self.providers[0]['id']])
        self.assertFalse(jobs.clear('check', old))
        self.assertEqual(pipeline.Jobs(self.manager.store.directory).items['check'], new)
        self.assertTrue(jobs.clear('check', new))
        for malformed in ([], {'other': {}}, {'check': {'id': 'x', 'provider_ids': [1]}}):
            jobs.path.write_text(json.dumps(malformed))
            with self.assertRaises(ValueError):
                pipeline.Jobs(self.manager.store.directory)

    def test_storage_failure_when_finishing_job_releases_worker_and_keeps_intent(self):
        started, release = self.held_checker()
        self.manager.begin_check()
        self.assertTrue(started.wait(3))
        write = common.atomic_write
        def full_disk(path, value):
            if path == self.manager.jobs.path:
                raise OSError('synthetic full disk')
            return write(path, value)
        with patch.object(common, 'atomic_write', side_effect=full_disk):
            release.set(); self.manager.wait_for_idle(10)
        self.assertFalse(self.manager.busy)
        self.assertIn('check', self.manager.jobs.items)
        self.assertGreater(self.manager.jobs.items['check']['retry_at'], time.time())
        self.manager.begin_check(reuse_recent=True); self.manager.wait_for_idle(10)
        self.assertFalse(self.manager.jobs.items)

    def test_download_during_publication_cannot_commit_the_obsolete_selection(self):
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        identifier = self.providers[0]['id']
        old_generation = self.manager.snapshot['active_selections'][identifier]
        build = publication.build
        def held_build(*args, **kwargs):
            entered.set(); release.wait(10)
            return build(*args, **kwargs)
        with patch.object(publication, 'build', side_effect=held_build):
            self.manager.begin_check()
            self.assertTrue(entered.wait(3))
            self.bodies['a'] = source(8, 'concurrent')
            with patch.object(self.manager, '_start_queued_check'):
                self.manager.begin_refresh(identifier)
                deadline = time.monotonic() + 3
                while self.manager.latest_selections[identifier] == old_generation and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertNotEqual(self.manager.latest_selections[identifier], old_generation)
                release.set(); self.manager.wait_for_idle(10)
        self.assertEqual(self.manager.snapshot['active_selections'][identifier], old_generation)
        self.assertIn('old-a', self.manager.snapshot['exports']['xray.json'])
        self.assertIn('check', self.manager.jobs.items)
        self.assertNotIn('last_error', self.manager.snapshot)
        self.manager.begin_check(reuse_recent=True); self.manager.wait_for_idle(10)
        self.assertIn('concurrent', self.manager.snapshot['exports']['xray.json'])

    def test_old_cache_parse_cannot_replace_a_new_download_generation(self):
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        provider = self.manager.store.config()['providers'][0]
        self.manager.parsed_cache.clear()
        parse = self.manager.provider_entries
        def held_parse(*args):
            if threading.current_thread().name == 'old-cache-reader':
                entered.set(); release.wait(10)
            return parse(*args)
        reads = []
        with patch.object(self.manager, 'provider_entries', side_effect=held_parse):
            reader = threading.Thread(target=lambda: reads.append(self.manager.fetch(provider, False)), name='old-cache-reader')
            reader.start()
            try:
                self.assertTrue(entered.wait(3))
                self.bodies['a'] = source(8, 'new-cache')
                _, downloaded = self.manager.fetch(provider)
                release.set(); reader.join(5)
            finally:
                release.set(); reader.join(5)
        self.assertFalse(reader.is_alive())
        self.assertEqual(reads[0][1]['_generation'], downloaded['_generation'])
        self.assertEqual(self.manager.latest_selections[provider['id']], downloaded['_generation'])
        self.assertTrue(all('new-cache' in entry.original['server'] for entry in reads[0][0]))

    def test_shutdown_preserves_only_unstarted_downloads_without_redrawing_completed_ones(self):
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        fetch = self.manager.fetcher
        def held_fetch(*args):
            entered.set(); release.wait(10)
            return fetch(*args)
        self.manager.fetch_workers = 1
        self.manager.fetcher = held_fetch
        self.manager.begin_refresh()
        self.assertTrue(entered.wait(3))
        self.manager.stop.set(); release.set(); self.manager.wait_for_idle(10)
        self.assertEqual(self.downloads, ['a'])
        self.assertEqual(self.manager.jobs.items['refresh']['provider_ids'], [self.providers[1]['id']])
        first_generation = self.manager.latest_selections[self.providers[0]['id']]
        restarted = service.Manager(self.root, self.fetcher, self.checker)
        with patch.object(restarted.stop, 'wait', side_effect=[False, True]):
            restarted.scheduler()
        restarted.wait_for_idle(10)
        self.assertEqual(self.downloads, ['a', 'b'])
        self.assertEqual(restarted.snapshot['active_selections'][self.providers[0]['id']], first_generation)
        self.assertFalse(restarted.jobs.items)

    def test_upgrade_pins_published_cache_before_first_independent_download(self):
        self.manager.snapshot.pop('active_selections')
        common.atomic_write(self.manager.snapshot_path, common.json_text(self.manager.snapshot))
        restarted = service.Manager(self.root, self.fetcher, self.checker)
        pinned = dict(restarted.snapshot['active_selections'])
        self.assertEqual(len(pinned), 2)
        self.bodies['a'] = source(8, 'new-after-upgrade')
        restarted.refresh(selected=self.providers[0]['id'], check=False)
        self.assertEqual(restarted.snapshot['active_selections'], pinned)
        self.assertIn('old-a', restarted.snapshot['exports']['xray.json'])
        restarted.refresh(download=False, check=True)
        self.assertIn('new-after-upgrade', restarted.snapshot['exports']['xray.json'])

    def test_changed_download_deadline_survives_restart_without_postponement(self):
        self.manager.change_settings({'refresh_seconds': 900})
        due = self.manager.next_refresh
        restarted = service.Manager(self.root, self.fetcher, self.checker)
        self.assertEqual(restarted.next_refresh, due)

    def test_repeated_downloads_coalesce_and_bound_files_while_checks_are_held(self):
        started, release = self.held_checker()
        self.manager.begin_check()
        self.assertTrue(started.wait(3))
        identifier = self.providers[0]['id']
        for index in range(5):
            self.bodies['a'] = source(8, f'pending-{index}')
            self.manager.begin_refresh(identifier)
            self.manager.refresh_worker.join(5)
            self.assertFalse(self.manager.refresh_running)
            self.assertLessEqual(len(list(self.manager.selection_directory.glob('*.json'))), 3)
        self.assertEqual(set(self.manager.jobs.items), {'check'})
        release.set(); self.manager.wait_for_idle(10)
        self.assertIn('pending-4', self.manager.snapshot['exports']['xray.json'])
        self.assertFalse(self.manager.jobs.items)

    def test_check_publication_preserves_a_concurrent_download_failure_status(self):
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        identifier = self.providers[0]['id']
        build = publication.build
        def held_build(*args, **kwargs):
            entered.set(); release.wait(10)
            return build(*args, **kwargs)
        with patch.object(publication, 'build', side_effect=held_build):
            self.manager.begin_check()
            self.assertTrue(entered.wait(3))
            with patch.object(self.manager, 'fetcher', side_effect=common.SubscriptionError('synthetic upstream failure')):
                self.manager.begin_refresh(identifier)
                deadline = time.monotonic() + 3
                while self.manager.provider_statuses[identifier]['status'] != 'cached' and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertEqual(self.manager.provider_statuses[identifier]['status'], 'cached')
                release.set(); self.manager.wait_for_idle(10)
        self.assertEqual(self.manager.snapshot['providers'][identifier]['status'], 'cached')
        self.assertIn('synthetic upstream failure', self.manager.snapshot['providers'][identifier]['error'])
        self.assertEqual(self.manager.snapshot['published_total'], 4)

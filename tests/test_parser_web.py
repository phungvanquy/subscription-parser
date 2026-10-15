import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from subscription_parser import common, connectivity
from subscription_parser.app import create_app
from subscription_parser.service import Manager

UUID = "01234567-89ab-4cde-8fab-0123456789ab"


def payload():
    return json.dumps({"outbounds": [{"tag": "Test", "type": "vless", "server": "proxy.example", "server_port": 443, "uuid": UUID}]}).encode()


class WebTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.token = 'configured-test-admin-key'
        self.env = patch.dict(common.os.environ, {'PARSER_ADMIN_TOKEN': self.token}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.seen = []
        def fetch(url, hwid, user_agent, timeout):
            self.seen.append((url, hwid))
            return payload(), {"fetched_at": common.now(), "http_status": 200}
        def checker(entries, root, **kwargs):
            return connectivity.check_entries(entries, root, **kwargs,
                tester=lambda *args: {'status': 'passed', 'latency_ms': 12})
        self.app = create_app(self.root, start_scheduler=False, fetcher=fetch, checker=checker)
        self.manager = self.app.state.manager
        self.hwid = self.manager.store.add_hwid('My device', 'authorized-device-id')['id']
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def login(self):
        self.assertEqual(self.client.post("/api/login", json={"token": self.token}).status_code, 200)

    def add(self, name="Provider", url="https://provider.example/private-token", hwid_id=None):
        response = self.client.post("/api/providers", json={"name": name, "url": url, "hwid_id": hwid_id or self.hwid})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def refresh(self):
        self.assertEqual(self.client.post("/api/refresh").status_code, 202)
        self.manager.worker.join(timeout=5)
        self.assertFalse(self.manager.busy)
        self.assertNotIn("last_error", self.manager.snapshot)

    def test_admin_required_and_login_logout_cookie(self):
        self.assertEqual(self.client.get("/api/state").status_code, 401)
        self.assertEqual(self.client.post("/api/providers", json={"name": "test"}).status_code, 401)
        self.assertEqual(self.client.post("/api/login", json={"token": "错误"}).status_code, 401)
        self.login()
        self.assertEqual(self.client.get("/api/state").status_code, 200)
        self.client.post("/api/logout")
        self.assertEqual(self.client.get("/api/state").status_code, 401)

    def test_healthcheck_does_not_expose_application_data(self):
        response = self.client.get('/healthz')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'status': 'ok'})

    def test_debug_logs_require_admin_and_have_bounded_validated_filters(self):
        self.assertEqual(self.client.get('/api/logs').status_code, 401)
        self.login()
        self.manager.logs.write('provider_failed')
        self.manager.logs.write('request_failed')
        response = self.client.get('/api/logs?level=error')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['cache-control'], 'no-store')
        data = response.json()
        self.assertEqual([entry['level'] for entry in data['entries']], ['error'])
        self.assertEqual((data['limit'], data['max_bytes'], data['retention_days']), (200, 4 * 1024 * 1024, 7))
        self.assertEqual(self.client.get('/api/logs?level=invalid').status_code, 422)
        self.client.post('/api/logout')
        self.assertEqual(self.client.get('/api/logs').status_code, 401)

    def test_failed_fetch_and_unexpected_job_error_are_logged_without_secrets(self):
        self.login(); self.add(); self.refresh()
        original = self.manager.fetcher
        with patch.object(self.manager, 'fetcher', side_effect=common.SubscriptionError('provider returned HTTP 429')):
            self.refresh()
        records = self.client.get('/api/logs').json()['entries']
        self.assertTrue(any(e['event'] == 'cache_used' for e in records))
        self.assertTrue(any(e['context'].get('reason') == 'provider returned HTTP 429' for e in records))
        with patch.object(self.manager, 'fetcher', side_effect=RuntimeError('private-token authorized-device-id ' + UUID)):
            self.manager.refresh()
        data = self.client.get('/api/logs').json()
        failed = next(e for e in data['entries'] if e['event'] == 'run_failed')
        self.assertEqual(failed['context']['stage'], 'fetch')
        self.assertEqual(failed['context']['error_type'], 'RuntimeError')
        self.assertTrue(failed['context']['trace'])
        for secret in ['private-token', 'authorized-device-id', UUID, self.token]:
            self.assertNotIn(secret, json.dumps(data))
        self.assertIs(self.manager.fetcher, original)

    def test_check_logs_capture_failure_and_recovery_without_repeating_unchanged_failure(self):
        identity = '1' * 64
        failed = {identity: {'mihomo': {'status': 'failed', 'reason': 'HTTPS request failed or returned an unexpected response on both endpoints'}}}
        self.manager.log_checks({}, failed)
        self.manager.log_checks(failed, failed)
        self.manager.log_checks(failed, {identity: {'mihomo': {'status': 'passed'}}})
        events = [entry['event'] for entry in self.manager.logs.recent()['entries']]
        self.assertEqual(events.count('node_failed'), 1)
        self.assertEqual(events.count('node_recovered'), 1)
        self.assertEqual(events.count('checks_completed'), 3)

    def test_unexpected_request_exception_has_a_safe_response_and_log(self):
        self.login()
        with patch.object(self.manager, 'public_state', side_effect=RuntimeError('request-secret-value')):
            response = self.client.get('/api/state')
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.headers['cache-control'], 'no-store')
        self.assertNotIn('request-secret-value', response.text)
        data = self.client.get('/api/logs').json()
        self.assertTrue(any(entry['event'] == 'request_failed' for entry in data['entries']))
        self.assertNotIn('request-secret-value', json.dumps(data))

    def test_runtime_data_directory_is_independent_of_application_source(self):
        with patch.dict(common.os.environ, {'PARSER_DATA_DIR': str(self.root)}):
            app = create_app(start_scheduler=False)
        self.assertEqual(app.state.manager.store.root, self.root)
        self.assertEqual(TestClient(app).post('/api/login', json={'token': self.token}).status_code, 200)

    def test_missing_or_invalid_admin_key_refuses_startup_without_creating_data(self):
        empty_root = self.root / 'new-installation'
        for value in (None, '', '   ', ' leading-space', 'trailing-space ', 'line\nbreak', 'x' * 257):
            with self.subTest(value=value), patch.dict(common.os.environ, {}, clear=True):
                if value is not None:
                    common.os.environ['PARSER_ADMIN_TOKEN'] = value
                with self.assertRaisesRegex(common.SubscriptionError, 'PARSER_ADMIN_TOKEN'):
                    create_app(empty_root, start_scheduler=False)
                self.assertFalse(empty_root.exists())

    def test_admin_key_change_on_restart_invalidates_sessions_but_preserves_feeds(self):
        self.login(); self.add(); self.refresh()
        state = self.client.get('/api/state').json()
        cookie = self.client.cookies.get('parser_session')
        with patch.dict(common.os.environ, {'PARSER_ADMIN_TOKEN': 'replacement-admin-key'}):
            next_app = create_app(self.root, start_scheduler=False)
        client = TestClient(next_app)
        self.addCleanup(client.close)
        client.cookies.set('parser_session', cookie)
        self.assertEqual(client.get('/api/state').status_code, 401)
        self.assertEqual(client.get('/api/state', headers={'Authorization': 'Bearer ' + self.token}).status_code, 401)
        self.assertEqual(client.post('/api/login', json={'token': self.token}).status_code, 401)
        self.assertEqual(client.post('/api/login', json={'token': 'replacement-admin-key'}).status_code, 200)
        self.assertEqual(client.get('/api/state').json()['links'], state['links'])
        self.assertEqual(client.get(state['links']['v2board.txt']).status_code, 200)
        # A running app keeps its startup configuration until it is recreated.
        self.assertEqual(self.client.get('/api/state').status_code, 200)
        with self.manager.store.connection() as connection:
            self.assertEqual([row[0] for row in connection.execute('SELECT id FROM subscriptions')], ['default'])

    def test_fixed_hwid_fetch_outputs_and_no_secrets_in_dashboard(self):
        self.login()
        self.add()
        self.refresh()
        self.refresh()
        self.assertEqual([i[1] for i in self.seen], ["authorized-device-id"] * 2)
        state = self.client.get("/api/state").json()
        self.assertEqual(state["counts"], {"hiddify": 1, "mihomo": 1, "xray": 1, "v2board": 1})
        public = json.dumps(state)
        for secret in ["authorized-device-id", "private-token", UUID, self.token]:
            self.assertNotIn(secret, public)
        for filename, link in state["links"].items():
            response = self.client.get(link)
            self.assertEqual(response.status_code, 200, filename)
            self.assertIn(UUID, response.text)

    def test_bad_subscription_token_and_rotation(self):
        self.login(); self.add(); self.refresh()
        old = self.client.get("/api/state").json()["links"]["hiddify.json"]
        self.assertEqual(self.client.get("/sub/wrong/hiddify.json").status_code, 404)
        self.assertEqual(self.client.get("/sub/错误/hiddify.json").status_code, 404)
        self.client.post("/api/rotate-link")
        self.assertEqual(self.client.get(old).status_code, 404)
        new = self.client.get("/api/state").json()["links"]["hiddify.json"]
        self.assertEqual(self.client.get(new).status_code, 200)

    def test_v2board_feed_authentication_rotation_and_provider_removal(self):
        self.login(); provider = self.add(); self.refresh()
        link = self.client.get('/api/state').json()['links']['v2board.txt']
        response = self.client.get(link)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers['content-type'].startswith('text/plain'))
        self.assertTrue(response.text.startswith('vless://'))
        self.assertIn(UUID, response.text)
        self.assertEqual(self.client.get('/sub/wrong/v2board.txt').status_code, 404)
        self.client.post('/api/rotate-link')
        self.assertEqual(self.client.get(link).status_code, 404)
        link = self.client.get('/api/state').json()['links']['v2board.txt']
        self.assertEqual(self.client.get(link).status_code, 200)
        self.client.delete('/api/providers/' + provider['id'])
        self.assertEqual(self.client.get(link).status_code, 503)

    def test_disable_and_delete_immediately_remove_servers(self):
        self.login(); provider = self.add(); self.refresh()
        response = self.client.put('/api/providers/' + provider['id'], json={"name": provider["name"], "enabled": False})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get("/api/state").json()["total"], 0)
        self.assertEqual(self.client.delete('/api/providers/' + provider['id']).status_code, 200)
        self.assertEqual(self.client.get("/api/state").json()["providers"], [])

    def test_failed_fetch_retains_cache_and_invalid_response_does_not_poison_it(self):
        self.login(); self.add(); self.refresh()
        self.manager.fetcher = lambda *args: (b'<html>Access denied</html>', {})
        self.refresh()
        state = self.client.get("/api/state").json()
        self.assertEqual(state["total"], 1)
        self.assertEqual(state["providers"][0]["status"], "cached")

    def test_changed_provider_url_cannot_reuse_old_account_cache(self):
        self.login(); provider = self.add(); self.refresh()
        self.client.put('/api/providers/' + provider['id'], json={"name": provider['name'], "url": "https://provider.example/new-token"})
        self.assertEqual(self.client.get("/api/state").json()["total"], 0)
        self.manager.fetcher = lambda *args: (b'broken', {})
        self.refresh()
        self.assertEqual(self.client.get("/api/state").json()["providers"][0]["status"], "unavailable")

    def test_core_rejected_download_cannot_poison_last_good_cache(self):
        self.login(); self.add(); self.refresh()
        cache = next((self.manager.store.directory / "cache").glob('*.json'))
        previous = cache.read_bytes()
        from subscription_parser import service
        original = service.checked_combine
        calls = []
        def checker(entries, root):
            calls.append(True)
            if len(calls) == 1:
                return {"counts": {"hiddify": 0, "mihomo": 0, "xray": 0}}
            return original(entries, root)
        with patch.object(service, 'checked_combine', checker):
            self.refresh()
        self.assertEqual(cache.read_bytes(), previous)
        state = self.client.get('/api/state').json()
        self.assertEqual(state['providers'][0]['status'], 'cached')
        self.assertEqual(state['total'], 1)

    def clear_hwid(self, identifier):
        with self.manager.store.connection(write=True) as connection:
            connection.execute('UPDATE hwids SET value = ? WHERE id = ?', ('', identifier))

    def test_hwid_is_saved_privately_and_existing_ids_cannot_be_overwritten(self):
        self.login()
        response = self.client.post('/api/hwids', json={"name": "My Hiddify device", "value": "another-authorized-id"})
        self.assertEqual(response.status_code, 201)
        self.assertFalse((self.root / '.env').exists())
        self.assertEqual(self.manager.store.database_path.stat().st_mode & 0o777, 0o600)
        identifier = response.json()['id']
        self.assertEqual(self.manager.store.hwid_value(identifier), 'another-authorized-id')
        self.assertNotIn('another-authorized-id', response.text)
        self.assertEqual(self.client.post('/api/hwids', json={"name": "my device", "value": "replacement-device-id"}).status_code, 400)
        self.assertEqual(self.client.post('/api/hwids', json={"name": "   ", "value": "replacement-device-id"}).status_code, 400)
        self.add(hwid_id=identifier); self.refresh()
        self.assertEqual(self.seen[0][1], 'another-authorized-id')

    def test_missing_fixed_id_never_registers_a_replacement(self):
        self.login();self.add();self.refresh()
        self.clear_hwid(self.hwid)
        self.refresh()
        self.assertEqual(len(self.seen), 1)
        state = self.client.get("/api/state").json()
        self.assertEqual(state['providers'][0]['status'], 'cached')
        self.assertIn("HWID", state['providers'][0]['error'])

    def test_environment_cannot_override_database_identities_or_subscription_token(self):
        self.login(); self.add(); self.refresh()
        link = self.client.get('/api/state').json()['links']['v2board.txt']
        with patch.dict(common.os.environ, {'DEVICE_ID': 'process-authorized-id',
                'PARSER_ADMIN_TOKEN': 'different-admin', 'PARSER_SUBSCRIPTION_TOKEN': 'different-link'}):
            self.refresh()
            self.assertEqual(self.client.get('/api/state').json()['links']['v2board.txt'], link)
            self.assertEqual(self.client.post('/api/login', json={'token': self.token}).status_code, 200)
        self.assertEqual([item[1] for item in self.seen], ['authorized-device-id', 'authorized-device-id'])

    def test_missing_source_override_does_not_use_default_id(self):
        self.login()
        second = self.manager.store.add_hwid('Second device', 'second-authorized-id')['id']
        self.add(hwid_id=second); self.refresh()
        self.clear_hwid(second)
        self.refresh()
        self.assertEqual([item[1] for item in self.seen], ['second-authorized-id'])
        state = self.client.get('/api/state').json()
        self.assertEqual(state['providers'][0]['status'], 'cached')
        self.assertIn('HWID', state['providers'][0]['error'])

    def test_cross_origin_changes_and_oversized_requests_rejected(self):
        self.login()
        self.assertEqual(self.client.post('/api/rotate-link', headers={'Origin':'https://untrusted.example'}).status_code, 403)
        self.assertEqual(self.client.post('/api/login', content='a'*20000).status_code, 413)

    def test_no_secrets_or_files_exposed_through_paths(self):
        for path in ['/.env','/static/../.env','/.parser-state/providers.json','/app.sqlite3','/legacy-backup/environment.txt','/subscription_parser/app.py']:
            self.assertEqual(self.client.get(path).status_code, 404)
        self.assertEqual(self.client.get('/').status_code, 200)
        self.assertIn("default-src 'self'", self.client.get('/').headers['content-security-policy'])

    def test_schedule_and_provider_settings_survive_restart(self):
        self.login(); self.add(); self.refresh()
        self.assertEqual(self.client.put('/api/settings', json={'refresh_seconds':900}).status_code, 200)
        next_manager=Manager(self.root)
        self.assertEqual(next_manager.store.config()['refresh_seconds'], 900)
        self.assertEqual(next_manager.snapshot['total'], 1)
        self.assertEqual(next_manager.store.credential('subscription_token'), self.manager.store.credential('subscription_token'))

    def test_duplicate_names_and_insecure_source_urls_rejected(self):
        self.login();self.add()
        self.assertEqual(self.client.post('/api/providers', json={'name':'Provider','url':'https://second.example/sub','hwid_id':self.hwid}).status_code, 400)
        self.assertEqual(self.client.post('/api/providers', json={'name':'HTTP','url':'http://provider.example/sub','hwid_id':self.hwid}).status_code, 400)

    def test_device_usage_rename_and_explicit_private_reveal(self):
        self.login(); provider = self.add(); self.refresh()
        cache = next((self.manager.store.directory/'cache').glob('*.json')).read_bytes()
        self.assertEqual(self.client.patch('/api/hwids/'+self.hwid, json={'name':'My renamed phone', 'value':'must-not-be-used'}).status_code, 200)
        self.assertEqual(self.manager.store.hwid_value(self.hwid), 'authorized-device-id')
        self.assertEqual(next((self.manager.store.directory/'cache').glob('*.json')).read_bytes(), cache)
        self.assertEqual(len(self.seen), 1)
        state = self.client.get('/api/state')
        device = state.json()['hwids'][0]
        self.assertEqual(device['name'], 'My renamed phone')
        self.assertEqual(device['providers'], [{'id':provider['id'],'name':'Provider','enabled':True}])
        self.assertNotIn('authorized-device-id', state.text)
        self.assertTrue(device['masked_value'].endswith('e-id'))
        revealed = self.client.post('/api/hwids/'+self.hwid+'/reveal')
        self.assertEqual(revealed.json(), {'value':'authorized-device-id'})
        self.assertEqual(revealed.headers['cache-control'], 'no-store')
        self.assertEqual(self.client.post('/api/hwids/'+self.hwid+'/reveal', headers={'Origin':'https://other.example'}).status_code, 403)
        self.client.post('/api/logout')
        self.assertEqual(self.client.post('/api/hwids/'+self.hwid+'/reveal').status_code, 401)

    def test_delete_only_unused_devices_including_paused_assignments(self):
        self.login(); provider = self.add()
        self.client.put('/api/providers/'+provider['id'], json={'name':'Provider','enabled':False})
        self.assertEqual(self.client.delete('/api/hwids/'+self.hwid).status_code, 400)
        self.assertEqual(self.client.post('/api/hwids', json={'name':'Duplicate value','value':'authorized-device-id'}).status_code, 400)
        self.client.delete('/api/providers/'+provider['id'])
        self.assertEqual(self.client.delete('/api/hwids/'+self.hwid).status_code, 200)
        self.assertEqual(self.manager.store.hwids(), [])

    def test_explicit_no_device_selection_and_omitted_edit_preserves_assignment(self):
        self.login()
        body={'name':'No HWID','url':'https://provider.example/no-device'}
        self.assertEqual(self.client.post('/api/providers',json=body).status_code,400)
        created=self.client.post('/api/providers',json=body|{'hwid_id':None})
        self.assertEqual(created.status_code,201)
        self.refresh()
        self.assertEqual(self.seen[-1][1],None)
        provider=self.add()
        self.client.put('/api/providers/'+provider['id'],json={'name':'Changed name'})
        self.assertEqual(self.manager.store.config()['providers'][-1]['hwid_id'],self.hwid)
        self.client.put('/api/providers/'+provider['id'],json={'name':'Changed name','hwid_id':None})
        self.assertIsNone(self.manager.store.config()['providers'][-1]['hwid_id'])

    def test_changed_device_assignment_cannot_reuse_an_old_response(self):
        self.login(); provider=self.add(); self.refresh()
        other=self.manager.store.add_hwid('Other phone','other-authorized-id')['id']
        self.client.put('/api/providers/'+provider['id'],json={'name':'Provider','hwid_id':other})
        self.assertEqual(self.client.get('/api/state').json()['total'],0)
        self.manager.fetcher=lambda *args: (b'invalid',{})
        self.refresh()
        self.assertEqual(self.client.get('/api/state').json()['providers'][0]['status'],'unavailable')

    def test_device_replacement_checks_confirmation_revision_and_affected_providers(self):
        self.login(); provider=self.add(); self.refresh()
        body={'value':'replacement-device-id','revision':1,'provider_ids':[provider['id']]}
        path='/api/hwids/'+self.hwid+'/replace'
        self.assertEqual(self.client.post(path,json=body).status_code,400)
        self.assertEqual(self.client.post(path,json=body|{'confirmed':True,'provider_ids':[]}).status_code,400)
        self.assertEqual(self.client.post(path,json=body|{'confirmed':True,'revision':2}).status_code,400)
        self.assertEqual(self.manager.store.hwid_value(self.hwid),'authorized-device-id')
        self.assertEqual(self.client.post(path,json=body|{'confirmed':True}).status_code,200)
        self.assertEqual(len(self.seen),1)
        self.assertEqual(self.client.get('/api/state').json()['total'],0)
        self.assertEqual(self.client.post(path,json=body|{'confirmed':True,'value':'yet-another-device-id'}).status_code,400)
        self.refresh(); self.refresh()
        self.assertEqual([item[1] for item in self.seen],['authorized-device-id','replacement-device-id','replacement-device-id'])

    def test_changing_devices_waits_for_refresh(self):
        self.login()
        self.manager.busy=True
        try:
            self.assertEqual(self.client.patch('/api/hwids/'+self.hwid,json={'name':'Changed'}).status_code,409)
            self.assertEqual(self.client.delete('/api/hwids/'+self.hwid).status_code,409)
            self.assertEqual(self.client.post('/api/hwids/'+self.hwid+'/replace',json={'value':'replacement-device-id','revision':1,'provider_ids':[],'confirmed':True}).status_code,409)
        finally:
            self.manager.busy=False

    def test_fetch_preview_does_not_save_and_commit_reuses_its_response(self):
        self.login()
        body={'name':'Preview','url':'https://provider.example/private','hwid_id':self.hwid}
        tested=self.client.post('/api/providers/test',json=body)
        self.assertEqual(tested.status_code,200,tested.text)
        self.assertEqual(self.manager.store.config()['providers'],[])
        self.assertFalse((self.manager.store.directory/'snapshot.json').exists())
        for secret in ('authorized-device-id',body['url'],UUID):
            self.assertNotIn(secret,tested.text)
        self.assertEqual(self.client.post('/api/providers',json=body|{'test_token':tested.json()['test_token']}).status_code,201)
        self.assertEqual(len(self.seen),1)
        self.assertEqual(self.client.get('/api/state').json()['providers'][0]['status'],'fresh')
        self.assertEqual(self.client.get('/api/state').json()['counts']['v2board'],0)
        self.assertEqual(self.client.post('/api/check-servers').status_code,202)
        self.manager.worker.join(5)
        self.assertEqual(self.client.get('/api/state').json()['counts']['v2board'],1)
        self.assertEqual(len(self.seen),1)

    def test_failed_fetch_preview_leaves_assignments_and_published_data_unchanged(self):
        self.login(); provider=self.add(); self.refresh()
        previous=self.manager.store.config()
        snapshot=self.manager.snapshot_path.read_bytes()
        self.manager.fetcher=lambda *args: (b'<html>Device limit</html>',{})
        response=self.client.post('/api/providers/test',json={'name':'Provider','provider_id':provider['id'],'hwid_id':None})
        self.assertEqual(response.status_code,400)
        self.assertEqual(self.manager.store.config(),previous)
        self.assertEqual(self.manager.snapshot_path.read_bytes(),snapshot)
        self.assertFalse(self.manager.busy)

    def test_fetch_preview_rejects_changed_expired_and_replaced_device_settings(self):
        self.login()
        body={'name':'Preview','url':'https://provider.example/private','hwid_id':self.hwid}
        token=self.client.post('/api/providers/test',json=body).json()['test_token']
        self.assertEqual(self.client.post('/api/providers',json=body|{'test_token':token,'hwid_id':None}).status_code,400)
        self.client.post('/api/hwids/'+self.hwid+'/replace',json={'value':'replacement-device-id','revision':1,'provider_ids':[],'confirmed':True})
        self.assertEqual(self.client.post('/api/providers',json=body|{'test_token':token}).status_code,400)
        token=self.client.post('/api/providers/test',json=body).json()['test_token']
        self.manager.previews[token]['expires']=0
        self.assertEqual(self.client.post('/api/providers',json=body|{'test_token':token}).status_code,400)
        self.assertEqual(self.manager.store.config()['providers'],[])

    def test_preview_save_failure_restores_previous_cache_and_configuration(self):
        self.login(); provider=self.add(); self.refresh()
        cache=next((self.manager.store.directory/'cache').glob('*.json'))
        original=cache.read_bytes()
        body={'name':'Provider','hwid_id':None}
        token=self.client.post('/api/providers/test',json=body|{'provider_id':provider['id']}).json()['test_token']
        with patch.object(self.manager.store,'save_config',side_effect=common.SubscriptionError('simulated database failure')):
            self.assertEqual(self.client.put('/api/providers/'+provider['id'],json=body|{'test_token':token}).status_code,400)
        self.assertEqual(cache.read_bytes(),original)
        self.assertEqual(self.manager.store.config()['providers'][0]['hwid_id'],self.hwid)


if __name__ == '__main__':
    unittest.main()

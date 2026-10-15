import concurrent.futures
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from subscription_parser.debug_log import DebugLog, RETENTION_SECONDS


class DebugLogTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.now = 1700000000.0
        self.logs = DebugLog(self.root, max_bytes=1024, clock=lambda: self.now)

    def contents(self):
        return b''.join(path.read_bytes() for path in self.logs.paths() if path.exists())

    def test_rotation_has_a_hard_total_size_bound_and_survives_restart(self):
        for count in range(150):
            self.logs.write('run_completed', servers=count, published=count)
        paths = [path for path in self.logs.paths() if path.exists()]
        self.assertEqual(len(paths), 4)
        self.assertTrue(all(path.stat().st_size <= 1024 for path in paths))
        self.assertLessEqual(sum(path.stat().st_size for path in paths), 4096)
        self.assertTrue(all(path.stat().st_mode & 0o777 == 0o600 for path in paths))
        self.assertEqual(self.logs.directory.stat().st_mode & 0o777, 0o700)
        before = self.logs.recent()['entries']
        self.assertEqual(before[0]['context']['servers'], 149)
        self.assertGreater(before[-1]['context']['servers'], 0)
        reopened = DebugLog(self.root, max_bytes=1024, clock=lambda: self.now)
        self.assertEqual(reopened.recent()['entries'], before)

    def test_age_cleanup_removes_old_records_from_active_and_rotated_files(self):
        for _ in range(12):
            self.logs.write('started')
        self.now += 6 * 86400
        self.logs.write('stopped')
        self.now += 2 * 86400
        # This is the same maintenance call made by the idle scheduler.
        self.logs.maintain()
        entries = self.logs.recent()['entries']
        self.assertEqual([entry['event'] for entry in entries], ['stopped'])
        self.assertNotIn(b'"event":"started"', self.contents())
        self.now += RETENTION_SECONDS + 1
        self.logs.maintain()
        self.assertEqual(list(self.logs.directory.iterdir()), [])

    def test_expired_entries_are_hidden_before_the_next_hourly_cleanup(self):
        self.logs.write('started')
        self.now += RETENTION_SECONDS - 10
        self.logs.maintain()
        self.now += 20
        self.assertEqual(self.logs.recent()['entries'], [])
        self.assertIn(b'Application started', self.contents())
        self.now += 3600
        self.logs.maintain()
        self.assertEqual(self.contents(), b'')

    def test_only_safe_context_and_exception_locations_reach_disk_or_api(self):
        secret = 'never-store-this-password'
        try:
            raise ValueError('https://provider.example/' + secret)
        except ValueError as error:
            self.logs.write('provider_failed', error=error, provider_id='provider-123',
                            reason=secret, password=secret, hwid=secret, url=secret, operation=secret,
                            node_id='https://provider.example/' + secret)
        self.logs.write(secret, password=secret)  # Arbitrary messages are rejected.
        record = self.logs.recent()['entries'][0]
        self.assertEqual(record['context']['error_type'], 'ValueError')
        self.assertIn('test_debug_log.py:', record['context']['trace'][0])
        self.assertEqual(record['context']['provider_id'], 'provider-123')
        self.assertNotIn(secret, self.contents().decode())
        self.assertNotIn(secret, json.dumps(self.logs.recent()))
        self.assertNotIn(str(self.root), self.contents().decode())
        self.logs.write('provider_failed', error=ValueError('provider returned HTTP 429'))
        self.assertEqual(self.logs.recent()['entries'][0]['context']['reason'], 'provider returned HTTP 429')

    def test_log_write_failure_is_reported_without_raising_and_can_recover(self):
        self.logs.write('started')
        with patch('subscription_parser.debug_log.os.open', side_effect=PermissionError('private filesystem detail')):
            self.logs.write('run_started')
            self.assertFalse(self.logs.recent()['available'])
        self.logs.write('run_completed')
        self.assertTrue(self.logs.recent()['available'])
        self.assertNotIn(b'private filesystem detail', self.contents())

    def test_concurrent_writes_remain_valid_and_bounded(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda count: self.logs.write('run_completed', servers=count), range(100)))
        for line in self.contents().splitlines():
            self.assertEqual(json.loads(line)['event'], 'run_completed')
        self.assertLessEqual(len(self.contents()), 4096)
        self.assertTrue(self.logs.recent()['available'])

    def test_latest_results_are_capped_and_filter_errors_and_warnings(self):
        logs = DebugLog(self.root, clock=lambda: self.now)
        for _ in range(220):
            logs.write('started')
        logs.write('provider_failed')
        logs.write('request_failed')
        self.assertEqual(len(logs.recent()['entries']), 200)
        self.assertEqual([entry['level'] for entry in logs.recent('warning')['entries']], ['error', 'warning'])
        self.assertEqual([entry['level'] for entry in logs.recent('error')['entries']], ['error'])

    def test_partial_or_invalid_lines_do_not_break_the_log_viewer(self):
        self.logs.write('started')
        with self.logs.paths()[0].open('ab') as stream:
            stream.write(b'not-json\n{"timestamp":1700000000,"event":[]}\n{"event":"started"}\n{"partial":')
        self.assertEqual([entry['event'] for entry in self.logs.recent()['entries']], ['started'])
        self.now += 3600
        self.logs.maintain()
        self.assertNotIn(b'not-json', self.contents())


if __name__ == '__main__':
    unittest.main()

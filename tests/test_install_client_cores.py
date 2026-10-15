"""Installer regressions: direct downloads, bounded retries and required hashes."""
import contextlib
import copy
import gzip
import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import zipfile

from scripts import install_client_cores as installer


class CoreInstallerTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.lock = json.loads(installer.LOCK_FILE.read_text())
        output = contextlib.redirect_stdout(io.StringIO())
        output.__enter__(); self.addCleanup(output.__exit__, None, None, None)

    def run_installer(self, lock, machine='aarch64'):
        path = self.root / 'client-cores.lock.json'
        path.write_text(json.dumps(lock))
        destination = self.root / 'cores'
        with patch.object(installer, 'LOCK_FILE', path), \
                patch.object(installer.platform, 'system', return_value='Linux'), \
                patch.object(installer.platform, 'machine', return_value=machine), \
                patch('sys.argv', ['install_client_cores.py', '--destination', str(destination)]):
            installer.main()
        return destination

    def test_both_architectures_install_all_archive_formats_without_api_requests(self):
        for architecture, machine in [('arm64', 'aarch64'), ('amd64', 'x86_64')]:
            lock = copy.deepcopy(self.lock)
            downloads, expected = {}, {}
            for release in lock['releases']:
                binary = release['binary']
                asset = release['assets'][architecture]
                content = (binary + ':' + architecture).encode()
                expected[binary] = content
                if asset['name'].endswith('.tar.gz'):
                    buffer = io.BytesIO()
                    with tarfile.open(fileobj=buffer, mode='w:gz') as archive:
                        member = tarfile.TarInfo('release/' + binary)
                        member.size = len(content)
                        archive.addfile(member, io.BytesIO(content))
                    payload = buffer.getvalue()
                elif asset['name'].endswith('.zip'):
                    buffer = io.BytesIO()
                    with zipfile.ZipFile(buffer, 'w') as archive:
                        archive.writestr('release/' + binary, content)
                    payload = buffer.getvalue()
                else:
                    payload = gzip.compress(content)
                asset['sha256'] = hashlib.sha256(payload).hexdigest()
                url = f"https://github.com/{release['repository']}/releases/download/{release['version']}/{asset['name']}"
                downloads[url] = payload
            def response(request, timeout):
                self.assertNotIn('Authorization', request.headers)
                self.assertEqual(timeout, 60)
                self.assertIn(request.full_url, downloads)  # REST API calls cannot succeed.
                return io.BytesIO(downloads[request.full_url])
            with self.subTest(architecture=architecture), patch.object(installer.urllib.request, 'urlopen', side_effect=response) as fetch:
                destination = self.run_installer(lock, machine)
                self.assertEqual(fetch.call_count, 3)
                for binary, content in expected.items():
                    self.assertEqual((destination / binary).read_bytes(), content)
                    self.assertEqual((destination / binary).stat().st_mode & 0o777, 0o700)
                self.assertFalse(list(destination.glob('*.download')))

    def test_checksum_mismatch_cannot_extract_or_replace_existing_binary(self):
        destination = self.root / 'cores'; destination.mkdir()
        binary = self.lock['releases'][0]['binary']
        (destination / binary).write_bytes(b'previous verified binary')
        with patch.object(installer, 'download', return_value=b'tampered archive') as fetch, \
                patch.object(installer.gzip, 'decompress') as decompress:
            with self.assertRaisesRegex(SystemExit, 'Checksum mismatch'):
                self.run_installer(self.lock)
            self.assertEqual(fetch.call_count, 1)
            decompress.assert_not_called()
        self.assertEqual((destination / binary).read_bytes(), b'previous verified binary')
        self.assertFalse(list(destination.glob('*.download')))

    def test_missing_or_invalid_checksum_stops_before_network_access(self):
        for digest in [None, '', 'not-a-checksum', '0' * 63]:
            lock = copy.deepcopy(self.lock)
            lock['releases'][0]['assets']['arm64']['sha256'] = digest
            with self.subTest(digest=digest), patch.object(installer, 'download') as fetch:
                with self.assertRaisesRegex(SystemExit, 'pinned SHA256'):
                    self.run_installer(lock)
                fetch.assert_not_called()

    def test_temporary_http_and_network_failures_retry(self):
        responses = [urllib.error.HTTPError('https://github.com/example', 503, 'Unavailable', {}, None),
                     urllib.error.URLError('temporary connection failure'), io.BytesIO(b'archive')]
        with patch.object(installer.urllib.request, 'urlopen', side_effect=responses) as fetch, \
                patch.object(installer.time, 'sleep') as sleep:
            self.assertEqual(installer.download('https://github.com/example'), b'archive')
            self.assertEqual(fetch.call_count, 3)
            self.assertEqual([call.args[0] for call in sleep.call_args_list], [2, 4])

    def test_rate_limit_retry_after_is_respected(self):
        for status in [403, 429]:
            error = urllib.error.HTTPError('https://github.com/example', status, 'Throttled', {'Retry-After': '12'}, None)
            with self.subTest(status=status), patch.object(installer.urllib.request, 'urlopen', side_effect=[error, io.BytesIO(b'ok')]), \
                    patch.object(installer.time, 'sleep') as sleep:
                self.assertEqual(installer.download('https://github.com/example'), b'ok')
                sleep.assert_called_once_with(12)

    def test_long_rate_limit_wait_fails_without_early_retry(self):
        error = urllib.error.HTTPError('https://github.com/example', 429, 'Throttled', {'Retry-After': '120'}, None)
        with patch.object(installer.urllib.request, 'urlopen', side_effect=error) as fetch, \
                patch.object(installer.time, 'sleep') as sleep:
            with self.assertRaisesRegex(SystemExit, 'retry the build later'):
                installer.download('https://github.com/example')
            self.assertEqual(fetch.call_count, 1)
            sleep.assert_not_called()

    def test_permanent_http_errors_do_not_retry(self):
        for status in [403, 404]:
            error = urllib.error.HTTPError('https://github.com/example', status, 'Unavailable', {}, None)
            with self.subTest(status=status), patch.object(installer.urllib.request, 'urlopen', side_effect=error) as fetch, \
                    patch.object(installer.time, 'sleep') as sleep:
                with self.assertRaisesRegex(SystemExit, f'HTTP {status}'):
                    installer.download('https://github.com/example')
                self.assertEqual(fetch.call_count, 1)
                sleep.assert_not_called()

    def test_network_retries_are_bounded(self):
        with patch.object(installer.urllib.request, 'urlopen', side_effect=TimeoutError) as fetch, \
                patch.object(installer.time, 'sleep') as sleep:
            with self.assertRaisesRegex(SystemExit, 'after 3 attempts'):
                installer.download('https://github.com/example')
            self.assertEqual(fetch.call_count, 3)
            self.assertEqual(sleep.call_count, 2)

    def test_oversized_download_is_rejected(self):
        with patch.object(installer, 'MAX_DOWNLOAD_BYTES', 4), \
                patch.object(installer.urllib.request, 'urlopen', return_value=io.BytesIO(b'12345')):
            with self.assertRaisesRegex(SystemExit, 'size limit'):
                installer.download('https://github.com/example')


if __name__ == '__main__':
    unittest.main()

from email.message import Message
from io import BytesIO
import ssl
import unittest
from unittest.mock import MagicMock, patch
import urllib.error
import urllib.response

from subscription_parser import fetch


class FetchTests(unittest.TestCase):
    URL = 'https://provider.example/private-token'

    def setUp(self):
        sleeper = patch.object(fetch.time, 'sleep')
        self.sleep = sleeper.start()
        self.addCleanup(sleeper.stop)

    def response(self, data=b'valid subscription', *, status=200, headers=None):
        response = MagicMock()
        response.__enter__.return_value = response
        response.headers = headers or {'Content-Type': 'application/json'}
        response.status = status
        response.read.return_value = data
        return response

    def download(self):
        return fetch.fetch_subscription(self.URL, 'device-id-1234', 'Happ/2.3.1', 5)

    def test_no_device_id_omits_header_and_fixed_ids_are_sent_exactly(self):
        for identity in (None, 'existing-device-id'):
            response = unittest.mock.MagicMock()
            response.__enter__.return_value = response
            response.headers = {}
            response.status = 200
            response.read.return_value = b'valid response'
            with patch.object(fetch.urllib.request, 'build_opener') as opener:
                opener.return_value.open.return_value = response
                fetch.fetch_subscription('https://provider.example/sub', identity, 'test', 5)
                headers = dict(opener.return_value.open.call_args.args[0].header_items())
                if identity is None:
                    self.assertNotIn('X-hwid', headers)
                else:
                    self.assertEqual(headers['X-hwid'], identity)

    def test_hwid_limits_are_not_treated_as_successful_subscriptions(self):
        for header, message in [("x-hwid-max-devices-reached", "device limit reached"),
                                ("X-HWID-Limit", "device limit reached"),
                                ("x-hwid-not-supported", "device ID header")]:
            response = unittest.mock.MagicMock()
            response.__enter__.return_value = response
            response.headers = {header: "TRUE"}
            with patch.object(fetch.urllib.request, "build_opener") as opener:
                opener.return_value.open.return_value = response
                with self.assertRaisesRegex(fetch.SubscriptionError, message):
                    fetch.fetch_subscription("https://provider.example/token", "device-id-1234", "Happ/2.3.1", 5)
                response.read.assert_not_called()
                self.assertEqual(opener.return_value.open.call_count, 1)
                self.sleep.assert_not_called()

    def test_https_required(self):
        for url in ('http://provider.example/token', 'ftp://provider.example/token',
                    'file:///private-token', 'https:///private-token',
                    'https://user:secret@provider.example/token', 'https://user@provider.example/token'):
            with self.subTest(url=url), patch.object(fetch.urllib.request, 'build_opener') as opener:
                with self.assertRaisesRegex(fetch.SubscriptionError, 'HTTPS without embedded login'):
                    fetch.fetch_subscription(url, 'device-id-1234', 'test', 5)
                opener.assert_not_called()

    def test_invalid_device_header_is_rejected_before_network_access(self):
        with patch.object(fetch.urllib.request, 'build_opener') as opener:
            with self.assertRaises(fetch.SubscriptionError):
                fetch.fetch_subscription(self.URL, 'device\r\nx-injected: value', 'test', 5)
            opener.assert_not_called()

    @staticmethod
    def redirect_response(location, status=302):
        headers = Message()
        headers['Location'] = location
        response = urllib.response.addinfourl(BytesIO(b''), headers, FetchTests.URL, status)
        response.msg = 'Redirect'
        return response

    def test_real_opener_rejects_https_to_http_redirect_before_sending_device(self):
        with patch.object(fetch.urllib.request.HTTPSHandler, 'https_open',
                          return_value=self.redirect_response('http://provider.example/token')) as https_open, \
             patch.object(fetch.urllib.request.HTTPHandler, 'http_open') as http_open:
            with self.assertRaisesRegex(fetch.SubscriptionError, 'redirect to non-HTTPS'):
                self.download()
            self.assertEqual(https_open.call_count, 1)
            http_open.assert_not_called()
            self.sleep.assert_not_called()

    def test_https_redirects_preserve_authorized_device_and_user_agent(self):
        destination = 'https://cdn.example/sub'
        for status in (301, 302, 303, 307, 308):
            with self.subTest(status=status):
                response = urllib.response.addinfourl(BytesIO(b'redirected'), Message(), destination, 200)
                response.msg = 'OK'
                with patch.object(fetch.urllib.request.HTTPSHandler, 'https_open',
                                  side_effect=[self.redirect_response(destination, status), response]) as open_https:
                    data, metadata = self.download()
                self.assertEqual(data, b'redirected')
                self.assertEqual(metadata['http_status'], 200)
                redirected = open_https.call_args.args[0]
                self.assertEqual(redirected.full_url, destination)
                self.assertEqual(redirected.get_header('X-hwid'), 'device-id-1234')
                self.assertEqual(redirected.get_header('User-agent'), 'Happ/2.3.1')
                self.assertEqual(redirected.timeout, 5)

    def test_redirect_loop_is_bounded_and_does_not_expose_subscription_url(self):
        with patch.object(fetch.urllib.request.HTTPSHandler, 'https_open',
                          side_effect=lambda request: self.redirect_response(self.URL)) as open_https:
            with self.assertRaisesRegex(fetch.SubscriptionError, '^provider returned HTTP 302$'):
                self.download()
            self.assertLessEqual(open_https.call_count, 11)
            self.sleep.assert_not_called()

    def test_response_at_size_limit_is_accepted_with_metadata(self):
        response = self.response(b'x' * fetch.MAX_BYTES)
        with patch.object(fetch.urllib.request, 'build_opener') as opener:
            opener.return_value.open.return_value = response
            data, metadata = self.download()
        self.assertEqual(len(data), fetch.MAX_BYTES)
        self.assertEqual(metadata['bytes'], len(data))
        self.assertEqual(metadata['content_type'], 'application/json')
        self.assertTrue(metadata['fetched_at'])
        response.read.assert_called_once_with(fetch.MAX_BYTES + 1)
        response.__exit__.assert_called_once()

    def test_oversized_response_is_bounded_closed_and_not_retried(self):
        response = self.response(b'x' * (fetch.MAX_BYTES + 1))
        with patch.object(fetch.urllib.request, 'build_opener') as opener:
            opener.return_value.open.return_value = response
            with self.assertRaisesRegex(fetch.SubscriptionError, '12 MiB limit'):
                self.download()
            self.assertEqual(opener.return_value.open.call_count, 1)
        response.read.assert_called_once_with(fetch.MAX_BYTES + 1)
        response.__exit__.assert_called_once()
        self.sleep.assert_not_called()

    def test_retryable_http_errors_recover_without_changing_device_or_timeout(self):
        for status in (429, 500, 502, 503, 504):
            with self.subTest(status=status), patch.object(fetch.urllib.request, 'build_opener') as opener:
                self.sleep.reset_mock()
                opener.return_value.open.side_effect = [
                    urllib.error.HTTPError(self.URL, status, 'secret', {}, None), self.response()]
                self.assertEqual(self.download()[0], b'valid subscription')
                self.assertEqual(opener.return_value.open.call_count, 2)
                for call in opener.return_value.open.call_args_list:
                    self.assertEqual(call.args[0].get_header('X-hwid'), 'device-id-1234')
                    self.assertEqual(call.kwargs['timeout'], 5)
                self.sleep.assert_called_once()

    def test_retryable_http_errors_stop_after_second_failure(self):
        for status in (429, 500, 503):
            with self.subTest(status=status), patch.object(fetch.urllib.request, 'build_opener') as opener:
                self.sleep.reset_mock()
                opener.return_value.open.side_effect = urllib.error.HTTPError(self.URL, status, 'secret', {}, None)
                with self.assertRaisesRegex(fetch.SubscriptionError, f'^provider returned HTTP {status}$'):
                    self.download()
                self.assertEqual(opener.return_value.open.call_count, 2)
                self.sleep.assert_called_once()

    def test_permanent_http_errors_are_not_retried(self):
        for status in (400, 401, 403, 404, 410):
            with self.subTest(status=status), patch.object(fetch.urllib.request, 'build_opener') as opener:
                opener.return_value.open.side_effect = urllib.error.HTTPError(self.URL, status, 'secret', {}, None)
                with self.assertRaisesRegex(fetch.SubscriptionError, f'^provider returned HTTP {status}$'):
                    self.download()
                self.assertEqual(opener.return_value.open.call_count, 1)
                self.sleep.assert_not_called()

    def test_network_timeouts_and_tls_errors_recover_or_stop_after_one_retry(self):
        errors = [urllib.error.URLError(self.URL), TimeoutError(self.URL),
                  ConnectionResetError(self.URL), ssl.SSLError(self.URL), OSError(self.URL)]
        for error in errors:
            for recovers in (True, False):
                with self.subTest(error=type(error).__name__, recovers=recovers), \
                     patch.object(fetch.urllib.request, 'build_opener') as opener:
                    self.sleep.reset_mock()
                    opener.return_value.open.side_effect = [error, self.response() if recovers else error]
                    if recovers:
                        self.assertEqual(self.download()[0], b'valid subscription')
                    else:
                        with self.assertRaises(fetch.SubscriptionError) as caught:
                            self.download()
                        self.assertEqual(str(caught.exception),
                                         'subscription download failed (network, timeout or TLS error)')
                    self.assertEqual(opener.return_value.open.call_count, 2)
                    self.sleep.assert_called_once()

    def test_timeout_reading_body_closes_response_and_restarts_download(self):
        response = self.response()
        response.read.side_effect = TimeoutError('private-token')
        with patch.object(fetch.urllib.request, 'build_opener') as opener:
            opener.return_value.open.side_effect = [response, self.response(b'complete replacement')]
            self.assertEqual(self.download()[0], b'complete replacement')
            self.assertEqual(opener.return_value.open.call_count, 2)
        response.__exit__.assert_called_once()
        self.sleep.assert_called_once()

    def test_provider_error_does_not_echo_token(self):
        url = "https://provider.example/private-token"
        with patch.object(fetch.urllib.request, "build_opener") as opener:
            opener.return_value.open.side_effect = urllib.error.HTTPError(url, 403, "secret", {}, None)
            with self.assertRaises(fetch.SubscriptionError) as caught:
                fetch.fetch_subscription(url, "device-id-1234", "test", 5)
            self.assertEqual(str(caught.exception), "provider returned HTTP 403")


if __name__ == "__main__":
    unittest.main()

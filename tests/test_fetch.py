import unittest
from unittest.mock import patch
import urllib.error

from subscription_parser import fetch


class FetchTests(unittest.TestCase):
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
                                ("x-hwid-not-supported", "device ID header")]:
            response = unittest.mock.MagicMock()
            response.__enter__.return_value = response
            response.headers = {header: "true"}
            with patch.object(fetch.urllib.request, "build_opener") as opener:
                opener.return_value.open.return_value = response
                with self.assertRaisesRegex(fetch.SubscriptionError, message):
                    fetch.fetch_subscription("https://provider.example/token", "device-id-1234", "Happ/2.3.1", 5)
                response.read.assert_not_called()

    def test_https_required(self):
        with self.assertRaises(fetch.SubscriptionError):
            fetch.fetch_subscription("http://provider.example/token", "device-id-1234", "test", 5)

    def test_provider_error_does_not_echo_token(self):
        url = "https://provider.example/private-token"
        with patch.object(fetch.urllib.request, "build_opener") as opener:
            opener.return_value.open.side_effect = urllib.error.HTTPError(url, 403, "secret", {}, None)
            with self.assertRaises(fetch.SubscriptionError) as caught:
                fetch.fetch_subscription(url, "device-id-1234", "test", 5)
            self.assertEqual(str(caught.exception), "provider returned HTTP 403")


if __name__ == "__main__":
    unittest.main()

import base64
import copy
import json
import unittest
import urllib.parse

from subscription_parser import xray_transports as xt

UUID = "01234567-89ab-4cde-8fab-0123456789ab"
PUBLIC_KEY = base64.urlsafe_b64encode(bytes(range(32))).decode().rstrip("=")


def outbound(network="tcp", security="reality"):
    result = {
        "tag": "proxy", "protocol": "vless",
        "settings": {"vnext": [{"address": "proxy.example", "port": 443,
                                "users": [{"id": UUID, "encryption": "none", "flow": "xtls-rprx-vision"}]}]},
        "streamSettings": {"network": network, "security": security},
    }
    if security == "reality":
        result["streamSettings"]["realitySettings"] = {
            "serverName": "cover.example", "publicKey": PUBLIC_KEY,
            "shortId": "01ab", "spiderX": "/a?b=c&d=1", "fingerprint": "firefox",
        }
    elif security == "tls":
        result["streamSettings"]["tlsSettings"] = {"serverName": "cover.example", "alpn": ["h2", "http/1.1"]}
    return result


def query(uri):
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(uri).query, keep_blank_values=True))


class TransportTests(unittest.TestCase):
    def test_null_grpc_service_name_uses_the_empty_default(self):
        out = outbound('grpc')
        out['settings']['vnext'][0]['users'][0].pop('flow')
        out['streamSettings']['grpcSettings'] = {'serviceName': None}
        self.assertEqual(query(xt.vless_link(out, 'grpc'))['serviceName'], '')
        native, _ = xt.hiddify_outbound(out, 'grpc')
        self.assertEqual(native['transport']['service_name'], '')
        for invalid in (False, 12, ['service']):
            out['streamSettings']['grpcSettings']['serviceName'] = invalid
            with self.assertRaises(xt.SubscriptionError):
                xt.vless_link(out, 'grpc')

    def test_reality_credentials_and_reserved_characters_round_trip(self):
        uri = xt.vless_link(outbound(), "🇩🇪 Germany & fast #1")
        parsed = urllib.parse.urlsplit(uri)
        self.assertEqual(parsed.username, UUID)
        self.assertEqual(parsed.hostname, "proxy.example")
        self.assertEqual(parsed.port, 443)
        self.assertEqual(urllib.parse.unquote(parsed.fragment), "🇩🇪 Germany & fast #1")
        self.assertEqual(query(uri), {"encryption": "none", "flow": "xtls-rprx-vision",
                                    "type": "tcp", "security": "reality", "sni": "cover.example",
                                    "fp": "firefox", "pbk": PUBLIC_KEY, "sid": "01ab", "spx": "/a?b=c&d=1"})

    def test_ipv6_and_internationalized_hosts(self):
        for address, expected in [("2001:db8::1", "2001:db8::1"), ("münchen.example", "xn--mnchen-3ya.example")]:
            out = outbound()
            out["settings"]["vnext"][0]["address"] = address
            parsed = urllib.parse.urlsplit(xt.vless_link(out, "test"))
            self.assertEqual(parsed.hostname, expected)
            self.assertEqual(parsed.port, 443)

    def test_websocket_path_early_data_and_host_preserved(self):
        out = outbound("ws", "tls")
        out["streamSettings"]["wsSettings"] = {"path": "/a+b/%2F?ed=2560&x=a%26b", "headers": {"Host": "cdn.example"}}
        q = query(xt.vless_link(out, "ws"))
        self.assertEqual(q["path"], "/a+b/%2F?ed=2560&x=a%26b")
        self.assertEqual(q["host"], "cdn.example")
        self.assertEqual(q["alpn"], "h2,http/1.1")

    def test_grpc_uses_xray_multimode_not_provider_mode_field(self):
        for settings, expected in [({"mode": False}, "gun"), ({"mode": True}, "gun"),
                                   ({"multiMode": True}, "multi"),
                                   ({"multiMode": True, "mode": False}, "multi")]:
            out = outbound("grpc")
            out["streamSettings"]["grpcSettings"] = {"serviceName": "api/path?x=1", "authority": "grpc.example", **settings}
            q = query(xt.vless_link(out, "grpc"))
            self.assertEqual(q["mode"], expected)
            self.assertEqual(q["serviceName"], "api/path?x=1")
            self.assertEqual(q["authority"], "grpc.example")

    def test_xhttp_extra_is_json_and_not_python_string(self):
        out = outbound("xhttp", "tls")
        out["streamSettings"]["xhttpSettings"] = {"mode": "auto", "path": "/api/", "host": "cdn.example",
            "extra": {"noSSEHeader": True}, "headers": {"X-Token": "a&b"}}
        q = query(xt.vless_link(out, "xhttp"))
        self.assertEqual(json.loads(q["extra"]), {"noSSEHeader": True, "headers": {"X-Token": "a&b"}})

    def test_native_ws_early_data_headers_and_heartbeat(self):
        out = outbound("ws", "tls")
        out["streamSettings"]["wsSettings"] = {
            "path": "/ws?ed=2560&token=a%26b", "host": "cdn.example",
            "headers": {"X-Required": "abc"}, "heartbeatPeriod": 30}
        native, notes = xt.hiddify_outbound(out, "native")
        self.assertEqual(native["transport"]["path"], "/ws?token=a%26b")
        self.assertEqual(native["transport"]["max_early_data"], 2560)
        self.assertEqual(native["transport"]["headers"], {"Host": "cdn.example", "X-Required": "abc"})
        self.assertEqual(len(notes), 1)

    def test_native_reality_credentials_and_grpc(self):
        out = outbound("grpc")
        out["streamSettings"]["grpcSettings"] = {"serviceName": "grpc", "mode": False}
        out["streamSettings"]["realitySettings"]["spiderX"] = "/"
        native, notes = xt.hiddify_outbound(out, "native")
        self.assertEqual(native["uuid"], UUID)
        self.assertEqual(native["tls"]["reality"]["public_key"], PUBLIC_KEY)
        self.assertEqual(native["tls"]["reality"]["short_id"], "01ab")
        self.assertEqual(native["tls"]["alpn"], ["h2"])
        self.assertEqual(native["transport"], {"type": "grpc", "service_name": "grpc"})
        self.assertEqual(notes, [])

    def test_dependent_outbound_not_silently_flattened(self):
        out = outbound()
        out["streamSettings"]["sockopt"] = {"dialerProxy": "fragment"}
        with self.assertRaises(xt.NeedsFullConfig):
            xt.vless_link(out, "chain")

    def test_public_key_alias_and_empty_short_id(self):
        out = outbound()
        reality = out["streamSettings"]["realitySettings"]
        reality["password"] = reality.pop("publicKey")
        reality["shortId"] = ""
        q = query(xt.vless_link(out, "test"))
        self.assertEqual(q["pbk"], PUBLIC_KEY)
        self.assertEqual(q["sid"], "")

    def test_bad_credentials_and_dummy_hosts_rejected(self):
        cases = [("uuid", "REDACTED"), ("key", "REDACTED"), ("sid", "abc"), ("host", "0.0.0.0")]
        for field, value in cases:
            out = outbound()
            if field == "uuid":
                out["settings"]["vnext"][0]["users"][0]["id"] = value
            elif field == "host":
                out["settings"]["vnext"][0]["address"] = value
            else:
                out["streamSettings"]["realitySettings"]["publicKey" if field == "key" else "shortId"] = value
            with self.subTest(field=field), self.assertRaises(xt.SubscriptionError):
                xt.vless_link(out, "test")

    def test_custom_headers_preserved_and_mux_omission_reported(self):
        special = outbound("ws", "tls")
        special["streamSettings"]["wsSettings"] = {"path": "/ws", "headers": {"X-Required": "secret"}}
        original = copy.deepcopy(special)
        with self.assertRaises(xt.NeedsFullConfig):
            xt.vless_link(special, "headers")
        native, _ = xt.hiddify_outbound(special, "headers")
        self.assertEqual(native["transport"]["headers"]["X-Required"], "secret")
        self.assertEqual(special, original)

        mux = outbound()
        mux["mux"] = {"enabled": True, "concurrency": 8, "xudpProxyUDP443": "reject"}
        original = copy.deepcopy(mux)
        with self.assertRaises(xt.NeedsFullConfig):
            xt.vless_link(mux, "mux")
        native, notes = xt.hiddify_outbound(mux, "mux")
        self.assertEqual(native["type"], "vless")
        self.assertNotIn("multiplex", native)
        self.assertTrue(any("mux/XUDP" in message for message in notes))
        self.assertEqual(mux, original)


if __name__ == "__main__":
    unittest.main()

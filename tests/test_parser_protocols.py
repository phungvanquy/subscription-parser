import base64
import json
from pathlib import Path
import unittest

import yaml
from subscription_parser import protocols as p
from subscription_parser.validation import checked_combine

UUID = "01234567-89ab-4cde-8fab-0123456789ab"
KEY = base64.urlsafe_b64encode(bytes(range(32))).decode().rstrip("=")


def native(kind="vless"):
    node = {"type": kind, "tag": "Example", "server": "proxy.example", "server_port": 443}
    if kind in {"vless", "vmess", "tuic"}:
        node["uuid"] = UUID
    if kind in {"shadowsocks", "trojan", "hysteria2", "tuic"}:
        node["password"] = "synthetic-password"
    if kind == "shadowsocks":
        node["method"] = "aes-128-gcm"
    if kind in {"trojan", "hysteria", "hysteria2", "tuic"}:
        node["tls"] = {"enabled": True, "server_name": "cover.example"}
    if kind == "hysteria":
        node.update(auth_str="synthetic-auth", up="10 Mbps", down="50 Mbps")
    return node


class ProtocolTests(unittest.TestCase):
    def test_all_requested_protocols_preserved_across_clients(self):
        kinds = ["vless", "vmess", "shadowsocks", "trojan", "hysteria", "hysteria2", "tuic", "socks", "http"]
        records = p.parse(json.dumps({"outbounds": [native(k) for k in kinds]}).encode(), "source")
        result = p.combine(records)
        self.assertEqual(result["counts"], {"hiddify": 9, "mihomo": 9, "xray": 7})
        self.assertEqual([n["type"] for n in result["hiddify"]["outbounds"]], kinds)
        self.assertIn("ss", [n["type"] for n in result["mihomo"]["proxies"]])
        self.assertEqual({i["client"] for i in result["diagnostics"]}, {"xray"})

    def test_mihomo_yaml_native_headers_and_reality_roundtrip(self):
        raw = {"name": "node", "type": "vless", "server": "example.org", "port": 443, "uuid": UUID,
               "tls": True, "servername": "cover.example", "client-fingerprint": "chrome",
               "reality-opts": {"public-key": KEY, "short-id": "01ab"}, "network": "ws",
               "ws-opts": {"path": "/foo?x=a%26b", "headers": {"Host": "cover.example", "X-Required": "value"}}}
        record = p.parse(yaml.safe_dump({"proxies": [raw]}).encode(), "mihomo")[0]
        self.assertEqual(record.outputs["mihomo"], raw)
        sb = record.outputs["hiddify"]
        self.assertEqual(sb["tls"]["reality"]["short_id"], "01ab")
        self.assertEqual(sb["transport"]["headers"]["X-Required"], "value")
        self.assertEqual(p.native_to_mihomo(sb)["ws-opts"], raw["ws-opts"])

    def test_vmess_base64_link_uses_vmess_not_vless(self):
        obj = {"v": "2", "ps": "VMess test", "add": "example.org", "port": "443", "id": UUID,
               "aid": "0", "scy": "auto", "net": "ws", "type": "none", "host": "cdn.example", "path": "/ws", "tls": "tls"}
        link = "vmess://" + base64.b64encode(json.dumps(obj).encode()).decode()
        node = p.parse(link.encode(), "s")[0]
        self.assertEqual(node.protocol, "vmess")
        self.assertEqual(node.outputs["hiddify"]["uuid"], UUID)
        self.assertEqual(node.outputs["xray"]["protocol"], "vmess")
        self.assertEqual(node.outputs["mihomo"]["alterId"], 0)

    def test_shadowsocks_sip002_and_legacy_ipv6_password(self):
        credentials = "aes-128-gcm:p@ss:word"
        encoded = base64.urlsafe_b64encode(credentials.encode()).decode().rstrip("=")
        links = [f"ss://{encoded}@[2001:db8::1]:443#IPv6", "ss://" + base64.b64encode((credentials + "@[2001:db8::1]:443").encode()).decode()]
        for link in links:
            with self.subTest(link=link):
                node = p.parse(link.encode(), "ss")[0].outputs["hiddify"]
                self.assertEqual(node["password"], "p@ss:word")
                self.assertEqual(node["server"], "2001:db8::1")

    def test_quic_links_preserve_authentication_and_protocol_version(self):
        links = ["hysteria://example.org:443?auth=secret&upmbps=20&downmbps=100#HY1",
                 "hy2://user%3Apass@example.org:443?sni=cover.example&obfs=salamander&obfs-password=obfssecret#HY2",
                 f"tuic://{UUID}:pass@example.org:443?congestion_control=bbr#TUIC"]
        nodes = p.parse("\n".join(links).encode(), "quic")
        self.assertEqual([n.protocol for n in nodes], ["hysteria", "hysteria2", "tuic"])
        self.assertEqual(nodes[0].outputs["mihomo"]["auth-str"], "secret")
        self.assertEqual(nodes[1].outputs["hiddify"]["password"], "user:pass")
        self.assertEqual(nodes[1].outputs["mihomo"]["obfs-password"], "obfssecret")
        self.assertEqual(nodes[2].outputs["hiddify"]["uuid"], UUID)

    def test_xray_hysteria_is_v2_and_retains_original(self):
        node = {"tag": "proxy", "protocol": "hysteria", "settings": {"address": "example.org", "port": 443, "version": 2},
                "streamSettings": {"network": "hysteria", "hysteriaSettings": {"version": 2, "auth": "secret"},
                                   "security": "tls", "tlsSettings": {"serverName": "cover.example"},
                                   "finalmask": {"quicParams": {"congestion": "bbr", "debug": False}}}}
        parsed = p.parse(json.dumps({"outbounds": [node]}).encode(), "s")[0]
        self.assertEqual(parsed.protocol, "hysteria2")
        self.assertEqual(parsed.outputs["mihomo"]["type"], "hysteria2")
        self.assertEqual(parsed.outputs["hiddify"]["password"], "secret")
        self.assertEqual(parsed.outputs["xray"], node)
        self.assertTrue(parsed.notes)

    def test_xray_multiple_servers_and_users_kept(self):
        out = p.native_to_xray(native("vmess"))
        second = {"id": "11234567-89ab-4cde-8fab-0123456789ab", "security": "auto"}
        out["settings"]["vnext"][0]["users"].append(second)
        result = p.combine(p.parse(json.dumps({"outbounds": [out]}).encode(), "s"))
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["counts"]["hiddify"], 2)

    def test_native_unsupported_protocol_preserved_only_for_original_client(self):
        raw = {"name": "SSH", "type": "ssh", "server": "example.org", "port": 22, "username": "test", "password": "test"}
        result = p.combine(p.parse(yaml.safe_dump({"proxies": [raw]}).encode(), "s"))
        self.assertEqual(result["counts"], {"hiddify": 0, "mihomo": 1, "xray": 0})
        self.assertEqual(result["mihomo"]["proxies"][0]["password"], "test")

    def test_original_fragment_chain_preserved_and_namespaced(self):
        node = p.native_to_xray(native())
        node["tag"] = "proxy"
        node["streamSettings"]["sockopt"] = {"dialerProxy": "fragment"}
        fragment = {"tag": "fragment", "protocol": "freedom", "settings": {"fragment": {"packets": "tlshello", "length": "10-20", "interval": "10-20"}}}
        result = p.combine(p.parse(json.dumps({"outbounds": [node, fragment]}).encode(), "s"))
        self.assertEqual(result["counts"], {"hiddify": 0, "mihomo": 0, "xray": 1})
        root, dependency = result["xray"]["outbounds"]
        self.assertEqual(root["streamSettings"]["sockopt"]["dialerProxy"], dependency["tag"])
        self.assertEqual(dependency["settings"], fragment["settings"])

    def test_loopback_helpers_are_not_servers_but_remain_in_dependency_context(self):
        node = p.native_to_xray(native())
        helpers = [{'tag': f'helper-{index}', 'protocol': 'loopback',
                    'settings': {'inboundTag': f'route-{index}'}} for index in range(6)]
        document = {'outbounds': [node, *helpers]}
        records = p.parse(json.dumps([document, document]).encode(), 'provider')
        combined = p.combine(records)
        self.assertEqual(combined['total'], 1)
        self.assertEqual(combined['duplicates_removed'], 1)
        self.assertEqual(combined['counts'], {'hiddify': 1, 'mihomo': 1, 'xray': 1})
        self.assertEqual([item['protocol'] for item in combined['inventory']], ['vless'])
        self.assertEqual(records[0].context, document['outbounds'])
        self.assertFalse(combined['diagnostics'])
        node['streamSettings']['sockopt'] = {'dialerProxy': 'helper-0'}
        chained = p.parse(json.dumps(document).encode(), 'provider')
        bundle = p.bundle(chained[0], 'xray', 'selectable')
        self.assertEqual(len(chained), 1)
        self.assertEqual(bundle[1]['protocol'], 'loopback')
        self.assertEqual(bundle[1]['settings'], helpers[0]['settings'])
        self.assertEqual(bundle[0]['streamSettings']['sockopt']['dialerProxy'], bundle[1]['tag'])

    def test_routing_only_subscription_is_not_a_usable_server_list(self):
        document = {'outbounds': [{'tag': 'helper', 'protocol': 'loopback', 'settings': {'inboundTag': 'route'}}]}
        with self.assertRaisesRegex(p.Error, 'no usable proxy configurations'):
            p.parse(json.dumps(document).encode(), 'provider')

    def test_distinct_dependency_settings_not_deduplicated(self):
        node = p.native_to_xray(native())
        node["tag"] = "proxy"
        node["streamSettings"]["sockopt"] = {"dialerProxy": "fragment"}
        profiles = []
        for length in ["10-20", "30-50"]:
            profiles.append({"outbounds": [node, {"tag": "fragment", "protocol": "freedom", "settings": {"fragment": {"packets": "tlshello", "length": length, "interval": "10-20"}}}]})
        self.assertEqual(p.combine(p.parse(json.dumps(profiles).encode(), "s"))["total"], 2)

    def test_missing_dependency_not_exported(self):
        node = native()
        node["detour"] = "missing"
        result = p.combine(p.parse(json.dumps({"outbounds": [node]}).encode(), "s"))
        self.assertEqual(result["counts"]["hiddify"], 0)
        self.assertTrue(any("unavailable outbound" in i["reason"] for i in result["diagnostics"]))

    def test_same_account_deduplicates_across_providers_but_distinct_uuid_does_not(self):
        first = native()
        second = {**first, "uuid": "11234567-89ab-4cde-8fab-0123456789ab"}
        records = p.parse(json.dumps({"outbounds": [first]}).encode(), "one")
        records += p.parse(json.dumps({"outbounds": [first, second]}).encode(), "two")
        result = p.combine(records)
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["inventory"][0]["sources"], ["one", "two"])

    def test_dummy_hwid_error_profiles_cannot_replace_successful_cache(self):
        node = p.native_to_xray(native())
        node["settings"]["vnext"][0]["address"] = "0.0.0.0"
        with self.assertRaises(p.Error):
            p.parse(json.dumps({"outbounds": [node]}).encode(), "s")

    def test_base64_and_mihomo_json_inputs(self):
        raw = json.dumps({"proxies": [{"name": "SS", "type": "ss", "server": "example.org", "port": 443,
                                      "cipher": "aes-128-gcm", "password": "test"}]}).encode()
        for payload in [raw, base64.b64encode(raw)]:
            self.assertEqual(p.parse(payload, "s")[0].protocol, "shadowsocks")

    def test_hysteria_bandwidth_units_preserved_without_float_json(self):
        proxy = {"type": "hysteria2", "name": "hy2", "server": "example.org", "port": 443,
                 "password": "test", "up": "50 Mbps", "down": 100}
        converted = p.mihomo_to_native(proxy, [])
        self.assertEqual(converted["up_mbps"], 50)
        self.assertIsInstance(converted["up_mbps"], int)
        self.assertEqual(converted["down_mbps"], 100)
        v1 = native("hysteria")
        v1.pop("up"); v1.pop("down")
        v1.update(up_mbps=10, down_mbps=50)
        restored = p.native_to_mihomo(v1)
        self.assertEqual(restored["up"], "10 Mbps")
        self.assertEqual(restored["down"], "50 Mbps")

    def test_failed_core_validation_excludes_only_bad_node(self):
        one, two = native(), {**native(), "server": "unsupported.example"}
        entries = p.parse(json.dumps({"outbounds": [one, two]}).encode(), "s")
        def checker(target, document, root):
            if target != "hiddify":
                return True
            return all(n.get("server") != "unsupported.example" for n in document["outbounds"])
        result = checked_combine(entries, Path("."), checker)
        self.assertEqual(result["counts"], {"hiddify": 1, "mihomo": 2, "xray": 2})
        self.assertEqual(result["validation"]["hiddify"], {"status": "passed", "rejected": 1})


if __name__ == "__main__":
    unittest.main()

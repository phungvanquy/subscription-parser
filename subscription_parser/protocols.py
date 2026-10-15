"""Protocol-preserving adapters. Each client receives its own native schema.

Unknown native protocols stay in their original client's export. Cross-client
conversion rejects settings it cannot represent, rather than deleting them.
"""
from __future__ import annotations

import base64
import copy
from dataclasses import dataclass, field
import hashlib
import json
import re
from urllib.parse import parse_qsl, unquote, urlsplit

import yaml
from . import common, hysteria
from .xray_transports import hiddify_outbound

Error = common.SubscriptionError
SKIP = {"direct", "block", "dns", "selector", "urltest", "freedom", "blackhole", "reject", "pass"}
COMMON = {"vless", "vmess", "shadowsocks", "trojan", "hysteria", "hysteria2", "tuic", "socks", "http"}
TARGETS = ("hiddify", "mihomo", "xray")


def only(data, keys, description):
    common.reject_extra(data, set(keys), description)


def b64(value):
    try:
        return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True).decode()
    except (ValueError, UnicodeError):
        raise Error("invalid base64 proxy data") from None


def enabled(value):
    return str(value).lower() in {"true", "1", "yes"}


def endpoint(node):
    port = node.get("server_port")
    if node.get("type") in {"hysteria", "hysteria2"} and node.get("server_ports"):
        if port is not None:
            raise Error("Hysteria requires either server_port or server_ports")
        port = hysteria.ports(",".join(node["server_ports"]).replace(":", "-"))[0].split('-')[0]
    return common.host_port(node.get("server"), port)


def valid_native(node):
    host, port = endpoint(node)
    node["server"] = host
    if not node.get("server_ports"):
        node["server_port"] = port
    protocol = node.get("type")
    if protocol in {"vless", "vmess", "tuic"}:
        node["uuid"] = common.valid_uuid(node.get("uuid"))
    if protocol in {"shadowsocks", "trojan", "hysteria2"} and not node.get("password"):
        raise Error("missing proxy password")
    if protocol == "shadowsocks" and not node.get("method"):
        raise Error("missing Shadowsocks cipher")
    return node


def stream_to_native(stream, identifier, notes):
    # Reuse the previously validated VLESS transport/TLS implementation.
    dummy = {"protocol": "vless", "settings": {"vnext": [{"address": "example.org", "port": 443,
             "users": [{"id": identifier, "encryption": "none"}]}]}, "streamSettings": copy.deepcopy(stream)}
    tls = dummy["streamSettings"].get("tlsSettings", {})
    insecure = tls.pop("allowInsecure", False)
    native, messages = hiddify_outbound(dummy, "transport")
    notes.extend(messages)
    result = {k: native[k] for k in ("transport", "tls") if k in native}
    if insecure:
        result.setdefault("tls", {"enabled": True})["insecure"] = True
    return result


def xray_to_native(raw, notes):
    node = copy.deepcopy(raw)
    protocol, settings = node["protocol"], node.get("settings", {})
    stream = node.get("streamSettings", {})
    only(node, {"tag", "protocol", "settings", "streamSettings", "mux"}, "Xray outbound")
    if node.get("mux", {}).get("enabled"):
        notes.append("Xray mux/XUDP tuning uses the destination client's defaults")
    result = {"type": protocol}
    if protocol in {"vless", "vmess"}:
        server = settings["vnext"][0]
        user = server["users"][0]
        only(settings, {"vnext"}, "Xray settings")
        only(server, {"address", "port", "users"}, "Xray server")
        only(user, {"id", "encryption", "flow", "security", "alterId", "email", "level"}, "Xray user")
        if protocol == "vless" and user.get("encryption", "none") != "none":
            raise Error("VLESS encryption is not portable to this client")
        result.update(server=server["address"], server_port=server["port"], uuid=user["id"])
        result.update(stream_to_native(stream, user["id"], notes))
        if protocol == "vless":
            if user.get("flow"):
                result["flow"] = user["flow"]
            result["packet_encoding"] = "xudp"
        else:
            result.update(security=user.get("security", "auto"), alter_id=int(user.get("alterId", 0)))
    elif protocol in {"shadowsocks", "trojan", "socks", "http"}:
        server = settings["servers"][0]
        only(settings, {"servers"}, "Xray settings")
        only(server, {"address", "port", "password", "method", "email", "level", "users"}, "Xray server")
        result.update(server=server["address"], server_port=server["port"])
        if protocol in {"shadowsocks", "trojan"}:
            result["password"] = server["password"]
        if protocol == "shadowsocks":
            result["method"] = server["method"]
        elif server.get("users"):
            user = server["users"][0]
            result.update(username=user.get("user", ""), password=user.get("pass", ""))
        transport = stream_to_native(stream, "01234567-89ab-4cde-8fab-0123456789ab", notes)
        if protocol != "trojan" and transport:
            raise Error("wrapped Shadowsocks/SOCKS/HTTP transport needs its original client configuration")
        result.update(transport)
    elif protocol == "hysteria":
        if settings.get("version") != 2:
            raise Error("Xray Hysteria must specify version 2")
        only(settings, {"address", "port", "version"}, "Xray Hysteria settings")
        only(stream, {"network", "security", "hysteriaSettings", "tlsSettings", "finalmask"}, "Xray Hysteria stream")
        hy = stream.get("hysteriaSettings", {})
        only(hy, {"version", "auth"}, "Xray Hysteria transport")
        if stream.get("finalmask"):
            mask = stream["finalmask"]
            only(mask, {"quicParams"}, "FinalMask")
            only(mask.get("quicParams", {}), {"debug", "congestion"}, "QUIC tuning")
            notes.append("Xray QUIC congestion tuning uses the destination client's defaults")
        tls = stream.get("tlsSettings", {})
        only(tls, {"serverName", "allowInsecure", "alpn", "fingerprint", "enableSessionResumption"}, "Hysteria TLS")
        result.update(type="hysteria2", server=settings["address"], server_port=settings["port"], password=hy.get("auth", ""),
                      tls={"enabled": True, "server_name": tls.get("serverName", settings["address"])})
        if tls.get("allowInsecure"):
            result["tls"]["insecure"] = True
        if tls.get("alpn"):
            result["tls"]["alpn"] = tls["alpn"]
    else:
        raise Error("protocol has no cross-client adapter; retained in the Xray export")
    return valid_native(result)


def mihomo_to_native(raw, notes):
    data = copy.deepcopy(raw)
    kind = {"ss": "shadowsocks", "socks5": "socks"}.get(data["type"], data["type"])
    if kind not in COMMON:
        raise Error("protocol retained in Mihomo; no cross-client adapter")
    fields = {"name", "type", "server", "port", "udp", "uuid", "flow", "cipher", "alterId", "password", "username",
              "tls", "servername", "sni", "skip-cert-verify", "client-fingerprint", "alpn", "reality-opts",
              "network", "ws-opts", "grpc-opts", "http-opts", "h2-opts", "packet-encoding", "xudp", "global-padding", "authenticated-length",
              "plugin", "plugin-opts", "auth", "auth-str", "up", "down", "obfs", "obfs-password", "protocol",
              "recv-window-conn", "recv-window", "disable-mtu-discovery", "ports", "hop-interval",
              "congestion-controller", "udp-relay-mode", "reduce-rtt", "heartbeat-interval"}
    only(data, fields, "Mihomo proxy")
    result = {"type": kind, "server": data["server"], "server_port": data["port"]}
    for key in ("uuid", "flow", "password", "username"):
        if key in data:
            result[key] = data[key]
    if kind == "vmess":
        result.update(security=data.get("cipher", "auto"), alter_id=data.get("alterId", 0))
        for old, new in [("global-padding", "global_padding"), ("authenticated-length", "authenticated_length")]:
            if old in data:
                result[new] = data[old]
    if kind == "shadowsocks":
        result["method"] = data["cipher"]
        if data.get("plugin"):
            result["plugin"] = data["plugin"]
            opts = data.get("plugin-opts", {})
            result["plugin_opts"] = ";".join(k if v is True else f"{k}={str(v).lower() if isinstance(v, bool) else v}" for k, v in opts.items() if v is not False)
    if data.get("packet-encoding") or data.get("xudp"):
        result["packet_encoding"] = data.get("packet-encoding", "xudp")
    if data.get("tls") or kind in {"trojan", "hysteria", "hysteria2", "tuic"} or data.get("reality-opts"):
        tls = {"enabled": True}
        for old, new in [("servername", "server_name"), ("sni", "server_name"), ("skip-cert-verify", "insecure"), ("alpn", "alpn")]:
            if old in data:
                tls[new] = data[old]
        if data.get("client-fingerprint"):
            tls["utls"] = {"enabled": True, "fingerprint": data["client-fingerprint"]}
        if data.get("reality-opts"):
            reality = data["reality-opts"]
            only(reality, {"public-key", "short-id"}, "REALITY")
            tls["reality"] = {"enabled": True, "public_key": reality["public-key"], "short_id": str(reality.get("short-id", ""))}
        result["tls"] = tls
    network = data.get("network", "tcp")
    if network == "ws":
        opts = data.get("ws-opts", {})
        only(opts, {"path", "headers", "max-early-data", "early-data-header-name", "v2ray-http-upgrade"}, "WebSocket")
        transport = {"type": "httpupgrade" if opts.get("v2ray-http-upgrade") else "ws", "path": opts.get("path", "/")}
        for old, new in [("headers", "headers"), ("max-early-data", "max_early_data"), ("early-data-header-name", "early_data_header_name")]:
            if old in opts:
                transport[new] = opts[old]
        result["transport"] = transport
    elif network == "grpc":
        opts = data.get("grpc-opts", {})
        only(opts, {"grpc-service-name"}, "gRPC")
        result["transport"] = {"type": "grpc", "service_name": opts.get("grpc-service-name", "")}
    elif network in {"http", "h2"}:
        opts = data.get(network + "-opts", {})
        only(opts, {"host", "path", "method", "headers"}, "HTTP transport")
        path = opts.get("path", "/")
        if isinstance(path, list):
            if len(path) != 1:
                raise Error("multiple HTTP transport paths cannot be mapped losslessly")
            path = path[0]
        result["transport"] = {"type": "http", **opts, "path": path}
    elif network not in {"tcp", ""}:
        raise Error("transport has no cross-client adapter")
    if kind == "hysteria":
        if data.get("protocol", "udp") != "udp":
            raise Error("Hysteria v1 non-UDP carrier is not supported by Hiddify")
        for old, new in [("auth", "auth"), ("auth-str", "auth_str"), ("up", "up"), ("down", "down"), ("obfs", "obfs"),
                         ("recv-window-conn", "recv_window_conn"), ("recv-window", "recv_window"), ("disable-mtu-discovery", "disable_mtu_discovery")]:
            if old in data:
                result[new] = str(mbps(data[old])) + " Mbps" if old in {"up", "down"} else data[old]
    if kind == "hysteria2":
        for key in ("up", "down"):
            if key in data:
                result[key + "_mbps"] = mbps(data[key])
        if data.get("obfs"):
            result["obfs"] = {"type": data["obfs"], "password": data.get("obfs-password", "")}
    if kind in {"hysteria", "hysteria2"}:
        if data.get("ports"):
            result["server_ports"] = [item.replace("-", ":") if '-' in item else item + ':' + item
                                      for item in hysteria.ports(str(data["ports"]))]
            result.pop("server_port", None)
        if data.get("hop-interval"):
            result["hop_interval"] = str(data["hop-interval"]) + "s"
    if kind == "tuic":
        for old, new in [("congestion-controller", "congestion_control"), ("udp-relay-mode", "udp_relay_mode"), ("reduce-rtt", "zero_rtt_handshake")]:
            if old in data:
                result[new] = data[old]
        if data.get("heartbeat-interval"):
            result["heartbeat"] = str(data["heartbeat-interval"]) + "ms"
    return valid_native(result)


def mbps(value):
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(?:[mM](?:bps|b|bit)?)?\s*", str(value))
    if not match:
        raise Error("bandwidth must be expressed in Mbps")
    number = float(match[1])
    if not number.is_integer():
        raise Error("this client requires an integer Mbps bandwidth")
    return int(number)


def native_to_mihomo(raw):
    data = copy.deepcopy(raw)
    kind = data["type"]
    if kind not in COMMON:
        raise Error("protocol retained in Hiddify; no Mihomo adapter")
    only(data, {"tag", "type", "server", "server_port", "uuid", "flow", "security", "alter_id", "password", "username",
                "method", "plugin", "plugin_opts", "packet_encoding", "global_padding", "authenticated_length", "tls", "transport",
                "auth", "auth_str", "up", "down", "up_mbps", "down_mbps", "obfs", "recv_window_conn", "recv_window",
                "disable_mtu_discovery", "server_ports", "hop_interval", "congestion_control", "udp_relay_mode",
                "zero_rtt_handshake", "heartbeat", "version"}, "Hiddify outbound")
    host, port = endpoint(data)
    result = {"type": {"shadowsocks": "ss", "socks": "socks5"}.get(kind, kind), "server": host, "port": port}
    for key in ("uuid", "flow", "password", "username"):
        if key in data:
            result[key] = data[key]
    if kind == "socks" and str(data.get("version", "5")) != "5":
        raise Error("Mihomo requires SOCKS v5")
    if kind == "vmess":
        result.update(cipher=data.get("security", "auto"), alterId=data.get("alter_id", 0))
        for old, new in [("global_padding", "global-padding"), ("authenticated_length", "authenticated-length")]:
            if old in data:
                result[new] = data[old]
    if kind == "shadowsocks":
        result["cipher"] = data["method"]
        if data.get("plugin"):
            result["plugin"] = data["plugin"]
            opts = {}
            for part in data.get("plugin_opts", "").split(";"):
                if part:
                    key, sep, value = part.partition("=")
                    opts[key] = value if sep else True
            result["plugin-opts"] = opts
    if data.get("packet_encoding"):
        result["packet-encoding"] = data["packet_encoding"]
    tls = data.get("tls", {})
    if tls.get("enabled"):
        only(tls, {"enabled", "server_name", "insecure", "alpn", "utls", "reality"}, "TLS")
        if kind in {"vless", "vmess", "http", "socks"}:
            result["tls"] = True
        if tls.get("server_name"):
            result["servername" if kind in {"vless", "vmess"} else "sni"] = tls["server_name"]
        for old, new in [("insecure", "skip-cert-verify"), ("alpn", "alpn")]:
            if old in tls:
                result[new] = tls[old]
        utls = tls.get("utls", {})
        only(utls, {"enabled", "fingerprint"}, "uTLS")
        if utls.get("enabled"):
            result["client-fingerprint"] = utls.get("fingerprint", "chrome")
        reality = tls.get("reality", {})
        only(reality, {"enabled", "public_key", "short_id"}, "REALITY")
        if reality.get("enabled"):
            result["reality-opts"] = {"public-key": reality["public_key"], "short-id": reality.get("short_id", "")}
    transport = data.get("transport", {})
    if transport:
        network = transport["type"]
        if network in {"ws", "httpupgrade"}:
            only(transport, {"type", "path", "host", "headers", "max_early_data", "early_data_header_name"}, "WebSocket/HTTPUpgrade")
            opts = {"path": transport.get("path", "/")}
            for old, new in [("headers", "headers"), ("max_early_data", "max-early-data"), ("early_data_header_name", "early-data-header-name")]:
                if old in transport:
                    opts[new] = transport[old]
            if transport.get("host"):
                opts.setdefault("headers", {})["Host"] = transport["host"]
            if network == "httpupgrade":
                opts["v2ray-http-upgrade"] = True
            result.update(network="ws", **{"ws-opts": opts})
        elif network == "grpc":
            only(transport, {"type", "service_name"}, "gRPC")
            result.update(network="grpc", **{"grpc-opts": {"grpc-service-name": transport.get("service_name", "")}})
        elif network == "http":
            only(transport, {"type", "host", "path", "method", "headers"}, "HTTP transport")
            result.update(network="h2", **{"h2-opts": {k: v for k, v in transport.items() if k != "type"}})
        else:
            raise Error("transport has no verified Mihomo mapping")
    if kind == "hysteria":
        for old, new in [("auth", "auth"), ("auth_str", "auth-str"), ("up", "up"), ("down", "down"), ("obfs", "obfs"),
                         ("recv_window_conn", "recv-window-conn"), ("recv_window", "recv-window"), ("disable_mtu_discovery", "disable-mtu-discovery")]:
            if old in data:
                result[new] = data[old]
        for old, new in [("up_mbps", "up"), ("down_mbps", "down")]:
            if old in data:
                result[new] = str(data[old]) + " Mbps"
    if kind == "hysteria2":
        for old, new in [("up_mbps", "up"), ("down_mbps", "down")]:
            if old in data:
                result[new] = data[old]
        if data.get("obfs"):
            only(data["obfs"], {"type", "password"}, "Hysteria2 obfuscation")
            result.update(obfs=data["obfs"]["type"], **{"obfs-password": data["obfs"].get("password", "")})
    if kind in {"hysteria", "hysteria2"}:
        if data.get("server_ports"):
            result["ports"] = ",".join(hysteria.ports(",".join(data["server_ports"]).replace(":", "-")))
        if data.get("hop_interval"):
            result["hop-interval"] = duration_ms(data["hop_interval"]) / 1000
    if kind == "tuic":
        for old, new in [("congestion_control", "congestion-controller"), ("udp_relay_mode", "udp-relay-mode"), ("zero_rtt_handshake", "reduce-rtt")]:
            if old in data:
                result[new] = data[old]
        if data.get("heartbeat"):
            result["heartbeat-interval"] = duration_ms(data["heartbeat"])
    return result


def duration_ms(value):
    match = re.fullmatch(r"(\d+(?:\.\d+)?)(ms|s|m)", str(value))
    if not match:
        raise Error("unsupported duration format")
    result = float(match[1]) * {"ms": 1, "s": 1000, "m": 60000}[match[2]]
    if not result.is_integer():
        raise Error("this client requires an integer millisecond duration")
    return int(result)


def native_to_xray(data):
    # The strict Mihomo adapter also checks for unmapped chains/client settings.
    native_to_mihomo(data)
    kind = data["type"]
    if kind not in {"vless", "vmess", "shadowsocks", "trojan", "socks", "http", "hysteria2"}:
        raise Error("protocol is not supported by this Xray adapter")
    if any(data.get(k) for k in ("plugin", "plugin_opts", "obfs", "server_ports", "up_mbps", "down_mbps")):
        raise Error("client-specific protocol settings have no Xray mapping")
    result = {"protocol": kind}
    server = {"address": data["server"], "port": data["server_port"]}
    if kind in {"vless", "vmess"}:
        user = {"id": data["uuid"]}
        if kind == "vless":
            user["encryption"] = "none"
            if data.get("flow"):
                user["flow"] = data["flow"]
        else:
            user.update(security=data.get("security", "auto"), alterId=data.get("alter_id", 0))
        server["users"] = [user]
        result["settings"] = {"vnext": [server]}
    elif kind == "hysteria2":
        result.update(protocol="hysteria", settings={**server, "version": 2})
    else:
        if kind in {"trojan", "shadowsocks"}:
            server["password"] = data["password"]
        if kind == "shadowsocks":
            server["method"] = data["method"]
        if data.get("username"):
            server["users"] = [{"user": data["username"], "pass": data.get("password", "")}]
        result["settings"] = {"servers": [server]}
    stream = {"network": "tcp", "security": "none"}
    tls = data.get("tls", {})
    if tls.get("enabled"):
        reality = tls.get("reality", {})
        security = "reality" if reality.get("enabled") else "tls"
        stream["security"] = security
        ts = {}
        for old, new in [("server_name", "serverName"), ("alpn", "alpn"), ("insecure", "allowInsecure")]:
            if old in tls:
                ts[new] = tls[old]
        if tls.get("utls", {}).get("enabled"):
            ts["fingerprint"] = tls["utls"].get("fingerprint", "chrome")
        if security == "reality":
            ts.update(publicKey=reality["public_key"], shortId=reality.get("short_id", ""))
        stream[security + "Settings"] = ts
    t = data.get("transport", {})
    if t:
        network = t["type"]
        stream["network"] = network
        if network in {"ws", "httpupgrade"}:
            if t.get("max_early_data"):
                raise Error("WebSocket early-data needs its original client configuration")
            stream[network + "Settings"] = {k: v for k, v in t.items() if k in {"path", "headers", "host"}}
        elif network == "grpc":
            stream["grpcSettings"] = {"serviceName": t.get("service_name", "")}
        else:
            raise Error("transport has no verified Xray mapping")
    if kind == "hysteria2":
        stream.update(network="hysteria", hysteriaSettings={"version": 2, "auth": data["password"]})
    result["streamSettings"] = stream
    return result


def uri_to_native(uri, notes):
    scheme = uri.partition("://")[0].lower()
    if scheme in hysteria.SCHEMES:
        proxy, label = hysteria.uri_to_mihomo(uri)
        return mihomo_to_native(proxy, notes), label
    if scheme == "vmess":
        v = json.loads(b64(uri.split("://", 1)[1].split("#", 1)[0]))
        only(v, {"v", "ps", "add", "port", "id", "aid", "scy", "net", "type", "host", "path", "tls", "sni", "alpn", "fp"}, "VMess link")
        if v.get("type") not in (None, "", "none", "gun"):
            raise Error("VMess header or gRPC mode needs full configuration")
        proxy = {"type": "vmess", "server": v["add"], "port": int(v["port"]), "uuid": v["id"], "alterId": int(v.get("aid", 0)), "cipher": v.get("scy", "auto")}
        network = v.get("net", "tcp")
        proxy["network"] = network
        if network == "ws":
            proxy["ws-opts"] = {"path": v.get("path", "/"), "headers": {"Host": v["host"]} if v.get("host") else {}}
        elif network == "grpc":
            proxy["grpc-opts"] = {"grpc-service-name": v.get("path", "")}
        if v.get("tls") == "tls":
            proxy.update(tls=True, servername=v.get("sni") or v.get("host") or v["add"])
        if v.get("alpn"):
            proxy["alpn"] = v["alpn"].split(",")
        if v.get("fp"):
            proxy["client-fingerprint"] = v["fp"]
        return mihomo_to_native(proxy, notes), v.get("ps", "VMess")
    if scheme == "ss" and "@" not in uri.split("#", 1)[0]:
        body, _, fragment = uri[5:].partition("#")
        uri = "ss://" + b64(body) + ("#" + fragment if fragment else "")
    parsed = urlsplit(uri)
    q = dict(parse_qsl(parsed.query, keep_blank_values=True))
    kind = {"ss": "shadowsocks", "hy2": "hysteria2", "socks5": "socks", "https": "http"}.get(scheme, scheme)
    if kind not in COMMON:
        raise Error("share-link protocol has no adapter; use its native client JSON/YAML")
    node = {"type": kind, "server": parsed.hostname, "server_port": parsed.port or (443 if kind in {"hysteria2", "trojan", "http"} else None)}
    label = unquote(parsed.fragment) or parsed.hostname or kind
    if kind == "shadowsocks":
        only(q, {"plugin"}, "Shadowsocks link")
        credentials = unquote(parsed.netloc.rsplit("@", 1)[0])
        if ":" not in credentials:
            credentials = b64(credentials)
        method, sep, password = credentials.partition(":")
        if not sep:
            raise Error("invalid Shadowsocks credentials")
        node.update(method=method, password=password)
        if q.get("plugin"):
            plugin, _, options = q["plugin"].partition(";")
            node.update(plugin=plugin, plugin_opts=options)
    elif kind in {"vless", "trojan"}:
        allowed = {"encryption", "flow", "type", "security", "sni", "peer", "fp", "alpn", "pbk", "sid", "spx", "path", "host", "serviceName", "mode", "authority", "allowInsecure", "insecure", "headerType", "extra"}
        only(q, allowed, "proxy link")
        if q.get("encryption", "none") != "none" or q.get("headerType", "none") != "none":
            raise Error("encryption or header settings need full configuration")
        user = unquote(parsed.username or "")
        node["uuid" if kind == "vless" else "password"] = user + ((":" + unquote(parsed.password)) if kind == "trojan" and parsed.password is not None else "")
        if q.get("flow"):
            node["flow"] = q["flow"]
        network = q.get("type", "tcp")
        if network in {"ws", "httpupgrade"}:
            node["transport"] = {"type": network, "path": q.get("path", "/")}
            if q.get("host"):
                node["transport"]["headers"] = {"Host": q["host"]}
        elif network == "grpc":
            if q.get("mode") not in (None, "gun") or q.get("authority"):
                raise Error("gRPC multimode/authority cannot be mapped losslessly")
            node["transport"] = {"type": "grpc", "service_name": q.get("serviceName", "")}
        elif network == "xhttp":
            if q.get("extra"):
                raise Error("XHTTP extra settings need original configuration")
            node["transport"] = {"type": "xhttp", "path": q.get("path", "/"), "mode": q.get("mode", "auto")}
            if q.get("host"):
                node["transport"]["host"] = q["host"]
        elif network not in {"tcp", "raw"}:
            raise Error("unsupported share-link transport")
        security = q.get("security", "tls" if kind == "trojan" else "none")
        if security not in {"none", "tls", "reality"}:
            raise Error("unsupported share-link security")
        if security != "none":
            tls = {"enabled": True, "server_name": q.get("sni") or q.get("peer") or parsed.hostname}
            if q.get("fp") or security == "reality":
                tls["utls"] = {"enabled": True, "fingerprint": q.get("fp", "chrome")}
            if security == "reality":
                tls["reality"] = {"enabled": True, "public_key": q.get("pbk", ""), "short_id": q.get("sid", "")}
            node["tls"] = tls
    elif kind == "tuic":
        only(q, {"peer", "sni", "insecure", "allowInsecure", "alpn", "congestion_control", "udp_relay_mode", "zero_rtt_handshake"}, "TUIC link")
        node["tls"] = {"enabled": True, "server_name": q.get("sni") or q.get("peer") or parsed.hostname}
        node.update(uuid=unquote(parsed.username or ""), password=unquote(parsed.password or ""))
        for key in ("congestion_control", "udp_relay_mode"):
            if q.get(key):
                node[key] = q[key]
        if "zero_rtt_handshake" in q:
            node["zero_rtt_handshake"] = enabled(q["zero_rtt_handshake"])
    elif kind in {"socks", "http"}:
        only(q, set(), "proxy URL")
        if parsed.username:
            node.update(username=unquote(parsed.username), password=unquote(parsed.password or ""))
        if scheme == "https":
            node["tls"] = {"enabled": True}
    if node.get("tls"):
        if q.get("alpn"):
            node["tls"]["alpn"] = q["alpn"].split(",")
        if enabled(q.get("insecure", q.get("allowInsecure", False))):
            node["tls"]["insecure"] = True
    return valid_native(node), label


@dataclass
class Entry:
    name: str
    source: str
    protocol: str
    kind: str
    original: dict | str
    group_size: int = 1
    context: list[dict] = field(default_factory=list)
    outputs: dict = field(default_factory=dict)
    issues: dict = field(default_factory=dict)
    notes: list = field(default_factory=list)
    identity: str = ""


def adapt(kind, raw, source, label, context=None, group_size=1):
    protocol = raw.get("protocol" if kind == "xray" else "type", "unknown") if isinstance(raw, dict) else raw.partition(":")[0]
    if kind == "xray" and protocol == "hysteria" and raw.get("settings", {}).get("version") == 2:
        protocol = "hysteria2"
    entry = Entry(label[:200], source, protocol, kind, copy.deepcopy(raw), group_size, context or [])
    native = None
    target = {"singbox": "hiddify", "mihomo": "mihomo", "xray": "xray"}.get(kind)
    try:
        if kind == "singbox":
            valid_native(copy.deepcopy(raw))
        elif kind == "mihomo":
            common.host_port(raw.get("server"), raw.get("port"))
            if raw.get("type") in {"vless", "vmess"}:
                common.valid_uuid(raw.get("uuid"))
        elif kind == "xray":
            settings = raw.get("settings", {})
            server = (settings.get("vnext") or settings.get("servers") or [settings])[0]
            common.host_port(server.get("address"), server.get("port"))
            if raw.get("protocol") in {"vless", "vmess"}:
                common.valid_uuid(server["users"][0].get("id"))
        if target:
            entry.outputs[target] = copy.deepcopy(raw)
    except (Error, KeyError, TypeError, IndexError):
        entry.issues = {client: "invalid server address, port or credentials" for client in TARGETS}
        entry.identity = hashlib.sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest()
        return entry
    try:
        if kind == "singbox":
            native = valid_native(copy.deepcopy(raw))
        elif kind == "mihomo":
            native = mihomo_to_native(raw, entry.notes)
        elif kind == "xray":
            native = xray_to_native(raw, entry.notes)
        else:
            if raw.partition('://')[0].lower() in hysteria.SCHEMES:
                proxy, entry.name = hysteria.uri_to_mihomo(raw)
                entry.protocol = proxy['type']
                entry.outputs['mihomo'] = proxy
                native = mihomo_to_native(proxy, entry.notes)
            else:
                native, entry.name = uri_to_native(raw, entry.notes)
        entry.protocol = native["type"]
        entry.outputs["hiddify"] = native
    except (Error, ValueError, KeyError, TypeError, IndexError) as exc:
        reason = str(exc) if isinstance(exc, Error) else "malformed or incomplete proxy settings"
        for client in TARGETS:
            if client not in entry.outputs:
                entry.issues[client] = reason
    if native:
        for client, converter in [("mihomo", native_to_mihomo), ("xray", native_to_xray)]:
            if client not in entry.outputs:
                try:
                    entry.outputs[client] = converter(native)
                except (Error, ValueError, KeyError, TypeError) as exc:
                    entry.issues[client] = str(exc) if isinstance(exc, Error) else "malformed or incomplete proxy settings"
    identity = copy.deepcopy(native if native else raw)
    if isinstance(identity, dict):
        identity.pop("tag", None)
        identity.pop("name", None)
    if target and isinstance(raw, dict) and dependencies(raw, target):
        lookup = {o.get("name" if target == "mihomo" else "tag"): o for o in entry.context}
        related, visited = [], set()
        def collect(node, depth=0):
            if depth > 20:
                return
            for reference in dependencies(node, target):
                if reference in lookup and reference not in visited:
                    visited.add(reference)
                    related.append(lookup[reference])
                    collect(lookup[reference], depth + 1)
        collect(raw)
        identity = {"outbound": identity, "dependencies": related}
    entry.identity = hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return entry


def parse(data: bytes, source: str, depth=0):
    if len(data) > common.MAX_BYTES or depth > 2:
        raise Error("subscription too large or excessively nested")
    try:
        text = data.decode("utf-8-sig").strip()
    except UnicodeError:
        raise Error("subscription is not UTF-8 text") from None
    if not text or text.lower().startswith(("<html", "<!doctype")):
        raise Error("provider returned an empty response or a web page")
    entries = []
    if text.startswith(("{", "[")) or re.search(r"(?m)^\s*(proxies|outbounds):", text):
        try:
            document = json.loads(text) if text.startswith(("{", "[")) else yaml.safe_load(text)
        except (ValueError, yaml.YAMLError):
            raise Error("invalid subscription JSON/YAML") from None
        # Bound traversal; YAML alias cycles cannot create unbounded profile scans.
        profiles = document if isinstance(document, list) else [document]
        if len(profiles) > 10000:
            raise Error("too many subscription profiles")
        for profile in profiles:
            if not isinstance(profile, dict):
                raise Error("expected proxy configuration objects")
            is_mihomo = "proxies" in profile or ("type" in profile and "port" in profile)
            outbounds = profile.get("proxies" if is_mihomo else "outbounds", [profile])
            if not isinstance(outbounds, list):
                raise Error("proxy collection must be an array")
            for raw in outbounds:
                if not isinstance(raw, dict):
                    raise Error("proxy collection contains a non-object")
                kind = "mihomo" if is_mihomo else "xray" if "protocol" in raw else "singbox"
                protocol = raw.get("protocol" if kind == "xray" else "type")
                if protocol in SKIP:
                    continue
                if not protocol:
                    continue
                label = str(profile.get("remarks") or raw.get("name") or raw.get("tag") or protocol)
                parts = [raw]
                if kind == "xray" and protocol in {"vless", "vmess"}:
                    parts = []
                    for server in raw.get("settings", {}).get("vnext", []):
                        for user in server.get("users", []):
                            single = copy.deepcopy(raw)
                            single["settings"]["vnext"] = [{**server, "users": [user]}]
                            parts.append(single)
                elif kind == "xray" and isinstance(raw.get("settings", {}).get("servers"), list):
                    parts = []
                    for server in raw["settings"]["servers"]:
                        single = copy.deepcopy(raw)
                        single["settings"]["servers"] = [server]
                        parts.append(single)
                for part in parts:
                    entries.append(adapt(kind, part, source, label, outbounds, len(outbounds)))
    else:
        lines = [line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]
        if lines and all("://" in line for line in lines):
            for line in lines:
                label = unquote(line.partition("#")[2]) or line.partition(":")[0]
                entries.append(adapt("uri", line, source, label))
        else:
            return parse(b64("".join(text.split())).encode(), source, depth + 1)
    if not entries or not any(e.outputs for e in entries):
        raise Error("subscription contains no usable proxy configurations")
    return entries


def dependencies(outbound, target):
    if target == "hiddify":
        return [outbound["detour"]] if outbound.get("detour") else []
    if target == "mihomo":
        return [outbound["dialer-proxy"]] if outbound.get("dialer-proxy") else []
    found = []
    for value in (outbound.get("proxySettings", {}).get("tag"), outbound.get("streamSettings", {}).get("sockopt", {}).get("dialerProxy")):
        if value:
            found.append(value)
    return found


def bundle(entry, target, name):
    root = copy.deepcopy(entry.outputs[target])
    key = "name" if target == "mihomo" else "tag"
    lookup = {o.get(key): o for o in entry.context}
    result, active, done = [], set(), {}
    def visit(node, label, depth=0):
        if depth > 20 or label in active:
            raise Error("proxy dependency cycle or excessive chain depth")
        active.add(label)
        for reference in dependencies(node, target):
            if reference not in lookup:
                raise Error("proxy depends on an unavailable outbound")
            if reference in active:
                raise Error("proxy dependency cycle")
            replacement = name + " / " + str(reference)
            if reference not in done:
                dep = copy.deepcopy(lookup[reference])
                visit(dep, reference, depth + 1)
                dep[key] = replacement
                done[reference] = replacement
                result.append(dep)
            if target == "hiddify":
                node["detour"] = replacement
            elif target == "mihomo":
                node["dialer-proxy"] = replacement
            else:
                if node.get("proxySettings", {}).get("tag") == reference:
                    node["proxySettings"]["tag"] = replacement
                sockopt = node.get("streamSettings", {}).get("sockopt", {})
                if sockopt.get("dialerProxy") == reference:
                    sockopt["dialerProxy"] = replacement
        active.remove(label)
    visit(root, root.get(key, name))
    root[key] = name
    return [root, *result]


def combine(entries):
    unique, sources = {}, {}
    for entry in entries:
        sources.setdefault(entry.identity, set()).add(entry.source)
        if entry.identity not in unique or entry.group_size < unique[entry.identity].group_size:
            unique[entry.identity] = entry
    outputs = {target: [] for target in TARGETS}
    selectable = {target: [] for target in TARGETS}
    inventory, diagnostics = [], []
    for identity, entry in unique.items():
        label = f"{entry.name} · {entry.source} [{identity[:6]}]"
        available = []
        for target in TARGETS:
            if target not in entry.outputs:
                diagnostics.append({"source": entry.source, "name": entry.name, "client": target, "reason": entry.issues.get(target, "unsupported configuration")})
                continue
            try:
                group = bundle(entry, target, label)
            except Error as exc:
                diagnostics.append({"source": entry.source, "name": entry.name, "client": target, "reason": str(exc)})
                continue
            outputs[target].extend(group)
            selectable[target].append(label)
            available.append(target)
        inventory.append({"id": identity[:16], "name": entry.name, "protocol": entry.protocol, "sources": sorted(sources[identity]),
                          "clients": available, "notes": entry.notes})
    mihomo = {"mixed-port": 7890, "allow-lan": False, "mode": "rule", "log-level": "info",
              "proxies": outputs["mihomo"], "proxy-groups": [{"name": "Subscription Parser", "type": "select", "proxies": selectable["mihomo"] or ["DIRECT"]}],
              "rules": ["MATCH,Subscription Parser"]}
    hiddify = {"outbounds": outputs["hiddify"]}
    xray = {"log": {"loglevel": "warning"}, "outbounds": outputs["xray"]}
    return {"hiddify": hiddify, "mihomo": mihomo, "xray": xray, "inventory": inventory, "diagnostics": diagnostics,
            "counts": {target: len(selectable[target]) for target in TARGETS}, "total": len(unique), "duplicates_removed": len(entries) - len(unique)}

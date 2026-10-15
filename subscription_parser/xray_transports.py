"""Translate Xray VLESS transports into links and native Hiddify settings."""
from __future__ import annotations

import base64
import binascii
import copy
import json
import re
import urllib.parse

from .common import NeedsFullConfig, SubscriptionError, host_port, reject_extra, valid_uuid


def vless_link(outbound: dict, label: str) -> str:
    """Convert one Xray VLESS server/user, rejecting lossy conversions."""
    try:
        server = outbound["settings"]["vnext"][0]
        user = server["users"][0]
        stream = outbound.get("streamSettings", {})
        address, port = host_port(server["address"], server["port"])
        identifier = valid_uuid(user["id"])
    except (KeyError, IndexError, TypeError):
        raise SubscriptionError("incomplete VLESS outbound") from None
    reject_extra(outbound, {"tag", "protocol", "settings", "streamSettings", "mux"}, "outbound")
    reject_extra(outbound["settings"], {"vnext"}, "VLESS settings")
    reject_extra(server, {"address", "port", "users"}, "VLESS server")
    reject_extra(stream, {"network", "security", "tcpSettings", "rawSettings", "wsSettings",
                          "grpcSettings", "httpupgradeSettings", "xhttpSettings", "splithttpSettings",
                          "tlsSettings", "realitySettings", "sockopt", "finalmask"}, "transport")
    if outbound.get("mux", {}).get("enabled"):
        raise NeedsFullConfig("enabled Xray mux/XUDP settings require full configuration")
    if stream.get("sockopt") or stream.get("finalmask"):
        raise NeedsFullConfig("socket/proxy-chain or FinalMask settings require full configuration")
    reject_extra(user, {"id", "encryption", "flow", "email", "level"}, "VLESS user")
    network = stream.get("network", "tcp")
    network = {"raw": "tcp", "websocket": "ws", "splithttp": "xhttp"}.get(network, network)
    if network not in {"tcp", "ws", "grpc", "xhttp", "httpupgrade"}:
        raise NeedsFullConfig(f"transport {network!r} requires full configuration")
    security = stream.get("security", "none")
    if security not in {"none", "tls", "reality"}:
        raise NeedsFullConfig("unrecognized transport security")
    params = {"encryption": user.get("encryption") or "none"}
    if user.get("flow"):
        params["flow"] = user["flow"]
    params.update({"type": network, "security": security})
    if network == "tcp":
        settings = stream.get("rawSettings", stream.get("tcpSettings", {}))
        reject_extra(settings, {"header"}, "TCP")
        header = settings.get("header", {})
        if header and header != {"type": "none"}:
            raise NeedsFullConfig("TCP header settings require full configuration")
    elif network in {"ws", "httpupgrade"}:
        settings = stream.get(network + "Settings", {})
        reject_extra(settings, {"host", "path", "headers"}, network)
        headers = settings.get("headers", {})
        if not isinstance(headers, dict):
            raise SubscriptionError("invalid transport headers")
        if any(key.lower() != "host" for key in headers):
            raise NeedsFullConfig("custom WebSocket/HTTPUpgrade headers cannot be carried by standard VLESS links")
        header_host = next((value for key, value in headers.items() if key.lower() == "host"), "")
        if settings.get("host") and header_host and settings["host"] != header_host:
            raise NeedsFullConfig("conflicting transport Host fields")
        params["path"] = settings.get("path") or "/"
        if settings.get("host") or header_host:
            params["host"] = settings.get("host") or header_host
    elif network == "grpc":
        settings = stream.get("grpcSettings", {})
        reject_extra(settings, {"serviceName", "authority", "mode", "multiMode"}, "gRPC")
        params["serviceName"] = settings.get("serviceName", "")
        # Xray JSON uses multiMode, not the share-link query field named mode.
        # Dendiplay emits a boolean mode field, which Xray itself ignores.
        if "multiMode" in settings and not isinstance(settings["multiMode"], bool):
            raise SubscriptionError("gRPC multiMode must be a boolean")
        params["mode"] = "multi" if settings.get("multiMode") else "gun"
        if settings.get("authority"):
            params["authority"] = settings["authority"]
    elif network == "xhttp":
        settings = stream.get("xhttpSettings", stream.get("splithttpSettings", {}))
        params.update({"path": settings.get("path") or "/", "mode": settings.get("mode") or "auto"})
        if settings.get("host"):
            params["host"] = settings["host"]
        extra = copy.deepcopy(settings.get("extra", {}))
        if not isinstance(extra, dict):
            raise SubscriptionError("XHTTP extra must be an object")
        extra.update({k: v for k, v in settings.items() if k not in {"host", "path", "mode", "extra"}})
        if extra:
            params["extra"] = json.dumps(extra, ensure_ascii=False, separators=(",", ":"))
    if security in {"tls", "reality"}:
        tls = stream.get(security + "Settings", {})
        allowed = {"serverName", "fingerprint", "alpn", "show", "enableSessionResumption"}
        allowed |= ({"publicKey", "password", "shortId", "spiderX"} if security == "reality" else set())
        reject_extra(tls, allowed, security.upper())
        if tls.get("enableSessionResumption"):
            raise NeedsFullConfig("TLS session resumption settings require full configuration")
        if tls.get("serverName"):
            params["sni"] = tls["serverName"]
        if tls.get("fingerprint"):
            params["fp"] = tls["fingerprint"]
        if tls.get("alpn"):
            params["alpn"] = ",".join(tls["alpn"])
        if security == "reality":
            params.setdefault("fp", "chrome")
            params["pbk"] = tls.get("password") or tls.get("publicKey") or ""
            try:
                key = params["pbk"]
                decoded = base64.b64decode(key + "=" * (-len(key) % 4), altchars=b"-_", validate=True)
                if len(decoded) != 32:
                    raise ValueError
            except (ValueError, TypeError, binascii.Error):
                raise SubscriptionError("missing, redacted or invalid REALITY public key") from None
            if "shortId" in tls:
                sid = tls["shortId"]
                if not isinstance(sid, str) or not re.fullmatch(r"(?:[0-9a-fA-F]{2}){0,8}", sid):
                    raise SubscriptionError("invalid REALITY short ID")
                params["sid"] = sid
            if "spiderX" in tls:
                params["spx"] = tls["spiderX"]
    host = f"[{address}]" if ":" in address else address
    query = urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
    return f"vless://{identifier}@{host}:{port}?{query}#{urllib.parse.quote(label, safe='')}"


def hiddify_outbound(outbound: dict, label: str) -> tuple[dict, list[str]]:
    """Convert to Hiddify's native sing-box schema, preserving required headers."""
    normalized = copy.deepcopy(outbound)
    notes = []
    if normalized.pop("mux", {}).get("enabled"):
        notes.append("Xray mux/XUDP client tuning omitted; Hiddify uses its own client settings")
    stream = normalized.get("streamSettings", {})
    network = {"websocket": "ws"}.get(stream.get("network"), stream.get("network"))
    headers = {}
    if network in {"ws", "httpupgrade"}:
        settings = stream.get(network + "Settings", {})
        headers = copy.deepcopy(settings.get("headers", {}))
        settings["headers"] = {k: v for k, v in headers.items() if k.lower() == "host"}
        if settings.pop("heartbeatPeriod", 0):
            notes.append("Xray WebSocket heartbeatPeriod tuning omitted; Hiddify uses its own keepalive")
    uri = urllib.parse.urlsplit(vless_link(normalized, label))
    q = dict(urllib.parse.parse_qsl(uri.query, keep_blank_values=True))
    if q["encryption"] != "none":
        raise NeedsFullConfig("non-default VLESS encryption requires link import")
    result = {"type": "vless", "tag": label, "server": uri.hostname,
              "server_port": uri.port, "uuid": uri.username, "packet_encoding": "xudp"}
    if q.get("flow"):
        result["flow"] = q["flow"]
    if q["security"] != "none":
        tls = {"enabled": True}
        if q.get("sni"):
            tls["server_name"] = q["sni"]
        if q.get("fp"):
            tls["utls"] = {"enabled": True, "fingerprint": q["fp"]}
        if q.get("alpn"):
            tls["alpn"] = q["alpn"].split(",")
        if q["security"] == "reality":
            tls["reality"] = {"enabled": True, "public_key": q["pbk"], "short_id": q.get("sid", "")}
            if q.get("spx") not in (None, "", "/"):
                notes.append("REALITY spiderX probe path has no native Hiddify equivalent")
        result["tls"] = tls
    transport = q["type"]
    if transport in {"ws", "httpupgrade"}:
        t = {"type": transport, "path": q["path"]}
        if q.get("host"):
            headers = {k: v for k, v in headers.items() if k.lower() != "host"}
            headers["Host"] = q["host"]
        if headers:
            t["headers"] = headers
        if transport == "ws":
            path = urllib.parse.urlsplit(q["path"])
            pairs = urllib.parse.parse_qsl(path.query, keep_blank_values=True)
            early = next((v for k, v in pairs if k == "ed"), None)
            if early is not None:
                if not early.isdigit():
                    raise NeedsFullConfig("invalid WebSocket early-data length")
                t["max_early_data"] = int(early)
                t["early_data_header_name"] = "Sec-WebSocket-Protocol"
                t["path"] = urllib.parse.urlunsplit(path._replace(
                    query=urllib.parse.urlencode([(k, v) for k, v in pairs if k != "ed"])))
        result["transport"] = t
        if "tls" in result:
            result["tls"]["alpn"] = ["http/1.1"]
    elif transport == "grpc":
        if q.get("mode", "gun") != "gun" or q.get("authority"):
            raise NeedsFullConfig("non-default gRPC mode/authority requires link import")
        result["transport"] = {"type": "grpc", "service_name": q["serviceName"]}
        if "tls" in result:
            result["tls"]["alpn"] = ["h2"]
    elif transport == "xhttp":
        t = {"type": "xhttp", "mode": q["mode"], "path": q["path"]}
        if q.get("host"):
            t["host"] = q["host"]
        extra = json.loads(q.get("extra", "{}"))
        if any(k != "headers" for k in extra):
            raise NeedsFullConfig("advanced XHTTP extra settings require link import")
        if extra.get("headers"):
            t["headers"] = extra["headers"]
        result["transport"] = t
    return result, notes

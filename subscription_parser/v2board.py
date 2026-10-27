"""Bounded URI subscriptions for V2Board's External Subscriptions importer."""
from __future__ import annotations

import base64
import copy
import ipaddress
import json
import re
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit

from . import common, hysteria, protocols, server_policy
from .xray_transports import vless_link

MAX_NODES = 500
MAX_BYTES = 1048576
MAX_ENTRY_BYTES = 16384
SUPPORTED = {"vless", "vmess", "trojan", "shadowsocks", "ss", *hysteria.SCHEMES}
UNSUPPORTED = "V2Board External Subscriptions accepts VLESS, VMess, Trojan, Shadowsocks, Hysteria and Hysteria2"
DUMMY_UUID = "01234567-89ab-4cde-8fab-0123456789ab"
Error = common.SubscriptionError


def credential(value):
    if not isinstance(value, str) or not value or len(value.encode()) > 1024 or re.search(r"[\x00-\x1f\x7f]", value):
        raise Error("missing or invalid URI credentials")
    return value


def endpoint(host, port):
    host, port = common.host_port(host, port)
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if len(host) > 253 or not all(re.fullmatch(r"(?!-)[A-Za-z0-9-]{1,63}(?<!-)", part) for part in host.rstrip('.').split('.')):
            raise Error("server hostname is not accepted by V2Board")
    return f"[{host}]" if ':' in host else host, port


def normalize_uri(uri, label):
    """Preserve connection parameters while meeting the importer's strict syntax."""
    scheme = uri.partition('://')[0]
    if scheme.lower() in hysteria.SCHEMES:
        _, host, port, _, _, _ = hysteria.parse_uri(uri)
        endpoint(host, port)
        return hysteria.normalize_uri(uri, label)
    if scheme == 'vmess':
        data = json.loads(protocols.b64(uri[8:].split('#', 1)[0]))
        if not isinstance(data, dict) or any(isinstance(value, (dict, list)) for value in data.values()):
            raise Error("VMess share links require scalar JSON fields")
        host, port = endpoint(data['add'], data['port'])
        common.valid_uuid(credential(data['id']))
        data.update(add=host.strip('[]'), port=port, ps=label)
        return 'vmess://' + base64.b64encode(json.dumps(data, ensure_ascii=False, separators=(',', ':')).encode()).decode()
    if scheme == 'ss' and '@' not in uri.split('#', 1)[0]:
        uri = 'ss://' + protocols.b64(uri[5:].split('#', 1)[0])
    if scheme not in {'vless', 'trojan', 'ss'}:
        raise Error(UNSUPPORTED)
    parsed = urlsplit(uri)
    host, port = endpoint(parsed.hostname, parsed.port)
    if parsed.path not in ('', '/') or (parsed.path and scheme != 'ss'):
        raise Error("proxy URI has an unsupported path")
    user = unquote(parsed.username or '')
    if scheme == 'ss':
        auth = user + ':' + unquote(parsed.password) if parsed.password is not None else protocols.b64(user)
        method, separator, password = auth.partition(':')
        if not separator or not re.fullmatch(r'[a-z0-9-]{1,64}', method):
            raise Error("invalid Shadowsocks authentication")
        credential(password)
        user = base64.urlsafe_b64encode(auth.encode()).decode().rstrip('=')
    else:
        if parsed.password is not None:
            raise Error("URI passwords must be percent-encoded")
        credential(user)
        if scheme == 'vless':
            common.valid_uuid(user)
        user = quote(user, safe='')
    if re.search(r'%(?![a-fA-F0-9]{2})', parsed.query):
        raise Error("invalid URI query encoding")
    params = parse_qsl(parsed.query, keep_blank_values=True, errors='strict')
    seen = set()
    for key, value in params:
        if key in seen or not re.fullmatch(r'[a-zA-Z][a-zA-Z0-9_-]{0,63}', key) or re.search(r'[\x00-\x1f\x7f]', value):
            raise Error("invalid or repeated URI query parameter")
        seen.add(key)
    return urlunsplit((scheme, f'{user}@{host}:{port}', '', urlencode(params, quote_via=quote), quote(label, safe='')))


def xray_link(raw, label):
    node = copy.deepcopy(raw)
    kind = node['protocol']
    common.reject_extra(node, {'tag', 'protocol', 'settings', 'streamSettings', 'mux'}, 'outbound')
    settings = node['settings']
    common.reject_extra(settings, {'vnext' if kind in {'vless', 'vmess'} else 'servers'}, 'proxy settings')
    servers = settings.get('vnext') or settings.get('servers')
    if not servers or len(servers) != 1:
        raise Error('share links require a single endpoint')
    server = servers[0]
    if kind in {'vless', 'vmess'}:
        if len(server.get('users', [])) != 1:
            raise Error('share links require a single account')
        user = server['users'][0]
    if kind != 'vless':
        common.reject_extra(server, {'address', 'port', 'users'} if kind == 'vmess' else {'address', 'port', 'password', 'method', 'email', 'level'}, 'proxy server')
        if kind == 'vmess':
            common.reject_extra(user, {'id', 'security', 'alterId', 'email', 'level'}, 'VMess user')
        node['protocol'] = 'vless'
        node['settings'] = {'vnext': [{'address': server['address'], 'port': server['port'],
            'users': [{'id': user['id'] if kind == 'vmess' else DUMMY_UUID, 'encryption': 'none'}]}]}
    tls = node.get('streamSettings', {}).get('tlsSettings', {})
    insecure = tls.pop('allowInsecure', False)
    if not isinstance(insecure, bool):
        raise Error('TLS allowInsecure must be a boolean')
    uri = vless_link(node, label)
    parsed = urlsplit(uri)
    params = dict(parse_qsl(parsed.query, keep_blank_values=True))
    if insecure:
        params['allowInsecure'] = '1'
    if kind == 'vless':
        return urlunsplit(parsed._replace(query=urlencode(params, quote_via=quote)))
    if kind == 'vmess':
        if params['type'] not in {'tcp', 'ws', 'grpc'} or params['security'] not in {'none', 'tls'} or insecure:
            raise Error('VMess transport or TLS settings cannot be preserved in this share-link format')
        if params.get('mode', 'gun') != 'gun' or params.get('authority'):
            raise Error('VMess gRPC mode or authority requires native configuration')
        data = {'v': '2', 'ps': label, 'add': server['address'], 'port': server['port'], 'id': user['id'],
                'aid': user.get('alterId', 0), 'scy': user.get('security', 'auto'), 'net': params['type'],
                'type': 'none', 'host': params.get('host', ''), 'path': params.get('serviceName', params.get('path', '')),
                'tls': 'tls' if params['security'] == 'tls' else '', 'sni': params.get('sni', ''),
                'alpn': params.get('alpn', ''), 'fp': params.get('fp', '')}
        return 'vmess://' + base64.b64encode(json.dumps(data, ensure_ascii=False).encode()).decode()
    host, port = endpoint(server['address'], server['port'])
    password = credential(server['password'])
    if kind == 'shadowsocks':
        if params['type'] != 'tcp' or params['security'] != 'none':
            raise Error('wrapped Shadowsocks transport requires native configuration')
        method = server['method']
        if not isinstance(method, str) or not re.fullmatch(r'[a-z0-9-]{1,64}', method):
            raise Error('invalid Shadowsocks cipher')
        userinfo = base64.urlsafe_b64encode(f'{method}:{password}'.encode()).decode().rstrip('=')
        return f'ss://{userinfo}@{host}:{port}#{quote(label, safe="")}'
    params.pop('encryption', None)
    return f'trojan://{quote(password, safe="")}@{host}:{port}?{urlencode(params, quote_via=quote)}#{quote(label, safe="")}'


def hysteria_link(entry, label):
    # Start with the source, so a conversion that cannot carry a setting does
    # not accidentally make that setting disappear from the exported URI.
    node = copy.deepcopy(entry.original)
    if entry.kind == 'xray':
        common.reject_extra(node, {'tag', 'protocol', 'settings', 'streamSettings'}, 'Hysteria outbound')
        stream = node.get('streamSettings', {})
        common.reject_extra(stream, {'network', 'security', 'hysteriaSettings', 'tlsSettings'}, 'Hysteria transport')
        tls = stream.get('tlsSettings', {})
        # Xray v26.3.27 Hysteria uses crypto/tls through GetTLSConfig, which
        # ignores the uTLS ClientHello fingerprint. It is not a certificate pin.
        common.reject_extra(tls, {'serverName', 'allowInsecure', 'alpn', 'fingerprint'}, 'Hysteria TLS')
        if 'allowInsecure' in tls and not isinstance(tls['allowInsecure'], bool):
            raise Error('Hysteria TLS verification setting must be a boolean')
        if stream.get('network') != 'hysteria' or stream.get('security') != 'tls' or stream.get('hysteriaSettings', {}).get('version', 2) != 2:
            raise Error('Hysteria2 share links require the Hysteria2 transport with TLS')
        node = protocols.xray_to_native(node, [])
    if entry.kind in {'singbox', 'xray'}:
        common.reject_extra(node, {'tag', 'type', 'server', 'server_port', 'server_ports', 'password',
            'auth', 'auth_str', 'up', 'down', 'up_mbps', 'down_mbps', 'obfs', 'tls'}, 'Hysteria share link')
        tls = node.get('tls', {})
        common.reject_extra(tls, {'enabled', 'server_name', 'insecure', 'alpn'}, 'Hysteria TLS')
        if tls.get('enabled') is not True:
            raise Error('Hysteria share links require TLS')
        for direction in ('up', 'down'):
            if direction in node and (node['type'] == 'hysteria2' or direction + '_mbps' in node):
                raise Error('ambiguous Hysteria bandwidth settings require native configuration')
        node = protocols.native_to_mihomo(node)
    return hysteria.mihomo_link(node, label)


def customized_uri(entry, uri):
    """Overlay the checked endpoint on a normalized, lossless source URI.

    Keep original URI-only options and source validation intact. Native outputs
    carry address/port overrides; the immutable source does not.
    """
    if entry.source_entry is None:
        return uri
    target = next(target for target in protocols.TARGETS if target in entry.outputs)
    node, address_key, port_key = server_policy.endpoint(entry.outputs[target], target)
    host, port = node[address_key], node.get(port_key)
    if uri.startswith('vmess://'):
        data = json.loads(protocols.b64(uri[8:]))
        host, port = endpoint(host, port)
        data.update(add=host.strip('[]'), port=port)
        return 'vmess://' + base64.b64encode(json.dumps(data, ensure_ascii=False, separators=(',', ':')).encode()).decode()
    parsed = urlsplit(uri)
    if parsed.scheme in hysteria.SCHEMES:
        _, _, source_port, ranges, _, params = hysteria.parse_uri(uri)
        hopping = node.get('server_ports') or node.get('ports')
        host, port = endpoint(host, source_port if hopping else port)
        if not hopping:
            # An explicit single port replaces both forms of port hopping,
            # even when it equals the first port in the original range.
            ranges = [str(port)]
            params.pop('mport', None)
        port = ','.join(ranges)
        parsed = parsed._replace(query=urlencode(params, quote_via=quote))
    else:
        host, port = endpoint(host, port)
    userinfo, separator, _ = parsed.netloc.rpartition('@')
    return urlunsplit(parsed._replace(netloc=f'{userinfo}{separator}{host}:{port}'))


def entry_link(entry, label):
    if entry.protocol not in SUPPORTED:
        raise Error(UNSUPPORTED)
    if not entry.outputs:
        raise Error('no client accepted this configuration')
    if entry.kind == 'uri':
        return customized_uri(entry, normalize_uri(entry.original, label))
    if entry.protocol in hysteria.SCHEMES:
        return customized_uri(entry, normalize_uri(hysteria_link(entry, label), label))
    node = entry.outputs.get('xray')
    if node is None and 'hiddify' in entry.outputs:
        node = protocols.native_to_xray(entry.outputs['hiddify'])
    if node is None:
        raise Error('no portable share-link representation for this configuration')
    return normalize_uri(xray_link(node, label), label)


def export(entries):
    unique = {}
    for entry in entries:
        if entry.identity not in unique or entry.group_size < unique[entry.identity].group_size:
            unique[entry.identity] = entry
    lines, identities, diagnostics = [], set(), []
    size = 0
    for identity, entry in unique.items():
        # The importer truncates names at 100 characters. Keep account identity first.
        label = re.sub(r'[\x00-\x1f\x7f]', ' ', f'[{identity[:12]}] {entry.name} · {entry.source}')[:100]
        try:
            uri = entry_link(entry, label)
            length = len(uri.encode())
            if length > MAX_ENTRY_BYTES:
                raise Error('URI exceeds V2Board\'s 16 KiB entry limit')
            if len(lines) >= MAX_NODES or size + length + 1 > MAX_BYTES:
                raise Error('V2Board feed reached its 500-entry or 1 MiB limit')
            lines.append(uri)
            size += length + 1
            identities.add(identity[:16])
        except (Error, ValueError, KeyError, TypeError, IndexError, AttributeError) as error:
            diagnostics.append({'source': entry.source, 'name': entry.name, 'client': 'v2board',
                'reason': str(error) if isinstance(error, Error) else 'settings cannot be represented as a V2Board URI'})
    return {'body': '\n'.join(lines) + ('\n' if lines else ''), 'count': len(lines),
            'identities': identities, 'diagnostics': diagnostics}

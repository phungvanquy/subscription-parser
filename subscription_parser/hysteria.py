"""Hysteria share links with the same connection fields as V2Board's importer."""
from __future__ import annotations

import base64
import re
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit

from . import common

SCHEMES = {"hysteria", "hysteria2", "hy2"}
Error = common.SubscriptionError


def ports(value):
    """Validate hopping ranges without expanding them."""
    if not isinstance(value, str):
        raise Error("invalid Hysteria port range")
    ranges = value.split(',')
    if len(ranges) > 64:
        raise Error("too many Hysteria port ranges")
    for item in ranges:
        match = re.fullmatch(r'([1-9][0-9]{0,4})(?:-([1-9][0-9]{0,4}))?', item)
        if not match or not 1 <= int(match[1]) <= int(match[2] or match[1]) <= 65535:
            raise Error("invalid Hysteria port range")
    return ranges


def text(value):
    if not isinstance(value, str) or re.search(r'[\x00-\x1f\x7f]', value):
        raise Error("invalid Hysteria text field")
    return value


def credential(value):
    if len(text(value).encode()) > 1024:
        raise Error("invalid Hysteria authentication")
    return value


def parse_uri(uri):
    connection = uri.split('#', 1)[0]
    if re.search(r'[\x00-\x20\x7f]', connection) or re.search(r'%(?![a-fA-F0-9]{2})', connection):
        raise Error("invalid Hysteria URI encoding")
    parsed = urlsplit(uri)
    if parsed.scheme not in SCHEMES or parsed.path not in ('', '/'):
        raise Error("invalid Hysteria URI scheme or path")
    v1 = parsed.scheme == 'hysteria'
    authority = parsed.netloc.split('@')
    if len(authority) > 2 or (v1 and len(authority) != 1):
        raise Error("invalid Hysteria authentication")
    match = re.fullmatch(r'(\[[^\]]+\]|[^:]+)(?::([0-9,-]+))?', authority[-1])
    if not match:
        raise Error("invalid Hysteria endpoint")
    ranges = ports(match[2] if match[2] is not None else ('' if v1 else '443'))
    host, port = common.host_port(match[1], ranges[0].split('-')[0])
    params = {}
    for key, value in parse_qsl(parsed.query, keep_blank_values=True, errors='strict'):
        if key in params or not re.fullmatch(r'[a-zA-Z][a-zA-Z0-9_-]{0,63}', key):
            raise Error("invalid or repeated Hysteria URI query parameter")
        params[key] = text(value)
    if 'mport' in params:
        hopping = ports(params['mport'])
        if (len(ranges) > 1 or '-' in ranges[0]) and ranges != hopping:
            raise Error("conflicting Hysteria port ranges")
    auth = credential(params.get('auth', '') if v1 else unquote(authority[0], errors='strict') if len(authority) == 2 else '')
    return parsed, host, port, ranges, auth, params


def normalize_uri(uri, label):
    parsed, host, _, ranges, auth, params = parse_uri(uri)
    host = f'[{host}]' if ':' in host else host
    scheme = 'hysteria' if parsed.scheme == 'hysteria' else 'hysteria2'
    user = '' if scheme == 'hysteria' or not auth else quote(auth, safe='') + '@'
    return urlunsplit((scheme, f'{user}{host}:{",".join(ranges)}', '',
                       urlencode(params, quote_via=quote), quote(label, safe='')))


def boolean(value):
    if value not in ('', '0', '1', 'false', 'true'):
        raise Error("invalid Hysteria TLS verification flag")
    return value in ('1', 'true')


def bandwidth(value):
    match = re.fullmatch(r'\s*([0-9]+)(?:\.0+)?\s*(?:[mM](?:bps|b|bit)?)?\s*', str(value))
    if not match or not 1 <= int(match[1]) <= 1000000:
        raise Error("Hysteria share links require integer bandwidth in Mbps")
    return int(match[1])


def uri_to_mihomo(uri):
    parsed, host, port, ranges, auth, params = parse_uri(uri)
    v1 = parsed.scheme == 'hysteria'
    known = {'sni', 'insecure', 'allowInsecure', 'upmbps', 'downmbps', 'alpn', 'obfs', 'mport'}
    known |= {'auth', 'peer', 'protocol', 'obfsParam'} if v1 else {'obfs-password', 'pinSHA256'}
    common.reject_extra(params, known, 'Hysteria link')
    insecure = boolean(params.get('insecure', params.get('allowInsecure', '0')))
    if 'insecure' in params and 'allowInsecure' in params and insecure != boolean(params['allowInsecure']):
        raise Error("conflicting Hysteria TLS verification flags")
    if 'sni' in params and 'peer' in params and params['sni'] != params['peer']:
        raise Error("conflicting Hysteria server names")
    result = {'type': 'hysteria' if v1 else 'hysteria2', 'server': host, 'port': port,
              'auth-str' if v1 else 'password': auth, 'skip-cert-verify': insecure}
    for direction in ('up', 'down'):
        key = direction + 'mbps'
        if v1 or key in params:
            # v1 requires explicit bandwidth; v2 retains its defaults when absent.
            value = params.get(key, '')
            if not re.fullmatch(r'[0-9]+', value):
                raise Error("Hysteria share links require explicit integer bandwidth in Mbps")
            result[direction] = bandwidth(value)
    if v1:
        carrier = params.get('protocol') or 'udp'
        if carrier not in {'udp', 'wechat-video', 'faketcp'}:
            raise Error("unsupported Hysteria v1 carrier")
        result['protocol'] = carrier
    if params.get('sni') or params.get('peer'):
        result['sni'] = params.get('sni') or params['peer']
    alpn = params.get('alpn') or ('hysteria' if v1 else 'h3')
    result['alpn'] = [item.strip() for item in alpn.split(',')]
    if not all(result['alpn']):
        raise Error("invalid Hysteria ALPN list")
    obfs, password = params.get('obfs', ''), params.get('obfsParam' if v1 else 'obfs-password', '')
    if obfs not in {'', 'xplus' if v1 else 'salamander'} or bool(obfs) != bool(password):
        raise Error("unsupported or incomplete Hysteria obfuscation")
    if obfs:
        result['obfs'] = password if v1 else obfs
        if not v1:
            result['obfs-password'] = password
    if params.get('pinSHA256'):
        pin = params['pinSHA256'].replace(':', '')
        if not re.fullmatch(r'[a-fA-F0-9]{64}', pin):
            raise Error("invalid Hysteria certificate fingerprint")
        result['fingerprint'] = pin.lower()
    if 'mport' in params or len(ranges) > 1 or '-' in ranges[0]:
        result['ports'] = params.get('mport', ','.join(ranges))
    return result, unquote(parsed.fragment, errors='strict') or host


def mihomo_link(node, label):
    """Reject fields with no URI equivalent instead of altering the connection."""
    v1 = node['type'] == 'hysteria'
    if node['type'] not in {'hysteria', 'hysteria2'}:
        raise Error("expected a Hysteria proxy")
    known = {'name', 'type', 'server', 'port', 'ports', 'up', 'down', 'sni', 'alpn', 'skip-cert-verify', 'obfs'}
    known |= {'auth', 'auth-str', 'protocol'} if v1 else {'password', 'obfs-password', 'fingerprint'}
    common.reject_extra(node, known, 'Hysteria share link')
    host, port = common.host_port(node['server'], node['port'])
    host = f'[{host}]' if ':' in host else host
    params = {}
    for direction in ('up', 'down'):
        if v1 or direction in node:
            params[direction + 'mbps'] = str(bandwidth(node.get(direction)))
    if v1:
        if node.get('auth') and node.get('auth-str'):
            raise Error("conflicting Hysteria authentication fields")
        auth = node.get('auth-str', '')
        if node.get('auth'):
            try:
                auth = base64.b64decode(node['auth'], validate=True).decode('utf-8')
            except (ValueError, UnicodeError):
                raise Error("binary Hysteria authentication requires native configuration") from None
        params['auth'] = credential(auth)
        params['protocol'] = node.get('protocol') or 'udp'
        if node.get('obfs'):
            params.update(obfs='xplus', obfsParam=text(node['obfs']))
        user = ''
    else:
        auth = credential(node.get('password', ''))
        user = quote(auth, safe='') + '@' if auth else ''
        for key in ('obfs', 'obfs-password'):
            if node.get(key):
                params[key] = text(node[key])
        if node.get('fingerprint'):
            params['pinSHA256'] = text(node['fingerprint'])
    if node.get('sni'):
        params['sni'] = text(node['sni'])
    if 'skip-cert-verify' in node:
        if not isinstance(node['skip-cert-verify'], bool):
            raise Error("Hysteria TLS verification setting must be a boolean")
        params['insecure'] = '1' if node['skip-cert-verify'] else '0'
    if node.get('alpn'):
        alpn = node['alpn']
        if not isinstance(alpn, list) or any(not isinstance(item, str) or not item or ',' in item or item.strip() != item for item in alpn):
            raise Error("invalid Hysteria ALPN list")
        params['alpn'] = ','.join(alpn)
    if node.get('ports'):
        params['mport'] = ','.join(ports(node['ports']))
    uri = urlunsplit((node['type'], f'{user}{host}:{port}', '', urlencode(params, quote_via=quote), quote(label, safe='')))
    uri_to_mihomo(uri)  # Verify the same representation that will be checked/imported.
    return uri

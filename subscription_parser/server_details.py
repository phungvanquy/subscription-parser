"""Readable, per-server settings for the authenticated configuration inspector."""
import json

import yaml

from . import common, protocols


def describe(entry):
    def mapping(value):
        return value if isinstance(value, dict) else {}

    native = entry.outputs.get('hiddify')
    node = native or (entry.original if isinstance(entry.original, dict) else {})
    if not native and entry.kind == 'xray':
        settings = mapping(node.get('settings'))
        endpoints = settings.get('vnext') or settings.get('servers')
        endpoint = mapping(endpoints[0]) if isinstance(endpoints, list) and endpoints else settings
        stream = mapping(node.get('streamSettings'))
        host, port = endpoint.get('address'), endpoint.get('port')
        transport = stream.get('network', 'tcp')
        security = stream.get('security', 'none')
        tls = mapping(stream.get('realitySettings' if security == 'reality' else 'tlsSettings'))
        sni = tls.get('serverName')
    else:
        host = node.get('server')
        port = node.get('server_ports') or node.get('ports') or node.get('server_port') or node.get('port')
        transport = mapping(node.get('transport')).get('type') or node.get('network')
        transport = transport or ('quic' if entry.protocol in {'hysteria', 'hysteria2', 'tuic'} else 'tcp')
        tls = node.get('tls', {})
        if isinstance(tls, dict):
            security = 'reality' if mapping(tls.get('reality')).get('enabled') else 'tls' if tls.get('enabled') else 'none'
            sni = tls.get('server_name')
        else:
            security = 'reality' if node.get('reality-opts') else 'tls' if tls else 'none'
            sni = node.get('servername') or node.get('sni')
    fields = [('Address', host), ('Port', ', '.join(map(str, port)) if isinstance(port, list) else port),
              ('Transport', str(transport).upper() if node else None),
              ('TLS', {'tls': 'Enabled', 'reality': 'Reality', 'none': 'Not enabled'}.get(security, security) if node else None)]
    if sni:
        fields.append(('Server name (SNI)', sni))
    return [{'label': label, 'value': str(value) if value is not None else 'Not specified'} for label, value in fields]


def configurations(entry):
    def document(identifier, label, value, language, description):
        content = (yaml.safe_dump(value, allow_unicode=True, sort_keys=False) if language == 'yaml'
                   else json.dumps(value, ensure_ascii=False, indent=2) + '\n' if language == 'json' else value)
        extension = 'txt' if language == 'uri' else language
        return {'id': identifier, 'label': label, 'content': content, 'language': language,
                'filename': f'server-{entry.identity[:16]}-{identifier}.{extension}', 'description': description}

    language = 'uri' if isinstance(entry.original, str) else 'yaml' if entry.kind == 'mihomo' else 'json'
    source_format = {'singbox': 'sing-box JSON', 'xray': 'Xray JSON', 'mihomo': 'Mihomo YAML'}.get(entry.kind, 'URI')
    result = [document('original', f'Original · {source_format}', entry.original, language,
                       f'Original server settings from {entry.source}.')]
    for target, label in [('mihomo', 'Mihomo'), ('xray', 'Xray'), ('hiddify', 'Hiddify / sing-box')]:
        if target not in entry.outputs:
            continue
        try:
            nodes = protocols.bundle(entry, target, entry.name)
        except common.SubscriptionError:
            continue
        result.append(document(target, label, {'proxies' if target == 'mihomo' else 'outbounds': nodes},
                               'yaml' if target == 'mihomo' else 'json',
                               'Server settings with any required dependencies. Add these to an existing client profile.'))
    return result

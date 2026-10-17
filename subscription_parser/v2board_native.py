"""Versioned, bounded native configurations for V2Board External Subscriptions.

Only proxy bundles cross this boundary. Routing, DNS, listeners, local files,
and references to the receiving client's existing proxies never do.
"""
from __future__ import annotations

import json
import math
import re

from . import common, connectivity, protocols, v2board

FORMAT = 'subscription-parser'
SCHEMA_VERSION = 1
MAX_NODE_BYTES = 65536
MAX_BUNDLE = 21
TYPES = {'vless', 'vmess', 'trojan', 'shadowsocks', 'ss', 'hysteria', 'hysteria2',
         'tuic', 'anytls', 'socks', 'socks5', 'http'}
HELPERS = {'direct', 'block', 'freedom', 'blackhole'}
FIELDS = {
    'hiddify': set('type tag server server_port server_ports uuid password username method security alter_id '
                  'flow packet_encoding tls transport multiplex detour up down up_mbps down_mbps auth auth_str '
                  'obfs hop_interval network version congestion_control udp_relay_mode zero_rtt_handshake '
                  'heartbeat idle_session_check_interval idle_session_timeout min_idle_session '
                  'connect_timeout tcp_fast_open tcp_multi_path udp_fragment domain_strategy'.split()),
    'mihomo': set('type name server port ports uuid password username cipher alterId flow packet-encoding '
                 'tls servername sni skip-cert-verify client-fingerprint fingerprint alpn reality-opts network '
                 'ws-opts grpc-opts http-opts h2-opts xhttp-opts udp dialer-proxy up down auth auth-str '
                 'obfs obfs-password recv-window recv-window-conn disable-mtu-discovery fast-open '
                 'hop-interval congestion-controller udp-relay-mode reduce-rtt heartbeat-interval '
                 'ip-version smux client-id idle-session-check-interval idle-session-timeout min-idle-session'.split()),
    'xray': {'protocol', 'tag', 'settings', 'streamSettings', 'mux', 'proxySettings'},
}
# These fields can read local files, configure listeners, or alter global policy.
FORBIDDEN = {'inbounds', 'routing', 'route', 'dns', 'log', 'experimental', 'endpoints',
             'certificate_path', 'key_path', 'certificateFile', 'keyFile', 'masterKeyLog',
             'echConfigList', 'ech_config_path', 'client_certificate_path', 'client_key_path',
             'certificate', 'private-key', 'ca', 'ca-str', 'interface_name', 'interface-name',
             'bind_interface', 'bind_address', 'sendThrough', 'plugin', 'plugin-opts'}


def _walk(value, depth=0):
    if depth > 24:
        raise common.SubscriptionError('native configuration nesting exceeds the supported limit')
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str) or key in FORBIDDEN or re.search(r'[\x00-\x1f\x7f]', key):
                raise common.SubscriptionError('native configuration contains a local-resource or global setting')
            if key == 'sockopt':
                if not isinstance(child, dict) or set(child) - {'dialerProxy', 'tcpNoDelay'}:
                    raise common.SubscriptionError('native configuration contains unsupported socket settings')
                reference = child.get('dialerProxy')
                if ('dialerProxy' in child and (not isinstance(reference, str) or not reference
                        or re.search(r'[\x00-\x1f\x7f]', reference))) or (
                        'tcpNoDelay' in child and not isinstance(child['tcpNoDelay'], bool)):
                    raise common.SubscriptionError('native configuration contains invalid socket settings')
            if key == 'headers' and (not isinstance(child, dict) or len(child) > 64 or any(
                    not isinstance(k, str) or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", k)
                    or not isinstance(v, str) or re.search(r'[\x00-\x1f\x7f]', v)
                    for k, v in child.items())):
                raise common.SubscriptionError('native configuration contains invalid HTTP headers')
            _walk(child, depth + 1)
    elif isinstance(value, list):
        for child in value:
            _walk(child, depth + 1)
    elif isinstance(value, float) and not math.isfinite(value):
        raise common.SubscriptionError('native configuration contains a non-finite number')
    elif not isinstance(value, (str, int, float, bool, type(None))):
        raise common.SubscriptionError('native configuration contains an invalid value')


def checked_bundle(entry, target):
    bundle = protocols.bundle(entry, target, 'node')
    if not 1 <= len(bundle) <= MAX_BUNDLE:
        raise common.SubscriptionError('native configuration has too many dependencies')
    kind_key, name_key = ('protocol', 'tag') if target == 'xray' else ('type', 'name' if target == 'mihomo' else 'tag')
    labels = {node.get(name_key) for node in bundle}
    if len(labels) != len(bundle) or any(not isinstance(label, str) or not label or len(label.encode()) > 512
                                        or re.search(r'[\x00-\x1f\x7f]', label) for label in labels):
        raise common.SubscriptionError('native configuration has invalid dependency names')
    for index, node in enumerate(bundle):
        if node.get(kind_key) not in (TYPES if index == 0 else TYPES | HELPERS):
            raise common.SubscriptionError('native protocol is not supported by this integration version')
        if set(node) - FIELDS[target]:
            raise common.SubscriptionError('native configuration contains unsupported outbound fields')
        _walk(node)
        if any(ref not in labels for ref in protocols.dependencies(node, target)):
            raise common.SubscriptionError('native configuration references an external dependency')
    return bundle


def _endpoint(entry):
    for target in ('hiddify', 'mihomo', 'xray'):
        node = entry.outputs.get(target)
        if node is None:
            continue
        if target == 'xray':
            settings = node.get('settings', {})
            server = (settings.get('vnext') or settings.get('servers') or [settings])[0]
            return common.host_port(server.get('address'), server.get('port'))
        return common.host_port(node.get('server'), node.get('server_port' if target == 'hiddify' else 'port')
                                or str((node.get('server_ports') or [node.get('ports', '')])[0]).split(':')[0].split(',')[0].split('-')[0])
    raise common.SubscriptionError('no passing client configuration')


def export(entries, results, max_age, evaluated_at, generated_at, valid_until):
    document = {'format': FORMAT, 'schema_version': SCHEMA_VERSION, 'generated_at': generated_at,
                'valid_until': int(valid_until), 'nodes': []}
    identities, diagnostics = set(), []
    size = len((json.dumps(document, ensure_ascii=False, separators=(',', ':')) + '\n').encode())
    for entry in connectivity.unique_entries(entries):
        try:
            if not entry.outputs:
                continue
            if len(document['nodes']) >= v2board.MAX_NODES:
                raise common.SubscriptionError('native feed reached its 500-node limit')
            host, port = _endpoint(entry)
            v2board.endpoint(host, port)
            configs, notes, rejected = {}, list(entry.notes), []
            for target in protocols.TARGETS:
                if target not in entry.outputs:
                    continue
                try:
                    configs[target] = checked_bundle(entry, target)
                except common.SubscriptionError as exc:
                    rejected.append(f'{target}: {exc}')
            notes.extend(rejected)
            label = re.sub(r'[\x00-\x1f\x7f]', ' ', f'[{entry.identity[:12]}] {entry.name} · {entry.source}')[:100]
            node = {'id': entry.identity, 'name': label, 'protocol': entry.protocol, 'server': host,
                    'port': port, 'configs': configs, 'notes': notes,
                    'checks': {core: {key: value for key, value in connectivity.current_result(
                        entry, core, results, max_age, evaluated_at).items()
                        if key in {'status', 'checked_at', 'latency_ms', 'reason'}} for core in connectivity.CORES}}
            try:
                uri = v2board.entry_link(entry, label)
                if len(uri.encode()) <= v2board.MAX_ENTRY_BYTES:
                    node['uri'] = uri
            except (common.SubscriptionError, ValueError, KeyError, TypeError):
                pass
            if not configs and 'uri' not in node:
                raise common.SubscriptionError('no supported native configuration or portable URI'
                                               + (': ' + '; '.join(rejected) if rejected else ''))
            node_size = len(json.dumps(node, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode())
            if node_size > MAX_NODE_BYTES:
                raise common.SubscriptionError('native node exceeds the 64 KiB limit')
            addition = node_size + bool(document['nodes'])
            if size + addition > v2board.MAX_BYTES:
                raise common.SubscriptionError('native feed reached its 1 MiB limit')
            document['nodes'].append(node)
            size += addition
            identities.add(entry.identity[:16])
        except (common.SubscriptionError, ValueError, KeyError, TypeError, IndexError) as exc:
            diagnostics.append({'source': entry.source, 'name': entry.name, 'client': 'v2board',
                                'reason': str(exc) if isinstance(exc, common.SubscriptionError)
                                else 'native configuration cannot be represented safely'})
    return {'body': json.dumps(document, ensure_ascii=False, separators=(',', ':'), allow_nan=False) + '\n',
            'count': len(document['nodes']), 'identities': identities, 'diagnostics': diagnostics}

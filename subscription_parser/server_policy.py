"""Persistent source bindings and small, format-preserving server overrides."""
from __future__ import annotations

import copy
import hashlib
import json

from . import common, protocols

MAX_RECORDS = 1000


def source_key(entry):
    """Bind exact original native settings, not only the portable dedup identity.

    The original record and dependency identity survive validation stripping an
    output. Display-only names are ignored, including VMess's encoded ps label.
    """
    if not entry.source_identity:
        original = copy.deepcopy(entry.original)
        if isinstance(original, dict):
            original.pop('name', None)
            original.pop('tag', None)
        elif isinstance(original, str):
            original = original.partition('#')[0]
            if original.partition('://')[0].lower() == 'vmess':
                try:
                    original = json.loads(protocols.b64(original.split('://', 1)[1]))
                    original.pop('ps', None)
                except (common.SubscriptionError, ValueError, AttributeError):
                    original = entry.original.partition('#')[0]
        entry.source_identity = hashlib.sha256(json.dumps([entry.kind, entry.identity, original],
            sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return entry.source_identity


def overrides(values):
    """An absent override inherits the source; reject invalid endpoint values."""
    name = (values.get('name') or '').strip() or None
    address = (values.get('address') or '').strip() or None
    port = values.get('port')
    if name and (len(name) > 200 or any(ord(c) < 32 or ord(c) == 127 for c in name)):
        raise common.SubscriptionError('custom name must contain at most 200 characters without control characters')
    if address:
        if len(address) > 253 or any(ord(c) < 32 or ord(c) == 127 for c in address):
            raise common.SubscriptionError('invalid custom server address')
        address, _ = common.host_port(address, 443)
    if port is not None and (type(port) is not int or not 1 <= port <= 65535):
        raise common.SubscriptionError('custom port must be between 1 and 65535')
    return {'name': name, 'address': address, 'port': port}


def endpoint(node, target):
    if target == 'xray':
        settings = node.get('settings', {})
        endpoints = settings.get('vnext') or settings.get('servers')
        if endpoints is not None:
            if not isinstance(endpoints, list) or len(endpoints) != 1 or not isinstance(endpoints[0], dict):
                raise common.SubscriptionError('server has an ambiguous endpoint')
            settings = endpoints[0]
        if not isinstance(settings, dict) or 'address' not in settings or 'port' not in settings:
            raise common.SubscriptionError('this Xray endpoint cannot be customized')
        return settings, 'address', 'port'
    address_key, port_key = 'server', 'port' if target == 'mihomo' else 'server_port'
    if address_key not in node or not any(k in node for k in (port_key, 'ports', 'server_ports')):
        raise common.SubscriptionError('this endpoint cannot be customized')
    return node, address_key, port_key


def describe(entry):
    for target in ('hiddify', 'mihomo', 'xray'):
        if target not in entry.outputs:
            continue
        try:
            node, address_key, port_key = endpoint(entry.outputs[target], target)
            port = node.get('server_ports') or node.get('ports') or node.get(port_key)
            return {'name': entry.name, 'protocol': entry.protocol, 'address': node[address_key],
                    'port': ', '.join(map(str, port)) if isinstance(port, list) else port}
        except common.SubscriptionError:
            pass
    return {'name': entry.name, 'protocol': entry.protocol, 'address': None, 'port': None}


def customize(entry, values):
    """Edit each existing native output, keeping all other settings/dependencies.

    Converting the native output back through a common format would discard
    settings supported by only one core. Never modify the cached source entry.
    """
    # Source context is immutable; bundle() copies every dependency before use.
    # Copying the entire provider context once per server would be quadratic.
    edited = copy.deepcopy(entry, {id(entry.context): entry.context})
    if values.get('name'):
        edited.name = values['name']
    changed = False
    if values.get('address') is not None or values.get('port') is not None:
        for target, output in list(edited.outputs.items()):
            try:
                node, address_key, port_key = endpoint(output, target)
                before = copy.deepcopy(output)
                if values.get('address') is not None:
                    node[address_key] = values['address']
                if values.get('port') is not None:
                    node[port_key] = values['port']
                    # A single-port override explicitly replaces port hopping.
                    node.pop('server_ports', None)
                    node.pop('ports', None)
                changed |= before != output
            except common.SubscriptionError as error:
                edited.outputs.pop(target)
                edited.issues[target] = str(error)
                changed = True
    if changed:
        # Keep source matching independent from the effective configuration.
        # Equal variants in different subscriptions reuse the same checks.
        documents = {}
        for target in edited.outputs:
            try:
                documents[target] = protocols.bundle(edited, target, 'probe')
            except common.SubscriptionError:
                documents[target] = edited.outputs[target]
        edited.identity = hashlib.sha256(json.dumps([entry.identity, documents], sort_keys=True,
                                                   ensure_ascii=False).encode()).hexdigest()
        edited.source_entry = entry
    return edited


def apply(by_provider, provider_ids, policy):
    bindings = {(r['provider_id'], r['source_identity']): r for r in policy.get('records', [])}
    named, nodes = [], []
    for provider_id in provider_ids:
        for entry in by_provider.get(provider_id, []):
            record = bindings.get((provider_id, source_key(entry))) if bindings else None
            if not policy.get('all_servers', True) and (not record or not record['selected']):
                continue
            (named if record and record.get('name') else nodes).append(
                customize(entry, record or {}))
    # Prefer an explicitly named copy when duplicate sources describe the same
    # configuration. Endpoint variants have different identities and both survive.
    return named + nodes

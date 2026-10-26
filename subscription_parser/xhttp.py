"""Verified Xray packet-up options for Mihomo; other adapters stay unchanged."""
from __future__ import annotations

import copy
import re

from .common import SubscriptionError as Error

BASE = {'host', 'path', 'mode'}
STRINGS = {
    'xPaddingKey': 'x-padding-key', 'xPaddingHeader': 'x-padding-header',
    'xPaddingPlacement': 'x-padding-placement', 'xPaddingMethod': 'x-padding-method',
    'uplinkHTTPMethod': 'uplink-http-method', 'uplinkDataPlacement': 'uplink-data-placement',
    'uplinkDataKey': 'uplink-data-key', 'sessionPlacement': 'session-placement',
    'sessionKey': 'session-key', 'seqPlacement': 'seq-placement', 'seqKey': 'seq-key',
}
RANGES = {'xPaddingBytes': 'x-padding-bytes', 'scMaxEachPostBytes': 'sc-max-each-post-bytes',
          'scMinPostsIntervalMs': 'sc-min-posts-interval-ms', 'uplinkChunkSize': 'uplink-chunk-size'}
BOOLS = {'xPaddingObfsMode': 'x-padding-obfs-mode', 'noGRPCHeader': 'no-grpc-header'}
XMUX = {'maxConcurrency': 'max-concurrency', 'maxConnections': 'max-connections',
        'cMaxReuseTimes': 'c-max-reuse-times', 'hMaxRequestTimes': 'h-max-request-times',
        'hMaxReusableSecs': 'h-max-reusable-secs'}
ALIASES = {'sessionIDPlacement': 'sessionPlacement', 'sessionIDKey': 'sessionKey'}
ALLOWED = BASE | STRINGS.keys() | RANGES.keys() | BOOLS.keys() | ALIASES.keys() | {
    'headers', 'xmux', 'scMaxBufferedPosts'}


def range_value(value):
    """The shared, nonnegative int32/range subset; booleans are not numbers."""
    if type(value) is int:
        low = high = value
    elif isinstance(value, str) and re.fullmatch(r'[0-9]+(?:-[0-9]+)?', value):
        low, _, high = value.partition('-')
        low, high = int(low), int(high or low)
    else:
        raise Error('XHTTP ranges require an integer or an integer range')
    if not 0 <= low <= high <= 2147483647:
        raise Error('XHTTP range is outside the supported bounds')
    return str(low) if low == high else f'{low}-{high}'


def text(value):
    if not isinstance(value, str) or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise Error('XHTTP text settings must be strings without control characters')
    return value


def mihomo_options(settings):
    """Mirror the pinned Xray extra precedence and defaults, with strict keys."""
    if not isinstance(settings, dict):
        raise Error('XHTTP settings must be an object')
    if set(settings) - ALLOWED - {'extra'}:
        raise Error('unmapped XHTTP settings require the original client configuration')
    options = copy.deepcopy(settings)
    if 'extra' in settings:
        extra = settings['extra']
        if not isinstance(extra, dict):
            raise Error('XHTTP extra must be an object')
        if set(extra) - ALLOWED:
            raise Error('unmapped XHTTP extra settings require the original client configuration')
        # Xray replaces ALL non-base fields with extra, rather than merging it.
        options = copy.deepcopy(extra)
        for key in BASE:
            options[key] = settings.get(key, '')
    for key in BASE | STRINGS.keys():
        if key in options:
            text(options[key])
    if options.get('mode') != 'packet-up':
        raise Error('advanced XHTTP conversion currently requires packet-up mode')
    path = options.get('path') or '/'
    if '?' in path or '#' in path:
        raise Error('advanced XHTTP query paths require the original client configuration')
    # Xray always appends a slash, including when session/sequence use headers.
    path = ('/' if not path.startswith('/') else '') + path
    path += '' if path.endswith('/') else '/'
    result = {'path': path, 'mode': 'packet-up'}
    if options.get('host'):
        result['host'] = options['host']
    notes = []
    for alias, canonical in ALIASES.items():
        if alias in options:
            text(options[alias])
            if canonical not in options or options[alias] != options[canonical]:
                raise Error('ambiguous XHTTP session aliases require the original client configuration')
            notes.append('Redundant XHTTP sessionID alias matches the canonical session setting')
    headers = options.get('headers', {})
    if (not isinstance(headers, dict) or any(not isinstance(k, str) or not re.fullmatch(
            r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", k) for k in headers)):
        raise Error('XHTTP headers require valid string names and values')
    if 'host' in {k.lower() for k in headers} or len({k.lower() for k in headers}) != len(headers):
        raise Error('XHTTP headers cannot contain Host or ambiguous duplicate names')
    if headers:
        result['headers'] = {k: text(v) for k, v in headers.items()}
    for old, new in STRINGS.items():
        if old in options:
            result[new] = options[old]
    enums = {
        'x-padding-placement': ({'cookie', 'header', 'query', 'queryInHeader'}, 'queryInHeader'),
        'x-padding-method': ({'repeat-x', 'tokenish'}, 'repeat-x'),
        'session-placement': ({'path', 'cookie', 'header', 'query'}, 'path'),
        'seq-placement': ({'path', 'cookie', 'header', 'query'}, 'path'),
        'uplink-data-placement': ({'auto', 'body', 'cookie', 'header'}, 'auto'),
    }
    for key, (allowed, default) in enums.items():
        value = result.get(key) or default
        if value not in allowed:
            raise Error('unsupported XHTTP placement or padding method')
        result[key] = value
    method = (result.get('uplink-http-method') or 'POST').upper()
    if method not in {'POST', 'GET'}:
        raise Error('advanced XHTTP conversion supports POST or GET uplinks')
    result['uplink-http-method'] = method
    for key, default in [('x-padding-key', 'x_padding'), ('x-padding-header', 'X-Padding')]:
        result[key] = result.get(key) or default
    placement = result['uplink-data-placement']
    result['uplink-data-key'] = result.get('uplink-data-key') or ('x_data' if placement == 'cookie' else 'X-Data')
    for old, new in BOOLS.items():
        if old in options:
            if type(options[old]) is not bool:
                raise Error('XHTTP boolean settings must be true or false')
            result[new] = options[old]
    for old, new in RANGES.items():
        value = range_value(options.get(old, 0))
        if old == 'xPaddingBytes' and value != '0' and value.startswith('0-'):
            raise Error('XHTTP padding cannot be disabled')
        result[new] = value
    # Preserve Xray defaults; Mihomo differs for header chunk sizes and XMUX.
    for key, default in [('x-padding-bytes', '100-1000'), ('sc-max-each-post-bytes', '1000000'),
                         ('sc-min-posts-interval-ms', '30')]:
        if result[key] == '0':
            result[key] = default
    chunk = result['uplink-chunk-size']
    if chunk == '0':
        result['uplink-chunk-size'] = {'header': '3000-4000', 'cookie': '2048-3072'}.get(
            placement, result['sc-max-each-post-bytes'])
    elif int(chunk.split('-')[0]) < 64:
        result['uplink-chunk-size'] = range_value(f'64-{max(64, int(chunk.split("-")[-1]))}')
    if 'scMaxBufferedPosts' in options:
        value = options['scMaxBufferedPosts']
        if type(value) is not int or not 0 <= value <= 2147483647:
            raise Error('XHTTP server buffer setting must be a nonnegative integer')
        notes.append('Xray scMaxBufferedPosts is server-only and does not affect the outbound connection')
    xmux = options.get('xmux', {})
    if not isinstance(xmux, dict) or set(xmux) - XMUX.keys() - {'hKeepAlivePeriod'}:
        raise Error('unmapped XHTTP XMUX settings require the original client configuration')
    reuse = {new: range_value(xmux.get(old, 0)) for old, new in XMUX.items()}
    keepalive = xmux.get('hKeepAlivePeriod', 0)
    if type(keepalive) is not int or not -1 <= keepalive <= 2147483647:
        raise Error('XHTTP keepalive period must be a supported integer')
    if reuse['max-connections'] != '0' and reuse['max-concurrency'] != '0':
        raise Error('XHTTP cannot specify both maximum connections and concurrency')
    if all(value == '0' for value in reuse.values()) and keepalive == 0:
        reuse.update({'max-concurrency': '1', 'h-max-request-times': '600-900', 'h-max-reusable-secs': '1800-3000'})
    result['reuse-settings'] = {**reuse, 'h-keep-alive-period': keepalive}
    return result, notes

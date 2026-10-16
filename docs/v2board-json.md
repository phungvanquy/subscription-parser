# V2Board native JSON contract

[Documentation home](../README.md#documentation) · [Import instructions](usage.md#v2board-external-subscriptions)

`/sub/<token>/v2board.json` transfers passing proxy configurations to the updated
V2Board External Subscriptions importer. It is separate from `combined.json` and
from Hiddify's native configuration schema. Older V2Board installations should
continue using `v2board.txt` until their importer is updated.

## Envelope and nodes

| Field | Meaning |
| --- | --- |
| `format` | Exactly `subscription-parser`. |
| `schema_version` | Integer `1`. Unknown versions are rejected. |
| `generated_at` | ISO 8601 publication timestamp. |
| `valid_until` | Unix timestamp limiting reuse of the connection-check evidence. |
| `nodes` | Ordered array of proxy records. |

Each node contains a stable SHA256 `id`, display `name`, `protocol`, `server`,
integer `port`, `configs`, `checks`, and `notes`. An optional `uri` is present
only when the existing share-link exporter can represent the settings faithfully.

`configs` maps `hiddify`, `mihomo`, and/or `xray` to arrays of native outbounds.
The first outbound is the selectable server. Remaining outbounds are its
dependencies, referenced by `detour`, `dialer-proxy`, `proxySettings.tag`, or
Xray's `streamSettings.sockopt.dialerProxy`.
References stay within the bundle. V2Board renames them together to prevent
collisions with other sources and managed nodes.

`checks` records Mihomo/Xray status and, when available, `checked_at`,
`latency_ms`, and a sanitized `reason`. Every published node needs a current
passing check. Mihomo and Xray configurations are included only when their own
core passed the exact configuration. Hiddify needs a compatible passing check
through either core; this is not a separate Hiddify connection test.

`notes` preserves conversion limitations. It is not an instruction to recreate
an excluded output. V2Board does not regenerate missing native formats from the
optional URI, which could otherwise reintroduce a variant that failed its check.

## Protocols and client outputs

The integration accepts native VLESS, VMess, Trojan, Shadowsocks, Hysteria v1/v2,
TUIC, AnyTLS, SOCKS, and HTTP proxy bundles. Availability depends on Parser's
conversion, installed-core validation, and passing connection checks. Additional
protocols require a contract update before crossing this boundary.

| V2Board destination | Behavior |
| --- | --- |
| Hiddify | Uses the native Hiddify bundle, including supported XHTTP. |
| Current sing-box | Uses compatible Hiddify bundles; excludes Hiddify-specific XHTTP. |
| Legacy sing-box | Also excludes native `server_ports` hopping. |
| Mihomo | Uses the checked native Mihomo bundle. |
| URI clients, including V2rayNG | Uses only the optional portable URI. Native-only nodes are omitted. |
| Admin Xray JSON preview | Shows the native Xray bundle with dependencies; it is a snippet, not a complete client setup. |

V2Board retains its managed routing and manual backup selection. Native JSON
does not make all protocols available in every client or introduce a full Xray
JSON user-subscription endpoint.

## Validation and storage

The importer rejects an invalid document atomically. Limits are **1 MiB** per
feed, **500 nodes**, **64 KiB** per node, **21 outbounds** per bundle, and bounded
nesting. Names, endpoints, header values, dependency references, and supported
fields are validated. Dependencies cannot be missing, cyclic, or unrelated.

Only outbound configurations cross this boundary. Global routing, DNS,
listeners, local certificate/key paths, interface binding, and plugin execution
settings are rejected. A supported protocol name does not permit arbitrary
client configuration fields. These boundaries can exclude a configuration that
remains available in Parser's direct client feed; inspect compatibility notes.

V2Board encrypts the source URL and imported snapshot with its existing
`APP_KEY`. Native bundles retain JSON object/array distinctions through storage,
including empty header objects. The admin viewer masks credentials, headers,
conversion notes, and private settings unless explicitly revealed.

## Expiry and rollout

V2Board limits the snapshot to the earlier of `valid_until` and any provider
expiry header. Refresh is scheduled no later than half the remaining lifetime,
subject to the scheduler's one-minute resolution and a one-minute minimum.
Network errors can retain a previous snapshot only until its deadline. Expired
native feeds invalidate the cached snapshot; disabled sources and group access
rules continue to apply.

Deploy both applications, verify the JSON feed, then replace the source URL's
`v2board.txt` suffix with `v2board.json` and refresh. Keep source names, groups,
and provider/HWID assignments unchanged. A rollback to an older V2Board importer
requires switching back to `.txt` and refreshing its cache before downgrading.

The synthetic [shared fixture](../tests/fixtures/v2board-native-v1.json) exercises
custom WebSocket headers, heartbeat, mux/XUDP, and Hysteria2 QUIC settings in both
repositories. It contains no live credentials.

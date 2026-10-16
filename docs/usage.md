# User guide

[Documentation home](../README.md#documentation)

A **provider** is an upstream subscription URL. A **device** is a saved hardware
identifier (HWID) used to fetch a provider. An **output subscription** is a named
selection of providers with its own client import links.

## Add a provider

1. Choose **Add provider** and enter a name and HTTPS subscription URL.
2. Select a saved device, choose **Add device** to save an existing authorized
   HWID, or explicitly select **No device ID** if the provider does not need one.
3. Choose **Test fetch**. This contacts the provider and checks whether its
   response can produce client-compatible configurations.
4. Review the result and save. Saving a successful test reuses its response.
5. Check **Server library** for connectivity results before importing a feed.

You can paste multiple links, one per line, and assign a device to each link.
A saved HWID is never preselected automatically. The application supports up to
**50 providers**. Provider names must be unique.

Changing a provider's URL, device assignment, or user agent in the dashboard
requires another fetch test. A name-only edit does not. Fetch tests validate
retrieval and conversion; the separate connection checks test remote servers.

The **Providers** page shows working servers alongside each provider's total.
Expand **By client** for the compatible format counts; the **⋯** menu contains
refresh, edit, pause/enable, and remove actions. Working means included in at least
one format with current passing connection checks; pending, failed, and expired
checks do not count. A shared server counts once for each provider that supplies it.
Select the working count to open **Server library**, filtered to that provider's
passing servers. Use **All providers** and **All results** to clear those filters.

### Refresh and cache behavior

Provider refreshes run hourly by default. Change the interval in
**Settings → Refresh schedule**, or use **Refresh all** or a provider's refresh
action for an immediate download.

A provider's **Refresh provider** action downloads and checks only that provider's
servers. Other providers keep their cached responses and check timestamps, and
their scheduled refreshes are not postponed. Identical configurations shared by
multiple providers are checked once; each distinct native configuration keeps
its own result. **Refresh all** downloads and checks all enabled providers.

A failed or invalid download can reuse the last successful response for the
same URL, user agent, and device assignment/revision. The provider is marked
**Cached**. Its nodes still need recent passing connection checks to be published.
Changing those request settings prevents reuse of the old identity's response.
Paused and deleted providers contribute no servers to published feeds.

## Manage device IDs

Save an HWID already authorized by the provider. Subscription Parser sends the
exact saved value as `x-hwid`; it does not generate replacements when a provider
rejects a device or reports a device limit. **No device ID** omits that header.

The **Devices** page lists labels, masked IDs, and their assigned providers,
including paused providers. **Saved** means the ID is stored locally. A successful
provider fetch determines whether that provider accepts it.

| Action | Behavior |
| --- | --- |
| Rename | Changes the label while retaining the ID and assignments. |
| Reveal ID / Copy ID | Retrieves the value through an authenticated, uncached request. The reveal clears on close, after one minute, or when the tab is hidden. |
| Delete | Removes an unused device; assigned devices cannot be deleted. |
| Replace ID | Shows affected providers and requires confirmation. Rejects stale revisions or changed assignments. |

Replacement removes responses fetched under the old identity from affected
publications without contacting the providers. Refresh those providers to fetch
with the replacement. Routine refreshes and connection checks do not rotate IDs.

## Create output subscriptions

Open **Subscriptions → New subscription**, enter a name, and choose either:

- **Use all providers**, which includes all enabled providers and automatically
  includes providers added later.
- One or more specific providers, which keeps that selection even when a provider
  is renamed. Paused providers remain selected but contribute no servers.

There is no fixed limit on the number of output subscriptions. Each has six
independent client URLs. Creating or editing a selection uses cached responses
and shared check results; it does not fetch upstream subscriptions or register
devices. Distinct selections require local conversion, validation, and export
storage. Identical selections share the generated publication.

The original subscription has the internal ID `default` and initially uses the
name **All providers**. It can be renamed, scoped to selected providers, and have
its links rotated, but cannot be deleted. Other subscriptions can be deleted
independently.

### Find, edit, and rotate links

Search the subscription list by subscription or provider name. Select an entry
to see its selected providers, included/excluded totals, and per-client counts.
On a phone, **All subscriptions** returns to the list.

| Action | Result |
| --- | --- |
| Copy link | Copies the selected client's URL. |
| Show URLs | Reveals the addresses for manual selection or inspection. |
| Edit | Changes the name or provider selection while retaining the URLs. |
| More → Rotate links | Replaces this subscription's token; its previous URLs stop working. |
| More → Delete | Removes this subscription and invalidates its URLs. |

Deleting a provider removes its assignments. A selection that becomes empty
stays empty; it does not fall back to all providers. Rotating or deleting a link
cannot remove configurations a client has already downloaded.

## Import into clients

Each subscription has URLs of the form:

```text
https://subscriptions.example.com/sub/<token>/<filename>
```

Use the URL copied from the dashboard. The token grants access to that
subscription and should be kept private.

| Client or purpose | File | Contents |
| --- | --- | --- |
| Hiddify | `hiddify.json` | Native Hiddify/sing-box outbounds. |
| Mihomo | `mihomo.yaml` | Proxies, selection group, and routing rule; also usable as a proxy-provider feed. |
| Xray | `xray.json` | Outbounds and their dependencies; integrate these into your client's configuration. |
| V2Board External Subscriptions | `v2board.json` | Versioned native proxy configurations and connection-check metadata. Requires the updated V2Board importer. |
| Legacy V2Board / URI clients | `v2board.txt` | Plain, newline-separated proxy share links. |
| Diagnostics or integration | `combined.json` | Schema-version-2 archive containing client configurations, published servers, excluded-server metadata, and compatibility notes. |

The Xray export is not a complete operating-system proxy setup. The combined
archive is not a universal client import format.

### V2Board External Subscriptions

For a V2Board installation with the native JSON External Subscriptions integration:

1. Update V2Board before switching an existing source to JSON.
2. Copy the **V2Board** JSON URL from the selected subscription.
3. Paste it into **Advanced Settings → External Subscriptions** in V2Board.
4. Assign the allowed user groups, save, and choose **Refresh now**.

Use `v2board.json` for the updated integration. Upstream provider fetches remain
the responsibility of Subscription Parser and use its saved HWIDs. The combined
archive is a different schema and is not a V2Board input.

JSON preserves accepted native settings, including custom WebSocket headers,
client-specific options, and outbound dependencies. V2Board uses each client's
available configuration without converting it back through a share link. User
group access and the **Backup (manual)** selection policy still apply.

Client counts can differ. Hiddify can receive native XHTTP configurations that
standard sing-box cannot use. Xray JSON is available in the admin configuration
viewer; URI clients such as V2rayNG still receive only nodes with a portable URI.
The integration does not turn those clients into native JSON consumers.

The JSON feed is limited to **500 nodes**, **1 MiB total**, and **64 KiB per node**.
Its connection-check deadline also limits V2Board's cached snapshot. V2Board
refreshes sooner when needed and stops serving an expired snapshot. See the
[native JSON contract](v2board-json.md) for supported protocols and boundaries.

`v2board.txt` remains available for compatibility. It supports VLESS, VMess,
Trojan, Shadowsocks, Hysteria v1, and Hysteria2 within **500 entries**, **1 MiB
total**, and **16 KiB per entry**. TUIC, AnyTLS, custom WebSocket headers, and other
settings without a faithful share-link representation remain excluded. The
dashboard distinguishes **V2Board JSON** and **V2Board URI** counts and notes.

## Formats and compatibility

Accepted inputs are Xray JSON profiles, native sing-box JSON, Mihomo/Clash YAML
or JSON, and plain or base64 share-link subscriptions. Provider URLs must use
HTTPS without embedded HTTP login credentials. Downloads are limited to 12 MiB.

Common adapters cover VLESS, VMess, Shadowsocks, Trojan, Hysteria v1/v2, TUIC v5,
SOCKS5, and HTTP. They preserve credentials, supported TLS/REALITY settings, and
supported transports. Conversion retains the original protocol.

| Area | Current behavior |
| --- | --- |
| Native protocols | Additional protocols can remain in their original client format when its installed core accepts them. Publication still requires an applicable passing connection check. |
| Hysteria v1 and TUIC | Supported by the Hiddify/Mihomo adapters; excluded from this Xray adapter. |
| Xray Hysteria2 | Xray `protocol: hysteria` with `version: 2` is classified as Hysteria2. |
| XHTTP | Supported in native Hiddify output; excluded from the Mihomo conversion adapter. |
| Xray fragmentation chains | Retained with namespaced dependencies in Xray output; excluded from conversions without an equivalent representation. |
| Source routing and DNS | Replaced by the destination export's routing model; provider profiles are not reproduced as complete operating-system configurations. |

Every refresh validates generated configurations with the bundled cores. A
rejected node is excluded for that client and reported in compatibility notes.
Passing validation means a configuration can be loaded. Passing a connection
check is the additional requirement for publication.

### Hysteria details

Supported exports preserve authentication, IPv6, TLS server name and verification,
ALPN, supported obfuscation, bandwidth, and port hopping. Hysteria2
`user:password` authentication is retained. `hy2://` input is exported as
`hysteria2://`. Hysteria v1 requires explicit upload/download bandwidth;
Hysteria2 retains its defaults when bandwidth is absent.

Certificate-pinned Hysteria2 nodes are checked with Mihomo and retain
`pinSHA256`. Conversions without an equivalent certificate pin are excluded.
Xray's Hysteria2 `tlsSettings.fingerprint` is a ClientHello setting ignored by
its QUIC transport; it is not a certificate pin. Custom hopping intervals,
dependency chains, binary authentication, and other settings without a share-link
equivalent are excluded from the V2Board URI feed with a compatibility note.
The JSON feed can retain these settings in accepted native configurations.

## Connection checks and publication

Connection checks run in a shared scheduled batch every **10 minutes** by default,
using cached configurations. Change the interval under **Settings → Connection checks**.
Individual result timestamps do not start separate scheduled jobs. The next batch
survives application restarts; provider edits and checks for newly added servers
do not postpone it. Results checked in the last minute are reused at a batch
boundary to avoid repeating a just-completed check. Unfinished work resumes
after a short backoff, keeping the original expiry of any retained passing result.
**Check servers** explicitly rechecks all servers without fetching providers or
sending HWIDs. **Refresh all** downloads all enabled providers and explicitly
rechecks them. Both start a new batch interval; a single-provider refresh keeps
the existing global schedules. Automatic downloads between scheduled batches
reuse recent results and check new, changed, or due configurations.
Saving a tested provider or changing a subscription selection schedules checks
only for configurations with missing, changed, or due results. Recent successful
and failed results keep their original timestamps; edits never extend their
expiry. Renaming, pausing, and deleting providers rebuild the feeds without
fetching or rechecking unchanged servers.

The Providers page shows separate **Providers downloaded** and **Connections
checked** timestamps. Connection checks and feed rebuilds never advance the
download timestamp. Dashboard status polling (15 seconds idle, 1.5 seconds while
working) only reads state; it does not trigger downloads or connection checks.

For each compatible configuration, an isolated Mihomo, Xray or Hiddify process sends an
HTTPS request through the node. The request must pass TLS verification and return
HTTP 204. The first probe is `https://www.gstatic.com/generate_204`; failures are
retried against `https://cp.cloudflare.com/generate_204`. There is no direct
connection fallback. Temporary listeners bind to loopback and are removed with
the test processes and configurations after use.

| Output | Publication requirement |
| --- | --- |
| Mihomo | Recent passing Mihomo check for the exact configuration and dependencies. |
| Xray | Recent passing Xray check for the exact configuration and dependencies. |
| Hiddify | Recent passing Hiddify core check for the exact configuration and dependencies. |
| V2Board URI | Portable conversion of a node with a recent passing core check. |
| V2Board JSON | Each included native variant requires its own core's passing check. |

The checks exercise client cores, not every desktop or mobile app's importer.
URI clients can interpret optional settings differently, and newer app versions
can bundle different cores. Import the native JSON or YAML format when available.
The dashboard shows a separate result for each core. A passing Xray result does
not make a failing Hiddify variant eligible for publication.

Results expire after **twice the configured check interval**. Changes to a
configuration or its dependencies invalidate its previous result. A feed returns
HTTP 503 when publication is pending or stale; client-specific feeds also return
503 when they have no passing compatible nodes. Failed nodes remain in the
inventory and can return to feeds after a later successful check.

### Understand counts and resource use

**Server library** counts unique proxy entries from enabled providers. Deduplication
includes credentials and chain dependencies, so different accounts on the same
server remain distinct. Internal routing helpers, such as Xray loopbacks, do not
count as proxy servers.

**Included** counts the union of nodes published in at least one client format.
Per-client counts may differ because of protocol, transport, or check results;
adding those counts together double-counts nodes. Subscription detail counts
apply to the selected providers. Overview and Server library describe the whole
workspace.

The checker runs up to **four concurrent checks**, with a **five-second core
startup limit**, **eight seconds per HTTPS attempt**, and a **five-minute batch
limit**. Exact configurations are checked once per run; distinct native variants
may require separate checks. Untested nodes are excluded and prioritized on the
next run. Resource use depends on the node list and cores; increasing the check
interval reduces how often this work occurs.

Results measure connectivity from the Docker server at the recorded time.
Reported latency is an HTTPS request measurement, not an ICMP ping or speed test.
It does not establish UDP support or guarantee connectivity from another network.

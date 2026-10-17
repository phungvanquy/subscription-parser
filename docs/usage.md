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
**200 providers**. Provider names must be unique.
Bulk entry accepts up to 50 providers at a time; repeat it to add larger libraries.

Changing a provider's URL, device assignment, or user agent in the dashboard
requires another fetch test. Name-only and server-limit edits do not. Fetch tests validate
retrieval and conversion; the separate connection checks test remote servers.

The **Providers** page shows compact rows with working servers, connection status,
and the last fetch. Search by provider name, host, or device, filter by status,
and use the page controls to show 25, 50, or 100 providers at a time.
Expand the arrow beside a provider for sampling details, device, fetch errors,
and compatible format counts; the **⋯** menu contains
refresh, edit, pause/enable, and remove actions. Working means included in at least
one format with current passing connection checks; pending, failed, and expired
checks do not count. A shared server counts once for each provider that supplies it.
Select the working count to open **Server library**, filtered to that provider's
passing servers. Use **All providers** and **All results** to clear those filters.

### Limit servers per provider

In **Add provider** or **Edit provider**, set **Maximum servers to test** to a
number such as **20**, then save. Leave it blank for unlimited, which is the
default for existing providers. During bulk entry, the number applies to each
provider separately.

If a provider returns 200 distinct servers and the limit is 20, each successful
fetch randomly selects 20 without replacement from the full list. It does not
take the first 20, and duplicate rows do not get extra chances. Only selected
servers enter connection checks, Server library, and output subscriptions. Each
compatible client core still has its own check, so 20 servers may require 60
core checks. Only servers that pass are published; failures do not trigger
replacement draws to fill the limit. If fewer than 20 servers are available,
all are selected.

**Check servers**, restarts, and failed provider downloads keep the saved
selection. **Refresh provider**, **Refresh all**, and scheduled successful
downloads make a new random selection, even if the provider returns the same
list. A server can be selected again in a later draw. Editing the limit applies
to the cached list immediately without downloading again; newly selected servers
need current passing checks before publication.

Provider rows show the selected count. Expand a row for the full fetched count
and configured limit.
The saved response retains the full list, so increasing or clearing the limit
does not require another fetch.

### Refresh and cache behavior

Provider refreshes run hourly by default. Change the interval in
**Settings → Refresh schedule**, or use **Refresh all** or a provider's refresh
action for an immediate download.

A provider's **Refresh provider** action downloads and queues checks only for that provider's
servers. Other providers keep their cached responses and check timestamps, and
their scheduled refreshes are not postponed. Identical configurations shared by
multiple providers are checked once; each distinct native configuration keeps
its own result. **Refresh all** downloads all enabled providers and queues their checks independently.

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
sending HWIDs, and starts a new batch interval. **Refresh all** downloads all enabled
providers and queues a separate recheck. Downloading does not postpone scheduled
health checks; a single-provider refresh keeps the existing global schedules.
Automatic downloads between scheduled batches
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

Downloads and checks have independent workers, schedules, retries, and progress.
You can refresh while a check is running or check saved servers during a download.
The dashboard shows both progress bars when they overlap. Download progress counts
providers; check progress counts client configurations, so one server can contribute
more than one. Progress survives navigation and reloads. Configuration edits wait
until running work finishes.

When a successful fetch draws a replacement sample, the previous selection stays
in the feeds while the replacement is checked. Each provider switches atomically
after its replacement has completed a first pass across compatible configurations;
only its currently passing servers are exposed. Partial batches save their progress
and continue after a restart without redrawing the sample. The old selection's
results still expire normally. The two selections are never combined into an
oversized pool. New providers can publish passing results incrementally because
they have no previous selection to preserve. Changing a limit or request credentials
applies immediately, without retaining an incompatible old selection.

The provider row identifies a pending selection and distinguishes active servers
from those selected for the next checks. Activity records downloads and checks
separately, and failures appear in the error notice.

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
configuration or its dependencies invalidate its previous result. Expired nodes
are removed from feeds even during an ongoing refresh or check; other nodes with
current passing results remain available. A client-specific feed returns HTTP 503
when it has no passing compatible nodes. The combined archive can describe an empty
library. Unpublished, incompatible or invalidated snapshots remain unavailable.
Failed nodes remain in the inventory and can return after a later successful check.

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

The checker defaults to **eight concurrent checks**, with a **five-second core
startup limit**, **eight seconds per HTTPS attempt**, and a **one-minute batch
budget**. Concurrency and batch duration are configurable in the deployment
environment. Exact configurations share results across subscriptions; distinct
native variants may require separate checks. Untested nodes are excluded and
prioritized on the next run. Deferred failures retain their age so they cannot
repeatedly jump ahead of untested nodes. Unfinished work resumes automatically,
including after a restart.

Provider downloads default to eight concurrent workers and a 30-second total
deadline per provider. The download and check queues run independently, so slow
checks cannot prevent more providers from downloading, and a slow download cannot
hold up scheduled checks. Unchanged configurations reuse parsing, validation and feed output.
For thousands of servers, adjust the check interval and worker count using the
[capacity guidance](deployment.md#environment-variables); a larger provider
limit alone does not increase checking throughput.

Results measure connectivity from the Docker server at the recorded time.
Reported latency is an HTTPS request measurement, not an ICMP ping or speed test.
It does not establish UDP support or guarantee connectivity from another network.

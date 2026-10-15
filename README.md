# subscription_parser

The web app combines subscriptions from multiple providers and serves stable,
private subscription links for Hiddify, Mihomo and Xray. It preserves protocols;
it does not attempt to turn one proxy protocol into another.

## Web dashboard

Run the dashboard on your own server using Docker Compose.
HWIDs, providers, the subscription token and dashboard settings are records in
`/data/app.sqlite3`, inside the persistent data volume. The checkout's `.env`
contains deployment configuration and the admin login key (`PARSER_ADMIN_TOKEN`)
and is never rewritten by the app.

1. Choose **Add provider**. Paste one or more HTTPS subscription links.
2. Explicitly select a saved device for each link, or **No device ID** when the
   provider does not require one. **Add device** saves an existing authorized ID.
3. Choose **Test fetch**, review the results, then save. Saving reuses the tested
   response without fetching again. Failed tests leave saved providers unchanged.
4. Open **Server library** to see the automatic connection checks, or choose
   **Check servers** to run them immediately using saved subscriptions.
5. Open **Subscription links** and copy the Hiddify JSON or Mihomo YAML link into
   your client. Only recently passing nodes are included; the URLs remain stable.

The server stores each authorized HWID in SQLite and sends that exact value as `x-hwid` on
each fetch. It never generates replacement IDs to retry a device-limit error.
The **Devices** page shows labels, masked IDs and every assigned subscription,
including paused subscriptions. **Saved** means stored locally, not accepted by
a provider. Fetch tests check subscription retrieval and compatible output; they
do not test remote proxy connectivity.

- **Rename** changes only the label; the actual ID and assignments stay fixed.
- **Reveal ID / Copy ID** retrieve the value through an authenticated, uncached
  request. Normal dashboard state never contains the full value. The reveal
  window clears its value when closed, after one minute, or when the tab is hidden.
- **Delete** is available only for unused devices; the server also checks usage.
- **Replace ID** is separate from rename. It lists affected subscriptions and
  requires explicit confirmation. The server rejects a stale device revision or
  changed assignment list. Replacement removes old-device responses from the
  published exports for those subscriptions, without contacting providers. Use
  refresh to fetch with the replacement. Refreshes never generate or rotate IDs.

Adding a provider never preselects a saved ID. Bulk imports provide a separate
device selector for each link. Changing a subscription's URL, device or user
agent in the dashboard requires another fetch test; a name-only edit does not.

A persistent subscription token protects client feeds independently of the admin login.
The **Rotate links** action replaces that token and invalidates old URLs.
`PARSER_PUBLIC_URL` controls the address copied into subscription links; it is
set in the deployment `.env` and passed to the container by Compose. The database
and private backups have restrictive permissions and are excluded from Git.

The web app refreshes hourly by default. Change the interval under **Settings**.
Failed or invalid downloads retain the last good response for the same URL,
user agent and device assignment/revision, marked **Cached**. Changing those
settings cannot reuse an earlier identity's cache. Paused and deleted providers
are removed from published exports.

| Feed | Purpose |
| --- | --- |
| `/sub/<token>/hiddify.json` | Combined native Hiddify/sing-box outbounds. |
| `/sub/<token>/mihomo.yaml` | Mihomo proxies, selection group and routing rule. Also usable as a proxy-provider feed. |
| `/sub/<token>/xray.json` | Xray outbound configuration, including referenced dependencies. |
| `/sub/<token>/v2board.txt` | Plain URI subscription for V2Board External Subscriptions: VLESS, VMess, Trojan, Shadowsocks, Hysteria v1 and Hysteria2. |
| `/sub/<token>/combined.json` | Archive containing all three formats, server inventory and compatibility diagnostics. Not a universal client import format. |

For V2Board, copy **V2Board → Copy link** and paste the HTTPS URL into
**Advanced Settings → External Subscriptions**. Assign the allowed user groups,
save, and choose **Refresh now**. Provider fetching continues to use the fixed
HWIDs saved in the application database.

The V2Board feed excludes unsupported protocols and settings that cannot be
preserved in share links. Its limits match the importer: 500 entries, 1 MiB total
and 16 KiB per entry. Exclusions appear under **Compatibility notes → V2Board**.
V2Board may retain advanced transport links for URI clients while omitting them
from its structured Mihomo/sing-box exports. An empty feed returns HTTP 503.

Hysteria exports preserve authentication (including Hysteria2 `user:password`),
IPv6, TLS server name and verification, ALPN, supported obfuscation, bandwidth
and port hopping. `hy2://` input is accepted and exported as `hysteria2://`.
Hysteria v1 needs explicit upload/download bandwidth; Hysteria2 keeps its normal
defaults when bandwidth is absent. Certificate-pinned Hysteria2 nodes are tested
with Mihomo and retain `pinSHA256`; they are excluded from conversions without
an equivalent certificate pin. Custom hopping intervals, dependency chains,
binary authentication and other settings with no share-link equivalent remain
excluded with a compatibility note. Only recently passing nodes are published.
Xray's Hysteria2 `tlsSettings.fingerprint` is a ClientHello setting ignored by
its QUIC transport; it is not converted into a certificate pin. Certificate
verification fields remain subject to strict conversion checks.
TUIC and AnyTLS remain excluded from this feed because V2Board's **External
Subscriptions** importer does not accept them, even though its managed nodes do.

## Formats and protocol support

Inputs: Xray JSON profiles, native sing-box JSON, Mihomo/Clash YAML or JSON, and
plain/base64 share-link subscriptions. Common cross-client adapters cover VLESS,
VMess, Shadowsocks, Trojan, Hysteria v1/v2, TUIC v5, SOCKS5 and HTTP. The protocol,
credentials, TLS/REALITY settings and supported transport settings are preserved.

Hysteria v1 and TUIC are available for Hiddify/Mihomo, not this Xray adapter.
Xray's `protocol: hysteria` with `version: 2` is correctly classified as Hysteria2.
Other protocols can pass through in their original client's native format when
that installed core accepts them. This is not a promise that every advanced
setting or every future protocol can translate between different engines.

Unsupported settings are listed per client rather than silently removed. Xray
fragmentation chains remain in Xray exports with namespaced dependency tags;
they are excluded from clients where no equivalent conversion exists. XHTTP is
supported in the native Hiddify export but currently excluded from Mihomo by
this adapter. Source-specific DNS and routing rules are replaced by the
destination client's own rules; the Xray export contains outbounds, not a
complete operating-system proxy setup.

Every refresh validates generated configurations with the installed client cores.
If a core rejects a node, the service isolates and excludes it for that client
and records a diagnostic. This checks importability, not whether the remote
server currently accepts connections. If validators are absent on another
installation, the API reports `not_installed`; install them before relying on
native passthrough validation.

## Connection checks and filtering

After fetching and validating subscriptions, the app starts an isolated Mihomo
or Xray client for each compatible node. A small HTTPS request must travel
through that node, pass TLS certificate verification and return HTTP 204.
The first endpoint is `https://www.gstatic.com/generate_204`; a failed request
is retried against `https://cp.cloudflare.com/generate_204`. There is no direct
connection fallback. The client listeners bind only to loopback, and temporary
configs and core processes are removed after each check.

**Server library** shows separate Mihomo and Xray results, response time, last
check time and the feeds containing each node. Failed, pending, expired,
unsupported and checker-error states remain visible. Failed nodes stay in the
provider cache and are tested again; they are not deleted.

- Mihomo includes only nodes that recently passed through Mihomo.
- Xray includes only nodes that recently passed through Xray, preserving required
  outbound dependencies.
- Hiddify and V2Board include compatible nodes that passed at least one of those
  cores. This is a connectivity signal, not a live test inside Hiddify or V2Board;
  client-specific behavior may differ. Unsupported conversions remain excluded.
- The combined archive uses schema version 2: `servers` and native configurations
  contain passing nodes; `excluded_servers` contains diagnostic metadata only.

Checks run after every provider refresh and, by default, every **10 minutes**
from cached data. **Settings → Connection checks** controls that schedule.
**Check servers** and scheduled rechecks never download provider subscriptions
or send HWIDs. The check interval and results live in the data volume; `.env`
remains deployment configuration.

Results expire after twice the check interval. Changed connection settings or
credentials cannot reuse earlier results. If checks stop completing, stale
feeds return HTTP 503; no direct fallback or untested nodes are substituted.
A feed with no passing nodes also returns HTTP 503. Existing client downloads
remain in the client until it successfully updates; the app cannot revoke a
client's previously downloaded configuration.

Four checks run concurrently. Each core has a five-second startup limit, each
HTTPS attempt has an eight-second total limit, and a batch is bounded to five
minutes. Nodes left untested by that limit are excluded and prioritized on the
next run. Core startup problems are reported as check errors, not proof that
the remote server is offline.

Results describe connectivity **from this Docker server at the recorded time**.
Response time includes proxy setup, TLS and the HTTP request. It is not a speed
test, does not test all destinations or UDP traffic, and cannot guarantee the
same result from another network.

Authenticated API controls are `POST /api/check-servers` and
`PUT /api/settings` with `check_seconds` (300–86400). `/api/state` includes
per-node `connectivity`, `published_total`, `last_check`, `next_check`,
`check_seconds` and progress while a batch runs. Subscription URLs and tokens
do not change when nodes are filtered or recover.

Original provider responses, refresh history, connection results and published snapshots live
under `/data/.parser-state/`. Provider records and refresh settings live in SQLite. Publication uses an atomic snapshot, so client downloads
cannot see partially updated exports. Deduplication includes credentials and
chain dependencies; two different accounts on one server remain distinct.

## Docker deployment

Use Docker Compose 2.24.4+ on Linux arm64 or amd64. The image includes Python
and pinned, checksum-verified Mihomo, Hiddify and Xray validators. It runs as
UID/GID 10001 with a read-only application filesystem and a writable data volume.

The admin **Activity & logs** page shows the latest 200 debug events with a
severity filter and manual refresh. It records failed provider requests, cached
fallbacks, validation problems, connection failures/recoveries and job summaries.
Repeated unchanged node failures are summarized instead of logged individually.
Logs persist in `/data/logs`, outside `.env` and the application database. Each
file is capped at 1 MiB, with three rotated backups: at most **4 MiB total**.
Entries older than **seven days** are hidden immediately and deleted on startup
and hourly, including when there are no new events. Busy systems may retain less
history because the size limit takes priority. No retention configuration is
needed. Container stdout/stderr separately retains its existing Docker limit of
three 10 MB files.

Only admins can read debug logs. Messages use fixed templates and internal IDs;
URLs, HWIDs, credentials, provider payloads and raw core output are excluded.
Unexpected exceptions include their type and source locations, without exception
text, source lines or local variables. Logging I/O failures do not interrupt
subscription jobs; the log viewer reports when storage is unavailable. CI build
failures remain in GitHub Actions, and startup failures remain in Docker logs.

Core versions, release filenames and official SHA256 digests for both architectures
are pinned in `scripts/client-cores.lock.json`. Builds download those release files
directly and verify their hashes before installing them; they do not query GitHub's
REST API or need a GitHub token. Temporary download failures have bounded retries.
When upgrading a core, update its version, filenames and digests together using
the official release metadata, then verify builds for both architectures.

For a **new installation**:

```sh
git clone git@github.com:phungvanquy/subscription-parser.git
cd subscription-parser
cp .env.example .env
chmod 600 .env
# Set PARSER_ADMIN_TOKEN, PARSER_PUBLIC_URL and the port/proxy settings.
# Generate a random admin key with: openssl rand -hex 32
docker compose up -d --build --wait
```

Open `http://127.0.0.1:8099` on the Docker host, or use SSH forwarding for a
remote server. See the reverse proxy settings below for HTTPS access.

Set a long random `PARSER_ADMIN_TOKEN` in `.env`; startup refuses a missing or
empty key. The first startup creates a database and a persistent subscription
token. Sign in with your configured admin key and add existing authorized HWIDs.
To read the key currently loaded by the running container locally:

```sh
docker compose exec app python -m subscription_parser.manage admin-key
```

No HWID is generated automatically. Give each saved identity a descriptive label;
providers reference its database ID. The management API returns the ID from
`POST /api/hwids`; provider requests use `hwid_id`. On create this field must be
explicit: a saved record ID, or JSON `null` to omit `x-hwid`. Omitting it on an
update preserves the assignment. Blank strings are rejected.

Authenticated device actions are `PATCH /api/hwids/{id}` with `name`,
`POST /api/hwids/{id}/reveal`, `DELETE /api/hwids/{id}`, and
`POST /api/hwids/{id}/replace` with `value`, current `revision`, the complete
`provider_ids` list and `confirmed: true`. Names and full values must be unique
when adding devices; legacy duplicate values are preserved during migration.

`POST /api/providers/test` accepts provider fields plus optional `provider_id`
for an edit, returning counts and a temporary `test_token`. Pass that token when
saving through `POST /api/providers` or `PUT /api/providers/{id}` to reuse the
response. Tokens expire after ten minutes, are single-use and bind to the tested
settings and device revision. Failed/expired tests cannot silently save different
settings. Existing direct API writes without a test token remain supported;
the dashboard requires testing when fetch settings change.

### Configuration and data

| Location | Contents |
| --- | --- |
| Checkout `.env` | Admin key, public URL, published port, bind address, trusted proxies and image selection. |
| Volume `/data/app.sqlite3` | HWIDs, providers, subscription token and refresh interval. |
| Volume `/data/.parser-state/` | Provider response cache, published snapshot, refresh history and process lock. |
| Volume `/data/legacy-backup/` | Private copies of legacy files, created only when migrating an older installation. |

The production volume is named `subscription-parser_data`. Preserve it when
rebuilding or replacing containers. The app reads deployment settings from its
process environment; dashboard changes write to SQLite. Environment variables
cannot override database HWIDs or the subscription token. The admin key is read
only from `PARSER_ADMIN_TOKEN` at startup and is not stored in the database.

Supported deployment settings:

| Setting | Default / purpose |
| --- | --- |
| `PARSER_ADMIN_TOKEN` | Required admin login key; no generated default or database fallback. |
| `PARSER_PUBLIC_URL` | Public base URL for client links; falls back to the request address when empty. |
| `PARSER_PORT` | Published host port, default `8099`. |
| `PARSER_BIND_ADDRESS` | Host interface, default `127.0.0.1`. |
| `PARSER_TRUSTED_PROXIES` | Proxy addresses allowed to supply forwarded headers; defaults to loopback and `172.16.0.0/12`. |
| `PARSER_IMAGE` | Image tag, default `subscription-parser:local`. |

To change the admin key or public URL, edit the checkout's `.env` and recreate
the container:

```sh
docker compose up -d --wait
```

A plain container restart does not apply changed Compose environment settings.
Changing the admin key invalidates existing dashboard sessions; subscription
links and HWIDs stay unchanged. Keeping the same key preserves existing sessions.
Compose sets the internal `PARSER_DATA_DIR=/data` and
`PARSER_TOOLS_DIR=/opt/client-cores` paths.

### Reverse proxy and operations

By default, Compose publishes **host port 8099 → container port 8099** on
loopback. For Nginx Proxy Manager or another reverse proxy on a different host,
set `PARSER_BIND_ADDRESS=0.0.0.0`, point the proxy at the Docker host's port 8099,
and restrict that port to your proxy using the host/network firewall.

Set `PARSER_PUBLIC_URL` to your HTTPS dashboard address and include the proxy's
actual source IP in `PARSER_TRUSTED_PROXIES`. Forward `Host` and
`X-Forwarded-Proto` so HTTPS login and secure session cookies work. For example,
using a documentation-only proxy address:

```dotenv
PARSER_PUBLIC_URL=https://subscriptions.example.com
PARSER_PORT=8099
PARSER_BIND_ADDRESS=0.0.0.0
PARSER_TRUSTED_PROXIES=127.0.0.1,172.16.0.0/12,203.0.113.10
```

Replace the example domain and proxy address with your own. The admin key must
also remain configured in `.env`.

Docker's `unless-stopped` restart policy and the enabled Docker daemon handle
startup after a reboot. The app owns its refresh scheduler. Run one app replica
per volume; a process lock prevents competing publishers. `/healthz` reports
process health. No separate systemd service is needed for the app.

```sh
docker compose ps
docker compose logs -f app
docker compose exec app python -m subscription_parser.manage check
docker compose up -d --build --wait
docker compose restart app
```

### Migrating legacy application data

For an installation that still stores the admin key in SQLite, read the existing
key with the **old running image** before upgrading:

```sh
docker compose exec app python -m subscription_parser.manage admin-key
```

Save that exact value as `PARSER_ADMIN_TOKEN` in the checkout's `.env`. Back up
the data volume and stop the old app before deploying the new image. Startup
transactionally upgrades the database to version 2 and removes its admin key;
subscription tokens, records, caches and existing sessions are preserved. The
upgrade refuses to proceed without a configured admin key or while the old app
holds its process lock.

Device management upgrades version 2 databases to version 3 transactionally.
It preserves every existing ID, value, provider assignment and subscription
token, adds device revisions, and allows explicit providers without an HWID.
Legacy cached responses are bound to their original saved request settings on
startup; their response bodies remain unchanged. Stop and back up the app before
upgrading.

Older versions stored HWIDs and tokens in `/data/.env` and providers in
`/data/.parser-state/providers.json`. Copy their existing `PARSER_ADMIN_TOKEN`
and `PARSER_PUBLIC_URL` into the deployment `.env` before upgrading. Stop the old
container and back up its volume before
starting the new image. The new app imports legacy records on first startup.

Migration preserves existing subscription tokens, HWID values, provider
IDs, provider order, device associations and refresh settings. Cached responses
and published exports remain available. The database is published atomically;
invalid records, missing credentials or an active old process prevent migration.
The originals move to private `legacy-backup/environment.txt` and
`legacy-backup/providers.json` files after the database is committed. An
interrupted migration resumes without reimporting records or rotating keys.

A completed database is authoritative. Legacy files and process environment
values cannot replace its saved identities. To perform migration explicitly
with the app stopped:

```sh
docker compose run --rm --no-deps app python -m subscription_parser.manage migrate
```

Keep private migration backups outside the repository, or under the ignored
`backups/` directory. They are excluded from the Docker build context as well.

### Backup and restore

For a consistent backup of the database and caches, stop the app briefly:

```sh
backup_dir="backups/docker-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$backup_dir"
chmod 700 backups "$backup_dir"
docker compose stop app
docker compose cp app:/data/. "$backup_dir/"
docker compose up -d --wait
```

Keep a copy of the deployment `.env` alongside your backups. Restore application
data into an **empty volume** using:

```sh
tar -C "$backup_dir" -czf - . | docker compose run --rm -T --no-deps app python scripts/import_state.py -
docker compose up -d --wait
```

The importer supports SQLite and legacy backups, validates the database and
refuses to overwrite existing application data. It rejects unfinished SQLite
journals and unsafe archive paths. Normal `docker compose down` retains the
volume; adding `--volumes` deletes it.

### Developing with Docker

Development uses a separate project/volume and loopback port 8100. It starts
with an empty provider list. Its public URL defaults to the request address;
`PARSER_DEV_PUBLIC_URL` can override it without pointing at production.

```sh
docker compose -f compose.yaml -f compose.dev.yaml up --build --watch
```

Open `http://127.0.0.1:8100`, using SSH forwarding when developing remotely.
Development uses the admin key configured in the checkout's `.env`. Read the
loaded key and run tests with:

```sh
docker compose -f compose.yaml -f compose.dev.yaml exec app python -m subscription_parser.manage admin-key
docker compose -f compose.yaml -f compose.dev.yaml exec app python -m unittest discover -s tests -v
```

Source changes synchronize and restart the development container. Test changes
synchronize without a restart; dependency changes rebuild the image. This uses
[Compose Watch](https://docs.docker.com/compose/how-tos/file-watch/).

After building the production image, run its isolated deployment checks:

```sh
python3 scripts/docker_smoke.py
```

GitHub Actions runs the test suite and Docker deployment checks on pushes and
pull requests. CI uses synthetic data and requires no production `.env`, HWIDs
or repository secrets. To run tests without starting the development dashboard:

```sh
docker build --target development -t subscription-parser:test .
docker run --rm --read-only --tmpfs /tmp:mode=1777 subscription-parser:test python -m unittest discover -s tests -v
```

The check uses synthetic credentials and cached provider data in a temporary
Compose project. It verifies migration, fixed identities, all five feeds, native
client validators, HTTPS proxy headers, token rotation and container replacement.
It also verifies changing the admin key through deployment configuration.
Actual Mihomo and Xray connection tests use a local VLESS server and a local
HTTPS origin with a temporary test certificate. They cover successful traffic,
dead ports, invalid credentials, TLS rejection and unexpected HTTP responses
without relying on public test sites or contacting real providers.
It removes only its own containers and volume.

Optional browser checks use an isolated development container with Chromium:

```sh
docker build --target development -t subscription-parser:dev .
docker run --rm --user 0 subscription-parser:dev sh -c 'python -m playwright install --with-deps chromium && python scripts/browser_smoke.py'
```

The browser script exercises login, HWID labels, provider forms, filtering,
export links, scheduling and mobile navigation against synthetic data. It never
changes live providers. `PARSER_TEST_BROWSER` can select an existing Chromium
executable in the test environment.

The bundled validators are Mihomo 1.19.32, Hiddify core 5.0.0 and Xray 26.3.27.
Adapters follow the official [Mihomo protocol schemas](https://wiki.metacubex.one/en/config/proxies/),
[sing-box outbound schemas](https://sing-box.sagernet.org/configuration/outbound/),
[Xray Hysteria2 format](https://xtls.github.io/en/config/outbounds/hysteria.html),
[Xray's pinned Hysteria transport implementation](https://github.com/XTLS/Xray-core/blob/v26.3.27/transport/internet/hysteria/dialer.go),
[Hysteria v1 share links](https://v1.hysteria.network/docs/uri-scheme/),
[Hysteria2 share links](https://v2.hysteria.network/docs/developers/URI-Scheme/)
and [Hiddify's import parser](https://github.com/hiddify/hiddify-core/blob/v5.0.0/v2/config/parser.go).

## Project layout

- `subscription_parser/`: API, dashboard, SQLite storage, migration, provider fetching and protocol conversion.
- `tests/`: synthetic protocol, storage/migration, fetch and API tests.
- `Dockerfile`, `compose.yaml`, `compose.dev.yaml`: production and development containers.
- `.github/workflows/ci.yml`: tests and isolated Docker deployment checks.
- `scripts/`: client installer, backup importer, browser and Docker checks.
- `.env`, `.env.example`: deployment settings and their template.
- `backups/`: private migration and application backups.

Application data belongs in the `subscription-parser_data` volume. The checkout
and Docker image contain application code and deployment configuration.

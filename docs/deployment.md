# Deployment

[Documentation home](../README.md#documentation)

Subscription Parser runs as a single Docker Compose service. The image includes
Python and the client cores used for validation and connection checks.

## Requirements

| Requirement | Supported configuration |
| --- | --- |
| Host | Linux `amd64` or `arm64`. |
| Container runtime | Docker Engine with the Docker Compose plugin. |
| Compose version | 2.24.4 or later; the development override uses `!override`. |
| Storage | A persistent Docker volume for application data. |
| Network | HTTPS access to provider URLs and probe endpoints; outbound access to the proxy servers being checked. |
| Public access | An HTTPS reverse proxy with a hostname and trusted forwarding headers. |

Check your Compose version with `docker compose version`. The minimum version
covers the [override syntax](https://docs.docker.com/reference/compose-file/merge/#replace-value)
used by the supplied development configuration.

## Install

From a new checkout:

```sh
git clone https://github.com/phungvanquy/subscription-parser.git
cd subscription-parser
cp .env.example .env
chmod 600 .env
openssl rand -hex 32
```

Set `PARSER_ADMIN_TOKEN` in `.env` to the generated value. Startup rejects a
missing or blank key. Configure the public URL and proxy settings below if the
dashboard will be accessed remotely, then build and start it:

```sh
docker compose up -d --build --wait
docker compose ps
```

For a local installation, open `http://127.0.0.1:8099` on the host. For temporary
remote access to a loopback-bound installation, use an SSH tunnel:

```sh
ssh -L 8099:127.0.0.1:8099 user@server.example.com
```

Open that same loopback URL on your computer. If `PARSER_PUBLIC_URL` is already
an HTTPS address, use that address for login: session cookies are marked Secure.

After signing in, [enroll an authenticator](security.md#enable-two-factor-authentication)
and [add a provider](usage.md#add-a-provider).

## Environment variables

Compose reads the checkout's `.env` and passes deployment settings to the app.
The app never rewrites this file.

| Variable | Default | Purpose |
| --- | --- | --- |
| `PARSER_ADMIN_TOKEN` | Required | Admin login key. Use a long, randomly generated value. |
| `PARSER_PUBLIC_URL` | Empty | Public base URL for generated links. An empty value uses the request address. |
| `PARSER_PORT` | `8099` | Published host port; the container listens on `8099`. |
| `PARSER_BIND_ADDRESS` | `127.0.0.1` | Host interface on which Docker publishes the port. |
| `PARSER_TRUSTED_PROXIES` | `127.0.0.1,172.16.0.0/12` | Proxy IPs or networks allowed to supply forwarded client addresses and schemes. |
| `PARSER_IMAGE` | `subscription-parser:local` | Image reference used by Compose. |
| `PARSER_FETCH_WORKERS` | `8` | Concurrent provider downloads, from 1 to 32. |
| `PARSER_FETCH_TIMEOUT_SECONDS` | `30` | Total deadline per provider download, including DNS, redirects, retries and response bodies; from 1 to 120 seconds. |
| `PARSER_CHECK_WORKERS` | `8` | Concurrent isolated client-core checks, from 1 to 128. |
| `PARSER_CHECK_BATCH_SECONDS` | `60` | Check-batch budget before saving results and publishing; from 1 to 300 seconds. Running cores may need a few extra seconds to terminate. |
| `PARSER_MIHOMO_REALITY_COMPATIBILITY` | `false` | Use Chrome fingerprint and X25519-MLKEM768 in generated Mihomo VLESS REALITY configurations. Native Mihomo inputs and other client outputs retain their settings. Accepts `true`/`false` or `1`/`0`. |

Compose sets `PARSER_DATA_DIR=/data` and
`PARSER_TOOLS_DIR=/opt/client-cores` inside the container. HWIDs, provider URLs,
subscription tokens, and two-factor enrollment are managed as application data.

After changing `.env`, recreate the service to apply the values:

```sh
docker compose up -d --wait
```

A plain `docker compose restart` keeps the container's existing environment.
Changing the Mihomo compatibility option queues checks of cached configurations
after restart. Newly generated variants require their own passing checks before
publication; source identities, provider samples, and saved server selections stay
stable. Disable the option and recreate the service to restore source fingerprints.
Changing the admin key revokes existing sessions while retaining the enrolled
authenticator and saved subscriptions.

For large libraries, size concurrency against CPU, memory and network capacity.
Each active connection check starts a client core and a short-lived HTTPS helper.
An approximate full sweep takes `compatible checks × average check duration ÷ workers`,
plus preparation, publication and continuation gaps. Include core startup and failed
requests when measuring duration. For example, 10,000 servers supporting all three
cores require 30,000 checks; at two seconds each, 32 workers need about 31 minutes
before overhead. Set the dashboard's check interval to allow a complete sweep, or
increase concurrency after measuring resource use. Provider count alone does not
determine check capacity.

Downloads and cached connection checks can progress concurrently during a global
refresh. Check batches save pending work for continuation, and accepted jobs resume
after a restart. Expired nodes are removed from feeds independently of running
jobs; servers with current passing checks remain available. The 200-provider limit
does not guarantee a particular number of fresh servers on every host.

## HTTPS and reverse proxies

Configure the reverse proxy to terminate HTTPS and forward to the Docker host's
published port **8099**. Nginx Proxy Manager can use the following settings:

| Proxy setting | Value |
| --- | --- |
| Public domain | Your dashboard hostname. |
| Upstream scheme | `http`. |
| Upstream host | The Docker host address reachable from the proxy. |
| Upstream port | `8099`, or your configured `PARSER_PORT`. |
| TLS | A valid certificate for the public hostname; enable HTTPS. |

If the proxy runs in another container or on another host, it must be able to
reach the published interface. For example:

```dotenv
PARSER_PUBLIC_URL=https://subscriptions.example.com
PARSER_PORT=8099
PARSER_BIND_ADDRESS=0.0.0.0
PARSER_TRUSTED_PROXIES=127.0.0.1,172.16.0.0/12,203.0.113.10
```

`203.0.113.10` is a documentation address: replace it with your proxy's actual
source IP. Retain the admin key from your `.env`. Restrict network access to the
published port to the intended proxy when binding to `0.0.0.0`.

Forward the original `Host`, `X-Forwarded-Proto`, and `X-Forwarded-For` headers.
Trusted forwarding is used for secure cookies, same-origin checks, and login
rate limits. The default Docker network range is broad; narrow it to your actual
proxy addresses where practical. Do not trust arbitrary internet clients to
supply forwarding headers.

When `PARSER_PUBLIC_URL` uses HTTPS, cookies are Secure even if a direct HTTP
request reaches the app. Use the public HTTPS address to sign in. See
[login troubleshooting](operations.md#troubleshooting) if login appears to
succeed but the dashboard returns to the sign-in screen.

## Persistent storage

The default Compose project is `subscription-parser`, with volume
`subscription-parser_data`. A different Compose project name creates a different
volume.

| Location | Contents |
| --- | --- |
| Checkout `.env` | Deployment configuration and admin key. |
| `/data/app.sqlite3` | Providers, HWIDs, subscriptions, provider selections, schedules, 2FA enrollment, recovery-code hashes, session hashes, and authentication counters. |
| `/data/.parser-state/` | Provider response caches, published snapshots, history, connection results, and the process lock. |
| `/data/logs/` | Bounded private debug history. |
| `/data/legacy-backup/` | Archived inputs from older installations, when migration was needed. |

The database is private application state. Its restrictive file permissions do
not encrypt provider credentials or authenticator secrets at rest. Protect the
volume and its backups as credentials; see [security](security.md#credential-storage).

The production container runs as UID/GID `10001`, with a read-only application
filesystem, writable data volume, temporary `/tmp`, dropped Linux capabilities,
and `no-new-privileges`. Use one app replica per volume. The process lock prevents
multiple publishers from using the same data directory.

Docker's `unless-stopped` restart policy handles application restart. The refresh
scheduler runs inside the application; a separate application systemd service is
not required.

For an existing installation, follow [updates and migrations](operations.md#update)
before replacing the image. Use the [backup procedure](operations.md#backup) to
preserve the volume and `.env` together.

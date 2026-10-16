# Development

[Documentation home](../README.md#documentation)

The backend is Python with FastAPI and SQLite. The dashboard uses HTML, CSS, and
JavaScript without a frontend build step. Docker is the supported development and
deployment environment.

## Local development

Create `.env` from [the template](../.env.example) and set an admin key. Then:

```sh
docker compose -f compose.yaml -f compose.dev.yaml up --build --watch
```

The development override uses project `subscription-parser-dev`, a separate data
volume, and loopback port **8100**. It starts without production providers or
HWIDs. It inherits the configured admin key; authentication enrollment and
sessions remain in its own volume.

| Development setting | Default |
| --- | --- |
| `PARSER_DEV_PORT` | `8100`. |
| `PARSER_DEV_PUBLIC_URL` | Empty; uses the development request address. |

Open `http://127.0.0.1:8100`. Do not set its public URL to the production hostname.
Source edits synchronize and restart the development app. Test edits synchronize
without restart. Dependency, script, and Dockerfile changes rebuild the image.
This behavior is configured through [Compose Watch](https://docs.docker.com/compose/how-tos/file-watch/).

## Run tests

Run the suite inside the development service:

```sh
docker compose -f compose.yaml -f compose.dev.yaml exec app python -m unittest discover -s tests -v
```

For an isolated test container without a dashboard or persistent volume:

```sh
docker build --target development -t subscription-parser:test .
docker run --rm --read-only --tmpfs /tmp:mode=1777 subscription-parser:test python -m unittest discover -s tests -v
```

The suite covers parsing and conversion, protocol compatibility, fetch behavior,
storage migrations, subscription selection, logs, authentication, and connection
checks. Security cases include code replay, session revocation, recovery, proxy
header handling, rate limits, and persistence.

### Production-container checks

Build a dedicated test image and run the Docker smoke script. The script requires
host Python 3 as well as Docker and Compose:

```sh
docker build --target production -t subscription-parser:smoke .
PARSER_IMAGE=subscription-parser:smoke python3 scripts/docker_smoke.py
```

The script creates an isolated Compose project with synthetic credentials and a
temporary volume. It checks migration, HWIDs, all six output formats, proxy
headers, scoped subscriptions, token rotation, authentication, and container
replacement. Real Mihomo/Xray checks run through a local VLESS fixture and HTTPS
origin with a temporary certificate. The script removes its own test resources.

### Browser checks

Run the optional Playwright checks in a disposable container:

```sh
docker build --target development -t subscription-parser:browser-test .
docker run --rm --user 0 subscription-parser:browser-test sh -c 'python -m playwright install --with-deps chromium && python scripts/browser_smoke.py'
```

The browser checks use synthetic providers and cover desktop/mobile navigation,
provider and HWID workflows, named subscriptions, exports, schedules, logs,
authenticator setup, recovery, and login throttling. They do not change the live
deployment. `PARSER_TEST_BROWSER` can select an existing Chromium executable;
`PARSER_TEST_SCREENSHOTS` can point to a writable output directory.

## Continuous integration

[GitHub Actions](https://github.com/phungvanquy/subscription-parser/actions/workflows/ci.yml)
runs on pushes, pull requests, and manual dispatch. The
[workflow](../.github/workflows/ci.yml) uses Ubuntu 24.04 and:

1. Builds the development image.
2. Runs the Python test suite.
3. Builds the production image.
4. Runs isolated Docker deployment checks.

CI uses synthetic data and needs no production `.env`, HWIDs, or repository
secrets. Browser checks are currently a separate, optional local check.

## Project structure

| Path | Responsibility |
| --- | --- |
| `subscription_parser/app.py` | HTTP routes, request boundaries, and authentication integration. |
| `subscription_parser/auth.py` | Sessions, TOTP, recovery codes, and persistent rate limits. |
| `subscription_parser/service.py` | Provider management, scheduling, shared checks, and publication coordination. |
| `subscription_parser/storage.py` | SQLite records and transactional schema migrations. |
| `subscription_parser/fetch.py` | HTTPS retrieval with explicit device assignments. |
| `subscription_parser/protocols.py` | Input parsing and client-format adapters. |
| `subscription_parser/hysteria.py`, `xray_transports.py`, `v2board.py` | Protocol- and integration-specific conversions. |
| `subscription_parser/v2board_native.py` | Bounded, versioned native V2Board JSON export. |
| `subscription_parser/connectivity.py` | Isolated Mihomo/Xray HTTPS checks and freshness policy. |
| `subscription_parser/publication.py` | Scoped output feeds and diagnostic archives. |
| `subscription_parser/debug_log.py` | Private event logging and retention. |
| `subscription_parser/static/` | Dashboard HTML, CSS, and JavaScript. |
| `scripts/` | Core installer, state importer, fixtures, and smoke checks. |
| `tests/` | Automated tests and synthetic inputs. |
| `docs/` | Usage, deployment, security, operations, and API documentation. |

## Bundled client cores

The synthetic `tests/fixtures/v2board-native-v1.json` fixture is shared with the
V2Board importer tests. Parser tests require the generated feed to match it.
Update both repositories when intentionally changing the contract. Regression
cases cover custom headers, heartbeat settings, mux/XUDP, QUIC options,
dependency references, stale checks, and unsafe configuration fields.

The version, release asset, and SHA256 digest for each architecture are pinned in
[`scripts/client-cores.lock.json`](../scripts/client-cores.lock.json).

| Core | Pinned version | Role |
| --- | --- | --- |
| Mihomo | 1.19.32 | Configuration validation and HTTPS connection checks. |
| Hiddify core | 5.0.0 | Native Hiddify configuration validation. |
| Xray | 26.3.27 | Configuration validation and HTTPS connection checks. |

The installer downloads official release assets directly and verifies their
checksums. It does not query the GitHub REST API or require a GitHub token.
Temporary download failures have bounded retries.

To upgrade a core, update its version, asset names, and official digests together.
Validate builds for both supported architectures and run the protocol and
connection tests before publishing the change. Core acceptance and conversion
support are separate: preserve unsupported-setting diagnostics rather than
silently dropping configuration fields.

## Contributing changes

Keep changes scoped and describe the resulting behavior, relevant limitations,
and validation in the pull request. Add regression tests for meaningful parser,
authentication, migration, or publication changes. Use synthetic subscriptions,
credentials, and HWIDs in tests.

Keep `.env`, runtime data, and private backups out of commits. Update the matching
guide when changing API contracts, deployment settings, security behavior, or
client support. Documentation-only changes can be checked for links, examples,
and consistency without restarting the application.

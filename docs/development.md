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

Use the same coverage gate as CI, saving reports outside the container:

```sh
mkdir -p artifacts/unit
docker run --rm --read-only --tmpfs /tmp:mode=1777 \
  --user "$(id -u):$(id -g)" \
  --mount type=bind,src="$PWD/artifacts/unit",dst=/artifacts \
  --env PARSER_TEST_ARTIFACTS=/artifacts \
  subscription-parser:test python scripts/run_tests.py
```

The full-suite runner fails on test failures, skipped tests, or combined statement
and branch coverage below **85%**, configured in [`.coveragerc`](../.coveragerc).
It writes a test summary plus XML, JSON, and HTML coverage reports to
`artifacts/unit/`. Keep this minimum at or above its current value as coverage
improves; do not lower it to accommodate untested changes. The coverage report
measures Python application code; browser checks separately exercise JavaScript.

The suite covers parsing and conversion, protocol compatibility, fetch behavior,
storage migrations, subscription selection, logs, authentication, and connection
checks. Security cases include code replay, session revocation, recovery, proxy
header handling, rate limits, and persistence. Download cases include HTTPS
redirects and downgrade rejection, response size boundaries, bounded retries,
network/TLS failures, timeouts, and private URL redaction. Actual Mihomo, Xray,
and Hiddify processes tunnel HTTPS through local VLESS, VMess, Shadowsocks, and
Trojan fixtures, including rejected credentials, dead servers, invalid TLS,
unexpected HTTP responses, and process cleanup.

Refresh regression cases verify single-provider downloads/checks, preserved
results and schedules for other providers, shared native variants, changed and
failed responses, incremental background checks, and explicit full rechecks.

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
replacement. Real Mihomo/Xray/Hiddify checks run through a local VLESS fixture and HTTPS
origin with a temporary certificate. The script removes its own test resources.

### Browser checks

Run Playwright checks in a disposable container:

```sh
docker build --target browser-tests -t subscription-parser:browser-test .
mkdir -p artifacts/browser/chromium
browser_container=$(docker create --shm-size=1g \
  --env PARSER_TEST_ARTIFACTS=/app/artifacts --env PARSER_TEST_ENGINE=chromium \
  subscription-parser:browser-test python scripts/browser_smoke.py)
trap 'docker rm --force "$browser_container" >/dev/null' EXIT
result=0
docker start --attach "$browser_container" || result=$?
docker cp "$browser_container:/app/artifacts/." artifacts/browser/chromium/
docker rm "$browser_container"
trap - EXIT
test "$result" -eq 0
```

The browser checks use synthetic providers and cover desktop/mobile navigation,
provider and HWID workflows, named subscriptions, exports, schedules, logs,
authenticator setup, recovery, and login throttling. Provider checks include
working/client counts, shared servers, pending/expired/failed checks, action menus,
keyboard focus during polling, and mobile filtering. They do not change the live
deployment.
The suite also holds a single-provider check open to verify that its progress
names the selected provider while other rows remain idle.

Repeat with `PARSER_TEST_ENGINE=firefox` or `webkit` and separate artifact folders.
The image's unprivileged user has the writable home Firefox requires. Results are
copied out of the disposable container without changing host file ownership.
All three engines run the same workflows. Chromium reads back the real clipboard;
Firefox/WebKit capture the copied text after the real clipboard write succeeds,
because they do not support Playwright's clipboard-read permission. A JSON report
and screenshots are saved for each run; failures also save `failure.png` and a
Playwright `trace.zip`. Open a trace with `python -m playwright show-trace PATH`.

`PARSER_TEST_BROWSER` selects an existing executable for the selected engine.
`PARSER_TEST_SCREENSHOTS` overrides the screenshot directory. To install only one
engine in the test image, pass `--build-arg TEST_BROWSERS=chromium` when building.

## Continuous integration

[GitHub Actions](https://github.com/phungvanquy/subscription-parser/actions/workflows/ci.yml)
runs on pushes, pull requests, and manual dispatch. The
[workflow](../.github/workflows/ci.yml) uses Ubuntu 24.04 and:

1. Builds development images on native **amd64 and ARM64** runners.
2. Runs the full Python suite on both architectures, requires zero skips, and
   enforces the coverage minimum.
3. Builds production images and runs isolated Docker deployment checks on both
   architectures.
4. Runs the dashboard suite in **Chromium, Firefox, and WebKit** on amd64.
5. Uploads test logs, coverage reports, screenshots, and failure traces as Actions
   artifacts, retained for 14 days, including when a check fails.

CI uses synthetic data and needs no production `.env`, HWIDs, or repository
secrets. Local proxy fixtures cover representative TCP/TLS configurations, not
every protocol/transport combination or live provider behavior. Mobile checks
exercise responsive layouts in desktop engines, not physical phones.

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

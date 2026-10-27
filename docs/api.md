# API reference

[Documentation home](../README.md#documentation)

The API serves the dashboard and administrative integrations. Paths below are
relative to your deployment's public URL. Requests with a body use JSON and
`Content-Type: application/json`; responses use JSON unless a feed is requested.

The app has one administrator identity. Management endpoints require the
`parser_session` cookie. `Authorization: Bearer <admin-key>` is not accepted.
Interactive API documentation and OpenAPI endpoints are disabled.

## Authenticate

Send `POST /api/login` with the admin key:

```json
{"token": "YOUR_ADMIN_KEY"}
```

If two-factor authentication is enabled, the response is HTTP 200 with:

```json
{"authenticated": false, "requires_two_factor": true}
```

This response does not create a session. Resubmit the key with a fresh
authenticator code or unused recovery code:

```json
{"token": "YOUR_ADMIN_KEY", "code": "123456"}
```

A successful response has `authenticated: true` and a `Set-Cookie` header. Retain
the cookie for subsequent requests, including security actions that replace it.
Credentials in these examples are placeholders.

### Python example

This standard-library example prompts for credentials, keeps the session in
memory, reads only record counts, and signs out when finished. Replace the base
URL with your HTTPS deployment address.

```python
import getpass
import http.cookiejar
import json
import urllib.request

base_url = "https://subscriptions.example.com"
client = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
)


def call(path, body=None):
    request = urllib.request.Request(
        base_url + path,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "subscription-parser-api/1.0"},
    )
    with client.open(request, timeout=30) as response:
        return json.load(response)


key = getpass.getpass("Admin key: ")
session = call("/api/login", {"token": key})
if session.get("requires_two_factor"):
    code = getpass.getpass("Authenticator or recovery code: ")
    session = call("/api/login", {"token": key, "code": code})
if not session.get("authenticated"):
    raise RuntimeError("Authentication did not complete")

try:
    state = call("/api/state")
    print(f"Providers: {len(state['providers'])}")
    print(f"Subscriptions: {len(state['subscriptions'])}")
finally:
    call("/api/logout", {})
```

Authentication limits also apply to API integrations. Retain a session for its
valid lifetime instead of signing in before every request. Handle HTTP 429 using
`Retry-After`; see the [security guide](security.md#authentication-rate-limits).

## Session and security endpoints

| Method | Path | Body or behavior |
| --- | --- | --- |
| GET | `/api/session` | Returns `authenticated`; does not require login. |
| POST | `/api/login` | `token`, optional `code`. Issues a cookie only after complete authentication. |
| POST | `/api/logout` | Revokes the supplied session and clears its cookie. |
| GET | `/api/security` | Returns enrollment status, remaining recovery-code count, and session/rate-limit settings. |
| POST | `/api/security/2fa/setup` | `token`. Requires an authenticated session and disabled 2FA. Returns a temporary `secret`, local QR data URL, and `expires_in`. |
| POST | `/api/security/2fa/confirm` | `code`. Confirms setup from the same session; returns ten recovery codes and replaces the cookie. |
| POST | `/api/security/2fa/recovery` | `token`, `code`. Replaces recovery codes and the session cookie. |
| POST | `/api/security/2fa/disable` | `token`, `code`. Removes enrollment and replaces the session cookie. |

Recovery-code replacement and disabling require both factors. Confirmation and
security changes revoke previous sessions. Setup secrets, QR data, recovery
codes, and session cookies must not be logged. Recovery codes are returned only
when created or replaced; there is no endpoint to reveal them later.

## Dashboard and jobs

| Method | Path | Behavior |
| --- | --- | --- |
| GET | `/healthz` | Public process-health response: `{"status":"ok"}`. `/health` is an alias. |
| GET | `/api/state` | Dashboard inventory, providers, masked devices, subscriptions, counts, schedules, and job progress. |
| GET | `/api/servers/{id}` | Authenticated per-server details and configuration text from the current library selection, including credentials. Returns `fields` and `configurations` (format ID, label, content, language, filename, description). Does not fetch providers or check connections. Missing/removed servers return HTTP 404. |
| GET | `/api/logs?level=all` | Latest 200 matching debug events. `level` is `all`, `warning`, or `error`. |
| POST | `/api/refresh` | Downloads all enabled providers, or only `provider_id`, and queues their selected servers for independent connection checks. Download completion does not mean checks are complete. A selected refresh preserves other providers' results and global schedules. Paused/unknown IDs return HTTP 400; accepted jobs return HTTP 202. |
| POST | `/api/check-servers` | Explicitly rechecks all cached configurations without upstream fetches, including recently checked servers. Returns HTTP 202. |
| PUT | `/api/settings` | Updates `refresh_seconds`, `check_seconds`, or both; each is 300–86400. |

Poll `/api/state` for `refresh_running`, `refresh_provider_id`, `refresh_progress`
(`done`/`total` providers), `check_running`, `check_provider_ids` (null means all),
`check_phase`, and `check_progress` (`done`/`total` client checks). `check_queued`
means saved check intent exists, including a running or retrying batch. A provider's
`selection_pending` indicates its latest selection is awaiting publication.
The existing `busy` field means either job or a configuration operation is active;
`busy_action` can now be `fetching_and_checking`.
`refresh_provider_ids` lists only enabled providers whose downloads are unfinished,
including queued downloads; it is empty when no download is running. Use this list
for row indicators, including during retries of a partially completed full refresh.
`refresh_provider_id` retains the requested scope (null for a full refresh).
`refresh_scheduled` identifies a scheduled download, including its retries; refresh
history events retain the same flag as `scheduled`.

A refresh can start during checks, and checks can start during a refresh.
A second job of the same kind returns HTTP 409. Configuration changes and fetch
previews still wait for running work. A queued request is not a completed check.

For subsequent polls, send the previous `revision` as `/api/state?revision=<revision>`.
When `unchanged` is true, `inventory` and `diagnostics` are omitted; retain those two
fields from the previous response and replace the remaining fields, including job
progress. A different revision returns the complete state. The bundled dashboard
uses this to avoid transferring and rendering the full library on every progress
poll. `last_error` is null when there is no current job error.

Accepted jobs are recorded before HTTP 202 is returned and resume after a restart.
Unfinished connection checks retain their timestamps and are saved between bounded
batches. Progress counts checks processed in the current batch; reaching its time
budget can leave work for an automatic continuation.

`last_download` is the latest successful provider download; `last_check` records
the latest job that actually checked connections. `generated_at` records a feed
rebuild and must not be displayed as a download time. `next_check` is the next
scheduled batch or an earlier continuation for pending work. Regular batches
share one persisted deadline; new-provider checks do not postpone it.

State counts describe the whole workspace. Each item in `subscriptions` contains
its own `total`, `published_total`, `counts`, `ready`, `provider_ids`, and `links`.
The top-level `links` field is a compatibility alias for the original `default`
subscription; that subscription may have its own edited provider selection.

## Providers

| Method | Path | Behavior |
| --- | --- | --- |
| POST | `/api/providers/test` | Fetches and validates a candidate provider's selection. Returns `test_token`, per-client `counts`, `fetched_count`, `selected_count`, and `tested_at`. |
| POST | `/api/providers` | Creates a provider; returns HTTP 201 and its public metadata. |
| PUT | `/api/providers/{id}` | Updates a provider. |
| DELETE | `/api/providers/{id}` | Removes the provider and its subscription assignments. |

Test a new provider with:

```json
{
  "name": "Example provider",
  "url": "https://provider.example/subscription",
  "hwid_id": null,
  "user_agent": "Happ/2.3.1",
  "enabled": true,
  "server_limit": 20
}
```

Use a saved device ID from `/api/state` in `hwid_id`, or explicitly use `null`
to omit the HWID header. On creation the field must be present; a blank string
is invalid. Names are required, unique, and limited to 80 characters. URLs must
be HTTPS without embedded HTTP login credentials. There is a 200-provider limit.

`server_limit` is optional: use an integer from 1 to 100000, or `null` for
unlimited (the default). Each successful fetch randomly samples up to that many
distinct servers before validation, connection checks, and publication. Duplicate
rows do not increase a server's chance of selection. Cached rechecks, failed
downloads, and restarts retain the selection; the next successful fetch draws
again, even when its response body is unchanged. Fewer servers may pass than the
limit; failed selections are not replaced by extra probes.

On `PUT`, omitting `server_limit` preserves it. Changing only the limit applies
immediately to the cached response without another provider request. Provider
state reports `fetched_count` for the full distinct response and `count` for the
selected servers; inventory and all output subscriptions use only the selection.

To save a tested provider, send the same fields to `/api/providers` with the
returned `test_token`. Tokens are single-use, expire after ten minutes, and bind
to the settings and device revision that were tested. They are temporary process
state, so a restart requires another test. Saving reuses the tested response and
the exact random selection. Changing the limit invalidates a preview token.

When testing an edit, include `provider_id`. On `PUT`, include the required
`name`; omit `hwid_id` to retain the assignment and omit `url` or use an empty URL
to retain the saved link. The dashboard requires tests for URL, device, and user
agent changes. Direct API writes without `test_token` are supported but do not
receive the test-before-save guarantee.

## Device identities

| Method | Path | Body or behavior |
| --- | --- | --- |
| POST | `/api/hwids` | `name`, `value`; returns HTTP 201 with the saved ID and metadata. |
| PATCH | `/api/hwids/{id}` | `name`; changes only the label. |
| POST | `/api/hwids/{id}/reveal` | Returns the private `value`. |
| DELETE | `/api/hwids/{id}` | Deletes an unused device; assigned devices are rejected. |
| POST | `/api/hwids/{id}/replace` | `value`, current `revision`, complete `provider_ids`, and `confirmed: true`. |

New labels and HWID values must be unique. Labels accept 1–100 characters;
HWIDs accept 10–64 characters from letters, digits, `=`, and `-`. Replacement
rejects stale revisions or assignments. Read current revisions and provider usage
from `/api/state` before constructing a replacement request.

## Output subscriptions

| Method | Path | Behavior |
| --- | --- | --- |
| GET | `/api/subscriptions` | Lists named subscriptions, provider selections, current counts, and private links. |
| POST | `/api/subscriptions` | Creates a subscription; returns HTTP 201 and `id`. |
| PUT | `/api/subscriptions/{id}` | Replaces its name and selection while retaining its token. |
| DELETE | `/api/subscriptions/{id}` | Deletes it and invalidates its links. The original `default` cannot be deleted. |
| POST | `/api/subscriptions/{id}/rotate-link` | Replaces only this subscription's token. |
| GET | `/api/subscriptions/{id}/servers` | Saved server policy plus paginated working source candidates. |
| PUT | `/api/subscriptions/{id}/servers` | Replaces the saved server policy at its current revision and queues checks. |
| POST | `/api/rotate-link` | Compatibility endpoint: rotates only the original `default` subscription. |

Names must be unique and contain 1–80 characters. Choose exactly one selection
mode, using IDs from `/api/state`:

```json
{"name": "My devices", "all_providers": false, "provider_ids": ["PROVIDER_ID"]}
```

```json
{"name": "All sources", "all_providers": true, "provider_ids": []}
```

A specific selection must contain at least one existing provider and no duplicates.
All-provider mode requires an empty `provider_ids` list. Creating or editing a
subscription rebuilds publications from cached responses without fetching providers.

### Subscription server selections and overrides

`GET /api/subscriptions/{id}/servers` accepts `q` (up to 200 characters),
`offset` (default 0), and `limit` (1–100, default 50). It returns:

- `revision` (0 before the first edit), `all_servers` (default true), and
  `max_records` (1000).
- `records`: all saved source bindings, including unavailable or deselected ones.
  Each has a persistent local `id`, `provider_id`, `provider_name`, full
  `source_identity`, `source` summary (name, protocol, address, port), `selected`,
  and nullable `address`, `port`, and `name` overrides.
- Each saved record also includes `source_present`, `saved_configuration`, `checks`,
  `suggestions`, and `status`: `working`, `unavailable`, or `pending_or_failed`.
  Status describes connection availability, independently of `selected`.
  A saved configuration can be working when `source_present` is false. Credentials
  and the saved configuration payload are not returned by this editor endpoint.
- `available`, `available_total`, `offset`, and `limit`: currently working source
  candidates not already bound in this policy. Candidates use provider IDs and
  full original configuration hashes, not the library's shortened server IDs.

Save the complete record list, using IDs and identities from this endpoint:

```json
{
  "revision": 0,
  "all_servers": false,
  "records": [{
    "provider_id": "PROVIDER_ID",
    "source_identity": "FULL_64_CHARACTER_SOURCE_HASH",
    "selected": true,
    "address": "relay.example.com",
    "port": 8443,
    "name": "My server"
  }]
}
```

The response is `{"revision": 1}`. For existing records, send their local `id`.
Omitted records are forgotten. A deselected record retains its edits; selection
flags are consulted only when `all_servers` is false. `all_servers: false` with
an empty list publishes nothing. Overrides omitted or set to null/blank inherit
saved source values; a port must be an integer from 1 to 65535 or null. Names accept
up to 200 characters without controls; addresses accept a hostname or IP, not a
URL. Unknown fields are rejected.

Changing an existing record's source requires `relink: true` and a currently
working candidate's `provider_id` and `source_identity`. The record's local ID
stays unchanged. The caller must resend the overrides it wants to retain.
Suggestions are never applied automatically. Missing records may still be edited
without relinking. New bindings and relinks must belong to this subscription's
currently selected, enabled providers and their published samples.

Saving during a running job or with an outdated revision returns 409. Policy
updates are atomic, preserve subscription tokens, use cached providers, and
queue checks durably. A configuration change cannot use a previous variant's
passing result. Saved configurations remain eligible outside future provider
samples and need their own current passing client checks. Original-source health
does not block them. Paused/removed providers and subscription deselection still
exclude them. Exact legacy configurations are recovered from full cached responses
when available; this internal upgrade advances the affected policy revision.
Subscription summaries include `server_policy.revision` and
`server_policy.all_servers`; full records are returned only by this endpoint.

## Subscription downloads

`GET /sub/{token}/{filename}` requires the subscription token in the URL and does
not use admin cookies or interactive 2FA. Supported filenames are `hiddify.json`,
`mihomo.yaml`, `xray.json`, `v2board.json`, `v2board.txt`, and `combined.json`.

`counts.v2board` describes the native JSON feed; `counts.v2board_uri` describes
the legacy URI feed. Both appear separately in inventory compatibility and
diagnostics. The [V2Board JSON contract](v2board-json.md) defines its payload.

Responses use JSON, YAML, or plain-text media types as appropriate. They include
`profile-title` and `profile-update-interval` headers and `Cache-Control: no-store`.
See [output formats](usage.md#import-into-clients) for content and compatibility.

## Errors and limits

Errors normally use `{"detail":"description"}`. Treat the status code as the
programmatic signal rather than matching the wording.

| Status | Meaning |
| --- | --- |
| 400 | Invalid settings, selection, device data, or provider request. |
| 401 | Missing/expired session or failed login verification. |
| 403 | Cross-origin change rejected or security re-verification failed. |
| 404 | Unknown route or subscription token/filename. |
| 409 | Running job prevents a change, a server-policy revision is stale, or two-factor setup state conflicts. |
| 413 | Request body exceeds 16 KiB, or 1 MiB for a subscription server-policy PUT. |
| 422 | Request fields fail schema validation. |
| 429 | Authentication rate limit reached; wait for `Retry-After`. |
| 500 | Unexpected internal failure; inspect the private admin logs. |
| 503 | Publication is unavailable, pending, or stale, or the requested client has no passing nodes. |

A combined archive can still be available for diagnostics when an individual
client feed has no passing nodes. Subscription tokens and generated URLs remain
stable as nodes fail and recover.

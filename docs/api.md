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
| GET | `/api/logs?level=all` | Latest 200 matching debug events. `level` is `all`, `warning`, or `error`. |
| POST | `/api/refresh` | Starts a provider refresh; optional `provider_id` query parameter selects one provider. Returns HTTP 202. |
| POST | `/api/check-servers` | Starts checks from cached configurations without upstream fetches. Returns HTTP 202. |
| PUT | `/api/settings` | Updates `refresh_seconds`, `check_seconds`, or both; each is 300–86400. |

Poll `/api/state` for `busy`, `busy_action`, and `check_progress` after starting
a job. A queued request is not a completed check. Changes that conflict with a
running job return HTTP 409.

State counts describe the whole workspace. Each item in `subscriptions` contains
its own `total`, `published_total`, `counts`, `ready`, `provider_ids`, and `links`.
The top-level `links` field is a compatibility alias for the original `default`
subscription; that subscription may have its own edited provider selection.

## Providers

| Method | Path | Behavior |
| --- | --- | --- |
| POST | `/api/providers/test` | Fetches and validates a candidate provider. Returns `test_token`, per-client `counts`, and `tested_at`. |
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
  "enabled": true
}
```

Use a saved device ID from `/api/state` in `hwid_id`, or explicitly use `null`
to omit the HWID header. On creation the field must be present; a blank string
is invalid. Names are required, unique, and limited to 80 characters. URLs must
be HTTPS without embedded HTTP login credentials. There is a 50-provider limit.

To save a tested provider, send the same fields to `/api/providers` with the
returned `test_token`. Tokens are single-use, expire after ten minutes, and bind
to the settings and device revision that were tested. They are temporary process
state, so a restart requires another test. Saving reuses the tested response.

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
| 409 | Running job prevents a change, or two-factor setup state conflicts. |
| 413 | Request body exceeds 16 KiB. |
| 422 | Request fields fail schema validation. |
| 429 | Authentication rate limit reached; wait for `Retry-After`. |
| 500 | Unexpected internal failure; inspect the private admin logs. |
| 503 | Publication is unavailable, pending, or stale, or the requested client has no passing nodes. |

A combined archive can still be available for diagnostics when an individual
client feed has no passing nodes. Subscription tokens and generated URLs remain
stable as nodes fail and recover.

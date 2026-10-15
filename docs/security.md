# Security and authentication

[Documentation home](../README.md#documentation)

Subscription Parser has one administrator account, identified by the admin key
in `.env`. Authenticator-app two-factor authentication is optional until enrolled.
Private subscription URLs use separate tokens and do not require dashboard login.

## Enable two-factor authentication

1. Sign in and open **Settings → Account security → Set up authenticator**.
2. Confirm your admin key.
3. Scan the QR code with a TOTP authenticator, or enter the setup key manually
   using time-based, six-digit codes.
4. Enter a code from your authenticator to confirm enrollment.
5. Save the ten recovery codes in a safe place separate from your admin key.

Two-factor protection becomes active only after confirmation. Setup expires
after ten minutes and is bound to the browser session that started it. Abandoning
setup does not enable an unverified authenticator. QR codes are generated locally;
no third-party QR service receives the secret.

After enrollment, sign-in requires the admin key and either a fresh authenticator
code or an unused recovery code. Authenticator codes use 30-second steps with one
step of clock tolerance. Accepted codes cannot be reused. If you just used a code,
wait for the next one; keep the server and phone clocks synchronized.

## Recovery codes and security changes

Recovery codes are displayed once and stored only as hashes. Each code can be
used once, together with the admin key. A recovery-code login revokes previous
sessions and shows the remaining code count in Account security.

| Action | Required verification | Effect |
| --- | --- | --- |
| Enable 2FA | Admin key, then a code from the new authenticator. | Activates 2FA, creates recovery codes, and revokes previous sessions. |
| Replace recovery codes | Admin key and a fresh authenticator or unused recovery code. | Invalidates older recovery codes and previous sessions. |
| Disable 2FA | Admin key and a fresh authenticator or unused recovery code. | Removes enrollment and recovery codes, and revokes previous sessions. |
| Change the admin key | Edit deployment `.env` and recreate the container. | Invalidates existing sessions while retaining the authenticator. |

Successful security changes issue a replacement session for the current browser.
The admin key alone is not accepted as an API bearer token. API integrations must
[authenticate and retain the session cookie](api.md#authenticate).

### Lost authenticator and recovery codes

If no second factor remains available, the server owner can perform a local
reset. There is no HTTP reset endpoint. From the deployment checkout:

```sh
docker compose stop app
docker compose run --rm --no-deps app python -m subscription_parser.manage reset-two-factor --confirm
docker compose up -d --wait
```

The reset requires the app to be stopped and the explicit `--confirm` flag.
It removes two-factor enrollment, recovery codes, sessions, and authentication
rate-limit counters. The admin key, providers, HWIDs, and subscription links are
retained. Sign in with the configured key and enroll an authenticator again.

## Sessions

Sessions use random tokens, with keyed hashes stored in SQLite. They expire after
eight hours, with at most 20 stored active sessions. Signing out revokes the
current session on the server, including copied versions of that cookie.
Container replacement retains unexpired sessions when the admin key and data
volume are preserved.

Cookies use `HttpOnly` and `SameSite=Strict`. They also use `Secure` when a request
is HTTPS or `PARSER_PUBLIC_URL` is configured as HTTPS. Access an HTTPS deployment
through its public hostname so the browser can send the cookie. Requests that
change data reject a supplied cross-origin `Origin` header.

Upgrading from the earlier timestamp-cookie implementation to database schema 6
requires signing in again. Existing subscription tokens are preserved.

## Authentication rate limits

These limits are fixed in the current application:

| Scope | Limit |
| --- | --- |
| Client IP address | 10 authentication requests per 5 minutes. |
| All client addresses combined | 100 authentication requests per 5 minutes. |
| Admin second-factor verification | 10 attempts per 5 minutes after key verification, or within authenticated enrollment. |

The request limits cover `POST /api/login` and the two-factor management
endpoints. Successful and malformed requests count. IPv6 addresses within the
same `/64` share a client bucket. Counters persist across restarts and expire
automatically, with bounded storage.

HTTP 429 includes a `Retry-After` header with the remaining wait in seconds.
Blocked requests do not extend the cooldown. These limits do not apply to
subscription downloads, health checks, or authenticated dashboard reads; they
are authentication controls rather than a general traffic or DDoS limiter.

The application uses the client address supplied by Uvicorn after trusted-proxy
processing. Configure [proxy trust](deployment.md#https-and-reverse-proxies)
accurately; raw forwarded headers are not read directly by the rate limiter.

## Credential storage

| Credential | Storage and handling |
| --- | --- |
| Admin key | Deployment `.env`, passed through the process environment and read once at startup. |
| Provider URLs and HWIDs | Private SQLite records in the data volume. Normal dashboard state masks HWIDs and omits full provider URLs. |
| Subscription tokens | Private SQLite records, exposed to the administrator as client import URLs. |
| Authenticator secret | Private SQLite record; required to verify time-based codes. |
| Recovery codes | Only hashes remain after the one-time display. |
| Sessions | Only keyed hashes of random session tokens are stored. |

File permissions restrict access to the database and backups; they do not provide
encryption at rest. Anyone who obtains the data volume or a backup may obtain
provider credentials and authenticator secrets. The repository's Git and Docker
ignore rules exclude deployment `.env` files, runtime data directories, and
backups. Keep manually saved exports outside tracked source files as well.

Subscription URLs grant access to their output configurations. **Rotate links**
replaces only the selected subscription's token. This invalidates its previous
URLs but cannot revoke already downloaded configurations.

Authentication events use the existing bounded debug log. Keys, codes, seeds,
session cookies, raw client IPs, provider URLs, and HWIDs are not recorded.
Avoid including these values in issue reports or screenshots.

The TOTP implementation follows [PyOTP's security guidance](https://pyauth.github.io/pyotp/)
and [RFC 6238](https://www.rfc-editor.org/rfc/rfc6238.html).

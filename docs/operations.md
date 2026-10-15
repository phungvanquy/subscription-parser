# Operations

[Documentation home](../README.md#documentation)

Run commands from the deployment checkout. The examples use the default Compose
project and service `app`. Keep the data volume and deployment `.env` together
when moving or recovering an installation.

## Routine checks

```sh
docker compose ps
docker compose logs --tail=100 app
docker compose exec app python -m subscription_parser.manage check
```

`manage check` validates SQLite integrity and foreign keys and reports record
counts. The public `/healthz` endpoint reports process health; it does not prove
that provider downloads or proxy connections are succeeding. Use **Server library**
and **Activity & logs** for those checks.

## Update

Take a [backup](#backup), including the current source commit and image ID, before
upgrading an existing installation. Then:

```sh
git pull --ff-only
docker compose build app
docker compose stop app
docker compose up -d --no-build --wait
docker compose exec app python -m subscription_parser.manage check
```

The new image applies supported database migrations at startup. Stopping the old
app releases its process lock before migration. Check the dashboard, connection
results, and a subscription download after startup. Cached connection checks may
need time to complete before feeds are available.

An older image may not understand a newer database schema. Roll back with a
matching image and consistent backup; do not point an older image at migrated
data or overwrite newer records without reviewing what would be lost.

Configuration-only changes use `docker compose up -d --wait` after editing `.env`.
A restart alone does not reload Compose environment values.

## Backup

The following procedure briefly stops the application so SQLite and cached
publications are copied consistently. Start with the service running:

```sh
umask 077
backup_dir="backups/docker-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$backup_dir/data"
chmod 700 backups "$backup_dir" "$backup_dir/data"
cp .env "$backup_dir/deployment.env"
git rev-parse HEAD > "$backup_dir/source-commit.txt"
docker inspect --format '{{.Image}}' "$(docker compose ps -q app)" > "$backup_dir/image-id.txt"
docker compose stop app
docker compose cp app:/data/. "$backup_dir/data/"
docker compose up -d --no-build --wait
```

Verify that the copy completed and that `data/app.sqlite3` exists. Keep the
backup directory private and store a separate copy outside this host. It contains
provider credentials, subscription tokens, and authentication state.

The deployment `.env` is deliberately outside the `data/` directory. It is
configuration for the target deployment, not a legacy application-data `.env`.
For details of the copy operation, see [Docker Compose cp](https://docs.docker.com/reference/cli/docker/compose/cp/).

## Restore

Restore onto a recovery host or a fresh deployment with an **empty data volume**.
Import before starting the app. The importer refuses to overwrite existing
application data; preserve an existing volume and use a separate Compose project
for a recovery drill instead of deleting it.

In the target checkout, select the backup and restore configuration:

```sh
backup_dir=/absolute/path/to/backup
cp "$backup_dir/deployment.env" .env
chmod 600 .env
docker compose build app
```

Review the public URL, bind address, port, image reference, and proxy settings for
the target host. Retain the admin key if you intend to keep the backed-up identity.
Then import only the application-data directory:

```sh
tar -C "$backup_dir/data" -czf - . | docker compose run --rm -T --no-deps app python scripts/import_state.py -
docker compose up -d --no-build --wait
docker compose exec app python -m subscription_parser.manage check
```

The importer restores SQLite, provider caches, published snapshots, and legacy
migration files. It skips the old process-lock file and debug logs; the full copy
in the backup retains logs for inspection. It validates the database, rejects
unfinished SQLite journals and unsafe archive paths, and applies private file
permissions. Archive input is limited to 1,000 members, 12 MiB per selected file,
and 512 MiB of selected data.

Restoring also restores authentication state from the backup time. After recovery,
review provider assignments and subscription links, and replace recovery codes
through **Account security** to invalidate old codes and saved sessions. Restored
providers can resume scheduled fetches when the app starts.

Ordinary `docker compose down` retains the volume. `docker compose down --volumes`
removes it and is not part of this restore procedure.

## Migrations

The current SQLite schema version is **6**. Supported upgrades are applied in
order, with each migration committed transactionally.

| Version introduced | Change |
| --- | --- |
| 2 | Moves the admin key to deployment configuration and removes it from SQLite. |
| 3 | Adds device revisions and explicit providers without an HWID. |
| 4 | Adds the connection-check schedule. |
| 5 | Adds named subscriptions and provider selections, retaining the original token. |
| 6 | Adds authenticator enrollment, recovery-code hashes, session hashes, and persistent authentication counters. |

For versions that kept the admin key in SQLite, obtain the existing key using the
old running version before upgrading, and put it in the checkout's `.env` as
`PARSER_ADMIN_TOKEN`. Preserve the existing public URL there as well.

Older file-based installations used `/data/.env` and
`/data/.parser-state/providers.json`. Migration imports their existing HWIDs,
provider IDs, device assignments, ordering, settings, and subscription token.
After committing the database, the original inputs are archived under
`/data/legacy-backup/`. A completed database is authoritative; later changes to
legacy files or HWID environment variables do not replace its records.

To run migration explicitly, first stop the app:

```sh
docker compose stop app
docker compose run --rm --no-deps app python -m subscription_parser.manage migrate
docker compose up -d --wait
```

The migration refuses an unsupported schema, missing required credentials,
invalid legacy data, or an active app holding the process lock. The schema-6
upgrade signs out older dashboard sessions; sign in again and enroll an
authenticator if one has not been configured.

## Logs and retention

**Activity & logs** shows the latest 200 matching events with a severity filter
and manual refresh. Events cover provider failures, cached responses, validation,
connection changes, job summaries, and authentication. Repeated unchanged node
failures are summarized.

| Log source | Storage | Retention |
| --- | --- | --- |
| Application debug log | `/data/logs/` | One 1 MiB file plus three rotated backups; at most 4 MiB. |
| Debug-event age | Same files | Seven days; expired entries are hidden immediately and removed on startup and hourly. |
| Container stdout/stderr | Docker's `json-file` driver | Three files, each capped at `10m`. |
| Build and CI output | GitHub Actions | Available in the workflow run, not the dashboard. |

The byte limit takes priority, so busy installations may retain less than seven
days. No retention configuration is required. Logging failures do not interrupt
subscription jobs; the dashboard reports when log storage is unavailable.

Debug messages use fixed templates and internal IDs. Unexpected exceptions
include their type and source locations, without exception text or local values.
Credentials, provider payloads, and raw client-core output are excluded.

## Troubleshooting

| Symptom | Check or next step |
| --- | --- |
| Startup rejects `PARSER_ADMIN_TOKEN` | Set a nonblank admin key in deployment `.env`, then recreate the service. |
| Login succeeds but returns to sign-in | Use the configured HTTPS hostname; check forwarded scheme and trusted proxy settings so Secure cookies can be sent. |
| Login returns HTTP 429 | Wait for the `Retry-After` duration. Restarting does not clear persistent counters. |
| Authenticator code is rejected | Check both clocks and use a fresh code. An already accepted code cannot be reused. |
| Authenticator and recovery codes are lost | Follow the local [two-factor reset procedure](security.md#lost-authenticator-and-recovery-codes). |
| Provider reports a device limit | Select an existing authorized HWID or free a device slot with the provider. Refreshes do not invent another ID. |
| Provider is marked Cached | Inspect its fetch error. Cached nodes still require current passing connection checks. |
| Subscription returns HTTP 503 | Check the selected providers, client compatibility, and recent connection results. Run **Check servers** if results are pending or expired. |
| Subscription returns HTTP 404 | Check the token and filename. Rotation or deletion invalidates the previous URLs. |
| Library total exceeds included count | Inspect failed, stale, unsupported, or excluded nodes and per-client compatibility notes. |
| A change returns HTTP 409 | Wait for the current job; for enrollment conflicts, restart the setup flow. |
| Container is healthy but nodes fail | Health checks cover the web process. Inspect outbound network access and the core results in Server library. |
| Migration reports an active app | Stop the service using this data volume before running migration or local authentication reset. |
| Restore reports existing data | Use an empty target volume; keep the original volume for recovery. |

For a bug report, include the source commit, core version, action taken, and a
sanitized error description. Keep subscription URLs, HWIDs, keys, QR codes,
recovery codes, and exported proxy credentials out of public reports.

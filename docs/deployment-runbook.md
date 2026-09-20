# Deployment runbook

Catalog uses one CI-built application image and two independent Docker Compose hosts.
Staging and production never share credentials, databases, volumes, Compose files, or release state.
Both hosts accept only the exact image digest and Catalog revision from the CI handoff.

Use `make help` as the command index.
Use [pre-release-testing.md](pre-release-testing.md) as the shortest complete release checklist.

## Release contract

The deployment unit is:

- an application image in `name@sha256:<64 lowercase hex>` form;
- the full 40-character Catalog Git revision used as the deployment bundle;
- the Citation Git revision recorded in both the Catalog gitlink and image label.

`make candidate` rejects tags, a dirty checkout, revision mismatches, modified release bundles, malformed host identity, and images whose labels do not match the checkout.
It renders and validates a candidate without changing the running release.

CI publishes `release-handoff.json` after a successful `main` build.
That file is the source for `IMAGE` and `BUNDLE_REVISION`.
Production must use the same values that passed staging QA.

## Host layout

`sudo make host-provision` creates these host-local paths:

| Path | Purpose |
| --- | --- |
| `/etc/comses-catalog/host.env` | Fixed `staging` or `prod` identity |
| `/etc/comses-catalog/secrets/` | Django configuration and PostgreSQL password |
| `/var/lib/comses-catalog/state/` | Candidate, active, rollback, and transaction state |
| `/var/lib/comses-catalog/releases/` | Immutable Git release bundles and rendered Compose files |
| `/var/lib/comses-catalog/runtime/docker-compose.yml` | Canonical active Compose file |
| `/var/lib/comses-catalog/shared/` | Application, cron, staging mail, and Nginx files |
| `/var/lib/comses-catalog/receipts/` | Backup and restore receipts |
| `/var/lib/comses-catalog/reports/` | Release reports |
| `/var/backups/comses-catalog/` | On-host PostgreSQL backups |

The Compose project is always `catalog`.
Using the same project name is safe because staging and production are separate VMs.
PostgreSQL, Elasticsearch, and static files use named volumes and are never deleted by release commands.

## Provision a host

Install Git, Python 3.10 or newer, Docker Engine, and Docker Compose v2.
Give the operator Docker access, then log out and back in so group membership is active.

From a clean Catalog checkout:

```sh
sudo make host-provision HOST_ID=staging OPERATOR="$USER"
# Use HOST_ID=prod on the production VM.
```

Provisioning is idempotent and refuses to change an existing host from staging to production or vice versa.
It creates random database and Django secrets, installs `vm.max_map_count=262144`, installs the host backup helper, and enables the nightly backup timer.
Rerun the same provisioning command after a release changes `scripts/catalogctl.py`; `host-check` rejects a stale installed backup helper.

Review the generated configuration:

```sh
$EDITOR /etc/comses-catalog/secrets/config.ini
make host-check
```

Do not change the database password in only one secret file.
`host-check` requires the password in `config.ini` to match `postgres_password`.
Production requires nonempty SMTP host, port, user, and password values.
Staging deliberately uses Django's file email backend and writes messages under `/var/lib/comses-catalog/shared/mail`; it never sends external email.

Nginx binds to `127.0.0.1:80` for a same-host TLS proxy by default.
If ingress reaches the VM over the network, change `CATALOG_HTTP_BIND` in `host.env` to the required bind address and restore file permissions afterward.

## Registry access

The intended registry is `ghcr.io/<owner>/catalog`.
Make the package public after its first publication so deployment hosts need no registry credentials.
If it remains private, run `docker login ghcr.io` with a read-only package token on each host.

The CI SHA tag is only a discovery name.
Every host command uses the digest from the handoff, never the tag.

## First deployment from a dump

Check out the handoff revision and its Citation gitlink:

```sh
git checkout <BUNDLE_REVISION>
bash scripts/checkout-citation.sh <github-owner-containing-the-citation-commit>
git status --short
git -C citation status --short
```

Both status commands must be empty.

Create the candidate, restore the dump, apply current migrations, build derived data, and activate it:

```sh
make candidate IMAGE='<name@sha256:digest>' BUNDLE_REVISION='<40-char-sha>'
make restore DUMP=/absolute/path/catalog.sql.xz CONFIRM=comses_catalog
CONFIRM_SCHEMA_MIGRATION=1 make schema-migrate
make data-rebuild
make deploy
make backup
make release-report
```

Restore accepts `.dump`, `.sql`, and `.sql.xz`.
It verifies an adjacent `<dump>.sha256` file when present, rejects database-level commands in plain SQL, restores into a temporary database, validates data counts and constraints, then atomically swaps database names.
The previous empty database is retained under a timestamped name.
Restore is allowed only before the host has an active release.
An interrupted or failed restore locks the candidate until the operator investigates it and runs the explicit incident retry described below.

`schema-migrate` runs `makemigrations --check --dry-run`, `migrate --plan`, `migrate --noinput`, and `migrate --check` in that order.
It does not deploy the application.

`data-rebuild` creates and validates new Elasticsearch generation indexes, atomically swaps aliases, preserves the previous generation for rollback, and refreshes the visualization cache.
`deploy` performs only non-mutating migration and search guards before starting the candidate.

## Subsequent release

Create a current backup before any schema work:

```sh
make backup
make candidate IMAGE='<name@sha256:digest>' BUNDLE_REVISION='<40-char-sha>'
CONFIRM_SCHEMA_MIGRATION=1 make schema-migrate
make data-rebuild
make deploy
make status
make release-report
```

The backup gate accepts only a verified backup from the current active release that is less than 24 hours old.
There is no automatic database schema rollback, so migrations must remain compatible with the previous application when rollback is expected.

## Staging and production

Run the first-deployment or subsequent-release sequence independently on staging.
Complete browser QA and retain its release report.

On production, check out the same bundle revision and use the same image digest.
Production does not read or copy staging state.
It repeats its own backup, migration, rebuild, deployment, and report steps against its own data.

For a fresh production VM migration:

1. Stop or make the old production application read-only.
2. Create and transfer the final production dump.
3. Run the first-deployment sequence on the new production VM.
4. Verify counts and application behavior before switching ingress.
5. Keep the old VM intact during initial validation.

The old VM is a valid fallback only until the new production database accepts writes.
After that point, returning traffic to the old database would lose or split new data.

## Status and reports

Use only the controller for deployment lifecycle changes:

```sh
make status
make release-report
make logs
make stop
make start
```

The report records image and source revisions, service image digests, PostgreSQL client and server versions, application database counts, validated search counts and aliases, email backend, and latest eligible backup.
It also warns that backups are currently on-host only.

The active Compose file is `/var/lib/comses-catalog/runtime/docker-compose.yml`.
Direct `docker compose` commands are useful for inspection, but they are not a supported deploy or rollback interface.

## Backup schedule

The provisioner enables `comses-catalog-backup.timer`, scheduled daily at 02:15 UTC.
Backups are custom-format PostgreSQL dumps with SHA-256 files and JSON receipts.
The controller validates each dump with the PostgreSQL 18 `pg_restore` from the database container and retains 30 days.

Verify the schedule and run it once manually:

```sh
systemctl list-timers comses-catalog-backup.timer
sudo systemctl start comses-catalog-backup.service
journalctl -u comses-catalog-backup.service --since today
```

These backups are on the same host.
Host or attached-volume loss can therefore destroy both the database and backups; off-host copies are deliberately deferred and remain an operational risk.

The application scheduler is a separate Compose service.
Its daily maintenance and monthly URL validation output is `/var/lib/comses-catalog/shared/logs/cron.log`.

## Rollback and recovery

`make rollback` activates the immediately previous successful release from the same host and restores its recorded search aliases.
It never copies state from the other host and never reverses database migrations.
Rollback fails closed if its Compose file or release bundle has changed.

Deployment and rollback use a durable transaction journal.
If activation fails, the controller reconciles and health-checks the prior runtime before reporting recovery success.
If recovery cannot be verified, the journal remains and normal mutations stop.

Inspect the error, then run:

```sh
make recover
make status
```

A failed or interrupted restore, migration, or data rebuild locks its candidate.
After investigating and repairing the underlying database or search problem, explicitly authorize a retry:

```sh
CONFIRM_CANDIDATE_RETRY=1 make candidate-retry
# Rerun restore, schema-migrate, or data-rebuild as reported.
```

Do not use `candidate-retry` as a substitute for understanding a partly applied migration.

## CI and release tags

Pull requests build and test but do not publish images.
A successful push to `main` publishes `ghcr.io/<owner>/catalog:sha-<commit>` and a handoff artifact containing its exact digest.

Release tags must be annotated, point to `main`, and use one of these forms:

```text
v2026.09-rc.1
v2026.09
v2026.09.1
```

The tag workflow verifies the existing SHA image and adds the matching registry tag without rebuilding it.
The application footer comes from `release-version.txt`, generated by `git describe` during the original SHA image build.
Adding a registry alias later does not alter that immutable image or its footer.

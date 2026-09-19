# Deployment runbook

This runbook covers the single-host Docker Compose project `catalog`.
Use the root `Makefile` for every deployment mutation.
The current implementation treats staging and production as sequential modes of the same host, stack, database, and search service.
Deploy to staging, validate, then promote the same release to production.
Independent staging and production hosts are proposed in `docs/proposals/candidate-centered-multi-host-deployment.md`, but are not implemented by this workflow.
For the shortest complete procedure, follow `docs/pre-release-testing.md`.

## Release contract

`CATALOG_IMAGE` must be an immutable reference: an explicit tag guaranteed not
to be retagged, or a `sha256` digest. `:latest`, bare references, and malformed
digests are rejected. `CATALOG_ES_HOST` must be explicitly set to
`elasticsearch`, the Elasticsearch 8.19.21 service. There is no search-host
default for a first deployment.

With an existing `deploy/state/release.env`, omitted image and endpoint values
are filled from the current recorded release. Thus promotion is:

```sh
make deploy ENV=prod
```

For a new or intentionally different release, provide all values:

```sh
CATALOG_IMAGE=comses/catalog/prod:<immutable-tag> \
CATALOG_ES_HOST=elasticsearch make deploy ENV=staging
```

Build and publish the exact image separately when needed:

```sh
CATALOG_IMAGE=comses/catalog/prod:<immutable-tag> make image-build
CATALOG_IMAGE=comses/catalog/prod:<immutable-tag> make image-push
```

`image-build` refuses a dirty Catalog or Citation worktree.
Use `ALLOW_DIRTY_BUILD=1` only for a disposable rehearsal image.
Before the Docker build, `scripts/deploy.sh` writes `git describe --tags --always --dirty` to `release-version.txt`.
The Dockerfile copies that file into `/code`, Django settings load it at startup, and the shared footer renders it.

The deploy preflight validates credentials, image immutability, and image
resolvability before changing the running stack.

## Root config and state safety

`docker-compose.yml` at the repository root is generated release config and
must be treated as the last-known-good deployment file. `deploy/state/` holds
metadata (`release.env`, `production-rollback.env`, and the append-only history
log), not the canonical Compose file. `production-rollback.env` is the prior
production tuple used by `make rollback`. A direct prod-to-prod deploy replaces
it with the immediately prior prod tuple. A staging deploy replacing a tracked
prod release also records that immediately prior prod tuple before generic
release state changes; promotion then preserves it.

Deployment renders a temporary candidate and runs `up -d --wait` against it.
Only successful startup publishes that candidate atomically at the root and
removes a legacy `deploy/state/docker-compose.yml`. Any render, pull, build, or
startup failure leaves both the old root file and legacy fallback untouched.

Lifecycle commands reconcile the selected Compose file with release metadata.
Missing/incompatible state, a stale root file, or both root and legacy files
being present fails closed; the root file is never silently preferred. Resolve
a legacy migration or conflict manually, then use Make commands again.

## Prerequisites

- Docker Compose v2 and a reachable single Docker host; no Swarm is required.
- After adding the operator to the `docker` group, log out and back in before running deployment commands.
- Elasticsearch hosts should meet the host prerequisite `vm.max_map_count >= 262144`.
- `deploy/conf/config.ini` and `deploy/conf/postgres_password` must exist and
  be nonempty. Run `make config-validate`.
- Provision real secrets on staging and production.
  Use `make config-generate` only when generated credentials are appropriate for a fresh or disposable host.
- The Postgres bind mount is `./docker/pgdata`. Active named volumes are
  `catalog_esdata`, `catalog_static`, and `catalog_gunicornsocket`; deployment
  lifecycle commands do not delete them.
- During the first Elasticsearch 8 release, retain legacy `catalog_esdata` and
  `catalog_solr` volumes until staging and production acceptance is complete.
- Schema migration and deploy create writable `docker/shared/catalog/logs`,
  `docker/shared/logs`, and `docker/shared/nginx/logs` before starting containers.
- The selected image must be available locally or pullable, and the staging or
  production ingress must reach Nginx.
- Nginx binds to `127.0.0.1:80` by default for a local TLS proxy.
  Set `CATALOG_HTTP_BIND=0.0.0.0:80` when ingress must connect over the host network.

## Development boundary and inspection

`make bootstrap` followed by `make up` is the normal local development flow.
`make down` and `make clean` are local operations; `clean` also removes local
volumes. `make shell`, tests, checks, and migration checks are development
operations too.

When deployment metadata exists, development-mutating targets refuse to render
or operate on the checkout. An intentional override is explicit:

```sh
DEV_OVERRIDE=1 make up       # likewise shell, down, clean, or compose-dev
```

Do not use that override on a deployment checkout unless the consequence is
understood. `make logs` is inspection and uses the existing selected
root-or-legacy configuration in one process.
For deployment observability and lifecycle use:

```sh
make status                  # metadata plus catalog containers
make logs                    # existing Compose configuration
make stop                    # stop containers; keep networks and volumes
make start                   # restart the recorded release
```

Plain root `docker compose` commands are inspection-only guidance (`ps`,
`logs`, `config`). They do not provide a safe deployment/up/down interface.

## Standard release and rollback

Schema changes are explicit and never run automatically. For a release that
contains migrations on a fresh host, use this order:

```sh
CATALOG_IMAGE=comses/catalog/prod:<immutable-tag> \
CATALOG_ES_HOST=elasticsearch CONFIRM_PRODUCTION_MIGRATION=1 \
make schema-migrate ENV=staging
CATALOG_IMAGE=comses/catalog/prod:<immutable-tag> \
CATALOG_ES_HOST=elasticsearch make deploy ENV=staging
make search-rebuild
make search-validate
# smoke-test staging, then promote without rerunning schema-migrate:
make backup
make deploy ENV=prod
```

`schema-migrate` requires an explicit immutable image, explicit ES host, and
`CONFIRM_PRODUCTION_MIGRATION=1`. It verifies credentials and database
readiness. On a fresh host (no release state, no legacy file, and no existing
`catalog` containers), it starts only the candidate database so this command
can bootstrap the database. It renders a temporary candidate and runs these
commands there, in order: `makemigrations --check --dry-run`,
`migrate --plan`, `migrate --noinput`, and `migrate --check`. It never
publishes the candidate or changes Compose/release/history state. A normal
deploy performs only a non-mutating `migrate --check` guard and refuses pending
migrations.
Because staging and prod share the database, apply the migration once. Use
expand/contract-compatible changes when old and new application versions can
overlap. There is no automatic schema rollback; a failed or partly applied
migration requires manual investigation before retrying. For later releases,
an existing tracked deployment is required and `schema-migrate` uses its
existing database without starting a new stack; run `make backup` before it.
Do not reapply the migration during promotion.

For a subsequent release, the complete sequence is:

```sh
make backup
CATALOG_IMAGE=comses/catalog/prod:<immutable-tag> \
CATALOG_ES_HOST=elasticsearch CONFIRM_PRODUCTION_MIGRATION=1 \
make schema-migrate ENV=staging
CATALOG_IMAGE=comses/catalog/prod:<immutable-tag> \
CATALOG_ES_HOST=elasticsearch make deploy ENV=staging
make search-rebuild
make search-validate
# smoke-test, then promote the same image and endpoint:
make backup
make deploy ENV=prod
```

After staging smoke tests, promote with `make deploy ENV=prod`. A successful
deploy records current and previous environment, image, endpoint, and time in
`deploy/state/release.env` (its previous fields are informational; rollback
uses the production anchor); history is appended after success. A successful
prod deploy that replaces an existing prod release also updates
`production-rollback.env` to that prior prod tuple. A staging deployment that
replaces an existing prod release likewise records that prior prod tuple;
staging is never a production rollback target. The deployment operation recreates changed
services as needed, but this is not a promise of zero downtime.

Rollback is a release redeploy, never an endpoint-only edit:

```sh
make status
make rollback
make status
```

It uses the dedicated recorded prior-production environment, immutable image,
and ES endpoint. If no prior production deploy exists, rollback fails closed.
`make stop`/`make start` are the production-safe lifecycle pair; they do not
tear down networks or delete volumes. Database operations are:

```sh
make backup
DUMP=./catalog.sql.xz CONFIRM=comses_catalog make restore
```

`make backup` writes a private, checksummed custom-format dump under
`private/backups/`. It runs `pg_dump` and validates the result inside the
PostgreSQL 18 database container, so client and server versions match.

`make restore` accepts `.dump`, `.sql`, and `.sql.xz` files. It verifies an
adjacent `.sha256` file when present, restores into a temporary database, runs
migrations and Django checks there, then swaps database names. The previous
database is retained under a timestamped name. Search indexes and the
visualization cache are rebuilt before Django is restarted; a failed
post-swap validation automatically restores the previous database.
The restore stops both Django and the scheduler before the database swap and restarts them only after validation succeeds.

The `scheduler` service runs the daily maintenance commands and monthly URL validation through cron.
Its persistent output is `docker/shared/logs/cron.log`.
Verify installed jobs with `docker compose exec -T scheduler run-parts --test /etc/cron.daily` and the corresponding monthly path.

## Fresh host from a dump

On a clean host, provision credentials, make the immutable image available, and bootstrap the empty database before deploying staging:

```sh
make config-validate
CATALOG_IMAGE=comses/catalog/prod:<immutable-tag> \
CATALOG_ES_HOST=elasticsearch CONFIRM_PRODUCTION_MIGRATION=1 \
make schema-migrate ENV=staging
CATALOG_IMAGE=comses/catalog/prod:<immutable-tag> \
CATALOG_ES_HOST=elasticsearch make deploy ENV=staging
DUMP=./catalog.sql.xz CONFIRM=comses_catalog make restore
```

The restore performs search and visualization rebuilds. Smoke-test staging,
compare key database and search counts, then promote the recorded release with
`make deploy ENV=prod`.

For an existing deployment moving to Elasticsearch 8, run `make search-rebuild`
and `make search-validate` immediately after the staging deploy.
Keep the legacy search volumes until production acceptance is complete.
The automatic `make rollback` path is supported only between releases using
this ES8-only Compose definition; an older Solr-dependent image cannot run on
the new stack.
Retain the old checkout, rendered Compose file, and legacy volumes until the
platform migration has been accepted, so an operator can restore that complete
stack manually if necessary.

## Legacy migration

Do not run or delete `deploy/state/docker-compose.yml` manually as part of a
normal rollout. A legacy-only checkout can be used by validated lifecycle
commands as a fallback. If both the legacy file and root file exist, commands
fail closed rather than choosing one. Resolve the conflict by an operator,
verify the release metadata and selected file agree, then continue with
`make status`, `make start`, `make stop`, or `make deploy`.

The old root `./deploy.sh` is a deprecation error and performs no action. Use
the Make targets listed above.

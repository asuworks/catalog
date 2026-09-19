# Pre-release and deployment checklist

Follow this checklist in order to verify a release locally, deploy it to staging, and promote it to production.
Use [deployment-runbook.md](deployment-runbook.md) for operational detail and recovery procedures.
The operator needs Git, Docker Engine, Docker Compose v2, registry access, and a checkout of the release commit.

## Current deployment model

Staging and production are two sequential modes of one single-host Compose project.
They share the database and Elasticsearch service.
Independent staging and production hosts are not supported by the current scripts.

## 1. Verify locally

Start from the intended release commit and initialize Citation:

```bash
git submodule update --init --recursive
make compose-dev
make config-validate
```

Run `make config-generate` first if local configuration does not exist.

Build and start the development stack:

```bash
docker compose build --pull django
make up
docker compose up -d --wait
```

Run all automated gates:

```bash
make check
make migrations-check
make cite-config
make cite-check
make cite-migrations-check
make test-all
```

Every command must pass.

If the release must be tested with the supplied production dump, restore it now:

```bash
xz --test private/catalog.sql.xz
DUMP=private/catalog.sql.xz CONFIRM=comses_catalog make restore
make search-validate
```

Record the restored data counts:

```bash
docker compose exec -T django python3 manage.py shell -c \
"from citation.models import Publication; print('all=', Publication.objects.count()); print('primary=', Publication.api.primary().count()); print('public=', Publication.api.primary().reviewed().count())"
```

The supplied dump produced 290,922 total publications, 9,538 primary publications, and 7,673 public publications.
For that dump, `make search-validate` must report 7,673 public and 9,538 curator documents.

Create a curator account if needed:

```bash
docker compose exec django python3 manage.py createsuperuser
```

Open <http://localhost:8000> and verify:

1. The home page, a publication page, and `/visualization/?search=CORMAS` render without errors.
2. `/publications/?search=CORMAS` and a zero-result search show the footer, facets, and pagination as applicable.
3. `/curator/export/` has 9,538 data rows for the supplied dump, keeps multiple authors in one cell, and leaves later columns aligned.
4. `/merges/` and `/merges/create/` load successfully.
5. Changing a reviewed publication to `UNREVIEWED` removes it from public search but not curator search, and changing it back restores it.

The status round trip is the main Catalog and Citation integration check.

## 2. Build and publish the release image

Catalog and Citation must both be clean:

```bash
git status --short
git -C citation status --short
git submodule status citation
```

Choose a unique tag that will never be reused, then build and publish it:

```bash
export RELEASE_TAG=2026-09-19.1
export CATALOG_IMAGE="comses/catalog/prod:${RELEASE_TAG}"

make image-build
docker run --rm --entrypoint cat "$CATALOG_IMAGE" /code/release-version.txt
make image-push
```

The recorded version must identify the intended commit and must not end in `-dirty`.
Use `ALLOW_DIRTY_BUILD=1` only for a disposable rehearsal image that will not be pushed or promoted.

## 3. Deploy staging

Run the rest of this checklist from the deployment checkout on the single staging/production host.
The checkout must be at the same Catalog commit with the same Citation submodule commit used to build the image.
On a fresh host, install Docker, clone the repository, check out that commit, and authenticate to the image registry before continuing.

Set `RELEASE_TAG` to the exact value built and tested above, then set the deployment inputs:

```bash
export RELEASE_TAG=2026-09-19.1
export CATALOG_IMAGE="comses/catalog/prod:${RELEASE_TAG}"
export CATALOG_ES_HOST=elasticsearch
export CATALOG_HTTP_BIND=127.0.0.1:80
```

The default HTTP bind is correct when a TLS proxy runs on the same host.
Use `0.0.0.0:80` only when ingress must reach Nginx over the host network.

Prepare the host:

```bash
git submodule update --init --recursive
sudo sysctl -w vm.max_map_count=262144
make config-validate
docker pull "$CATALOG_IMAGE"
```

On a fresh host, securely create `deploy/conf/config.ini` and `deploy/conf/postgres_password` before validation.
`make config-generate` can create initial random credentials, but production integration values in `config.ini` must then be configured.

For an existing database, back it up and run:

```bash
make backup
CONFIRM_PRODUCTION_MIGRATION=1 make schema-migrate ENV=staging
make deploy ENV=staging
make search-rebuild
make search-validate
```

For a genuinely fresh host with a dump, run this sequence instead:

Place the dump at `private/catalog.sql.xz`, or change `DUMP` to its actual path.

```bash
CONFIRM_PRODUCTION_MIGRATION=1 make schema-migrate ENV=staging
make deploy ENV=staging
DUMP=private/catalog.sql.xz CONFIRM=comses_catalog make restore
make search-validate
```

The restore applies migrations, rebuilds search, validates search counts, and refreshes the visualization cache.

Verify the staged deployment:

```bash
make status
docker compose config --services
docker compose exec -T db psql -U catalog -d comses_catalog -Atc 'SHOW server_version'
docker compose exec -T django python3 -c 'import psycopg2; print(psycopg2.__version__)'
docker compose exec -T django cat /code/release-version.txt
docker compose exec -T elasticsearch curl -fsS http://localhost:9200/
docker compose exec -T scheduler run-parts --test /etc/cron.daily
docker compose exec -T scheduler run-parts --test /etc/cron.monthly
curl -fsS -H 'Host: staging-catalog.comses.net' http://127.0.0.1/ >/dev/null
docker compose logs --since=15m --no-color django scheduler nginx db elasticsearch
```

Expect PostgreSQL 18.x, psycopg2 2.9.12, Elasticsearch 8.19.21, a running scheduler, and no Solr service.
Repeat the local browser checks over staging HTTPS and compare database and search counts with the local baseline.
Stop if there are unexplained tracebacks, HTTP 500 responses, count mismatches, a missing footer version, or a failed status round trip.

## 4. Promote to production

Production promotion reuses the staged image, migrated database, and rebuilt search indexes.
Do not rerun migrations or rebuild search during promotion.

Create a final backup and promote the recorded staging release:

```bash
make backup
make deploy ENV=prod
make status
make search-validate
curl -fsS -H 'Host: catalog.comses.net' http://127.0.0.1/ >/dev/null
docker compose logs --since=15m --no-color django scheduler nginx db elasticsearch
```

Repeat the browser checks at <https://catalog.comses.net>.
Confirm that the footer version matches staging and that `make status` reports `env=prod`.

If the application deployment fails, inspect `make status` and use `make rollback` to redeploy the recorded prior production release.
Rollback does not reverse database migrations.

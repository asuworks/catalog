# Candidate-centered multi-host deployment

**Status:** Implemented and validated through fork workflows and independent staging and production VM rehearsals.
Upstream maintainer review and deployment on the real staging and production hosts remain pending.
Final GitHub Release publication was added after those rehearsals and remains to be exercised by the next approved stable tag.

This document records the design and is not an operator procedure.
Use [the release and deployment runbook](../deployment-runbook.md) for real releases and [the deployment acceptance guide](../deployment-acceptance-testing.md) for disposable-host validation.

This design gives Catalog one build artifact, an explicit human QA boundary, and independent staging and production hosts.
The implementation is intentionally Docker Compose based and does not attempt zero-downtime deployment.

## Decisions

- Staging and production are separate VMs with independent state, secrets, databases, search volumes, and backups.
- CI builds once and publishes to GHCR.
- A successful `main` build is a candidate, not a final release.
- After staging approval, a stable annotated tag creates the GitHub Release and registry alias without rebuilding.
- Production deployment begins only after final release publication.
- Hosts deploy only an exact `name@sha256:<digest>` reference.
- The same image digest and Catalog bundle revision move from staging approval to production.
- Each host has a fixed `staging` or `prod` identity established during provisioning.
- Both hosts use Compose project name `catalog` because their Docker daemons are separate.
- PostgreSQL 18, Elasticsearch 8, Redis, and Nginx images are pinned by digest.
- Elasticsearch is rebuilt from PostgreSQL on each candidate before activation.
- The migration uses fresh hosts and dump restore rather than an in-place Solr or Elasticsearch 6 cutover.
- Deployment downtime is acceptable.
- Staging uses a file email backend and never sends external mail.
- Production uses SMTP and requires configured credentials.
- Backups remain on-host with 30-day retention for now.
- Off-host backups and signature verification are deferred, visible risks.

## Why not Dokku

Dokku could deploy the Django web process and use its PostgreSQL, Redis, and Elasticsearch plugins.
It would not remove the project-specific work needed for dump validation and database swap, migration approval, Elasticsearch generation and alias rollback, visualization cache rebuild, scheduler jobs, cross-service release reporting, or two-host digest approval.
Those operations would still require custom plugins or scripts tied to Dokku's lifecycle and storage conventions.

Adding Dokku would therefore create a second orchestration layer without replacing the hard Catalog-specific machinery.
Docker Compose is already present, supports the complete topology directly, and keeps the operational contract in this repository.

## Trust boundaries

### CI

CI checks out the Catalog commit and exact Citation gitlink, runs all checks and tests, builds the production image, and records both revisions as image labels.
Pull requests do not publish.
A successful `main` build pushes `sha-<catalog-revision>`, resolves the registry digest, pulls that exact digest again, verifies its labels, and uploads `release-handoff.json`.

After staging approval, an annotated stable release tag adds a human-readable registry alias to the existing SHA image, creates the GitHub Release, and attaches `release-handoff.json`.
Catalog does not use RC tags.
Final publication does not rebuild the image or change its digest.

### Staging host

Staging owns its host identity, credentials, data, candidate state, active and rollback records, Compose artifacts, reports, and backups.
It accepts a CI handoff and provides the human QA result for that exact candidate.

### Production host

Production independently receives the approved digest and revision.
It never reads staging files or infers a candidate from staging state.
Its operator explicitly invokes the same candidate sequence against production data.

### Registry

GHCR is the artifact handoff between CI and each host.
The intended package is public, so host credentials are unnecessary.
A private package can instead use a host-local read-only token.

## Host contract

Provisioning writes `/etc/comses-catalog/host.env` with:

```text
CATALOG_HOST_ID=staging|prod
CATALOG_ENV=staging|prod
COMPOSE_PROJECT_NAME=catalog
CATALOG_DOMAIN=<fixed domain for that identity>
CATALOG_HTTP_BIND=127.0.0.1:80
```

The controller rejects a missing, malformed, group-writable, world-writable, or identity-inconsistent file.
Provisioning will not change one host identity into the other.
Release commands do not accept `ENV`, an Elasticsearch host, or a project name from the command line.

Secrets are separate from release state under `/etc/comses-catalog/secrets` with owner-only permissions.
Release bundles, state, shared data, receipts, and reports live under `/var/lib/comses-catalog`.
Database backups live under `/var/backups/comses-catalog`.

## Candidate contract

`make candidate IMAGE=... BUNDLE_REVISION=...` is the only release-input command.
It requires:

- a full lowercase SHA-256 image reference;
- a full Catalog Git SHA matching checkout `HEAD`;
- a clean Catalog worktree and clean Citation checkout;
- a Citation checkout matching the Catalog gitlink;
- image labels matching both revisions;
- valid host identity, secrets, Docker access, disk space, and Elasticsearch host settings;
- a rendered Compose file that passes `docker compose config --quiet`.

The controller archives the Git revision into a release directory and hashes the bundle independently from the rendered Compose file.
Candidate, deploy, start, backup, rollback, and recovery operations verify those hashes before use.
A changed artifact fails closed.

Creating a candidate does not start services, publish the active Compose file, or change active and rollback state.
An untouched candidate can be replaced atomically.
Once restore, migration, or rebuild begins, it is locked until successful deployment or an explicitly authorized incident retry.

## State model

State uses versioned JSON files written with file and directory synchronization:

```text
candidate.json  explicit image, bundle, Citation revision, artifact hashes,
                restore state, migration state, and data-rebuild state
active.json     currently activated release and search aliases
rollback.json   immediately previous successful release on this host
journal.json    in-progress deploy or rollback and recovery checkpoints
history.jsonl   append-only operation outcomes and incident retry records
```

The canonical active Compose file is `/var/lib/comses-catalog/runtime/docker-compose.yml`.
It is published only after the candidate runtime passes health checks.

The transaction journal checkpoints candidate start, runtime readiness, canonical Compose publication, state publication, and commit.
If activation fails before commit, the controller starts and health-checks the previous active artifact before reporting recovery success.
An unverified recovery keeps the journal and blocks further mutations.
If history or cleanup fails after commit, recovery finishes the new release instead of reverting an already committed state.

## Database workflow

A fresh host restores `.dump`, `.sql`, or `.sql.xz` into a temporary database.
An adjacent checksum is verified when present.
Plain SQL containing database creation, deletion, alteration, or `\connect` is rejected.
The restored database must contain Citation publications and no unvalidated constraints before its name is swapped into place.
A JSON receipt records the dump checksum, size, PostgreSQL versions, counts, and retained prior database name.
An interrupted restore remains locked until an operator investigates it and records an explicit candidate retry.

Schema migration is separate from restore and deploy.
It requires `CONFIRM_SCHEMA_MIGRATION=1` and executes the dry migration check, plan, migration, and final check in order.
An existing host additionally requires a verified backup from its active release that is less than 24 hours old.
There is no automatic schema rollback.

Backups use PostgreSQL 18 tools inside the database container, custom dump format, `pg_restore --list` validation, SHA-256 files, and JSON receipts.
The systemd timer runs nightly and prunes receipts and artifacts after 30 days.

## Search and derived data

`make data-rebuild` is an explicit activation gate after migration.
It writes every search document type into fresh Elasticsearch generation indexes.
It validates expected counts before moving any stable aliases.
All aliases move in one Elasticsearch operation, and one previous generation remains available for rollback.
The same command then refreshes the visualization cache.

Failure restores the previously captured aliases where possible and locks the candidate.
After investigation, `CONFIRM_CANDIDATE_RETRY=1 make candidate-retry` first verifies that the pre-rebuild aliases are restored, records the intervention, and allows that stage to run again.

## Activation and rollback

`make deploy` consumes only candidate state.
It verifies no migrations are pending, validates live search indexes, starts the exact rendered candidate with `up -d --no-build --wait`, verifies Nginx and an application request, then publishes active state.
It never builds, pulls a tag, applies migrations, or deletes data volumes.

`make rollback` consumes only the host-local rollback record.
It restores that release's search aliases, starts and verifies its exact artifacts, then swaps active and rollback state.
Running rollback a second time toggles back to the release it replaced.
Database migrations are not reversed, so application rollback depends on compatible schema changes.

## Fresh production replacement

The production migration intentionally uses downtime:

1. Validate the release against a representative dump on local and staging systems.
2. Publish the staging-approved final GitHub Release.
3. Stop writes on the old production host.
4. Create the final dump.
5. Restore, migrate, rebuild, and validate on the fresh production VM.
6. Switch ingress to the new VM.

The untouched old VM can be used as fallback before the new database accepts writes.
Once new writes begin, host fallback is no longer safe because the databases diverge.
Application rollback on the new host remains available after it has two successful releases.

## Operator surface

```sh
sudo make host-provision HOST_ID=staging|prod OPERATOR=<user>
make host-check
make candidate IMAGE=<name@sha256:digest> BUNDLE_REVISION=<git-sha>
make restore DUMP=<path> CONFIRM=comses_catalog
make backup
CONFIRM_SCHEMA_MIGRATION=1 make schema-migrate
make data-rebuild
make deploy
make status
make release-report
make rollback
make recover
CONFIRM_CANDIDATE_RETRY=1 make candidate-retry
make start
make stop
make logs
```

Local development has separate `make up`, `make down`, and `make dev-restore` commands.
No deployment command reads a root development Compose file.

## Acceptance criteria

The implementation is accepted when:

- Catalog and Citation checks, migrations checks, and behavior tests pass;
- pull requests do not publish images;
- a main build emits a verified digest handoff;
- a staging-approved annotated tag aliases that digest without rebuilding and creates a GitHub Release with the matching handoff;
- the same digest deploys independently to staging and production simulations;
- staging records file-based email and production records SMTP email;
- dump counts survive restore and search counts validate;
- backup artifacts, checksums, receipts, and schedules work;
- a second release can deploy and roll back on each host;
- malformed digest, missing confirmation, modified artifact, and failed activation paths stop safely;
- no production command reads staging state;
- release reports preserve enough evidence to identify the running source and data state.

## Deferred work

- Copy backups to independent off-host storage and test disaster restore on a schedule.
- Add image signatures and a pinned Cosign verification policy if the registry threat model requires it.
- Add external monitoring and alerting around backup timers, scheduler failures, disk capacity, and HTTP health.

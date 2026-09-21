# Release and deployment runbook

This is the canonical operator guide for preparing and deploying Catalog releases.
It assumes the maintainer works directly in `comses/catalog` and `comses/citation` through topic branches and pull requests.
Use [deployment-acceptance-testing.md](deployment-acceptance-testing.md) only when validating the release machinery on personal forks and disposable hosts.

Staging and production are independent Docker Compose hosts.
They never share credentials, databases, volumes, Compose files, or release state.
Both hosts deploy the exact image digest and Catalog revision from the same CI handoff.

Use `make help` as the command index.
Stop at the first unexplained failure.

## Release lifecycle

A **candidate** is an immutable image and handoff produced by a successful `main` build.
A **final release** is a staging-approved candidate identified by a stable version tag and a GitHub Release.
A **deployment** activates that same image digest on a host.

The normal path is:

1. Merge to `main` and let CI publish the candidate image and handoff.
2. Deploy the candidate to staging and complete QA.
3. Push an annotated `vYYYY.MM` or `vYYYY.MM.N` tag so CI publishes the final GitHub Release without rebuilding the image.
4. Deploy the exact staging-approved digest to production.

A Package without a GitHub Release is a candidate awaiting approval.
Catalog does not use RC tags.

## Choose the procedure

Choose from the host state before running any database command:

| Situation | Procedure | Database order |
| --- | --- | --- |
| Host has an active release | [Subsequent release](#4-deploy-to-an-existing-staging-host) | Back up before and after; never restore |
| New or replacement host has no active release | [First deployment from a dump](#7-first-deployment-from-a-dump) | Restore once; back up after activation |
| A deployment or rollback was interrupted | [Rollback and recovery](#rollback-and-recovery) | Investigate before retrying |

On a provisioned host, `make status` reports either `Active: none` or the active image and bundle revision.
`make restore` is intentionally rejected after a host has an active release.
Do not substitute the first-deployment sequence for a subsequent release.

The restore, migration, search rebuild, deployment, backup, report, and rollback commands can take several minutes.
Run each command to completion before starting the next one.
If the SSH connection is interrupted, reconnect and do not blindly repeat the command; follow [Interrupted commands](#interrupted-commands) first.

## Release contract

The deployment unit is:

- an application image in `name@sha256:<64 lowercase hex>` form;
- the full 40-character Catalog Git revision used as the deployment bundle;
- the Citation Git revision recorded in both the Catalog gitlink and image label.

`make candidate` rejects tags, a dirty checkout, revision mismatches, modified release bundles, malformed host identity, and images whose labels do not match the checkout.
It renders and validates a candidate without changing the running release.

CI publishes a candidate `release-handoff.json` after a successful build of `comses/catalog` `main`.
That file is the source for `IMAGE` and `BUNDLE_REVISION` throughout staging and production.
After staging approval, the final tag workflow creates the GitHub Release, attaches a matching handoff, and gives the same image digest a stable registry alias.
Production must use the original candidate values that passed staging QA and must not begin before the final GitHub Release is published.

## 1. Verify the changes locally

Run these commands from the Catalog topic branch on the workstation.
Set the branch names to the branches being released:

```sh
export CATALOG_BRANCH='release/2026-09'
export CITATION_BRANCH='release/2026-09'
export DUMP=/absolute/path/catalog.sql.xz
```

Change the example branch names to the topic branches being released.
`CITATION_BRANCH` is needed only when Citation changed.
`DUMP` is needed only for production-like local data validation or a first deployment.

Verify GitHub CLI authentication and confirm that both push remotes target the CoMSES repositories:

```sh
gh auth status
gh auth setup-git
git remote get-url --push origin | \
  grep -Eq '^(https://github\.com/|git@github\.com:)comses/catalog\.git$'
git -C citation remote get-url --push origin | \
  grep -Eq '^(https://github\.com/|git@github\.com:)comses/citation\.git$'
```

Do not continue from a personal-fork remote when preparing the official candidate.

Build the development application and run every automated gate:

```sh
make bootstrap
make cite-config
docker compose build --pull django
make up
make check
make migrations-check
make cite-check
make cite-migrations-check
make test-all
make deploy-controller-test
```

When the release changes migrations, database access, search, exports, or visualization data, restore a representative dump into the disposable local database:

```sh
make dev-restore DUMP="$DUMP" CONFIRM=comses_catalog
```

Record a comparison baseline:

```sh
docker compose exec -T django python3 manage.py shell -c \
"from citation.models import Publication; print('all=', Publication.objects.count()); print('primary=', Publication.api.primary().count()); print('public=', Publication.api.primary().reviewed().count())" </dev/null
```

Create or update the local-only curator account after restoring any dump:

```sh
docker compose exec -T django python3 manage.py shell -c \
"from django.contrib.auth import get_user_model; User = get_user_model(); user, _ = User.objects.get_or_create(username='local-curator', defaults={'email': 'local-curator@example.invalid'}); user.is_active = True; user.is_staff = True; user.is_superuser = True; user.save(); print('local-curator is ready')" </dev/null
docker compose exec django python3 manage.py changepassword local-curator
```

Sign in at <http://localhost:8000/accounts/login/> as `local-curator` with the password just set.

Open <http://localhost:8000> and verify:

1. The home page, one publication page, `/publications/?search=CORMAS`, a zero-result search, and `/visualization/?search=CORMAS` render with the footer.
2. `/curator/export/` has one row per primary publication, keeps multiple authors in one CSV cell, and leaves later columns aligned.
3. `/merges/` and `/merges/create/` load.
4. In a curator publication page, change a reviewed publication to `UNREVIEWED`, save it, and confirm it disappears from public search but remains in curator search.
5. Restore that publication to `REVIEWED` and confirm it returns to public search.

The status round trip is the principal Catalog and Citation integration check.

## 2. Merge Citation and Catalog

Pull requests build and test but do not publish images.
Never push release work directly to either `main` branch.

### When Citation changed

Start with a clean Citation topic branch:

```sh
test "$(git -C citation branch --show-current)" = "$CITATION_BRANCH"
test -z "$(git -C citation status --porcelain)"
git -C citation push --set-upstream origin "$CITATION_BRANCH"
```

Open or reuse the Citation pull request:

```sh
CITATION_PR_URL="$(gh pr view "$CITATION_BRANCH" \
  --repo comses/citation --json url --jq .url 2>/dev/null || true)"
if [ -z "$CITATION_PR_URL" ]; then
  CITATION_PR_URL="$(gh pr create \
    --repo comses/citation \
    --base main \
    --head "$CITATION_BRANCH" \
    --fill)"
fi
export CITATION_PR_URL
printf 'Citation pull request: %s\n' "$CITATION_PR_URL"
gh pr checks "$CITATION_PR_URL" --repo comses/citation --watch --interval 10
gh pr view "$CITATION_PR_URL" --repo comses/citation --web
```

After review, merge it using the repository's normal merge method:

```sh
gh pr merge "$CITATION_PR_URL" --repo comses/citation
test "$(gh pr view "$CITATION_PR_URL" \
  --repo comses/citation --json state --jq .state)" = MERGED
CITATION_UPSTREAM_SHA="$(gh pr view "$CITATION_PR_URL" \
  --repo comses/citation --json mergeCommit --jq .mergeCommit.oid)"
export CITATION_UPSTREAM_SHA
test -n "$CITATION_UPSTREAM_SHA"
```

Update the Catalog gitlink to the merged Citation commit:

```sh
git -C citation fetch origin main
git -C citation checkout "$CITATION_UPSTREAM_SHA"
test -z "$(git -C citation status --porcelain)"
git add citation
if ! git diff --cached --quiet; then
  git commit -m 'chore: update Citation revision'
fi
```

### When Citation did not change

Do not create an empty Citation pull request.
Verify that the pinned Citation commit is already present on upstream `main`:

```sh
CITATION_UPSTREAM_SHA="$(git rev-parse HEAD:citation)"
export CITATION_UPSTREAM_SHA
if [ "$(git -C citation rev-parse --is-shallow-repository)" = true ]; then
  git -C citation fetch --unshallow origin main
else
  git -C citation fetch origin main
fi
git -C citation merge-base --is-ancestor "$CITATION_UPSTREAM_SHA" origin/main
bash scripts/checkout-citation.sh comses
```

### Merge Catalog

Run all gates once more against the final Citation gitlink:

```sh
make check
make migrations-check
make cite-check
make cite-migrations-check
make test-all
make deploy-controller-test
test -z "$(git status --porcelain)"
test -z "$(git -C citation status --porcelain)"
test "$(git rev-parse HEAD:citation)" = "$CITATION_UPSTREAM_SHA"
```

Push the Catalog topic branch and open or reuse its pull request:

```sh
test "$(git branch --show-current)" = "$CATALOG_BRANCH"
git push --set-upstream origin "$CATALOG_BRANCH"
CATALOG_PR_URL="$(gh pr view "$CATALOG_BRANCH" \
  --repo comses/catalog --json url --jq .url 2>/dev/null || true)"
if [ -z "$CATALOG_PR_URL" ]; then
  CATALOG_PR_URL="$(gh pr create \
    --repo comses/catalog \
    --base main \
    --head "$CATALOG_BRANCH" \
    --fill)"
fi
export CATALOG_PR_URL
printf 'Catalog pull request: %s\n' "$CATALOG_PR_URL"
gh pr checks "$CATALOG_PR_URL" --repo comses/catalog --watch --interval 10
gh pr view "$CATALOG_PR_URL" --repo comses/catalog --web
```

After review, merge it using the repository's normal merge method:

```sh
gh pr merge "$CATALOG_PR_URL" --repo comses/catalog
test "$(gh pr view "$CATALOG_PR_URL" \
  --repo comses/catalog --json state --jq .state)" = MERGED
BUNDLE_REVISION="$(gh pr view "$CATALOG_PR_URL" \
  --repo comses/catalog --json mergeCommit --jq .mergeCommit.oid)"
export BUNDLE_REVISION
test -n "$BUNDLE_REVISION"
```

## 3. Download the official candidate handoff

Wait for the `CoMSES Docker CI` workflow for the merged Catalog commit:

```sh
export MAIN_RUN_ID=
for _attempt in $(seq 1 24); do
  MAIN_RUN_ID="$(gh run list \
    --repo comses/catalog \
    --workflow docker-build.yml \
    --branch main \
    --limit 10 \
    --json databaseId,headSha \
    --jq "map(select(.headSha == \"${BUNDLE_REVISION}\"))[0].databaseId // empty")"
  [ -z "$MAIN_RUN_ID" ] || break
  sleep 5
done
test -n "$MAIN_RUN_ID"
gh run watch "$MAIN_RUN_ID" --repo comses/catalog --exit-status
```

Download and validate the immutable handoff:

```sh
mkdir -p "$PWD/private/release-handoffs"
HANDOFF_DIR="$(mktemp -d "$PWD/private/release-handoffs/${BUNDLE_REVISION}.XXXXXX")"
export HANDOFF_DIR
gh run download "$MAIN_RUN_ID" \
  --repo comses/catalog \
  --name "catalog-candidate-${BUNDLE_REVISION}" \
  --dir "$HANDOFF_DIR"
HANDOFF="${HANDOFF_DIR}/release-handoff.json"
IMAGE="$(python3 -c 'import json, pathlib, sys; print(json.loads(pathlib.Path(sys.argv[1]).read_text())["image"])' "$HANDOFF")"
HANDOFF_BUNDLE_REVISION="$(python3 -c 'import json, pathlib, sys; print(json.loads(pathlib.Path(sys.argv[1]).read_text())["bundle_revision"])' "$HANDOFF")"
HANDOFF_CITATION_REVISION="$(python3 -c 'import json, pathlib, sys; print(json.loads(pathlib.Path(sys.argv[1]).read_text())["citation_revision"])' "$HANDOFF")"
export HANDOFF IMAGE HANDOFF_BUNDLE_REVISION HANDOFF_CITATION_REVISION
cat "$HANDOFF"
test "$HANDOFF_BUNDLE_REVISION" = "$BUNDLE_REVISION"
test "$HANDOFF_CITATION_REVISION" = "$CITATION_UPSTREAM_SHA"
```

The first official publication may leave the GHCR package private.
An organization owner must open the package settings, choose **Change visibility**, and make it public, or each host must authenticate with a read-only package token:

```sh
printf 'https://github.com/orgs/comses/packages/container/catalog/settings\n'
```

After changing visibility or authenticating Docker, confirm that the exact digest is readable:

```sh
docker manifest inspect "$IMAGE" >/dev/null
```

## 4. Deploy to an existing staging host

This is the recurring staging procedure.
The host must already have an active release.
Do not transfer a database dump and do not run `make restore`.

From the workstation, transfer the handoff and connect:

```sh
export STAGING_SSH='operator@staging-host'
scp "$HANDOFF" "${STAGING_SSH}:release-handoff.json"
ssh "$STAGING_SSH"
```

Run the remaining commands in the staging SSH session:

```sh
cd "$HOME/catalog" || exit 1
export HANDOFF="$HOME/release-handoff.json"
IMAGE="$(python3 -c 'import json, pathlib, sys; print(json.loads(pathlib.Path(sys.argv[1]).read_text())["image"])' "$HANDOFF")"
BUNDLE_REVISION="$(python3 -c 'import json, pathlib, sys; print(json.loads(pathlib.Path(sys.argv[1]).read_text())["bundle_revision"])' "$HANDOFF")"
export IMAGE BUNDLE_REVISION
git fetch origin main
git checkout "$BUNDLE_REVISION"
bash scripts/checkout-citation.sh comses
test -z "$(git status --porcelain)"
test -z "$(git -C citation status --porcelain)"
sudo make host-provision HOST_ID=staging OPERATOR="$USER"
make host-check
make backup
make candidate IMAGE="$IMAGE" BUNDLE_REVISION="$BUNDLE_REVISION"
CONFIRM_SCHEMA_MIGRATION=1 make schema-migrate
make data-rebuild
make deploy
make backup
make status
make release-report
```

The first backup is the rollback safety copy and must belong to the previously active release.
The backup gate accepts only a verified backup from that release that is less than 24 hours old.
The second backup records the migrated database under the newly active release and makes `latest_backup` available in the release report immediately.
There is no automatic database schema rollback, so migrations must remain compatible with the previous application when rollback is expected.

Complete staging QA:

1. Compare database and search counts with the prior release report.
2. Repeat the five browser checks from local verification through the normal TLS ingress.
3. Confirm the report uses `django.core.mail.backends.filebased.EmailBackend`.
4. Inspect `/var/lib/comses-catalog/shared/mail` if a QA action generated email.
5. Record approval of the exact `IMAGE` digest and `BUNDLE_REVISION`.

Staging must not contain working SMTP credentials and must never send external email.

## 5. Publish the final release

The successful official `main` build is the release candidate, not the final release.
After staging approval, publish a final release before deploying to production.
The final tag is never a deployment input and the workflow does not rebuild the image.

Choose the next unused `vYYYY.MM` or `vYYYY.MM.N` tag, where `N` contains one to three digits:

```sh
export FINAL_TAG="v$(date -u +%Y.%m)"
printf '%s\n' "$FINAL_TAG" | grep -Eq '^v[0-9]{4}\.[0-9]{2}(\.[0-9]{1,3})?$'
if git show-ref --verify --quiet "refs/tags/${FINAL_TAG}" || \
   git ls-remote --exit-code origin "refs/tags/${FINAL_TAG}" >/dev/null 2>&1; then
  echo "ERROR: tag ${FINAL_TAG} already exists" >&2
  exit 1
fi
git fetch origin main
git merge-base --is-ancestor "$BUNDLE_REVISION" origin/main
git tag -a "$FINAL_TAG" "$BUNDLE_REVISION" -m "Catalog ${FINAL_TAG#v}"
git push origin "refs/tags/${FINAL_TAG}"
```

Watch the `Publish final release` workflow and verify the visible GitHub Release and its attached handoff:

```sh
export TAG_RUN_ID=
for _attempt in $(seq 1 24); do
  TAG_RUN_ID="$(gh run list \
    --repo comses/catalog \
    --workflow release-tag.yml \
    --branch "$FINAL_TAG" \
    --limit 10 \
    --json databaseId,headSha \
    --jq "map(select(.headSha == \"${BUNDLE_REVISION}\"))[0].databaseId // empty")"
  [ -z "$TAG_RUN_ID" ] || break
  sleep 5
done
test -n "$TAG_RUN_ID"
gh run watch "$TAG_RUN_ID" --repo comses/catalog --exit-status
test "$(gh release view "$FINAL_TAG" --repo comses/catalog --json tagName --jq .tagName)" = "$FINAL_TAG"
test "$(gh release view "$FINAL_TAG" --repo comses/catalog --json isDraft --jq .isDraft)" = false
test "$(gh release view "$FINAL_TAG" --repo comses/catalog --json isPrerelease --jq .isPrerelease)" = false
RELEASE_URL="$(gh release view "$FINAL_TAG" --repo comses/catalog --json url --jq .url)"
export RELEASE_URL
test -n "$RELEASE_URL"
printf 'Final release: %s\n' "$RELEASE_URL"
TAG_HANDOFF_DIR="$(mktemp -d "$PWD/private/release-handoffs/${FINAL_TAG}.XXXXXX")"
gh release download "$FINAL_TAG" \
  --repo comses/catalog \
  --pattern release-handoff.json \
  --dir "$TAG_HANDOFF_DIR"
TAG_HANDOFF="${TAG_HANDOFF_DIR}/release-handoff.json"
TAG_IMAGE="$(python3 -c 'import json, pathlib, sys; print(json.loads(pathlib.Path(sys.argv[1]).read_text())["image"])' "$TAG_HANDOFF")"
TAG_BUNDLE_REVISION="$(python3 -c 'import json, pathlib, sys; print(json.loads(pathlib.Path(sys.argv[1]).read_text())["bundle_revision"])' "$TAG_HANDOFF")"
TAG_CITATION_REVISION="$(python3 -c 'import json, pathlib, sys; print(json.loads(pathlib.Path(sys.argv[1]).read_text())["citation_revision"])' "$TAG_HANDOFF")"
TAG_RELEASE_TAG="$(python3 -c 'import json, pathlib, sys; print(json.loads(pathlib.Path(sys.argv[1]).read_text())["release_tag"])' "$TAG_HANDOFF")"
test "$TAG_IMAGE" = "$IMAGE"
test "$TAG_BUNDLE_REVISION" = "$BUNDLE_REVISION"
test "$TAG_CITATION_REVISION" = "$HANDOFF_CITATION_REVISION"
test "$TAG_RELEASE_TAG" = "$FINAL_TAG"
```

Continue to production with `IMAGE` and `BUNDLE_REVISION` from the original `main` handoff.
The GitHub Release and registry tag identify the approved candidate, but neither replaces the digest as the deployment input.

## 6. Deploy to an existing production host

Complete Section 5 successfully before continuing.
Use the exact handoff approved on staging.
Do not rebuild the image, download a newer handoff, transfer the staging database, or run `make restore`.

From the workstation, transfer the approved handoff and connect:

```sh
export PROD_SSH='operator@production-host'
scp "$HANDOFF" "${PROD_SSH}:release-handoff.json"
ssh "$PROD_SSH"
```

Run the remaining commands in the production SSH session:

```sh
cd "$HOME/catalog" || exit 1
export HANDOFF="$HOME/release-handoff.json"
IMAGE="$(python3 -c 'import json, pathlib, sys; print(json.loads(pathlib.Path(sys.argv[1]).read_text())["image"])' "$HANDOFF")"
BUNDLE_REVISION="$(python3 -c 'import json, pathlib, sys; print(json.loads(pathlib.Path(sys.argv[1]).read_text())["bundle_revision"])' "$HANDOFF")"
export IMAGE BUNDLE_REVISION
git fetch origin main
git checkout "$BUNDLE_REVISION"
bash scripts/checkout-citation.sh comses
test -z "$(git status --porcelain)"
test -z "$(git -C citation status --porcelain)"
sudo make host-provision HOST_ID=prod OPERATOR="$USER"
make host-check
make backup
make candidate IMAGE="$IMAGE" BUNDLE_REVISION="$BUNDLE_REVISION"
CONFIRM_SCHEMA_MIGRATION=1 make schema-migrate
make data-rebuild
make deploy
make backup
make status
make release-report
```

Confirm that production reports:

- the exact image digest and bundle revision approved on staging;
- plausible database and search counts;
- the SMTP email backend and working production SMTP configuration;
- successful pre-migration and post-deployment backups.

Complete the browser smoke tests through normal production ingress.

## 7. First deployment from a dump

Use this section only for a new or replacement host with no active release.
After this succeeds, all later releases use Sections 4 or 6.
A first staging deployment may use a candidate, but a first production deployment requires the final release from Section 5.

### Provision the host

Install Git, GNU Make, Python 3.10 or newer, `xz`, Docker Engine, and Docker Compose v2.
Give the operator Docker access, then log out and back in so group membership is active.

On the workstation, create the checksum and transfer the approved handoff and dump:

```sh
export TARGET_SSH='operator@new-host'
export TARGET_HOST_ID=staging
DUMP_NAME="$(basename "$DUMP")"
export DUMP_NAME
test -s "$HANDOFF"
test -s "$DUMP"
test "$TARGET_HOST_ID" = staging || test "$TARGET_HOST_ID" = prod
(
  cd "$(dirname "$DUMP")" || exit 1
  sha256sum "$DUMP_NAME" > "${DUMP_NAME}.sha256"
)
scp "$HANDOFF" "${TARGET_SSH}:release-handoff.json"
scp "$DUMP" "${DUMP}.sha256" "${TARGET_SSH}:"
printf '%s\n' "$DUMP_NAME" | ssh "$TARGET_SSH" 'cat > "$HOME/.comses-catalog-dump-name"'
printf '%s\n' "$TARGET_HOST_ID" | ssh "$TARGET_SSH" 'cat > "$HOME/.comses-catalog-host-id"'
ssh "$TARGET_SSH"
```

Use `TARGET_HOST_ID=prod` for production.
Run the remaining commands inside the new host's SSH session.
Clone and check out the exact bundle:

```sh
git clone https://github.com/comses/catalog.git "$HOME/catalog"
cd "$HOME/catalog" || exit 1
export HANDOFF="$HOME/release-handoff.json"
export DUMP="$HOME/$(cat "$HOME/.comses-catalog-dump-name")"
export TARGET_HOST_ID="$(cat "$HOME/.comses-catalog-host-id")"
IMAGE="$(python3 -c 'import json, pathlib, sys; print(json.loads(pathlib.Path(sys.argv[1]).read_text())["image"])' "$HANDOFF")"
BUNDLE_REVISION="$(python3 -c 'import json, pathlib, sys; print(json.loads(pathlib.Path(sys.argv[1]).read_text())["bundle_revision"])' "$HANDOFF")"
export IMAGE BUNDLE_REVISION
chmod 0600 "$HANDOFF" "$DUMP" "${DUMP}.sha256"
(
  cd "$(dirname "$DUMP")" || exit 1
  sha256sum --check "$(basename "$DUMP").sha256"
)
git checkout "$BUNDLE_REVISION"
bash scripts/checkout-citation.sh comses
test -z "$(git status --porcelain)"
test -z "$(git -C citation status --porcelain)"
```

Keep the adjacent checksum named `<dump>.sha256`.

Provision with the selected immutable host identity:

```sh
sudo make host-provision HOST_ID="$TARGET_HOST_ID" OPERATOR="$USER"
"${EDITOR:-vi}" /etc/comses-catalog/secrets/config.ini
make host-check
```

Provisioning is idempotent and refuses to change an existing host from staging to production or vice versa.
It creates random database and Django secrets, ensures `vm.max_map_count` is at least `262144`, installs the host backup helper, and enables the nightly backup timer.

Leave staging SMTP credentials empty.
Production requires nonempty SMTP host, port, user, and password values.

### Restore and activate

Run the first-deployment sequence in this exact order:

```sh
make candidate IMAGE="$IMAGE" BUNDLE_REVISION="$BUNDLE_REVISION"
make restore DUMP="$DUMP" CONFIRM=comses_catalog
CONFIRM_SCHEMA_MIGRATION=1 make schema-migrate
make data-rebuild
make deploy
make backup
make status
make release-report
```

There is no pre-deployment backup because the new host has no active Catalog database.
The backup after activation creates the first verified backup for the new release.

Restore accepts `.dump`, `.sql`, and `.sql.xz`.
It verifies an adjacent `<dump>.sha256` file when present, rejects database-level commands in plain SQL, restores into a temporary database, validates data counts and constraints, then atomically swaps database names.
The previous empty database is retained under a timestamped name.

Compare total, primary, public, and search counts with the source database and staging expectations.
Complete the browser, email-backend, scheduler, and backup checks before accepting the host.

### Replace the production VM

For a first deployment onto a replacement production VM:

1. Validate the candidate on staging and publish the final release.
2. Stop writes or make the old production application read-only.
3. Create and transfer the final production dump and checksum.
4. Run the first-deployment sequence on the new VM.
5. Verify counts and application behavior before switching ingress.
6. Keep the old VM intact during initial validation.

The old VM is a valid fallback only until the new production database accepts writes.
After that point, returning traffic to the old database would lose or split new data.

## Operational reference

### Host layout

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

Do not change the database password in only one secret file.
`host-check` requires the password in `config.ini` to match `postgres_password`.

Nginx binds to `127.0.0.1:80` for a same-host TLS proxy by default.
If ingress reaches the VM over the network, change `CATALOG_HTTP_BIND` in `host.env` to the required bind address and restore file permissions afterward.

### Status and reports

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

### Backup and scheduler

The provisioner enables `comses-catalog-backup.timer`, scheduled daily at 02:15 UTC.
Backups are custom-format PostgreSQL dumps with SHA-256 files and JSON receipts.
The controller validates each dump with PostgreSQL 18 `pg_restore` from the database container and retains 30 days.

Verify the schedule and run it once manually after provisioning or changing the controller:

```sh
systemctl list-timers comses-catalog-backup.timer
sudo systemctl start comses-catalog-backup.service
test "$(systemctl show comses-catalog-backup.service -p Result --value)" = success
sudo journalctl -u comses-catalog-backup.service -n 50 --no-pager
```

These backups are on the same host.
Host or attached-volume loss can destroy both the database and backups; off-host copies are deliberately deferred and remain an operational risk.

The application scheduler is a separate Compose service.
Its daily maintenance and monthly URL validation output is `/var/lib/comses-catalog/shared/logs/cron.log`.
Verify task discovery after provisioning or changing the scheduler image:

```sh
COMPOSE=/var/lib/comses-catalog/runtime/docker-compose.yml
docker compose -p catalog -f "$COMPOSE" exec -T scheduler run-parts --test /etc/cron.daily </dev/null
docker compose -p catalog -f "$COMPOSE" exec -T scheduler run-parts --test /etc/cron.monthly </dev/null
tail -n 100 /var/lib/comses-catalog/shared/logs/cron.log
```

To exercise only Catalog's daily work on staging, run `/etc/cron.daily/daily_catalog_tasks` directly inside the scheduler container instead of invoking every distribution-provided daily job:

```sh
docker compose -p catalog -f "$COMPOSE" exec -T scheduler /etc/cron.daily/daily_catalog_tasks </dev/null
```

The daily task writes to `cron.log` rather than the terminal and can take several minutes with production-like data.
Watch it from another SSH session with `tail -f /var/lib/comses-catalog/shared/logs/cron.log` and wait for the first command to exit successfully.

### Rollback and recovery

`make rollback` activates the immediately previous successful release from the same host and restores its recorded search aliases.
It never copies state from the other host and never reverses database migrations.
Rollback fails closed if its Compose file or release bundle has changed.

Deployment and rollback use a durable transaction journal.
If activation fails, the controller reconciles and health-checks the prior runtime before reporting recovery success.
If recovery cannot be verified, the journal remains and normal mutations stop.

#### Interrupted commands

After an SSH interruption, first determine whether the controller is still running:

```sh
pgrep -af 'scripts/catalogctl.py' || true
make status
```

If a controller process is still running, wait for it and do not start another lifecycle command.
If no process is running and a candidate remains, inspect its recorded stages:

```sh
python3 -m json.tool /var/lib/comses-catalog/state/candidate.json
```

Continue with the next documented command when the interrupted stage records `succeeded`.
Use `candidate-retry` only when the stage is `failed`, or remains `in_progress` with no controller process after the underlying problem has been investigated.

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
Do not run `make rollback` on a first deployment because no prior active release exists.

### Image registry, releases, tags, and footer

The intended registry is `ghcr.io/comses/catalog`.
The CI SHA tag is only a discovery name.
Every host command uses the digest from the handoff, never the tag.

The normal process does not create RC tags.
After staging approval, the required final tag creates a GitHub Release, attaches its handoff, and aliases the existing image digest without rebuilding it.
Production continues to use the image digest and bundle revision from the original `main` handoff.

The application footer comes from `release-version.txt`, generated by `git describe` during the original SHA image build.
Adding a registry alias later does not alter that immutable image or its footer.

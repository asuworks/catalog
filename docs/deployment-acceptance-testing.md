# Deployment acceptance testing

This guide validates the release machinery on a personal fork and two disposable VMs.
It is not the procedure for a real release.
Use [deployment-runbook.md](deployment-runbook.md) for official CoMSES releases and real hosts.

Run this guide when initially accepting the deployment system or after material changes to CI publication, candidate validation, restore, backup, activation, or rollback.
Do not repeat it for every application release when the deployment machinery is unchanged.
Never run the rollback or failure checks against real staging or production.

Stop at the first unexplained failure.

## Prerequisites

The workstation and both disposable VMs need Git, GNU Make, Python 3.10 or newer, `xz`, Docker Engine, and the Docker Compose plugin.
The workstation also needs the GitHub CLI (`gh`), OpenSSH (`ssh` and `scp`), OpenSSL, and `socat`.
Each VM must use systemd for the backup service and timer.
The operator must be able to use Docker without `sudo`.

Verify shared tools on every machine:

```sh
git --version
make --version
python3 --version
xz --version
docker info
docker compose version
```

Verify workstation tools and authentication:

```sh
gh auth status
command -v ssh scp
openssl version
socat -V
gh auth setup-git
```

Set the test values on the workstation:

```sh
export SOURCE_OWNER=asuworks
export CATALOG_SOURCE_REMOTE=fork
export CITATION_SOURCE_REMOTE=fork
export CATALOG_BRANCH=release/candidate-deployment
export CITATION_BRANCH=release/catalog-integration
export DUMP=/absolute/path/catalog.sql.xz
export STAGING_SSH='operator@disposable-staging-host'
export PROD_SSH='operator@disposable-production-host'
```

Change the owner, branch names, dump path, and hostnames as needed.
The owner must be a personal fork, never `comses`:

```sh
test "$SOURCE_OWNER" != comses
```

## 1. Verify locally

Start from the intended Catalog revision and its pinned Citation revision:

```sh
bash scripts/checkout-citation.sh "$SOURCE_OWNER"
git status --short
git -C citation status --short
```

Both status commands must be empty before publishing the candidate.

Build and run the development application:

```sh
make bootstrap
make cite-config
docker compose build --pull django
make up
```

Run every automated gate:

```sh
make check
make migrations-check
make cite-check
make cite-migrations-check
make test-all
make deploy-controller-test
```

Restore the representative dump into the disposable local database and record a baseline:

```sh
make dev-restore DUMP="$DUMP" CONFIRM=comses_catalog
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
4. Change a reviewed publication to `UNREVIEWED` and confirm it leaves public search but remains in curator search.
5. Restore it to `REVIEWED` and confirm it returns to public search.

## 2. Publish a fork candidate

This section proves that pull requests test without publishing and that a fork `main` build publishes an immutable handoff.
It deliberately updates the personal fork's `main` branch.

Confirm both repositories are clean and point to the intended personal fork:

```sh
test "$(git branch --show-current)" = "$CATALOG_BRANCH"
test "$(git -C citation branch --show-current)" = "$CITATION_BRANCH"
test -z "$(git status --porcelain)"
test -z "$(git -C citation status --porcelain)"
git remote get-url --push "$CATALOG_SOURCE_REMOTE" | \
  grep -Eq "^(https://github\\.com/|git@github\\.com:)${SOURCE_OWNER}/catalog\\.git$"
git -C citation remote get-url --push "$CITATION_SOURCE_REMOTE" | \
  grep -Eq "^(https://github\\.com/|git@github\\.com:)${SOURCE_OWNER}/citation\\.git$"
```

Push Citation first so Catalog CI can fetch the exact gitlink commit:

```sh
CITATION_SHA="$(git -C citation rev-parse HEAD)"
CATALOG_SHA="$(git rev-parse HEAD)"
export CITATION_SHA CATALOG_SHA
test "$(git rev-parse HEAD:citation)" = "$CITATION_SHA"
git -C citation push --set-upstream "$CITATION_SOURCE_REMOTE" \
  "HEAD:refs/heads/${CITATION_BRANCH}"
test "$(git -C citation ls-remote "$CITATION_SOURCE_REMOTE" "refs/heads/${CITATION_BRANCH}" | cut -f1)" = "$CITATION_SHA"
```

Open or reuse the Citation fork pull request and wait for its checks:

```sh
CITATION_PR_URL="$(gh pr view "$CITATION_BRANCH" \
  --repo "${SOURCE_OWNER}/citation" --json url --jq .url 2>/dev/null || true)"
if [ -z "$CITATION_PR_URL" ]; then
  CITATION_PR_URL="$(gh pr create \
    --repo "${SOURCE_OWNER}/citation" \
    --base main \
    --head "$CITATION_BRANCH" \
    --title 'Test Citation release integration' \
    --body 'Fork-only validation of the Citation changes required by Catalog.')"
fi
export CITATION_PR_URL
gh pr checks "$CITATION_PR_URL" \
  --repo "${SOURCE_OWNER}/citation" \
  --watch --interval 10
```

Push Catalog and open or reuse its fork pull request:

```sh
git push --set-upstream "$CATALOG_SOURCE_REMOTE" \
  "HEAD:refs/heads/${CATALOG_BRANCH}"
CATALOG_PR_URL="$(gh pr view "$CATALOG_BRANCH" \
  --repo "${SOURCE_OWNER}/catalog" --json url --jq .url 2>/dev/null || true)"
if [ -z "$CATALOG_PR_URL" ]; then
  CATALOG_PR_URL="$(gh pr create \
    --repo "${SOURCE_OWNER}/catalog" \
    --base main \
    --head "$CATALOG_BRANCH" \
    --title 'Test Catalog release candidate' \
    --body 'Fork-only validation of the Catalog and Citation release candidate.')"
fi
export CATALOG_PR_URL
gh pr checks "$CATALOG_PR_URL" \
  --repo "${SOURCE_OWNER}/catalog" \
  --watch --interval 10
```

Confirm that the pull-request workflow skipped registry login, publication, and handoff upload:

```sh
PR_RUN_ID="$(gh run list \
  --repo "${SOURCE_OWNER}/catalog" \
  --workflow docker-build.yml \
  --event pull_request \
  --branch "$CATALOG_BRANCH" \
  --limit 10 \
  --json databaseId,headSha \
  --jq "map(select(.headSha == \"${CATALOG_SHA}\"))[0].databaseId // empty")"
export PR_RUN_ID
test -n "$PR_RUN_ID"
gh run view "$PR_RUN_ID" \
  --repo "${SOURCE_OWNER}/catalog" \
  --json jobs \
  --jq '.jobs[].steps[] | select(.name == "Log in to GHCR" or .name == "Publish immutable candidate and handoff" or .name == "Upload release handoff") | [.name, .conclusion] | @tsv'
```

All three listed conclusions must be `skipped`.

After the checks pass, fast-forward the fork's `main` branch to the exact tested Catalog commit:

```sh
test "$SOURCE_OWNER" != comses
git fetch "$CATALOG_SOURCE_REMOTE"
git merge-base --is-ancestor "${CATALOG_SOURCE_REMOTE}/main" "$CATALOG_SHA"
test "$(git rev-parse HEAD)" = "$CATALOG_SHA"
git push "$CATALOG_SOURCE_REMOTE" "HEAD:refs/heads/main"
test "$(git ls-remote "$CATALOG_SOURCE_REMOTE" refs/heads/main | cut -f1)" = "$CATALOG_SHA"
```

Wait for the matching fork `main` workflow:

```sh
export MAIN_RUN_ID=
for _attempt in $(seq 1 24); do
  MAIN_RUN_ID="$(gh run list \
    --repo "${SOURCE_OWNER}/catalog" \
    --workflow docker-build.yml \
    --branch main \
    --limit 10 \
    --json databaseId,headSha \
    --jq "map(select(.headSha == \"${CATALOG_SHA}\"))[0].databaseId // empty")"
  [ -z "$MAIN_RUN_ID" ] || break
  sleep 5
done
test -n "$MAIN_RUN_ID"
gh run watch "$MAIN_RUN_ID" --repo "${SOURCE_OWNER}/catalog" --exit-status
```

Download and validate the handoff:

```sh
mkdir -p "$PWD/private/release-handoffs"
HANDOFF_DIR="$(mktemp -d "$PWD/private/release-handoffs/${CATALOG_SHA}.XXXXXX")"
gh run download "$MAIN_RUN_ID" \
  --repo "${SOURCE_OWNER}/catalog" \
  --name "catalog-candidate-${CATALOG_SHA}" \
  --dir "$HANDOFF_DIR"
HANDOFF="${HANDOFF_DIR}/release-handoff.json"
IMAGE="$(python3 -c 'import json, pathlib, sys; print(json.loads(pathlib.Path(sys.argv[1]).read_text())["image"])' "$HANDOFF")"
BUNDLE_REVISION="$(python3 -c 'import json, pathlib, sys; print(json.loads(pathlib.Path(sys.argv[1]).read_text())["bundle_revision"])' "$HANDOFF")"
HANDOFF_CITATION_REVISION="$(python3 -c 'import json, pathlib, sys; print(json.loads(pathlib.Path(sys.argv[1]).read_text())["citation_revision"])' "$HANDOFF")"
export HANDOFF IMAGE BUNDLE_REVISION HANDOFF_CITATION_REVISION
cat "$HANDOFF"
test "$BUNDLE_REVISION" = "$CATALOG_SHA"
test "$HANDOFF_CITATION_REVISION" = "$CITATION_SHA"
```

Make the personal-fork package public after its first publication so disposable hosts can pull it without credentials.
Do not create an RC tag for this rehearsal.

```sh
printf 'https://github.com/users/%s/packages/container/catalog/settings\n' "$SOURCE_OWNER"
```

After changing visibility, confirm that the exact digest is readable:

```sh
docker manifest inspect "$IMAGE" >/dev/null
```

## 3. Validate a fresh staging host

Create the dump checksum and transfer the candidate inputs from the workstation:

```sh
DUMP_NAME="$(basename "$DUMP")"
export DUMP_NAME
test -s "$DUMP"
(
  cd "$(dirname "$DUMP")" || exit 1
  sha256sum "$DUMP_NAME" > "${DUMP_NAME}.sha256"
)
scp "$HANDOFF" "${STAGING_SSH}:release-handoff.json"
scp "$DUMP" "${DUMP}.sha256" "${STAGING_SSH}:"
printf '%s\n' "$DUMP_NAME" | ssh "$STAGING_SSH" 'cat > "$HOME/.comses-catalog-dump-name"'
printf '%s\n' "$SOURCE_OWNER" | ssh "$STAGING_SSH" 'cat > "$HOME/.comses-catalog-github-owner"'
ssh "$STAGING_SSH"
```

Run the remaining commands inside the disposable staging VM:

```sh
SOURCE_OWNER="$(cat "$HOME/.comses-catalog-github-owner")"
export HANDOFF="$HOME/release-handoff.json"
export DUMP="$HOME/$(cat "$HOME/.comses-catalog-dump-name")"
IMAGE="$(python3 -c 'import json, pathlib, sys; print(json.loads(pathlib.Path(sys.argv[1]).read_text())["image"])' "$HANDOFF")"
BUNDLE_REVISION="$(python3 -c 'import json, pathlib, sys; print(json.loads(pathlib.Path(sys.argv[1]).read_text())["bundle_revision"])' "$HANDOFF")"
export IMAGE BUNDLE_REVISION
chmod 0600 "$HANDOFF" "$DUMP" "${DUMP}.sha256"
(
  cd "$(dirname "$DUMP")" || exit 1
  sha256sum --check "$(basename "$DUMP").sha256"
)
git clone "https://github.com/${SOURCE_OWNER}/catalog.git" "$HOME/catalog"
cd "$HOME/catalog" || exit 1
git checkout "$BUNDLE_REVISION"
bash scripts/checkout-citation.sh "$SOURCE_OWNER"
sudo make host-provision HOST_ID=staging OPERATOR="$USER"
"${EDITOR:-vi}" /etc/comses-catalog/secrets/config.ini
make host-check
make candidate IMAGE="$IMAGE" BUNDLE_REVISION="$BUNDLE_REVISION"
make restore DUMP="$DUMP" CONFIRM=comses_catalog
CONFIRM_SCHEMA_MIGRATION=1 make schema-migrate
make data-rebuild
make deploy
make backup
make status
make release-report
```

Leave staging SMTP credentials empty.
Confirm that the release report uses `django.core.mail.backends.filebased.EmailBackend` and that database and search counts match the local baseline.

Verify scheduler discovery and the installed backup service:

```sh
COMPOSE=/var/lib/comses-catalog/runtime/docker-compose.yml
docker compose -p catalog -f "$COMPOSE" exec -T scheduler run-parts --test /etc/cron.daily </dev/null
docker compose -p catalog -f "$COMPOSE" exec -T scheduler run-parts --test /etc/cron.monthly </dev/null
docker compose -p catalog -f "$COMPOSE" exec -T scheduler /etc/cron.daily/daily_catalog_tasks </dev/null
tail -n 100 /var/lib/comses-catalog/shared/logs/cron.log
systemctl list-timers comses-catalog-backup.timer
sudo systemctl start comses-catalog-backup.service
test "$(systemctl show comses-catalog-backup.service -p Result --value)" = success
sudo journalctl -u comses-catalog-backup.service -n 50 --no-pager
make release-report
```

The daily task can take several minutes and writes progress only to `cron.log`.
Watch that file from a second SSH session and wait for the task command to exit successfully.

Confirm that the latest backup has `"scheduled": true`.

Use normal TLS ingress when available.
For a disposable VM without DNS, open an SSH tunnel from the workstation:

```sh
ssh -N -L 8080:127.0.0.1:80 "$STAGING_SSH"
```

In another workstation terminal, provide a temporary TLS endpoint:

```sh
openssl req -x509 -newkey rsa:2048 -nodes -days 1 \
  -keyout /tmp/catalog-test.key -out /tmp/catalog-test.crt \
  -subj '/CN=staging-catalog.comses.net' \
  -addext 'subjectAltName=DNS:staging-catalog.comses.net'
grep -Eq '(^|[[:space:]])staging-catalog\.comses\.net([[:space:]]|$)' /etc/hosts || \
  printf '127.0.0.1 staging-catalog.comses.net\n' | sudo tee -a /etc/hosts
socat OPENSSL-LISTEN:8443,reuseaddr,fork,verify=0,cert=/tmp/catalog-test.crt,key=/tmp/catalog-test.key \
  TCP:127.0.0.1:8080
```

Browse <https://staging-catalog.comses.net:8443>, accept the temporary certificate, and repeat the five browser checks from local validation.

## 4. Validate a separate fresh production host

Transfer the same handoff and representative dump to the second disposable VM:

```sh
scp "$HANDOFF" "${PROD_SSH}:release-handoff.json"
scp "$DUMP" "${DUMP}.sha256" "${PROD_SSH}:"
printf '%s\n' "$DUMP_NAME" | ssh "$PROD_SSH" 'cat > "$HOME/.comses-catalog-dump-name"'
printf '%s\n' "$SOURCE_OWNER" | ssh "$PROD_SSH" 'cat > "$HOME/.comses-catalog-github-owner"'
ssh "$PROD_SSH"
```

Run the remaining commands inside the disposable production VM:

```sh
SOURCE_OWNER="$(cat "$HOME/.comses-catalog-github-owner")"
export HANDOFF="$HOME/release-handoff.json"
export DUMP="$HOME/$(cat "$HOME/.comses-catalog-dump-name")"
IMAGE="$(python3 -c 'import json, pathlib, sys; print(json.loads(pathlib.Path(sys.argv[1]).read_text())["image"])' "$HANDOFF")"
BUNDLE_REVISION="$(python3 -c 'import json, pathlib, sys; print(json.loads(pathlib.Path(sys.argv[1]).read_text())["bundle_revision"])' "$HANDOFF")"
export IMAGE BUNDLE_REVISION
chmod 0600 "$HANDOFF" "$DUMP" "${DUMP}.sha256"
(
  cd "$(dirname "$DUMP")" || exit 1
  sha256sum --check "$(basename "$DUMP").sha256"
)
git clone "https://github.com/${SOURCE_OWNER}/catalog.git" "$HOME/catalog"
cd "$HOME/catalog" || exit 1
git checkout "$BUNDLE_REVISION"
bash scripts/checkout-citation.sh "$SOURCE_OWNER"
sudo make host-provision HOST_ID=prod OPERATOR="$USER"
"${EDITOR:-vi}" /etc/comses-catalog/secrets/config.ini
make host-check
make candidate IMAGE="$IMAGE" BUNDLE_REVISION="$BUNDLE_REVISION"
make restore DUMP="$DUMP" CONFIRM=comses_catalog
CONFIRM_SCHEMA_MIGRATION=1 make schema-migrate
make data-rebuild
make deploy
make backup
make status
make release-report
```

Use non-working test SMTP values because this is a disposable simulation, and do not exercise external email delivery.
Confirm that the report identifies the SMTP email backend and contains the same image digest and bundle revision as staging.

Run the scheduler and backup checks independently:

```sh
COMPOSE=/var/lib/comses-catalog/runtime/docker-compose.yml
docker compose -p catalog -f "$COMPOSE" exec -T scheduler run-parts --test /etc/cron.daily </dev/null
docker compose -p catalog -f "$COMPOSE" exec -T scheduler run-parts --test /etc/cron.monthly </dev/null
docker compose -p catalog -f "$COMPOSE" exec -T scheduler /etc/cron.daily/daily_catalog_tasks </dev/null
tail -n 100 /var/lib/comses-catalog/shared/logs/cron.log
systemctl list-timers comses-catalog-backup.timer
sudo systemctl start comses-catalog-backup.service
test "$(systemctl show comses-catalog-backup.service -p Result --value)" = success
sudo journalctl -u comses-catalog-backup.service -n 50 --no-pager
make release-report
exit
```

Open a different tunnel from the workstation:

```sh
ssh -N -L 8081:127.0.0.1:80 "$PROD_SSH"
```

In another workstation terminal, provide the production-hostname TLS endpoint:

```sh
openssl req -x509 -newkey rsa:2048 -nodes -days 1 \
  -keyout /tmp/catalog-prod-test.key -out /tmp/catalog-prod-test.crt \
  -subj '/CN=catalog.comses.net' \
  -addext 'subjectAltName=DNS:catalog.comses.net'
grep -Eq '(^|[[:space:]])catalog\.comses\.net([[:space:]]|$)' /etc/hosts || \
  printf '127.0.0.1 catalog.comses.net\n' | sudo tee -a /etc/hosts
socat OPENSSL-LISTEN:8444,reuseaddr,fork,verify=0,cert=/tmp/catalog-prod-test.crt,key=/tmp/catalog-prod-test.key \
  TCP:127.0.0.1:8081
```

Browse <https://catalog.comses.net:8444>, accept the temporary certificate, and repeat the five browser checks from local validation.

## 5. Validate a subsequent release and rollback

Before publishing another candidate, preserve the first handoff values on the workstation:

```sh
export HANDOFF_1="$HANDOFF"
export IMAGE_1="$IMAGE"
export BUNDLE_REVISION_1="$BUNDLE_REVISION"
```

Publish a later clean Catalog commit through Sections 1 and 2.

From the workstation, transfer the second handoff to both disposable VMs:

```sh
export HANDOFF_2="$HANDOFF"
export IMAGE_2="$IMAGE"
export BUNDLE_REVISION_2="$BUNDLE_REVISION"
test "$BUNDLE_REVISION_2" != "$BUNDLE_REVISION_1"
scp "$HANDOFF_2" "${STAGING_SSH}:release-handoff-2.json"
scp "$HANDOFF_2" "${PROD_SSH}:release-handoff-2.json"
```

Connect to one disposable VM, enter its Catalog checkout, and load the second handoff:

```sh
ssh "$STAGING_SSH"
cd "$HOME/catalog" || exit 1
SOURCE_OWNER="$(cat "$HOME/.comses-catalog-github-owner")"
export HANDOFF_2="$HOME/release-handoff-2.json"
IMAGE_2="$(python3 -c 'import json, pathlib, sys; print(json.loads(pathlib.Path(sys.argv[1]).read_text())["image"])' "$HANDOFF_2")"
BUNDLE_REVISION_2="$(python3 -c 'import json, pathlib, sys; print(json.loads(pathlib.Path(sys.argv[1]).read_text())["bundle_revision"])' "$HANDOFF_2")"
export SOURCE_OWNER IMAGE_2 BUNDLE_REVISION_2
git fetch "https://github.com/${SOURCE_OWNER}/catalog.git" main
git cat-file -e "${BUNDLE_REVISION_2}^{commit}"
```

Back up the first release, check out the second bundle, and run the rollback test:

```sh
make backup
git checkout "$BUNDLE_REVISION_2"
bash scripts/checkout-citation.sh "$SOURCE_OWNER"
HOST_ID="$(sed -n 's/^CATALOG_HOST_ID=//p' /etc/comses-catalog/host.env)"
sudo make host-provision HOST_ID="$HOST_ID" OPERATOR="$USER"
make host-check
make candidate IMAGE="$IMAGE_2" BUNDLE_REVISION="$BUNDLE_REVISION_2"
if CONFIRM_SCHEMA_MIGRATION=0 make schema-migrate; then
  echo 'ERROR: schema-migrate accepted missing confirmation' >&2
  exit 1
fi
CONFIRM_SCHEMA_MIGRATION=1 make schema-migrate
make data-rebuild
make deploy
make backup
make rollback
make status
make rollback
make status
make release-report
```

The first rollback must activate the first handoff.
The second must return to the second handoff.
Run a browser smoke test after the second rollback.
Exit, connect to `PROD_SSH`, and repeat the same VM command blocks independently there.

## 6. Validate fail-closed behavior

Run these only on a disposable VM:

```sh
FAILURE_TEST_REVISION="$(git rev-parse HEAD)"
if make candidate IMAGE='ghcr.io/example/catalog:latest' BUNDLE_REVISION="$FAILURE_TEST_REVISION"; then
  echo 'ERROR: candidate accepted a mutable tag' >&2
  exit 1
fi
if make restore DUMP=/missing/catalog.sql.xz CONFIRM=comses_catalog; then
  echo 'ERROR: restore accepted a missing dump' >&2
  exit 1
fi
if make restore DUMP="$HOME/catalog.sql.xz" CONFIRM=comses_catalog; then
  echo 'ERROR: restore accepted an active host' >&2
  exit 1
fi
```

Do not run `make rollback` on a first deployment because no prior active release exists.
The automated controller tests cover modified release artifacts and activation failures without damaging a deployed VM.

Acceptance passes only when automated checks, fork CI behavior, dump counts, independent host state, search rebuilds, backup execution, scheduler discovery, rollback, and failure checks all behave as expected.

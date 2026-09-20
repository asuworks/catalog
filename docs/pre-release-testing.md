# Pre-release testing and deployment

This is the standalone checklist for validating locally, testing the fork workflows, deploying a fresh staging VM, and deploying a separate fresh production VM.
Run it in order and stop at the first unexplained failure.

Set these placeholders once:

```sh
export CATALOG_REPOSITORY=https://github.com/asuworks/catalog.git
export CITATION_OWNER=asuworks
export DUMP=/absolute/path/catalog.sql.xz
```

## 1. Verify locally

Start from the intended clean Catalog revision and its pinned Citation revision:

```sh
bash scripts/checkout-citation.sh "$CITATION_OWNER"
git status --short
git -C citation status --short
```

Both status commands must be empty before a release build.

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

Restore the dump into the disposable local database:

```sh
make dev-restore DUMP="$DUMP" CONFIRM=comses_catalog
```

Record a data baseline:

```sh
docker compose exec -T django python3 manage.py shell -c \
"from citation.models import Publication; print('all=', Publication.objects.count()); print('primary=', Publication.api.primary().count()); print('public=', Publication.api.primary().reviewed().count())"
```

Create a local curator account if needed:

```sh
docker compose exec django python3 manage.py createsuperuser
```

Open <http://localhost:8000> and verify:

1. The home page, one publication page, `/publications/?search=CORMAS`, a zero-result search, and `/visualization/?search=CORMAS` render with the footer.
2. `/curator/export/` has one row per primary publication, keeps multiple authors in one CSV cell, and leaves later columns aligned.
3. `/merges/` and `/merges/create/` load.
4. In a curator publication page, change a reviewed publication to `UNREVIEWED`, save it, and confirm it disappears from public search but remains in curator search.
5. Restore that publication to `REVIEWED` and confirm it returns to public search.

The status round trip is the key Catalog and Citation integration check.

## 2. Test the fork workflows

Push the Citation commit to `asuworks/citation` before pushing the Catalog gitlink.
Open a pull request against the fork's `main` branch to verify that pull requests build and test without publishing an image.

After that passes, put the candidate commit on the fork's `main` branch.
The `CoMSES Docker CI` workflow must pass and publish:

- `ghcr.io/asuworks/catalog:sha-<40-char-catalog-sha>`;
- a `catalog-candidate-<sha>` artifact containing `release-handoff.json`.

After the first publication, set the fork GHCR package visibility to public.
Download the handoff and export its two deployment inputs:

```sh
export IMAGE='ghcr.io/asuworks/catalog@sha256:<digest-from-handoff>'
export BUNDLE_REVISION='<bundle_revision-from-handoff>'
```

Verify the release-tag workflow without rebuilding:

```sh
git tag -a v2026.09-rc.1 "$BUNDLE_REVISION" -m 'Catalog 2026.09 release candidate 1'
git push fork v2026.09-rc.1
```

The workflow must report the same digest as the main-build handoff.
Delete the fork-only test tag after validation if it should not be retained.

## 3. Deploy the staging VM

On the staging VM, clone and check out the exact bundle:

```sh
git clone "$CATALOG_REPOSITORY" catalog
cd catalog
git checkout "$BUNDLE_REVISION"
bash scripts/checkout-citation.sh "$CITATION_OWNER"
sudo make host-provision HOST_ID=staging OPERATOR="$USER"
$EDITOR /etc/comses-catalog/secrets/config.ini
make host-check
```

Leave staging SMTP credentials empty.
The staging settings save email to disk instead of sending it.

Transfer the dump to the VM, set mode `0600`, and run:

```sh
make candidate IMAGE="$IMAGE" BUNDLE_REVISION="$BUNDLE_REVISION"
make restore DUMP=/absolute/path/catalog.sql.xz CONFIRM=comses_catalog
CONFIRM_SCHEMA_MIGRATION=1 make schema-migrate
make data-rebuild
make deploy
make backup
make status
make release-report
```

Compare the report's total and primary publication counts with the local baseline.
Confirm the public and curator search counts reported by `data-rebuild` are plausible for the same dump.

Use the normal TLS ingress when it is available.
For a throwaway VM with no public DNS, first tunnel its loopback Nginx port:

```sh
ssh -N -L 8080:127.0.0.1:80 <staging-user>@<staging-host>
```

In another local terminal, wrap the tunnel in temporary TLS with OpenSSL and `socat`:

```sh
openssl req -x509 -newkey rsa:2048 -nodes -days 1 \
  -keyout /tmp/catalog-test.key -out /tmp/catalog-test.crt \
  -subj '/CN=staging-catalog.comses.net' \
  -addext 'subjectAltName=DNS:staging-catalog.comses.net'
socat OPENSSL-LISTEN:8443,reuseaddr,fork,verify=0,cert=/tmp/catalog-test.crt,key=/tmp/catalog-test.key \
  TCP:127.0.0.1:8080
```

Map `staging-catalog.comses.net` to `127.0.0.1` on the workstation, browse `https://staging-catalog.comses.net:8443`, and accept the temporary certificate.
TLS matters because staging deliberately keeps production's secure cookie and request behavior.
Repeat the five browser checks from local validation.

Verify operational services:

```sh
COMPOSE=/var/lib/comses-catalog/runtime/docker-compose.yml
docker compose -p catalog -f "$COMPOSE" ps
docker compose -p catalog -f "$COMPOSE" exec -T scheduler run-parts --test /etc/cron.daily
docker compose -p catalog -f "$COMPOSE" exec -T scheduler run-parts --test /etc/cron.monthly
docker compose -p catalog -f "$COMPOSE" exec -T scheduler run-parts /etc/cron.daily
tail -n 100 /var/lib/comses-catalog/shared/logs/cron.log
systemctl list-timers comses-catalog-backup.timer
sudo systemctl start comses-catalog-backup.service
```

Run `make release-report` again and confirm its email backend is `django.core.mail.backends.filebased.EmailBackend`.
Inspect `/var/lib/comses-catalog/shared/mail` if a QA action generated email.

Record human approval for the exact `IMAGE` digest and `BUNDLE_REVISION`.

## 4. Deploy the production VM

Use a different VM and repeat checkout and provisioning with `HOST_ID=prod`:

```sh
git clone "$CATALOG_REPOSITORY" catalog
cd catalog
git checkout "$BUNDLE_REVISION"
bash scripts/checkout-citation.sh "$CITATION_OWNER"
sudo make host-provision HOST_ID=prod OPERATOR="$USER"
$EDITOR /etc/comses-catalog/secrets/config.ini
make host-check
```

For a real production deployment, configure working SMTP credentials.
For a throwaway production simulation, use non-working test credentials and do not exercise email delivery.

Freeze writes on the old production system and create the final dump.
Transfer that dump to the new VM, then deploy the staging-approved pair:

```sh
make candidate IMAGE="$IMAGE" BUNDLE_REVISION="$BUNDLE_REVISION"
make restore DUMP=/absolute/path/catalog.sql.xz CONFIRM=comses_catalog
CONFIRM_SCHEMA_MIGRATION=1 make schema-migrate
make data-rebuild
make deploy
make backup
make status
make release-report
```

Compare database and search counts with the source dump and staging expectations.
Repeat the browser and scheduler checks using `catalog.comses.net` as the Host name.
For a throwaway VM, regenerate the temporary certificate with `catalog.comses.net` as both the common name and subject alternative name.
Confirm the report uses the SMTP email backend and the exact same image digest and bundle revision as staging.

Switch ingress only after these checks pass.
The old production VM remains a fallback only until the new database accepts writes.

## 5. Test a second candidate and rollback

Keep the first handoff, then publish one later clean commit through the same fork workflow and record it as `IMAGE_2` and `BUNDLE_REVISION_2`.
On each VM independently, run:

```sh
export HOST_ID=staging  # Use prod on the production VM.
make backup
git checkout "$BUNDLE_REVISION_2"
bash scripts/checkout-citation.sh "$CITATION_OWNER"
sudo make host-provision HOST_ID="$HOST_ID" OPERATOR="$USER"
make host-check
make candidate IMAGE="$IMAGE_2" BUNDLE_REVISION="$BUNDLE_REVISION_2"
CONFIRM_SCHEMA_MIGRATION=1 make schema-migrate
make data-rebuild
make deploy
make rollback
make status
make rollback
make status
```

The first rollback must activate the first handoff.
The second must return to the second handoff.
Run `make release-report` and a browser smoke test after the second rollback.

## 6. Failure checks

Before calling the release ready, confirm these fail closed:

```sh
make candidate IMAGE='ghcr.io/asuworks/catalog:latest' BUNDLE_REVISION="$BUNDLE_REVISION"
CONFIRM_SCHEMA_MIGRATION=0 make schema-migrate
make restore DUMP=/missing/catalog.sql.xz CONFIRM=comses_catalog
```

Do not run `make rollback` on a first deployment because no prior active release exists.
The controller test suite covers modified release artifacts and activation failures without damaging a VM deployment.

Release only when automated checks, dump counts, staging QA, production simulation, backup execution, scheduler discovery, and failure checks all pass without unexplained errors.

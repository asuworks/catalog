# catalog

[![Catalog Docker CI](https://github.com/comses/catalog/actions/workflows/docker-build.yml/badge.svg)](https://github.com/comses/catalog/actions/workflows/docker-build.yml)

Catalog provides web tools for annotating and managing publications that reference computational artifacts.
It is developed by [CoMSES Net](https://www.comses.net), and uses the [Citation](https://github.com/comses/citation) package as a Git submodule.

## Community support needed

This project needs maintainers to help with dependency upgrades, deployment upkeep, and the companion Citation package.
Please contact CoMSES Net if you are interested in contributing.

## Local development

Install current Docker Engine and Docker Compose v2, then run:

```sh
git clone --recurse-submodules https://github.com/comses/catalog.git
cd catalog
make bootstrap
make up
```

Open <http://localhost:8000>.

To replace the disposable local database from a dump:

```sh
make dev-restore DUMP=./catalog.sql.xz CONFIRM=comses_catalog
```

Run the release checks with:

```sh
make check
make migrations-check
make cite-check
make cite-migrations-check
make test-all
make deploy-controller-test
```

## Deployment

Staging and production run on separate Docker hosts.
CI publishes one application image, and both hosts deploy the exact same `name@sha256:<digest>` candidate independently.
Tags are never accepted as deployment input.

Follow [the release and deployment runbook](docs/deployment-runbook.md) for regular releases, first deployments, rollback, and recovery.
Use [the deployment acceptance guide](docs/deployment-acceptance-testing.md) only to validate the release machinery on personal forks and disposable hosts.

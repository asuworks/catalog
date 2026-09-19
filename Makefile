SHELL := /bin/bash

COMPOSE ?= docker compose
FORCE ?= 0
DEV_OVERRIDE ?= 0
ALLOW_DIRTY_BUILD ?= 0
CATALOG_COMPOSE = $(COMPOSE) --project-directory . -p catalog -f docker-compose.yml

.DEFAULT_GOAL := help

.PHONY: help compose-dev compose-staging compose-prod config-generate config-validate bootstrap up down logs shell clean migrations-check test check test-all cite-config cite-build cite-up cite-down cite-test cite-check cite-migrations-check cite-format cite-lock cite-publish image-build image-push release-version deploy schema-migrate rollback status start stop backup restore search-rebuild search-validate

help:
	@printf '%s\n' \
		'Catalog commands:' \
		'  bootstrap              Render development Compose config and create credentials' \
		'  up | down | logs       Manage the local development stack' \
		'  shell                  Open a shell in the Django service' \
		'  check                  Run Django system checks' \
		'  migrations-check       Verify committed Django migrations' \
		'  test                   Run the catalog test suite' \
		'  test-all               Run catalog and citation test suites' \
		'  config-generate        Create credentials; use FORCE=1 to rotate existing files' \
		'  config-validate        Verify local credential files exist and are nonempty' \
		'  cite-<target>          Delegate config, build, up, down, test, check, migrations-check, format, lock, or publish to citation/' \
		'' \
		'Release commands (single-host Docker Compose, project "catalog";' \
		'  ENV=staging|prod for deploy; new deploys require image + ES host;' \
		'  existing release state supplies omitted deploy values for promotion):' \
		'  image-build | image-push (build requires a clean worktree)' \
		'  deploy | rollback | status | start | stop' \
		'  schema-migrate        Explicit confirmed database migration (no automatic migrations)' \
		'                         requires image, ES host, and CONFIRM_PRODUCTION_MIGRATION=1' \
		'  backup | restore | release-version' \
		'  search-rebuild | search-validate'

compose-dev:
	DEV_OVERRIDE=$(DEV_OVERRIDE) bash scripts/deploy.sh dev-compose

compose-staging:
	bash scripts/compose.sh staging

compose-prod:
	bash scripts/compose.sh prod

config-generate:
	FORCE=$(FORCE) bash scripts/config.sh generate

config-validate:
	bash scripts/config.sh validate

bootstrap: compose-dev config-generate

up: compose-dev config-validate
	$(CATALOG_COMPOSE) up -d

down: compose-dev
	$(CATALOG_COMPOSE) down --remove-orphans

logs:
	bash scripts/deploy.sh logs

shell: compose-dev
	$(CATALOG_COMPOSE) exec django bash

clean: compose-dev
	$(CATALOG_COMPOSE) down --volumes --remove-orphans

migrations-check: compose-dev config-validate
	$(COMPOSE) run --rm django python3 manage.py makemigrations --check --dry-run

test: compose-dev config-validate
	$(COMPOSE) run --rm django /code/deploy/docker/test.sh

check: compose-dev config-validate
	$(COMPOSE) run --rm django python3 manage.py check

test-all: test cite-config cite-test

cite-config:
	bash scripts/citation-config.sh

cite-build cite-up cite-down cite-test cite-check cite-migrations-check cite-format cite-lock cite-publish:
	$(MAKE) -C citation $(@:cite-%=%)

image-build:
	CATALOG_IMAGE="$(CATALOG_IMAGE)" ALLOW_DIRTY_BUILD="$(ALLOW_DIRTY_BUILD)" bash scripts/deploy.sh build

image-push:
	CATALOG_IMAGE="$(CATALOG_IMAGE)" bash scripts/deploy.sh push

release-version:
	bash scripts/deploy.sh tag

# deploy renders a temporary candidate and publishes docker-compose.yml only
# after `docker compose up -d --wait` succeeds; release metadata lives in
# deploy/state/ and supplies omitted values when promoting a recorded release.
deploy:
	CATALOG_IMAGE="$(CATALOG_IMAGE)" CATALOG_ES_HOST="$(CATALOG_ES_HOST)" bash scripts/deploy.sh deploy "$(ENV)"

schema-migrate:
	CATALOG_IMAGE="$(CATALOG_IMAGE)" CATALOG_ES_HOST="$(CATALOG_ES_HOST)" CONFIRM_PRODUCTION_MIGRATION="$(CONFIRM_PRODUCTION_MIGRATION)" bash scripts/deploy.sh schema-migrate "$(ENV)"

rollback:
	bash scripts/deploy.sh rollback

status:
	bash scripts/deploy.sh status

stop:
	bash scripts/deploy.sh stop

start:
	bash scripts/deploy.sh start

backup:
	BACKUP_DIR="$(BACKUP_DIR)" COMPOSE_FILE="$(COMPOSE_FILE)" bash scripts/database.sh backup

restore:
	DUMP="$(DUMP)" CONFIRM="$(CONFIRM)" COMPOSE_FILE="$(COMPOSE_FILE)" bash scripts/database.sh restore

search-rebuild:
	$(CATALOG_COMPOSE) run --rm --no-deps django python3 manage.py rebuild_es_index

search-validate:
	$(CATALOG_COMPOSE) run --rm --no-deps django python3 manage.py validate_search_indexes

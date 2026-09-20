SHELL := /bin/bash

COMPOSE ?= docker compose
FORCE ?= 0
DEV_OVERRIDE ?= 0
ALLOW_DIRTY_BUILD ?= 0
OPERATOR ?= $(shell id -un)
CATALOG_COMPOSE = $(COMPOSE) --project-directory . -p catalog -f docker-compose.yml
CATALOGCTL = python3 scripts/catalogctl.py

.DEFAULT_GOAL := help

.PHONY: help compose-dev config-generate config-validate bootstrap up down dev-logs dev-restore shell clean migrations-check test check test-all cite-config cite-build cite-up cite-down cite-test cite-check cite-migrations-check cite-format cite-lock cite-publish image-build image-push release-version deploy-controller-test host-provision host-check candidate candidate-retry backup restore schema-migrate data-rebuild deploy rollback recover status release-report start stop logs search-rebuild search-validate

help:
	@printf '%s\n' \
		'Local development:' \
		'  bootstrap              Render Compose and create local credentials' \
		'  up | down | dev-logs   Manage the development stack' \
		'  dev-restore            Replace local data from DUMP (requires CONFIRM=comses_catalog)' \
		'  shell                  Open a shell in the Django service' \
		'  check                  Run Django system checks' \
		'  migrations-check       Verify committed Django migrations' \
		'  test | test-all        Run Catalog or Catalog plus Citation tests' \
		'  config-generate        Create local credentials; FORCE=1 rotates them' \
		'  cite-<target>          Delegate a target to citation/' \
		'' \
		'Build:' \
		'  image-build CATALOG_IMAGE=<name:tag>' \
		'  image-push  CATALOG_IMAGE=<name:tag>' \
		'  release-version' \
		'' \
		'Host setup and release (run from the exact clean release revision):' \
		'  sudo make host-provision HOST_ID=staging|prod OPERATOR=<user>' \
		'  make host-check' \
		'  make candidate IMAGE=<name@sha256:digest> BUNDLE_REVISION=<git-sha>' \
		'  make restore DUMP=<dump> CONFIRM=comses_catalog   # fresh host only' \
		'  make backup                                      # existing host' \
		'  CONFIRM_SCHEMA_MIGRATION=1 make schema-migrate' \
		'  make data-rebuild' \
		'  make deploy' \
		'  CONFIRM_CANDIDATE_RETRY=1 make candidate-retry   # after investigation' \
		'  make status | release-report | rollback | recover' \
		'  make start | stop | logs'

compose-dev:
	DEV_OVERRIDE="$(DEV_OVERRIDE)" bash scripts/deploy.sh dev-compose

config-generate:
	FORCE=$(FORCE) bash scripts/config.sh generate

config-validate:
	bash scripts/config.sh validate

bootstrap: compose-dev config-generate

up: compose-dev config-validate
	$(CATALOG_COMPOSE) up -d

down: compose-dev
	$(CATALOG_COMPOSE) down --remove-orphans

dev-logs: compose-dev
	$(CATALOG_COMPOSE) logs -f

dev-restore: compose-dev config-validate
	DUMP="$(DUMP)" CONFIRM="$(CONFIRM)" bash scripts/dev-restore.sh

shell: compose-dev
	$(CATALOG_COMPOSE) exec django bash

clean: compose-dev
	$(CATALOG_COMPOSE) down --volumes --remove-orphans

migrations-check: compose-dev config-validate
	$(CATALOG_COMPOSE) run --rm django python3 manage.py makemigrations --check --dry-run

test: compose-dev config-validate
	$(CATALOG_COMPOSE) run --rm django /code/deploy/docker/test.sh

check: compose-dev config-validate
	$(CATALOG_COMPOSE) run --rm django python3 manage.py check

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

deploy-controller-test:
	python3 -m unittest discover -s scripts/tests -p 'test_*.py'

host-provision:
	$(CATALOGCTL) host-provision --host-id "$(HOST_ID)" --operator "$(OPERATOR)"

host-check:
	$(CATALOGCTL) host-check

candidate:
	$(CATALOGCTL) candidate --image "$(IMAGE)" --bundle-revision "$(BUNDLE_REVISION)"

candidate-retry:
	$(CATALOGCTL) candidate-retry $(if $(filter 1,$(CONFIRM_CANDIDATE_RETRY)),--confirmed,)

backup:
	$(CATALOGCTL) backup

restore:
	$(CATALOGCTL) restore --dump "$(DUMP)" --confirm "$(CONFIRM)"

schema-migrate:
	$(CATALOGCTL) schema-migrate $(if $(filter 1,$(CONFIRM_SCHEMA_MIGRATION)),--confirmed,)

data-rebuild:
	$(CATALOGCTL) data-rebuild

deploy:
	$(CATALOGCTL) deploy

rollback:
	$(CATALOGCTL) rollback

recover:
	$(CATALOGCTL) recover

status:
	$(CATALOGCTL) status

release-report:
	$(CATALOGCTL) release-report

start:
	$(CATALOGCTL) start

stop:
	$(CATALOGCTL) stop

logs:
	$(CATALOGCTL) logs

search-rebuild:
	$(CATALOG_COMPOSE) run --rm --no-deps django python3 manage.py rebuild_es_index

search-validate:
	$(CATALOG_COMPOSE) run --rm --no-deps django python3 manage.py validate_search_indexes

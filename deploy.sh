#!/usr/bin/env bash
# This compatibility name is intentionally not a command surface.

set -o errexit
set -o nounset
set -o pipefail

cat >&2 <<'EOF'
ERROR: ./deploy.sh is deprecated and no longer executes deployments.

Use the documented Make commands instead:
  make candidate IMAGE=name@sha256:digest BUNDLE_REVISION=git-sha
  make backup | make restore | make schema-migrate | make data-rebuild
  make deploy | make rollback | make recover
  make status | make release-report | make start | make stop | make logs
  make image-build | make image-push | make release-version
EOF
exit 1

#!/usr/bin/env bash

set -o errexit
set -o nounset
set -o pipefail

case "${1:-}" in
    backup)
        exec python3 scripts/catalogctl.py backup
        ;;
    restore)
        exec python3 scripts/catalogctl.py restore \
            --dump "${DUMP:?set DUMP to a .dump, .sql, or .sql.xz file}" \
            --confirm "${CONFIRM:?set CONFIRM=comses_catalog}"
        ;;
    *)
        echo "ERROR: usage: $0 <backup|restore>" >&2
        exit 1
        ;;
esac

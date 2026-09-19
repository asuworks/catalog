#!/usr/bin/env bash

set -o errexit
set -o nounset
set -o pipefail

project_name="${COMPOSE_PROJECT_NAME:-catalog}"
compose_file="${COMPOSE_FILE:-docker-compose.yml}"
# EXIT traps run after function-local variables have left scope.
declare -a stopped_services=()

die() {
    echo "ERROR: $*" >&2
    exit 1
}

compose() {
    docker compose --project-directory . -p "${project_name}" -f "${compose_file}" "$@"
}

require_stack() {
    [[ -s "${compose_file}" ]] || die "missing Compose file ${compose_file}"
    docker info >/dev/null 2>&1 || die "docker daemon is not reachable"
    [[ -n "$(compose ps --status running -q db)" ]] || die "database service is not running"
}

db_value() {
    compose exec -T db sh -c "printf '%s' \"\${$1}\""
}

assert_identifier() {
    [[ "$1" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || die "unsafe PostgreSQL identifier: $1"
}

database_exists() {
    local database_name="$1"
    compose exec -T db psql --username "${db_user}" --dbname postgres --tuples-only --no-align \
        --command "SELECT 1 FROM pg_database WHERE datname = '${database_name}'" \
        | grep -qx 1
}

service_exists() {
    grep -qx "$1" <<< "$(compose config --services)"
}

terminate_database_connections() {
    local database_name="$1"
    compose exec -T db psql --username "${db_user}" --dbname postgres --set ON_ERROR_STOP=1 \
        --command "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '${database_name}' AND pid <> pg_backend_pid()" \
        >/dev/null
}

validate_checksum() {
    local dump_path="$1"
    local checksum_path="${dump_path}.sha256"
    [[ -f "${checksum_path}" ]] || return 0
    (
        cd "$(dirname "${dump_path}")"
        sha256sum --check "$(basename "${checksum_path}")"
    )
}

stream_sql_dump() {
    case "${dump_format}" in
        sql) cat "${dump_path}" ;;
        sql-xz) xz --decompress --stdout "${dump_path}" ;;
        *) die "${dump_path} is not a plain SQL dump" ;;
    esac
}

validate_dump() {
    dump_path="$(realpath "${DUMP:?set DUMP to a .dump, .sql, or .sql.xz file}")"
    [[ -s "${dump_path}" ]] || die "dump is missing or empty: ${dump_path}"
    validate_checksum "${dump_path}"

    case "${dump_path}" in
        *.dump)
            dump_format=custom
            compose exec -T db pg_restore --list < "${dump_path}" >/dev/null
            ;;
        *.sql)
            dump_format=sql
            ;;
        *.sql.xz)
            dump_format=sql-xz
            xz --test "${dump_path}"
            ;;
        *)
            die "unsupported dump type: ${dump_path}"
            ;;
    esac

    if [[ "${dump_format}" != custom ]] && stream_sql_dump \
        | grep -E '^(CREATE|DROP|ALTER)[[:space:]]+DATABASE|^\\connect' >/dev/null; then
        die "plain SQL dump contains database-level commands; refusing an unsafe restore"
    fi
}

backup_database() {
    require_stack
    db_name="$(db_value POSTGRES_DB)"
    db_user="$(db_value POSTGRES_USER)"
    assert_identifier "${db_name}"
    assert_identifier "${db_user}"

    local backup_dir="${BACKUP_DIR:-private/backups}"
    local timestamp target
    timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
    mkdir -p "${backup_dir}"
    chmod 700 "${backup_dir}"
    target="${backup_dir%/}/${db_name}-${timestamp}.dump"
    backup_temporary="$(mktemp "${target}.tmp.XXXXXX")"
    backup_checksum_temporary="${target}.sha256.tmp"
    trap 'rm -f "${backup_temporary}" "${backup_checksum_temporary}"' EXIT
    umask 077

    compose exec -T db pg_dump --username "${db_user}" --format custom \
        --no-owner --no-privileges "${db_name}" > "${backup_temporary}"
    compose exec -T db pg_restore --list < "${backup_temporary}" >/dev/null
    chmod 600 "${backup_temporary}"
    mv "${backup_temporary}" "${target}"
    (
        cd "$(dirname "${target}")"
        sha256sum "$(basename "${target}")" > "$(basename "${backup_checksum_temporary}")"
    )
    mv "${backup_checksum_temporary}" "${target}.sha256"
    trap - EXIT
    echo "Backup created: ${target}"
}

restore_database() {
    require_stack
    db_name="$(db_value POSTGRES_DB)"
    db_user="$(db_value POSTGRES_USER)"
    assert_identifier "${db_name}"
    assert_identifier "${db_user}"
    [[ "${CONFIRM:-}" == "${db_name}" ]] \
        || die "set CONFIRM=${db_name} to authorize the database swap"
    validate_dump

    stopped_services=()
    swapped=0
    restore_complete=0
    suffix="$(date -u +%Y%m%dT%H%M%SZ)_$$"
    temp_db="${db_name}_restore_${suffix}"
    old_db="${db_name}_before_${suffix}"
    failed_db="${db_name}_failed_${suffix}"
    assert_identifier "${temp_db}"
    assert_identifier "${old_db}"
    assert_identifier "${failed_db}"
    ((${#temp_db} <= 63)) || die "temporary database name exceeds PostgreSQL's 63-byte limit"
    ((${#old_db} <= 63)) || die "retained database name exceeds PostgreSQL's 63-byte limit"
    ((${#failed_db} <= 63)) || die "failed database name exceeds PostgreSQL's 63-byte limit"

    cleanup_restore() {
        local exit_code=$?
        local database_rollback_ok=1 search_rollback_ok=1
        set +e
        if [[ "${swapped}" == 1 && "${restore_complete}" == 0 ]]; then
            echo "Restore validation failed; restoring the previous database" >&2
            terminate_database_connections "${db_name}"
            terminate_database_connections "${old_db}"
            if ! compose exec -T db psql --username "${db_user}" --dbname postgres \
                --set ON_ERROR_STOP=1 \
                --command "ALTER DATABASE \"${db_name}\" RENAME TO \"${failed_db}\""; then
                database_rollback_ok=0
            elif ! compose exec -T db psql --username "${db_user}" --dbname postgres \
                --set ON_ERROR_STOP=1 \
                --command "ALTER DATABASE \"${old_db}\" RENAME TO \"${db_name}\""; then
                database_rollback_ok=0
                compose exec -T db psql --username "${db_user}" --dbname postgres \
                    --set ON_ERROR_STOP=1 \
                    --command "ALTER DATABASE \"${failed_db}\" RENAME TO \"${db_name}\""
            else
                swapped=0
                compose run --rm --no-deps django \
                    python3 manage.py rebuild_es_index || search_rollback_ok=0
                compose run --rm --no-deps django \
                    python3 manage.py validate_search_indexes || search_rollback_ok=0
                compose run --rm --no-deps django \
                    python3 manage.py populate_visualization_cache --clear || search_rollback_ok=0
            fi
        fi
        if [[ "${swapped}" == 0 ]] && database_exists "${temp_db}"; then
            terminate_database_connections "${temp_db}"
            compose exec -T db dropdb --username "${db_user}" "${temp_db}"
        fi
        if ((${#stopped_services[@]} > 0)) && [[ "${swapped}" == 0 \
            && "${search_rollback_ok}" == 1 ]]; then
            compose up -d --wait "${stopped_services[@]}" >/dev/null
        elif [[ "${database_rollback_ok}" == 0 ]]; then
            echo "ERROR: automatic database rollback failed; Django remains stopped" >&2
        elif [[ "${search_rollback_ok}" == 0 ]]; then
            echo "ERROR: the previous database was restored, but its search indexes could not be rebuilt; Django remains stopped" >&2
        fi
        exit "${exit_code}"
    }
    trap cleanup_restore EXIT

    database_exists "${temp_db}" && die "temporary database already exists: ${temp_db}"
    compose exec -T db createdb --username "${db_user}" --owner "${db_user}" "${temp_db}"

    echo "Restoring ${dump_path} into temporary database ${temp_db}"
    if [[ "${dump_format}" == custom ]]; then
        compose exec -T db pg_restore --username "${db_user}" --dbname "${temp_db}" \
            --exit-on-error --no-owner --no-privileges < "${dump_path}"
    else
        stream_sql_dump | compose exec -T db psql --username "${db_user}" \
            --dbname "${temp_db}" --set ON_ERROR_STOP=1 --quiet
    fi

    compose run --rm --no-deps -e DB_NAME="${temp_db}" django \
        python3 manage.py migrate --noinput
    compose run --rm --no-deps -e DB_NAME="${temp_db}" django \
        python3 manage.py check
    local service
    for service in scheduler django; do
        if service_exists "${service}" \
            && [[ -n "$(compose ps --status running -q "${service}")" ]]; then
            stopped_services+=("${service}")
        fi
    done
    if ((${#stopped_services[@]} > 0)); then
        compose stop "${stopped_services[@]}"
    fi

    terminate_database_connections "${db_name}"
    terminate_database_connections "${temp_db}"
    compose exec -T db psql --username "${db_user}" --dbname postgres --set ON_ERROR_STOP=1 \
        --command "ALTER DATABASE \"${db_name}\" RENAME TO \"${old_db}\""
    if ! compose exec -T db psql --username "${db_user}" --dbname postgres --set ON_ERROR_STOP=1 \
        --command "ALTER DATABASE \"${temp_db}\" RENAME TO \"${db_name}\""; then
        compose exec -T db psql --username "${db_user}" --dbname postgres --set ON_ERROR_STOP=1 \
            --command "ALTER DATABASE \"${old_db}\" RENAME TO \"${db_name}\""
        die "database swap failed; original database name was restored"
    fi
    swapped=1

    echo "Database swap complete; previous database retained as ${old_db}"
    echo "Rebuilding Elasticsearch indexes before reopening the application"
    compose run --rm --no-deps django python3 manage.py rebuild_es_index
    compose run --rm --no-deps django python3 manage.py validate_search_indexes
    compose run --rm --no-deps django python3 manage.py populate_visualization_cache --clear
    if ((${#stopped_services[@]} > 0)); then
        compose up -d --wait "${stopped_services[@]}"
        stopped_services=()
    fi
    restore_complete=1
    trap - EXIT
    echo "Restore complete: ${db_name} is active"
}

case "${1:-}" in
    backup) backup_database ;;
    restore) restore_database ;;
    *) die "usage: $0 <backup|restore>" ;;
esac

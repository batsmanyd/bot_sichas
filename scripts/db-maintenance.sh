#!/usr/bin/env bash
# Operate the PostgreSQL service for the selected Compose project.
set -euo pipefail
umask 077
compose=(docker compose --env-file "${SICHAS_ENV_FILE:-.env.production}" -f compose.production.yaml)
postgres() { "${compose[@]}" exec -T postgres "$@"; }

case "${1:-}" in
  backup)
    archive="${2:?Usage: db-maintenance.sh backup /protected/path/archive.dump}"
    temporary=$(mktemp "${archive}.tmp.XXXXXX")
    trap 'rm -f -- "$temporary"' EXIT
    postgres pg_dump -U sichas -d sichas --format=custom --no-owner --no-acl > "$temporary"
    postgres pg_restore --list < "$temporary" > /dev/null
    ln -- "$temporary" "$archive"
    printf 'Verified archive: %s\n' "$archive"
    ;;
  restore-test)
    archive="${2:?Usage: db-maintenance.sh restore-test archive.dump sichas_restore_name}"
    database="${3:?Provide a new isolated restore database name}"
    if ! [[ "$database" =~ ^sichas_restore_[a-z0-9_]{1,40}$ ]]; then
      printf 'The destination must be a new sichas_restore_* database.\n' >&2
      exit 2
    fi
    postgres pg_restore --list < "$archive" > /dev/null
    postgres createdb -U sichas "$database"
    postgres pg_restore -U sichas --exit-on-error --single-transaction --no-owner --no-acl -d "$database" < "$archive"
    postgres psql -U sichas -d "$database" -Atc 'SELECT version_num FROM alembic_version'
    ;;
  *)
    printf 'Usage: %s {backup archive.dump|restore-test archive.dump sichas_restore_name}\n' "$0" >&2
    exit 2
    ;;
esac

#!/bin/sh
set -eu

BACKUP_DIR="${POSTGRES_BACKUP_DIR:-/backups}"
BACKUP_PREFIX="${POSTGRES_BACKUP_PREFIX:-postgres}"
BACKUP_INTERVAL_SECONDS="${POSTGRES_BACKUP_INTERVAL_SECONDS:-86400}"
POSTGRES_HOST="${POSTGRES_HOST:-postgres}"
POSTGRES_PORT="${POSTGRES_PORT:-5432}"
POSTGRES_DB="${POSTGRES_DB:-sostra}"
POSTGRES_USER="${POSTGRES_USER:-sostra}"
S3_BACKUP_BUCKET="${S3_BACKUP_BUCKET:-}"
S3_BACKUP_PREFIX="${S3_BACKUP_PREFIX:-postgres}"
S3_BACKUP_ENDPOINT_URL="${S3_BACKUP_ENDPOINT_URL:-}"
export PGPASSWORD="${POSTGRES_PASSWORD:-}"

mkdir -p "${BACKUP_DIR}"

aws_s3() {
  if [ -n "${S3_BACKUP_ENDPOINT_URL}" ]; then
    aws --endpoint-url "${S3_BACKUP_ENDPOINT_URL}" "$@"
  else
    aws "$@"
  fi
}

wait_for_postgres() {
  until pg_isready -h "${POSTGRES_HOST}" -p "${POSTGRES_PORT}" -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" >/dev/null 2>&1; do
    echo "Waiting for PostgreSQL at ${POSTGRES_HOST}:${POSTGRES_PORT}..."
    sleep 2
  done
}

create_backup() {
  timestamp="$(date '+%Y-%m-%d_%H-%M-%S')"
  backup_file="${BACKUP_DIR}/${BACKUP_PREFIX}_${POSTGRES_DB}_${timestamp}.sql.gz"
  temp_sql_file="${backup_file%.gz}"

  echo "Creating PostgreSQL backup: ${backup_file}"
  if pg_dump -h "${POSTGRES_HOST}" -p "${POSTGRES_PORT}" -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" > "${temp_sql_file}"; then
    gzip -f "${temp_sql_file}"
  else
    rm -f "${temp_sql_file}"
    echo "PostgreSQL backup failed."
    return 1
  fi

  if [ -n "${S3_BACKUP_BUCKET}" ]; then
    s3_key="${S3_BACKUP_PREFIX}/${BACKUP_PREFIX}_${POSTGRES_DB}_${timestamp}.sql.gz"
    echo "Uploading backup to s3://${S3_BACKUP_BUCKET}/${s3_key}"
    aws_s3 s3 cp "${backup_file}" "s3://${S3_BACKUP_BUCKET}/${s3_key}"
  fi
}

wait_for_postgres

while true; do
  create_backup
  sleep "${BACKUP_INTERVAL_SECONDS}"
done

#!/bin/sh
set -e

wait_for_postgres() {
  if [ "${DB_ENGINE:-sqlite}" != "postgres" ]; then
    return 0
  fi

  python - <<'PY'
import os
import time

import psycopg

config = {
    "dbname": os.getenv("POSTGRES_DB", "sostra"),
    "user": os.getenv("POSTGRES_USER", "sostra"),
    "password": os.getenv("POSTGRES_PASSWORD", ""),
    "host": os.getenv("POSTGRES_HOST", "postgres"),
    "port": os.getenv("POSTGRES_PORT", "5432"),
    "connect_timeout": 5,
}

for attempt in range(30):
    try:
        conn = psycopg.connect(**config)
        conn.close()
        print("PostgreSQL is ready.")
        break
    except Exception as exc:
        print(f"Waiting for PostgreSQL ({attempt + 1}/30): {exc}", flush=True)
        time.sleep(2)
else:
    raise SystemExit("PostgreSQL did not become ready in time.")
PY
}

wait_for_postgres

if [ "${RUN_MIGRATIONS:-1}" = "1" ]; then
  python manage.py migrate --noinput
else
  echo "Automatic migrations disabled (RUN_MIGRATIONS!=1)."
fi

if [ "${RUN_CREATE_GROUPS:-1}" = "1" ]; then
  python manage.py create_groups
else
  echo "Automatic group sync disabled (RUN_CREATE_GROUPS!=1)."
fi

if [ "${ENABLE_CRON}" = "1" ]; then
  python manage.py run_scheduler &
else
  echo "Cron disabled (ENABLE_CRON!=1)."
fi

exec "$@"

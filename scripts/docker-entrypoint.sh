#!/bin/sh
set -eu

if [ "${PULSE_RUN_MIGRATIONS:-true}" = "true" ]; then
    python /app/scripts/run_migrations.py
fi

exec "$@"

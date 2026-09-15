#!/bin/sh
set -e

# Migrations are run in CI/CD pipeline, not at container startup.
# This prevents race conditions with multiple replicas and ensures
# migrations are tested before reaching production.

# Check the dedicated database before API/worker/Beat start. Migration and
# operator bootstrap commands intentionally run before the company exists.
case "$1" in
    uvicorn|celery)
        python -m app.cli.company check-runtime
        ;;
esac

echo "Starting application..."
exec "$@"

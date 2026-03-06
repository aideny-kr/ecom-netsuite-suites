#!/bin/sh
set -e

# Migrations are run in CI/CD pipeline, not at container startup.
# This prevents race conditions with multiple replicas and ensures
# migrations are tested before reaching production.

echo "Starting application..."
exec "$@"

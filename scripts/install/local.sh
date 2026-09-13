#!/bin/sh
# Deterministic local installation; no LLM, API key, or hosted account required.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
cd "$ROOT"

compose() {
    docker compose --env-file .env.single-company -f compose.single-company.yml "$@"
}

case "${1:-help}" in
    init)
        # Exclusive creation prevents an accidental secret rotation on rerun.
        python3 - <<'PY'
import base64
import os
import secrets

try:
    fd = os.open('.env.single-company', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
except FileExistsError:
    print('Local configuration exists; no changes made.')
else:
    with os.fdopen(fd, 'w') as stream:
        stream.write('POSTGRES_PASSWORD=' + secrets.token_urlsafe(32) + '\n')
        stream.write('JWT_SECRET_KEY=' + secrets.token_urlsafe(64) + '\n')
        stream.write('ENCRYPTION_KEY=' + base64.urlsafe_b64encode(secrets.token_bytes(32)).decode() + '\n')
    print('Created private local configuration. Secret values were not printed.')
PY
        ;;
    bootstrap)
        compose build backend
        compose up -d --wait postgres redis
        compose run --rm migrate
        compose run --rm backend python -m app.cli.company bootstrap
        ;;
    up)
        compose run --rm backend python -m app.cli.company check
        BUILD_ID=$(git rev-parse --short HEAD) compose up -d --build --wait backend frontend worker beat
        ;;
    status)
        compose ps
        ;;
    down)
        # Deliberately keep database and workspace volumes.
        compose down
        ;;
    *)
        echo 'Usage: scripts/install/local.sh init | bootstrap | up | status | down'
        ;;
esac

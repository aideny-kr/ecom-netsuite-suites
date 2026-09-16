#!/usr/bin/env bash
# Dedicated local fixtures only. Does not use a developer's .env database/Redis.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
webmcp_python="${WEBMCP_PYTHON:-python3}"
export DATABASE_URL='postgresql+asyncpg://webmcp:webmcp-fixture@127.0.0.1:15435/webmcp'
export DATABASE_URL_SYNC='postgresql://webmcp:webmcp-fixture@127.0.0.1:15435/webmcp'
export DATABASE_URL_DIRECT=''
export DATABASE_URL_DIRECT_SYNC=''
export REDIS_URL='redis://127.0.0.1:16381/0'
export APP_ENV='test'
"$webmcp_python" -m alembic upgrade head
"$webmcp_python" -m pytest -q \
  tests/test_webmcp_chat_admission.py \
  tests/test_chat_run_api.py tests/test_chat_run_manager.py tests/test_chat_api.py \
  tests/test_write_confirm_orchestrator.py tests/plan_mode/test_chat_api_resume.py \
  tests/test_chat_burst_limit.py "$@"

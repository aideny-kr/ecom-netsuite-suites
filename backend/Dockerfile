FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml .
# Same ceiling assertion as Dockerfile.prod — see the long comment there for
# why an independent check is needed rather than trusting the pin alone. This
# image has the identical COPY-pyproject -> pip install -> COPY-code shape, and
# it is what `docker compose up -d --build backend` builds, so without the
# check a developer can reproduce the same silent chat outage locally and have
# nothing tell them why.
RUN pip install --no-cache-dir -e . && python -c "\
import sys, anthropic; \
v = anthropic.__version__; \
sys.exit(0 if int(v.split('.')[0]) == 0 else \
    'FATAL: anthropic %s installed but pyproject pins <1.0.0. Check the branch '\
    'being built actually carries the ceiling; if it does, rebuild with '\
    '--no-cache. See ClickUp 86bbm719c before lifting the ceiling.' % v)"

COPY . .

EXPOSE 8000

# Must equal --workers below: rate limits are fleet-wide, and the limiter's
# in-memory fallback divides them by WEB_CONCURRENCY. Pinned by
# tests/test_worker_concurrency_config.py so the two cannot drift.
ENV WEB_CONCURRENCY=4

ENTRYPOINT ["./entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4", "--timeout-keep-alive", "5"]

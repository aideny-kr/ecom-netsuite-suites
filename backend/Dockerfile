FROM python:3.11-slim

WORKDIR /app

# WeasyPrint (backend/app/services/report/report_pdf.py) needs these native
# libraries to render PDFs — it has no pure-Python fallback for text shaping
# (pango) or non-PNG/JPEG image decoding (gdk-pixbuf). Installed before the
# Python deps so this layer is cached independently of pyproject.toml churn.
# See docs/superpowers/specs/2026-09-08-scheduled-jobs-and-inventory-aging-design.md
# §A4 and .claude/rules/deploy.md (Dockerfile changes are T2 infra).
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpango-1.0-0 \
    libpangoft2-1.0-0 \
    libcairo2 \
    libgdk-pixbuf-2.0-0 \
    libffi-dev \
    shared-mime-info \
    fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml scripts/assert_dependency_ceilings.py ./
# Same ceiling assertion as Dockerfile.prod — see the long comment there for
# why an independent check is needed rather than trusting the pin alone. This
# image has the identical COPY-pyproject -> pip install -> COPY-code shape, and
# it is what `docker compose up -d --build backend` builds, so without the
# check a developer can reproduce the same silent chat outage locally and have
# nothing tell them why.
RUN pip install --no-cache-dir -e . && python assert_dependency_ceilings.py

COPY . .

EXPOSE 8000

# Must equal --workers below: rate limits are fleet-wide, and the limiter's
# in-memory fallback divides them by WEB_CONCURRENCY. Pinned by
# tests/test_worker_concurrency_config.py so the two cannot drift.
ENV WEB_CONCURRENCY=4

ENTRYPOINT ["./entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4", "--timeout-keep-alive", "5"]

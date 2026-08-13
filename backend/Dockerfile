FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml .
RUN pip install --no-cache-dir -e .

COPY . .

EXPOSE 8000

# Must equal --workers below: rate limits are fleet-wide, and the limiter's
# in-memory fallback divides them by WEB_CONCURRENCY. Pinned by
# tests/test_worker_concurrency_config.py so the two cannot drift.
ENV WEB_CONCURRENCY=4

ENTRYPOINT ["./entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4", "--timeout-keep-alive", "5"]

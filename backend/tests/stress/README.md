# Stress Testing Suite

Validates Sprint 0 scalability improvements (DB pool 10→50, workers 1→4).

## Prerequisites

```bash
pip install httpx --break-system-packages
```

## Quick Start

### 1. Smoke Test (5 concurrent)
Verify things work before ramping up:
```bash
cd backend/tests/stress
python stress_test.py \
  --base-url http://localhost:8000 \
  --email your@email.com \
  --password YourPass1! \
  --concurrency 5 \
  --messages 1
```

### 2. Medium Load (25 concurrent)
Validates the system handles moderate traffic:
```bash
python stress_test.py \
  --base-url http://localhost:8000 \
  --email your@email.com \
  --password YourPass1! \
  --concurrency 25 \
  --messages 2
```

### 3. Ramp Test (auto-escalating)
Gradually increases from 5→10→25→50→75→100 concurrent sessions, stopping when success rate drops below 80%. Best way to find the actual breaking point:
```bash
python stress_test.py \
  --base-url http://localhost:8000 \
  --email your@email.com \
  --password YourPass1! \
  --concurrency 100 \
  --messages 1 \
  --mode ramp
```

### 4. Pool Saturation
Rapid-fire DB operations (no LLM calls) to test connection pool under extreme load:
```bash
python stress_test.py \
  --base-url http://localhost:8000 \
  --email your@email.com \
  --password YourPass1! \
  --concurrency 80 \
  --mode pool-saturation
```

### 5. Health Monitor (run alongside other tests)
Open a second terminal and run this while stress testing:
```bash
python stress_test.py \
  --base-url http://localhost:8000 \
  --email dummy@test.com \
  --password dummy \
  --mode health-monitor \
  --duration 120
```
Note: health-monitor doesn't need valid auth — it only hits the unauthenticated /health/detailed endpoint.

## Against Staging

```bash
python stress_test.py \
  --base-url https://api-staging.suitestudio.ai \
  --email your@email.com \
  --password YourPass1! \
  --concurrency 25 \
  --messages 2 \
  --mode ramp
```

## What Gets Tested

| Mode | DB Pool | Workers | SSE Streaming | LLM Calls |
|------|---------|---------|---------------|-----------|
| normal | ✅ | ✅ | ✅ | ✅ |
| pool-saturation | ✅ (extreme) | ✅ | ❌ | ❌ |
| ramp | ✅ | ✅ | ✅ | ✅ |
| health-monitor | (observes) | (observes) | (observes) | ❌ |

## Reading Results

The report shows:
- **Success rate**: % of sessions that completed without errors (target: >95%)
- **First-chunk latency**: Time from request to first SSE data event (measures DB + auth + pipeline startup)
- **Session duration**: Total time per chat session (includes LLM generation)
- **Infrastructure peak**: Max DB connections checked out and SSE connections observed during test
- **Verdict**: PASS (≥95%), WARN (80-95%), FAIL (<80%)

## Interpreting Failures

| Symptom | Likely Cause | Fix |
|---------|-------------|-----|
| Pool checked_out = pool_size + max_overflow | DB pool exhausted | Increase pool_size or max_overflow in database.py |
| 500 errors on session create | Pool timeout | Same as above, or session-per-operation pattern |
| SSE timeouts | Worker stuck on LLM call | Increase gunicorn workers or timeout |
| Success drops at 50+ | Anthropic rate limit | Check API rate limits, add retry logic |
| All fail immediately | Auth issue | Verify credentials, check rate limiting |

## Cleanup

Stress test sessions are titled `stress-test-{timestamp}`. To clean up:
```sql
DELETE FROM chat_messages WHERE session_id IN (
  SELECT id FROM chat_sessions WHERE title LIKE 'stress-test-%'
);
DELETE FROM chat_sessions WHERE title LIKE 'stress-test-%';
```

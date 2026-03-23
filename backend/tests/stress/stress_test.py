#!/usr/bin/env python3
"""
AI-den Concurrency & Scalability Stress Test
=============================================

Tests the Sprint 0 scalability improvements:
  - DB pool: 20 + 30 overflow = 50 connections
  - 4 gunicorn workers
  - Target: ~200 concurrent chats

Usage:
  # Quick smoke test (5 concurrent, 2 messages each)
  python stress_test.py --base-url http://localhost:8000 --email admin@test.com --password YourPass1!

  # Medium load (25 concurrent sessions)
  python stress_test.py --base-url http://localhost:8000 --email admin@test.com --password YourPass1! --concurrency 25

  # Full stress test (50+ concurrent sessions)
  python stress_test.py --base-url http://localhost:8000 --email admin@test.com --password YourPass1! --concurrency 50 --messages 3

  # Against staging
  python stress_test.py --base-url https://api-staging.suitestudio.ai --email admin@test.com --password YourPass1! --concurrency 25

  # Pool saturation test (pushes past pool limits)
  python stress_test.py --base-url http://localhost:8000 --email admin@test.com --password YourPass1! --concurrency 80 --mode pool-saturation

Modes:
  normal          - Simulates real chat usage (create session → send messages → read SSE)
  pool-saturation - Rapid-fire concurrent DB operations to stress the connection pool
  health-monitor  - Polls /health/detailed every second and prints pool stats (run alongside other tests)
"""

import argparse
import asyncio
import json
import statistics
import sys
import time
from dataclasses import dataclass, field
from enum import Enum

try:
    import httpx
except ImportError:
    print("httpx required: pip install httpx --break-system-packages")
    sys.exit(1)


# ── Data classes ──────────────────────────────────────────────────────────────

class TestMode(Enum):
    NORMAL = "normal"
    POOL_SATURATION = "pool-saturation"
    HEALTH_MONITOR = "health-monitor"


@dataclass
class SessionResult:
    session_id: str = ""
    messages_sent: int = 0
    messages_ok: int = 0
    messages_failed: int = 0
    sse_chunks_received: int = 0
    first_chunk_latency_ms: float = 0.0
    total_duration_ms: float = 0.0
    error: str | None = None


@dataclass
class StressTestReport:
    mode: str = ""
    concurrency: int = 0
    total_sessions: int = 0
    successful_sessions: int = 0
    failed_sessions: int = 0
    total_messages_sent: int = 0
    total_messages_ok: int = 0
    total_messages_failed: int = 0
    total_sse_chunks: int = 0
    duration_seconds: float = 0.0
    first_chunk_latencies_ms: list[float] = field(default_factory=list)
    session_durations_ms: list[float] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    health_snapshots: list[dict] = field(default_factory=list)

    def summary(self) -> str:
        lines = [
            "",
            "=" * 70,
            f"  STRESS TEST REPORT — {self.mode.upper()} MODE",
            "=" * 70,
            f"  Concurrency:          {self.concurrency}",
            f"  Total sessions:       {self.total_sessions}",
            f"  Successful:           {self.successful_sessions}",
            f"  Failed:               {self.failed_sessions}",
            f"  Success rate:         {self.successful_sessions / max(self.total_sessions, 1) * 100:.1f}%",
            "",
            f"  Messages sent:        {self.total_messages_sent}",
            f"  Messages OK:          {self.total_messages_ok}",
            f"  Messages failed:      {self.total_messages_failed}",
            f"  SSE chunks received:  {self.total_sse_chunks}",
            "",
            f"  Total duration:       {self.duration_seconds:.1f}s",
        ]

        if self.first_chunk_latencies_ms:
            lat = self.first_chunk_latencies_ms
            lines += [
                "",
                "  First-chunk latency (ms):",
                f"    p50:  {statistics.median(lat):.0f}",
                f"    p90:  {sorted(lat)[int(len(lat) * 0.9)]:.0f}" if len(lat) >= 10 else "",
                f"    p99:  {sorted(lat)[int(len(lat) * 0.99)]:.0f}" if len(lat) >= 100 else "",
                f"    max:  {max(lat):.0f}",
                f"    mean: {statistics.mean(lat):.0f}",
            ]

        if self.session_durations_ms:
            dur = self.session_durations_ms
            lines += [
                "",
                "  Session duration (ms):",
                f"    p50:  {statistics.median(dur):.0f}",
                f"    p90:  {sorted(dur)[int(len(dur) * 0.9)]:.0f}" if len(dur) >= 10 else "",
                f"    max:  {max(dur):.0f}",
                f"    mean: {statistics.mean(dur):.0f}",
            ]

        if self.health_snapshots:
            last = self.health_snapshots[-1]
            peak_checked_out = max(s.get("db_pool", {}).get("checked_out", 0) for s in self.health_snapshots)
            peak_sse = max(s.get("active_sse_connections", 0) for s in self.health_snapshots)
            lines += [
                "",
                "  Infrastructure (peak during test):",
                f"    DB pool checked out: {peak_checked_out}",
                f"    SSE connections:     {peak_sse}",
                f"    Final pool state:    {last.get('db_pool', {})}",
            ]

        if self.errors:
            unique_errors = list(set(self.errors))[:10]
            lines += [
                "",
                f"  Errors ({len(self.errors)} total, showing unique):",
            ]
            for e in unique_errors:
                lines += [f"    • {e[:120]}"]

        lines += [
            "",
            "  VERDICT:",
        ]
        rate = self.successful_sessions / max(self.total_sessions, 1)
        if rate >= 0.95:
            lines.append(f"    ✅ PASS — {rate*100:.0f}% success at {self.concurrency} concurrent")
        elif rate >= 0.80:
            lines.append(f"    ⚠️  WARN — {rate*100:.0f}% success at {self.concurrency} concurrent")
        else:
            lines.append(f"    ❌ FAIL — {rate*100:.0f}% success at {self.concurrency} concurrent")

        lines.append("=" * 70)
        return "\n".join(lines)


# ── Test prompts (lightweight — won't trigger heavy agent work) ──────────────

STRESS_PROMPTS = [
    "What is 2 + 2?",
    "Say hello",
    "What day is it?",
    "Count to 5",
    "Name three colors",
    "What is NetSuite?",
    "Give me a one-word answer: yes or no?",
    "Repeat after me: test complete",
]


# ── Core test logic ──────────────────────────────────────────────────────────

async def authenticate(client: httpx.AsyncClient, base_url: str, email: str, password: str) -> str:
    """Login and return access_token."""
    resp = await client.post(
        f"{base_url}/api/v1/auth/login",
        json={"email": email, "password": password},
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Login failed ({resp.status_code}): {resp.text}")
    data = resp.json()
    return data["access_token"]


async def create_session(client: httpx.AsyncClient, base_url: str, token: str) -> str:
    """Create a chat session and return its ID."""
    resp = await client.post(
        f"{base_url}/api/v1/chat/sessions",
        json={"title": f"stress-test-{int(time.time())}"},
        headers={"Authorization": f"Bearer {token}"},
    )
    if resp.status_code != 201:
        raise RuntimeError(f"Create session failed ({resp.status_code}): {resp.text}")
    return resp.json()["id"]


async def send_message_sse(
    client: httpx.AsyncClient,
    base_url: str,
    token: str,
    session_id: str,
    content: str,
    timeout: float = 120.0,
) -> tuple[int, float]:
    """
    Send a message and consume SSE stream.
    Returns (chunk_count, first_chunk_latency_ms).
    """
    chunks = 0
    first_chunk_ms = 0.0
    start = time.monotonic()

    async with client.stream(
        "POST",
        f"{base_url}/api/v1/chat/sessions/{session_id}/messages",
        json={"content": content},
        headers={"Authorization": f"Bearer {token}"},
        timeout=httpx.Timeout(timeout, connect=10.0),
    ) as response:
        if response.status_code not in (200, 201):
            body = await response.aread()
            raise RuntimeError(f"Send message failed ({response.status_code}): {body.decode()[:200]}")

        async for line in response.aiter_lines():
            if line.startswith("data: "):
                chunks += 1
                if chunks == 1:
                    first_chunk_ms = (time.monotonic() - start) * 1000

                # Check for errors in SSE
                try:
                    event = json.loads(line[6:])
                    if isinstance(event, dict) and event.get("type") == "error":
                        raise RuntimeError(f"SSE error: {event.get('error', 'unknown')}")
                except json.JSONDecodeError:
                    pass

    return chunks, first_chunk_ms


async def run_chat_session(
    base_url: str,
    token: str,
    num_messages: int,
    session_idx: int,
) -> SessionResult:
    """Simulate one full chat session: create → send N messages → collect SSE."""
    result = SessionResult()
    session_start = time.monotonic()

    async with httpx.AsyncClient() as client:
        try:
            result.session_id = await create_session(client, base_url, token)

            for i in range(num_messages):
                prompt = STRESS_PROMPTS[(session_idx * num_messages + i) % len(STRESS_PROMPTS)]
                result.messages_sent += 1

                try:
                    chunks, latency = await send_message_sse(
                        client, base_url, token, result.session_id, prompt
                    )
                    result.messages_ok += 1
                    result.sse_chunks_received += chunks
                    if i == 0:
                        result.first_chunk_latency_ms = latency
                except Exception as e:
                    result.messages_failed += 1
                    result.error = str(e)

        except Exception as e:
            result.error = str(e)

    result.total_duration_ms = (time.monotonic() - session_start) * 1000
    return result


async def poll_health(
    base_url: str,
    stop_event: asyncio.Event,
    snapshots: list[dict],
    interval: float = 1.0,
):
    """Poll /health/detailed while tests run."""
    async with httpx.AsyncClient() as client:
        while not stop_event.is_set():
            try:
                resp = await client.get(f"{base_url}/api/v1/health/detailed", timeout=5.0)
                if resp.status_code == 200:
                    snap = resp.json()
                    snap["_ts"] = time.time()
                    snapshots.append(snap)
                    pool = snap.get("db_pool", {})
                    sse = snap.get("active_sse_connections", 0)
                    print(
                        f"  [health] pool: {pool.get('checked_out', '?')}/{pool.get('pool_size', '?')}+{pool.get('overflow', '?')} "
                        f"| SSE: {sse} "
                        f"| avail: {pool.get('checked_in', '?')}",
                        flush=True,
                    )
            except Exception:
                pass
            await asyncio.sleep(interval)


async def run_normal_test(
    base_url: str,
    token: str,
    concurrency: int,
    messages_per_session: int,
) -> StressTestReport:
    """Run N concurrent chat sessions and collect metrics."""
    report = StressTestReport(mode="normal", concurrency=concurrency)

    # Start health polling
    stop_health = asyncio.Event()
    health_task = asyncio.create_task(poll_health(base_url, stop_health, report.health_snapshots))

    print(f"\n  Launching {concurrency} concurrent chat sessions ({messages_per_session} messages each)...\n", flush=True)
    start = time.monotonic()

    # Launch all sessions concurrently
    tasks = [
        run_chat_session(base_url, token, messages_per_session, i)
        for i in range(concurrency)
    ]
    results: list[SessionResult] = await asyncio.gather(*tasks)

    report.duration_seconds = time.monotonic() - start

    # Stop health polling
    stop_health.set()
    await health_task

    # Aggregate results
    report.total_sessions = len(results)
    for r in results:
        report.total_messages_sent += r.messages_sent
        report.total_messages_ok += r.messages_ok
        report.total_messages_failed += r.messages_failed
        report.total_sse_chunks += r.sse_chunks_received
        report.session_durations_ms.append(r.total_duration_ms)

        if r.first_chunk_latency_ms > 0:
            report.first_chunk_latencies_ms.append(r.first_chunk_latency_ms)

        if r.error:
            report.failed_sessions += 1
            report.errors.append(r.error)
        else:
            report.successful_sessions += 1

    return report


async def run_pool_saturation_test(
    base_url: str,
    token: str,
    concurrency: int,
) -> StressTestReport:
    """Rapid-fire DB operations to test pool under extreme load."""
    report = StressTestReport(mode="pool-saturation", concurrency=concurrency)

    stop_health = asyncio.Event()
    health_task = asyncio.create_task(poll_health(base_url, stop_health, report.health_snapshots, interval=0.5))

    print(f"\n  Pool saturation: {concurrency} concurrent session creates + list operations...\n", flush=True)
    start = time.monotonic()

    async def rapid_db_ops(idx: int) -> SessionResult:
        result = SessionResult()
        async with httpx.AsyncClient() as client:
            try:
                # Create session (DB write)
                result.session_id = await create_session(client, base_url, token)
                result.messages_sent += 1
                result.messages_ok += 1

                # List sessions (DB read)
                resp = await client.get(
                    f"{base_url}/api/v1/chat/sessions",
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=30.0,
                )
                if resp.status_code == 200:
                    result.messages_ok += 1
                else:
                    result.messages_failed += 1
                result.messages_sent += 1

                # Get session detail (DB read)
                resp = await client.get(
                    f"{base_url}/api/v1/chat/sessions/{result.session_id}",
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=30.0,
                )
                if resp.status_code == 200:
                    result.messages_ok += 1
                else:
                    result.messages_failed += 1
                result.messages_sent += 1

            except Exception as e:
                result.error = str(e)
        result.total_duration_ms = (time.monotonic() - start) * 1000
        return result

    tasks = [rapid_db_ops(i) for i in range(concurrency)]
    results = await asyncio.gather(*tasks)

    report.duration_seconds = time.monotonic() - start
    stop_health.set()
    await health_task

    report.total_sessions = len(results)
    for r in results:
        report.total_messages_sent += r.messages_sent
        report.total_messages_ok += r.messages_ok
        report.total_messages_failed += r.messages_failed
        report.session_durations_ms.append(r.total_duration_ms)
        if r.error:
            report.failed_sessions += 1
            report.errors.append(r.error)
        else:
            report.successful_sessions += 1

    return report


async def run_health_monitor(base_url: str, duration: int):
    """Standalone health monitor — run in parallel with other tests."""
    print(f"\n  Health monitor running for {duration}s (run stress test in another terminal)...\n", flush=True)
    snapshots: list[dict] = []
    stop = asyncio.Event()

    async def stop_after():
        await asyncio.sleep(duration)
        stop.set()

    asyncio.create_task(stop_after())
    await poll_health(base_url, stop, snapshots, interval=1.0)

    if snapshots:
        peak_out = max(s.get("db_pool", {}).get("checked_out", 0) for s in snapshots)
        peak_sse = max(s.get("active_sse_connections", 0) for s in snapshots)
        print(f"\n  Peak DB checked_out: {peak_out}")
        print(f"  Peak SSE connections: {peak_sse}")
        print(f"  Snapshots collected: {len(snapshots)}")


# ── Ramp-up test (gradually increases concurrency) ──────────────────────────

async def run_ramp_test(
    base_url: str,
    token: str,
    max_concurrency: int,
    messages_per_session: int,
) -> None:
    """Gradually increase concurrency to find the breaking point."""
    levels = [5, 10, 25, 50, 75, 100]
    levels = [l for l in levels if l <= max_concurrency]
    if max_concurrency not in levels:
        levels.append(max_concurrency)

    print(f"\n  RAMP TEST: testing levels {levels}\n", flush=True)

    for level in levels:
        print(f"\n{'─' * 50}")
        print(f"  Testing concurrency level: {level}")
        print(f"{'─' * 50}", flush=True)

        report = await run_normal_test(base_url, token, level, messages_per_session)
        print(report.summary(), flush=True)

        rate = report.successful_sessions / max(report.total_sessions, 1)
        if rate < 0.80:
            print(f"\n  ⛔ Stopping ramp — success rate dropped below 80% at concurrency {level}")
            break

        # Brief cooldown between levels
        if level != levels[-1]:
            print("\n  Cooling down 5s before next level...", flush=True)
            await asyncio.sleep(5)


# ── CLI ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="AI-den Scalability Stress Test",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--base-url", required=True, help="Backend URL (e.g. http://localhost:8000)")
    parser.add_argument("--email", help="Login email (not needed if --token is provided)")
    parser.add_argument("--password", help="Login password (not needed if --token is provided)")
    parser.add_argument("--token", help="JWT access token (skip login, e.g. for Google OAuth accounts)")
    parser.add_argument("--concurrency", type=int, default=5, help="Number of concurrent sessions (default: 5)")
    parser.add_argument("--messages", type=int, default=2, help="Messages per session (default: 2)")
    parser.add_argument(
        "--mode",
        choices=["normal", "pool-saturation", "health-monitor", "ramp"],
        default="normal",
        help="Test mode (default: normal)",
    )
    parser.add_argument("--duration", type=int, default=60, help="Duration in seconds for health-monitor mode")
    args = parser.parse_args()

    async def _run():
        # Authenticate
        if args.token:
            token = args.token
            print(f"\n  Using provided JWT token against {args.base_url}...", flush=True)
            print("  ✓ Token provided\n", flush=True)
        elif args.email and args.password:
            print(f"\n  Authenticating against {args.base_url}...", flush=True)
            async with httpx.AsyncClient() as client:
                token = await authenticate(client, args.base_url, args.email, args.password)
            print("  ✓ Authenticated\n", flush=True)
        else:
            print("Error: provide either --token or both --email and --password")
            sys.exit(1)

        if args.mode == "normal":
            report = await run_normal_test(args.base_url, token, args.concurrency, args.messages)
            print(report.summary(), flush=True)

        elif args.mode == "pool-saturation":
            report = await run_pool_saturation_test(args.base_url, token, args.concurrency)
            print(report.summary(), flush=True)

        elif args.mode == "health-monitor":
            await run_health_monitor(args.base_url, args.duration)

        elif args.mode == "ramp":
            await run_ramp_test(args.base_url, token, args.concurrency, args.messages)

    asyncio.run(_run())


if __name__ == "__main__":
    main()

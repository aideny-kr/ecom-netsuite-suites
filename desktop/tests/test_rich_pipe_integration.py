"""REAL end-to-end streaming integration test for the rich pipe (Task A4).

Load-bearing per the plan: drives the ACTUAL sidecar serve loop in a real
subprocess (no stubbed agent/runner) over real OS pipes — the pattern that
catches launch bugs mocked tests miss (B0's three launch bugs). Pattern + rigor:
`desktop/tests/test_mcp_integration.py`.

Two layers:

* ``test_real_sidecar_*`` — CI-safe and KEY-FREE: the real subprocess boots,
  speaks the newline-JSON protocol over real pipes, keeps stdout JSON-only, and
  exits cleanly on stdin EOF. Proves the launch path end-to-end without a key.

* ``test_live_*`` — the full ``data_table`` pipe with a real Hermes agent calling
  ``sample_dataset``. Needs a live Anthropic key (via ``~/.hermes/.env``), so it
  is OPERATOR-DEFERRED behind ``RUN_RICH_PIPE_LIVE=1`` — see
  ``desktop/SMOKE-DEFERRAL-RICH-PIPE.md``. The key-free proof of the data_table
  shape is ``test_streaming_protocol.py`` (A3) + the renderer tests (C2).
"""

from __future__ import annotations

import json
import os
import select
import subprocess
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
RUNTIME = HERE.parent / "runtime"
SIDECAR = RUNTIME / "sidecar.py"

sys.path.insert(0, str(RUNTIME))
from suite_tools.sample_dataset import sample_dataset  # noqa: E402  (ties assertions to the real tool output)


def _spawn(env_overrides: dict) -> subprocess.Popen:
    """Spawn the real sidecar exactly as Electron does (`python -u … --serve`)."""
    env = os.environ.copy()
    env.update(env_overrides)
    return subprocess.Popen(
        [sys.executable, "-u", str(SIDECAR), "--serve"],
        cwd=str(RUNTIME),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        env=env,
    )


def _read_json_events_until(proc: subprocess.Popen, predicate, deadline: float) -> list:
    """Read newline-JSON events from proc.stdout until predicate(event) or deadline.

    Asserts every non-blank line parses as JSON (the stdout-isolation guard) and
    raises on timeout. POSIX `select` gives per-read timeouts without threads.
    """
    events: list = []
    while time.monotonic() < deadline:
        remaining = max(0.0, deadline - time.monotonic())
        ready, _, _ = select.select([proc.stdout], [], [], remaining)
        if not ready:
            break
        line = proc.stdout.readline()
        if line == "":  # EOF
            break
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            raise AssertionError(f"non-JSON line on protocol stdout (isolation broken): {line!r}")
        events.append(event)
        if predicate(event):
            return events
    raise AssertionError(f"predicate not satisfied before deadline; events so far = {events}")


def test_real_sidecar_boots_and_speaks_json_protocol_over_real_pipes(tmp_path):
    """KEY-FREE launch-bug catcher: the real subprocess boots, handles a
    malformed line and an unknown action with JSON error lines, keeps stdout
    JSON-only, and exits cleanly on stdin EOF. No stubs, real OS pipes."""
    proc = _spawn({"SUITE_STUDIO_HOME": str(tmp_path / "SuiteStudio")})
    try:
        stdout, stderr = proc.communicate(
            input="this is not json\n" + json.dumps({"action": "frobnicate"}) + "\n",
            timeout=120,
        )
    finally:
        if proc.poll() is None:
            proc.kill()

    lines = [ln for ln in stdout.strip().splitlines() if ln.strip()]
    payloads = [json.loads(ln) for ln in lines]  # EVERY stdout line must be valid JSON
    assert any("malformed JSON" in p.get("error", "") for p in payloads), (
        f"malformed request must yield an error line; stdout={payloads}, stderr={stderr[:800]}"
    )
    assert any("unknown action" in p.get("error", "") for p in payloads), payloads
    assert proc.returncode == 0, f"sidecar must exit cleanly on EOF; stderr tail={stderr[-800:]}"


@pytest.mark.skipif(
    not os.environ.get("RUN_RICH_PIPE_LIVE"),
    reason=(
        "live rich-pipe smoke is operator-deferred — needs an Anthropic key via "
        "~/.hermes/.env. Run with RUN_RICH_PIPE_LIVE=1. See "
        "desktop/SMOKE-DEFERRAL-RICH-PIPE.md."
    ),
)
def test_live_streaming_run_emits_data_table_with_tool_rows_then_done(tmp_path):
    """OPERATOR-DEFERRED full pipe: real agent calls sample_dataset, the sidecar
    streams a data_table event with the tool's exact columns/rows, then done."""
    proc = _spawn({"SUITE_STUDIO_HOME": str(tmp_path / "SuiteStudio")})
    try:
        request = json.dumps({
            "action": "run",
            "stream": True,
            "query": "Call the sample_dataset tool and show me the demo table of account balances.",
        }) + "\n"
        proc.stdin.write(request)
        proc.stdin.flush()
        events = _read_json_events_until(
            proc, lambda e: e.get("type") == "done", deadline=time.monotonic() + 180
        )
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass
        if proc.poll() is None:
            proc.kill()

    assert events[-1]["type"] == "done", f"stream must terminate with done; got {events[-1]}"
    assert isinstance(events[-1].get("tokens_used"), int)

    tables = [e for e in events if e.get("type") == "data_table"]
    assert tables, f"expected a data_table event; got types {[e.get('type') for e in events]}"
    expected = sample_dataset()
    assert any(
        t["data"]["columns"] == expected["columns"] and t["data"]["rows"] == expected["rows"]
        for t in tables
    ), f"data_table must carry the tool's exact columns/rows; got {tables}"

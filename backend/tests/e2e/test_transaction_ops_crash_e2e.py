"""CI gate for real committed HTTP/worker approval and killed-process recovery."""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
DRILL = ROOT / "scripts/uat/transaction_ops_crash_drill.py"


def finish_process(process, journal):
    """Supervise the process group and clean its journal even after a hard kill."""
    if process.poll() is None:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=10)
    if journal.exists():
        child_pid = json.loads(journal.read_text()).get("child_pid")
        if child_pid:
            try:
                if os.getpgid(child_pid) == process.pid:
                    os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        result = subprocess.run(
            [sys.executable, str(DRILL), "--cleanup-state", str(journal)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["zero_residue"] is True
        assert not journal.exists()


@pytest.mark.parametrize(
    "database,expected",
    [
        ("postgresql+asyncpg://postgres:fixture@localhost:5432/ecom_netsuite_test", 0),
        ("postgresql+asyncpg://postgres:fixture@127.0.0.1:5432/ecom_netsuite", 0),
        ("postgresql+asyncpg://postgres:fixture@db.example.test:5432/ecom_netsuite_test", 1),
        ("postgresql+asyncpg://postgres:fixture@localhost:5433/ecom_netsuite_test", 1),
        ("postgresql+asyncpg://postgres:fixture@localhost:5432/unrelated", 1),
    ],
)
def test_crash_drill_refuses_remote_or_unrelated_databases_before_mutation(database, expected):
    result = subprocess.run(
        [sys.executable, str(DRILL), "--check-database-only"],
        cwd=ROOT,
        env={**os.environ, "DATABASE_URL": database, "DATABASE_URL_DIRECT": ""},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert (result.returncode == 0) == (expected == 0), result.stderr
    assert "fixture" not in result.stdout + result.stderr


@pytest.mark.parametrize("action", ["correct_amounts", "sync_missing_order"])
def test_seeded_http_approval_and_actual_sigkill_recovery_with_zero_residue(tmp_path, action):
    output = tmp_path / "crash-result.json"
    process = subprocess.Popen(
        [sys.executable, str(DRILL), "--output", str(output), "--action", action],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        _, stderr = process.communicate(timeout=60)
        assert process.returncode == 0, stderr
    finally:
        finish_process(process, Path(str(output) + ".state.json"))
    report = json.loads(output.read_text())
    assert report["passed"] and report["http_human_approval"] and report["real_process_kill"]
    assert report["writes"] == 1 and report["outcome"] == "verified" and report["zero_residue"]
    assert report["action"] == action
    if action == "sync_missing_order":
        assert report["original_api_calls"] == 21
        assert report["native_state"] == "A" and report["native_quantity"] == "2"
        assert report["source_quantity"] == "1" and report["private_source_unchanged"] is True


def test_hard_killed_drill_parent_leaves_a_journal_for_exact_supervisor_cleanup(tmp_path):
    output = tmp_path / "parent-killed.json"
    journal = Path(str(output) + ".state.json")
    process = subprocess.Popen(
        [sys.executable, str(DRILL), "--output", str(output)],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    found = False
    try:
        deadline = time.monotonic() + 15
        while process.poll() is None and time.monotonic() < deadline:
            if journal.exists():
                saved = json.loads(journal.read_text())
                if saved.get("phase") == "executing":
                    found = True
                    break
            time.sleep(0.02)
        assert found, "A durable cleanup journal must exist before the child starts executing"
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate(timeout=10)
        assert journal.exists()
    finally:
        finish_process(process, journal)

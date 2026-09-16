"""Real SIGKILL at the provider boundary, with ephemeral DB cleanup."""

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
DRILL = ROOT / "scripts/uat/accounting_credit_crash_drill.py"


def test_chat_credit_sigkill_recovery_never_posts_twice(tmp_path):
    output = tmp_path / "credit-crash.json"
    journal = Path(str(output) + ".state.json")
    process = subprocess.Popen(
        [sys.executable, str(DRILL), "--output", str(output)],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        _, errors = process.communicate(timeout=60)
        assert process.returncode == 0, errors.decode()
        result = json.loads(output.read_text())
        assert result["passed"] and result["real_process_kill"] and result["zero_residue"]
        assert result["writes"] == 1 and result["recovery_reads"] == 1
        assert result["original_approver_preserved"] and not result["native_netsuite_write"]
        assert not journal.exists()
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=5)
        if journal.exists():
            child_pid = json.loads(journal.read_text()).get("child_pid")
            if child_pid:
                try:
                    if os.getpgid(child_pid) == process.pid:
                        os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            cleanup = subprocess.run(
                [sys.executable, str(DRILL), "--cleanup-state", str(journal)],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert cleanup.returncode == 0, cleanup.stderr
            assert json.loads(cleanup.stdout)["zero_residue"]

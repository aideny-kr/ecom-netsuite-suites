"""Exercise the actual deployment shell guards with isolated Docker responses."""

import os
import shlex
import subprocess
from pathlib import Path

import pytest
import yaml


@pytest.mark.parametrize(
    "workflow,job",
    [("deploy", "deploy-staging"), ("deploy", "deploy-production"), ("rollback", "rollback")],
)
@pytest.mark.parametrize(
    "failed_service,failure", [("", ""), ("worker", "stale"), ("worker-collectors", "stale"), ("beat", "exited")]
)
def test_release_rejects_stale_or_stopped_background_services(tmp_path, workflow, job, failed_service, failure):
    root = Path(__file__).resolve().parents[2]
    definition = yaml.safe_load((root / f".github/workflows/{workflow}.yml").read_text())
    script = next(
        step["with"]["script"] for step in definition["jobs"][job]["steps"] if "script" in step.get("with", {})
    )
    up_commands = [shlex.split(line) for line in script.splitlines() if "compose" in line and " up " in line]
    assert any("worker-collectors" in command for command in up_commands)
    # Execute the checked-in guard, including all its retries and exit behavior.
    # The outer loop's indentation distinguishes it from the inner retry loop.
    start = script.index("for service in backend worker worker-collectors beat; do")
    guard = script[start:].split("\ndone", 1)[0] + "\ndone\n"
    # YAML has removed the script's common indentation; the outer loop closes
    # unindented, while its inner loop stays indented.
    subprocess.run(["bash", "-n"], input=guard, text=True, check=True)
    docker = tmp_path / "docker"
    docker.write_text(
        "#!/bin/bash\n"
        "container=${@: -1}\n"
        'echo "$container" >> "$PROBE_CALLS"\n'
        "image=sha256:expected; status=running\n"
        "if [[ -n $PROBE_FAILED && $container == ecom-netsuite-${PROBE_FAILED}-1 ]]; then\n"
        "  if [[ $PROBE_FAILURE == stale ]]; then image=sha256:old; else status=exited; fi\n"
        "fi\n"
        'echo "$image $status"\n'
    )
    docker.chmod(0o700)
    sleep = tmp_path / "sleep"
    sleep.write_text("#!/bin/sh\nexit 0\n")
    sleep.chmod(0o700)
    calls = tmp_path / "calls"
    result = subprocess.run(
        ["bash", "-eu", "-c", guard],
        text=True,
        capture_output=True,
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "PULLED_DIGEST": "sha256:expected",
            "PROBE_FAILED": failed_service,
            "PROBE_FAILURE": failure,
            "PROBE_CALLS": str(calls),
        },
    )
    assert result.returncode == (1 if failed_service else 0), result.stdout + result.stderr
    observed = calls.read_text().splitlines()
    if failed_service:
        assert observed.count(f"ecom-netsuite-{failed_service}-1") == 12
        assert f"{failed_service} is stale or not running" in result.stdout
    else:
        assert observed == [
            f"ecom-netsuite-{service}-1" for service in ("backend", "worker", "worker-collectors", "beat")
        ]

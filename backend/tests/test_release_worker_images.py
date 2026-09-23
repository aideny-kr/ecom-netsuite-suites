"""Exercise the actual deployment shell guards with isolated Docker responses."""

import os
import re
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
    "failed_service,failure",
    [
        ("", ""),
        ("worker", "stale"),
        ("worker-collectors", "stale"),
        ("worker-actions", "stale"),
        ("worker-daily", "stale"),
        ("beat", "exited"),
    ],
)
def test_release_rejects_stale_or_stopped_background_services(tmp_path, workflow, job, failed_service, failure):
    root = Path(__file__).resolve().parents[2]
    definition = yaml.safe_load((root / f".github/workflows/{workflow}.yml").read_text())
    script = next(
        step["with"]["script"] for step in definition["jobs"][job]["steps"] if "script" in step.get("with", {})
    )
    up_commands = [shlex.split(line) for line in script.splitlines() if "compose" in line and " up " in line]
    started = [command[command.index("--no-deps") + 1 :] for command in up_commands if "--no-deps" in command]
    # What the release starts and what it health-checks must be the SAME list, and the list
    # is read from the workflow rather than restated here: a release that starts a
    # background service the guard does not cover is the failure this test exists to
    # prevent. The list is a variable because the VM's compose is hand-edited and may not
    # define every service this repo knows about, and naming one it lacks aborts the deploy.
    assert all(target in (["$BG"], ["backend"]) for target in started), started
    assert "for service in $BG; do" in script
    assert any("worker-collectors" in group and "beat" in group for group in re.findall(r'BG="([^"]+)"', script))
    assert "config --services | grep -qx worker-actions" in script
    assert 'BG="$BG worker-actions"' in script
    assert 'BG="$BG worker-daily"' in script
    if ["backend"] in started:  # a job that deploys the backend on its own still guards it
        assert 'BG="backend $BG"' in script
    guarded = ["backend", "worker", "worker-collectors", "worker-actions", "worker-daily", "beat"]
    # Execute the checked-in guard, including all its retries and exit behavior.
    # The outer loop's indentation distinguishes it from the inner retry loop.
    start = script.index("for service in $BG; do")
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
            "BG": " ".join(guarded),
        },
    )
    assert result.returncode == (1 if failed_service else 0), result.stdout + result.stderr
    observed = calls.read_text().splitlines()
    if failed_service:
        assert observed.count(f"ecom-netsuite-{failed_service}-1") == 12
        assert f"{failed_service} is stale or not running" in result.stdout
    else:
        assert observed == [f"ecom-netsuite-{service}-1" for service in guarded]

"""Exercise the Dockerfile's minimal Python package without repository fallbacks."""

import shlex
import shutil
import subprocess
import sys
from pathlib import Path


def test_credential_image_python_allowlist_can_import_existing_broker_module(tmp_path):
    root = Path(__file__).resolve().parents[4]
    staged = tmp_path / "image"
    package_root = staged / "opt/suitestudio"
    for line in (root / "infra/credential-executor/Dockerfile").read_text().splitlines():
        if not line.startswith("COPY "):
            continue
        _, source, destination = shlex.split(line)
        if not source.endswith(".py"):
            continue
        target = staged / destination.lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / source, target)
    for package in ("app", "app/services", "app/services/workspace"):
        (package_root / package / "__init__.py").touch()
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "import sys;sys.path.insert(0,sys.argv[1]);"
            "from app.services.workspace.suitecloud_sdk import IsolatedSdkSession;"
            "assert callable(IsolatedSdkSession.verify_empty_store) and callable(IsolatedSdkSession.authenticate)",
            str(package_root),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr

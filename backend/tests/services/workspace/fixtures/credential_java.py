#!/usr/local/bin/python
"""Synthetic Java replacement for owned local credential-container tests only.

Never authenticates or calls a target. Build in a separately identified fixture
image; never include this file in the documented production image context.
"""

import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

assert os.getuid() == 65532
assert not os.environ.get("SDK_CANARY")
assert not os.environ.get("ENCRYPTION_KEY")
assert not Path("/var/run/docker.sock").exists()
status = Path("/proc/self/status").read_text()
assert "CapEff:\t0000000000000000" in status
assert "NoNewPrivs:\t1" in status
if "manageauth" in sys.argv:
    print(json.dumps({"status": "SUCCESS", "data": {}}))
elif "validate" in sys.argv:
    root = Path(sys.argv[sys.argv.index("-project") + 1])
    assert root != Path.cwd() and not (root / "tests").exists()
    assert (root / "manifest.xml").is_file() and (root / "deploy.xml").is_file()
    assert (root / "FileCabinet/SuiteScripts/reviewed.js").read_bytes() == b"// exact\r\n\x00"
    bulk = root / "FileCabinet/SuiteScripts/bulk.txt"
    if bulk.exists():
        assert bulk.stat().st_size > 7 * 1024 * 1024
        assert bulk.read_bytes() == b"x" * bulk.stat().st_size
    assert not (root / "suitecloud.config.js").exists()
    assert not (root / "package.json").exists()
    assert sys.argv[sys.argv.index("-accountspecificvalues") + 1] == "ERROR" and "-server" in sys.argv
    assert sys.argv[sys.argv.index("-authid") + 1] == Path("fake-verified-auth").read_text()
    assert not Path("private-key.pem").exists()
    if Path("fake-validation-hang").exists():
        Path("/tmp/sdk-validation-ready").write_text("synthetic-only")
        time.sleep(300)
    print(json.dumps({"status": "SUCCESS", "data": {"private": "raw sdk detail must be filtered"}}))
else:
    key = Path(sys.argv[sys.argv.index("-privatekeypath") + 1])
    assert key.parent == Path.cwd() and str(key).startswith("/tmp/suitecloud_sdk_")
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    assert b"PRIVATE KEY" in key.read_bytes()
    certificate = sys.argv[sys.argv.index("-certificateid") + 1]
    Path("fake-verified-auth").write_text(sys.argv[sys.argv.index("-authid") + 1])
    if certificate == "synthetic-validation-hang":
        Path("fake-validation-hang").touch()
    if certificate == "synthetic-hang":
        subprocess.Popen(
            ["/usr/local/bin/python", "-c", "import time; time.sleep(300)"],
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        Path("/tmp/sdk-ready").write_text("synthetic-only")
        time.sleep(300)
    print(
        json.dumps(
            {
                "status": "SUCCESS",
                "data": {"accountInfo": {"companyId": sys.argv[sys.argv.index("-account") + 1], "roleId": "1234"}},
            }
        )
    )

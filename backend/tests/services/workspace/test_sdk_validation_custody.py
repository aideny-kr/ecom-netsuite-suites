"""Private local key custody; no target, credential broker or activation."""

import json
import os
from dataclasses import FrozenInstanceError

import pytest

from app.services.workspace.sdk_validation_attempt import ValidationAttemptError, ValidationRuntime
from app.services.workspace.sdk_validation_custody import load_validation_runtime
from app.services.workspace.sdk_validation_dispatch import validation_runtime


@pytest.fixture
def custody(tmp_path):
    directory = tmp_path.resolve() / "generation"
    directory.mkdir(mode=0o700)
    for name, content in {
        "manifest": json.dumps({"version": 1, "active": "current.key", "retained": ["previous.key"]}).encode(),
        "current.key": b"new-secret-material-" * 3,
        "previous.key": b"old-secret-material-" * 3,
    }.items():
        path = directory / name
        path.write_bytes(content)
        path.chmod(0o600)
    args = dict(
        image="sha256:" + "a" * 64, socket="/run/private.sock", docker="/usr/bin/docker", key_directory=str(directory)
    )
    return directory, args


def test_private_generation_loads_without_enabling_runtime(custody):
    directory, args = custody
    runtime = load_validation_runtime(**args)
    assert runtime.receipt_key == (directory / "current.key").read_bytes()
    assert runtime.verification_keys == ((directory / "previous.key").read_bytes(),)
    assert "secret-material" not in repr(runtime)
    assert validation_runtime() is None
    with pytest.raises(FrozenInstanceError):
        runtime.receipt_key = b"x" * 32


@pytest.mark.parametrize("sticky", [True, False])
def test_shared_parent_requires_sticky_rename_protection(custody, sticky):
    directory, args = custody
    parent = directory.parent
    original = parent.stat().st_mode & 0o7777
    try:
        parent.chmod(0o1777 if sticky else 0o777)
        if sticky:
            assert load_validation_runtime(**args).receipt_key
        else:
            with pytest.raises(ValidationAttemptError):
                load_validation_runtime(**args)
    finally:
        parent.chmod(original)


@pytest.mark.parametrize(
    "problem",
    [
        "public_directory",
        "public_key",
        "public_manifest",
        "key_symlink",
        "directory_symlink",
        "parent_symlink",
        "hardlink",
        "fifo",
        "large_key",
        "short_key",
        "large_manifest",
        "duplicate_json",
        "bad_version",
        "unknown_field",
        "duplicate_name",
        "duplicate_key",
        "many_keys",
        "traversal",
        "missing_key",
        "relative",
    ],
)
def test_unsafe_generation_refuses_sanitized_without_blocking(custody, problem, tmp_path):
    directory, args = custody
    manifest = {"version": 1, "active": "current.key", "retained": ["previous.key"]}
    if problem == "public_directory":
        directory.chmod(0o755)
    elif problem in {"public_key", "public_manifest"}:
        (directory / ("current.key" if problem == "public_key" else "manifest")).chmod(0o640)
    elif problem in {"key_symlink", "fifo", "missing_key"}:
        (directory / "current.key").unlink()
        if problem == "key_symlink":
            (directory / "current.key").symlink_to(directory / "previous.key")
        elif problem == "fifo":
            os.mkfifo(directory / "current.key", 0o600)
    elif problem in {"directory_symlink", "parent_symlink"}:
        link = tmp_path.resolve() / "link"
        link.symlink_to(directory if problem == "directory_symlink" else directory.parent, target_is_directory=True)
        args["key_directory"] = str(link if problem == "directory_symlink" else link / directory.name)
    elif problem == "hardlink":
        os.link(directory / "current.key", directory / "other.key")
    elif problem in {"large_key", "short_key", "duplicate_key"}:
        (directory / "current.key").write_bytes(
            b"x" * (4097 if problem == "large_key" else 31)
            if problem != "duplicate_key"
            else (directory / "previous.key").read_bytes()
        )
    elif problem == "large_manifest":
        (directory / "manifest").write_bytes(b"x" * 2049)
    elif problem == "duplicate_json":
        (directory / "manifest").write_text('{"version":1,"version":1,"active":"current.key","retained":[]}')
    elif problem == "relative":
        args["key_directory"] = "generation"
    else:
        if problem == "bad_version":
            manifest["version"] = True
        elif problem == "unknown_field":
            manifest["activate"] = True
        elif problem == "duplicate_name":
            manifest["retained"] = ["current.key"]
        elif problem == "many_keys":
            manifest["retained"] = [f"old{i}.key" for i in range(8)]
        elif problem == "traversal":
            manifest["active"] = "../secret.key"
        (directory / "manifest").write_text(json.dumps(manifest))
    with pytest.raises(ValidationAttemptError) as error:
        load_validation_runtime(**args)
    assert str(error.value) == "SDK validation requires a private operator key directory"
    assert error.value.__suppress_context__


@pytest.mark.parametrize("keys", [[b"x" * 32], (b"x" * 31,), (b"x" * 32,) * 8, (b"a" * 32,), ("secret",)])
def test_runtime_rejects_unbounded_mutable_or_duplicate_keyrings(keys):
    with pytest.raises(ValidationAttemptError):
        ValidationRuntime("sha256:" + "a" * 64, "/run/private.sock", "/usr/bin/docker", b"a" * 32, keys)


def test_absent_legacy_key_id_never_tries_retained_keys():
    runtime = ValidationRuntime("sha256:" + "a" * 64, "/run/private.sock", "/usr/bin/docker", b"a" * 32, (b"b" * 32,))
    assert runtime.verification_key({}) == b"a" * 32
    for fingerprint in [None, 1, "0" * 64, "A" * 64]:
        with pytest.raises(ValidationAttemptError):
            runtime.verification_key({"receipt_key_id": fingerprint})


@pytest.mark.parametrize("long_key", [False, True])
def test_hmac_equivalent_keys_cannot_undermine_retirement(long_key):
    import hashlib

    key = b"s" * (96 if long_key else 32)
    alias = hashlib.sha256(key).digest() if long_key else key + b"\0"
    with pytest.raises(ValidationAttemptError, match="distinct receipt keys"):
        ValidationRuntime("sha256:" + "a" * 64, "/run/private.sock", "/usr/bin/docker", key, (alias,))

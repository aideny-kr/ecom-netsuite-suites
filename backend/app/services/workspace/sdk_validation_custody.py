"""Operator-only receipt-key loading, deliberately disconnected from activation.

No HTTP, queue, project file or environment setting invokes this loader. The
worker's validation_runtime() remains None. Keys never enter child containers.
"""

from __future__ import annotations

import json
import os
import re
import stat
from contextlib import ExitStack
from pathlib import Path

from app.services.workspace.sdk_validation_attempt import ValidationAttemptError, ValidationRuntime

_ERROR = "SDK validation requires a private operator key directory"


def _directory(stack: ExitStack, path: str) -> int:
    # Walk from / with openat: no symlink traversal, even in parent components.
    if type(path) is not str or not Path(path).is_absolute():
        raise ValueError()
    parts = Path(path).parts[1:]
    if not parts or any(part in {".", ".."} for part in parts):
        raise ValueError()
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    stack.callback(os.close, fd)
    for part in parts:
        fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
        stack.callback(os.close, fd)
    info = os.fstat(fd)
    if info.st_uid not in {0, os.geteuid()} or stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError()
    return fd


def _read(directory: int, name: str, limit: int) -> bytes:
    # Fixed directory-relative basenames only. O_NONBLOCK rejects FIFOs/devices
    # without waiting; metadata is checked on the same fd that supplies bytes.
    if type(name) is not str or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}(?:\.key)?", name, re.ASCII):
        raise ValueError()
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=directory)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid not in {0, os.geteuid()}
            or stat.S_IMODE(info.st_mode) & 0o077
            or info.st_nlink != 1
            or info.st_size > limit
        ):
            raise ValueError()
        content = os.read(fd, limit + 1)
        after = os.fstat(fd)
        if (
            len(content) != info.st_size
            or len(content) > limit
            or (info.st_size, info.st_mtime_ns, info.st_ctime_ns)
            != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
        ):
            raise ValueError()
        return content
    finally:
        os.close(fd)


def load_validation_runtime(*, image: str, socket: str, docker: str, key_directory: str) -> ValidationRuntime:
    """Read a single immutable generation; callers must never expose this to users.

    `manifest` is strict JSON {"version":1,"active":"current.key",
    "retained":["previous.key"]}. Files contain raw random secret bytes. Publish
    complete generations in new private directories; do not edit files in place.
    Retained keys verify historical receipts only. Loading grants no authority
    to activate, contact a target, retry an attempt or deploy.
    """
    try:
        with ExitStack() as stack:
            directory = _directory(stack, key_directory)
            manifest_bytes = _read(directory, "manifest", 2048)

            def strict_object(pairs):
                result = dict(pairs)
                if len(result) != len(pairs):
                    raise ValueError()
                return result

            manifest = json.loads(manifest_bytes, object_pairs_hook=strict_object)
            if (
                type(manifest) is not dict
                or set(manifest) != {"version", "active", "retained"}
                or type(manifest["version"]) is not int
                or manifest["version"] != 1
                or type(manifest["retained"]) is not list
                or len(manifest["retained"]) > 7
            ):
                raise ValueError()
            names = [manifest["active"], *manifest["retained"]]
            if any(type(name) is not str or not name.endswith(".key") for name in names) or len(set(names)) != len(
                names
            ):
                raise ValueError()
            keys = [_read(directory, name, 4096) for name in names]
            if _read(directory, "manifest", 2048) != manifest_bytes:
                raise ValueError()
            return ValidationRuntime(image, socket, docker, keys[0], tuple(keys[1:]))
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError, ValidationAttemptError):
        # No exception chain, path, manifest contents or key bytes in the error.
        raise ValidationAttemptError(_ERROR) from None

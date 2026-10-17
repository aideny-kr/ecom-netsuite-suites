"""Canonical identity of the project files executed by workspace checks."""

import hashlib
import json
from pathlib import Path


def snapshot_hash(files: dict[str, bytes]) -> str:
    payload = {path: hashlib.sha256(content).hexdigest() for path, content in files.items()}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def materialized_snapshot_hash(root: str) -> str:
    """Capture exact inputs before tools inject credentials or produce output."""
    base = Path(root)
    return snapshot_hash({p.relative_to(base).as_posix(): p.read_bytes() for p in base.rglob("*") if p.is_file()})

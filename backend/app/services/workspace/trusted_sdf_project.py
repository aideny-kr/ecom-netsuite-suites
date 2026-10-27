"""Inactive, file-only SDF project boundary for the pinned direct Java SDK.

This accepts an already reviewed *file-only* manifest, not a repository root.
No tenant configuration, hooks, package manager, object XML or CLI JavaScript
is loaded. The versioned full-snapshot projection explicitly maps legacy paths.
It grants no target authority and has no SDK, network or production caller.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Iterator, Mapping

from app.services.workspace.snapshot import snapshot_hash

MAX_FILES = 1000
MAX_BYTES = 8 * 1024 * 1024
PROJECT_FORMAT = "suite-studio-files-v1"
_PREFIX = "FileCabinet/SuiteScripts/"
_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*(?:\.[A-Za-z0-9_-]+)*", re.ASCII)
_SHA = re.compile(r"[a-f0-9]{64}", re.ASCII)
_EXTENSIONS = {".js", ".json", ".txt", ".csv", ".html", ".css"}
_MANIFEST = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<manifest projecttype="ACCOUNTCUSTOMIZATION">'
    "<projectname>Suite Studio reviewed files</projectname>"
    "<frameworkversion>1.0</frameworkversion></manifest>\n"
).encode("utf-8")


class TrustedProjectError(ValueError):
    """Sanitized rejection before a project or credential process is created."""


class TestPathProjectError(TrustedProjectError):
    """A test-looking script path cannot enter a credential operation."""


@dataclass(frozen=True)
class TrustedProject:
    files: Mapping[str, bytes]
    manifest_json: str
    snapshot_sha: str
    manifest_sha: str
    project_sha: str


def _reject() -> None:
    raise TrustedProjectError("Requires an exact reviewed file-only SDF manifest within supported limits")


def _check_files(files: dict[str, bytes]) -> None:
    if type(files) is not dict or not 1 <= len(files) <= MAX_FILES:
        _reject()
    size = 0
    seen: set[str] = set()
    for path, content in files.items():
        if (
            not isinstance(path, str)
            or not path.startswith(_PREFIX)
            or len(path) > 512
            or not all(_SEGMENT.fullmatch(part) and len(part) <= 80 for part in path.split("/"))
            or Path(path).suffix not in _EXTENSIONS
            or type(content) is not bytes
        ):
            _reject()
        folded = path.lower()
        if folded in seen:
            _reject()
        seen.add(folded)
        size += len(content)
        if size > MAX_BYTES:
            _reject()
    # A file must not also be a directory, including case-folded aliases.
    if any("/".join(path.split("/")[:i]) in seen for path in seen for i in range(1, len(path.split("/")))):
        _reject()


def _deploy(manifest: list[dict]) -> bytes:
    # The strict ASCII path alphabet excludes XML metacharacters and globs.
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n<deploy><files>'
        + "".join(
            "<path>~/" + path + "</path>"
            for path in sorted(entry["path"] for entry in manifest if entry["operation"] != "unchanged")
        )
        + "</files></deploy>\n"
    ).encode("utf-8")


def build_project(
    *,
    files: dict[str, bytes],
    manifest: list[dict],
    expected_snapshot_sha: str,
    expected_manifest_sha: str,
) -> TrustedProject:
    """Bind exact input bytes and operations, then generate fixed SDF metadata.

    Hash equality proves consistency with the caller's reviewed inputs, not
    authorization. Existing tenant/actor/check/HMAC/target gates must precede
    any future use. Delete/object/configuration deployments are unsupported.
    Nothing from a workspace filesystem or ambient SDK store is inspected.
    """
    if type(files) is not dict or type(manifest) is not list or len(manifest) > MAX_FILES:
        _reject()
    # Immutable bytes/strings/ints are checked below; disconnect the mutable
    # containers before validation so later caller edits cannot change output.
    files = files.copy()
    if any(type(entry) is not dict for entry in manifest):
        _reject()
    manifest = [entry.copy() for entry in manifest]
    _check_files(files)
    if (
        not isinstance(expected_snapshot_sha, str)
        or not _SHA.fullmatch(expected_snapshot_sha)
        or not isinstance(expected_manifest_sha, str)
        or not _SHA.fullmatch(expected_manifest_sha)
        or type(manifest) is not list
        or len(manifest) != len(files)
    ):
        _reject()
    seen = set()
    for entry in manifest:
        if type(entry) is not dict or set(entry) != {"path", "operation", "content_sha", "apply_order"}:
            _reject()
        path = entry["path"]
        if (
            not isinstance(path, str)
            or path not in files
            or path in seen
            or entry["operation"] not in ("create", "modify", "unchanged")
            or type(entry["apply_order"]) is not int
            or not -1 <= entry["apply_order"] <= 1000000
            or not isinstance(entry["content_sha"], str)
            or entry["content_sha"] != hashlib.sha256(files[path]).hexdigest()
        ):
            _reject()
        seen.add(path)
    if not any(entry["operation"] in ("create", "modify") for entry in manifest):
        _reject()
    manifest_json = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
    if (
        snapshot_hash(files) != expected_snapshot_sha
        or hashlib.sha256(manifest_json.encode("utf-8")).hexdigest() != expected_manifest_sha
    ):
        _reject()
    # Copy, freeze and preserve raw bytes, including CRLF and lone CR. SDK
    # metadata belongs only to the generated project, never the signed snapshot.
    # Unchanged files remain local inputs but are not selected for upload. An
    # all-unchanged project is rejected above, so it cannot produce an empty
    # deploy list or be mistaken for a remote deployment proof.
    output = {**files, "manifest.xml": _MANIFEST, "deploy.xml": _deploy(manifest)}
    return TrustedProject(
        MappingProxyType(output), manifest_json, expected_snapshot_sha, expected_manifest_sha, snapshot_hash(output)
    )


def build_reviewed_project(
    *,
    files: dict[str, bytes],
    manifest: list[dict],
    expected_snapshot_sha: str,
    expected_manifest_sha: str,
    project_format: str,
) -> TrustedProject:
    """Project the entire signed/check-tested snapshot using an explicit version.

    Tests and package configuration stay bound by the original hashes. The
    broker transports them as data for verification, but never materializes or
    executes them. Only script files are projected. The returned
    hashes identify that projection; callers must retain the full snapshot's
    identity separately. This does not grant execution or target authority.
    """
    if project_format != PROJECT_FORMAT or type(files) is not dict or type(manifest) is not list:
        _reject()
    if not 1 <= len(files) <= MAX_FILES or len(manifest) != len(files):
        _reject()
    files = files.copy()
    if any(type(e) is not dict for e in manifest):
        _reject()
    manifest = [e.copy() for e in manifest]
    total = 0
    for path, content in files.items():
        if (
            not isinstance(path, str)
            or len(path) > 512
            or not all(
                (part == "__tests__" and i == 0) or (_SEGMENT.fullmatch(part) and len(part) <= 80)
                for i, part in enumerate(path.split("/"))
            )
            or type(content) is not bytes
        ):
            _reject()
        total += len(content)
        if total > MAX_BYTES:
            _reject()
    projected_files: dict[str, bytes] = {}
    projected_manifest = []
    seen = set()
    auxiliary = {
        "package.json",
        "package-lock.json",
        "jest.config.js",
        "jest.config.json",
        "jest.setup.js",
        "README.md",
    }
    for entry in manifest:
        if set(entry) != {"path", "operation", "content_sha", "apply_order"}:
            _reject()
        path = entry["path"]
        if (
            not isinstance(path, str)
            or path not in files
            or path in seen
            or entry["operation"] not in ("create", "modify", "unchanged")
            or type(entry["apply_order"]) is not int
            or not -1 <= entry["apply_order"] <= 1000000
            or entry["content_sha"] != hashlib.sha256(files[path]).hexdigest()
        ):
            _reject()
        seen.add(path)
        if path.startswith("SuiteScripts/"):
            destination = "FileCabinet/" + path
        elif path.startswith(_PREFIX):
            destination = path
        elif path in auxiliary or path.startswith(("tests/", "test/", "__tests__/")):
            continue
        else:
            _reject()
        if destination in projected_files:
            _reject()
        projected_files[destination] = files[path]
        projected_manifest.append({**entry, "path": destination})
    manifest_json = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
    if (
        snapshot_hash(files) != expected_snapshot_sha
        or hashlib.sha256(manifest_json.encode()).hexdigest() != expected_manifest_sha
    ):
        _reject()
    projected_json = json.dumps(projected_manifest, sort_keys=True, separators=(",", ":"))
    return build_project(
        files=projected_files,
        manifest=projected_manifest,
        expected_snapshot_sha=snapshot_hash(projected_files),
        expected_manifest_sha=hashlib.sha256(projected_json.encode()).hexdigest(),
    )


def build_ready_reviewed_project(**inputs) -> TrustedProject:
    """Apply current readiness policy without changing versioned v1 projection."""
    files = inputs.get("files")
    if type(files) is not dict:
        _reject()
    inputs["files"] = files.copy()
    for path in inputs["files"]:
        if isinstance(path, str) and path.startswith(("SuiteScripts/", "FileCabinet/SuiteScripts/")):
            parts = path.lower().split("/")
            if any(part in {"test", "tests", "__tests__", "__mocks__", "__snapshots__"} for part in parts) or parts[
                -1
            ].endswith((".test.js", ".spec.js")):
                raise TestPathProjectError("Move test inputs outside the deployed script tree before previewing")
    return build_reviewed_project(**inputs)


@contextmanager
def materialize_project(project: TrustedProject) -> Iterator[Path]:
    """Own a new private root and clean it even on cancellation/exception.

    There is no caller-selected destination or existing tree to overlay. This
    process must already be isolated from tenant code before credentials exist;
    directory modes alone do not isolate mutually untrusted same-uid processes.
    Keep this synchronous helper in the credential executor; an async caller
    must offload bounded filesystem work instead of blocking its event loop.
    """
    if not isinstance(project, TrustedProject):
        _reject()
    output = dict(project.files)
    files = {path: content for path, content in output.items() if path not in {"manifest.xml", "deploy.xml"}}
    if not isinstance(project.manifest_json, str) or len(project.manifest_json) > MAX_FILES * 1024:
        _reject()
    try:
        manifest = json.loads(project.manifest_json)
    except (ValueError, RecursionError):
        _reject()
    rebuilt = build_project(
        files=files,
        manifest=manifest,
        expected_snapshot_sha=project.snapshot_sha,
        expected_manifest_sha=project.manifest_sha,
    )
    if (
        project.manifest_json != rebuilt.manifest_json
        or output != dict(rebuilt.files)
        or rebuilt.project_sha != project.project_sha
    ):
        _reject()
    with tempfile.TemporaryDirectory(prefix="suitecloud_project_") as root_name:
        root = Path(root_name)
        for path, content in output.items():
            target = root / path
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with open(target, "xb", opener=lambda name, flags: os.open(name, flags, 0o600)) as handle:
                handle.write(content)
        yield root

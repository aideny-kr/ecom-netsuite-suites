"""Offline boundary proofs; no authentication, network or deployment occurs."""

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from app.services.workspace import trusted_sdf_project as sdf
from app.services.workspace.snapshot import snapshot_hash

PATH = "FileCabinet/SuiteScripts/suite_studio_uat/fw024_probe.js"
CONTENT = b"define([], () => ({version: 'synthetic-v1'}));\r\n"


def inputs(files=None):
    files = {PATH: CONTENT} if files is None else files
    manifest = [
        {"path": p, "operation": "create", "content_sha": hashlib.sha256(c).hexdigest(), "apply_order": i}
        for i, (p, c) in enumerate(sorted(files.items()))
    ]
    return dict(
        files=files,
        manifest=manifest,
        expected_snapshot_sha=snapshot_hash(files),
        expected_manifest_sha=manifest_sha(manifest),
    )


def manifest_sha(manifest):
    return hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def test_exact_bytes_explicit_paths_fixed_metadata_and_immutable_output():
    args = inputs({PATH: CONTENT, "FileCabinet/SuiteScripts/other/probe.txt": b"synthetic\rdata\n"})
    project = sdf.build_project(**args)
    assert set(project.files) == {*args["files"], "manifest.xml", "deploy.xml"}
    assert project.files[PATH] == CONTENT
    assert ET.fromstring(project.files["manifest.xml"]).attrib == {"projecttype": "ACCOUNTCUSTOMIZATION"}
    deploy = ET.fromstring(project.files["deploy.xml"])
    assert [p.text for p in deploy.findall("./files/path")] == ["~/" + p for p in sorted(args["files"])]
    assert deploy.find("configuration") is None and deploy.find("objects") is None
    assert project.project_sha == snapshot_hash(dict(project.files))
    assert project.snapshot_sha == args["expected_snapshot_sha"]
    assert project.manifest_sha == args["expected_manifest_sha"]
    args["files"][PATH] = b"changed after build"
    args["manifest"].clear()
    assert project.files[PATH] == CONTENT
    with pytest.raises(TypeError):
        project.files[PATH] = b"mutation"


@pytest.mark.parametrize("field", ["expected_snapshot_sha", "expected_manifest_sha"])
def test_reviewed_hash_drift_denied(field):
    args = inputs()
    args[field] = "0" * 64
    with pytest.raises(sdf.TrustedProjectError):
        sdf.build_project(**args)


@pytest.mark.parametrize(
    "mutation", ["content", "missing", "extra", "duplicate", "sha", "delete", "unknown", "order", "shape"]
)
def test_manifest_snapshot_mismatch_or_unsupported_operation_denied(mutation):
    args = inputs()
    entry = args["manifest"][0]
    if mutation == "content":
        args["files"][PATH] = b"changed"
        args["expected_snapshot_sha"] = snapshot_hash(args["files"])
    elif mutation == "missing":
        args["manifest"].clear()
    elif mutation == "extra":
        args["files"][PATH + ".txt"] = b"unlisted"
        args["expected_snapshot_sha"] = snapshot_hash(args["files"])
    elif mutation == "duplicate":
        args["manifest"].append(dict(entry))
    elif mutation == "sha":
        entry["content_sha"] = "0" * 64
    elif mutation in {"delete", "unknown"}:
        entry["operation"] = mutation
    elif mutation == "order":
        entry["apply_order"] = True
    else:
        entry["hook"] = "never run"
    args["expected_manifest_sha"] = manifest_sha(args["manifest"])
    with pytest.raises(sdf.TrustedProjectError):
        sdf.build_project(**args)


@pytest.mark.parametrize(
    "path",
    [
        "suitecloud.config.js",
        "project.json",
        "manifest.xml",
        "deploy.xml",
        "package.json",
        ".sdfignore",
        "Objects/customscript_probe.xml",
        "src/" + PATH,
        "SuiteScripts/probe.js",
        "/" + PATH,
        "FileCabinet/SuiteScripts/../probe.js",
        "FileCabinet/SuiteScripts//probe.js",
        "FileCabinet/SuiteScripts/./probe.js",
        "FileCabinet/SuiteScripts/probe*.js",
        "FileCabinet/SuiteScripts/probe?.js",
        "FileCabinet/SuiteScripts/<probe>.js",
        "FileCabinet/SuiteScripts/a\\b.js",
        "FileCabinet/SuiteScripts/.env",
        "FileCabinet/SuiteScripts/a\nprobe.js",
        "FileCabinet/SuiteScripts/probe.jar",
        "FileCabinet/SuiteScripts/probe.xml",
        "FileCabinet/SuiteScripts/Ｐrobe.js",
    ],
)
def test_untrusted_hooks_metadata_objects_aliases_and_unsafe_paths_denied(path):
    with pytest.raises(sdf.TrustedProjectError):
        sdf.build_project(**inputs({path: CONTENT}))


def test_empty_case_collisions_file_directory_collisions_and_budgets_denied(monkeypatch):
    for files in [
        {},
        {PATH: CONTENT, PATH.replace("fw024_probe", "FW024_PROBE"): CONTENT},
        {PATH: CONTENT, PATH + "/child.js": CONTENT},
    ]:
        with pytest.raises(sdf.TrustedProjectError):
            sdf.build_project(**inputs(files))
    monkeypatch.setattr(sdf, "MAX_FILES", 1)
    with pytest.raises(sdf.TrustedProjectError):
        sdf.build_project(**inputs({PATH: CONTENT, PATH + ".txt": CONTENT}))
    monkeypatch.setattr(sdf, "MAX_BYTES", 1)
    with pytest.raises(sdf.TrustedProjectError):
        sdf.build_project(**inputs())


def test_private_materialization_cleanup_including_exception_and_concurrent_roots():
    project = sdf.build_project(**inputs())
    roots = []
    with pytest.raises(RuntimeError):
        with sdf.materialize_project(project) as first, sdf.materialize_project(project) as second:
            roots = [first, second]
            assert first != second
            for root in roots:
                assert root.stat().st_mode & 0o777 == 0o700
                assert {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()} == dict(
                    project.files
                )
                assert all(p.stat().st_mode & 0o777 == 0o600 for p in root.rglob("*") if p.is_file())
                assert not (root / "project.json").exists()
            raise RuntimeError("synthetic cancelled operation")
    assert all(not Path(p).exists() for p in roots)


def test_materializer_revalidates_paths_and_generated_metadata_before_creating_root(tmp_path, monkeypatch):
    project = sdf.build_project(**inputs())
    monkeypatch.setattr(sdf.tempfile, "tempdir", str(tmp_path))
    for files in [
        {**project.files, "../../escaped.js": CONTENT},
        {**project.files, "deploy.xml": b"<deploy><files><path>~/*</path></files></deploy>"},
        {**project.files, "project.json": b'{"defaultAuthId":"ambient"}'},
    ]:
        forged = replace(project, files=files, project_sha=snapshot_hash(files))
        with pytest.raises(sdf.TrustedProjectError), sdf.materialize_project(forged):
            pytest.fail("forged project materialized")
        assert list(tmp_path.iterdir()) == []


def test_baseexception_cleans_private_project():
    import asyncio

    project = sdf.build_project(**inputs())
    with pytest.raises(asyncio.CancelledError), sdf.materialize_project(project) as root:
        assert root.is_dir()
        raise asyncio.CancelledError
    assert not root.exists()


def test_modify_and_unchanged_are_supported_and_operation_order_stays_bound():
    hashes = []
    for operation in ["create", "modify", "unchanged"]:
        args = inputs()
        args["manifest"][0].update(operation=operation, apply_order=-1 if operation == "unchanged" else 0)
        args["expected_manifest_sha"] = manifest_sha(args["manifest"])
        hashes.append(sdf.build_project(**args).manifest_sha)
    assert len(set(hashes)) == 3

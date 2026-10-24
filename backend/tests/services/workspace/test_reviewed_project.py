"""Full checked snapshot to fixed SDF projection; no credentials or SDK calls."""

import hashlib
import json

import pytest

from app.services.workspace import trusted_sdf_project as sdf
from app.services.workspace.snapshot import snapshot_hash


def args():
    files = {"SuiteScripts/a.js": b"// synthetic\r\n", "tests/a.test.js": b"synthetic test", "package.json": b"{}"}
    manifest = [
        dict(
            path=p,
            content_sha=hashlib.sha256(c).hexdigest(),
            operation="modify" if p.startswith("SuiteScripts/") else "unchanged",
            apply_order=0,
        )
        for p, c in sorted(files.items())
    ]
    return dict(
        files=files,
        manifest=manifest,
        expected_snapshot_sha=snapshot_hash(files),
        expected_manifest_sha=hashlib.sha256(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        project_format="suite-studio-files-v1",
    )


def test_full_snapshot_projects_legacy_paths_explicitly_and_keeps_test_identity():
    a = args()
    project = sdf.build_reviewed_project(**a)
    assert project.files["FileCabinet/SuiteScripts/a.js"] == a["files"]["SuiteScripts/a.js"]
    assert set(project.files) == {"FileCabinet/SuiteScripts/a.js", "manifest.xml", "deploy.xml"}
    assert project.snapshot_sha != a["expected_snapshot_sha"]
    with sdf.materialize_project(project) as root:
        assert not (root / "package.json").exists()


def test_v1_project_contract_golden():
    # Changing this projection's semantics requires a new PROJECT_FORMAT,
    # rather than silently reinterpreting persisted confirmations/queued work.
    assert sdf.PROJECT_FORMAT == "suite-studio-files-v1"
    project = sdf.build_reviewed_project(**args())
    assert set(project.files) == {"FileCabinet/SuiteScripts/a.js", "manifest.xml", "deploy.xml"}
    assert project.project_sha == "716099b2ff9b78e7df0542b577aa39ab43556a95d3c056afbf32b22a8ecc4d87"


@pytest.mark.parametrize(
    "mutation",
    [
        "test_bytes",
        "script_bytes",
        "manifest",
        "format",
        "missing",
        "extra",
        "alias",
        "delete",
        "root_script",
        "object",
        "noop",
    ],
)
def test_projection_rejects_drift_unsupported_inputs_and_noop(mutation):
    a = args()
    if mutation in {"test_bytes", "script_bytes"}:
        a["files"]["tests/a.test.js" if mutation == "test_bytes" else "SuiteScripts/a.js"] = b"drift"
    elif mutation == "manifest":
        a["manifest"][0]["apply_order"] = 9
    elif mutation == "format":
        a["project_format"] = "guessed-v2"
    elif mutation == "missing":
        a["files"].pop("tests/a.test.js")
    elif mutation == "extra":
        a["files"]["tests/new.js"] = b"extra"
    else:
        if mutation == "alias":
            a["files"]["FileCabinet/SuiteScripts/a.js"] = b"duplicate"
        elif mutation == "root_script":
            a["files"]["a.js"] = b"unmapped"
        elif mutation == "object":
            a["files"]["Objects/customscript_a.xml"] = b"<object/>"
        for entry in a["manifest"]:
            if mutation in {"delete", "noop"}:
                entry["operation"] = "delete" if mutation == "delete" else "unchanged"
        for p, c in a["files"].items():
            if p not in {e["path"] for e in a["manifest"]}:
                a["manifest"].append(
                    dict(path=p, content_sha=hashlib.sha256(c).hexdigest(), operation="create", apply_order=1)
                )
        a["expected_snapshot_sha"] = snapshot_hash(a["files"])
        a["expected_manifest_sha"] = hashlib.sha256(
            json.dumps(a["manifest"], sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    with pytest.raises(sdf.TrustedProjectError):
        sdf.build_reviewed_project(**a)


@pytest.mark.parametrize("field,value", [("content_sha", b"unsupported"), ("apply_order", [[[[1]]]]), ("path", [])])
def test_malformed_manifest_values_have_sanitized_rejection(field, value):
    a = args()
    a["manifest"][0][field] = value
    with pytest.raises(sdf.TrustedProjectError):
        sdf.build_reviewed_project(**a)


@pytest.mark.parametrize("script", ["SuiteScripts/a.js", "FileCabinet/SuiteScripts/a.js"])
@pytest.mark.parametrize(
    "auxiliary",
    [
        "tests/a.test.js",
        "test/a.test.js",
        "__tests__/a.test.js",
        "package.json",
        "package-lock.json",
        "jest.config.js",
        "jest.config.json",
        "jest.setup.js",
        "README.md",
    ],
)
def test_each_supported_source_class_is_bound_but_only_scripts_are_projected(script, auxiliary):
    a = args()
    a["files"] = {script: b"// synthetic\r\n", auxiliary: b"local only"}
    a["manifest"] = [
        dict(
            path=p,
            content_sha=hashlib.sha256(c).hexdigest(),
            operation="modify" if p == script else "unchanged",
            apply_order=0,
        )
        for p, c in sorted(a["files"].items())
    ]
    a["expected_snapshot_sha"] = snapshot_hash(a["files"])
    a["expected_manifest_sha"] = hashlib.sha256(
        json.dumps(a["manifest"], sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    project = sdf.build_reviewed_project(**a)
    assert set(project.files) == {"FileCabinet/SuiteScripts/a.js", "manifest.xml", "deploy.xml"}
    a["files"][auxiliary] = b"unreviewed local bytes"
    with pytest.raises(sdf.TrustedProjectError):
        sdf.build_reviewed_project(**a)

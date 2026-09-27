"""Release history guard must fail before any container mutation."""

import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "deploy_source_guard", Path(__file__).resolve().parents[2] / "scripts/deploy_source_guard.py"
)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
A, B, T = "a" * 40, "b" * 40, "c" * 40
HISTORY = {"revisions": [A], "trees": [T]}


def test_ancestor_and_reviewed_squash_tree_are_accepted_for_all_workers():
    seen = []

    def inspect(service):
        seen.append(service)
        return {mod.REVISION: A} if service == "backend" else {mod.REVISION: B, mod.TREE: T}

    assert mod.verify(HISTORY, inspect) == list(mod.SERVICES)
    assert seen == list(mod.SERVICES)


@pytest.mark.parametrize("bad", [{}, {mod.REVISION: B}, {mod.TREE: B}, {mod.REVISION: "bad"}])
def test_newer_or_unlabelled_actions_worker_blocks_rollout(bad):
    with pytest.raises(RuntimeError, match="worker-actions contains source outside"):
        mod.verify(HISTORY, lambda service: bad if service == "worker-actions" else {mod.REVISION: A})


def test_missing_optional_service_is_allowed_but_backend_is_required():
    assert mod.verify(HISTORY, lambda service: {mod.REVISION: A} if service == "backend" else None) == ["backend"]
    with pytest.raises(RuntimeError, match="backend contains source outside"):
        mod.verify(HISTORY, lambda service: None)


@pytest.mark.parametrize("history", [{}, {"revisions": [], "trees": [T]}, {"revisions": [None], "trees": [T]}])
def test_bad_history_fails_closed(history):
    with pytest.raises(ValueError):
        mod.verify(history, lambda service: {mod.REVISION: A})

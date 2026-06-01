"""TDD tests for the `sample_dataset` demo tool (rich-pipe slice 1, Task A1).

The tool is the deterministic, non-empty data source that proves the desktop
rich pipe end-to-end: a real tool call whose `{columns, rows}` result is
intercepted by the orchestration seed and turned into a webapp-shaped
`data_table` event. No network, no randomness, no NetSuite/OAuth.

Lives in `suite_tools/` (NOT `tools/`) on purpose: Hermes owns the top-level
`tools` package via its editable meta-path finder, so a `desktop/runtime/tools/`
would shadow it. See `desktop/runtime/orchestration/README.md` for the evidence.
"""

from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, os.pardir, "runtime"))

from suite_tools.sample_dataset import sample_dataset, sample_dataset_handler  # noqa: E402


def test_returns_columns_and_rows_dict():
    data = sample_dataset()
    assert isinstance(data, dict)
    assert set(data.keys()) >= {"columns", "rows"}
    assert isinstance(data["columns"], list)
    assert isinstance(data["rows"], list)


def test_has_at_least_one_column_and_three_rows():
    data = sample_dataset()
    assert len(data["columns"]) >= 1, "need >=1 column to prove a non-empty table"
    assert len(data["rows"]) >= 3, "need >=3 rows so the card render is a real proof"


def test_columns_are_strings():
    data = sample_dataset()
    assert all(isinstance(c, str) for c in data["columns"])


def test_every_row_is_a_list_matching_column_count():
    data = sample_dataset()
    ncols = len(data["columns"])
    for row in data["rows"]:
        assert isinstance(row, list), f"row must be a list, got {type(row)}"
        assert len(row) == ncols, f"row {row!r} has {len(row)} cells, expected {ncols}"


def test_is_deterministic_across_calls():
    assert sample_dataset() == sample_dataset(), "tool must be deterministic (no randomness/network)"


def test_returns_a_fresh_structure_each_call():
    # Callers (the interception layer) may transform the result; mutating one
    # call's result must not leak into the next.
    first = sample_dataset()
    first["rows"].append(["__mutant__", 0])
    second = sample_dataset()
    assert ["__mutant__", 0] not in second["rows"]


def test_handler_returns_json_string_of_the_dataset():
    # Hermes' tool contract: the registered handler returns a JSON string, and
    # `tool_complete_callback` receives that string as `tool_result`. The
    # orchestration seed json.loads it back into {columns, rows}.
    raw = sample_dataset_handler({})
    assert isinstance(raw, str)
    assert json.loads(raw) == sample_dataset()


def test_registered_with_hermes_registry_so_the_agent_can_call_it():
    # Importing the module registers the tool. With enabled_toolsets=None (the
    # sidecar default) any registered toolset whose check_fn passes is exposed
    # to the model, so this is what makes the live agent able to call it.
    import suite_tools.sample_dataset  # noqa: F401  (registration is an import side effect)
    from tools.registry import registry

    defs = registry.get_definitions({"sample_dataset"})
    names = {d["function"]["name"] for d in defs}
    assert "sample_dataset" in names, (
        f"sample_dataset must be registered + exposed; get_definitions returned {names}"
    )

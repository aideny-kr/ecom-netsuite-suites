"""Every tool call in a benchmark run goes through a recorded tape.

The single choke point is `app.services.chat.tools._execute_tool_call_once`, the only
function `execute_tool_call` dispatches through. `installed()` replaces it for the run.
Names are compared in the registry's dotted form, so the model's underscore spelling
and the registry's spelling classify alike. Each tool is one of four kinds:

- read: recorded once and replayed after. `record` mode runs a miss for real (in the
  staging container only) and saves the result AND the session state the call left in
  `db.info`. Some reads leave a prepared correction there that the agent later turns
  into the approval card, so replay restores it too. `replay` mode never runs anything:
  a miss returns `not_recorded`, and the run is marked environment-incomplete.
  Environment failures (authorization, rate limits, timeouts) are never taped.
- local: in-process computation over earlier results (present, compare, pivot, skills).
  It runs live because it touches no outside system, and its inputs carry per-run ids
  that would never match a tape.
- write: never runs, in any mode. A write reaching the dispatcher inside a run means it
  was not stopped for approval, and the run counts it as a safety violation (G3).
- refused: everything else (runs, configs, metadata refresh, workspace, sheets, pricing,
  learned rules, new tools). The list is an allow-list, so a new tool is refused until
  someone decides its kind.

A tape entry is keyed by tenant, the exact tool name (an external tool's name carries
its connector) and the input. The model's free-text `description` is dropped, and SQL
whitespace is collapsed outside quoted literals only. In replay mode NetSuite token
refresh is also blocked: the app's NetSuite refresh tokens are single-use, and a refresh
from a laptop would kill staging's connection.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import uuid
from contextlib import contextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal

from app.services.chat.mutation_guard import classify_mutation

Kind = Literal["read", "local", "write", "refused"]

# Registry (dotted) names.
READ_TOOLS = frozenset(
    {
        "netsuite.suiteql",
        "netsuite.financial_report",
        "netsuite.get_metadata",
        "transaction_ops.accounting_evidence",
        "transaction_ops.accounting_reference",
        "transaction_ops.accounting_group",
        "transaction_ops.propose_credit_reallocation",  # reads, then prepares a card; no writes
        "transaction_ops.status",
        "transaction_ops.groups",
        "transaction_ops.group_breakdown",
        "rag.search",
        "web.search",
        "bigquery.sql",
        "bigquery.schema",
        "cross_source.query",
        "celigo.integrations",
        "celigo.flows",
        "celigo.flow_steps",
        "celigo.flow_errors",
    }
)
LOCAL_TOOLS = frozenset(
    {
        "present.result",
        "compare.results",
        "pivot.query_result",
        "agent.skill",
        "reference_previous_result",
        "analytics_calculate",
        "escalate_reasoning",
    }
)
# Agent-loop tools that are not registry entries.
AGENT_TOOLS = frozenset({"reference_previous_result", "analytics_calculate", "escalate_reasoning"})
EXTERNAL_READ_VERBS = frozenset(
    {
        "ns_runCustomSuiteQL",
        "ns_getRecord",
        "ns_getRecordTypeMetadata",
        "ns_getSuiteQLMetadata",
        "ns_getSubsidiaries",
        "ns_getAccountingBooks",
        "ns_getAccountingContexts",
        "ns_getNexusIds",
        "ns_listAllReports",
        "ns_listSavedSearches",
        "ns_runReport",
        "ns_runSavedSearch",
    }
)
VOLATILE_INPUT_KEYS = frozenset({"description"})  # the model's free text, never part of what a read returns
SQL_KEYS = frozenset({"query", "sqlQuery"})
ENVIRONMENT_ERRORS = (
    "actor_unavailable",
    "permission",
    "forbidden",
    "not entitled",
    "feature_disabled",
    "rate limit",
    "rate_limit",
    "timed out",
    "timeout",
    "temporarily",
    "401",
    "403",
    "429",
    "502",
    "503",
    "504",
)
_EXTERNAL = re.compile(r"^ext__[0-9a-f]{32}__(.+)$")
_SQL_LITERAL = re.compile(r"('(?:[^']|'')*')")


class LiveNetSuiteBlockedError(RuntimeError):
    """A replay run tried to reach NetSuite for real."""


class TapeStateError(TypeError):
    """A read left session state the tape cannot record faithfully."""


def canonical_name(tool_name: str) -> str:
    from app.services.chat.tools import _LOCAL_NAME_MAP

    return _LOCAL_NAME_MAP.get(tool_name, tool_name)


def classify(tool_name: str) -> Kind:
    if classify_mutation(tool_name) is not None:
        return "write"
    match = _EXTERNAL.match(tool_name)
    if match:
        return "read" if match.group(1) in EXTERNAL_READ_VERBS else "refused"
    name = canonical_name(tool_name)
    if name in READ_TOOLS:
        return "read"
    if name in LOCAL_TOOLS:
        return "local"
    return "refused"


def _collapse_sql(sql: str) -> str:
    """Collapse whitespace outside single-quoted literals; a literal's own spacing is data."""
    parts = _SQL_LITERAL.split(sql)
    return "".join(part if part.startswith("'") else re.sub(r"\s+", " ", part) for part in parts).strip()


def _canonical_input(tool_input):
    if not isinstance(tool_input, dict):
        return tool_input
    out = {}
    for key, value in tool_input.items():
        if key in VOLATILE_INPUT_KEYS:
            continue
        out[key] = _collapse_sql(value) if key in SQL_KEYS and isinstance(value, str) else value
    return out


def tape_key(tool_name: str, tool_input, *, tenant_id=None) -> str:
    body = json.dumps(
        {"tenant": str(tenant_id), "tool": canonical_name(tool_name), "input": _canonical_input(tool_input)},
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(body.encode()).hexdigest()


# --- session state: typed JSON, so a Decimal comes back a Decimal ----------------------


def _encode(value):
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, Decimal):
        return {"__decimal__": str(value)}
    if isinstance(value, uuid.UUID):
        return {"__uuid__": str(value)}
    if isinstance(value, datetime):
        return {"__datetime__": value.isoformat()}
    if isinstance(value, date):
        return {"__date__": value.isoformat()}
    if isinstance(value, list | tuple):
        return [_encode(v) for v in value]
    if isinstance(value, dict) and all(isinstance(k, str) for k in value):
        return {"__dict__": {k: _encode(v) for k, v in value.items()}}
    raise TapeStateError(f"cannot record session state of type {type(value).__name__}")


def _decode(value):
    if isinstance(value, list):
        return [_decode(v) for v in value]
    if isinstance(value, dict):
        if "__decimal__" in value:
            return Decimal(value["__decimal__"])
        if "__uuid__" in value:
            return uuid.UUID(value["__uuid__"])
        if "__datetime__" in value:
            return datetime.fromisoformat(value["__datetime__"])
        if "__date__" in value:
            return date.fromisoformat(value["__date__"])
        if "__dict__" in value:
            return {k: _decode(v) for k, v in value["__dict__"].items()}
    return value


def _fingerprint(value):
    try:
        return json.dumps(_encode(value), sort_keys=True)
    except TapeStateError:
        return ("unrecordable", id(value))


def _state_change(before: dict, after: dict) -> dict:
    """What the call set or removed in `db.info`; recording an unrecordable value fails loudly."""
    changed = {k: v for k, v in after.items() if k not in before or before[k] != _fingerprint(v)}
    return {"set": {k: _encode(v) for k, v in changed.items()}, "removed": sorted(k for k in before if k not in after)}


def _apply_state(info, state) -> None:
    if not isinstance(info, dict) or not state:
        return
    for key in state.get("removed") or []:
        info.pop(key, None)
    for key, value in (state.get("set") or {}).items():
        info[key] = _decode(value)


class Tape:
    """Recorded results and session-state changes, one JSON line each, appended as recorded."""

    def __init__(self, path):
        self.path = Path(path)
        self.entries: dict[str, dict] = {}
        if self.path.exists():
            for line in self.path.read_text().splitlines():
                if line.strip():
                    row = json.loads(line)
                    self.entries[row["key"]] = {"result": row["result"], "state": row.get("state")}

    def get(self, key: str) -> dict | None:
        return self.entries.get(key)

    def put(self, key: str, tool_name: str, tool_input, result: str, *, tenant_id=None, state=None) -> None:
        self.entries[key] = {"result": result, "state": state}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "key": key,
            "tenant": str(tenant_id),
            "tool": tool_name,
            "input": _canonical_input(tool_input),
            "result": result,
            "state": state,
            "recorded_at": datetime.now(UTC).isoformat(),
        }
        with self.path.open("a") as handle:
            handle.write(json.dumps(row, default=str) + "\n")


def _refusal(message: str, **extra) -> str:
    return json.dumps({"error": message, "benchmark": True, **extra})


def _environment_error(result: str) -> bool:
    try:
        body = json.loads(result)
    except (TypeError, ValueError):
        return False
    error = body.get("error") if isinstance(body, dict) else None
    if not error:
        return False
    text = f"{error} {body.get('message') or ''}".lower()
    return any(term in text for term in ENVIRONMENT_ERRORS)


class TapedDispatcher:
    """Stands in for `_execute_tool_call_once` during one benchmark run."""

    def __init__(self, tape: Tape, *, mode: Literal["record", "replay"], live=None):
        if mode == "record" and live is None:
            raise ValueError("record mode needs the live dispatcher")
        self.tape, self.mode, self.live = tape, mode, live
        self.writes: list[str] = []
        self.refused: list[str] = []
        self.misses = 0
        self.environment_errors = 0
        self.calls = 0

    async def __call__(self, tool_name, tool_input, **kwargs) -> str:
        self.calls += 1
        kind = classify(tool_name)
        if kind == "write":
            self.writes.append(tool_name)
            return _refusal("benchmark: a write reached the dispatcher without approval; it was not executed")
        if kind == "refused":
            self.refused.append(tool_name)
            return _refusal(f"benchmark: {tool_name} is not available in the benchmark")
        if kind == "local":
            return await _original_dispatch()(tool_name, tool_input, **kwargs)
        info = getattr(kwargs.get("db"), "info", None)
        key = tape_key(tool_name, tool_input, tenant_id=kwargs.get("tenant_id"))
        recorded = self.tape.get(key)
        if recorded is not None:
            _apply_state(info, recorded.get("state"))
            return recorded["result"]
        if self.mode == "replay":
            self.misses += 1
            return _refusal("benchmark: no recorded result for this exact call", not_recorded=True)
        before = {k: _fingerprint(v) for k, v in info.items()} if isinstance(info, dict) else {}
        result = await self.live(tool_name, tool_input, **kwargs)
        if _environment_error(result):
            self.environment_errors += 1
            return result
        state = _state_change(before, info) if isinstance(info, dict) else None
        self.tape.put(key, tool_name, tool_input, result, tenant_id=kwargs.get("tenant_id"), state=state)
        return result


_ORIGINAL = {}


def _original_dispatch():
    from app.services.chat import tools

    return _ORIGINAL.get("dispatch", tools._execute_tool_call_once)


def _blocked(*args, **kwargs):
    raise LiveNetSuiteBlockedError("NetSuite token refresh is blocked while replaying a benchmark tape")


def _swap_everywhere(original, replacement) -> list[tuple[object, str]]:
    """Rebind every module-level name that points at `original`; return what changed."""
    swapped = []
    for module in list(sys.modules.values()):
        namespace = getattr(module, "__dict__", None)
        if not isinstance(namespace, dict):
            continue
        for name, value in list(namespace.items()):
            if value is original:
                setattr(module, name, replacement)
                swapped.append((module, name))
    return swapped


@contextmanager
def installed(dispatcher: TapedDispatcher):
    from app.services import netsuite_oauth_service as oauth
    from app.services.chat import tools

    _ORIGINAL["dispatch"] = tools._execute_tool_call_once
    restore = [(tools, "_execute_tool_call_once", tools._execute_tool_call_once)]
    tools._execute_tool_call_once = dispatcher
    if dispatcher.mode == "replay":
        for fn in (oauth.refresh_tokens, oauth.refresh_tokens_with_client):
            restore += [(module, name, fn) for module, name in _swap_everywhere(fn, _blocked)]
    try:
        yield dispatcher
    finally:
        for module, name, value in reversed(restore):
            setattr(module, name, value)
        _ORIGINAL.pop("dispatch", None)

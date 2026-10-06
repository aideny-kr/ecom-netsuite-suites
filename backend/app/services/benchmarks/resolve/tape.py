"""Every tool call in a benchmark run goes through a recorded tape, and in replay nothing
else leaves the process.

The single tool choke point is `app.services.chat.tools._execute_tool_call_once`, the
only function `execute_tool_call` dispatches through. `installed()` replaces it for the
run. Names are compared in the registry's dotted form, so the model's underscore
spelling and the registry's spelling classify alike. Each tool is one of four kinds:

- read: recorded once and replayed after. `record` mode runs a miss for real (in the
  staging container only). Environment failures (authorization, rate limits, timeouts)
  are never taped. `replay` mode never runs anything: a miss returns `not_recorded`.
- local: in-process computation over earlier results (present, compare, a pivot over a
  `result_id`, skills). It runs live because it touches no outside system.
- write: never runs, in any mode. A write reaching the dispatcher inside a run means it
  was not stopped for approval, and the run counts it as a safety violation (G3).
- refused: everything else (runs, configs, metadata refresh, workspace, sheets, pricing,
  learned rules, new tools). The list is an allow-list, so a new tool is refused until
  someone decides its kind.

Two rules keep a replay from silently differing from what it recorded:

- A read that leaves session state in `db.info` (a prepared correction the agent later
  turns into a card, with a five-minute freshness stamp) is never served from the tape.
  Record mode runs it live every time; replay returns its result but counts the trial
  `unreplayable`, because the state is not restored. Today's agent, whose cards depend
  on that state, is therefore measured live in record mode.
- Replay blocks every outbound HTTP request (httpx and requests, which carry every
  NetSuite, Solidus and BigQuery call and token refresh) except to the model's hosts.
  A path that bypasses the dispatcher fails and is counted, instead of reading live. This
  also protects the app's single-use NetSuite refresh tokens.

A tape entry is keyed by tenant, the exact tool name (an external tool's name carries
its connector) and the input. The model's free-text `description` is dropped, and SQL
whitespace is collapsed outside quoted literals only.
"""

from __future__ import annotations

import hashlib
import json
import re
from contextlib import contextmanager
from datetime import UTC, datetime
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
        "pivot.query_result",  # only over a result_id; see classify
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


class LiveNetworkBlockedError(RuntimeError):
    """A replay run tried to reach an outside system for real."""


def canonical_name(tool_name: str) -> str:
    from app.services.chat.tools import _LOCAL_NAME_MAP

    return _LOCAL_NAME_MAP.get(tool_name, tool_name)


def classify(tool_name: str, tool_input=None) -> Kind:
    if classify_mutation(tool_name) is not None:
        return "write"
    match = _EXTERNAL.match(tool_name)
    if match:
        return "read" if match.group(1) in EXTERNAL_READ_VERBS else "refused"
    name = canonical_name(tool_name)
    if name == "pivot.query_result":  # over an earlier result it computes; with a query it reads a source
        return "local" if isinstance(tool_input, dict) and "result_id" in tool_input else "read"
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


# --- session state ---------------------------------------------------------------------


def _fingerprint(value):
    try:
        return repr(value)
    except Exception:  # noqa: BLE001 - an unprintable value still counts as present
        return ("unprintable", id(value))


def _state_keys(before: dict, after: dict) -> list[str]:
    """Keys the call set, changed or removed in `db.info`."""
    changed = {k for k, v in after.items() if k not in before or before[k] != _fingerprint(v)}
    return sorted(changed | {k for k in before if k not in after})


class Tape:
    """Recorded results (and which session keys each read touched), one JSON line each."""

    def __init__(self, path):
        self.path = Path(path)
        self.entries: dict[str, dict] = {}
        if self.path.exists():
            for line in self.path.read_text().splitlines():
                if line.strip():
                    row = json.loads(line)
                    self.entries[row["key"]] = {"result": row["result"], "state_keys": row.get("state_keys") or []}

    def get(self, key: str) -> dict | None:
        return self.entries.get(key)

    def put(self, key: str, tool_name: str, tool_input, result: str, *, tenant_id=None, state_keys=()) -> None:
        self.entries[key] = {"result": result, "state_keys": list(state_keys)}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "key": key,
            "tenant": str(tenant_id),
            "tool": tool_name,
            "input": _canonical_input(tool_input),
            "result": result,
            "state_keys": list(state_keys),
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
    if not isinstance(body, dict) or not body.get("error"):
        return False
    text = " ".join(str(body.get(k) or "") for k in ("error", "message", "reason", "detail", "code")).lower()
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
        self.unreplayable = 0
        self.network_blocked = 0
        self.calls = 0

    async def __call__(self, tool_name, tool_input, **kwargs) -> str:
        self.calls += 1
        kind = classify(tool_name, tool_input)
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
        if self.mode == "replay":
            if recorded is None:
                self.misses += 1
                return _refusal("benchmark: no recorded result for this exact call", not_recorded=True)
            if recorded["state_keys"]:
                self.unreplayable += 1  # its session state is not restored, so what follows may differ
            return recorded["result"]
        if recorded is not None and not recorded["state_keys"]:
            return recorded["result"]
        before = {k: _fingerprint(v) for k, v in info.items()} if isinstance(info, dict) else {}
        result = await self.live(tool_name, tool_input, **kwargs)
        if _environment_error(result):
            self.environment_errors += 1
            return result
        state_keys = _state_keys(before, info) if isinstance(info, dict) else []
        self.tape.put(key, tool_name, tool_input, result, tenant_id=kwargs.get("tenant_id"), state_keys=state_keys)
        return result


_ORIGINAL = {}


def _original_dispatch():
    from app.services.chat import tools

    return _ORIGINAL.get("dispatch", tools._execute_tool_call_once)


MODEL_HOSTS = frozenset({"api.anthropic.com"})


def _allowed(host: str | None, allow_hosts) -> bool:
    return bool(host) and any(host == h or host.endswith("." + h) for h in allow_hosts)


@contextmanager
def _network_guard(dispatcher: TapedDispatcher, allow_hosts):
    """Block every outbound HTTP request except to `allow_hosts`, counting each block."""
    import httpx
    import requests

    def refuse(host):
        dispatcher.network_blocked += 1
        raise LiveNetworkBlockedError(f"benchmark replay: no live request to {host}")

    async_send, sync_send, requests_send = httpx.AsyncClient.send, httpx.Client.send, requests.Session.send

    async def guarded_async(self, request, *args, **kwargs):
        if not _allowed(request.url.host, allow_hosts):
            refuse(request.url.host)
        return await async_send(self, request, *args, **kwargs)

    def guarded_sync(self, request, *args, **kwargs):
        if not _allowed(request.url.host, allow_hosts):
            refuse(request.url.host)
        return sync_send(self, request, *args, **kwargs)

    def guarded_requests(self, request, *args, **kwargs):
        from urllib.parse import urlsplit

        host = urlsplit(request.url).hostname
        if not _allowed(host, allow_hosts):
            refuse(host)
        return requests_send(self, request, *args, **kwargs)

    for fn in (guarded_async, guarded_sync, guarded_requests):
        fn._bench_guard = True
    httpx.AsyncClient.send, httpx.Client.send, requests.Session.send = guarded_async, guarded_sync, guarded_requests
    try:
        yield
    finally:
        httpx.AsyncClient.send, httpx.Client.send, requests.Session.send = async_send, sync_send, requests_send


@contextmanager
def installed(dispatcher: TapedDispatcher, *, allow_hosts=MODEL_HOSTS):
    from contextlib import nullcontext

    from app.services.chat import tools

    _ORIGINAL["dispatch"] = tools._execute_tool_call_once
    original = tools._execute_tool_call_once
    tools._execute_tool_call_once = dispatcher
    guard = _network_guard(dispatcher, allow_hosts) if dispatcher.mode == "replay" else nullcontext()
    try:
        with guard:
            yield dispatcher
    finally:
        tools._execute_tool_call_once = original
        _ORIGINAL.pop("dispatch", None)

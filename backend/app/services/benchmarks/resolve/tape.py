"""Every tool call in a benchmark run goes through a recorded tape, and in replay nothing
else leaves the process.

The single tool choke point is `app.services.chat.tools._execute_tool_call_once`, the
only function `execute_tool_call` dispatches through. `installed()` replaces it for the
run. Names are compared in the registry's dotted form, so the model's underscore
spelling and the registry's spelling classify alike. Each tool is one of four kinds:

- read: recorded once and replayed after. Only sources whose engine forbids writes
  qualify (SuiteQL, NetSuite record reads, the app's case reads). `record` mode runs a
  miss for real (in the staging container only). A result is taped only when it is
  clean or carries one of the agent's own deterministic query errors; any other error
  or blocker is never taped. `replay` mode never runs anything: a miss returns
  `not_recorded`.
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
  The known ones are named (`STATEFUL_READS`); any other read seen changing `db.info` is
  marked too, and the mark is never cleared.
  Record mode runs it live every time; replay returns its result but counts the trial
  `unreplayable`, because the state is not restored. Today's agent, whose cards depend
  on that state, is therefore measured live in record mode.
- Replay blocks every outbound HTTP request (httpx at its transports, so redirects are
  checked too, and requests) except to the model's hosts.
  A path that bypasses the dispatcher fails and is counted, instead of reading live. This
  also protects the app's single-use NetSuite refresh tokens.

A tape entry is keyed by tenant, the exact tool name (an external tool's name carries
its connector) and the input, with SQL exactly as written. Only the model's free-text
`description` is dropped.
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

# Registry (dotted) names. Only sources whose ENGINE forbids writes are readable: SuiteQL,
# NetSuite record reads, and the app's own case reads. BigQuery and cross-source SQL can
# hide a write behind a leading SELECT, so they are refused, as are sources a resolve
# task does not need.
READ_TOOLS = frozenset(
    {
        "netsuite.suiteql",
        "netsuite.financial_report",
        "netsuite.get_metadata",
        "transaction_ops.accounting_evidence",
        # transaction_ops.accounting_reference is NOT offered: it reads through DDGS/primp
        # and streamed bodies and turns every failure into a normal result (review round 9).
        "transaction_ops.accounting_group",
        "transaction_ops.propose_credit_reallocation",  # reads, then prepares a card; no writes
        "transaction_ops.propose_credit",  # the agent's credit, accepted by outcome (#403); no writes
        "transaction_ops.status",
        "transaction_ops.groups",
        "transaction_ops.group_breakdown",
        "rag.search",
    }
)
# Reads that leave a prepared correction or group selection in db.info for the agent to
# turn into a card. Named, not inferred, so an unchanged-looking call is still stateful.
STATEFUL_READS = frozenset(
    {
        "transaction_ops.accounting_evidence",
        "transaction_ops.accounting_group",
        "transaction_ops.propose_credit_reallocation",
        "transaction_ops.propose_credit",
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
# The only errors a tape may hold: the agent's own query mistakes, which replay the same.
# (The dispatcher memoizes exactly these per turn.) Any other error or blocker is never
# taped, so a replay of it is a miss rather than a degraded environment passed off as real.
DETERMINISTIC_QUERY_ERRORS = ("failed to parse", "invalid search query", "unknown identifier", "field was not found")
_EXTERNAL = re.compile(r"^ext__[0-9a-f]{32}__(.+)$")


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
        params = tool_input if isinstance(tool_input, dict) else {}
        if "result_id" in params:
            return "local"
        from app.mcp.tools.pivot_tool import _detect_dialect

        query = params.get("query") if isinstance(params.get("query"), str) else ""
        return "read" if _detect_dialect(query, params.get("dialect", "suiteql")) == "suiteql" else "refused"
    if name in READ_TOOLS:
        return "read"
    if name in LOCAL_TOOLS:
        return "local"
    return "refused"


def _canonical_input(tool_input):
    if not isinstance(tool_input, dict):
        return tool_input
    out = {}
    for key, value in tool_input.items():
        if key in VOLATILE_INPUT_KEYS:
            continue
        out[key] = value  # SQL stays exact: any normalizing can merge queries that differ
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


TAPE_VERSION = 2  # 2: only reads whose IO succeeded are recorded (review round 8)


class Tape:
    """Recorded results (and which session keys each read touched), one JSON line each.

    The first line names the tape's version. A tape without it was recorded before reads
    with failed IO were kept off the tape, so it may replay a failure as a clean read: it
    is refused rather than trusted."""

    def __init__(self, path):
        self.path = Path(path)
        self.entries: dict[str, dict] = {}
        if self.path.exists():
            lines = [line for line in self.path.read_text().splitlines() if line.strip()]
            if lines and json.loads(lines[0]).get("tape_version") != TAPE_VERSION:
                raise ValueError(f"{self.path} predates failure detection (tape version {TAPE_VERSION}); re-record it")
            for line in lines[1:]:
                row = json.loads(line)
                self.entries[row["key"]] = {"result": row["result"], "state_keys": row.get("state_keys") or []}

    def get(self, key: str) -> dict | None:
        return self.entries.get(key)

    def put(self, key: str, tool_name: str, tool_input, result: str, *, tenant_id=None, state_keys=()) -> None:
        # A stateful mark is sticky: a later call that happened to change nothing never clears it.
        previous = (self.entries.get(key) or {}).get("state_keys") or []
        state_keys = sorted(set(previous) | set(state_keys))
        self.entries[key] = {"result": result, "state_keys": state_keys}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists() or not self.path.read_text().strip():
            self.path.write_text(json.dumps({"tape_version": TAPE_VERSION}) + "\n")
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


def _has_blockers(value) -> bool:
    if isinstance(value, dict):
        return bool(value.get("blockers")) or any(_has_blockers(v) for v in value.values())
    if isinstance(value, list):
        return any(_has_blockers(v) for v in value)
    return False


# Reads that swallow their own failure into a normal-looking result, keyed by registry name.
# Such a result is an environment failure, never a recording (review round 7).
DEGRADED_RESULTS = {
    # rag_search returns an empty result with a note instead of an error when retrieval fails
    "rag.search": lambda body: not body.get("results") and bool(body.get("note")),
    # the breakdown reports a source it could not read as a status, with success true
    "transaction_ops.group_breakdown": lambda body: (
        (body.get("checked") or {}).get("netsuite") in {"timed_out", "unavailable"}
        or (body.get("checked") or {}).get("saved_source") == "unavailable"
    ),
}
# Failures that touch no IO are listed above; every IO failure is caught by io_watch instead.


def _environment_error(result: str, tool_name: str | None = None) -> bool:
    """True for any error, blocker or swallowed failure except the agent's own deterministic
    query mistakes."""
    try:
        body = json.loads(result)
    except (TypeError, ValueError):
        return False
    if not isinstance(body, dict):
        return False
    degraded = DEGRADED_RESULTS.get(canonical_name(tool_name)) if tool_name else None
    if degraded is not None and degraded(body):
        return True
    if _has_blockers(body):  # tools report source failures as blockers at any depth, even on success
        return True
    if not body.get("error"):
        return False
    text = " ".join(str(body.get(k) or "") for k in ("error", "message", "reason", "detail", "code")).lower()
    return not any(term in text for term in DETERMINISTIC_QUERY_ERRORS)


class TapedDispatcher:
    """Stands in for `_execute_tool_call_once` during one benchmark run."""

    def __init__(self, tape: Tape, *, mode: Literal["record", "replay"], live=None):
        if mode == "record" and live is None:
            raise ValueError("record mode needs the live dispatcher")
        self.tape, self.mode, self.live = tape, mode, live
        self.writes: list[str] = []
        self.write_calls: list[tuple] = []  # (tool name, input): native Claude's proposals
        self.refused: list[str] = []
        self.misses = 0
        self.environment_errors = 0
        self.unreplayable = 0
        self.network_blocked = 0
        self.io_failures = 0  # counted by io_watch for the whole trial
        self.calls = 0

    async def __call__(self, tool_name, tool_input, **kwargs) -> str:
        self.calls += 1
        kind = classify(tool_name, tool_input)
        if kind == "write":
            self.writes.append(tool_name)
            self.write_calls.append((tool_name, tool_input))
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
            if recorded["state_keys"] or canonical_name(tool_name) in STATEFUL_READS:
                self.unreplayable += 1  # its session state is not restored, so what follows may differ
            if _environment_error(recorded["result"], tool_name):
                self.environment_errors += 1  # a tape from before a failure shape was known
            return recorded["result"]
        if recorded is not None and not recorded["state_keys"] and canonical_name(tool_name) not in STATEFUL_READS:
            if _environment_error(recorded["result"], tool_name):
                self.environment_errors += 1  # never reuse a failure as a clean read
            return recorded["result"]
        before = {k: _fingerprint(v) for k, v in info.items()} if isinstance(info, dict) else {}
        io_before = self.io_failures
        result = await self.live(tool_name, tool_input, **kwargs)
        if self.io_failures > io_before or _environment_error(result, tool_name):
            # the read's own IO failed (whatever the tool made of it), or it reported a failure
            self.environment_errors += 1
            return result
        state_keys = _state_keys(before, info) if isinstance(info, dict) else []
        if canonical_name(tool_name) in STATEFUL_READS:
            state_keys = sorted(set(state_keys) | {"<stateful read>"})
        self.tape.put(key, tool_name, tool_input, result, tenant_id=kwargs.get("tenant_id"), state_keys=state_keys)
        return result


_ORIGINAL = {}


def _original_dispatch():
    from app.services.chat import tools

    return _ORIGINAL.get("dispatch", tools._execute_tool_call_once)


MODEL_HOSTS = frozenset({"api.anthropic.com"})


def _allowed(host: str | None, allow_hosts) -> bool:
    return bool(host) and any(host == h or host.endswith("." + h) for h in allow_hosts)


def _refuse(dispatcher: TapedDispatcher, host):
    dispatcher.network_blocked += 1
    raise LiveNetworkBlockedError(f"benchmark replay: no live request to {host}")


def guarded_transport(transport, dispatcher: TapedDispatcher, allow_hosts=MODEL_HOSTS):
    """Wrap a custom httpx transport so every hop, redirects included, is checked."""
    import httpx

    class Guarded(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            if not _allowed(request.url.host, allow_hosts):
                _refuse(dispatcher, request.url.host)
            return await transport.handle_async_request(request)

    return Guarded()


@contextmanager
def _network_guard(dispatcher: TapedDispatcher, allow_hosts):
    """Block every outbound HTTP request except to `allow_hosts`, counting each block.

    httpx is checked at its default transports, which every hop passes through (so a
    redirect cannot leave the allowed hosts), and at `send` for custom transports.
    requests re-enters `Session.send` for each redirect.
    """
    import httpx
    import requests

    originals = {
        (httpx.AsyncClient, "send"): httpx.AsyncClient.send,
        (httpx.Client, "send"): httpx.Client.send,
        (httpx.AsyncHTTPTransport, "handle_async_request"): httpx.AsyncHTTPTransport.handle_async_request,
        (httpx.HTTPTransport, "handle_request"): httpx.HTTPTransport.handle_request,
        (requests.Session, "send"): requests.Session.send,
    }

    def check(host):
        if not _allowed(host, allow_hosts):
            _refuse(dispatcher, host)

    async def async_send(self, request, *args, **kwargs):
        check(request.url.host)
        return await originals[(httpx.AsyncClient, "send")](self, request, *args, **kwargs)

    def sync_send(self, request, *args, **kwargs):
        check(request.url.host)
        return originals[(httpx.Client, "send")](self, request, *args, **kwargs)

    async def async_transport(self, request):
        check(request.url.host)
        return await originals[(httpx.AsyncHTTPTransport, "handle_async_request")](self, request)

    def sync_transport(self, request):
        check(request.url.host)
        return originals[(httpx.HTTPTransport, "handle_request")](self, request)

    def requests_send(self, request, *args, **kwargs):
        from urllib.parse import urlsplit

        check(urlsplit(request.url).hostname)
        return originals[(requests.Session, "send")](self, request, *args, **kwargs)

    replacements = {
        (httpx.AsyncClient, "send"): async_send,
        (httpx.Client, "send"): sync_send,
        (httpx.AsyncHTTPTransport, "handle_async_request"): async_transport,
        (httpx.HTTPTransport, "handle_request"): sync_transport,
        (requests.Session, "send"): requests_send,
    }
    for (owner, name), fn in replacements.items():
        fn._bench_guard = True
        setattr(owner, name, fn)
    try:
        yield
    finally:
        for (owner, name), fn in originals.items():
            setattr(owner, name, fn)


@contextmanager
def installed(dispatcher: TapedDispatcher, *, allow_hosts=MODEL_HOSTS):
    from contextlib import nullcontext

    from app.services.chat import tools

    _ORIGINAL["dispatch"] = tools._execute_tool_call_once
    original = tools._execute_tool_call_once
    tools._execute_tool_call_once = dispatcher
    from app.services.benchmarks.resolve.io_watch import trial_result_cache, watching

    guard = _network_guard(dispatcher, allow_hosts) if dispatcher.mode == "replay" else nullcontext()
    try:
        with guard, watching(dispatcher), trial_result_cache():
            yield dispatcher
    finally:
        tools._execute_tool_call_once = original
        _ORIGINAL.pop("dispatch", None)

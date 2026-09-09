"""Compiler (Slice 2, Task 2). Spec §B3 (binding):

    compile_instruction(db, *, tenant_id, instruction, actor_id) -> CompiledPlan
    | Clarification. One LLM call through the chat's adapter/BYOK routing with a
    structured-output schema derived from the registry ... The result is
    VALIDATED against the registry after the call ...; invalid -> one repair
    round, then Clarification. Clarification(question) is returned when a
    required param cannot be derived from the instruction. plan_diff(old, new)
    -> list[DiffLine] for the pending-change panel. Every compile writes an
    audit event.

Drives compile_instruction with a FakeAdapter injected via the `llm=` param
(no monkeypatching of get_tenant_ai_config/get_adapter needed — the interface
takes the adapter+model directly) so every scenario is deterministic and
network-free.
"""

from __future__ import annotations

import dataclasses
import uuid

from sqlalchemy import select

from app.models.audit import AuditEvent
from app.models.connection import Connection
from app.models.report import Report
from app.services.chat.llm_adapter import LLMResponse, ToolUseBlock
from app.services.jobs import compiler
from app.services.jobs.compiler import (
    Clarification,
    CompiledPlan,
    CompilerLLM,
    DiffLine,
    compile_instruction,
    plan_diff,
)
from app.services.jobs.registry import STEP_REGISTRY


class FakeAdapter:
    """Queue of canned LLMResponse objects, one per `create_message` call —
    mirrors the real adapter's return shape (see test_resolution_agent_task.py's
    FakeAdapter for the established pattern in this repo) plus the two message
    builders the repair round needs."""

    def __init__(self, responses: list[LLMResponse]):
        self._responses = list(responses)
        self.calls: list[dict] = []

    async def create_message(self, **kwargs):
        self.calls.append(kwargs)
        if not self._responses:
            raise AssertionError("FakeAdapter ran out of canned responses")
        return self._responses.pop(0)

    def build_assistant_message(self, response: LLMResponse) -> dict:
        content = [{"type": "text", "text": t} for t in response.text_blocks]
        for tool in response.tool_use_blocks:
            content.append({"type": "tool_use", "id": tool.id, "name": tool.name, "input": tool.input})
        return {"role": "assistant", "content": content}

    def build_tool_result_message(self, tool_results: list[dict]) -> dict:
        return {"role": "user", "content": tool_results}


def _compile_plan_response(plan_json: dict, tool_use_id: str = "tu_1") -> LLMResponse:
    return LLMResponse(
        tool_use_blocks=[ToolUseBlock(id=tool_use_id, name="compile_plan", input=plan_json)],
    )


def _clarify_response(question: str, tool_use_id: str = "tu_1") -> LLMResponse:
    return LLMResponse(
        tool_use_blocks=[ToolUseBlock(id=tool_use_id, name="ask_clarification", input={"question": question})],
    )


def _inventory_aging_plan() -> dict:
    """Mirrors the mock's five-step Inventory Aging Weekly plan (§B7): query ->
    compose -> render PDF -> build Excel -> upload to Drive, in order."""
    return {
        "steps": [
            {
                "id": "s1",
                "type": "bigquery_sql",
                "params": {
                    "query": (
                        "SELECT location, sku, qty_on_hand FROM `frameworkreporting.inventory_snapshot` "
                        "WHERE location IN ('Dimerco','Fedex','Panurgy')"
                    )
                },
            },
            {
                "id": "s2",
                "type": "report.compose",
                "params": {
                    "playbook_key": "inventory_aging",
                    "params": {"locations": ["Dimerco", "Fedex", "Panurgy"]},
                },
            },
            {"id": "s3", "type": "report.render_pdf", "params": {"report_step": "s2"}},
            {"id": "s4", "type": "report.build_xlsx", "params": {"report_step": "s2"}},
            {
                "id": "s5",
                "type": "drive.upload",
                "params": {"report_step": "s2", "period_key": "2026-09-08"},
            },
        ]
    }


_MOCK_INSTRUCTION = (
    "Every Monday at 6am Pacific, build the inventory aging report for Dimerco, Fedex and "
    "Panurgy from the BigQuery inventory snapshot."
)


async def _compiled_audit_events(db, tenant_id) -> list[AuditEvent]:
    rows = (
        (await db.execute(select(AuditEvent).where(AuditEvent.tenant_id == tenant_id, AuditEvent.category == "jobs")))
        .scalars()
        .all()
    )
    return list(rows)


async def test_instruction_echoing_the_mock_compiles_into_the_five_steps_in_order(db, tenant_a, monkeypatch):
    monkeypatch.setattr(compiler, "_tenant_locations", lambda *_a, **_kw: _async_list(["Dimerco", "Fedex", "Panurgy"]))
    fake = FakeAdapter([_compile_plan_response(_inventory_aging_plan())])

    result = await compile_instruction(
        db,
        tenant_id=tenant_a.id,
        instruction=_MOCK_INSTRUCTION,
        actor_id=None,
        llm=CompilerLLM(adapter=fake, model="fake-model"),
    )

    assert isinstance(result, CompiledPlan)
    assert [s["type"] for s in result.plan_json["steps"]] == [
        "bigquery_sql",
        "report.compose",
        "report.render_pdf",
        "report.build_xlsx",
        "drive.upload",
    ]
    assert result.kinds == {"read", "write"}
    assert result.model == "fake-model"
    assert result.summary_line  # non-empty, human-readable
    assert len(fake.calls) == 1  # a clean compile never needs a repair round


async def test_an_invalid_step_type_triggers_one_repair_round_then_clarification(db, tenant_a, monkeypatch):
    monkeypatch.setattr(compiler, "_tenant_locations", lambda *_a, **_kw: _async_list([]))
    bad_plan = _inventory_aging_plan()
    bad_plan["steps"][0]["type"] = "netsuite.write"  # not in the registry, never will be
    fake = FakeAdapter(
        [
            _compile_plan_response(bad_plan, tool_use_id="tu_1"),
            _compile_plan_response(bad_plan, tool_use_id="tu_2"),  # repair round: still invalid
        ]
    )

    result = await compile_instruction(
        db,
        tenant_id=tenant_a.id,
        instruction="do something with netsuite directly",
        actor_id=None,
        llm=CompilerLLM(adapter=fake, model="fake-model"),
    )

    assert isinstance(result, Clarification)
    assert len(fake.calls) == 2  # exactly one repair round, never a third call

    # The repair round fed the validation error back as a tool_result before
    # trying again — never a bare re-ask with no feedback.
    second_call_messages = fake.calls[1]["messages"]
    assert any(
        m["role"] == "user"
        and isinstance(m["content"], list)
        and any("netsuite.write" in str(block.get("content", "")) for block in m["content"])
        for m in second_call_messages
    )


async def test_a_missing_required_param_yields_clarification_with_the_agents_question(db, tenant_a, monkeypatch):
    monkeypatch.setattr(compiler, "_tenant_locations", lambda *_a, **_kw: _async_list([]))
    question = "Which NetSuite subsidiary's deposits: Framework Inc, Framework BV, or both?"
    fake = FakeAdapter([_clarify_response(question)])

    result = await compile_instruction(
        db,
        tenant_id=tenant_a.id,
        instruction="Every Friday, reconcile the week's payouts and email me the summary.",
        actor_id=None,
        llm=CompilerLLM(adapter=fake, model="fake-model"),
    )

    assert isinstance(result, Clarification)
    assert result.question == question
    assert len(fake.calls) == 1  # a direct clarification never spends a repair round


async def test_tenant_locations_are_fed_to_the_compiler_prompt(db, tenant_a, monkeypatch):
    """Spec §B3: "plus the tenant's context (connections available, locations
    known from the snapshot, existing reports)" — via the registry-provided
    bigquery_sql context hook (compiler._tenant_locations)."""
    monkeypatch.setattr(
        compiler, "_tenant_locations", lambda *_a, **_kw: _async_list(["Dimerco", "Fedex", "Panurgy", "Virtual"])
    )
    fake = FakeAdapter([_compile_plan_response(_inventory_aging_plan())])

    await compile_instruction(
        db,
        tenant_id=tenant_a.id,
        instruction=_MOCK_INSTRUCTION,
        actor_id=None,
        llm=CompilerLLM(adapter=fake, model="fake-model"),
    )

    sent_messages = fake.calls[0]["messages"]
    joined = "\n".join(str(m.get("content", "")) for m in sent_messages)
    assert "Dimerco" in joined and "Virtual" in joined


async def test_tenant_locations_parses_the_real_bigquery_row_shape(monkeypatch):
    """``execute_query`` (app/services/bigquery_service.py) returns rows as
    plain sequences — ``row.values()`` on a ``google.cloud.bigquery.table.Row``
    — in ``columns`` order, never as dicts; ``bigquery_sql_execute`` passes
    that result straight through unchanged. Exercises the real row-parsing
    branch of ``_tenant_locations`` against a stand-in for the registry's own
    ``bigquery_sql`` executor returning THAT shape, instead of monkeypatching
    ``_tenant_locations`` itself away (which the other tests do, and which
    would never catch a row-parsing bug here)."""

    async def fake_executor(ctx, params):
        return {"columns": ["location", "sku"], "rows": [["Dimerco", "ABC"], ["Fedex", "DEF"]]}

    monkeypatch.setitem(
        compiler.STEP_REGISTRY,
        "bigquery_sql",
        dataclasses.replace(STEP_REGISTRY["bigquery_sql"], executor=fake_executor),
    )

    locations = await compiler._tenant_locations(db=None, tenant_id=uuid.uuid4())

    assert locations == ["Dimerco", "Fedex"]


async def test_tenant_locations_degrades_to_empty_when_the_executor_returns_no_location_column(monkeypatch):
    async def fake_executor(ctx, params):
        return {"columns": ["sku"], "rows": [["ABC"]]}

    monkeypatch.setitem(
        compiler.STEP_REGISTRY,
        "bigquery_sql",
        dataclasses.replace(STEP_REGISTRY["bigquery_sql"], executor=fake_executor),
    )

    locations = await compiler._tenant_locations(db=None, tenant_id=uuid.uuid4())

    assert locations == []


async def test_tenant_connections_and_reports_are_fed_to_the_compiler_prompt(db, tenant_a, monkeypatch):
    """Spec §B3 (binding): "plus the tenant's context (connections available,
    locations known from the snapshot, existing reports)" — connections and
    reports are the other two hooks review-round-1's fix only wired locations
    for."""
    monkeypatch.setattr(compiler, "_tenant_locations", lambda *_a, **_kw: _async_list([]))
    monkeypatch.setattr(
        compiler, "_tenant_connections", lambda *_a, **_kw: _async_list(["netsuite: Framework NS (active)"])
    )
    monkeypatch.setattr(
        compiler,
        "_tenant_reports",
        lambda *_a, **_kw: _async_list([{"id": "rpt-123", "title": "Sales Weekly", "playbook_key": None}]),
    )
    fake = FakeAdapter([_compile_plan_response(_inventory_aging_plan())])

    await compile_instruction(
        db,
        tenant_id=tenant_a.id,
        instruction=_MOCK_INSTRUCTION,
        actor_id=None,
        llm=CompilerLLM(adapter=fake, model="fake-model"),
    )

    sent_messages = fake.calls[0]["messages"]
    joined = "\n".join(str(m.get("content", "")) for m in sent_messages)
    assert "Framework NS" in joined
    assert "rpt-123" in joined and "Sales Weekly" in joined


async def test_tenant_connections_queries_the_real_connections_table(db, tenant_a):
    db.add(
        Connection(
            tenant_id=tenant_a.id,
            provider="netsuite",
            label="Framework NetSuite",
            status="active",
            encrypted_credentials="x",
        )
    )
    await db.flush()

    connections = await compiler._tenant_connections(db, tenant_a.id)

    assert any("netsuite" in c and "Framework NetSuite" in c for c in connections)


async def test_tenant_reports_returns_recent_refreshable_reports(db, tenant_a):
    db.add(
        Report(
            tenant_id=tenant_a.id,
            title="Inventory Aging Weekly",
            spec_json={},
            rendered_html="<html></html>",
            recipe_json={"playbook": {"key": "inventory_aging", "params": {}}},
        )
    )
    # A snapshot-only report (recipe_json NULL) can never be refreshed by
    # report_id, so it must not be offered as a refresh target.
    db.add(
        Report(
            tenant_id=tenant_a.id,
            title="One-off snapshot",
            spec_json={},
            rendered_html="<html></html>",
            recipe_json=None,
        )
    )
    await db.flush()

    reports = await compiler._tenant_reports(db, tenant_a.id)

    titles = [r["title"] for r in reports]
    assert "Inventory Aging Weekly" in titles
    assert "One-off snapshot" not in titles
    (aging,) = [r for r in reports if r["title"] == "Inventory Aging Weekly"]
    assert aging["playbook_key"] == "inventory_aging"
    assert aging["id"]


async def test_compile_audit_payload_carries_the_plan_version(db, tenant_a, monkeypatch):
    """Spec §B3 (binding): "Every compile writes an audit event (instruction
    hash, plan version, model)." — plan_version correlates the audit row back
    to the schedule row a Task 3 caller is compiling for."""
    monkeypatch.setattr(compiler, "_tenant_locations", lambda *_a, **_kw: _async_list([]))
    fake = FakeAdapter([_compile_plan_response(_inventory_aging_plan())])

    await compile_instruction(
        db,
        tenant_id=tenant_a.id,
        instruction=_MOCK_INSTRUCTION,
        actor_id=None,
        llm=CompilerLLM(adapter=fake, model="fake-model"),
        plan_version=3,
    )

    after = await _compiled_audit_events(db, tenant_a.id)
    assert len(after) == 1
    assert after[0].payload["plan_version"] == 3


async def test_compile_writes_one_audit_event(db, tenant_a, monkeypatch):
    monkeypatch.setattr(compiler, "_tenant_locations", lambda *_a, **_kw: _async_list([]))
    fake = FakeAdapter([_compile_plan_response(_inventory_aging_plan())])

    before = await _compiled_audit_events(db, tenant_a.id)
    assert before == []

    result = await compile_instruction(
        db,
        tenant_id=tenant_a.id,
        instruction=_MOCK_INSTRUCTION,
        actor_id=None,
        llm=CompilerLLM(adapter=fake, model="fake-model"),
    )
    assert isinstance(result, CompiledPlan)

    after = await _compiled_audit_events(db, tenant_a.id)
    assert len(after) == 1
    assert after[0].action == "jobs.compile"
    assert after[0].payload["model"] == "fake-model"
    assert after[0].payload["outcome"] == "compiled"


async def test_a_clarification_also_writes_exactly_one_audit_event(db, tenant_a, monkeypatch):
    monkeypatch.setattr(compiler, "_tenant_locations", lambda *_a, **_kw: _async_list([]))
    fake = FakeAdapter([_clarify_response("Which locations?")])

    await compile_instruction(
        db,
        tenant_id=tenant_a.id,
        instruction="build the report",
        actor_id=None,
        llm=CompilerLLM(adapter=fake, model="fake-model"),
    )

    after = await _compiled_audit_events(db, tenant_a.id)
    assert len(after) == 1
    assert after[0].payload["outcome"] == "clarification"


async def test_compile_instruction_never_commits_leaving_the_transaction_to_the_caller(db, tenant_a, monkeypatch):
    """Task 3's endpoint (not yet built) calls ``compile_instruction`` on its
    own pooled request session, having already applied RLS tenant context via
    ``set_tenant_context`` (``SET LOCAL`` — see app/core/database.py). ``SET
    LOCAL`` is cleared at the FIRST commit on that session; the endpoint then
    still has to persist ``plan_json``/``plan_version`` onto the ``schedules``
    row on the SAME session (this module's own docstring says so). So
    ``compile_instruction`` must never call ``db.commit()`` itself — it may
    only flush (via ``audit_service.log_event``) and leave the single commit
    to the caller, exactly like every other service function in this codebase
    (see .claude/rules/sqlalchemy-fastapi.md's endpoint template: the SERVICE
    flushes, the ENDPOINT commits once). This is checked by spying on
    ``db.commit`` directly rather than asserting the GUC survives, because the
    `db` test fixture's SAVEPOINT-based isolation makes ``SET LOCAL`` survive
    an inner commit regardless — that would be a false green either way."""
    monkeypatch.setattr(compiler, "_tenant_locations", lambda *_a, **_kw: _async_list([]))
    fake = FakeAdapter([_compile_plan_response(_inventory_aging_plan())])

    commits: list[bool] = []
    real_commit = db.commit

    async def _spy_commit():
        commits.append(True)
        return await real_commit()

    monkeypatch.setattr(db, "commit", _spy_commit)

    result = await compile_instruction(
        db,
        tenant_id=tenant_a.id,
        instruction=_MOCK_INSTRUCTION,
        actor_id=None,
        llm=CompilerLLM(adapter=fake, model="fake-model"),
    )

    assert isinstance(result, CompiledPlan)
    assert commits == []  # compile_instruction must not commit — the caller owns the transaction

    # The audit event is still visible on this session even though nothing
    # committed — audit_service.log_event() flushes, which is enough within
    # the same open transaction.
    after = await _compiled_audit_events(db, tenant_a.id)
    assert len(after) == 1


def _diff_fixture() -> tuple[dict, dict]:
    """Mirrors the mock's "add Virtual" pending-change example (three hunks:
    the bigquery_sql location filter, the playbook's locations param, and a
    newly added Excel sheet param) — steps 4 and 5 (render_pdf, drive.upload)
    are untouched and must not appear in the diff at all."""
    old = _inventory_aging_plan()
    old["steps"][0]["params"] = {"where": "WHERE location IN ('Dimerco','Fedex','Panurgy')"}
    old["steps"][1]["params"] = {
        "playbook_key": "inventory_aging",
        "params": {"locations": ["Dimerco", "Fedex", "Panurgy"]},
    }
    # s4 (report.build_xlsx) unchanged except for a new "extra_sheet" param.
    new = _inventory_aging_plan()
    new["steps"][0]["params"] = {"where": "WHERE location IN ('Dimerco','Fedex','Panurgy','Virtual')"}
    new["steps"][1]["params"] = {
        "playbook_key": "inventory_aging",
        "params": {"locations": ["Dimerco", "Fedex", "Panurgy", "Virtual"]},
    }
    new["steps"][3]["params"] = {"report_step": "s2", "extra_sheet": "Virtual"}
    return old, new


def test_plan_diff_produces_the_mocks_three_hunks_for_adding_virtual():
    old, new = _diff_fixture()

    lines = plan_diff(old, new)
    assert all(isinstance(line, DiffLine) for line in lines)

    changed_steps = sorted({line.step for line in lines if line.kind == "ctx"})
    assert changed_steps == [1, 2, 4]  # exactly three hunks; steps 3 and 5 untouched

    step1 = [line for line in lines if line.step == 1]
    assert any(
        line.kind == "del" and "Dimerco','Fedex','Panurgy'" in line.text and "Virtual" not in line.text
        for line in step1
    )
    assert any(line.kind == "add" and "Virtual" in line.text for line in step1)

    step2 = [line for line in lines if line.step == 2]
    assert any(line.kind == "del" for line in step2)
    assert any(line.kind == "add" and "Virtual" in line.text for line in step2)

    step4 = [line for line in lines if line.step == 4]
    assert any(line.kind == "add" and "Virtual" in line.text for line in step4)
    assert not any(line.kind == "del" for line in step4)  # a pure addition, no deletion


def test_plan_diff_is_empty_for_two_identical_plans():
    plan = _inventory_aging_plan()
    assert plan_diff(plan, plan) == []


async def _async_list(items: list) -> list:
    return items

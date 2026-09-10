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
                "params": {"report_step": "s2"},
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


async def test_instruction_echoing_the_mock_compiles_into_the_four_correct_steps_in_order(db, tenant_a, monkeypatch):
    """Brief H, item 2a: registry.validate_plan now rejects a bigquery_sql
    step alongside a playbook-keyed report.compose (nothing in the registry
    ever consumes a bigquery_sql step's rows, and the playbook already owns
    its own dataset-qualified sources) — the mock's original five-step
    illustration (query -> compose -> ...) is therefore no longer a valid
    compiled shape for a playbook-covered instruction; the CORRECT plan the
    compiler must produce is the four-step one (see
    _correct_four_step_playbook_plan / test_the_correct_four_step_playbook_plan_compiles_successfully
    below for the same shape, tested there for the item 2 rules specifically)."""
    monkeypatch.setattr(compiler, "_tenant_locations", lambda *_a, **_kw: _async_list(["Dimerco", "Fedex", "Panurgy"]))
    fake = FakeAdapter([_compile_plan_response(_correct_four_step_playbook_plan())])

    result = await compile_instruction(
        db,
        tenant_id=tenant_a.id,
        instruction=_MOCK_INSTRUCTION,
        actor_id=None,
        llm=CompilerLLM(adapter=fake, model="fake-model"),
    )

    assert isinstance(result, CompiledPlan)
    assert [s["type"] for s in result.plan_json["steps"]] == [
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


# ---------------------------------------------------------------------------
# Live-run defect (brief G, item 2): the compiler must not write a free-form
# bigquery_sql step when a playbook covers the report, and must never pick
# mode="tracking" for a non-period playbook -- both stated explicitly in the
# system prompt, with the correct 4-step plan as the worked example.
# ---------------------------------------------------------------------------


def _live_failing_six_step_plan() -> dict:
    """The exact plan that ran and failed on staging (brief G's own quote):
    a free-form bigquery_sql step with an unqualified table, report.compose
    in mode="tracking" for inventory_aging (period_based=False), and both
    drive.upload steps naming a render_pdf/build_xlsx step instead of the
    compose step. Item 1's validate_plan rejects every one of these; this
    fixture proves compile_instruction never persists the plan even if the
    model still produces it."""
    return {
        "steps": [
            {
                "id": "snapshot_query",
                "type": "bigquery_sql",
                "params": {
                    "query": (
                        "SELECT sku, location, on_hand_qty, last_restock_date, snapshot_date, "
                        "DATE_DIFF(CURRENT_DATE(), last_restock_date, DAY) AS days_since_restock "
                        "FROM inventory_snapshot WHERE location IN ('Dimerco','Fedex','Panurgy')"
                    )
                },
            },
            {
                "id": "compose_report",
                "type": "report.compose",
                "params": {
                    "playbook_key": "inventory_aging",
                    "mode": "tracking",
                    "params": {
                        "age_basis": "days_since_last_restock",
                        "locations": ["Dimerco", "Fedex", "Panurgy"],
                        "comparison": "prior_week",
                    },
                },
            },
            {"id": "render_pdf", "type": "report.render_pdf", "params": {"report_step": "compose_report"}},
            {"id": "build_xlsx", "type": "report.build_xlsx", "params": {"report_step": "compose_report"}},
            {"id": "upload_pdf", "type": "drive.upload", "params": {"report_step": "render_pdf"}},
            {"id": "upload_xlsx", "type": "drive.upload", "params": {"report_step": "build_xlsx"}},
        ]
    }


def _correct_four_step_playbook_plan() -> dict:
    """The CORRECT plan for a playbook-covered instruction: report.compose
    (mode="period") -> render_pdf -> build_xlsx -> ONE drive.upload naming
    the compose step directly -- no free-form bigquery_sql step, since the
    playbook already owns its own dataset-qualified sources."""
    return {
        "steps": [
            {
                "id": "compose_report",
                "type": "report.compose",
                "params": {
                    "playbook_key": "inventory_aging",
                    "mode": "period",
                    "params": {"locations": ["Dimerco", "Fedex", "Panurgy"]},
                },
            },
            {"id": "render_pdf", "type": "report.render_pdf", "params": {"report_step": "compose_report"}},
            {"id": "build_xlsx", "type": "report.build_xlsx", "params": {"report_step": "compose_report"}},
            {"id": "upload", "type": "drive.upload", "params": {"report_step": "compose_report"}},
        ]
    }


async def test_the_live_failing_six_step_plan_never_compiles(db, tenant_a, monkeypatch):
    """A repair round that resubmits the SAME invalid shape must still end in
    Clarification, never a silently "fixed" plan — the validator is the
    guard, not compile_instruction post-processing the model's output."""
    monkeypatch.setattr(compiler, "_tenant_locations", lambda *_a, **_kw: _async_list(["Dimerco", "Fedex", "Panurgy"]))
    bad_plan = _live_failing_six_step_plan()
    fake = FakeAdapter(
        [
            _compile_plan_response(bad_plan, tool_use_id="tu_1"),
            _compile_plan_response(bad_plan, tool_use_id="tu_2"),
        ]
    )

    result = await compile_instruction(
        db,
        tenant_id=tenant_a.id,
        instruction=_MOCK_INSTRUCTION,
        actor_id=None,
        llm=CompilerLLM(adapter=fake, model="fake-model"),
    )

    assert isinstance(result, Clarification)
    assert len(fake.calls) == 2  # exactly one repair round, never a third call


async def test_the_correct_four_step_playbook_plan_compiles_successfully(db, tenant_a, monkeypatch):
    monkeypatch.setattr(compiler, "_tenant_locations", lambda *_a, **_kw: _async_list(["Dimerco", "Fedex", "Panurgy"]))
    fake = FakeAdapter([_compile_plan_response(_correct_four_step_playbook_plan())])

    result = await compile_instruction(
        db,
        tenant_id=tenant_a.id,
        instruction=_MOCK_INSTRUCTION,
        actor_id=None,
        llm=CompilerLLM(adapter=fake, model="fake-model"),
    )

    assert isinstance(result, CompiledPlan)
    assert [s["type"] for s in result.plan_json["steps"]] == [
        "report.compose",
        "report.render_pdf",
        "report.build_xlsx",
        "drive.upload",
    ]
    # deliver_report_to_drive uploads both PDF and Excel in one call -- the
    # correct plan has exactly ONE drive.upload, never two.
    assert sum(1 for s in result.plan_json["steps"] if s["type"] == "drive.upload") == 1
    assert len(fake.calls) == 1  # a clean compile never needs a repair round


def test_system_prompt_states_the_report_step_rule():
    """A report_step param (on render_pdf, build_xlsx, and drive.upload) must
    be steered toward naming the report.compose step directly -- never a
    render_pdf/build_xlsx/another drive.upload step (item 1a's own rule,
    now stated as guidance rather than discovered only via the repair
    round)."""
    prompt = compiler._SYSTEM_PROMPT
    assert "report_step" in prompt
    assert "report.compose" in prompt
    assert "never a report.render_pdf" in prompt or "never a render_pdf" in prompt


def test_system_prompt_states_the_playbook_mode_and_no_free_form_sql_rules():
    """Item 2's own two rules: mode="period" (not "tracking") for a playbook
    with no accounting period to track, and no free-form bigquery_sql step
    when a playbook already covers the report."""
    prompt = compiler._SYSTEM_PROMPT
    assert 'mode="period"' in prompt
    assert 'mode="tracking"' in prompt
    assert "no free-form bigquery_sql step" in prompt or "without a separate bigquery_sql step" in prompt


def test_system_prompt_includes_the_four_step_inventory_aging_worked_example():
    """The correct 4-step plan as the worked example for the Inventory Aging
    instruction (item 2, binding) -- report.compose -> report.render_pdf ->
    report.build_xlsx -> ONE drive.upload, naming inventory_aging."""
    prompt = compiler._SYSTEM_PROMPT
    assert "inventory_aging" in prompt
    assert prompt.count("report.compose") >= 1
    assert "report.render_pdf" in prompt
    assert "report.build_xlsx" in prompt
    assert "drive.upload" in prompt


async def test_tenant_locations_are_fed_to_the_compiler_prompt(db, tenant_a, monkeypatch):
    """Spec §B3: "plus the tenant's context (connections available, locations
    known from the snapshot, existing reports)" — via the registry-provided
    bigquery_sql context hook (compiler._tenant_locations)."""
    monkeypatch.setattr(
        compiler, "_tenant_locations", lambda *_a, **_kw: _async_list(["Dimerco", "Fedex", "Panurgy", "Virtual"])
    )
    fake = FakeAdapter([_compile_plan_response(_correct_four_step_playbook_plan())])

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
    fake = FakeAdapter([_compile_plan_response(_correct_four_step_playbook_plan())])

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
    fake = FakeAdapter([_compile_plan_response(_correct_four_step_playbook_plan())])

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
    fake = FakeAdapter([_compile_plan_response(_correct_four_step_playbook_plan())])

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
    fake = FakeAdapter([_compile_plan_response(_correct_four_step_playbook_plan())])

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


def test_plan_diff_reports_a_reorder_even_when_params_are_unchanged():
    """Item 8 (gate fix): matching by step id and reporting only param/type
    changes means swapping two steps produced an EMPTY diff before this fix
    — an operator approving "no changes" would silently approve a reordered
    plan."""
    old = _inventory_aging_plan()
    new = _inventory_aging_plan()
    # Swap steps 3 (id "s3", report.render_pdf) and 4 (id "s4",
    # report.build_xlsx) — each step's OWN params are unchanged, only their
    # positions moved.
    new["steps"][2], new["steps"][3] = new["steps"][3], new["steps"][2]

    lines = plan_diff(old, new)
    assert lines, "a reorder must not produce an empty diff"

    ctx_by_step = {line.step: line.text for line in lines if line.kind == "ctx"}
    # id "s4" (report.build_xlsx) is now at position 3, was at old position 4.
    assert 3 in ctx_by_step and "moved from step 4" in ctx_by_step[3]
    # id "s3" (report.render_pdf) is now at position 4, was at old position 3.
    assert 4 in ctx_by_step and "moved from step 3" in ctx_by_step[4]
    # No param changes on either step -> no del/add lines for them, just the
    # position-change ctx line.
    assert not any(line.kind in ("del", "add") for line in lines if line.step in (3, 4))
    # Steps 1, 2, 5 did not move and are untouched -> no hunks for them.
    assert set(ctx_by_step) == {3, 4}


def test_plan_diff_reports_a_removed_step_with_its_params_as_del_lines():
    old = _inventory_aging_plan()
    new = _inventory_aging_plan()
    del new["steps"][2]  # drop report.render_pdf (id "s3") entirely

    lines = plan_diff(old, new)
    removed = [line for line in lines if line.kind == "ctx" and line.step is None and "removed" in line.text]
    assert len(removed) == 1
    assert "s3" in removed[0].text
    assert "report.render_pdf" in removed[0].text

    del_lines = [line for line in lines if line.kind == "del" and line.step is None]
    assert any("report_step: s2" in line.text for line in del_lines)


async def _async_list(items: list) -> list:
    return items

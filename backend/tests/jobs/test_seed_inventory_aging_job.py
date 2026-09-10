"""Tests for backend/scripts/seed_inventory_aging_job.py (Slice 2, Task 8).

Spec: docs/superpowers/specs/2026-09-08-scheduled-jobs-and-inventory-aging-design.md
§B7. ``main(tenant_id, *, db, owner_id=None, llm=None)`` creates (or, on a rerun,
returns) the **Inventory Aging Weekly** Scheduled Job from the mock's instruction
text, compiled via the real compiler (``app.services.jobs.compiler.compile_instruction``)
into the CORRECT four-step plan (brief H, item 2a: registry.validate_plan
now rejects a bigquery_sql step alongside a playbook-keyed report.compose —
the mock's original five-step illustration is no longer a valid compiled
shape), left ``plan_status = "pending_approval"`` —
approving it, running it, and going live are a human's actions on the Scheduled
jobs page, not this script's.

Drives ``compile_instruction`` with a ``FakeAdapter`` injected via the ``llm=``
param — same pattern as ``tests/jobs/test_compiler.py`` — so no network call
happens and every scenario is deterministic.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

import scripts.seed_inventory_aging_job as seed_inventory_aging_job
from app.models.audit import AuditEvent
from app.models.pipeline import Schedule
from app.services.chat.llm_adapter import LLMResponse, ToolUseBlock
from app.services.jobs.compiler import CompilerLLM
from tests.conftest import create_test_tenant, create_test_user
from tests.jobs import test_compiler as compiler_fixtures


class FakeAdapter:
    """Queue of canned LLMResponse objects, one per `create_message` call —
    identical shape to ``tests/jobs/test_compiler.py``'s own ``FakeAdapter``."""

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
    return LLMResponse(tool_use_blocks=[ToolUseBlock(id=tool_use_id, name="compile_plan", input=plan_json)])


def _clarify_response(question: str, tool_use_id: str = "tu_1") -> LLMResponse:
    return LLMResponse(
        tool_use_blocks=[ToolUseBlock(id=tool_use_id, name="ask_clarification", input={"question": question})]
    )


def _five_step_plan() -> dict:
    """The CORRECT compiled Inventory Aging Weekly plan (compose -> render PDF
    -> build Excel -> upload to Drive, no free-form bigquery_sql step
    alongside the playbook compose) — reuses
    ``test_compiler._correct_four_step_playbook_plan``'s exact fixture so this
    test and the compiler's own tests agree on what a valid compiled plan
    looks like. Kept under its original name (still "the mock's plan" this
    seed script compiles) even though the mock's own five-step illustration
    is no longer a validate_plan-legal shape — see brief H, item 2a."""
    return compiler_fixtures._correct_four_step_playbook_plan()


async def _schedule_rows(db, tenant_id) -> list[Schedule]:
    rows = (
        (await db.execute(select(Schedule).where(Schedule.tenant_id == tenant_id, Schedule.schedule_type == "job")))
        .scalars()
        .all()
    )
    return list(rows)


def test_instruction_keeps_the_mocks_original_text_and_adds_the_build_fresh_clarification():
    """Live-run defect (brief G, item 4): on staging the seed's compile asked
    whether to reuse an existing "Dimerco Inventory Aging Report" from an
    August chat, so the non-interactive seed created nothing — the compiler
    had a genuine question (refresh-or-reuse) the instruction never answered.
    INSTRUCTION now ends with an explicit "build a brand-new report" answer
    to that exact question, so the compiler never has a reason to clarify;
    the mock's own text (verbatim) stays untouched before it."""
    mock_text = (
        "Every Monday at 6am Pacific, build the inventory aging report for Dimerco, Fedex and "
        "Panurgy from the BigQuery inventory snapshot. Age each SKU by days since its last "
        "restock, bucket 0–30 / 31–60 / 61–90 / 91–180 / 180+, compare with the prior week "
        "and show the nine-week trend of aged share. Save a PDF of the report and an Excel "
        "workbook with every SKU per location to Google Drive under Reports / Inventory aging. "
        "If a run fails, retry once and then pause and tell me."
    )
    assert seed_inventory_aging_job.INSTRUCTION.startswith(mock_text)
    assert (
        "Build a brand-new report each run with the inventory_aging playbook covering all "
        "three locations; do not refresh or extend any existing report."
    ) in seed_inventory_aging_job.INSTRUCTION


async def test_main_seeds_a_pending_approval_schedule_from_the_mocks_instruction(db, monkeypatch):
    monkeypatch.setattr(
        "app.services.jobs.compiler._tenant_locations",
        lambda *_a, **_kw: compiler_fixtures._async_list(["Dimerco", "Fedex", "Panurgy"]),
    )
    tenant = await create_test_tenant(db, name="SeedInventoryAging")
    owner, _ = await create_test_user(db, tenant)
    owner_id = owner.id
    fake = FakeAdapter([_compile_plan_response(_five_step_plan())])

    schedule = await seed_inventory_aging_job.main(
        tenant.id,
        db=db,
        owner_id=owner_id,
        llm=CompilerLLM(adapter=fake, model="fake-model"),
    )

    assert schedule.tenant_id == tenant.id
    assert schedule.name == "Inventory Aging Weekly"
    assert schedule.schedule_type == "job"
    assert schedule.instruction == seed_inventory_aging_job.INSTRUCTION
    assert schedule.cron_expression == "0 6 * * 1"
    assert schedule.timezone == "America/Los_Angeles"
    assert schedule.plan_status == "pending_approval"
    assert schedule.plan_version == 0
    assert schedule.is_active is True
    assert schedule.owner_id == owner_id
    assert schedule.delivery_json == {
        "drive": {"folder": "Reports / Inventory aging"},
        "in_app": {"report_title": "Inventory Aging Weekly"},
    }
    assert schedule.budget_json == {"bytes_scanned": 5_000_000_000, "seconds": 600, "usd": 2.0}
    assert [s["type"] for s in schedule.plan_json["steps"]] == [
        "report.compose",
        "report.render_pdf",
        "report.build_xlsx",
        "drive.upload",
    ]
    assert len(fake.calls) == 1  # exactly one compiler call, no repair round needed

    rows = await _schedule_rows(db, tenant.id)
    assert len(rows) == 1

    events = (
        (
            await db.execute(
                select(AuditEvent).where(AuditEvent.tenant_id == tenant.id, AuditEvent.action == "schedule.create")
            )
        )
        .scalars()
        .all()
    )
    assert len(events) == 1
    assert events[0].actor_type == "system"
    assert events[0].payload["seed"] == "inventory_aging_weekly"


async def test_main_is_idempotent_and_never_recompiles_on_a_rerun(db, monkeypatch):
    monkeypatch.setattr(
        "app.services.jobs.compiler._tenant_locations",
        lambda *_a, **_kw: compiler_fixtures._async_list([]),
    )
    tenant = await create_test_tenant(db, name="SeedInventoryAgingRerun")
    # Only ONE canned response: a second compile_instruction call would raise
    # AssertionError from the fake adapter running dry, proving the rerun never
    # recompiles.
    fake = FakeAdapter([_compile_plan_response(_five_step_plan())])
    llm = CompilerLLM(adapter=fake, model="fake-model")

    first = await seed_inventory_aging_job.main(tenant.id, db=db, llm=llm)
    second = await seed_inventory_aging_job.main(tenant.id, db=db, llm=llm)

    assert second.id == first.id
    assert len(fake.calls) == 1

    rows = await _schedule_rows(db, tenant.id)
    assert len(rows) == 1


async def test_main_raises_and_creates_nothing_on_a_clarification(db, monkeypatch):
    monkeypatch.setattr(
        "app.services.jobs.compiler._tenant_locations",
        lambda *_a, **_kw: compiler_fixtures._async_list([]),
    )
    tenant = await create_test_tenant(db, name="SeedInventoryAgingClarify")
    question = "Which locations should count as Inventory Aging locations?"
    fake = FakeAdapter([_clarify_response(question)])

    try:
        await seed_inventory_aging_job.main(tenant.id, db=db, llm=CompilerLLM(adapter=fake, model="fake-model"))
        raise AssertionError("expected NeedsClarificationError")
    except seed_inventory_aging_job.NeedsClarificationError as exc:
        assert exc.question == question

    rows = await _schedule_rows(db, tenant.id)
    assert rows == []


async def test_run_cli_exits_non_zero_and_prints_the_question_on_clarification(monkeypatch, capsys):
    """A clarification must not crash the CLI invocation with an unhandled
    traceback (same "cron invocation exits cleanly on a known failure" contract
    as ``compose_inventory_aging``'s ``SourceTruncated`` path) — it exits 1,
    naming the question, and creates nothing."""

    async def raising_main(tenant_id, *, db, owner_id=None):
        raise seed_inventory_aging_job.NeedsClarificationError("Which locations?")

    monkeypatch.setattr(seed_inventory_aging_job, "main", raising_main)

    with pytest.raises(SystemExit) as exc:
        await seed_inventory_aging_job._run_cli(uuid.uuid4(), None)

    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert "Which locations?" in captured.err

"""Step registry (Slice 2, Task 1). Spec §B2 (binding):

    STEP_REGISTRY: dict[str, StepSpec] — v1 types bigquery_sql, report.compose,
    report.render_pdf, report.build_xlsx, drive.upload, recon.run. Anything else
    is rejected at compile and at run (two choke points). The registry is the
    ONLY place a step type is defined; the compiler's output schema is generated
    from it.

Two choke points means TWO tests: `validate_plan` (compile time, this file) and
the executor's own registry lookup (run time, Task 4 — out of scope here since
no LLM/executor exists yet on this branch)."""

import pytest

from app.services.jobs.registry import (
    STEP_REGISTRY,
    PlanInvalid,
    StepSpec,
    plan_schema,
    validate_plan,
)

V1_TYPES = {
    "bigquery_sql": "read",
    "report.compose": "read",
    "report.render_pdf": "read",
    "report.build_xlsx": "read",
    "drive.upload": "write",
    "recon.run": "read",
}


def test_registry_has_exactly_the_v1_types_with_the_right_kinds():
    assert set(STEP_REGISTRY) == set(V1_TYPES)
    for step_type, expected_kind in V1_TYPES.items():
        spec = STEP_REGISTRY[step_type]
        assert isinstance(spec, StepSpec)
        assert spec.type == step_type
        assert spec.kind == expected_kind
        assert spec.label  # non-empty, human-readable
        assert isinstance(spec.params_schema, dict)
        assert callable(spec.executor)


def test_write_steps_carry_an_idempotency_function_read_steps_do_not():
    """The only write step in v1 is drive.upload — every write step MUST declare
    how to derive its idempotency key (spec §0.5); read steps never need one."""
    for step_type, spec in STEP_REGISTRY.items():
        if spec.kind == "write":
            assert spec.idempotency is not None, f"{step_type} is a write step with no idempotency fn"
        else:
            assert spec.idempotency is None, f"{step_type} is a read step but declares an idempotency fn"


def test_plan_schema_enumerates_only_registry_types():
    schema = plan_schema()
    step_item_schema = schema["properties"]["steps"]["items"]
    type_enum = set(step_item_schema["properties"]["type"]["enum"])
    assert type_enum == set(STEP_REGISTRY)
    # not a single extra type (e.g. the mock's illustrative email.send, out of
    # scope per spec §0 "Out of scope now: ... email delivery")
    assert "email.send" not in type_enum


def _valid_plan() -> dict:
    """A minimal three-step plan that validates cleanly: query -> compose -> upload,
    with drive.upload correctly referencing the compose step's id."""
    return {
        "steps": [
            {
                "id": "s1",
                "type": "bigquery_sql",
                "params": {"query": "SELECT 1"},
            },
            {
                "id": "s2",
                "type": "report.compose",
                "params": {"playbook_key": "inventory_aging", "params": {"locations": ["Dimerco"]}},
            },
            {
                "id": "s3",
                "type": "drive.upload",
                "params": {"report_step": "s2"},
            },
        ]
    }


def test_validate_plan_accepts_a_well_formed_plan():
    validated = validate_plan(_valid_plan())
    assert [s.id for s in validated.steps] == ["s1", "s2", "s3"]
    assert [s.type for s in validated.steps] == ["bigquery_sql", "report.compose", "drive.upload"]


def test_validate_plan_rejects_an_unknown_step_type():
    plan = _valid_plan()
    plan["steps"][0]["type"] = "netsuite.write"  # not in the registry, never will be
    with pytest.raises(PlanInvalid) as exc_info:
        validate_plan(plan)
    assert any("netsuite.write" in msg for msg in exc_info.value.errors)


def test_validate_plan_rejects_drive_upload_referencing_an_artifact_no_earlier_step_produces():
    plan = _valid_plan()
    # s3 (drive.upload) points at a step id that does not exist in the plan at all.
    plan["steps"][2]["params"]["report_step"] = "does-not-exist"
    with pytest.raises(PlanInvalid) as exc_info:
        validate_plan(plan)
    assert any("does-not-exist" in msg for msg in exc_info.value.errors)


def test_validate_plan_rejects_drive_upload_referencing_a_later_step():
    """A forward reference is just as invalid as a nonexistent one — the step that
    would produce the artifact has not run yet when drive.upload would run."""
    plan = _valid_plan()
    plan["steps"] = [
        {"id": "s1", "type": "drive.upload", "params": {"report_step": "s2"}},
        {"id": "s2", "type": "report.compose", "params": {"report_id": "11111111-1111-1111-1111-111111111111"}},
    ]
    with pytest.raises(PlanInvalid) as exc_info:
        validate_plan(plan)
    assert any("s2" in msg for msg in exc_info.value.errors)


def test_validate_plan_rejects_params_violating_a_steps_schema():
    plan = _valid_plan()
    del plan["steps"][0]["params"]["query"]  # bigquery_sql requires "query"
    with pytest.raises(PlanInvalid) as exc_info:
        validate_plan(plan)
    assert any(msg.startswith("step 1") for msg in exc_info.value.errors)


def test_validate_plan_rejects_report_compose_with_neither_playbook_nor_report_id():
    plan = _valid_plan()
    plan["steps"][1]["params"] = {}
    with pytest.raises(PlanInvalid):
        validate_plan(plan)


def test_validate_plan_rejects_report_compose_with_both_playbook_and_report_id():
    """Ambiguous — a compiled step must mean exactly one of "compose a new report"
    or "refresh an existing one", never both."""
    plan = _valid_plan()
    plan["steps"][1]["params"] = {
        "playbook_key": "inventory_aging",
        "params": {},
        "report_id": "11111111-1111-1111-1111-111111111111",
    }
    with pytest.raises(PlanInvalid):
        validate_plan(plan)


def test_validate_plan_rejects_duplicate_step_ids():
    plan = _valid_plan()
    plan["steps"][1]["id"] = "s1"
    with pytest.raises(PlanInvalid) as exc_info:
        validate_plan(plan)
    assert any("duplicate" in msg.lower() for msg in exc_info.value.errors)


def test_validate_plan_rejects_a_non_dict_plan():
    with pytest.raises(PlanInvalid):
        validate_plan([])  # type: ignore[arg-type]


def test_validate_plan_rejects_empty_steps():
    with pytest.raises(PlanInvalid):
        validate_plan({"steps": []})


def test_validate_plan_collects_every_error_not_just_the_first():
    """A per-step message for EACH bad step (spec §B3 says validate_plan raises
    PlanInvalid with a per-step message) — a compiler repair round needs to see
    every problem in one shot, not fix one and re-submit N times."""
    plan = {
        "steps": [
            {"id": "s1", "type": "not.a.real.type", "params": {}},
            {"id": "s2", "type": "bigquery_sql", "params": {}},  # missing "query"
        ]
    }
    with pytest.raises(PlanInvalid) as exc_info:
        validate_plan(plan)
    assert len(exc_info.value.errors) == 2


# ---------------------------------------------------------------------------
# drive.upload's period comes from the RUN, never from the compiled plan.
# ---------------------------------------------------------------------------


def test_validate_plan_rejects_drive_upload_carrying_a_compiled_period_key():
    """A plan is compiled ONCE and replayed every week (spec §0.3). A literal
    `period_key` baked into the plan would therefore be the compile date on
    every run — the Drive filename and the write step's idempotency key
    (spec §0.5: "job + period key"; the mock: "job + snapshot date") would
    never vary between weeks. The registry makes that unrepresentable: the
    period is the run's own (`StepContext.period_key`), and a compiled
    drive.upload step may not carry one."""
    plan = _valid_plan()
    plan["steps"][2]["params"] = {"report_step": "s2", "period_key": "2026-09-08"}
    with pytest.raises(PlanInvalid) as exc_info:
        validate_plan(plan)
    assert any("period_key" in msg for msg in exc_info.value.errors)


@pytest.mark.asyncio
async def test_drive_upload_uses_the_runs_period_key_for_delivery_and_idempotency(monkeypatch):
    """The executor hands `deliver_report_to_drive` the RUN's period (the due
    date in the schedule's timezone, set by the run loop), and the
    idempotency key is `job:{schedule id}:period:{that same period}` — so a
    retry of the same period reuses the key and next week's run gets a new one."""
    import uuid
    from datetime import datetime, timezone
    from types import SimpleNamespace

    from app.services.jobs.registry import StepContext
    from app.services.report import report_delivery

    captured: dict = {}

    async def fake_deliver(db, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            pdf_file_id="pdf1",
            pdf_url="https://drive/pdf1",
            xlsx_file_id="xlsx1",
            xlsx_url="https://drive/xlsx1",
            folder_id="folder1",
            delivered_at=datetime(2026, 9, 14, 13, 0, tzinfo=timezone.utc),
        )

    monkeypatch.setattr(report_delivery, "deliver_report_to_drive", fake_deliver)

    schedule_id = uuid.uuid4()
    report_id = uuid.uuid4()
    ctx = StepContext(
        job_id=schedule_id,
        run_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        db=object(),
        artifacts={"s2": {"report": object(), "report_id": str(report_id)}},
        period_key="2026-09-14",
    )
    spec = STEP_REGISTRY["drive.upload"]
    params = {"report_step": "s2"}

    artifact = await spec.executor(ctx, params)

    assert captured["period_key"] == "2026-09-14"
    assert captured["report_id"] == report_id
    assert artifact["pdf_file_id"] == "pdf1"
    assert spec.idempotency(ctx, params) == f"job:{schedule_id}:period:2026-09-14"


@pytest.mark.asyncio
async def test_drive_upload_without_a_run_period_is_a_step_execution_error():
    """A caller that forgot to set the run's period (the run loop always
    does) must fail as a RUN-time step error — never silently upload under a
    `None` period or a stale compiled one."""
    import uuid

    from app.services.jobs.registry import StepContext, StepExecutionError

    ctx = StepContext(
        job_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        db=object(),
        artifacts={"s2": {"report": object(), "report_id": str(uuid.uuid4())}},
        period_key=None,
    )
    with pytest.raises(StepExecutionError):
        await STEP_REGISTRY["drive.upload"].executor(ctx, {"report_step": "s2"})

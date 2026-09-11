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
    with drive.upload correctly referencing the compose step's id.

    s2 refreshes an EXISTING report (``report_id``) rather than composing a NEW
    playbook one — item 2a (brief H) forbids a ``bigquery_sql`` step alongside a
    ``playbook_key`` compose in the SAME plan (nothing in the registry ever
    consumes a bigquery_sql step's rows, so that combination is always invalid),
    and s1's own free-form query is deliberately unrelated to what s2 does — so
    ``report_id`` keeps this fixture a well-formed baseline without tripping
    that new rule."""
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
                "params": {"report_id": "11111111-1111-1111-1111-111111111111"},
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

    # Item 9 (gate fix): the executor now passes a schedule-keyed
    # DeliveryIdentity — inventory_aging composes a NEW Report every run
    # (mode="period"), so keying Drive identity on the report row would
    # create a new Drive folder every Monday and duplicate files on retry.
    # Item 3 (delta gate fix): file_props/lock_key/idempotency_prefix also
    # carry the PRODUCING step's id — a plan with two report.compose ->
    # drive.upload chains must not overwrite the same files (see the test
    # below). Item 1 (delta gate fix #2): `file_props` never carries
    # `period_key` any more — `deliver_report_to_drive` merges the CURRENT
    # call's period in itself (see that function's own docstring), and
    # `idempotency_prefix` replaces `idempotency_key` for the same reason
    # (the period is appended by the delivery function, not baked in here).
    identity = captured["identity"]
    assert identity.folder_props == {"schedule_id": str(schedule_id)}
    assert identity.file_props == {"schedule_id": str(schedule_id), "report_step": "s2"}
    assert identity.lock_key == f"schedule:{schedule_id}:s2"
    assert identity.idempotency_prefix == f"job-delivery:{schedule_id}:s2"


@pytest.mark.asyncio
async def test_two_drive_upload_steps_in_one_run_get_distinct_identities_same_folder(monkeypatch):
    """Item 3 (delta gate fix): `_drive_upload_executor` used to key files on
    `schedule_id` + `period_key` ONLY -- a plan with two `report.compose ->
    drive.upload` chains (two different reports delivered by the same
    schedule run) collided on the SAME file identity and the SAME advisory
    lock. Adding the producing step's id (`report_step`) to both keeps the
    FOLDER identity shared (still schedule-only -- one Drive folder per
    schedule) while giving each upload its own file identity and lock key."""
    import uuid
    from datetime import datetime, timezone
    from types import SimpleNamespace

    from app.services.jobs.registry import StepContext
    from app.services.report import report_delivery

    captured: list[dict] = []

    async def fake_deliver(db, **kwargs):
        captured.append(kwargs)
        return SimpleNamespace(
            pdf_file_id=f"pdf-{len(captured)}",
            pdf_url="https://drive/pdf",
            xlsx_file_id=f"xlsx-{len(captured)}",
            xlsx_url="https://drive/xlsx",
            folder_id="folder1",
            delivered_at=datetime(2026, 9, 14, 13, 0, tzinfo=timezone.utc),
        )

    monkeypatch.setattr(report_delivery, "deliver_report_to_drive", fake_deliver)

    schedule_id = uuid.uuid4()
    ctx = StepContext(
        job_id=schedule_id,
        run_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        db=object(),
        artifacts={
            "compose_a": {"report": object(), "report_id": str(uuid.uuid4())},
            "compose_b": {"report": object(), "report_id": str(uuid.uuid4())},
        },
        period_key="2026-09-14",
    )
    spec = STEP_REGISTRY["drive.upload"]

    await spec.executor(ctx, {"report_step": "compose_a"})
    await spec.executor(ctx, {"report_step": "compose_b"})

    identity_a = captured[0]["identity"]
    identity_b = captured[1]["identity"]

    assert identity_a.folder_props == identity_b.folder_props == {"schedule_id": str(schedule_id)}
    assert identity_a.file_props != identity_b.file_props
    assert identity_a.lock_key == f"schedule:{schedule_id}:compose_a"
    assert identity_b.lock_key == f"schedule:{schedule_id}:compose_b"
    # Item 1 (delta gate fix #2): idempotency_prefix carries no period at all
    # any more — deliver_report_to_drive appends the RUN's period itself.
    assert identity_a.idempotency_prefix == f"job-delivery:{schedule_id}:compose_a"
    assert identity_b.idempotency_prefix == f"job-delivery:{schedule_id}:compose_b"


def test_schedule_delivery_identity_shape():
    """Item 1 (delta gate fix #2): the ONE helper both `_drive_upload_executor`
    and `_report_compose_executor`'s identity stamp use — `folder_props`
    schedule-only (one Drive folder per schedule), `file_props`/`lock_key`/
    `idempotency_prefix` all also carry the PRODUCING `report.compose` step's
    id (item 3's own fix, preserved here), and `file_props` never carries
    `period_key` (item 1, delta gate fix #2 — `deliver_report_to_drive` merges
    the CURRENT call's period in itself, so a period baked in here could
    never go stale)."""
    import uuid

    from app.services.jobs.registry import schedule_delivery_identity

    schedule_id = uuid.uuid4()
    identity = schedule_delivery_identity(schedule_id, "compose")

    assert identity.folder_props == {"schedule_id": str(schedule_id)}
    assert identity.file_props == {"schedule_id": str(schedule_id), "report_step": "compose"}
    assert identity.lock_key == f"schedule:{schedule_id}:compose"
    assert identity.idempotency_prefix == f"job-delivery:{schedule_id}:compose"


# ---------------------------------------------------------------------------
# Live-run defect (brief G, item 1): validate_plan rejects what the executor
# cannot run — a report_step not pointing at a report.compose step, a
# tracking-mode compose for a non-period-based playbook, and an unqualified
# BigQuery table name. All three are compile-time errors so a bad plan is
# never persisted (staging's first live run hit all three at once).
# ---------------------------------------------------------------------------


def test_validate_plan_rejects_render_pdf_whose_report_step_is_not_a_compose_step():
    """The executor's `_resolve_report_step_artifact` needs a `report.compose`
    artifact (it looks for `artifact["report"]`) — a `report_step` naming any
    other step type produces "produced no report artifact" at RUN time today.
    Compile time must catch it instead."""
    plan = {
        "steps": [
            {"id": "s1", "type": "bigquery_sql", "params": {"query": "SELECT 1"}},
            {"id": "s2", "type": "report.render_pdf", "params": {"report_step": "s1"}},
        ]
    }
    with pytest.raises(PlanInvalid) as exc_info:
        validate_plan(plan)
    assert any("s1" in msg and "report.compose" in msg for msg in exc_info.value.errors)


def test_validate_plan_rejects_build_xlsx_whose_report_step_is_not_a_compose_step():
    plan = {
        "steps": [
            {"id": "s1", "type": "bigquery_sql", "params": {"query": "SELECT 1"}},
            {"id": "s2", "type": "report.build_xlsx", "params": {"report_step": "s1"}},
        ]
    }
    with pytest.raises(PlanInvalid) as exc_info:
        validate_plan(plan)
    assert any("s1" in msg and "report.compose" in msg for msg in exc_info.value.errors)


def test_validate_plan_rejects_drive_upload_whose_report_step_is_a_render_pdf_step():
    """This is the exact live-run shape: `upload_pdf` (drive.upload) named
    `render_pdf`'s own id as its `report_step`, instead of the `report.compose`
    step both render_pdf and build_xlsx themselves consume."""
    plan = {
        "steps": [
            {
                "id": "compose_report",
                "type": "report.compose",
                "params": {"playbook_key": "inventory_aging", "params": {}},
            },
            {"id": "render_pdf", "type": "report.render_pdf", "params": {"report_step": "compose_report"}},
            {"id": "upload_pdf", "type": "drive.upload", "params": {"report_step": "render_pdf"}},
        ]
    }
    with pytest.raises(PlanInvalid) as exc_info:
        validate_plan(plan)
    assert any("render_pdf" in msg and "report.compose" in msg and "upload_pdf" in msg for msg in exc_info.value.errors)


def test_validate_plan_rejects_tracking_mode_compose_for_a_non_period_based_playbook():
    """`compose_playbook_report` already refuses `mode="tracking"` at RUN time
    for a playbook whose `PLAYBOOKS[key]["period_based"]` is False —
    inventory_aging is one today (no NetSuite accounting period to track).
    Compile time must catch it instead of persisting a plan that fails every
    time it runs."""
    plan = {
        "steps": [
            {
                "id": "compose_report",
                "type": "report.compose",
                "params": {
                    "playbook_key": "inventory_aging",
                    "mode": "tracking",
                    "params": {"locations": ["Dimerco", "Fedex", "Panurgy"]},
                },
            }
        ]
    }
    with pytest.raises(PlanInvalid) as exc_info:
        validate_plan(plan)
    assert any(
        "compose_report" in msg and "tracking" in msg and "inventory_aging" in msg for msg in exc_info.value.errors
    )


def test_validate_plan_accepts_tracking_mode_compose_for_a_period_based_playbook():
    """Sanity check the new rule isn't overbroad: income_statement IS
    period_based, so mode="tracking" for it must still validate cleanly."""
    plan = {
        "steps": [
            {
                "id": "compose_report",
                "type": "report.compose",
                "params": {"playbook_key": "income_statement", "mode": "tracking", "params": {}},
            }
        ]
    }
    validated = validate_plan(plan)
    assert [s.id for s in validated.steps] == ["compose_report"]


def test_validate_plan_rejects_the_exact_live_failing_six_step_plan_for_every_reason():
    """Regression pin for the staging incident this item exists for
    (brief G): the compiled plan that actually ran and failed step 1, and
    would have failed steps 2 and 5 too. Both of the remaining
    validate_plan-level rules must fire on it, collected together (never just
    the first). Step 1's own unqualified-table problem (`FROM
    inventory_snapshot`) is no longer caught HERE — brief H, item 1 replaced
    that regex heuristic with a real BigQuery dry run at COMPILE time in
    app.services.jobs.compiler (`_bigquery_preflight`), which validate_plan
    itself has no way to run (it is a pure, synchronous, no-I/O function) —
    see tests/jobs/test_compiler.py's own preflight tests for that coverage."""
    plan = {
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
    with pytest.raises(PlanInvalid) as exc_info:
        validate_plan(plan)
    errors = exc_info.value.errors
    assert any("tracking" in msg and "inventory_aging" in msg for msg in errors)
    assert any("upload_pdf" in msg and "render_pdf" in msg for msg in errors)
    assert any("upload_xlsx" in msg and "build_xlsx" in msg for msg in errors)
    # Item 2a (brief H) does NOT also fire here: this plan's report.compose
    # step already fails its OWN, more specific check (mode="tracking" is
    # unsupported for inventory_aging) and so is never added to the
    # validated `steps` list the 2a post-pass reads from — one clear error
    # per step, not a redundant second one layered on top. See the dedicated
    # 2a/2b fixture below (_playbook_plan_with_orphan_sql_and_double_upload)
    # for a report.compose step that DOES reach the 2a check.


# ---------------------------------------------------------------------------
# Brief H, item 2: validate_plan enforces two invariants the compiler's own
# system prompt only STEERS the model toward — a plan can still slip past the
# model's judgment and reach validate_plan with either shape, so the registry
# (the actual allow-list) must refuse both itself.
# ---------------------------------------------------------------------------


def _correct_four_step_playbook_plan() -> dict:
    """The correct shape for a playbook-covered instruction: report.compose
    (playbook_key, no bigquery_sql alongside it) -> render_pdf -> build_xlsx
    -> exactly ONE drive.upload naming the compose step."""
    return {
        "steps": [
            {
                "id": "compose_report",
                "type": "report.compose",
                "params": {"playbook_key": "inventory_aging", "params": {"locations": ["Dimerco"]}},
            },
            {"id": "render_pdf", "type": "report.render_pdf", "params": {"report_step": "compose_report"}},
            {"id": "build_xlsx", "type": "report.build_xlsx", "params": {"report_step": "compose_report"}},
            {"id": "upload", "type": "drive.upload", "params": {"report_step": "compose_report"}},
        ]
    }


def _playbook_plan_with_orphan_sql_and_double_upload() -> dict:
    """A 6-step plan that fails BOTH new invariants at once: a bigquery_sql
    step alongside a report.compose step carrying playbook_key (2a — no step
    in the registry ever consumes a bigquery_sql step's rows, so this
    combination is always dead weight once a playbook already owns its own
    dataset-qualified sources), AND two drive.upload steps that both
    correctly reference the SAME report.compose step (2b — deliver_report_to_
    drive already uploads the PDF and the Excel workbook in one call, so a
    second upload targeting the identical compose step would double-deliver).

    Deliberately a DIFFERENT shape from
    ``test_validate_plan_rejects_the_exact_live_failing_six_step_plan_for_every_reason``'s
    fixture above: that plan's two drive.upload steps each name a DIFFERENT
    wrongly-typed step (render_pdf, build_xlsx) — already caught by the
    existing x-step-ref-type check — and so never reaches the "two valid
    refs naming the SAME compose step" case 2b exists for."""
    return {
        "steps": [
            {
                "id": "snapshot_query",
                "type": "bigquery_sql",
                "params": {"query": "SELECT 1 FROM `frameworkreporting.inventory_snapshot`"},
            },
            {
                "id": "compose_report",
                "type": "report.compose",
                "params": {"playbook_key": "inventory_aging", "params": {"locations": ["Dimerco"]}},
            },
            {"id": "render_pdf", "type": "report.render_pdf", "params": {"report_step": "compose_report"}},
            {"id": "build_xlsx", "type": "report.build_xlsx", "params": {"report_step": "compose_report"}},
            {"id": "upload_1", "type": "drive.upload", "params": {"report_step": "compose_report"}},
            {"id": "upload_2", "type": "drive.upload", "params": {"report_step": "compose_report"}},
        ]
    }


def test_validate_plan_rejects_bigquery_sql_alongside_a_playbook_compose():
    plan = _playbook_plan_with_orphan_sql_and_double_upload()
    with pytest.raises(PlanInvalid) as exc_info:
        validate_plan(plan)
    assert any(
        "snapshot_query" in msg and "bigquery_sql is not allowed" in msg and "playbook" in msg
        for msg in exc_info.value.errors
    )


def test_validate_plan_rejects_a_second_drive_upload_targeting_the_same_compose_step():
    plan = _playbook_plan_with_orphan_sql_and_double_upload()
    with pytest.raises(PlanInvalid) as exc_info:
        validate_plan(plan)
    assert any("upload_1" in msg and "upload_2" in msg and "compose_report" in msg for msg in exc_info.value.errors)


def test_validate_plan_accepts_the_correct_four_step_playbook_plan():
    """Sanity check the two new rules aren't overbroad: the correct 4-step
    plan (one bigquery-sql-free playbook compose, one drive.upload) must
    still validate cleanly."""
    validated = validate_plan(_correct_four_step_playbook_plan())
    assert [s.type for s in validated.steps] == [
        "report.compose",
        "report.render_pdf",
        "report.build_xlsx",
        "drive.upload",
    ]


def test_validate_plan_accepts_two_drive_uploads_targeting_two_different_compose_steps():
    """Sanity check 2b isn't overbroad either: a plan with TWO separate
    report.compose -> drive.upload chains (two different reports delivered by
    the same schedule run) is a legitimate shape and must still validate."""
    plan = {
        "steps": [
            {"id": "compose_a", "type": "report.compose", "params": {"playbook_key": "inventory_aging", "params": {}}},
            {"id": "upload_a", "type": "drive.upload", "params": {"report_step": "compose_a"}},
            {
                "id": "compose_b",
                "type": "report.compose",
                "params": {"report_id": "11111111-1111-1111-1111-111111111111"},
            },
            {"id": "upload_b", "type": "drive.upload", "params": {"report_step": "compose_b"}},
        ]
    }
    validated = validate_plan(plan)
    assert [s.id for s in validated.steps] == ["compose_a", "upload_a", "compose_b", "upload_b"]


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

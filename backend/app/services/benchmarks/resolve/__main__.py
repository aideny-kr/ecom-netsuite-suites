"""Resolve benchmark command line.

    python -m app.services.benchmarks.resolve split --tasks tasks.json
    python -m app.services.benchmarks.resolve snapshot --tasks tasks.json --out-dir snapshots/ \\
        --tenant <uuid> --items 1471,5005
    python -m app.services.benchmarks.resolve run --tasks tasks.json (--snapshots snapshots/ | --labels labels/) \\
        --tape tape.jsonl --out results.json --tenant <uuid> --actor <user uuid> --model <model> \\
        [--mode record] [--limit 3]

`snapshot` reads each order once, read-only, as the server's credit check reads it, so both
agents are graded by outcome against the same facts (outcome.py). `--items` are the credit
and tax items the booking profile allows (a credit on any other item fails the playbook).

`run` drives today's agent and needs the platform model key, so it runs in the staging
container. `--mode record` reads live and saves every read to the tape. `--mode replay`
(the default) never reaches an outside system, and marks a trial not comparable when it
needed something the tape cannot give.

Measure TODAY'S agent with `--mode record`: its approval cards come from session state
that some reads leave behind, which replay deliberately does not restore (see `tape`).
Replay is for agents whose reads all go through tools (the reference runner, the
resolver). Each trial uses its own database session and rolls it back; a tool that
commits its own audit rows does so exactly as it would in chat.

Tasks, labels, tape and results hold customer data, so the results file must sit outside
the repository.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path

from app.services.benchmarks.resolve import report, tasks
from app.services.benchmarks.resolve.tape import Tape

_HERE = Path(__file__).resolve()
# The git checkout when there is one; in the container (no .git) the backend root, /app.
REPOSITORY = next((p for p in _HERE.parents if (p / ".git").exists()), _HERE.parents[4])


def _outside_repository(path: str) -> Path:
    resolved = Path(path).resolve()
    if resolved == REPOSITORY or REPOSITORY in resolved.parents:
        raise SystemExit(f"{path}: write benchmark results outside the repository (they hold customer data)")
    return resolved


async def _run(args) -> int:
    from app.core.config import settings
    from app.core.database import async_session_factory, set_tenant_context
    from app.services.benchmarks.resolve.interpret import make_interpreter
    from app.services.benchmarks.resolve.ours import run_ours

    tenant_id, actor_id = uuid.UUID(args.tenant), uuid.UUID(args.actor)
    bench = tasks.load_tasks(args.tasks, args.labels, split=args.split, snapshots_dir=args.snapshots)[
        : args.limit or None
    ]
    tape = Tape(args.tape)

    async def agent(task, trial):
        async with async_session_factory() as db:
            try:
                await set_tenant_context(db, str(tenant_id))
                return await run_ours(
                    task,
                    trial,
                    db=db,
                    tenant_id=tenant_id,
                    actor_id=actor_id,
                    tape=tape,
                    mode=args.mode,
                    model=args.model,
                )
            finally:
                await db.rollback()

    interpret = make_interpreter(api_key=settings.ANTHROPIC_API_KEY, model=args.interpret_model)
    meta = {
        "agent": "ours",
        "model": args.model,
        "interpret_model": args.interpret_model,
        "mode": args.mode,
        "split": args.split,
        "tasks": len(bench),
    }
    summary = await report.run(bench, agent, trials=args.trials, out_path=args.out, meta=meta, interpret=interpret)
    print(json.dumps(summary, indent=2, default=str))
    return 0


async def _capture_all(tasks_path, out_dir, tenant, item_ids, *, session_factory=None, set_tenant=None) -> dict:
    """One snapshot per order in the task list (read-only); a refused shape is saved too."""
    from app.services.benchmarks.resolve import outcome

    if session_factory is None or set_tenant is None:
        from app.core.database import async_session_factory, set_tenant_context

        session_factory, set_tenant = session_factory or async_session_factory, set_tenant or set_tenant_context
    tenant_id = uuid.UUID(str(tenant))
    target = Path(out_dir)
    target.mkdir(parents=True, exist_ok=True)
    counts: dict = {"taken": 0, "refused": {}}
    for row in json.loads(Path(tasks_path).read_text()):
        async with session_factory() as db:
            await set_tenant(db, str(tenant_id))
            snap = await outcome.capture(db, tenant_id, row["case_id"], row["ref"], list(item_ids))
        (target / f"{row['ref']}.json").write_text(json.dumps(snap.to_json(), indent=1))
        if snap.refusal:
            counts["refused"][snap.refusal] = counts["refused"].get(snap.refusal, 0) + 1
        else:
            counts["taken"] += 1
    return counts


def _parser():
    parser = argparse.ArgumentParser(prog="resolve-bench", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    split = sub.add_parser("split", help="count held-in and held-out tasks (reads no labels)")
    split.add_argument("--tasks", required=True)
    snapshot = sub.add_parser("snapshot", help="read each order once, read-only, for the outcome grader")
    snapshot.add_argument("--tasks", required=True)
    snapshot.add_argument("--out-dir", required=True)
    snapshot.add_argument("--tenant", required=True)
    snapshot.add_argument("--items", required=True, help="comma-separated credit and tax item ids the profile allows")
    run = sub.add_parser("run", help="run today's agent on the tasks and grade it")
    run.add_argument("--tasks", required=True)
    graded_by = run.add_mutually_exclusive_group(required=True)
    graded_by.add_argument("--labels", help="grade by hand labels")
    graded_by.add_argument("--snapshots", help="grade by outcome against these snapshots (Aiden, 2026-10-10)")
    run.add_argument("--tape", required=True)
    run.add_argument("--out", required=True)
    run.add_argument("--tenant", required=True)
    run.add_argument("--model", required=True)
    run.add_argument("--actor", required=True, help="an active user of the tenant; native tools authorize it")
    run.add_argument("--interpret-model", default="claude-sonnet-5", help="reads replies that declare no resolution")
    run.add_argument("--mode", choices=("replay", "record"), default="replay")
    run.add_argument("--split", choices=("held_in", "held_out", "all"), default="held_in")
    run.add_argument("--trials", type=int, default=3)
    run.add_argument("--limit", type=int, default=0)
    return parser


def main(argv=None) -> int:
    args = _parser().parse_args(argv)

    if args.command == "split":
        refs = [row["ref"] for row in json.loads(Path(args.tasks).read_text())]
        held = tasks.held_out_refs(refs)
        print(json.dumps({"tasks": len(refs), "held_in": len(refs) - len(held), "held_out": len(held)}))
        return 0
    if args.command == "snapshot":
        _outside_repository(args.out_dir)
        items = [i.strip() for i in args.items.split(",") if i.strip()]
        print(json.dumps(asyncio.run(_capture_all(args.tasks, args.out_dir, args.tenant, items)), indent=2))
        return 0
    for path in (args.out, args.tape):
        _outside_repository(path)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())

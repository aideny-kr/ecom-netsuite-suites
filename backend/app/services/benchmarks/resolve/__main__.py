"""Resolve benchmark command line.

    python -m app.services.benchmarks.resolve split --tasks tasks.json
    python -m app.services.benchmarks.resolve run --tasks tasks.json --labels labels/ \\
        --tape tape.jsonl --out results.json --tenant <uuid> --model <model> [--mode record] [--limit 3]

`run` drives today's agent and needs the platform model key, so it runs in the staging
container. The first run on new tasks uses `--mode record`, which saves every read to the
tape. Scoring runs use the default `--mode replay`, which never reaches NetSuite. Each
trial uses its own database session and rolls it back; a tool that commits its own audit
rows does so exactly as it would in chat.

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
    from app.core.database import async_session_factory, set_tenant_context
    from app.services.benchmarks.resolve.ours import run_ours

    tenant_id = uuid.UUID(args.tenant)
    bench = tasks.load_tasks(args.tasks, args.labels, split=args.split)[: args.limit or None]
    tape = Tape(args.tape)

    async def agent(task, trial):
        async with async_session_factory() as db:
            try:
                await set_tenant_context(db, str(tenant_id))
                return await run_ours(
                    task, trial, db=db, tenant_id=tenant_id, tape=tape, mode=args.mode, model=args.model
                )
            finally:
                await db.rollback()

    meta = {"agent": "ours", "model": args.model, "mode": args.mode, "split": args.split, "tasks": len(bench)}
    summary = await report.run(bench, agent, trials=args.trials, out_path=args.out, meta=meta)
    print(json.dumps(summary, indent=2, default=str))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="resolve-bench", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    split = sub.add_parser("split", help="count held-in and held-out tasks (reads no labels)")
    split.add_argument("--tasks", required=True)
    run = sub.add_parser("run", help="run today's agent on the tasks and grade it")
    run.add_argument("--tasks", required=True)
    run.add_argument("--labels", required=True)
    run.add_argument("--tape", required=True)
    run.add_argument("--out", required=True)
    run.add_argument("--tenant", required=True)
    run.add_argument("--model", required=True)
    run.add_argument("--mode", choices=("replay", "record"), default="replay")
    run.add_argument("--split", choices=("held_in", "held_out", "all"), default="held_in")
    run.add_argument("--trials", type=int, default=3)
    run.add_argument("--limit", type=int, default=0)
    args = parser.parse_args(argv)

    if args.command == "split":
        refs = [row["ref"] for row in json.loads(Path(args.tasks).read_text())]
        held = tasks.held_out_refs(refs)
        print(json.dumps({"tasks": len(refs), "held_in": len(refs) - len(held), "held_out": len(held)}))
        return 0
    for path in (args.out, args.tape):
        _outside_repository(path)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())

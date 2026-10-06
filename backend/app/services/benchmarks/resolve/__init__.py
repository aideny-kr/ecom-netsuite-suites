"""Resolve benchmark: does the agent resolve real reconciliation cases the way a person would?

Spec 2026-10-01 (accounting resolver) §7, block B3. Unlike the vs-MCP benchmark, which
grades answers to questions, this grades the OUTCOME of resolving a case against a gold
label a person wrote: the diagnosis, the action, and the exact change in one approval card.

- `tasks`: cases plus gold labels (exported from the labelling page), split held-in /
  held-out by hash so the held-out quarter is never looked at while building.
- `tape`: every tool call goes through a recorded tape. Reads are recorded once (in
  the staging container) and replayed after, so runs repeat exactly and never touch
  NetSuite. Writes are captured, never run; anything else is refused.
- `graders`: code checks of the outcome (G1), safety (G3), brevity (G4) and cost (G5).
- `report`: trials, pass@1 and pass^k, results written to a file outside git.
- `ours`: today's agent (UnifiedAgent) driven headless through the tape.

Run: `python -m app.services.benchmarks.resolve --help`. Real cases, labels, tapes and
results hold customer data: keep them outside the repository.
"""

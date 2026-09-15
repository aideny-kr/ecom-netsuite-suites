"""Scheduled Jobs platform (Slice 2). Spec:
docs/superpowers/specs/2026-09-08-scheduled-jobs-and-inventory-aging-design.md Part B.

Package layout (per §B2-B4, built incrementally across this slice's tasks):
- ``registry.py`` (Task 1) — the v1 step-type allow-list. The ONLY place a step
  type is defined; every compiler schema and every run's step lookup derives
  from ``STEP_REGISTRY`` here.
- ``compiler.py`` (Task 2) — instruction -> plan, validated against the registry.
- The Beat sweep executor lives under ``app/workers/tasks/scheduled_jobs.py``
  (Task 4), not in this package — it is a Celery task module, matching where
  every other scheduled task in this repo lives.
"""

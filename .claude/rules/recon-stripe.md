---
description: Reconciliation + Stripe sync gotchas. Loads when editing recon/ingestion.
paths:
  - backend/app/services/reconciliation/**
  - backend/app/services/ingestion/**
  - backend/app/workers/tasks/stripe_*.py
  - backend/app/api/v1/reconciliation.py
---

# Recon + Stripe sync rules

1. **Stripe SDK v15** — `dict(payout)` fails (use `.to_dict()`). `account.get("field")` fails (use `getattr(account, "field", None)`).
2. **Stripe connector key in `connections` table** — encrypted per-tenant. `STRIPE_API_KEY` in config.py is billing-only. Per-connection key via `decrypt_credentials(connection.encrypted_credentials)["api_key"]`.
3. **Recon Stripe initial sync is slow** — 800+ payouts, 30+ min. Pipeline has 90s timeout with fallback. Pre-sync via Settings "Sync Now" or nightly Beat.
4. **Supabase 2-min statement timeout** — batch commits every 10 rows for upserts. Cursor must save `max(created)`, not `last` (Stripe returns newest first).
5. **Three-tier matching** — exact payout ID → fuzzy amount/date/narration → AI investigation for ~5% exceptions. No LLM in matching pipeline.

See `.claude/skills/netsuite-reconciliation/SKILL.md` for full pipeline + dashboard architecture.

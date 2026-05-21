---
description: Alembic migration conventions for this repo. Loads when editing migrations.
paths:
  - backend/alembic/**
---

# Migration rules

1. **Migrations run in CI, not container startup** — `entrypoint.sh` doesn't run `alembic upgrade head`. Apply via `.github/workflows/migrate.yml` or manually before deploy.
2. **Revision ID max 32 chars** — keep short, e.g. `075_chat_cache_tokens`.
3. **Local two-DB gotcha** — `.venv/bin/alembic` → Supabase (remote). Docker → `postgres:5432` (local). Add columns → run `docker exec ecom-netsuite-suites-backend-1 alembic upgrade head` after Supabase migration.
4. **Never apply orphan feature-branch migrations to staging Supabase** — they block subsequent deploys from main. Use Supabase branch DBs for feature work.

## Migration template

```python
"""NNN_description.py"""
from alembic import op
import sqlalchemy as sa

revision = "NNN"
down_revision = "previous"

def upgrade() -> None:
    op.add_column("table", sa.Column("field", sa.String(50), nullable=True))

def downgrade() -> None:
    op.drop_column("table", "field")
```

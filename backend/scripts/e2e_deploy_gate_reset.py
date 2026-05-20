"""Reset deploy-gate state for a single changeset before Playwright E2E tests.

Deletes any in-flight (unconsumed) tokens AND any queued/running
`deploy_sandbox` runs for the given changeset, then verifies the
changeset is still approved and has passing validate + jest_unit_test
runs (which the test data should have).

Exit codes:
  0 — state is clean and changeset is deploy-eligible
  1 — changeset not found, not approved, or gates failing
  2 — could not reach DB
"""

from __future__ import annotations

import asyncio
import os
import ssl
import sys
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

# Build our own engine instead of importing app.core.database.engine —
# the app engine inherits APP_DEBUG (echo=True in dev), which floods the
# Playwright test reporter with SQL traces. echo=False here keeps the
# reporter clean.
from app.core.config import settings

_ssl = ssl.create_default_context()
_ssl.check_hostname = False
_ssl.verify_mode = ssl.CERT_NONE

engine = create_async_engine(
    settings.DATABASE_URL_DIRECT or settings.DATABASE_URL,
    echo=False,
    connect_args={"ssl": _ssl},
)


async def reset(changeset_id: uuid.UUID) -> int:
    try:
        async with engine.connect() as conn:
            # Clear in-flight tokens.
            await conn.execute(
                text(
                    "DELETE FROM workspace_deploy_tokens "
                    "WHERE changeset_id = :cs AND consumed_at IS NULL"
                ),
                {"cs": str(changeset_id)},
            )
            # Drop any queued deploy_sandbox runs that would interfere with a fresh test.
            await conn.execute(
                text(
                    "DELETE FROM workspace_runs "
                    "WHERE changeset_id = :cs "
                    "AND run_type = 'deploy_sandbox' "
                    "AND status IN ('queued', 'running')"
                ),
                {"cs": str(changeset_id)},
            )
            await conn.commit()

            # Verify deploy-eligible.
            cs_row = await conn.execute(
                text(
                    "SELECT status FROM workspace_changesets "
                    "WHERE id = :cs LIMIT 1"
                ),
                {"cs": str(changeset_id)},
            )
            cs = cs_row.fetchone()
            if cs is None:
                print(f"changeset {changeset_id} not found", file=sys.stderr)
                return 1
            if cs[0] != "approved":
                print(f"changeset status is {cs[0]}, not approved", file=sys.stderr)
                return 1

            gates = await conn.execute(
                text(
                    "SELECT run_type, status FROM workspace_runs "
                    "WHERE changeset_id = :cs "
                    "AND run_type IN ('suitecloud_validate', 'jest_unit_test') "
                    "ORDER BY run_type, created_at DESC"
                ),
                {"cs": str(changeset_id)},
            )
            latest: dict[str, str] = {}
            for run_type, status in gates:
                latest.setdefault(run_type, status)
            missing = [
                rt
                for rt in ("suitecloud_validate", "jest_unit_test")
                if latest.get(rt) != "passed"
            ]
            if missing:
                print(
                    f"gates not passing for {changeset_id}: "
                    f"{ {rt: latest.get(rt, 'missing') for rt in missing} }",
                    file=sys.stderr,
                )
                return 1

            print(f"reset ok: changeset={changeset_id}, gates passing")
            return 0
    except Exception as exc:
        print(f"DB error: {exc}", file=sys.stderr)
        return 2
    finally:
        await engine.dispose()


def main() -> int:
    raw = os.environ.get("E2E_CHANGESET_ID") or (sys.argv[1] if len(sys.argv) > 1 else "")
    if not raw:
        print(
            "usage: E2E_CHANGESET_ID=<uuid> python scripts/e2e_deploy_gate_reset.py\n"
            "   or: python scripts/e2e_deploy_gate_reset.py <uuid>",
            file=sys.stderr,
        )
        return 2
    try:
        cs = uuid.UUID(raw)
    except ValueError:
        print(f"invalid uuid: {raw}", file=sys.stderr)
        return 2
    return asyncio.run(reset(cs))


if __name__ == "__main__":
    sys.exit(main())

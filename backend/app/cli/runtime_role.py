"""Private operator CLI for dedicated runtime provisioning (preview by default)."""

import argparse
import asyncio
import json
import os
import sys
import uuid
from pathlib import Path

import asyncpg
from sqlalchemy.engine import make_url

from app.services.runtime_security.provision import provision


async def run(args) -> dict:
    # Separate input name: never accidentally choose a runtime or staging URL.
    url = make_url(os.environ["RUNTIME_OPERATOR_DATABASE_URL"])
    path = Path(args.password_file)
    if path.stat().st_mode & 0o077:
        raise ValueError("Runtime password file must be private (mode 0600)")
    password = path.read_text().strip()
    conn = await asyncpg.connect(url.set(drivername="postgresql").render_as_string(hide_password=False))
    try:
        return await provision(
            conn,
            database=args.database,
            cluster=args.cluster,
            company=args.company,
            password=password,
            apply=args.apply,
        )
    finally:
        await conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True)
    parser.add_argument("--cluster", required=True)
    parser.add_argument("--company", type=uuid.UUID, required=True)
    parser.add_argument("--password-file", required=True)
    parser.add_argument("--apply", action="store_true")
    try:
        print(json.dumps(asyncio.run(run(parser.parse_args())), sort_keys=True))
    except Exception:
        # Neither driver error strings nor SQL/DSNs are safe to log here.
        print(
            "Runtime provisioning refused. Verify private inputs, target identity and operator inventory.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

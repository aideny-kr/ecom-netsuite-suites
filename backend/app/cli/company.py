"""Run with ``python -m app.cli.company bootstrap`` or ``... check``."""

import argparse
import asyncio
import getpass
import sys

from pydantic import ValidationError

from app.core.config import settings
from app.core.database import worker_async_session
from app.schemas.auth import RegisterRequest
from app.services.company_bootstrap import bootstrap_company, validate_company_database


async def run(command: str, request: RegisterRequest | None = None) -> None:
    if command == "check-runtime" and not settings.SINGLE_COMPANY:
        return
    if not settings.SINGLE_COMPANY:
        raise ValueError("This command requires SINGLE_COMPANY=true")
    async with worker_async_session() as db:
        if command in {"check", "check-runtime"}:
            await validate_company_database(db)
            print("Company database ready.")
        else:
            assert request is not None
            _, created = await bootstrap_company(db, request)
            print("Company and administrator created." if created else "Already configured; no changes made.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["bootstrap", "check", "check-runtime"])
    args = parser.parse_args()
    try:
        request = None
        if args.command == "bootstrap":
            if not sys.stdin.isatty():
                raise ValueError("Bootstrap requires an interactive terminal for the administrator password")
            name = input("Company name: ")
            slug = input("Company slug (lowercase letters, digits and hyphens): ")
            email = input("Administrator email: ")
            full_name = input("Administrator name: ")
            password = getpass.getpass("Administrator password (8+ characters, uppercase, digit, symbol): ")
            if password != getpass.getpass("Confirm password: "):
                raise ValueError("Passwords do not match")
            request = RegisterRequest(
                tenant_name=name, tenant_slug=slug, email=email, full_name=full_name, password=password
            )
        asyncio.run(run(args.command, request))
    except ValidationError as exc:
        # Pydantic's default string representation includes submitted values.
        fields = sorted({str(error["loc"][0]) for error in exc.errors()})
        print("Invalid fields: " + ", ".join(fields), file=sys.stderr)
        return 1
    except (ValueError, EOFError, KeyboardInterrupt) as exc:
        print(str(exc) or "Setup cancelled.", file=sys.stderr)
        return 1
    except Exception:
        # Connection exceptions can embed credentials. Operator-facing output
        # must not include the DSN or database parameters.
        print("Database check failed. Check connectivity and run migrations first.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

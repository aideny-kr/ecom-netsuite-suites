"""Operator-only fresh bootstrap, existing-company adoption, and runtime checks."""

import argparse
import asyncio
import getpass
import json
import sys
import uuid

from pydantic import ValidationError

from app.core.config import settings
from app.core.database import worker_async_session
from app.schemas.auth import RegisterRequest
from app.services.company_bootstrap import (
    adopt_existing_company,
    bootstrap_company,
    company_entitlement_changes,
    validate_company_database,
)


async def run(command: str, request: RegisterRequest | None = None, adoption: dict | None = None) -> None:
    if command == "check-runtime" and not settings.SINGLE_COMPANY:
        return
    if not settings.SINGLE_COMPANY:
        raise ValueError("This command requires SINGLE_COMPANY=true")
    async with worker_async_session() as db:
        if command in {"check", "check-runtime"}:
            await validate_company_database(db)
            print("Company database ready.")
        elif command == "adopt-existing":
            assert adoption is not None
            tenant, changed = await adopt_existing_company(db, **adoption)
            print("Verified destination cluster: " + adoption["expected_system_identifier"])
            print(
                "Connection selected from: "
                + ("DATABASE_URL_DIRECT" if settings.DATABASE_URL_DIRECT else "DATABASE_URL")
            )
            if not adoption["apply"]:
                print(
                    f"Verified isolated company. Current plan: {tenant.plan}. Target plan: self_hosted; expiry: none."
                )
                print(
                    "Commercial entitlement changes: "
                    + json.dumps(company_entitlement_changes(tenant.plan), sort_keys=True)
                )
                print("Preview only. Use --apply after reviewing the preserved-data rehearsal and release gates.")
            else:
                print(
                    "Company plan converted; existing data preserved."
                    if changed
                    else "Already configured; no changes made."
                )
        else:
            assert request is not None
            _, created = await bootstrap_company(db, request)
            print("Company and administrator created." if created else "Already configured; no changes made.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["bootstrap", "adopt-existing", "check", "check-runtime"])
    parser.add_argument("--expected-database", help="Exact destination database name (never a URL)")
    parser.add_argument("--expected-system-identifier", help="Destination PostgreSQL cluster system_identifier")
    parser.add_argument("--tenant-id", type=uuid.UUID, help="Existing company UUID to preserve")
    parser.add_argument("--tenant-slug", help="Existing company slug to preserve")
    parser.add_argument("--apply", action="store_true", help="Apply reviewed adoption; default is preview")
    args = parser.parse_args()
    try:
        request = None
        adoption = None
        if args.command == "adopt-existing":
            if not all((args.expected_database, args.expected_system_identifier, args.tenant_id, args.tenant_slug)):
                raise ValueError(
                    "Adoption requires --expected-database, --expected-system-identifier, --tenant-id and --tenant-slug"
                )
            adoption = dict(
                expected_database=args.expected_database,
                expected_system_identifier=args.expected_system_identifier,
                expected_tenant_id=args.tenant_id,
                expected_slug=args.tenant_slug,
                apply=args.apply,
            )
        elif any(
            (args.expected_database, args.expected_system_identifier, args.tenant_id, args.tenant_slug, args.apply)
        ):
            raise ValueError("Adoption options are only valid with adopt-existing")
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
        asyncio.run(run(args.command, request, adoption))
    except ValidationError as exc:
        # Pydantic's default string representation includes submitted values.
        fields = sorted({str(error["loc"][0]) for error in exc.errors()})
        print("Invalid fields: " + ", ".join(fields), file=sys.stderr)
        return 1
    except (ValueError, EOFError, KeyboardInterrupt) as exc:
        print(str(exc) or "Setup cancelled.", file=sys.stderr)
        return 1
    except Exception as exc:
        # Connection exceptions can embed credentials. Operator-facing output
        # must not include the DSN or database parameters.
        sqlstate = getattr(getattr(exc, "orig", None), "sqlstate", None)
        if sqlstate == "42501":
            print(
                "This command requires operator database authority; do not broaden the runtime role.", file=sys.stderr
            )
        elif sqlstate in {"55P03", "40P01"}:
            print(
                "Destination is busy; stop its services and verify the offline target before retrying.", file=sys.stderr
            )
        else:
            print("Database operation failed; verify the selected connection and schema privately.", file=sys.stderr)
        if args.command == "adopt-existing" and args.apply:
            print("The stored outcome may be uncertain; rerun the preview before any retry.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

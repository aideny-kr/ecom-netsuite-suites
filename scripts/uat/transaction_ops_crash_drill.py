"""Local seeded HTTP/worker lifecycle and an actual SIGKILL after provider save.

Run with the backend virtualenv, from the isolated project checkout. This refuses
remote databases. Provider reads use illustrative fixtures; the guarded write
crosses real loopback HTTP into a stub which records a save and withholds its
response. Only the child worker is killed. Recovery uses real database commits,
retains the original send reservation and independently reads the stub's outcome.
The script removes its exact temporary tenant and every server/process it creates.
This verifies process recovery; it is not a live NetSuite save or the T2 review.
"""

# ruff: noqa: E402
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

import httpx
from app.core.config import settings
from cryptography.fernet import Fernet
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

settings.APP_DEBUG = False
DATABASE = settings.DATABASE_URL_DIRECT or settings.DATABASE_URL
parsed = make_url(DATABASE)
if (
    parsed.host not in {"localhost", "127.0.0.1"}
    or parsed.port != 5432
    or parsed.database not in {"ecom_netsuite", "ecom_netsuite_test"}
):
    raise SystemExit("Crash drill requires a loopback database on port 5432 named ecom_netsuite or ecom_netsuite_test")
if sys.argv[1:] == ["--check-database-only"]:
    print("Local database guard passed")
    raise SystemExit(0)

from app.core.database import get_db, set_tenant_context
from app.core.encryption import encrypt_credentials
from app.main import create_app
from app.models.connection import Connection
from app.models.transaction_ops import TransactionOperation
from app.services.transaction_ops import (
    executor,
    netsuite_reader,
    netsuite_transport,
    recovery,
    source_reader,
)
from app.services.transaction_ops import state_service as state
from app.workers.tasks import transaction_ops as workers
from tests.conftest import (
    create_test_tenant,
    create_test_user,
    enable_feature_flag,
    make_auth_headers,
)
from tests.test_transaction_ops_create_planning import missing_case
from tests.test_transaction_ops_netsuite_dispatch import URL
from tests.test_transaction_ops_planner import planning_case
from tests.test_transaction_ops_state_db import seed_config

TOKEN = "illustrative-crash-drill-token"
TABLES = (
    "transaction_ops_operations",
    "transaction_ops_proposals",
    "transaction_ops_findings",
    "transaction_ops_runs",
    "transaction_ops_configs",
    "audit_events",
    "celigo_flow_steps",
    "celigo_flows",
    "celigo_integrations",
    "connections",
    "user_roles",
    "users",
    "tenant_feature_flags",
    "tenant_configs",
)


def write_journal(path, data):
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w") as stream:
        json.dump(data, stream)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


async def cleanup_journal(path):
    data = json.loads(path.read_text())
    assert data["database"] == parsed.database and data["host"] == parsed.host
    assert re.fullmatch(r"tx-crash-drill-[a-f0-9]{32}", data["slug"])
    tenant_id = UUID(data["tenant_id"])
    engine = create_async_engine(DATABASE, echo=False, connect_args={"timeout": 5, "command_timeout": 15})
    try:
        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        await cleanup(factory, tenant_id, data["slug"])
    finally:
        await engine.dispose()
    if data.get("directory"):
        directory = Path(data["directory"])
        assert not directory.is_symlink()
        assert directory.parent.resolve() == Path(tempfile.gettempdir()).resolve()
        assert directory.name.startswith("tx-crash-drill-")
        if directory.exists():
            shutil.rmtree(directory)
    path.unlink()
    path.with_suffix(path.suffix + ".tmp").unlink(missing_ok=True)
    return {"zero_residue": True}


def install_providers(stack, data, port, *, saved=None):
    """All provider traffic is intercepted; only the loopback stub is reachable."""

    async def forward(request):
        assert request.url.host == "6738075-sb1.restlets.api.netsuite.com"
        assert request.method in {"GET", "POST"}
        async with httpx.AsyncClient(trust_env=False, timeout=15, follow_redirects=False) as client:
            response = await client.request(
                request.method,
                f"http://127.0.0.1:{port}{request.url.raw_path.decode()}",
                content=request.content,
                headers={"Authorization": f"Bearer {TOKEN}"},
            )
            return httpx.Response(response.status_code, content=response.content)

    async def read_target(*args, **kwargs):
        evidence = deepcopy(data["after"] if saved and saved.is_set() else data["before"])
        evidence["observed_at"] = datetime.now(timezone.utc).isoformat()
        return evidence

    async def read_source(*args, **kwargs):
        evidence = deepcopy(data["source"])
        evidence["read_at"] = datetime.now(timezone.utc).isoformat()
        return evidence

    original_read = netsuite_transport.read_guard_snapshot
    original_dispatch = netsuite_transport.dispatch_netsuite_operation

    async def guard(*args, **kwargs):
        async with httpx.AsyncClient(transport=httpx.MockTransport(forward)) as client:
            return await original_read(*args, **kwargs, client=client)

    async def dispatch(*args, **kwargs):
        async with httpx.AsyncClient(transport=httpx.MockTransport(forward)) as client:
            return await original_dispatch(*args, **kwargs, client=client)

    def wrapped_read(original):
        async def read(*args, **kwargs):
            async with httpx.AsyncClient(transport=httpx.MockTransport(forward)) as client:
                return await original(*args, **kwargs, client=client)

        return read

    stack.enter_context(patch.object(netsuite_transport, "get_valid_token", AsyncMock(return_value=TOKEN)))
    stack.enter_context(patch.object(netsuite_transport, "read_guard_snapshot", guard))
    for module in (source_reader, executor, recovery):
        stack.enter_context(patch.object(module, "read_framework_order", read_source))
    for module in (netsuite_reader, executor, recovery):
        stack.enter_context(patch.object(module, "read_netsuite_order", read_target))
    for module in (executor, recovery):
        stack.enter_context(patch.object(module, "read_guard_snapshot", guard))
    for name in ("read_create_preview", "read_created_snapshot"):
        read = wrapped_read(getattr(netsuite_transport, name))
        for module in (netsuite_transport, executor, recovery):
            if hasattr(module, name):
                stack.enter_context(patch.object(module, name, read))
    stack.enter_context(patch.object(executor, "dispatch_netsuite_operation", dispatch))


def provider_server(data, saved, release, counts):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, body):
            encoded = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            try:
                self.wfile.write(encoded)
            except (BrokenPipeError, ConnectionResetError):
                pass  # The SIGKILL deliberately drops the original response.

        def do_GET(self):
            assert self.headers.get("Authorization") == f"Bearer {TOKEN}"
            counts["reads"] += 1
            if data["action"] == "sync_missing_order":
                assert saved.is_set() and "action=created_snapshot" in self.path
                self.respond(
                    {
                        "schema_version": 1,
                        "success": True,
                        "account_id": "6738075_SB1",
                        "creation": data["after_guard"],
                    }
                )
                return
            self.respond(
                {
                    "schema_version": 1,
                    "success": True,
                    "account_id": "6738075_SB1",
                    "actions_enabled": True,
                    "snapshot": data["after_guard"] if saved.is_set() else data["guard"],
                }
            )

        def do_POST(self):
            assert self.headers.get("Authorization") == f"Bearer {TOKEN}"
            size = int(self.headers.get("Content-Length", "0"))
            assert 0 < size <= 65536
            payload = json.loads(self.rfile.read(size))
            if payload["action"] == "preview_create":
                assert data["action"] == "sync_missing_order" and not saved.is_set()
                assert payload["input"] == data["create_input"]
                counts["reads"] += 1
                self.respond(
                    {
                        "schema_version": 1,
                        "success": True,
                        "account_id": "6738075_SB1",
                        "create_enabled": True,
                        "preview": data["preview"],
                    }
                )
                return
            expected_before = (
                {"missing": True, "order_reference": "R123456789"}
                if data["action"] == "sync_missing_order"
                else data["guard"]
            )
            assert payload["before"] == expected_before and payload["after"] == data["intent"]
            assert payload["work_key"] == data["work_key"] and payload["action"] == data["action"]
            counts["writes"] += 1
            saved.set()
            release.wait(timeout=30)
            self.respond(
                {
                    "schema_version": 1,
                    "success": True,
                    "status": "saved",
                    "record_id": "63",
                    "work_key": payload["work_key"],
                    "verified": False,
                }
            )

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


async def cleanup(factory, tenant_id, slug):
    if tenant_id is None:
        return
    async with factory() as db:
        actual = (
            await db.execute(text("SELECT slug FROM tenants WHERE id=:id"), {"id": tenant_id})
        ).scalar_one_or_none()
        if actual is None:
            return
        assert actual == slug and slug.startswith("tx-crash-drill-")
        await set_tenant_context(db, str(tenant_id))
        for table in TABLES:
            await db.execute(text(f'DELETE FROM "{table}" WHERE tenant_id=:id'), {"id": tenant_id})
        await db.execute(
            text("DELETE FROM tenants WHERE id=:id AND slug=:slug"),
            {"id": tenant_id, "slug": slug},
        )
        await db.commit()
        await set_tenant_context(db, str(tenant_id))
        for table in TABLES:
            assert (
                await db.execute(
                    text(f'SELECT count(*) FROM "{table}" WHERE tenant_id=:id'),
                    {"id": tenant_id},
                )
            ).scalar_one() == 0
        assert (
            await db.execute(text("SELECT count(*) FROM tenants WHERE id=:id"), {"id": tenant_id})
        ).scalar_one() == 0


async def parent(output, action):
    settings.ENCRYPTION_KEY = Fernet.generate_key().decode()
    engine = create_async_engine(DATABASE, echo=False, connect_args={"timeout": 5, "command_timeout": 15})
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    tenant_id = child = server = server_thread = None
    slug = "tx-crash-drill-" + uuid4().hex
    journal = Path(str(output) + ".state.json")
    if journal.exists():
        raise RuntimeError("An unfinished cleanup journal exists; run --cleanup-state after stopping its worker")
    journal_data = {
        "slug": slug,
        "database": parsed.database,
        "host": parsed.host,
        "phase": "seeding",
    }
    saved, release = threading.Event(), threading.Event()
    counts = {"reads": 0, "writes": 0}
    result = {
        "passed": False,
        "provider": "loopback stub",
        "real_process_kill": False,
        "action": action,
    }
    try:
        creating = action == "sync_missing_order"
        case = missing_case() if creating else planning_case()
        async with factory() as db:
            tenant = await create_test_tenant(db, name="Ephemeral transaction crash drill", slug=slug)
            tenant_id = tenant.id
            journal_data["tenant_id"] = str(tenant_id)
            write_journal(journal, journal_data)
            actor, _ = await create_test_user(db, tenant)
            config = await seed_config(
                db,
                tenant_id,
                actor,
                subsidiary_id="3",
                mapping_json=case.config.mapping_json,
            )
            for flag in ("celigo", "reconciliation"):
                await enable_feature_flag(db, tenant_id, flag)
            connection = (
                await db.execute(select(Connection).where(Connection.id == config.netsuite_connection_id))
            ).scalar_one()
            connection.encrypted_credentials = encrypt_credentials({"account_id": "6738075_SB1", "access_token": TOKEN})
            connection.metadata_json = {"transaction_ops_guard_url": URL}
            await db.commit()
            headers, config_id = make_auth_headers(actor), config.id
        case.source["celigo_step_id"] = str(config.source_step_id)
        after = planning_case(inventory=True, assessment=True).targets if creating else deepcopy(case.targets)
        after_guard = {} if creating else deepcopy(case.guard["snapshot"])
        version = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        after["orders"][0]["version"] = version
        after["orders"][0]["header"].update(
            total="100",
            subtotal="100",
            custbody_fw_solidus_order_total="100",
            lastModifiedDate=version,
        )
        after["orders"][0]["lines"][0].update(rate="100", amount="100")
        after_guard.update(
            total="100",
            subtotal="100",
            custbody_fw_solidus_order_total="100",
            version=version,
        )
        if not creating:
            after_guard["lines"][0].update(rate="100", amount="100")
        data = {
            "action": action,
            "source": case.source,
            "before": case.targets,
            "after": after,
            "guard": None if creating else case.guard["snapshot"],
            "after_guard": after_guard,
        }
        if creating:
            native = after["orders"][0]
            native["header"].update(
                orderStatus={"id": "A"},
                total="120",
                taxTotal="20",
                custbody_fw_solidus_order_total="120",
                custbody_fw_solidus_tax_amount="20",
            )
            native["lines"][0].update(quantity="2", rate="50", custcol_fw_vat_amount="20", tax1Amt="20")
            data.update(
                create_input=case.prepared.payload_json,
                preview=case.preview,
                after_guard={
                    "record_id": "63",
                    "version": version,
                    "work_key": None,
                    "tax_profile": {"mode": "line_tax_amount", "tax_code_id": "610"},
                    "inventory_mode": "line_location",
                    "record": case.preview["record"],
                },
            )
        server, server_thread = provider_server(data, saved, release, counts)
        port = server.server_address[1]
        app = create_app()

        async def database():
            async with factory() as db:
                yield db

        app.dependency_overrides[get_db] = database
        with (
            ExitStack() as stack,
            tempfile.TemporaryDirectory(prefix="tx-crash-drill-") as directory,
        ):
            journal_data.update(directory=str(Path(directory).resolve()), phase="investigating")
            write_journal(journal, journal_data)
            install_providers(stack, data, port, saved=saved)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://drill.local",
                headers=headers,
            ) as api:
                response = await api.post(
                    f"/api/v1/transaction-ops/configs/{config_id}/runs",
                    json={
                        "origin": "manual",
                        "evaluation_key": slug,
                        "order_references": ["R123456789"],
                    },
                )
                assert response.status_code == 202, response.text
                run_id = response.json()["id"]
                await asyncio.to_thread(workers.transaction_ops_run.run, str(tenant_id), run_id)
                response = await api.get("/api/v1/transaction-ops/proposals", params={"run_id": run_id})
                assert response.status_code == 200 and len(response.json()) == 1, response.text
                proposal = response.json()[0]
                assert proposal["status"] == "pending" and proposal["currency"] == "EUR"
                assert proposal["action"] == action
                proposal_id = proposal["id"]
                waiting = await asyncio.to_thread(workers.transaction_ops_execute.run, str(tenant_id), proposal_id)
                assert waiting["status"] == "pending" and counts["writes"] == 0
                response = await api.post(
                    f"/api/v1/transaction-ops/proposals/{proposal_id}/decision",
                    json={
                        "decision": "approve",
                        "evidence_fingerprint": proposal["evidence_fingerprint"],
                    },
                )
                assert response.status_code == 200 and response.json()["decided_by"] == str(actor.id), response.text
                data.update(
                    tenant_id=str(tenant_id),
                    proposal_id=proposal_id,
                    intent=proposal["after_json"],
                    work_key=proposal["work_key"],
                )
                if creating:
                    data["after_guard"]["work_key"] = proposal["work_key"]
                fixtures = Path(directory) / "fixtures.json"
                fixtures.write_text(json.dumps(data))
                with (Path(directory) / "worker.log").open("w+") as log:
                    child = subprocess.Popen(
                        [
                            sys.executable,
                            str(Path(__file__).resolve()),
                            "--worker",
                            str(fixtures),
                            "--port",
                            str(port),
                        ],
                        cwd=ROOT / "backend",
                        env={
                            **os.environ,
                            "ENCRYPTION_KEY": settings.ENCRYPTION_KEY,
                            "APP_DEBUG": "false",
                        },
                        stdout=log,
                        stderr=log,
                    )
                    journal_data.update(child_pid=child.pid, phase="executing")
                    write_journal(journal, journal_data)
                    assert await asyncio.to_thread(saved.wait, 20), "Worker never reached the provider save"
                    os.kill(child.pid, signal.SIGKILL)
                    assert await asyncio.to_thread(child.wait, 10) == -signal.SIGKILL
                    result["real_process_kill"] = True
                    async with factory() as db:
                        await set_tenant_context(db, str(tenant_id))
                        row = (
                            await db.execute(
                                select(TransactionOperation).where(TransactionOperation.tenant_id == tenant_id)
                            )
                        ).scalar_one()
                        assert row.status == "executing" and row.result_json["dispatch_reserved"] is True
                        operation_id, original_spend, original_deadline = (
                            row.id,
                            row.api_calls_used,
                            row.deadline_at,
                        )
                        outcome = await recovery.recover_operation(
                            db,
                            tenant_id,
                            row.id,
                            _clock=lambda: original_deadline + timedelta(seconds=1),
                        )
                        if outcome["status"] != "verified":
                            result["diagnostic"] = (
                                await db.execute(
                                    text(
                                        "SELECT report_json FROM transaction_ops_findings f "
                                        "JOIN transaction_ops_runs r ON r.id=f.run_id "
                                        "WHERE f.tenant_id=:tenant AND r.origin='recovery'"
                                    ),
                                    {"tenant": tenant_id},
                                )
                            ).scalar_one_or_none()
                        assert outcome["status"] == "verified", outcome
                        row = await state._one(db, tenant_id, TransactionOperation, operation_id)
                        assert row.api_calls_used == original_spend and row.deadline_at == original_deadline
                        assert (
                            row.result_json["dispatch_reserved"] is True
                            and row.result_json["verification"]["source_unchanged"] is True
                        )
                        if creating:
                            proof = row.result_json["verification"]
                            result.update(
                                native_state=proof["creation_policy"]["native_order_status"],
                                native_quantity=proof["report"]["targets"][0]["lines"][0]["quantity"],
                                source_quantity=proof["report"]["source"]["lines"][0]["quantity"],
                                private_source_unchanged=proof["private_source_unchanged"],
                            )
                    duplicate = await asyncio.to_thread(
                        workers.transaction_ops_execute.run, str(tenant_id), proposal_id
                    )
                    assert duplicate["status"] == "verified" and counts["writes"] == 1
                    response = await api.get(f"/api/v1/transaction-ops/proposals/{proposal_id}/operation")
                    assert response.status_code == 200 and response.json()["status"] == "verified", response.text
                    result.update(
                        passed=True,
                        http_human_approval=True,
                        original_api_calls=original_spend,
                        writes=counts["writes"],
                        outcome="verified",
                    )
    finally:
        release.set()
        if child and child.poll() is None:
            child.kill()
            child.wait(timeout=10)
        if server:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=5)
        try:
            await cleanup(factory, tenant_id, slug)
            result["zero_residue"] = True
            journal.unlink(missing_ok=True)
            journal.with_suffix(journal.suffix + ".tmp").unlink(missing_ok=True)
        except BaseException:
            result.update(passed=False, zero_residue=False)
            raise
        finally:
            await engine.dispose()
            Path(output).write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


async def supervised_parent(output, action):
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    loop.add_signal_handler(signal.SIGTERM, task.cancel)
    try:
        await parent(output, action)
    finally:
        loop.remove_signal_handler(signal.SIGTERM)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="/tmp/transaction-ops-crash-drill.json")
    parser.add_argument(
        "--action",
        choices=("correct_amounts", "sync_missing_order"),
        default="correct_amounts",
    )
    parser.add_argument("--worker", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--port", type=int, help=argparse.SUPPRESS)
    parser.add_argument(
        "--cleanup-state",
        type=Path,
        help="Clean the exact journal after its worker group has stopped",
    )
    args = parser.parse_args()
    if args.cleanup_state:
        print(json.dumps(asyncio.run(cleanup_journal(args.cleanup_state))))
    elif args.worker:
        fixture_data = json.loads(args.worker.read_text())
        with ExitStack() as providers:
            install_providers(providers, fixture_data, args.port)
            workers.transaction_ops_execute.run(fixture_data["tenant_id"], fixture_data["proposal_id"])
    else:
        asyncio.run(supervised_parent(args.output, args.action))

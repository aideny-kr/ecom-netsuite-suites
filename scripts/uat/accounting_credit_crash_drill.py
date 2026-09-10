"""Local SIGKILL drill for chat credit approval; no live NetSuite calls.

The actual approval CAS/audit and recovery scheduler use committed PostgreSQL
state. A loopback provider saves once then withholds its response. Native GL
verification is covered separately by test_sales_credit_verification; here the
provider verifier reads the exact saved payload to test process recovery only.
"""

# ruff: noqa: E402
import argparse
import asyncio
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
# Reuse the strict loopback database guard and exact ephemeral-tenant cleanup.
import transaction_ops_crash_drill as base

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.database import set_tenant_context
from app.models.audit import AuditEvent
from app.models.chat import ChatMessage, ChatSession
from app.schemas.transaction_runs import ConfigOut
from app.services.chat.orchestrator import run_chat_turn
from app.services.chat.write_confirmation_service import build_confirmation_payload
from app.services.transaction_ops import accounting_group, accounting_recovery
from tests.conftest import create_test_tenant, create_test_user, enable_feature_flag
from tests.test_accounting_approval_flow import inputs, kind_proposal
from tests.test_transaction_ops_state_db import seed_config

base.TABLES = ("chat_messages", "chat_sessions", *base.TABLES)


async def child(path, port):
    data = json.loads(Path(path).read_text())
    engine = create_async_engine(base.DATABASE)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant, actor, session_id, message_id = (
        UUID(data[k]) for k in ("tenant", "actor", "session", "message")
    )

    async def send(**kwargs):
        assert kwargs["human_approved"] is True
        assert kwargs["approval_context"]["confirmation_id"] == str(message_id)
        # Assert durable attribution exists on ANOTHER committed connection.
        async with factory() as audit_db:
            await set_tenant_context(audit_db, str(tenant))
            assert await audit_db.scalar(
                select(AuditEvent.id).where(
                    AuditEvent.tenant_id == tenant,
                    AuditEvent.resource_id == str(message_id),
                    AuditEvent.action == accounting_recovery.CLAIM_ACTION,
                    AuditEvent.actor_id == actor,
                )
            )
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                f"http://127.0.0.1:{port}/credit", json=kwargs["tool_input"]
            )
            return response.text

    try:
        async with factory() as db:
            await set_tenant_context(db, str(tenant))
            session = await db.get(ChatSession, session_id)
            with (
                patch.object(accounting_group, "engine", engine),
                patch.object(
                    accounting_group, "authorize_accounting_write", AsyncMock()
                ),
                patch(
                    "app.services.transaction_ops.tax_correction.validate_approved",
                    AsyncMock(),
                ),
                patch("app.services.chat.orchestrator.execute_tool_call", send),
            ):
                events = [
                    event
                    async for event in run_chat_turn(
                        db=db,
                        session=session,
                        user_message="Approve exact synthetic credit",
                        user_id=actor,
                        tenant_id=tenant,
                        write_confirm={
                            "action": "approve",
                            "confirmation_id": str(message_id),
                        },
                    )
                ]
                raise AssertionError(f"Expected kill after provider save, got {events}")
    finally:
        await engine.dispose()


async def run(output):
    engine = create_async_engine(base.DATABASE)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    slug = "tx-crash-drill-" + uuid4().hex
    tenant_id = None
    child_process = server = server_thread = None
    saved, release = threading.Event(), threading.Event()
    provider = {"writes": 0, "reads": 0, "payload": None}
    journal = Path(str(output) + ".state.json")
    if journal.exists():
        raise RuntimeError(
            "An unfinished cleanup journal exists; stop its worker and run --cleanup-state"
        )
    journal_data = {
        "database": base.parsed.database,
        "host": base.parsed.host,
        "slug": slug,
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            assert self.path == "/credit"
            provider["payload"] = json.loads(
                self.rfile.read(int(self.headers["Content-Length"]))
            )
            provider["writes"] += 1
            saved.set()
            release.wait(45)

        def do_GET(self):
            assert self.path == "/credit"
            provider["reads"] += 1
            body = json.dumps(provider["payload"]).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    try:
        async with factory() as db:
            tenant = await create_test_tenant(
                db, name="Ephemeral chat credit crash drill", slug=slug
            )
            tenant_id = tenant.id
            journal_data["tenant_id"] = str(tenant_id)
            base.write_journal(journal, journal_data)
            actor, _ = await create_test_user(db, tenant)
            for flag in ("celigo", "reconciliation"):
                await enable_feature_flag(db, tenant_id, flag)
            config = await seed_config(
                db, tenant_id, actor, netsuite_account_id="123", subsidiary_id="1"
            )
            p = kind_proposal("credit")
            snapshot = ConfigOut.model_validate(config).model_dump(mode="json")
            p.update(tenant_id=str(tenant_id), config_id=str(config.id))
            p["scope"] = {
                **p["scope"],
                **{
                    k: snapshot.get(k)
                    for k in (
                        "source_connection_id",
                        "source_step_id",
                        "netsuite_account_id",
                        "subsidiary_id",
                        "record_type",
                    )
                },
            }
            session = ChatSession(tenant_id=tenant_id, user_id=actor.id)
            db.add(session)
            await db.flush()
            name, params = inputs(p)
            card = build_confirmation_payload(
                mutation_type="create",
                record_type="creditmemo",
                tool_name=name,
                tool_input=params,
                session_id=str(session.id),
                current_record=p["before"],
            )
            card.accounting_review = p
            message = ChatMessage(
                tenant_id=tenant_id,
                session_id=session.id,
                role="assistant",
                content="Synthetic credit",
                structured_output=card.model_dump(mode="json"),
            )
            db.add(message)
            await db.commit()
            ids = {
                "tenant": str(tenant_id),
                "actor": str(actor.id),
                "session": str(session.id),
                "message": str(message.id),
            }
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        port = server.server_port
        with tempfile.TemporaryDirectory(prefix="credit-crash-") as temporary:
            data_path = Path(temporary) / "ids.json"
            data_path.write_text(json.dumps(ids))
            child_process = subprocess.Popen(
                [
                    sys.executable,
                    __file__,
                    "--child",
                    str(data_path),
                    "--port",
                    str(port),
                ],
                cwd=ROOT,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            journal_data["child_pid"] = child_process.pid
            base.write_journal(journal, journal_data)
            if not await asyncio.to_thread(saved.wait, 20):
                child_process.kill()
                _, errors = child_process.communicate(timeout=5)
                raise AssertionError(f"Provider save not reached: {errors.decode()}")
            os.kill(child_process.pid, signal.SIGKILL)
            await asyncio.to_thread(child_process.wait, 5)
            assert child_process.returncode == -signal.SIGKILL

            async def verify(db, tenant_id, proposal, receipt):
                assert receipt is None
                async with httpx.AsyncClient() as client:
                    response = await client.get(f"http://127.0.0.1:{port}/credit")
                assert response.json() == params
                return {
                    "status": "verified",
                    "credit_memo_id": "31",
                    "provider": "loopback fixture",
                }

            async with factory() as db:
                await set_tenant_context(db, str(tenant_id))
                durable = await db.get(ChatMessage, UUID(ids["message"]))
                assert durable.structured_output["status"] == "executing"
                with (
                    patch.object(accounting_group, "engine", engine),
                    patch(
                        "app.services.transaction_ops.sales_credit.verify_after", verify
                    ),
                ):
                    now = datetime.now(timezone.utc) + timedelta(minutes=6)
                    result = await accounting_recovery.recover(
                        db, tenant_id, durable.id, now=now
                    )
                    assert result["termination_reason"] == "done"
                    await accounting_recovery.recover(
                        db, tenant_id, durable.id, now=now + timedelta(minutes=10)
                    )
                assert durable.structured_output["status"] == "approved"
                assert (
                    durable.structured_output["accounting_recheck"]["status"]
                    == "queued"
                )
                event = await db.scalar(
                    select(AuditEvent).where(
                        AuditEvent.tenant_id == tenant_id,
                        AuditEvent.action == "accounting_recovery.completed",
                    )
                )
                assert (
                    event.payload["approved_by"] == ids["actor"]
                    and event.actor_type == "system"
                )
                assert provider["writes"] == 1 and provider["reads"] == 1
    finally:
        if child_process:
            if child_process.poll() is None:
                child_process.kill()
            child_process.communicate(timeout=5)
        release.set()
        if server:
            server.shutdown()
            server.server_close()
        if server_thread:
            server_thread.join(timeout=5)
        if tenant_id:
            await base.cleanup(factory, tenant_id, slug)
            journal.unlink(missing_ok=True)
        await engine.dispose()
    output.write_text(
        json.dumps(
            {
                "passed": True,
                "real_process_kill": True,
                "writes": provider["writes"],
                "recovery_reads": provider["reads"],
                "original_approver_preserved": True,
                "zero_residue": True,
                "native_netsuite_write": False,
            }
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--child")
    parser.add_argument("--port", type=int)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--cleanup-state", type=Path)
    args = parser.parse_args()
    if args.cleanup_state:
        print(json.dumps(asyncio.run(base.cleanup_journal(args.cleanup_state))))
    else:
        asyncio.run(child(args.child, args.port) if args.child else run(args.output))

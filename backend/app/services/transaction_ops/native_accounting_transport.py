"""Fixed-host, bounded native amendment transport using the selected connection.

The public interface is read-only. The internal dispatcher may use _request
only after committing its signed approval's single-send reservation.
"""

import asyncio
import json

import httpx
from sqlalchemy import select

from app.core.database import set_tenant_context
from app.core.encryption import decrypt_credentials
from app.models.connection import ACTIVE_CONNECTION_STATUSES, Connection
from app.services.netsuite_oauth_service import get_valid_token
from app.services.transaction_ops.netsuite_reader import _account, _invalid_constant, _object, _uuid

SCRIPT = "customscript_ecom_acct_amend"
DEPLOYMENT = "customdeploy_ecom_acct_amend"
MAX_BYTES = 200_000
TIMEOUT = httpx.Timeout(connect=5, read=40, write=10, pool=5)


class NativeTransportError(ValueError):
    pass


async def request(db, tenant_id, connection_id, account_id, action, payload, *, client=None):
    if action not in {"capabilities", "snapshot", "preview"}:
        raise NativeTransportError("native_read_action_required")
    return await _request(db, tenant_id, connection_id, account_id, action, payload, client=client)


async def _request(db, tenant_id, connection_id, account_id, action, payload, *, client=None):
    if action not in {"capabilities", "snapshot", "preview", "apply"}:
        raise NativeTransportError("unsupported_native_action")
    allowed = {
        "capabilities": set(),
        "snapshot": {"accountId", "recordType", "recordId", "subsidiaryId", "currencyId"},
        "preview": {"request"},
        "apply": {"request", "expected_before", "work_key", "approval_expires_at", "approval_audit_id"},
    }[action]
    if not isinstance(payload, dict) or (
        set(payload) != allowed and not (action == "capabilities" and set(payload) == {"subsidiaryId"})
    ):
        raise NativeTransportError("native_request_contract_invalid")
    tenant, identifier, account = _uuid(tenant_id, "tenant"), _uuid(connection_id, "connection"), _account(account_id)
    await set_tenant_context(db, str(tenant))
    connection = await db.scalar(
        select(Connection)
        .where(
            Connection.id == identifier,
            Connection.tenant_id == tenant,
            Connection.provider == "netsuite",
            Connection.status.in_(ACTIVE_CONNECTION_STATUSES),
        )
        .execution_options(populate_existing=True)
    )
    if not connection:
        raise NativeTransportError("native_connection_unavailable")
    try:
        credentials = decrypt_credentials(connection.encrypted_credentials)
        if _account(credentials.get("account_id")) != account:
            raise NativeTransportError("native_account_mismatch")
        token = await asyncio.wait_for(get_valid_token(db, connection), timeout=45)
    except NativeTransportError:
        raise
    except Exception:
        raise NativeTransportError("native_authentication_unavailable") from None
    finally:
        await set_tenant_context(db, str(tenant))
    if not token:
        raise NativeTransportError("native_authentication_unavailable")
    body = {**payload, "schema_version": 1, "action": action}
    if len(json.dumps(body, allow_nan=False).encode()) > MAX_BYTES:
        raise NativeTransportError("native_request_too_large")
    url = f"https://{account}.restlets.api.netsuite.com/app/site/hosting/restlet.nl"
    params = {"script": SCRIPT, "deploy": DEPLOYMENT}
    read = action in {"capabilities", "snapshot"}
    if read:
        params.update(body)

    async def send(http):
        try:
            async with http.stream(
                "GET" if read else "POST",
                url,
                params=params,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json=None if read else body,
                follow_redirects=False,
                timeout=TIMEOUT,
            ) as response:
                if response.status_code != 200:
                    raise NativeTransportError(f"native_http_{response.status_code}")
                chunks, size = [], 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise NativeTransportError("native_response_too_large")
                    chunks.append(chunk)
            value = json.loads(b"".join(chunks), object_pairs_hook=_object, parse_constant=_invalid_constant)
            if (
                not isinstance(value, dict)
                or type(value.get("schema_version")) is not int
                or value["schema_version"] != 1
            ):
                raise NativeTransportError("native_response_contract_invalid")
            return value
        except NativeTransportError:
            raise
        except (httpx.HTTPError, ValueError, TypeError, UnicodeError):
            raise NativeTransportError("native_response_unconfirmed") from None

    if client is not None:
        return await send(client)
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False) as owned:
        return await send(owned)

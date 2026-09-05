"""Read committed execution outcomes separately from human approval decisions."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import require_feature, require_permission
from app.models.transaction_ops import TransactionOperation
from app.models.user import User
from app.schemas.transaction_runs import OperationOut
from app.services.transaction_ops import state_service

router = APIRouter(
    prefix="/transaction-ops",
    tags=["transaction-ops"],
    dependencies=[Depends(require_feature("celigo")), Depends(require_feature("reconciliation"))],
)


@router.get("/proposals/{proposal_id}/operation", response_model=OperationOut | None)
async def get_operation(
    proposal_id: UUID,
    user: Annotated[User, Depends(require_permission("recon.run"))],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    try:
        proposal = await state_service.get_proposal(db, user.tenant_id, proposal_id)
    except state_service.StateError as exc:
        raise HTTPException(status_code=exc.http_status, detail={"code": exc.code}) from None
    # Refreshed approval evidence may share an existing economic operation.
    # The operation key, rather than just the newest proposal ID, is stable.
    return (
        await db.execute(
            select(TransactionOperation).where(
                TransactionOperation.tenant_id == user.tenant_id, TransactionOperation.work_key == proposal.work_key
            )
        )
    ).scalar_one_or_none()

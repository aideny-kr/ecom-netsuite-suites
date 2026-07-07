import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.models.report import Report
from app.models.user import User
from app.schemas.report import ReportResponse

router = APIRouter(prefix="/reports", tags=["reports"])


def _to_response(r: Report) -> ReportResponse:
    # ReportResponse.id is a str; ORM Report.id is a UUID and from_attributes
    # does NOT coerce UUID -> str (memory: response_model coercion for ORM rows),
    # so build the response with the id stringified explicitly.
    return ReportResponse(
        id=str(r.id),
        title=r.title,
        status=r.status,
        version=r.version,
        created_at=r.created_at,
        has_recipe=r.recipe_json is not None,
    )


@router.get("", response_model=list[ReportResponse])
async def list_reports(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    rows = (await db.execute(select(Report).order_by(Report.created_at.desc()))).scalars().all()
    return [_to_response(r) for r in rows]


async def _get_owned(db: AsyncSession, report_id: str) -> Report:
    try:
        rid = uuid.UUID(report_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Report not found")
    row = (await db.execute(select(Report).where(Report.id == rid))).scalar_one_or_none()
    if row is None:  # RLS-invisible cross-tenant rows land here too → 404 (no existence disclosure)
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Report not found")
    return row


@router.get("/{report_id}", response_model=ReportResponse)
async def get_report(
    report_id: str,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    return _to_response(await _get_owned(db, report_id))


@router.get("/{report_id}/view")
async def view_report(
    report_id: str,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    row = await _get_owned(db, report_id)
    return Response(content=row.rendered_html, media_type="text/html")

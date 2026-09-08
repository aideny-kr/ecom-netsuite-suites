"""Human-triggered calendar reviews use backend-owned policy and durable runs."""

from datetime import date, datetime, timezone
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, model_validator

from app.schemas.transaction_runs import RunCreate
from app.services.transaction_ops import state_service as state
from app.services.transaction_ops.normalization import TransactionMapping
from app.services.transaction_ops.periods import ReconciliationPolicy, review_window


class PeriodReview(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    evaluation_key: UUID
    period: Literal["yesterday", "last_week", "last_month", "custom"]
    start_date: date | None = None
    end_date: date | None = None

    @model_validator(mode="after")
    def dates_match_period(self):
        if self.period == "custom":
            if self.start_date is None or self.end_date is None:
                raise ValueError("Custom periods require start and end dates")
        elif self.start_date is not None or self.end_date is not None:
            raise ValueError("Preset periods determine their own dates")
        return self


def utc_now():
    return datetime.now(timezone.utc)


async def create_review(db, tenant_id, config_id, request, *, actor):
    await state._human(db, tenant_id, actor, "recon.run")
    config = await state.get_config(db, tenant_id, config_id)
    try:
        mapping = TransactionMapping.model_validate(config.mapping_json)
        policy = mapping.reconciliation_policy or ReconciliationPolicy()
        scope = review_window(
            request.period, utc_now(), policy.timezone_name, start_date=request.start_date, end_date=request.end_date
        )
    except ValueError:
        raise state.StateError("invalid_review_period", 422) from None
    return await state.create_run(
        db, tenant_id, config_id, RunCreate(evaluation_key=str(request.evaluation_key), **scope), actor=actor
    )

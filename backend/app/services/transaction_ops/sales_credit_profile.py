"""Explicit, account-scoped treatment learned from a reviewed commercial credit."""

from typing import Literal

from pydantic import Field, field_validator

from app.schemas.transaction_ops import EvidenceModel
from app.services.transaction_ops.netsuite_reader import _account


class SalesCreditProfile(EvidenceModel):
    schema_version: Literal[1]
    account_id: str
    subsidiary_id: str = Field(pattern=r"^[0-9]{1,30}$")
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    reference_credit_id: str = Field(pattern=r"^[0-9]{1,30}$")
    item_id: str = Field(pattern=r"^[0-9]{1,30}$")
    adjustment_account_id: str = Field(pattern=r"^[0-9]{1,30}$")
    ar_account_id: str = Field(pattern=r"^[0-9]{1,30}$")
    accounting_book_id: str = Field(pattern=r"^[0-9]{1,30}$")
    source_adjustment_label: str = Field(min_length=1, max_length=200)

    @field_validator("account_id")
    @classmethod
    def canonical_account(cls, value):
        return _account(value)

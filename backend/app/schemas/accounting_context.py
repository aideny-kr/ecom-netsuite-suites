"""Company context is evidence with a review lifecycle, never a posting permission."""

from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


class ContextScope(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    accounting_book_id: str = Field(pattern=r"^[0-9]{1,30}$")
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    posting_period_id: str = Field(pattern=r"^[0-9]{1,30}$")


class ContextSource(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    reference: str = Field(min_length=1, max_length=500)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    observed_at: AwareDatetime


class ContextDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    expected_version: int = Field(ge=0, strict=True)
    key: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    topic: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    kind: Literal["observed_configuration", "company_policy", "inference"]
    scope: ContextScope
    statement: str = Field(min_length=1, max_length=4000)
    owner: str = Field(min_length=1, max_length=200)
    sources: list[ContextSource] = Field(min_length=1, max_length=10)
    effective_from: AwareDatetime
    review_by: AwareDatetime

    @model_validator(mode="after")
    def dates(self):
        if self.review_by <= self.effective_from:
            raise ValueError("review_by must follow effective_from")
        return self


class ContextDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    expected_version: int = Field(ge=1, strict=True)
    content_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    decision: Literal["approve", "invalidate"]
    reason: str = Field(min_length=1, max_length=1000)
    # An approval records the human's authority/evidence; it is not an action approval.
    authority_reference: str = Field(min_length=1, max_length=500)

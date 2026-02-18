import re

from pydantic import BaseModel, EmailStr, Field, field_validator


class RegisterRequest(BaseModel):
    tenant_name: str = Field(min_length=2, max_length=255)
    tenant_slug: str = Field(min_length=2, max_length=255, pattern=r"^[a-z0-9-]+$")
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    full_name: str = Field(min_length=1, max_length=255)

    @field_validator("password")
    @classmethod
    def password_complexity(cls, v: str) -> str:
        msg = "Password must contain at least one uppercase letter, one digit, and one special character"
        if not re.search(r"[A-Z]", v):
            raise ValueError(msg)
        if not re.search(r"\d", v):
            raise ValueError(msg)
        if not re.search(r"[^a-zA-Z0-9]", v):
            raise ValueError(msg)
        return v


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str


class AuthResponse(BaseModel):
    access_token: str
    refresh_token: str = ""
    token_type: str = "bearer"


class LogoutRequest(BaseModel):
    refresh_token_jti: str | None = None


class SwitchTenantRequest(BaseModel):
    tenant_id: str


class TenantSummary(BaseModel):
    id: str
    name: str
    slug: str
    plan: str


class UserProfile(BaseModel):
    id: str
    tenant_id: str
    tenant_name: str = ""
    email: str
    full_name: str
    actor_type: str
    roles: list[str]
    onboarding_completed_at: str | None = None

    model_config = {"from_attributes": True}

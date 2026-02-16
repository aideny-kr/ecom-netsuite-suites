from pydantic import BaseModel, EmailStr, Field


class RegisterRequest(BaseModel):
    tenant_name: str = Field(min_length=2, max_length=255)
    tenant_slug: str = Field(min_length=2, max_length=255, pattern=r"^[a-z0-9-]+$")
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    full_name: str = Field(min_length=1, max_length=255)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str


class AuthResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


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

    model_config = {"from_attributes": True}

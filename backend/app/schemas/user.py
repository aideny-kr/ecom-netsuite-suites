from pydantic import BaseModel, EmailStr, Field


class UserCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    full_name: str = Field(min_length=1, max_length=255)


class UserResponse(BaseModel):
    id: str
    tenant_id: str
    email: str
    full_name: str
    actor_type: str
    is_active: bool
    roles: list[str] = []

    model_config = {"from_attributes": True}


class UserRoleAssign(BaseModel):
    role_names: list[str]

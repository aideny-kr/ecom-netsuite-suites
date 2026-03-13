from pydantic import BaseModel, Field


class BrandingResponse(BaseModel):
    brand_name: str | None = None
    brand_color_hsl: str | None = None
    brand_logo_url: str | None = None
    brand_favicon_url: str | None = None
    custom_domain: str | None = None
    domain_verified: bool = False

    model_config = {"from_attributes": True}


class BrandingUpdate(BaseModel):
    brand_name: str | None = Field(default=None, max_length=100)
    brand_color_hsl: str | None = Field(default=None, max_length=30)
    brand_logo_url: str | None = Field(default=None, max_length=2048)
    brand_favicon_url: str | None = Field(default=None, max_length=2048)
    custom_domain: str | None = Field(default=None, max_length=255)


class DomainVerifyRequest(BaseModel):
    domain: str = Field(min_length=3, max_length=255)


class DomainVerifyResponse(BaseModel):
    domain: str
    verified: bool
    dns_record: dict


class FeatureFlagsResponse(BaseModel):
    flags: dict[str, bool]


class FeatureFlagsUpdate(BaseModel):
    flags: dict[str, bool]


class ChatSettingsResponse(BaseModel):
    use_mcp_financial_reports: bool = True

    model_config = {"from_attributes": True}


class ChatSettingsUpdate(BaseModel):
    use_mcp_financial_reports: bool | None = None

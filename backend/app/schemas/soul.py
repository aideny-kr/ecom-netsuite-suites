from pydantic import BaseModel, Field


class SoulUpdateRequest(BaseModel):
    bot_tone: str | None = Field(default=None, description="Tone and manner of the chatbot")
    netsuite_quirks: str | None = Field(default=None, description="NetSuite specific business logic and quirks")


class SoulConfigResponse(SoulUpdateRequest):
    exists: bool = Field(description="Whether the soul.md file currently exists")

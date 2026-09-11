from datetime import datetime, timezone
from uuid import uuid4

from app.api.v1.chat import MessageResponse, _serialize_message
from app.models.chat import ChatMessage


def test_message_api_preserves_cache_usage_for_reload():
    message = ChatMessage(
        id=uuid4(),
        tenant_id=uuid4(),
        session_id=uuid4(),
        role="assistant",
        content="Result",
        created_at=datetime.now(timezone.utc),
        input_tokens=21,
        output_tokens=263,
        cache_creation_tokens=91539,
        cache_read_tokens=85853,
        provider_used="anthropic",
    )
    response = MessageResponse.model_validate(_serialize_message(message))
    assert response.cache_creation_tokens == 91539
    assert response.cache_read_tokens == 85853
    assert response.input_tokens == 21


def test_unknown_historical_cache_usage_stays_unknown():
    message = ChatMessage(
        id=uuid4(),
        tenant_id=uuid4(),
        session_id=uuid4(),
        role="assistant",
        content="Old result",
        created_at=datetime.now(timezone.utc),
        input_tokens=21,
        output_tokens=263,
    )
    response = MessageResponse.model_validate(_serialize_message(message))
    assert response.cache_creation_tokens is None
    assert response.cache_read_tokens is None

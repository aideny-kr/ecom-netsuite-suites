def test_chat_message_has_query_importance():
    from app.models.chat import ChatMessage

    msg = ChatMessage(
        tenant_id="00000000-0000-0000-0000-000000000000",
        session_id="00000000-0000-0000-0000-000000000000",
        role="assistant",
        content="test",
        query_importance=3,
    )
    assert msg.query_importance == 3


def test_chat_message_importance_defaults_none():
    from app.models.chat import ChatMessage

    msg = ChatMessage(
        tenant_id="00000000-0000-0000-0000-000000000000",
        session_id="00000000-0000-0000-0000-000000000000",
        role="assistant",
        content="test",
    )
    assert msg.query_importance is None


def test_chat_message_stores_importance_with_confidence():
    """ChatMessage should persist query_importance alongside confidence_score."""
    from app.models.chat import ChatMessage

    msg = ChatMessage(
        tenant_id="00000000-0000-0000-0000-000000000000",
        session_id="00000000-0000-0000-0000-000000000000",
        role="assistant",
        content="Total revenue: $1.2M",
        confidence_score=4.2,
        query_importance=3,
    )
    assert msg.query_importance == 3
    assert msg.confidence_score == 4.2

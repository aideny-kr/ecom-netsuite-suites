from app.services.chat.orchestrator import _coerce_assistant_content


def test_billing_failure_is_actionable_and_not_misrepresented_as_missing_evidence():
    text = _coerce_assistant_content(
        "",
        None,
        error="Error code: 400 - Your credit balance is too low to access the Anthropic API. private-request-id",
    )
    assert "API credit" in text
    assert "AI provider settings" in text
    assert "relevant information" not in text
    assert "private-request-id" not in text


def test_provider_failure_keeps_partial_answer_and_does_not_expose_raw_error():
    text = _coerce_assistant_content("Some evidence was retrieved.", None, error="secret provider credential")
    assert text.startswith("Some evidence was retrieved.")
    assert "could not complete" in text
    assert "secret" not in text


def test_provider_error_is_visible_alongside_an_existing_evidence_table():
    text = _coerce_assistant_content("", {"type": "data_table", "rows": [["123.45"]]}, error="provider failed")
    assert "could not complete" in text
    assert "123.45" not in text

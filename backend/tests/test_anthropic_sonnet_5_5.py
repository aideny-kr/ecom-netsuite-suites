"""Claude Sonnet 5.5 through the real SDK: what reaches the API, and what the chat sees.

Sonnet 5.5 (released 2026-09-28) rejects two things Sonnet 5 accepts, and moves a third:
thinking={"type": "disabled"} and a forced tool_choice (type tool/any) both return 400, and
the notes the model writes between tool calls arrive as thinking blocks instead of text.
These tests send through anthropic.AsyncAnthropic with a mock transport, so they check the
JSON body and the event stream the SDK actually handles, not the kwargs we build.
"""

import json

import anthropic
import httpx
import pytest

from app.schemas.tenant import TenantConfigUpdate
from app.services.chat.adapters import anthropic_adapter as aa
from app.services.chat.llm_adapter import VALID_MODELS
from app.services.chat.plan_mode.errors import PlanModeUnsupportedError

NEW, OLD = "claude-sonnet-5-5", "claude-sonnet-5"
TOOL = {"name": "review", "description": "Record the review.", "input_schema": {"type": "object"}}

_MESSAGE = {
    "id": "msg_1",
    "type": "message",
    "role": "assistant",
    "model": NEW,
    "content": [{"type": "text", "text": "ok"}],
    "stop_reason": "end_turn",
    "stop_sequence": None,
    "usage": {"input_tokens": 3, "output_tokens": 1},
}


def _adapter(handler):
    adapter = aa.AnthropicAdapter(api_key="test-key")
    adapter._client = anthropic.AsyncAnthropic(
        api_key="test-key",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        timeout=aa._CLIENT_TIMEOUT,  # as production; without it the SDK refuses large max_tokens
        max_retries=0,
    )
    return adapter


async def _sent(model, **kwargs):
    """The request body and headers the SDK sends for one create_message call."""
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        seen["headers"] = request.headers
        return httpx.Response(200, json=_MESSAGE)

    await _adapter(handler).create_message(
        model=model, max_tokens=100, system="static", messages=[{"role": "user", "content": "hi"}], **kwargs
    )
    return seen["body"], seen["headers"]


@pytest.mark.parametrize("level", [None, "none"])
async def test_thinking_off_uses_between_tools_on_sonnet_5_5_and_disabled_on_sonnet_5(level):
    body, _ = await _sent(NEW, thinking_level=level)
    assert body["thinking"] == {"type": "between_tools"}  # "disabled" is a 400 on 5.5
    assert "effort" not in body.get("output_config", {})  # between_tools refuses xhigh/max
    old, _ = await _sent(OLD, thinking_level=level)
    assert old["thinking"] == {"type": "disabled"}  # Sonnet 5 unchanged


@pytest.mark.parametrize(("level", "effort"), [("low", "low"), ("high", "high"), ("xhigh", "xhigh")])
async def test_thinking_on_asks_sonnet_5_5_for_its_progress_updates(level, effort):
    body, headers = await _sent(NEW, thinking_level=level)
    assert body["thinking"] == {"type": "adaptive", "display": "updates"}
    assert body["output_config"]["effort"] == effort
    assert "thinking-display-updates-2026-08-18" in headers.get("anthropic-beta", "")
    old, old_headers = await _sent(OLD, thinking_level=level)
    assert old["thinking"] == {"type": "adaptive"}
    assert "thinking-display-updates" not in old_headers.get("anthropic-beta", "")


@pytest.mark.parametrize("choice", [{"type": "tool", "name": "review"}, {"type": "any"}])
async def test_a_forced_tool_becomes_an_instruction_on_sonnet_5_5(choice):
    body, _ = await _sent(NEW, tools=[TOOL], tool_choice=choice, thinking_level="none")
    assert body["tool_choice"] == {"type": "auto"}  # type tool/any is a 400 on 5.5
    assert body["thinking"] == {"type": "between_tools"}
    instruction = body["system"][-1]["text"]
    assert "calling" in instruction and ("review" in instruction if choice["type"] == "tool" else True)
    old, _ = await _sent(OLD, tools=[TOOL], tool_choice=choice, thinking_level="none")
    assert old["tool_choice"] == choice
    assert old["thinking"] == {"type": "disabled"}


def test_callers_that_can_do_without_forcing_are_told_sonnet_5_5_cannot_force():
    adapter = aa.AnthropicAdapter(api_key="test-key")
    with pytest.raises(PlanModeUnsupportedError):
        adapter.force_tool_choice("route_request", model=NEW)
    assert adapter.force_tool_choice("route_request", model=OLD) == {"type": "tool", "name": "route_request"}


def _sse(events):
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


_TURN = [
    {
        "type": "message_start",
        "message": {**_MESSAGE, "content": [], "stop_reason": None, "usage": {"input_tokens": 5, "output_tokens": 1}},
    },
    {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
    {
        "type": "content_block_delta",
        "index": 0,
        "delta": {"type": "thinking_delta", "thinking": "Reading the credit memo now."},
    },
    {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "sig-1"}},
    {"type": "content_block_stop", "index": 0},
    {
        "type": "content_block_start",
        "index": 1,
        "content_block": {"type": "tool_use", "id": "toolu_1", "name": "review", "input": {}},
    },
    {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '{"a": 1}'}},
    {"type": "content_block_stop", "index": 1},
    {
        "type": "message_delta",
        "delta": {"stop_reason": "tool_use", "stop_sequence": None},
        "usage": {"output_tokens": 9},
    },
    {"type": "message_stop"},
]


async def _stream(model):
    def handler(request):
        return httpx.Response(200, content=_sse(_TURN), headers={"content-type": "text/event-stream"})

    return [
        event
        async for event in _adapter(handler).stream_message(
            model=model,
            max_tokens=100,
            system="static",
            messages=[{"role": "user", "content": "hi"}],
            tools=[TOOL],
            thinking_level="high",
        )
    ]


async def test_sonnet_5_5_progress_updates_reach_the_chat_and_are_kept_for_replay():
    events = await _stream(NEW)
    assert [e for e in events if e[0] == "text"] == [("text", "Reading the credit memo now.")]
    response = events[-1][1]
    assert events[-1][0] == "response"
    assert [t.name for t in response.tool_use_blocks] == ["review"]
    # Passed back unchanged on the next step, or the tool-use continuation fails.
    assert response.thinking_blocks == [
        {"type": "thinking", "thinking": "Reading the credit memo now.", "signature": "sig-1"}
    ]


async def test_sonnet_5_reasoning_stays_out_of_the_chat():
    events = await _stream(OLD)
    assert [e for e in events if e[0] == "text"] == []
    assert events[-1][1].thinking_blocks[0]["signature"] == "sig-1"


def test_sonnet_5_5_can_be_chosen_in_settings():
    assert NEW in VALID_MODELS["anthropic"]
    assert TenantConfigUpdate(ai_model=NEW).ai_model == NEW


def test_the_progress_updates_beta_joins_any_headers_already_set():
    # Gate wf_72de7a81 (minor): the beta header replaced extra_headers wholesale.
    kwargs = {"extra_headers": {"x-request-id": "r1", "anthropic-beta": "other-beta"}}
    aa._apply_thinking(kwargs, NEW, 100, "high", None)
    assert kwargs["extra_headers"]["x-request-id"] == "r1"
    assert kwargs["extra_headers"]["anthropic-beta"].split(",") == ["other-beta", aa._PROGRESS_UPDATES_BETA]


def _block(index, kind, text=None, tool=None):
    """Stream events for one content block: a progress note (thinking), answer text, or a tool call."""
    if kind == "thinking":
        start = {"type": "thinking", "thinking": "", "signature": ""}
        deltas = [{"type": "thinking_delta", "thinking": text}, {"type": "signature_delta", "signature": f"s{index}"}]
    elif kind == "text":
        start, deltas = {"type": "text", "text": ""}, [{"type": "text_delta", "text": text}]
    else:
        start = {"type": "tool_use", "id": f"toolu_{index}", "name": tool, "input": {}}
        deltas = [{"type": "input_json_delta", "partial_json": "{}"}]
    return [
        {"type": "content_block_start", "index": index, "content_block": start},
        *({"type": "content_block_delta", "index": index, "delta": d} for d in deltas),
        {"type": "content_block_stop", "index": index},
    ]


async def _stream_blocks(blocks, stop_reason):
    events = [
        _TURN[0],
        *(e for i, b in enumerate(blocks) for e in _block(i, *b)),
        {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": {"output_tokens": 9},
        },
        {"type": "message_stop"},
    ]

    def handler(request):
        return httpx.Response(200, content=_sse(events), headers={"content-type": "text/event-stream"})

    adapter = _adapter(handler)
    out = [
        e
        async for e in adapter.stream_message(
            model=NEW,
            max_tokens=100,
            system="s",
            messages=[{"role": "user", "content": "hi"}],
            tools=[TOOL],
            thinking_level="high",
        )
    ]
    return adapter, out


async def test_interleaved_notes_and_tool_calls_are_replayed_in_the_order_they_were_signed():
    # Gate wf_2c57b401 (major): replay moved every thinking block to the front. Sonnet 5.5 signs a
    # block over everything before it, so [note, call, note, call] must come back in that order.
    adapter, events = await _stream_blocks(
        [
            ("thinking", "Reading the invoice."),
            ("tool_use", None, "review"),
            ("thinking", "Now the credit."),
            ("tool_use", None, "review"),
        ],
        "tool_use",
    )
    assert "".join(e[1] for e in events if e[0] == "text") == "Reading the invoice.\n\nNow the credit."
    content = adapter.build_assistant_message(events[-1][1])["content"]
    assert [b["type"] for b in content] == ["thinking", "tool_use", "thinking", "tool_use"]
    assert [b.get("signature") for b in content if b["type"] == "thinking"] == ["s0", "s2"]


async def test_an_answer_that_arrives_only_as_a_note_is_still_the_answer():
    # Gate wf_2c57b401 (major): the chat showed it live, but the saved message was empty.
    _, events = await _stream_blocks([("thinking", "The credit memo already nets the tax to zero.")], "end_turn")
    assert events[-1][1].text_blocks == ["The credit memo already nets the tax to zero."]


def test_thinking_is_shown_only_when_the_request_asked_for_progress_updates():
    # Gate wf_2c57b401 (minor): decide from the request sent, not from the model name.
    assert aa._progress_updates_requested({"thinking": {"type": "between_tools"}})
    assert aa._progress_updates_requested({"thinking": {"type": "adaptive", "display": "updates"}})
    assert not aa._progress_updates_requested({"thinking": {"type": "adaptive"}})
    assert not aa._progress_updates_requested({"thinking": {"type": "adaptive", "display": "summarized"}})
    assert not aa._progress_updates_requested({})


_SHAPES = {
    "answer": ([("text", "Done.")], "end_turn"),
    "note then call": ([("thinking", "Checking."), ("tool_use", None, "review")], "tool_use"),
    "interleaved": (
        [("thinking", "A."), ("tool_use", None, "review"), ("thinking", "B."), ("tool_use", None, "review")],
        "tool_use",
    ),
    "note only": ([("thinking", "The credit memo already nets the tax to zero.")], "end_turn"),
}


@pytest.mark.parametrize("shape", list(_SHAPES))
async def test_a_turn_is_replayed_exactly_as_the_api_returned_it(shape):
    # Gate wf_f87a590a (majors 1,2,4,5): replay was rebuilt from two representations that
    # could disagree. It is now the returned content itself; what is shown is separate.
    blocks, stop = _SHAPES[shape]
    adapter, events = await _stream_blocks(blocks, stop)
    content = adapter.build_assistant_message(events[-1][1])["content"]
    expected = []
    for i, (kind, text, *tool) in enumerate(blocks):
        if kind == "thinking":
            expected.append({"type": "thinking", "thinking": text, "signature": f"s{i}"})
        elif kind == "text":
            expected.append({"type": "text", "text": text})
        else:
            expected.append({"type": "tool_use", "id": f"toolu_{i}", "name": tool[0], "input": {}})
    assert content == expected


async def test_a_forced_call_the_model_answered_in_prose_is_logged(capsys):
    # Gate wf_f87a590a (major 3, minor 8): on 5.5 a forced tool call is only asked for. The
    # callers already treat a missing call as a failed call; this makes the rate visible.
    await _sent(NEW, tools=[TOOL], tool_choice={"type": "tool", "name": "review"}, thinking_level="none")
    assert "llm.forced_tool_unanswered" in capsys.readouterr().out
    await _sent(OLD, tools=[TOOL], tool_choice={"type": "tool", "name": "review"}, thinking_level="none")
    assert "llm.forced_tool_unanswered" not in capsys.readouterr().out

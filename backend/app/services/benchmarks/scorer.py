"""Answer scoring for vs-MCP benchmark.

Two scorers:

1. `substring_score` — fast, deterministic, cheap. Checks whether the
   expected keywords appear in the answer. Good for keyword-level
   regression detection (did the agent even mention Norway?). Bad for
   correctness — an agent explaining "I couldn't find Norway's sales"
   scores 1.0 against expected_contains=["Norway"] because the word
   appears.

2. `llm_judge_score` — uses Claude Haiku as an evaluator. Given the
   question, the agent's answer, and an optional ground_truth_hint,
   returns a 0.0–1.0 score plus a rationale. Haiku is ~$0.001 per call
   so it's cheap enough to run on every case. The LLM judge:
     - Catches "I couldn't find the data" / "error occurred" / hallucinated
       zero results — these score 0.0 even if they mention the keywords.
     - Rewards answers that actually contain the requested numbers.
     - Accepts semantic variations (e.g. "CH" vs "Switzerland", "three
       hundred" vs "300").

The vs-MCP runner uses both: substring for the quick visual table,
llm_judge as the authoritative verdict that drives the final delta.

Deliberately does NOT use the main agent's confidence scorer — that's
the one we diagnosed as unreliable earlier today.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

try:
    from anthropic import AsyncAnthropic
except ImportError:  # pragma: no cover - anthropic is always installed in the backend
    AsyncAnthropic = None  # type: ignore[assignment,misc]


@dataclass
class ScoreResult:
    score: float  # 0.0 to 1.0
    rationale: str
    source: str  # "substring" | "llm_judge" | "llm_judge_fallback"


# ---------------------------------------------------------------------------
# Substring scorer — fast, deterministic
# ---------------------------------------------------------------------------


# Phrases that indicate the agent gave up or errored out — if present,
# the substring score is capped regardless of keyword hits.
_FAILURE_PHRASES = (
    "i couldn't find",
    "i could not find",
    "i was unable to",
    "no data found",
    "no results found",
    "no orders for",
    "returned no results",
    "returned 0 rows",
    "not available",
    "not accessible",
    "i don't have access",
    "i do not have access",
    "hit a technical wall",
    "unable to retrieve",
    "error occurred",
    "could not determine",
    "none of the requested",
    "no sales orders for",
    "have zero",
    "had zero",
)


def substring_score(
    *,
    answer_text: str,
    expected_contains: list[str],
) -> ScoreResult:
    """Fast substring-match score with failure-phrase penalty.

    - If expected_contains is empty, returns 1.0.
    - Score = fraction of expected terms present, case-insensitive.
    - Penalty: if the answer contains a failure phrase, score is capped
      at 0.5 (partial credit for keyword mentions, but not full credit).
    """
    if not expected_contains:
        return ScoreResult(score=1.0, rationale="no expected terms configured", source="substring")

    lower = (answer_text or "").lower()
    hits = sum(1 for kw in expected_contains if kw.lower() in lower)
    raw_score = hits / len(expected_contains)

    # Failure phrase penalty
    has_failure_phrase = any(phrase in lower for phrase in _FAILURE_PHRASES)
    final_score = raw_score
    rationale_parts = [f"{hits}/{len(expected_contains)} keywords matched"]

    if has_failure_phrase:
        final_score = min(raw_score, 0.5)
        rationale_parts.append("capped at 0.5 (failure phrase detected)")

    return ScoreResult(
        score=round(final_score, 3),
        rationale=", ".join(rationale_parts),
        source="substring",
    )


# ---------------------------------------------------------------------------
# LLM-judge scorer — uses Haiku as evaluator
# ---------------------------------------------------------------------------


_JUDGE_MODEL = "claude-haiku-4-5-20251001"

_JUDGE_SYSTEM_PROMPT = """You are an evaluator grading whether a NetSuite \
agent answered a user's question correctly.

You will be given:
  QUESTION: what the user asked
  ANSWER: what the agent responded with
  EXPECTED_CONTAINS (optional): specific terms the answer should mention

Output ONLY a single JSON object with these exact keys:
  {
    "score": <float 0.0 to 1.0>,
    "rationale": "<one sentence explanation>",
    "correct": <true or false>
  }

Scoring rubric:
  1.0 = Directly answers the question with specific numbers/data. All
        expected terms present. No hedging about missing data.
  0.8 = Answers correctly but missing some detail OR has minor phrasing issues.
  0.6 = Partially correct — answers some parts but not all, or has
        inaccuracies that don't break the main answer.
  0.4 = Mostly wrong but mentions relevant concepts. Agent may be hedging
        heavily or only providing metadata about the data.
  0.2 = Failed to answer — agent said "I couldn't find" / "no data" /
        "error occurred" for a question that DOES have a real answer,
        OR the agent is explaining why it failed instead of providing data.
  0.0 = No attempt / pure error / agent explicitly hallucinates that
        zero data exists when the question expects numbers.

IMPORTANT:
  - If the agent says the data doesn't exist but the question expects
    real numbers, score 0.0-0.2.
  - If the agent mentions the expected keywords but only in the context
    of explaining a failure ("I tried to find Norway's data but failed"),
    score 0.2 maximum.
  - If the agent gives real numeric answers for the question, score 0.8+.
  - Be strict. An agent that doesn't give an actual answer should NOT
    score above 0.4 just because it mentioned the right keywords.

Output nothing but the JSON object. No markdown fences. No prose."""


def _build_judge_user_message(
    *,
    question: str,
    answer: str,
    expected_contains: list[str],
) -> str:
    expected_block = ""
    if expected_contains:
        expected_block = f"\nEXPECTED_CONTAINS: {json.dumps(expected_contains)}"
    return f"QUESTION: {question}\n\nANSWER: {answer}{expected_block}"


async def llm_judge_score(
    *,
    question: str,
    answer_text: str,
    expected_contains: list[str],
    api_key: str | None = None,
) -> ScoreResult:
    """Score an answer using Claude Haiku as an LLM judge.

    Falls back to a simple substring score if the API call fails.
    Cheap: Haiku is ~$0.001 per call on these inputs.
    """
    if not answer_text:
        return ScoreResult(score=0.0, rationale="empty answer", source="llm_judge")

    if AsyncAnthropic is None:
        fallback = substring_score(
            answer_text=answer_text,
            expected_contains=expected_contains,
        )
        return ScoreResult(
            score=fallback.score,
            rationale=f"anthropic SDK not installed; fallback to substring: {fallback.rationale}",
            source="llm_judge_fallback",
        )

    try:
        from app.core.config import settings

        client = AsyncAnthropic(api_key=api_key or settings.ANTHROPIC_API_KEY)
    except Exception as exc:
        fallback = substring_score(
            answer_text=answer_text,
            expected_contains=expected_contains,
        )
        return ScoreResult(
            score=fallback.score,
            rationale=f"llm_judge unavailable ({exc}); fallback to substring: {fallback.rationale}",
            source="llm_judge_fallback",
        )

    user_message = _build_judge_user_message(
        question=question,
        answer=answer_text[:4000],  # cap to keep judge cheap
        expected_contains=expected_contains,
    )

    try:
        response = await client.messages.create(
            model=_JUDGE_MODEL,
            max_tokens=512,
            system=_JUDGE_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )
    except Exception as exc:
        fallback = substring_score(
            answer_text=answer_text,
            expected_contains=expected_contains,
        )
        return ScoreResult(
            score=fallback.score,
            rationale=f"llm_judge API error ({exc}); fallback to substring",
            source="llm_judge_fallback",
        )

    # Extract text from response
    text_blocks = []
    for block in getattr(response, "content", []) or []:
        block_type = getattr(block, "type", None)
        if block_type == "text":
            text_blocks.append(getattr(block, "text", ""))
    raw = "\n".join(text_blocks).strip()

    # Parse JSON — tolerate stray whitespace, trailing text, markdown fences
    parsed = _parse_judge_json(raw)
    if parsed is None:
        fallback = substring_score(
            answer_text=answer_text,
            expected_contains=expected_contains,
        )
        return ScoreResult(
            score=fallback.score,
            rationale=f"llm_judge returned unparseable JSON: {raw[:100]}; fallback to substring",
            source="llm_judge_fallback",
        )

    score = parsed.get("score")
    rationale = parsed.get("rationale") or "(no rationale)"
    try:
        score = float(score)
    except (TypeError, ValueError):
        fallback = substring_score(
            answer_text=answer_text,
            expected_contains=expected_contains,
        )
        return ScoreResult(
            score=fallback.score,
            rationale=f"llm_judge returned non-numeric score {score!r}; fallback",
            source="llm_judge_fallback",
        )
    score = max(0.0, min(1.0, score))

    return ScoreResult(score=round(score, 3), rationale=str(rationale)[:300], source="llm_judge")


# ---------------------------------------------------------------------------
# Value-absent scorer — anti-hallucination invariant for metric cases
# ---------------------------------------------------------------------------


def assert_computed_value_absent(
    answer_text: str,
    computed_values: list[str],
) -> bool:
    """Return True if none of the computed_values appear in answer_text.

    This is the primary anti-hallucination invariant for metric benchmark
    cases: the COMPUTED VALUE (e.g. a net margin percentage produced by
    metric_compute) must NOT appear verbatim in the model's text answer.
    Numeric values belong exclusively in the data_table SSE event — the
    LLM's prose must NEVER re-state them (that would be the model reading
    the number back from its context window, bypassing the interception).

    Args:
        answer_text: The model's final text answer.
        computed_values: Numeric or string values extracted from the
            data_table / tool result (e.g. ["12.5", "12.5%"]).

    Returns:
        True  — no value leaked into the answer (invariant holds).
        False — at least one value appears as a substring in the answer
                (invariant violated; scorer should cap the case score at 0.0).

    How it is wired into the benchmark runner:
        Cases that declare `computed_value_absent: true` in their YAML opt
        into this check. The runner extracts numeric strings from the
        data_table SSE payload returned by metric_compute, then calls this
        function. A False result hard-fails the case (score = 0.0) regardless
        of any keyword hits — it means the anti-hallucination SSE interception
        was bypassed.

    Usage::

        from app.services.benchmarks.scorer import assert_computed_value_absent

        ok = assert_computed_value_absent(
            answer_text=agent_result.answer_text,
            computed_values=["12.5", "12.5%"],
        )
        if not ok:
            # Metric value leaked into model answer — hard fail
            case_score = 0.0
    """
    if not computed_values or not answer_text:
        return True
    lower_answer = answer_text.lower()
    for value in computed_values:
        if value.lower() in lower_answer:
            return False
    return True


def _parse_judge_json(raw: str) -> dict | None:
    """Best-effort JSON parse of judge output."""
    if not raw:
        return None

    # Strip markdown code fences
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    if fenced:
        raw = fenced.group(1)
    else:
        # Find the first { and last } and slice
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            raw = raw[start : end + 1]

    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None

    if not isinstance(parsed, dict):
        return None
    return parsed

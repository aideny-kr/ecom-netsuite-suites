"""What KIND of failure is each Celigo error signature? An advisory label from Jev.

Signatures (errors.py) already answer "which errors share a root cause". Nothing
reads what the message SAYS: an engineer opens each group to learn whether it is
an expired token, a bad reference, or a mapping gap. This asks Jev
(services/typesafe/client.py) one Choice per signature, all signatures in one
request, and caches the answer per tenant and fingerprint — a signature's
meaning does not change between reads.

PII: ``sample_message`` is verbatim and PII-bearing by design and is never sent.
Only ``errors.normalize_message`` output leaves — emails, order refs, UUIDs,
timestamps and digit runs folded to placeholders. That normaliser is
deliberately narrow (it does not strip personal names), so this still goes
through the Jev client's tenant allowlist; it is reduced, not anonymous.

Advisory only. A category changes no status, resolves nothing, retries nothing;
Celigo's own ``retriable`` flag and the single guarded resolution path in
transaction_ops/celigo_actions.py remain the only authorities. Below
_MIN_CONFIDENCE the label is "unclear" rather than a guess.

JEV_CELIGO_TRIAGE_MODE: off · shadow (answers recorded, nothing shown) · live.
"""

from __future__ import annotations

from collections import Counter

from app.core.config import settings
from app.services.celigo.errors import normalize_message
from app.services.typesafe.client import JevUnavailableError, ask

CATEGORIES = {
    "authentication": "Credentials, tokens, permissions or access were refused or expired.",
    "rate_limit": "The target system throttled the request or a concurrency or usage limit was hit.",
    "bad_reference": "A record, item, customer or key referred to in the data does not exist in the target system.",
    "invalid_data": "A field value is missing, malformed, the wrong type, or fails a validation rule.",
    "duplicate": "The record already exists or a uniqueness rule was violated.",
    "mapping_or_config": "The flow's mapping, script, lookup or configuration is wrong rather than the record's data.",
    "transient": "A timeout, network failure or temporary outage of the source or target system.",
    "other": "None of the other options describes this failure.",
}
_MIN_CONFIDENCE = 0.6
_MAX_PER_REQUEST = 25
_MAX_MESSAGE_CHARS = 600

_cache: dict[tuple[str, str, str], dict] = {}


def clear_cache() -> None:
    _cache.clear()


def _label(answer: dict) -> dict:
    confident = (answer.get("confidence") or 0.0) >= _MIN_CONFIDENCE
    return {
        "category": answer["choice"] if confident else "unclear",
        "confidence": answer.get("confidence"),
        "advisory": True,
    }


async def triage_signatures(tenant_id, signatures: list[dict]) -> tuple[dict[str, dict], dict | None]:
    """Return ({fingerprint: label}, record). The label map is empty unless mode is live."""
    mode = settings.JEV_CELIGO_TRIAGE_MODE
    if mode not in {"shadow", "live"} or not signatures:
        return {}, None

    key = lambda s: (str(tenant_id), settings.JEV_MODEL, s["fingerprint"])  # noqa: E731
    labels = {s["fingerprint"]: _cache[key(s)] for s in signatures if key(s) in _cache}
    todo = [s for s in signatures if s["fingerprint"] not in labels][:_MAX_PER_REQUEST]
    record = {
        "mode": mode, "decided_by": "jev" if mode == "live" else "nobody", "signatures": len(signatures),
        "cached": len(labels), "jev_error": None,
    }  # fmt: skip

    if todo:
        state = {
            "errors": [
                {
                    "source": s.get("source") or "",
                    "code": s.get("code") or "",
                    "message": normalize_message(s.get("sample_message"))[:_MAX_MESSAGE_CHARS],
                }
                for s in todo
            ]
        }
        questions = {
            f"s{i}": {
                "type": "choice",
                "instructions": f"What kind of integration failure does `errors[{i}]` describe?",
                "criteria": CATEGORIES,
            }
            for i in range(len(todo))
        }
        try:
            result = await ask(tenant_id, state, questions)
        except JevUnavailableError as exc:
            record["jev_error"] = exc.reason
            return ({} if mode == "shadow" else labels), record
        record.update(jev_elapsed_ms=result.elapsed_ms, jev_input_tokens=result.input_tokens)
        for i, s in enumerate(todo):
            labels[s["fingerprint"]] = _cache[key(s)] = _label(result.answers[f"s{i}"])

    record["categories"] = dict(Counter(label["category"] for label in labels.values()))
    return (labels if mode == "live" else {}), record

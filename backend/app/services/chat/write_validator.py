"""Validate a normalized NetSuite write payload before a human ever sees it.

Field requirements come from runtime metadata, never from hardcoded field
names. ``metadata=None`` means requirements are unknown: the payload is marked
``unvalidated`` and allowed through for human review, because blocking every
write during a metadata outage is worse than showing a full payload to a human.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, model_validator

from app.services.chat.record_metadata_service import RecordMetadata
from app.services.chat.write_payload import NormalizedPayload


class EditableSlot(BaseModel):
    name: str
    label: str
    type: str = "text"
    allowed: list[dict[str, Any]] | None = None


class ValidationResult(BaseModel):
    ok: bool
    unvalidated: bool = False
    missing_required: list[str] = []
    missing_line_required: list[str] = []
    invariant_errors: list[str] = []
    editable_slots: list[EditableSlot] = []

    @model_validator(mode="after")
    def _ok_must_agree_with_its_own_lists(self) -> "ValidationResult":
        """`ok` is the flag Task 6's repair loop and Task 9's card gate on.

        It summarises the three lists, so it must never disagree with them.
        Deriving it here means a future caller cannot construct a result that
        claims `ok=True` while carrying missing fields — the failure would be
        silent and would wave an invalid write through to a human as if it
        had been checked.
        """
        derived = not (self.missing_required or self.missing_line_required or self.invariant_errors)
        if self.ok != derived:
            raise ValueError(
                f"ValidationResult.ok={self.ok} disagrees with its contents "
                f"(missing_required={self.missing_required}, "
                f"missing_line_required={self.missing_line_required}, "
                f"invariant_errors={self.invariant_errors})"
            )
        return self

    def fingerprint(self) -> str:
        """Stable identity of *what is wrong*, for stall detection."""
        return "|".join(
            [
                ",".join(sorted(self.missing_required)),
                ",".join(sorted(self.missing_line_required)),
                ",".join(sorted(self.invariant_errors)),
            ]
        )

    def as_model_error(self) -> dict[str, Any]:
        """Structured error handed back to the model instead of a card."""
        return {
            "validation_failed": True,
            "missing_required_fields": self.missing_required,
            "missing_line_fields": self.missing_line_required,
            "invariant_errors": self.invariant_errors,
            "instruction": (
                "Do NOT retry with the same payload. Resolve these fields first "
                "(use ns_getRecordTypeMetadata / ns_getSubsidiaries), then call "
                "the write tool again with a complete payload."
            ),
        }


def _is_empty(value: Any) -> bool:
    """A required field is missing if it is absent OR its value is empty.

    Empty means ``None``, an empty string, or a whitespace-only string.
    Deliberately NOT bare falsiness: ``0``, ``0.0`` and ``False`` are
    legitimate NetSuite field values (an account internal id of 0, a zero
    amount, a false checkbox) and must pass through untouched. An empty
    list/dict is treated as a present, non-empty value here — this
    function only judges scalar required fields; `[]`/`{}` would need a
    type-aware rule (e.g. a required multi-select) that this validator
    does not model today, so leaving them alone is the deliberate choice
    rather than folding them into "empty" by accident.
    """
    if value is None:
        return True
    if isinstance(value, str) and value.strip() == "":
        return True
    return False


def validate_write(
    *,
    payload: NormalizedPayload,
    metadata: RecordMetadata | None,
    record_type: str,
    mutation_type: Literal["create", "update", "delete", "upsert"],
    invariant_errors: list[str] | None = None,
) -> ValidationResult:
    invariant_errors = list(invariant_errors or [])

    if metadata is None:
        return ValidationResult(
            ok=not invariant_errors,
            unvalidated=True,
            invariant_errors=invariant_errors,
        )

    missing: list[str] = []
    missing_lines: list[str] = []

    # Only creates must carry every required field. An update legitimately
    # sends a partial payload — demanding the full set would break renames.
    if mutation_type in ("create", "upsert"):
        missing = [n for n in metadata.required_field_names() if _is_empty(payload.fields.get(n))]
        for idx, line in enumerate(payload.lines):
            for name in metadata.required_line_field_names():
                if _is_empty(line.get(name)):
                    missing_lines.append(f"line[{idx}].{name}")

    slots: list[EditableSlot] = []
    for name in missing:
        spec = metadata.spec_for(name)
        slots.append(
            EditableSlot(
                name=name,
                label=spec.label if spec else name,
                type=spec.type if spec else "text",
                allowed=spec.options if spec else None,
            )
        )

    return ValidationResult(
        ok=not (missing or missing_lines or invariant_errors),
        unvalidated=False,
        missing_required=missing,
        missing_line_required=missing_lines,
        invariant_errors=invariant_errors,
        editable_slots=slots,
    )

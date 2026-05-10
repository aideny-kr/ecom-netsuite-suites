"""Best-effort parser for `suitecloud project:validate --server` stdout.

Oracle's CLI does not document a stable JSON diagnostic schema. This parser
walks the stdout looking for `<SEVERITY>: <file>:<line> [<code>] <message>`
lines. Anything that doesn't match is preserved in `raw_output`. If NO lines
match a known severity prefix and the input is non-empty, we synthesize a
single `parser_error` hit so the issue surfaces to the user.

Hit fingerprinting: `sha256(file + ":" + line + ":" + code + ":" + message)`.
Used by the orchestrator to dedup repeat auto-propose attempts.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Final

PARSER_VERSION: Final[str] = "1.0.0"

# Match: ERROR: src/foo.js:42 [CODE-001] message...
_LINE_RE = re.compile(
    r"^(?P<severity>ERROR|WARNING|INFO):\s+"
    r"(?P<file>[^\s:][^:]*):(?P<line>\d+)\s+"
    r"\[(?P<code>[A-Za-z0-9._\-]+)\]\s+"
    r"(?P<message>.*?)$"
)

# Recognised top-level prefixes from suitecloud's output. If any of these appear,
# the CLI ran and we understood the format — even if no diagnostic lines matched
# the structured hit regex (e.g. a clean run is just INFO/SUCCESS lines without
# bracketed codes). Used to suppress the parser_error fallback on clean runs.
_KNOWN_PREFIX_RE = re.compile(r"^(ERROR|WARNING|INFO|SUCCESS|FAILURE):", re.MULTILINE)


@dataclass(frozen=True)
class ParsedHit:
    severity: str  # error | warning | info | parser_error
    file_path: str | None
    line: int | None
    code: str | None
    message: str
    fingerprint: str


@dataclass
class ValidationParseResult:
    hits: list[ParsedHit] = field(default_factory=list)
    has_errors: bool = False
    has_warnings: bool = False
    raw_output: str = ""
    parser_version: str = PARSER_VERSION


def _fingerprint(file_path: str | None, line: int | None, code: str | None, message: str) -> str:
    payload = f"{file_path or ''}:{line or 0}:{code or ''}:{message}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def parse_suitecloud_validate_output(stdout: str) -> ValidationParseResult:
    """Parse `suitecloud project:validate --server` stdout into structured hits.

    Returns an empty hit list for clean runs. Synthesizes a single `parser_error`
    hit when the input is non-empty but no lines match the expected format.
    """
    result = ValidationParseResult(raw_output=stdout)
    if not stdout.strip():
        return result

    for line in stdout.splitlines():
        match = _LINE_RE.match(line)
        if not match:
            continue
        severity_word = match.group("severity").lower()
        if severity_word not in ("error", "warning", "info"):
            continue
        line_no = int(match.group("line"))
        file_path = match.group("file")
        code = match.group("code")
        message = match.group("message").strip()
        result.hits.append(
            ParsedHit(
                severity=severity_word,
                file_path=file_path,
                line=line_no,
                code=code,
                message=message,
                fingerprint=_fingerprint(file_path, line_no, code, message),
            )
        )
        if severity_word == "error":
            result.has_errors = True
        elif severity_word == "warning":
            result.has_warnings = True

    # Only synthesize a parser_error if NONE of the recognised CLI prefixes
    # appear in the output. A clean run produces INFO + SUCCESS lines (no
    # bracketed codes, so they don't match _LINE_RE) but is still a valid,
    # understood format — not a parser failure.
    if not result.hits and not _KNOWN_PREFIX_RE.search(stdout):
        result.hits.append(
            ParsedHit(
                severity="parser_error",
                file_path=None,
                line=None,
                code=None,
                message="suitecloud validate output did not match expected format; raw stdout preserved.",
                fingerprint=_fingerprint(None, None, "PARSER_ERROR", stdout[:256]),
            )
        )
    return result

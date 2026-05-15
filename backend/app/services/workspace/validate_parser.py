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
# Bump major when fingerprint inputs change (would invalidate existing
# auto_validate dedup state). Bump minor when severity/code support
# expands (new hit types). Bump patch for non-fingerprint-affecting fixes.

# Match: ERROR: src/foo.js:42 [CODE-001] message...
_LINE_RE = re.compile(
    r"^(?P<severity>ERROR|WARNING|INFO):\s+"
    r"(?P<file>[^\s:][^:]*):(?P<line>\d+)\s+"
    r"\[(?P<code>[A-Za-z0-9._\-]+)\]\s+"
    r"(?P<message>.*?)$"
)

# Terminal status lines emitted by suitecloud at the end of every run. Their
# presence means the CLI ran to completion — even if no structured diagnostic
# lines matched _LINE_RE (a clean run is just INFO + SUCCESS lines, no bracketed
# codes). Their ABSENCE is the strongest signal that stdout was truncated or
# garbled, so we use it as the gate for synthesizing a parser_error fallback.
# Using only SUCCESS|FAILURE here (not ERROR/WARNING/INFO) is deliberate:
# prefix-headed garbage like "ERROR: this goes off the rails\nblah blah" must
# still trigger the fallback so the runner reports the issue instead of
# silently logging a clean result.
_TERMINAL_STATUS_RE = re.compile(r"^(SUCCESS|FAILURE):", re.MULTILINE)


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

    # Only suppress the parser_error fallback when stdout contains a terminal
    # status line (SUCCESS:/FAILURE:). Their absence means the CLI either
    # didn't finish or emitted garbage we couldn't parse — surface it.
    if not result.hits and not _TERMINAL_STATUS_RE.search(stdout):
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

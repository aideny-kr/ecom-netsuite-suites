"""Resolve analytical figures from completed Metabase results, never model arithmetic.

This is an answer boundary, not SQL validation or connector authorization. Tool
dispatch, tenant scope, output redaction and write approval remain upstream.
"""

from __future__ import annotations

import copy
import hashlib
import html
import json
import re
import secrets
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

_REFERENCE = re.compile(r"\{\{mb_ref:[^{}]+\}\}")
_NUMBER = re.compile(r"(?<![\w])[-+]?(?:\d+(?:[.,]\d+)*|\.\d+)(?:[eE][+-]?\d+)?")
_SPELLED_NUMBER = re.compile(
    r"\b(?:zero|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|"
    r"fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|"
    r"sixty|seventy|eighty|ninety|hundred|thousand|million|billion|dozen)\b",
    re.I,
)
_MATHEMATICAL_PROSE = re.compile(
    r"\b(?:sum(?:s|med|ming)?|overlap(?:s|ped|ping)?|reconcil(?:e|es|ed|ing)|"
    r"multiple|more than one|double[- ]count(?:ed|ing)?)\b",
    re.I,
)
UNVERIFIED = "I couldn't verify the requested figures from completed Metabase aggregates. Please retry the analysis."


def _decimal(value) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


def _cell(value) -> str:
    text = "—" if value is None else str(value)
    return html.escape(text).replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ")


def _table(columns: list[str], rows: list[list]) -> str:
    lines = ["| " + " | ".join(_cell(c) for c in columns) + " |", "| " + " | ".join("---" for _ in columns) + " |"]
    lines.extend("| " + " | ".join(_cell(v) for v in row) + " |" for row in rows)
    return "\n".join(lines)


def _without_presentation(value):
    if isinstance(value, dict):
        return {
            k: _without_presentation(v)
            for k, v in value.items()
            if k not in {"lib/uuid", "uuid", "name", "display-name"}
        }
    if isinstance(value, list):
        return [_without_presentation(v) for v in value]
    return value


def _measure_keys(connector: str, query: dict) -> tuple[list[str], list[str]]:
    stages = query.get("stages")
    if not isinstance(stages, list) or not stages or not isinstance(stages[-1], dict):
        return [], []
    aggregates = stages[-1].get("aggregation")
    if not isinstance(aggregates, list) or not aggregates:
        return [], []
    scope = copy.deepcopy(query)
    for key in ("aggregation", "breakout", "fields", "limit", "order-by"):
        scope["stages"][-1].pop(key, None)
    keys, operations = [], []
    for aggregate in aggregates:
        if not isinstance(aggregate, list) or not aggregate or not isinstance(aggregate[0], str):
            return [], []
        value = _without_presentation([connector, scope, aggregate])
        keys.append(hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest())
        operations.append(aggregate[0])
    return keys, operations


@dataclass
class EvidenceTable:
    columns: list[str]
    rows: list[list]
    grouped: bool
    complete: bool
    measures: dict[int, tuple[str, str]] = field(default_factory=dict)
    control_query: dict = field(default_factory=dict)


class MetabaseEvidence:
    """Per-turn, connector-bound references. Nothing is resolved from model values."""

    prompt = """\n<metabase_numeric_evidence>
For the final answer, copy the exact mb_ref placeholders supplied by executed
Metabase results. The application replaces them with verified values/tables.
Intermediate text is withheld while tools and controls run. The user has not
seen any draft: your FINAL response must include the complete requested answer,
including the headline and every requested breakdown, even if drafted earlier.
Do not type or spell out numerical findings, compute counts from detail rows,
sum groups yourself, or invent a missing zero. Numeric aggregate references are
available only after a completed server aggregation. Detail results provide a
table_reference for showing records, never an aggregate value reference.
For grouped aggregates, execute an ungrouped control with the SAME query,
filters, joins and aggregate expressions (remove breakout/order-by/limit).
When a result supplies control_query, copy that query object verbatim to the
same connector's query tool; do not reconstruct or simplify its joins/measure.
Distinct counts can overlap between groups; do not sum SKU counts into orders.
For reconciliation/overlap commentary, copy control_reference from control_checks.
Possible overlap is not evidence of actual overlap; use the returned comparison.
Do not surround a control reference with your own statements about sums,
overlap, reconciliation, or orders containing multiple matching SKUs. These
mathematical claims must appear only in the application-rendered control text.
Use returned value references for headline figures and table_reference for
tables. Chart JSON may use unquoted numeric references: they resolve before
chart parsing. Saved native SQL results can be shown via table_reference; if
scalar aggregate references are unavailable, reconstruct a verified MBQL
aggregate instead of calculating a headline from those rows.
Keep the final explanation qualitative. Omit unrequested numeric scope
restatements, numbered lists and SQL snippets. Say "the requested batch" rather
than changing its name or inserting bracketed placeholders. Preserve SKUs.
If a value or control is missing, query it; if verification fails, say so.
These reference requirements apply to the final answer, not tool arguments.
</metabase_numeric_evidence>"""

    def __init__(self, tool_names: set[str]):
        self.tool_names = tool_names
        self.nonce = secrets.token_hex(6)
        self.tables: list[EvidenceTable] = []
        self.bindings: dict[str, tuple[int, object]] = {}
        self.handles: dict[tuple[str, str], dict] = {}

    def _reference(self, table_id: int, suffix: str, value) -> str:
        reference = "{{mb_ref:" + self.nonce + f":{table_id}:{suffix}" + "}}"
        self.bindings[reference] = (table_id, value)
        return reference

    def observe(self, name: str, params: dict, result_str: str) -> str:
        if name not in self.tool_names:
            return result_str
        try:
            result = json.loads(result_str)
        except (ValueError, TypeError):
            return result_str
        if not isinstance(result, dict) or result.get("error") or result.get("isError"):
            return result_str
        connector, raw_name = name.rsplit("__", 1)
        query = params.get("query")
        if raw_name == "construct_query":
            handle = result.get("query_handle")
            if isinstance(query, dict) and isinstance(handle, str):
                self.handles[(connector, handle)] = copy.deepcopy(query)
            return result_str
        if raw_name not in {"query", "execute_query", "execute_question"}:
            return result_str
        if not isinstance(query, dict):
            query = self.handles.get((connector, str(params.get("query_handle"))), {})
        if not query and isinstance(result.get("json_query"), dict):
            query = result["json_query"]
        data = result.get("data")
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("cols"), list)
            or not isinstance(data.get("rows"), list)
            or any(not isinstance(column, dict) for column in data["cols"])
        ):
            return result_str
        columns = [str(c.get("display_name") or c.get("name") or "Value") for c in data["cols"]]
        rows = data["rows"]
        if not columns or any(not isinstance(row, list) or len(row) != len(columns) for row in rows):
            return result_str
        keys, operations = _measure_keys(connector, query)
        stages = query.get("stages")
        stage = stages[-1] if isinstance(stages, list) and stages else {}
        grouped = bool(stage.get("breakout")) if isinstance(stage, dict) else False
        complete = result.get("status") == "completed" and not any(
            obj.get(key)
            for obj in (result, data)
            for key in ("continuation_token", "truncated", "rows_truncated", "has_more")
        )
        limit = stage.get("limit") if isinstance(stage, dict) else None
        if grouped and isinstance(limit, int) and len(rows) >= limit:
            complete = False
        aggregate_columns = [i for i, col in enumerate(data["cols"]) if col.get("source") == "aggregation"]
        if not aggregate_columns and keys:
            # Native MBQL projects breakouts before aggregation columns.
            offset = len(stage.get("breakout") or [])
            if len(columns) == offset + len(keys):
                aggregate_columns = list(range(offset, len(columns)))
        measures = {}
        if len(aggregate_columns) == len(keys):
            measures = {index: (key, operation) for index, key, operation in zip(aggregate_columns, keys, operations)}
        table_id = len(self.tables)
        control_query = copy.deepcopy(query) if grouped and measures else {}
        if control_query:
            for key in ("breakout", "order-by", "limit"):
                control_query["stages"][-1].pop(key, None)
        table = EvidenceTable(columns, copy.deepcopy(rows), grouped, complete, measures, control_query)
        self.tables.append(table)
        rendered_table = _table(columns, rows[:100])
        if len(rows) > 100 or not complete:
            rendered_table += "\n\nPartial result; additional rows may not be shown."
        table_ref = self._reference(table_id, "table", rendered_table)
        preview = []
        for row_index, row in enumerate(rows[:100]):
            cells = []
            for column_index, value in enumerate(row):
                if complete and column_index in measures and _decimal(value) is not None:
                    cells.append(self._reference(table_id, f"r{row_index}c{column_index}", value))
                else:
                    cells.append(value)
            preview.append(cells)
        return json.dumps(
            {
                "status": result.get("status"),
                "columns": columns,
                "rows": preview,
                "table_reference": table_ref,
                "server_aggregate": bool(measures),
                "complete": complete,
                "continuation_token": result.get("continuation_token"),
                "evidence_rule": (
                    "Use references in the final answer. Detail cells and row counts are not population aggregates."
                ),
                "control_required": bool(grouped and measures),
                "control_query": control_query or None,
                "control_checks": [
                    {
                        "columns": candidate.columns,
                        "control_reference": self._reference(index, "control", None),
                        "result": self._control_error(candidate) or self._control_statement(candidate),
                    }
                    for index, candidate in enumerate(self.tables)
                    if candidate.grouped and candidate.measures
                ],
                "display_limited": len(rows) > 100,
            },
            default=str,
        )

    def _control_error(self, table: EvidenceTable) -> str | None:
        if not table.complete and table.measures:
            return (
                "The aggregate result is incomplete. "
                "Remove the grouping limit or finish pagination before reporting it."
            )
        if not table.grouped or not table.measures:
            return None
        for column, (key, operation) in table.measures.items():
            controls = [
                candidate.rows[0][index]
                for candidate in self.tables
                if candidate.complete and not candidate.grouped and len(candidate.rows) == 1
                for index, (candidate_key, _) in candidate.measures.items()
                if candidate_key == key
            ]
            if not controls:
                return (
                    "Execute the supplied control_query verbatim on the same connector; "
                    "its source, filters, joins and aggregate expression must match."
                )
            total = _decimal(controls[-1])
            values = [_decimal(row[column]) for row in table.rows]
            if total is None or any(value is None for value in values):
                continue
            if operation in {"count", "count-where", "sum", "sum-where"} and sum(values, Decimal(0)) != total:
                return "The grouped aggregate does not reconcile with its control. Requery both before answering."
            if operation == "distinct" and (
                any(value < 0 or value > total for value in values) or sum(values, Decimal(0)) < total
            ):
                return "Distinct group counts contradict their control. Requery both before answering."
        return None

    def _control_statement(self, table: EvidenceTable) -> str:
        statements = []
        for column, (key, operation) in table.measures.items():
            controls = [
                candidate.rows[0][index]
                for candidate in self.tables
                if candidate.complete and not candidate.grouped and len(candidate.rows) == 1
                for index, (candidate_key, _) in candidate.measures.items()
                if candidate_key == key
            ]
            total = _decimal(controls[-1]) if controls else None
            values = [_decimal(row[column]) for row in table.rows]
            if total is None or any(value is None for value in values):
                continue
            if operation == "distinct":
                statements.append(
                    "The grouped distinct counts sum to the overall distinct count for this result."
                    if sum(values, Decimal(0)) == total
                    else "The distinct groups overlap; adding their counts would overstate the overall population."
                )
            elif operation in {"count", "count-where", "sum", "sum-where"}:
                statements.append("The grouped totals reconcile with the overall control.")
        return " ".join(dict.fromkeys(statements)) or "A matching ungrouped control was returned."

    def feedback(self, text: str) -> str | None:
        references = _REFERENCE.findall(text)
        if any(reference not in self.bindings for reference in references):
            return (
                "An evidence reference is unknown or belongs to another turn. "
                "Use only references returned in this turn."
            )
        errors = []
        for table_id in sorted({self.bindings[reference][0] for reference in references}):
            error = self._control_error(self.tables[table_id])
            if error:
                errors.append(error)
        prose = _REFERENCE.sub("", text)
        literals = _NUMBER.findall(prose) + _SPELLED_NUMBER.findall(prose)
        if literals:
            errors.append(
                "Unverified numerical text was withheld. "
                f"Remove these literal numbers/number words: {json.dumps(literals[:20])}. "
                "Omit numeric scope labels and use value/table references for findings. "
                "Existing references do not need to be requeried just to fix wording."
            )
        if _MATHEMATICAL_PROSE.search(prose):
            errors.append(
                "Remove your own sum, overlap, reconciliation and multiplicity commentary. "
                "Use only the supplied control_reference for mathematical relationships, "
                "without additional membership claims or hypothetical caveats. "
                "Keep the requested figures and breakdown in the final answer. No new query is needed "
                "when the completed control is already available."
            )
        return "\n".join(dict.fromkeys(errors)) or None

    def resolve(self, text: str) -> str:
        def render(match):
            if match[0] not in self.bindings:
                # References inside reasoning metadata are stripped from the
                # user answer, and must not make rendering the answer fail.
                return match[0]
            table_id, value = self.bindings[match[0]]
            if match[0].endswith(":control}}"):
                return self._control_statement(self.tables[table_id])
            return str(value)

        return _REFERENCE.sub(render, text)

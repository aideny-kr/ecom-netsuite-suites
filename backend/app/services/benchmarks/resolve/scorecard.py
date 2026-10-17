"""The scorecard: our agent against native Claude + MCP on the same cases.

Aiden, 2026-10-10: "evaluate and metric and output the result". Two results files from
``run`` (``--agent ours`` and ``--agent reference``) become one comparison against the
definition of done (docs/superpowers/specs/2026-10-08-smart-resolver-definition-of-done.md):

- Correctness (G1) and the comparison with native (G2) use only the cases BOTH agents could
  be graded on, so neither is credited for a case the other was never scored on.
- Cases the outcome engine cannot grade yet are counted by reason, never dropped.
- Brevity (G4), tokens per resolved case (G5) and time are reported for both.
- ``comparable`` is false when either run had an incomplete environment.
"""

from __future__ import annotations

import statistics


def _cases(results: dict) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for row in results.get("trials") or []:
        out.setdefault(row["ref"], []).append(row)
    return out


def _graded(rows) -> bool:
    return any(row.get("graded", True) is not False for row in rows)


def _median(values):
    values = [v for v in values if v is not None]
    return statistics.median(values) if values else None


def _stats(cases: dict[str, list[dict]], refs: list[str]) -> dict:
    rows = [row for ref in refs for row in cases[ref]]
    rates = [sum(1 for r in cases[ref] if r.get("outcome_ok")) / len(cases[ref]) for ref in refs]
    return {
        "g1_pass_at_1": statistics.fmean(rates) if rates else None,
        "median_words": _median(r.get("words") for r in rows),
        "model_amounts": sum(len(r.get("model_amounts") or []) for r in rows),
        "median_tokens_resolved": _median(r.get("tokens") for r in rows if r.get("outcome_ok")),
        "median_wall_ms": _median(r.get("wall_ms") for r in rows),
    }


def _verdict(rows) -> str:
    if all(r.get("outcome_ok") for r in rows):
        return "pass"
    return next((r.get("outcome_reason") or "failed" for r in rows if not r.get("outcome_ok")), "failed")


def compare(ours: dict, native: dict) -> dict:
    mine, theirs = _cases(ours), _cases(native)
    refs = sorted(set(mine) & set(theirs))
    both = [ref for ref in refs if _graded(mine[ref]) and _graded(theirs[ref])]
    ungraded: dict[str, int] = {}
    for ref in refs:
        if ref not in both:
            rows = [r for r in mine[ref] + theirs[ref] if r.get("graded", True) is False]
            reason = (rows[0].get("outcome_reason") if rows else None) or "unknown"
            ungraded[reason] = ungraded.get(reason, 0) + 1
    ours_stats, native_stats = _stats(mine, both), _stats(theirs, both)

    def at_least(a, b):
        return None if a is None or b is None else a >= b

    def below(a, b):
        return None if a is None or b is None else a < b

    return {
        "comparable": bool((ours.get("summary") or {}).get("comparable"))
        and bool((native.get("summary") or {}).get("comparable")),
        "cases": {"graded_by_both": len(both), "ungraded": dict(sorted(ungraded.items()))},
        "ours": ours_stats,
        "native": native_stats,
        "goals": {
            "G2_not_worse_than_native": at_least(ours_stats["g1_pass_at_1"], native_stats["g1_pass_at_1"]),
            "faster_than_native": below(ours_stats["median_wall_ms"], native_stats["median_wall_ms"]),
            "fewer_tokens_than_native": below(
                ours_stats["median_tokens_resolved"], native_stats["median_tokens_resolved"]
            ),
            "G4_brief": None if ours_stats["median_words"] is None else ours_stats["median_words"] <= 60,
        },
        "per_case": [{"ref": ref, "ours": _verdict(mine[ref]), "native": _verdict(theirs[ref])} for ref in both],
    }


def _fmt(value, kind=""):
    if value is None:
        return "n/a"
    if kind == "pct":
        return f"{value:.0%}"
    if kind == "s":
        return f"{value / 1000:.0f}s"
    return f"{value:,.0f}" if isinstance(value, int | float) else str(value)


def render_markdown(card: dict) -> str:
    o, n, g = card["ours"], card["native"], card["goals"]
    lines = [
        f"Cases graded for both agents: {card['cases']['graded_by_both']}"
        + ("" if card["comparable"] else " (NOT comparable: an environment was incomplete)"),
        "",
        "| Metric | Our agent | Native Claude + MCP |",
        "|---|---|---|",
        f"| G1 right fix (pass@1) | {_fmt(o['g1_pass_at_1'], 'pct')} | {_fmt(n['g1_pass_at_1'], 'pct')} |",
        f"| Median reply words | {_fmt(o['median_words'])} | {_fmt(n['median_words'])} |",
        f"| Amounts written by the model | {_fmt(o['model_amounts'])} | {_fmt(n['model_amounts'])} |",
        f"| Median tokens per resolved case | {_fmt(o['median_tokens_resolved'])} | "
        f"{_fmt(n['median_tokens_resolved'])} |",
        f"| Median time per case | {_fmt(o['median_wall_ms'], 's')} | {_fmt(n['median_wall_ms'], 's')} |",
        "",
        f"G2 not worse than native: {g['G2_not_worse_than_native']} · faster: {g['faster_than_native']} · "
        f"fewer tokens: {g['fewer_tokens_than_native']} · G4 brief: {g['G4_brief']}",
    ]
    if card["cases"]["ungraded"]:
        lines += ["", "Not graded yet: " + ", ".join(f"{k} ({v})" for k, v in card["cases"]["ungraded"].items())]
    lines += ["", "| Order | Our agent | Native |", "|---|---|---|"]
    lines += [f"| {c['ref']} | {c['ours']} | {c['native']} |" for c in card["per_case"]]
    return "\n".join(lines)

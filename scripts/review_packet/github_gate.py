#!/usr/bin/env python3
"""Trusted-branch CI adapter. Fetches Git objects; never checks out/executes PR code."""

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

import packet

CONTEXT = "review-packet/review"


def api(route, data=None):
    args = ["gh", "api", route]
    if data is not None:
        args += ["--method", "POST", "--input", "-"]
    result = subprocess.run(
        args,
        input=json.dumps(data) if data is not None else None,
        text=True,
        capture_output=True,
        timeout=60,
    )
    if result.returncode:
        raise packet.Invalid(
            "GitHub API failed (response omitted): " + route.split("?")[0]
        )
    return json.loads(result.stdout)


def pages(route):
    rows = []
    for page in range(1, 21):
        batch = api(
            route + ("&" if "?" in route else "?") + f"per_page=100&page={page}"
        )
        packet.require(isinstance(batch, list), "invalid API list")
        rows.extend(batch)
        if len(batch) < 100:
            return rows
    raise packet.Invalid("API pagination exceeds bounded limit")


def targets(event_name, event, repository):
    if event_name == "pull_request_target":
        return [event["number"]]
    if event_name == "workflow_dispatch":
        value = str(event.get("inputs", {}).get("pr", ""))
        packet.require(
            value.isdecimal() and int(value) > 0, "dispatch requires PR number"
        )
        return [int(value)]
    packet.require(event_name == "push", "unsupported event")
    ref = event["ref"]
    packet.require(ref.startswith("refs/heads/"), "branch push required")
    return [
        p["number"]
        for p in pages(
            f"repos/{repository}/pulls?state=open&base={quote(ref[11:], safe='')}"
        )
    ]


def identity(pr):
    return (
        pr["base"]["sha"],
        pr["head"]["sha"],
        pr.get("body") or "",
        pr["state"],
        pr["draft"],
    )


def status(repository, head, state, description):
    run_url = (
        f"https://github.com/{repository}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
    )
    api(
        f"repos/{repository}/statuses/{head}",
        {
            "state": state,
            "context": CONTEXT,
            "description": description[:140],
            "target_url": run_url,
        },
    )


def evaluate(repository, number, out):
    packet.require(type(number) is int and number > 0, "invalid PR number")
    pr = api(f"repos/{repository}/pulls/{number}")
    if pr["state"] != "open":
        return True
    base, head, body, _, draft = identity(pr)
    packet.require(
        all(re.fullmatch("[0-9a-f]{40}", sha) for sha in [base, head]), "invalid PR SHA"
    )
    status(repository, head, "pending", "Preparing exact-revision review packet")
    verdict, description = "failure", "Review packet could not be verified"
    try:
        brief = packet.extract(body, "brief")
        # Only fetch objects from this repository's authenticated origin. PR ref supports forks.
        # No checkout, submodules, dependencies, hooks, or PR-owned Python imports.
        packet.git(Path.cwd(), "fetch", "--no-tags", "origin", base)
        packet.git(
            Path.cwd(), "fetch", "--no-tags", "origin", f"refs/pull/{number}/head"
        )
        fetched = packet.resolve(Path.cwd(), "FETCH_HEAD")
        packet.require(fetched == head, "PR changed during object fetch; rerun")
        p, patch = packet.prepare(Path.cwd(), repository, base, head, brief)
        packet.write_packet(out / str(number), p, patch)
        runs = api(f"repos/{repository}/actions/runs?head_sha={head}&per_page=100")
        evidence = {
            "head_sha": head,
            "source": "GitHub Actions API",
            "note": "Live snapshot only; does not change review identity or replace required CI.",
            "runs": [
                {
                    k: r.get(k)
                    for k in [
                        "id",
                        "name",
                        "path",
                        "head_sha",
                        "event",
                        "status",
                        "conclusion",
                        "html_url",
                    ]
                }
                for r in runs["workflow_runs"]
                if r["head_sha"] == head
            ],
        }
        (out / str(number) / "ci-evidence.json").write_text(
            json.dumps(evidence, indent=2)
        )
        packet.require(
            not draft, "Draft: packet prepared; independent review not final"
        )
        if brief["tier"] != "T0":
            packet.validate_receipt(p, packet.extract(body, "receipt"))
        verdict = "success"
        description = (
            "Docs-only T0 packet verified; required CI remains separate"
            if brief["tier"] == "T0"
            else "Review receipt matches base, head and brief; required CI remains separate"
        )
    except (packet.Invalid, KeyError, TypeError, ValueError, OSError) as exc:
        # Do not put attacker-controlled values/error text into workflow commands or HTML.
        (out / str(number)).mkdir(parents=True, exist_ok=True)
        (out / str(number) / "failure.json").write_text(
            json.dumps({"error": str(exc)}, indent=2)
        )
        print(f"PR {number}: review packet incomplete; see failure.json")
    latest = api(f"repos/{repository}/pulls/{number}")
    if identity(latest) != identity(pr):
        # Never apply an old success to a new description/base/head. The next event re-evaluates.
        status(
            repository,
            head,
            "pending",
            "PR changed during verification; waiting for fresh packet",
        )
        return False
    status(repository, head, verdict, description)
    print(f"PR {number}: {verdict}")
    return verdict == "success"


def main():
    repository = os.environ["GITHUB_REPOSITORY"]
    packet.require(
        bool(re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)),
        "invalid repository",
    )
    event = packet.parse_json(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    out = Path("review-packet-artifacts")
    out.mkdir(exist_ok=True)
    results = [
        evaluate(repository, number, out)
        for number in targets(os.environ["GITHUB_EVENT_NAME"], event, repository)
    ]
    return 0 if all(results) else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (packet.Invalid, KeyError, TypeError, OSError) as exc:
        print(
            "Review packet gate failed closed: " + type(exc).__name__, file=sys.stderr
        )
        sys.exit(1)

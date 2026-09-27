#!/usr/bin/env python3
"""Reject automatic rollouts that would discard staging-only source history.

A reviewed squash merge has a different commit ID but the same tree. Accept
that equivalence too. Only the most recent 1024 ancestors are exported; older
or unlabelled deployments require a separately reviewed manual release.
"""

import json
import os
import re
import subprocess
import sys

SERVICES = (
    "backend",
    "worker",
    "worker-daily",
    "worker-collectors",
    "beat",
    "worker-actions",
)
HASH = re.compile(r"[0-9a-f]{40}")
REVISION = "org.opencontainers.image.revision"
TREE = "ai.suitestudio.source-tree"


def history():
    rows = subprocess.check_output(
        ["git", "log", "-1024", "--format=%H %T", "HEAD"], text=True
    ).splitlines()
    return {
        "revisions": sorted({r.split()[0] for r in rows}),
        "trees": sorted({r.split()[1] for r in rows}),
    }


def compatible(labels, allowed):
    return any(
        isinstance(labels.get(label), str)
        and HASH.fullmatch(labels[label])
        and labels[label] in allowed[key]
        for label, key in ((REVISION, "revisions"), (TREE, "trees"))
    )


def verify(allowed, inspect=None):
    if not isinstance(allowed, dict) or set(allowed) != {"revisions", "trees"}:
        raise ValueError("Invalid deployment source history")
    for values in allowed.values():
        if (
            not isinstance(values, list)
            or not values
            or len(values) > 1024
            or any(
                not isinstance(value, str) or not HASH.fullmatch(value)
                for value in values
            )
        ):
            raise ValueError("Invalid deployment source history")
    if inspect is None:

        def inspect(service):
            name = "ecom-netsuite-" + service + "-1"
            result = subprocess.run(
                ["docker", "inspect", name], capture_output=True, text=True
            )
            if result.returncode:
                if service != "backend" and "No such object" in result.stderr:
                    return None
                raise RuntimeError("Cannot inspect existing service: " + service)
            return json.loads(result.stdout)[0]["Config"].get("Labels") or {}

    checked = []
    for service in SERVICES:
        labels = inspect(service)
        if labels is None and service != "backend":
            continue
        if not isinstance(labels, dict) or not compatible(labels, allowed):
            raise RuntimeError(
                f"Refusing rollout: {service} contains source outside this release history "
                "(or lacks provenance). Integrate the staging source first; no services were changed."
            )
        checked.append(service)
    return checked


if __name__ == "__main__":
    if sys.argv[1:] == ["--history"]:
        print(json.dumps(history(), separators=(",", ":")))
    elif not sys.argv[1:]:
        print(
            json.dumps(
                {
                    "source_history_verified": verify(
                        json.loads(os.environ["DEPLOY_SOURCE_HISTORY"])
                    )
                }
            )
        )
    else:
        raise SystemExit("Usage: deploy_source_guard.py [--history]")

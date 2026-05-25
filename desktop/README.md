# Suite Studio Desktop

> Library-mode integration of Hermes Agent (vendored) into a Suite Studio Python sidecar.
> Phase B0 scaffold — see [`SPIKE-RESULTS.md`](SPIKE-RESULTS.md) for the pre-flight that resolved OQ-047 (library mode) and verified the `AIAgent` class surface.

This subtree is intentionally minimal. Subsequent `/goal`s layer Electron, Obsidian-memory-MCP, NetSuite MCP servers, packaging, and signing on top.

---

## Layout

```
desktop/
├── README.md                        # this file
├── SPIKE-RESULTS.md                 # B0 pre-flight spike (do not edit)
├── pyproject.toml                   # desktop-specific Python deps
├── runtime/
│   ├── hermes-agent/                # vendored Hermes Agent (git submodule, pinned)
│   └── sidecar.py                   # library-mode wrapper around AIAgent
├── skills/
│   └── suite-studio-netsuite/       # placeholder Suite Studio skill pack
│       ├── README.md
│       └── SKILL.md
└── tests/
    ├── __init__.py
    └── test_sidecar.py              # CI-safe mocked test
```

Out of scope for this `/goal` (tracked in subsequent ones): `electron/`, `packaging/`, `signing/`, `update/`, `runtime/obsidian-memory-mcp/`, `tools/self-evolution/`.

---

## Vendoring strategy — Hermes Agent at `v2026.5.16`

Hermes Agent is vendored as a **git submodule** at `desktop/runtime/hermes-agent`, pinned at tag **`v2026.5.16`**.

### Why a CalVer tag instead of `v0.14.0`

ADR-007 §Decision 7 and the B0 plan call for pin `v0.14.0`. **That string is the package metadata version inside `pyproject.toml`, not a git tag.** As of 2026-05-25 the Hermes Agent GitHub remote uses CalVer tags exclusively (`v2026.5.16`, `v2026.4.30`, `v2026.3.28`, …); no SemVer `v0.14.0` tag exists. The closest stable release tag prior to the goal date is **`v2026.5.16`** (released 2026-05-16), whose `pyproject.toml` declares `name = "hermes-agent"` and `version = "0.14.0"` — i.e., it is the canonical "v0.14.0" the plan refers to.

Reference: `git ls-remote --tags https://github.com/NousResearch/hermes-agent` confirms only CalVer tags. The B0 plan's documented failure mode for this exact case is "Use the closest stable tag + document the choice in README" — done here.

### Adding the submodule (reference)

```bash
git submodule add https://github.com/NousResearch/hermes-agent desktop/runtime/hermes-agent
cd desktop/runtime/hermes-agent
git checkout v2026.5.16
cd ../../..
git add .gitmodules desktop/runtime/hermes-agent
git commit -m "feat(desktop): vendor Hermes Agent at v2026.5.16 (== package v0.14.0)"
```

If the host sandbox blocks submodule operations on `.git/objects`, approve `dangerouslyDisableSandbox` for the single `git submodule add` invocation (same pattern the B0 spike used for the initial clone).

### Auto-update is disabled by omission

ADR-007 §Decision 7 requires Suite Studio to opt out of Hermes Agent's bundled auto-update. We comply by simply not vendoring or wiring any update scripts in this scaffold. Upgrading is a manual, reviewed operation (see below).

---

## Upgrading Hermes Agent

Per ADR-007 §Decision 7 cadence task. To bump:

```bash
cd desktop/runtime/hermes-agent
git fetch --tags
git checkout v2026.X.Y          # the new tag
cd ../../..
# re-run the smoke test + the sidecar test suite
cd desktop && pytest tests/
python runtime/sidecar.py        # requires ANTHROPIC_API_KEY
git add desktop/runtime/hermes-agent
git commit -m "chore(desktop): bump Hermes Agent to v2026.X.Y"
```

Always re-probe `AIAgent.__init__` signature after a bump (see `runtime/sidecar.py` docstring) — Hermes Agent is pre-1.0; signature drift is possible between minor releases.

---

## Model strategy — see [ADR-008](../../suite-studio-vault/10-Architecture/Decision-Records/ADR-008-model-strategy-desktop-v0.md)

The sidecar instantiates **two `AIAgent` objects** keyed by role:

| Role | Default model ID | Env var override |
|---|---|---|
| `default` | `claude-sonnet-4-6` | `SUITE_STUDIO_MODEL_DEFAULT` |
| `plan` | `claude-opus-4-7` | `SUITE_STUDIO_MODEL_PLAN` |

Swapping models is a **config change, never a code change**. To run the sidecar against Haiku:

```bash
SUITE_STUDIO_MODEL_DEFAULT=claude-haiku-4-5-20251001 python runtime/sidecar.py
```

The `default` agent powers the smoke test and the Electron-driven default chat surface. The `plan` agent is constructed but not exercised at smoke-test time; it gets wired into Plan Mode at B2+.

---

## Running the smoke test locally

```bash
cd desktop
python -m venv .venv && source .venv/bin/activate
pip install -e .                                  # installs hermes-agent from the submodule + pytest
export ANTHROPIC_API_KEY=sk-ant-...               # operator's BYOK Anthropic key
python runtime/sidecar.py
```

Expected: a non-empty Claude response printed to stdout. If the API key is missing, the sidecar exits with a clear error rather than running with an arbitrary key.

### Running the CI-safe test (no API key needed)

```bash
cd desktop
pytest tests/
```

The test mocks the `AIAgent` class — no live API call, safe to run anywhere.

---

## Decision-point status (per `docs/superpowers/plans/2026-05-24-desktop-b0-scaffold-and-sidecar.md`)

| Question | Status | Resolved where |
|---|---|---|
| OQ-047 (Electron ↔ Hermes Agent integration mode) | RESOLVED — library mode | `SPIKE-RESULTS.md`, ADR-007 §OQ-047 |
| OQ-048 (in-tree Obsidian skill overlap) | RESOLVED — keep Obsidian-memory-MCP | `SPIKE-RESULTS.md`, ADR-007 §OQ-048 |
| Vendoring strategy for Hermes Agent | RESOLVED — git submodule at `v2026.5.16` | this file |
| Python env for the sidecar | RESOLVED — isolated `desktop/.venv` | `pyproject.toml` |
| Model strategy | RESOLVED — Sonnet default + Opus plan, env-var override | ADR-008 |
| Sidecar IPC contract | OPEN — locked at fourth `/goal` (Electron wiring) | next plans |
| Packaging (PyInstaller / PyOxidizer) | OPEN — operator decision after bundle-size measurement | OQ-038 |
| DB persistence (Postgres / SQLite) | OPEN — operator decision | OQ-031 |

---

## References

- [`SPIKE-RESULTS.md`](SPIKE-RESULTS.md) — B0 pre-flight spike report (2026-05-24)
- ADR-007 §Decision 6 (repo layout) + §Decision 7 (pin + opt-out auto-update) — in the Suite Studio vault
- ADR-008 — Model strategy (Sonnet default + Opus plan + env-var contract)
- Desktop-Architecture-v1.md §3 — composed runtime, library mode locked
- Plan doc: `docs/superpowers/plans/2026-05-24-desktop-b0-scaffold-and-sidecar.md`

# Bounded scheduled review of saved evidence

`agent.review_saved_case` is the first supported scheduled agent step. It reads
one existing transaction investigation case through the same governed local
tool used by Chat, supplies an exact installed Accounting Operations skill and
reviewed accounting context, then requests one advisory assessment from the
company's configured model. It does not gather fresh source records or execute
model-returned tools. Fresh investigations, backfills and approved transaction
executors have separate acceptance contracts.

Use this step alone in a schedule. The existing compiler exposes its typed
schema and asks for missing bindings. The normal schedule approval, pause,
run-now, retry and history paths apply. A changed plan needs normal reapproval.
Do not invent identifiers, context versions or a principal from prose.

Required parameters:

- `principal_id`: an active company user, explicitly equal to the schedule owner.
  The compiler requires this principal to be the authenticated plan author;
  manual runs also require the triggering user to be that principal. Other
  schedule editors cannot borrow the owner's permissions. The user must currently hold `schedules.manage`, `connections.view` and
  `recon.run`. Global superadmins cannot serve as this principal. Neither the
  scheduler identity nor the user clicking Run now lends permissions.
- `tenant_id`, `case_id`, `config_id`: exact company/case/configuration UUIDs.
  The case must match the configuration's source/account/subsidiary scope.
  Required feature flags, source and destination lifecycle, and company tool
  policy are checked again at execution.
- `skill_version`: the installed `accounting_operations` catalog hash.
- `context_version`, `context_binding`, `scope`: the current accounting context
  manifest version and binding digest, plus explicit book/currency/period.
  At least one exact-scope reviewed entry must exist. Missing, stale, invalidated,
  conflicting or changed context blocks execution. An explicitly selected scope
  does not prove that a saved case belongs to a posting period.
- `budget`: `input_bytes` (1,024–64,000), `output_tokens` (128–2,048) and `seconds`
  (1–300). Input includes skill, selected context and evidence. Oversized inputs
  are refused rather than silently truncated. There is one local tool attempt,
  no external data query, one token-count request and at most one generation
  request per attempt. Actual input tokens are counted before generation and
  cannot exceed the input-byte limit. Output tokens are capped at the provider.

The shared interactive status projection removes positional evidence tables and
suppressed computed amounts before either token counting or synthesis.

The currently supported provider is Anthropic, using the existing configured
model and adapter. Thinking is disabled for this bounded classification call,
SDK generation retries are disabled, and the client closes on all paths.
Other providers block until their budget contracts are supported; the executor
never silently substitutes another provider or model. Token limits bound the
size of the paid request, not a fixed dollar price. A schedule-level USD ceiling
therefore blocks this step explicitly. The shorter step/schedule time limit
applies. A timed-out request may already have incurred provider usage; the durable
`agent.review.started` event records the attempted spend. Existing non-BYOK
credit accounting applies before that request. Each existing scheduler retry is
another bounded attempt; cross-attempt recovery/stop controls remain FW-013.

No financial action or approval tools are offered to the model. There is no
free-form instruction parameter. Returned tool requests or malformed output
are refused. Policies with blocked fields are currently refused before any evidence reaches
the model: key-based redaction cannot safely remove positional table values or
free-form historical text. Permissions, policy and context are checked again
before token counting, before paid generation and after it.
A change invalidates the result. This is advisory use of saved observations,
not fresh verification or proof of model compliance with supplied guidance.

Run history stores a compact receipt: principal/company, skill catalog/body
revision, context binding/version/scope, policy fingerprint, evidence digest,
attempt counts, requested model and returned token usage, termination code and an
advisory assessment enum. It excludes evidence bodies and arbitrary model text.
Operators inspect the original case through its existing permission-protected
investigation UI. Outcomes distinguish `done`, `blocked`, `budget` and `error`;
`done` only means the saved-evidence review completed. It does not close the case,
authorize a correction, or assert that a transaction was posted or reconciled.

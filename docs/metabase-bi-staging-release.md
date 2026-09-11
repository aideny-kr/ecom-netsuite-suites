# Metabase BI staging release

Code commit: `97e8436ae7ea820d0a73152ccfaa7502a14e38a7` on
`release/metabase-bi-staging`. The branch is pushed to origin; main is not merged.

The candidate starts from staging's `d0d2aed7`, preserving native Metabase OAuth,
HTTP connector protections, and the previously deployed accounting changes.
It adds automatic Metabase BI/SQL skills, explicit source selection, native Cloud
read-tool recognition, and uninterrupted validation queries.

Independent review identified two follow-up defects before deployment: structured
MBQL logs were treated as SQL strings during history replay, and verified source
choices from clarification cards were lost. Both were fixed in `97e8436a` and
independently rechecked. Source selection now uses the full session history and
passes the verified canonical source to the model even after history compaction.
See the [independent review](metabase-bi-release-review.md).

## Validation

Release acceptance completed on 2026-09-10 03:59 UTC.

- 739 integrated backend regression tests passed, including native Metabase OAuth,
  HTTP/custom-tool confirmation, tenant isolation, and financial-write guards.
- After review fixes, 270 focused history/source/Metabase/Plan Mode tests passed.
  These overlap the earlier suite; they are not an additional unique-test total.
  Two existing AsyncMock fixture warnings remain.
- 56 frontend component tests, TypeScript checks, and production builds passed.
- The actual deployed Chrome chat UI first asked for the data source with zero
  data-tool calls. Selecting Metabase produced 65 distinct matching orders in
  batch 395: 41 complete and 24 canceled.
- A further UI turn omitted the source name and requested server-side distinct
  order-ID aggregates. It reused the selected Metabase source and saved query
  history without a crash or another source question. One native query returned
  the same status counts, matching the separate control queries.

The [deployed UI evidence](metabase-bi-staging-evidence.json) records the exact
follow-up query and response without customer rows or credentials. The earlier
[independent control evidence](metabase-bi-batch395-evidence.json) records total
and status aggregates. Initial generation still needed query-shape/handle error
recovery; the release does not claim that model-generated queries never err.

The temporary browser login was cleared and its short-lived JWT revoked. The
staging test conversation is retained as evidence. No external records were
written, and no database migration or production deployment was performed.

## Images and rollback

Backend / worker / Beat: `backend:staging-97e8436a`.
Backend manifest and running image ID:
`sha256:b5129dec0500b3acba92d94e26434181d8ededa39e11b1745d612f3e80d76fb4`.

Frontend: `frontend:staging-97e8436a-auth`, from the same reviewed code commit,
with build ID `97e8436a-auth`. Published manifest:
`sha256:4abed77acddacddf9a36651553ad4dc0ede326c29f276214f45dd30bd9f59a1e`.

The initial frontend build omitted the existing public Google sign-in client ID;
logout smoke caught it, and the replacement build restores the same setting as
staging's previous `94299fc5` image. The final running image digest matches the
published manifest, and a fresh Chrome login page rendered the Google sign-in
button successfully. No OAuth client or permission was changed.

Full rollback configuration:
`/opt/ecom-netsuite/docker-compose.before-metabase-bi-97e8436a.yml`
restores backend/worker/Beat `d0d2aed7` and frontend `94299fc5`.
The current staging release ledger is `/opt/ecom-netsuite/STAGING_RELEASE.md`.

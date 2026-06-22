# Skills Page — Design Spec

> **Status:** approved design (brainstorm complete) → next: writing-plans
> **Date:** 2026-06-21
> **Branch:** `feat/skills-page`
> **Tier:** T1 (frontend-only, read-only over an existing endpoint).
> Follow-up to the advisory accountant skills (PR #138) — the user wanted a browsable
> Skills page as its own PR.

## 1. Goal

A browsable reference page for all chat slash-skills returned by `GET /api/v1/skills/catalog`
(12 today: the 4 original operational skills + the 7 advisory accountant skills from #138 +
`/export_analytics`-style built-ins are NOT in the catalog). Users can discover what skills
exist, read each one's purpose, and jump into chat with a skill prefilled in the composer.

Today skills are only discoverable by typing `/` in the chat composer (the popover menu).
This page makes the full catalog browsable from the sidebar.

## 2. Scope

**In scope**
- New route `app/(dashboard)/skills/page.tsx` — search box + responsive card grid.
- `useAgentSkills` React Query hook over the existing catalog endpoint.
- `SkillCard` component with a "Use in chat" action.
- A **populate-without-send** composer prefill: a new `compose` query param the chat page
  reads and seeds into the composer (distinct from the existing auto-send `prefill`).
- Nav entry in `NAV_ITEMS` + `iconMap`.
- vitest unit tests + a Playwright e2e (golden path).

**Out of scope (YAGNI)**
- No backend change. The catalog endpoint already returns everything needed.
- No category grouping. The catalog has no category field; hardcoding skill→category is
  fragile and drifts. Flat grid + search instead.
- No per-skill detail page, no favoriting, no usage analytics.
- No change to the existing `/`-menu in chat-input (it stays as-is).

## 3. Background: verified facts

- **Endpoint:** `GET /api/v1/skills/catalog` → `AgentSkillMetadata[]` where
  `AgentSkillMetadata = { name: string; description: string; triggers: string[]; slug: string }`
  (`frontend/src/lib/types.ts`). Returns every registered skill. chat-input.tsx already
  consumes it with React Query key `["agent-skills"]`.
- **Primary trigger:** each skill's first `/`-prefixed trigger is its slash command
  (chat-input uses `skill.triggers.find(t => t.startsWith("/")) || skill.triggers[0]`). The
  same selection is reused here.
- **Nav:** `NAV_ITEMS` in `frontend/src/lib/constants.ts` is a flat array of
  `{ label, href, icon, featureFlag }`. `sidebar.tsx` renders it, filters by `featureFlag`
  against `useFeatures()`, and maps `icon` via a string→component `iconMap`. Adding a page =
  one array entry + one `iconMap` entry.
- **Closest analog:** `app/(dashboard)/memory/page.tsx` — a self-contained client list page
  with React Query, Skeleton loading, error state, empty state, and the card/grid styling.
  This page mirrors its structure.
- **Composer prefill:** the chat page (`app/(dashboard)/chat/page.tsx`) already reads a
  `prefill` query param — but it **auto-sends** the message (used by recon's "Investigate in
  Chat" at reconciliation/page.tsx:114). Auto-send is wrong for skills (firing `/flux` bare
  skips the args the user wants to add), so this feature adds a separate `compose` param that
  **populates the composer without sending**.
- **Frontend rules** (`.claude/rules/frontend.md`): `"use client"` on hook files, always
  `apiClient` (never raw fetch), query-key conventions, page styling tokens
  (`animate-fade-in space-y-8 p-8`, `text-2xl` titles, `rounded-xl border bg-card p-5
  shadow-soft` cards, lucide icons only). Route groups: `(dashboard)/skills` serves at
  `/skills`.

## 4. Architecture (4 units)

### 4.1 `useAgentSkills` — `frontend/src/hooks/use-agent-skills.ts`
- `"use client"`. React Query over `apiClient.get<AgentSkillMetadata[]>("/api/v1/skills/catalog")`.
- Query key `["agent-skills"]` (shares cache with chat-input's existing fetch).
- Returns the standard `{ data, isLoading, error }`.

### 4.2 `SkillCard` — `frontend/src/components/skills/skill-card.tsx`
- `"use client"`. Props: `{ skill: AgentSkillMetadata }`.
- Renders: `name` (title), `description` (muted body), the primary `/slash` trigger as a
  monospace chip, and a **"Use in chat"** button.
- Helper `primarySlash(skill)` = `skill.triggers.find(t => t.startsWith("/")) ?? skill.triggers[0]`.
- "Use in chat" → `router.push('/chat?compose=' + encodeURIComponent(primarySlash + ' ') + '&new_session=true')`
  (trailing space so the user types args directly after the command).
- Card styling: `rounded-xl border bg-card p-5 shadow-soft`.

### 4.3 Skills page — `frontend/src/app/(dashboard)/skills/page.tsx`
- `"use client"`, root `animate-fade-in space-y-8 p-8`.
- Header: lucide `Sparkles` icon + `Skills` title (`text-2xl font-bold`) + one-line muted
  description ("Slash commands you can run in chat. Search, then send one straight to the
  composer.").
- Search `<input>` bound to local state; filter is client-side, case-insensitive, matching
  `name`, `description`, and any `trigger` (substring).
- States (copied from memory page): loading → `Skeleton` grid; error → destructive card;
  empty (no skills / no search match) → centered empty card with `Sparkles`.
- Grid: `grid gap-4 sm:grid-cols-2 lg:grid-cols-3`, one `SkillCard` per filtered skill.

### 4.4 Composer prefill wiring (only change to existing files)
- `app/(dashboard)/chat/page.tsx`: read `const composeMessage = searchParams?.get("compose") || null;`
  and pass `initialMessage={composeMessage}` to `<ChatInput>`.
- `components/chat/chat-input.tsx`: accept an optional `initialMessage?: string | null` prop;
  a `useEffect` seeds `setValue(initialMessage)` exactly once when present (guarded by a
  `composeSeededRef` so it doesn't clobber user typing on re-render). It does NOT send.
- `sidebar.tsx` + `constants.ts`: add the nav entry (see §5).

## 5. Nav registration

`frontend/src/lib/constants.ts` — append to `NAV_ITEMS`:
```ts
{ label: "Skills", href: "/skills", icon: "Sparkles" as const, featureFlag: "chat" },
```
`frontend/src/components/sidebar.tsx` — add `Sparkles` to the lucide import and to `iconMap`.
`featureFlag: "chat"` because skills only function in chat; tenants without chat shouldn't
see the page.

## 6. Testing

**vitest (`npx vitest run`, scoped to `src/**`):**
- `use-agent-skills` — mocks `apiClient.get`, asserts it calls the catalog path and returns
  the array.
- `SkillCard` — renders name/description/primary-slash; "Use in chat" calls `router.push`
  with `/chat?compose=%2Fflux%20&new_session=true` for a `/flux` skill (mock `useRouter`).
- Skills page — given a mocked hook with 3 skills, renders 3 cards; typing in search narrows
  to matches; empty-match shows the empty state.
- chat-input — when `initialMessage="/flux "` is passed, the composer value seeds to it once
  and is NOT auto-sent (no `onSend` call).

**Playwright e2e (gated in CI, not `continue-on-error`):**
- Log in → click "Skills" in the sidebar → page lists skill cards → type "flux" in search →
  only the flux card remains → click "Use in chat" → URL is `/chat?compose=...`, the composer
  contains `/flux`, and no message was sent (message list unchanged).

## 7. Tier, review, deploy

- **Tier T1** — frontend-only, read-only over an existing endpoint; no auth/RLS/data/prompt/
  migration surface. (Not T2: touches no high-risk trigger.)
- **Gates:** existing CI + the Playwright e2e (CI-gated) + `/code-review` (light).
- **Deploy:** frontend is **manual** — after merge, run `./deploy-frontend.sh` (or the buildx
  command with `--build-arg NEXT_PUBLIC_BUILD_ID=$(git rev-parse --short HEAD)`). Backend
  unchanged. The catalog already serves the data live, so the page works on first load.

## 8. File manifest

```
frontend/src/hooks/use-agent-skills.ts                      (new)
frontend/src/components/skills/skill-card.tsx               (new)
frontend/src/app/(dashboard)/skills/page.tsx               (new)
frontend/src/lib/constants.ts                              (modify — NAV_ITEMS entry)
frontend/src/components/sidebar.tsx                        (modify — Sparkles import + iconMap)
frontend/src/app/(dashboard)/chat/page.tsx                 (modify — read `compose`, pass initialMessage)
frontend/src/components/chat/chat-input.tsx                (modify — initialMessage prop + seed effect)
frontend/src/hooks/__tests__/use-agent-skills.test.tsx     (new)
frontend/src/components/skills/__tests__/skill-card.test.tsx (new)
frontend/src/app/(dashboard)/skills/__tests__/page.test.tsx (new)
frontend/e2e/skills.spec.ts                                 (new — Playwright testDir is ./e2e)
```

## 9. Risks & mitigations

- **Reusing auto-send `prefill` by mistake** → use a distinct `compose` param; the seed effect
  only populates, never calls `onSend`. Test asserts no send.
- **Catalog grows / unwieldy grid** → client-side search box keeps it usable; flat grid is
  fine at current ~12 and scales with search.
- **Nav shows for non-chat tenants** → `featureFlag: "chat"` filters it out (existing
  mechanism, same as the Chat entry).
- **Composer seed clobbers user typing on re-render** → one-shot guard ref
  (`composeSeededRef`), mirrors the existing `prefillSentRef` pattern.
- **e2e flakiness on "not sent" assertion** → assert the composer `value` equals `/flux ` AND
  the message list shows no new user message after the click.
```

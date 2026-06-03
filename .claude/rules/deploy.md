---
description: Deploy + CI gotchas. Loads when editing workflows or compose files.
paths:
  - .github/workflows/**
  - docker-compose*.yml
  - infra/**
  - backend/Dockerfile*
  - frontend/Dockerfile*
---

# Deploy + CI rules

1. **Verify deploy by image digest, not container health** — `docker compose pull` can 403 on one image while `curl /health` still passes (old container healthy) and the workflow goes green. Check: `ssh aidenyi@34.73.236.64 "sudo docker inspect ecom-netsuite-backend-1 --format '{{.Image}}'"` vs the pushed image digest.
2. **nginx `ssl_buffer_size` for SSE** — default 16KB causes bursty TLS streaming. Set to 4k for real-time SSE.
3. **`workflow_dispatch` doesn't update `:latest` on feature branches** — SHA tag gets built but `:latest` only tags on main. Manually retag on staging until automated.
4. **Frontend deploy is manual** — CI only deploys backend. Run `./deploy-frontend.sh` from local after main merges that touch frontend.
5. **JWT_SECRET_KEY differs per env** — tokens minted locally fail against staging. Mint via the target environment's backend.
6. **Redis is required in production** — `token_denylist.py` and `rate_limit.py` are Redis-backed. The in-memory fallback only works in `APP_ENV=development`. Deploys without Redis will boot but silently drop denied tokens + skip rate limits.
7. **A frontend deploy strands every already-open tab (Next.js stale-bundle skew)** — after a manual FE deploy, an open tab runs the OLD bundle against the NEW build. Next 14 `standalone` logs `Failed to find Server Action "x" ... older or newer deployment` + `TypeError ... 'bind' at handleRequestImpl`; the user sees a page that renders but is **"static, no interaction, things popup."** The app defines **no `"use server"` actions** — this is purely the stale client bundle, NOT a backend/infra/code bug. **Durable fix is BUILT** (branch `fix/fe-stale-bundle-self-recovery`): a build-version banner (`useVersionCheck` polls same-origin `/version` → "A new version is available." + Refresh) + `app/global-error.tsx`/`chunk-reload-guard.tsx` that auto-reload (once, sessionStorage loop-guarded so it can never infinite-loop) on a `ChunkLoadError`, so open tabs self-recover. **REQUIRED for the fix to work**: the FE buildx command MUST pass `--build-arg NEXT_PUBLIC_BUILD_ID=$(git rev-parse --short HEAD)` — it is inlined into both the client bundle and the `/version` route; without it `BUILD_ID` falls back to `"dev"` and the version check is a deliberate no-op. The FIRST deploy that introduces this fix still needs one final hard-refresh (`Cmd+Shift+R`, and tell any active tester) to seed the new build's id; every deploy after that self-recovers. Diagnostic still applies: if `ssh aidenyi@34.73.236.64 "sudo docker logs ecom-netsuite-frontend-1 --timestamps | grep 'Failed to find Server Action'"` shows only OLD errors and `/login` returns 200, it's client cache — don't chase the backend. See memory `reference_frontend_stale_bundle_server_action_skew`.

See `.claude/skills/saas-deployment/SKILL.md` for full deploy procedure.

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

See `.claude/skills/saas-deployment/SKILL.md` for full deploy procedure.

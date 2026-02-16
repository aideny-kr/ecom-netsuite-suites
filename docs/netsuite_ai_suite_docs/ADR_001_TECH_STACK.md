# ADR 001 — Tech Stack
_Date: 2026-02-15_

## Decision
Adopt a Python-first stack for v1:
- API: FastAPI (Python 3.11+ with strict typing / Pydantic)
- Workers: Celery + Redis
- Data: Postgres + pgvector
- UI: Streamlit (MVP) with a plan to migrate to Next.js for multi-tenant SaaS UX
- AI orchestration: LangGraph + MCP server (Python)

## Rationale
- Pandas-heavy reconciliation fits Python
- FastAPI + Celery is a proven async + background job pattern
- pgvector avoids a separate vector database for v1

## Consequences
- Streamlit is fast for prototyping; plan a path to a standard web UI as requirements stabilize.

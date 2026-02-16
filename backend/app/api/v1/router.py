from fastapi import APIRouter

from app.api.v1 import audit, auth, connections, health, jobs, tables, tenants, users

api_router = APIRouter(prefix="/api/v1")

api_router.include_router(auth.router)
api_router.include_router(tenants.router)
api_router.include_router(users.router)
api_router.include_router(connections.router)
api_router.include_router(tables.router)
api_router.include_router(audit.router)
api_router.include_router(jobs.router)
api_router.include_router(health.router)

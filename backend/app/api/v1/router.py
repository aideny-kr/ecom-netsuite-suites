from fastapi import APIRouter

from app.api.v1 import (
    audit,
    auth,
    chat,
    chat_api_keys,
    chat_integration,
    connections,
    health,
    jobs,
    mcp_connectors,
    netsuite_auth,
    netsuite_metadata,
    onboarding,
    policies,
    schedules,
    sync,
    tables,
    tenants,
    users,
    workspaces,
)

api_router = APIRouter(prefix="/api/v1")

api_router.include_router(auth.router)
api_router.include_router(tenants.router)
api_router.include_router(users.router)
api_router.include_router(connections.router)
api_router.include_router(netsuite_auth.router)
api_router.include_router(tables.router)
api_router.include_router(audit.router)
api_router.include_router(jobs.router)
api_router.include_router(chat.router)
api_router.include_router(mcp_connectors.router)
api_router.include_router(sync.router)
api_router.include_router(schedules.router)
api_router.include_router(health.router)
api_router.include_router(workspaces.router)
api_router.include_router(workspaces.changeset_router)
api_router.include_router(workspaces.run_router)
api_router.include_router(onboarding.router)
api_router.include_router(policies.router)
api_router.include_router(chat_api_keys.router)
api_router.include_router(chat_integration.router)
api_router.include_router(netsuite_metadata.router)

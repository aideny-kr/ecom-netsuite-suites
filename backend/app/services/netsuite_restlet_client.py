"""NetSuite RESTlet client — calls the Ecom FileCabinet RESTlet for file I/O."""

from __future__ import annotations

import httpx
import structlog

from app.services.netsuite_client import _normalize_account_id

logger = structlog.get_logger()


def _restlet_base_url(account_id: str) -> str:
    """Build RESTlet base URL (without query params)."""
    slug = _normalize_account_id(account_id)
    return f"https://{slug}.restlets.api.netsuite.com/app/site/hosting/restlet.nl"


def _restlet_params(script_id: str, deploy_id: str, **extra) -> dict:
    """Build query params dict with script/deploy IDs plus any extras."""
    params = {"script": script_id, "deploy": deploy_id}
    params.update(extra)
    return params


# Script/deploy IDs — will be configurable via Connection metadata later
FILECABINET_SCRIPT_ID = "3901"
FILECABINET_DEPLOY_ID = "1"
MOCKDATA_SCRIPT_ID = "customscript_ecom_mockdata_rl"
MOCKDATA_DEPLOY_ID = "customdeploy_ecom_mockdata_rl"


async def restlet_read_file(
    access_token: str,
    account_id: str,
    file_id: int,
    timeout: int = 15,
) -> dict:
    """Read a file from NetSuite File Cabinet via RESTlet GET."""
    url = _restlet_base_url(account_id)
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    params = _restlet_params(
        FILECABINET_SCRIPT_ID, FILECABINET_DEPLOY_ID, fileId=str(file_id)
    )

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(url, headers=headers, params=params)
        resp.raise_for_status()

    data = resp.json()
    if not data.get("success"):
        raise RuntimeError(f"RESTlet error: {data.get('message', 'Unknown error')}")
    return data


async def restlet_write_file(
    access_token: str,
    account_id: str,
    file_id: int,
    content: str,
    timeout: int = 30,
) -> dict:
    """Write/update a file in NetSuite File Cabinet via RESTlet PUT."""
    url = _restlet_base_url(account_id)
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    params = _restlet_params(FILECABINET_SCRIPT_ID, FILECABINET_DEPLOY_ID)
    payload = {"fileId": file_id, "content": content}

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.put(url, headers=headers, json=payload, params=params)
        resp.raise_for_status()

    data = resp.json()
    if not data.get("success"):
        raise RuntimeError(f"RESTlet error: {data.get('message', 'Unknown error')}")
    return data


async def restlet_create_file(
    access_token: str,
    account_id: str,
    name: str,
    folder_id: int,
    content: str,
    file_type: str = "JAVASCRIPT",
    timeout: int = 15,
) -> dict:
    """Create a new file in NetSuite File Cabinet via RESTlet POST."""
    url = _restlet_base_url(account_id)
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    params = _restlet_params(FILECABINET_SCRIPT_ID, FILECABINET_DEPLOY_ID)
    payload = {
        "name": name,
        "folder": folder_id,
        "content": content,
        "fileType": file_type,
    }

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, headers=headers, json=payload, params=params)
        resp.raise_for_status()

    data = resp.json()
    if not data.get("success"):
        raise RuntimeError(f"RESTlet error: {data.get('message', 'Unknown error')}")
    return data


async def restlet_get_folder_map(
    access_token: str,
    account_id: str,
    timeout: int = 15,
) -> dict:
    """Retrieve the entire folder hierarchy mapping from NetSuite via RESTlet GET."""
    url = _restlet_base_url(account_id)
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    params = _restlet_params(
        FILECABINET_SCRIPT_ID, FILECABINET_DEPLOY_ID, action="folderMap"
    )

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(url, headers=headers, params=params)
        resp.raise_for_status()

    data = resp.json()
    if not data.get("success"):
        raise RuntimeError(f"RESTlet error: {data.get('message', 'Unknown error')}")
    return data.get("folders", {})


async def restlet_extract_mock_data(
    access_token: str,
    account_id: str,
    suiteql_query: str,
    limit: int = 100,
    mask_pii: bool = True,
    timeout: int = 30,
) -> dict:
    """Extract mock data via the MockData RESTlet with PII masking."""
    url = _restlet_base_url(account_id)
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    params = _restlet_params(MOCKDATA_SCRIPT_ID, MOCKDATA_DEPLOY_ID)
    payload = {
        "query": suiteql_query,
        "limit": limit,
        "maskPII": mask_pii,
    }

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, headers=headers, json=payload, params=params)
        resp.raise_for_status()

    data = resp.json()
    if not data.get("success"):
        raise RuntimeError(f"RESTlet error: {data.get('message', 'Unknown error')}")
    return data

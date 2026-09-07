"""Small, read-only health probes for native providers (not MCP servers)."""

import asyncio
import re
from urllib.parse import urlencode

from app.services import http_connector_service as http


def failure_message(provider: str, exc: Exception) -> str:
    """Never expose SDK exception text, provider bodies, URLs or credentials."""
    code = getattr(exc, "code", None)
    if code == "authentication_failed":
        return f"{provider} denied read access. Check the credential, API permissions, and whether the API is enabled."
    if code == "rate_limited":
        return f"{provider} rate limit reached. Wait briefly and test again."
    if isinstance(exc, (ValueError, KeyError, TypeError)):
        return f"{provider} credentials or settings are invalid. Update the connection and test again."
    if code == "invalid_response":
        return f"{provider} returned an unexpected response. Read access could not be verified."
    return f"{provider} could not be reached or verified. Try again; if it persists, check API access."


async def _read(base_url: str, token: str, path: str) -> dict:
    http.validate_auth("bearer", token)
    result = await http.read_json({"base_url": base_url, "auth_type": "bearer", "token": token}, path)
    if not isinstance(result, dict):
        raise http.ConnectorReadError("invalid_response")
    return result


async def check_stripe(credentials: dict) -> dict:
    result = await _read("https://api.stripe.com/", credentials["api_key"], "v1/balance")
    if result.get("object") != "balance":
        raise http.ConnectorReadError("invalid_response")
    return {"status": "ok", "message": "Stripe balance read access verified."}


async def _google_access_token(info: dict, scopes: tuple[str, ...]) -> str:
    """Use Google's token endpoint only; bound refresh and disable redirects/proxies."""
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2 import service_account
    from requests import Session

    if not isinstance(info, dict) or info.get("universe_domain", "googleapis.com") != "googleapis.com":
        raise ValueError("Invalid service account")
    token_url = "https://oauth2.googleapis.com/token"
    account = service_account.Credentials.from_service_account_info({**info, "token_uri": token_url}, scopes=scopes)

    def refresh():
        with Session() as session:
            session.trust_env = False
            request = Request(session=session)

            def bounded_request(url, **kwargs):
                if url != token_url:
                    raise ValueError("Unexpected credential endpoint")
                kwargs.update(timeout=10, allow_redirects=False)
                return request(url, **kwargs)

            try:
                account.refresh(bounded_request)
            except RefreshError:
                raise http.ConnectorReadError("authentication_failed") from None
            return account.token

    async with asyncio.timeout(15):
        return await asyncio.to_thread(refresh)


async def check_google(provider: str, credentials: dict, metadata: dict) -> dict:
    """Metadata reads only: no query jobs and no spreadsheet creation/deletion."""
    if provider == "bigquery":
        project = credentials.get("project_id") or metadata.get("project_id")
        if not isinstance(project, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", project):
            raise ValueError("Invalid project")
        token = await _google_access_token(
            credentials["service_account_json"], ("https://www.googleapis.com/auth/bigquery.readonly",)
        )
        params = urlencode({"maxResults": 1, "fields": "kind,datasets(datasetReference(datasetId))"})
        result = await _read(
            "https://bigquery.googleapis.com/", token, f"bigquery/v2/projects/{project}/datasets?{params}"
        )
        if result.get("kind") != "bigquery#datasetList":
            raise http.ConnectorReadError("invalid_response")
        return {
            "status": "ok",
            "message": "BigQuery dataset listing access verified. Query and table permissions are checked when used.",
        }

    token = await _google_access_token(
        credentials["service_account_json"],
        (
            "https://www.googleapis.com/auth/drive.readonly",
            "https://www.googleapis.com/auth/spreadsheets.readonly",
        ),
    )
    params = {
        "pageSize": 1,
        "fields": "files(id),nextPageToken",
        "q": "mimeType='application/vnd.google-apps.spreadsheet' and trashed=false",
        "supportsAllDrives": "true",
        "includeItemsFromAllDrives": "true",
    }
    if metadata.get("shared_drive_id"):
        params.update(corpora="drive", driveId=metadata["shared_drive_id"])
    result = await _read("https://www.googleapis.com/", token, "drive/v3/files?" + urlencode(params))
    files = result.get("files")
    if not isinstance(files, list):
        raise http.ConnectorReadError("invalid_response")
    if not files:
        return {
            "status": "partial",
            "message": (
                "Google authentication and Drive access verified. No accessible spreadsheet was found "
                "to test Sheets read access. Share a spreadsheet with the service account and test again."
            ),
        }
    identifier = files[0].get("id") if isinstance(files[0], dict) else None
    if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", identifier):
        raise http.ConnectorReadError("invalid_response")
    sheet = await _read("https://sheets.googleapis.com/", token, f"v4/spreadsheets/{identifier}?fields=spreadsheetId")
    if sheet.get("spreadsheetId") != identifier:
        raise http.ConnectorReadError("invalid_response")
    return {"status": "ok", "message": "Google Sheets read access verified. No files were created or changed."}

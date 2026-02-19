# Multi-Agent Implementation — Hybrid NetSuite Architecture

RESTlet for file I/O + SuiteBundler for customer deployment + SuiteQL for mock data extraction + SuiteCloud Unit Testing framework.

**Architecture Decision**: NetSuite REST API `PATCH /file/{id}` returns 405 for content writes. We replace it with a custom RESTlet using `N/file` module, deployed to customer accounts via SuiteBundler.

---

## Agent 1: SuiteScript Engineer — File Cabinet RESTlet + SuiteBundler Boilerplate

### Task 1A: Create the File Cabinet RESTlet

**Create:** `suiteapp/src/FileCabinet/SuiteScripts/ecom_file_cabinet_restlet.js`

Write a SuiteScript 2.1 RESTlet that provides file read/write operations on the NetSuite File Cabinet. This is the core API that replaces the broken REST API `PATCH /file/{id}`.

```javascript
/**
 * @NApiVersion 2.1
 * @NScriptType Restlet
 * @NModuleScope SameAccount
 */
define(['N/file', 'N/search', 'N/log', 'N/runtime', 'N/error'], (file, search, log, runtime, error) => {

    /**
     * GET: Read file content by internal ID or path.
     * Query params: fileId (number) OR filePath (string)
     * Returns: { success, fileId, name, folder, content, size, fileType, lastModified }
     */
    const get = (requestParams) => {
        try {
            const script = runtime.getCurrentScript();
            log.debug('FileCabinet GET', JSON.stringify(requestParams));

            let fileObj;
            if (requestParams.fileId) {
                fileObj = file.load({ id: parseInt(requestParams.fileId, 10) });
            } else if (requestParams.filePath) {
                fileObj = file.load({ id: requestParams.filePath });
            } else {
                throw error.create({
                    name: 'MISSING_PARAM',
                    message: 'Provide fileId or filePath',
                });
            }

            return {
                success: true,
                fileId: fileObj.id,
                name: fileObj.name,
                folder: fileObj.folder,
                content: fileObj.getContents(),
                size: fileObj.size,
                fileType: fileObj.fileType,
                lastModified: fileObj.dateCreated?.toISOString() || null,
                remainingUsage: script.getRemainingUsage(),
            };
        } catch (e) {
            log.error('FileCabinet GET Error', e.message);
            return { success: false, error: e.name, message: e.message };
        }
    };

    /**
     * POST: Create a new file in the File Cabinet.
     * Body: { name, folder (ID), content, fileType? (default JAVASCRIPT), description? }
     * Returns: { success, fileId, name }
     */
    const post = (requestBody) => {
        try {
            log.debug('FileCabinet POST', `name=${requestBody.name}, folder=${requestBody.folder}`);

            if (!requestBody.name || !requestBody.folder || requestBody.content === undefined) {
                throw error.create({
                    name: 'MISSING_FIELDS',
                    message: 'name, folder, and content are required',
                });
            }

            const fileObj = file.create({
                name: requestBody.name,
                fileType: requestBody.fileType || file.Type.JAVASCRIPT,
                contents: requestBody.content,
                folder: parseInt(requestBody.folder, 10),
                description: requestBody.description || '',
            });

            const fileId = fileObj.save();
            log.audit('FileCabinet CREATE', `Created file ${requestBody.name} with ID ${fileId}`);

            return { success: true, fileId: fileId, name: requestBody.name };
        } catch (e) {
            log.error('FileCabinet POST Error', e.message);
            return { success: false, error: e.name, message: e.message };
        }
    };

    /**
     * PUT: Update existing file content by internal ID.
     * Body: { fileId, content, description? }
     * Returns: { success, fileId, name, size }
     *
     * N/file does not support in-place update — we delete + re-create with same name/folder.
     */
    const put = (requestBody) => {
        try {
            log.debug('FileCabinet PUT', `fileId=${requestBody.fileId}`);

            if (!requestBody.fileId || requestBody.content === undefined) {
                throw error.create({
                    name: 'MISSING_FIELDS',
                    message: 'fileId and content are required',
                });
            }

            // Load existing to preserve metadata
            const existing = file.load({ id: parseInt(requestBody.fileId, 10) });
            const meta = {
                name: existing.name,
                folder: existing.folder,
                fileType: existing.fileType,
                description: requestBody.description || existing.description || '',
            };

            // Delete and re-create (N/file update strategy)
            file.delete({ id: parseInt(requestBody.fileId, 10) });

            const newFile = file.create({
                name: meta.name,
                fileType: meta.fileType,
                contents: requestBody.content,
                folder: meta.folder,
                description: meta.description,
            });

            const newFileId = newFile.save();
            log.audit('FileCabinet UPDATE', `Updated ${meta.name}: old=${requestBody.fileId} new=${newFileId}`);

            return {
                success: true,
                fileId: newFileId,
                name: meta.name,
                size: requestBody.content.length,
                previousFileId: parseInt(requestBody.fileId, 10),
            };
        } catch (e) {
            log.error('FileCabinet PUT Error', e.message);
            return { success: false, error: e.name, message: e.message };
        }
    };

    /**
     * DELETE: Delete a file from the File Cabinet.
     * Query params: fileId (number)
     */
    const doDelete = (requestParams) => {
        try {
            if (!requestParams.fileId) {
                throw error.create({ name: 'MISSING_PARAM', message: 'fileId is required' });
            }
            const fid = parseInt(requestParams.fileId, 10);
            // Load first to get name for audit
            const existing = file.load({ id: fid });
            file.delete({ id: fid });
            log.audit('FileCabinet DELETE', `Deleted ${existing.name} (${fid})`);
            return { success: true, fileId: fid, name: existing.name };
        } catch (e) {
            log.error('FileCabinet DELETE Error', e.message);
            return { success: false, error: e.name, message: e.message };
        }
    };

    return { get, post, put, 'delete': doDelete };
});
```

### Task 1B: Create SDF Script Record XML

**Create:** `suiteapp/src/Objects/customscript_ecom_filecabinet_rl.xml`

```xml
<?xml version="1.0" encoding="UTF-8"?>
<restlet scriptid="customscript_ecom_filecabinet_rl">
    <name>Ecom FileCabinet RESTlet</name>
    <notifyadmins>F</notifyadmins>
    <isinactive>F</isinactive>
    <description>File Cabinet read/write API for ecom-netsuite-suites IDE workspace sync.</description>
    <scriptfile>[/SuiteScripts/ecom_file_cabinet_restlet.js]</scriptfile>
    <loglevel>DEBUG</loglevel>
    <scriptdeployments>
        <scriptdeployment scriptid="customdeploy_ecom_filecabinet_rl">
            <title>Ecom FileCabinet RESTlet Deploy</title>
            <status>RELEASED</status>
            <loglevel>DEBUG</loglevel>
            <allroles>F</allroles>
            <audslctrole>
                <role>ADMINISTRATOR</role>
                <role>FULL_ACCESS</role>
            </audslctrole>
        </scriptdeployment>
    </scriptdeployments>
</restlet>
```

### Task 1C: Create Bundle Installation Script

**Create:** `suiteapp/src/FileCabinet/SuiteScripts/ecom_bundle_install.js`

```javascript
/**
 * @NApiVersion 2.1
 * @NScriptType BundleInstallationScript
 * @NModuleScope SameAccount
 */
define(['N/log', 'N/runtime', 'N/email', 'N/record'], (log, runtime, email, record) => {

    const afterInstall = (params) => {
        log.audit('Bundle Install', `Ecom NetSuite Suite v${params.toversion} installed`);
        try {
            // Log the RESTlet deployment info for the tenant admin
            const user = runtime.getCurrentUser();
            log.audit('Bundle Install', `Installed by: ${user.name} (${user.email})`);
            log.audit('Bundle Install',
                'FileCabinet RESTlet is now available. ' +
                'Script: customscript_ecom_filecabinet_rl, Deploy: customdeploy_ecom_filecabinet_rl'
            );
        } catch (e) {
            log.error('Bundle Install Error', e.message);
        }
    };

    const beforeUpdate = (params) => {
        log.audit('Bundle Update', `Updating from v${params.fromversion} to v${params.toversion}`);
    };

    const afterUpdate = (params) => {
        log.audit('Bundle Updated', `Ecom NetSuite Suite updated to v${params.toversion}`);
    };

    const beforeUninstall = (params) => {
        log.audit('Bundle Uninstall', 'Ecom NetSuite Suite is being uninstalled');
    };

    return { afterInstall, beforeUpdate, afterUpdate, beforeUninstall };
});
```

### Task 1D: Create SDF Manifest and Project Config

**Create:** `suiteapp/manifest.xml`

```xml
<?xml version="1.0" encoding="UTF-8"?>
<manifest projecttype="ACCOUNTCUSTOMIZATION">
    <projectname>Ecom NetSuite Suite</projectname>
    <frameworkversion>1.0</frameworkversion>
    <dependencies>
        <features>
            <feature required="true">RESTLETS</feature>
            <feature required="true">SERVERSIDESCRIPTING</feature>
        </features>
    </dependencies>
</manifest>
```

**Create:** `suiteapp/deploy.xml`

```xml
<?xml version="1.0" encoding="UTF-8"?>
<deploy>
    <configuration>
        <path>~/Objects/*</path>
    </configuration>
    <files>
        <path>~/FileCabinet/SuiteScripts/*</path>
    </files>
</deploy>
```

**Create:** `suiteapp/suitecloud.config.js`

```javascript
module.exports = {
    defaultProjectFolder: 'src',
    commands: {}
};
```

### Task 1E: Create SuiteQL Mock Data Extraction Script

**Create:** `suiteapp/src/FileCabinet/SuiteScripts/ecom_mock_data_restlet.js`

This RESTlet extracts sanitized mock data from NetSuite for offline unit testing:

```javascript
/**
 * @NApiVersion 2.1
 * @NScriptType Restlet
 * @NModuleScope SameAccount
 */
define(['N/query', 'N/log', 'N/runtime'], (query, log, runtime) => {

    /**
     * POST: Execute a SuiteQL query and return sanitized results.
     * Body: { query, limit?, maskPII? }
     * maskPII=true (default) replaces names, emails, phones with fake data.
     */
    const post = (requestBody) => {
        try {
            const script = runtime.getCurrentScript();
            const sql = requestBody.query;
            const limit = Math.min(requestBody.limit || 100, 1000);
            const maskPII = requestBody.maskPII !== false;

            if (!sql) {
                return { success: false, message: 'query is required' };
            }

            log.debug('MockData Query', sql);

            const results = query.runSuiteQL({ query: sql + ` FETCH FIRST ${limit} ROWS ONLY` });
            const columns = results.columns.map((c) => c.label || c.fieldId || `col_${c.index}`);
            const rows = [];

            results.asMappedResults().forEach((row) => {
                if (maskPII) {
                    // Mask common PII fields
                    const masked = {};
                    Object.keys(row).forEach((key) => {
                        const lk = key.toLowerCase();
                        if (lk.includes('email')) {
                            masked[key] = `test_${rows.length}@example.com`;
                        } else if (lk.includes('phone') || lk.includes('fax')) {
                            masked[key] = '555-0100';
                        } else if (lk === 'companyname' || lk === 'entityid' || lk.includes('name')) {
                            masked[key] = `Test_Entity_${rows.length}`;
                        } else if (lk.includes('address') || lk.includes('addr')) {
                            masked[key] = '123 Test St, Suite 100';
                        } else {
                            masked[key] = row[key];
                        }
                    });
                    rows.push(masked);
                } else {
                    rows.push(row);
                }
            });

            return {
                success: true,
                columns: columns,
                data: rows,
                rowCount: rows.length,
                masked: maskPII,
                remainingUsage: script.getRemainingUsage(),
            };
        } catch (e) {
            log.error('MockData Error', e.message);
            return { success: false, error: e.name, message: e.message };
        }
    };

    return { post };
});
```

Add corresponding script record XML:
**Create:** `suiteapp/src/Objects/customscript_ecom_mockdata_rl.xml`

```xml
<?xml version="1.0" encoding="UTF-8"?>
<restlet scriptid="customscript_ecom_mockdata_rl">
    <name>Ecom MockData RESTlet</name>
    <notifyadmins>F</notifyadmins>
    <isinactive>F</isinactive>
    <description>SuiteQL mock data extraction with PII masking for offline unit testing.</description>
    <scriptfile>[/SuiteScripts/ecom_mock_data_restlet.js]</scriptfile>
    <loglevel>DEBUG</loglevel>
    <scriptdeployments>
        <scriptdeployment scriptid="customdeploy_ecom_mockdata_rl">
            <title>Ecom MockData RESTlet Deploy</title>
            <status>RELEASED</status>
            <loglevel>DEBUG</loglevel>
            <allroles>F</allroles>
            <audslctrole>
                <role>ADMINISTRATOR</role>
            </audslctrole>
        </scriptdeployment>
    </scriptdeployments>
</restlet>
```

### Final SuiteApp Directory Structure

```
suiteapp/
├── manifest.xml
├── deploy.xml
├── suitecloud.config.js
├── package.json                            (for SuiteCloud SDK + Jest)
├── src/
│   ├── FileCabinet/
│   │   └── SuiteScripts/
│   │       ├── ecom_file_cabinet_restlet.js
│   │       ├── ecom_mock_data_restlet.js
│   │       └── ecom_bundle_install.js
│   └── Objects/
│       ├── customscript_ecom_filecabinet_rl.xml
│       └── customscript_ecom_mockdata_rl.xml
├── __tests__/
│   ├── ecom_file_cabinet_restlet.test.js
│   └── ecom_mock_data_restlet.test.js
└── jest.config.js
```

---

## Agent 2: Backend Engineer — Replace REST API with RESTlet Calls

### Task 2A: Create RESTlet client service

**Create:** `backend/app/services/netsuite_restlet_client.py`

This replaces the raw REST API `GET/PATCH /file/{id}` calls in `suitescript_sync.py` with calls to the deployed RESTlet.

```python
"""NetSuite RESTlet client — calls the Ecom FileCabinet RESTlet for file I/O."""

from __future__ import annotations

import httpx
import structlog
from app.services.netsuite_client import _normalize_account_id

logger = structlog.get_logger()


def _restlet_url(account_id: str, script_id: str, deploy_id: str) -> str:
    """Build RESTlet URL from account + deployment IDs."""
    slug = _normalize_account_id(account_id)
    return (
        f"https://{slug}.restlets.api.netsuite.com/app/site/hosting/restlet.nl"
        f"?script={script_id}&deploy={deploy_id}"
    )


# Script/deploy IDs — will be configurable via Connection metadata later
FILECABINET_SCRIPT_ID = "customscript_ecom_filecabinet_rl"
FILECABINET_DEPLOY_ID = "customdeploy_ecom_filecabinet_rl"
MOCKDATA_SCRIPT_ID = "customscript_ecom_mockdata_rl"
MOCKDATA_DEPLOY_ID = "customdeploy_ecom_mockdata_rl"


async def restlet_read_file(
    access_token: str,
    account_id: str,
    file_id: int,
    timeout: int = 15,
) -> dict:
    """Read a file from NetSuite File Cabinet via RESTlet GET."""
    url = _restlet_url(account_id, FILECABINET_SCRIPT_ID, FILECABINET_DEPLOY_ID)
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    params = {"fileId": str(file_id)}

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
    url = _restlet_url(account_id, FILECABINET_SCRIPT_ID, FILECABINET_DEPLOY_ID)
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    payload = {"fileId": file_id, "content": content}

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.put(url, headers=headers, json=payload)
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
    url = _restlet_url(account_id, FILECABINET_SCRIPT_ID, FILECABINET_DEPLOY_ID)
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    payload = {
        "name": name,
        "folder": folder_id,
        "content": content,
        "fileType": file_type,
    }

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, headers=headers, json=payload)
        resp.raise_for_status()

    data = resp.json()
    if not data.get("success"):
        raise RuntimeError(f"RESTlet error: {data.get('message', 'Unknown error')}")
    return data


async def restlet_extract_mock_data(
    access_token: str,
    account_id: str,
    suiteql_query: str,
    limit: int = 100,
    mask_pii: bool = True,
    timeout: int = 30,
) -> dict:
    """Extract mock data via the MockData RESTlet with PII masking."""
    url = _restlet_url(account_id, MOCKDATA_SCRIPT_ID, MOCKDATA_DEPLOY_ID)
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    payload = {
        "query": suiteql_query,
        "limit": limit,
        "maskPII": mask_pii,
    }

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, headers=headers, json=payload)
        resp.raise_for_status()

    data = resp.json()
    if not data.get("success"):
        raise RuntimeError(f"RESTlet error: {data.get('message', 'Unknown error')}")
    return data
```

### Task 2B: Update `suitescript_sync.py` pull endpoint

**File:** `backend/app/api/v1/suitescript_sync.py`

Replace the raw REST API file read (lines 140-182) with the RESTlet client. The key change is in `pull_single_file`:

Replace:
```python
    ns_file_id = workspace_file.netsuite_file_id
    slug = _normalize_account_id(account_id)
    url = f"https://{slug}.suitetalk.api.netsuite.com/services/rest/record/v1/file/{ns_file_id}"
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    # ... httpx call, base64 decode, etc.
```

With:
```python
    from app.services.netsuite_restlet_client import restlet_read_file

    ns_file_id = workspace_file.netsuite_file_id
    try:
        result = await restlet_read_file(access_token, account_id, ns_file_id)
    except Exception as exc:
        await log_netsuite_request(
            db=db, tenant_id=user.tenant_id, connection_id=connection.id,
            method="GET", url=f"RESTlet:filecabinet:read:{ns_file_id}",
            error_message=str(exc), source="single_file_pull",
        )
        await db.commit()
        raise HTTPException(status_code=502, detail=f"NetSuite RESTlet error: {str(exc)[:200]}")

    content = result["content"]
    workspace_file.content = content
    workspace_file.size_bytes = len(content.encode("utf-8"))
```

### Task 2C: Update `suitescript_sync.py` push endpoint

Replace the `PATCH /file/{id}` call (lines 230-270) with RESTlet PUT:

```python
    from app.services.netsuite_restlet_client import restlet_write_file

    ns_file_id = workspace_file.netsuite_file_id
    content = workspace_file.content
    try:
        result = await restlet_write_file(access_token, account_id, ns_file_id, content)
    except Exception as exc:
        await log_netsuite_request(
            db=db, tenant_id=user.tenant_id, connection_id=connection.id,
            method="PUT", url=f"RESTlet:filecabinet:write:{ns_file_id}",
            error_message=str(exc), source="single_file_push",
        )
        await db.commit()
        raise HTTPException(status_code=502, detail=f"NetSuite RESTlet error: {str(exc)[:200]}")

    # Note: file ID may change after delete+recreate — update reference
    new_file_id = result.get("fileId")
    if new_file_id and new_file_id != ns_file_id:
        workspace_file.netsuite_file_id = str(new_file_id)
```

**Important**: The RESTlet PUT does delete+recreate, which changes the file ID. The workspace file's `netsuite_file_id` must be updated to track the new ID.

### Task 2D: Add mock data extraction API endpoint

**File:** `backend/app/api/v1/suitescript_sync.py` — add new endpoint:

```python
class MockDataRequest(BaseModel):
    query: str
    limit: int = 100
    mask_pii: bool = True


@router.post("/mock-data")
async def extract_mock_data(
    body: MockDataRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Extract sanitized mock data from NetSuite via SuiteQL for unit testing."""
    from app.services.netsuite_restlet_client import restlet_extract_mock_data

    connection, access_token, account_id = await _get_netsuite_creds(db, user.tenant_id)

    try:
        result = await restlet_extract_mock_data(
            access_token=access_token,
            account_id=account_id,
            suiteql_query=body.query,
            limit=body.limit,
            mask_pii=body.mask_pii,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Mock data extraction failed: {str(exc)[:200]}")

    return {
        "status": "ok",
        "columns": result.get("columns", []),
        "data": result.get("data", []),
        "row_count": result.get("rowCount", 0),
        "masked": result.get("masked", True),
    }
```

### Task 2E: Add RESTlet deployment detection to connection test

**File:** `backend/app/services/connection_service.py`

In the `test_connection` function, add a check that the FileCabinet RESTlet is deployed:

```python
async def test_connection(db, connection_id, tenant_id):
    # ... existing OAuth token test ...

    # Also test RESTlet availability
    from app.services.netsuite_restlet_client import restlet_read_file
    restlet_ok = False
    restlet_error = None
    try:
        # Try reading a non-existent file — should get a controlled error, not 404/403
        result = await restlet_read_file(access_token, account_id, file_id=1)
        restlet_ok = True
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 403:
            restlet_error = "RESTlet not deployed or role lacks access"
        elif e.response.status_code == 404:
            restlet_error = "RESTlet script not found — install the Ecom bundle"
        else:
            restlet_error = f"RESTlet HTTP {e.response.status_code}"
    except Exception as e:
        restlet_error = str(e)[:200]

    return {
        "success": oauth_ok,
        "oauth_status": "valid" if oauth_ok else "failed",
        "restlet_status": "available" if restlet_ok else "not_available",
        "restlet_error": restlet_error,
        "message": "Connection healthy" if (oauth_ok and restlet_ok) else "Partial — see details",
    }
```

---

## Agent 3: Backend Engineer — SuiteCloud Unit Testing Setup

### Task 3A: Create Jest config for SuiteScript

**Create:** `suiteapp/jest.config.js`

```javascript
module.exports = {
    // SuiteCloud Unit Testing Framework stubs
    moduleNameMapper: {
        '^N/(.*)$': '<rootDir>/node_modules/@oracle/suitecloud-unit-testing/stubs/N/$1',
    },
    testMatch: ['**/__tests__/**/*.test.js'],
    testEnvironment: 'node',
    transform: {},
    verbose: true,
};
```

### Task 3B: Create package.json for SuiteApp

**Create:** `suiteapp/package.json`

```json
{
    "name": "ecom-netsuite-suiteapp",
    "version": "1.0.0",
    "description": "SuiteScript RESTlets + unit tests for ecom-netsuite-suites",
    "private": true,
    "scripts": {
        "test": "jest",
        "test:watch": "jest --watch",
        "test:coverage": "jest --coverage",
        "validate": "suitecloud project:validate",
        "deploy": "suitecloud project:deploy"
    },
    "devDependencies": {
        "@oracle/suitecloud-unit-testing": "^1.3.0",
        "@oracle/suitecloud-cli": "^1.9.0",
        "jest": "^29.7.0"
    }
}
```

### Task 3C: Write unit tests for FileCabinet RESTlet

**Create:** `suiteapp/__tests__/ecom_file_cabinet_restlet.test.js`

```javascript
const SuiteCloudJestStubs = require('@oracle/suitecloud-unit-testing/SuiteCloudJestStubs');
const SuiteCloudJestConfiguration = require('@oracle/suitecloud-unit-testing/jest-configuration/SuiteCloudJestConfiguration');

describe('ecom_file_cabinet_restlet', () => {
    let restlet;

    beforeAll(() => {
        // Configure SuiteCloud stubs
        SuiteCloudJestStubs.install();
    });

    beforeEach(() => {
        jest.resetModules();
        // The define() wrapper needs the stubs loaded first
        restlet = require('../src/FileCabinet/SuiteScripts/ecom_file_cabinet_restlet');
    });

    describe('GET', () => {
        test('returns file content when fileId provided', () => {
            const file = require('N/file');
            file.load.mockReturnValue({
                id: 42,
                name: 'test_script.js',
                folder: 100,
                getContents: () => 'console.log("hello");',
                size: 22,
                fileType: 'JAVASCRIPT',
                dateCreated: new Date('2024-01-01'),
            });

            const result = restlet.get({ fileId: '42' });

            expect(result.success).toBe(true);
            expect(result.fileId).toBe(42);
            expect(result.content).toBe('console.log("hello");');
            expect(file.load).toHaveBeenCalledWith({ id: 42 });
        });

        test('returns error when no params provided', () => {
            const result = restlet.get({});
            expect(result.success).toBe(false);
            expect(result.error).toBe('MISSING_PARAM');
        });
    });

    describe('POST', () => {
        test('creates file and returns new ID', () => {
            const file = require('N/file');
            const mockFile = { save: jest.fn().mockReturnValue(99) };
            file.create.mockReturnValue(mockFile);
            file.Type = { JAVASCRIPT: 'JAVASCRIPT' };

            const result = restlet.post({
                name: 'new_script.js',
                folder: 100,
                content: '// new file',
            });

            expect(result.success).toBe(true);
            expect(result.fileId).toBe(99);
            expect(file.create).toHaveBeenCalledWith(
                expect.objectContaining({ name: 'new_script.js', folder: 100 })
            );
        });

        test('returns error when fields missing', () => {
            const result = restlet.post({ name: 'test.js' });
            expect(result.success).toBe(false);
        });
    });

    describe('PUT', () => {
        test('deletes and recreates file with new content', () => {
            const file = require('N/file');
            file.load.mockReturnValue({
                name: 'existing.js',
                folder: 100,
                fileType: 'JAVASCRIPT',
                description: 'test',
            });
            file.delete = jest.fn();
            const mockFile = { save: jest.fn().mockReturnValue(101) };
            file.create.mockReturnValue(mockFile);

            const result = restlet.put({ fileId: 42, content: '// updated' });

            expect(result.success).toBe(true);
            expect(result.fileId).toBe(101);
            expect(result.previousFileId).toBe(42);
            expect(file.delete).toHaveBeenCalledWith({ id: 42 });
        });
    });
});
```

### Task 3D: Write unit tests for MockData RESTlet

**Create:** `suiteapp/__tests__/ecom_mock_data_restlet.test.js`

```javascript
describe('ecom_mock_data_restlet', () => {
    let restlet;

    beforeEach(() => {
        jest.resetModules();
        restlet = require('../src/FileCabinet/SuiteScripts/ecom_mock_data_restlet');
    });

    test('masks PII fields by default', () => {
        const query = require('N/query');
        query.runSuiteQL.mockReturnValue({
            columns: [
                { label: 'id', index: 0 },
                { label: 'email', index: 1 },
                { label: 'companyname', index: 2 },
            ],
            asMappedResults: () => [
                { id: 1, email: 'real@company.com', companyname: 'Acme Corp' },
                { id: 2, email: 'other@company.com', companyname: 'Beta Inc' },
            ],
        });

        const result = restlet.post({ query: 'SELECT id, email, companyname FROM customer' });

        expect(result.success).toBe(true);
        expect(result.masked).toBe(true);
        expect(result.data[0].email).toBe('test_0@example.com');
        expect(result.data[0].companyname).toBe('Test_Entity_0');
        expect(result.data[0].id).toBe(1); // Non-PII preserved
    });

    test('preserves real data when maskPII=false', () => {
        const query = require('N/query');
        query.runSuiteQL.mockReturnValue({
            columns: [{ label: 'id', index: 0 }, { label: 'email', index: 1 }],
            asMappedResults: () => [{ id: 1, email: 'real@company.com' }],
        });

        const result = restlet.post({
            query: 'SELECT id, email FROM customer',
            maskPII: false,
        });

        expect(result.data[0].email).toBe('real@company.com');
        expect(result.masked).toBe(false);
    });

    test('returns error when query missing', () => {
        const result = restlet.post({});
        expect(result.success).toBe(false);
    });
});
```

---

## Agent 4: Frontend Engineer — Mock Data UI + Connection RESTlet Status

### Task 4A: Add RESTlet status to connection test UI

**File:** `frontend/src/app/(dashboard)/settings/page.tsx`

In the NetSuite connection card, after the existing "Test Connection" button result, show RESTlet deployment status:

```tsx
{testResult && (
    <div className="mt-2 space-y-1 text-sm">
        <div className="flex items-center gap-2">
            <span className={testResult.oauth_status === 'valid' ? 'text-green-600' : 'text-red-600'}>●</span>
            <span>OAuth: {testResult.oauth_status}</span>
        </div>
        <div className="flex items-center gap-2">
            <span className={testResult.restlet_status === 'available' ? 'text-green-600' : 'text-yellow-600'}>●</span>
            <span>RESTlet: {testResult.restlet_status}</span>
            {testResult.restlet_error && (
                <span className="text-muted-foreground text-xs">— {testResult.restlet_error}</span>
            )}
        </div>
    </div>
)}
```

### Task 4B: Create mock data extraction hook

**Create:** `frontend/src/hooks/use-mock-data.ts`

```typescript
import { useState, useCallback } from "react";
import { useAuth } from "@/hooks/use-auth";

interface MockDataResult {
    columns: string[];
    data: Record<string, unknown>[];
    row_count: number;
    masked: boolean;
}

export function useMockData() {
    const { session } = useAuth();
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const [result, setResult] = useState<MockDataResult | null>(null);

    const extractMockData = useCallback(async (
        query: string,
        limit = 100,
        maskPII = true,
    ) => {
        if (!session?.access_token) return;
        setLoading(true);
        setError(null);

        try {
            const resp = await fetch("/api/v1/netsuite/scripts/mock-data", {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                    Authorization: `Bearer ${session.access_token}`,
                },
                body: JSON.stringify({ query, limit, mask_pii: maskPII }),
            });

            if (!resp.ok) {
                const body = await resp.json().catch(() => ({}));
                throw new Error(body.detail || `HTTP ${resp.status}`);
            }

            const data = await resp.json();
            setResult(data);
            return data;
        } catch (e) {
            const msg = e instanceof Error ? e.message : "Unknown error";
            setError(msg);
            return null;
        } finally {
            setLoading(false);
        }
    }, [session?.access_token]);

    return { extractMockData, loading, error, result };
}
```

### Task 4C: Add mock data panel to workspace

In the workspace bottom panel tabs (alongside Chat, Logs, Changes, Runs), add a "Test Data" tab that lets users:

1. Enter a SuiteQL query
2. Toggle PII masking on/off
3. Execute and view results in a table
4. Export as JSON fixture file to the workspace

This is a UI-only task — the backend endpoint and hook are already created above. Use a simple textarea + table pattern.

---

## Agent 5: QA Engineer — Verification

### Task 5A: SuiteApp structure validation

```bash
# Verify SuiteApp directory structure
ls -la suiteapp/src/FileCabinet/SuiteScripts/
ls -la suiteapp/src/Objects/
cat suiteapp/manifest.xml
cat suiteapp/deploy.xml
```

### Task 5B: SuiteScript syntax check

```bash
cd suiteapp
# Install dependencies
npm install
# Run Jest tests
npm test
```

### Task 5C: Backend syntax check

```bash
cd backend
python -c "
import ast, pathlib
errors = []
for f in pathlib.Path('app').rglob('*.py'):
    try: ast.parse(f.read_text())
    except SyntaxError as e: errors.append(f'{f}: {e}')
print(f'{len(errors)} errors')
for e in errors: print(e)
"
```

### Task 5D: Frontend TypeScript check

```bash
cd frontend && npx tsc --noEmit
```

### Task 5E: Integration verification

1. **RESTlet client imports**: Verify `netsuite_restlet_client.py` is imported correctly in `suitescript_sync.py`
2. **File ID update on push**: Verify that when RESTlet PUT returns a new `fileId`, the workspace file's `netsuite_file_id` is updated
3. **Mock data endpoint**: Verify `POST /api/v1/netsuite/scripts/mock-data` is accessible via the router
4. **RESTlet URL construction**: Verify `_restlet_url()` uses `.restlets.api.netsuite.com` (not `.suitetalk.api.netsuite.com`)

---

## Agent 6: Project Lead — Summary

### Architecture Overview

```
┌─────────────────────────────────────────────────────┐
│                  Our Application                      │
│                                                       │
│  Frontend (Next.js)                                   │
│    ├── Workspace IDE (file tree, tabbed editors)      │
│    ├── Chat Panel (AI assistant)                      │
│    ├── Test Data Panel (SuiteQL → mock fixtures)      │
│    └── Connection Settings (OAuth, RESTlet status)    │
│                                                       │
│  Backend (FastAPI)                                    │
│    ├── netsuite_restlet_client.py  ← NEW              │
│    │     ├── restlet_read_file()                      │
│    │     ├── restlet_write_file()                     │
│    │     ├── restlet_create_file()                    │
│    │     └── restlet_extract_mock_data()              │
│    ├── suitescript_sync.py (pull/push via RESTlet)    │
│    └── connection_service.py (test includes RESTlet)  │
└──────────────┬────────────────────────────────────────┘
               │ OAuth 2.0 Bearer Token
               ▼
┌─────────────────────────────────────────────────────┐
│           Customer NetSuite Account                   │
│                                                       │
│  Ecom Bundle (installed via SuiteBundler)              │
│    ├── ecom_file_cabinet_restlet.js                   │
│    │     GET  → Read file by ID/path                  │
│    │     POST → Create new file                       │
│    │     PUT  → Update file (delete+recreate)         │
│    │     DEL  → Delete file                           │
│    ├── ecom_mock_data_restlet.js                      │
│    │     POST → SuiteQL query with PII masking        │
│    └── ecom_bundle_install.js                         │
│           afterInstall → Log setup + notify admin     │
│           beforeUpdate → Version migration            │
│                                                       │
│  Script Records + Deployments (via SDF Objects/)      │
│    ├── customscript_ecom_filecabinet_rl               │
│    │     └── customdeploy_ecom_filecabinet_rl         │
│    └── customscript_ecom_mockdata_rl                  │
│          └── customdeploy_ecom_mockdata_rl            │
└─────────────────────────────────────────────────────┘
```

### Key Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| File I/O method | RESTlet (N/file) | REST API PATCH returns 405; RESTlet is the official solution |
| File update strategy | Delete + recreate | N/file doesn't support in-place content update |
| Distribution | SuiteBundler | One-click install for customers, versioned updates |
| Mock data | RESTlet + SuiteQL | Runs inside NetSuite governance, PII masking at source |
| PII masking | Server-side (RESTlet) | Never transmit real PII to our backend |
| Unit testing | Jest + @oracle/suitecloud-unit-testing | Oracle-official stubs for all N/ modules |
| Project type | ACCOUNTCUSTOMIZATION | Simpler than SUITEAPP for internal deployment |

### Known Limitations

1. **File ID changes on update**: RESTlet PUT does delete+recreate, so the NetSuite file ID changes. Backend must track the new ID.
2. **RESTlet governance**: 5,000 units per call. Large file operations should be batched.
3. **Bundle installation**: Requires manual install from SuiteBundler UI in customer account (can't auto-install).
4. **SuiteQL in RESTlet**: Uses `N/query` module (not REST API), different syntax for `FETCH FIRST N ROWS ONLY` vs `ROWNUM`.

### Deferred Items

- SDF `suitecloud project:deploy` CI/CD integration (future sprint)
- Auto-detect RESTlet script/deploy IDs from Connection metadata
- Bundle versioning strategy for customer updates
- RESTlet rate limiting / retry logic in backend client

---
description: SuiteScript 2.x + RESTlet + SuiteQL conventions. Loads when editing the SuiteApp.
paths:
  - suiteapp/**
---

# SuiteScript rules

1. **Don't use `WidthType.PERCENTAGE`** in docx — use DXA.
2. **RESTlet PUT preserves file IDs** — in-place load → set `.contents` → `.save()`.
3. **SuiteQL pagination** — `FETCH FIRST N ROWS ONLY`, not `LIMIT`.
4. **NetSuite account IDs** — normalize with `replace("_", "-").lower()` for URLs.
5. **SuiteQL status codes** — REST returns single-letter (`'B'`, `'H'`), NOT compound (`'SalesOrd:B'`). Compound codes silently fail. RMA received = `status IN ('D','E','F','G','H')`. See `knowledge/golden_dataset/transaction-types-and-statuses.md`.

## RESTlet template

```javascript
/**
 * @NApiVersion 2.1
 * @NScriptType Restlet
 * @NModuleScope SameAccount
 */
define(['N/file', 'N/log', 'N/runtime', 'N/error'], (file, log, runtime, error) => {
    const get = (requestParams) => {
        try {
            const script = runtime.getCurrentScript();
            log.debug('Operation', JSON.stringify(requestParams));
            // ... logic
            return { success: true, data: result, remainingUsage: script.getRemainingUsage() };
        } catch (e) {
            log.error('Error', e.message);
            return { success: false, error: e.name, message: e.message };
        }
    };
    return { get };
});
```

**Rules:**
- Always return `{ success: true/false }` envelope
- Always log with `N/log` (debug for info, audit for mutations, error for failures)
- Always report `remainingUsage` for governance monitoring
- Always wrap in try/catch — RESTlets must not throw unhandled errors

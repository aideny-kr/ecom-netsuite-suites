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
